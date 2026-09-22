# -*- coding: utf-8 -*-
"""印刷导出：刀模线层（模切轮廓）。

## 刀模线是什么

模切机沿着刀模线把贴纸从整张材料上**切下来**。所以这一层：

- **只画轮廓**，不填色（填色会被 RIP 当成要印刷的色块）
- 线色用 **100% 红**（行业惯例，与任何印刷色都不冲突，RIP 会识别为"非印刷标记"）
- 轮廓要**闭合**，否则模切机会走出错误路径

## 出血（bleed）

裁切有机械误差（通常 ±0.5~1mm）。如果图案正好画到轮廓线上，
切偏一点就会露出白边。所以把**图案向外扩 3mm**，
多出来的部分会被切掉，保证成品边缘饱满。

这里用「Alpha 膨胀」实现出血：先把 Alpha 向外扩 `bleed_px`，
再对这个扩张后的形状取轮廓 —— 这样刀模线比原图案大一圈，
图案本身也随之扩大（在 `print_export.py` 里同步放大）。
"""
from __future__ import annotations

import logging

from PIL import Image, ImageDraw

__all__ = [
    "OutlineResult",
    "find_outline",
    "draw_dieline",
    "is_closed",
    "bleed_pixels",
    "DEFAULT_LINE_PX",
]

log = logging.getLogger("app.print_export.dieline")

# 刀模线宽（像素 @300DPI 约等于 0.5pt）
DEFAULT_LINE_PX = 3
# 轮廓闭合判定的容差（像素）
CLOSE_TOL = 2.0


class OutlineResult:
    """轮廓查找结果。"""

    def __init__(self, contour=None, closed: bool = False, area: float = 0.0,
                 points: int = 0, error: str = "") -> None:
        self.contour = contour          # numpy 数组 (N,1,2) 或 None
        self.closed = closed
        self.area = area
        self.points = points
        self.error = error

    def __bool__(self) -> bool:
        return self.contour is not None


def bleed_pixels(bleed_mm: float, dpi: int) -> int:
    """把出血毫米数换算成像素。"""
    return max(1, int(round(bleed_mm / 25.4 * dpi)))


def _alpha_array(img: Image.Image):
    """取 Alpha 通道为 numpy 数组（uint8）。"""
    import numpy as np

    if img.mode == "L":
        return np.asarray(img)
    return np.asarray(img.convert("RGBA").getchannel("A"))


def find_outline(alpha_img: Image.Image, *, bleed_px: int = 0
                 ) -> OutlineResult:
    """从 Alpha 找**最大外轮廓**。

    Args:
        alpha_img: 带 Alpha 的图（或 L 模式的 mask）
        bleed_px: 先向外膨胀这么多像素再取轮廓（出血）

    Returns:
        ``OutlineResult``；失败时 ``contour is None`` 且带 ``error``
    """
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        return OutlineResult(error=f"缺少 opencv/numpy：{exc}")

    alpha = _alpha_array(alpha_img)

    # 二值化 + 轻微膨胀确保边缘连续（去背后常有 1px 断点）
    _, binary = cv2.threshold(alpha, 128, 255, cv2.THRESH_BINARY)
    kernel = np.ones((3, 3), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)

    # 出血：向外膨胀
    if bleed_px > 0:
        k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (bleed_px * 2 + 1, bleed_px * 2 + 1)
        )
        binary = cv2.dilate(binary, k, iterations=1)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return OutlineResult(error="未找到任何轮廓（图可能是全透明）")

    # 取面积最大的轮廓（贴纸主体）
    main = max(contours, key=cv2.contourArea)
    area = float(cv2.contourArea(main))
    if area < 16:
        return OutlineResult(error=f"主轮廓面积过小（{area:.0f}px²），可能是空图或全透明")

    # 适度简化：减少点数，模切机处理更快，同时保持形状
    peri = cv2.arcLength(main, True)
    approx = cv2.approxPolyDP(main, max(0.5, peri * 0.0008), True)

    closed = is_closed(approx)
    return OutlineResult(contour=approx, closed=closed, area=area, points=len(approx))


def is_closed(contour, tol: float = CLOSE_TOL) -> bool:
    """判断轮廓是否闭合。

    ⚠️ **不能用"首尾点距离 ≤ 固定像素"或"≤ 周长比例"判定**（实测都误报）：

        `cv2.approxPolyDP(..., closed=True)` 返回的是**多边形顶点序列** ——
        首尾点是两个相邻顶点，它们之间的间距就是**最后一条边的长度**，
        正常情况下等于其它边长，而不是接近 0。

        用「周长 5%」判定时，一个 20 边形每条边占周长 5%，
        首尾间距正好卡在阈值上，稍一波动就误报"未闭合"。

    **正确判据**：`approxPolyDP(closed=True)` 的输出在语义上**就是闭合多边形**
    （边由相邻顶点连成，最后一条边隐式回到起点）。所以只要：

        · 点数 ≥ 3（能构成多边形）
        · 首尾间距不超过**最长边**的 1.5 倍（排除退化形状）

    即可认为闭合。绘制时还会显式把首点再接一次，双保险。
    """
    if contour is None or len(contour) < 3:
        return False

    import math

    pts = [p[0] for p in contour]
    first, last = pts[0], pts[-1]
    gap = math.hypot(float(first[0]) - float(last[0]),
                     float(first[1]) - float(last[1]))

    # 最长边（相邻顶点间距）
    longest = 0.0
    for i in range(len(pts) - 1):
        d = math.hypot(float(pts[i + 1][0]) - float(pts[i][0]),
                       float(pts[i + 1][1]) - float(pts[i][1]))
        longest = max(longest, d)

    if longest <= 0:
        return True
    return gap <= max(tol, longest * 1.5)


def draw_dieline(size: tuple[int, int], outline: OutlineResult,
                 *, line_px: int = DEFAULT_LINE_PX,
                 color: tuple[int, int, int] = (255, 0, 0)) -> Image.Image:
    """画刀模线层：白底 + 红色描边，**不填色**。

    Args:
        size: 画布尺寸 (宽, 高)
        outline: `find_outline()` 的结果
        line_px: 线宽（像素）
        color: 线色，默认纯红

    Returns:
        ``RGB`` 模式的刀模线图
    """
    canvas = Image.new("RGB", size, (255, 255, 255))
    if not outline:
        return canvas

    draw = ImageDraw.Draw(canvas)
    pts = [(int(p[0][0]), int(p[0][1])) for p in outline.contour]
    if len(pts) < 3:
        return canvas

    # 闭合：显式把首点再接一次，避免 draw.line 留下缺口
    draw.line(pts + [pts[0]], fill=color, width=line_px, joint="curve")
    return canvas
