# -*- coding: utf-8 -*-
"""
效果图 —— 用 **AI 生成的真实感背景** 重跑 01 门店，并与模拟背景版对比。

对比三方：
    A. 旧版（两层合成 + 几何色块背景）
    B. 新版四层（模拟背景）
    C. 新版四层 + AI 真实感背景  ← 期望质变

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_effect_v3_ai.py
"""
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageStat  # noqa: E402

from app.effect_renderer import (  # noqa: E402
    EFFECT_RENDERER_VERSION,
    EffectRenderError,
    render_storefront_glass,
)

STORE_DIR_NAME = "01_房屋中介门店"
STORE_DISPLAY = "房屋中介"
OUT_ROOT = ROOT / "output_local_effect_v3_ai"
BG_DIR = ROOT / "output" / "_effect_backgrounds"

# AI 背景中玻璃区域的归一化坐标（竞品约束：贴纸绝不跨过门框/竖梃）
GLASS_REGIONS = {
    # 1:1 双开门 → 左扇
    "ai_20260920_104815_1.png": (0.165, 0.060, 0.482, 0.900),
    "ai_20260920_105006_1.png": (0.095, 0.095, 0.488, 0.893),
    "ai_20260920_105012_2.png": (0.095, 0.095, 0.488, 0.893),
    # 2:3 竖版双开门 → 左扇（与生成图同比例，无裁切）
    "ai_20260920_105131_1.png": (0.075, 0.045, 0.478, 0.925),
    "ai_20260920_105142_2.png": (0.075, 0.045, 0.478, 0.925),
    # 2:3 竖版整块落地玻璃（最佳：玻璃占画面约 80%，贴纸可贴得大而自然）
    "ai_20260920_105257_1.png": (0.030, 0.020, 0.950, 0.930),
    "ai_20260920_105306_2.png": (0.030, 0.020, 0.950, 0.930),
    "__default__": (0.030, 0.020, 0.950, 0.930),
}


def pick_background() -> pathlib.Path:
    """默认取最新一张 AI 背景。"""
    cands = sorted(BG_DIR.glob("ai_*.png"))
    if not cands:
        raise SystemExit("未找到 AI 背景，请先运行 tools/gen_effect_background.py")
    return cands[-1]


def find_inputs() -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """返回 (生成图目录, 旧效果图目录, 模拟背景新版目录)。"""
    batches = sorted((p for p in (ROOT / "output").glob("batch_*") if p.is_dir()), reverse=True)
    gen = old = None
    for b in batches:
        g = b / STORE_DIR_NAME / f"{STORE_DIR_NAME}生成图"
        if g.is_dir() and any(g.glob("*.png")):
            if max(p.stat().st_size for p in g.glob("*.png")) > 100_000:
                gen = g
                e = b / STORE_DIR_NAME / f"{STORE_DIR_NAME}效果图"
                old = e if e.is_dir() else None
                break
    if gen is None:
        raise SystemExit("未找到可用的生成图批次")
    simulated = ROOT / "output_local_effect_v3" / STORE_DIR_NAME
    return gen, old, simulated


def stat_of(p: pathlib.Path) -> dict:
    with Image.open(p) as im:
        rgb = im.convert("RGB")
        g = rgb.convert("L")
        s = ImageStat.Stat(g)
        # 背景亮度：取四角 12% 区域
        w, h = g.size
        mask = Image.new("L", (w, h), 0)
        d = ImageDraw.Draw(mask)
        d.rectangle((0, 0, int(w * 0.12), int(h * 0.12)), fill=255)
        d.rectangle((int(w * 0.88), 0, w, int(h * 0.12)), fill=255)
        d.rectangle((0, int(h * 0.88), int(w * 0.12), h), fill=255)
        d.rectangle((int(w * 0.88), int(h * 0.88), w, h), fill=255)
        corner = ImageStat.Stat(g, mask).mean[0]
        return {
            "mean": round(s.mean[0], 1),
            "sd": round(s.stddev[0], 1),
            "corner": round(corner, 1),
            "match": round(s.mean[0] / max(1.0, corner), 2),
        }


def sheet(rows: list[tuple[str, list[pathlib.Path]]], out: pathlib.Path, cell: int = 380) -> None:
    cols = max(len(r[1]) for r in rows)
    pad, lh = 8, 24
    W = cols * cell + (cols + 1) * pad
    H = len(rows) * (cell + lh) + (len(rows) + 1) * pad
    canvas = Image.new("RGB", (W, H), (243, 245, 248))
    d = ImageDraw.Draw(canvas)
    for ri, (label, items) in enumerate(rows):
        y = pad + ri * (cell + lh + pad)
        d.rectangle([pad, y, pad + 130, y + 20], fill=(30, 41, 59))
        d.text((pad + 7, y + 4), label, fill=(255, 255, 255))
        for ci, p in enumerate(items):
            x = pad + ci * (cell + pad)
            if p and p.exists():
                with Image.open(p) as im:
                    im = im.convert("RGB")
                    im.thumbnail((cell, cell), Image.Resampling.LANCZOS)
                    canvas.paste(im, (x + (cell - im.width) // 2,
                                      y + lh + (cell - im.height) // 2))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=93)
    print(f"对比图：{out.relative_to(ROOT)}  ({W}×{H})")


def main() -> int:
    print("=" * 78)
    print(f"AI 背景效果图验证 —— {STORE_DISPLAY}   渲染器 {EFFECT_RENDERER_VERSION}")
    print("=" * 78)

    gen_dir, old_dir, sim_dir = find_inputs()
    bg = pick_background()
    print(f"生成图  ：{gen_dir.relative_to(ROOT)}")
    print(f"AI 背景 ：{bg.relative_to(ROOT)}")
    region = GLASS_REGIONS.get(bg.name, GLASS_REGIONS["__default__"])
    print(f"玻璃区域：{region}（不跨门框/竖梃）")
    print("-" * 78)

    out_dir = OUT_ROOT / STORE_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)

    bg_bytes = bg.read_bytes()
    bg_meta = {
        "id": bg.stem,
        "file_name": bg.name,
        "kind": "ai_generated_background",
        "label": "AI 生成的门店玻璃背景（非实拍）",
    }

    sources = sorted(gen_dir.glob("*.png"))
    rows_old, rows_sim, rows_ai = [], [], []

    print(f"{'文件':<32}{'AI耗时':>8}{'模拟SD':>8}{'AI版SD':>8}{'AI匹配':>8}")
    print("-" * 78)

    for src in sources:
        theme = src.stem.split("_", 2)[-1]

        old = None
        if old_dir:
            c = [p for p in old_dir.glob("*.png") if theme in p.name]
            old = c[0] if c else None
        sim = sim_dir / src.name if sim_dir.is_dir() else None

        t0 = time.perf_counter()
        try:
            res = render_storefront_glass(
                src, STORE_DISPLAY,
                background=bg_bytes,
                background_asset=bg_meta,
                realism_iteration=3,
                glass_region=region,
            )
        except EffectRenderError as exc:
            print(f"{src.name:<32}  ❌ {exc}")
            continue
        dt = time.perf_counter() - t0

        dst = out_dir / src.name
        dst.write_bytes(res.data)

        s_ai = stat_of(dst)
        s_sim = stat_of(sim) if sim and sim.exists() else {"sd": 0, "match": 0}
        rows_old.append(old)
        rows_sim.append(sim)
        rows_ai.append(dst)

        print(f"{src.name:<32}{dt:>7.2f}s{s_sim['sd']:>8.1f}{s_ai['sd']:>8.1f}{s_ai['match']:>8.2f}")

    print("-" * 78)

    if rows_ai:
        ai_sd = [stat_of(p)["sd"] for p in rows_ai]
        ai_m = [stat_of(p)["match"] for p in rows_ai]
        print("\n【量化】")
        print(f"  新版(模拟背景) 对比度均值 : {sum(stat_of(p)['sd'] for p in rows_sim if p and p.exists())/max(1,len([p for p in rows_sim if p and p.exists()])):.1f}")
        print(f"  新版(AI 背景) 对比度均值 : {sum(ai_sd)/len(ai_sd):.1f}")
        print(f"  AI 版环境匹配度均值      : {sum(ai_m)/len(ai_m):.2f}  （越接近 1 越贴合）")

    sheet([
        ("A 旧版·几何背景", rows_old),
        ("B 新版·模拟背景", rows_sim),
        ("C 新版·AI 背景", rows_ai),
    ], OUT_ROOT / f"_三方对比_{STORE_DIR_NAME}.jpg")

    # 校验
    ok = 0
    for p in sorted(out_dir.glob("*.png")):
        with Image.open(p) as im:
            if im.format == "PNG" and im.width >= 512:
                ok += 1
    print(f"\n输出校验：PNG 合法 {ok}/{len(sources)} 张")
    print(f"产物目录：{OUT_ROOT.relative_to(ROOT)}")
    print("=" * 78)
    return 0 if rows_ai else 1


if __name__ == "__main__":
    sys.exit(main())
