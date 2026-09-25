# -*- coding: utf-8 -*-
"""修复 settings.json 的 UTF-8 BOM（PowerShell 写文件留下的）。

⚠️ 背景：
    PowerShell 5.1 的 `Set-Content -Encoding UTF8` **会写 BOM**，
    而 Python 的 `json.loads()` **不接受 BOM** → 整个配置文件读取失败
    → `load_config()` 回落到内置默认值（表现为 active_batch 丢失、
    服务商变 mock、图片列表空白）。

修法：用 `utf-8-sig` 读（容忍 BOM）、用 `utf-8` 写（不带 BOM），并做全量校验。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOTS = [
    Path(r"E:\1_Software\6_AI工具\deepseek\2_开发\图片生成"),
    Path(r"E:\1_Software\6_AI工具\deepseek\2_开发\AI生图架构版"),
]

BOM = b"\xef\xbb\xbf"


def fix_json_files(root: Path) -> list[str]:
    """扫描项目里的 .json，去掉 BOM，返回修复清单。"""
    fixed: list[str] = []
    for p in root.rglob("*.json"):
        if ".venv" in p.parts or "output_backup" in str(p):
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        if not raw.startswith(BOM):
            continue
        try:
            text = raw.decode("utf-8-sig")
            data = json.loads(text)          # 校验是合法 JSON
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f"    ⚠️ {p.relative_to(root)} BOM 且内容异常：{exc}")
            continue
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
        fixed.append(str(p.relative_to(root)))
    return fixed


def main() -> int:
    print("=" * 74)
    print("修复 settings.json 的 UTF-8 BOM")
    print("=" * 74)
    total = 0
    for root in ROOTS:
        if not root.is_dir():
            print(f"\n  ⏭  {root.name} 不存在")
            continue
        print(f"\n【{root.name}】")
        fixed = fix_json_files(root)
        if fixed:
            for f in fixed:
                print(f"    ✅ 已去 BOM：{f}")
            total += len(fixed)
        else:
            print("    ✅ 无 BOM 文件")

    # 验证两版配置能否被正确读取
    print()
    print("=" * 74)
    print("验证：load_config() 是否恢复正常")
    print("=" * 74)
    for root in ROOTS:
        if not root.is_dir():
            continue
        sys.path.insert(0, str(root))
        # 清掉已导入的 app 模块，强制用目标项目的代码
        for m in [k for k in list(sys.modules) if k == "app" or k.startswith("app.")]:
            del sys.modules[m]
        try:
            from app.config import load_config  # type: ignore

            cfg = load_config()
            print(f"\n  【{root.name}】")
            print(f"    provider    = {cfg.provider}      ✓" if cfg.provider != "mock"
                  else f"    provider    = {cfg.provider}      ⚠️（应为真实服务商）")
            print(f"    model       = {cfg.model}")
            print(f"    batch_id    = '{cfg.batch_id}'"
                  + ("   ✓" if cfg.batch_id else "   ❌ 仍为空"))
            print(f"    output_root = {cfg.output_root}")
            print(f"    api_key     = {'已配置' if cfg.api_key else '❌ 空'}")
        except Exception as exc:  # noqa: BLE001
            print(f"\n  【{root.name}】读取失败：{type(exc).__name__}: {exc}")
        finally:
            sys.path.remove(str(root))

    print()
    print("=" * 74)
    print(f"  共修复 {total} 个文件")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
