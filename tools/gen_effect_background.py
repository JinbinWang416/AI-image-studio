# -*- coding: utf-8 -*-
"""用图像服务商生成真实感门店玻璃背景（命令行入口）。

核心逻辑在 `app/effect_background.py`，本脚本只做参数解析与输出。
网页端等价接口：``POST /api/effect-backgrounds/generate``。

⚠️ 生成的背景标记为 ``ai_generated_background``，与用户实拍（``real_photo``）
严格区分，不得作为实拍图使用。

用法：
    .\\.venv\\Scripts\\python.exe tools\\gen_effect_background.py --store 房屋中介 --count 2
    .\\.venv\\Scripts\\python.exe tools\\gen_effect_background.py --all-stores --count 2   # 23 个行业各生成
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.effect.background import generate_backgrounds  # noqa: E402
from app.state.store_repo import StoreRepository  # noqa: E402


def _store_names() -> list[str]:
    """从数据包读取 23 个门店的主标题（用于店内描述匹配）。"""
    try:
        return [s.main_title for s in StoreRepository().load()]
    except Exception:  # noqa: BLE001
        return []


async def run(provider_name: str, stores: list[str], count: int, door: str, size: str) -> int:
    cfg = load_config(provider_name)
    problems = cfg.validate()
    if problems:
        print("❌ 配置不可用：")
        for p in problems:
            print(f"   - {p}")
        return 1

    print(f"服务商：{cfg.provider_label} / {cfg.model}")
    print(f"门型  ：{door}    尺寸：{size}")
    print(f"门店  ：{len(stores)} 个，每店 {count} 张")
    print("-" * 70)

    total_saved = 0
    total_errors: list[str] = []

    for idx, name in enumerate(stores, 1):
        print(f"\n[{idx}/{len(stores)}] {name}")

        def on_progress(ev: dict) -> None:
            if ev["type"] == "background_saved":
                print(f"    ✅ {ev['file_name']}  ({ev['elapsed']}s)")
            elif ev["type"] == "background_progress":
                print(f"    生成中 {ev['index']}/{ev['total']} …")
            elif ev["type"] == "background_failed":
                print(f"    ❌ {ev['error'][:110]}")

        result = await generate_backgrounds(
            cfg, name, count, door=door, size=size, on_progress=on_progress
        )
        total_saved += result["saved"]
        total_errors.extend(result["errors"])

    print("\n" + "=" * 70)
    print(f"完成：成功 {total_saved} 张，失败 {len(total_errors)} 次")
    if total_errors:
        print("失败摘要：")
        for e in total_errors[:5]:
            print(f"  - {e[:140]}")
    print("⚠️  这些是 AI 生成的模拟背景（ai_generated_background），")
    print("    与用户实拍照片（real_photo）严格区分，不得作为实拍图使用。")
    print("=" * 70)
    return 0 if total_saved else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="生成真实感门店玻璃背景")
    ap.add_argument("--provider", "-p", default="qwen", help="服务商（默认 qwen）")
    ap.add_argument("--store", "-s", default="房屋中介", help="门店主标题（决定店内描述）")
    ap.add_argument("--all-stores", action="store_true", help="为数据包里全部 23 个门店各生成")
    ap.add_argument("--count", "-c", type=int, default=1, help="每个门店生成数量")
    ap.add_argument("--door", "-d", default="single", choices=["single", "double"],
                    help="门型：single 整块落地玻璃（推荐）/ double 双开")
    ap.add_argument("--size", default="1024x1536",
                    help="背景尺寸，须与生成图同比例（默认 1024x1536）")
    args = ap.parse_args()

    stores = _store_names() if args.all_stores else [args.store]
    if not stores:
        print("❌ 未能读取门店列表")
        return 1
    return asyncio.run(run(args.provider, stores, args.count, args.door, args.size))


if __name__ == "__main__":
    sys.exit(main())
