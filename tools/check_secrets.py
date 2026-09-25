# -*- coding: utf-8 -*-
"""检查配置文件里是否含真实 API Key。

⚠️ 只输出「有/无」和长度，**绝不打印 Key 内容**（AGENTS.md 硬约束）。
⚠️ 退出码：发现 Key 返回 1 —— 可直接串进推送前的 gate。

用法：
    python tools/check_secrets.py config/settings.json
    python tools/check_secrets.py config/*.bak-*          # 通配由 shell 展开
    python tools/check_secrets.py --glob config           # 自动扫 config 下所有 json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

KEY_FIELDS = ("api_key", "apikey", "token", "secret", "password")


def scan(path: Path) -> int:
    """扫一个 JSON 文件，返回命中的 Key 字段数。"""
    if not path.exists():
        print(f"  [跳过] {path} 不存在")
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"  [!] {path} 解析失败：{exc}")
        return 0
    if not isinstance(data, dict):
        print(f"  [--] {path} 不是对象，跳过")
        return 0

    hits = 0

    def walk(node: object, prefix: str) -> None:
        nonlocal hits
        if isinstance(node, dict):
            for k, v in node.items():
                name = f"{prefix}.{k}" if prefix else str(k)
                if isinstance(v, str):
                    if any(f in str(k).lower() for f in KEY_FIELDS) and v:
                        hits += 1
                        print(f"  [!!] {path} -> {name} 非空（长度 {len(v)}）")
                else:
                    walk(v, name)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{prefix}[{i}]")

    walk(data, "")
    if not hits:
        print(f"  [OK] {path} 不含任何非空密钥字段")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", help="要检查的文件")
    ap.add_argument("--glob", dest="glob_dir", help="扫描该目录下所有 .json / .bak-* 文件")
    args = ap.parse_args()

    targets: list[Path] = [Path(p) for p in args.paths]
    if args.glob_dir:
        base = Path(args.glob_dir)
        targets += sorted(
            p for p in base.rglob("*")
            if p.is_file() and (p.suffix == ".json" or ".bak" in p.name or "polluted" in p.name)
        )
    if not targets:
        targets = [Path("config/settings.json")]

    print(f"检查 {len(targets)} 个文件（只报有无，不打印内容）：")
    total = sum(scan(t) for t in targets)
    print(f"\n合计命中非空密钥字段：{total}")
    if total:
        print("[!!] 存在真实密钥 —— 这些文件绝不能进入 git / 推送 / 日志")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
