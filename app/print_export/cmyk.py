# -*- coding: utf-8 -*-
"""印刷导出：sRGB → CMYK 转换与偏色保护。

## 为什么必须走 ICC（而不是 `img.convert("CMYK")`）

Pillow 的朴素 `convert("CMYK")` 用的是**无色彩管理的数学换算**：

    C = 255 - R,  M = 255 - G,  Y = 255 - B,  K = 0

这在印刷上几乎总是错的 —— 黑色会变成"三色叠印的深褐"，
亮蓝和大红会明显发灰。**必须用 ICC profile 做真实分色**。

## ICC 查找顺序

1. `settings.print.icc_path`（用户显式指定）
2. `config/icc/*.icc` 或 `*.icm`（放进去就自动生效）
3. 系统默认：Windows 的 `RSWOP.icm`（轮转胶印标准，随系统自带）
4. **都没有 → 朴素转换 + 记警告**（不阻断，但 manifest 里标明）

## 偏色保护

即使走 ICC，某些高饱和色在标准 CMYK 色域外（out-of-gamut），
分色后仍会明显掉色。这里对**白底 / 亮蓝 / 大红**做定向校正：
找出原图中接近这些颜色的像素，把转换结果**拉回标准值**。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageCms

__all__ = [
    "find_icc_profile",
    "to_cmyk",
    "protect_colors",
    "PROTECT_COLORS",
    "SYSTEM_ICC_DIRS",
]

log = logging.getLogger("app.print_export.cmyk")

# 系统 ICC 目录（按平台）
SYSTEM_ICC_DIRS = [
    Path(r"C:\Windows\System32\spool\drivers\color"),
    Path("/usr/share/color/icc"),
    Path("/Library/ColorSync/Profiles"),
]

# 系统默认 CMYK profile 的候选名（按优先级）
SYSTEM_CMYK_NAMES = ["RSWOP.icm", "USWebCoatedSWOP.icc", "ISOcoated_v2_eci.icc",
                     "CoatedFOGRA39.icc", "default_cmyk.icc"]

# 偏色保护表：原图颜色 → 期望的 CMYK 值
#   说明：CMYK 在 Pillow 里是 0~255 表示 0~100%
PROTECT_COLORS: dict[str, dict[str, Any]] = {
    "white": {
        "rgb": (255, 255, 255),
        "cmyk": (0, 0, 0, 0),          # 白底必须纯 0，否则印刷整体发灰
        "tolerance": 18,
        "label": "白底",
    },
    "red": {
        "rgb": (230, 30, 40),
        "cmyk": (0, 255, 240, 10),     # 大红：高 M + 高 Y，避免发暗
        "tolerance": 60,
        "label": "大红",
    },
    "blue": {
        "rgb": (0, 90, 175),
        "cmyk": (255, 150, 0, 10),     # 亮蓝：高 C + 中 M
        "tolerance": 60,
        "label": "亮蓝",
    },
}


def find_icc_profile(explicit: str = "", project_root: Path | None = None
                     ) -> tuple[Path | None, str]:
    """按优先级查找 CMYK ICC profile。

    Returns:
        ``(路径或 None, 来源说明)`` —— 来源用于写进 manifest
    """
    # ① 显式指定
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p, "settings"
        log.warning("settings 指定的 ICC 不存在：%s", explicit)

    # ② 项目 config/icc/
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent.parent
    icc_dir = project_root / "config" / "icc"
    if icc_dir.is_dir():
        for pat in ("*.icc", "*.icm", "*.ICC", "*.ICM"):
            found = sorted(icc_dir.glob(pat))
            if found:
                return found[0], "config/icc"

    # ③ 系统
    for d in SYSTEM_ICC_DIRS:
        if not d.is_dir():
            continue
        for name in SYSTEM_CMYK_NAMES:
            p = d / name
            if p.is_file():
                return p, "system"

    return None, "none"


def to_cmyk(img: Image.Image, icc_path: Path | None,
            *, intent: int = ImageCms.Intent.PERCEPTUAL
            ) -> tuple[Image.Image, dict]:
    """sRGB → CMYK。

    Args:
        img: RGB 图（带不带 Alpha 都行，Alpha 会被忽略）
        icc_path: CMYK profile 路径；None 表示无 profile

    Returns:
        ``(CMYK 图, icc 信息字典)`` —— 字典直接进 manifest
    """
    rgb = img.convert("RGB")

    if icc_path is None or not Path(icc_path).is_file():
        log.warning("无可用 CMYK ICC，降级为朴素转换（颜色会有偏差）")
        return rgb.convert("CMYK"), {
            "name": "", "path": "", "fallback": True,
            "note": "未找到 CMYK ICC，使用朴素转换。强烈建议在 config/icc/ 放入印刷厂提供的 profile。",
        }

    try:
        src_profile = ImageCms.createProfile("sRGB")
        dst_profile = ImageCms.getOpenProfile(str(icc_path))
        transform = ImageCms.buildTransform(
            src_profile, dst_profile, "RGB", "CMYK",
            renderingIntent=intent,
        )
        out = ImageCms.applyTransform(rgb, transform)
        return out, {
            "name": Path(icc_path).name,
            "path": str(icc_path),
            "fallback": False,
            "intent": "perceptual",
        }
    except Exception as exc:  # noqa: BLE001 - profile 损坏/不兼容
        log.warning("ICC 转换失败（%s），降级为朴素转换", exc)
        return rgb.convert("CMYK"), {
            "name": Path(icc_path).name if icc_path else "",
            "path": str(icc_path) if icc_path else "",
            "fallback": True,
            "note": f"ICC 转换失败：{type(exc).__name__}。已降级为朴素转换。",
        }


def protect_colors(cmyk: Image.Image, original_rgb: Image.Image
                   ) -> tuple[Image.Image, list[str]]:
    """对白底 / 亮蓝 / 大红做定向校正。

    做法：在**原 RGB** 上找出接近保护色的像素，把这些位置在 CMYK 结果里
    强制写成标准值。这样既保留 ICC 对其它颜色的正确处理，
    又避免关键色掉色。

    Returns:
        ``(校正后的 CMYK 图, 触发的校正说明列表)``
    """
    try:
        import numpy as np
    except ImportError:
        return cmyk, []

    # ⚠️ 必须用 int32/float32：int16 下 (255-0)² × 3 = 195075 会溢出，
    #    导致 sqrt 收到负数 → 触发 RuntimeWarning 且色距算错（偏色保护失效）。
    rgb = np.asarray(original_rgb.convert("RGB")).astype(np.int32)
    arr = np.asarray(cmyk).astype(np.uint8).copy()
    notes: list[str] = []

    for key, spec in PROTECT_COLORS.items():
        target = np.array(spec["rgb"], dtype=np.int32)
        dist = np.sqrt(((rgb - target) ** 2).sum(axis=2).astype(np.float64))
        mask = dist <= spec["tolerance"]
        count = int(mask.sum())
        if count == 0:
            continue
        cmyk_vals = np.array(spec["cmyk"], dtype=np.uint8)
        arr[mask] = cmyk_vals
        pct = count / max(1, mask.size) * 100
        notes.append(f"{spec['label']}：校正 {count} 像素（{pct:.2f}%）→ CMYK{tuple(int(v) for v in cmyk_vals)}")

    if not notes:
        return cmyk, []
    return Image.fromarray(arr, mode="CMYK"), notes
