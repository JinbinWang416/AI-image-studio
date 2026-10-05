# -*- coding: utf-8 -*-
"""把已生成批次里的**效果图**按当前参数重新合成并覆盖，同时同步批次清单。

用途：改了效果图参数（背景 / 不模糊 / 透光率 / 玻璃区标定 …）之后，
不必重新调服务商生成贴纸，直接把老批次的效果图刷新成新参数产物。

走的是与网页、批量完全相同的链路：
    load_config() → resolve_render_options(cfg, 门店主标题) → render_storefront_glass()
因此背景自动匹配、玻璃区标定、17 个微调参数都会生效。

用法：
    .\\.venv\\Scripts\\python.exe tools\\refresh_batch_effects.py            # 最新批次全部门店
    .\\.venv\\Scripts\\python.exe tools\\refresh_batch_effects.py --store 01  # 只刷 01
    .\\.venv\\Scripts\\python.exe tools\\refresh_batch_effects.py --batch <批次目录名>

⚠️ 会**覆盖**批次内 `*效果图/` 下的旧效果图，并更新 `_manifest.json` 的
   `runtime_metrics.effect_image`。生成图（贴纸原图）绝不动。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import load_config  # noqa: E402
from app.effect.background import resolve_render_options  # noqa: E402
from app.effect.renderer import (  # noqa: E402
    EFFECT_RENDERER_VERSION,
    EffectRenderError,
    render_storefront_glass,
)

GEN_SUFFIX = "生成图"
EFFECT_SUFFIX = "效果图"


def latest_batch() -> pathlib.Path:
    batches = sorted(
        (p for p in (ROOT / "output").glob("batch_*") if p.is_dir()), reverse=True
    )
    for b in batches:
        if any(b.rglob(f"*{GEN_SUFFIX}")):
            return b
    raise SystemExit("未找到任何批次")


def store_main_title(store_dir_name: str) -> str:
    """把「01_房屋中介门店」映射回数据包里的门店主标题（「房屋中介」）。

    ⚠️ 一定要用 ``main_title``：背景图的 ``INTERIORS`` 是按主标题索引的，
    传文件夹名（带「门店」后缀）会查不到行业描述、悄悄回退成通用店内。
    """
    folder_name = (
        store_dir_name.split("_", 1)[-1] if "_" in store_dir_name else store_dir_name
    )
    try:
        from app.state.store_repo import StoreRepository

        for s in StoreRepository().load():
            if s.folder_name == folder_name:
                return s.main_title
    except Exception:  # noqa: BLE001
        pass
    return folder_name


def process_store(cfg, store_dir: pathlib.Path, dry: bool) -> tuple[int, int]:
    gen_dir = store_dir / f"{store_dir.name}{GEN_SUFFIX}"
    eff_dir = store_dir / f"{store_dir.name}{EFFECT_SUFFIX}"
    manifest_path = store_dir / "_manifest.json"
    # ⚠️ 兼容**两种目录结构**：
    #      · 新结构：`<门店>/<门店>生成图/`
    #      · 旧结构：生成图直接放在 `<门店>/` 根目录（09-19 那批全是这样）
    #    只认新结构的话，22 个老门店会被整体跳过 —— 表现为「只刷新了 1 个门店」。
    if not gen_dir.is_dir():
        gen_dir = store_dir
    if not manifest_path.is_file():
        print(f"  跳过 {store_dir.name}（缺清单）")
        return 0, 0

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("entries") or {}
    store_name = store_main_title(store_dir.name)
    opts = resolve_render_options(cfg, store_name)
    bg = opts["background"]
    bg_asset = opts["background_asset"] or {}
    print(f"  门店主标题 = {store_name}")
    print(f"  背景 = {bg}")
    print(f"  背景类型 = {bg_asset.get('kind') or 'simulated'}   玻璃区 = {opts['glass_region']}")
    print(f"  参数 = 背景模糊 {opts['params'].background_blur_ratio}"
          f" / 贴纸透光 {opts['params'].transmission}")

    eff_dir.mkdir(parents=True, exist_ok=True)
    done = failed = 0
    for key, entry in entries.items():
        # ⚠️ 不能只认顶层的 `status`。
        #
        #    架构版重构后 manifest 的 entry 里**没有** `status` 字段
        #    （成功/失败记在 `runtime_metrics.effect_image.status` 里），
        #    于是 `entry.get("status") != "success"` 把**所有条目**都跳过了 ——
        #    现象就是「清单已更新」但「刷新 0 张」，一张也没刷。
        #
        #    改成：顶层有 status 才拿它判断；没有就看生成图在不在。
        status = entry.get("status")
        if status is not None and status != "success":
            continue
        src = pathlib.Path(entry.get("image_path") or "")
        if not src.is_file():
            src = gen_dir / str(entry.get("file_name") or "")
        if not src.is_file():
            # ⚠️ 架构版的 manifest **极简**：`entry` 里只有 `runtime_metrics`，
            #    既没有 `image_path` 也没有 `file_name`（重构时精简掉了）。
            #    所以退回**按 pic_index 在生成图目录里找** ——
            #    命名格式是 `{时间戳}_{pic_index}_{主题}.png`。
            #
            #    排除 `_效果图`（那是产物）与 `_up`（那是超分放大版），
            #    优先取没有这两个后缀的原图。
            cands = sorted(
                p for p in gen_dir.glob(f"*_{key}_*.png")
                if "_效果图" not in p.name and "_up" not in p.stem
            )
            if cands:
                src = cands[0]
        if not src.is_file():
            print(f"    ✗ {key} 找不到生成图")
            failed += 1
            continue
        if dry:
            print(f"    · {src.stem} → {src.stem}_效果图.png（演练，未写盘）")
            done += 1
            continue
        try:
            result = render_storefront_glass(src, store_name, **opts)
        except EffectRenderError as exc:
            print(f"    ✗ {src.stem} 合成失败：{exc}")
            entry.setdefault("runtime_metrics", {})["effect_image"] = {
                "status": "failed",
                "renderer": EFFECT_RENDERER_VERSION,
                "error": str(exc),
            }
            failed += 1
            continue

        target = eff_dir / f"{src.stem}_效果图.png"
        target.write_bytes(result.data)
        entry.setdefault("runtime_metrics", {})["effect_image"] = result.metadata(target)
        entry["runtime_metrics"]["effect_image"]["refreshed_at"] = datetime.now().isoformat(
            timespec="seconds"
        )
        print(f"    ✓ {src.stem} → {target.name}")
        done += 1

    if not dry:
        manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
        tmp = manifest_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tmp.replace(manifest_path)
        print(f"  清单已更新：{manifest_path.name}")
    return done, failed


def main() -> int:
    ap = argparse.ArgumentParser(description="按当前参数刷新批次里的效果图")
    ap.add_argument("--batch", "-b", default="", help="批次目录名；默认取最新批次")
    ap.add_argument("--store", "-s", default="", help="门店序号前缀（如 01）；默认全部")
    ap.add_argument("--dry-run", action="store_true", help="只演练，不写盘")
    args = ap.parse_args()

    batch = (ROOT / "output" / args.batch) if args.batch else latest_batch()
    if not batch.is_dir():
        print(f"❌ 批次不存在：{batch}")
        return 1
    print(f"批次：{batch.name}")

    cfg = load_config()
    problems = cfg.validate()
    if problems:
        print("❌ 配置不可用：")
        for p in problems:
            print(f"   - {p}")
        return 1

    stores = [
        d for d in sorted(batch.iterdir())
        # ⚠️ 同样要兼容旧结构：有些门店没有「生成图」子目录，
        #    生成图就在门店根目录 —— 只按子目录过滤会漏掉它们。
        if d.is_dir()
        and ((d / f"{d.name}{GEN_SUFFIX}").is_dir() or any(d.glob("*.png")))
        and (not args.store or d.name.startswith(args.store))
    ]
    if not stores:
        print("❌ 没找到匹配的门店目录")
        return 1

    total_done = total_failed = 0
    for d in stores:
        print(f"\n[{d.name}]")
        done, failed = process_store(cfg, d, args.dry_run)
        total_done += done
        total_failed += failed

    print("\n" + "=" * 66)
    print(f"完成：刷新 {total_done} 张，失败 {total_failed} 张")
    if not args.dry_run and total_done:
        print("已覆盖批次内旧效果图并同步 _manifest.json（生成图未被改动）")
    print("=" * 66)
    return 0 if total_done else 1


if __name__ == "__main__":
    sys.exit(main())
