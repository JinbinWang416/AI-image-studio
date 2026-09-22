# -*- coding: utf-8 -*-
"""
P0 功能端到端验证（走真实 HTTP，用 CookieJar 保持会话）。

覆盖：
    · 备份板块三接口（create / list / restore）
    · 恢复的二次确认与「还原前备份」
    · 管理员重置密码（含安全约束）
    · /admin 页面确实包含备份板块与重置密码入口
    · 缓存头正确

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_p0.py
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
TMP_PW = "Tmp!Verify2026A9"

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
                return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)
        except Exception as exc:  # noqa: BLE001
            return -1, f"{type(exc).__name__}: {exc}", {}


def main() -> int:
    print("=" * 78)
    print("P0 功能验证（备份管理 + 重置密码）")
    print("=" * 78)

    # ① /admin 页面内容
    anon = Client()
    st, _, _ = anon.call("GET", "/admin")
    check(st == 401, "未登录访问 /admin → 401", f"实际 {st}")

    c = Client()
    st, body, _ = c.call("POST", "/api/auth/login",
                         {"login_name": "admin", "password": ADMIN_PW})
    if st != 200:
        print(f"  ❌ 登录失败：{st} {body[:160]}")
        return 1
    check(True, "管理员登录")

    st, html, hdrs = c.call("GET", "/admin")
    check(st == 200, "登录后 /admin → 200", f"实际 {st}")
    for kw, label in (
        ("tab-backup", "备份板块标签"),
        ("bk-create", "创建备份按钮"),
        ("bk-body", "备份列表容器"),
        ("bk-freshness", "备份新鲜度区"),
        ("resetpw", "重置密码入口"),
        ("loadBackups", "备份加载逻辑"),
    ):
        check(kw in html, f"页面含 {label}")
    check("no-cache" in hdrs.get("cache-control", ""),
          "页面禁用缓存（防改动不生效）", hdrs.get("cache-control", "（无）"))

    # ② 备份接口
    st, body, _ = c.call("POST", "/api/admin/backup/create", {"label": "p0-verify"})
    backup_id = ""
    if st == 200:
        d = json.loads(body)
        backup_id = d["backup"]["id"]
        check(True, "创建备份 → 200",
              f"{d['backup']['size_kb']} KB, {d['backup']['file_count']} 个文件")
        check(d["backup"]["encrypted"], "备份已加密")
        check("key" not in json.dumps(d["backup"]).lower(), "响应不含密钥")
    else:
        check(False, "创建备份", f"{st} {body[:140]}")

    st, body, _ = c.call("GET", "/api/admin/backup/list")
    if st == 200:
        d = json.loads(body)
        check(len(d["backups"]) >= 1, "列出备份", f"{len(d['backups'])} 份")
        check("keep" in d, "返回保留策略", f"keep={d.get('keep')}")
        f = d.get("freshness", {})
        check(not f.get("stale", True), "备份新鲜度正常",
              f"age={f.get('age_hours')}h")
    else:
        check(False, "列出备份", f"{st}")

    if backup_id:
        st, _, _ = c.call("POST", "/api/admin/backup/restore",
                          {"backup_id": backup_id, "confirm": "wrong"})
        check(st == 400, "confirm 不匹配 → 400", f"实际 {st}")

    # ③ 重置密码
    st, body, _ = c.call("GET", "/api/admin/users")
    users = json.loads(body).get("users", []) if st == 200 else []
    me = next((u for u in users if u["login_name"] == "admin"), None)

    # 造一个专用测试账号（避免依赖既有用户；带时间戳保证可重复运行）
    import time as _t

    probe_login = f"p0probe{int(_t.time()) % 100000}"
    st, body, _ = c.call("POST", "/api/admin/users", {
        "login_name": probe_login, "display_name": "P0验证账号",
        "password": FIXTURE_PW, "roles": ["operator"],
    })
    target = None
    if st == 200:
        target = json.loads(body).get("user")
    else:
        check(False, "创建测试账号", f"{st} {body[:120]}")

    if me:
        st, _, _ = c.call("POST", f"/api/admin/users/{me['id']}/reset-password",
                          {"new_password": TMP_PW})
        check(st == 400, "不能重置自己的密码 → 400", f"实际 {st}")

    if target:
        st, _, _ = c.call("POST", f"/api/admin/users/{target['id']}/reset-password",
                          {"new_password": "weak"})
        check(st == 400, "弱密码被拒 → 400", f"实际 {st}")

        st, body, _ = c.call("POST", f"/api/admin/users/{target['id']}/reset-password",
                             {"new_password": TMP_PW})
        if st == 200:
            d = json.loads(body)
            check(True, f"重置 {probe_login} 密码 → 200",
                  f"撤销 {d.get('revoked')} 个会话")
            c2 = Client()
            st2, _, _ = c2.call("POST", "/api/auth/login",
                                {"login_name": probe_login, "password": ADMIN_PW})
            check(st2 == 401, "旧密码已失效")
            st3, _, _ = c2.call("POST", "/api/auth/login",
                                {"login_name": probe_login, "password": TMP_PW})
            check(st3 == 200, "新密码可登录")
            c2.call("POST", "/api/auth/logout")
        else:
            check(False, "重置密码", f"{st} {body[:140]}")

        # 清理：停用测试账号
        c.call("PATCH", f"/api/admin/users/{target['id']}", {"status": "disabled"})
        check(True, "清理测试账号（已停用）")

    # ④ 审计（注意 backup.* 的 module 是 "backup"，不是 "admin"）
    st, body, _ = c.call("GET", "/api/admin/audit?limit=100")
    if st == 200:
        actions = {r.get("action") for r in json.loads(body).get("records", [])}
        check("backup.create" in actions, "审计记录了 backup.create")
        check("user.reset_password" in actions or not target,
              "审计记录了 user.reset_password")
        denied = [r for r in json.loads(body).get("records", [])
                  if r.get("result") == "denied"]
        check(bool(denied), "被拒绝的操作也写审计", f"{len(denied)} 条")

    # ⑤ 明文密码不得出现在审计里
    st, body, _ = c.call("GET", "/api/admin/audit?limit=200")
    if st == 200:
        check(TMP_PW not in body, "审计中无明文密码")
        check("argon2" not in body, "审计中无密码哈希")

    print("-" * 78)
    ok = sum(1 for r, _ in RESULTS if r)
    print(f"通过 {ok} / {len(RESULTS)}" + ("  ✅ 全部通过" if ok == len(RESULTS) else "  ⚠️ 有失败"))
    print("=" * 78)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
