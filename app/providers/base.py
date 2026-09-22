# -*- coding: utf-8 -*-
"""
生成服务商的统一抽象接口。

设计目标：**换模型不改业务代码**。上层（编排器）只依赖这里的接口，
具体走阿里、可灵还是智谱，由 `app/providers/__init__.py` 的注册表决定。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class GenerateRequest:
    """一次生成请求。"""

    prompt: str
    negative_prompt: str = ""
    size: str = "1024x1024"
    n: int = 1
    seed: int | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class GenerateResult:
    """一次生成结果。"""

    images: list[bytes] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    elapsed: float = 0.0
    raw: dict = field(default_factory=dict)
    error: str = ""
    retryable: bool = True

    @property
    def ok(self) -> bool:
        return bool(self.images) and not self.error


class ProviderError(Exception):
    """生成失败。

    `retryable=False` 表示不该重试——例如内容违规、参数错误、余额不足。
    """

    def __init__(self, message: str, retryable: bool = True, code: str = ""):
        super().__init__(message)
        self.retryable = retryable
        self.code = code

    @property
    def requires_account_recovery(self) -> bool:
        """Whether this error is resolved outside the app and then retried.

        A payment or free-quota block is different from a bad prompt: sending
        the remaining jobs after the first rejection just creates a long list
        of avoidable failures.  The orchestrator pauses the batch for these
        codes and leaves its incomplete jobs available for resume.
        """
        return self.code in {"ACCOUNT_ARREARAGE", "QUOTA"}


def _mode_label(mode: str) -> str:
    """图生图模式的中文名（用于错误提示）。"""
    return {"image": "图生图", "multi": "多图生图"}.get(str(mode or "").lower(), "图生图")


class BaseProvider(ABC):
    """生成服务商抽象基类。"""
    name: str = "base"
    label: str = "未命名服务商"

    # ---- 能力声明（子类覆盖，编排器据此调整行为）----
    supports_negative: bool = False   # 是否支持 negative_prompt
    supports_n: bool = False          # 是否支持一次出多张
    max_n: int = 1
    rpm_limit: int = 0                # 0 = 官方未设 QPS 限制
    price_per_image: float = 0.0      # 元/张

    # ---- 图生图能力（AGENTS.md：必须显式声明，不得静默降级）----
    #   子类**必须**明确声明；默认全 False 表示"不支持"，
    #   这样新增服务商时若忘了声明，行为是"拒绝"而不是"静默丢弃参考图"。
    supports_image: bool = False          # 图生图（单张参考图）
    supports_multi_image: bool = False    # 多图生图（多张参考图）
    max_references: int = 0               # 最多接受几张参考图（0 = 不接受）

    # 支持图生图时，哪些模型可用（空元组 = 不限制模型）
    image_models: tuple[str, ...] = ()

    def image_mode_error(self, mode: str, ref_count: int = 0) -> str:
        """检查图生图模式是否被支持。

        各服务商在 `generate()` 开头调用它，**本地就报错**，避免：
          · 静默丢弃参考图（用户以为生效了）
          · 白白发起一次付费 API 调用才发现模型不支持

        Args:
            mode: `text` / `image` / `multi`
            ref_count: 参考图张数

        Returns:
            错误说明；**空字符串表示支持**。
        """
        if mode in ("", "text"):
            return ""
        if ref_count <= 0:
            return f"{self.label}：{_mode_label(mode)}需要至少 1 张参考图"

        if not self.supports_image:
            return (
                f"{self.label}不支持图生图/多图生图。"
                f"请改用支持的服务商（如阿里云百炼 qwen-image-3.0、Seedream、Gemini）"
            )
        if mode == "multi" and not self.supports_multi_image:
            return f"{self.label}不支持多图生图（一次只能传 1 张参考图）"
        if self.image_models and self.model and self.model not in self.image_models:
            allowed = "、".join(self.image_models)
            return (
                f"当前模型 {self.model} 不支持{_mode_label(mode)}；"
                f"可用模型：{allowed}"
            )
        if self.max_references and ref_count > self.max_references:
            return (
                f"{self.label}最多接受 {self.max_references} 张参考图，"
                f"当前 {ref_count} 张"
            )
        return ""

    def image_capabilities(self) -> dict:
        """给前端/接口用的能力描述。"""
        return {
            "supports_image": self.supports_image,
            "supports_multi_image": self.supports_multi_image,
            "max_references": self.max_references,
            "image_models": list(self.image_models),
            "modes": (
                ["text"]
                + (["image"] if self.supports_image else [])
                + (["multi"] if self.supports_multi_image else [])
            ),
        }

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        timeout: float = 120.0,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    # ------------------------------------------------------------ 核心
    @abstractmethod
    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """执行一次生成。失败时抛 `ProviderError`。"""
        raise NotImplementedError

    async def close(self) -> None:
        """释放资源（如 HTTP 连接池）。"""
        return None

    # ------------------------------------------------------------ 辅助
    def clamp_n(self, n: int) -> int:
        """把请求张数收敛到服务商支持的范围。"""
        if not self.supports_n:
            return 1
        return max(1, min(n, self.max_n))

    def describe(self) -> str:
        caps = []
        caps.append("负向词✓" if self.supports_negative else "负向词✗")
        caps.append(f"n≤{self.max_n}" if self.supports_n else "n=1")
        caps.append(f"{self.rpm_limit} RPM" if self.rpm_limit else "无 QPS 限制")
        return f"{self.label}（{self.model}｜{'｜'.join(caps)}）"

    async def __aenter__(self) -> "BaseProvider":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
