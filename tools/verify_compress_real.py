# -*- coding: utf-8 -*-
"""用**真实产物图**验证压缩效果（人工噪声图不代表实际情况）。"""

from __future__ import annotations

import base64
import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app.reference_assets import (  # noqa: E402
    REFERENCE_MAX_EDGE,
    ReferenceAssetStore,
    downscale_image,
)

print("=" * 78)
print("真实图片压缩验证")
print("=" * 78)

# 找真实的生成图 / 效果图
cands: list[Path] = []
for pat in ("output/*/*/*生成图/*.png", "output/*/*/*效果图/*.png",
            "output/_references/*.png", "output/_references/*.jpg"):
    cands.extend(sorted(ROOT.glob(pat))[:3])
if not cands:
    print("  ⚠️ 没找到真实图片，跳过")
    raise SystemExit(0)

print(f"  {'文件':<34} {'尺寸':<13} {'原始':>9} {'压缩后':>9} {'变化':>9}")
print("  " + "-" * 74)

total_before = total_after = 0
for p in cands[:8]:
    raw = p.read_bytes()
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    out, info = downscale_image(raw, mime)
    with Image.open(io.BytesIO(raw)) as im:
        src_size = im.size
    with Image.open(io.BytesIO(out)) as im:
        dst_size = im.size

    before, after = len(raw), len(out)
    total_before += before
    total_after += after
    delta = (after - before) * 100 // max(before, 1)
    sign = "+" if delta >= 0 else ""
    flag = "✅" if after < before else ("➖" if not info.get("applied") else "⚠️")
    print(f"  {p.name[:32]:<34} {str(src_size):<13} "
          f"{before // 1024:>7} KB {after // 1024:>7} KB {sign}{delta:>7}%  {flag}")

print("  " + "-" * 74)
if total_before:
    d = (total_after - total_before) * 100 // max(total_before, 1)
    print(f"  {'合计':<34} {'':<13} {total_before // 1024:>7} KB "
          f"{total_after // 1024:>7} KB {d:>+8}%")

# ---------------------------------------------------------------- 入库链路
print()
print("=" * 78)
print("入库链路 + 去重 + payload")
print("=" * 78)

p = cands[0]
raw = p.read_bytes()
mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
with Image.open(io.BytesIO(raw)) as im:
    print(f"  源图: {p.name}  {im.size}  {len(raw) // 1024} KB")

with tempfile.TemporaryDirectory() as tmp:
    store = ReferenceAssetStore(Path(tmp))
    url = f"data:{mime};base64," + base64.b64encode(raw).decode()
    asset = store.add_data_url(url, p.name)

    print(f"  入库: {asset.bytes // 1024} KB  id={asset.id}")

    # 去重
    again = store.add_data_url(url, p.name + ".dup")
    print(f"  去重: {'✅ 同一资产' if again.id == asset.id else '❌ 新资产'}")

    # 缓存：先清掉，再对比
    from app import reference_assets as ra

    ra._URL_CACHE.clear()
    import time

    t0 = time.monotonic()
    for _ in range(20):
        asset.data_url()
    cold = time.monotonic() - t0

    t0 = time.monotonic()
    for _ in range(20):
        asset.data_url()
    warm = time.monotonic() - t0

    print(f"  data_url 20 次（含首次）: {cold * 1000:.1f} ms")
    print(f"  data_url 20 次（全缓存）: {warm * 1000:.2f} ms")
    if warm > 0:
        print(f"  缓存提速约 {cold / warm:.0f}×")

    # payload
    orig_payload = len(f"data:{mime};base64," + base64.b64encode(raw).decode())
    new_payload = len(asset.data_url())
    print(f"\n  Base64 payload: {orig_payload // 1024} KB → {new_payload // 1024} KB "
          f"({(new_payload - orig_payload) * 100 // max(orig_payload, 1):+d}%)")

print("=" * 78)
