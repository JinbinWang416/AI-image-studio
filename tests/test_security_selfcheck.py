# -*- coding: utf-8 -*-
"""启动一致性自检测试（块 4-a，对应原规划用例 13）。"""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.security import selfcheck  # noqa: E402
from app.security.selfcheck import (  # noqa: E402
    SelfCheckError,
    check_permission_consistency,
    run_startup_selfcheck,
)
from app.security.permissions import PERMISSION_CODES  # noqa: E402


class SelfCheckTests(unittest.TestCase):
    def test_current_codebase_is_consistent(self) -> None:
        """当前代码库必须自检通过（否则服务起不来）。"""
        problems = check_permission_consistency()
        self.assertEqual(problems, [], f"权限不一致：{problems}")

    def test_detects_dangling_code_in_access_rules(self) -> None:
        """路径映射里写了不存在的权限码 → 必须被发现。"""
        import app.web.access_rules as ar

        orig = ar.PATH_PERMISSION_RULES
        try:
            ar.PATH_PERMISSION_RULES = orig + (
                ("POST", "/api/__fake__", "not.a.real.permission"),
            )
            problems = check_permission_consistency()
        finally:
            ar.PATH_PERMISSION_RULES = orig

        self.assertTrue(problems, "未发现悬空权限码")
        self.assertTrue(
            any("not.a.real.permission" in p for p in problems),
            f"问题列表里没有提到悬空码：{problems}",
        )

    def test_detects_dangling_code_in_routes(self) -> None:
        """接口上 require_permission 用错码 → 必须被发现。"""
        import app.web.admin_routes as ar

        orig = ar.require_permission
        try:
            def fake(code: str):
                return lambda: None

            ar.require_permission = fake          # type: ignore[assignment]

            # 直接构造一个用了假码的源文件内容来测提取逻辑
            fake_src = pathlib.Path(self._tmpfile())
            fake_src.write_text(
                'def x():\n    pass\n\n\ndef y():\n'
                '    return require_permission("totally.fake.code")\n',
                encoding="utf-8",
            )
            used = selfcheck._codes_used_in_source(fake_src)
            self.assertIn("totally.fake.code", used)
            self.assertTrue(used - set(PERMISSION_CODES), "应能识别为未定义")
            fake_src.unlink()
        finally:
            ar.require_permission = orig  # type: ignore[assignment]

    def _tmpfile(self) -> str:
        import tempfile

        fd, name = tempfile.mkstemp(suffix=".py")
        import os

        os.close(fd)
        return name

    def test_startup_raises_on_problem(self) -> None:
        """自检发现问题时必须拒绝启动。"""
        orig = selfcheck.check_permission_consistency
        try:
            selfcheck.check_permission_consistency = lambda: ["模拟问题：x"]  # type: ignore
            with self.assertRaises(SelfCheckError):
                run_startup_selfcheck()
        finally:
            selfcheck.check_permission_consistency = orig  # type: ignore[assignment]

    def test_skip_env_bypasses(self) -> None:
        """SHS_SKIP_SELFCHECK=1 可应急跳过。"""
        import os

        orig = selfcheck.check_permission_consistency
        old = os.environ.get(selfcheck.SKIP_ENV)
        try:
            selfcheck.check_permission_consistency = lambda: ["模拟问题"]  # type: ignore
            os.environ[selfcheck.SKIP_ENV] = "1"
            run_startup_selfcheck()          # 不应抛异常
        finally:
            selfcheck.check_permission_consistency = orig  # type: ignore[assignment]
            if old is None:
                os.environ.pop(selfcheck.SKIP_ENV, None)
            else:
                os.environ[selfcheck.SKIP_ENV] = old

    def test_selfcheck_error_message_has_guidance(self) -> None:
        """报错信息必须包含修复指引（否则运维不知道怎么办）。"""
        orig = selfcheck.check_permission_consistency
        try:
            selfcheck.check_permission_consistency = lambda: ["问题A"]  # type: ignore
            with self.assertRaises(SelfCheckError) as ctx:
                run_startup_selfcheck()
        finally:
            selfcheck.check_permission_consistency = orig  # type: ignore[assignment]
        msg = str(ctx.exception)
        self.assertIn("问题A", msg)
        self.assertIn("access_rules.py", msg)
        self.assertIn("接入指南", msg)

    def test_code_extraction_from_real_routes(self) -> None:
        """能从真实路由文件里提取到权限码（证明自检确实在扫）。"""
        used = selfcheck._codes_used_in_source(ROOT / "app" / "web" / "admin_routes.py")
        self.assertIn("system.user.manage", used)
        self.assertIn("system.backup.manage", used)


if __name__ == "__main__":
    unittest.main(verbosity=2)
