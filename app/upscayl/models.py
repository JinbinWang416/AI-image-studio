# -*- coding: utf-8 -*-
"""Upscayl 本地超分：模型清单。

模型名即 `tools/upscayl/models/` 下 `.param` 文件的主名（不含扩展名）。
每个模型需要 `.bin` + `.param` 成对存在，缺一不可。

引擎是 Real-ESRGAN 的 ncnn 实现（Vulkan GPU 推理，本地免费）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UpscaylModel:
    """一个可用的超分模型。"""

    name: str
    label: str
    scales: tuple[int, ...]
    blurb: str
    # 生成图（偏插画/线稿）：animevideov3 系列对线条与色块更友好
    # 印刷链路（照片级合成）：x4plus 系列细节更稳
    for_generated: bool = False
    for_print: bool = False


# 与 tools/upscayl/models/ 下的文件一一对应（P0 已验证存在）
UPSCAYL_MODELS: dict[str, UpscaylModel] = {
    "realesr-animevideov3-x2": UpscaylModel(
        name="realesr-animevideov3-x2",
        label="Real-ESRGAN AnimeVideo v3 ×2",
        scales=(2,),
        # ⚠️ 保留在清单里（用户可能想自己验证），但**不要用作默认**：
        #    实测 3 张真实贴纸平均 SSIM 仅 0.57，输出有严重平铺伪影。
        blurb="⚠️ 实测不可用（SSIM 0.57，平铺伪影）。除非重新验证，否则别选。",
        for_generated=False,
    ),
    "realesr-animevideov3-x3": UpscaylModel(
        name="realesr-animevideov3-x3",
        label="Real-ESRGAN AnimeVideo v3 ×3",
        scales=(3,),
        blurb="动漫/插画向，3 倍。",
        for_generated=True,
    ),
    "realesr-animevideov3-x4": UpscaylModel(
        name="realesr-animevideov3-x4",
        label="Real-ESRGAN AnimeVideo v3 ×4",
        scales=(4,),
        blurb="动漫/插画向，4 倍。",
        for_generated=True,
    ),
    "realesrgan-x4plus": UpscaylModel(
        name="realesrgan-x4plus",
        label="Real-ESRGAN x4plus",
        scales=(4,),
        blurb="通用照片向，4 倍。细节最稳，印刷链路默认（实测 2048→8192 约 23 秒）。",
        for_print=True,
    ),
    "realesrgan-x4plus-anime": UpscaylModel(
        name="realesrgan-x4plus-anime",
        label="Real-ESRGAN x4plus Anime",
        scales=(4,),
        blurb="动漫向的 x4plus 变体，4 倍。线条更锐但可能过冲。",
        for_generated=True,
    ),
}

# ⚠️ 实测结论（2026-10-04，3 张真实贴纸 × 4 模型，tile=128）：
#
#     模型                        平均 SSIM   判定
#     realesr-animevideov3-x4      0.9784     ✅ 最佳
#     realesrgan-x4plus            0.9692     ✅
#     realesrgan-x4plus-anime      0.9605     ✅
#     realesr-animevideov3-x2      0.5747     ❌ 不可用
#
#   **`realesr-animevideov3-x2` 会产出严重的平铺伪影**：同一块内容被复制多次、
#   颜色错乱、笔画重复错位。而且换 tile（128/256/512）都一样坏 ——
#   所以不是 tile 的问题，是**这一个模型文件**的问题
#   （同系列的 x4 完全正常，bin 文件大小也相同，故不是下载损坏）。
#
#   踩坑记录：它的「锐度比」高达 6~8 倍（远超 0.90 门槛），看起来很"清晰"——
#   那是平铺伪影制造的虚假高频。**只信锐度比会误判**，必须同时看 SSIM，
#   或者直接看对比图。详细对比图见 `tools/upscayl_compare.py` 的输出。

# 默认模型：生成图用 animevideov3-x4（实测 SSIM 最高），印刷用 x4plus（P0 已验证大图）
DEFAULT_MODEL_GENERATED = "realesr-animevideov3-x4"
DEFAULT_MODEL_PRINT = "realesrgan-x4plus"

# 默认 tile。**绝不能是 0** —— 见 engine.py 的说明。
DEFAULT_TILE = 128

# 允许的倍数（CLI 的 -s 参数）
ALLOWED_SCALES = (2, 3, 4)


def get_model(name: str) -> UpscaylModel | None:
    return UPSCAYL_MODELS.get(name)


def model_names() -> list[str]:
    return sorted(UPSCAYL_MODELS)
