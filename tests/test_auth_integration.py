# -*- coding: utf-8 -*-
"""
认证与授权的**集成测试**（文档 §13）。

用 FastAPI TestClient 发起真实 HTTP 请求，覆盖：
    · 首次初始化（创建管理员）
    · 未登录访问 → 401
    · 登录成功 / 失败（防枚举）
    · 无权限访问 → 403（且写审计）
    · 会话 Cookie 属性
    · 登出
    · 安全响应头

全程使用临时目录，不触碰真实数据（文档 §13：隔离环境与合成数据）。
"""
from __future__ import annotations
import os

import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.security.audit import AuditLogger  # noqa: E402
from app.security.auth import SessionStore, hash_password  # noqa: E402
from app.security.service import SecurityService  # noqa: E402
from app.security.users import UserStore  # noqa: E402
import app.web.auth_routes as auth_routes  # noqa: E402
from app.web.server import app  # noqa: E402

# ⚠️ 测试夹具密码：**不能**默认成空串（空串会被「至少 10 位」的强度规则拒绝）。
#    给一个满足强度的固定测试值，可用环境变量覆盖。
ADMIN_PW = os.environ.get("SHS_ADMIN_PW") or "Str0ng!Passw0rd"


class AuthIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self._tmp.name)

        # 用临时存储替换全局单例，避免污染真实 data/
        users = UserStore(base / "security.json")
        sessions = SessionStore(base / "sessions.json")
        audit = AuditLogger(base / "audit")
        svc = SecurityService(users, sessions, audit)

        self._orig = auth_routes.security
        auth_routes.security = svc
        self.svc = svc
        self.audit = audit

        # 关闭启动时的静态检查干扰
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self) -> None:
        auth_routes.security = self._orig
        self.client.close()
        self._tmp.cleanup()

    # ---------------------------------------------------------------- 工具
    def _setup_admin(self) -> None:
        r = self.client.post("/api/auth/setup", json={
            "login_name": "admin",
            "display_name": "管理员",
            "password": ADMIN_PW,
        })
        self.assertEqual(r.status_code, 200, r.text)

    def _login(self, name: str = "admin", pw: str = ADMIN_PW):
        return self.client.post("/api/auth/login", json={
            "login_name": name, "password": pw,
        })

    # ---------------------------------------------------------------- 初始化
    def test_status_before_setup(self) -> None:
        r = self.client.get("/api/auth/status")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["needs_setup"])
        self.assertFalse(data["authenticated"])

    def test_setup_creates_admin(self) -> None:
        self._setup_admin()
        r = self.client.get("/api/auth/status")
        data = r.json()
        self.assertFalse(data["needs_setup"])
        self.assertTrue(data["authenticated"])

    def test_setup_only_once(self) -> None:
        self._setup_admin()
        r = self.client.post("/api/auth/setup", json={
            "login_name": "admin2", "display_name": "第二个", "password": ADMIN_PW,
        })
        self.assertEqual(r.status_code, 409)

    def test_setup_rejects_weak_password(self) -> None:
        r = self.client.post("/api/auth/setup", json={
            "login_name": "admin", "display_name": "管理员", "password": "123",
        })
        self.assertEqual(r.status_code, 400)

    # ---------------------------------------------------------------- 会话
    def test_me_requires_login(self) -> None:
        """未登录访问受保护接口必须 401（文档 §13 用例 1）。"""
        self._setup_admin()
        self.client.cookies.clear()
        r = self.client.get("/api/auth/me")
        self.assertEqual(r.status_code, 401)

    def test_cookie_security_attributes(self) -> None:
        """会话 Cookie 必须具备 HttpOnly / SameSite（文档 §3.11）。"""
        self._setup_admin()
        raw = "; ".join(f"{k}={v}" for k, v in
                        (self.client.cookies.items() if hasattr(self.client.cookies, "items")
                         else []))
        # TestClient 不暴露属性，改从响应头检查
        r = self._login()
        set_cookie = r.headers.get("set-cookie", "")
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=lax", set_cookie.lower().replace("samesite=lax", "SameSite=lax"))

    def test_login_and_me(self) -> None:
        self._setup_admin()
        r = self._login()
        self.assertEqual(r.status_code, 200, r.text)
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        data = me.json()
        self.assertEqual(data["user"]["login_name"], "admin")
        self.assertIn("system.user.manage", data["permissions"])

    def test_login_wrong_password(self) -> None:
        self._setup_admin()
        r = self._login(pw="wrongpassword!")
        self.assertEqual(r.status_code, 401)

    def test_no_enumeration_over_http(self) -> None:
        """账号不存在与密码错误返回**完全相同**的响应（文档 §3.8）。"""
        self._setup_admin()
        r1 = self._login(name="ghost", pw="whatever123!")
        r2 = self._login(name="admin", pw="wrongpassword!")
        self.assertEqual(r1.status_code, r2.status_code)
        self.assertEqual(r1.json()["detail"], r2.json()["detail"])

    def test_logout(self) -> None:
        self._setup_admin()
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    # ---------------------------------------------------------------- 权限
    def test_low_privilege_denied(self) -> None:
        """低权限用户访问管理接口必须 403（文档 §13 用例 10）。"""
        self._setup_admin()
        self.svc.users.create(
            "staff", "客服", hash_password(ADMIN_PW), roles=["operator"], status="active"
        )
        self.client.post("/api/auth/logout")
        r = self._login("staff")
        self.assertEqual(r.status_code, 200, r.text)

        # 「我的设备」是任何登录用户都能用的，先确认登录有效
        self.assertEqual(self.client.get("/api/auth/sessions").status_code, 200)

        # 客服没有 system.user.manage —— 用一个需要该权限的接口验证
        # （管理员接口尚未接入，这里直接验证服务层判断）
        staff = self.svc.users.get_by_login("staff")
        self.assertFalse(self.svc.has_permission(staff, "system.user.manage"))

    def test_denied_writes_audit(self) -> None:
        """被拒绝的访问必须写入审计（文档 §8）。"""
        self._setup_admin()
        staff = self.svc.users.create(
            "staff2", "客服2", hash_password(ADMIN_PW), roles=["operator"], status="active"
        )
        self.client.post("/api/auth/logout")
        self._login("staff2")

        # 直接调用依赖函数模拟受保护接口
        from fastapi import HTTPException
        from app.web.auth_routes import require_permission
        dep = require_permission("system.user.manage")

        class FakeReq:
            method = "POST"
            url = type("U", (), {"path": "/api/admin/users"})()
            client = type("C", (), {"host": "127.0.0.1"})()
            cookies = {"shs_session": ""}

        # 无 cookie 时是 401；这里改用真实 cookie
        token = self.client.cookies.get("shs_session")
        FakeReq.cookies = {"shs_session": token}
        with self.assertRaises(HTTPException) as ctx:
            dep(FakeReq())
        self.assertEqual(ctx.exception.status_code, 403)

        denied = self.audit.query(result="denied")
        self.assertTrue(any(r.get("action") == "access.denied" for r in denied),
                        "拒绝访问未写审计")

    # ---------------------------------------------------------------- 安全头
    def test_security_headers(self) -> None:
        r = self.client.get("/api/auth/status")
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        self.assertEqual(r.headers.get("referrer-policy"), "no-referrer")

    # ---------------------------------------------------------------- 会话管理
    def test_list_and_revoke_own_sessions(self) -> None:
        self._setup_admin()
        data = self.client.get("/api/auth/sessions").json()
        self.assertGreaterEqual(len(data["sessions"]), 1)
        self.assertTrue(any(s["current"] for s in data["sessions"]))

    def test_change_password(self) -> None:
        self._setup_admin()
        r = self.client.post("/api/auth/password", json={
            "old_password": ADMIN_PW, "new_password": "N3w!Passw0rdX",
        })
        self.assertEqual(r.status_code, 200, r.text)
        self.client.post("/api/auth/logout")
        self.assertEqual(self._login(pw=ADMIN_PW).status_code, 401)
        self.assertEqual(self._login(pw="N3w!Passw0rdX").status_code, 200)

    # ---------------------------------------------------------------- SSRF
    def test_ssrf_blocked_via_http(self) -> None:
        """前端传内网 base_url 必须被拒绝（文档 §13 用例 1~2）。"""
        self._setup_admin()
        for bad in ("http://169.254.169.254/", "http://127.0.0.1:22"):
            r = self.client.post("/api/settings/test", json={
                "provider": "openai", "api_key": "sk-test", "base_url": bad,
            })
            body = r.text
            self.assertIn("安全校验", body, f"{bad} 未被拦截：{body[:200]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
