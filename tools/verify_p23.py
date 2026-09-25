# -*- coding: utf-8 -*-
"""
P2 + P3 + 批3 端到端验证。

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_p23.py
"""
from __future__ import annotations
import os

import json
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE = "http://127.0.0.1:8000"
ADMIN_PW = os.environ.get("SHS_ADMIN_PW", "")            # 真实 admin 密码（登录用）
FIXTURE_PW = "Str0ng!Passw0rd"  # 新建测试用户用：必须满足强度规则（≥10 位）
RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    RESULTS.append((ok, label))
    print(f"  {'✅' if ok else '❌'} {label}" + (f"   {extra}" if extra else ""))


class Client:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(
            BASE + path, data=data, method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with self.opener.open(req, timeout=40) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            return -1, f"{type(exc).__name__}: {exc}"


def main() -> int:
    print("=" * 78)
    print("P2 + P3 + 批3 验证")
    print("=" * 78)

    c = Client()
    st, body = c.call("POST", "/api/auth/login",
                      {"login_name": "admin", "password": ADMIN_PW})
    if st != 200:
        print(f"  ❌ 登录失败：{st} {body[:150]}")
        return 1
    check(True, "管理员登录")

    # ================= P2：用户搜索/分页/详情 =================
    st, body = c.call("GET", "/api/admin/users?size=2&page=1")
    if st == 200:
        d = json.loads(body)
        check("total" in d and "pages" in d, "用户列表返回分页信息",
              f"total={d.get('total')} pages={d.get('pages')}")
        check(len(d["users"]) <= 2, "size 参数生效", f"{len(d['users'])} 条")
    else:
        check(False, "用户列表分页", f"{st}")

    st, body = c.call("GET", "/api/admin/users?q=admin")
    if st == 200:
        d = json.loads(body)
        check(any(u["login_name"] == "admin" for u in d["users"]), "搜索 q=admin 命中")

    st, body = c.call("GET", "/api/admin/users?q=__不存在__")
    if st == 200:
        check(json.loads(body)["total"] == 0, "搜索无结果时 total=0")

    st, body = c.call("GET", "/api/admin/users?role=admin")
    if st == 200:
        d = json.loads(body)
        check(all("admin" in u["roles"] for u in d["users"]), "按角色筛选生效")

    st, body = c.call("GET", "/api/admin/users?status=nonexistent")
    check(st == 200 and json.loads(body)["total"] == 0, "按不存在状态筛选 → 0")

    st, body = c.call("GET", "/api/admin/users")
    admin_id = next((u["id"] for u in json.loads(body)["users"]
                     if u["login_name"] == "admin"), None) if st == 200 else None

    if admin_id:
        st, body = c.call("GET", f"/api/admin/users/{admin_id}")
        if st == 200:
            d = json.loads(body)
            for key in ("user", "permissions", "permission_details", "sessions",
                        "recent_actions", "role_names"):
                check(key in d, f"用户详情含 {key}")
            check(len(d["permission_details"]) == len(d["permissions"]),
                  "权限明细与权限码数量一致")

        st, _ = c.call("GET", "/api/admin/users/no-such-id")
        check(st == 404, "不存在的用户 → 404", f"实际 {st}")

    # ================= P2：会话 =================
    st, body = c.call("GET", "/api/admin/sessions")
    pre = len(json.loads(body).get("sessions", [])) if st == 200 else 0
    st, body = c.call("POST", "/api/admin/sessions/cleanup")
    check(st == 200 and "removed" in json.loads(body), "清理过期会话 → 200",
          f"清理 {json.loads(body).get('removed') if st == 200 else '?'} 条")

    # ================= P2：审计分页 =================
    st, body = c.call("GET", "/api/admin/audit?limit=5&offset=0")
    if st == 200:
        d = json.loads(body)
        check("has_more" in d and "offset" in d, "审计返回分页字段")
        check(len(d["records"]) <= 5, "limit 生效", f"{len(d['records'])} 条")

    st, body = c.call("GET", "/api/admin/audit?limit=5&offset=5")
    if st == 200:
        check(json.loads(body)["offset"] == 5, "offset 生效")

    # ================= P3：磁盘与清理 =================
    st, body = c.call("GET", "/api/admin/system/disk")
    if st == 200:
        d = json.loads(body)
        check(len(d["items"]) >= 5, "磁盘明细返回多项", f"{len(d['items'])} 项")
        check(all("level" in i for i in d["items"]), "每项含清理建议等级")
        check(all(i["level"] in ("safe", "caution", "keep") for i in d["items"]),
              "等级取值合法")
        check("total_mb" in d, "返回总体积")
    else:
        check(False, "磁盘明细", f"{st}")

    st, body = c.call("GET", "/api/admin/system/cleanup/preview")
    if st == 200:
        d = json.loads(body)
        for key in ("log_files", "sessions", "backups", "corrupt_files"):
            check(key in d, f"清理预览含 {key}")
    else:
        check(False, "清理预览", f"{st}")

    # ================= P3：日志 =================
    st, body = c.call("GET", "/api/admin/system/logs")
    if st == 200:
        d = json.loads(body)
        check("files" in d, "日志文件列表可用", f"{len(d['files'])} 个")

    st, _ = c.call("GET", "/api/admin/system/logs?name=../../config/settings.json")
    check(st == 400, "日志路径穿越被拒 → 400", f"实际 {st}")
    st, _ = c.call("GET", "/api/admin/system/logs?name=nonexistent.log")
    check(st == 404, "不存在的日志 → 404", f"实际 {st}")

    # ================= 批3：批量与导出 =================
    import time as _t

    probe = f"p3probe{int(_t.time()) % 100000}"
    st, body = c.call("POST", "/api/admin/users", {
        "login_name": probe, "display_name": "P3验证", "password": FIXTURE_PW,
        "roles": ["operator"]})
    ok_create = st == 200
    check(ok_create, f"创建测试账号 {probe}")
    if ok_create:
        pid = json.loads(body)["user"]["id"]

        st, body = c.call("POST", "/api/admin/users/batch", {
            "action": "disable", "user_ids": [pid, admin_id]})
        if st == 200:
            d = json.loads(body)
            check(True, "批量停用 → 200", d.get("message"))
            check(d.get("skipped_self", 0) >= 1, "批量操作跳过自己")
        else:
            check(False, "批量操作", f"{st} {body[:120]}")

        st, body = c.call("POST", "/api/admin/users/batch", {"action": "bogus",
                                                            "user_ids": [pid]})
        check(st == 400, "未知批量动作 → 400", f"实际 {st}")

        # 导出 CSV
        st, body = c.call("GET", "/api/admin/users/export")
        check(st == 200 and "登录名" in body, "导出用户 CSV → 200",
              f"{len(body)} 字节")
        check("argon2" not in body and "$argon2" not in body, "CSV 不含密码哈希")
        check(probe in body, "CSV 含刚创建的账号")

    # ================= 批3：角色复制 =================
    new_role = f"copy{int(_t.time()) % 100000}"
    st, body = c.call("POST", "/api/admin/roles/operator/copy", {"code": new_role})
    if st == 200:
        d = json.loads(body)
        check(True, f"复制角色 → {d['code']}", f"{d['permissions']} 项权限")
        st2, body2 = c.call("GET", "/api/admin/roles/matrix")
        if st2 == 200:
            m = json.loads(body2)["matrix"]
            check(m.get(new_role, {}).get("batch.read") is True, "副本继承了源角色权限")
        c.call("DELETE", f"/api/admin/roles/{new_role}")
    else:
        check(False, "角色复制", f"{st} {body[:120]}")

    st, _ = c.call("POST", "/api/admin/roles/operator/copy", {"code": "operator"})
    check(st == 409, "复制到已存在编码 → 409", f"实际 {st}")

    # ================= 批3：请求追踪 =================
    st, body = c.call("GET", "/api/admin/audit?limit=30")
    rid = ""
    if st == 200:
        for r in json.loads(body)["records"]:
            if r.get("request_id"):
                rid = r["request_id"]
                break
    if rid:
        st, body = c.call("GET", f"/api/admin/audit/trace?request_id={rid}")
        check(st == 200 and json.loads(body)["count"] >= 1, "请求追踪可用",
              f"request_id={rid} → {json.loads(body)['count']} 条")
    else:
        print("  ⏭  没有可追踪的 request_id")

    # ================= 页面元素 =================
    st, html = c.call("GET", "/admin")
    for kw, label in (
        ("u-drawer", "用户详情抽屉"),
        ("u-q", "用户搜索框"),
        ("u-pager", "用户分页器"),
        ("u-all", "全选复选框"),
        ("u-batch-go", "批量执行按钮"),
        ("u-export", "导出 CSV 按钮"),
        ("s-auto", "会话自动刷新"),
        ("s-cleanup", "会话清理按钮"),
        ("s-revoke-all", "全部下线按钮"),
        ("disk-body", "磁盘明细表"),
        ("cl-preview", "清理预览按钮"),
        ("cl-run", "执行清理按钮"),
        ("lg-file", "日志文件选择器"),
        ("lg-body", "日志内容区"),
        ("previewRole", "角色预览逻辑"),
        ("audit/trace", "请求追踪调用"),
        ("users/batch", "批量接口调用"),
        ("roles/", "角色复制调用"),
    ):
        check(kw in html, f"页面含 {label}")

    print("-" * 78)
    ok = sum(1 for r, _ in RESULTS if r)
    print(f"通过 {ok} / {len(RESULTS)}" + ("  ✅ 全部通过" if ok == len(RESULTS) else "  ⚠️ 有失败"))
    print("=" * 78)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
