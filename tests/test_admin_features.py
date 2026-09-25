# -*- coding: utf-8 -*-
"""P2 / P3 / 批3 的管理后台功能测试。

覆盖关键安全约束与边界，不重复端到端已覆盖的部分。
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

# ⚠️ 测试夹具密码：**不能**默认成空串。
#    早先被批量脚本改成 `os.environ.get("SHS_ADMIN_PW", "")`，没设环境变量时
#    就是空密码，被「至少 10 位」的强度规则拒绝，一次性挂掉 66 个测试。
#    这里给一个满足强度规则的固定测试值（不匹配任何真实账号），
#    需要时仍可用环境变量覆盖。
PW = os.environ.get("SHS_ADMIN_PW") or "Str0ng!Passw0rd"


class Base(unittest.TestCase):
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
            "login_name": "admin", "display_name": "管理员", "password": PW})
        self.admin_id = self.svc.users.get_by_login("admin").id

    def tearDown(self) -> None:
        auth_routes.security = self._orig
        self.client.close()
        self._tmp.cleanup()

    def mkuser(self, name: str, roles: list[str] | None = None):
        return self.svc.users.create(name, name, hash_password(PW),
                                     roles=roles or ["operator"])


class UserQueryTests(Base):
    def test_pagination(self) -> None:
        for i in range(7):
            self.mkuser(f"u{i:02d}")
        d = self.client.get("/api/admin/users?size=3&page=1").json()
        self.assertEqual(d["total"], 8)          # 7 + admin
        self.assertEqual(len(d["users"]), 3)
        self.assertEqual(d["pages"], 3)

    def test_search_by_login_and_name(self) -> None:
        self.mkuser("zhangsan")
        self.assertEqual(self.client.get("/api/admin/users?q=zhang").json()["total"], 1)
        self.assertEqual(self.client.get("/api/admin/users?q=ZHANG").json()["total"], 1,
                         "搜索应大小写不敏感")
        self.assertEqual(self.client.get("/api/admin/users?q=不存在").json()["total"], 0)

    def test_filter_by_role_and_status(self) -> None:
        self.mkuser("op1", ["operator"])
        self.mkuser("ds1", ["designer"])
        self.assertEqual(self.client.get("/api/admin/users?role=designer").json()["total"], 1)
        self.svc.users.update_fields(
            self.svc.users.get_by_login("op1").id, status="disabled")
        self.assertEqual(
            self.client.get("/api/admin/users?status=disabled&role=operator").json()["total"], 1)

    def test_detail_structure(self) -> None:
        uid = self.mkuser("detailuser").id
        d = self.client.get(f"/api/admin/users/{uid}").json()
        self.assertEqual(d["user"]["login_name"], "detailuser")
        self.assertEqual(len(d["permissions"]), len(d["permission_details"]))
        self.assertIn("recent_actions", d)

    def test_detail_404(self) -> None:
        self.assertEqual(self.client.get("/api/admin/users/nope").status_code, 404)

    def test_export_route_not_shadowed(self) -> None:
        """⭐ 回归：/users/export 不能被 /users/{user_id} 抢走。

        FastAPI 按定义顺序匹配，静态路径必须在动态路径之前 ——
        这个 bug 曾导致导出返回 404「用户不存在」。
        """
        r = self.client.get("/api/admin/users/export")
        self.assertEqual(r.status_code, 200, r.text[:150])
        self.assertIn("登录名", r.text)

    def test_export_has_no_secrets(self) -> None:
        self.mkuser("expuser")
        r = self.client.get("/api/admin/users/export")
        self.assertNotIn("$argon2", r.text)
        self.assertNotIn("password_hash", r.text)
        self.assertIn("expuser", r.text)

    def test_export_writes_audit(self) -> None:
        self.client.get("/api/admin/users/export")
        self.assertTrue(self.svc.audit.query(action="user.export"))


class BatchTests(Base):
    def test_batch_disable_skips_self(self) -> None:
        u1 = self.mkuser("batch1")
        u2 = self.mkuser("batch2")
        r = self.client.post("/api/admin/users/batch", json={
            "action": "disable", "user_ids": [u1.id, u2.id, self.admin_id]})
        d = r.json()
        self.assertEqual(d["succeeded"], 2)
        self.assertEqual(d["skipped_self"], 1)
        self.assertEqual(self.svc.users.get(self.admin_id).status, "active",
                         "不应停用自己")

    def test_batch_enable_and_unlock(self) -> None:
        u = self.mkuser("batch3")
        self.svc.users.update_fields(u.id, status="disabled", failed_attempts=5,
                                     locked_until="2099-01-01T00:00:00+08:00")
        self.client.post("/api/admin/users/batch",
                         json={"action": "enable", "user_ids": [u.id]})
        self.assertEqual(self.svc.users.get(u.id).status, "active")
        self.client.post("/api/admin/users/batch",
                         json={"action": "unlock", "user_ids": [u.id]})
        fresh = self.svc.users.get(u.id)
        self.assertEqual(fresh.failed_attempts, 0)
        self.assertEqual(fresh.locked_until, "")

    def test_batch_invalid_action(self) -> None:
        u = self.mkuser("batch4")
        self.assertEqual(self.client.post("/api/admin/users/batch", json={
            "action": "delete_everything", "user_ids": [u.id]}).status_code, 400)

    def test_batch_empty_ids(self) -> None:
        self.assertEqual(self.client.post("/api/admin/users/batch", json={
            "action": "disable", "user_ids": []}).status_code, 400)

    def test_batch_writes_audit(self) -> None:
        u = self.mkuser("batch5")
        self.client.post("/api/admin/users/batch",
                         json={"action": "disable", "user_ids": [u.id]})
        self.assertTrue(self.svc.audit.query(action="user.batch_disable"))


class RoleCopyTests(Base):
    def test_copy_role(self) -> None:
        r = self.client.post("/api/admin/roles/operator/copy", json={"code": "op2"})
        self.assertEqual(r.status_code, 200, r.text)
        roles = self.svc.users.list_roles()
        self.assertEqual(sorted(roles["op2"]["permissions"]),
                         sorted(roles["operator"]["permissions"]))

    def test_copy_conflict(self) -> None:
        self.assertEqual(
            self.client.post("/api/admin/roles/operator/copy",
                             json={"code": "operator"}).status_code, 409)

    def test_copy_unknown_source(self) -> None:
        self.assertEqual(
            self.client.post("/api/admin/roles/nope/copy",
                             json={"code": "x"}).status_code, 404)

    def test_copy_sanitizes_code(self) -> None:
        r = self.client.post("/api/admin/roles/operator/copy",
                             json={"code": "Bad Code!!"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["code"], "badcode")

    def test_matrix_after_copy(self) -> None:
        self.client.post("/api/admin/roles/operator/copy", json={"code": "op3"})
        d = self.client.get("/api/admin/roles/matrix").json()
        self.assertIn("op3", d["matrix"])
        self.assertTrue(d["matrix"]["op3"]["batch.read"])


class SystemMaintenanceTests(Base):
    def test_disk_listing(self) -> None:
        d = self.client.get("/api/admin/system/disk").json()
        self.assertGreaterEqual(len(d["items"]), 5)
        for item in d["items"]:
            self.assertIn(item["level"], ("safe", "caution", "keep"))
            self.assertIn("note", item)

    def test_disk_marks_output_as_keep(self) -> None:
        """批次图片必须标为不可清理（AGENTS.md 硬约束）。"""
        d = self.client.get("/api/admin/system/disk").json()
        batch = next((i for i in d["items"] if "批次图片" in i["name"]), None)
        self.assertIsNotNone(batch)
        self.assertEqual(batch["level"], "keep")

    def test_cleanup_preview(self) -> None:
        d = self.client.get("/api/admin/system/cleanup/preview").json()
        for key in ("log_files", "sessions", "backups", "corrupt_files"):
            self.assertIn(key, d)

    def test_cleanup_does_not_touch_output(self) -> None:
        """清理不得删除 output/ 下的任何文件。"""
        r = self.client.post("/api/admin/system/cleanup",
                             json={"logs": False, "sessions": True,
                                   "corrupt_files": False})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertNotIn("output", str(d.get("removed", {})))

    def test_cleanup_writes_audit(self) -> None:
        self.client.post("/api/admin/system/cleanup", json={"logs": False})
        self.assertTrue(self.svc.audit.query(action="system.cleanup"))

    def test_log_name_traversal_blocked(self) -> None:
        """日志文件名必须防目录穿越。"""
        for bad in ("../../config/settings.json", "..\\..\\secret.log",
                    "sub/dir.log", "settings.json"):
            r = self.client.get(f"/api/admin/system/logs?name={bad}")
            self.assertIn(r.status_code, (400, 404), f"{bad} 未被拦截")

    def test_log_list(self) -> None:
        d = self.client.get("/api/admin/system/logs").json()
        self.assertIn("files", d)


class AuditPagingTests(Base):
    def test_paging_fields(self) -> None:
        for i in range(12):
            self.svc.audit.log(action=f"t{i}", module="test")
        d = self.client.get("/api/admin/audit?limit=5&offset=0").json()
        self.assertIn("has_more", d)
        self.assertLessEqual(len(d["records"]), 5)

    def test_offset(self) -> None:
        for i in range(10):
            self.svc.audit.log(action=f"o{i}", module="test")
        first = self.client.get("/api/admin/audit?limit=3&offset=0").json()["records"]
        second = self.client.get("/api/admin/audit?limit=3&offset=3").json()["records"]
        self.assertNotEqual([r["action"] for r in first], [r["action"] for r in second])

    def test_trace_by_request_id(self) -> None:
        self.svc.audit.log(action="trace.a", module="test", request_id="req-xyz")
        self.svc.audit.log(action="trace.b", module="test", request_id="req-xyz")
        self.svc.audit.log(action="other", module="test", request_id="req-other")
        d = self.client.get("/api/admin/audit/trace?request_id=req-xyz").json()
        self.assertEqual(d["count"], 2)
        self.assertTrue(all(r["request_id"] == "req-xyz" for r in d["records"]))

    def test_trace_requires_id(self) -> None:
        self.assertEqual(self.client.get("/api/admin/audit/trace").status_code, 422)


class PermissionBoundaryTests(Base):
    def test_low_privilege_blocked(self) -> None:
        self.mkuser("lowp", ["operator"])
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"login_name": "lowp", "password": PW})
        for path in ("/api/admin/users", "/api/admin/users/export",
                     "/api/admin/system/disk", "/api/admin/system/logs",
                     "/api/admin/system/cleanup/preview", "/api/admin/audit/trace?request_id=x",
                     "/api/admin/roles/matrix"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 403, f"{path} 未拦截（{r.status_code}）")

    def test_auditor_can_read_not_write(self) -> None:
        self.mkuser("aud1", ["auditor"])
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", json={"login_name": "aud1", "password": PW})
        # 可读
        self.assertEqual(self.client.get("/api/admin/system/disk").status_code, 200)
        self.assertEqual(self.client.get("/api/admin/audit").status_code, 200)
        # 不可写
        self.assertEqual(self.client.post("/api/admin/system/cleanup",
                                          json={}).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/users").status_code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
