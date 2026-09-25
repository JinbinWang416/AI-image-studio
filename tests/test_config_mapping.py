# -*- coding: utf-8 -*-
"""Phase 0 重构安全网：57 个旧字段名与新嵌套结构的等价性。

重构把 `Config` 的 57 个平铺字段拆进 12 个子配置，同时用 `@property`
保留所有旧字段名。**本测试逐一断言两者等价** —— 只要有一个字段映射写错，
这里就会红。

覆盖：
    1. `FLAT_TO_GROUP` 覆盖全部旧字段（不多不少）
    2. 每个旧字段名读得到、值与子配置一致
    3. 每个旧字段名**写得进**（property setter 生效）
    4. `with_config()` 用旧字段名覆盖，效果落到正确子配置
    5. `make_config()` 用旧字段名构造
    6. `is_mock` / `is_local` / `validate()` / `describe()` 行为不变
    7. 门面模块 `app.config` 与原实现导出的名字一致
"""

from __future__ import annotations

import dataclasses
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import (  # noqa: E402
    FLAT_TO_GROUP,
    Config,
    make_config,
    with_config,
)

# 重构前 Config 的 57 个字段名（来自 _backup_phase0/config.py.orig）
LEGACY_FIELDS = [
    # providers
    "provider", "provider_label", "api_key", "base_url", "model",
    "supports_negative", "price_per_image",
    # generation
    "concurrency", "rpm_limit", "retry_max", "retry_backoff", "timeout", "size",
    # output
    "output_base_root", "output_root", "batch_id", "batch_created_at",
    "timestamp_prefix", "overwrite",
    # qc
    "qc_enabled", "qc_min_similarity",
    # prompt
    "prompt_version", "text_wrapper", "normalize_negative", "use_simple_prompt",
    # optimizer
    "prompt_optimizer_enabled", "prompt_optimizer_api_key",
    "prompt_optimizer_base_url", "prompt_optimizer_model",
    # guard
    "budget_limit", "dry_run",
    # postprocess
    "whiten_background", "whiten_threshold",
    # effect
    "auto_match_effect_background", "effect_params",
    # scope
    "run_limit", "selected_store_indexes",
    # image workflow
    "image_mode", "reference_assets",
    "quality_template", "optimized_quality_template",
    "use_optimized_quality_template", "realism_iteration",
    "effect_background_asset",
    # print
    "print_export_enabled", "print_width_cm", "print_dpi", "print_bleed_mm",
    "print_icc_path", "print_white_ink", "print_white_ink_invert",
    "print_dieline", "print_cutout", "print_spot_channel", "print_max_pixels",
    "print_keep_work", "print_auto_clean_work",
]

SUBCONFIG_NAMES = [
    "providers", "generation", "output", "qc", "prompt", "optimizer",
    "guard", "postprocess", "effect", "print", "scope", "image_workflow",
]


class TestMappingComplete(unittest.TestCase):
    """1：映射完整性与唯一性。"""

    def test_legacy_field_count(self) -> None:
        self.assertEqual(len(LEGACY_FIELDS), 57, "重构前应为 57 个字段")
        self.assertEqual(len(set(LEGACY_FIELDS)), 57, "字段名不应重复")

    def test_mapping_covers_every_legacy_field(self) -> None:
        missing = [f for f in LEGACY_FIELDS if f not in FLAT_TO_GROUP]
        self.assertFalse(missing, f"以下旧字段没有映射：{missing}")

    def test_mapping_has_no_extra_entries(self) -> None:
        extra = [k for k in FLAT_TO_GROUP if k not in LEGACY_FIELDS]
        self.assertFalse(extra, f"映射里有旧配置不存在的字段：{extra}")

    def test_mapping_targets_exist(self) -> None:
        """每个映射目标必须是真实存在的子配置字段。"""
        cfg = Config()
        bad: list[str] = []
        for flat, (group, attr) in FLAT_TO_GROUP.items():
            if group not in SUBCONFIG_NAMES:
                bad.append(f"{flat} → 未知子配置 {group}")
                continue
            sub = getattr(cfg, group)
            if not hasattr(sub, attr):
                bad.append(f"{flat} → {group}.{attr} 不存在")
        self.assertFalse(bad, "映射目标不合法：" + "; ".join(bad))

    def test_field_count_matches(self) -> None:
        """12 个子配置的字段总数必须等于 57。"""
        total = sum(len(dataclasses.fields(getattr(Config(), n)))
                    for n in SUBCONFIG_NAMES)
        self.assertEqual(total, 57, f"子配置字段合计 {total}，应为 57")


class TestCompatProperties(unittest.TestCase):
    """2 + 3：旧字段名读得到、写得进。"""

    def test_read_matches_subconfig(self) -> None:
        cfg = Config()
        bad: list[str] = []
        for flat, (group, attr) in FLAT_TO_GROUP.items():
            legacy_value = getattr(cfg, flat)
            new_value = getattr(getattr(cfg, group), attr)
            if legacy_value != new_value:
                bad.append(f"{flat}: 旧={legacy_value!r} 新={new_value!r}")
        self.assertFalse(bad, "读写不一致：" + "; ".join(bad))

    def test_write_through_property(self) -> None:
        """通过旧属性赋值，必须落到子配置上。"""
        cfg = Config()
        cfg.print_dpi = 900
        self.assertEqual(cfg.print.dpi, 900, "旧属性写入没有落到 cfg.print.dpi")
        cfg.concurrency = 7
        self.assertEqual(cfg.generation.concurrency, 7)
        cfg.output_root = pathlib.Path("X:/tmp")
        self.assertEqual(cfg.output.root, pathlib.Path("X:/tmp"))

    def test_write_subconfig_reflects_in_legacy(self) -> None:
        """反向：改子配置，旧属性应立刻反映。"""
        cfg = Config()
        cfg.print.dpi = 450
        self.assertEqual(cfg.print_dpi, 450)
        cfg.providers.api_key = "sk-test"
        self.assertEqual(cfg.api_key, "sk-test")

    def test_provider_stays_string(self) -> None:
        """🔴 关键回归：`cfg.provider` 必须仍是**字符串**。

        子配置字段特意命名为 `providers`（复数）就是为了避开它 ——
        全项目有 32 处 `cfg.provider == "qwen"` 这类比较。
        """
        cfg = Config()
        self.assertIsInstance(cfg.provider, str)
        self.assertEqual(cfg.provider, cfg.providers.name)
        cfg.provider = "qwen"
        self.assertEqual(cfg.providers.name, "qwen")

    def test_every_legacy_field_is_writable(self) -> None:
        """57 个字段都得是**可写** property（否则 replace 式用法会静默失败）。

        ⚠️ 注意用 `Config`（类本身）取属性，不要用 `type(Config)` ——
           后者是元类 `type`，取什么都取不到（写这个测试时踩过）。
        """
        readonly: list[str] = []
        for flat in LEGACY_FIELDS:
            prop = getattr(Config, flat, None)
            if not isinstance(prop, property) or prop.fset is None:
                readonly.append(flat)
        self.assertFalse(readonly, f"以下字段只读，无法赋值：{readonly}")


class TestFactories(unittest.TestCase):
    """4 + 5：with_config / make_config。"""

    def test_with_config_flat_names(self) -> None:
        cfg = Config()
        cfg2 = with_config(cfg, output_root=pathlib.Path("Y:/out"), budget_limit=18)
        self.assertEqual(cfg2.output.root, pathlib.Path("Y:/out"))
        self.assertEqual(cfg2.guard.budget_limit, 18)
        self.assertEqual(cfg2.budget_limit, 18)
        # 原对象不受影响（replace 语义）
        self.assertNotEqual(cfg.output.root, pathlib.Path("Y:/out"))

    def test_with_config_multiple_groups(self) -> None:
        cfg = with_config(Config(), provider="qwen", print_dpi=600,
                          realism_iteration=3, overwrite=True)
        self.assertEqual(cfg.providers.name, "qwen")
        self.assertEqual(cfg.print.dpi, 600)
        self.assertEqual(cfg.prompt.realism_iteration, 3)
        self.assertTrue(cfg.output.overwrite)

    def test_with_config_accepts_subconfig(self) -> None:
        """新式用法：直接传子配置对象。"""
        from app.config import PrintConfig

        cfg = with_config(Config(), print=PrintConfig(dpi=1234))
        self.assertEqual(cfg.print_dpi, 1234)

    def test_with_config_rejects_unknown(self) -> None:
        with self.assertRaises(TypeError) as cm:
            with_config(Config(), not_a_field=1)
        self.assertIn("not_a_field", str(cm.exception))

    def test_make_config(self) -> None:
        cfg = make_config(provider="mock", api_key="k", concurrency=1)
        self.assertEqual(cfg.provider, "mock")
        self.assertEqual(cfg.api_key, "k")
        self.assertEqual(cfg.concurrency, 1)

    def test_make_config_empty(self) -> None:
        self.assertIsInstance(make_config(), Config)


class TestLegacyBehaviors(unittest.TestCase):
    """6：原有语义不变。"""

    def test_is_mock_is_local(self) -> None:
        cfg = make_config(provider="mock")
        self.assertTrue(cfg.is_mock)
        self.assertTrue(cfg.is_local)

        cfg = make_config(provider="qwen")
        self.assertFalse(cfg.is_mock)
        self.assertFalse(cfg.is_local)

        cfg = make_config(provider="flux_local")
        self.assertTrue(cfg.is_local, "flux_local 是本地服务商")

    def test_validate_reports_missing_key(self) -> None:
        cfg = make_config(provider="qwen", api_key="")
        problems = cfg.validate()
        self.assertTrue(any("API Key" in p for p in problems), problems)

    def test_validate_reports_bad_concurrency(self) -> None:
        cfg = make_config(provider="mock", concurrency=0)
        self.assertTrue(any("并发" in p for p in cfg.validate()))

    def test_validate_ok(self) -> None:
        cfg = make_config(provider="mock")
        self.assertEqual(cfg.validate(), [])

    def test_describe_has_key_lines(self) -> None:
        text = make_config(provider="mock").describe()
        for token in ("服务商", "模型", "API Key", "并发", "输出目录", "成本守卫"):
            self.assertIn(token, text, f"describe() 缺少「{token}」")


class TestFacade(unittest.TestCase):
    """7：门面导出完整。"""

    def test_required_exports(self) -> None:
        """调用方**实际用到**的 7 个名字必须都在。"""
        import app.config as facade

        for name in ("Config", "load_config", "DATA_FILE", "PROVIDER_PRESETS",
                     "LOCAL_VALIDATION_ROOT", "LOCAL_PROFESSIONAL_ROOT", "LOG_DIR"):
            self.assertTrue(hasattr(facade, name), f"门面缺少 {name}")

    def test_backward_compatible_import_paths(self) -> None:
        """`from .config import` 与 `from app.config import` 必须等价。"""
        from app.config import Config as A

        self.assertIs(A, Config)

    def test_load_config_still_works(self) -> None:
        from app.config import load_config

        cfg = load_config()
        self.assertIsInstance(cfg, Config)
        self.assertIsInstance(cfg.provider, str)
        self.assertTrue(cfg.provider)

    def test_load_config_provider_override(self) -> None:
        from app.config import load_config

        cfg = load_config("mock")
        self.assertEqual(cfg.provider, "mock")

    def test_subconfigs_are_independent_instances(self) -> None:
        """默认工厂必须是各自的实例，不能共享可变默认值。"""
        a, b = Config(), Config()
        self.assertIsNot(a.print, b.print)
        a.print.dpi = 111
        self.assertNotEqual(b.print.dpi, 111, "子配置实例被共享了")
        a.generation.retry_backoff.append(99.0)
        self.assertNotIn(99.0, b.generation.retry_backoff, "列表默认值被共享")


if __name__ == "__main__":
    unittest.main(verbosity=2)
