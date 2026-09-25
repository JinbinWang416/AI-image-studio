# -*- coding: utf-8 -*-
"""安全地设置 API Key：输入不回显、不打印内容、不经过聊天记录。

⚠️ 为什么需要它

   `config/settings.json` 里存着真实 Key，而它**已被 `.gitignore` 排除**，
   所以换 Key 只能在本机做。网页设置页可以，但生产版和架构版
   **各有独立的配置文件**，来回切换比较烦；这个脚本一条命令搞定，
   而且比手动改 JSON 安全（不会把文件写出 BOM —— BOM 会让整个配置读不到，
   provider 退回 mock，这个坑实测踩过）。

用法

    # 在当前项目的 config/settings.json 里设置 qwen 的 Key（交互输入）
    .\\.venv\\Scripts\\python.exe tools\\set_api_key.py

    # 一次把架构版也设上
    .\\.venv\\Scripts\\python.exe tools\\set_api_key.py --also "E:\\...\\AI生图架构版"

    # 指定其它服务商
    .\\.venv\\Scripts\\python.exe tools\\set_api_key.py --provider openai

    # 只看当前状态，不做改动
    .\\.venv\\Scripts\\python.exe tools\\set_api_key.py --show
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def mask(key: str) -> str:
    """只用于回显，绝不完整打印。"""
    if not key:
        return "(空)"
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}****{key[-4:]}  长度 {len(key)}"


def config_path(root: Path) -> Path:
    return root / "config" / "settings.json"


def read_key(root: Path, provider: str) -> str:
    path = config_path(root)
    if not path.exists():
        return ""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        print(f"  [!] {path.name} 带 UTF-8 BOM —— 建议用本脚本重写一次以清除")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    return ((data.get("providers") or {}).get(provider) or {}).get("api_key") or ""


def write_key(root: Path, provider: str, key: str) -> None:
    """写入指定项目的配置。

    ⚠️ 用纯 JSON 读写（不用 `SettingsStore`）—— 因为一次要给**多个项目**
       写配置，而它们都有一个叫 `app` 的包，同一个进程里 import 两次会冲突。
       我们只改 key 这一个字段，其余内容原样保留。
    """
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_bytes().decode("utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"  [!!] {path} 解析失败，为避免误删内容，跳过这个目录")
            return

    providers = data.setdefault("providers", {})
    entry = providers.setdefault(provider, {})
    entry["api_key"] = key

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)                       # 原子替换，且不带 BOM


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="qwen", help="服务商标识（默认 qwen）")
    ap.add_argument("--root", default=str(ROOT), help="主项目根目录（默认脚本所在项目）")
    ap.add_argument("--also", action="append", default=[],
                    help="同时写入另一个项目根目录（可重复多次，如架构版目录）")
    ap.add_argument("--show", action="store_true", help="只显示当前状态")
    args = ap.parse_args()

    roots = [Path(args.root).resolve()] + [Path(p).resolve() for p in args.also]
    roots = [r for r in roots if config_path(r).exists() or not args.show]

    print(f"[{args.provider}] 目标配置：")
    for r in roots:
        exists = "存在" if config_path(r).exists() else "不存在"
        print(f"  {r}")
        print(f"      当前 = {mask(read_key(r, args.provider))}   ({exists})")

    if args.show:
        return 0

    print("\n请粘贴新的 API Key（输入不回显；直接回车取消）：")
    try:
        key = getpass.getpass("  API Key: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消")
        return 1
    if not key:
        print("未输入内容，已取消")
        return 1

    print()
    for r in roots:
        write_key(r, args.provider, key)
        print(f"  [写入] {r.name:24} 现在 = {mask(read_key(r, args.provider))}")

    print("\n[OK] 完成。复验（只报长度，不打印内容）：")
    print(f'  .\\.venv\\Scripts\\python.exe tools\\check_secrets.py config/settings.json')
    print("\n提示：网页设置页里显示的应该是新 Key 的掩码；"
          "建议随便生成一张图确认鉴权通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
