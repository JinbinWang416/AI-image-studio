# -*- coding: utf-8 -*-
"""印刷导出：图层处理（去背 / Alpha 收边 / 白墨层）。

## 去背的两条路

| 路径 | 触发 | 质量 | 依赖 |
|------|------|------|------|
| **rembg（u2net）** | 装了 `rembg[cpu]` 且 `onnxruntime` 可用 | 好，能处理复杂背景 | 约 226 MB（含模型） |
| **纯 Pillow/numpy 降级** | 无 rembg 或调用失败 | 一般，适合**背景纯净**的 AI 图 | 零额外依赖 |

AI 生成的设计图通常是**纯色背景**（提示词里锁定了"纯白背景"），
降级路径取四角颜色做背景基准 + 色距容差 + 连通域清理，能覆盖大部分情况。

## 白墨层极性（Q2 = A，印刷惯例）

```
白墨层 = "哪里要印白墨" 的版

  不透明区（图案本身）→ 255（印白墨）
  透明区（图案之外）  → 0  （不印）
```

⚠️ 这一项做反会导致**整批印刷报废**。若工艺需要"满版白墨 + 图案镂空"，
把 `invert=True` 传进来即可（对应 settings 的 `print_white_ink_invert`）。
"""
from __future__ import annotations

import logging
from typing import Any

from PIL import Image, ImageFilter

__all__ = [
    "CutoutSession",
    "cutout",
    "fallback_cutout",
    "tighten_alpha",
    "clean_alpha",
    "make_white_ink",
    "alpha_coverage",
]

log = logging.getLogger("app.print_export.layers")

# 降级去背的默认容差（欧氏色距，0~441）
DEFAULT_TOLERANCE = 42.0
# 判定 Alpha "实心" 的阈值
ALPHA_SOLID = 128


# ================================================================ rembg 会话
class CutoutSession:
    """复用 rembg 推理会话。

    ⚠️ **批量时不要每张重建会话** —— 首次加载 u2net 模型约 3~5 秒，
    复用后单张只需 1~2 秒。这是"一张 2000px 图整条流水线 ≤10 秒"的关键。
    """

    def __init__(self, model: str = "u2net") -> None:
        self.model = model
        self._session: Any = None
        self._tried = False
        self.last_error = ""

    @property
    def available(self) -> bool:
        """rembg 是否真的可用（装了 + 有 onnxruntime 后端）。"""
        if not self._tried:
            self._init()
        return self._session is not None

    def _init(self) -> None:
        self._tried = True
        try:
            # rembg 在缺后端时会往 stdout 直接 print 一大段安装提示，
            # 污染服务日志。这里临时重定向掉（异常信息仍能从 last_error 取到）。
            import contextlib
            import io

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                from rembg import new_session  # type: ignore

                self._session = new_session(self.model)
            self._rembg_noise = buf.getvalue().strip()
            log.info("rembg 会话已就绪（model=%s）", self.model)
        except (Exception, SystemExit) as exc:
            # ⚠️ 必须同时捕获 SystemExit：
            #    rembg 在缺少 onnxruntime 后端时**不抛异常**，而是
            #    print 一段提示后直接 `sys.exit()` —— 而 SystemExit 继承自
            #    BaseException，`except Exception` 抓不到，会导致**整个服务进程退出**。
            #    实测踩到过：一次导出尝试直接把 Web 服务干掉了。
            self._session = None
            detail = str(exc)
            self.last_error = (
                f"{type(exc).__name__}: {detail}" if detail else "onnxruntime 后端缺失"
            )
            log.info("rembg 不可用，将使用纯色去背降级路径：%s", self.last_error)

    def cutout(self, img: Image.Image) -> Image.Image | None:
        """用 rembg 去背；失败返回 None（由调用方走降级）。"""
        if not self.available:
            return None
        try:
            from rembg import remove  # type: ignore

            out = remove(img, session=self._session)
            return out if isinstance(out, Image.Image) else Image.open(out)
        except (Exception, SystemExit) as exc:
            # 同上：rembg 内部也可能走 sys.exit
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("rembg 去背失败，改用降级路径：%s", self.last_error)
            return None


# ================================================================ 降级去背
def fallback_cutout(img: Image.Image, tolerance: float = DEFAULT_TOLERANCE) -> Image.Image:
    """纯 Pillow/numpy 去背：四角取背景色 → 色距容差 → 连通域清理。

    适用于**背景纯净**的 AI 设计图。不需要任何额外依赖（numpy 已是 Pillow 生态常见配套）。
    """
    src = img.convert("RGBA")
    try:
        import numpy as np
    except ImportError:  # numpy 理论上一定在（opencv 依赖），但保持优雅
        log.warning("numpy 不可用，跳过降级去背（保留原图 Alpha）")
        return src

    # ⚠️ int32：int16 下 (255-0)² × 3 = 195075 会溢出，色距算错
    rgb = np.asarray(src.convert("RGB")).astype(np.int32)
    h, w = rgb.shape[:2]
    if h < 8 or w < 8:
        return src

    # ① 背景基准色：取四角 6×6 区域的中位数（比均值抗噪）
    corners = np.concatenate([
        rgb[0:6, 0:6].reshape(-1, 3),
        rgb[0:6, -6:].reshape(-1, 3),
        rgb[-6:, 0:6].reshape(-1, 3),
        rgb[-6:, -6:].reshape(-1, 3),
    ])
    bg = np.median(corners, axis=0)

    # ② 色距 → 前景掩膜
    dist = np.sqrt(((rgb - bg) ** 2).sum(axis=2).astype(np.float64))
    fg = dist > tolerance

    # ③ 若前景占比过高（>92%），说明四角采样失败（背景不纯），直接保留原图
    if fg.mean() > 0.92:
        log.info("背景不纯净（前景占比 %.1f%%），保留原图 Alpha", fg.mean() * 100)
        return src

    # ④ 去噪：中值式 3×3 多数表决（用 PIL 的 MinFilter/MaxFilter 组合近似）
    mask = Image.fromarray((fg * 255).astype("uint8"), mode="L")
    mask = mask.filter(ImageFilter.MedianFilter(size=3))
    mask = mask.filter(ImageFilter.MaxFilter(size=3))    # 补掉前景里的小洞
    mask = mask.filter(ImageFilter.MinFilter(size=3))    # 削掉背景里的小岛

    out = src.copy()
    out.putalpha(mask)
    return out


def cutout(img: Image.Image, session: CutoutSession | None = None,
           tolerance: float = DEFAULT_TOLERANCE) -> tuple[Image.Image, str]:
    """统一去背入口。

    Returns:
        ``(RGBA 图, 实际使用的方法)`` —— 方法为 ``"rembg"`` 或 ``"fallback"``
    """
    if session is not None:
        try:
            got = session.cutout(img)
        except SystemExit:      # 见 CutoutSession._init 的说明
            got = None
        if got is not None:
            return got.convert("RGBA"), "rembg"
    return fallback_cutout(img, tolerance), "fallback"


# ================================================================ Alpha 处理
def tighten_alpha(img: Image.Image, px: int = 1) -> Image.Image:
    """Alpha 向内收边 `px` 像素。

    **为什么必须做**：AI 图的边缘常带 1~2 像素的半透明杂边（灰边/光晕）。
    印刷时这些半透明像素会被 RIP 解释成"浅色油墨"，在成品上表现为
    **一圈脏边**。向内收 1px 可干净地去掉它。
    """
    if px <= 0:
        return img
    out = img.convert("RGBA")
    alpha = out.getchannel("A")
    # MinFilter 取邻域最小值 = 向内腐蚀
    alpha = alpha.filter(ImageFilter.MinFilter(size=px * 2 + 1))
    out.putalpha(alpha)
    return out


def clean_alpha(img: Image.Image, *, solidify: int = 8) -> Image.Image:
    """清理 Alpha：把"几乎全透明"的残留像素彻底清掉。

    去背后常留下 alpha=1~7 的幽灵像素，肉眼看不见，但印刷会让 RIP
    生成不该有的微小网点。
    """
    out = img.convert("RGBA")
    alpha = out.getchannel("A")
    # 低于 solidify 的直接归零，其余保持
    alpha = alpha.point(lambda a: 0 if a < solidify else a)
    out.putalpha(alpha)
    return out


def alpha_coverage(img: Image.Image) -> float:
    """不透明区域占比（0~1），用于判断去背是否合理。"""
    alpha = img.convert("RGBA").getchannel("A")
    hist = alpha.histogram()
    total = sum(hist) or 1
    solid = sum(hist[ALPHA_SOLID:])
    return solid / total


# ================================================================ 白墨层
def make_white_ink(alpha_img: Image.Image, *, invert: bool = False,
                   threshold: int = ALPHA_SOLID,
                   smooth: bool = True) -> Image.Image:
    """由 Alpha 生成白墨层（单通道灰度 L）。

    Args:
        alpha_img: 带 Alpha 的 RGBA 图（或直接是 L 模式的 alpha）
        invert: 极性。False = **不透明区印白墨**（印刷惯例，Q2 决策 A）；
                True = 透明区全白（满版白墨 + 图案镂空工艺）
        threshold: 判定为"实心"的 Alpha 阈值
        smooth: 是否做 1px 抗锯齿，避免印刷出现硬锯齿边

    Returns:
        ``L`` 模式灰度图：**255 = 印白墨，0 = 不印**

    ⚠️ 极性做反 = 整批报废。调用方应从 settings 传入 ``print_white_ink_invert``。
    """
    if alpha_img.mode == "L":
        alpha = alpha_img
    else:
        alpha = alpha_img.convert("RGBA").getchannel("A")

    # 二值化：实心 → 255
    mask = alpha.point(lambda a: 255 if a >= threshold else 0)

    if smooth:
        # 轻微模糊再二值化，得到 1px 柔和过渡（印刷厂 RIP 会正确处理）
        mask = mask.filter(ImageFilter.GaussianBlur(0.6))
        mask = mask.point(lambda a: 255 if a >= 128 else 0)

    if invert:
        mask = mask.point(lambda a: 255 - a)

    return mask
