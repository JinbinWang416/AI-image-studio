# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.config import Config
from app.manifest import ManifestStore
from app.models import Job, JobStatus, Store
from app.orchestrator import Orchestrator
from app.providers.base import GenerateResult, ProviderError
from app.providers.qwen import QwenProvider
from app.settings import SettingsStore
from app.storage import Storage
from app.web.server import AppState


def png_bytes() -> bytes:
    image = Image.new("RGB", (24, 24), "#2563eb")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class _Response:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class _Repo:
    def __init__(self) -> None:
        self.stores = [Store(
            folder_index="01", folder_name="测试门店", output_dir="01_测试门店",
            pdd_title="测试", main_title="测试", sub_title="测试", color_theme="蓝色",
            compliance_note="", simple_prompt="", items=[],
        )]
        self.jobs = [
            Job(
                job_id=f"01-{index:02d}", store_index="01", store_name="测试门店",
                output_dir="01_测试门店", pic_index=f"{index:02d}", theme=f"主题{index}",
                file_name=f"{index:02d}_主题{index}.png", positive_prompt="测试", negative_prompt="",
                simple_prompt="", expected_text=[],
            )
            for index in range(1, 4)
        ]

    def build_jobs(self, prompt_version: str = "") -> list[Job]:
        return copy.deepcopy(self.jobs)


class _ArrearageProvider:
    name = "qwen"
    model = "qwen-image-3.0"
    supports_negative = True
    calls = 0

    def describe(self) -> str:
        return "阿里云百炼（测试）"

    async def generate(self, request):
        self.calls += 1
        raise ProviderError("账户欠费", retryable=False, code="ACCOUNT_ARREARAGE")

    async def close(self) -> None:
        return None


class _SuccessProvider(_ArrearageProvider):
    calls = 0

    async def generate(self, request):
        self.calls += 1
        return GenerateResult(images=[png_bytes()], provider=self.name, model=self.model)


class _ImageCapabilityProvider(_SuccessProvider):
    def __init__(self) -> None:
        self.request = None
        self.closed = False

    async def generate(self, request):
        self.request = request
        return GenerateResult(images=[png_bytes()], provider=self.name, model=self.model)

    async def close(self) -> None:
        self.closed = True


class _ImageCapabilityArrearageProvider(_ImageCapabilityProvider):
    async def generate(self, request):
        self.request = request
        raise ProviderError("账户欠费", retryable=False, code="ACCOUNT_ARREARAGE")


def config(root: Path) -> Config:
    return Config(
        provider="qwen", provider_label="阿里云百炼", model="qwen-image-3.0",
        output_base_root=root, output_root=root, concurrency=1, retry_max=1,
        retry_backoff=[0], whiten_background=False, budget_limit=10,
    )


class AccountRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_qwen_arrearage_is_explicitly_recoverable(self) -> None:
        provider = QwenProvider(api_key="test", base_url="https://dashscope.aliyuncs.com")
        with self.assertRaises(ProviderError) as ctx:
            provider._raise_http(_Response(400, '{"code":"Arrearage","message":"overdue-payment","request_id":"request-test-123"}'))
        self.assertEqual(ctx.exception.code, "ACCOUNT_ARREARAGE")
        self.assertFalse(ctx.exception.retryable)
        self.assertTrue(ctx.exception.requires_account_recovery)
        self.assertIn("request-test-123", str(ctx.exception))

    async def test_arrearage_pauses_dispatch_and_a_later_run_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            cfg = config(root)
            repo = _Repo()
            storage = Storage(root, whiten_bg=False)
            storage.prepare_directories(repo.stores)
            manifests = ManifestStore(root)
            events: list[dict] = []

            blocked = _ArrearageProvider()
            first = Orchestrator(cfg, blocked, repo, storage, manifests)
            first.on_event(events.append)
            stats = await first.run()

            self.assertTrue(first.paused)
            self.assertEqual(blocked.calls, 1)
            self.assertEqual(stats.failed, 0)
            self.assertIn("账户额度不足", stats.aborted)
            self.assertIn("run_paused", [event["type"] for event in events])
            entry = ManifestStore(root).for_store("01_测试门店").entries["01"]
            self.assertEqual(entry["status"], JobStatus.PAUSED.value)

            restored = _SuccessProvider()
            second = Orchestrator(cfg, restored, repo, storage, ManifestStore(root))
            resumed = await second.run()
            self.assertEqual(restored.calls, 3)
            self.assertEqual(resumed.success, 3)
            self.assertTrue((root / "01_测试门店" / "01_测试门店生成图" / "01_主题1.png").is_file())

    async def test_successful_image_test_unblocks_only_the_paused_provider_and_batch(self) -> None:
        state = AppState()
        state.pause_for_account_recovery("qwen", "batch-1", "账户欠费", "ACCOUNT_ARREARAGE")
        self.assertTrue(state.recovery_for("qwen", "batch-1")["required"])
        self.assertFalse(state.recovery_for("openai", "batch-1")["required"])
        self.assertFalse(state.recovery_for("qwen", "batch-1")["verified"])

        state.confirm_account_recovery("qwen")
        self.assertTrue(state.recovery_for("qwen", "batch-1")["verified"])
        state.clear_account_recovery("qwen", "batch-1")
        self.assertFalse(state.recovery_for("qwen", "batch-1")["required"])

    async def test_masked_provider_key_survives_settings_save(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            settings = SettingsStore(Path(folder) / "settings.json")
            settings.save({"providers": {"qwen": {"api_key": "test-secret-qwen-key"}}})
            masked = settings.masked()["providers"]["qwen"]["api_key"]
            self.assertIn("****", masked)

            settings.save({"providers": {"qwen": {"api_key": masked, "base_url": "https://example.test/v1"}}})
            saved = settings.load(reload=True)["providers"]["qwen"]
            self.assertEqual(saved["api_key"], "test-secret-qwen-key")
            self.assertEqual(saved["base_url"], "https://example.test/v1")

    async def test_image_capability_success_verifies_paused_qwen_batch(self) -> None:
        from app.web import server

        with tempfile.TemporaryDirectory() as folder:
            settings = SettingsStore(Path(folder) / "settings.json")
            settings.save({"providers": {"qwen": {
                "api_key": "test-key", "base_url": "https://example.test/v1", "model": "qwen-image-3.0",
            }}})
            state = AppState()
            state.pause_for_account_recovery("qwen", "batch-1", "账户欠费", "ACCOUNT_ARREARAGE")
            provider = _ImageCapabilityProvider()
            old_state = server.STATE
            server.STATE = state
            try:
                with patch.object(server, "get_store", return_value=settings), \
                     patch.object(server, "load_config", return_value=config(Path(folder) / "output")), \
                     patch.object(server, "create_provider", return_value=provider):
                    response = await server.api_test_image_capability({"provider": "qwen"})
                body = json.loads(response.body)
                self.assertTrue(body["ok"])
                self.assertEqual(body["status"], "image_ready")
                self.assertEqual(provider.request.size, "1024x1024")
                self.assertEqual(provider.request.n, 1)
                self.assertTrue(provider.closed)
                self.assertTrue(state.recovery_for("qwen", "batch-1")["verified"])
            finally:
                server.STATE = old_state

    async def test_image_capability_arrearage_keeps_batch_locked(self) -> None:
        from app.web import server

        with tempfile.TemporaryDirectory() as folder:
            settings = SettingsStore(Path(folder) / "settings.json")
            settings.save({"providers": {"qwen": {
                "api_key": "test-key", "base_url": "https://example.test/v1", "model": "qwen-image-3.0",
            }}})
            state = AppState()
            state.pause_for_account_recovery("qwen", "batch-1", "账户欠费", "ACCOUNT_ARREARAGE")
            provider = _ImageCapabilityArrearageProvider()
            old_state = server.STATE
            server.STATE = state
            try:
                with patch.object(server, "get_store", return_value=settings), \
                     patch.object(server, "load_config", return_value=config(Path(folder) / "output")), \
                     patch.object(server, "create_provider", return_value=provider):
                    response = await server.api_test_image_capability({"provider": "qwen"})
                body = json.loads(response.body)
                self.assertFalse(body["ok"])
                self.assertEqual(body["status"], "ACCOUNT_ARREARAGE")
                self.assertTrue(provider.closed)
                self.assertFalse(state.recovery_for("qwen", "batch-1")["verified"])
            finally:
                server.STATE = old_state

    async def test_paused_manifest_restores_unverified_recovery_after_web_restart(self) -> None:
        from app.web import server

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "output"
            repo = _Repo()
            manifests = ManifestStore(root)
            manifest = manifests.for_store(repo.stores[0].output_dir)
            manifest.entries["01"] = {
                "status": JobStatus.PAUSED.value,
                "provider": "qwen",
                "error": "阿里云百炼账户欠费或余额不足",
            }
            manifest.save()
            cfg = config(root)
            cfg.batch_id = "batch-1"
            old_state = server.STATE
            server.STATE = AppState()
            try:
                server._restore_account_recovery_from_manifests(cfg, ManifestStore(root), repo.stores)
                recovery = server.STATE.recovery_for("qwen", "batch-1")
                self.assertTrue(recovery["required"])
                self.assertFalse(recovery["verified"])
            finally:
                server.STATE = old_state


if __name__ == "__main__":
    unittest.main()
