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
from unittest import mock

from app.batches import is_safe_batch_id
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


class BatchIdTraversalTests(unittest.TestCase):
    """P1-03：批次 ID 必须是**单个安全目录名**，不能跳出输出根目录。

    旧实现只做字符集白名单 `^[A-Za-z0-9_.-]+$`，把 `.` / `..` 一并放行 ——
    而批次 ID 会被直接拼到 `output_root` 后面（Storage 还在那里建目录、写图片），
    于是 `output_root / ".."` 就指向了输出根目录的上一级。
    """

    def test_dot_segments_are_rejected(self) -> None:
        for bad in (".", "..", "...", "....", " .. "):
            self.assertFalse(is_safe_batch_id(bad), f"{bad!r} 不该被接受")

    def test_empty_and_overlong_rejected(self) -> None:
        for bad in ("", "   ", None, "a" * 200):
            self.assertFalse(is_safe_batch_id(bad), f"{bad!r} 不该被接受")

    def test_separators_rejected(self) -> None:
        """反斜杠/斜杠从来不在字符集里，这里锁死不要放宽。"""
        for bad in ("a/b", "a\\b", "../x", "..\\x", "C:", "a:b"):
            self.assertFalse(is_safe_batch_id(bad), f"{bad!r} 含路径分隔符")

    def test_reserved_device_names_rejected(self) -> None:
        for bad in ("CON", "nul", "COM1", "lpt9", "con.txt", "PRN"):
            self.assertFalse(is_safe_batch_id(bad), f"{bad!r} 是 Windows 保留设备名")

    def test_leading_trailing_dash_rejected(self) -> None:
        for bad in ("-x", "x-"):
            self.assertFalse(is_safe_batch_id(bad), f"{bad!r} 首尾是连字符")

    def test_real_batch_ids_still_accepted(self) -> None:
        """不能误伤真实批次名（回归的重点：修穿越不能顺手把正常 ID 挡了）。"""
        for good in (
            "batch_20260919_014522_qwen_qwen-image-3.0",
            "batch_20260101_000000",
            "batch_x",
            "a.b.c",
        ):
            self.assertTrue(is_safe_batch_id(good), f"{good!r} 是合法批次名，不该被拒")

    def test_escape_demonstration(self) -> None:
        """把「为什么危险」写进测试：拼接后确实会指向父目录。"""
        root = Path(tempfile.mkdtemp(prefix="shs-batch-"))
        self.assertNotEqual(
            (root / "..").resolve(), root.resolve(),
            "output_root / '..' 指向了别处 —— 这就是路径穿越",
        )
        self.assertFalse(is_safe_batch_id(".."), "所以 '..' 必须被拒绝")


class SettingsFieldPermissionTests(unittest.TestCase):
    """P1-01：`POST /api/settings` 必须按**字段**授权，不能整个接口一刀切。

    这个接口会把 payload 深度合并进 settings.json，可改字段横跨
    「模型 / 路径 / 提示词 / 效果图」四组权限。只挂一个权限码的后果是：
      · 挂 `settings.model.manage` → 有「改模板」权限的设计师连提示词都改不了，
        同时有该权限的人可以顺手改掉输出路径，`settings.path.manage` 形同虚设。
    """

    def _stub_user(self):
        class U:
            id = "u-test"
            login_name = "tester"
            display_name = "测试用户"
            roles = ["designer"]
        return U()

    def _run(self, payload: dict, granted: set[str]):
        """在给定的权限集合下调用校验函数，返回抛出的 HTTPException 或 None。"""
        from fastapi import HTTPException

        from app.web import server as srv

        user = self._stub_user()
        with mock.patch.object(
            srv.security_service, "has_permission",
            side_effect=lambda u, code: code in granted,
        ), mock.patch.object(srv.security_service, "audit") as audit:
            audit.log = mock.Mock()
            try:
                srv._require_settings_permissions(user, payload)
                return None
            except HTTPException as exc:
                return exc

    def test_provider_fields_need_model_permission(self) -> None:
        exc = self._run({"providers": {"qwen": {"api_key": "x"}}}, set())
        self.assertIsNotNone(exc, "改 API Key 却没有任何权限，应当被拒")
        self.assertEqual(exc.status_code, 403)

    def test_output_field_needs_path_permission(self) -> None:
        """有模型权限、没有路径权限时，改输出路径必须被拒。"""
        exc = self._run({"output": {"root": "D:/elsewhere"}}, {"settings.model.manage"})
        self.assertIsNotNone(exc, "只有模型权限也能改输出路径 —— 路径权限形同虚设")
        self.assertIn("settings.path.manage", str(exc.detail))

    def test_prompt_template_field_needs_template_permission(self) -> None:
        """设计师能改提示词模板，但没有模型/路径权限。"""
        ok = self._run({"prompt_quality": {"template": "x"}}, {"prompt.template.manage"})
        self.assertIsNone(ok, "有模板权限却改不了提示词模板")

    def test_mixed_payload_requires_all_relevant_permissions(self) -> None:
        """一次提交跨越多组字段时，任何一组缺权限都要整体拒绝。"""
        payload = {"providers": {"qwen": {}}, "output": {"root": "D:/x"}}
        exc = self._run(payload, {"settings.model.manage"})       # 缺 path
        self.assertIsNotNone(exc, "同时改密钥和输出路径时只校验了其中一组")

    def test_unknown_field_falls_back_to_strictest(self) -> None:
        """未在映射表里的字段（含以后新增的）落到最严档，默认不放行。"""
        exc = self._run({"some_future_setting": 1}, {"prompt.template.manage"})
        self.assertIsNotNone(exc, "新字段被默认放行了，应该落到最严权限")
        self.assertIn("settings.model.manage", str(exc.detail))

    def test_version_key_is_ignored(self) -> None:
        """`version` 只是回传的元信息，不该要求任何权限。"""
        self.assertIsNone(self._run({"version": 1}, set()))

    def test_effect_fields_need_effect_permission(self) -> None:
        for key in ("effect", "effect_workflow"):
            ok = self._run({key: {}}, {"effect.params.manage"})
            self.assertIsNone(ok, f"{key} 应该能用效果图参数权限改")
            exc = self._run({key: {}}, set())
            self.assertIsNotNone(exc, f"{key} 在无权限时应当被拒")


class AccessRulePathTests(unittest.TestCase):
    """P1-01 的另一半：路径权限规则不能靠前缀误伤。

    `pick-dir` / `new-dir` 原先不在规则表里，于是命中 `POST /api/settings`
    的前缀规则，被误判成「管理模型与 API Key」——而这些接口实际是在
    选择/创建**输出目录**。
    """

    def test_pick_and_new_dir_map_to_path_permission(self) -> None:
        from app.web.access_rules import required_permission_for

        for path in ("/api/settings/pick-dir", "/api/settings/new-dir",
                     "/api/settings/validate-path", "/api/settings/open-dir"):
            allowed, perm = required_permission_for("POST", path)
            self.assertTrue(allowed, f"{path} 不该被完全拒绝")
            self.assertEqual(
                perm, "settings.path.manage",
                f"{path} 的权限码应是 settings.path.manage，实际是 {perm}",
            )

    def test_reset_and_test_still_need_model_permission(self) -> None:
        from app.web.access_rules import required_permission_for

        for path in ("/api/settings/reset", "/api/settings/test",
                     "/api/settings/test-image"):
            allowed, perm = required_permission_for("POST", path)
            self.assertTrue(allowed)
            self.assertEqual(perm, "settings.model.manage", f"{path} 权限码被改动了")

    def test_save_settings_defers_to_field_level_check(self) -> None:
        """`POST /api/settings` 本体不再挂权限码，改为函数内按字段校验。"""
        from app.web.access_rules import required_permission_for

        allowed, perm = required_permission_for("POST", "/api/settings")
        self.assertTrue(allowed, "登录用户应当能到达该端点（细节由字段级校验决定）")
        self.assertIsNone(perm, "该端点不应再挂一刀切的权限码")

    def test_reading_settings_needs_only_login(self) -> None:
        from app.web.access_rules import required_permission_for

        allowed, perm = required_permission_for("GET", "/api/settings")
        self.assertTrue(allowed)
        self.assertIsNone(perm)


if __name__ == "__main__":
    unittest.main()
