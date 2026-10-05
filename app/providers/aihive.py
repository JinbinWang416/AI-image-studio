# -*- coding: utf-8 -*-
"""AI Hive OpenAPI 图片生成适配器。

端点**逐字对齐** `nano-banana-pro` skill 的 `imagegen.py`（AiHiveClient）：

    base   : {base}/openapi/v1/...
    上传   : POST media/upload-token → PUT(OSS) → POST media/{id}/complete
    生成   : POST generation/image                → taskId
    轮询   : GET  generation/tasks/{taskId}

默认模型 `public_model_nano_banana_pro`，路由 `COST_FIRST`。
支持文生图与图生图（参考图自动走上传三步转成 mediaId）。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import re
import time
from typing import Any

from .base import BaseProvider, GenerateRequest, GenerateResult, ProviderError

_DEFAULT_BASE_URL = "https://ai-hive.iclip.cn/api"
_DEFAULT_MODEL = "public_model_nano_banana_pro"
_ROUTING = "COST_FIRST"
_POLL_INTERVAL = 3.0
_POLL_TIMEOUT = 1200.0
_DATA_URL_RE = re.compile(
    r"^data:image/[a-zA-Z0-9.+-]+;base64,([A-Za-z0-9+/=\s]+)$"
)


class AiHiveProvider(BaseProvider):
    """调用 AI Hive OpenAPI 的服务商。"""

    name = "aihive"
    label = "AI Hive（香蕉 Pro）"
    supports_negative = False
    supports_n = False
    max_n = 1
    rpm_limit = 0
    # 图生图：参考图自动上传为 mediaId 后传入 generation/image
    supports_image = True
    supports_multi_image = True
    max_references = 4
    price_per_image = 0.0  # 账单以 AI Hive 平台为准，不硬编码猜测值

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if not self.base_url:
            self.base_url = _DEFAULT_BASE_URL
        self.base_url = self.base_url.rstrip("/")
        self.model = self.model or _DEFAULT_MODEL
        self._client = None
        self._pricing_cache: dict | None = None

    # ------------------------------------------------------------ httpx
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
            self._client = httpx.AsyncClient(
                timeout=self.timeout, headers=self._headers()
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}/openapi/v1/{path}"

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------ pricing
    async def _pricing_snapshot(self) -> dict:
        """从模型列表取得当前模型的 pricingSnapshot（路由 COST_FIRST）。"""
        if self._pricing_cache is not None:
            return self._pricing_cache
        client = await self._client_get()
        try:
            resp = await client.get(self._url("models"), params={"modelType": "IMAGE"})
        except Exception as exc:
            raise ProviderError(
                f"AI Hive 模型列表获取失败：{exc}", retryable=True, code="NETWORK"
            ) from exc
        if resp.status_code >= 400:
            self._raise_http(resp)
        try:
            payload = resp.json()
        except Exception as exc:
            raise ProviderError(
                "AI Hive 模型列表返回非 JSON", retryable=True, code="BAD_RESPONSE"
            ) from exc

        models = payload
        if isinstance(payload, dict):
            models = payload.get("data") or payload.get("models") or []
        entry = next(
            (m for m in models if m.get("publicModelId") == self.model), None
        )
        if entry is None:
            raise ProviderError(
                f"AI Hive 未找到模型 {self.model}（IMAGE 类型下）",
                retryable=False,
                code="MODEL_NOT_FOUND",
            )
        snapshots = entry.get("pricingSnapshot") or []
        snap = next(
            (s for s in snapshots if s.get("routingMode") == _ROUTING), None
        )
        if snap is None and snapshots:
            snap = snapshots[0]
        if snap is None:
            raise ProviderError(
                f"AI Hive 模型 {self.model} 无 pricingSnapshot（路由 {_ROUTING}）",
                retryable=False,
                code="NO_PRICING",
            )
        self._pricing_cache = snap
        return snap

    # ------------------------------------------------------------ 上传参考图
    @staticmethod
    def _decode_reference(value: str) -> bytes:
        """把参考图转成原始字节：支持 data URL 与本地文件路径。"""
        if value.startswith("data:"):
            match = _DATA_URL_RE.fullmatch(value.strip())
            if not match:
                raise ProviderError(
                    "AI Hive 参考图格式无效（需 data URL 或本地路径）",
                    retryable=False,
                    code="INVALID_REFERENCE",
                )
            try:
                return base64.b64decode(match.group(1), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ProviderError(
                    "AI Hive 参考图无法解码", retryable=False, code="INVALID_REFERENCE"
                ) from exc
        if value.startswith("http://") or value.startswith("https://"):
            raise ProviderError(
                "AI Hive 暂不支持远程参考图 URL，请使用本地文件或上传参考图",
                retryable=False,
                code="INVALID_REFERENCE",
            )
        from pathlib import Path

        p = Path(value)
        if not p.is_file():
            raise ProviderError(
                f"AI Hive 参考图文件不存在：{value}",
                retryable=False,
                code="INVALID_REFERENCE",
            )
        return p.read_bytes()

    async def _create_upload_token(
        self, filename: str, content_type: str, size: int
    ) -> dict:
        client = await self._client_get()
        body = {"filename": filename, "contentType": content_type, "sizeBytes": size}
        try:
            resp = await client.post(self._url("media/upload-token"), json=body)
        except Exception as exc:
            raise ProviderError(
                f"AI Hive 上传凭证获取失败：{exc}", retryable=True, code="NETWORK"
            ) from exc
        if resp.status_code >= 400:
            self._raise_http(resp)
        return resp.json()

    async def _upload_reference(self, data: bytes) -> str:
        """三步上传：upload-token → PUT(OSS) → complete。返回 mediaId。"""
        client = await self._client_get()
        token = await self._create_upload_token("reference.png", "image/png", len(data))
        media_id = token["mediaId"]
        upload = token.get("upload") or {}
        url = upload.get("url")
        method = str(upload.get("method", "PUT")).upper()
        up_headers = dict(upload.get("headers") or {})
        try:
            if method == "PUT":
                resp = await client.put(url, headers=up_headers, content=data)
            else:
                resp = await client.request(
                    method, url, headers=up_headers, content=data
                )
        except Exception as exc:
            raise ProviderError(
                f"AI Hive 参考图上传失败：{exc}", retryable=True, code="NETWORK"
            ) from exc
        if not resp.is_success:
            raise ProviderError(
                f"AI Hive 参考图上传失败 HTTP {resp.status_code}",
                retryable=True,
                code="UPLOAD_FAILED",
            )
        try:
            cresp = await client.post(self._url(f"media/{media_id}/complete"))
        except Exception as exc:
            raise ProviderError(
                f"AI Hive 上传确认失败：{exc}", retryable=True, code="NETWORK"
            ) from exc
        if cresp.status_code >= 400:
            self._raise_http(cresp)
        return media_id

    # ------------------------------------------------------------ 生成
    async def generate(self, req: GenerateRequest) -> GenerateResult:
        if not self.api_key:
            raise ProviderError(
                "缺少 AI Hive API Key（在设置 → 模型 API 配置填写）",
                retryable=False,
                code="NO_KEY",
            )
        started = time.monotonic()
        references = list(req.extra.get("reference_images") or [])
        mode = str(req.extra.get("image_mode") or "text")

        # 统一的图生图能力校验：本地报错，不静默丢弃参考图
        _err = self.image_mode_error(mode, len(references))
        if _err:
            raise ProviderError(_err, retryable=False, code="UNSUPPORTED_IMAGE_MODE")

        pricing = await self._pricing_snapshot()

        media_ids: list[str] = []
        if references:
            for ref in references[: self.max_references]:
                data = self._decode_reference(ref)
                media_ids.append(await self._upload_reference(data))

        body: dict[str, Any] = {
            "publicModelId": self.model,
            "routingMode": _ROUTING,
            "prompt": req.prompt,
            "batchSize": 1,
            "imageMediaIds": media_ids,
            "params": {},
            "pricingSnapshot": pricing,
        }
        client = await self._client_get()
        try:
            resp = await client.post(self._url("generation/image"), json=body)
        except Exception as exc:
            raise ProviderError(
                f"AI Hive 图像请求失败：{exc}", retryable=True, code="NETWORK"
            ) from exc
        if resp.status_code >= 400:
            self._raise_http(resp)
        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderError(
                "AI Hive 返回非 JSON", retryable=True, code="BAD_RESPONSE"
            ) from exc
        task_id = data.get("taskId")
        if not task_id:
            raise ProviderError(
                f"AI Hive 未返回 taskId：{data}", retryable=True, code="NO_TASK"
            )

        items = await self._poll_task(client, task_id)
        images: list[bytes] = []
        for item in items:
            url = item.get("resultUrl")
            if not url:
                continue
            try:
                dresp = await client.get(url)
            except Exception:
                continue
            if dresp.is_success:
                images.append(dresp.content)
        if not images:
            raise ProviderError(
                "AI Hive 任务完成但没有可用图片", retryable=True, code="NO_IMAGE"
            )
        return GenerateResult(
            images=images,
            provider=self.name,
            model=self.model,
            elapsed=time.monotonic() - started,
            raw={
                "taskId": task_id,
                "image_mode": mode,
                "reference_count": len(media_ids),
            },
        )

    async def _poll_task(self, client, task_id: str) -> list[dict]:
        deadline = time.monotonic() + _POLL_TIMEOUT
        while time.monotonic() < deadline:
            try:
                resp = await client.get(
                    self._url(f"generation/tasks/{task_id}")
                )
            except Exception as exc:
                raise ProviderError(
                    f"AI Hive 任务轮询失败：{exc}", retryable=True, code="NETWORK"
                ) from exc
            if resp.status_code >= 400:
                self._raise_http(resp)
            try:
                task = resp.json()
            except Exception as exc:
                raise ProviderError(
                    "AI Hive 任务返回非 JSON", retryable=True, code="BAD_RESPONSE"
                ) from exc
            items = task.get("items") or []
            all_done = True
            for it in items:
                if it.get("status") not in ("COMPLETED", "FAILED"):
                    all_done = False
            if all_done:
                failed = [it for it in items if it.get("status") == "FAILED"]
                if failed:
                    msgs = "; ".join(
                        str(it.get("errorMessage") or "") for it in failed
                    )
                    raise ProviderError(
                        f"AI Hive 生成失败：{msgs}",
                        retryable=False,
                        code="GEN_FAILED",
                    )
                return items
            await asyncio.sleep(_POLL_INTERVAL)
        raise ProviderError(
            f"AI Hive 任务轮询超时（{int(_POLL_TIMEOUT)}s）",
            retryable=True,
            code="POLL_TIMEOUT",
        )

    # ------------------------------------------------------------ 错误映射
    @classmethod
    def _raise_http(cls, resp) -> None:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        message = ""
        if isinstance(detail, dict):
            message = str(
                detail.get("message")
                or detail.get("error")
                or detail.get("detail")
                or ""
            )
        elif isinstance(detail, str):
            message = detail
        message = (message or f"HTTP {resp.status_code}")[:400]
        low = message.lower()
        if resp.status_code in (401, 403):
            raise ProviderError(
                "AI Hive API Key 无效或无图片生成权限",
                retryable=False,
                code="AUTH",
            )
        if resp.status_code == 429:
            raise ProviderError(
                "AI Hive 触发限流或额度限制，请稍后重试",
                retryable=True,
                code="RATE_LIMIT",
            )
        if any(t in low for t in ("quota", "balance", "额度", "余额")):
            raise ProviderError(
                "AI Hive 额度/余额不足", retryable=False, code="QUOTA"
            )
        if any(t in low for t in ("content", "policy", "safety", "敏感", "违规")):
            raise ProviderError(
                f"AI Hive 内容审核未通过：{message}",
                retryable=False,
                code="CONTENT_POLICY",
            )
        if resp.status_code in (400, 404, 422):
            raise ProviderError(
                f"AI Hive 请求参数错误：{message}",
                retryable=False,
                code="INVALID_REQUEST",
            )
        raise ProviderError(
            f"AI Hive 接口错误 HTTP {resp.status_code}：{message}",
            retryable=True,
            code=f"HTTP_{resp.status_code}",
        )
