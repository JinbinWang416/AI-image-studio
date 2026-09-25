# -*- coding: utf-8 -*-
"""印刷导出冒烟测试：用真实生成图跑一遍完整流水线。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app.config import load_config  # noqa: E402
from app.print_export import PrintExporter, read_print_manifest  # noqa: E402

# 找一张真实的生成图
# 实际结构：output/<批次>/<门店>/<门店>生成图/<文件>.png
src = None
for p in sorted((ROOT / "output").glob("batch_*/*/*生成图/*.png")):
    src = p
    break

if src is None:
    print("  ❌ 没找到测试用 PNG")
    raise SystemExit(1)

print("=" * 74)
print("印刷导出冒烟测试")
print("=" * 74)
print(f"  源图: {src.name}  ({src.stat().st_size // 1024} KB)")

store_dir = src.parent.parent
print(f"  门店目录: {store_dir.name}")

# 记录源图 hash，稍后验证"未被修改"
import hashlib  # noqa: E402

before = hashlib.sha256(src.read_bytes()).hexdigest()
before_mtime = src.stat().st_mtime_ns

cfg = load_config()
# 冒烟用小尺寸，加快速度（正式默认 60cm@300DPI）
cfg.print_width_cm = 20.0
cfg.print_dpi = 150
cfg.print_max_pixels = 2000

t0 = time.monotonic()
exporter = PrintExporter(cfg)
res = exporter.export_store(store_dir, src, store_name="SMOKE")
wall = time.monotonic() - t0

print()
print(f"  ok={res.ok}  error={res.error_code}  内部耗时={res.elapsed:.2f}s  总耗时={wall:.2f}s")

if res.ok and res.print_dir:
    d = res.print_dir
    print(f"\n  输出目录: {d}")
    for f in sorted(d.iterdir()):
        print(f"    {f.name:<28} {f.stat().st_size // 1024:>6} KB")

    print("\n  图层校验:")
    for name, expect in (("SMOKE_CMYK.tif", "CMYK"), ("SMOKE_白墨.tif", "L"),
                         ("SMOKE_刀模.tif", "RGB"), ("SMOKE_合并预览.tif", "RGB")):
        fp = d / name
        if not fp.is_file():
            print(f"    ❌ {name} 缺失")
            continue
        im = Image.open(fp)
        ok = "✅" if im.mode == expect else f"⚠️ 期望 {expect}"
        print(f"    {name:<28} mode={im.mode:<6} size={im.size}  {ok}")

    m = read_print_manifest(d)
    if m:
        print("\n  manifest 关键字段:")
        print(f"    status        : {m.get('status')}")
        print(f"    source.png    : {(m.get('source') or {}).get('png')}")
        print(f"    output.pixels : {(m.get('output') or {}).get('pixels')}")
        print(f"    output.dpi    : {(m.get('output') or {}).get('dpi')}")
        print(f"    effective_dpi : {(m.get('output') or {}).get('effective_dpi')}")
        print(f"    icc           : {(m.get('icc') or {}).get('name')} / source={(m.get('icc') or {}).get('source')}")
        print(f"    bleed         : {m.get('bleed')}")
        print(f"    layers        : {list((m.get('layers') or {}).keys())}")
        print(f"    warnings      : {m.get('warnings')}")
    else:
        print("  ❌ print_manifest.json 读取失败")
else:
    print(f"  错误: {res.error_message}")

# ---- 验证源图未被修改 ----
after = hashlib.sha256(src.read_bytes()).hexdigest()
after_mtime = src.stat().st_mtime_ns
print()
print(f"  源图 hash 不变 : {'✅' if before == after else '❌ 被修改了！'}")
print(f"  源图 mtime 不变: {'✅' if before_mtime == after_mtime else '❌ 被改动了！'}")

# ---- 验证 _work 链接 ----
work = store_dir / "_work"
if work.is_dir():
    items = list(work.iterdir())
    print(f"  _work/ 内容    : {[i.name for i in items]}")
    if items:
        try:
            same = items[0].stat().st_ino == src.stat().st_ino and items[0].stat().st_dev == src.stat().st_dev
            print(f"  母版与源图同一 inode: {'✅ 硬链接' if same else '⚠️ 是副本（跨分区或不支持硬链接）'}")
        except OSError:
            pass

print("=" * 74)
