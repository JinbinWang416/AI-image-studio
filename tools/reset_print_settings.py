# -*- coding: utf-8 -*-
"""把 config/settings.json 的 print 段恢复为默认值。

背景：验证脚本会临时改 print 段并尝试恢复，但当原值为空对象时
「恢复」不起作用，会把测试值留在配置里。这里统一清掉 ——
删除整个 print 段即可，`default_settings()` 会在读取时补齐默认值。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SF = ROOT / "config" / "settings.json"

if not SF.is_file():
    print("  ⏭  settings.json 不存在，无需处理")
    raise SystemExit(0)

raw = SF.read_text(encoding="utf-8")
data = json.loads(raw)

print("=" * 70)
print("恢复 print 段为默认值")
print("=" * 70)

before = data.get("print")
if not before:
    print("  ⏭  没有 print 段，已是默认状态")
    raise SystemExit(0)

print("  当前值:", json.dumps(before, ensure_ascii=False)[:120])

# 备份
bak = SF.with_suffix(".json.bak-printreset")
shutil.copy2(SF, bak)
print(f"  已备份: {bak.name}")

# 删除 print 段 → 读取时由 default_settings() 补齐
data.pop("print", None)
SF.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("  ✅ 已删除 print 段（读取时会用默认值）")

# 验证
sys.path.insert(0, str(ROOT))
from app.config import load_config  # noqa: E402

cfg = load_config()
print()
print("  恢复后的有效值:")
for k in ("print_export_enabled", "print_width_cm", "print_dpi", "print_bleed_mm",
          "print_cutout", "print_white_ink", "print_dieline"):
    print(f"    {k:<24} = {getattr(cfg, k)}")
print("=" * 70)
