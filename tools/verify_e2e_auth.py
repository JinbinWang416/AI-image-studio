# -*- coding: utf-8 -*-
"""
端到端验证：完整的安全流程（文档 §13）。

流程：
    1. 检查状态（是否需要初始化）
    2. 创建管理员
    3. 用新账号登录
    4. 带 Cookie 访问业务接口 → 应放行
    5. 登出 → 再访问 → 应 401

⚠️ 会**真实创建管理员账号**到 data/security/。
   仅用于本地首次部署验证；如已有账号请勿重复运行。

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_e2e_auth.py [--login 名称 --password 密码]
"""
from __future__ import annotations
import os

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8000"


class Client:
    def __init__(self) -> None:
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, str]:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(
            BASE + path, data=data, method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with self.opener.open(req, timeout=20) as r:
                return r.status, r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            return -1, f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", default="admin")
    ap.add_argument("--password", default=os.environ.get("SHS_ADMIN_PW", ""))
    ap.add_argument("--name", default="管理员")
    args = ap.parse_args()

    c = Client()
    print("=" * 72)
    print("端到端安全流程验证")
    print("=" * 72)

    # ① 状态
    status, body = c.call("GET", "/api/auth/status")
    if status != 200:
        print(f"  ❌ 无法访问服务（{status}）：{body[:120]}")
        return 1
    st = json.loads(body)
    print(f"  ① 服务状态      : HTTP {status}  需要初始化={st['needs_setup']}")

    # ② 创建管理员（如果需要）
    if st["needs_setup"]:
        status, body = c.call("POST", "/api/auth/setup", {
            "login_name": args.login,
            "display_name": args.name,
            "password": args.password,
        })
        if status != 200:
            print(f"  ❌ 创建管理员失败（{status}）：{body[:200]}")
            return 1
        print(f"  ② 创建管理员    : ✅ {args.login}")
    else:
        print("  ② 创建管理员    : ⏭ 已存在，跳过")

    # ③ 登出后用密码重新登录（验证登录路径）
    c.call("POST", "/api/auth/logout")
    status, body = c.call("GET", "/api/state")
    print(f"  ③ 登出后访问业务: HTTP {status} {'✅ 已拦截' if status == 401 else '❌ 未拦截'}")

    status, body = c.call("POST", "/api/auth/login", {
        "login_name": args.login, "password": args.password,
    })
    if status != 200:
        print(f"  ❌ 登录失败（{status}）：{body[:200]}")
        return 1
    data = json.loads(body)
    print(f"  ④ 登录          : ✅ {data['user']['display_name']}"
          f"（{len(data['permissions'])} 项权限）")

    # ⑤ 带会话访问业务接口
    status, body = c.call("GET", "/api/state")
    print(f"  ⑤ 登录后访问业务: HTTP {status} {'✅ 已放行' if status == 200 else '❌ 仍被拦'}")

    # ⑥ 会话列表
    status, body = c.call("GET", "/api/auth/sessions")
    if status == 200:
        n = len(json.loads(body)["sessions"])
        print(f"  ⑥ 我的登录设备  : ✅ {n} 个会话")

    # ⑦ 安全响应头
    req = urllib.request.Request(BASE + "/api/auth/status")
    with urllib.request.urlopen(req, timeout=10) as r:
        hdrs = {k.lower(): v for k, v in r.headers.items()}
    checks = {
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
    }
    bad = [k for k, v in checks.items() if hdrs.get(k) != v]
    print(f"  ⑦ 安全响应头    : {'✅ 齐全' if not bad else '❌ 缺少 ' + ','.join(bad)}")

    print("=" * 72)
    ok = status == 200 and not bad
    print("结论：✅ 安全流程可用" if ok else "结论：⚠️ 存在问题")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
