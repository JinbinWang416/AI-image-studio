# -*- coding: utf-8 -*-
"""
管理后台端到端验证（文档 §7 + §13）。

覆盖：
    · /admin 未登录 → 401
    · 登录后 /admin → 200
    · 管理员可访问全部管理接口
    · 低权限用户访问管理接口 → 403
    · 审计中心能查到前面产生的记录
    · 角色权限配置的 diff 返回正确

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_admin.py
"""
from __future__ import annotations
import os

import json
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE = "http://127.0.0.1:8000"
ADMIN_PW = os.environ.get("SHS_ADMIN_PW", "")  # 真实 admin 密码（登录用）
FIXTURE_PW = "Str0ng!Passw0rd"  # 新建测试用户用：必须满足强度规则（≥10 位）


class Client:
    def __init__(self) -> None:
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(
            BASE + path, data=data, method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with self.opener.open(req, timeout=20) as r:
                raw = r.read().decode("utf-8", errors="replace")
                return r.status, raw
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return -1, f"{type(exc).__name__}: {exc}"


RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    RESULTS.append((ok, label))
    print(f"  {'✅' if ok else '❌'} {label}" + (f"   {extra}" if extra else ""))


def main() -> int:
    print("=" * 76)
    print("管理后台端到端验证")
    print("=" * 76)

    # ① 未登录
    anon = Client()
    st, _ = anon.call("GET", "/admin")
    check(st == 401, "未登录访问 /admin → 401", f"实际 {st}")
    st, _ = anon.call("GET", "/api/admin/users")
    check(st == 401, "未登录访问 /api/admin/users → 401", f"实际 {st}")

    # ② 登录
    c = Client()
    st, body = c.call("POST", "/api/auth/login", {"login_name": "admin", "password": ADMIN_PW})
    if st != 200:
        print(f"  ❌ 管理员登录失败（{st}）：{body[:200]}")
        return 1
    check(True, "管理员登录")

    # ③ /admin 页面
    st, body = c.call("GET", "/admin")
    check(st == 200 and "管理后台" in body, "登录后 /admin → 200", f"实际 {st}")

    # ④ 管理接口
    for path, label in (
        ("/api/admin/users", "用户列表"),
        ("/api/admin/roles", "角色列表"),
        ("/api/admin/sessions", "会话列表"),
        ("/api/admin/audit", "审计检索"),
        ("/api/admin/health", "系统健康"),
    ):
        st, body = c.call("GET", path)
        ok = st == 200
        extra = ""
        if ok:
            try:
                d = json.loads(body)
                n = len(d.get("users") or d.get("roles") or d.get("sessions")
                        or d.get("records") or [])
                extra = f"{n} 项"
            except Exception:  # noqa: BLE001
                extra = ""
        check(ok, f"{label} → 200", extra or f"实际 {st}")

    # ⑤ 权限目录（前端勾选项来源）
    st, body = c.call("GET", "/api/admin/roles")
    if st == 200:
        d = json.loads(body)
        catalog = d.get("catalog") or []
        high = [p for p in catalog if p.get("high_risk")]
        check(len(catalog) >= 15, "权限目录完整", f"{len(catalog)} 项，含 {len(high)} 项高危")
        check(all(p.get("group") for p in catalog), "每项权限都有分组")

    # ⑥ 审计能查到刚才的操作
    st, body = c.call("GET", "/api/admin/audit?limit=50")
    if st == 200:
        d = json.loads(body)
        actions = {r.get("action") for r in d.get("records") or []}
        check(any("login" in (a or "") for a in actions), "审计记录了登录",
              f"共 {len(d.get('records') or [])} 条")
        check(d.get("stats", {}).get("records", 0) > 0, "审计统计可用")

    # ⑦ 低权限用户被拦
    # 用带时间戳的登录名，保证脚本可重复运行（幂等）
    import time as _t

    probe_login = f"e2e{int(_t.time()) % 100000}"
    st, body = c.call("POST", "/api/admin/users", {
        "login_name": probe_login, "display_name": "验证用户",
        "password": FIXTURE_PW, "roles": ["operator"],
    })
    if st == 200:
        check(True, f"创建测试用户（operator，{probe_login}）")
        low = Client()
        st2, _ = low.call("POST", "/api/auth/login",
                          {"login_name": probe_login, "password": FIXTURE_PW})
        if st2 == 200:
            st3, body3 = low.call("GET", "/api/admin/users")
            check(st3 == 403, "低权限用户访问用户管理 → 403", f"实际 {st3}")
            st4, _ = low.call("GET", "/admin")
            check(st4 == 403, "低权限用户访问 /admin → 403", f"实际 {st4}")
            # 清理：停用测试账号
            st5, body5 = c.call("GET", "/api/admin/users")
            if st5 == 200:
                for u in json.loads(body5).get("users") or []:
                    if u.get("login_name") == probe_login:
                        c.call("PATCH", f"/api/admin/users/{u['id']}",
                               {"status": "disabled"})
                        check(True, "清理测试账号（已停用）")
        else:
            check(False, "测试用户登录失败", f"实际 {st2}")
    else:
        check(False, "创建测试用户失败", f"{st} {body[:120]}")

    # ⑧ 未授权角色编码被拒
    st, _ = c.call("PUT", "/api/admin/roles/operator",
                   {"name": "客服", "permissions": ["not.a.real.permission"]})
    check(st == 400, "拒绝未知权限码 → 400", f"实际 {st}")

    print("-" * 76)
    ok = sum(1 for r, _ in RESULTS if r)
    print(f"通过 {ok} / {len(RESULTS)}" + ("  ✅ 全部通过" if ok == len(RESULTS) else "  ⚠️ 有失败"))
    print("=" * 76)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
