# -*- coding: utf-8 -*-
"""
对指定批次跑**完整的四层效果图合成**，验证真实产出。

走的是与网页完全相同的链路：
    load_config() → _effect_render_options(cfg, 门店名) → render_storefront_glass()
也就是说会经过：背景自动匹配 → 玻璃区标定 → 四层合成 → 自动曝光。

用法：
    .\\.venv\\Scripts\\python.exe tools\\run_effect_batch.py                 # 用最新批次
    .\\.venv\\Scripts\\python.exe tools\\run_effect_batch.py 01 03           # 只跑部分门店
"""
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFilter, ImageStat  # noqa: E402

from app.config import load_config  # noqa: E402
from app.effect.renderer import (  # noqa: E402
    EFFECT_RENDERER_VERSION,
    EffectRenderError,
    render_storefront_glass,
)
from app.web.server import _effect_render_options  # noqa: E402

OUT_ROOT = ROOT / "output_local_effect_final"


def latest_batch() -> pathlib.Path:
    batches = sorted(
        (p for p in (ROOT / "output").glob("batch_*") if p.is_dir()), reverse=True
    )
    for b in batches:
        for gen in b.rglob("*生成图"):
            if any(p.stat().st_size > 100_000 for p in gen.glob("*.png")):
                return b
    raise SystemExit("未找到含真实生成图的批次")


def stat6(p: pathlib.Path) -> tuple:
    with Image.open(p) as im:
        rgb = im.convert("RGB")
        g = rgb.convert("L")
        w, h = rgb.size
        st = ImageStat.Stat(g)
        rst = ImageStat.Stat(rgb)
        r, _, b = rst.mean
        sat = ImageStat.Stat(rgb.convert("HSV")).mean[1]

        def edge(box):
            return ImageStat.Stat(
                rgb.crop(box).convert("L").filter(ImageFilter.FIND_EDGES)
            ).stddev[0]

        return (
            round(st.mean[0], 1), round(st.stddev[0], 1),
            round(r / max(1.0, b), 3), round(sat, 1),
            round(edge((int(w * .3), int(h * .3), int(w * .7), int(h * .7))), 1),
        )


def main() -> int:
    only = {a.zfill(2) for a in sys.argv[1:]}
    cfg = load_config()
    batch = latest_batch()

    print("=" * 78)
    print("完整效果图产出验证（走网页同款链路）")
    print(f"渲染器：{EFFECT_RENDERER_VERSION}   批次：{batch.name}")
    print("=" * 78)

    total, ok, failed = 0, 0, []
    all_pairs: list[tuple[pathlib.Path, pathlib.Path, str]] = []

    for store_dir in sorted(batch.iterdir()):
        if not store_dir.is_dir():
            continue
        gen = store_dir / f"{store_dir.name}生成图"
        if not gen.is_dir():
            continue
        idx = store_dir.name.split("_")[0]
        if only and idx not in only:
            continue

        sources = [p for p in sorted(gen.glob("*.png")) if p.stat().st_size > 100_000]
        if not sources:
            continue

        store_name = store_dir.name.split("_", 1)[-1].replace("门店", "")
        out_dir = OUT_ROOT / batch.name / store_dir.name
        out_dir.mkdir(parents=True, exist_ok=True)

        opts = _effect_render_options(cfg, store_name)
        bg = opts.get("background")
        meta = opts.get("background_asset") or {}
        print(f"\n【{store_dir.name}】门店名「{store_name}」 → {len(sources)} 张")
        print(f"  背景匹配：{'命中 ' + str(meta.get('store_hint')) if bg else '未命中，用模拟背景'}"
              f"   kind={meta.get('kind') or 'simulated'}")
        print(f"  玻璃区域：{opts.get('glass_region')}")

        for src in sources:
            total += 1
            t0 = time.perf_counter()
            try:
                res = render_storefront_glass(src, store_name, **opts)
            except EffectRenderError as exc:
                failed.append(f"{src.name}: {exc}")
                print(f"    ❌ {src.name}  {exc}")
                continue
            dt = time.perf_counter() - t0
            dst = out_dir / src.name
            dst.write_bytes(res.data)
            ok += 1
            all_pairs.append((src, dst, store_name))
            s = stat6(dst)
            print(f"    ✅ {src.name[:34]:<36}{dt:>5.2f}s  亮度{s[0]:>6}  对比{s[1]:>5}  "
                  f"暖{s[2]:>5}  饱和{s[3]:>5}  反光倍率{res.reflection_scale}")

    # ---------------- 总览图 ----------------
    if all_pairs:
        cell, pad = 330, 6
        cols = 6
        rows = (len(all_pairs) + cols - 1) // cols
        W = cols * cell + (cols + 1) * pad
        H = rows * (cell + 22) + (rows + 1) * pad
        sheet = Image.new("RGB", (W, H), (240, 242, 245))
        d = ImageDraw.Draw(sheet)
        for i, (src, dst, name) in enumerate(all_pairs):
            r, c = divmod(i, cols)
            x = pad + c * (cell + pad)
            y = pad + r * (cell + 22 + pad)
            with Image.open(dst) as im:
                im = im.convert("RGB")
                im.thumbnail((cell, cell), Image.Resampling.LANCZOS)
                sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
            d.rectangle([x, y, x + 76, y + 18], fill=(30, 41, 59))
            d.text((x + 6, y + 4), name[:6], fill=(255, 255, 255))
        OUT_ROOT.mkdir(parents=True, exist_ok=True)
        sheet_path = OUT_ROOT / "_总览_完整效果图.jpg"
        sheet.save(sheet_path, quality=92)
        print(f"\n总览图：{sheet_path.relative_to(ROOT)}  ({W}×{H})")

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 78)
    print(f"成功 {ok} / {total} 张" + (f"，失败 {len(failed)}" if failed else ""))
    if all_pairs:
        vals = [stat6(p) for _, p, _ in all_pairs]
        avg = [round(sum(v[i] for v in vals) / len(vals), 1) for i in range(5)]
        print(f"本批均值：亮度 {avg[0]} / 对比 {avg[1]} / 暖度 {avg[2]} / 饱和 {avg[3]} / 中心 {avg[4]}")
        print("参照系  ：拼多多 77.0/56.4/2.400/152.8/67.1")
        print("          你的产品照 85.1/64.4/1.626/110.5/39.9")
    print(f"产物目录：{OUT_ROOT.relative_to(ROOT)}")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
