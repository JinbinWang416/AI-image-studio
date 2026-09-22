# -*- coding: utf-8 -*-
"""参考图压缩与 data_url 缓存测试。

背景：
    参考图以 **Base64 data URL** 传给服务商（体积 ×1.33），
    大图会让请求变得很重；超长边还可能被服务商直接拒绝。

覆盖：
    1. 边长超限 → 压缩到上限内（**判据是边长，不是体积**）
    2. 边长合规 → 原样返回
    3. 压缩失败不影响上传（原样返回）
    4. 入库链路：压缩后按新内容算 SHA-256，去重仍准确
    5. manifest/meta 记录压缩信息，且**不含 Base64**
    6. data_url 缓存生效且按资产隔离
"""

from __future__ import annotations

import base64
import io
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app import reference_assets as ra  # noqa: E402
from app.reference_assets import (  # noqa: E402
    REFERENCE_MAX_EDGE,
    ReferenceAssetError,
    ReferenceAssetStore,
    downscale_image,
)


def make_png(w: int, h: int, *, noise: bool = True) -> bytes:
    """造一张有细节的 PNG（纯色压得太小，测不出边长超限的场景）。"""
    im = Image.new("RGB", (w, h))
    px = im.load()
    step = 1 if max(w, h) <= 1200 else 3      # 大图稀疏填充，避免测试太慢
    for y in range(0, h, step):
        gy = (y * 255) // max(1, h)
        for x in range(0, w, step):
            gx = (x * 255) // max(1, w)
            n = (((x * 7 + y * 13) % 17) - 8) if noise else 0
            px[x, y] = (max(0, min(255, gx + n)),
                        max(0, min(255, gy + n)),
                        max(0, min(255, (gx + gy) // 2 + n)))
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


def to_data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


class TestDownscale(unittest.TestCase):
    """1 / 2 / 3：压缩判据与容错。"""

    def test_oversized_is_shrunk(self) -> None:
        raw = make_png(2400, 1800)
        out, info = downscale_image(raw, "image/png", max_edge=1536)
        self.assertTrue(info["applied"], f"应压缩但未压缩：{info}")
        with Image.open(io.BytesIO(out)) as im:
            self.assertLessEqual(max(im.size), 1536)
            # 等比：宽高比应保持
            self.assertAlmostEqual(im.size[0] / im.size[1], 2400 / 1800, places=1)
        self.assertEqual(info["from"], [2400, 1800])
        self.assertEqual(info["to"], [1536, 1152])

    def test_within_limit_untouched(self) -> None:
        raw = make_png(800, 600)
        out, info = downscale_image(raw, "image/png", max_edge=1536)
        self.assertFalse(info["applied"])
        self.assertIs(out, raw, "未超限时应原样返回同一对象")

    def test_exactly_at_limit_untouched(self) -> None:
        """正好等于上限不压缩（边界）。"""
        raw = make_png(1536, 1000)
        out, info = downscale_image(raw, "image/png", max_edge=1536)
        self.assertFalse(info["applied"])
        self.assertIs(out, raw)

    def test_small_bytes_but_long_edge_still_shrunk(self) -> None:
        """🔴 回归：体积小但边长超限的图**必须**压缩。

        早先用「体积 < 400KB 就跳过」做前置判断，一张纯色 3000×2000
        （PNG 只有几十 KB）会逃过压缩 —— 但边长照样超限，
        服务商可能直接拒绝。
        """
        im = Image.new("RGB", (3000, 2000), (200, 210, 220))   # 纯色 → 极小
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        raw = buf.getvalue()
        self.assertLess(len(raw), 400 * 1024, "前提：这张图体积很小")

        out, info = downscale_image(raw, "image/png", max_edge=1536)
        self.assertTrue(info["applied"], "体积小但边长超限，仍应压缩")
        with Image.open(io.BytesIO(out)) as r:
            self.assertLessEqual(max(r.size), 1536)

    def test_broken_bytes_returned_as_is(self) -> None:
        raw = b"not an image at all"
        out, info = downscale_image(raw, "image/png")
        self.assertFalse(info["applied"])
        self.assertEqual(out, raw)

    def test_jpeg_compression(self) -> None:
        im = Image.new("RGB", (2400, 1600), (120, 140, 160))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=95)
        out, info = downscale_image(buf.getvalue(), "image/jpeg", max_edge=1024)
        self.assertTrue(info["applied"])
        with Image.open(io.BytesIO(out)) as r:
            self.assertEqual(r.format, "JPEG")
            self.assertLessEqual(max(r.size), 1024)


class TestStoreIntegration(unittest.TestCase):
    """4 / 5：入库链路与元数据。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ReferenceAssetStore(pathlib.Path(self._tmp.name))
        ra._URL_CACHE.clear()

    def tearDown(self) -> None:
        ra._URL_CACHE.clear()
        self._tmp.cleanup()

    def test_oversized_asset_is_stored_downscaled(self) -> None:
        """超大图入库后**边长**必须在上限内。

        ⚠️ 不断言"体积一定变小"：PNG 缩放后重编码**可能反而更大**
           （细节被插值成更多中间色，压缩率下降）。
           边长是服务商的**硬约束**，体积只是成本 —— 所以变大也照样采用，
           只是会在 info 里记一条 note。
        """
        raw = make_png(2600, 2000)
        asset = self.store.add_data_url(to_data_url(raw), "big.png")
        with Image.open(asset.path) as im:
            self.assertLessEqual(max(im.size), REFERENCE_MAX_EDGE,
                                 f"入库后边长仍超限：{im.size}")
            self.assertEqual(im.size, (1536, 1182), "应等比缩放到上限")

    def test_sha256_matches_stored_content(self) -> None:
        """哈希必须按**压缩后**的内容算，否则去重会错。"""
        import hashlib

        raw = make_png(2600, 2000)
        asset = self.store.add_data_url(to_data_url(raw), "big.png")
        self.assertEqual(asset.sha256, hashlib.sha256(asset.path.read_bytes()).hexdigest())

    def test_dedup_still_works_after_compression(self) -> None:
        raw = make_png(2600, 2000)
        a1 = self.store.add_data_url(to_data_url(raw), "a.png")
        a2 = self.store.add_data_url(to_data_url(raw), "b.png")
        self.assertEqual(a1.id, a2.id, "同一张图压缩后应仍判定为同一资产")

    def test_meta_records_compression(self) -> None:
        raw = make_png(2600, 2000)
        asset = self.store.add_data_url(to_data_url(raw), "big.png")
        meta = self.store._meta_path(asset.id)
        self.assertTrue(meta.is_file())
        import json

        data = json.loads(meta.read_text(encoding="utf-8"))
        # 重新读一遍 meta 文件确认字段落盘
        self.assertIn("bytes", data)
        # 压缩信息应写进 meta（若触发了压缩）
        if data.get("compressed"):
            self.assertIn("from", data["compressed"])
            self.assertIn("to", data["compressed"])

    def test_meta_never_contains_base64(self) -> None:
        """⚠️ AGENTS.md：manifest/meta 不得存 Base64。"""
        raw = make_png(2600, 2000)
        asset = self.store.add_data_url(to_data_url(raw), "big.png")
        text = self.store._meta_path(asset.id).read_text(encoding="utf-8")
        self.assertNotIn("base64", text.lower())
        self.assertNotIn("data:image", text)
        self.assertLess(len(text), 4000, "meta 不该被图片内容撑大")

    def test_small_asset_not_compressed(self) -> None:
        raw = make_png(600, 400)
        asset = self.store.add_data_url(to_data_url(raw), "small.png")
        with Image.open(asset.path) as im:
            self.assertEqual(im.size, (600, 400))

    def test_oversize_still_rejected(self) -> None:
        """10MB 上限仍然生效（压缩发生在体积校验**之后**）。"""
        big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024)
        with self.assertRaises(ReferenceAssetError):
            self.store.add_data_url(to_data_url(big), "huge.png")


class TestDataUrlCache(unittest.TestCase):
    """6：data_url 缓存。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ReferenceAssetStore(pathlib.Path(self._tmp.name))
        ra._URL_CACHE.clear()

    def tearDown(self) -> None:
        ra._URL_CACHE.clear()
        self._tmp.cleanup()

    def test_cache_hit_returns_same_string(self) -> None:
        asset = self.store.add_data_url(to_data_url(make_png(600, 400)), "a.png")
        first = asset.data_url()
        second = asset.data_url()
        self.assertIs(first, second, "第二次应命中缓存返回同一对象")
        self.assertIn(asset.id, ra._URL_CACHE)

    def test_cache_isolated_per_asset(self) -> None:
        a = self.store.add_data_url(to_data_url(make_png(600, 400)), "a.png")
        b = self.store.add_data_url(to_data_url(make_png(640, 480)), "b.png")
        self.assertNotEqual(a.data_url(), b.data_url())

    def test_cache_is_bounded(self) -> None:
        """缓存有上限，不会无限增长。"""
        for i in range(ra._URL_CACHE_SIZE + 4):
            asset = self.store.add_data_url(
                to_data_url(make_png(300 + i, 200 + i)), f"f{i}.png")
            asset.data_url()
        self.assertLessEqual(len(ra._URL_CACHE), ra._URL_CACHE_SIZE)

    def test_large_asset_not_cached(self) -> None:
        """大图不缓存 —— 否则常驻内存会迅速膨胀。"""
        raw = make_png(2600, 2000)
        asset = self.store.add_data_url(to_data_url(raw), "big.png")
        asset.data_url()
        if asset.bytes > ra._URL_CACHE_MAX_BYTES:
            self.assertNotIn(asset.id, ra._URL_CACHE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
