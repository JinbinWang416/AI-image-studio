# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from PIL import Image

from app.state.batches import create_new_batch, load_batch_snapshot
from app.config import Config, make_config
from app.core.models import Job
from app.prompt.profiles import DEFAULT_QUALITY_TEMPLATE, build_effective_prompt, realism_requirement, validate_template
from app.providers.base import GenerateRequest, ProviderError
from app.providers.gemini import GeminiImageProvider
from app.providers.qwen import QwenProvider
from app.providers.seedream import SeedreamProvider
from app.state.assets import EffectBackgroundStore, ReferenceAssetError, ReferenceAssetStore, validate_mode_assets
from app.state.settings_store import SettingsStore
from app.web import server


def png_bytes(color: str = "#2563eb") -> bytes:
    image = Image.new("RGB", (8, 8), color)
    data = io.BytesIO()
    image.save(data, format="PNG")
    return data.getvalue()


PNG = png_bytes()
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")


class ScopeAndReferenceTests(unittest.TestCase):
    def test_reference_asset_is_deduplicated_and_mode_checked(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            assets = ReferenceAssetStore(Path(folder))
            first = assets.add_data_url(DATA_URL, "门店参考.png")
            second = assets.add_data_url(DATA_URL, "重复文件.png")
            self.assertEqual(first.id, second.id)
            self.assertEqual(len(assets.list()), 1)
            public = first.public()
            self.assertNotIn("data_url", public)
            validate_mode_assets("image", [first])
            validate_mode_assets("multi", [first, first])
            with self.assertRaises(ReferenceAssetError):
                validate_mode_assets("text", [first])
            with self.assertRaises(ReferenceAssetError):
                assets.add_data_url("data:image/png;base64,bad", "bad.png")

    def test_effect_background_asset_is_isolated_from_image_references(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            image_assets = ReferenceAssetStore(Path(folder))
            effect_assets = EffectBackgroundStore(Path(folder))
            ref = image_assets.add_data_url(DATA_URL, "风格参考.png")
            effect = effect_assets.add_data_url(DATA_URL, "门店实拍.png")
            self.assertEqual(ref.id, effect.id)
            self.assertNotEqual(image_assets.root, effect_assets.root)
            self.assertTrue((image_assets.root / f"{ref.id}.png").is_file())
            self.assertTrue((effect_assets.root / f"{effect.id}.png").is_file())

    def test_prompt_template_locks_chinese_titles_and_rendered_values(self) -> None:
        job = Job(
            job_id="01-01", store_index="01", store_name="房屋中介门店",
            output_dir="01_房屋中介门店", pic_index="01", theme="楼房线稿",
            file_name="01_楼房线稿.png", positive_prompt="原始行业提示词", negative_prompt="",
            simple_prompt="", expected_text=["房屋中介", "房源咨询 欢迎到店"],
            main_title="房屋中介", sub_title="房源咨询 欢迎到店", color_theme="蓝金白色",
            subject="楼房与钥匙",
        )
        prompt = build_effective_prompt(job, DEFAULT_QUALITY_TEMPLATE, "1024x1280")
        self.assertIn("房屋中介", prompt)
        self.assertIn("房源咨询 欢迎到店", prompt)
        self.assertIn("楼房与钥匙", prompt)
        self.assertNotIn("{{subject}}", prompt)
        self.assertIn("真实感 V2", build_effective_prompt(job, DEFAULT_QUALITY_TEMPLATE, "1024x1280", 2))
        self.assertIn("专业门店成片标准", realism_requirement(99))
        with self.assertRaises(Exception):
            validate_template("这是一个缺少全部占位符的质量模板，长度虽然够但不能使用。")

    def test_scope_snapshot_is_ordered_immutable_and_safe(self) -> None:
        repo = server._repo()
        selected = server._ordered_store_indexes(repo, ["03", "01"])
        self.assertEqual(selected, ["01", "03"])
        cfg = make_config(
            provider="mock", provider_label="本地模拟", model="mock-v1",
            output_base_root=Path(tempfile.gettempdir()) / "scope-snapshot-test",
            output_root=Path(tempfile.gettempdir()) / "scope-snapshot-test",
            selected_store_indexes=selected, size="1024x1280",
            image_mode="text", quality_template=DEFAULT_QUALITY_TEMPLATE,
        )
        snapshot = server._new_batch_snapshot(cfg, selected)
        self.assertEqual(snapshot["store_indexes"], ["01", "03"])
        self.assertEqual(snapshot["generation"]["size"], "1024x1280")
        self.assertNotIn("data_url", json.dumps(snapshot, ensure_ascii=False))
        self.assertEqual(snapshot["effect_workflow"]["realism_iteration"], 1)
        with tempfile.TemporaryDirectory() as folder:
            cfg.output_base_root = Path(folder)
            cfg.output_root = Path(folder)
            batch = create_new_batch(cfg, SettingsStore(Path(folder) / "settings.json"), snapshot)
            saved = load_batch_snapshot(batch.path)["run_snapshot"]
            self.assertEqual(saved, snapshot)


class ProviderReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_qwen_reference_uses_openai_image_extension(self) -> None:
        async def handler(request):
            self.assertEqual(request.url.path, "/compatible-mode/v1/images/generations")
            payload = json.loads(request.content)
            self.assertEqual(payload["image"], DATA_URL)
            self.assertFalse(payload["prompt_extend"])
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode("ascii")} ]})

        provider = QwenProvider(api_key="key", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen-image-3.0")
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(provider.close)
        result = await provider.generate(GenerateRequest(prompt="测试", extra={"reference_images": [DATA_URL]}))
        self.assertEqual(result.images, [PNG])

    async def test_gemini_reference_uses_interactions_and_aspect_ratio(self) -> None:
        async def handler(request):
            self.assertEqual(request.url.path, "/v1beta/interactions")
            self.assertEqual(request.headers["x-goog-api-key"], "gem-key")
            payload = json.loads(request.content)
            self.assertEqual(payload["response_format"]["aspect_ratio"], "4:5")
            self.assertEqual([part["type"] for part in payload["input"]], ["text", "image"])
            return httpx.Response(200, json={"output_image": {"data": base64.b64encode(PNG).decode("ascii")}})

        provider = GeminiImageProvider(api_key="gem-key", base_url="https://generativelanguage.googleapis.com/v1beta", model="gemini-3.1-flash-image")
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(provider.close)
        result = await provider.generate(GenerateRequest(prompt="测试", size="1024x1280", extra={"reference_images": [DATA_URL]}))
        self.assertEqual(result.images, [PNG])
        self.assertEqual(result.raw["reference_count"], 1)

    async def test_seedream_reference_uses_images_api_and_returns_png(self) -> None:
        async def handler(request):
            self.assertEqual(request.url.path, "/api/v3/images/generations")
            self.assertEqual(request.headers["authorization"], "Bearer ark-key")
            payload = json.loads(request.content)
            self.assertEqual(payload["image"], [DATA_URL, DATA_URL])
            self.assertEqual(payload["response_format"], "b64_json")
            self.assertIn("2:3", payload["prompt"])
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode("ascii")} ]})

        provider = SeedreamProvider(api_key="ark-key", base_url="https://ark.cn-beijing.volces.com/api/v3", model="doubao-seedream-4-0-250828")
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(provider.close)
        result = await provider.generate(GenerateRequest(prompt="测试", size="1024x1536", extra={"reference_images": [DATA_URL, DATA_URL]}))
        self.assertEqual(result.images, [PNG])

    async def test_invalid_references_do_not_retry(self) -> None:
        provider = GeminiImageProvider(api_key="gem-key", base_url="https://example.test/v1beta", model="gemini-3.1-flash-image")
        with self.assertRaises(ProviderError) as ctx:
            await provider.generate(GenerateRequest(prompt="测试", extra={"reference_images": ["invalid"]}))
        self.assertEqual(ctx.exception.code, "INVALID_REFERENCE")
        self.assertFalse(ctx.exception.retryable)


if __name__ == "__main__":
    unittest.main()
