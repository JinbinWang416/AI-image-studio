# -*- coding: utf-8 -*-
"""把 `config/settings.json` 的 `active_provider` 还原成指定值。

⚠️ 只改这一个字段，其它内容原样保留（含用户自己填的 Key、批次名等）。
⚠️ 用 Python 写而不是 PowerShell —— `Set-Content -Encoding UTF8` 在 PS 5.1 会加 BOM，
   而 `json.loads()` 遇到 BOM 直接抛错，整个配置会读不到（实测踩过）。

用法：
    python tools/restore_provider.py qwen
    python tools/restore_provider.py qwen --root "E:\\...\\AI生图架构版"
    python tools/restore_provider.py --check
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def settings_path(root: Path) -> Path:
    return root / "config" / "settings.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("provider", nargs="?", default="qwen", help="要还原成的服务商名")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="项目根目录")
    ap.add_argument("--check", action="store_true", help="只报告当前值，不改动")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    path = settings_path(root)
    if not path.exists():
        print(f"[X] 配置不存在：{path}")
        return 1

    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        print("[!] 警告：配置带 UTF-8 BOM，Python 的 json 会读不动它")

    data = json.loads(raw.decode("utf-8-sig"))
    current = data.get("active_provider")

    if args.check:
        print(f"路径：{path}")
        print(f"active_provider = {current!r}")
        print(f"修改时间：{datetime.fromtimestamp(path.stat().st_mtime)}")
        return 0

    if current == args.provider:
        print(f"[=] active_provider 已经是 {args.provider!r}，无需改动")
        return 0

    # 先留一份现场，便于事后取证
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    keep = path.with_name(f"settings.json.polluted-{stamp}")
    shutil.copy2(path, keep)
    print(f"[i] 现场已保存：{keep.name}")

    data["active_provider"] = args.provider
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)                    # 原子替换

    back = json.loads(path.read_text(encoding="utf-8"))
    print(f"[OK] active_provider: {current!r} -> {back.get('active_provider')!r}")
    print(f"     文件大小 {len(raw)} -> {path.stat().st_size}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
