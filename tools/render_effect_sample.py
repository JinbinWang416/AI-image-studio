# -*- coding: utf-8 -*-
"""渲染效果图样板（命令行入口）。

配合 ``tools/gen_effect_background.py`` 使用：先生成 AI 门店玻璃背景，
再用本工具把某个门店的**全部成品贴纸**渲染成效果图，并打印亮度指标，
用于快速肉眼验收「明亮 / 真实 / 贴纸不跨框」。

用法：
    # 1) 生成背景（可选，已有背景可跳过）
    .\\.venv\\Scripts\\python.exe tools\\gen_effect_background.py --provider aihive --store 房屋中介 --size 1024x1024

    # 2) 渲染样板
    .\\.venv\\Scripts\\python.exe tools\\render_effect_sample.py --store 房屋中介门店

    # 若还没生成背景，可加 --generate 一步到位
    .\\.venv\\Scripts\\python.exe tools\\render_effect_sample.py --store 房屋中介门店 --generate

输出：
    output/_effect_preview/<贴纸名>_效果图_明亮.png
    output/_effect_preview/ai_background_<门店名>.<ext>
    output/_effect_preview/_对比_BEFORE_AFTER.png（找到旧批次时自动生成）

⚠️ 背景若来自本工具生成的 AI 图，其 ``kind`` 为 ``ai_generated_background``，
与用户实拍（``real_photo``）严格区分，不得作为实拍图使用。
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import shutil
import sys
import time
from pathlib import Path

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageStat  # noqa: E402

from app.core.config import load_config  # noqa: E402
from app.effect.background import (  # noqa: E402
    generate_backgrounds,
    pick_background_for_store,
    resolve_render_options,
)
from app.effect.renderer import render_storefront_glass  # noqa: E402

OUT_DIR = ROOT / "output" / "_effect_preview"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def brightness(path: Path) -> dict:
    """平均亮度 / 对比度 / 四角亮度 —— 用于量化验收「是否发暗」。"""
    with Image.open(path) as im:
        g = im.convert("L")
        w, h = g.size
        stat = ImageStat.Stat(g)
        corners = [
            g.crop((0, 0, w // 6, h // 6)),
            g.crop((w - w // 6, 0, w, h // 6)),
            g.crop((0, h - h // 6, w // 6, h)),
            g.crop((w - w // 6, h - h // 6, w, h)),
        ]
        return {
            "size": f"{w}x{h}",
            "mean": round(stat.mean[0], 1),
            "stddev": round(stat.stddev[0], 1),
            "corners": [round(ImageStat.Stat(c).mean[0], 1) for c in corners],
        }


def find_store_dir(store: str) -> Path | None:
    """在 output/ 下找到最新批次中该门店的生成图目录。"""
    base = ROOT / "output"
    if not base.is_dir():
        return None
    candidates: list[Path] = []
    for batch in sorted(base.glob("batch_*"), reverse=True):
        for d in batch.glob(f"*{store}*"):
            gen = d / f"{d.name}生成图"
            if gen.is_dir() and any(gen.glob("*.png")):
                candidates.append(gen)
    return candidates[0] if candidates else None


def find_old_effect(gen_dir: Path) -> Path | None:
    """找到同批次里已渲染过的效果图（用于生成 BEFORE/AFTER 对比）。"""
    eff = gen_dir.parent / f"{gen_dir.name.replace('生成图', '效果图')}"
    if not eff.is_dir():
        return None
    for p in sorted(eff.glob("*效果图*.png")):
        return p
    return None


async def run(args) -> int:
    cfg = load_config(provider=args.provider or None)
    log(f"服务商 = {cfg.provider} / 模型 = {cfg.model}")
    if args.generate and not getattr(cfg, "api_key", ""):
        log("错误：当前服务商没有 API Key，请在设置页填写（或去掉 --generate 复用已有背景）")
        return 2

    # ---------------- ① 生成 AI 背景（可选） ----------------
    if args.generate:
        log(f"开始生成 AI 背景（{cfg.provider}，约 30~120s）...")

        def on_progress(ev: dict) -> None:
            et = ev.get("type")
            if et == "background_started":
                log(f"  已提交 provider={ev.get('provider')}")
            elif et == "background_saved":
                log(f"  第 {ev.get('index')} 张已保存 id={ev.get('id')} 用时 {ev.get('elapsed')}s")
            elif et == "background_failed":
                log(f"  第 {ev.get('index')} 张失败：{ev.get('error')}")
            elif et == "background_finished":
                log(f"  结束 saved={ev.get('saved')} errors={ev.get('errors')}")

        result = await generate_backgrounds(
            cfg, args.store, args.count, door=args.door, size=args.size,
            on_progress=on_progress,
        )
        if not result["saved"]:
            log("失败：没有生成任何背景")
            for e in result["errors"]:
                log(f"  error: {e}")
            return 3

    # ---------------- ② 回读背景 ----------------
    picked = pick_background_for_store(cfg.output_base_root, args.store)
    if picked is None:
        log(f"资产库里没有可用于「{args.store}」的 AI 背景，"
            f"请加 --generate 先生成一张")
        return 4
    bg_path = Path(picked.path)
    log(f"背景 = {bg_path}")
    log(f"背景亮度 = {brightness(bg_path)}")

    # ---------------- ③ 渲染效果图 ----------------
    opts = resolve_render_options(cfg, args.store)
    log(f"玻璃区 = {opts['glass_region']}  真实感等级 = {opts['realism_iteration']}  "
        f"背景类型 = {opts['background_asset'].get('kind')}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    bg_copy = OUT_DIR / f"ai_background_{args.store}{bg_path.suffix}"
    shutil.copy2(bg_path, bg_copy)

    gen_dir = Path(args.batch) if args.batch else find_store_dir(args.store)
    if gen_dir is None or not gen_dir.is_dir():
        log(f"找不到「{args.store}」的生成图目录，请用 --batch 指定")
        return 5
    stickers = sorted(gen_dir.glob("*.png"))
    log(f"生成图目录 = {gen_dir}（{len(stickers)} 张）")

    first_out: Path | None = None
    for st in stickers:
        try:
            res = render_storefront_glass(st, args.store, **opts)
        except Exception as exc:  # noqa: BLE001
            log(f"  渲染失败 {st.name}: {exc}")
            continue
        out = OUT_DIR / f"{st.stem}_效果图_明亮.png"
        out.write_bytes(res.data)
        first_out = first_out or out
        log(f"  {st.stem} -> {out.name}  亮度={brightness(out)['mean']}")

    # ---------------- ④ BEFORE / AFTER 对比 ----------------
    old = find_old_effect(gen_dir)
    if old and first_out:
        a = Image.open(old).convert("RGB")
        b = Image.open(first_out).convert("RGB")
        W = 760
        a, b = a.resize((W, W)), b.resize((W, W))
        canvas = Image.new("RGB", (W * 2 + 24, W + 56), (255, 255, 255))
        canvas.paste(a, (0, 56))
        canvas.paste(b, (W + 24, 56))
        d = ImageDraw.Draw(canvas)
        d.text((14, 20), f"BEFORE  {brightness(old)}", fill=(180, 40, 40))
        d.text((W + 38, 20), f"AFTER  {brightness(first_out)}", fill=(20, 120, 60))
        cmp_path = OUT_DIR / "_对比_BEFORE_AFTER.png"
        canvas.save(cmp_path)
        log(f"对比图 -> {cmp_path}")

    log(f"完成，输出目录：{OUT_DIR}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染门店效果图样板")
    ap.add_argument("--store", "-s", required=True, help="门店名（如 房屋中介门店）")
    ap.add_argument("--batch", "-b", default="", help="生成图目录；默认自动找最新批次")
    ap.add_argument("--generate", action="store_true", help="渲染前先生成一张 AI 背景")
    ap.add_argument("--provider", "-p", default="", help="服务商；默认用当前配置的")
    ap.add_argument("--count", "-c", type=int, default=1, help="生成背景数量")
    ap.add_argument("--door", "-d", default="single", choices=["single", "double"])
    ap.add_argument("--size", default="1024x1024",
                    help="背景尺寸，须与生成图同比例（2048x2048 贴纸用 1024x1024）")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
