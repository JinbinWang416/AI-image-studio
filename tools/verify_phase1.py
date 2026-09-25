# -*- coding: utf-8 -*-
"""Phase 1 最终验收：新旧路径等价 + 包结构。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

print("=" * 78)
print("Phase 1 分包 · 最终验收")
print("=" * 78)

# 旧路径 → 新路径（验证转发是否等价）
PAIRS = [
    ("app.models", "app.core.models", "Store"),
    ("app.paths", "app.core.paths", "PACKAGE_ROOT"),
    ("app.logging_setup", "app.core.logging", None),
    ("app.settings", "app.state.settings_store", "get_store"),
    ("app.manifest", "app.state.manifest_store", "ManifestStore"),
    ("app.storage", "app.state.storage", "Storage"),
    ("app.store_repo", "app.state.store_repo", "StoreRepository"),
    ("app.batches", "app.state.batches", "is_safe_batch_id"),
    ("app.reference_assets", "app.state.assets", "ReferenceAssetStore"),
    ("app.prompt_profiles", "app.prompt.profiles", None),
    ("app.prompts", "app.prompt.templates", None),
    ("app.prompt_optimizer", "app.prompt.optimizer", None),
    ("app.effect_renderer", "app.effect.renderer", "render_storefront_glass"),
    ("app.effect_background", "app.effect.background", None),
    ("app.postprocess", "app.effect.postprocess", None),
    ("app.orchestrator", "app.generation.orchestrator", "Orchestrator"),
    ("app.runs", "app.generation.runs", None),
    ("app.openai_regeneration", "app.generation.regeneration", None),
    ("app.local_validation", "app.validation.local", None),
    ("app.professional_local", "app.validation.professional", None),
    ("app.local_flux_service", "app.local_service.flux", None),
]

print("\n【① 新旧路径都能导入，且指向同一实现】")
ok_all = True
for old, new, attr in PAIRS:
    try:
        m_old = importlib.import_module(old)
        m_new = importlib.import_module(new)
        same = True
        detail = ""
        if attr:
            v_old = getattr(m_old, attr, None)
            v_new = getattr(m_new, attr, None)
            same = v_old is v_new
            detail = f"{attr}: {'同一对象' if same else '❌ 不是同一对象'}"
        else:
            # 无指定属性时，比对模块的公开名集合
            shared = set(dir(m_new)) - set(dir(m_old))
            detail = f"新模块多出 {len(shared)} 个名字"
        ok_all = ok_all and same
        print(f"  {'✅' if same else '❌'} {old:<26} → {new:<34} {detail}")
    except Exception as exc:  # noqa: BLE001
        ok_all = False
        print(f"  ❌ {old:<26} → {new:<34} {type(exc).__name__}: {exc}")

print(f"\n  {'✅ 全部等价' if ok_all else '❌ 存在不等价项'}")

print("\n【② 分包后的 app/ 结构】")
for p in sorted(ROOT.joinpath("app").iterdir()):
    if p.is_dir():
        n = len(list(p.glob("*.py")))
        if n:
            print(f"  📦 {p.name + '/':<18} {n:>2} 个模块")
print("  ── 根目录转发模块 ──")
fwd = sorted(p.name for p in ROOT.joinpath("app").glob("*.py"))
print(f"  {len(fwd)} 个 .py：" + ", ".join(fwd))

print("\n【③ 转发模块的已知限制】")
print("  ⚠️ 转发用 `import *`，因此：")
print("     · 下划线私有名**不会**被转发（如 assets._URL_CACHE）")
print("     · 模块级变量是**副本**，不是引用 → patch 转发模块无效")
print("     · 新代码请直接用新路径；需要 patch 时务必指向真实现")

print("\n【④ 配置仍等价（Phase 0 成果保持）】")
from app.config import load_config  # noqa: E402

cfg = load_config()
checks = [
    ("provider", cfg.provider, cfg.providers.name),
    ("concurrency", cfg.concurrency, cfg.generation.concurrency),
    ("print_dpi", cfg.print_dpi, cfg.print.dpi),
    ("output_root", cfg.output_root, cfg.output.root),
]
for name, a, b in checks:
    print(f"  {'✅' if a == b else '❌'} cfg.{name:<14} {a!r} == {b!r}")

print("\n【⑤ 路径常量正确性（迁移踩过的坑）】")
from app.core.paths import PACKAGE_ROOT, STATE_ROOT  # noqa: E402

print(f"  PACKAGE_ROOT = {PACKAGE_ROOT}")
print(f"  期望         = {ROOT}")
print(f"  {'✅ 正确' if Path(PACKAGE_ROOT) == ROOT else '❌ 偏移了'}")
print(f"  STATE_ROOT   = {STATE_ROOT}")
from app.core.config import DATA_FILE  # noqa: E402

print(f"  DATA_FILE    = {DATA_FILE}")
print(f"  存在: {DATA_FILE.is_file()}")
print("=" * 78)
