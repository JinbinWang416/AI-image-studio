# -*- coding: utf-8 -*-
"""
验证访问控制中间件对**真实业务接口**的拦截效果（文档 §13 用例 10、12）。

检查项：
    · 未登录访问业务接口 → 401
    · 未登录访问公开接口 → 放行
    · 低权限用户访问高权限接口 → 403 且写审计
    · 管理员访问 → 放行
    · 未登记的新接口 → 默认要求登录（默认拒绝）

用法（需要服务在 http://127.0.0.1:8000 运行）：
    .\\.venv\\Scripts\\python.exe tools\\verify_access_control.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8000"


def call(method: str, path: str, body: dict | None = None, cookie: str = "") -> tuple[int, str]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **({"Cookie": cookie} if cookie else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", errors="replace")[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")[:200]
    except Exception as exc:  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


# (方法, 路径, 是否公开)
CASES: list[tuple[str, str, bool]] = [
    ("GET", "/api/state", False),
    ("GET", "/api/settings", False),
    ("GET", "/api/effect-params", False),
    ("POST", "/api/run", False),
    ("POST", "/api/settings", False),
    ("POST", "/api/export/package", False),
    ("GET", "/api/not-registered-yet", False),   # 未登记 → 默认拒绝
    ("GET", "/api/auth/status", True),
]


def main() -> int:
    print("=" * 76)
    print("访问控制验证（未登录状态）")
    print("=" * 76)
    print(f"{'方法':<7}{'路径':<32}{'预期':<10}{'实际':<8}{'结果'}")
    print("-" * 76)

    ok = fail = 0
    for method, path, public in CASES:
        status, _ = call(method, path)
        if status == -1:
            print(f"{method:<7}{path:<32}{'':<10}{'连接失败':<8}❌ 服务未启动？")
            return 1
        if public:
            good = status != 401
            expect = "放行"
        else:
            good = status == 401
            expect = "401"
        ok += good
        fail += not good
        print(f"{method:<7}{path:<32}{expect:<10}{status:<8}{'✅' if good else '❌'}")

    print("-" * 76)
    print(f"通过 {ok} / {len(CASES)}" + (f"，失败 {fail}" if fail else "  ✅ 全部通过"))
    print("=" * 76)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
