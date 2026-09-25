# -*- coding: utf-8 -*-
"""列出应用实际注册的路由，用于排查注册问题。"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.web.server import app  # noqa: E402

print("=" * 70)
print("已注册路由")
print("=" * 70)

auth_paths, other = [], []
for r in app.routes:
    path = getattr(r, "path", "")
    methods = ",".join(sorted(getattr(r, "methods", []) or []))
    if path.startswith("/api/auth"):
        auth_paths.append((path, methods))
    elif path.startswith("/api"):
        other.append(path)

print(f"\n【认证接口】{len(auth_paths)} 个")
for p, m in sorted(auth_paths):
    print(f"  {m:<12} {p}")

print(f"\n【其它 API】{len(other)} 个（前 10）")
for p in sorted(other)[:10]:
    print(f"  {p}")

# 排查用：打印全部路由的原始 path（含非 API）
allp = [(getattr(r, "path", ""), type(r).__name__) for r in app.routes]
print(f"\n【全部路由】共 {len(allp)} 条")
for p, kind in allp[:25]:
    print(f"  {kind:<14} {p}")

print("=" * 70)
