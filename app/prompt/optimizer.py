# -*- coding: utf-8 -*-
"""DeepSeek 中文门店贴纸提示词优化器。

它是 OpenAI 图像服务之前的可选文本步骤，不生成图片，也不会改变用户要求
必须逐字出现的主标题与副标题。DeepSeek 的 Chat Completions API 与 OpenAI
兼容，采用官方 ``POST /chat/completions`` 端点。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


class PromptOptimizerError(Exception):
    """提示词优化失败，调用方可回退到原始提示词继续出图。"""


@dataclass(frozen=True)
class PromptOptimization:
    prompt: str
    model: str
    elapsed: float


class DeepSeekPromptOptimizer:
    """通过 DeepSeek 把现有中文提示词润色为更明确的视觉指令。"""

    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 45.0):
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip() or "deepseek-flash"
        self.timeout = timeout

    @staticmethod
    def _system_message() -> str:
        return (
            "你是中文线下门店异形玻璃贴的专业视觉提示词编辑。"
            "只输出一段可直接提交给图像模型的中文正向提示词，不写标题、说明、"
            "Markdown 或引号。必须保留用户提供的主标题和副标题，逐字不改、"
            "不添加任何其他文字。强化彩色商业插画、主体、层级、材质、白色画布边缘、"
            "1:1 构图与可制作的异形贴纸轮廓；不得加入真实品牌、价格、电话号码、"
            "二维码、人物肖像或无法验证的承诺。"
        )

    async def optimize(
        self,
        *,
        prompt: str,
        main_title: str,
        sub_title: str,
        variation: str,
    ) -> PromptOptimization:
        if not self.api_key:
            raise PromptOptimizerError("缺少 DeepSeek API Key")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - requirements 已声明 httpx
            raise PromptOptimizerError("缺少 httpx，无法调用 DeepSeek") from exc

        user_message = (
            "请优化以下门店贴纸图像提示词。\n"
            f"主标题（必须逐字出现）：{main_title}\n"
            f"副标题（必须逐字出现）：{sub_title}\n"
            f"本轮构图方向：{variation}\n"
            f"原始提示词：\n{prompt}\n\n"
            "请只返回优化后的中文正向提示词。"
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system_message()},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "temperature": 0.55,
            "max_tokens": 1200,
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
        except Exception as exc:  # noqa: BLE001 - 给工作流统一回退提示
            kind = type(exc).__name__
            raise PromptOptimizerError(f"DeepSeek 请求失败：{kind}: {exc}") from exc

        if response.status_code >= 400:
            try:
                body = response.json()
                err = body.get("error", {}) if isinstance(body, dict) else {}
                detail = err.get("message") if isinstance(err, dict) else str(err)
            except Exception:  # noqa: BLE001
                detail = response.text[:240]
            if response.status_code in (401, 403):
                raise PromptOptimizerError("DeepSeek API Key 无效或没有权限")
            if response.status_code == 429:
                raise PromptOptimizerError("DeepSeek 当前限流，请稍后重试；本张将使用原始提示词")
            raise PromptOptimizerError(f"DeepSeek HTTP {response.status_code}：{detail}")

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PromptOptimizerError("DeepSeek 返回内容缺少优化提示词") from exc
        if not isinstance(content, str) or not content.strip():
            raise PromptOptimizerError("DeepSeek 返回了空提示词")
        cleaned = content.strip().strip('"').strip("“”")
        if len(cleaned) < 40:
            raise PromptOptimizerError("DeepSeek 返回的提示词过短，已保留原始提示词")
        # 防止模型漏掉排版必须保留的中文；失败时宁可回退原始高约束提示词。
        if main_title not in cleaned or sub_title not in cleaned:
            raise PromptOptimizerError("DeepSeek 改动了必须保留的中文文案，已保留原始提示词")
        return PromptOptimization(
            prompt=cleaned,
            model=str(body.get("model") or self.model),
            elapsed=time.monotonic() - started,
        )

    async def optimize_quality_template(self, template: str) -> PromptOptimization:
        """优化一次项目级质量模板，同时严格保留变量占位符。

        逐张调用会放大文本模型开销且容易造成同套贴纸风格漂移，因此图像
        工作区只调用本方法一次，再由编排器为每张图渲染变量。
        """
        from .profiles import REQUIRED_VARIABLES, validate_template

        template = validate_template(template)
        if not self.api_key:
            raise PromptOptimizerError("缺少 DeepSeek API Key")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise PromptOptimizerError("缺少 httpx，无法调用 DeepSeek") from exc

        required = "、".join(REQUIRED_VARIABLES)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是中文门店异形玻璃贴的专业图像提示词优化师。"
                        "只输出优化后的中文质量模板，不要 Markdown、解释或引号。"
                        f"必须逐字保留这些变量占位符：{required}。"
                        "强化彩色商业设计、标题层级、行业主体、可裁切轮廓、纯白边缘和可制作性；"
                        "不要增加真实品牌、价格、电话、二维码或人物肖像。"
                    ),
                },
                {"role": "user", "content": f"请优化以下项目质量模板：\n{template}"},
            ],
            "stream": False,
            "temperature": 0.45,
            "max_tokens": 1200,
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
        except Exception as exc:  # noqa: BLE001
            raise PromptOptimizerError(f"DeepSeek 请求失败：{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            if response.status_code in (401, 403):
                raise PromptOptimizerError("DeepSeek API Key 无效或没有权限")
            if response.status_code == 429:
                raise PromptOptimizerError("DeepSeek 当前限流，请稍后再试")
            raise PromptOptimizerError(f"DeepSeek HTTP {response.status_code}：{response.text[:240]}")
        try:
            body = response.json()
            cleaned = str(body["choices"][0]["message"]["content"]).strip().strip('"').strip("“”")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PromptOptimizerError("DeepSeek 返回内容缺少优化模板") from exc
        try:
            validate_template(cleaned)
        except ValueError as exc:
            raise PromptOptimizerError(f"DeepSeek 改动了模板变量，未保存：{exc}") from exc
        return PromptOptimization(
            prompt=cleaned,
            model=str(body.get("model") or self.model),
            elapsed=time.monotonic() - started,
        )
