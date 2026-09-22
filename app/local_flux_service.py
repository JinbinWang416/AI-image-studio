# -*- coding: utf-8 -*-
"""只监听回环地址的 FLUX.2 klein 4B 离线推理服务。

运行环境由 ``tools/setup_flux_local.ps1`` 安装在
``E:\\AIModels\\flux2-klein-4b\\.venv``。这个文件有意只使用官方
``flux2`` 核心加载和采样接口：不调用 CLI、不加载提示词扩写模型、不接受
参考图，也不在本地权重缺失时联网下载。
"""
from __future__ import annotations

import asyncio
import base64
import gc
import io
import json
import os
import subprocess
import sys
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# 必须在 transformers / huggingface_hub 导入前强制离线。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from PIL import Image


MODEL_NAME = "flux.2-klein-4b"
DISPLAY_MODEL = "FLUX.2-klein-4b"
DEFAULT_HOME = Path(r"E:\AIModels\flux2-klein-4b")
MODEL_HOME = Path(os.environ.get("FLUX_LOCAL_HOME", str(DEFAULT_HOME))).resolve()
SOURCE_DIR = Path(os.environ.get("FLUX_LOCAL_SOURCE", str(MODEL_HOME / "source"))).resolve()
WEIGHTS_DIR = Path(os.environ.get("FLUX_LOCAL_WEIGHTS", str(MODEL_HOME / "weights"))).resolve()
MODEL_PATH = Path(os.environ.get("KLEIN_4B_MODEL_PATH", str(WEIGHTS_DIR / "flux-2-klein-4b.safetensors"))).resolve()
AE_PATH = Path(os.environ.get("AE_MODEL_PATH", str(WEIGHTS_DIR / "ae.safetensors"))).resolve()
MANIFEST_PATH = MODEL_HOME / "model_manifest.json"

if str(SOURCE_DIR / "src") not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR / "src"))


def _driver_version() -> str:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return out.strip().splitlines()[0]
    except Exception:  # noqa: BLE001 - diagnostics must never fail health
        return ""


class FluxRuntime:
    """持久化模型和单并发 CPU/GPU 卸载推理。"""

    def __init__(self) -> None:
        self.status = "not_loaded"
        self.error = ""
        self.model: Any = None
        self.ae: Any = None
        self.text_encoder: Any = None
        self.torch: Any = None
        self._sampling: dict[str, Any] = {}
        self.loaded_at = ""
        self.gpu_name = ""
        self.manifest: dict[str, Any] = {}

    def _require_files(self) -> None:
        missing = [str(p) for p in (MODEL_PATH, AE_PATH) if not p.is_file()]
        if missing:
            raise RuntimeError(
                "模型权重缺失：" + "；".join(missing) + "。请先运行 tools/setup_flux_local.ps1。"
            )
        if not (SOURCE_DIR / "src" / "flux2" / "util.py").is_file():
            raise RuntimeError(f"官方 FLUX 源码不存在：{SOURCE_DIR}。请先运行 tools/setup_flux_local.ps1。")

    def load(self) -> None:
        if self.status == "ready":
            return
        self.status = "loading"
        self.error = ""
        try:
            self._require_files()
            os.environ["KLEIN_4B_MODEL_PATH"] = str(MODEL_PATH)
            os.environ["AE_MODEL_PATH"] = str(AE_PATH)

            import torch
            from flux2.sampling import batched_prc_img, batched_prc_txt, denoise, get_schedule, scatter_ids
            from flux2.util import load_ae, load_flow_model, load_text_encoder

            if not torch.cuda.is_available():
                raise RuntimeError("未检测到可用 CUDA GPU。FLUX 本地验证需要 NVIDIA CUDA GPU。")

            # 所有大模型常驻系统内存；每次请求只将当前阶段的一个模型放上 GPU。
            # 这是 8GB 显存场景的保守模式，避免同时驻留 Qwen 编码器、Flow 和 VAE。
            self.torch = torch
            # Qwen3 FP8 initializes CUDA/Triton kernels on Windows. It is released
            # to CPU immediately after loading and moved back only for embedding.
            self.text_encoder = load_text_encoder(MODEL_NAME, device="cuda").eval().cpu()
            torch.cuda.empty_cache()
            self.model = load_flow_model(MODEL_NAME, device="cpu").eval()
            self.ae = load_ae(MODEL_NAME, device="cpu").eval()
            self._sampling = {
                "batched_prc_img": batched_prc_img,
                "batched_prc_txt": batched_prc_txt,
                "denoise": denoise,
                "get_schedule": get_schedule,
                "scatter_ids": scatter_ids,
            }
            self.gpu_name = torch.cuda.get_device_name(0)
            if MANIFEST_PATH.exists():
                self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            self.loaded_at = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.status = "ready"
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.error = f"{type(exc).__name__}: {exc}"
            self.model = self.ae = self.text_encoder = None
            raise

    def _release_gpu(self, obj: Any) -> None:
        if obj is not None:
            obj.cpu()
        self.torch.cuda.empty_cache()

    def generate(self, prompt: str, seed: int, width: int, height: int) -> tuple[bytes, dict[str, Any]]:
        if self.status != "ready":
            raise RuntimeError(self.error or "模型尚未就绪")
        if width != 1024 or height != 1024:
            raise ValueError("仅支持 1024×1024")
        if not prompt.strip():
            raise ValueError("prompt 不能为空")

        torch = self.torch
        started = time.monotonic()
        torch.cuda.reset_peak_memory_stats()
        try:
            with torch.inference_mode():
                # FP8 encoder, Flow model and VAE take turns on GPU; they never
                # co-reside, leaving room for the RTX 4070 Laptop's 8GB VRAM.
                self.text_encoder = self.text_encoder.to("cuda")
                ctx = self.text_encoder([prompt]).to(device="cuda", dtype=torch.bfloat16)
                self._release_gpu(self.text_encoder)
                ctx, ctx_ids = self._sampling["batched_prc_txt"](ctx)

                self.model = self.model.to("cuda")
                shape = (1, 128, height // 16, width // 16)
                generator = torch.Generator(device="cuda").manual_seed(seed)
                noise = torch.randn(shape, generator=generator, dtype=torch.bfloat16, device="cuda")
                x, x_ids = self._sampling["batched_prc_img"](noise)
                timesteps = self._sampling["get_schedule"](4, x.shape[1])
                x = self._sampling["denoise"](
                    self.model, x, x_ids, ctx, ctx_ids, timesteps=timesteps,
                    guidance=1.0, img_cond_seq=None, img_cond_seq_ids=None,
                )
                self._release_gpu(self.model)
                del ctx, ctx_ids, noise
                torch.cuda.empty_cache()

                x = torch.cat(self._sampling["scatter_ids"](x, x_ids)).squeeze(2)
                self.ae = self.ae.to("cuda")
                decoded = self.ae.decode(x).float().clamp(-1, 1)
                self._release_gpu(self.ae)
                image_data = (
                    (decoded[0].permute(1, 2, 0) + 1.0) * 127.5
                ).clamp(0, 255).to(torch.uint8).cpu().numpy()
                image = Image.fromarray(image_data, "RGB")
                buffer = io.BytesIO()
                image.save(buffer, format="PNG", optimize=False)
                elapsed = round(time.monotonic() - started, 3)
                peak_allocated_mib = round(torch.cuda.max_memory_allocated() / 1024 / 1024, 1)
                peak_reserved_mib = round(torch.cuda.max_memory_reserved() / 1024 / 1024, 1)
                return buffer.getvalue(), {
                    "seed": seed,
                    "elapsed_seconds": elapsed,
                    # Allocated memory is the comparable GPU-memory peak.  PyTorch's
                    # caching allocator may reserve more than the physical board total.
                    "peak_vram_mib": peak_allocated_mib,
                    "peak_vram_allocated_mib": peak_allocated_mib,
                    "peak_vram_reserved_mib": peak_reserved_mib,
                    "gpu": self.gpu_name,
                    "model": DISPLAY_MODEL,
                    "width": width,
                    "height": height,
                }
        except torch.cuda.OutOfMemoryError:
            self._release_gpu(self.model)
            self._release_gpu(self.ae)
            gc.collect()
            raise
        finally:
            # `x` may be absent when model loading or text encoding fails.
            gc.collect()

    def health(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ready": self.status == "ready",
            "status": self.status,
            "model": DISPLAY_MODEL,
            "model_key": MODEL_NAME,
            "model_home": str(MODEL_HOME),
            "model_path": str(MODEL_PATH),
            "ae_path": str(AE_PATH),
            "cpu_offload": True,
            "prompt_upsampling": False,
            "image_to_image": False,
            "remote_fallback": False,
            "last_error": self.error,
            "loaded_at": self.loaded_at,
            "driver": _driver_version(),
        }
        if self.torch is not None and self.torch.cuda.is_available():
            props = self.torch.cuda.get_device_properties(0)
            data.update({
                "gpu": self.gpu_name,
                "vram_total_mib": round(props.total_memory / 1024 / 1024, 1),
                "vram_used_mib": round(self.torch.cuda.memory_allocated() / 1024 / 1024, 1),
                "torch": self.torch.__version__,
                "cuda_runtime": self.torch.version.cuda,
            })
        if self.manifest:
            data["model_manifest"] = {
                "source_commit": self.manifest.get("source_commit", ""),
                "created_at": self.manifest.get("created_at", ""),
                "weight_files": self.manifest.get("weight_files", []),
            }
        return data


RUNTIME = FluxRuntime()
GENERATION_LOCK = asyncio.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        await asyncio.to_thread(RUNTIME.load)
    except Exception:  # Keep process alive so /health can expose the actionable error.
        traceback.print_exc()
    yield


app = FastAPI(
    title="FLUX.2 klein 4B local validation",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> JSONResponse:
    payload = RUNTIME.health()
    return JSONResponse(payload, status_code=200 if payload["ready"] else 503)


@app.post("/generate")
async def generate(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求必须是 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="请求 JSON 必须是对象")
    allowed = {"prompt", "seed", "width", "height"}
    unexpected = sorted(set(payload) - allowed)
    if unexpected:
        raise HTTPException(status_code=400, detail=f"不支持字段：{', '.join(unexpected)}。本服务只接受纯文本生成。")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=422, detail="prompt 不能为空")
    if len(prompt) > 20000:
        raise HTTPException(status_code=422, detail="prompt 超过 20000 字符")
    width, height = payload.get("width", 1024), payload.get("height", 1024)
    if width != 1024 or height != 1024:
        raise HTTPException(status_code=422, detail="本地验证固定为 1024×1024")
    raw_seed = payload.get("seed")
    if raw_seed is None:
        seed = int.from_bytes(os.urandom(4), "big")
    elif isinstance(raw_seed, int) and 0 <= raw_seed < 2**32:
        seed = raw_seed
    else:
        raise HTTPException(status_code=422, detail="seed 必须是 0 至 2^32-1 的整数")
    if RUNTIME.status != "ready":
        raise HTTPException(status_code=503, detail=RUNTIME.error or "模型尚未就绪")

    async with GENERATION_LOCK:
        try:
            png, metadata = await asyncio.to_thread(RUNTIME.generate, prompt, seed, width, height)
        except Exception as exc:  # noqa: BLE001
            if RUNTIME.torch is not None and isinstance(exc, RUNTIME.torch.cuda.OutOfMemoryError):
                return JSONResponse({"code": "CUDA_OOM", "message": "CUDA 显存不足；请关闭占用 GPU 的程序后重启服务。"}, status_code=507)
            return JSONResponse({"code": "GENERATION_FAILED", "message": f"{type(exc).__name__}: {exc}"}, status_code=500)
    return JSONResponse({"image_base64": base64.b64encode(png).decode("ascii"), **metadata})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8189, log_level="info")
