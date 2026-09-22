# -*- coding: utf-8 -*-
"""运行启动一致性自检并输出结果。"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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
