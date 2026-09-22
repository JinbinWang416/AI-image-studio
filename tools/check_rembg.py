# -*- coding: utf-8 -*-
"""验证 rembg 可用性（首次运行会下载 u2net 模型，约 176MB）。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.print_export.layers import CutoutSession  # noqa: E402

print("=" * 74)
print("rembg 可用性验证")
print("=" * 74)

print("  正在初始化会话（首次会下载 u2net 模型，请耐心等待）…")
t0 = time.monotonic()
s = CutoutSession()
ok = s.available          # 触发 _init()
dt = time.monotonic() - t0

print(f"  可用: {'✅ 是' if ok else '❌ 否'}   耗时 {dt:.1f}s")
if not ok:
    print(f"  原因: {s.last_error}")

# 模型缓存位置
home = Path.home() / ".u2net"
if home.is_dir():
    print(f"\n  模型缓存: {home}")
    for f in sorted(home.iterdir()):
        print(f"    {f.name}  {f.stat().st_size // 1024 // 1024} MB")
else:
    print(f"\n  模型缓存目录不存在: {home}")

print("=" * 74)
