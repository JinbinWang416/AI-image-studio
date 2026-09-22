# -*- coding: utf-8 -*-
"""
块 1 端到端验证：备份 / 列表 / 恢复 / 二次确认（走真实 HTTP）。

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_backup_api.py
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
            with self.opener.open(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return -1, f"{type(exc).__name__}: {exc}"


def check(ok: bool, label: str, extra: str = "") -> None:
    RESULTS.append((ok, label))
    print(f"  {'✅' if ok else '❌'} {label}" + (f"   {extra}" if extra else ""))


def main() -> int:
    print("=" * 76)
    print("备份与恢复 API 端到端验证")
    print("=" * 76)

    anon = Client()
    st, _ = anon.call("GET", "/api/admin/backup/list")
    check(st == 401, "未登录访问备份列表 → 401", f"实际 {st}")

    c = Client()
    st, body = c.call("POST", "/api/auth/login",
                      {"login_name": "admin", "password": ADMIN_PW})
    if st != 200:
        print(f"  ❌ 登录失败（{st}）：{body[:160]}")
        return 1
    check(True, "管理员登录")

    # ① 创建
    st, body = c.call("POST", "/api/admin/backup/create", {"label": "e2e"})
    ok = st == 200
    backup_id = ""
    if ok:
        d = json.loads(body)
        backup_id = d["backup"]["id"]
        check(True, "创建备份 → 200", f"{d['backup']['size_kb']} KB, "
                                     f"{d['backup']['file_count']} 个文件")
        check(d["backup"]["encrypted"], "备份已加密")
        check("key" not in json.dumps(d["backup"]).lower(), "响应不含密钥")
    else:
        check(False, "创建备份", f"{st} {body[:160]}")

    # ② 列表 + 新鲜度
    st, body = c.call("GET", "/api/admin/backup/list")
    if st == 200:
        d = json.loads(body)
        check(len(d["backups"]) >= 1, "列出备份", f"{len(d['backups'])} 份")
        check(d["freshness"]["count"] >= 1 and not d["freshness"]["stale"],
              "备份新鲜度正常", f"age={d['freshness']['age_hours']}h")
        check(all("key" not in json.dumps(b).lower() for b in d["backups"]),
              "列表不含密钥")
    else:
        check(False, "列出备份", f"{st}")

    # ③ 恢复：二次确认
    if backup_id:
        st, body = c.call("POST", "/api/admin/backup/restore",
                          {"backup_id": backup_id, "confirm": "错误确认"})
        check(st == 400, "confirm 不匹配 → 400", f"实际 {st}")

        st, body = c.call("POST", "/api/admin/backup/restore",
                          {"backup_id": backup_id, "confirm": backup_id})
        if st == 200:
            d = json.loads(body)
            check(True, "正确确认后恢复成功", f"恢复 {len(d['restored_files'])} 个文件")
            check(bool(d["pre_restore_backup"]), "已生成『还原前备份』",
                  d["pre_restore_backup"])
        else:
            check(False, "恢复失败", f"{st} {body[:200]}")

    # ④ 健康接口含备份与磁盘
    st, body = c.call("GET", "/api/admin/health")
    if st == 200:
        d = json.loads(body)
        check("backup" in d and "disk" in d, "健康接口含备份新鲜度与磁盘水位",
              f"free={d.get('disk', {}).get('free_gb')}GB")
    else:
        check(False, "健康接口", f"{st}")

    # ⑤ 审计留痕
    st, body = c.call("GET", "/api/admin/audit?module=backup&limit=20")
    if st == 200:
        d = json.loads(body)
        actions = {r.get("action") for r in d["records"]}
        check("backup.create" in actions, "审计记录了 backup.create")
        check("backup.restore" in actions, "审计记录了 backup.restore")
        denied = [r for r in d["records"] if r.get("result") == "denied"]
        check(bool(denied), "二次确认失败也被审计", f"{len(denied)} 条")
    else:
        check(False, "审计检索", f"{st}")

    print("-" * 76)
    ok = sum(1 for r, _ in RESULTS if r)
    print(f"通过 {ok} / {len(RESULTS)}" + ("  ✅ 全部通过" if ok == len(RESULTS) else "  ⚠️ 有失败"))
    print("=" * 76)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
