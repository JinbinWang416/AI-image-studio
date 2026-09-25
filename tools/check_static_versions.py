# -*- coding: utf-8 -*-
"""验证静态资源版本号是否已自动生成（基于 mtime）。"""

from __future__ import annotations

import re
import urllib.request

html = urllib.request.urlopen("http://127.0.0.1:8000/", timeout=15).read().decode("utf-8")

print("=" * 74)
print("静态资源版本号")
print("=" * 74)
rows = list(re.finditer(r'/static/([A-Za-z0-9_.\-]+\.(?:css|js))\?v=([^"\']*)', html))
for m in rows:
    name, v = m.group(1), m.group(2)
    kind = "自动(mtime)" if (v.isdigit() and len(v) > 12) else "手写"
    print(f"  {name:<28} v={v[:18]:<20} {kind}")

print()
print(f"  共 {len(rows)} 个资源")
auto = sum(1 for m in rows if m.group(2).isdigit() and len(m.group(2)) > 12)
print(f"  自动生成: {auto} / {len(rows)}" + ("  ✅" if auto == len(rows) else "  ⚠️ 仍有手写版本号"))

# 面板结构
print()
print("=" * 74)
print("设置面板（拆分后）")
print("=" * 74)
navs = re.findall(r'data-pane="([a-z]+)">([^<]+)</button>', html)
for pane, label in navs:
    print(f"  {pane:<12} {label.strip()}")

secs = re.findall(r'<section class="sgroup settings-pane[^"]*" data-pane="([a-z]+)">', html)
from collections import Counter

print()
for pane, n in Counter(secs).items():
    print(f"  面板 {pane:<12} 含 {n} 个 section")
print("=" * 74)
