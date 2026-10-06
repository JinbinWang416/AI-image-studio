# -*- coding: utf-8 -*-
"""为全部 23 个门店各生成一个「蒙泰应能直接打印」的测试 TIF。

用途：多测几张，确认白墨极性问题是**个别图案**还是**普遍**的。

用法::

    python tools\\make_print_test.py          # 全部 23 个门店，每店 1 张
    python tools\\make_print_test.py 04 01    # 只做指定门店
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.print_export.print_export import PrintExporter  # noqa: E402
from app.print_export.stacked import describe  # noqa: E402
from app.state.store_repo import StoreRepository  # noqa: E402

SRC_BATCH = ROOT / "output" / "batch_20260919_014522_qwen_qwen-image-3.0"
DST = ROOT / "output" / "_print_test"


def pick_source(store_dir: Path, store_name: str, index: str) -> Path | None:
    """拿该门店的**第一张**生成图（新结构子目录或旧结构根目录都认）。"""
    cands: list[Path] = []
    for base in (store_dir / f"{store_name}生成图", store_dir):
        if base.is_dir():
            cands += [p for p in base.glob("*.png")
                      if "_效果图" not in p.name and "_up" not in p.stem]
    if not cands:
        return None
    cands = [p for p in cands if f"_{index}_" in p.name] or cands
    return sorted(cands)[0]


def main() -> int:
    wanted = [a for a in sys.argv[1:] if a.isdigit()]
    stores = {s.folder_index: s for s in StoreRepository().load()}
    DST.mkdir(parents=True, exist_ok=True)

    ok: list[tuple[str, Path, str, dict, object]] = []
    skipped: list[str] = []

    for idx in sorted(stores):
        if wanted and idx not in wanted:
            continue
        s = stores[idx]
        store_dir = SRC_BATCH / s.output_dir
        if not store_dir.is_dir():
            skipped.append(f"{s.output_dir}（批次里没有）")
            continue
        src = pick_source(store_dir, s.output_dir, s.items[0].pic_index if s.items else "01")
        if src is None:
            skipped.append(f"{s.output_dir}（找不到生成图）")
            continue

        sd = DST / s.output_dir
        sd.mkdir(parents=True, exist_ok=True)
        cfg = load_config()
        cfg.print.dpi = 120
        cfg.print.dieline = False
        cfg.print.single_file = True
        cfg.print.keep_merged_preview = False
        cfg.print.keep_preview_jpg = False

        res = PrintExporter(cfg).export_store(sd, src, store_name=f"{s.output_dir}_试印")
        if not res.ok:
            skipped.append(f"{s.output_dir}（{res.error_code}）")
            continue
        tif = sd / "印刷TIF" / res.files[0]
        ok.append((s.output_dir, tif, src.name, describe(tif), res))
        print(f"  ✓ [{idx}] {s.output_dir[:18]:20} <- {src.name[:28]}")

    print()
    print("=" * 96)
    print(f"  {'门店':22} {'通道':>4} {'ExtraSamples':>13} {'专色名':>12} "
          f"{'W1占比':>7} {'背景印白墨':>10} {'判定':>6}")
    print("  " + "-" * 92)

    import numpy as np
    import tifffile

    bad: list[str] = []
    for name, tif, srcname, d, res in ok:
        arr = tifffile.imread(tif)
        # ⚠️ 白墨在**第 5** 通道（紧跟 CMYK），不是最后一个。
        #    顺序是 CMYK(4) + W1(5) + 透明度(6) —— W1 必须排在前，
        #    蒙泰取「第一个 alpha 通道」当专色。读最后一个会拿到透明度。
        w = arr[..., 4]
        h, wd = w.shape
        border = np.concatenate([w[0, :], w[-1, :], w[:, 0], w[:, -1]])
        bg = (border == 255).mean() * 100
        cy, cx = h // 2, wd // 2
        r = min(h, wd) // 6
        ctr = (w[cy - r:cy + r, cx - r:cx + r] == 255).mean() * 100
        pct = (w == 255).mean() * 100
        if bg < 10 and ctr > 80:
            verdict = "✅"
        elif bg > 90 and ctr < 20:
            verdict = "❌反了"
            bad.append(name)
        else:
            verdict = "⚠️查"
            bad.append(name)
        print(f"  {name[:20]:22} {d['samples_per_pixel']:4} "
              f"{str(d['extrasamples']):>13} {str(d['channel_names'])[:12]:>12} "
              f"{pct:6.1f}% {bg:9.1f}% {verdict:>6}")

    print("=" * 96)
    print()
    print(f"  成功 {len(ok)} 个，跳过 {len(skipped)} 个")
    for s in skipped:
        print(f"    - {s}")
    if bad:
        print(f"  ⚠️ 需人工确认：{bad}")
    else:
        print("  ✅ 全部背景不印白墨、中心印白墨 —— 极性一致")
    print()
    print(f"  输出目录：{DST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
