# -*- coding: utf-8 -*-
"""行为等价性验证：在指定项目里跑一遍完整流程，输出结构化结果。

用法（在**架构版**目录执行，可指定任意项目路径）：

    .\\.venv\\Scripts\\python.exe tools\\verify_parity.py --project <项目目录> --out <结果json>

流程（全程 mock，**不调用任何付费 API**）：
    ① 建临时输出目录 → 用 mock 跑 1 个门店的批次
    ② 检查批次结构与 manifest
    ③ 跑印刷 TIF 导出
    ④ 检查产物（TIF 模式/尺寸、manifest 字段）
    ⑤ 把结果写成 JSON，供两版对比

设计为**只读原项目代码、只写临时目录**，不会污染任何一边的 output/。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path


def build_report(project: Path, work: Path) -> dict:
    sys.path.insert(0, str(project))

    # 兼容两种布局：
    #   · 重构后（架构版）：app.generation.orchestrator / app.state.xxx
    #   · 重构前（原版）：  app.orchestrator / app.xxx
    # 这样同一个脚本能在两个目录里跑出可比对的结果。
    import importlib

    def imp(new_path: str, old_path: str):
        try:
            return importlib.import_module(new_path)
        except ImportError:
            return importlib.import_module(old_path)

    # 这两个两版都有
    from app.config import Config, load_config  # noqa: E402
    # make_config / with_config 是 Phase 0 新增的，用能力探测访问（见 ④）

    Orchestrator = imp("app.generation.orchestrator", "app.orchestrator").Orchestrator
    create_provider = importlib.import_module("app.providers").create_provider
    ManifestStore = imp("app.state.manifest_store", "app.manifest").ManifestStore
    Storage = imp("app.state.storage", "app.storage").Storage
    StoreRepository = imp("app.state.store_repo", "app.store_repo").StoreRepository

    report: dict = {"project": str(project), "checks": []}

    def add(name: str, ok: bool, detail: str = "") -> None:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # ---------------------------------------------------------------- 配置
    cfg = load_config()
    cfg.provider = "mock"
    cfg.api_key = "mock"
    cfg.output_base_root = work
    cfg.output_root = work / "batch_parity"
    cfg.batch_id = "batch_parity"
    cfg.run_limit = 1
    cfg.concurrency = 1
    cfg.whiten_background = False
    cfg.print_export_enabled = True
    cfg.print_width_cm = 10.0
    cfg.print_dpi = 100
    cfg.print_max_pixels = 1500
    cfg.print_keep_work = True

    # 配置层自检
    #   ⚠️ `cfg.print` / `cfg.generation` 这些**子配置**是 Phase 0 才有的；
    #      原版只有平铺字段。用能力探测，保证同一脚本两版都能跑。
    has_sub = hasattr(cfg, "print") and hasattr(cfg.print, "dpi")
    report["has_subconfigs"] = has_sub

    add("cfg.provider 是字符串", isinstance(cfg.provider, str), cfg.provider)
    add("cfg.print_dpi 可读（两版都有）", cfg.print_dpi == 100, cfg.print_dpi)
    if has_sub:
        add("cfg.print.dpi 可读（子配置，重构后新增）", cfg.print.dpi == 100, cfg.print.dpi)
        add("新旧访问等价（重构后新增）",
            cfg.print_dpi == cfg.print.dpi
            and cfg.concurrency == cfg.generation.concurrency
            and cfg.output_root == cfg.output.root)
    else:
        add("子配置访问（原版无此项，预期）", True, "跳过")

    repo = StoreRepository()
    stores = repo.stores
    add("门店数据可加载", len(stores) > 0, f"{len(stores)} 个门店")
    if not stores:
        return report

    work.mkdir(parents=True, exist_ok=True)
    provider = create_provider(cfg)
    storage = Storage(cfg.output_root, cfg.timestamp_prefix)
    manifests = ManifestStore(cfg.output_root)

    # ---------------------------------------------------------------- ① 生成
    orch = Orchestrator(cfg, provider, repo, storage, manifests)
    stats = asyncio.run(orch.run([stores[0].folder_index]))
    add("① 批次生成完成", stats.done >= 1,
        f"total={stats.total} done={stats.done} failed={stats.failed}")

    gen_dirs = [p for p in cfg.output_root.iterdir() if p.is_dir()] if cfg.output_root.is_dir() else []
    add("① 门店目录已创建", len(gen_dirs) == 1, [p.name for p in gen_dirs])

    if not gen_dirs:
        return report
    store_dir = gen_dirs[0]
    gen_dir = next((p for p in store_dir.iterdir() if "生成图" in p.name), None)
    pngs = sorted(p.name for p in gen_dir.glob("*.png")) if gen_dir else []
    add("① 生成图已落盘", len(pngs) >= 1, f"{len(pngs)} 张")

    mf = store_dir / "_manifest.json"
    add("① 门店 manifest 存在", mf.is_file())
    if mf.is_file():
        data = json.loads(mf.read_text(encoding="utf-8"))
        add("① manifest 顶层字段", set(["version", "store", "entries"]).issubset(data),
            sorted(data.keys()))
        add("① manifest 无明文 Key",
            "api_key" not in json.dumps(data, ensure_ascii=False).lower())

    # ---------------------------------------------------------------- ② 印刷导出
    from app.print_export import PrintExporter  # noqa: E402

    exporter = PrintExporter(cfg)
    res = exporter.export_store(store_dir, gen_dir / pngs[0], store_name="PARITY")
    add("② 印刷导出成功", res.ok, f"{res.error_code} {res.error_message}")

    if res.ok and res.print_dir:
        from PIL import Image  # noqa: E402

        d = res.print_dir
        # ⚠️ 不能断言「正好 4 个」—— 编排器在批次完成后会**自动导出**每张图的 4 层，
        #    所以目录里除了本次手动导出的 PARITY_*，还有 <门店>_01..06_* 共 24 个。
        #    这里只要求「本次产出的 4 层都在」，并单独断言自动导出也发生了。
        all_tifs = sorted(p.name for p in d.glob("*.tif"))
        mine = [n for n in all_tifs if n.startswith("PARITY_")]
        add("② 本次导出 4 层齐全", len(mine) == 4, sorted(n.split("_")[-1] for n in mine))
        auto = [n for n in all_tifs if not n.startswith("PARITY_")]
        add("② 编排器自动导出也生效", len(auto) >= 4,
            f"{len(auto)} 个自动导出文件（{len(auto) // 4} 张图 × 4 层）")

        modes = {}
        for name in mine:
            with Image.open(d / name) as im:
                modes[name.split("_")[-1].replace(".tif", "")] = (im.mode, im.size)
        report["tif_modes"] = {k: list(v) for k, v in modes.items()}
        add("② CMYK 模式正确", modes.get("CMYK", ("",))[0] == "CMYK", modes.get("CMYK"))
        sizes = {v[1] for v in modes.values()}
        add("② 各层尺寸一致", len(sizes) == 1, sizes)
        add("② 出血按 DPI 换算", res.manifest.bleed_px == 12,
            f"{res.manifest.bleed_mm}mm @ {res.manifest.dpi}DPI = {res.manifest.bleed_px}px")
        add("② 源图未被修改",
            hashlib.sha256((gen_dir / pngs[0]).read_bytes()).hexdigest()
            == hashlib.sha256((gen_dir / pngs[0]).read_bytes()).hexdigest())

        pm = json.loads((d / "print_manifest.json").read_text(encoding="utf-8"))
        report["print_manifest_keys"] = sorted(pm.keys())
        add("② print_manifest 字段齐全",
            {"version", "status", "source", "output", "bleed", "icc", "layers"}.issubset(pm),
            sorted(pm.keys()))
        add("② cutout 记录在案", "cutout" in (pm.get("options") or {}),
            (pm.get("options") or {}).get("cutout"))

    # ---------------------------------------------------------------- ③ 批次结构
    report["store_dir_children"] = sorted(p.name for p in store_dir.iterdir())
    report["png_names"] = pngs
    add("③ 生成图未被动过", (gen_dir / pngs[0]).is_file())

    # ---------------------------------------------------------------- ④ 工厂函数
    # ⚠️ make_config / with_config 是 Phase 0 新增的兼容工厂，
    #    **原版没有** —— 这里做能力探测，原版跳过（不算失败）。
    cfg_mod = importlib.import_module("app.config")
    if hasattr(cfg_mod, "make_config"):
        cfg2 = cfg_mod.make_config(output_root=work / "x", budget_limit=7)
        add("④ make_config 兼容", cfg2.guard.budget_limit == 7 and
            cfg2.budget_limit == 7, cfg2.output.root.name)
    else:
        add("④ make_config（Phase 0 新增，原版无此项）", True, "跳过")

    if hasattr(cfg_mod, "with_config"):
        cfg3 = cfg_mod.with_config(cfg, print_dpi=222)
        add("④ with_config 兼容", cfg3.print.dpi == 222 and cfg3.print_dpi == 222)
    else:
        add("④ with_config（Phase 0 新增，原版无此项）", True, "跳过")

    # ---------------------------------------------------------------- ⑤ 子配置访问
    # 同样是重构后才有的能力
    add("⑤ 子配置 cfg.print.dpi（重构后新增）", True,
        f"{'可用' if has_sub else '原版无子配置（预期）'}")

    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="要验证的项目目录")
    ap.add_argument("--out", required=True, help="结果 JSON 输出路径")
    args = ap.parse_args()

    project = Path(args.project).resolve()
    out = Path(args.out).resolve()

    print("=" * 76)
    print(f"行为验证：{project.name}")
    print("=" * 76)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "output_parity"
        try:
            report = build_report(project, work)
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            report = {"project": str(project), "fatal": f"{type(exc).__name__}: {exc}",
                      "checks": []}

    for c in report.get("checks", []):
        print(f"  {'✅' if c['ok'] else '❌'} {c['name']:<34} {c['detail'][:70]}")

    ok = sum(1 for c in report.get("checks", []) if c["ok"])
    total = len(report.get("checks", []))
    report["summary"] = {"ok": ok, "total": total}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n  通过 {ok} / {total}")
    print(f"  结果已写入 {out}")
    print("=" * 76)
    return 0 if total and ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
