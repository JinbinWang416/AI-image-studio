# -*- coding: utf-8 -*-
"""印刷导出：合并预览 TIF 与 JPEG 缩略图。

## 两种预览的用途不同

| 输出 | 给谁看 | 用途 |
|------|--------|------|
| `<门店>_合并预览.tif` | 印刷厂 / 设计师 | 三层套准核对：彩色层位置对不对、白墨有没有漏、刀模线是否包住图案 |
| `预览/<门店>_预览.jpg` | 前端页面 | 浏览器打不开 TIF，所以必须另出一张 JPEG 给界面用 |
"""
from __future__ import annotations

import logging

from PIL import Image, ImageDraw

__all__ = [
    "make_merged_preview",
    "make_jpeg_preview",
    "checkerboard",
    "PREVIEW_MAX_EDGE",
    "PREVIEW_QUALITY",
]

log = logging.getLogger("app.print_export.preview")

# 前端预览图：最长边与质量（需求规定）
PREVIEW_MAX_EDGE = 1200
PREVIEW_QUALITY = 85

# 棋盘格格子大小（像素，在缩略图尺度上）
_CHECKER = 24


def checkerboard(size: tuple[int, int], light: int = 255, dark: int = 232
                 ) -> Image.Image:
    """生成浅色棋盘格背景。

    透明区域在纯白背景上看不出来，用棋盘格才能明确区分
    "这里是透明的" 与 "这里是白色的"。
    """
    w, h = size
    board = Image.new("RGB", size, (light, light, light))
    draw = ImageDraw.Draw(board)
    for y in range(0, h, _CHECKER):
        for x in range(0, w, _CHECKER):
            if (x // _CHECKER + y // _CHECKER) % 2:
                draw.rectangle([x, y, x + _CHECKER - 1, y + _CHECKER - 1],
                               fill=(dark, dark, dark))
    return board


def make_merged_preview(cmyk: Image.Image | None,
                        white_ink: Image.Image | None = None,
                        dieline: Image.Image | None = None,
                        *, size: tuple[int, int] | None = None,
                        with_checker: bool = True) -> Image.Image:
    """把三层合到一张图，供人工核对。

    合成顺序：棋盘格底 → 彩色层（按 Alpha 合成）→ 白墨层轮廓（浅灰提示）→ 刀模线（红）

    Args:
        cmyk: 彩色层（CMYK 或 RGB 均可）
        white_ink: 白墨层（L 模式；非透明区代表要印白墨）
        dieline: 刀模线层（RGB，红线）
        size: 输出尺寸；None 则取 cmyk 的尺寸
        with_checker: 是否用棋盘格（False 则纯白底）
    """
    if size is None:
        if cmyk is not None:
            size = cmyk.size
        elif dieline is not None:
            size = dieline.size
        else:
            size = (1024, 1024)

    base = checkerboard(size) if with_checker else Image.new("RGB", size, (255, 255, 255))

    # ① 彩色层：CMYK 无法直接合成，先转 RGB
    if cmyk is not None:
        color = cmyk.convert("RGB") if cmyk.mode == "CMYK" else cmyk.convert("RGB")
        if color.size != size:
            color = color.resize(size, Image.LANCZOS)

        # 用白墨层当作"图案范围"的蒙版来抠出图案（没有任何 Alpha 时整张铺上）
        if white_ink is not None and white_ink.size == size:
            mask = white_ink.convert("L")
            base.paste(color, (0, 0), mask)
        else:
            base.paste(color, (0, 0))

    # ② 白墨层：用绿色描出它的**边界**，方便核对白墨范围
    #    （直接把整个白墨区铺绿会盖住图案，只描边界更实用）
    if white_ink is not None:
        try:
            import numpy as np
            from PIL import ImageFilter

            wi = white_ink.convert("L")
            if wi.size != size:
                wi = wi.resize(size, Image.LANCZOS)
            grown = wi.filter(ImageFilter.MaxFilter(size=3))
            edge = (np.asarray(grown).astype("int16")
                    - np.asarray(wi).astype("int16")) > 40
            if edge.any():
                mask = Image.fromarray((edge * 255).astype("uint8"), mode="L")
                green = Image.new("RGB", size, (30, 150, 60))
                base.paste(green, (0, 0), mask)
        except Exception as exc:  # noqa: BLE001 - 叠加失败不影响主流程
            log.debug("白墨边界叠加失败（忽略）：%s", exc)

    # ③ 刀模线：只在有红线的地方覆盖（避免把整张盖住）
    if dieline is not None:
        try:
            import numpy as np

            dl = dieline.convert("RGB")
            if dl.size != size:
                dl = dl.resize(size, Image.LANCZOS)
            arr = np.asarray(dl).astype("int16")
            # 红色像素：R 明显高于 G/B
            is_red = (arr[:, :, 0] > 150) & (arr[:, :, 1] < 110) & (arr[:, :, 2] < 110)
            if is_red.any():
                mask = Image.fromarray((is_red * 255).astype("uint8"), mode="L")
                base.paste(dl, (0, 0), mask)
        except Exception as exc:  # noqa: BLE001
            log.debug("刀模线叠加失败（忽略）：%s", exc)

    return base


def make_jpeg_preview(img: Image.Image, *, max_edge: int = PREVIEW_MAX_EDGE,
                      quality: int = PREVIEW_QUALITY,
                      background: tuple[int, int, int] = (255, 255, 255)
                      ) -> Image.Image:
    """从任意图生成前端用的 JPEG 缩略图。

    JPEG 不支持透明通道，所以透明区域会被**铺成白底**
    （印刷预览里这样最不容易误判）。

    Args:
        max_edge: 最长边上限（等比缩放）
        quality: JPEG 质量（保存时使用）
    """
    # CMYK 先转 RGB 才能存 JPEG
    src = img.convert("RGB") if img.mode in ("CMYK", "L", "P") else img.convert("RGBA")

    if src.mode == "RGBA":
        flat = Image.new("RGB", src.size, background)
        flat.paste(src, (0, 0), src.getchannel("A"))
        src = flat
    else:
        src = src.convert("RGB")

    w, h = src.size
    longest = max(w, h)
    if longest > max_edge:
        scale = max_edge / longest
        src = src.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                         Image.LANCZOS)

    return src
