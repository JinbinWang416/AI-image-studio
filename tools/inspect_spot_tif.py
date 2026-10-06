# -*- coding: utf-8 -*-
"""深挖 TIFF 里的 Photoshop 专色通道信息，为「自动生成可打印 TIF」提供依据。

目标：搞清楚 PS 存的「专色通道」到底把哪些数据写进了文件，
     好让我们直接生成同构文件，省掉「进 PS 手工设成专色」这一步。

分析三层：
  1. TIFF 标签（SamplesPerPixel / ExtraSamples / Photometric …）
  2. Photoshop IRB（tag 34377）—— 逐个资源块，重点是 0x03EE 通道名、0x0432 专色
  3. ImageSourceData（tag 37724）—— PS 私有的 LayerInfo，专色定义可能藏在这里

用法::

    python tools\\inspect_spot_tif.py <文件.tif>
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import tifffile

# Photoshop IRB 资源 ID
IRB_NAMES = {
    0x03E8: "ObsoletePhotoshop", 0x03E9: "MacPrintInfo",
    0x03ED: "ResolutionInfo", 0x03EE: "AlphaChannelsNames",
    0x03F0: "PrintFlags", 0x03F1: "ColorHalftoningInfo",
    0x03F2: "ColorTransferFunctions", 0x03F3: "LayerStateInfo",
    0x03F4: "WorkingPath", 0x03F5: "GridGuidesInfo",
    0x03F8: "Caption", 0x03FD: "XResolution2", 0x03FE: "DisplayInfo2",
    0x0400: "PrintFlags2", 0x0404: "IPTCData", 0x0406: "JPEG_Quality",
    0x0408: "GridGuidesInfo2", 0x0409: "ThumbnailResource2",
    0x040A: "CopyrightFlag", 0x040B: "URL", 0x040C: "ThumbnailResource",
    0x040D: "GlobalAngle", 0x040E: "ColorSamplers2",
    0x040F: "ICC_Profile", 0x0410: "Watermark", 0x0411: "ICC_Untagged",
    0x0412: "EffectsVisible", 0x0413: "SpotHalftone",
    0x0414: "DocumentSpecificIDsSeed", 0x0415: "UnicodeAlphaNames",
    0x0416: "IndexedColorTable", 0x0417: "TransparencyIndex",
    0x0419: "GlobalAltitude", 0x041A: "Slices", 0x041B: "WorkflowURL",
    0x041C: "JumpToXPEP", 0x041D: "AlphaIdentifiers",
    0x041E: "URLList", 0x0421: "VersionInfo", 0x0422: "ExifData1",
    0x0423: "ExifData3", 0x0424: "XMP", 0x0425: "CaptionDigest",
    0x0426: "PrintScale", 0x0428: "PixelAspectRatio",
    0x0429: "LayerComps", 0x042A: "AlternateDuotoneColors",
    0x042B: "AlternateSpotColors", 0x042D: "LayerSelectionIds",
    0x042E: "HDRToningInfo", 0x042F: "PrintInfo", 0x0430: "LayerGroupsEnabledID",
    0x0431: "ColorSamplers", 0x0432: "MeasurementScale",
    0x0433: "TimelineInfo", 0x0434: "SheetDisclosure",
    0x0435: "DisplayInfo", 0x0436: "OnionSkins", 0x0438: "CountInfo",
    0x043A: "PrintInfo2", 0x043B: "PrintStyle", 0x043C: "MacNSPrintInfo",
    0x043D: "WinDevMode", 0x043E: "AutoSaveFilePath",
    0x043F: "AutoSaveFormat", 0x0440: "PathSelectionState",
    0x0441: "LayerGroupsEnabledID2", 0x0442: "LightroomWorkflow",
    0x07D0: "PathSelectionState2", 0x0BB7: "ClippingPathName",
    0x0BB8: "OriginPathInfo", 0x0FA0: "PathInfo1",
    0x1B58: "PathInfo2", 0x2710: "PathInfo3", 0x2AF8: "PathInfo4",
    0x3A98: "PathInfo5", 0x3E80: "PathInfo6",
}


def hexdump(data: bytes, limit: int = 320) -> str:
    d = data[:limit]
    s = d.hex()
    return " ".join(s[i:i + 2] for i in range(0, len(s), 2)) + (" …" if len(data) > limit else "")


def parse_irb(blob: bytes, verbose: bool = True) -> dict:
    """解析 PS IRB，返回 {资源ID: 数据}。"""
    if isinstance(blob, tuple):
        blob = b"".join(blob)
    out: dict[int, bytes] = {}
    pos, n, idx = 0, len(blob), 0
    while pos + 12 <= n:
        if blob[pos:pos + 4] != b"8BIM":
            if verbose:
                print(f"    [{idx}] 签名异常 {blob[pos:pos+4]!r} @ {pos}，停止")
            break
        rid = struct.unpack(">H", blob[pos + 4:pos + 6])[0]
        name_len = blob[pos + 6]
        pad = (name_len + 1) % 2
        size_off = pos + 7 + name_len + pad
        if size_off + 4 > n:
            break
        size = struct.unpack(">I", blob[size_off:size_off + 4])[0]
        data = blob[size_off + 4:size_off + 4 + size]
        out[rid] = data
        if verbose:
            label = IRB_NAMES.get(rid, "")
            rname = blob[pos + 7:pos + 7 + name_len].decode("latin-1", "replace")
            print(f"    [{idx:2}] 0x{rid:04X} ({rid:5}) {label:24} name={rname!r:8} size={size}")
            if rid in (0x03EE, 0x042A, 0x042B, 0x041D) and size:
                print(f"          {hexdump(data, 200)}")
                # AlphaChannelsNames 是若干 Pascal 串
                if rid == 0x03EE:
                    names, i = [], 0
                    while i < len(data):
                        ln = data[i]
                        names.append(data[i + 1:i + 1 + ln].decode("latin-1", "replace"))
                        i += 1 + ln
                    print(f"          → 通道名: {names}")
        idx += 1
        pos = size_off + 4 + size + (size % 2)
    return out


def scan_layer_info(blob: bytes, needles: tuple[bytes, ...]) -> None:
    """在 LayerInfo（私有格式）里搜关键字符串，定位专色定义。"""
    print("  === LayerInfo（tag 37724）关键字符串扫描 ===")
    for needle in needles:
        positions = []
        start = 0
        while True:
            i = blob.find(needle, start)
            if i < 0:
                break
            positions.append(i)
            start = i + 1
            if len(positions) >= 6:
                break
        tag = needle.decode("latin-1", "replace")
        if positions:
            print(f"    {tag!r:22} 出现 {len(positions)} 次  偏移 {positions}")
            i = positions[0]
            ctx = blob[max(0, i - 24):i + 64]
            print(f"        上下文: {hexdump(ctx, 88)}")
        else:
            print(f"    {tag!r:22} 未找到")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    p = Path(sys.argv[1])
    if not p.is_file():
        print(f"  [!!] 文件不存在：{p}")
        return 2

    print("=" * 78)
    print(f"  {p.name}   ({p.stat().st_size / 1024 / 1024:.2f} MB)")
    print("=" * 78)

    with tifffile.TiffFile(p) as tf:
        pg = tf.pages[0]
        print()
        print("  === 1. TIFF 关键标签 ===")
        for k, tag in sorted(pg.tags.items(), key=lambda x: x[0]):
            if k in (273, 279, 324, 325):        # 偏移表太长，跳过
                continue
            v = tag.value
            if isinstance(v, (bytes, bytearray)) and len(v) > 50:
                v = f"<{len(v)} bytes>"
            try:
                nm = tag.name
            except Exception:
                nm = "?"
            print(f"    {k:6} {nm:26} = {str(v)[:70]}")

        print()
        print("  === 2. Photoshop IRB（tag 34377）===")
        irb_tag = pg.tags.get(34377)
        irb = {}
        if irb_tag is None:
            print("    [无] —— 这个文件没有 IRB，那专色信息只能靠 ExtraSamples 表达")
        else:
            irb = parse_irb(irb_tag.value)
            print()
            print(f"    资源块共 {len(irb)} 个，ID: {[hex(k) for k in sorted(irb)]}")

        print()
        print("  === 3. ImageSourceData（tag 37724 = PS LayerInfo）===")
        src = pg.tags.get(37724)
        if src is None:
            print("    [无]")
        else:
            blob = src.value
            if isinstance(blob, tuple):
                blob = b"".join(blob)
            print(f"    大小 {len(blob) / 1024 / 1024:.2f} MB")
            # 通道名 / 专色相关关键字
            scan_layer_info(blob, (b"W1", b"W2", b"White", b"Spot", b"spot",
                                   b"\xe4\xb8\x93\xe8\x89\xb2",   # 专色(UTF-8)
                                   b"\xd7\xa8\xc9\xab"))          # 专色(GBK)

        print()
        print("  === 4. 逐通道统计 ===")
        arr = pg.asarray()
        print(f"    shape = {arr.shape}  dtype = {arr.dtype}")
        import numpy as np
        for i in range(arr.shape[-1]):
            ch = arr[..., i]
            nm = "CMYK"[i] if i < 4 else f"第{i+1}通道"
            uniq = len(np.unique(ch))
            print(f"    {nm:10} 范围 {ch.min():3}~{ch.max():3}  唯一值 {uniq:4}  "
                  f"均值 {ch.mean():6.1f}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
