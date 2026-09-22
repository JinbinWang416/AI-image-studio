# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.batches import BATCH_INFO_FILE, create_new_batch
from app.config import Config
from app.manifest import ManifestStore
from app.providers.base import GenerateResult
from app.settings import SettingsStore


def png_bytes() -> bytes:
    image = Image.new("RGB", (32, 32), "#2b6cb0")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class FakeProvider:
    name = "mock"
    model = "mock-v1"
    price_per_image = 0.0
    supports_negative = True

    def describe(self) -> str:
        return "本地模拟（fake）"

    async def generate(self, request):
        return GenerateResult(images=[png_bytes()], provider=self.name, model=self.model)

    async def close(self) -> None:
        return None


def cfg(root: Path) -> Config:
    return Config(
        provider="mock",
        provider_label="本地模拟",
        model="mock-v1",
        output_base_root=root,
        output_root=root,
        overwrite=False,
        retry_max=1,
        retry_backoff=[0],
        concurrency=1,
        whiten_background=False,
    )


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_batches_are_isolated_and_keep_previous_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "output"
            legacy = base / "01_房屋中介门店"
            legacy.mkdir(parents=True)
            old = legacy / "01_楼房线稿.png"
            old.write_bytes(b"old-output")
            store = SettingsStore(Path(tmp) / "settings.json")

            first = create_new_batch(cfg(base), store)
            second = create_new_batch(cfg(base), store)

            self.assertNotEqual(first.batch_id, second.batch_id)
            self.assertEqual(old.read_bytes(), b"old-output")
            self.assertTrue((first.path / BATCH_INFO_FILE).is_file())
            meta = json.loads((second.path / BATCH_INFO_FILE).read_text(encoding="utf-8"))
            self.assertEqual(meta["batch_id"], second.batch_id)
            self.assertEqual(store.load()["output"]["active_batch"], second.batch_id)

    async def test_new_batch_endpoint_runs_under_new_folder_without_skipping_legacy(self) -> None:
        from app import runs
        from app.web import server

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "output"
            legacy = base / "01_房屋中介门店"
            legacy.mkdir(parents=True)
            (legacy / "01_楼房线稿.png").write_bytes(b"old-output")
            settings = SettingsStore(Path(tmp) / "settings.json")
            old_state = server.STATE
            server.STATE = server.AppState()
            try:
                with patch.object(server, "load_config", return_value=cfg(base)), \
                     patch.object(server, "get_store", return_value=settings), \
                     patch.object(server, "create_provider", return_value=FakeProvider()), \
                     patch.object(runs, "RUNS_FILE", Path(tmp) / "runs.json"):
                    response = await server.api_run_new_batch({"limit": 1})
                    body = json.loads(response.body)
                    batch_id = body["batch"]["id"]
                    self.assertTrue(batch_id.startswith("batch_"))
                    task = server.STATE.task
                    self.assertIsNotNone(task)
                    await task

                batch_root = base / batch_id
                generated = batch_root / "01_房屋中介门店" / "01_房屋中介门店生成图"
                effects = batch_root / "01_房屋中介门店" / "01_房屋中介门店效果图"
                self.assertTrue((generated / "01_楼房线稿.png").is_file())
                self.assertTrue((effects / "01_楼房线稿_效果图.png").is_file())
                self.assertEqual((legacy / "01_楼房线稿.png").read_bytes(), b"old-output")
                self.assertEqual(settings.load()["output"]["active_batch"], batch_id)
                entry = ManifestStore(batch_root).for_store("01_房屋中介门店").entries["01"]
                self.assertEqual(entry["runtime_metrics"]["batch_id"], batch_id)
                self.assertIn("batch_variant", entry["runtime_metrics"])
            finally:
                server.STATE = old_state

    async def test_web_has_distinct_continue_and_new_batch_actions(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "app" / "web" / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "app" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="btn-new-batch"', html)
        self.assertIn('id="recovery-banner"', html)
        self.assertIn("/api/run/new-batch", js)
        self.assertIn("run_paused", js)
        self.assertIn("当前批次已完成", js)
        self.assertIn("/api/settings/test-image", js)
        self.assertIn("验证图片出图权限", js)
        self.assertIn('id="image-view-generated"', html)
        self.assertIn('id="image-view-effect"', html)
        self.assertIn("/api/effects/generate", js)

        from app.web import server
        response = await server.index()
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        body = response.body.decode("utf-8")
        self.assertIn("/static/app.js?v=", body)
        # 样式表已重构为 tokens.css（设计令牌）+ app.css（组件样式），
        # 原 style.css 合并进 app.css。这里只校验"确实引入了带版本号的样式表"，
        # 不再绑定具体文件名，避免下次重构又要改测试。
        self.assertRegex(body, r'/static/[A-Za-z0-9_.\-]+\.css\?v=')

    async def test_timestamped_manifest_image_is_used_for_web_preview(self) -> None:
        from app.web import server

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            cfg_value = cfg(root)
            cfg_value.timestamp_prefix = True
            repo = server._repo()
            store = repo.stores[0]
            image_dir = root / store.output_dir
            image_dir.mkdir(parents=True)
            actual = image_dir / "20260919_014527_01_楼房线稿.png"
            actual.write_bytes(png_bytes())
            (image_dir / "_manifest.json").write_text(json.dumps({
                "version": 1,
                "store": {},
                "entries": {"01": {
                    "status": "success",
                    "image_path": str(actual),
                }},
            }, ensure_ascii=False), encoding="utf-8")

            payload = server.build_state_payload(cfg_value)
            item = payload["stores"][0]["items"][0]
            self.assertTrue(item["exists"])
            self.assertEqual(item["image_file"], actual.name)
            self.assertEqual(item["status"], "success")
            self.assertEqual(payload["next_scope"]["store_indexes"], cfg_value.selected_store_indexes)

    async def test_new_batch_uses_full_scope_even_when_trial_limit_is_set(self) -> None:
        from app.web import server

        config = cfg(Path(tempfile.gettempdir()) / "image-agent-output")
        config.run_limit = 6
        self.assertEqual(server._request_run_limit({}, config, create_new_batch=True), 0)
        self.assertEqual(server._request_run_limit({"limit": 1}, config, create_new_batch=True), 1)
        self.assertEqual(server._request_run_limit({}, config, create_new_batch=False), 6)
        config.batch_id = "batch_20260919_020000_mock_mock-v1"
        self.assertEqual(server._request_run_limit({}, config, create_new_batch=False), 6)


if __name__ == "__main__":
    unittest.main()
