# -*- coding: utf-8 -*-
"""Upscayl 集成测试 —— **全 mock，绝不真调 CLI**。

覆盖三类必须守住的行为：

1. **`-t` 永不为 0**（实测 `-t 0` 会输出尺寸正常但**全黑**的图，
   比报错更危险 —— 报错会被发现，全黑图可能一路流到印刷环节）
2. **alpha 拆合往返一致**（印刷链路在去背后是 RGBA）
3. **失败一律降级**（缺二进制 / 超时 / 非零退出码 → 返回 None，不抛错，
   由调用方回落 LANCZOS，批次不中断）
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from app.upscayl import (
    ALLOWED_SCALES,
    DEFAULT_TILE,
    MIN_TILE,
    UPSCAYL_MODELS,
    UpscaylEngine,
    binarize_alpha,
    upscale_rgba,
)
from app.upscayl.alpha import ALPHA_THRESHOLD


def _fake_run_ok(cmd, **kwargs):
    """假 runner：把输入图复制成输出，返回退出码 0。"""
    idx = cmd.index("-o")
    dst = Path(cmd[idx + 1])
    src = Path(cmd[cmd.index("-i") + 1])
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return subprocess.CompletedProcess(cmd, 0, b"Upscayled Successfully!\n", b"")


class BuildCmdTests(unittest.TestCase):
    """命令行构造 —— 硬规则的唯一落点。"""

    def setUp(self) -> None:
        # 不依赖机器上是否真的装了 Upscayl：直接把探测结果打桩
        self.engine = UpscaylEngine()
        self.engine._binary_cache = Path("C:/fake/upscayl-bin.exe")
        self.engine._models_cache = Path("C:/fake/models")

    def _cmd(self, **kw):
        kw.setdefault("model", "realesr-animevideov3-x2")
        kw.setdefault("scale", 2)
        kw.setdefault("tile", DEFAULT_TILE)
        return self.engine._build_cmd(Path("in.png"), Path("out.png"), **kw)

    def test_tile_zero_is_forced_to_default(self) -> None:
        """🔴 核心断言：`-t 0` 必须被改写成安全值。

        这不是「锦上添花」—— 实测 `-t 0` 会让 CLI 输出**全黑**图，
        而且退出码是 0、尺寸也正常，一路到印刷才会发现。
        """
        cmd = self._build_with_tile(0)
        self.assertIn("-t", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], str(DEFAULT_TILE))
        self.assertNotEqual(cmd[cmd.index("-t") + 1], "0")

    def test_small_tiles_are_forced_to_default(self) -> None:
        for bad in (1, 2, 16, MIN_TILE - 1, -8):
            with self.subTest(tile=bad):
                cmd = self._build_with_tile(bad)
                self.assertEqual(cmd[cmd.index("-t") + 1], str(DEFAULT_TILE))

    def test_valid_tiles_pass_through(self) -> None:
        for good in (MIN_TILE, 64, 128, 256, 512):
            with self.subTest(tile=good):
                cmd = self._build_with_tile(good)
                self.assertEqual(cmd[cmd.index("-t") + 1], str(good))

    def _build_with_tile(self, tile: int) -> list[str]:
        return self.engine._build_cmd(
            Path("in.png"), Path("out.png"), "realesr-animevideov3-x2", 2, tile
        )

    def test_required_flags_present(self) -> None:
        cmd = self._cmd()
        for flag in ("-i", "-o", "-n", "-s", "-t", "-m"):
            self.assertIn(flag, cmd, f"缺少 {flag} 参数")
        self.assertEqual(cmd[0], str(self.engine.binary))
        self.assertEqual(cmd[cmd.index("-m") + 1], str(self.engine.models_dir))

    def test_rejects_unknown_model(self) -> None:
        from app.upscayl import UpscaylError

        with self.assertRaises(UpscaylError):
            self._cmd(model="no-such-model")

    def test_rejects_bad_scale(self) -> None:
        from app.upscayl import UpscaylError

        for bad in (0, 1, 5, 8):
            with self.subTest(scale=bad):
                with self.assertRaises(UpscaylError):
                    self._cmd(scale=bad)

    def test_all_catalog_models_are_accepted(self) -> None:
        for name in UPSCAYL_MODELS:
            with self.subTest(model=name):
                cmd = self._cmd(model=name)
                self.assertEqual(cmd[cmd.index("-n") + 1], name)


class DegradeTests(unittest.TestCase):
    """失败必须返回 None（降级），而不是抛错中断批次。"""

    def test_missing_binary_returns_none(self) -> None:
        engine = UpscaylEngine(binary="C:/definitely/not/here.exe")
        engine._binary_cache = None
        engine._models_cache = None
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.png"
            Image.new("RGB", (8, 8)).save(src)
            self.assertIsNone(
                engine.upscale_image(src, Path(tmp) / "out.png", "realesr-animevideov3-x2", 2)
            )

    def test_missing_input_returns_none(self) -> None:
        engine = UpscaylEngine()
        engine._binary_cache = Path("C:/fake/bin.exe")
        engine._models_cache = Path("C:/fake/models")
        self.assertIsNone(
            engine.upscale_image(Path("nope.png"), Path("out.png"), "realesr-animevideov3-x2", 2)
        )

    def test_nonzero_exit_returns_none(self) -> None:
        def failing(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, b"", b"vkQueueSubmit failed -4")

        engine = UpscaylEngine(runner=failing)
        engine._binary_cache = Path("C:/fake/bin.exe")
        engine._models_cache = Path("C:/fake/models")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.png"
            Image.new("RGB", (8, 8)).save(src)
            with self.assertLogs("app.upscayl.engine", level="WARNING"):
                got = engine.upscale_image(src, Path(tmp) / "out.png", "realesr-animevideov3-x2", 2)
            self.assertIsNone(got)

    def test_timeout_returns_none(self) -> None:
        def timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)

        engine = UpscaylEngine(runner=timeout)
        engine._binary_cache = Path("C:/fake/bin.exe")
        engine._models_cache = Path("C:/fake/models")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.png"
            Image.new("RGB", (8, 8)).save(src)
            with self.assertLogs("app.upscayl.engine", level="WARNING"):
                got = engine.upscale_image(src, Path(tmp) / "out.png", "realesr-animevideov3-x2", 2)
            self.assertIsNone(got)

    def test_success_returns_path(self) -> None:
        engine = UpscaylEngine(runner=_fake_run_ok)
        engine._binary_cache = Path("C:/fake/bin.exe")
        engine._models_cache = Path("C:/fake/models")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.png"
            Image.new("RGB", (8, 8)).save(src)
            dst = Path(tmp) / "out.png"
            self.assertEqual(
                engine.upscale_image(src, dst, "realesr-animevideov3-x2", 2), dst
            )
            self.assertTrue(dst.is_file())


class AlphaTests(unittest.TestCase):
    """alpha 拆合 —— 印刷链路在去背后是 RGBA。"""

    def test_binarize_alpha_produces_only_two_values(self) -> None:
        """二值化后只能有 0 和 255。

        ⚠️ 这是印刷硬边的要求：AI 超分会造出渐变的半透明像素，
           印在玻璃上就是一圈灰边。
        """
        alpha = Image.new("L", (16, 16), 0)
        for x in range(16):
            for y in range(16):
                alpha.putpixel((x, y), (x * 16 + y) % 256)
        out = binarize_alpha(alpha)
        self.assertEqual(sorted(set(out.getdata())), [0, 255])

    def test_binarize_threshold_boundary(self) -> None:
        alpha = Image.new("L", (2, 1))
        alpha.putpixel((0, 0), ALPHA_THRESHOLD - 1)
        alpha.putpixel((1, 0), ALPHA_THRESHOLD)
        out = list(binarize_alpha(alpha).getdata())
        self.assertEqual(out, [0, 255])

    def test_upscale_rgba_keeps_alpha_structure(self) -> None:
        """透明/不透明区域的比例必须保持（内容不变形）。"""
        engine = UpscaylEngine(runner=_fake_run_ok)
        engine._binary_cache = Path("C:/fake/bin.exe")
        engine._models_cache = Path("C:/fake/models")

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.png"
            img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
            for x in range(16, 32):
                for y in range(32):
                    img.putpixel((x, y), (255, 255, 255, 255))
            img.save(src)

            dst = Path(tmp) / "out.png"
            got = upscale_rgba(engine, src, dst, "realesr-animevideov3-x2", 2, DEFAULT_TILE)
            self.assertEqual(got, dst)

            with Image.open(dst) as out:
                self.assertEqual(out.mode, "RGBA")
                self.assertEqual(out.size, (64, 64))
                alpha = out.getchannel("A")
                values = set(alpha.getdata())
                self.assertTrue(values <= {0, 255}, f"alpha 未二值化：{sorted(values)[:8]}")
                # 左半透明、右半不透明
                self.assertEqual(alpha.getpixel((4, 32)), 0)
                self.assertEqual(alpha.getpixel((60, 32)), 255)

    def test_upscale_rgba_degrades_when_cli_fails(self) -> None:
        def failing(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, b"", b"boom")

        engine = UpscaylEngine(runner=failing)
        engine._binary_cache = Path("C:/fake/bin.exe")
        engine._models_cache = Path("C:/fake/models")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.png"
            Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(src)
            with self.assertLogs("app.upscayl.engine", level="WARNING"):
                got = upscale_rgba(engine, src, Path(tmp) / "out.png",
                                   "realesr-animevideov3-x2", 2, DEFAULT_TILE)
            self.assertIsNone(got)


class ConfigDefaultTests(unittest.TestCase):
    """默认必须全关 —— 这是「上线零风险」的保证。"""

    def test_defaults_are_off(self) -> None:
        from app.config import load_config

        u = load_config().upscayl
        self.assertFalse(u.enabled, "Upscayl 默认必须是关闭的")
        self.assertFalse(u.upscale_generated)
        self.assertFalse(u.upscale_print)

    def test_default_tile_is_safe(self) -> None:
        from app.config import load_config

        u = load_config().upscayl
        self.assertGreaterEqual(u.tile, MIN_TILE, "默认 tile 不能小于安全下限")

    def test_default_models_exist_in_catalog(self) -> None:
        from app.config import load_config

        u = load_config().upscayl
        self.assertIn(u.model_generated, UPSCAYL_MODELS)
        self.assertIn(u.model_print, UPSCAYL_MODELS)
        self.assertIn(u.scale, ALLOWED_SCALES)


if __name__ == "__main__":
    unittest.main()
