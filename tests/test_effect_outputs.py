# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from app.config import Config
from app.effect_renderer import EFFECT_RENDERER_VERSION, render_storefront_glass
from app.manifest import ManifestStore
from app.models import JobStatus
from app.storage import Storage
from app.store_repo import StoreRepository
from app.web import server


def sticker_png() -> bytes:
    image = Image.new("RGB", (256, 384), "white")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((26, 45, 230, 333), radius=22, fill="#174a72", outline="#d4af37", width=8)
    draw.rectangle((55, 132, 201, 250), fill="#f8d26b")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class EffectOutputTests(unittest.TestCase):
    def test_renderer_returns_same_canvas_size_valid_png_and_source_hash(self) -> None:
        source = sticker_png()
        result = render_storefront_glass(source, "房屋中介门店")
        with Image.open(io.BytesIO(result.data)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (256, 384))
        self.assertNotEqual(result.data, source)
        self.assertEqual(len(result.source_sha256), 64)

    def test_renderer_uses_real_photo_background_and_records_it(self) -> None:
        background = Image.new("RGB", (320, 480), "#785f4d")
        draw = ImageDraw.Draw(background)
        draw.rectangle((24, 48, 296, 438), fill="#2d4e5c")
        payload = io.BytesIO()
        background.save(payload, format="JPEG")
        result = render_storefront_glass(
            sticker_png(),
            "房屋中介门店",
            background=payload.getvalue(),
            background_asset={"id": "a" * 16, "file_name": "实拍门店.jpg", "sha256": "b" * 64},
            realism_iteration=3,
        )
        with Image.open(io.BytesIO(result.data)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (256, 384))
        metadata = result.metadata(Path("效果图.png"))
        self.assertEqual(metadata["background"]["mode"], "real_photo")
        self.assertEqual(metadata["background"]["file_name"], "实拍门店.jpg")
        self.assertEqual(metadata["realism_iteration"], 3)

    def test_generated_and_effect_directories_and_backfill_metadata(self) -> None:
        repo = StoreRepository()
        store = repo.stores[0]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "output"
            cfg = Config(
                provider="mock", provider_label="本地模拟", model="mock",
                output_root=root, output_base_root=root, selected_store_indexes=[store.folder_index],
                whiten_background=False,
            )
            storage = Storage(root, whiten_bg=False)
            job = next(job for job in repo.build_jobs() if job.store_index == store.folder_index and job.pic_index == "01")
            source_path = storage.save_image(job, sticker_png())
            self.assertEqual(source_path.parent, storage.generated_dir(store.output_dir))
            self.assertTrue(source_path.is_file())
            manifest = ManifestStore(root).for_store(store.output_dir)
            job.status = JobStatus.SUCCESS
            manifest.record(job, provider="mock", model="mock")
            manifest.save()

            events: list[dict] = []
            stats = server._generate_missing_effects(cfg, events.append)
            effect_path = storage.effect_dir(store.output_dir) / f"{source_path.stem}_效果图.png"
            self.assertEqual(stats["success"], 1)
            self.assertTrue(effect_path.is_file())
            entry = ManifestStore(root).for_store(store.output_dir).entries["01"]
            self.assertEqual(entry["runtime_metrics"]["effect_image"]["status"], "success")
            self.assertEqual(entry["runtime_metrics"]["effect_image"]["renderer"], EFFECT_RENDERER_VERSION)
            self.assertIn("effect_ready", [event["type"] for event in events])

            state = server.build_state_payload(cfg)
            item = state["stores"][0]["items"][0]
            self.assertTrue(item["exists"])
            self.assertTrue(item["effect_exists"])
            self.assertEqual(item["effect_file"], effect_path.name)


if __name__ == "__main__":
    unittest.main()
