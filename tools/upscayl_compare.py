# -*- coding: utf-8 -*-
"""Upscayl 文字边缘验收门禁。

## 为什么要这个脚本

超分模型是为**照片**训练的，对**中文笔画**未必友好 —— 笔画粘连、糊边、
过冲（overshoot）都会在 60cm 印刷尺寸下放大成肉眼可见的缺陷。
所以在把 Upscayl 接进印刷链路之前，先用它确认：

- **锐度不降**：文字包围盒内 Laplacian 方差 ≥ LANCZOS 的 90%
  （低于这个数说明超分把笔画抹平了，还不如直接拉伸）
- **结构不坏**：与 LANCZOS 结果做 SSIM ≥ 0.95
  （低于这个数说明整体结构被模型改动过大，可能是幻觉细节）
- 并且**产出对比图供人眼审查** —— 这两个指标只能筛掉明显不行的，
  「这个字还认得出吗」必须用眼睛看

若某个模型不达标，**换模型重测即可，不要改这里的代码**
（例如 animevideov3-x2 不过就试 realesrgan-x4plus-anime / x4plus）。

用法：

    .\\.venv\\Scripts\\python.exe tools\\upscayl_compare.py
    .\\.venv\\Scripts\\python.exe tools\\upscayl_compare.py --model realesrgan-x4plus-anime
    .\\.venv\\Scripts\\python.exe tools\\upscayl_compare.py --image <某张PNG> --scale 2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _console import enable_safe_output  # noqa: E402

enable_safe_output()

from app.upscayl import DEFAULT_TILE, UpscaylEngine, model_names  # noqa: E402

OUT_DIR = ROOT / "output" / "_upscayl_smoke"
MIN_SHARPNESS_RATIO = 0.90
MIN_SSIM = 0.95


def _laplacian_var(img) -> float:
    """Laplacian 方差：经典的对焦/锐度指标，越大越锐。"""
    import cv2
    import numpy as np

    arr = np.asarray(img.convert("L"), dtype=np.float64)
    return float(cv2.Laplacian(arr, cv2.CV_64F).var())


def _text_bbox(gray) -> tuple[int, int, int, int] | None:
    """粗略找出「有墨」的区域（文字包围盒）。

    用 Otsu 阈值把非白像素当墨，取外接矩形。找不到就返回 None，
    调用方退化为整图评估。
    """
    import cv2
    import numpy as np

    arr = np.asarray(gray)
    _, binary = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ys, xs = np.where(binary > 0)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _crop(img, box):
    if box is None:
        return img
    x0, y0, x1, y1 = box
    if x1 - x0 < 4 or y1 - y0 < 4:
        return img
    return img.crop((x0, y0, x1, y1))


def evaluate(src: Path, model: str, scale: int, tile: int) -> dict:
    """对一张图跑 Upscayl 与 LANCZOS 两条路线并比较。"""
    from PIL import Image

    engine = UpscaylEngine()
    if not engine.available:
        raise SystemExit(f"Upscayl 不可用：{engine.unavailable_reason()}")

    with Image.open(src) as im:
        base = im.convert("RGB")
    target = (base.width * scale, base.height * scale)

    # 参照路线：LANCZOS
    lanczos = base.resize(target, Image.LANCZOS)

    # 超分路线
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    up_path = OUT_DIR / f"cmp_{model}_x{scale}.png"
    got = engine.upscale_image(src, up_path, model, scale, tile)
    if got is None:
        raise SystemExit(f"Upscayl 执行失败：{model}")

    with Image.open(got) as im:
        up = im.convert("RGB")
    if up.size != target:
        up = up.resize(target, Image.LANCZOS)

    # 文字区域（以 LANCZOS 版为准取包围盒，两边裁同一块）
    box = _text_bbox(lanczos.convert("L"))
    l_crop, u_crop = _crop(lanczos, box), _crop(up, box)

    lap_l = _laplacian_var(l_crop)
    lap_u = _laplacian_var(u_crop)
    ratio = (lap_u / lap_l) if lap_l > 1e-9 else float("inf")

    ssim_val = None
    try:
        from skimage.metrics import structural_similarity

        import numpy as np

        ssim_val = float(structural_similarity(
            np.asarray(l_crop.convert("L"), dtype=np.float64),
            np.asarray(u_crop.convert("L"), dtype=np.float64),
            data_range=255.0,
        ))
    except ImportError:
        pass

    # ---- 产出对比图 ----
    stem = f"{src.stem}_{model}_x{scale}"
    side = OUT_DIR / f"{stem}_side.png"
    canvas = Image.new("RGB", (l_crop.width * 2 + 24, l_crop.height + 40), (245, 245, 245))
    canvas.paste(l_crop, (8, 32))
    canvas.paste(u_crop, (l_crop.width + 16, 32))
    canvas.save(side)

    # 局部 2 倍放大（看笔画细节）
    zoom_box = _center_zoom(box, target)
    zl, zu = lanczos.crop(zoom_box), up.crop(zoom_box)
    zl = zl.resize((zl.width * 2, zl.height * 2), Image.NEAREST)
    zu = zu.resize((zu.width * 2, zu.height * 2), Image.NEAREST)
    zoom = OUT_DIR / f"{stem}_zoom.png"
    zc = Image.new("RGB", (zl.width * 2 + 24, zl.height + 40), (245, 245, 245))
    zc.paste(zl, (8, 32))
    zc.paste(zu, (zl.width + 16, 32))
    zc.save(zoom)

    return {
        "src": src.name,
        "model": model,
        "scale": scale,
        "target": target,
        "lap_lanczos": lap_l,
        "lap_upscayl": lap_u,
        "ratio": ratio,
        "ssim": ssim_val,
        "side": side,
        "zoom": zoom,
        "ok": (ratio >= MIN_SHARPNESS_RATIO) and (ssim_val is None or ssim_val >= MIN_SSIM),
    }


def _center_zoom(box, size):
    """取文字区域中央的一小块用于局部放大。"""
    w, h = size
    if box is None:
        cx, cy = w // 2, h // 2
    else:
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    half = max(48, min(w, h) // 8)
    return (
        max(0, cx - half), max(0, cy - half),
        min(w, cx + half), min(h, cy + half),
    )


def find_sample_images(limit: int) -> list[Path]:
    """找几张**纯贴纸生成图**做样本。

    ⚠️ 必须排除「效果图」：那是合成到门店实拍照片上的图，
       超分模型对照片会做大幅去噪/锐化，SSIM 会掉到 0.6 以下 ——
       但那是**预期行为**（我们本来就不打算对效果图超分），
       拿它当样本只会得到一个无意义的 FAIL。

       真正要验收的是：**含中文笔画的贴纸**超分后会不会糊边。
    """
    pats = ("*房屋门牌*.png", "*服务柜台*.png", "*楼房线稿*.png")
    found: list[Path] = []
    for pat in pats:
        for p in sorted((ROOT / "output").rglob(pat)):
            name = p.name
            if "_upscayl" in str(p):
                continue
            if "效果图" in name:            # ← 关键排除
                continue
            found.append(p)
            break
    return found[:limit]


def main() -> int:
    ap = argparse.ArgumentParser(description="Upscayl 文字边缘验收")
    ap.add_argument("--model", default="realesr-animevideov3-x2",
                    help=f"模型名，可选：{', '.join(model_names())}")
    ap.add_argument("--scale", type=int, default=2, choices=(2, 3, 4))
    ap.add_argument("--tile", type=int, default=DEFAULT_TILE)
    ap.add_argument("--image", default="", help="单张图路径；留空则自动找样本")
    ap.add_argument("--limit", type=int, default=3)
    args = ap.parse_args()

    images = [Path(args.image)] if args.image else find_sample_images(args.limit)
    if not images:
        print("  [!!] 没找到样本图，请用 --image 指定一张含中文的贴纸 PNG")
        return 2

    print("=" * 78)
    print(f"Upscayl 文字边缘验收   模型={args.model}  倍数={args.scale}  tile={args.tile}")
    print("=" * 78)
    print(f"  判定门槛：锐度比 ≥ {MIN_SHARPNESS_RATIO:.0%}    SSIM ≥ {MIN_SSIM}")
    print()

    all_ok = True
    for img in images:
        try:
            r = evaluate(img, args.model, args.scale, args.tile)
        except SystemExit as exc:
            print(f"  [!!] {img.name}: {exc}")
            all_ok = False
            continue

        mark = "OK  " if r["ok"] else "FAIL"
        ssim_txt = f"{r['ssim']:.4f}" if r["ssim"] is not None else "(skimage 未装)"
        print(f"  [{mark}] {r['src'][:42]}")
        print(f"         目标尺寸 {r['target'][0]}×{r['target'][1]}")
        print(f"         Laplacian  LANCZOS={r['lap_lanczos']:.1f}  Upscayl={r['lap_upscayl']:.1f}"
              f"  比值={r['ratio']:.2f}")
        print(f"         SSIM       {ssim_txt}")
        print(f"         对比图     {r['side'].relative_to(ROOT)}")
        print(f"         局部放大   {r['zoom'].relative_to(ROOT)}")
        print()
        all_ok = all_ok and r["ok"]

    print("=" * 78)
    if all_ok:
        print("  结论：全部达标 ✅（仍建议人工看一眼对比图确认中文笔画）")
        return 0
    print("  结论：有不达标项 ❌ —— 换模型重测（如 realesrgan-x4plus-anime），代码不用改")
    return 1


if __name__ == "__main__":
    sys.exit(main())
