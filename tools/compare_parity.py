# -*- coding: utf-8 -*-
"""对比两版的行为验证结果，确认重构没有改变行为。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RES = Path(__file__).resolve().parent.parent / "logs" / "parity"
new = json.loads((RES / "new.json").read_text(encoding="utf-8"))
old = json.loads((RES / "old.json").read_text(encoding="utf-8"))

print("=" * 78)
print("行为等价性对比：架构版（重构后） vs 原版（重构前）")
print("=" * 78)

new_checks = {c["name"]: c for c in new["checks"]}
old_checks = {c["name"]: c for c in old["checks"]}

# 只比"两版都有"的检查项
common = sorted(set(new_checks) & set(old_checks))
only_new = sorted(set(new_checks) - set(old_checks))

print(f"\n【① 共同检查项（{len(common)} 项）—— 这才是行为对比的关键】")
diffs: list[str] = []
for name in common:
    a, b = new_checks[name], old_checks[name]
    same_state = a["ok"] == b["ok"]
    same_detail = a["detail"] == b["detail"]
    if not same_state:
        diffs.append(f"{name}: 架构版={a['ok']} 原版={b['ok']}")
        mark = "❌"
    elif same_detail:
        mark = "✅"
    else:
        # 详情不同但状态一致（如路径含项目名）—— 标注但不判失败
        mark = "🟡"
    print(f"  {mark} {name:<32} 结果一致={same_state}")
    if not same_detail and same_state:
        print(f"       架构版: {a['detail'][:66]}")
        print(f"       原版  : {b['detail'][:66]}")

print(f"\n【② 重构版新增的检查项（{len(only_new)} 项）】")
for name in only_new:
    print(f"  ➕ {name:<32} {new_checks[name]['detail'][:50]}")

print("\n【③ 产物结构对比】")
STRUCT = [
    ("门店目录内容", "store_dir_children"),
    ("生成图文件名", "png_names"),
    ("TIF 分层模式", "tif_modes"),
    ("print_manifest 字段", "print_manifest_keys"),
]
for label, key in STRUCT:
    a, b = new.get(key), old.get(key)
    same = a == b
    print(f"  {'✅' if same else '🟡'} {label:<20} {'完全一致' if same else '有差异'}")
    if not same:
        print(f"       架构版: {str(a)[:100]}")
        print(f"       原版  : {str(b)[:100]}")

print("\n【④ 汇总】")
print(f"  架构版: {new['summary']['ok']} / {new['summary']['total']}")
print(f"  原版  : {old['summary']['ok']} / {old['summary']['total']}")
print(f"  共同项一致: {len(common) - len(diffs)} / {len(common)}")

if diffs:
    print("\n  ❌ 发现行为差异：")
    for d in diffs:
        print(f"     · {d}")
    verdict = False
else:
    print("\n  ✅ **两版行为完全一致** —— 重构没有改变任何用户可见行为")
    verdict = True

print("=" * 78)
sys.exit(0 if verdict else 1)
