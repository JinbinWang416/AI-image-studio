# -*- coding: utf-8 -*-
"""抽检 v6 背景的质量，并与参照系对比。"""

from __future__ import annotations

import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageStat  # noqa: E402

BG = ROOT / "output" / "_effect_backgrounds"
STAMP_FILE = ROOT / "logs" / "_bg_v6_stamp.txt"


def measure(p: pathlib.Path) -> tuple:
    with Image.open(p) as im:
        rgb = im.convert("RGB")
        g = rgb.convert("L")
        w, h = rgb.size
        st = ImageStat.Stat(g)
        rst = ImageStat.Stat(rgb)
        r, _, b = rst.mean
        sat = ImageStat.Stat(rgb.convert("HSV")).mean[1]
        corner = ImageStat.Stat(g.crop((0, 0, w // 8, h // 8))).mean[0]
        return (st.mean[0], st.stddev[0], r / max(1.0, b), sat, corner)


def main() -> int:
    # 文件里的时间戳带时区，与 st_mtime 比较时统一为 naive（本机同一时区）
    stamp = datetime.datetime.fromisoformat(
        STAMP_FILE.read_text(encoding="utf-8-sig").strip()
    ).replace(tzinfo=None)
    files = [
        p for p in sorted(BG.glob("*.png"))
        if datetime.datetime.fromtimestamp(p.stat().st_mtime) >= stamp
    ]
    print("=" * 70)
    print(f"v6 背景质量抽检（本次生成 {len(files)} 张）")
    print("=" * 70)
    print(f"{'样本':<26}{'亮度':>8}{'对比':>8}{'暖度':>8}{'饱和':>8}{'四角':>8}")
    print("-" * 70)
    print(f"{'★ 拼多多商品效果图':<26}{77.0:>8.1f}{56.4:>8.1f}{2.400:>8.3f}{152.8:>8.1f}{56.0:>8.1f}")
    print("-" * 70)

    vals = []
    for p in files[:8]:
        v = measure(p)
        vals.append(v)
        print(f"{p.name[:24]:<26}{v[0]:>8.1f}{v[1]:>8.1f}{v[2]:>8.3f}{v[3]:>8.1f}{v[4]:>8.1f}")
    print("-" * 70)

    if vals:
        n = len(vals)
        avg = [sum(v[i] for v in vals) / n for i in range(5)]
        print(f"{'v6 均值':<26}{avg[0]:>8.1f}{avg[1]:>8.1f}{avg[2]:>8.3f}{avg[3]:>8.1f}{avg[4]:>8.1f}")
        print(f"{'（对比 v5 夜景版）':<26}{'62~66':>8}{'57.2':>8}{'3.6~3.8':>8}{'168~173':>8}{'7~14':>8}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
