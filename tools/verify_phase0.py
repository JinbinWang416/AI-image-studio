# -*- coding: utf-8 -*-
"""Phase 0 完成后的最终验收：新旧访问等价 + 行数对比。"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config, load_config  # noqa: E402

print("=" * 76)
print("Phase 0 重构 · 最终验收")
print("=" * 76)

cfg = load_config()

print("\n【① 旧字段访问（调用方在用的写法）】")
for name in ("provider", "provider_label", "model", "concurrency", "size",
             "output_root", "print_dpi", "print_cutout", "qc_enabled",
             "realism_iteration", "budget_limit", "image_mode"):
    val = getattr(cfg, name)
    print(f"    cfg.{name:<24} = {val!r}")

print("\n【② 新子配置访问（重构后的写法）】")
pairs = [
    ("cfg.providers.name", cfg.providers.name),
    ("cfg.generation.concurrency", cfg.generation.concurrency),
    ("cfg.generation.size", cfg.generation.size),
    ("cfg.output.root", cfg.output.root),
    ("cfg.qc.enabled", cfg.qc.enabled),
    ("cfg.print.dpi", cfg.print.dpi),
    ("cfg.print.cutout", cfg.print.cutout),
    ("cfg.prompt.realism_iteration", cfg.prompt.realism_iteration),
    ("cfg.guard.budget_limit", cfg.guard.budget_limit),
    ("cfg.image_workflow.mode", cfg.image_workflow.mode),
]
for expr, val in pairs:
    print(f"    {expr:<32} = {val!r}")

print("\n【③ 新旧等价性抽查】")
checks = [
    ("provider", cfg.provider, cfg.providers.name),
    ("concurrency", cfg.concurrency, cfg.generation.concurrency),
    ("size", cfg.size, cfg.generation.size),
    ("output_root", cfg.output_root, cfg.output.root),
    ("print_dpi", cfg.print_dpi, cfg.print.dpi),
    ("print_cutout", cfg.print_cutout, cfg.print.cutout),
    ("qc_enabled", cfg.qc_enabled, cfg.qc.enabled),
    ("budget_limit", cfg.budget_limit, cfg.guard.budget_limit),
    ("realism_iteration", cfg.realism_iteration, cfg.prompt.realism_iteration),
    ("image_mode", cfg.image_mode, cfg.image_workflow.mode),
]
all_ok = True
for flat, old, new in checks:
    ok = old == new
    all_ok = all_ok and ok
    print(f"    {'✅' if ok else '❌'} cfg.{flat:<20} {old!r} == {new!r}")
print(f"\n    {'✅ 新旧完全等价' if all_ok else '❌ 存在不一致'}")

print("\n【④ provider 仍是字符串（最关键的兼容点）】")
print(f"    type(cfg.provider) = {type(cfg.provider).__name__}")
print(f"    cfg.provider == 'qwen' 这类比较仍可用: "
      f"{'✅' if isinstance(cfg.provider, str) else '❌'}")

print("\n【⑤ 行数对比】")
new_impl = ROOT / "app" / "core" / "config.py"
facade = ROOT / "app" / "config.py"
backup = ROOT / "_backup_phase0" / "config.py.orig"
for label, p in (("重构前 app/config.py", backup),
                 ("重构后 app/core/config.py（实现）", new_impl),
                 ("重构后 app/config.py（门面）", facade)):
    if p.is_file():
        n = len(p.read_text(encoding="utf-8").splitlines())
        print(f"    {label:<36} {n:>4} 行")

print("\n【⑥ 子配置字段分布】")
import dataclasses  # noqa: E402

total = 0
for name in ("providers", "generation", "output", "qc", "prompt", "optimizer",
             "guard", "postprocess", "effect", "print", "scope", "image_workflow"):
    sub = getattr(cfg, name)
    n = len(dataclasses.fields(sub))
    total += n
    print(f"    {name:<18} {n:>2} 字段   ({type(sub).__name__})")
print(f"    {'合计':<18} {total:>2} 字段")

print("\n【⑦ 语义方法保留】")
print(f"    validate()  → {cfg.validate() or '（无问题）'}")
print(f"    is_mock     → {cfg.is_mock}")
print(f"    is_local    → {cfg.is_local}")
print(f"    describe()  → {len(cfg.describe().splitlines())} 行摘要")
print("=" * 76)
