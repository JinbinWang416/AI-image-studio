# -*- coding: utf-8 -*-
"""
P1 功能端到端验证（审计看板 + 权限矩阵）。

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_p1.py
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
    print("P1 功能验证（审计看板 + 权限矩阵）")
    print("=" * 78)

    anon = Client()
    st, _ = anon.call("GET", "/api/admin/audit/stats")
    check(st == 401, "未登录访问审计统计 → 401", f"实际 {st}")
    st, _ = anon.call("GET", "/api/admin/roles/matrix")
    check(st == 401, "未登录访问权限矩阵 → 401", f"实际 {st}")

    c = Client()
    st, body = c.call("POST", "/api/auth/login",
                      {"login_name": "admin", "password": ADMIN_PW})
    if st != 200:
        print(f"  ❌ 登录失败：{st} {body[:150]}")
        return 1
    check(True, "管理员登录")

    # ---------------- 审计统计 ----------------
    st, body = c.call("GET", "/api/admin/audit/stats?days=7&bucket=day")
    if st != 200:
        check(False, "审计统计 → 200", f"{st} {body[:150]}")
    else:
        d = json.loads(body)
        check(True, "审计统计 → 200", f"total={d.get('total')}")
        for key in ("buckets", "top_actors", "top_actions", "top_modules",
                    "top_denied", "results", "window", "failed_rate"):
            check(key in d, f"返回含 {key}")
        check(isinstance(d.get("buckets"), list) and len(d["buckets"]) == 7,
              "按天返回 7 个桶", f"实际 {len(d.get('buckets') or [])}")
        check(all(("t" in b and "n" in b and "failed" in b) for b in d["buckets"]),
              "每个桶含 t/n/failed")
        check(0 <= d.get("failed_rate", -1) <= 100, "失败率在合理区间",
              f"{d.get('failed_rate')}%")

    st, body = c.call("GET", "/api/admin/audit/stats?days=1&bucket=hour")
    if st == 200:
        d = json.loads(body)
        check(len(d["buckets"]) == 24, "按小时返回 24 个桶", f"实际 {len(d['buckets'])}")
        check(d["window"]["bucket"] == "hour", "窗口粒度正确")

    st, _ = c.call("GET", "/api/admin/audit/stats?days=999")
    check(st == 422, "days 超范围 → 422（参数校验生效）", f"实际 {st}")
    st, _ = c.call("GET", "/api/admin/audit/stats?bucket=week")
    check(st == 422, "非法 bucket → 422", f"实际 {st}")

    # ---------------- 权限矩阵 ----------------
    st, body = c.call("GET", "/api/admin/roles/matrix")
    if st != 200:
        check(False, "权限矩阵 → 200", f"{st} {body[:150]}")
    else:
        d = json.loads(body)
        check(True, "权限矩阵 → 200",
              f"{d['stats']['total_roles']} 角色 × {d['stats']['total_permissions']} 权限")
        check(len(d["groups"]) >= 4, "按分组返回权限", f"{len(d['groups'])} 组")
        check(all(("name" in g and "permissions" in g) for g in d["groups"]),
              "每组含 name/permissions")

        # admin 应拥有全部权限
        admin_row = d["matrix"].get("admin", {})
        check(all(admin_row.values()) and len(admin_row) == d["stats"]["total_permissions"],
              "管理员拥有全部权限")

        # operator 不应有高危权限
        op = d["matrix"].get("operator", {})
        check(op.get("batch.create") is False, "客服无 batch.create")
        check(op.get("system.user.manage") is False, "客服无用户管理")

        # auditor 应能读审计但不能改业务
        au = d["matrix"].get("auditor", {})
        check(au.get("system.audit.read") is True, "审计员可读审计")
        check(au.get("batch.create") is False, "审计员不能创建批次")

        check("orphan_permissions" in d["stats"], "返回孤立权限诊断")
        check("empty_roles" in d["stats"], "返回空角色诊断")

    # ---------------- 页面元素 ----------------
    st, html = c.call("GET", "/admin")
    check(st == 200, "/admin 可访问")
    for kw, label in (
        ("a-chart", "看板柱状图容器"),
        ("a-range", "时间范围选择器"),
        ("a-bucket", "粒度选择器"),
        ("kpi-total", "KPI 卡片"),
        ("a-top-actors", "Top 操作者"),
        ("a-top-denied", "被拒绝统计"),
        ("m-wrap", "矩阵容器"),
        ("loadAuditBoard", "看板加载逻辑"),
        ("loadMatrix", "矩阵加载逻辑"),
        ("audit/stats", "看板接口调用"),
        ("roles/matrix", "矩阵接口调用"),
    ):
        check(kw in html, f"页面含 {label}")

    print("-" * 78)
    ok = sum(1 for r, _ in RESULTS if r)
    print(f"通过 {ok} / {len(RESULTS)}" + ("  ✅ 全部通过" if ok == len(RESULTS) else "  ⚠️ 有失败"))
    print("=" * 78)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
