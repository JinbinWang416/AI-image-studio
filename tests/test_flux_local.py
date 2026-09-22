# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.providers.base import GenerateRequest, ProviderError
from app.providers.flux_local import FluxLocalProvider


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4////fwAJ+wP+8s6d0QAAAABJRU5ErkJggg=="
)


class FluxLocalProviderTests(unittest.IsolatedAsyncioTestCase):
    async def _provider(self, handler) -> FluxLocalProvider:
        provider = FluxLocalProvider(base_url="http://127.0.0.1:8189", model="FLUX.2-klein-4b", timeout=1)
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(provider.close)
        return provider

    async def test_service_unavailable_is_not_retryable(self) -> None:
        async def handler(request):
            raise httpx.ConnectError("offline", request=request)
        provider = await self._provider(handler)
        health = await provider.health()
        self.assertFalse(health["ok"])
        self.assertEqual(health["status"], "unavailable")
        with self.assertRaises(ProviderError) as ctx:
            await provider.generate(GenerateRequest(prompt="x"))
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.code, "UNAVAILABLE")

    async def test_cuda_oom_is_not_retryable(self) -> None:
        async def handler(request):
            return httpx.Response(507, json={"code": "CUDA_OOM", "message": "oom"})
        provider = await self._provider(handler)
        with self.assertRaises(ProviderError) as ctx:
            await provider.generate(GenerateRequest(prompt="x"))
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.code, "CUDA_OOM")
        self.assertIn("显存", str(ctx.exception))

    async def test_timeout_is_not_retryable(self) -> None:
        async def handler(request):
            raise httpx.ReadTimeout("slow", request=request)
        provider = await self._provider(handler)
        with self.assertRaises(ProviderError) as ctx:
            await provider.generate(GenerateRequest(prompt="x"))
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.code, "TIMEOUT")

    async def test_non_png_is_not_retryable(self) -> None:
        async def handler(request):
            return httpx.Response(200, json={"image_base64": base64.b64encode(b"not png").decode()})
        provider = await self._provider(handler)
        with self.assertRaises(ProviderError) as ctx:
            await provider.generate(GenerateRequest(prompt="x"))
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.code, "NON_PNG")


class FluxRuntimeInstallTests(unittest.TestCase):
    def test_missing_model_files_have_actionable_error(self) -> None:
        from app import local_flux_service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(local_flux_service, "MODEL_PATH", root / "missing-model.safetensors"), \
                 patch.object(local_flux_service, "AE_PATH", root / "missing-ae.safetensors"):
                runtime = local_flux_service.FluxRuntime()
                with self.assertRaisesRegex(RuntimeError, "模型权重缺失"):
                    runtime._require_files()


if __name__ == "__main__":
    unittest.main()
