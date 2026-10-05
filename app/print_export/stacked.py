# -*- coding: utf-8 -*-
"""把 CMYK 与白墨合成**单个 5 通道 TIF** —— 对齐海邦达 H-2003E 的可用样例。

## 为什么要这样

拿现场「能直接打印」的样例（`学生托管门店_01.tif`）反向分析，它的结构是：

    SamplesPerPixel          = 5
    BitsPerSample            = (8, 8, 8, 8, 8)
    PhotometricInterpretation= 5 (separated / CMYK)
    ExtraSamples             = (0,)          ← 1 个额外通道 = 白墨专色
    Compression              = 5 (LZW)
    Predictor                = 2
    Resolution               = 120 × 120

第 5 通道是**双峰蒙版**（42.3% 为 0、56.7% 为 255、中间值仅 0.9%），
即 `255 = 印白墨`（图案区）、`0 = 不印`（透明背景）。

**Pillow 写不了 5 通道**（`Image.mode` 里没有 CMYKA 这种组合），
所以这里用 `tifffile` 直接写数组。

⚠️ 依赖 `imagecodecs`（tifffile 解/压 LZW 需要它）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# 与样例一致：LZW + 水平差分预测（能显著减小印刷图的体积）
COMPRESSION = "lzw"
PREDICTOR = 2


def save_stacked_cmyk_white(
    path: Path,
    cmyk: Image.Image,
    white: Image.Image | None,
    dpi: int = 120,
) -> Path:
    """写出 5 通道 TIF（CMYK + 白墨专色）。

    Args:
        path: 输出路径
        cmyk: CMYK 图像（4 通道）
        white: 白墨单通道图；``None`` 时补一张全 255（整版印白）
        dpi: 分辨率，写进 TIFF 标签

    Returns:
        输出路径

    Raises:
        RuntimeError: 数组形状不合法（调用方应捕获并降级）
    """
    import numpy as np
    import tifffile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if cmyk.mode != "CMYK":
        cmyk = cmyk.convert("CMYK")
    arr = np.asarray(cmyk, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] != 4:
        raise RuntimeError(f"CMYK 数组形状异常：{arr.shape}，期望 (H, W, 4)")

    if white is None:
        w = np.full(arr.shape[:2], 255, dtype=np.uint8)
    else:
        if white.mode != "L":
            white = white.convert("L")
        w = np.asarray(white, dtype=np.uint8)
        if w.shape != arr.shape[:2]:
            # 尺寸不一致会让 RIP 直接拒收，宁可在这里显式失败
            raise RuntimeError(
                f"白墨层尺寸 {w.shape} 与 CMYK {arr.shape[:2]} 不一致"
            )

    stacked = np.dstack([arr, w])

    tifffile.imwrite(
        path,
        stacked,
        photometric="separated",        # CMYK
        compression=COMPRESSION,
        predictor=PREDICTOR,
        resolution=(float(dpi), float(dpi)),
        resolutionunit="INCH",
        extrasamples=[0],               # 1 个 UNSPECIFIED 额外通道（与样例一致）
        metadata=None,                  # 不写 XMP，避免把无关信息带进印刷文件
    )
    log.info("印刷单文件已写出：%s（%d 通道，%d dpi，%.1f MB）",
             path.name, stacked.shape[2], dpi, path.stat().st_size / 1024 / 1024)
    return path


def read_stacked(path: Path) -> tuple[Image.Image, Image.Image]:
    """读回 5 通道 TIF，返回 `(CMYK, 白墨)`。

    主要给测试与人工核对用 —— 读回后能逐通道比对，确认写出的文件没问题。
    """
    import numpy as np
    import tifffile

    with tifffile.TiffFile(Path(path)) as tf:
        page = tf.pages[0]
        arr = page.asarray()
        if arr.ndim != 3 or arr.shape[2] < 4:
            raise RuntimeError(f"不是预期的多通道 TIFF：shape={arr.shape}")
        cmyk = Image.fromarray(arr[..., :4], mode="CMYK")
        if arr.shape[2] >= 5:
            white = Image.fromarray(arr[..., 4], mode="L")
        else:
            white = Image.new("L", cmyk.size, 255)
        return cmyk, white


def describe(path: Path) -> dict:
    """读出关键标签，用于自检 / 与样例对比。"""
    import tifffile

    with tifffile.TiffFile(Path(path)) as tf:
        p = tf.pages[0]
        return {
            "samples_per_pixel": p.samplesperpixel,
            "bits_per_sample": p.bitspersample,
            "photometric": int(p.photometric),
            "extrasamples": [int(x) for x in (p.extrasamples or ())],
            "compression": int(p.compression),
            "predictor": int(p.predictor or 0),
            "shape": tuple(p.shape),
            "size": p.shape[:2][::-1] if p.shape else (0, 0),
        }
