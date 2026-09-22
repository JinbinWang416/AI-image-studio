# -*- coding: utf-8 -*-
"""按需求口径测性能：2000px 图整套处理是否 ≤ 10 秒。

同时对比不同目标尺寸的耗时，找出瓶颈。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.print_export import PrintExporter  # noqa: E402

src = None
for p in sorted((ROOT / "output").glob("batch_*/*/*生成图/*.png")):
    src = p
    break
if src is None:
    print("  ❌ 没有可用的测试 PNG")
    raise SystemExit(1)

store_dir = src.parent.parent

print("=" * 78)
print("性能测试（需求口径：2000px 图 ≤ 10 秒）")
print("=" * 78)

# 不同目标尺寸的耗时
CASES = [
    ("1000px", 1000),
    ("2000px", 2000),      # ← 需求口径
    ("4000px", 4000),
    ("7087px（生产默认）", 7087),
]

for label, px in CASES:
    cfg = load_config()
    cfg.print_width_cm = px / (300 / 2.54)   # 反推宽度，使 @300DPI 得到该像素
    cfg.print_dpi = 300
    cfg.print_max_pixels = px
    cfg.print_keep_work = False              # 不重复建硬链接

    exporter = PrintExporter(cfg)
    t0 = time.monotonic()
    res = exporter.export_store(store_dir, src, store_name=f"PERF{px}")
    dt = time.monotonic() - t0

    out_px = res.manifest.output_pixels if res.manifest else (0, 0)
    flag = "✅" if dt <= 10 else "⚠️"
    print(f"  {label:<20} 实际 {out_px[0]}×{out_px[1]:<6} "
          f"耗时 {dt:>6.2f}s  {flag if px == 2000 else ''}")

print()
print("  需求：2000px 图 ≤ 10 秒")
print("  说明：7087px 是 60cm@300DPI 的生产尺寸，像素量是 2000px 的 12.5 倍。")
print("=" * 78)
