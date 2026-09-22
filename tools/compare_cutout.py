# -*- coding: utf-8 -*-
"""对比 rembg 与纯色降级的去背质量。

用同一张真实生成图，分别走两条路径，比对：
    · Alpha 覆盖率（前景占比是否合理）
    · 边缘像素数（杂边多少）
    · 耗时
并把结果图存到 logs/ 供人眼核对。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app.print_export.layers import CutoutSession, fallback_cutout  # noqa: E402

# 模型缓存位置排查
print("=" * 76)
print("rembg 模型缓存位置")
print("=" * 76)
import os  # noqa: E402

for cand in [
    Path.home() / ".u2net",
    Path.home() / ".cache" / "rembg",
    Path(os.environ.get("USERPROFILE", "")) / ".u2net",
    Path(os.environ.get("LOCALAPPDATA", "")) / "rembg",
]:
    if cand.is_dir():
        files = list(cand.iterdir())
        total = sum(f.stat().st_size for f in files if f.is_file())
        print(f"  ✅ {cand}  ({len(files)} 个文件，{total // 1024 // 1024} MB)")
        for f in files[:3]:
            print(f"       {f.name}")

# 找测试图
src = None
for p in sorted((ROOT / "output").glob("batch_*/*/*生成图/*.png")):
    src = p
    break
if src is None:
    print("  ❌ 没有测试图")
    raise SystemExit(1)

print()
print("=" * 76)
print("去背质量对比")
print("=" * 76)
print(f"  源图: {src.name}")

img = Image.open(src)
print(f"  尺寸: {img.size[0]}×{img.size[1]}\n")


def stats(rgba: Image.Image, label: str, dt: float) -> dict:
    a = rgba.convert("RGBA").getchannel("A")
    hist = a.histogram()
    total = sum(hist) or 1
    solid = sum(hist[128:])          # 不透明
    semi = sum(hist[8:128])          # 半透明（杂边候选）
    ghost = sum(hist[1:8])           # 幽灵像素
    print(f"  {label}")
    print(f"    耗时        : {dt:.2f}s")
    print(f"    前景占比    : {solid / total * 100:.1f}%")
    print(f"    半透明像素  : {semi}（{semi / total * 100:.2f}%）← 印刷杂边来源")
    print(f"    幽灵像素    : {ghost}（alpha 1~7）")
    return {"solid": solid, "semi": semi, "ghost": ghost, "dt": dt}


# ① 纯色降级
t0 = time.monotonic()
fb = fallback_cutout(img)
t_fb = time.monotonic() - t0
s_fb = stats(fb, "① 纯色降级（fallback）", t_fb)

# ② rembg
session = CutoutSession()
t0 = time.monotonic()
rb = session.cutout(img)
t_rb = time.monotonic() - t0
if rb is None:
    print("\n  ⚠️ rembg 不可用，跳过对比")
    raise SystemExit(0)
s_rb = stats(rb, "\n② rembg（u2net）", t_rb)

# 保存对比图
out_dir = ROOT / "logs"
out_dir.mkdir(exist_ok=True)
for name, im in (("cutout_fallback.png", fb), ("cutout_rembg.png", rb)):
    p = out_dir / name
    im.save(p)
    print(f"\n  已保存: logs/{name}")

print()
print("=" * 76)
print("结论")
print("=" * 76)
print(f"  半透明杂边: fallback {s_fb['semi']}  vs  rembg {s_rb['semi']}")
if s_fb["semi"] > 0:
    ratio = s_rb["semi"] / s_fb["semi"]
    better = "rembg 更干净" if ratio < 1 else "fallback 更干净"
    print(f"  → {better}（比值 {ratio:.2f}）")
print(f"  耗时: fallback {t_fb:.2f}s  vs  rembg {t_rb:.2f}s")
print("=" * 76)
