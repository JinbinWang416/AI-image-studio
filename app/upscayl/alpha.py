# -*- coding: utf-8 -*-
"""RGBA 超分：把 alpha 拆出来单独处理，再合成回去。

## 为什么要拆

印刷链路（`print_export`）在去背之后拿到的是 **RGBA** 图。
直接把 RGBA 喂给 Upscayl，alpha 的处置取决于 CLI 版本的实现细节 ——
项目交接文档记录的是「alpha 被丢弃」，本次实测（`realesr-animevideov3-x2`）
却是「完整保留」。两种行为都无法保证，而印刷对边缘的要求很严
（半透明灰边印出来就是脏边），所以**不依赖 CLI 的行为**：

1. 把 RGBA 贴纸**合成到白底** → 拆出纯 RGB
2. RGB 走 Upscayl 超分
3. alpha 通道**单独用 LANCZOS** 放大到同尺寸，再按 **128 阈值二值化**
   —— 二值化是关键：AI 超分会在边缘造出渐变的半透明像素，
      印在玻璃上就是一圈灰边；二值化让它回到干净的黑白边界
4. `putalpha` 合成回 RGBA

这样无论 CLI 对 alpha 怎么处理，输出都是可控的。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from .engine import UpscaylEngine

log = logging.getLogger(__name__)

# alpha 二值化阈值：≥ 该值算不透明，否则全透明
ALPHA_THRESHOLD = 128

# 白底合成时的底色（贴纸底色是白，不是黑 —— 黑会让 AI 超分把边缘压暗）
MATTE = (255, 255, 255)


def _flatten_on_white(rgba: Image.Image) -> Image.Image:
    """RGBA → 白底 RGB。"""
    if rgba.mode != "RGBA":
        rgba = rgba.convert("RGBA")
    bg = Image.new("RGB", rgba.size, MATTE)
    bg.paste(rgba, mask=rgba.getchannel("A"))
    return bg


def binarize_alpha(alpha: Image.Image, threshold: int = ALPHA_THRESHOLD) -> Image.Image:
    """把 alpha 二值化成纯 0 / 255。

    ⚠️ 这一步不是可选的：超分后的 alpha 边缘必然带渐变，
       直接用于印刷会出现半透明灰边（玻璃贴纸是硬边工艺）。
    """
    return alpha.point(lambda v: 255 if v >= threshold else 0, mode="L")


def upscale_rgba(
    engine: "UpscaylEngine",
    src: Path,
    dst: Path,
    model: str,
    scale: int,
    tile: int,
) -> Path | None:
    """超分一张 RGBA 图，返回输出路径；失败返回 ``None``。

    失败时调用方应回落 LANCZOS —— 这里不抛错。
    """
    src, dst = Path(src), Path(dst)
    try:
        with Image.open(src) as im:
            rgba = im.convert("RGBA")
    except OSError as exc:
        log.warning("读取待超分图失败：%s: %s", type(exc).__name__, exc)
        return None

    target = (rgba.width * scale, rgba.height * scale)

    # ① 拆出纯 RGB（白底合成，避免透明区在超分时变成黑块）
    flat = _flatten_on_white(rgba)
    tmp_rgb = dst.with_name(dst.stem + "__rgb.png")
    tmp_alpha = dst.with_name(dst.stem + "__alpha.png")
    try:
        flat.save(tmp_rgb)
    except OSError as exc:
        log.warning("写出中间图失败：%s", type(exc).__name__)
        return None

    # ② RGB 走 Upscayl
    got = engine.upscale_image(tmp_rgb, dst, model, scale, tile)
    if got is None:
        tmp_rgb.unlink(missing_ok=True)
        return None

    # ③ alpha 单独 LANCZOS 放大 + 二值化
    try:
        big_alpha = binarize_alpha(
            rgba.getchannel("A").resize(target, Image.LANCZOS)
        )
    except OSError as exc:
        log.warning("alpha 放大失败：%s", type(exc).__name__)
        tmp_rgb.unlink(missing_ok=True)
        return None

    # ④ 合成回 RGBA
    try:
        with Image.open(dst) as out:
            rgb = out.convert("RGB")
        if rgb.size != target:
            rgb = rgb.resize(target, Image.LANCZOS)
        merged = rgb.convert("RGBA")
        merged.putalpha(big_alpha)
        merged.save(dst)
    except OSError as exc:
        log.warning("合成 RGBA 失败：%s", type(exc).__name__)
        return None
    finally:
        tmp_rgb.unlink(missing_ok=True)
        tmp_alpha.unlink(missing_ok=True)

    return dst


def upscale_rgba_to_target(
    engine: "UpscaylEngine",
    src: Path,
    dst: Path,
    target_px: int,
    model: str,
    tile: int,
) -> Path | None:
    """RGBA 版「放大到至少 target_px」，语义同 `engine.upscale_to_target`。"""
    src = Path(src)
    try:
        with Image.open(src) as im:
            cur = max(im.size)
    except OSError:
        return None
    if cur <= 0:
        return None

    from .models import ALLOWED_SCALES, get_model

    info = get_model(model)
    scales = info.scales if info else ALLOWED_SCALES
    need = target_px / cur
    pick = next((s for s in sorted(scales) if s >= need), max(scales))
    return upscale_rgba(engine, src, dst, model, pick, tile)
