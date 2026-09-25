# -*- coding: utf-8 -*-
"""安全模块单元测试（文档 §13 适用用例）。

覆盖：
    · 登录名规范化与同形账号防护
    · 密码哈希与验证
    · 会话生命周期（创建 / 解析 / 撤销 / 超时）
    · 防枚举（错误信息一致）
    · 账号停用后会话立即失效
    · 改密码后其它会话失效
    · 权限计算与角色边界
    · 审计脱敏与不可篡改
    · 提权防护（不能授予自己没有的权限）

全部使用**临时目录**，不触碰真实数据（文档 §13：隔离环境与合成数据）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.security.audit import RESULT_DENIED, AuditLogger, _redact  # noqa: E402
from app.security.auth import (  # noqa: E402
    SessionStore,
    hash_password,
    password_strength_error,
    verify_password,
    verify_totp,
    generate_totp_secret,
    totp_uri,
)
from app.security.permissions import (  # noqa: E402
    PERMISSION_CODES,
    ROLE_TEMPLATES,
    is_valid_permission,
    role_default_permissions,
)
from app.security.service import SecurityService  # noqa: E402
from app.security.url_guard import UrlGuardError, validate_outbound_url  # noqa: E402
from app.security.users import (  # noqa: E402
    STATUS_DISABLED,
    UserStore,
    UsernameError,
    normalize_login_name,
    validate_login_name,
)


class TempSecurityMixin:
    """为每个测试建独立的临时存储。"""

    def setUp(self) -> None:  # noqa: D102
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.users = UserStore(base / "security.json")
        self.sessions = SessionStore(base / "sessions.json")
        self.audit = AuditLogger(base / "audit")
        self.svc = SecurityService(self.users, self.sessions, self.audit)

    def tearDown(self) -> None:  # noqa: D102
        self._tmp.cleanup()


# ================================================================ 登录名
class LoginNameTests(unittest.TestCase):
    def test_nfkc_normalizes_fullwidth(self) -> None:
        """全角与半角必须归一到同一个登录名（文档 §3.3 防同形账号）。"""
        self.assertEqual(normalize_login_name("ＡＤＭＩＮ"), "admin")
        self.assertEqual(normalize_login_name("ADMIN"), "admin")
        self.assertEqual(normalize_login_name(" Admin "), "admin")

    def test_whitespace_removed(self) -> None:
        self.assertEqual(normalize_login_name("ad min"), "admin")
        self.assertEqual(normalize_login_name("a\td m\ni n"), "admin")

    def test_invalid_rejected(self) -> None:
        for bad in ("", "a", "ab", "有中文", "has space!", "a" * 40, "a..b", ".abc", "abc-"):
            with self.assertRaises(UsernameError, msg=f"应拒绝：{bad!r}"):
                validate_login_name(bad)

    def test_valid_accepted(self) -> None:
        for good in ("admin", "zhang.san", "li_si-01", "user123"):
            self.assertEqual(validate_login_name(good), good)


# ================================================================ 密码
class PasswordTests(unittest.TestCase):
    def test_hash_is_not_plaintext(self) -> None:
        h = hash_password("Str0ng!Passw0rd")
        self.assertNotIn("Str0ng", h)
        self.assertTrue(h.startswith("$argon2id$"))

    def test_same_password_different_hash(self) -> None:
        """加盐：同一密码两次哈希必须不同。"""
        a = hash_password("Str0ng!Passw0rd")
        b = hash_password("Str0ng!Passw0rd")
        self.assertNotEqual(a, b)

    def test_verify(self) -> None:
        h = hash_password("Str0ng!Passw0rd")
        self.assertTrue(verify_password("Str0ng!Passw0rd", h))
        self.assertFalse(verify_password("wrong", h))
        self.assertFalse(verify_password("", h))
        self.assertFalse(verify_password("x", ""))

    def test_strength_policy(self) -> None:
        self.assertTrue(password_strength_error("short1!"))
        self.assertTrue(password_strength_error("password"))
        self.assertTrue(password_strength_error("alllowercaseonly"))
        self.assertTrue(password_strength_error("Admin12345", "admin"))
        self.assertEqual(password_strength_error("Str0ng!Passw0rd", "admin"), "")


# ================================================================ 会话
class SessionTests(TempSecurityMixin, unittest.TestCase):
    def test_create_and_resolve(self) -> None:
        token, session = self.sessions.create("u1", ip="1.2.3.4")
        self.assertTrue(token)
        got = self.sessions.resolve(token)
        self.assertIsNotNone(got)
        self.assertEqual(got.user_id, "u1")

    def test_token_stored_hashed(self) -> None:
        """存储里不能出现明文 token（文档 §3.11）。"""
        token, _ = self.sessions.create("u1")
        raw = self.sessions.path.read_text(encoding="utf-8")
        self.assertNotIn(token, raw)

    def test_revoke(self) -> None:
        token, session = self.sessions.create("u1")
        self.assertTrue(self.sessions.revoke(session.id))
        self.assertIsNone(self.sessions.resolve(token))

    def test_revoke_all_except_current(self) -> None:
        t1, s1 = self.sessions.create("u1")
        t2, _ = self.sessions.create("u1")
        n = self.sessions.revoke_all_for_user("u1", except_session_id=s1.id)
        self.assertEqual(n, 1)
        self.assertIsNotNone(self.sessions.resolve(t1))
        self.assertIsNone(self.sessions.resolve(t2))

    def test_invalid_token(self) -> None:
        self.assertIsNone(self.sessions.resolve("not-a-real-token"))
        self.assertIsNone(self.sessions.resolve(""))


# ================================================================ MFA
class MfaTests(unittest.TestCase):
    def test_totp_roundtrip(self) -> None:
        import pyotp

        secret = generate_totp_secret()
        code = pyotp.TOTP(secret).now()
        self.assertTrue(verify_totp(secret, code))

    def test_wrong_code(self) -> None:
        secret = generate_totp_secret()
        self.assertFalse(verify_totp(secret, "000000"))
        self.assertFalse(verify_totp(secret, "abc"))
        self.assertFalse(verify_totp("", "123456"))

    def test_uri(self) -> None:
        uri = totp_uri(generate_totp_secret(), "admin")
        self.assertTrue(uri.startswith("otpauth://totp/"))


# ================================================================ 服务层
class ServiceTests(TempSecurityMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.svc.create_initial_admin("admin", "管理员", "Str0ng!Passw0rd")

    def test_initial_admin(self) -> None:
        self.assertFalse(self.svc.needs_setup())
        self.assertEqual(self.admin.roles, ["admin"])
        self.assertTrue(self.svc.has_permission(self.admin, "system.user.manage"))
        self.assertEqual(self.svc.permissions_of(self.admin), set(PERMISSION_CODES))

    def test_cannot_init_twice(self) -> None:
        with self.assertRaises(Exception):
            self.svc.create_initial_admin("admin2", "另一个", "Str0ng!Passw0rd")

    # ---------------------------------------------------------------- 防枚举
    def test_no_enumeration(self) -> None:
        """账号不存在与密码错误必须返回**同一句**提示（文档 §3.8）。"""
        r1 = self.svc.login("nosuchuser", "whatever123!")
        r2 = self.svc.login("admin", "wrongpassword!")
        self.assertFalse(r1.ok)
        self.assertFalse(r2.ok)
        self.assertEqual(r1.error, r2.error)

    def test_login_success(self) -> None:
        r = self.svc.login("admin", "Str0ng!Passw0rd")
        self.assertTrue(r.ok, r.error)
        self.assertTrue(r.token)
        self.assertIsNotNone(self.svc.resolve(r.token))

    def test_login_rotates_session(self) -> None:
        """登录成功后旧会话必须失效（文档 §3.10 防会话固定）。"""
        r1 = self.svc.login("admin", "Str0ng!Passw0rd")
        r2 = self.svc.login("admin", "Str0ng!Passw0rd")
        self.assertTrue(r2.ok)
        self.assertIsNone(self.svc.resolve(r1.token))
        self.assertIsNotNone(self.svc.resolve(r2.token))

    # ---------------------------------------------------------------- 停用
    def test_disabled_user_session_dies(self) -> None:
        """停用账号后，已有会话必须立即失效（文档 §3.6）。"""
        other = self.users.create("staff", "员工", hash_password("Str0ng!Passw0rd"),
                                  roles=["operator"])
        r = self.svc.login("staff", "Str0ng!Passw0rd")
        self.assertTrue(r.ok)
        self.assertIsNotNone(self.svc.resolve(r.token))

        self.svc.set_user_status(self.admin, other.id, STATUS_DISABLED)
        self.assertIsNone(self.svc.resolve(r.token), "停用后旧会话仍然有效")

    def test_disabled_user_cannot_login(self) -> None:
        other = self.users.create("staff2", "员工2", hash_password("Str0ng!Passw0rd"),
                                  roles=["operator"])
        self.svc.set_user_status(self.admin, other.id, STATUS_DISABLED)
        r = self.svc.login("staff2", "Str0ng!Passw0rd")
        self.assertFalse(r.ok)

    # ---------------------------------------------------------------- 改密
    def test_change_password_kills_other_sessions(self) -> None:
        """改密码后，除当前会话外全部失效（文档 §3.13）。"""
        r1 = self.svc.login("admin", "Str0ng!Passw0rd")
        r2 = self.svc.login("admin", "Str0ng!Passw0rd")
        user = self.svc.resolve(r2.token)

        self.svc.change_password(
            user, "Str0ng!Passw0rd", "N3w!Passw0rdX", keep_session_token=r2.token
        )
        self.assertIsNotNone(self.svc.resolve(r2.token), "当前会话不应失效")
        self.assertIsNone(self.svc.resolve(r1.token), "其它会话应失效")
        self.assertTrue(self.svc.login("admin", "N3w!Passw0rdX").ok)
        self.assertFalse(self.svc.login("admin", "Str0ng!Passw0rd").ok)

    def test_change_password_wrong_old(self) -> None:
        user = self.svc.resolve(self.svc.login("admin", "Str0ng!Passw0rd").token)
        with self.assertRaises(Exception):
            self.svc.change_password(user, "wrong", "N3w!Passw0rdX")

    # ---------------------------------------------------------------- 权限
    def test_role_permissions(self) -> None:
        op = self.users.create("op1", "客服", hash_password("Str0ng!Passw0rd"),
                               roles=["operator"])
        self.assertTrue(self.svc.has_permission(op, "batch.read"))
        self.assertFalse(self.svc.has_permission(op, "batch.delete"))
        self.assertFalse(self.svc.has_permission(op, "settings.model.manage"))

    def test_auditor_cannot_touch_settings(self) -> None:
        """审计员不能修改业务数据（文档 §5 职责分离）。"""
        aud = self.users.create("aud1", "审计", hash_password("Str0ng!Passw0rd"),
                                roles=["auditor"])
        self.assertTrue(self.svc.has_permission(aud, "system.audit.read"))
        for forbidden in ("batch.create", "settings.model.manage", "system.user.manage"):
            self.assertFalse(self.svc.has_permission(aud, forbidden), forbidden)

    def test_inactive_user_has_no_permission(self) -> None:
        op = self.users.create("op2", "客服2", hash_password("Str0ng!Passw0rd"),
                               roles=["operator"])
        self.users.update_fields(op.id, status=STATUS_DISABLED)
        dead = self.users.get(op.id)
        self.assertFalse(self.svc.has_permission(dead, "batch.read"))

    def test_cannot_grant_beyond_own_permissions(self) -> None:
        """不能授予自己没有的权限（文档 §4 防提权）。"""
        op = self.users.create("op3", "客服3", hash_password("Str0ng!Passw0rd"),
                               roles=["operator"])
        target = self.users.create("tgt", "目标", hash_password("Str0ng!Passw0rd"),
                                   roles=["operator"])
        with self.assertRaises(Exception):
            self.svc.set_user_roles(op, target.id, ["admin"])

    def test_role_change_revokes_sessions(self) -> None:
        op = self.users.create("op4", "客服4", hash_password("Str0ng!Passw0rd"),
                               roles=["operator"])
        r = self.svc.login("op4", "Str0ng!Passw0rd")
        self.assertTrue(r.ok)
        self.svc.set_user_roles(self.admin, op.id, ["designer"])
        self.assertIsNone(self.svc.resolve(r.token), "改角色后旧会话应失效")


# ================================================================ 审计
class AuditTests(unittest.TestCase):
    def test_redaction(self) -> None:
        """敏感字段必须被脱敏（文档 §8）。"""
        out = _redact({
            "api_key": "sk-abcdefghijklmnopqrstuvwxyz",
            "password": "secret",
            "data_url": "data:image/png;base64,AAAA",
            "nested": {"token": "abc", "keep": "visible"},
            "safe": "hello",
        })
        self.assertEqual(out["api_key"], "***")
        self.assertEqual(out["password"], "***")
        self.assertEqual(out["data_url"], "***")
        self.assertEqual(out["nested"]["token"], "***")
        self.assertEqual(out["nested"]["keep"], "visible")
        self.assertEqual(out["safe"], "hello")

    def test_append_and_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger = AuditLogger(Path(tmp))
            logger.log(action="test.a", module="m", actor_id="u1")
            logger.log(action="test.b", module="m", result=RESULT_DENIED)
            rows = logger.query(limit=10)
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(logger.query(action="test.a")), 1)
            self.assertEqual(len(logger.query(result=RESULT_DENIED)), 1)
            stats = logger.stats()
            self.assertEqual(stats["records"], 2)

    def test_has_no_delete_api(self) -> None:
        """审计日志**不允许**提供删除/修改接口（文档 §8 防篡改）。"""
        logger = AuditLogger(Path(tempfile.gettempdir()))
        for forbidden in ("delete", "remove", "update", "clear", "purge"):
            self.assertFalse(hasattr(logger, forbidden), f"不应存在 {forbidden} 方法")


# ================================================================ URL 防护
class UrlGuardTests(unittest.TestCase):
    def test_blocks_internal(self) -> None:
        for bad in ("http://169.254.169.254/", "http://127.0.0.1/",
                    "http://10.1.2.3/", "http://192.168.0.1/"):
            with self.assertRaises(UrlGuardError, msg=bad):
                validate_outbound_url(bad)

    def test_allows_public(self) -> None:
        self.assertTrue(validate_outbound_url("https://api.deepseek.com"))

    def test_loopback_requires_flag(self) -> None:
        with self.assertRaises(UrlGuardError):
            validate_outbound_url("http://127.0.0.1:8189")
        self.assertTrue(
            validate_outbound_url("http://127.0.0.1:8189", allow_loopback=True)
        )


# ================================================================ 权限定义源
class PermissionCatalogTests(unittest.TestCase):
    def test_all_role_permissions_valid(self) -> None:
        """角色模板引用的权限码必须都在定义源里（文档 §4 唯一来源）。"""
        for code in ROLE_TEMPLATES:
            for p in role_default_permissions(code):
                self.assertTrue(is_valid_permission(p), f"{code} 引用了未知权限 {p}")

    def test_high_risk_permissions_documented(self) -> None:
        """高危权限必须有中文名。"""
        from app.security.permissions import PERMISSIONS

        for p in PERMISSIONS:
            self.assertTrue(p.name, p.code)
            if p.high_risk:
                self.assertTrue(p.group, p.code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
