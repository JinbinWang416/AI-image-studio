# -*- coding: utf-8 -*-
"""
分析「真实产品安装照」与「我们的合成图」的视觉差异，为参数校准提供数值依据。

测量项：
  - 清晰度（Laplacian 能量）：判断背景该有多清晰
  - 亮度 / 对比度：判断整体曝光
  - 色温倾向（R/B 比）：判断暖光强度
  - 饱和度：判断贴纸与背景的色彩浓度
  - 高频能量分区：中心区（贴纸所在）vs 边缘区（背景）

用法：
    .\\.venv\\Scripts\\python.exe tools\\analyze_benchmark.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFilter, ImageStat  # noqa: E402

BENCH_DIR = ROOT / "output" / "_references"
XHS_DIR = BENCH_DIR / "xhs"
PREVIEW_DIR = ROOT / "logs"


def laplacian_energy(image: Image.Image) -> float:
    """用边缘滤波后的标准差近似清晰度（越大越清晰）。"""
    edges = image.convert("L").filter(ImageFilter.FIND_EDGES)
    return round(ImageStat.Stat(edges).stddev[0], 2)


def region_energy(image: Image.Image, box_ratio: tuple[float, float, float, float]) -> float:
    w, h = image.size
    box = (int(w * box_ratio[0]), int(h * box_ratio[1]),
           int(w * box_ratio[2]), int(h * box_ratio[3]))
    return laplacian_energy(image.crop(box))


def analyze(path: pathlib.Path) -> dict:
    with Image.open(path) as im:
        rgb = im.convert("RGB")
        w, h = rgb.size
        gray = rgb.convert("L")
        st = ImageStat.Stat(gray)
        rst = ImageStat.Stat(rgb)
        r, g, b = rst.mean
        hsv = rgb.convert("HSV")
        sat = ImageStat.Stat(hsv).mean[1]
        return {
            "name": path.name,
            "size": f"{w}×{h}",
            "sharpness": laplacian_energy(rgb),
            # 中心区（贴纸区）与四角区（背景区）的清晰度
            "center_sharp": region_energy(rgb, (0.30, 0.30, 0.70, 0.70)),
            "corner_sharp": region_energy(rgb, (0.0, 0.0, 0.18, 0.18)),
            "mean": round(st.mean[0], 1),
            "contrast": round(st.stddev[0], 1),
            "warmth": round(r / max(1.0, b), 3),
            "saturation": round(sat, 1),
        }


def main() -> int:
    groups: list[tuple[str, list[pathlib.Path]]] = []

    bench = sorted(BENCH_DIR.glob("benchmark_*.jpg"))
    if bench:
        groups.append(("真实产品安装照（用户提供）", bench))

    xhs = sorted(XHS_DIR.glob("*.jpg")) + sorted(XHS_DIR.glob("*.png")) if XHS_DIR.is_dir() else []
    if xhs:
        groups.append(("小红书实拍参考", xhs[:12]))

    pdd_dir = BENCH_DIR / "pdd"
    pdd = sorted(pdd_dir.glob("*.jpg")) + sorted(pdd_dir.glob("*.png")) if pdd_dir.is_dir() else []
    if pdd:
        groups.append(("拼多多「东东窗花店」商品效果图", pdd))

    synth = sorted(PREVIEW_DIR.glob("preview_preset_*.png"))
    if synth:
        groups.append(("我们的合成图", synth))

    if not groups:
        print("未找到可分析的图片")
        return 1

    header = f"{'文件':<44}{'清晰度':>8}{'中心':>8}{'边角':>8}{'亮度':>8}{'对比':>8}{'暖度':>8}{'饱和':>8}"
    for title, files in groups:
        print("=" * len(header))
        print(title)
        print("=" * len(header))
        print(header)
        print("-" * len(header))
        for p in files:
            try:
                m = analyze(p)
            except Exception as exc:  # noqa: BLE001
                print(f"{p.name:<44}  ❌ {exc}")
                continue
            print(f"{m['name'][:42]:<44}{m['sharpness']:>8.1f}{m['center_sharp']:>8.1f}"
                  f"{m['corner_sharp']:>8.1f}{m['mean']:>8.1f}{m['contrast']:>8.1f}"
                  f"{m['warmth']:>8.3f}{m['saturation']:>8.1f}")
        print()

    print("=" * len(header))
    print("【解读】")
    print("  清晰度   ：边缘能量标准差，越大越锐利。真实照片通常 20~60，过度模糊会 <15")
    print("  中心/边角：贴纸区 vs 背景区的清晰度。真实实拍中 边角 明显低于 中心（景深）")
    print("  暖度     ：R/B 均值比。>1.15 偏暖（室内暖光），<1.0 偏冷")
    print("  饱和     ：>90 色彩浓郁（广告类贴纸），<50 素雅（磨砂/纯色类）")
    print("=" * len(header))
    return 0


if __name__ == "__main__":
    sys.exit(main())
