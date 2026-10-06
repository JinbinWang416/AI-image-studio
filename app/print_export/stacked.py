# -*- coding: utf-8 -*-
"""把 CMYK 与白墨合成**单个 5 通道 TIF** —— 适配蒙泰 V7.0 + 海邦达 H-2003E。

## 为什么是 5 通道

拿现场「能直接打印」的样例（`学生托管门店_01.tif`）反向分析：

    SamplesPerPixel           = 5
    BitsPerSample             = (8, 8, 8, 8, 8)
    PhotometricInterpretation = 5 (separated / CMYK)
    ExtraSamples              = (0,)          ← 1 个额外通道
    Compression               = 5 (LZW) / Predictor = 2
    Resolution                = 120 × 120

第 5 通道是**双峰蒙版**（42.3% 为 0、56.7% 为 255、中间值仅 0.9%），
即 `255 = 印白墨`（图案区）、`0 = 不印`（透明背景）。

## ⚠️ 光有第 5 个通道不够 —— RIP 得知道它是什么

**第一版**把第 5 通道标成 `UNSPECIFIED(0)`，结果**蒙泰不认**，
导出后还得进 PS 手工改一遍。现场照片解释了为什么：

    蒙泰 V7.0 → 白墨设定
      白彩关系        = 白彩同出
      白墨输出模式    = 【专色】      ← 按专色通道识别，不是按透明度
      通道数          = 1

所以第 5 通道必须表达成**专色通道**。Photoshop 存专色通道的组成是：

1. `ExtraSamples = UNASSOCIATED_ALPHA(2)` —— 通道本体是 alpha
2. IRB(34377) → `AlphaChannelsNames(0x03EE)` —— 通道**名字**

**两个都要有。** 只给 alpha 不给名字，蒙泰不知道它是白墨；只给名字不给 alpha，
通道类型就不对。之前把这两件事做成了互斥的两个选项，是设计错误。

通道名**因机而异**，不能照抄网上的教程。解析现场 PS 动作 `一键专色(1).ATN`
得到它执行的是：

    convertMode → CMYK
    make → SCch（Spot Color Channel），Nm = "White"，Clr = HSBC
    save → TIFF

所以**现场这台是 `White`**（网上教程写的 `W1` 是别人那台机器）。
配置项 `print.spot_channel_name` 可改。

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


def _irb_with_channel_names(names: list[str]) -> bytes:
    """构造 Photoshop IRB，写入 `AlphaChannelsNames`(0x03EE)。

    Photoshop 把「通道叫什么」记在 ImageResourcesBlock 里。蒙泰的白墨输出模式
    是**专色**，靠通道名认出哪条是白墨 —— 所以这个名字是必需的，不是装饰。

    格式（每个资源块）::

        '8BIM' | id(2B) | 名字长度(1B) | 名字 | 补偶 | 数据长度(4B) | 数据 | 补偶

    `AlphaChannelsNames` 的数据是若干 Pascal 串依次拼接。
    """
    import struct

    data = bytearray()
    for n in names:
        raw = n.encode("latin-1", "replace")[:255]
        data.append(len(raw))
        data += raw

    # 资源块头：'8BIM' + id + 空名字 + 补偶 + 长度
    block = b"8BIM" + struct.pack(">H", 0x03EE) + b"\x00\x00"
    block += struct.pack(">I", len(data)) + bytes(data)
    if len(data) % 2:
        block += b"\x00"
    return block


def save_stacked_cmyk_white(
    path: Path,
    cmyk: Image.Image,
    white: Image.Image | None,
    dpi: int = 120,
    alpha_mode: bool = True,
    spot_name: str = "White",
) -> Path:
    """写出 5 通道 TIF（CMYK + 白墨专色）—— 对齐蒙泰 V7.0 + H-2003E。

    现场照片确认：蒙泰「白墨设定」里**白墨输出模式 = 专色**、通道数 = 1、
    纸张类型 = 彩白彩。所以第 5 通道必须被表达成**专色通道**，而 Photoshop
    存专色通道的做法是：

      · `ExtraSamples = UNASSOCIATED_ALPHA(2)` —— 通道本体是 alpha
      · IRB(34377) 的 `AlphaChannelsNames(0x03EE)` —— 通道**名字**

    ⚠️ 两个都要有。只给 alpha 不给名字 → 蒙泰不知道它是白墨；
       只给名字不给 alpha → 连通道类型都不对。第一版固定在
       `UNSPECIFIED(0)`，结果是导出后还得进 PS 手工改。

    Args:
        path: 输出路径
        cmyk: CMYK 图像（4 通道）
        white: 白墨单通道图（``255 = 印白墨``）；``None`` 时补全 255
        dpi: 分辨率，写进 TIFF 标签
        alpha_mode: 是否标成 alpha（专色通道的存法）。默认 True。
        spot_name: 专色通道名。**按现场 PS 动作定** —— 解析 `一键专色(1).ATN`
            得到 `Nm = "White"`，所以现场这台是 `White`。

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

    # ⚠️ ExtraSamples 的取值决定 RIP 怎么理解第 5 通道：
    #    2 = UNASSOCIATED_ALPHA —— PS 存专色通道用的就是这个。**正确值**。
    #    0 = UNSPECIFIED —— 未指定，蒙泰不认（第一版的错误）。
    extrasamples = [2] if alpha_mode else [0]

    kwargs: dict = dict(
        photometric="separated",        # CMYK
        compression=COMPRESSION,
        predictor=PREDICTOR,
        resolution=(float(dpi), float(dpi)),
        resolutionunit="INCH",
        extrasamples=extrasamples,
        metadata=None,                  # 不写 XMP，避免把无关信息带进印刷文件
    )

    # 通道名 —— 蒙泰靠它认出「这条是白墨专色」。
    # AlphaChannelsNames 的第一项对应**第一个** alpha 通道（也就是第 5 通道）。
    try:
        irb = _irb_with_channel_names([spot_name])
        kwargs["extratags"] = [(34377, 7, len(irb), irb, False)]
    except Exception as exc:  # noqa: BLE001 - 写不上也不该阻断出图
        log.warning("专色通道名写入失败（%s），RIP 可能认不出白墨",
                    type(exc).__name__)

    tifffile.imwrite(path, stacked, **kwargs)
    log.info("印刷单文件已写出：%s（%d 通道，%d dpi，专色通道名=%s，%.1f MB）",
             path.name, stacked.shape[2], dpi, spot_name,
             path.stat().st_size / 1024 / 1024)
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
    import struct
    import tifffile

    with tifffile.TiffFile(Path(path)) as tf:
        p = tf.pages[0]
        d = {
            "samples_per_pixel": p.samplesperpixel,
            "bits_per_sample": p.bitspersample,
            "photometric": int(p.photometric),
            "extrasamples": [int(x) for x in (p.extrasamples or ())],
            "compression": int(p.compression),
            "predictor": int(p.predictor or 0),
            "shape": tuple(p.shape),
            "size": p.shape[:2][::-1] if p.shape else (0, 0),
        }
        # 把专色通道名读出来 —— 这是蒙泰认白墨的依据，必须能自检
        irb = p.tags.get(34377)
        d["channel_names"] = _read_channel_names(irb.value) if irb else []
    return d


def _read_channel_names(blob) -> list[str]:
    """从 IRB 里解析 `AlphaChannelsNames`(0x03EE)。"""
    import struct

    if isinstance(blob, tuple):
        blob = b"".join(blob)
    pos, n = 0, len(blob)
    while pos + 12 <= n:
        if blob[pos:pos + 4] != b"8BIM":
            break
        rid = struct.unpack(">H", blob[pos + 4:pos + 6])[0]
        name_len = blob[pos + 6]
        pad = (name_len + 1) % 2
        size_off = pos + 7 + name_len + pad
        size = struct.unpack(">I", blob[size_off:size_off + 4])[0]
        data = blob[size_off + 4:size_off + 4 + size]
        if rid == 0x03EE:
            names, i = [], 0
            while i < len(data):
                ln = data[i]
                names.append(data[i + 1:i + 1 + ln].decode("latin-1", "replace"))
                i += 1 + ln
            return names
        pos = size_off + 4 + size + (size % 2)
    return []
