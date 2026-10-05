# -*- coding: utf-8 -*-
"""Upscayl 本地超分（Real-ESRGAN / ncnn / Vulkan，本地免费）。

把「低价档生成 + 本地免费放大」做成一条可选链路，用来省图像 API 费用：

- 生成图：低价/免费档出 1024~2048px，落盘后本地超分到 2 倍另存（不覆盖母版）
- 印刷导出：把原来 `LANCZOS` 硬放大到 7087px 的那一步，换成 Upscayl 超分

**默认关闭**（`upscayl.enabled=False`），上线零风险；开启后失败会自动
回落 LANCZOS，绝不中断批次。

用法：

    from app.upscayl import UpscaylEngine

    engine = UpscaylEngine()
    if engine.available:
        engine.upscale_image(src, dst, "realesr-animevideov3-x2", 2, 128)
"""
from __future__ import annotations

from .alpha import (
    ALPHA_THRESHOLD,
    binarize_alpha,
    upscale_rgba,
    upscale_rgba_to_target,
)
from .engine import (
    DEFAULT_REL,
    MIN_TILE,
    UpscaylEngine,
    UpscaylError,
    resolve_binary,
    resolve_models_dir,
)
from .models import (
    ALLOWED_SCALES,
    DEFAULT_MODEL_GENERATED,
    DEFAULT_MODEL_PRINT,
    DEFAULT_TILE,
    UPSCAYL_MODELS,
    UpscaylModel,
    get_model,
    model_names,
)

__all__ = [
    "ALPHA_THRESHOLD",
    "ALLOWED_SCALES",
    "DEFAULT_MODEL_GENERATED",
    "DEFAULT_MODEL_PRINT",
    "DEFAULT_REL",
    "DEFAULT_TILE",
    "MIN_TILE",
    "UPSCAYL_MODELS",
    "UpscaylEngine",
    "UpscaylError",
    "UpscaylModel",
    "binarize_alpha",
    "get_model",
    "model_names",
    "resolve_binary",
    "resolve_models_dir",
    "upscale_rgba",
    "upscale_rgba_to_target",
]
