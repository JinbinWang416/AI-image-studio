# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.prompt_optimizer import DeepSeekPromptOptimizer, PromptOptimizerError
from app.settings import SettingsStore


class _MockClient:
    def __init__(self, handler):
        self.handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def post(self, url, headers, json):
        request = httpx.Request("POST", url, headers=headers, content=__import__("json").dumps(json).encode())
        return await self.handler(request)


class DeepSeekOptimizerTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_is_sent_to_official_chat_endpoint_and_titles_are_preserved(self) -> None:
        async def handler(request):
            self.assertEqual(request.url.path, "/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer test-key")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "deepseek-flash")
            self.assertFalse(payload["stream"])
            return httpx.Response(200, json={
                "model": "deepseek-flash",
                "choices": [{"message": {"content": "彩色异形商业贴纸，主标题 房屋中介，副标题 专业房产咨询，蓝金色楼宇和钥匙，白色边缘"}}],
            })

        optimizer = DeepSeekPromptOptimizer("test-key", "https://api.deepseek.com", "deepseek-flash")
        with patch("httpx.AsyncClient", return_value=_MockClient(handler)):
            result = await optimizer.optimize(
                prompt="原提示词", main_title="房屋中介", sub_title="专业房产咨询", variation="中心构图",
            )
        self.assertIn("房屋中介", result.prompt)
        self.assertIn("专业房产咨询", result.prompt)

    async def test_changed_required_text_and_auth_error_are_rejected(self) -> None:
        async def changed(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": "这是一段足够长但遗漏标题的彩色商业贴纸提示词，用于测试文案保护规则。"}}]})
        optimizer = DeepSeekPromptOptimizer("key", "https://api.deepseek.com", "deepseek-flash")
        with patch("httpx.AsyncClient", return_value=_MockClient(changed)):
            with self.assertRaises(PromptOptimizerError):
                await optimizer.optimize(prompt="x", main_title="房屋中介", sub_title="专业房产咨询", variation="x")

        async def unauthorized(request):
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        with patch("httpx.AsyncClient", return_value=_MockClient(unauthorized)):
            with self.assertRaisesRegex(PromptOptimizerError, "无效"):
                await optimizer.optimize(prompt="x", main_title="房屋中介", sub_title="专业房产咨询", variation="x")

    def test_optimizer_key_is_masked_and_mask_roundtrips(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = SettingsStore(Path(folder) / "settings.json")
            expected = "test-deepseek-key-12345678"
            settings.save({"prompt_optimizer": {"api_key": expected}})
            masked = settings.masked()["prompt_optimizer"]["api_key"]
            self.assertIn("****", masked)
            settings.save({"prompt_optimizer": {"api_key": masked}})
            self.assertEqual(settings.load()["prompt_optimizer"]["api_key"], expected)


if __name__ == "__main__":
    unittest.main()
