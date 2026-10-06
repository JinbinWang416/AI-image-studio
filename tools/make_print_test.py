# -*- coding: utf-8 -*-
"""生成一个「蒙泰应能直接打印」的测试 TIF，供现场试印。

不复用批次里的旧产物，而是拿现成的生成图按新参数重新导出：
单文件 5 通道 + 专色通道名 White + 120dpi（对齐现场样例的 60cm/2835px）。

通道名 White 来自现场 PS 动作 `一键专色(1).ATN` 的解析结果。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.core.models import Store  # noqa: E402
from app.state.store_repo import StoreRepository  # noqa: E402

SRC_BATCH = ROOT / "output" / "batch_20260919_014522_qwen_qwen-image-3.0"
DST = ROOT / "output" / "_print_test"


def main() -> int:
    cfg = load_config()
    stores = {s.folder_index: s for s in StoreRepository().load()}
    DST.mkdir(parents=True, exist_ok=True)

    # 找 2 张有代表性的：一张图案复杂、一张简单
    picks: list[tuple[Store, Path]] = []
    for idx in ("04", "01"):          # 04 干洗护理 / 01 房屋中介
        s = stores.get(idx)
        if s is None:
            continue
        d = SRC_BATCH / s.output_dir
        cands = [p for p in (list(d.glob("*.png"))
                             + list((d / f"{s.output_dir}生成图").glob("*.png")))
                 if "_效果图" not in p.name and "_up" not in p.stem]
        if cands:
            picks.append((s, sorted(cands)[0]))

    if not picks:
        print("  没找到可用的生成图")
        return 2

    # 只跑印刷导出，不重新生成贴纸
    from app.print_export.print_export import PrintExporter

    out_paths = []
    for s, src in picks:
        sd = DST / s.output_dir
        sd.mkdir(parents=True, exist_ok=True)
        cfg2 = load_config()
        exp = PrintExporter(cfg2)
        # 用现场样例的尺寸口径：60cm 宽、120dpi
        cfg2.print.dpi = 120
        cfg2.print.dieline = False
        cfg2.print.single_file = True
        cfg2.print.keep_merged_preview = False
        cfg2.print.keep_preview_jpg = False

        res = exp.export_store(sd, src, store_name=f"{s.output_dir}_试印")
        if res.ok:
            tif = sd / "印刷TIF" / res.files[0]
            out_paths.append((s.output_dir, src.name, tif, res))
            print(f"  ✓ {s.output_dir}  <- {src.name}")
            print(f"      TIF: {tif.name}  ({tif.stat().st_size / 1024 / 1024:.2f} MB)")
        else:
            print(f"  ✗ {s.output_dir} 导出失败：{res.error_code} {res.error_message}")

    # 汇总
    print()
    print("=" * 74)
    from app.print_export.stacked import describe

    for name, srcname, tif, res in out_paths:
        d = describe(tif)
        px = res.manifest.output_pixels
        cm = res.manifest.width_cm
        print(f"  {name}")
        print(f"    {tif}")
        print(f"    尺寸 {px[0]}x{px[1]} @ {res.manifest.dpi}dpi  "
              f"= {px[0] / res.manifest.dpi * 2.54:.1f}cm 宽")
        print(f"    通道 {d['samples_per_pixel']}  ExtraSamples {d['extrasamples']}  "
              f"专色名 {d['channel_names']}")
    print("=" * 74)
    print()
    print("  拿去蒙泰试印。重点确认：")
    print("    1) 能不能直接打开、不报错")
    print("    2) 白墨设定里那一路是否自动显示为「专色」（通道名应为 White）")
    print("    3) 打出来白墨位置对不对（贴在玻璃上颜色是不是实的）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
