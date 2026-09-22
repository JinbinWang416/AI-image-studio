# -*- coding: utf-8 -*-
"""
配置系统。

**优先级：环境变量 > `config/settings.json` > 内置预设**

- 环境变量：适合服务器 / CI 强制覆盖（`.env` 文件也会被加载为环境变量）
- `settings.json`：Web 设置界面读写，改完即时生效，无需重启
- 内置预设：来自 `app/providers/catalog.py`（调研结论）

服务商与模型的能力标注（价格 / 负向词 / 限流）统一来自 catalog，
本模块只负责把它们组装成一个运行时可用的 `Config` 对象。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .paths import PACKAGE_ROOT, STATE_ROOT

# ---------------------------------------------------------------- 路径
ROOT = PACKAGE_ROOT
ENV_FILE = STATE_ROOT / ".env"
DATA_FILE = PACKAGE_ROOT / "data" / "stores.json"
OUTPUT_ROOT = STATE_ROOT / "output"
LOCAL_VALIDATION_ROOT = STATE_ROOT / "output_local_validation"
LOCAL_PROFESSIONAL_ROOT = STATE_ROOT / "output_local_professional_v9"
LOG_DIR = STATE_ROOT / "logs"
FONT_DIR = PACKAGE_ROOT / "assets" / "fonts"


# ---------------------------------------------------------------- .env
def load_dotenv(path: Path = ENV_FILE) -> int:
    """加载 .env 文件（不覆盖已存在的环境变量）。返回加载条数。"""
    if not path.exists():
        return 0
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def _str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _bool(key: str, default: bool = False) -> bool:
    raw = _str(key).lower()
    return default if raw == "" else raw in {"1", "true", "yes", "on", "y"}


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


# 去背方式取值（印刷导出）
CUTOUT_MODES = ("auto", "rembg", "fallback")


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


# ---------------------------------------------------------------- 服务商预置
def _build_presets() -> dict[str, dict]:
    """从 catalog 派生服务商预置（供旧代码兼容引用）。"""
    from .providers.catalog import PROVIDER_CATALOG

    presets: dict[str, dict] = {}
    for name, p in PROVIDER_CATALOG.items():
        models = p.get("models") or [{}]
        default = next(
            (m for m in models if m.get("id") == p.get("default_model")), models[0]
        )
        presets[name] = {
            "label": p.get("label", name),
            "base_url": p.get("base_url", ""),
            "model": p.get("default_model", ""),
            "env_key": p.get("env_key", ""),
            "supports_negative": default.get("negative", False),
            "supports_n": default.get("max_n", 1) > 1,
            "rpm": default.get("rpm", 0),
            "price_per_image": default.get("price", 0.0),
            "note": p.get("summary", ""),
        }
    return presets


PROVIDER_PRESETS: dict[str, dict] = _build_presets()


# ---------------------------------------------------------------- 配置对象
@dataclass
class Config:
    """运行时配置。"""

    # --- 服务商 ---
    provider: str = "mock"
    provider_label: str = "本地模拟"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    supports_negative: bool = False
    price_per_image: float = 0.0

    # --- 调度 ---
    concurrency: int = 4
    rpm_limit: int = 0                                   # 0 = 不限制
    retry_max: int = 3
    retry_backoff: list[float] = field(default_factory=lambda: [2.0, 5.0, 15.0])
    timeout: float = 120.0
    size: str = "1024x1024"

    # --- 输出 ---
    output_base_root: Path = OUTPUT_ROOT
    output_root: Path = OUTPUT_ROOT
    batch_id: str = ""
    batch_created_at: str = ""
    timestamp_prefix: bool = False
    overwrite: bool = False

    # --- 质检 ---
    qc_enabled: bool = True
    qc_min_similarity: float = 0.80

    # --- 提示词 ---
    prompt_version: str = ""            # 留空 = 用数据里的当前生产版本
    text_wrapper: str = "【】"
    normalize_negative: bool = False
    use_simple_prompt: bool = False

    # --- 可选的文本提示词优化（仅用于 OpenAI 房屋中介 6 图流程）---
    prompt_optimizer_enabled: bool = True
    prompt_optimizer_api_key: str = ""
    prompt_optimizer_base_url: str = "https://api.deepseek.com"
    prompt_optimizer_model: str = "deepseek-flash"

    # --- 守门 ---
    budget_limit: int = 400
    dry_run: bool = False

    # --- 后处理 ---
    whiten_background: bool = True     # 生成后把外部背景刷成纯白
    whiten_threshold: int = 45         # flood fill 颜色容忍度

    # --- 效果图 ---
    auto_match_effect_background: bool = True   # 未选背景时，按门店自动匹配同行业 AI 背景
    effect_params: dict = field(default_factory=dict)   # 效果图合成可调参数（网页滑块）

    # --- 运行偏好（来自设置，不在 .env 中）---
    run_limit: int = 0                # 0 = 全部
    selected_store_indexes: list[str] = field(default_factory=lambda: [f"{index:02d}" for index in range(1, 24)])

    # --- 图像模式与项目质量模板（参考图本体只在内存中出现）---
    image_mode: str = "text"          # text | image | multi
    reference_assets: list[dict] = field(default_factory=list)
    quality_template: str = ""
    optimized_quality_template: str = ""
    use_optimized_quality_template: bool = False
    # 每个新批次自动递进至更严格的真实感档位（最高 3）。
    realism_iteration: int = 1
    # 真实门店玻璃背景只存公开元数据；服务启动时才解析本机文件路径。
    effect_background_asset: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ 印刷导出
    # 批次完成后自动把 PNG 处理成印刷厂可 RIP 的多图层 TIF。
    # 详见 docs/印刷TIF导出_分析报告与规划.md
    print_export_enabled: bool = True          # 总开关（失败不阻断主流程）
    print_width_cm: float = 60.0               # 贴纸实际宽度（cm）
    print_dpi: int = 300                       # 目标印刷 DPI
    print_bleed_mm: float = 3.0                # 刀模出血（mm）
    print_icc_path: str = ""                   # CMYK ICC 路径；留空则用 config/icc/ 或系统默认
    print_white_ink: bool = True               # 生成白墨层（玻璃静电贴必做）
    print_white_ink_invert: bool = False       # 白墨极性：False=不透明区印白墨（印刷惯例）
    print_dieline: bool = True                 # 生成刀模线层
    # 去背方式：auto（有 rembg 就用）| rembg（强制，不可用则降级）| fallback（跳过 rembg）
    #   实测：对背景纯净的 AI 图，fallback 与 rembg 最终产物几乎相同（白墨层同为硬边），
    #   但 fallback 快约 6 倍（0.42s vs 2.53s/张）。批量导出时切 fallback 能省大量时间。
    print_cutout: str = "auto"
    print_spot_channel: bool = False           # 补专色通道（本期仅留开关，未实现）
    print_max_pixels: int = 8000               # 单边像素上限，防止极大尺寸打爆内存
    print_keep_work: bool = True               # 保留 _work/ 母版（印刷确认前不清理）
    print_auto_clean_work: bool = False        # 印刷确认后自动清理 _work（需手动触发确认）

    @property
    def is_mock(self) -> bool:
        return self.provider == "mock"

    @property
    def is_local(self) -> bool:
        """本地服务商无需云端 API Key。"""
        return self.provider in {"mock", "flux_local"}

    def validate(self) -> list[str]:
        """返回配置问题清单（空列表 = 全部正常）。"""
        problems: list[str] = []
        if self.provider not in PROVIDER_PRESETS:
            problems.append(f"未知服务商 '{self.provider}'")
        if not self.is_local and not self.api_key:
            env_key = PROVIDER_PRESETS.get(self.provider, {}).get("env_key", "")
            problems.append(
                f"缺少 API Key：请在「设置 → 模型 API 配置」中填写"
                + (f"（或设置环境变量 {env_key}）" if env_key else "")
            )
        if self.concurrency < 1:
            problems.append("并发数必须 ≥ 1")
        if self.rpm_limit and self.concurrency > self.rpm_limit:
            problems.append(
                f"并发({self.concurrency}) 超过官方限流({self.rpm_limit} RPM)，会被服务端拒绝"
            )
        return problems

    def describe(self) -> str:
        """人类可读的配置摘要。"""
        lines = [
            f"服务商      : {self.provider_label}",
            f"模型        : {self.model or '(未设置)'}",
            f"API Key     : {'已配置' if self.api_key else '未配置'}",
            f"并发        : {self.concurrency}"
            + (f"（官方限流 {self.rpm_limit} RPM）" if self.rpm_limit else "（无 QPS 限制）"),
            f"重试        : 最多 {self.retry_max} 次，退避 {self.retry_backoff}",
            f"负向提示词  : {'支持' if self.supports_negative else '不支持（将忽略）'}",
            f"输出目录    : {self.output_root}",
            f"命名规则    : {'时间戳前缀 + 语义名' if self.timestamp_prefix else '语义名'}",
            f"OCR 质检    : {'开启' if self.qc_enabled else '关闭'}"
            + (f"（阈值 {self.qc_min_similarity}）" if self.qc_enabled else ""),
            f"成本守卫    : 最多 {self.budget_limit} 次调用",
            f"单张成本    : ¥{self.price_per_image:.4f}"
            + (f"（138 张约 ¥{self.price_per_image * 138:.2f}）" if self.price_per_image else ""),
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------- 加载
def load_config(provider: str | None = None) -> Config:
    """加载配置。

    Args:
        provider: 临时覆盖当前服务商（不改动 settings.json）
    """
    from .providers.catalog import PROVIDER_CATALOG, get_model
    from .settings import get_store

    load_dotenv()
    store = get_store()
    data = store.load()

    name = (
        provider
        or _str("PROVIDER")
        or data.get("active_provider")
        or "mock"
    ).lower()
    if name not in PROVIDER_CATALOG:
        raise ValueError(
            f"未知服务商 '{name}'，可选：{', '.join(PROVIDER_CATALOG)}"
        )

    catalog = PROVIDER_CATALOG[name]
    stored = data.get("providers", {}).get(name, {}) or {}

    model_id = (
        _str("MODEL")
        or stored.get("model")
        or catalog.get("default_model", "")
    )
    minfo = get_model(name, model_id) or {}

    gen = data.get("generation", {}) or {}
    out = data.get("output", {}) or {}
    qc = data.get("qc", {}) or {}
    guard = data.get("guard", {}) or {}
    pr = data.get("prompt", {}) or {}
    optimizer = data.get("prompt_optimizer", {}) or {}
    post = data.get("postprocess", {}) or {}
    scope = data.get("scope", {}) or {}
    image_workflow = data.get("image_workflow", {}) or {}
    quality = data.get("prompt_quality", {}) or {}
    effect_workflow = data.get("effect_workflow", {}) or {}
    effect_auto = data.get("effect", {}) or {}
    pe = data.get("print", {}) or {}          # 印刷导出配置段

    env_key_name = catalog.get("env_key") or ""
    api_key = (_str(env_key_name) if env_key_name else "") or stored.get("api_key", "")

    base_out_root = _pick("OUTPUT_ROOT", out.get("root"), "", "") or str(OUTPUT_ROOT)
    from .batches import is_safe_batch_id
    batch_id = str(out.get("active_batch") or "").strip()
    if not is_safe_batch_id(batch_id):
        batch_id = ""
    out_root = str(Path(base_out_root) / batch_id) if batch_id else base_out_root

    backoff = gen.get("retry_backoff") or [2, 5, 15]
    if isinstance(backoff, str):
        try:
            backoff = [float(x) for x in backoff.split(",") if x.strip()]
        except ValueError:
            backoff = [2, 5, 15]

    return Config(
        provider=name,
        provider_label=catalog.get("label", name),
        api_key=api_key,
        base_url=_pick("BASE_URL", stored.get("base_url"), catalog.get("base_url", ""), ""),
        model=model_id,
        supports_negative=bool(minfo.get("negative", False)),
        price_per_image=float(minfo.get("price", 0.0)),
        concurrency=_int("CONCURRENCY", gen.get("concurrency", 4)),
        rpm_limit=_int("RPM_LIMIT", minfo.get("rpm", 0)),
        retry_max=_int("RETRY_MAX", gen.get("retry_max", 3)),
        retry_backoff=[float(x) for x in backoff],
        timeout=_float("TIMEOUT", gen.get("timeout", 120)),
        size=gen.get("size", "1024x1024"),
        output_base_root=Path(base_out_root),
        output_root=Path(out_root),
        batch_id=batch_id,
        batch_created_at=str(out.get("active_batch_created_at") or ""),
        timestamp_prefix=_bool("TIMESTAMP_PREFIX", bool(out.get("timestamp_prefix", False))),
        overwrite=_bool("OVERWRITE", bool(out.get("overwrite", False))),
        qc_enabled=_bool("QC_ENABLED", bool(qc.get("enabled", True))),
        qc_min_similarity=_float("QC_MIN_SIMILARITY", float(qc.get("min_similarity", 0.8))),
        prompt_version=_str("PROMPT_VERSION") or pr.get("version", ""),
        text_wrapper=pr.get("text_wrapper", "【】"),
        normalize_negative=bool(pr.get("normalize_negative", False)),
        use_simple_prompt=bool(pr.get("use_simple", False)),
        prompt_optimizer_enabled=_bool(
            "DEEPSEEK_PROMPT_OPTIMIZER_ENABLED",
            bool(optimizer.get("enabled", True)),
        ),
        prompt_optimizer_api_key=(
            _str("DEEPSEEK_API_KEY") or optimizer.get("api_key", "")
        ),
        prompt_optimizer_base_url=(
            _str("DEEPSEEK_BASE_URL")
            or optimizer.get("base_url")
            or "https://api.deepseek.com"
        ),
        prompt_optimizer_model=(
            _str("DEEPSEEK_MODEL") or optimizer.get("model") or "deepseek-flash"
        ),
        budget_limit=_int("BUDGET_LIMIT", guard.get("budget_limit", 400)),
        dry_run=_bool("DRY_RUN", False),
        whiten_background=_bool(
            "WHITEN_BACKGROUND", bool(post.get("whiten_background", True))
        ),
        whiten_threshold=_int("WHITEN_THRESHOLD", int(post.get("threshold", 45))),
        auto_match_effect_background=bool(effect_auto.get("auto_match_background", True)),
        effect_params=dict(effect_auto.get("params") or {}),
        run_limit=int(gen.get("limit", 0) or 0),
        selected_store_indexes=[str(x) for x in (scope.get("store_indexes") or [])],
        image_mode=str(image_workflow.get("mode") or "text"),
        reference_assets=list(image_workflow.get("reference_assets") or []),
        quality_template=str(quality.get("template") or ""),
        optimized_quality_template=str(quality.get("optimized_template") or ""),
        use_optimized_quality_template=bool(quality.get("use_optimized", False)),
        realism_iteration=max(1, min(3, int(quality.get("realism_iteration", 1) or 1))),
        effect_background_asset=dict(effect_workflow.get("background_asset") or {}),
        # 印刷导出（settings.json 的 print 段）
        print_export_enabled=_bool("PRINT_EXPORT_ENABLED", bool(pe.get("enabled", True))),
        print_width_cm=_float("PRINT_WIDTH_CM", float(pe.get("width_cm", 60.0) or 60.0)),
        print_dpi=_int("PRINT_DPI", int(pe.get("dpi", 300) or 300)),
        print_bleed_mm=_float("PRINT_BLEED_MM", float(pe.get("bleed_mm", 3.0) or 3.0)),
        print_icc_path=str(pe.get("icc_path") or ""),
        print_white_ink=bool(pe.get("white_ink", True)),
        print_white_ink_invert=bool(pe.get("white_ink_invert", False)),
        print_dieline=bool(pe.get("dieline", True)),
        print_cutout=_cutout_mode(pe.get("cutout")),
        print_spot_channel=bool(pe.get("spot_channel", False)),
        print_max_pixels=max(
            2000, min(20000, int(pe.get("max_pixels", 8000) or 8000))
        ),
        print_keep_work=bool(pe.get("keep_work", True)),
        print_auto_clean_work=bool(pe.get("auto_clean_work", False)),
    )
