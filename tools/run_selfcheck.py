# -*- coding: utf-8 -*-
"""运行启动一致性自检并输出结果。"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# ⚠️ Windows 默认控制台是 GBK，下面那句 `print("  ✅ …")` 编不进 GBK，
#    会抛 UnicodeEncodeError 并让**整个脚本以退出码 1 结束** ——
#    看起来像「自检失败」，实际只是打印失败（评审 P2-03）。
#    按 README 原样执行即可复现，所以这里先把输出切成容错模式。
from _console import enable_safe_output  # noqa: E402

enable_safe_output()

from app.security.selfcheck import check_permission_consistency  # noqa: E402

problems = check_permission_consistency()
print("=" * 74)
print("权限一致性自检")
print("=" * 74)
if not problems:
    print("  ✅ 四方权限码完全一致")
else:
    print(f"  ⚠️  发现 {len(problems)} 个问题：")
    for p in problems:
        print(f"     · {p}")
print("=" * 74)
