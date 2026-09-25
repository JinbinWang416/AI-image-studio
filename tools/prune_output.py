# -*- coding: utf-8 -*-
"""精简 output/：只保留指定批次，其余**移到备份区**（不直接删除）。

用户确认的保留范围：
    ✅ 保留  batch_20260919_014522_qwen_qwen-image-3.0   （23 门店 × 6 图 = 138 PNG）
    ✅ 保留  _抽检清单.csv / _拼多多标题.csv             （产出文件）
    ➡️ 移走  其余 9 个历史批次
    ➡️ 移走  旧结构的 23 个门店目录
    ➡️ 移走  _references / _effect_backgrounds / _effect_preview（用户资产，126 MB）

⚠️ 采用「移动到备份区」而不是 `shutil.rmtree`：
    · 用户资产（门店实拍照片、参考图库）删了不可逆，需重新上传
    · 备份区内保留完整，确认无误后可一条命令彻底删除
    · 也符合 AGENTS.md「绝不删除 output/ 里数据」的精神

用法：
    python tools/prune_output.py                # 预览（不动任何东西）
    python tools/prune_output.py --apply        # 执行移动
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
KEEP_BATCH = "batch_20260919_014522_qwen_qwen-image-3.0"
KEEP_FILES = {"_抽检清单.csv", "_拼多多标题.csv"}

# 用户资产目录（要移走的）
ASSET_DIRS = {"_references", "_effect_backgrounds", "_effect_preview"}


def size_of(p: Path) -> tuple[int, int]:
    files = [f for f in p.rglob("*") if f.is_file()] if p.is_dir() else ([p] if p.is_file() else [])
    return len(files), sum(f.stat().st_size for f in files)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行移动（默认只预览）")
    args = ap.parse_args()

    if not OUT.is_dir():
        print(f"  ❌ output/ 不存在：{OUT}")
        return 1

    keep = OUT / KEEP_BATCH
    if not keep.is_dir():
        print(f"  ❌ 要保留的批次不存在：{KEEP_BATCH}")
        return 1

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / f"output_backup_{stamp[:8]}"

    # 分类
    to_move: list[tuple[str, Path, str]] = []   # (类别, 路径, 说明)

    for p in sorted(OUT.iterdir()):
        if p.name == KEEP_BATCH:
            continue
        if p.is_file():
            if p.name in KEEP_FILES:
                continue
            to_move.append(("文件", p, "非产出文件"))
            continue
        # 目录
        if p.name in ASSET_DIRS:
            n, s = size_of(p)
            to_move.append(("用户资产", p, f"{n} 文件 / {s/1024/1024:.1f} MB"))
        elif p.name.startswith("batch_"):
            n, s = size_of(p)
            to_move.append(("历史批次", p, f"{n} 文件 / {s/1024/1024:.1f} MB"))
        elif p.name[:2].isdigit():
            n, s = size_of(p)
            to_move.append(("旧结构门店", p, f"{n} 文件 / {s/1024/1024:.1f} MB"))
        else:
            n, s = size_of(p)
            to_move.append(("其它", p, f"{n} 文件 / {s/1024/1024:.1f} MB"))

    print("=" * 78)
    print("output/ 精简" + ("（执行）" if args.apply else "（预览，不动任何东西）"))
    print("=" * 78)

    kn, ks = size_of(keep)
    print(f"\n✅ 保留：{KEEP_BATCH}")
    print(f"        {kn} 文件 / {ks/1024/1024:.1f} MB")
    for name in sorted(KEEP_FILES):
        p = OUT / name
        if p.is_file():
            print(f"        {name}  {p.stat().st_size/1024:.1f} KB")

    print(f"\n➡️  移走 {len(to_move)} 项：")
    total_n = total_s = 0
    by_cat: dict[str, list] = {}
    for cat, p, note in to_move:
        by_cat.setdefault(cat, []).append((p, note))
        n, s = size_of(p)
        total_n += n
        total_s += s
    for cat, items in by_cat.items():
        print(f"\n  【{cat}】{len(items)} 项")
        for p, note in items[:6]:
            print(f"      {p.name:<52} {note}")
        if len(items) > 6:
            print(f"      … 其余 {len(items) - 6} 项")

    print(f"\n  合计移走 {total_n} 文件 / {total_s/1024/1024:.1f} MB")
    print(f"  备份区：{backup}")

    if not args.apply:
        print("\n  ⏭  预览模式。执行请加 --apply")
        print("=" * 78)
        return 0

    # 执行移动
    backup.mkdir(parents=True, exist_ok=True)
    moved = failed = 0
    for cat, p, _ in to_move:
        dest = backup / p.name
        try:
            if dest.exists():
                dest = backup / f"{p.name}.dup"
            shutil.move(str(p), str(dest))
            moved += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 移动失败 {p.name}: {type(exc).__name__}: {exc}")
            failed += 1

    # 写一份说明，方便日后还原
    (backup / "_README_这是备份不是删除.txt").write_text(
        "本目录是从 output/ 移出的内容，**尚未删除**。\n\n"
        f"移出时间：{datetime.now().isoformat(timespec='seconds')}\n"
        f"保留在 output/ 的：{KEEP_BATCH}\n\n"
        "## 分类\n"
        "  _references / _effect_backgrounds / _effect_preview\n"
        "      → 用户上传的参考图与门店玻璃实拍照片（效果图合成依赖）\n"
        "  batch_*\n"
        "      → 历史批次\n"
        "  NN_门店名/\n"
        "      → 旧结构的门店目录\n\n"
        "## 还原\n"
        "  直接把这些目录/文件移回 output/ 即可。\n\n"
        "## 彻底删除\n"
        "  确认不需要后，删除整个备份目录即可。\n",
        encoding="utf-8",
    )

    print(f"\n  ✅ 已移走 {moved} 项" + (f"，失败 {failed} 项" if failed else ""))
    print("\n  现在 output/ 内容：")
    for p in sorted(OUT.iterdir()):
        print(f"      {'[D]' if p.is_dir() else '[F]'} {p.name}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
