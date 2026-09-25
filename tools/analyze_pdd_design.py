# -*- coding: utf-8 -*-
"""
分析拼多多「东东窗花店」贴纸设计图的**配色与构图规律**。

目的：把"看起来不错"变成可写进提示词的具体描述。

用法：
    .\\.venv\\Scripts\\python.exe tools\\analyze_pdd_design.py
"""
from __future__ import annotations

import pathlib
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageFilter, ImageStat  # noqa: E402

PDD = ROOT / "output" / "_references" / "pdd"


def dominant_colors(image: Image.Image, k: int = 6) -> list[tuple[str, float]]:
    """用中位切分法近似取主色，返回 [(hex, 占比), ...]。"""
    small = image.convert("RGB").resize((120, 120), Image.Resampling.BILINEAR)
    # 量化到 32 级，统计直方图
    quant = small.quantize(colors=k, method=Image.Quantize.MEDIANCUT)
    palette = quant.getpalette() or []
    counts = Counter(quant.getdata())
    total = sum(counts.values()) or 1
    out: list[tuple[str, float]] = []
    for idx, cnt in counts.most_common(k):
        r, g, b = palette[idx * 3: idx * 3 + 3]
        out.append((f"#{r:02X}{g:02X}{b:02X}", round(cnt / total * 100, 1)))
    return out


def hue_family(hex_color: str) -> str:
    """把颜色归到中文色系。"""
    r = int(hex_color[1:3], 16)
    g = int(hex_color[3:5], 16)
    b = int(hex_color[5:7], 16)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 26:
        return "中性（黑/白/灰）"
    if mx == r and g >= b:
        return "暖红/橙/黄" if g > b * 1.25 else "红/粉"
    if mx == g:
        return "绿色系"
    if mx == b:
        return "蓝色系"
    return "其他"


def main() -> int:
    # 支持传入其它目录做同样的分析（例：对比我们自己的产出）
    target = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else PDD
    globs = ("*.jpg", "*.png")
    files: list[pathlib.Path] = []
    for g in globs:
        files.extend(sorted(target.glob(g)))
    if not files:
        print(f"未找到样本（{target}）")
        return 1

    print("=" * 84)
    print(f"贴纸设计规律分析 —— {target}")
    print("=" * 84)

    family_counter: Counter[str] = Counter()
    rows = []

    for p in files:
        with Image.open(p) as im:
            rgb = im.convert("RGB")
            w, h = rgb.size
            cols = dominant_colors(rgb)
            st = ImageStat.Stat(rgb.convert("L"))
            rst = ImageStat.Stat(rgb)
            r, g, b = rst.mean
            sat = ImageStat.Stat(rgb.convert("HSV")).mean[1]

            # 中央区域（贴纸主体）与其外侧的亮度差 → 贴纸是否比背景亮
            cx0, cy0 = int(w * .32), int(h * .32)
            cx1, cy1 = int(w * .68), int(h * .68)
            center = ImageStat.Stat(rgb.crop((cx0, cy0, cx1, cy1)).convert("L")).mean[0]
            border = ImageStat.Stat(
                rgb.crop((0, 0, int(w * .12), int(h * .12))).convert("L")
            ).mean[0]

            for hexc, pct in cols:
                if pct >= 8:
                    family_counter[hue_family(hexc)] += 1

            rows.append({
                "name": p.name, "size": f"{w}×{h}",
                "mean": round(st.mean[0], 1), "warm": round(r / max(1.0, b), 2),
                "sat": round(sat, 1),
                "center": round(center, 1), "border": round(border, 1),
                "delta": round(center - border, 1),
                "colors": cols[:4],
            })

    print(f"\n{'文件':<14}{'尺寸':>11}{'亮度':>7}{'暖度':>7}{'饱和':>7}{'中心':>7}{'四角':>7}{'差值':>7}")
    print("-" * 84)
    for r_ in rows:
        print(f"{r_['name']:<14}{r_['size']:>11}{r_['mean']:>7}{r_['warm']:>7}"
              f"{r_['sat']:>7}{r_['center']:>7}{r_['border']:>7}{r_['delta']:>7}")

    print("\n【每张图的主色（占比 ≥8% 的）】")
    for r_ in rows:
        cs = "  ".join(f"{c}({p}%)" for c, p in r_["colors"])
        print(f"  {r_['name']:<14}{cs}")

    print("\n【色系出现频次（按主色统计）】")
    for fam, cnt in family_counter.most_common():
        print(f"  {fam:<18}{cnt:>3} 次")

    # 汇总
    if rows:
        avg = lambda k: round(sum(r_[k] for r_ in rows) / len(rows), 1)  # noqa: E731
        print("\n【汇总】")
        print(f"  平均亮度     : {avg('mean')}")
        print(f"  平均暖度 R/B : {avg('warm')}")
        print(f"  平均饱和度   : {avg('sat')}")
        print(f"  中心比四角亮 : {avg('delta'):+} （正值表示贴纸区比环境亮）")
        bright = sum(1 for r_ in rows if r_["delta"] > 0)
        print(f"  贴纸亮于环境 : {bright}/{len(rows)} 张")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    sys.exit(main())
