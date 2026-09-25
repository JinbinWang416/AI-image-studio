# -*- coding: utf-8 -*-
"""把 output_local_validation 的结构对齐到代码期望（与 output/ 一致）。

## 问题

`app/local_validation.py` 用的是 `Storage(root).generated_dir(store_dir)`，
它返回 `<门店>/<门店>生成图/` —— 但历史上的文件**裸放在 `<门店>/`**：

    output_local_validation/01_房屋中介门店/
    ├── 01_楼房线稿.png          ← 实际在这
    ├── _manifest.json
    └── _history/

    代码却在找：
    output_local_validation/01_房屋中介门店/01_房屋中介门店生成图/*.png

→ **验证功能读不到图**（`build_validation_report` 会认为图不存在）。

## 修法（对齐 output/ 的结构）

    1. 建 `<门店>/<门店>生成图/`
    2. 把 6 张 PNG 移进去
    3. 更新 `_manifest.json` 里每条 entry 的 `image_path`
    4. `_history/` 保持不动

⚠️ 采用**复制 + 校验 + 删原文件**的顺序，失败时原文件还在。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOTS = [
    Path(r"E:\1_Software\6_AI工具\deepseek\2_开发\图片生成"),
    Path(r"E:\1_Software\6_AI工具\deepseek\2_开发\AI生图架构版"),
]

GENERATED_SUFFIX = "生成图"


def read_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8-sig"))


def write_json(p: Path, d: dict) -> None:
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fix_root(root: Path, apply: bool) -> tuple[int, int]:
    """返回 (移动的 PNG 数, 更新的 manifest 条数)。"""
    vroot = root / "output_local_validation"
    if not vroot.is_dir():
        print(f"    ⏭  {root.name}：无 output_local_validation")
        return 0, 0

    moved = updated = 0
    for store_dir in sorted(p for p in vroot.iterdir() if p.is_dir()):
        name = store_dir.name
        gen = store_dir / f"{name}{GENERATED_SUFFIX}"
        pngs = sorted(p for p in store_dir.glob("*.png") if not p.name.startswith("_"))
        if not pngs:
            print(f"    ⏭  {name}：根目录已无 PNG（可能已对齐）")
            continue

        print(f"    📁 {name}：{len(pngs)} 张 PNG → {gen.name}/")
        if apply:
            gen.mkdir(parents=True, exist_ok=True)
            for p in pngs:
                dst = gen / p.name
                if dst.exists():
                    continue
                shutil.copy2(p, dst)          # 先复制
                if dst.stat().st_size == p.stat().st_size:
                    p.unlink()                 # 校验一致再删原文件
                    moved += 1
                else:
                    print(f"        ⚠️ 大小不一致，保留原文件：{p.name}")

            # 更新 manifest 的 image_path
            mf = store_dir / "_manifest.json"
            if mf.is_file():
                try:
                    d = read_json(mf)
                    ents = d.get("entries") or {}
                    n = 0
                    for key, ent in ents.items():
                        fname = ent.get("file_name") or ""
                        if not fname:
                            continue
                        target = gen / fname
                        if target.is_file():
                            ent["image_path"] = str(target)
                            n += 1
                    if n:
                        write_json(mf, d)
                        updated += n
                        print(f"        ✅ 更新 manifest：{n} 条 image_path")
                except Exception as exc:  # noqa: BLE001
                    print(f"        ⚠️ manifest 更新失败：{type(exc).__name__}: {exc}")
        else:
            moved += len(pngs)
    return moved, updated


def main() -> int:
    apply = "--apply" in sys.argv
    print("=" * 78)
    print("output_local_validation 结构对齐" + ("（执行）" if apply else "（预览）"))
    print("=" * 78)
    total_m = total_u = 0
    for root in ROOTS:
        if not root.is_dir():
            continue
        print(f"\n  【{root.name}】")
        m, u = fix_root(root, apply)
        total_m += m
        total_u += u
    print()
    print("=" * 78)
    if apply:
        print(f"  ✅ 已移动 {total_m} 张 PNG，更新 {total_u} 条 manifest")
    else:
        print(f"  预览：将移动 {total_m} 张 PNG")
        print("  执行请加 --apply")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
