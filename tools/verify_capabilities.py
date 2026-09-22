# -*- coding: utf-8 -*-
"""验证各服务商的图生图能力声明与拒绝行为。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.providers import create_provider  # noqa: E402
from app.providers.base import GenerateRequest  # noqa: E402

print("=" * 78)
print("① 各服务商能力声明")
print("=" * 78)
print(f"  {'服务商':<12} {'图生图':<8} {'多图':<8} {'上限':<6} 支持模式")
print("-" * 78)

providers = {}
for name in ("qwen", "openai", "gemini", "seedream", "flux_local", "mock"):
    cfg = load_config(name)
    cfg.api_key = cfg.api_key or "dummy"
    try:
        p = create_provider(cfg)
        providers[name] = p
        caps = p.image_capabilities()
        yi = "✅" if caps["supports_image"] else "❌"
        mi = "✅" if caps["supports_multi_image"] else "❌"
        print(f"  {name:<12} {yi:<8} {mi:<8} {caps['max_references']:<6} "
              f"{' / '.join(caps['modes'])}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {name:<12} ❌ {type(exc).__name__}: {exc}")

# ================================================================
print()
print("=" * 78)
print("② 拒绝行为（不应发起网络请求）")
print("=" * 78)


async def probe(name: str, mode: str, refs: int) -> tuple[bool, str]:
    """返回 (是否被正确拒绝, 错误信息)。

    纯本地能力校验，不发起任何网络请求。
    """
    p = providers.get(name)
    if p is None:
        return False, "provider 未创建"
    err = p.image_mode_error(mode, refs)
    return bool(err), err


CASES = [
    # (服务商, 模式, 参考图数, 是否应被拒绝)
    ("flux_local", "image", 1, True),
    ("flux_local", "multi", 2, True),
    ("flux_local", "text", 0, False),
    ("mock", "image", 1, True),
    ("qwen", "image", 1, False),          # 当前模型 qwen-image-3.0 支持
    ("qwen", "multi", 3, False),
    ("qwen", "multi", 5, True),           # 超过 max_references=3
    ("seedream", "image", 1, False),
    ("gemini", "multi", 2, False),
    ("openai", "image", 1, False),
    ("openai", "multi", 2, True),         # 保守声明为不支持多图
    ("gemini", "image", 0, True),         # 图生图但没给参考图
]

ok_n = 0
for name, mode, refs, should_reject in CASES:
    rejected, msg = asyncio.run(probe(name, mode, refs))
    good = rejected == should_reject
    ok_n += good
    mark = "✅" if good else "❌"
    detail = msg[:52] if msg else "（放行）"
    print(f"  {mark} {name:<11} {mode:<6} refs={refs}  "
          f"{'应拒绝' if should_reject else '应放行':<7} → {detail}")

print("-" * 78)
print(f"  通过 {ok_n} / {len(CASES)}")
print("=" * 78)
sys.exit(0 if ok_n == len(CASES) else 1)
