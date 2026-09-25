# -*- coding: utf-8 -*-
"""定位污染源（第二轮）：测「全量 discover / 各验证脚本 / 服务重启」谁改配置。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "config" / "settings.json"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


def digest() -> str:
    return hashlib.sha256(SETTINGS.read_bytes()).hexdigest()


def snapshot() -> dict:
    d = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
    return {
        "provider": d.get("active_provider"),
        "key_len": len((d.get("providers") or {}).get("qwen", {}).get("api_key", "")),
        "scope": len((d.get("scope") or {}).get("store_indexes") or []),
        "batch": (d.get("output") or {}).get("active_batch", ""),
    }


def step(label: str, fn) -> bool:
    before_h, before_s = digest(), snapshot()
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        print(f"      （执行异常 {type(exc).__name__}: {exc}）")
    time.sleep(1)
    after_h, after_s = digest(), snapshot()
    changed = before_h != after_h
    mark = "🔴 改了" if changed else "✅ 未改"
    print(f"  {mark}  {label}")
    if changed:
        for k in before_s:
            if before_s[k] != after_s[k]:
                print(f"          {k}: {before_s[k]} → {after_s[k]}")
    return changed


print("=" * 76)
print("污染源定位（第二轮）")
print("=" * 76)
print(f"  基线: {json.dumps(snapshot(), ensure_ascii=False)}")
print()

# ① 全量测试
step("python -m unittest discover -s tests",
     lambda: subprocess.run([PY, "-m", "unittest", "discover", "-s", "tests"],
                            capture_output=True, cwd=ROOT, timeout=900))

# ② 各验证脚本
for name in ("verify_p0.py", "verify_p1.py", "verify_p23.py",
             "verify_caps_api.py", "verify_reference_thumb.py",
             "verify_print_api.py", "verify_print_settings.py"):
    p = ROOT / "tools" / name
    if p.is_file():
        step(f"tools/{name}",
             lambda p=p: subprocess.run([PY, str(p)], capture_output=True, cwd=ROOT, timeout=900))

# ③ verify_parity（会切项目路径）
p = ROOT / "tools" / "verify_parity.py"
if p.is_file():
    step("tools/verify_parity.py --project 原版",
         lambda: subprocess.run([PY, str(p), "--project",
                                 r"E:\1_Software\6_AI工具\deepseek\2_开发\图片生成",
                                 "--out", str(ROOT / "logs" / "_p.json")],
                                capture_output=True, cwd=ROOT, timeout=900))

print()
print("=" * 76)
print(f"  结束状态: {json.dumps(snapshot(), ensure_ascii=False)}")
print("=" * 76)
