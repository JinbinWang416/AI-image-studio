# -*- coding: utf-8 -*-
"""真实批次试跑：用默认参数（60cm @ 300DPI）验证性能与产出。

⚠️ 只读源 PNG；产出写入 印刷TIF/、预览/、_work/，不触碰生成图与效果图。
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

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
print("真实批次试跑（默认参数：60cm @ 300DPI）")
print("=" * 78)
print(f"  源图 : {src.name}  ({src.stat().st_size // 1024} KB)")
with Image.open(src) as im:
    print(f"  源尺寸: {im.size[0]} × {im.size[1]} px")

before = hashlib.sha256(src.read_bytes()).hexdigest()

cfg = load_config()
print(f"  目标 : {cfg.print_width_cm}cm @ {cfg.print_dpi}DPI "
      f"（出血 {cfg.print_bleed_mm}mm，max_pixels={cfg.print_max_pixels}）")
expect_w = round(cfg.print_width_cm / 2.54 * cfg.print_dpi)
print(f"  预期 : 宽度 {expect_w} px（受 max_pixels 限制前）")

t0 = time.monotonic()
exporter = PrintExporter(cfg)
res = exporter.export_store(store_dir, src, store_name="REAL")
wall = time.monotonic() - t0

print()
print(f"  ok={res.ok}  error={res.error_code or '（无）'}  耗时={wall:.2f}s")
print(f"  要求：单张 ≤ 10 秒 → {'✅ 达标' if wall <= 10 else '⚠️ 超时'}")

if res.ok and res.print_dir:
    d = res.print_dir
    print(f"\n  印刷TIF/ 目录内容:")
    total = 0
    for f in sorted(d.iterdir()):
        kb = f.stat().st_size // 1024
        total += kb
        print(f"    {f.name:<34} {kb:>7} KB")
    print(f"    {'合计':<34} {total:>7} KB")

    print("\n  图层属性:")
    for name in sorted(p.name for p in d.glob("*.tif")):
        with Image.open(d / name) as im:
            comp = im.info.get("compression", "?")
            print(f"    {name:<34} mode={im.mode:<6} {im.size[0]}×{im.size[1]}  "
                  f"compression={comp}")

    mf_path = d / "print_manifest.json"
    if mf_path.is_file():
        print("\n  print_manifest.json 全文:")
        print("-" * 78)
        print(mf_path.read_text(encoding="utf-8"))
        print("-" * 78)

    pv = store_dir / "预览"
    if pv.is_dir():
        for f in pv.iterdir():
            with Image.open(f) as im:
                print(f"  预览: {f.name}  {im.size[0]}×{im.size[1]}  "
                      f"{f.stat().st_size // 1024} KB")

after = hashlib.sha256(src.read_bytes()).hexdigest()
print()
print(f"  源图未被修改: {'✅' if before == after else '❌ 被改动了！'}")
print("=" * 78)
