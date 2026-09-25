# -*- coding: utf-8 -*-
"""官方 OpenAI Images API 适配器。

只使用 ``POST /v1/images/generations``。GPT Image 模型的响应固定带
base64 图片数据，因此无需依赖临时 URL；接口文档未定义 seed 参数，不能伪造
该参数来追求随机性。
"""
from __future__ import annotations

import base64
import binascii
import time
from typing import Any

from .base import BaseProvider, GenerateRequest, GenerateResult, ProviderError


class OpenAIImageProvider(BaseProvider):
    """调用 OpenAI GPT Image 的服务商。"""

    name = "openai"
    label = "OpenAI GPT Image"
    supports_negative = False
    supports_n = True
    max_n = 10
    rpm_limit = 0
    # 图生图：走 /images/edits（multipart，image[] 字段）
    # 多图生图未验证，保守声明为不支持，避免发出可能失败的请求
    supports_image = True
    supports_multi_image = False
    max_references = 1
    price_per_image = 0.0  # 实际账单由 OpenAI 平台按模型/质量结算，不能硬编码猜测值。

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._client = None

    async def _client_get(self):
        if self._client is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - requirements 已声明
                raise ProviderError(
                    "缺少 httpx，请执行 pip install -r requirements.txt",
                    retryable=False,
                    code="DEPENDENCY",
                ) from exc
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self, *, json_content: bool = True) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if json_content:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _reference_files(values: list[object]) -> list[tuple[str, tuple[str, bytes, str]]]:
        """把内存 data URL 转为 OpenAI edits 接口需要的 multipart image[]。"""
        import re

        if not 1 <= len(values) <= 3:
            raise ProviderError("OpenAI 图生图需要 1–3 张参考图", retryable=False, code="INVALID_REFERENCE")
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        pattern = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\s]+)$", re.I)
        extensions = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
        for index, value in enumerate(values, 1):
            match = pattern.fullmatch(str(value or "").strip())
            if not match:
                raise ProviderError("OpenAI 参考图格式无效", retryable=False, code="INVALID_REFERENCE")
            mime = match.group(1).lower()
            try:
                image = base64.b64decode(match.group(2), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ProviderError("OpenAI 参考图无法解码", retryable=False, code="INVALID_REFERENCE") from exc
            if not image or len(image) > 10 * 1024 * 1024:
                raise ProviderError("OpenAI 参考图为空或超过 10MB", retryable=False, code="INVALID_REFERENCE")
            files.append(("image[]", (f"reference-{index}.{extensions[mime]}", image, mime)))
        return files

    @staticmethod
    def _message(resp: Any) -> str:
        try:
            data = resp.json()
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict):
                return str(error.get("message") or error.get("code") or "")[:400]
            if isinstance(error, str):
                return error[:400]
        except Exception:  # noqa: BLE001 - 保留服务端正文作为兜底
            pass
        return (resp.text or "")[:400]

    @classmethod
    def _raise_http(cls, resp: Any) -> None:
        message = cls._message(resp) or f"HTTP {resp.status_code}"
        low = message.lower()
        if resp.status_code in (401, 403):
            raise ProviderError("OpenAI API Key 无效或没有图像模型权限", retryable=False, code="AUTH")
        if resp.status_code == 429:
            raise ProviderError("OpenAI 图像请求触发限流或额度限制，请稍后重试或检查账单", retryable=True, code="RATE_LIMIT")
        if any(token in low for token in ("content policy", "safety", "moderation")):
            raise ProviderError(f"OpenAI 内容审核未通过：{message}", retryable=False, code="CONTENT_POLICY")
        if resp.status_code in (400, 404, 422):
            raise ProviderError(f"OpenAI 请求参数或模型不可用：{message}", retryable=False, code="INVALID_REQUEST")
        raise ProviderError(f"OpenAI 图像接口错误 HTTP {resp.status_code}：{message}", retryable=True, code=f"HTTP_{resp.status_code}")

    async def generate(self, req: GenerateRequest) -> GenerateResult:
        if not self.api_key:
            raise ProviderError("缺少 OpenAI API Key", retryable=False, code="NO_KEY")
        started = time.monotonic()
        client = await self._client_get()
        model = self.model or "gpt-image-2.5-flare"
        payload: dict[str, Any] = {
            "model": model,
            "prompt": req.prompt,
            "size": req.size,
            "n": self.clamp_n(req.n),
            "quality": str(req.extra.get("quality") or "max"),
            "output_format": "png",
            "background": "opaque",
        }
        # OpenAI Images API 没有公开的 seed 参数；特意不把 req.seed 送出。

        references = list(req.extra.get("reference_images") or [])
        # 统一能力校验：本地报错，不发一次注定失败的付费请求
        _err = self.image_mode_error(
            str(req.extra.get("image_mode") or "text"), len(references))
        if _err:
            raise ProviderError(_err, retryable=False, code="UNSUPPORTED_IMAGE_MODE")
        try:
            if references:
                # OpenAI Images edits uses multipart; each source is an image[] field.
                # Keep all output choices explicit so edit and generation produce the
                # same opaque PNG sticker files.
                form = {key: str(value) for key, value in payload.items()}
                response = await client.post(
                    f"{self.base_url.rstrip('/')}/images/edits",
                    data=form,
                    files=self._reference_files(references),
                    headers=self._headers(json_content=False),
                )
            else:
                response = await client.post(
                    f"{self.base_url.rstrip('/')}/images/generations",
                    json=payload,
                    headers=self._headers(),
                )
        except Exception as exc:  # httpx 可选依赖，避免把其类暴露为硬依赖
            name = type(exc).__name__.lower()
            if "timeout" in name:
                raise ProviderError("OpenAI 图像请求超时", retryable=True, code="TIMEOUT") from exc
            raise ProviderError(f"无法连接 OpenAI 图像接口：{type(exc).__name__}: {exc}", retryable=True, code="NETWORK") from exc

        if response.status_code >= 400:
            self._raise_http(response)

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("OpenAI 图像接口返回非 JSON 数据", retryable=True, code="BAD_RESPONSE") from exc

        images: list[bytes] = []
        revised_prompts: list[str] = []
        for item in body.get("data") or []:
            if not isinstance(item, dict) or not item.get("b64_json"):
                continue
            try:
                data = base64.b64decode(item["b64_json"], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ProviderError("OpenAI 返回的图片 Base64 无法解码", retryable=True, code="BAD_IMAGE") from exc
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ProviderError("OpenAI 返回的图片不是 PNG", retryable=False, code="NON_PNG")
            images.append(data)
            if item.get("revised_prompt"):
                revised_prompts.append(str(item["revised_prompt"]))

        if not images:
            raise ProviderError("OpenAI 响应中没有 PNG 图片", retryable=True, code="NO_IMAGE")

        return GenerateResult(
            images=images,
            provider=self.name,
            model=model,
            elapsed=time.monotonic() - started,
            raw={
                "created": body.get("created"),
                "usage": body.get("usage") or {},
                "revised_prompts": revised_prompts,
                "quality": payload["quality"],
                "output_format": "png",
                "image_mode": "image" if references else "text",
                "reference_count": len(references),
            },
        )
