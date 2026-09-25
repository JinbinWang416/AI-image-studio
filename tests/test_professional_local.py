# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from app.validation.professional import REPORT_NAME, compose_chinese, run_professional_validation
from app.providers.base import GenerateResult
from app.state.store_repo import StoreRepository


def colorful_base() -> bytes:
    image = Image.new("RGB", (1024, 1024), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((130, 60, 890, 760), fill=(20, 83, 131), outline=(232, 184, 72), width=30)
    draw.polygon(((85, 430), (510, 220), (920, 480), (600, 620)), fill=(49, 163, 180))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class FakeProvider:
    async def generate(self, req):
        return GenerateResult(images=[colorful_base()], provider="flux_local", model="fake", elapsed=0.01, raw={"peak_vram_mib": 99.0})


class ProfessionalLocalTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_candidates_are_scored_and_chinese_selected_output_is_written(self) -> None:
        store = StoreRepository().stores[0]
        with tempfile.TemporaryDirectory() as tmp:
            report = await run_professional_validation(FakeProvider(), store, Path(tmp))
            self.assertTrue(report["technical_pass"])
            self.assertEqual(report["selected_count"], 6)
            self.assertEqual(len(report["rows"][0]["candidates"]), 3)
            self.assertEqual(report["typography"]["title"], "房屋中介")
            self.assertTrue((Path(tmp) / REPORT_NAME).exists())
            for row in report["rows"]:
                self.assertTrue(Path(row["selected_path"]).is_file())
                self.assertGreater(row["selected"]["score"], 0)
            rerun = await run_professional_validation(FakeProvider(), store, Path(tmp))
            self.assertTrue(all(row["selected"]["source"] == "previous" for row in rerun["rows"]))

    def test_compose_writes_valid_png_and_changes_the_base(self) -> None:
        store = StoreRepository().stores[0]
        output = compose_chinese(colorful_base(), store, store.items[0])
        self.assertTrue(output.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertNotEqual(output, colorful_base())


if __name__ == "__main__":
    unittest.main()
