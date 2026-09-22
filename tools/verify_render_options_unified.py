# -*- coding: utf-8 -*-
"""
验证「批量流程与网页流程使用同一套效果图选项」。

背景：`app/orchestrator.py` 曾直接读 `cfg.effect_background_asset`，
      绕过背景自动匹配、glass_region 与用户参数，导致批量产出是模拟背景。
      修复后两条路径都走 `app.effect_background.resolve_render_options`。

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_render_options_unified.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.effect_background import resolve_render_options  # noqa: E402
from app.store_repo import StoreRepository  # noqa: E402
from app.web.server import _effect_render_options  # noqa: E402


def main() -> int:
    cfg = load_config()
    stores = StoreRepository().load()

    print("=" * 78)
    print("效果图选项统一性验证")
    print("=" * 78)

    # 用同一个门店分别走两条路径
    sample = None
    for s in stores:
        r = resolve_render_options(cfg, s.main_title)
        if r.get("background") is not None:
            sample = s
            break

    if sample is None:
        print("❌ 没有任何门店匹配到背景，请先检查背景库")
        return 1

    print(f"样本门店：{sample.folder_index} {sample.main_title}\n")

    shared = resolve_render_options(cfg, sample.main_title)
    legacy = _effect_render_options(cfg, sample.main_title)

    keys = ("background", "background_asset", "realism_iteration", "glass_region", "params")
    print(f"{'键':<20}{'共用函数':<28}{'server 封装':<28}{'一致?'}")
    print("-" * 78)
    same = True
    for k in keys:
        a, b = shared.get(k), legacy.get(k)

        def brief(v):
            if v is None:
                return "None"
            if isinstance(v, dict):
                return f"dict(kind={v.get('kind')}, store={v.get('store_hint')})"
            if hasattr(v, "to_dict"):
                d = v.to_dict()
                return f"EffectParams(contrast={d.get('contrast')}, sat={d.get('saturation')})"
            if isinstance(v, tuple) and len(v) == 4:
                return f"tuple{tuple(round(x, 3) for x in v)}"
            return str(v)[:26]

        ok = (a == b) or (a is not None and b is not None and str(a) == str(b))
        same = same and ok
        print(f"{k:<20}{brief(a):<28}{brief(b):<28}{'✅' if ok else '❌'}")
    print("-" * 78)

    # 关键字段检查
    print("\n【关键字段】")
    bg = shared.get("background")
    meta = shared.get("background_asset") or {}
    params = shared.get("params")
    print(f"  背景命中      : {'✅ ' + str(meta.get('store_hint')) if bg else '❌ 未命中'}")
    print(f"  来源标记 kind : {meta.get('kind') or '（无）'}")
    print(f"  玻璃区域      : {shared.get('glass_region')}")
    if params is not None:
        d = params.to_dict()
        print(f"  用户参数已传入: ✅ contrast={d['contrast']} saturation={d['saturation']} "
              f"softness={d['softness']}")

    print("\n" + "=" * 78)
    print(f"结论：{'✅ 两条路径完全一致' if same else '❌ 存在不一致'}")
    print("=" * 78)
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
