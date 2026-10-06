# -*- coding: utf-8 -*-
"""印刷 TIF 导出测试。

全部使用**临时目录 + 合成 PNG**，不触碰真实 output/、不调用付费 API。

覆盖（对应需求 8 条）：
    1. 4 个 TIF + manifest 都存在
    2. CMYK 模式正确
    3. 刀模轮廓闭合、出血按 DPI 换算正确
    4. 白墨层尺寸与彩层一致、透明区对应不印白墨
    5. 预览 JPEG 最长边 ≤ 1200px
    6. manifest 字段齐全
    7. 失败降级：源图缺失 / ICC 缺失 / 磁盘满 / 全透明图
    8. 重跑幂等，不产生脏文件
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from app.config import Config, make_config  # noqa: E402
from app.print_export import (  # noqa: E402
    PRINT_DIR_NAME,
    PREVIEW_DIR_NAME,
    WORK_DIR_NAME,
    ErrorCode,
    PrintExporter,
    read_print_manifest,
)
from app.print_export.dieline import bleed_pixels, find_outline, is_closed  # noqa: E402
from app.print_export.layers import (  # noqa: E402
    CutoutSession,
    make_white_ink,
    tighten_alpha,
)


def make_sticker_png(path: pathlib.Path, *, size: int = 800,
                     transparent: bool = False) -> pathlib.Path:
    """造一张"像贴纸"的合成 PNG：白底 + 中央彩色圆形图案。"""
    img = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    m = size // 6
    if not transparent:
        # 深蓝圆 + 红方块 + 一些细节，模拟贴纸主体
        draw.ellipse([m, m, size - m, size - m], fill=(20, 60, 150))
        draw.ellipse([m * 2, m * 2, size - m * 2, size - m * 2], fill=(230, 30, 40))
        draw.rectangle([size // 2 - 40, size // 2 - 40, size // 2 + 40, size // 2 + 40],
                       fill=(255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return path


class PrintExportBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.store_dir = self.root / "batch_test" / "01_测试门店"
        self.gen_dir = self.store_dir / "01_测试门店生成图"
        self.gen_dir.mkdir(parents=True, exist_ok=True)
        self.png = make_sticker_png(self.gen_dir / "20260101_000000_01_测试主题.png")

        self.cfg = make_config()
        # 测试用小尺寸，跑得快；同时验证尺寸换算公式
        self.cfg.print_width_cm = 10.0
        self.cfg.print_dpi = 100
        self.cfg.print_bleed_mm = 3.0
        self.cfg.print_max_pixels = 4000
        self.cfg.print_export_enabled = True
        self.cfg.print_white_ink = True
        self.cfg.print_dieline = True
        self.cfg.print_keep_work = True
        # ⚠️ 测试基类用**分层模式**：这一组测试验证的是各层（CMYK / 白墨 / 刀模）
        #    自身的正确性，分层才看得到。单文件合成另有一组测试
        #    （`TestSingleFile`）专门验证。
        self.cfg.print.single_file = False

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def export(self, **overrides):
        """导出。

        ⚠️ `keep_*` 这类**新配置**在子配置里（`cfg.print.keep_preview_jpg`），
           不在扁平兼容层 —— 扁平层只登记旧字段名。
           所以这里对 `keep_` 开头的键写进子配置，其余仍走旧式扁平属性。
        """
        for k, v in overrides.items():
            if k.startswith("keep_") and hasattr(self.cfg.print, k):
                setattr(self.cfg.print, k, v)
            else:
                setattr(self.cfg, k, v)
        exporter = PrintExporter(self.cfg)
        return exporter.export_store(self.store_dir, self.png, store_name="01_测试门店_01")

    def print_dir(self) -> pathlib.Path:
        return self.store_dir / PRINT_DIR_NAME


class TestOutputs(PrintExportBase):
    """1 / 2 / 4 / 5：产出的文件与图层模式。"""

    def test_four_tifs_and_manifest_exist(self) -> None:
        """需求 1：交付层（CMYK / 白墨 / 刀模）+ manifest 都存在。

        ⚠️ 「合并预览」**默认不生成** —— 印刷厂（H-2003E + Caldera RIP）
           只吃这三层，预览是给人看的，留着只会让交付目录变乱。
           想人工核对时把 `print_keep_merged_preview` 打开即可。

        ⚠️ 白墨层**必须留着**：UV 打印在透明介质（玻璃贴纸）上要靠它打底，
           删了颜色就是透明的，贴上去看不见。
        """
        res = self.export()
        self.assertTrue(res.ok, f"导出失败：{res.error_code} {res.error_message}")
        d = self.print_dir()
        for suffix in ("_CMYK.tif", "_白墨.tif", "_刀模.tif"):
            f = d / f"01_测试门店_01{suffix}"
            self.assertTrue(f.is_file(), f"缺少 {f.name}")
        self.assertTrue((d / "print_manifest.json").is_file(), "缺少 print_manifest.json")
        self.assertFalse(
            (d / "01_测试门店_01_合并预览.tif").exists(),
            "默认不应生成合并预览（交付精简）",
        )

    def test_cmyk_mode(self) -> None:
        """需求 2：彩色层必须是 CMYK 模式。"""
        self.export()
        with Image.open(self.print_dir() / "01_测试门店_01_CMYK.tif") as im:
            self.assertEqual(im.mode, "CMYK")

    def test_layer_sizes_match(self) -> None:
        """需求 4（前半）：各层像素尺寸一致。"""
        self.export()
        d = self.print_dir()
        sizes = []
        for suffix in ("_CMYK.tif", "_白墨.tif", "_刀模.tif"):
            with Image.open(d / f"01_测试门店_01{suffix}") as im:
                sizes.append((suffix, im.size))
        first = sizes[0][1]
        for name, sz in sizes[1:]:
            self.assertEqual(sz, first, f"{name} 尺寸 {sz} 与彩色层 {first} 不一致")

    def test_white_ink_modes_and_polarity(self) -> None:
        """需求 4（后半）：白墨层是单通道；透明区不印白墨（极性 A）。

        注意取样点：**不能取图像中心** —— 测试图的中心是白色方块，
        与背景同色，去背后被正确判为透明。要取**圆环上的点**（一定是图案）。
        """
        self.export()
        with Image.open(self.print_dir() / "01_测试门店_01_白墨.tif") as im:
            self.assertEqual(im.mode, "L")
            # 角落属于背景 → 应不印白墨
            self.assertEqual(im.getpixel((2, 2)), 0,
                             "背景角落应为 0（不印白墨）")
            # 圆环上的点（左边中部）属于图案 → 应印白墨
            w, h = im.size
            ring = im.getpixel((int(w * 0.22), h // 2))
            self.assertEqual(ring, 255, f"图案区域应为 255（印白墨），实际 {ring}")

    def test_white_ink_invert_swaps_polarity(self) -> None:
        """极性开关：invert=True 时透明区变全白。"""
        self.export(print_white_ink_invert=True)
        with Image.open(self.print_dir() / "01_测试门店_01_白墨.tif") as im:
            corner = im.getpixel((2, 2))
            self.assertEqual(corner, 255, "invert 模式下透明区应为全白（印满版白墨）")

    def test_preview_jpeg_max_edge(self) -> None:
        """需求 5：预览 JPEG 最长边 ≤ 1200px。

        ⚠️ 预览默认**不生成**（交付精简），所以这里要显式开启 ——
           顺带验证「开关确实能开」（之前扁平属性没登记映射，
           `getattr` 永远返回 False，想开也开不了，属于真 bug）。
        """
        self.export(keep_preview_jpg=True)
        p = self.store_dir / PREVIEW_DIR_NAME / "01_测试门店_01_预览.jpg"
        self.assertTrue(p.is_file(), "预览 JPEG 未生成")
        with Image.open(p) as im:
            self.assertLessEqual(max(im.size), 1200, f"预览尺寸 {im.size} 超过 1200")
            self.assertEqual(im.format, "JPEG")


class TestSingleFile(PrintExportBase):
    """交付单文件模式：一个 5 通道 TIF（CMYK + 白墨专色）。

    结构是拿现场「能直接打印」的样例反推出来的
    （`学生托管门店_01.tif`）：

        SamplesPerPixel = 5 / BitsPerSample = (8,8,8,8,8)
        Photometric = 5 (separated/CMYK) / ExtraSamples = (0,)
        Compression = 5 (LZW) / Predictor = 2 / Resolution = 120

    第 5 通道是**双峰蒙版**：`255 = 印白墨`（图案区）、`0 = 不印`（透明背景）。
    极性反了会导致整版印白、或完全不印白（贴上去颜色透明）。
    """

    def setUp(self) -> None:
        super().setUp()
        self.cfg.print.single_file = True
        self.cfg.print.dpi = 120
        self.cfg.print.dieline = False

    def test_only_one_tif_and_manifest(self) -> None:
        """只产出一个 TIF + manifest（不再有 _CMYK / _白墨 / _刀模 分文件）。"""
        res = self.export()
        self.assertTrue(res.ok, f"导出失败：{res.error_code} {res.error_message}")
        tifs = sorted(p.name for p in self.print_dir().glob("*.tif"))
        self.assertEqual(len(tifs), 1, f"应只有一个 TIF，实际 {tifs}")
        self.assertEqual(tifs[0], "01_测试门店_01.tif")
        for unwanted in ("_CMYK.tif", "_白墨.tif", "_刀模.tif", "_合并预览.tif"):
            self.assertFalse(
                (self.print_dir() / f"01_测试门店_01{unwanted}").exists(),
                f"单文件模式下不应有 {unwanted}",
            )

    def test_structure_matches_sample(self) -> None:
        """TIFF 结构必须让蒙泰认出白墨专色 —— 这是「能直接打印」的前提。

        现场照片确认：蒙泰 V7.0「白墨设定」里**白墨输出模式 = 专色**、
        通道数 = 1、纸张类型 = 彩白彩。所以第 5 通道要表达成**专色通道**，
        而 Photoshop 存专色通道的组成是：

          · `ExtraSamples = UNASSOCIATED_ALPHA(2)`
          · IRB(34377) → `AlphaChannelsNames`，通道名 = `White`

        名字按现场的 PS 动作 `一键专色(1).ATN` 定：它执行 make → SCch
        （Spot Color Channel），`Nm = "White"`。

        ⚠️ 两个都要有。第一版固定用 `UNSPECIFIED(0)` 又没写通道名，
           蒙泰两边都不认，导出后还得进 PS 手工改成专色通道。
        """
        self.export()
        from app.print_export.stacked import describe

        d = describe(self.print_dir() / "01_测试门店_01.tif")
        self.assertEqual(d["samples_per_pixel"], 5, "必须是 5 通道（CMYK+白墨）")
        self.assertEqual(d["bits_per_sample"], 8)
        self.assertEqual(d["photometric"], 5, "CMYK（separated）")
        self.assertEqual(
            d["extrasamples"], [2],
            "第 5 通道必须是 UNASSOCIATED_ALPHA(2) —— 专色通道的存法",
        )
        self.assertEqual(
            d["channel_names"], ["W1"],
            "必须带专色通道名（现场 PS 通道面板显示的是 W1），"
            "否则蒙泰不认这是白墨",
        )
        self.assertEqual(d["compression"], 5, "LZW")
        self.assertEqual(d["predictor"], 2, "水平差分预测")

    def test_size_matches_60cm_at_dpi(self) -> None:
        """60cm @ 120dpi = 2835px（与样例完全一致）。"""
        res = self.export()
        # 测试基类把宽度设成 10cm，这里按 10cm @120dpi = 472px 验算
        expected = round(10.0 / 2.54 * 120)
        self.assertEqual(res.manifest.output_pixels[0], expected,
                         f"10cm@120dpi 应为 {expected}px")

    def test_white_channel_is_binary_and_correct_polarity(self) -> None:
        """第 5 通道是二值蒙版，且**图案区为 255**（印白墨）。"""
        import numpy as np

        self.export()
        from app.print_export.stacked import read_stacked

        _, white = read_stacked(self.print_dir() / "01_测试门店_01.tif")
        a = np.asarray(white)
        vals = set(np.unique(a).tolist())
        self.assertTrue(vals <= {0, 255}, f"白墨应是二值蒙版，实际取值 {sorted(vals)[:8]}")
        # 测试图是居中的圆角矩形，中心必是图案 → 必须印白墨
        cy, cx = a.shape[0] // 2, a.shape[1] // 2
        self.assertEqual(int(a[cy, cx]), 255, "图案区应印白墨（255）")
        # 左上角是背景 → 不印
        self.assertEqual(int(a[2, 2]), 0, "透明背景不应印白墨（0）")

    def test_no_preview_dir_created(self) -> None:
        """交付精简：不生成预览就不该留下空的 `预览/` 目录。"""
        self.export()
        self.assertFalse((self.store_dir / PREVIEW_DIR_NAME).exists(),
                         "默认不应创建 预览/ 目录")


class TestGeometry(PrintExportBase):
    """3：刀模 / 出血 / 闭合。"""

    def test_bleed_pixel_conversion(self) -> None:
        """3mm @ 300DPI = 35.43px → 35；@100DPI = 11.8 → 12。"""
        self.assertEqual(bleed_pixels(3.0, 300), 35)
        self.assertEqual(bleed_pixels(3.0, 100), 12)
        self.assertEqual(bleed_pixels(0.0, 300), 1)   # 至少 1px

    def test_target_size_follows_formula(self) -> None:
        """像素尺寸 = 宽cm / 2.54 × DPI（受 max_pixels 限制前）。"""
        res = self.export()
        self.assertTrue(res.ok, res.error_message)
        w, h = res.manifest.output_pixels
        expect = round(10.0 / 2.54 * 100)      # 394
        self.assertAlmostEqual(w, expect, delta=2,
                               msg=f"宽度 {w} 与公式 {expect} 不符")

    def test_dieline_closed(self) -> None:
        """需求 3：刀模轮廓闭合。

        ⚠️ 必须先走**去背**再找轮廓 —— 直接对白底 RGB 找轮廓的话，
           Alpha 全是不透明，轮廓会退化成整张图的边界。
        """
        from app.print_export.layers import cutout

        rgba, _ = cutout(Image.open(self.png), None)
        outline = find_outline(rgba, bleed_px=0)
        self.assertTrue(outline, f"未找到轮廓：{outline.error}")
        self.assertTrue(outline.closed, "轮廓未判定为闭合")
        self.assertGreaterEqual(outline.points, 3)

    def test_dieline_red_and_not_filled(self) -> None:
        """刀模线：红色描边，内部不填色（应为白色）。"""
        self.export()
        with Image.open(self.print_dir() / "01_测试门店_01_刀模.tif") as im:
            rgb = im.convert("RGB")
            w, h = rgb.size
            # 四角应为白（不填色）
            self.assertEqual(rgb.getpixel((2, 2)), (255, 255, 255), "刀模层角落应为白色")
            # 存在红色像素（描边）
            colors = rgb.getcolors(maxcolors=1 << 20) or []
            has_red = any(c[1][0] > 150 and c[1][1] < 110 and c[1][2] < 110
                          for c in colors)
            self.assertTrue(has_red, "刀模层未找到红色描边")

    def test_bleed_enlarges_outline(self) -> None:
        """出血：外扩后轮廓面积应大于原轮廓（需先去背）。"""
        from app.print_export.layers import cutout

        rgba, _ = cutout(Image.open(self.png), None)
        base = find_outline(rgba, bleed_px=0)
        grown = find_outline(rgba, bleed_px=20)
        self.assertTrue(base and grown, "轮廓查找失败")
        self.assertGreater(grown.area, base.area * 1.02,
                           f"出血后面积未明显增大：{base.area:.0f} → {grown.area:.0f}")


class TestManifest(PrintExportBase):
    """6：manifest 字段齐全。"""

    def test_fields_complete(self) -> None:
        self.export()
        m = read_print_manifest(self.print_dir())
        self.assertIsNotNone(m, "manifest 读取失败")

        self.assertEqual(m["status"], "success")
        self.assertTrue(m["version"], "缺少处理版本号")
        self.assertTrue(m["exported_at"], "缺少导出时间")

        src = m["source"]
        self.assertTrue(src["png"].endswith(".png"), "缺少源 PNG 路径")
        self.assertEqual(len(src["pixels"]), 2, "缺少源图像素尺寸")

        out = m["output"]
        for k in ("pixels", "dpi", "effective_dpi", "width_cm"):
            self.assertIn(k, out, f"output 缺少 {k}")
        self.assertEqual(out["dpi"], 100)

        self.assertIn("mm", m["bleed"], "缺少出血 mm")
        self.assertIn("name", m["icc"], "缺少 ICC 名称")
        self.assertTrue(m["layers"], "缺少图层清单")
        self.assertIn("cmyk", m["layers"])

    def test_effective_dpi_reflects_source(self) -> None:
        """有效 DPI 应按源图像素推算（源 800px / 10cm → 203DPI）。"""
        self.export()
        m = read_print_manifest(self.print_dir())
        eff = m["output"]["effective_dpi"]
        expect = 800 / (10.0 / 2.54)
        self.assertAlmostEqual(eff, expect, delta=2.0,
                               msg=f"有效 DPI {eff} 与推算 {expect:.1f} 不符")

    def test_manifest_has_no_absolute_sensitive_path(self) -> None:
        """manifest 里不应出现 API Key 之类敏感内容。"""
        self.export()
        text = json.dumps(read_print_manifest(self.print_dir()), ensure_ascii=False)
        for bad in ("sk-", "Bearer", "api_key", "data:image"):
            self.assertNotIn(bad, text, f"manifest 含敏感内容：{bad}")


class TestDegradation(PrintExportBase):
    """7：失败降级。"""

    def test_source_missing(self) -> None:
        """源 PNG 不存在 → 结构化错误码，不抛异常。"""
        exporter = PrintExporter(self.cfg)
        res = exporter.export_store(self.store_dir, self.store_dir / "不存在.png",
                                    store_name="X")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, ErrorCode.SOURCE_MISSING)

    def test_source_unreadable(self) -> None:
        """源文件损坏 → SOURCE_UNREADABLE。"""
        bad = self.gen_dir / "bad.png"
        bad.write_bytes(b"this is not a png")
        exporter = PrintExporter(self.cfg)
        res = exporter.export_store(self.store_dir, bad, store_name="BAD")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, ErrorCode.SOURCE_UNREADABLE)

    def test_icc_missing_degrades_gracefully(self) -> None:
        """ICC 路径无效 → 回落系统 profile；系统也没有则降级朴素转换。

        两种情况都必须：**产出全部文件**、**在 manifest 里说明用了什么**。
        （早期测试期望"必须 fallback"，但代码回落到系统 RSWOP.icm 更好 ——
          有 ICC 就用，没 ICC 才降级。）
        """
        self.cfg.print_icc_path = str(self.root / "不存在.icc")
        res = self.export()
        self.assertTrue(res.ok, f"ICC 路径无效不该导致失败：{res.error_message}")

        m = read_print_manifest(self.print_dir())
        icc = m["icc"]
        # 要么回落系统 profile，要么明确标记降级 —— 二者必居其一
        used_system = bool(icc.get("name")) and not icc.get("fallback")
        fallback = bool(icc.get("fallback"))
        self.assertTrue(used_system or fallback,
                        f"既没用上 profile 也没标记降级：{icc}")
        if fallback:
            warnings = " ".join(m.get("warnings") or [])
            self.assertIn("ICC", warnings, "降级时未记录 warnings")

        # 关键：不管走哪条路，文件都必须齐全
        self.assertTrue((self.print_dir() / "01_测试门店_01_CMYK.tif").is_file())
        self.assertTrue((self.print_dir() / "print_manifest.json").is_file())

    def test_all_transparent_image(self) -> None:
        """全透明图 → 找轮廓失败，但导出不崩，只记警告。"""
        blank = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
        p = self.gen_dir / "blank.png"
        blank.save(p)
        exporter = PrintExporter(self.cfg)
        res = exporter.export_store(self.store_dir, p, store_name="BLANK")
        # 允许成功（并在警告里说明轮廓失败），但绝不能抛异常
        self.assertIsNotNone(res.manifest)
        if res.ok:
            warnings = " ".join(res.manifest.warnings)
            self.assertIn("刀模", warnings, "全透明图应记录刀模失败警告")

    def test_failure_still_writes_manifest(self) -> None:
        """失败时也要留一份 manifest 便于排障。"""
        exporter = PrintExporter(self.cfg)
        res = exporter.export_store(self.store_dir, self.store_dir / "无.png",
                                    store_name="ERR")
        self.assertFalse(res.ok)
        m = read_print_manifest(self.print_dir())
        self.assertIsNotNone(m, "失败时未写 manifest")
        self.assertEqual(m["status"], "failed")
        self.assertEqual((m.get("error") or {}).get("code"), ErrorCode.SOURCE_MISSING)


class TestIdempotent(PrintExportBase):
    """8：重跑幂等。"""

    def test_rerun_same_result(self) -> None:
        """连跑两次：文件集合一致，不产生脏文件。"""
        res1 = self.export()
        self.assertTrue(res1.ok, res1.error_message)
        files1 = sorted(p.name for p in self.print_dir().iterdir())
        manifest1 = read_print_manifest(self.print_dir())

        res2 = self.export()
        self.assertTrue(res2.ok, res2.error_message)
        files2 = sorted(p.name for p in self.print_dir().iterdir())
        manifest2 = read_print_manifest(self.print_dir())

        self.assertEqual(files1, files2, "重跑后文件集合变化")
        self.assertNotIn("True", " ".join(files2))
        # 不应留下 .tmp 残留
        self.assertFalse([f for f in files2 if f.endswith(".tmp")], "留下临时文件")
        self.assertEqual(manifest1["layers"], manifest2["layers"])

    def test_rerun_does_not_touch_source(self) -> None:
        """重跑不得改动源 PNG（字节级）。"""
        import hashlib

        before = hashlib.sha256(self.png.read_bytes()).hexdigest()
        self.export()
        self.export()
        after = hashlib.sha256(self.png.read_bytes()).hexdigest()
        self.assertEqual(before, after, "源 PNG 被修改了")

    def test_work_dir_uses_hardlink_or_copy(self) -> None:
        """_work 归档：同分区应是硬链接（不占额外空间）。"""
        self.export()
        work = self.store_dir / WORK_DIR_NAME
        self.assertTrue(work.is_dir(), "_work 目录未创建")
        items = list(work.iterdir())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].name, self.png.name)
        # 硬链接或副本都接受，但内容必须一致
        self.assertEqual(items[0].stat().st_size, self.png.stat().st_size)


class TestSourcePreserved(PrintExportBase):
    """核心约束：绝不改动现有 PNG。"""

    def test_source_untouched(self) -> None:
        import hashlib

        before = hashlib.sha256(self.png.read_bytes()).hexdigest()
        before_mtime = self.png.stat().st_mtime_ns
        self.export()
        self.assertEqual(hashlib.sha256(self.png.read_bytes()).hexdigest(), before)
        self.assertEqual(self.png.stat().st_mtime_ns, before_mtime)

    def test_output_dirs_isolated(self) -> None:
        """新内容只写进 印刷TIF/ 预览/ _work/，不污染生成图目录。"""
        self.export()
        gen_files = sorted(p.name for p in self.gen_dir.iterdir())
        self.assertEqual(gen_files, [self.png.name],
                         f"生成图目录被污染：{gen_files}")


class TestCutoutMode(PrintExportBase):
    """去背方式开关（settings.print.cutout = auto | rembg | fallback）。"""

    def test_fallback_mode_skips_rembg(self) -> None:
        """fallback 模式：不建 rembg 会话，直接走纯色去背。"""
        self.cfg.print_cutout = "fallback"
        exporter = PrintExporter(self.cfg)
        self.assertIsNone(exporter.session, "fallback 模式不应创建 rembg 会话")
        self.assertEqual(exporter.engine, "fallback")

        res = exporter.export_store(self.store_dir, self.png, store_name="FB")
        self.assertTrue(res.ok, res.error_message)
        self.assertEqual(res.manifest.options.get("cutout"), "fallback")
        self.assertEqual(res.manifest.options.get("cutout_mode"), "fallback")

    def test_auto_mode_picks_available_engine(self) -> None:
        """auto 模式：有 rembg 用 rembg，没有就 fallback —— 两种都算通过。"""
        self.cfg.print_cutout = "auto"
        exporter = PrintExporter(self.cfg)
        self.assertIn(exporter.engine, ("rembg", "fallback"))

        res = exporter.export_store(self.store_dir, self.png, store_name="AUTO")
        self.assertTrue(res.ok, res.error_message)
        self.assertEqual(res.manifest.options.get("cutout_mode"), "auto")
        self.assertIn(res.manifest.options.get("cutout"), ("rembg", "fallback"))

    def test_rembg_mode_degrades_when_unavailable(self) -> None:
        """rembg 模式但引擎不可用 → 记警告并降级，**不能失败**。"""

        class Unavailable(CutoutSession):
            @property
            def available(self) -> bool:
                return False

        self.cfg.print_cutout = "rembg"
        exporter = PrintExporter(self.cfg, session=Unavailable())
        self.assertEqual(exporter.engine, "fallback", "不可用时实际引擎应是 fallback")

        res = exporter.export_store(self.store_dir, self.png, store_name="RB")
        self.assertTrue(res.ok, "rembg 不可用不该让导出失败")
        self.assertEqual(res.manifest.options.get("cutout_mode"), "rembg")
        self.assertEqual(res.manifest.options.get("cutout"), "fallback")

    def test_invalid_mode_falls_back_to_auto(self) -> None:
        """配置写错（非法值）→ 回落 auto，不中断导出。"""
        self.cfg.print_cutout = "不存在的模式"
        exporter = PrintExporter(self.cfg)
        self.assertEqual(exporter.cutout_mode, "auto")

        res = exporter.export_store(self.store_dir, self.png, store_name="BAD")
        self.assertTrue(res.ok, res.error_message)

    def test_modes_produce_same_layer_geometry(self) -> None:
        """两种去背方式产出的图层**尺寸完全一致**（只影响边缘细节）。"""
        sizes = {}
        for mode in ("fallback", "auto"):
            self.cfg.print_cutout = mode
            res = PrintExporter(self.cfg).export_store(
                self.store_dir, self.png, store_name=f"GEO_{mode}")
            self.assertTrue(res.ok, res.error_message)
            sizes[mode] = res.manifest.output_pixels
        self.assertEqual(sizes["fallback"], sizes["auto"],
                         "两种去背方式的目标尺寸应一致")


class TestHelpers(unittest.TestCase):
    """工具函数的边界。"""

    def test_tighten_alpha_shrinks(self) -> None:
        img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse([20, 20, 80, 80], fill=(255, 0, 0, 255))
        before = img.getchannel("A").histogram()[-1]
        after = tighten_alpha(img, 1).getchannel("A").histogram()[-1]
        self.assertLess(after, before, "收边后不透明像素应减少")

    def test_white_ink_threshold(self) -> None:
        """Alpha 低于阈值的区域不应出白墨。"""
        alpha = Image.new("L", (10, 10), 0)
        for x in range(5, 10):
            for y in range(10):
                alpha.putpixel((x, y), 200)
        white = make_white_ink(alpha, invert=False)
        self.assertEqual(white.getpixel((1, 1)), 0)
        self.assertEqual(white.getpixel((8, 8)), 255)

    def test_is_closed_rejects_short(self) -> None:
        self.assertFalse(is_closed(None))
        self.assertFalse(is_closed([[0, 0], [1, 1]]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
