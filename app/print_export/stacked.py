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

通道名**因机而异**，以**现场 PS 通道面板里的实际显示**为准 —— 现场是 `W1`。

⚠️ 这里踩过两次坑，都是「照抄别人」：

  1. 网上教程说他那台是 `W1` → 照抄 `W1`（这次碰巧对了）
  2. 解析现场动作 `一键专色(1).ATN` 得到 `Nm = "White"` → 改成 `White`，
     但**现场通道面板里显示的是 `W1`**，那个动作文件很可能不是现场在用的。

**结论：只认通道面板。** 配置项 `print.spot_channel_name` 可改。

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

# ---------------------------------------------------------------- 专色资源块
#
# 下面这几个字节是**从现场「能打印」的文件里逐字节挖出来的**
# （`01_房屋中介门店_试印(3).tif`，PS 27.0 处理过、蒙泰认它）。
#
# ⚠️ 只写 `AlphaChannelsNames` **不够** —— 第一版就是这么写的，
#    结果蒙泰仍要求手工设专色。PS 实际写了 **4 个**资源块，
#    其中 `AlternateSpotColors` 才是「油墨特性」（颜色 + 密度）的载体。
#
# `AlternateSpotColors` 的 18 字节结构：::
#
#     00 01             版本 1
#     00 01             专色数量 = 1
#     00 00 00 04       色彩空间 ID（HSB）
#     00 07             项数 7
#     27 10             = 10000 = 密度 100%
#     00 00 00 00 00 00 颜色值（白色）
SPOT_RES_ALPHA_NAMES = 0x03EE
SPOT_RES_UNICODE_NAMES = 0x0415
SPOT_RES_ALT_SPOT = 0x042B
SPOT_RES_ALPHA_IDS = 0x041D

# 现场实测值，照抄即可（改颜色/密度时才需要动）
_ALT_SPOT_DENSITY = 10000          # 0x2710 = 密度 100%
_ALPHA_IDS = (0, 4)

# 透明度通道的名字。PS 用 GBK 写在 AlphaChannelsNames 里，
# 用 UTF-16BE 写在 UnicodeAlphaNames 里。
TRANSPARENCY_NAME = "透明度"


def _res_block(rid: int, data: bytes) -> bytes:
    """包一个 Photoshop IRB 资源块。

    格式::

        '8BIM' | id(2B) | 名字长度(1B) | 名字 | 补偶 | 数据长度(4B) | 数据 | 补偶

    名字一律留空（PS 也是这么写的），数据长度不足偶数时补一个 0。
    """
    import struct

    block = b"8BIM" + struct.pack(">H", rid) + b"\x00\x00"
    block += struct.pack(">I", len(data)) + data
    if len(data) % 2:
        block += b"\x00"
    return block


def _pascal_names(names: list[str], encoding: str, width: int) -> bytes:
    """把名字列表编码成 IRB 里那两种 Pascal 串数组。

    · `AlphaChannelsNames` —— 1 字节长度 + 单字节编码（现场是 GBK）
    · `UnicodeAlphaNames`  —— 4 字节长度 + UTF-16BE

    ⚠️ 现场文件里第一个名字（透明度）**没有**结尾的 NUL，第二个有。
       PS 自己都不一致，这里按「每个名字补一个 NUL」统一写，
       长度字段随之加一 —— 蒙泰按长度读，不会错位。
    """
    out = bytearray()
    for n in names:
        if encoding == "utf-16-be":
            raw = (n + "\x00").encode("utf-16-be")
            out += (len(raw) // 2).to_bytes(4, "big")   # 长度按 UTF-16 单元数
            out += raw
        else:
            raw = n.encode(encoding, "replace")[:255]
            out.append(len(raw))
            out += raw
    return bytes(out)


def _irb_with_spot_channel(
    spot_name: str = "W1",
    has_transparency: bool = True,
    density: int = _ALT_SPOT_DENSITY,
) -> bytes:
    """构造完整的 Photoshop IRB（4 个资源块）—— 让蒙泰认出白墨专色。

    ⚠️ 这 4 个块是**实测得出**的，缺一不可：

      0x03EE `AlphaChannelsNames`  —— 通道名（GBK 单字节）
      0x0415 `UnicodeAlphaNames`   —— 通道名（UTF-16BE，PS 也写）
      0x042B `AlternateSpotColors` —— **油墨特性：颜色 + 密度**（关键！）
      0x041D `AlphaIdentifiers`    —— 各 alpha 通道的 ID

    第一版只写了 0x03EE，蒙泰照样不认 —— 缺的正是 `AlternateSpotColors`。
    """
    import struct

    names = [TRANSPARENCY_NAME, spot_name] if has_transparency else [spot_name]
    # GBK 是现场文件的编码（`cd b8 c3 f7 b6 c8` = 透明度）
    ansi = _pascal_names(names, "gbk", 1)
    uni = _pascal_names(names, "utf-16-be", 4)

    n_alpha = len(names)
    # 18 字节，逐字段对齐现场 PS 文件：
    #   00 01 | 00 01 | 00 00 00 04 | 00 07 | 27 10 | 00 00 00 00 00 00
    #   H(2)    H(2)    I(4)          H(2)    H(2)    6s(6)             = 18
    spot = struct.pack(
        ">HHIHH6s",
        1,                      # 版本
        1,                      # 专色数量（PS 恒为 1，即使有透明度通道）
        4,                      # 色彩空间 ID（HSB）
        7,                      # 项数
        density,                # 密度，10000 = 100%
        b"\x00" * 6,            # 颜色值（白色）
    )
    ids = b"".join(struct.pack(">I", i) for i in _ALPHA_IDS[:n_alpha])

    return (
        _res_block(SPOT_RES_ALPHA_NAMES, ansi)
        + _res_block(SPOT_RES_UNICODE_NAMES, uni)
        + _res_block(SPOT_RES_ALT_SPOT, spot)
        + _res_block(SPOT_RES_ALPHA_IDS, ids)
    )


def _irb_with_channel_names(names: list[str]) -> bytes:
    """只写通道名（旧接口，保留兼容）。

    新代码请用 `_irb_with_spot_channel()` —— 只写名字蒙泰不认。
    """
    return _res_block(SPOT_RES_ALPHA_NAMES,
                      _pascal_names(names, "latin-1", 1))

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
    spot_name: str = "W1",
    transparency: bool = True,
    density: int = _ALT_SPOT_DENSITY,
) -> Path:
    """写出多通道 TIF（CMYK + 透明度 + 白墨专色）—— 适配蒙泰 V7.0 + H-2003E。

    现场证据链：

    1. 蒙泰「白墨设定」—— **白墨输出模式 = 专色**、通道数 = 1、纸张类型 = 彩白彩
    2. PS 通道面板 —— 专色通道名 **`W1`**，颜色白色，密度 100%
    3. 一个「跑完 PS、确认能打印」的文件 —— 结构是：

           SamplesPerPixel = 6
           ExtraSamples    = (ASSOCALPHA(1), UNSPECIFIED(0))
           通道名           = ['透明度', 'W1']
           第5通道 255 占 50.5%   第6通道 255 占 49.5%   ← 两者反相

    所以形状是 **CMYK(4) + 透明度(1) + 白墨(1) = 6 通道**，
    而 IRB 里必须写 **4 个资源块**（只写通道名不够 —— 第一版试过，蒙泰仍要求手工设专色）：

      · 0x03EE `AlphaChannelsNames`  —— 通道名（GBK）
      · 0x0415 `UnicodeAlphaNames`   —— 通道名（UTF-16BE）
      · 0x042B `AlternateSpotColors` —— **油墨特性：白色 + 密度 100%**（关键）
      · 0x041D `AlphaIdentifiers`    —— alpha 通道 ID

    Args:
        path: 输出路径
        cmyk: CMYK 图像（4 通道）
        white: 白墨单通道图（``255 = 印白墨``）；``None`` 时补全 255
        dpi: 分辨率，写进 TIFF 标签
        alpha_mode: 保留参数（旧接口兼容），现在恒为真 —— PS 存专色通道
            用的就是 alpha，没有「不用 alpha」的合法形态。
        spot_name: 专色通道名。**以现场 PS 通道面板的实际显示为准** ——
            现场是 `W1`。（动作文件 `一键专色(1).ATN` 里写的是 `White`，
            但通道面板显示 `W1`，说明那个动作不是现场在用的。）
        transparency: 是否额外带一路「透明度」通道。现场那个能打印的文件
            是 **6 通道**（CMYK + 透明度 + W1），所以默认带上。

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

    if transparency:
        # ⚠️⚠️ **通道顺序是决定性的** —— 蒙泰取「第一个 alpha 通道」当专色。
        #
        #     顺序放错 → 它读到的是「透明度」（= 白墨反相）→ 白墨印到背景上，
        #     表现为「主体没有了、边框有墨」。实测 23 个门店**全部印反**。
        #
        #     现场三组对照（同一张图，只改通道安排）：
        #       A 透明度在前、W1 在后（6 通道） → ❌ 反
        #       B W1 在前、透明度在后（6 通道） → ✅ **正常**
        #       C 只有 W1（5 通道）              → ❌ 不行
        #
        #     所以：**第 5 通道必须是 W1**，透明度排它后面。
        #
        #     语义：白墨 255 = 印白墨（图案区）；透明度 255 = 完全透明（背景），
        #     两者互为反相。
        alpha = (255 - w).astype(np.uint8)
        stacked = np.dstack([arr, w, alpha])
        # ExtraSamples 沿用 B 变体实测可用的形态：
        # 第 5 通道（W1）标 ASSOCALPHA、第 6 通道（透明度）标 UNSPECIFIED。
        # ⚠️ 语义上看着"不匹配"，但**这是实测能打印的那一组**，不要凭直觉改。
        extrasamples = [1, 0]
    else:
        # 退化形态：只带白墨一路（实测蒙泰**不认**，仅作兼容保留）
        stacked = np.dstack([arr, w])
        extrasamples = [0] if not alpha_mode else [2]

    kwargs: dict = dict(
        photometric="separated",        # CMYK
        compression=COMPRESSION,
        predictor=PREDICTOR,
        resolution=(float(dpi), float(dpi)),
        resolutionunit="INCH",
        extrasamples=extrasamples,
        metadata=None,                  # 不写 XMP，避免把无关信息带进印刷文件
    )

    # 专色定义 —— 蒙泰靠它认出「这条是白墨」。**4 个资源块缺一不可**，
    # 只写通道名不够（第一版就是这么写的，蒙泰仍要求手工设专色）。
    try:
        irb = _irb_with_spot_channel(
            spot_name=spot_name,
            has_transparency=transparency,
            density=density,
        )
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
    """读回多通道 TIF，返回 `(CMYK, 白墨)`。

    ⚠️ 白墨在第 **5** 通道（紧跟 CMYK），不是最后一个：
       现在的通道顺序是 CMYK(4) + **W1**(5) + **透明度**(6) ——
       W1 必须排在前，蒙泰取「第一个 alpha 通道」当专色。
       拿成最后一个会读到透明度（= 白墨反相），极性整个颠倒。

    主要给测试与人工核对用。
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
    """从 IRB 里解析 `AlphaChannelsNames`(0x03EE)。

    ⚠️ 现场文件里「透明度」是 **GBK** 编码（`cd b8 c3 f7 b6 c8`），
       不是 latin-1 —— 用 latin-1 解会得到乱码 `Í¸Ã÷¶È`。
       这里先按 GBK 试，失败再退回 latin-1。
    """
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
            return _decode_pascal_names(data)
        pos = size_off + 4 + size + (size % 2)
    return []


def _decode_pascal_names(data: bytes) -> list[str]:
    """解 Pascal 串数组，优先 GBK。"""
    raw, i = [], 0
    while i < len(data):
        ln = data[i]
        raw.append(data[i + 1:i + 1 + ln])
        i += 1 + ln
    out = []
    for b in raw:
        try:
            out.append(b.decode("gbk"))
        except UnicodeDecodeError:
            out.append(b.decode("latin-1", "replace"))
    return out
