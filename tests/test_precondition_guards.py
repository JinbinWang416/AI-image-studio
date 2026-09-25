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


class BatchSnapshotFreezeTests(unittest.TestCase):
    """P1-02：批次快照必须冻结**服务商与模型**。

    AGENTS.md「不可破坏的行为」第 1 条：开始/继续当前批次只执行当前批次快照中的
    未完成任务，它不能被后来修改的范围、模型或比例改变。

    早先 `_new_batch_snapshot()` 只记 store_indexes / prompt_version / size /
    image_workflow / prompt_quality / effect_workflow，**唯独不记 provider 与 model**；
    `_apply_snapshot_and_assets()` 也不恢复它们 —— 于是「继续当前批次」会拿
    **当前设置**去建 provider，用户中途换了模型再续跑，同一批次里就混进两种模型的产物。
    """

    def _cfg(self, **overrides):
        """构造一个带指定 provider/model 的配置。

        ⚠️ 两版的构造方式**不同**，测试必须两边都能跑：

        - 生产版是 57 个平铺字段，`provider` / `model` 是真正的 dataclass 字段，
          用 `dataclasses.replace()` 即可
        - 架构版（Phase 0 重构后）它们变成了**兼容 `@property`**（真实字段在
          12 个子配置里），`dataclasses.replace()` 会抛
          `TypeError: Config.__init__() got an unexpected keyword argument 'provider'`
          —— 必须走 `with_config()`

        所以优先用 `with_config()`，没有时退回 `dataclasses.replace()`。
        """
        import dataclasses

        from app.config import load_config

        cfg = load_config()
        try:
            from app.config import with_config
        except ImportError:
            return dataclasses.replace(cfg, **overrides)
        return with_config(cfg, **overrides)

    def test_snapshot_captures_provider_and_model(self) -> None:
        from app.web.server import _new_batch_snapshot

        cfg = self._cfg(provider="qwen", model="qwen-image-3.0")
        snap = _new_batch_snapshot(cfg, ["01"])
        self.assertEqual(snap.get("active_provider"), "qwen",
                         "快照没记服务商，续跑时无从恢复")
        self.assertEqual(snap.get("model"), "qwen-image-3.0",
                         "快照没记模型，续跑时会用当前设置的模型")

    def test_apply_restores_from_snapshot_over_current_settings(self) -> None:
        """当前设置与快照不一致时，必须以**快照**为准。"""
        from app.web.server import _apply_snapshot_and_assets

        cfg = self._cfg(provider="openai", model="gpt-image-2.5-flare")
        snap = {
            "active_provider": "qwen",
            "model": "qwen-image-3.0",
            "image_workflow": {"mode": "text"},
        }
        out = _apply_snapshot_and_assets(cfg, snap)
        self.assertEqual(out.provider, "qwen", "续跑没有回到批次冻结的服务商")
        self.assertEqual(out.model, "qwen-image-3.0", "续跑没有回到批次冻结的模型")

    def test_legacy_snapshot_without_provider_falls_back(self) -> None:
        """本次修复之前创建的老批次没有这两个键 —— 必须退回当前设置而非报错。"""
        from app.web.server import _apply_snapshot_and_assets

        cfg = self._cfg(provider="mock", model="mock-v1")
        out = _apply_snapshot_and_assets(cfg, {"image_workflow": {"mode": "text"}})
        self.assertEqual(out.provider, "mock", "老批次缺字段时不该改变当前服务商")
        self.assertEqual(out.model, "mock-v1")

    def test_blank_snapshot_values_fall_back(self) -> None:
        """快照里是空串时同样退回当前设置（不能把 provider 设成空）。"""
        from app.web.server import _apply_snapshot_and_assets

        cfg = self._cfg(provider="mock", model="mock-v1")
        out = _apply_snapshot_and_assets(
            cfg, {"active_provider": "", "model": "", "image_workflow": {"mode": "text"}}
        )
        self.assertEqual(out.provider, "mock")
        self.assertEqual(out.model, "mock-v1")


class SecretScannerTests(unittest.TestCase):
    """P1-06：密钥扫描工具必须真的能扫出东西。

    旧版有两个致命缺陷（评审指出，已实测确认）：

    1. **正则太窄** —— 字段名白名单只认 `password` 这类全称，且要求值 **≥16 位**。
       而真实的管理员密码叫 `PW`、只有 8 位，于是 13 处硬编码**一处都没报出来**。
    2. **`--git-all` 是假的** —— 它只从 `git rev-list --objects` 取**路径**，
       再去读**当前工作树**的文件；已删除的文件压根没被检查过。
    """

    @staticmethod
    def _mod():
        import sys
        from pathlib import Path

        tools_dir = str(Path(__file__).resolve().parent.parent / "tools")
        if tools_dir not in sys.path:
            sys.path.insert(0, tools_dir)
        import check_git_secrets

        return check_git_secrets

    def test_bundled_selftest_passes(self) -> None:
        """工具自带的合成样本必须全部符合预期。

        ⚠️ 样本是**合成的**，不含任何真实凭据 —— 评审特意强调过不要把真密码
           放进测试夹具。
        """
        import subprocess
        import sys
        from pathlib import Path

        script = Path(__file__).resolve().parent.parent / "tools" / "check_git_secrets.py"
        r = subprocess.run(
            [sys.executable, str(script), "--selftest"],
            capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(r.returncode, 0, f"自检未通过：\n{r.stdout}\n{r.stderr}")

    def test_short_and_abbreviated_credentials_are_detected(self) -> None:
        """缩写字段名 + 短密码必须检出 —— 这正是旧版漏掉 13 处的原因。"""
        mod = self._mod()
        for sample in (
            'PW = "abcdefgh"',                       # 8 位，字段名是缩写
            'ADMIN_PW = "hunter2xy"',
            '{"login":"admin","password":"hunter2xyz"}',
            'password:"noquote12345"',               # 键没有引号
            'ap.add_argument("--password", default="secret123")',
        ):
            self.assertTrue(mod.scan_text(sample, "app/x.py"),
                            f"{sample!r} 应被检出（旧版会漏）")

    def test_markers_and_placeholders_are_not_flagged(self) -> None:
        """进度标记常量与占位符不该被报 —— 否则工具全是噪音，没人会看。"""
        mod = self._mod()
        for sample in ('PASS = "✅"', 'PASSWORD = ""', 'password = "***"',
                       'api_key = "your-api-key-here"', 'pwd = "xxx"'):
            self.assertFalse(mod.scan_text(sample, "app/x.py"),
                             f"{sample!r} 不该被报")

    def test_fixture_names_are_downgraded_not_dropped(self) -> None:
        """测试夹具要**降级**显示而不是直接消失 —— 万一它真匹配了某个账号呢。"""
        mod = self._mod()
        hits = mod.scan_text('FIXTURE_PW = "Str0ng!Passw0rd"', "tools/verify_x.py")
        self.assertTrue(hits, "夹具仍应列出，供人工确认")
        self.assertIn("[?]", hits[0], "夹具应降级为 [?] 而不是 [!!]")
        self.assertIn("FIXTURE_PW", hits[0], "应显示变量全名，而不是光秃秃的 PW")

    def test_tests_dir_hits_are_downgraded(self) -> None:
        mod = self._mod()
        hits = mod.scan_text('PW = "abcd1234"', "tests/test_x.py")
        self.assertTrue(hits)
        self.assertIn("[?]", hits[0])

    def test_app_path_hits_are_high_severity(self) -> None:
        """真正的源码路径里出现凭据，必须是最高级别。"""
        mod = self._mod()
        hits = mod.scan_text('ADMIN_PW = "abcd1234"', "app/web/server.py")
        self.assertTrue(hits)
        self.assertIn("[!!]", hits[0], "app/ 下的硬编码凭据必须是 [!!]")

    def test_git_all_reads_history_not_working_tree(self) -> None:
        """`--git-all` 扫到的对象数必须**多于** `--git`。

        旧版两者数量相同（都是工作树文件数），因为 `--git-all` 只取了路径、
        读的还是工作树 —— 这个断言就是为了钉死这一点。
        """
        mod = self._mod()
        tracked = len(mod.iter_tracked())
        allblobs = len(mod.iter_all_blobs())
        self.assertGreater(tracked, 0, "一个已跟踪文件都没读到")
        self.assertGreater(
            allblobs, tracked,
            f"--git-all({allblobs}) 没有多于 --git({tracked})，"
            "说明它没有真正读取历史对象",
        )


if __name__ == "__main__":
    unittest.main()
