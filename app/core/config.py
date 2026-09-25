# -*- coding: utf-8 -*-
"""核心层：配置。

Phase 0 重构产物 —— 把原来 `app/config.py` 里 **57 个平铺字段的 `Config`**
拆成 **12 个按关注点分组的子配置**，`Config` 只做组合。

## 为什么要拆

原来新增任何功能都要改两处：`Config` 加字段 + `load_config()` 加映射行。
57 个字段彼此语义无关（服务商 / 调度 / 输出 / 质检 / 提示词 / 优化器 /
守门 / 后处理 / 效果图 / 印刷 / 范围 / 图像模式混在一起），是典型的
「上帝对象」，极易误改。

## 兼容承诺（零破坏）

拆分**不改变任何调用方**：

1. **旧字段名继续可用** —— `cfg.print_dpi` / `cfg.concurrency` …
   由 57 个 `@property` 转发到对应子配置，**读写都行**。
2. **`dataclasses.replace` 的调用点**用 `with_config()` 替代
   （它接受同样的旧参数名，内部自动路由到子配置）。
3. **`Config(...)` 的构造点**用 `make_config()` 替代（同样接受旧参数名）。
4. **`from app.config import Config, load_config`** 完全不变
   （`app/config.py` 变成转发门面）。

## 子配置一览（字段数合计 57，与原 `Config` 一致）

| 子配置 | 字段 | 对应 settings.json 段 |
|--------|------|----------------------|
| `providers` | 7 | `providers.<name>` |
| `generation` | 6 | `generation` |
| `output` | 6 | `output` |
| `qc` | 2 | `qc` |
| `prompt` | 8 | `prompt` + `prompt_quality` |
| `optimizer` | 4 | `prompt_optimizer` |
| `guard` | 2 | `guard` |
| `postprocess` | 2 | `postprocess` |
| `effect` | 3 | `effect` + `effect_workflow` |
| `print` | 13 | `print` |
| `scope` | 2 | `scope` |
| `image_workflow` | 2 | `image_workflow` |

> ⚠️ 子配置字段叫 **`providers`（复数）** 而不是 `provider` ——
> 因为 `Config.provider` 是沿用了很久的**字符串**（服务商名，32 处在用），
> 若被对象占用会静默破坏所有 `cfg.provider == "qwen"` 判断。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields as dc_fields, replace as dc_replace
from pathlib import Path
from typing import Any

from .paths import PACKAGE_ROOT, STATE_ROOT

__all__ = [
    # 常量
    "ROOT", "ENV_FILE", "DATA_FILE", "OUTPUT_ROOT",
    "LOCAL_VALIDATION_ROOT", "LOCAL_PROFESSIONAL_ROOT", "LOG_DIR", "FONT_DIR",
    "CUTOUT_MODES", "PROVIDER_PRESETS",
    # 子配置
    "ProviderConfig", "GenerationConfig", "OutputConfig", "QcConfig",
    "PromptConfig", "OptimizerConfig", "GuardConfig", "PostprocessConfig",
    "EffectConfig", "PrintConfig", "ScopeConfig", "ImageWorkflowConfig",
    # 主配置与工具
    "Config", "load_config", "with_config", "make_config", "load_dotenv",
]

# ---------------------------------------------------------------- 路径常量
ROOT = PACKAGE_ROOT
ENV_FILE = STATE_ROOT / ".env"
DATA_FILE = PACKAGE_ROOT / "data" / "stores.json"
OUTPUT_ROOT = STATE_ROOT / "output"
LOCAL_VALIDATION_ROOT = STATE_ROOT / "output_local_validation"
LOCAL_PROFESSIONAL_ROOT = STATE_ROOT / "output_local_professional_v9"
LOG_DIR = STATE_ROOT / "logs"
FONT_DIR = PACKAGE_ROOT / "assets" / "fonts"

# 去背方式取值（印刷导出）
CUTOUT_MODES = ("auto", "rembg", "fallback")


# ---------------------------------------------------------------- .env
def load_dotenv(path: Path = ENV_FILE) -> int:
    """把 `.env` 里的键值对加载进 ``os.environ``（不覆盖已有变量）。"""
    if not path.exists():
        return 0
    loaded = 0
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
                loaded += 1
    except OSError:
        return 0
    return loaded


# ---------------------------------------------------------------- 环境变量助手
def _str(key: str, default: str = "") -> str:
    return os.environ.get(key, default) or default


def _bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int(key: str, default: int) -> int:
    try:
        return int(_str(key) or default)
    except (ValueError, TypeError):
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(_str(key) or default)
    except (ValueError, TypeError):
        return default


def _cutout_mode(value) -> str:
    """校验去背方式；非法值回落到 auto（不因配置写错而中断导出）。"""
    v = str(value or "").strip().lower()
    return v if v in CUTOUT_MODES else "auto"


def _pick(env_key: str, stored, preset, default):
    """环境变量 > settings.json > 内置预设 > 默认值。"""
    v = _str(env_key)
    if v != "":
        return v
    if stored not in (None, ""):
        return stored
    if preset not in (None, ""):
        return preset
    return default


def _section(data: dict, key: str) -> dict:
    """取 settings.json 的一个段（保证返回 dict）。"""
    v = data.get(key) if isinstance(data, dict) else None
    return v if isinstance(v, dict) else {}


def _as_float_list(value, fallback: list[float]) -> list[float]:
    """把 `retry_backoff` 规整成 float 列表（兼容 "2,5,15" 这种字符串写法）。"""
    if isinstance(value, str):
        try:
            value = [float(x) for x in value.split(",") if x.strip()]
        except ValueError:
            return list(fallback)
    try:
        return [float(x) for x in (value or fallback)]
    except (TypeError, ValueError):
        return list(fallback)


# ---------------------------------------------------------------- 服务商预设
def _build_presets() -> dict[str, dict]:
    """构建服务商预设（委托给 providers.catalog，保持单一真源）。"""
    try:
        from ..providers.catalog import PROVIDER_CATALOG

        return {
            name: {
                "label": meta.get("label", name),
                "env_key": meta.get("env_key", ""),
                "base_url": meta.get("base_url", ""),
            }
            for name, meta in PROVIDER_CATALOG.items()
        }
    except Exception:  # noqa: BLE001 - 导入期容错
        return {}


PROVIDER_PRESETS: dict[str, dict] = _build_presets()


# ================================================================ 子配置
@dataclass
class ProviderConfig:
    """服务商与模型（settings.json 的 `providers.<name>`）。"""

    name: str = "mock"
    label: str = "本地模拟"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    supports_negative: bool = False
    price_per_image: float = 0.0

    @property
    def is_local(self) -> bool:
        """本地服务商无需云端 API Key。"""
        return self.name in {"mock", "flux_local"}

    @property
    def is_mock(self) -> bool:
        return self.name == "mock"

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.name not in PROVIDER_PRESETS:
            problems.append(f"未知服务商 '{self.name}'")
        if not self.is_local and not self.api_key:
            env_key = PROVIDER_PRESETS.get(self.name, {}).get("env_key", "")
            problems.append(
                "缺少 API Key：请在「设置 → 模型 API 配置」中填写"
                + (f"（或设置环境变量 {env_key}）" if env_key else "")
            )
        return problems


@dataclass
class GenerationConfig:
    """调度参数（并发 / 限流 / 重试 / 尺寸）。"""

    concurrency: int = 4
    rpm_limit: int = 0                       # 0 = 不限制
    retry_max: int = 3
    retry_backoff: list[float] = field(default_factory=lambda: [2.0, 5.0, 15.0])
    timeout: float = 120.0
    size: str = "1024x1024"

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.concurrency < 1:
            problems.append("并发数必须 ≥ 1")
        if self.rpm_limit and self.concurrency > self.rpm_limit:
            problems.append(
                f"并发({self.concurrency}) 超过官方限流({self.rpm_limit} RPM)，会被服务端拒绝"
            )
        return problems


@dataclass
class OutputConfig:
    """输出目录与批次（settings.json 的 `output`）。"""

    base_root: Path = OUTPUT_ROOT
    root: Path = OUTPUT_ROOT
    batch_id: str = ""
    batch_created_at: str = ""
    timestamp_prefix: bool = False
    overwrite: bool = False


@dataclass
class QcConfig:
    """OCR 质检。"""

    enabled: bool = True
    min_similarity: float = 0.80


@dataclass
class PromptConfig:
    """提示词与质量模板（`prompt` + `prompt_quality` 两段）。"""

    version: str = ""                        # 留空 = 用数据里的当前生产版本
    text_wrapper: str = "【】"
    normalize_negative: bool = False
    use_simple: bool = False
    template: str = ""
    optimized_template: str = ""
    use_optimized: bool = False
    realism_iteration: int = 1               # 每个新批次自动递进（最高 3）


@dataclass
class OptimizerConfig:
    """DeepSeek 提示词优化器（`prompt_optimizer`）。"""

    enabled: bool = True
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"


@dataclass
class GuardConfig:
    """成本守卫。"""

    budget_limit: int = 400
    dry_run: bool = False


@dataclass
class PostprocessConfig:
    """生成后处理（刷白背景）。"""

    whiten_background: bool = True
    whiten_threshold: int = 45


@dataclass
class EffectConfig:
    """效果图合成（`effect` + `effect_workflow` 两段）。"""

    auto_match_background: bool = True
    params: dict = field(default_factory=dict)
    background_asset: dict = field(default_factory=dict)


@dataclass
class PrintConfig:
    """印刷 TIF 导出（settings.json 的 `print`）。"""

    enabled: bool = True
    width_cm: float = 60.0
    dpi: int = 300
    bleed_mm: float = 3.0
    icc_path: str = ""
    white_ink: bool = True
    white_ink_invert: bool = False
    dieline: bool = True
    cutout: str = "auto"                     # auto | rembg | fallback
    spot_channel: bool = False
    max_pixels: int = 8000
    keep_work: bool = True
    auto_clean_work: bool = False


@dataclass
class ScopeConfig:
    """生成范围。"""

    run_limit: int = 0                       # 0 = 全部
    selected_store_indexes: list[str] = field(
        default_factory=lambda: [f"{index:02d}" for index in range(1, 24)]
    )


@dataclass
class ImageWorkflowConfig:
    """文生图 / 图生图 / 多图生图。"""

    mode: str = "text"                       # text | image | multi
    reference_assets: list[dict] = field(default_factory=list)


# 子配置名 → 类型（供兼容层与工厂使用）
_SUBCONFIGS: dict[str, type] = {
    "providers": ProviderConfig,
    "generation": GenerationConfig,
    "output": OutputConfig,
    "qc": QcConfig,
    "prompt": PromptConfig,
    "optimizer": OptimizerConfig,
    "guard": GuardConfig,
    "postprocess": PostprocessConfig,
    "effect": EffectConfig,
    "print": PrintConfig,
    "scope": ScopeConfig,
    "image_workflow": ImageWorkflowConfig,
}

# 旧扁平字段名 → (子配置名, 子字段名)
#   这份映射是**兼容层的唯一真源**：@property 与 with_config/make_config 都读它。
FLAT_TO_GROUP: dict[str, tuple[str, str]] = {
    # providers
    "provider": ("providers", "name"),
    "provider_label": ("providers", "label"),
    "api_key": ("providers", "api_key"),
    "base_url": ("providers", "base_url"),
    "model": ("providers", "model"),
    "supports_negative": ("providers", "supports_negative"),
    "price_per_image": ("providers", "price_per_image"),
    # generation
    "concurrency": ("generation", "concurrency"),
    "rpm_limit": ("generation", "rpm_limit"),
    "retry_max": ("generation", "retry_max"),
    "retry_backoff": ("generation", "retry_backoff"),
    "timeout": ("generation", "timeout"),
    "size": ("generation", "size"),
    # output
    "output_base_root": ("output", "base_root"),
    "output_root": ("output", "root"),
    "batch_id": ("output", "batch_id"),
    "batch_created_at": ("output", "batch_created_at"),
    "timestamp_prefix": ("output", "timestamp_prefix"),
    "overwrite": ("output", "overwrite"),
    # qc
    "qc_enabled": ("qc", "enabled"),
    "qc_min_similarity": ("qc", "min_similarity"),
    # prompt
    "prompt_version": ("prompt", "version"),
    "text_wrapper": ("prompt", "text_wrapper"),
    "normalize_negative": ("prompt", "normalize_negative"),
    "use_simple_prompt": ("prompt", "use_simple"),
    "quality_template": ("prompt", "template"),
    "optimized_quality_template": ("prompt", "optimized_template"),
    "use_optimized_quality_template": ("prompt", "use_optimized"),
    "realism_iteration": ("prompt", "realism_iteration"),
    # optimizer
    "prompt_optimizer_enabled": ("optimizer", "enabled"),
    "prompt_optimizer_api_key": ("optimizer", "api_key"),
    "prompt_optimizer_base_url": ("optimizer", "base_url"),
    "prompt_optimizer_model": ("optimizer", "model"),
    # guard
    "budget_limit": ("guard", "budget_limit"),
    "dry_run": ("guard", "dry_run"),
    # postprocess
    "whiten_background": ("postprocess", "whiten_background"),
    "whiten_threshold": ("postprocess", "whiten_threshold"),
    # effect
    "auto_match_effect_background": ("effect", "auto_match_background"),
    "effect_params": ("effect", "params"),
    "effect_background_asset": ("effect", "background_asset"),
    # print
    "print_export_enabled": ("print", "enabled"),
    "print_width_cm": ("print", "width_cm"),
    "print_dpi": ("print", "dpi"),
    "print_bleed_mm": ("print", "bleed_mm"),
    "print_icc_path": ("print", "icc_path"),
    "print_white_ink": ("print", "white_ink"),
    "print_white_ink_invert": ("print", "white_ink_invert"),
    "print_dieline": ("print", "dieline"),
    "print_cutout": ("print", "cutout"),
    "print_spot_channel": ("print", "spot_channel"),
    "print_max_pixels": ("print", "max_pixels"),
    "print_keep_work": ("print", "keep_work"),
    "print_auto_clean_work": ("print", "auto_clean_work"),
    # scope
    "run_limit": ("scope", "run_limit"),
    "selected_store_indexes": ("scope", "selected_store_indexes"),
    # image_workflow
    "image_mode": ("image_workflow", "mode"),
    "reference_assets": ("image_workflow", "reference_assets"),
}


def _make_flat_property(group: str, attr: str):
    """生成一个转发到子配置的 property（读 + 写）。"""

    def _getter(self: "Config"):
        return getattr(getattr(self, group), attr)

    def _setter(self: "Config", value) -> None:
        setattr(getattr(self, group), attr, value)

    return property(_getter, _setter,
                    doc=f"兼容属性：转发到 `{group}.{attr}`（旧字段名）")


# ================================================================ 主配置
@dataclass
class Config:
    """运行时配置（组合 12 个子配置）。

    ⚠️ 57 个旧字段名通过下面的 `@property` 继续可用，**调用方无需改动**。
    """

    providers: ProviderConfig = field(default_factory=ProviderConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    qc: QcConfig = field(default_factory=QcConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    optimizer: OptimizerConfig = field(default_factory=OptimizerConfig)
    guard: GuardConfig = field(default_factory=GuardConfig)
    postprocess: PostprocessConfig = field(default_factory=PostprocessConfig)
    effect: EffectConfig = field(default_factory=EffectConfig)
    print: PrintConfig = field(default_factory=PrintConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    image_workflow: ImageWorkflowConfig = field(default_factory=ImageWorkflowConfig)

    # ---- 语义便捷属性（保留原有对外行为）----
    @property
    def is_mock(self) -> bool:
        return self.providers.is_mock

    @property
    def is_local(self) -> bool:
        return self.providers.is_local

    def validate(self) -> list[str]:
        """返回配置问题清单（空列表 = 全部正常）—— 聚合各子配置。"""
        problems: list[str] = []
        problems.extend(self.providers.validate())
        problems.extend(self.generation.validate())
        return problems

    def describe(self) -> str:
        """人类可读的配置摘要（保持原有格式）。"""
        g = self.generation
        pv = self.providers
        lines = [
            f"服务商      : {pv.label}",
            f"模型        : {pv.model or '(未设置)'}",
            f"API Key     : {'已配置' if pv.api_key else '未配置'}",
            f"并发        : {g.concurrency}"
            + (f"（官方限流 {g.rpm_limit} RPM）" if g.rpm_limit else "（无 QPS 限制）"),
            f"重试        : 最多 {g.retry_max} 次，退避 {g.retry_backoff}",
            f"负向提示词  : {'支持' if pv.supports_negative else '不支持（将忽略）'}",
            f"输出目录    : {self.output.root}",
            f"命名规则    : {'时间戳前缀 + 语义名' if self.output.timestamp_prefix else '语义名'}",
            f"OCR 质检    : {'开启' if self.qc.enabled else '关闭'}"
            + (f"（阈值 {self.qc.min_similarity}）" if self.qc.enabled else ""),
            f"成本守卫    : 最多 {self.guard.budget_limit} 次调用",
            f"单张成本    : ¥{pv.price_per_image:.4f}"
            + (f"（138 张约 ¥{pv.price_per_image * 138:.2f}）" if pv.price_per_image else ""),
        ]
        return "\n".join(lines)

    # ---- 分组视图别名（让 `cfg.print` 这种写法在保留 `print` 段的同时更直观）----
    @property
    def print_config(self) -> PrintConfig:
        """`print` 是 Python 关键字友好的字段名，这个别名更明确。"""
        return self.print


# 动态挂载 57 个兼容 property（避免手写 57 份样板）
for _flat, (_grp, _attr) in FLAT_TO_GROUP.items():
    setattr(Config, _flat, _make_flat_property(_grp, _attr))
del _flat, _grp, _attr


# ================================================================ 兼容工厂
def _flat_overrides_to_groups(overrides: dict[str, Any]) -> dict[str, dict]:
    """把旧扁平参数名翻译成 {子配置名: {子字段: 值}}。"""
    grouped: dict[str, dict] = {}
    unknown: list[str] = []
    for key, value in overrides.items():
        if key in _SUBCONFIGS:
            # 直接给子配置（新式用法）
            grouped[key] = value
            continue
        target = FLAT_TO_GROUP.get(key)
        if target is None:
            unknown.append(key)
            continue
        grp, attr = target
        grouped.setdefault(grp, {})[attr] = value
    if unknown:
        raise TypeError(
            "未知的配置项：" + ", ".join(sorted(unknown))
            + "。可用旧字段名见 config.FLAT_TO_GROUP，或直接传子配置名："
            + ", ".join(_SUBCONFIGS)
        )
    return grouped


def with_config(cfg: Config, **overrides) -> Config:
    """按**旧扁平字段名**覆盖配置，自动路由到对应子配置。

    用法与 `dataclasses.replace` 完全一致，只是换个名字：

        with_config(cfg, output_root=X)          # → output.root = X
        with_config(cfg, budget_limit=18)        # → guard.budget_limit = 18
        with_config(cfg, print_dpi=900)          # → print.dpi = 900

    混合用法也支持（直接传子配置对象）：`with_config(cfg, print=PrintConfig(...))`。
    """
    grouped = _flat_overrides_to_groups(overrides)
    new_groups: dict[str, Any] = {}
    for grp, patch in grouped.items():
        if not isinstance(patch, dict):
            new_groups[grp] = patch
            continue
        current = getattr(cfg, grp)
        new_groups[grp] = dc_replace(current, **patch)
    return dc_replace(cfg, **new_groups)


def make_config(**flat_kwargs) -> Config:
    """用**旧扁平字段名**构造 `Config`（供测试与内部使用）。

        make_config(provider="mock", output_root=Path(...), concurrency=1)
    """
    cfg = Config()
    if not flat_kwargs:
        return cfg
    return with_config(cfg, **flat_kwargs)


# ================================================================ 加载
def _provider_section(name: str, data: dict) -> ProviderConfig:
    from ..providers.catalog import PROVIDER_CATALOG, get_model

    catalog = PROVIDER_CATALOG[name]
    stored = _section(data.get("providers") or {}, name)
    model_id = _str("MODEL") or stored.get("model") or catalog.get("default_model", "")
    minfo = get_model(name, model_id) or {}
    env_key_name = catalog.get("env_key") or ""
    api_key = (_str(env_key_name) if env_key_name else "") or stored.get("api_key", "")
    return ProviderConfig(
        name=name,
        label=catalog.get("label", name),
        api_key=api_key,
        base_url=_pick("BASE_URL", stored.get("base_url"), catalog.get("base_url", ""), ""),
        model=model_id,
        supports_negative=bool(minfo.get("negative", False)),
        price_per_image=float(minfo.get("price", 0.0)),
    )


def _load_sections(data: dict) -> dict[str, Any]:
    """把 settings.json 的 16 段翻译成 12 个子配置。"""
    from ..providers.catalog import PROVIDER_CATALOG

    gen = _section(data, "generation")
    out = _section(data, "output")
    qc = _section(data, "qc")
    guard = _section(data, "guard")
    pr = _section(data, "prompt")
    quality = _section(data, "prompt_quality")
    optimizer = _section(data, "prompt_optimizer")
    post = _section(data, "postprocess")
    scope = _section(data, "scope")
    image_workflow = _section(data, "image_workflow")
    effect_workflow = _section(data, "effect_workflow")
    effect_auto = _section(data, "effect")
    pe = _section(data, "print")

    name = (
        data.get("_provider_override")
        or _str("PROVIDER")
        or data.get("active_provider")
        or "mock"
    )
    name = str(name).lower()
    if name not in PROVIDER_CATALOG:
        raise ValueError(f"未知服务商 '{name}'，可选：{', '.join(PROVIDER_CATALOG)}")

    base_out_root = _pick("OUTPUT_ROOT", out.get("root"), "", "") or str(OUTPUT_ROOT)
    # ⚠️ Phase 1 分包后 batches 移到了 app/state/。这里保持**函数内延迟导入**，
    #    避免 core/config 与 state/batches 在模块级形成循环。
    from ..state.batches import is_safe_batch_id

    batch_id = str(out.get("active_batch") or "").strip()
    if not is_safe_batch_id(batch_id):
        batch_id = ""
    out_root = str(Path(base_out_root) / batch_id) if batch_id else base_out_root

    return {
        "providers": _provider_section(name, data),
        "generation": GenerationConfig(
            concurrency=_int("CONCURRENCY", gen.get("concurrency", 4)),
            rpm_limit=_int("RPM_LIMIT", (PROVIDER_CATALOG[name] or {}).get("rpm", 0)),
            retry_max=_int("RETRY_MAX", gen.get("retry_max", 3)),
            retry_backoff=_as_float_list(gen.get("retry_backoff"), [2.0, 5.0, 15.0]),
            timeout=_float("TIMEOUT", gen.get("timeout", 120)),
            size=gen.get("size", "1024x1024"),
        ),
        "output": OutputConfig(
            base_root=Path(base_out_root),
            root=Path(out_root),
            batch_id=batch_id,
            batch_created_at=str(out.get("active_batch_created_at") or ""),
            timestamp_prefix=_bool("TIMESTAMP_PREFIX", bool(out.get("timestamp_prefix", False))),
            overwrite=_bool("OVERWRITE", bool(out.get("overwrite", False))),
        ),
        "qc": QcConfig(
            enabled=_bool("QC_ENABLED", bool(qc.get("enabled", True))),
            min_similarity=_float("QC_MIN_SIMILARITY", float(qc.get("min_similarity", 0.8))),
        ),
        "prompt": PromptConfig(
            version=_str("PROMPT_VERSION") or pr.get("version", ""),
            text_wrapper=pr.get("text_wrapper", "【】"),
            normalize_negative=bool(pr.get("normalize_negative", False)),
            use_simple=bool(pr.get("use_simple", False)),
            template=str(quality.get("template") or ""),
            optimized_template=str(quality.get("optimized_template") or ""),
            use_optimized=bool(quality.get("use_optimized", False)),
            realism_iteration=max(1, min(3, int(quality.get("realism_iteration", 1) or 1))),
        ),
        "optimizer": OptimizerConfig(
            enabled=_bool("DEEPSEEK_PROMPT_OPTIMIZER_ENABLED",
                          bool(optimizer.get("enabled", True))),
            api_key=_str("DEEPSEEK_API_KEY") or optimizer.get("api_key", ""),
            base_url=(_str("DEEPSEEK_BASE_URL") or optimizer.get("base_url")
                      or "https://api.deepseek.com"),
            model=_str("DEEPSEEK_MODEL") or optimizer.get("model") or "deepseek-flash",
        ),
        "guard": GuardConfig(
            budget_limit=_int("BUDGET_LIMIT", guard.get("budget_limit", 400)),
            dry_run=_bool("DRY_RUN", False),
        ),
        "postprocess": PostprocessConfig(
            whiten_background=_bool("WHITEN_BACKGROUND",
                                    bool(post.get("whiten_background", True))),
            whiten_threshold=_int("WHITEN_THRESHOLD", int(post.get("threshold", 45))),
        ),
        "effect": EffectConfig(
            auto_match_background=bool(effect_auto.get("auto_match_background", True)),
            params=dict(effect_auto.get("params") or {}),
            background_asset=dict(effect_workflow.get("background_asset") or {}),
        ),
        "print": PrintConfig(
            enabled=_bool("PRINT_EXPORT_ENABLED", bool(pe.get("enabled", True))),
            width_cm=_float("PRINT_WIDTH_CM", float(pe.get("width_cm", 60.0) or 60.0)),
            dpi=_int("PRINT_DPI", int(pe.get("dpi", 300) or 300)),
            bleed_mm=_float("PRINT_BLEED_MM", float(pe.get("bleed_mm", 3.0) or 3.0)),
            icc_path=str(pe.get("icc_path") or ""),
            white_ink=bool(pe.get("white_ink", True)),
            white_ink_invert=bool(pe.get("white_ink_invert", False)),
            dieline=bool(pe.get("dieline", True)),
            cutout=_cutout_mode(pe.get("cutout")),
            spot_channel=bool(pe.get("spot_channel", False)),
            max_pixels=max(2000, min(20000, int(pe.get("max_pixels", 8000) or 8000))),
            keep_work=bool(pe.get("keep_work", True)),
            auto_clean_work=bool(pe.get("auto_clean_work", False)),
        ),
        "scope": ScopeConfig(
            run_limit=int(gen.get("limit", 0) or 0),
            selected_store_indexes=[str(x) for x in (scope.get("store_indexes") or [])],
        ),
        "image_workflow": ImageWorkflowConfig(
            mode=str(image_workflow.get("mode") or "text"),
            reference_assets=list(image_workflow.get("reference_assets") or []),
        ),
    }


def load_config(provider: str | None = None) -> Config:
    """加载配置。

    Args:
        provider: 临时覆盖当前服务商（不改动 settings.json）
    """
    from ..providers.catalog import PROVIDER_CATALOG
    from ..state.settings_store import get_store

    load_dotenv()
    data = get_store().load()
    if provider:
        data = dict(data)
        data["_provider_override"] = provider
    return Config(**_load_sections(data))
