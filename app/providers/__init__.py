# -*- coding: utf-8 -*-
"""
服务商注册表。

新增服务商只需：
  1. 在 `app/providers/` 下实现一个 `BaseProvider` 子类；
  2. 在 `_REGISTRY` 中登记；
  3. 在 `app/config.py` 的 `PROVIDER_PRESETS` 里加上预置参数。
业务代码（编排器、Web）不需要任何改动。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import BaseProvider, GenerateRequest, GenerateResult, ProviderError
from .mock import MockProvider
from .flux_local import FluxLocalProvider

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config

# 轻量实现直接导入；重量级（依赖 httpx）的按需惰性导入，避免未装依赖时整体不可用
_REGISTRY: dict[str, type[BaseProvider]] = {
    "mock": MockProvider,
    "flux_local": FluxLocalProvider,
}


def _lazy(name: str) -> type[BaseProvider] | None:
    """惰性加载需要额外依赖的适配器。"""
    if name == "qwen":
        from .qwen import QwenProvider
        return QwenProvider
    if name == "openai":
        from .openai import OpenAIImageProvider
        return OpenAIImageProvider
    if name == "gemini":
        from .gemini import GeminiImageProvider
        return GeminiImageProvider
    if name == "seedream":
        from .seedream import SeedreamProvider
        return SeedreamProvider
    if name == "custom":
        # 「自定义」= 用户自填 Base URL 的 OpenAI 兼容服务。
        # 协议与 OpenAI Images API 一致，直接复用同一个适配器 ——
        # 这样它的图生图能力（/images/edits）也自动生效。
        # 早先这里没有分支，选了 custom 会抛「未知服务商」。
        from .openai import OpenAIImageProvider
        return OpenAIImageProvider
    if name == "kling":
        from .kling import KlingProvider
        return KlingProvider
    if name == "zhipu":
        from .zhipu import ZhipuProvider
        return ZhipuProvider
    return None


def available_providers() -> list[str]:
    return ["mock", "flux_local", "qwen", "openai", "gemini", "seedream", "kling", "zhipu"]


def get_provider(name: str) -> type[BaseProvider]:
    """按名称取得服务商类。"""
    if name in _REGISTRY:
        return _REGISTRY[name]
    cls = _lazy(name)
    if cls is None:
        raise ValueError(
            f"未知服务商 '{name}'，可选：{', '.join(available_providers())}"
        )
    _REGISTRY[name] = cls
    return cls


def create_provider(cfg: "Config") -> BaseProvider:
    """根据配置创建服务商实例。"""
    cls = get_provider(cfg.provider)
    return cls(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        model=cfg.model,
        timeout=cfg.timeout,
    )


__all__ = [
    "BaseProvider",
    "GenerateRequest",
    "GenerateResult",
    "ProviderError",
    "MockProvider",
    "FluxLocalProvider",
    "get_provider",
    "create_provider",
    "available_providers",
]
