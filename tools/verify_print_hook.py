# -*- coding: utf-8 -*-
"""
验证 orchestrator 的印刷导出挂接。

用 **mock 服务商** + **临时输出目录**跑一个最小批次，
确认批次完成后 `_schedule_print_export()` 真的触发、产出 TIF、回写 manifest。

⚠️ 不调用任何付费 API；不触碰真实 output/。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_config  # noqa: E402
from app.state.manifest_store import ManifestStore  # noqa: E402
from app.generation.orchestrator import Orchestrator  # noqa: E402
from app.providers import create_provider  # noqa: E402
from app.state.storage import Storage  # noqa: E402
from app.state.store_repo import StoreRepository  # noqa: E402

PASS = "✅"
FAIL = "❌"
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    results.append((ok, label))
    print(f"  {PASS if ok else FAIL} {label}" + (f"   {extra}" if extra else ""))


async def run() -> int:
    print("=" * 78)
    print("orchestrator 印刷导出挂接验证（mock 服务商）")
    print("=" * 78)

    tmp = tempfile.TemporaryDirectory()
    base = Path(tmp.name)
    batch_id = "batch_test_mock"
    out_root = base / batch_id
    out_root.mkdir(parents=True, exist_ok=True)

    cfg = load_config()
    cfg.provider = "mock"
    cfg.api_key = "mock"
    cfg.output_base_root = base
    cfg.output_root = out_root
    cfg.batch_id = batch_id
    cfg.run_limit = 1                     # 只跑 1 张
    cfg.concurrency = 1
    cfg.timeout = 30
    cfg.print_export_enabled = True
    cfg.print_width_cm = 10.0             # 小尺寸，跑得快
    cfg.print_dpi = 100
    cfg.print_max_pixels = 1500
    cfg.print_keep_work = True
    cfg.whiten_background = False

    repo = StoreRepository()
    stores = repo.stores
    if not stores:
        print("  ❌ 没有门店数据")
        return 1
    cfg.selected_store_indexes = [stores[0].folder_index]

    print(f"  provider = {cfg.provider}")
    print(f"  输出目录 = {out_root}")
    print(f"  门店     = {stores[0].folder_index} {stores[0].folder_name}")
    print(f"  试跑     = {cfg.run_limit} 张\n")

    provider = create_provider(cfg)
    storage = Storage(cfg.output_root, cfg.timestamp_prefix)
    manifests = ManifestStore(cfg.output_root)

    orch = Orchestrator(cfg, provider, repo, storage, manifests)
    # ⚠️ run() 接受的是**门店序号列表**（不是 Store 对象）
    stats = await orch.run([stores[0].folder_index])

    print(f"  生成结束: 总数={stats.total} 成功={stats.done} 失败={stats.failed}")
    check(stats.done >= 1, "至少生成 1 张", f"done={stats.done}")

    # `run()` 内部已排入后台线程。这里主动等待它完成 ——
    # 若不等，`asyncio.run()` 结束时会 shutdown 默认线程池，任务直接被取消。
    future = getattr(orch, "_print_future", None)
    print("\n  等待后台印刷导出…")
    if future is not None:
        await asyncio.wrap_future(future)
    else:
        # 排入失败：把原因查清楚（而不是只报"没生成"）
        from app.print_export import collect_export_targets

        print(f"  ⚠️ _print_future 为 None，排查原因：")
        print(f"     print_export_enabled = {cfg.print_export_enabled}")
        all_jobs = list(orch._current.values())
        print(f"     当前 job 数 = {len(all_jobs)}")
        for j in all_jobs[:3]:
            p = getattr(j, "image_path", "")
            print(f"       {j.file_name}: image_path={p!r} exists={Path(p).is_file() if p else False}")
        targets = collect_export_targets(all_jobs, cfg.output_root)
        print(f"     collect_export_targets → {len(targets)} 个目标")
        await asyncio.sleep(1)

    store_dir = None
    print_dir = None
    for c in [p for p in out_root.iterdir() if p.is_dir()]:
        if (c / "印刷TIF").is_dir():
            store_dir = c
            print_dir = c / "印刷TIF"
            break

    if not store_dir:
        check(False, "印刷TIF/ 目录已生成", "等 30 秒仍未出现")
        tmp.cleanup()
        _summary()
        return 1

    check(True, "印刷TIF/ 目录已生成", str(store_dir.name))

    # 产物检查
    tifs = sorted(p.name for p in print_dir.glob("*.tif"))
    check(len(tifs) >= 3, "至少 3 个 TIF", f"{len(tifs)} 个：{tifs}")

    mf_path = print_dir / "print_manifest.json"
    check(mf_path.is_file(), "print_manifest.json 已生成")
    if mf_path.is_file():
        mf = json.loads(mf_path.read_text(encoding="utf-8"))
        check(mf.get("status") == "success", "manifest status=success", str(mf.get("status")))
        check(bool(mf.get("layers")), "manifest 含 layers",
              f"{list((mf.get('layers') or {}).keys())}")

    # 批次 manifest 回写
    bmf = store_dir / "_manifest.json"
    check(bmf.is_file(), "批次 _manifest.json 存在")
    if bmf.is_file():
        data = json.loads(bmf.read_text(encoding="utf-8"))
        pe = data.get("print_export")
        check(isinstance(pe, dict), "批次 manifest 回写 print_export")
        if isinstance(pe, dict):
            check(pe.get("status") in ("success", "partial"),
                  "print_export.status 正常", str(pe.get("status")))
        for k in ("version", "store", "entries"):
            check(k in data, f"既有字段 {k} 保留")

    # 生成图未被改动
    gen_dirs = [p for p in store_dir.iterdir() if p.is_dir() and "生成图" in p.name]
    if gen_dirs:
        pngs = list(gen_dirs[0].glob("*.png"))
        check(bool(pngs), "生成图 PNG 仍在原处", f"{len(pngs)} 张")

    tmp.cleanup()
    return _summary()


def _summary() -> int:
    print("-" * 78)
    ok = sum(1 for r, _ in results if r)
    print(f"通过 {ok} / {len(results)}"
          + ("  ✅ 全部通过" if ok == len(results) else "  ⚠️ 有失败"))
    print("=" * 78)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
