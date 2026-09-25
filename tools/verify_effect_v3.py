# -*- coding: utf-8 -*-
"""
效果图四层合成 —— 01 房屋中介门店闭环验证。

做三件事：
  1. 用新版 `app/effect_renderer.py`（四层架构）重新合成 01 门店的 6 张效果图
  2. 与旧版效果图（历史批次里的）做**量化对比**（亮度/对比度/环境亮度匹配度）
  3. 输出横向对比图，供人工目视验收

用法：
    .\\.venv\\Scripts\\python.exe tools\\verify_effect_v3.py
"""
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageStat  # noqa: E402

from app.effect.renderer import (  # noqa: E402
    EFFECT_RENDERER_VERSION,
    EffectRenderError,
    render_storefront_glass,
)

STORE_DIR_NAME = "01_房屋中介门店"
STORE_DISPLAY = "房屋中介"
OUT_ROOT = ROOT / "output_local_effect_v3"


def find_latest_batch() -> tuple[pathlib.Path, pathlib.Path]:
    """找到 01 门店最新批次的 (生成图目录, 旧效果图目录)。"""
    batches = sorted(
        (p for p in (ROOT / "output").glob("batch_*") if p.is_dir()),
        reverse=True,
    )
    for batch in batches:
        store = batch / STORE_DIR_NAME
        if not store.is_dir():
            continue
        gen = store / f"{STORE_DIR_NAME}生成图"
        eff = store / f"{STORE_DIR_NAME}效果图"
        if gen.is_dir() and any(gen.glob("*.png")):
            # 只要生成图够大（排除 mock 占位图），就用这一批
            biggest = max(p.stat().st_size for p in gen.glob("*.png"))
            if biggest > 100_000:
                return gen, eff
    raise SystemExit("未找到可用的生成图批次（需要大于 100KB 的真实生成图）")


def metrics(path: pathlib.Path) -> dict:
    """统计一张图的亮度与对比度，用于量化对比。"""
    with Image.open(path) as im:
        rgb = im.convert("RGB")
        gray = rgb.convert("L")
        stat = ImageStat.Stat(gray)
        stat_rgb = ImageStat.Stat(rgb)
        # 边缘区域的亮度（用于判断背景明亮程度）
        w, h = gray.size
        border = Image.new("L", (w, h), 0)
        d = ImageDraw.Draw(border)
        d.rectangle((0, 0, w, int(h * 0.10)), fill=255)
        d.rectangle((0, int(h * 0.90), w, h), fill=255)
        border_mean = ImageStat.Stat(gray, border).mean[0]
        return {
            "size": f"{w}×{h}",
            "mean": round(stat.mean[0], 1),
            "stddev": round(stat.stddev[0], 1),
            "rms": round(stat.rms[0], 1),
            "border_mean": round(border_mean, 1),
            "rgb_mean": tuple(round(v, 1) for v in stat_rgb.mean),
            "kb": round(path.stat().st_size / 1024),
        }


def build_compare_sheet(pairs: list[tuple[pathlib.Path, pathlib.Path]],
                        out_path: pathlib.Path, cell: int = 420) -> None:
    """上排旧版、下排新版，做 6 列对比图。"""
    cols = len(pairs)
    pad = 8
    label_h = 26
    W = cols * cell + (cols + 1) * pad
    H = 2 * cell + label_h * 3 + pad * 3
    sheet = Image.new("RGB", (W, H), (244, 246, 249))
    draw = ImageDraw.Draw(sheet)

    draw.text((pad, pad // 2 + 4), "上排：旧版两层合成     下排：新版四层合成",
              fill=(60, 70, 85))

    for i, (old, new) in enumerate(pairs):
        x = pad + i * (cell + pad)
        for row, src in enumerate((old, new)):
            y = label_h + pad + row * (cell + label_h + pad)
            if src and src.exists():
                with Image.open(src) as im:
                    im = im.convert("RGB")
                    im.thumbnail((cell, cell), Image.Resampling.LANCZOS)
                    sheet.paste(im, (x + (cell - im.width) // 2,
                                     y + (cell - im.height) // 2))
            tag = "旧版" if row == 0 else "新版"
            draw.rectangle([x, y, x + 46, y + 20], fill=(30, 41, 59))
            draw.text((x + 7, y + 4), tag, fill=(255, 255, 255))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, quality=92)
    print(f"对比图已输出：{out_path.relative_to(ROOT)}  ({W}×{H})")


def main() -> int:
    print("=" * 76)
    print(f"效果图四层合成闭环验证 —— {STORE_DISPLAY}")
    print(f"渲染器版本：{EFFECT_RENDERER_VERSION}")
    print("=" * 76)

    gen_dir, old_eff_dir = find_latest_batch()
    print(f"生成图目录：{gen_dir.relative_to(ROOT)}")
    print(f"旧效果图目录：{old_eff_dir.relative_to(ROOT)}"
          f"（{'存在' if old_eff_dir.is_dir() else '不存在'}）")

    out_dir = OUT_ROOT / STORE_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)

    sources = sorted(gen_dir.glob("*.png"))
    if not sources:
        print("❌ 生成图目录为空")
        return 1

    rows = []
    pairs: list[tuple[pathlib.Path, pathlib.Path]] = []

    print("\n" + "-" * 76)
    print(f"{'文件':<34}{'耗时':>8}{'旧亮度':>9}{'新亮度':>9}{'环境匹配':>10}")
    print("-" * 76)

    for src in sources:
        # 找到对应的旧效果图（文件名可能带时间戳前缀，用主题关键字匹配）
        theme = src.stem.split("_", 2)[-1]        # 如 "01_楼房线稿"
        old = None
        if old_eff_dir.is_dir():
            cands = [p for p in old_eff_dir.glob("*.png") if theme in p.name]
            old = cands[0] if cands else None

        t0 = time.perf_counter()
        try:
            result = render_storefront_glass(src, STORE_DISPLAY, realism_iteration=3)
        except EffectRenderError as exc:
            print(f"{src.name:<34}  ❌ {exc}")
            continue
        elapsed = time.perf_counter() - t0

        dst = out_dir / src.name
        dst.write_bytes(result.data)

        m_new = metrics(dst)
        m_old = metrics(old) if old else None

        # 环境亮度匹配度：贴纸整体亮度与画面边缘（背景）亮度的比值，越接近 1 越自然
        match = round(m_new["mean"] / max(1.0, m_new["border_mean"]), 2)

        rows.append((src.name, elapsed, m_old, m_new, match))
        pairs.append((old, dst))

        print(f"{src.name:<34}{elapsed:>7.2f}s"
              f"{(m_old['mean'] if m_old else 0):>9.1f}{m_new['mean']:>9.1f}{match:>10.2f}")

    print("-" * 76)

    # ---------------- 量化对比 ----------------
    print("\n【量化对比】")
    if rows and rows[0][2]:
        old_means = [r[2]["mean"] for r in rows if r[2]]
        new_means = [r[3]["mean"] for r in rows]
        old_sd = [r[2]["stddev"] for r in rows if r[2]]
        new_sd = [r[3]["stddev"] for r in rows]
        print(f"  画面平均亮度   旧版 {sum(old_means)/len(old_means):>6.1f}   →   新版 {sum(new_means)/len(new_means):>6.1f}")
        print(f"  对比度(标准差) 旧版 {sum(old_sd)/len(old_sd):>6.1f}   →   新版 {sum(new_sd)/len(new_sd):>6.1f}")
        print(f"  平均环境匹配度 {sum(r[4] for r in rows)/len(rows):>6.2f}  （越接近 1 表示贴纸亮度越贴合环境）")
    else:
        print("  （无旧效果图可比，仅输出新版）")

    # ---------------- 对比图 ----------------
    print()
    build_compare_sheet(pairs, OUT_ROOT / f"_对比图_{STORE_DIR_NAME}.jpg")

    # ---------------- 校验输出 ----------------
    print("\n【输出校验】")
    ok = 0
    for p in sorted(out_dir.glob("*.png")):
        with Image.open(p) as im:
            valid = im.format == "PNG" and im.width >= 512
        if valid:
            ok += 1
    print(f"  PNG 合法性：{ok}/{len(sources)} 张通过")

    meta = render_storefront_glass(sources[0], STORE_DISPLAY, realism_iteration=3).metadata(
        out_dir / sources[0].name
    )
    print(f"  渲染层级：{' → '.join(meta['layers'])}")
    print(f"  场景标记：{meta['scene']}（{meta['background'].get('label')}）")
    if meta["background"].get("notice"):
        print(f"  ⚠️  {meta['background']['notice']}")

    print("\n" + "=" * 76)
    print(f"闭环完成。产物目录：{OUT_ROOT.relative_to(ROOT)}")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main())
