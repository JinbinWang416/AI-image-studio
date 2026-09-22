# -*- coding: utf-8 -*-
"""
离线演示：用修复后的统一链路重跑效果图，验证不再退回模拟背景。

不需要调用任何付费 API —— 只是拿**已有的生成图**重新合成效果图。

用法：
    .\\.venv\\Scripts\\python.exe tools\\demo_fixed_effect.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.effect_background import resolve_render_options  # noqa: E402
from app.effect_renderer import render_storefront_glass  # noqa: E402

STORE = "房屋中介"


def find_source() -> tuple[pathlib.Path, pathlib.Path]:
    """找含真实生成图的最新批次。"""
    batches = sorted(
        (b for b in (ROOT / "output").glob("batch_*") if b.is_dir()), reverse=True
    )
    for batch in batches:
        gen = batch / "01_房屋中介门店" / "01_房屋中介门店生成图"
        if not gen.is_dir():
            continue
        real = [p for p in sorted(gen.glob("*.png")) if p.stat().st_size > 100_000]
        if real:
            return batch, real[0]
    raise SystemExit("未找到含真实生成图的批次")


def main() -> int:
    batch, src = find_source()
    print("=" * 74)
    print("修复后链路演示（不调用付费 API）")
    print("=" * 74)
    print(f"批次 : {batch.name}")
    print(f"源图 : {src.name}")

    cfg = load_config()
    opts = resolve_render_options(cfg, STORE)
    meta = opts.get("background_asset") or {}

    print("\n【解析出的渲染选项】")
    print(f"  背景路径   : {'✅ 有' if opts.get('background') else '❌ 无（会退回模拟背景）'}")
    print(f"  背景门店   : {meta.get('store_hint')}")
    print(f"  来源标记   : {meta.get('kind')}")
    print(f"  玻璃区域   : {opts.get('glass_region')}")
    p = opts.get("params")
    if p is not None:
        d = p.to_dict()
        print(f"  用户参数   : contrast={d['contrast']} saturation={d['saturation']} "
              f"softness={d['softness']} warm={d['warm_strength']}")

    result = render_storefront_glass(src, STORE, **opts)
    out_dir = ROOT / "logs"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / "fixed_effect_demo.png"
    out.write_bytes(result.data)

    print("\n【合成结果】")
    print(f"  输出       : {out.relative_to(ROOT)}")
    print(f"  scene      : {result.background.get('scene')}")
    print(f"  mode       : {result.background.get('mode')}")
    print(f"  label      : {result.background.get('label')}")
    print(f"  反光倍率   : {result.reflection_scale}")
    print(f"  色系倍率   : {result.color_scale}")

    ok = result.background.get("mode") == "ai_generated"
    print("\n" + "=" * 74)
    print(f"结论：{'✅ 已使用 AI 背景（修复生效）' if ok else '❌ 仍在使用模拟背景'}")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
