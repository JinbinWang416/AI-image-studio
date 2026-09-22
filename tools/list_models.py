# -*- coding: utf-8 -*-
"""列出各服务商的模型清单，用于确定图生图白名单。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.providers.catalog import PROVIDER_CATALOG  # noqa: E402

print("=" * 78)
print("各服务商模型清单")
print("=" * 78)

for name in ("qwen", "openai", "gemini", "seedream", "custom", "kling", "zhipu"):
    meta = PROVIDER_CATALOG.get(name)
    if not meta:
        continue
    models = meta.get("models") or []
    print(f"\n  【{name}】{meta.get('label', '')}  默认={meta.get('default_model', '(无)')}")
    for m in models:
        mid = m.get("id", "?")
        label = m.get("label", "")
        flag = "← 默认" if mid == meta.get("default_model") else ""
        print(f"      {mid:<34} {label}  {flag}")
    if not models:
        print("      （无模型定义）")
print("=" * 78)
