# -*- coding: utf-8 -*-
"""
清理确认过的冗余：未使用的导入 + 明确废弃的私有函数。

每一项都在清理前经过人工确认（见 CLAUDE/AGENTS 记录的判断依据）。

用法：
    .\\.venv\\Scripts\\python.exe tools\\cleanup_redundant.py [--dry-run]
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (相对路径, 原文, 替换为, 说明)
EDITS: list[tuple[str, str, str, str]] = [
    (
        "app/orchestrator.py",
        "from dataclasses import dataclass, field",
        "from dataclasses import dataclass",
        "field 未被使用",
    ),
    (
        "app/orchestrator.py",
        "from pathlib import Path\n",
        "",
        "Path 未被使用（效果图逻辑已提取到 effect_background）",
    ),
    (
        "app/runs.py",
        "from pathlib import Path\n",
        "",
        "Path 未被使用",
    ),
    (
        "app/web/server.py",
        "from ..config import LOCAL_PROFESSIONAL_ROOT, LOCAL_VALIDATION_ROOT, PROVIDER_PRESETS, load_config",
        "from ..config import LOCAL_PROFESSIONAL_ROOT, LOCAL_VALIDATION_ROOT, load_config",
        "PROVIDER_PRESETS 未被使用（服务商清单走 catalog）",
    ),
    ("tests/test_account_recovery.py", "import asyncio\n", "", "asyncio 未被使用"),
    ("tests/test_batches.py", "import asyncio\n", "", "asyncio 未被使用"),
    ("tests/test_flux_local.py", "import asyncio\n", "", "asyncio 未被使用"),
    ("tests/test_flux_local.py", "from PIL import Image\n", "", "Image 未被使用"),
    ("tests/test_local_validation.py", "import asyncio\n", "", "asyncio 未被使用"),
    ("tests/test_professional_local.py", "import asyncio\n", "", "asyncio 未被使用"),
    ("tests/test_professional_local.py", "import json\n", "", "json 未被使用"),
    ("tests/test_rate_limiter.py", "import asyncio\n", "", "asyncio 未被使用"),
]


def find_private_orphan() -> tuple[str, str, str, str] | None:
    """定位 professional_local._palette（私有且无引用）。"""
    path = ROOT / "app" / "professional_local.py"
    src = path.read_text(encoding="utf-8")
    start = src.find("def _palette(")
    if start < 0:
        return None
    # 往前吃掉装饰器/空行
    head = src.rfind("\n\n\n", 0, start)
    head = head + 1 if head >= 0 else start
    # 往后到下一个顶层 def
    nxt = src.find("\ndef ", start)
    if nxt < 0:
        return None
    block = src[head:nxt + 1]
    if "def _palette(" not in block:
        return None
    return ("app/professional_local.py", block, "", "_palette 为私有函数且全项目无引用")


def main() -> int:
    dry = "--dry-run" in sys.argv
    print("=" * 76)
    print(f"冗余清理{'（预览模式）' if dry else ''}")
    print("=" * 76)

    total = 0
    for rel, old, new, why in EDITS:
        p = ROOT / rel
        if not p.is_file():
            print(f"  ⏭  {rel} 不存在，跳过")
            continue
        raw = p.read_bytes()
        crlf = b"\r\n" in raw
        text = raw.decode("utf-8").replace("\r\n", "\n")
        if old not in text:
            print(f"  ⏭  {rel}：未找到目标（可能已清理）")
            continue
        if dry:
            print(f"  ○  {rel}：{why}")
        else:
            text = text.replace(old, new, 1)
            out = text.replace("\n", "\r\n") if crlf else text
            p.write_bytes(out.encode("utf-8"))
            print(f"  ✅ {rel}：{why}")
        total += 1

    orphan = find_private_orphan()
    if orphan:
        rel, block, _, why = orphan
        if dry:
            print(f"  ○  {rel}：{why}（{len(block.splitlines())} 行）")
        else:
            p = ROOT / rel
            raw = p.read_bytes()
            crlf = b"\r\n" in raw
            text = raw.decode("utf-8").replace("\r\n", "\n")
            text = text.replace(block, "", 1)
            out = text.replace("\n", "\r\n") if crlf else text
            p.write_bytes(out.encode("utf-8"))
            print(f"  ✅ {rel}：{why}（删除 {len(block.splitlines())} 行）")
        total += 1
    else:
        print("  ⏭  _palette 已清理或未找到")

    print("-" * 76)
    print(f"共处理 {total} 项")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
