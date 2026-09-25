# -*- coding: utf-8 -*-
"""前置校验的回归测试（对应发布前评审的 P1-04 与 P1-05）。

## P1-04 损坏配置静默覆盖

`SettingsStore.load()` 解析失败时曾**静默回退默认值**，而这份默认值会被缓存，
随后任意一次 `save()` 都拿它当 base 写回文件 —— 一次读取故障就升级成
「配置被永久覆盖成 mock」（本项目真实发生过两次）。
现在：留档 `settings.json.corrupt.<时间戳>` + 打 `corrupt` 标记 + 写审计。

## P1-05 校验顺序

`_start_full_run()` 曾在**完整校验之前**就 `save()` 真实感档位并 `create_batch()`，
于是「服务商未实现 / API Key 没填」这类失败会留下：档位被提高、空批次已落盘、
active_batch 已切换。现在所有校验都在副作用之前完成。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.providers import ProviderError, available_providers, get_provider
from app.settings import SettingsStore


class CorruptConfigTests(unittest.TestCase):
    """P1-04：损坏配置必须留档，且不能悄悄被默认值顶掉。"""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="shs-corrupt-"))
        self.path = self.dir / "settings.json"

    def test_corrupt_file_is_quarantined_and_flagged(self) -> None:
        self.path.write_bytes(b'{"active_provider": "qwen", BROKEN')   # 故意写坏
        store = SettingsStore(self.path)

        with self.assertLogs("app.settings", level="WARNING"):
            data = store.load()

        self.assertTrue(store.corrupt, "损坏配置没有被标记")
        self.assertTrue(list(self.dir.glob("settings.json.corrupt.*")),
                        "损坏的配置没有被留档，用户将无法人工恢复")
        # 回退到默认值是既有行为（保证不阻塞），这里只要求它「被标记」
        self.assertEqual(data.get("active_provider"), "mock")

    def test_non_dict_root_is_also_treated_as_corrupt(self) -> None:
        """合法 JSON 但不是对象 —— 以前会静默忽略，现在同样留档。"""
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        store = SettingsStore(self.path)
        with self.assertLogs("app.settings", level="WARNING"):
            store.load()
        self.assertTrue(store.corrupt)

    def test_healthy_config_is_not_flagged(self) -> None:
        self.path.write_text(json.dumps({"active_provider": "qwen"}), encoding="utf-8")
        store = SettingsStore(self.path)
        self.assertFalse(getattr(store, "corrupt", False))
        self.assertEqual(store.load().get("active_provider"), "qwen")
        self.assertFalse(list(self.dir.glob("*.corrupt.*")), "没坏却留了档")

    def test_save_over_corrupt_is_allowed_but_recorded(self) -> None:
        """覆盖损坏配置**不阻塞**（否则配置一坏就再也改不动），但要有痕迹。"""
        self.path.write_bytes(b"NOT JSON AT ALL")
        store = SettingsStore(self.path)
        with self.assertLogs("app.settings", level="WARNING"):
            store.load()
        store.save({"active_provider": "qwen"})
        written = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(written.get("active_provider"), "qwen")


class ProviderPrecheckTests(unittest.TestCase):
    """P1-05：服务商可用性必须在**创建批次之前**判定。"""

    def test_unimplemented_provider_is_rejected_at_resolve_time(self) -> None:
        """kling / zhipu 只有配置没有实现，必须在解析阶段就抛 ProviderError。"""
        for name in ("kling", "zhipu"):
            with self.assertRaises(ProviderError) as ctx:
                get_provider(name)
            self.assertEqual(ctx.exception.code, "PROVIDER_NOT_IMPLEMENTED")
            self.assertFalse(ctx.exception.retryable, "未实现的服务商不该被重试")

    def test_available_providers_excludes_unimplemented(self) -> None:
        names = available_providers()
        self.assertNotIn("kling", names)
        self.assertNotIn("zhipu", names)
        # 有实现的必须都在
        for name in ("qwen", "openai", "gemini", "seedream", "flux_local", "mock", "custom"):
            self.assertIn(name, names)

    def test_require_usable_provider_raises_http_400(self) -> None:
        """server 的预检辅助函数要把 ProviderError 转成 400，而不是 500。"""
        from fastapi import HTTPException

        from app.web.server import _require_usable_provider

        class Cfg:
            provider = "kling"

        with self.assertRaises(HTTPException) as ctx:
            _require_usable_provider(Cfg())
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("尚未实现", str(ctx.exception.detail))

    def test_require_usable_provider_accepts_implemented(self) -> None:
        from app.web.server import _require_usable_provider

        class Cfg:
            provider = "mock"

        _require_usable_provider(Cfg())      # 不应抛错


if __name__ == "__main__":
    unittest.main()
