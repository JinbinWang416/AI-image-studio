# -*- coding: utf-8 -*-
"""
阿里云百炼图像生成适配器。

支持三种调用路径，**自动探测并记住可用的那一条**：

| 模型 | 端点 | 请求体风格 |
|------|------|-----------|
| `qwen-image-3.0` / `3.0-pro` | `/api/v1/services/aigc/image-generation/generation` | messages |
| `qwen-image-2.0` / wanx 系列 | `/api/v1/services/aigc/text2image/image-synthesis` | prompt |
| 任意（需 WorkspaceId） | `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/images/generations` | OpenAI |

实现要点（来自 2026-09-18 一手官方文档 + 真实调用实测）：

1. ⚠️ `prompt_extend` **默认 true**，会**自动改写正向提示词** ——
   本项目要求精确渲染指定中文短句，因此**必须显式设为 false**；
2. `negative_prompt` 是**阿里云百炼扩展字段**，直接生效；
3. `watermark` 默认 false（与火山 Seedream 默认 true **相反**）；
4. DashScope 原生协议 `size` 用 `*` 分隔（`1024*1024`）；
5. 返回的是**图片 URL，仅 24 小时有效** —— 必须立即下载落盘；
6. 限流 **20 次/分钟**，实测较敏感，建议并发 ≤ 2 并配足重试。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from urllib.parse import urlparse

from .base import BaseProvider, GenerateRequest, GenerateResult, ProviderError

# 明确不可重试的 HTTP 状态码（参数/鉴权问题，重试无意义）
_FATAL_STATUS = {400, 401, 403, 404, 422}

# 响应体中提示不可重试的关键词
_FATAL_HINTS = (
    "invalid", "unsupported", "forbidden", "unauthorized", "arrearage",
    "quota", "balance", "datainspectionfailed", "sensitive",
)

# 阿里云百炼欠费时可能返回 HTTP 400，而不是常见的 402/403。截图中的
# `Arrearage` / `overdue-payment` 就属于这一类。该状态可在充值后恢复，
# 因此不能把后续全部任务都当成普通失败继续发送。
_ACCOUNT_ARREARAGE_HINTS = (
    "arrearage", "overdue-payment", "overdue payment", "account is in good standing",
    "账户欠费", "余额不足", "欠费",
)

# 端点不匹配的特征（应当换另一条端点重试，而不是放弃）
_ENDPOINT_MISMATCH_HINTS = (
    "url error", "please check url", "unsupported url", "not found",
)

# DashScope 异步任务轮询参数
_POLL_INTERVAL = 2.0
_POLL_MAX = 90


def _request_id_from_error(text: str) -> str:
    """Extract Alibaba's trace id without exposing a full response body."""
    try:
        data = json.loads(text or "{}")
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    value = data.get("request_id") or data.get("requestId") or ""
    return str(value).strip()[:128]


class QwenProvider(BaseProvider):
    """阿里云百炼图像生成适配器。"""

    name = "qwen"
    label = "阿里云百炼"
    supports_negative = True
    supports_n = True
    max_n = 6
    rpm_limit = 20
    # 图生图/多图生图：仅 qwen-image-3.0 系列支持（走 OpenAI 兼容的 image 扩展字段）
    supports_image = True
    supports_multi_image = True
    max_references = 3
    image_models = ("qwen-image-3.0", "qwen-image-3.0-pro")
    price_per_image = 0.18

    # 候选端点：(路径, 请求体风格)
    _ENDPOINT_CANDIDATES: tuple[tuple[str, str], ...] = (
        ("/api/v1/services/aigc/image-generation/generation", "messages"),
        ("/api/v1/services/aigc/text2image/image-synthesis", "prompt"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._client = None
        self._good_endpoint: tuple[str, str] | None = None

    # ------------------------------------------------------------ 基础设施
    @property
    def protocol(self) -> str:
        """按 Base URL 判断协议：OpenAI 兼容 or DashScope 原生。"""
        return "openai" if "maas.aliyuncs.com" in (self.base_url or "") else "dashscope"

    @property
    def origin(self) -> str:
        p = urlparse(self.base_url or "https://dashscope.aliyuncs.com")
        return f"{p.scheme}://{p.netloc}" if p.netloc else "https://dashscope.aliyuncs.com"

    async def _client_get(self):
        if self._client is None:
            try:
                import httpx
            except ImportError as e:  # pragma: no cover
                raise ProviderError(
                    "缺少依赖 httpx，请执行：pip install -r requirements.txt",
                    retryable=False,
                ) from e
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    @classmethod
    def _is_endpoint_mismatch(cls, err: Exception) -> bool:
        low = str(err).lower()
        return any(h in low for h in _ENDPOINT_MISMATCH_HINTS)

    def _raise_http(self, resp, stage: str = "请求") -> None:
        text = (resp.text or "")[:400]
        low = text.lower()

        if any(h in low for h in _ACCOUNT_ARREARAGE_HINTS):
            request_id = _request_id_from_error(text)
            trace = f"（Request ID: {request_id}）" if request_id else ""
            raise ProviderError(
                "阿里云返回 Arrearage" + trace + "。当前 API Key 所属账号尚未恢复图像计费。"
                "若费用页已显示可用额度，请等待账务同步后重试；仍失败请在同一主账号的百炼控制台"
                "新建通用 API Key 后保存并验证。新 Key 仍失败时，请携带该 Request ID 向阿里云提交工单。",
                retryable=False,
                code="ACCOUNT_ARREARAGE",
            )

        if resp.status_code == 403 and "freetieronly" in low.replace(" ", ""):
            raise ProviderError(
                "免费额度已耗尽，已暂停当前批次。请在阿里云百炼控制台充值或关闭“仅使用免费额度”后，"
                "在设置中先测试连接，再验证图片出图权限，验证成功后继续当前批次。",
                retryable=False, code="QUOTA",
            )
        if resp.status_code == 429:
            raise ProviderError(
                "触发限流（20 次/分钟，充值不提升）", retryable=True, code="RATE_LIMIT"
            )

        retryable = (
            resp.status_code not in _FATAL_STATUS
            and not any(h in low for h in _FATAL_HINTS)
        )
        if resp.status_code == 404:
            msg = (
                f"{stage}返回 404 —— 端点不存在。Base URL：{self.base_url}，"
                f"协议：{self.protocol}"
            )
        else:
            msg = f"{stage} HTTP {resp.status_code}：{text or '(空响应体)'}"
        raise ProviderError(msg, retryable=retryable, code=f"HTTP_{resp.status_code}")

    # ------------------------------------------------------------ 生成入口
    async def generate(self, req: GenerateRequest) -> GenerateResult:
        references = list(req.extra.get("reference_images") or [])
        # 统一能力校验（走 base.image_mode_error，与其它服务商口径一致）：
        #   · 检查是否支持图生图/多图生图
        #   · 检查模型是否在白名单里
        #   · 检查参考图张数是否超限
        _err = self.image_mode_error(
            str(req.extra.get("image_mode") or "text"), len(references))
        if _err:
            raise ProviderError(_err, retryable=False, code="UNSUPPORTED_IMAGE_MODE")
        if references:
            # 参考图是 OpenAI 兼容 Images API 的 image 扩展字段。即使用户仍
            # 使用旧 DashScope 域名，只要 Base URL 是 compatible-mode/v1 就可调用。
            return await self._generate_openai(req)
        if self.protocol == "openai":
            return await self._generate_openai(req)

        # DashScope 原生：依次尝试候选端点，自动适配模型所属 API 风格
        last_err: ProviderError | None = None
        order = [self._good_endpoint] if self._good_endpoint else []
        order += [e for e in self._ENDPOINT_CANDIDATES if e != self._good_endpoint]

        for path, style in order:
            try:
                result = await self._submit_and_wait(req, path, style)
                self._good_endpoint = (path, style)
                return result
            except ProviderError as e:
                # 只有在「端点不匹配」时才换下一条；其他错误直接抛出
                if e.code == "HTTP_404" or self._is_endpoint_mismatch(e):
                    last_err = e
                    continue
                raise

        if last_err:
            raise last_err
        raise ProviderError("没有可用的端点", retryable=False)

    # ------------------------------------------------------------ DashScope 原生
    async def _submit_and_wait(
        self, req: GenerateRequest, path: str, style: str
    ) -> GenerateResult:
        started = time.monotonic()
        client = await self._client_get()
        submit_url = f"{self.origin}{path}"
        model = self.model or "qwen-image-3.0"

        parameters: dict = {
            "size": req.size.replace("x", "*").replace("×", "*"),   # 1024*1024
            "n": self.clamp_n(req.n),
            # --- 关键：这两项决定成败，不可省略 ---
            "prompt_extend": False,     # 禁止自动改写提示词
            "watermark": False,         # 显式关闭水印
        }
        if self.supports_negative and req.negative_prompt:
            parameters["negative_prompt"] = req.negative_prompt
        if req.seed is not None:
            parameters["seed"] = req.seed

        if style == "messages":
            payload = {
                "model": model,
                "input": {
                    "messages": [
                        {"role": "user", "content": [{"text": req.prompt}]}
                    ]
                },
                "parameters": parameters,
            }
        else:                                   # prompt 风格（旧版模型）
            payload = {
                "model": model,
                "input": {"prompt": req.prompt},
                "parameters": parameters,
            }

        # --- 1) 提交异步任务 ---
        try:
            resp = await client.post(
                submit_url, json=payload,
                headers=self._headers({"X-DashScope-Async": "enable"}),
            )
        except Exception as e:
            raise ProviderError(f"提交任务失败：{e}", retryable=True, code="NETWORK") from e

        if resp.status_code >= 400:
            self._raise_http(resp, "提交任务")

        try:
            data = resp.json()
        except Exception as e:
            raise ProviderError(f"提交响应非合法 JSON：{resp.text[:300]}", retryable=True) from e

        output = data.get("output") or {}
        task_id = output.get("task_id")

        if not task_id:
            urls = self._extract_urls(output)
            if urls:
                return await self._download(urls, started, data, model)
            raise ProviderError(
                f"未取得 task_id：{str(data)[:300]}", retryable=True, code="NO_TASK"
            )

        # --- 2) 轮询任务状态 ---
        return await self._wait_task(task_id, started, model)

    async def _wait_task(
        self, task_id: str, started: float, model: str
    ) -> GenerateResult:
        client = await self._client_get()
        task_url = f"{self.origin}/api/v1/tasks/{task_id}"

        for _ in range(_POLL_MAX):
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                r = await client.get(task_url, headers=self._headers())
            except Exception as e:
                raise ProviderError(f"查询任务失败：{e}", retryable=True, code="NETWORK") from e

            if r.status_code >= 400:
                self._raise_http(r, "查询任务")

            d = r.json()
            out = d.get("output") or {}
            status = (out.get("task_status") or "").upper()

            if status == "SUCCEEDED":
                urls = self._extract_urls(out)
                if not urls:
                    raise ProviderError(
                        f"任务成功但无图片 URL：{str(out)[:400]}", retryable=True
                    )
                return await self._download(urls, started, d, model)

            if status in ("FAILED", "CANCELED", "UNKNOWN"):
                msg = out.get("message") or out.get("code") or status
                low = str(msg).lower()
                if any(h in low for h in _ACCOUNT_ARREARAGE_HINTS):
                    request_id = _request_id_from_error((r.text or "")[:400])
                    trace = f"（Request ID: {request_id}）" if request_id else ""
                    raise ProviderError(
                        "阿里云返回 Arrearage" + trace + "。当前 API Key 所属账号尚未恢复图像计费。"
                        "若费用页已显示可用额度，请等待账务同步后重试；仍失败请在同一主账号的百炼控制台"
                        "新建通用 API Key 后保存并验证。新 Key 仍失败时，请携带该 Request ID 向阿里云提交工单。",
                        retryable=False,
                        code="ACCOUNT_ARREARAGE",
                    )
                retryable = not any(h in low for h in _FATAL_HINTS)
                raise ProviderError(f"任务失败：{msg}", retryable=retryable, code=status)

        raise ProviderError(
            f"任务轮询超时（{_POLL_MAX * _POLL_INTERVAL:.0f} 秒）",
            retryable=True, code="TIMEOUT",
        )

    # ------------------------------------------------------------ OpenAI 兼容
    async def _generate_openai(self, req: GenerateRequest) -> GenerateResult:
        started = time.monotonic()
        client = await self._client_get()
        model = self.model or "qwen-image-3.0"

        url = f"{self.base_url.rstrip('/')}/images/generations"
        payload: dict = {
            "model": model,
            "prompt": req.prompt,
            "size": req.size,
            "n": self.clamp_n(req.n),
            "prompt_extend": False,
            "watermark": False,
        }
        if self.supports_negative and req.negative_prompt:
            payload["negative_prompt"] = req.negative_prompt
        if req.seed is not None:
            payload["seed"] = req.seed
        references = list(req.extra.get("reference_images") or [])
        if references:
            payload["image"] = references[0] if len(references) == 1 else references

        try:
            resp = await client.post(url, json=payload, headers=self._headers())
        except Exception as e:
            raise ProviderError(f"请求失败：{e}", retryable=True, code="NETWORK") from e

        if resp.status_code >= 400:
            self._raise_http(resp)

        try:
            data = resp.json()
        except Exception as e:
            raise ProviderError(f"响应非合法 JSON：{resp.text[:300]}", retryable=True) from e

        images: list[bytes] = []
        for it in data.get("data") or []:
            if it.get("b64_json"):
                images.append(base64.b64decode(it["b64_json"]))
            elif it.get("url"):
                images.append(await self._fetch_image(it["url"]))

        if not images:
            raise ProviderError(f"响应中没有图片：{str(data)[:300]}", retryable=True)

        return GenerateResult(
            images=images, provider=self.name, model=model,
            elapsed=time.monotonic() - started,
            raw={"usage": data.get("usage", {}), "request_id": data.get("request_id", "")},
        )

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _extract_urls(output: dict) -> list[str]:
        """递归提取所有图片 URL。

        `qwen-image-3.0` 返回 messages 风格（`choices[].message.content[].image`），
        wanx 系列返回 `results[].url`。递归遍历可一次兼容所有已知与未知结构。
        """
        urls: list[str] = []
        seen: set[str] = set()

        def walk(node) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if (
                        k in ("url", "image", "image_url")
                        and isinstance(v, str)
                        and v.startswith("http")
                    ):
                        if v not in seen:
                            seen.add(v)
                            urls.append(v)
                    else:
                        walk(v)
            elif isinstance(node, list):
                for x in node:
                    walk(x)

        walk(output)
        return urls

    async def _fetch_image(self, url: str) -> bytes:
        client = await self._client_get()
        try:
            r = await client.get(url)
            r.raise_for_status()
            return r.content
        except Exception as e:
            raise ProviderError(f"图片下载失败：{e}", retryable=True, code="DOWNLOAD") from e

    async def _download(
        self, urls: list[str], started: float, raw: dict, model: str
    ) -> GenerateResult:
        images = [await self._fetch_image(u) for u in urls]
        return GenerateResult(
            images=images, provider=self.name, model=model,
            elapsed=time.monotonic() - started,
            raw={"request_id": raw.get("request_id", "")},
        )
