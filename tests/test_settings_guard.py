# -*- coding: utf-8 -*-
"""配置写入防线：锁死「测试污染真实 `config/settings.json`」这个回归。

⚠️ 背景（实测踩到两次）

   `config/settings.json` 两次在跑完测试后变成 `provider=mock`。
   逐个测试文件复跑又完全无法复现（23/23 全部「未改」），因为触发点在
   **测试直接调用真实端点**这条路上：

       tests/test_openai_regeneration.py
           → server.api_openai_house_regenerate()
               → get_store().save({"prompt_quality": {...}})
                   → 单例此前一律指向**真实配置**

   而 `default_settings()` 的默认 `active_provider` 恰好是 `"mock"`，
   所以一旦 `load()` 遇到读不动的内容（例如被 PowerShell 写出 BOM）
   而静默回退默认值，紧接着的 `save()` 就把 `provider=mock` **落了盘**。

现在的三道防线

1. **测试进程自动隔离**：进程里只要有 `unittest`，`get_store()` 就落到临时目录；
2. **真实路径守卫**：测试上下文里直接构造 `SettingsStore()` 写真实配置 → 抛错；
3. **审计日志**：每次真实写入（含被拦截）记录调用者，可事后追溯。

本文件逐条锁死，任何一道被改坏都会立刻失败。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import settings as mod


class TestTestProcessIsolation(unittest.TestCase):
    """防线 1：测试进程内单例不得指向真实配置。"""

    def test_singleton_is_not_real_config(self) -> None:
        store = mod.get_store()
        self.assertFalse(
            store.is_real_config(),
            f"测试进程内 get_store() 仍指向真实配置：{store.path}",
        )

    def test_singleton_lands_in_temp_dir(self) -> None:
        store = mod.get_store()
        self.assertIn(
            "shs-test-config-",
            str(store.path),
            f"单例没有落到临时目录：{store.path}",
        )

    def test_singleton_save_leaves_real_config_alone(self) -> None:
        """即使有人往单例里写 provider，也不能碰到真实文件。

        ⚠️ 这里**不要** patch `SETTINGS_FILE` —— 那会让单例瞬间「变成」真实配置，
           从而被守卫拦下，测的就不是隔离了。单例本来就该落在临时目录，
           直接写它即可，然后校验真实文件的字节没变。
        """
        before = mod.SETTINGS_FILE.read_bytes()
        mod.get_store().save({"active_provider": "mock"})
        self.assertEqual(
            mod.SETTINGS_FILE.read_bytes(),
            before,
            "测试进程内的 save() 改动了真实 config/settings.json",
        )

    def test_independent_store_in_temp_dir_is_writable(self) -> None:
        """正常能力不能因为加防线而丢失：临时路径必须写得进去。"""
        tmp = Path(tempfile.mkdtemp(prefix="shs-guard-")) / "settings.json"
        mod.SettingsStore(tmp).save({"active_provider": "qwen"})
        self.assertTrue(tmp.exists())
        self.assertEqual(
            json.loads(tmp.read_text(encoding="utf-8"))["active_provider"],
            "qwen",
        )

    def test_bare_construction_is_isolated(self) -> None:
        """⚠️ 最后一道兜底：哪怕忘了传路径，也拿不到真实配置。

        `SettingsStore()` 不带参数是最容易写出的写法，也是全项目 5 处端点的写法
        （`get_store()` 内部就调它）。测试进程里它必须落到临时目录，
        否则「测试跑完配置变 mock」会以另一种形式复发。
        """
        store = mod.SettingsStore()
        self.assertFalse(
            store.is_real_config(),
            f"裸构造 SettingsStore() 拿到了真实配置：{store.path}",
        )


class TestRealPathGuard(unittest.TestCase):
    """防线 2：测试上下文写真实配置必须被拒绝。"""

    def test_guard_recognises_test_context(self) -> None:
        caller, test_src = mod._scan_stack()
        self.assertTrue(test_src, "守卫无法识别测试上下文，防线已失效")
        self.assertIn(
            "test_settings_guard",
            test_src,
            f"测试来源定位到了框架而非具体测试：{test_src}",
        )

    def test_frame_classifier(self) -> None:
        """纯函数分类器：这是 `test_src` 判定的地基。"""
        self.assertTrue(mod._frame_is_test(r"C:\Python312\Lib\unittest\case.py"))
        self.assertTrue(mod._frame_is_test(r"E:\proj\tests\test_foo.py"))
        self.assertFalse(mod._frame_is_test(r"E:\proj\app\web\server.py"))
        self.assertFalse(mod._frame_is_test(r"E:\proj\app\settings.py"))

    def test_real_path_write_is_blocked(self) -> None:
        """把真实路径判定换成临时文件再冒充「真实配置」。

        这样即使守卫失灵，写坏的也只是临时文件 —— 测试本身绝不碰真配置。
        """
        tmp = Path(tempfile.mkdtemp(prefix="shs-guard-")) / "settings.json"
        with mock.patch.object(mod, "_REAL_SETTINGS_PATH", tmp.resolve()):
            store = mod.SettingsStore(tmp)
            self.assertTrue(store.is_real_config())
            with self.assertRaises(RuntimeError):
                store.save({"active_provider": "hacked"})
            self.assertFalse(tmp.exists(), "守卫放行了一次真实路径写入")

    def test_patching_settings_file_cannot_bypass_guard(self) -> None:
        """⚠️ 本文件最核心的一条回归。

        真实 `config/settings.json` 被改成 `provider=mock` 两次都没被察觉，
        根因正是：守卫和审计都拿 `SETTINGS_FILE` 这个**模块全局**做判定，
        而 `mock.patch.object` 一句话就能把它换掉。换掉之后，一个指向真实配置的
        store 会「看起来不像真实配置」—— 守卫提前 return、审计静默跳过，
        写入畅通无阻（文件 mtime 对得上，审计日志里却一条记录都没有）。

        现在判定基准是模块加载时固化的 `_REAL_SETTINGS_PATH`，
        patch `SETTINGS_FILE` 必须**完全无效**。
        """
        tmp = Path(tempfile.mkdtemp(prefix="shs-guard-")) / "settings.json"
        with mock.patch.object(mod, "_REAL_SETTINGS_PATH", tmp.resolve()), \
                mock.patch.object(mod, "SETTINGS_FILE", Path("X:/nowhere/settings.json")):
            store = mod.SettingsStore(tmp)
            self.assertTrue(
                store.is_real_config(),
                "patch 掉 SETTINGS_FILE 就绕过了真实路径判定，守卫等于没有",
            )
            with self.assertRaises(RuntimeError):
                store.save({"active_provider": "hacked"})
            self.assertFalse(tmp.exists())

    def test_escape_hatch_allows_write(self) -> None:
        """逃生开关必须有效，否则将来没法做需要真实路径的集成验证。"""
        tmp = Path(tempfile.mkdtemp(prefix="shs-guard-")) / "settings.json"
        with mock.patch.object(mod, "_REAL_SETTINGS_PATH", tmp.resolve()), \
                mock.patch.dict("os.environ", {mod.ALLOW_TEST_WRITE_ENV: "1"}):
            mod.SettingsStore(tmp).save({"active_provider": "qwen"})
            self.assertTrue(tmp.exists())


class TestRealConfigIntegrity(unittest.TestCase):
    """防线 3：真实配置必须始终可解析，且不带 BOM。"""

    def test_config_exists_and_parses(self) -> None:
        self.assertTrue(
            mod.SETTINGS_FILE.exists(),
            f"真实配置不见了：{mod.SETTINGS_FILE}",
        )
        data = json.loads(mod.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertIn("active_provider", data)

    def test_config_has_no_bom(self) -> None:
        """⚠️ PowerShell 的 `Set-Content -Encoding UTF8` 会加 BOM，
        而 `json.loads()` 遇到 BOM 直接抛错 —— 整个配置会读不到，
        provider 退回 mock。这条曾经真的把配置搞坏过。"""
        raw = mod.SETTINGS_FILE.read_bytes()
        self.assertFalse(
            raw.startswith(b"\xef\xbb\xbf"),
            "config/settings.json 带 UTF-8 BOM，Python 的 json 模块会读不动它",
        )


if __name__ == "__main__":
    unittest.main()
