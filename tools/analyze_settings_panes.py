# -*- coding: utf-8 -*-
"""统计设置弹窗各面板的字段数与一致性，为布局优化提供依据。"""

from __future__ import annotations

import pathlib
import re
import sys

HTML = pathlib.Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "index.html"
text = HTML.read_text(encoding="utf-8")

print("=" * 80)
print("设置面板结构统计")
print("=" * 80)

# 按 <section ... data-pane="xxx"> ... </section> 切分
sections = re.findall(
    r'<section\s+class="([^"]*?)"\s+data-pane="([a-z]+)"\s*>(.*?)</section>',
    text, re.S)

PANES: dict[str, list[dict]] = {}
for cls, pane, body in sections:
    title = re.search(r"<h3>(.*?)</h3>", body, re.S)
    title_txt = re.sub(r"<[^>]+>", "", title.group(1)).strip() if title else "?"
    fields = len(re.findall(r'class="sfield', body))
    checks = len(re.findall(r'class="scheck"', body))
    grids = len(re.findall(r'class="sgrid', body))
    notes = len(re.findall(r'class="snote', body))
    hints = len(re.findall(r'class="ghint"', body))
    PANES.setdefault(pane, []).append({
        "title": title_txt, "fields": fields, "checks": checks,
        "grids": grids, "notes": notes, "hints": hints,
        "cls": cls.strip(),
    })

for pane, items in PANES.items():
    total_f = sum(i["fields"] for i in items)
    total_c = sum(i["checks"] for i in items)
    print(f"\n【{pane}】{len(items)} 个 section，"
          f"字段 {total_f} 个，勾选 {total_c} 个")
    for i in items:
        flag = "" if i["cls"] == "sgroup settings-pane" else f"  ⚠ class={i['cls']}"
        print(f"   · {i['title']:<24} 字段{i['fields']} 勾选{i['checks']} "
              f"网格{i['grids']} 提示{i['hints']}{flag}")

print()
print("=" * 80)
print("一致性检查")
print("=" * 80)

# 1. 同一 pane 是否被多个 section 复用
multi = {p: len(v) for p, v in PANES.items() if len(v) > 1}
if multi:
    print("  ⚠️ 同一面板由多个 section 组成（切换时会一起显示）：")
    for p, n in multi.items():
        print(f"     {p}: {n} 个 section")
else:
    print("  ✅ 每个面板对应唯一 section")

# 2. 字段分布
all_f = [i["fields"] for items in PANES.values() for i in items]
if all_f:
    print(f"\n  单 section 字段数：最少 {min(all_f)}，最多 {max(all_f)}")
    print(f"  → 字段数差异大，固定列数网格需要按面板定制")

# 3. 类名一致性
bad_cls = [(p, i["title"], i["cls"])
           for p, items in PANES.items() for i in items
           if "sgroup" not in i["cls"] or "settings-pane" not in i["cls"]]
if bad_cls:
    print("\n  ⚠️ 类名不统一的 section：")
    for p, t, c in bad_cls:
        print(f"     {p} / {t}: {c}")
else:
    print("\n  ✅ 所有 section 都用 sgroup settings-pane")

print("=" * 80)
