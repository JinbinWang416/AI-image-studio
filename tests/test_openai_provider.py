# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import json
import unittest

import httpx

from app.providers.base import GenerateRequest, ProviderError
from app.providers.openai import OpenAIImageProvider


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4////fwAJ+wP+8s6d0QAAAABJRU5ErkJggg=="
)


class OpenAIImageProviderTests(unittest.IsolatedAsyncioTestCase):
    async def _provider(self, handler) -> OpenAIImageProvider:
        provider = OpenAIImageProvider(
            api_key="test-key", base_url="https://api.openai.com/v1",
            model="gpt-image-2.5-flare", timeout=1,
        )
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(provider.close)
        return provider

    async def test_request_uses_official_png_shape_without_seed(self) -> None:
        async def handler(request):
            self.assertEqual(request.url.path, "/v1/images/generations")
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            body = json.loads(request.content.decode("utf-8"))
            self.assertEqual(body["model"], "gpt-image-2.5-flare")
            self.assertEqual(body["size"], "1024x1024")
            self.assertEqual(body["quality"], "max")
            self.assertEqual(body["output_format"], "png")
            self.assertEqual(body["background"], "opaque")
            self.assertNotIn("seed", body)
            return httpx.Response(200, json={"created": 1, "data": [{"b64_json": base64.b64encode(PNG).decode(), "revised_prompt": "ok"}]})

        provider = await self._provider(handler)
        result = await provider.generate(GenerateRequest(prompt="房屋中介", seed=42))
        self.assertEqual(result.images, [PNG])
        self.assertEqual(result.raw["revised_prompts"], ["ok"])

    async def test_auth_content_and_rate_errors_are_classified(self) -> None:
        cases = [
            (401, {"error": {"message": "bad key"}}, "AUTH", False),
            (429, {"error": {"message": "too many requests"}}, "RATE_LIMIT", True),
            (400, {"error": {"message": "content policy violation"}}, "CONTENT_POLICY", False),
        ]
        for status, payload, code, retryable in cases:
            async def handler(request, status=status, payload=payload):
                return httpx.Response(status, json=payload)
            provider = await self._provider(handler)
            with self.assertRaises(ProviderError) as ctx:
                await provider.generate(GenerateRequest(prompt="x"))
            self.assertEqual(ctx.exception.code, code)
            self.assertEqual(ctx.exception.retryable, retryable)
            await provider.close()

    async def test_timeout_and_non_png_response(self) -> None:
        async def timeout_handler(request):
            raise httpx.ReadTimeout("slow", request=request)
        provider = await self._provider(timeout_handler)
        with self.assertRaises(ProviderError) as ctx:
            await provider.generate(GenerateRequest(prompt="x"))
        self.assertEqual(ctx.exception.code, "TIMEOUT")
        self.assertTrue(ctx.exception.retryable)
        await provider.close()

        async def non_png_handler(request):
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"not png").decode()}]})
        provider = await self._provider(non_png_handler)
        with self.assertRaises(ProviderError) as ctx:
            await provider.generate(GenerateRequest(prompt="x"))
        self.assertEqual(ctx.exception.code, "NON_PNG")
        self.assertFalse(ctx.exception.retryable)

    async def test_reference_images_use_multipart_edits_endpoint(self) -> None:
        reference = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")

        async def handler(request):
            self.assertEqual(request.url.path, "/v1/images/edits")
            self.assertIn("multipart/form-data", request.headers["content-type"])
            self.assertIn(b'name="image[]"', request.content)
            self.assertIn(b'name="model"', request.content)
            self.assertIn(b"gpt-image-2.5-flare", request.content)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

        provider = await self._provider(handler)
        result = await provider.generate(GenerateRequest(
            prompt="房屋中介彩色贴纸", size="1024x1280",
            extra={"reference_images": [reference]},
        ))
        self.assertEqual(result.images, [PNG])
        self.assertEqual(result.raw["image_mode"], "image")
        self.assertEqual(result.raw["reference_count"], 1)


if __name__ == "__main__":
    unittest.main()

