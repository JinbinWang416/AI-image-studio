# -*- coding: utf-8 -*-
"""
图片后处理。

## 为什么需要它

连续 5 版提示词实测证明：**模型对「白色图形 + 彩色背景」有强烈的固有偏好**，
靠提示词（无论正向还是负向）都无法稳定压住：

| 版本 | 提示词做法 | 实测背景 |
|------|-----------|---------|
| V2 | 「干净白底」 | 蓝灰底 |
| V3 | 「不要任何背景环境」（正向否定句） | **玻璃窗照片**（反而更糟） |
| V4 | 「纯白画布」 | 彩色纯色（深青绿/蓝灰/浅蓝） |
| V5 | 「整个画布为纯白色」+ 负向加「色块背景」 | 深灰 |

**结论：这是模型的构图先验，不是提示词能解决的。**

## 解法

生成后做**确定性后处理**：从画面四角向内 flood fill，把外部连通背景统一刷成纯白。
贴纸轮廓内部的白色与图形线条不受影响，结果 100% 可控。
"""
from __future__ import annotations

from io import BytesIO

try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except ImportError:  # pragma: no cover
    HAS_PIL = False


def whiten_background(data: bytes, thresh: int = 45, fill: tuple = (255, 255, 255)) -> bytes:
    """把图片外部背景刷成纯白。

    Args:
        data:   原始图片字节
        thresh: 颜色容忍度（与角落像素差异在此范围内的会被一并填充）
        fill:   填充色，默认纯白

    Returns:
        处理后的 PNG 字节；若 Pillow 不可用或处理失败则原样返回。
    """
    if not HAS_PIL:
        return data
    try:
        im = Image.open(BytesIO(data)).convert("RGB")
    except Exception:
        return data

    w, h = im.size
    if w < 4 or h < 4:
        return data

    # 从四个角落分别 flood fill，覆盖背景被图形分割的情况
    corners = ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))
    for xy in corners:
        try:
            ImageDraw.floodfill(im, xy, fill, thresh=thresh)
        except Exception:
            continue

    # 再扫一遍边缘：把仍非纯白的边缘像素强制刷白（处理细碎残留）
    try:
        px = im.load()
        for x in range(w):
            for y in (0, h - 1):
                if px[x, y] != fill:
                    px[x, y] = fill
        for y in range(h):
            for x in (0, w - 1):
                if px[x, y] != fill:
                    px[x, y] = fill
    except Exception:
        pass

    buf = BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def background_whiteness(data: bytes) -> float:
    """统计画面四边像素中纯白（>=250）的占比，用于质检。"""
    if not HAS_PIL:
        return -1.0
    try:
        im = Image.open(BytesIO(data)).convert("RGB")
    except Exception:
        return -1.0
    w, h = im.size
    px = im.load()
    total = white = 0
    step = max(1, w // 200)
    for x in range(0, w, step):
        for y in (0, 1, h - 2, h - 1):
            r, g, b = px[x, y]
            total += 1
            if r >= 250 and g >= 250 and b >= 250:
                white += 1
    for y in range(0, h, step):
        for x in (0, 1, w - 2, w - 1):
            r, g, b = px[x, y]
            total += 1
            if r >= 250 and g >= 250 and b >= 250:
                white += 1
    return round(white / total, 4) if total else -1.0
