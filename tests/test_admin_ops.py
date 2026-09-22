# -*- coding: utf-8 -*-
"""管理后台运维操作测试（P0-1：重置密码）。

覆盖文档 §7.1 的安全约束：
    · 不能重置自己的密码（应走「修改密码」并验证原密码）
    · 重置后旧会话必须失效
    · 重置后必须强制下次登录改密
    · 密码强度与自助改密同一套规则（不因管理员操作而放宽）
    · 无权限者 403
    · 全程写审计
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
from app.security.auth import SessionStore, hash_password, verify_password  # noqa: E402
from app.security.service import SecurityService  # noqa: E402
from app.security.users import UserStore  # noqa: E402
import app.web.auth_routes as auth_routes  # noqa: E402
from app.web.server import app  # noqa: E402

ADMIN_PW = os.environ.get("SHS_ADMIN_PW", "")
NEW_PW = "N3w!Passw0rdX"


class ResetPasswordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self._tmp.name)
        users = UserStore(base / "security.json")
        sessions = SessionStore(base / "sessions.json")
        audit = AuditLogger(base / "audit")
        self.svc = SecurityService(users, sessions, audit)
        self.audit = audit

        self._orig = auth_routes.security
        auth_routes.security = self.svc

        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/api/auth/setup", json={
            "login_name": "admin", "display_name": "管理员", "password": ADMIN_PW,
        })
        self.staff = self.svc.users.create(
            "staff", "员工", hash_password(ADMIN_PW), roles=["operator"]
        )

    def tearDown(self) -> None:
        auth_routes.security = self._orig
        self.client.close()
        self._tmp.cleanup()

    # ---------------------------------------------------------------- 正常路径
    def test_reset_success(self) -> None:
        r = self.client.post(f"/api/admin/users/{self.staff.id}/reset-password",
                             json={"new_password": NEW_PW})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIn("revoked", data)

        fresh = self.svc.users.get(self.staff.id)
        self.assertTrue(verify_password(NEW_PW, fresh.password_hash))
        self.assertFalse(verify_password(ADMIN_PW, fresh.password_hash))

    def test_forces_password_change(self) -> None:
        """重置后必须要求本人下次登录改密（管理员知道临时密码）。"""
        self.client.post(f"/api/admin/users/{self.staff.id}/reset-password",
                         json={"new_password": NEW_PW})
        fresh = self.svc.users.get(self.staff.id)
        self.assertTrue(fresh.must_change_password, "未设置强制改密标记")

    def test_revokes_existing_sessions(self) -> None:
        """重置后该用户已有会话必须立即失效。"""
        # 让 staff 登录拿到会话
        staff_client = TestClient(app, follow_redirects=False)
        r = staff_client.post("/api/auth/login",
                              json={"login_name": "staff", "password": ADMIN_PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(staff_client.get("/api/auth/me").status_code, 200)

        # 管理员重置
        self.client.post(f"/api/admin/users/{self.staff.id}/reset-password",
                         json={"new_password": NEW_PW})
        # 旧会话失效
        self.assertEqual(staff_client.get("/api/auth/me").status_code, 401,
                         "重置密码后旧会话仍然有效")
        staff_client.close()

    def test_old_password_no_longer_works(self) -> None:
        self.client.post(f"/api/admin/users/{self.staff.id}/reset-password",
                         json={"new_password": NEW_PW})
        self.client.post("/api/auth/logout")
        self.assertEqual(
            self.client.post("/api/auth/login",
                             json={"login_name": "staff", "password": ADMIN_PW}).status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/api/auth/login",
                             json={"login_name": "staff", "password": NEW_PW}).status_code,
            200,
        )

    def test_unlocks_account(self) -> None:
        """重置密码应同时解除登录失败锁定（否则本人还是进不去）。"""
        self.svc.users.update_fields(self.staff.id, failed_attempts=5,
                                     locked_until="2099-01-01T00:00:00+08:00")
        self.client.post(f"/api/admin/users/{self.staff.id}/reset-password",
                         json={"new_password": NEW_PW})
        fresh = self.svc.users.get(self.staff.id)
        self.assertEqual(fresh.failed_attempts, 0)
        self.assertEqual(fresh.locked_until, "")

    # ---------------------------------------------------------------- 安全约束
    def test_cannot_reset_self(self) -> None:
        """不能重置自己 —— 必须走「修改密码」并验证原密码。"""
        me = self.svc.users.get_by_login("admin")
        r = self.client.post(f"/api/admin/users/{me.id}/reset-password",
                             json={"new_password": NEW_PW})
        self.assertEqual(r.status_code, 400)
        self.assertIn("修改密码", r.json()["detail"])

    def test_weak_password_rejected(self) -> None:
        """强度规则与自助改密一致，不因管理员操作而放宽。"""
        for weak in ("short", "12345678", "password", "alllowercase"):
            r = self.client.post(f"/api/admin/users/{self.staff.id}/reset-password",
                                 json={"new_password": weak})
            self.assertEqual(r.status_code, 400, f"弱密码被接受：{weak}")

    def test_password_must_not_contain_login_name(self) -> None:
        r = self.client.post(f"/api/admin/users/{self.staff.id}/reset-password",
                             json={"new_password": "Staff12345!"})
        self.assertEqual(r.status_code, 400)

    def test_missing_password(self) -> None:
        r = self.client.post(f"/api/admin/users/{self.staff.id}/reset-password", json={})
        self.assertEqual(r.status_code, 400)

    def test_unknown_user(self) -> None:
        r = self.client.post("/api/admin/users/no-such-id/reset-password",
                             json={"new_password": NEW_PW})
        self.assertEqual(r.status_code, 404)

    def test_requires_permission(self) -> None:
        """低权限用户（operator）不能重置他人密码。"""
        low = TestClient(app, follow_redirects=False)
        low.post("/api/auth/login", json={"login_name": "staff", "password": ADMIN_PW})
        r = low.post(f"/api/admin/users/{self.staff.id}/reset-password",
                     json={"new_password": NEW_PW})
        self.assertEqual(r.status_code, 403)
        low.close()

    def test_unauthenticated_denied(self) -> None:
        anon = TestClient(app, follow_redirects=False)
        r = anon.post(f"/api/admin/users/{self.staff.id}/reset-password",
                      json={"new_password": NEW_PW})
        self.assertEqual(r.status_code, 401)
        anon.close()

    # ---------------------------------------------------------------- 审计
    def test_writes_audit(self) -> None:
        """重置密码必须写审计（账号接管类高危操作）。"""
        self.client.post(f"/api/admin/users/{self.staff.id}/reset-password",
                         json={"new_password": NEW_PW})
        rows = self.audit.query(action="user.reset_password")
        self.assertTrue(rows, "未写审计")
        rec = rows[0]
        self.assertEqual(rec["result"], "success")
        self.assertEqual(rec["target_id"], self.staff.id)
        self.assertTrue(rec["changes"].get("high_risk"))

    def test_audit_does_not_leak_password(self) -> None:
        """审计里**不得出现**明文密码（文档 §8）。"""
        self.client.post(f"/api/admin/users/{self.staff.id}/reset-password",
                         json={"new_password": NEW_PW})
        import json as _json

        rows = self.audit.query(action="user.reset_password")
        blob = _json.dumps(rows, ensure_ascii=False)
        self.assertNotIn(NEW_PW, blob, "审计泄漏了明文密码")

    def test_response_has_no_hash(self) -> None:
        r = self.client.post(f"/api/admin/users/{self.staff.id}/reset-password",
                             json={"new_password": NEW_PW})
        self.assertNotIn("password_hash", r.text)
        self.assertNotIn("argon2", r.text)


class AuditStatsTests(unittest.TestCase):
    """P1-1：审计聚合统计。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self._tmp.name)
        self.svc = SecurityService(UserStore(base / "security.json"),
                                   SessionStore(base / "sessions.json"),
                                   AuditLogger(base / "audit"))
        self.audit = self.svc.audit
        self._orig = auth_routes.security
        auth_routes.security = self.svc
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/api/auth/setup", json={
            "login_name": "admin", "display_name": "管理员", "password": ADMIN_PW,
        })
        # 造一些审计记录
        for i in range(5):
            self.audit.log(action="test.ok", module="test", result="success",
                           actor_id="u1", actor_name="甲")
        for i in range(3):
            self.audit.log(action="test.denied", module="test", result="denied",
                           actor_id="u2", actor_name="乙")

    def tearDown(self) -> None:
        auth_routes.security = self._orig
        self.client.close()
        self._tmp.cleanup()

    def test_stats_structure(self) -> None:
        r = self.client.get("/api/admin/audit/stats?days=1&bucket=hour")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        for key in ("total", "failed", "failed_rate", "buckets",
                    "top_actors", "top_actions", "top_denied", "results"):
            self.assertIn(key, d)
        self.assertEqual(len(d["buckets"]), 24)
        self.assertGreaterEqual(d["total"], 8)
        self.assertGreaterEqual(d["failed"], 3)

    def test_top_actors(self) -> None:
        d = self.client.get("/api/admin/audit/stats?days=1").json()
        names = {x["name"]: x["n"] for x in d["top_actors"]}
        self.assertEqual(names.get("甲"), 5)
        self.assertEqual(names.get("乙"), 3)

    def test_top_denied(self) -> None:
        d = self.client.get("/api/admin/audit/stats?days=1").json()
        denied = {x["name"]: x["n"] for x in d["top_denied"]}
        self.assertEqual(denied.get("test.denied"), 3)

    def test_day_buckets(self) -> None:
        d = self.client.get("/api/admin/audit/stats?days=7&bucket=day").json()
        self.assertEqual(len(d["buckets"]), 7)

    def test_param_validation(self) -> None:
        self.assertEqual(self.client.get("/api/admin/audit/stats?days=0").status_code, 422)
        self.assertEqual(self.client.get("/api/admin/audit/stats?days=999").status_code, 422)
        self.assertEqual(
            self.client.get("/api/admin/audit/stats?bucket=week").status_code, 422)

    def test_requires_permission(self) -> None:
        self.svc.users.create("low", "低权限", hash_password(ADMIN_PW),
                              roles=["operator"])
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"login_name": "low", "password": ADMIN_PW})
        self.assertEqual(self.client.get("/api/admin/audit/stats").status_code, 403)


class RolesMatrixTests(unittest.TestCase):
    """P1-2：权限矩阵。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self._tmp.name)
        self.svc = SecurityService(UserStore(base / "security.json"),
                                   SessionStore(base / "sessions.json"),
                                   AuditLogger(base / "audit"))
        self._orig = auth_routes.security
        auth_routes.security = self.svc
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/api/auth/setup", json={
            "login_name": "admin", "display_name": "管理员", "password": ADMIN_PW,
        })

    def tearDown(self) -> None:
        auth_routes.security = self._orig
        self.client.close()
        self._tmp.cleanup()

    def test_matrix_structure(self) -> None:
        r = self.client.get("/api/admin/roles/matrix")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        for key in ("groups", "roles", "matrix", "stats"):
            self.assertIn(key, d)
        self.assertEqual(d["stats"]["total_roles"], len(d["roles"]))
        self.assertGreaterEqual(d["stats"]["total_permissions"], 15)

    def test_admin_has_all(self) -> None:
        d = self.client.get("/api/admin/roles/matrix").json()
        admin = d["matrix"]["admin"]
        self.assertEqual(len(admin), d["stats"]["total_permissions"])
        self.assertTrue(all(admin.values()))

    def test_role_boundaries(self) -> None:
        """各角色的边界必须与角色模板一致。"""
        d = self.client.get("/api/admin/roles/matrix").json()
        m = d["matrix"]
        self.assertFalse(m["operator"].get("batch.create"))
        self.assertFalse(m["operator"].get("system.user.manage"))
        self.assertTrue(m["auditor"].get("system.audit.read"))
        self.assertFalse(m["auditor"].get("batch.create"))
        self.assertTrue(m["designer"].get("batch.create"))
        self.assertFalse(m["designer"].get("system.user.manage"))

    def test_groups_cover_all_permissions(self) -> None:
        d = self.client.get("/api/admin/roles/matrix").json()
        total = sum(len(g["permissions"]) for g in d["groups"])
        self.assertEqual(total, d["stats"]["total_permissions"])

    def test_orphan_and_empty_diagnostics(self) -> None:
        d = self.client.get("/api/admin/roles/matrix").json()
        self.assertIsInstance(d["stats"]["orphan_permissions"], list)
        self.assertIsInstance(d["stats"]["empty_roles"], list)

    def test_new_role_starts_empty(self) -> None:
        """新建角色应为空权限（避免误授）。"""
        self.client.post("/api/admin/roles", json={"code": "newrole", "name": "新角色"})
        d = self.client.get("/api/admin/roles/matrix").json()
        self.assertFalse(any(d["matrix"]["newrole"].values()))
        self.assertIn("newrole", d["stats"]["empty_roles"])

    def test_requires_permission(self) -> None:
        self.svc.users.create("low2", "低权限", hash_password(ADMIN_PW),
                              roles=["operator"])
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"login_name": "low2", "password": ADMIN_PW})
        self.assertEqual(self.client.get("/api/admin/roles/matrix").status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
