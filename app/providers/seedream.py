# -*- coding: utf-8 -*-
"""火山方舟 Seedream 图片生成与参考图编辑适配器。"""
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


class SeedreamProvider(BaseProvider):
    """调用火山方舟 ``/api/v3/images/generations``。

    Seedream 的 API 使用 JSON ``image`` 字段接收 URL 或 Base64 data URL，
    因此 1–3 张项目参考图可直接在同一请求内传入。输出会统一转换为 PNG，
    保持本项目磁盘文件与检验规则一致。
    """

    name = "seedream"
    label = "字节 Seedream"
    supports_negative = False
    supports_n = False
    max_n = 1
    rpm_limit = 0
    # 图生图：payload['image']，见 _validate_references
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
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    @staticmethod
    def _validate_references(values: list[object]) -> list[str]:
        if not 1 <= len(values) <= 3:
            raise ProviderError("Seedream 图像参考模式需要 1–3 张参考图", retryable=False, code="INVALID_REFERENCE")
        output: list[str] = []
        for value in values:
            data_url = str(value or "").strip()
            match = _DATA_URL.fullmatch(data_url)
            if not match:
                raise ProviderError("Seedream 参考图格式无效", retryable=False, code="INVALID_REFERENCE")
            try:
                raw = base64.b64decode(match.group(2), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ProviderError("Seedream 参考图无法解码", retryable=False, code="INVALID_REFERENCE") from exc
            if not raw or len(raw) > 10 * 1024 * 1024:
                raise ProviderError("Seedream 参考图为空或超过 10MB", retryable=False, code="INVALID_REFERENCE")
            output.append(data_url)
        return output

    @staticmethod
    def _message(response: Any) -> str:
        try:
            data = response.json()
            if isinstance(data, dict):
                err = data.get("error")
                if isinstance(err, dict):
                    return str(err.get("message") or err.get("code") or err)
                return str(data.get("message") or data)[:500]
        except Exception:
            pass
        return str(getattr(response, "text", ""))[:500]

    @classmethod
    def _raise_http(cls, response: Any) -> None:
        message = cls._message(response)
        low = message.lower()
        if response.status_code in (401, 403):
            raise ProviderError(f"火山方舟 API Key 无效或无 Seedream 权限：{message}", retryable=False, code="AUTH")
        if response.status_code == 429:
            raise ProviderError("Seedream 图像请求触发限流，请稍后重试", retryable=True, code="RATE_LIMIT")
        if any(word in low for word in ("balance", "quota", "arrearage", "余额", "欠费")):
            raise ProviderError(f"Seedream 额度或账单不可用：{message}", retryable=False, code="QUOTA")
        if any(word in low for word in ("safety", "policy", "sensitive", "审核")):
            raise ProviderError(f"Seedream 内容审核未通过：{message}", retryable=False, code="CONTENT_POLICY")
        if response.status_code in (400, 404, 422):
            raise ProviderError(f"Seedream 请求参数或模型不可用：{message}", retryable=False, code="INVALID_REQUEST")
        raise ProviderError(f"Seedream 图像接口错误 HTTP {response.status_code}：{message}", retryable=True, code=f"HTTP_{response.status_code}")

    @staticmethod
    def _png(raw: bytes) -> bytes:
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return raw
        try:
            from PIL import Image
            from io import BytesIO

            image = Image.open(BytesIO(raw)).convert("RGBA")
            buffer = BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("Seedream 返回的图片无法转换为 PNG", retryable=False, code="NON_PNG") from exc

    async def _download(self, url: str) -> bytes:
        client = await self._client_get()
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"Seedream 图片下载失败：{exc}", retryable=True, code="DOWNLOAD") from exc

    async def generate(self, req: GenerateRequest) -> GenerateResult:
        if not self.api_key:
            raise ProviderError("缺少火山方舟 API Key", retryable=False, code="NO_KEY")
        started = time.monotonic()
        references = list(req.extra.get("reference_images") or [])
        # 统一能力校验：本地报错，不发一次注定失败的付费请求
        _err = self.image_mode_error(
            str(req.extra.get("image_mode") or "text"), len(references))
        if _err:
            raise ProviderError(_err, retryable=False, code="UNSUPPORTED_IMAGE_MODE")
        ratio = _RATIOS.get(req.size.lower().replace("×", "x"), "1:1")
        # Seedream 的质量档使用 1K / 2K；方向与比例写入提示词以保留设置页选择。
        quality_size = "2K" if "4-5" in (self.model or "") else "1K"
        prompt = f"{req.prompt}\n\n画幅比例必须为 {ratio}，严格按该比例完成构图。"
        payload: dict[str, Any] = {
            "model": self.model or "doubao-seedream-4-0-250828",
            "prompt": prompt,
            "size": quality_size,
            "n": 1,
            "sequential_image_generation": "disabled",
            "stream": False,
            "response_format": "b64_json",
            "watermark": False,
        }
        if references:
            verified = self._validate_references(references)
            payload["image"] = verified[0] if len(verified) == 1 else verified
        client = await self._client_get()
        try:
            response = await client.post(f"{self.base_url.rstrip('/')}/images/generations", json=payload, headers=self._headers())
        except Exception as exc:  # noqa: BLE001
            if "timeout" in type(exc).__name__.lower():
                raise ProviderError("Seedream 图像请求超时", retryable=True, code="TIMEOUT") from exc
            raise ProviderError(f"无法连接 Seedream 图像接口：{type(exc).__name__}: {exc}", retryable=True, code="NETWORK") from exc
        if response.status_code >= 400:
            self._raise_http(response)
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("Seedream 图像接口返回非 JSON 数据", retryable=True, code="BAD_RESPONSE") from exc
        images: list[bytes] = []
        for item in body.get("data") or []:
            if not isinstance(item, dict):
                continue
            try:
                if item.get("b64_json"):
                    raw = base64.b64decode(str(item["b64_json"]), validate=True)
                elif item.get("url"):
                    raw = await self._download(str(item["url"]))
                else:
                    continue
            except (ValueError, binascii.Error) as exc:
                raise ProviderError("Seedream 返回的图片 Base64 无法解码", retryable=True, code="BAD_IMAGE") from exc
            images.append(self._png(raw))
        if not images:
            raise ProviderError("Seedream 响应中没有图片", retryable=True, code="NO_IMAGE")
        return GenerateResult(
            images=images,
            provider=self.name,
            model=payload["model"],
            elapsed=time.monotonic() - started,
            raw={"created": body.get("created"), "usage": body.get("usage") or {}, "image_mode": "image" if references else "text", "reference_count": len(references), "aspect_ratio": ratio, "quality_size": quality_size},
        )
