# -*- coding: utf-8 -*-
"""
离线模拟服务商 —— **无需任何 API Key 即可跑通全流程**。

用途：
  1. 在拿到真 Key 之前，验证 23 文件夹创建、并发调度、重试、断点续跑、
     manifest 记录、Web 界面进度推送等**所有非 API 环节**；
  2. 作为回归测试的固定桩。

生成的图片是**结构上像贴纸的占位图**（白底 + 圆角边框 + 上下色带模拟标题区），
使用纯标准库写 PNG，不依赖 Pillow。
"""
from __future__ import annotations

import asyncio
import os
import random
import struct
import time
import zlib

from .base import BaseProvider, GenerateRequest, GenerateResult, ProviderError

# 每个门店一组主题色（RGB），让 23 套图在观感上可区分
_PALETTE = [
    (37, 99, 235), (16, 185, 129), (245, 158, 11), (59, 130, 246),
    (120, 113, 108), (180, 83, 9), (14, 165, 233), (30, 64, 175),
    (220, 38, 38), (202, 138, 4), (234, 88, 12), (249, 115, 22),
    (71, 85, 105), (13, 148, 136), (217, 119, 6), (100, 116, 139),
    (161, 98, 7), (146, 64, 14), (202, 138, 4), (194, 65, 12),
    (120, 113, 108), (161, 98, 7), (51, 65, 85),
]


def _png_stream(width: int, height: int, rows: list[bytes]) -> bytes:
    """把 RGB 行数据编码为 PNG 字节流（纯标准库）。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + r for r in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def _render_placeholder(
    size: int,
    accent: tuple[int, int, int],
    variant: int,
    is_scene: bool,
) -> bytes:
    """画一张占位贴纸图：白底 + 不规则圆角边框 + 上下色带 + 中部主体示意。"""
    w = h = size
    white = b"\xff" * (w * 3)
    rows: list[bytes] = [white] * h

    band = bytes(accent) * w
    light = bytes(tuple(min(255, c + 120) for c in accent)) * w

    # 上下标题区色带
    for y in range(int(h * 0.11), int(h * 0.19)):
        rows[y] = band
    for y in range(int(h * 0.81), int(h * 0.89)):
        rows[y] = band

    # 中部主体：最后一张（场景图）画填充块，其余画线框
    top, bottom = int(h * 0.28), int(h * 0.72)
    left, right = int(w * 0.20), int(w * 0.80)
    thickness = max(2, h // 160)

    if is_scene:
        for y in range(top, bottom):
            row = bytearray(white)
            for x in range(left, right):
                if (x // 24 + y // 24) % 2 == 0:
                    row[x * 3: x * 3 + 3] = bytes(light[:3])
            rows[y] = bytes(row)
    else:
        for y in range(top, bottom):
            in_h = y < top + thickness or y >= bottom - thickness
            row = bytearray(white)
            if in_h:
                for x in range(left, right):
                    row[x * 3: x * 3 + 3] = bytes(accent)
            else:
                for x in list(range(left, left + thickness)) + list(range(right - thickness, right)):
                    row[x * 3: x * 3 + 3] = bytes(accent)
            rows[y] = bytes(row)

    # 变体微调：不同 pic_index 让中部图形有差异
    if variant % 2 == 1:
        mid = (top + bottom) // 2
        for y in range(mid - thickness, mid + thickness):
            row = bytearray(rows[y])
            for x in range(left, right):
                row[x * 3: x * 3 + 3] = bytes(accent)
            rows[y] = bytes(row)

    return _png_stream(w, h, rows)


class MockProvider(BaseProvider):
    """离线模拟服务商。"""

    name = "mock"
    label = "本地模拟器"
    supports_negative = True
    supports_n = True
    max_n = 6
    rpm_limit = 0
    price_per_image = 0.0
    # 模拟器只产出占位图，图生图没有实际语义 —— 显式声明不支持
    # （AGENTS.md 要求显式声明，不能靠基类默认值蒙混）
    supports_image = False
    supports_multi_image = False
    max_references = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 模拟单张耗时与失败率，便于测试重试逻辑
        self.latency = float(os.environ.get("MOCK_LATENCY", "0.35"))
        self.fail_rate = float(os.environ.get("MOCK_FAIL_RATE", "0"))

    async def generate(self, req: GenerateRequest) -> GenerateResult:
        started = time.monotonic()

        # 模拟网络延迟
        await asyncio.sleep(self.latency * random.uniform(0.6, 1.4))

        # 模拟失败（默认 0，可通过 MOCK_FAIL_RATE 打开以测试重试）
        if self.fail_rate > 0 and random.random() < self.fail_rate:
            raise ProviderError("模拟的服务端错误（用于测试重试）", retryable=True, code="MOCK_FAIL")

        # 从提示词里提取门店序号与图片序号，用于配色与变体
        seed = req.seed if req.seed is not None else random.randint(0, 10**6)
        store_no = seed % 23
        variant = (seed // 23) % 6
        accent = _PALETTE[store_no % len(_PALETTE)]
        is_scene = variant == 5          # 第 6 张按约定是场景图

        png = _render_placeholder(
            size=int(req.size.split("x")[0]) if "x" in req.size else 1024,
            accent=accent,
            variant=variant,
            is_scene=is_scene,
        )

        return GenerateResult(
            images=[png],
            provider=self.name,
            model=self.model or "mock-v1",
            elapsed=time.monotonic() - started,
            raw={"mock": True, "seed": seed},
        )
