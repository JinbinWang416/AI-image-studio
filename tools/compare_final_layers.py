# -*- coding: utf-8 -*-
"""查 rembg 模型缓存位置，并对比两种去背产出的最终白墨层。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

print("=" * 76)
print("① rembg 模型缓存位置")
print("=" * 76)

import os  # noqa: E402
import subprocess  # noqa: E402

# rembg 用 pooch 下载，缓存在平台默认缓存目录
cands = [
    Path.home() / ".u2net",
    Path(os.environ.get("USERPROFILE", "")) / ".u2net",
    Path(os.environ.get("LOCALAPPDATA", "")) / "rembg",
    Path(os.environ.get("APPDATA", "")) / "rembg",
    Path.home() / ".cache" / "rembg",
]
found = False
for c in cands:
    if c.is_dir() and any(c.iterdir()):
        files = [f for f in c.rglob("*") if f.is_file()]
        total = sum(f.stat().st_size for f in files)
        print(f"  ✅ {c}")
        print(f"     {len(files)} 个文件，{total // 1024 // 1024} MB")
        for f in files[:5]:
            print(f"       {f.name}  {f.stat().st_size // 1024 // 1024} MB")
        found = True

if not found:
    # 全盘找 u2net*.onnx
    print("  未在常见位置找到，搜索 u2net 模型文件…")
    for base in (Path.home(), Path(os.environ.get("LOCALAPPDATA", "C:/")),
                 Path(os.environ.get("USERPROFILE", "C:/"))):
        if not base.is_dir():
            continue
        try:
            hits = list(base.rglob("u2net*.onnx"))
        except (OSError, PermissionError):
            continue
        for h in hits[:5]:
            print(f"  ✅ {h}  {h.stat().st_size // 1024 // 1024} MB")
            found = True
        if hits:
            break

if not found:
    print("  ⚠️ 没找到模型文件（可能在别处，但不影响使用）")

# ================================================================
print()
print("=" * 76)
print("② 最终白墨层对比（含 1px 收边后）")
print("=" * 76)

from app.config import load_config  # noqa: E402
from app.print_export import PrintExporter  # noqa: E402

src = None
for p in sorted((ROOT / "output").glob("batch_*/*/*生成图/*.png")):
    src = p
    break
if src is None:
    print("  ❌ 没有测试图")
    raise SystemExit(1)

store_dir = src.parent.parent
img = Image.open(src)
print(f"  源图: {src.name}\n")

for label, use_rembg in (("fallback", False), ("rembg", True)):
    cfg = load_config()
    cfg.print_width_cm = 10.0
    cfg.print_dpi = 100
    cfg.print_max_pixels = 1200
    cfg.print_keep_work = False

    exporter = PrintExporter(cfg)
    if not use_rembg:
        # 禁用 rembg：用一个不可用的 session 迫使走降级
        from app.print_export.layers import CutoutSession

        class NoRembg(CutoutSession):
            @property
            def available(self):
                return False

        exporter.session = NoRembg()

    t0 = time.monotonic()
    res = exporter.export_store(store_dir, src, store_name=f"CMP_{label}")
    dt = time.monotonic() - t0

    method = (res.manifest.options.get("cutout") if res.manifest else "?")
    print(f"  【{label}】耗时 {dt:.2f}s  去背方式={method}")
    if res.ok:
        white = res.print_dir / f"CMP_{label}_白墨.tif"
        with Image.open(white) as w:
            hist = w.convert("L").histogram()
            # 白墨层只有 0 和 255 两个值才算"硬边"（印刷友好）
            mid = sum(hist[1:255])
            print(f"    白墨层: 纯黑={hist[0]}  纯白={hist[255]}  "
                  f"中间值={mid}  {'✅ 硬边' if mid == 0 else '⚠️ 有软边'}")
        cmyk = res.print_dir / f"CMP_{label}_CMYK.tif"
        print(f"    CMYK 层: {cmyk.stat().st_size // 1024} KB")
    else:
        print(f"    ❌ 失败: {res.error_code} {res.error_message}")

# 清理测试产物
import shutil  # noqa: E402

for sub in ("印刷TIF", "预览", "_work"):
    p = store_dir / sub
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
print("\n  ✅ 已清理测试产物")
print("=" * 76)
