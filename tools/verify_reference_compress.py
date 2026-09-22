# -*- coding: utf-8 -*-
"""验证参考图压缩与 data_url 缓存。"""

from __future__ import annotations

import io
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app.reference_assets import (  # noqa: E402
    REFERENCE_MAX_EDGE,
    ReferenceAssetStore,
    downscale_image,
)

print("=" * 76)
print("① downscale_image：大图压缩")
print("=" * 76)


def make_png(w: int, h: int) -> bytes:
    """造一张**有真实细节**的图（渐变 + 纹理）。

    纯色或稀疏点阵会被 PNG 压到极小，测不出"边长超限"的场景 ——
    而真实门店照片、AI 生成图都是连续色调。
    """
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        gy = (y * 255) // max(1, h)
        for x in range(w):
            gx = (x * 255) // max(1, w)
            # 渐变 + 轻微噪声，模拟真实图像
            n = ((x * 7919 + y * 104729) % 23) - 11
            px[x, y] = (
                max(0, min(255, gx + n)),
                max(0, min(255, gy + n)),
                max(0, min(255, (gx + gy) // 2 + n)),
            )
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=6)
    return buf.getvalue()


for label, (w, h) in (("大图 4000×3000", (4000, 3000)),
                      ("方图 2048×2048", (2048, 2048)),
                      ("小图 800×600", (800, 600))):
    raw = make_png(w, h)
    out, info = downscale_image(raw, "image/png")
    with Image.open(io.BytesIO(out)) as im:
        size = im.size
    applied = "✅ 已压缩" if info.get("applied") else "— 未压缩"
    print(f"  {label:<16} {len(raw) // 1024:>6} KB → {len(out) // 1024:>6} KB   "
          f"{size}   {applied}  ({info.get('reason')})")

# 压缩后边长必须在上限内
raw = make_png(4000, 3000)
out, info = downscale_image(raw, "image/png", max_edge=1200)
with Image.open(io.BytesIO(out)) as im:
    assert max(im.size) <= 1200, f"压缩后仍超限：{im.size}"
print(f"\n  ✅ 自定义上限 1200 生效：{im.size}")

print()
print("=" * 76)
print("② ReferenceAssetStore：入库压缩 + 去重 + data_url 缓存")
print("=" * 76)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    store = ReferenceAssetStore(root)

    import base64

    raw = make_png(3000, 2000)
    url = "data:image/png;base64," + base64.b64encode(raw).decode()
    asset = store.add_data_url(url, "big.png")

    print(f"  上传原始体积 : {len(raw) // 1024} KB (3000×2000)")
    print(f"  入库后体积   : {asset.bytes // 1024} KB")
    with Image.open(asset.path) as im:
        print(f"  入库后尺寸   : {im.size}  (上限 {REFERENCE_MAX_EDGE})")
        assert max(im.size) <= REFERENCE_MAX_EDGE, "入库后仍超过上限"
    print(f"  压缩记录     : {asset.extra.get('compressed') or '（meta 里，见文件）'}")

    # 去重：同一张图再传一次 → 同一资产
    again = store.add_data_url(url, "big-again.png")
    print(f"  重复上传     : {'✅ 同一资产（去重生效）' if again.id == asset.id else '❌ 生成了新资产'}")

    # data_url 缓存
    t0 = time.monotonic()
    for _ in range(20):
        asset.data_url()
    dt_cached = time.monotonic() - t0

    t0 = time.monotonic()
    for _ in range(20):
        base64.b64encode(asset.path.read_bytes()).decode("ascii")
    dt_raw = time.monotonic() - t0

    print(f"  20 次 data_url（缓存）: {dt_cached * 1000:.1f} ms")
    print(f"  20 次 直接编码        : {dt_raw * 1000:.1f} ms")
    if dt_raw > 0:
        print(f"  提速约 {dt_raw / max(dt_cached, 1e-9):.1f}×")

    # 压缩前后 payload 对比
    payload_kb = len(asset.data_url()) // 1024
    orig_payload_kb = len(base64.b64encode(raw)) // 1024
    print(f"\n  Base64 payload: 原始 {orig_payload_kb} KB → 现在 {payload_kb} KB "
          f"（省 {100 - payload_kb * 100 // max(orig_payload_kb, 1)}%）")

print("=" * 76)
