# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from app.providers.base import GenerateResult


def white_png() -> bytes:
    image = Image.new("RGB", (1024, 1024), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class FakeFluxProvider:
    name = "flux_local"
    model = "FLUX.2-klein-4b"
    supports_negative = False
    supports_n = False
    max_n = 1
    rpm_limit = 0
    price_per_image = 0.0

    async def health(self):
        return {"ok": True, "status": "ready", "message": "ready", "health": {"gpu": "test"}}

    async def generate(self, req):
        return GenerateResult(
            images=[white_png()], provider=self.name, model=self.model, elapsed=0.01,
            raw={"seed": 7, "peak_vram_mib": 123.0, "elapsed_seconds": 0.01},
        )

    def describe(self):
        return "FLUX.2 klein 4B（本地验证）"

    async def close(self):
        return None


class LocalValidationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_scope_isolated_output_and_manifest(self) -> None:
        from app.web import server
        from app import runs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output_local_validation"
            old_state = server.STATE
            server.STATE = server.AppState()
            try:
                with patch.object(server, "LOCAL_VALIDATION_ROOT", root), \
                     patch.object(server, "create_provider", side_effect=lambda cfg: FakeFluxProvider()), \
                     patch.object(runs, "RUNS_FILE", root / "logs" / "runs.json"):
                    response = await server.api_local_validation_run()
                    self.assertTrue(response.body)
                    await server.STATE.task
                    store_dir = root / "01_房屋中介门店"
                    generated_dir = store_dir / "01_房屋中介门店生成图"
                    effect_dir = store_dir / "01_房屋中介门店效果图"
                    self.assertEqual(len(list(generated_dir.glob("*.png"))), 6)
                    self.assertEqual(len(list(effect_dir.glob("*.png"))), 6)
                    self.assertTrue((store_dir / "_manifest.json").exists())
                    self.assertTrue((root / "_local_validation_report.json").exists())
                    self.assertTrue((root / "_人工评分表.csv").exists())
                    self.assertFalse((Path("output") / "01_房屋中介门店").resolve() == store_dir.resolve())
                    report = (root / "_local_validation_report.json").read_text(encoding="utf-8")
                    self.assertIn('"technical_pass": true', report)
                    history = runs.load_runs()
                    self.assertEqual(len(history), 1)
                    self.assertEqual(history[0]["scope"], "本地验证：门店 01 / V8 / 6 张 / 单并发")
            finally:
                server.STATE = old_state


if __name__ == "__main__":
    unittest.main()
