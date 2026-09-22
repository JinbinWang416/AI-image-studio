# -*- coding: utf-8 -*-
"""
验证「效果图背景自动匹配」——23 个门店是否都能命中对应行业的 AI 背景。

背景来源优先级（由 `_effect_render_options` 实现）：
    1. 用户明确选择的背景（实拍或 AI）
    2. 按门店名自动匹配的同行业 AI 背景
    3. 都没有 → 模拟背景

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_background_match.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.effect_background import pick_background_for_store  # noqa: E402
from app.store_repo import StoreRepository  # noqa: E402


def main() -> int:
    cfg = load_config()
    stores = StoreRepository().load()

    print("=" * 76)
    print("效果图背景自动匹配验证")
    print("=" * 76)
    print(f"输出根目录 : {cfg.output_base_root}")
    print(f"自动匹配   : {'开启' if cfg.auto_match_effect_background else '关闭'}")
    print("-" * 76)

    hit = 0
    miss: list[str] = []

    for s in stores:
        asset = pick_background_for_store(cfg.output_base_root, s.main_title)
        if asset is not None:
            hit += 1
            hint = asset.extra.get("store_hint", "")
            region = asset.extra.get("glass_region", [])
            print(f"  {s.folder_index}  {s.main_title:<10} → 命中 [{hint}]  "
                  f"region={region}")
        else:
            miss.append(f"{s.folder_index} {s.main_title}")
            print(f"  {s.folder_index}  {s.main_title:<10} → ❌ 无匹配（将回退模拟背景）")

    print("-" * 76)
    print(f"命中 {hit} / {len(stores)} 个门店")
    if miss:
        print("未命中：")
        for m in miss:
            print(f"  - {m}")

    # 边界用例
    print("\n【边界用例】")
    for name in ("不存在的行业XYZ", "", "房屋中介"):
        a = pick_background_for_store(cfg.output_base_root, name)
        label = a.extra.get("store_hint") if a else "(None → 回退模拟背景)"
        print(f"  {name!r:<18} → {label}")

    # ---------------- 端到端：渲染链路是否真的用上 AI 背景 ----------------
    print("\n【端到端链路验证】[_effect_render_options]")
    try:
        from app.web.server import _effect_render_options

        for name in ("房屋中介", "灯饰照明", "不存在的行业XYZ"):
            opts = _effect_render_options(cfg, name)
            bg = opts.get("background")
            meta = opts.get("background_asset") or {}
            region = opts.get("glass_region")
            kind = meta.get("kind") or ("simulated" if bg is None else "real_photo")
            print(f"  {name:<14} → background={'有' if bg else '无'}  "
                  f"kind={kind}  region={region}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 无法加载 server 模块：{exc}")
        return 1

    # ---------------- 实际渲染：验证来源标记正确 ----------------
    print("\n【实际渲染验证】来源标记")
    try:
        from app.effect_renderer import render_storefront_glass
        from app.web.server import _effect_render_options

        src = None
        for batch in sorted((ROOT / "output").glob("batch_*"), reverse=True):
            gen = batch / "01_房屋中介门店" / "01_房屋中介门店生成图"
            if gen.is_dir():
                for p in sorted(gen.glob("*.png")):
                    if p.stat().st_size > 100_000:
                        src = p
                        break
            if src:
                break

        if src is None:
            print("  ⚠️ 未找到可用的真实生成图，跳过")
        else:
            for name in ("房屋中介", "灯饰照明", "不存在的行业XYZ"):
                opts = _effect_render_options(cfg, name)
                result = render_storefront_glass(src, name, **opts)
                meta = result.metadata(pathlib.Path("_verify.png"))
                bg = meta["background"]
                print(f"  {name:<14} → mode={bg['mode']:<22} "
                      f"scene={meta['scene']}")
                if bg.get("notice"):
                    print(f"                   ⚠️ {bg['notice']}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ 渲染验证失败：{exc}")
        return 1

    print("=" * 76)
    return 0 if hit == len(stores) else 1


if __name__ == "__main__":
    sys.exit(main())
