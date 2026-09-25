# -*- coding: utf-8 -*-
"""定位「哪个测试写了真实 settings.json」。

方法：
    1. 记录当前 settings.json 的 hash 与 mtime
    2. 逐个测试文件跑一遍
    3. 每个文件跑完立刻对比 hash —— 变了就是它

⚠️ 只读操作，不改任何代码。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / "config" / "settings.json"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


def digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "(缺失)"


def main() -> int:
    if not SETTINGS.is_file():
        print(f"  ❌ 找不到 {SETTINGS}")
        return 1

    baseline = digest(SETTINGS)
    print("=" * 76)
    print("定位「谁写了真实 settings.json」")
    print("=" * 76)
    print(f"  基线 hash: {baseline[:16]}…")

    tests = sorted(p.stem for p in (ROOT / "tests").glob("test_*.py"))
    print(f"  测试文件 : {len(tests)} 个\n")

    suspects: list[str] = []
    for i, name in enumerate(tests, 1):
        before = digest(SETTINGS)
        r = subprocess.run(
            [PY, "-m", "unittest", f"tests.{name}"],
            capture_output=True, text=True, cwd=ROOT, timeout=600)
        after = digest(SETTINGS)
        changed = before != after
        flag = "🔴 改了配置！" if changed else "✅"
        print(f"  [{i:>2}/{len(tests)}] {flag} {name}")
        if changed:
            suspects.append(name)

    print()
    print("=" * 76)
    if suspects:
        print(f"  🔴 有 {len(suspects)} 个测试文件会写真实 settings.json：")
        for s in suspects:
            print(f"      · {s}")
    else:
        print("  ✅ 逐个跑都没改配置 —— 说明是**组合运行时**才触发（比如顺序依赖）")
    print(f"\n  跑完后 hash: {digest(SETTINGS)[:16]}…")
    print(f"  基线是否恢复: {'是' if digest(SETTINGS) == baseline else '否'}")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
