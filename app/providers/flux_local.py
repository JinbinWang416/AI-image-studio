# -*- coding: utf-8 -*-
"""FLUX.2 klein 4B 本机推理服务适配器。

该适配器只连接 `127.0.0.1:8189` 的项目内服务。它不携带 API Key，
不会把本地请求转发到云端；服务不可用、模型缺失、OOM、超时或返回非 PNG
均标为不可重试，防止六张验证任务做无意义重试。
"""
from __future__ import annotations

import base64
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from .base import BaseProvider, GenerateRequest, GenerateResult, ProviderError


class FluxLocalProvider(BaseProvider):
    name = "flux_local"
    label = "FLUX.2 klein 4B（本地验证）"
    supports_negative = False
    supports_n = False
    max_n = 1
    rpm_limit = 0
    # ⚠️ 本地验证流程只做**文生图**（payload 只有 prompt/size/seed）。
    #    之前没有声明，用户选图生图时参考图会被**静默丢弃** ——
    #    违反 AGENTS.md「图生图必须显式声明，不能静默降级」。
    #    现在声明为不支持，generate() 里会直接报 UNSUPPORTED_IMAGE_MODE。
    supports_image = False
    supports_multi_image = False
    max_references = 0
    price_per_image = 0.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.base_url = (self.base_url or "http://127.0.0.1:8189").rstrip("/")
        self.model = self.model or "FLUX.2-klein-4b"
        self._client: httpx.AsyncClient | None = None
        self._assert_loopback()

    def _assert_loopback(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ProviderError("FLUX 本地验证服务只允许连接 127.0.0.1:8189", retryable=False, code="LOCAL_ONLY")

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # 明确绕过系统代理，避免 localhost 被代理软件转发到网络。
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=4.0), trust_env=False)
        return self._client

    @staticmethod
    def _message(body: Any, fallback: str) -> str:
        if isinstance(body, dict):
            return str(body.get("message") or body.get("detail") or body.get("error") or fallback)
        return fallback

    async def health(self) -> dict:
        """检查真实本地服务状态，供设置页和验证入口使用。"""
        started = time.monotonic()
        try:
            client = await self._get_client()
            response = await client.get(f"{self.base_url}/health")
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {
                "ok": False,
                "status": "unavailable",
                "elapsed": round(time.monotonic() - started, 2),
                "message": "本地 FLUX 服务未启动。请先运行 tools/start_flux_local.ps1；如尚未安装模型，先运行 tools/setup_flux_local.ps1。",
                "detail": f"{type(exc).__name__}: {exc}",
            }

        elapsed = round(time.monotonic() - started, 2)
        if response.status_code != 200:
            return {
                "ok": False,
                "status": f"http_{response.status_code}",
                "elapsed": elapsed,
                "message": self._message(payload, f"本地服务返回 HTTP {response.status_code}"),
                "health": payload if isinstance(payload, dict) else {},
            }
        if not isinstance(payload, dict) or not payload.get("ready"):
            return {
                "ok": False,
                "status": "not_ready",
                "elapsed": elapsed,
                "message": self._message(payload, "模型尚未就绪。请检查模型权重和服务日志。"),
                "health": payload if isinstance(payload, dict) else {},
            }
        return {"ok": True, "status": "ready", "elapsed": elapsed, "message": "本地 FLUX 模型已就绪", "health": payload}

    async def generate(self, req: GenerateRequest) -> GenerateResult:
        # 🔴 图生图**显式拒绝**（P0 修复）
        #
        # 本地验证流程只做文生图（payload 只有 prompt/width/height/seed）。
        # 早先这里没有检查，用户选「图生图」时参考图会被**静默丢弃** ——
        # 违反 AGENTS.md「图生图必须显式声明，不能静默降级」。
        _refs = list(req.extra.get("reference_images") or [])
        _mode = str(req.extra.get("image_mode") or "text")
        _err = self.image_mode_error(_mode, len(_refs))
        if _err:
            raise ProviderError(_err, retryable=False, code="UNSUPPORTED_IMAGE_MODE")

        if req.negative_prompt:
            # 编排器会在能力声明处自动抑制；这里仍显式忽略以保证请求不外泄。
            pass
        if req.size != "1024x1024":
            raise ProviderError("FLUX 本地验证仅允许 1024×1024", retryable=False, code="INVALID_SIZE")
        if req.n != 1:
            raise ProviderError("FLUX 本地验证一次只能生成 1 张", retryable=False, code="INVALID_N")

        payload = {"prompt": req.prompt, "width": 1024, "height": 1024}
        if req.seed is not None:
            payload["seed"] = int(req.seed)
        started = time.monotonic()
        try:
            client = await self._get_client()
            response = await client.post(f"{self.base_url}/generate", json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError("本地 FLUX 推理超时；请检查服务日志和显存占用", retryable=False, code="TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise ProviderError("本地 FLUX 服务不可用；请启动 tools/start_flux_local.ps1", retryable=False, code="UNAVAILABLE") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("本地 FLUX 服务返回了非 JSON 响应", retryable=False, code="BAD_RESPONSE") from exc

        if response.status_code != 200:
            message = self._message(body, f"本地 FLUX 服务返回 HTTP {response.status_code}")
            code = str(body.get("code", "")) if isinstance(body, dict) else ""
            if response.status_code == 507 or code == "CUDA_OOM":
                message = "CUDA 显存不足（OOM）：请关闭占用 GPU 的程序后重启本地服务。"
            raise ProviderError(message, retryable=False, code=code or f"HTTP_{response.status_code}")

        if not isinstance(body, dict):
            raise ProviderError("本地 FLUX 服务响应格式错误", retryable=False, code="BAD_RESPONSE")
        encoded = body.get("image_base64")
        if not isinstance(encoded, str) or not encoded:
            raise ProviderError("本地 FLUX 服务没有返回 PNG 数据", retryable=False, code="NO_IMAGE")
        try:
            image = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ProviderError("本地 FLUX 服务返回的图片编码无效", retryable=False, code="BAD_IMAGE") from exc
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ProviderError("本地 FLUX 服务返回的不是 PNG 图片", retryable=False, code="NON_PNG")

        elapsed = float(body.get("elapsed_seconds") or time.monotonic() - started)
        raw = {
            "seed": body.get("seed"),
            "elapsed_seconds": round(elapsed, 3),
            "peak_vram_mib": body.get("peak_vram_mib"),
            "peak_vram_allocated_mib": body.get("peak_vram_allocated_mib"),
            "peak_vram_reserved_mib": body.get("peak_vram_reserved_mib"),
            "gpu": body.get("gpu", ""),
            "service_model": body.get("model", self.model),
            "width": body.get("width", 1024),
            "height": body.get("height", 1024),
        }
        return GenerateResult(images=[image], provider=self.name, model=str(body.get("model") or self.model), elapsed=elapsed, raw=raw)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
