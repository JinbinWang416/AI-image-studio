# -*- coding: utf-8 -*-
"""Google Gemini 原生图片生成与参考图编辑适配器。"""
from __future__ import annotations

import base64
import binascii
import re
import time
from typing import Any

from .base import BaseProvider, GenerateRequest, GenerateResult, ProviderError


_DATA_URL = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\s]+)$", re.I)
_RATIOS = {
    "1024x1024": "1:1",
    "1024x1280": "4:5",
    "1024x1536": "2:3",
    "1280x1024": "5:4",
    "1536x1024": "3:2",
}


class GeminiImageProvider(BaseProvider):
    """调用 Gemini Interactions API。

    文生图与 1–3 张参考图编辑共用 ``/interactions``；参考图在请求内以
    ``type=image``、Base64 与 MIME 类型传递，因此无需把本地文件上传到第三方。
    """

    name = "gemini"
    label = "Google Gemini"
    supports_negative = False
    supports_n = False
    max_n = 1
    rpm_limit = 0
    # 图生图：参考图拼进 inputs（见 _reference_inputs）
    supports_image = True
    supports_multi_image = True
    max_references = 3
    price_per_image = 0.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = None

    async def _client_get(self):
        if self._client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover
                raise ProviderError("缺少 httpx，请执行 pip install -r requirements.txt", retryable=False) from exc
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    @staticmethod
    def _reference_inputs(values: list[object]) -> list[dict[str, str]]:
        if not 1 <= len(values) <= 3:
            raise ProviderError("Gemini 图像参考模式需要 1–3 张参考图", retryable=False, code="INVALID_REFERENCE")
        parts: list[dict[str, str]] = []
        for value in values:
            match = _DATA_URL.fullmatch(str(value or "").strip())
            if not match:
                raise ProviderError("Gemini 参考图格式无效", retryable=False, code="INVALID_REFERENCE")
            try:
                data = base64.b64decode(match.group(2), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ProviderError("Gemini 参考图无法解码", retryable=False, code="INVALID_REFERENCE") from exc
            if not data or len(data) > 10 * 1024 * 1024:
                raise ProviderError("Gemini 参考图为空或超过 10MB", retryable=False, code="INVALID_REFERENCE")
            parts.append({"type": "image", "mime_type": match.group(1).lower(), "data": base64.b64encode(data).decode("ascii")})
        return parts

    @staticmethod
    def _message(response: Any) -> str:
        try:
            body = response.json()
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict):
                    return str(error.get("message") or error)
                return str(body.get("message") or body)[:500]
        except Exception:
            pass
        return str(getattr(response, "text", ""))[:500]

    @classmethod
    def _raise_http(cls, response: Any) -> None:
        message = cls._message(response)
        low = message.lower()
        if response.status_code in (401, 403):
            raise ProviderError(f"Gemini API Key 无效或无图像权限：{message}", retryable=False, code="AUTH")
        if response.status_code == 429:
            raise ProviderError("Gemini 图像请求触发限流或额度限制，请稍后重试或检查账单", retryable=True, code="RATE_LIMIT")
        if any(token in low for token in ("safety", "policy", "blocked")):
            raise ProviderError(f"Gemini 内容审核未通过：{message}", retryable=False, code="CONTENT_POLICY")
        if response.status_code in (400, 404, 422):
            raise ProviderError(f"Gemini 请求参数或模型不可用：{message}", retryable=False, code="INVALID_REQUEST")
        raise ProviderError(f"Gemini 图像接口错误 HTTP {response.status_code}：{message}", retryable=True, code=f"HTTP_{response.status_code}")

    @staticmethod
    def _images(body: dict) -> list[bytes]:
        encoded: list[str] = []
        output = body.get("output_image")
        if isinstance(output, dict) and output.get("data"):
            encoded.append(str(output["data"]))
        for step in body.get("steps") or []:
            if not isinstance(step, dict):
                continue
            for block in step.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "image" and block.get("data"):
                    encoded.append(str(block["data"]))
        images: list[bytes] = []
        for value in encoded:
            try:
                raw = base64.b64decode(value, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ProviderError("Gemini 返回的图片 Base64 无法解码", retryable=True, code="BAD_IMAGE") from exc
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ProviderError("Gemini 返回的图片不是 PNG", retryable=False, code="NON_PNG")
            images.append(raw)
        return images

    async def generate(self, req: GenerateRequest) -> GenerateResult:
        if not self.api_key:
            raise ProviderError("缺少 Gemini API Key", retryable=False, code="NO_KEY")
        started = time.monotonic()
        references = list(req.extra.get("reference_images") or [])
        # 统一能力校验：本地报错，不发一次注定失败的付费请求
        _err = self.image_mode_error(
            str(req.extra.get("image_mode") or "text"), len(references))
        if _err:
            raise ProviderError(_err, retryable=False, code="UNSUPPORTED_IMAGE_MODE")
        inputs: list[dict[str, str]] = [{"type": "text", "text": req.prompt}]
        if references:
            inputs.extend(self._reference_inputs(references))
        ratio = _RATIOS.get(req.size.lower().replace("×", "x"), "1:1")
        payload: dict[str, Any] = {
            "model": self.model or "gemini-3.1-flash-image",
            "input": inputs,
            "response_format": {"type": "image", "mime_type": "image/png", "aspect_ratio": ratio, "image_size": "1K"},
        }
        client = await self._client_get()
        try:
            response = await client.post(f"{self.base_url.rstrip('/')}/interactions", json=payload, headers=self._headers())
        except Exception as exc:  # noqa: BLE001
            if "timeout" in type(exc).__name__.lower():
                raise ProviderError("Gemini 图像请求超时", retryable=True, code="TIMEOUT") from exc
            raise ProviderError(f"无法连接 Gemini 图像接口：{type(exc).__name__}: {exc}", retryable=True, code="NETWORK") from exc
        if response.status_code >= 400:
            self._raise_http(response)
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("Gemini 图像接口返回非 JSON 数据", retryable=True, code="BAD_RESPONSE") from exc
        images = self._images(body)
        if not images:
            raise ProviderError("Gemini 响应中没有 PNG 图片", retryable=True, code="NO_IMAGE")
        return GenerateResult(
            images=images,
            provider=self.name,
            model=payload["model"],
            elapsed=time.monotonic() - started,
            raw={"id": body.get("id"), "usage": body.get("usage") or {}, "image_mode": "image" if references else "text", "reference_count": len(references), "aspect_ratio": ratio},
        )
