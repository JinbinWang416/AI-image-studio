# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import io
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from app.config import Config, make_config
from app.state.manifest_store import ManifestStore
from app.generation.regeneration import regenerate_openai_house
from app.providers.base import GenerateResult
from app.state.store_repo import StoreRepository


def image_bytes(number: int) -> bytes:
    image = Image.new("RGB", (256, 256), "white")
    draw = ImageDraw.Draw(image)
    rng = random.Random(number)
    for y in range(8):
        for x in range(8):
            color = tuple(rng.randrange(25, 225) for _ in range(3))
            draw.rectangle((x * 32, y * 32, x * 32 + 31, y * 32 + 31), fill=color)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class FakeOpenAIProvider:
    name = "openai"
    model = "gpt-image-2.5-flare"
    price_per_image = 0.0

    def __init__(self, first: bytes | None = None) -> None:
        self.calls = 0
        self.first = first

    def describe(self) -> str:
        return "OpenAI GPT Image（fake）"

    async def generate(self, req):
        self.calls += 1
        data = self.first if self.calls == 1 and self.first is not None else image_bytes(self.calls + 100)
        return GenerateResult(images=[data], provider=self.name, model=self.model, elapsed=0.01, raw={"revised_prompts": ["test"]})

    async def close(self) -> None:
        return None


class BlockingOpenAIProvider(FakeOpenAIProvider):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def generate(self, req):
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()


def config(root: Path) -> Config:
    return make_config(
        provider="openai", provider_label="OpenAI GPT Image", api_key="test",
        base_url="https://api.openai.com/v1", model="gpt-image-2.5-flare",
        output_root=root, prompt_version="v8", overwrite=True, concurrency=1,
        retry_max=1, retry_backoff=[0], whiten_background=False,
    )


class OpenAIRegenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_run_archives_all_six_and_rotates_variants(self) -> None:
        store = StoreRepository().stores[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            first = await regenerate_openai_house(cfg=config(root), provider=FakeOpenAIProvider(), store=store, root=root)
            self.assertEqual(first["success"], 6)
            store_dir = root / store.output_dir
            generated_dir = store_dir / f"{store.output_dir}生成图"
            effect_dir = store_dir / f"{store.output_dir}效果图"
            self.assertEqual(len(list(generated_dir.glob("*.png"))), 6)
            self.assertEqual(len(list(effect_dir.glob("*.png"))), 6)

            second = await regenerate_openai_house(cfg=config(root), provider=FakeOpenAIProvider(), store=store, root=root)
            self.assertEqual(second["success"], 6)
            self.assertNotEqual(first["run_id"], second["run_id"])
            history = generated_dir / "_history"
            self.assertEqual(len(list(history.glob("*.png"))), 6)
            manifest = ManifestStore(root).for_store(store.output_dir)
            metrics = manifest.entries["01"]["runtime_metrics"]
            self.assertEqual(metrics["run_id"], second["run_id"])
            self.assertIn("视觉变体", metrics["request_prompt"])
            self.assertTrue(metrics["archived_to"] if "archived_to" in metrics else manifest.entries["01"].get("archived_to"))
            self.assertEqual(metrics["effect_image"]["status"], "success")

    async def test_near_duplicate_is_retried_once_before_saving(self) -> None:
        store = StoreRepository().stores[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            await regenerate_openai_house(cfg=config(root), provider=FakeOpenAIProvider(), store=store, root=root)
            old = (root / store.output_dir / f"{store.output_dir}生成图" / store.items[0].file_name).read_bytes()
            provider = FakeOpenAIProvider(first=old)
            result = await regenerate_openai_house(cfg=config(root), provider=provider, store=store, root=root)
            self.assertEqual(result["success"], 6)
            self.assertEqual(result["duplicate_retries"], 1)
            self.assertEqual(provider.calls, 7)
            manifest = ManifestStore(root).for_store(store.output_dir)
            self.assertEqual(manifest.entries["01"]["runtime_metrics"]["duplicate_retries"], 1)

    async def test_web_entrypoint_runs_only_house_store(self) -> None:
        from app.generation import runs as runs
        from app.web import server

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            cfg = config(root)
            old_state = server.STATE
            server.STATE = server.AppState()
            try:
                with patch.object(server, "load_config", return_value=cfg), \
                     patch.object(server, "create_provider", side_effect=lambda ignored: FakeOpenAIProvider()), \
                     patch.object(runs, "RUNS_FILE", root / "logs" / "runs.json"):
                    response = await server.api_openai_house_regenerate()
                    self.assertIn(b"OpenAI", response.body)
                    task = server.STATE.task
                    self.assertIsNotNone(task)
                    await task
                    generated = root / "01_房屋中介门店" / "01_房屋中介门店生成图"
                    effects = root / "01_房屋中介门店" / "01_房屋中介门店效果图"
                    self.assertEqual(len(list(generated.glob("*.png"))), 6)
                    self.assertEqual(len(list(effects.glob("*.png"))), 6)
                    self.assertFalse((root / "02_租房服务门店").exists())
                    self.assertEqual(runs.load_runs()[0]["success"], 6)
            finally:
                server.STATE = old_state

    async def test_stop_cancels_current_request_before_any_new_file_is_written(self) -> None:
        from app.web import server

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            cfg = config(root)
            provider = BlockingOpenAIProvider()
            old_state = server.STATE
            server.STATE = server.AppState()
            try:
                with patch.object(server, "load_config", return_value=cfg), \
                     patch.object(server, "create_provider", return_value=provider):
                    await server.api_openai_house_regenerate()
                    task = server.STATE.task
                    self.assertIsNotNone(task)
                    await asyncio.wait_for(provider.started.wait(), timeout=1)
                    await server.api_stop()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertEqual(provider.calls, 1)
                    self.assertFalse((root / "01_房屋中介门店").exists())
            finally:
                server.STATE = old_state

    async def test_history_restore_swaps_current_image_without_losing_it(self) -> None:
        from app.web import server

        store = StoreRepository().stores[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            cfg = config(root)
            await regenerate_openai_house(cfg=cfg, provider=FakeOpenAIProvider(), store=store, root=root)
            await regenerate_openai_house(cfg=cfg, provider=FakeOpenAIProvider(), store=store, root=root)
            generated_dir = root / store.output_dir / f"{store.output_dir}生成图"
            target = generated_dir / store.items[0].file_name
            before_restore = target.read_bytes()
            source = next((generated_dir / "_history").glob("01_楼房线稿__*.png"))
            expected = source.read_bytes()
            old_state = server.STATE
            server.STATE = server.AppState()
            try:
                with patch.object(server, "load_config", return_value=cfg):
                    response = await server.api_history_restore({
                        "store": store.output_dir, "file": store.items[0].file_name,
                        "history_file": source.name,
                    })
                self.assertIn(b'"ok":true', response.body)
                self.assertEqual(target.read_bytes(), expected)
                history_images = list((generated_dir / "_history").glob("01_楼房线稿__*.png"))
                self.assertTrue(any(path.read_bytes() == before_restore for path in history_images))
            finally:
                server.STATE = old_state


if __name__ == "__main__":
    unittest.main()
