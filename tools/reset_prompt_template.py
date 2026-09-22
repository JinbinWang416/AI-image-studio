# -*- coding: utf-8 -*-
"""
把提示词质量模板重置为**代码中的当前默认值**。

背景：`config/settings.json` 的 `prompt_quality.template` 会覆盖
      `app/prompt_profiles.py` 里的 `DEFAULT_QUALITY_TEMPLATE`。
      所以在代码里改进了模板后，必须同步重置设置才会生效（与 effect.params 同理）。

用法：
    .\\.venv\\Scripts\\python.exe tools\\reset_prompt_template.py          # 重置
    .\\.venv\\Scripts\\python.exe tools\\reset_prompt_template.py --show   # 只看差异
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.prompt_profiles import DEFAULT_QUALITY_TEMPLATE  # noqa: E402

SETTINGS = ROOT / "config" / "settings.json"


def main() -> int:
    show_only = "--show" in sys.argv
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    quality = data.get("prompt_quality")
    if not isinstance(quality, dict):
        quality = {}
        data["prompt_quality"] = quality

    old = str(quality.get("template") or "")
    new = DEFAULT_QUALITY_TEMPLATE

    print("=" * 78)
    print("提示词质量模板重置")
    print("=" * 78)
    print(f"设置中的模板长度 : {len(old)}")
    print(f"代码默认模板长度 : {len(new)}")

    if old == new:
        print("\n✅ 两者已一致，无需修改")
        return 0

    print("\n【设置中的旧模板（前 200 字）】")
    print("  " + (old[:200] or "（空）").replace("\n", "\n  "))
    print("\n【代码中的新模板（前 200 字）】")
    print("  " + new[:200].replace("\n", "\n  "))

    if show_only:
        print("\n（--show 模式，未写入）")
        return 0

    quality["template"] = new
    # 优化版模板基于旧模板生成，重置后不再适用
    quality["optimized_template"] = ""
    quality["use_optimized"] = False

    SETTINGS.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n✅ 已写入设置（模板 {len(old)} → {len(new)} 字符）")
    print("   同时清空了 optimized_template 并关闭 use_optimized（旧优化结果已失效）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
