# -*- coding: utf-8 -*-
"""
服务商与模型清单 —— **设置界面的数据源**。

数据来源：`docs/图片生成智能体_可行性与开发规划.md` 3.3 节
（2026-09-18 两路联网调研，全部一手官方来源）。

所有能力标注（价格 / 是否支持负向提示词 / 限流 / 免费额度）都来自调研结论，
用户在界面上选择时一眼可见，避免选错模型。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 模型特性说明
FEATURE_NOTES = {
    "negative": "支持负向提示词 negative_prompt",
    "no_negative": "**不支持**负向提示词，风格要求须写成正向肯定式",
    "rpm": "每分钟请求上限，官方明确「充值不提升」",
    "free": "免费额度（张），且按模型独立计算",
}


PROVIDER_CATALOG: dict[str, dict] = {
    # ============================================================ 阿里云百炼
    "qwen": {
        "label": "阿里云百炼",
        "vendor": "阿里云",
        "kind": "cloud",
        "recommended": True,
        "badge": "首选",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env_key": "DASHSCOPE_API_KEY",
        "key_hint": "百炼控制台 → API-KEY 管理",
        "console_url": "https://bailian.console.aliyun.com/",
        "default_model": "qwen-image-3.0",
        "summary": "唯一同时满足「中文量化证据 + 支持负向词 + n≤6 + OpenAI 兼容 + 20 次/分」的服务商",
        "models": [
            {
                "id": "qwen-image-3.0",
                "price": 0.18,
                "negative": True,
                "max_n": 6,
                "rpm": 20,
                "free_quota": 10,
                "tags": ["⭐ 推荐"],
                "note": "官方「10px 小字 + 12 国语言原生渲染」；ChineseWord 中文渲染 58.30（厂商自评）",
            },
            {
                "id": "qwen-image-2.0",
                "price": 0.20,
                "negative": True,
                "max_n": 6,
                "rpm": 120,
                "free_quota": 100,
                "tags": ["限流最宽松"],
                "note": "默认输出 2048×2048；120 次/分钟，138 张约 70 秒",
            },
            {
                "id": "qwen-image-3.0-pro",
                "price": 0.25,
                "negative": True,
                "max_n": 6,
                "rpm": 5,
                "free_quota": 10,
                "tags": ["⚠️ 慢"],
                "note": "2K 档 ¥0.50；5 次/分钟，138 张需 28 分钟以上",
            },
            {
                "id": "wanx2.0-t2i-turbo",
                "price": 0.04,
                "negative": True,
                "max_n": 1,
                "rpm": 0,
                "free_quota": 500,
                "tags": ["最便宜", "500 张免费"],
                "note": "全表最低价；有 500 张免费额度，适合零成本验证",
            },
            {
                "id": "wanx-v1",
                "price": 0.16,
                "negative": True,
                "max_n": 1,
                "rpm": 0,
                "free_quota": 500,
                "tags": ["免费 500 张"],
                "note": "旧接口，仅异步；支持负向词",
            },
            {
                "id": "wan2.6-t2i",
                "price": 0.20,
                "negative": True,
                "max_n": 1,
                "rpm": 0,
                "free_quota": 50,
                "tags": [],
                "note": "北京 RPS 1；⚠️ n 默认值为 4，不显式设 1 会多花 4 倍钱",
            },
            {
                "id": "wan2.7-image",
                "price": 0.20,
                "negative": False,
                "max_n": 4,
                "rpm": 0,
                "free_quota": 0,
                "tags": ["⚠️ 无负向词"],
                "note": "新一代反而砍掉了 negative_prompt（反直觉）；⚠️ 组图模式 n 默认 12",
            },
        ],
    },

    # ============================================================ 快手可灵
    "kling": {
        "label": "快手可灵",
        "vendor": "快手",
        "kind": "cloud",
        "recommended": False,
        "badge": "吞吐最优",
        "base_url": "https://api-beijing.klingai.com",
        "env_key": "KLING_API_KEY",
        "key_hint": "可灵开放平台 → 密钥管理",
        "console_url": "https://klingai.com/",
        "default_model": "kling-v3",
        "summary": "官方「不设 QPS 限制」，图片任务并发 = n 值，套餐并发 9 → 138 张约 16 轮发完",
        "models": [
            {
                "id": "kling-v3",
                "price": 0.20,
                "negative": True,
                "max_n": 9,
                "rpm": 0,
                "free_quota": 0,
                "tags": ["⚡ 批量最快"],
                "note": "无 QPS 限制；并发 9；负向词 ≤2500 字符；原生 1:1",
            },
            {
                "id": "kling-v3-omni",
                "price": 0.20,
                "negative": True,
                "max_n": 9,
                "rpm": 0,
                "free_quota": 0,
                "tags": [],
                "note": "4K 档 ¥0.40",
            },
            {
                "id": "kling-image-o1",
                "price": 0.20,
                "negative": True,
                "max_n": 9,
                "rpm": 0,
                "free_quota": 0,
                "tags": [],
                "note": "",
            },
            {
                "id": "kling-v2-1",
                "price": 0.10,
                "negative": True,
                "max_n": 9,
                "rpm": 0,
                "free_quota": 0,
                "tags": ["便宜"],
                "note": "上一代，价格更低",
            },
        ],
        "warnings": [
            "字段名是 `model_name` 不是 `model` —— 写错会**静默降级到 V1 模型**",
            "图生图场景（image 字段非空）**不支持**负向提示词",
            "并发超限报错 `code 1303`，建议指数退避",
        ],
    },

    # ============================================================ 智谱
    "zhipu": {
        "label": "智谱 GLM",
        "vendor": "智谱 AI",
        "kind": "cloud",
        "recommended": False,
        "badge": "单价低",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env_key": "ZHIPU_API_KEY",
        "key_hint": "智谱开放平台 → API Keys",
        "console_url": "https://open.bigmodel.cn/",
        "default_model": "glm-image",
        "summary": "中文文字证据最量化（LongText-Bench 中文 0.9788），单价最低；代价是无负向词、单次只出一张",
        "models": [
            {
                "id": "glm-image",
                "price": 0.10,
                "negative": False,
                "max_n": 1,
                "rpm": 0,
                "free_quota": 0,
                "tags": ["中文榜第一"],
                "note": "LongText-Bench 中文 0.9788 / CVTG-2K WordAcc 0.9116；架构含 Glyph Encoder",
            },
            {
                "id": "cogview-4-250304",
                "price": 0.06,
                "negative": False,
                "max_n": 1,
                "rpm": 15,
                "free_quota": 0,
                "tags": ["便宜"],
                "note": "批量接口 ¥0.03/张；首个能生成汉字的开源模型",
            },
            {
                "id": "cogview-3-flash",
                "price": 0.0,
                "negative": False,
                "max_n": 1,
                "rpm": 15,
                "free_quota": 0,
                "tags": ["永久免费"],
                "note": "无文字渲染官方声明，仅适合流程验证",
            },
        ],
        "warnings": ["无 `negative_prompt` 参数；单次只返回一张图"],
    },

    # ============================================================ OpenAI GPT Image
    "openai": {
        "label": "OpenAI GPT Image",
        "vendor": "OpenAI",
        "kind": "cloud",
        "recommended": True,
        "badge": "质量优先",
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "key_hint": "OpenAI API 平台 → API Keys（不是 ChatGPT 网页登录）",
        "console_url": "https://platform.openai.com/api-keys",
        "default_model": "gpt-image-2.5-flare",
        "summary": "用于“房屋中介 6 张新图”：每次运行使用新变体提示词，按官方 Images API 返回 PNG。",
        "models": [
            {
                "id": "gpt-image-2.5-flare",
                "price": 0.0,
                "negative": False,
                "max_n": 10,
                "rpm": 0,
                "free_quota": 0,
                "tags": ["质量优先", "max"],
                "note": "固定 1024×1024、质量 max、PNG、不透明白底；实际费用以 OpenAI API 平台账单为准。",
            },
        ],
        "warnings": [
            "需使用具备图像模型权限的 OpenAI 平台 API Key。",
            "专用按钮一次生成门店 01 的 6 张图；每次会归档旧图并产生新版本。",
        ],
    },

    # ============================================================ Google Gemini
    "gemini": {
        "label": "Google Gemini",
        "vendor": "Google AI",
        "kind": "cloud",
        "recommended": False,
        "badge": "多参考图",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "env_key": "GEMINI_API_KEY",
        "key_hint": "Google AI Studio → API Keys",
        "console_url": "https://aistudio.google.com/app/apikey",
        "default_model": "gemini-3.1-flash-image",
        "summary": "原生支持文生图、图生图和多图参考；通过 Gemini Interactions API 输出 PNG，适合需要中文文字与成套视觉参考的设计任务。",
        "models": [
            {
                "id": "gemini-3.1-flash-image",
                "price": 0.0,
                "price_unknown": True,
                "negative": False,
                "max_n": 1,
                "rpm": 0,
                "free_quota": 0,
                "tags": ["推荐", "速度与质量平衡"],
                "note": "原生图像生成与编辑；支持多参考图和 1K PNG。实际价格、地区可用性以 Google AI Studio 为准。",
            },
            {
                "id": "gemini-3-pro-image",
                "price": 0.0,
                "price_unknown": True,
                "negative": False,
                "max_n": 1,
                "rpm": 0,
                "free_quota": 0,
                "tags": ["专业质量"],
                "note": "适合更复杂的专业资产生成与编辑；实际价格、地区可用性以 Google AI Studio 为准。",
            },
        ],
        "warnings": [
            "不支持独立 negative_prompt，限制条件会转写为正向质量模板。",
            "生成图片包含 SynthID 标识；请确认目标使用场景接受该标识。",
        ],
    },

    # ============================================================ 字节 Seedream
    "seedream": {
        "label": "字节 Seedream",
        "vendor": "火山方舟 / 字节跳动",
        "kind": "cloud",
        "recommended": False,
        "badge": "中文商业设计",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "env_key": "ARK_API_KEY",
        "key_hint": "火山方舟 → API Key 管理",
        "console_url": "https://console.volcengine.com/ark",
        "default_model": "doubao-seedream-4-0-250828",
        "summary": "火山方舟 Seedream：支持文生图、单参考图和多参考图生成。项目会以 PNG 落盘，并将设置的画幅比例写入生成请求。",
        "models": [
            {
                "id": "doubao-seedream-4-0-250828",
                "price": 0.0,
                "price_unknown": True,
                "negative": False,
                "max_n": 1,
                "rpm": 0,
                "free_quota": 0,
                "tags": ["1K", "参考图"],
                "note": "支持文生图、图生图和多参考图；费用及可用额度以火山方舟控制台为准。",
            },
            {
                "id": "doubao-seedream-4-5-251128",
                "price": 0.0,
                "price_unknown": True,
                "negative": False,
                "max_n": 1,
                "rpm": 0,
                "free_quota": 0,
                "tags": ["2K", "质量优先"],
                "note": "使用 2K 质量档，适合更精细的成品验证；费用及可用额度以火山方舟控制台为准。",
            },
        ],
        "warnings": [
            "不支持独立 negative_prompt，限制条件会转写为正向质量模板。",
            "比例会作为 Seedream 的构图约束；实际像素由所选 1K/2K 模型档位决定。",
        ],
    },

    # ============================================================ 自定义
    "custom": {
        "label": "自定义 OpenAI 兼容",
        "vendor": "自填",
        "kind": "custom",
        "recommended": False,
        "badge": "",
        "base_url": "",
        "env_key": "CUSTOM_API_KEY",
        "key_hint": "填写任意 OpenAI 兼容端点",
        "console_url": "",
        "default_model": "",
        "summary": "适配未来任何 OpenAI 兼容端点（如硅基流动、PPIO、本地 vLLM / ComfyUI 代理）",
        "models": [],
        "warnings": ["模型名与 Base URL 需自行确认；部分平台的图片接口并不兼容 OpenAI 协议"],
    },

    # ============================================================ 本地模拟
    "mock": {
        "label": "本地模拟",
        "vendor": "内置",
        "kind": "local",
        "recommended": False,
        "badge": "无需 Key",
        "base_url": "",
        "env_key": "",
        "key_hint": "",
        "console_url": "",
        "default_model": "mock-v1",
        "summary": "离线生成占位图，用于跑通全流程（建目录、并发、重试、断点续跑、Web 进度）",
        "models": [
            {
                "id": "mock-v1",
                "price": 0.0,
                "negative": True,
                "max_n": 6,
                "rpm": 0,
                "free_quota": 0,
                "tags": ["免费"],
                "note": "生成结构化的占位贴纸图，不产生真实设计稿",
            },
        ],
        "warnings": [],
    },

    # ============================================================ FLUX 本地样图验证
    "flux_local": {
        "label": "FLUX.2 klein 4B（本地验证）",
        "vendor": "Black Forest Labs / 本机",
        "kind": "local_flux",
        "recommended": False,
        "badge": "仅 6 张验证",
        "base_url": "http://127.0.0.1:8189",
        "env_key": "",
        "key_hint": "无需 API Key；请先运行 tools/start_flux_local.ps1",
        "console_url": "https://github.com/black-forest-labs/flux2",
        "default_model": "FLUX.2-klein-4b",
        "summary": "仅用于门店 01 的 V8 六张离线样图。服务只监听本机 127.0.0.1:8189，不会请求云端。",
        "models": [
            {
                "id": "FLUX.2-klein-4b",
                "price": 0.0,
                "negative": False,
                "max_n": 1,
                "rpm": 0,
                "free_quota": 0,
                "tags": ["离线", "8GB 显存验证"],
                "note": "4 步蒸馏、CPU 卸载。无负向提示词、无图生图、无远程回退。",
            },
        ],
        "warnings": [
            "只能从“运行本地样图验证”执行，固定门店 01、V8、6 张、单并发。",
            "请先下载权重并启动本地服务；未启动时连接检测会给出具体处理方法。",
        ],
    },
}


# ---------------------------------------------------------------- 便捷函数
def get_provider(name: str) -> dict | None:
    return PROVIDER_CATALOG.get(name)


def get_model(provider: str, model_id: str) -> dict | None:
    p = PROVIDER_CATALOG.get(provider)
    if not p:
        return None
    for m in p.get("models", []):
        if m["id"] == model_id:
            return m
    return None


def default_settings() -> dict:
    """由清单生成默认设置结构。"""
    providers: dict[str, dict] = {}
    for name, p in PROVIDER_CATALOG.items():
        providers[name] = {
            "api_key": "",
            "base_url": p.get("base_url", ""),
            "model": p.get("default_model", ""),
        }
    from ..prompt_profiles import DEFAULT_QUALITY_TEMPLATE

    return {
        "version": 1,
        "active_provider": "mock",
        "providers": providers,
        "generation": {
            "limit": 0,
            "concurrency": 4,
            "retry_max": 3,
            "retry_backoff": [2, 5, 15],
            "timeout": 120,
            "size": "1024x1024",
        },
        "scope": {"store_indexes": [f"{index:02d}" for index in range(1, 24)]},
        "image_workflow": {"mode": "text", "reference_assets": []},
        "prompt_quality": {
            "template": DEFAULT_QUALITY_TEMPLATE,
            "optimized_template": "",
            "use_optimized": False,
            "realism_iteration": 1,
        },
        "effect_workflow": {"background_asset": {}},
        # 效果图背景自动匹配：未明确选择背景时，按门店名自动套用同行业的 AI 生成背景
        "effect": {"auto_match_background": True},
        "output": {
            "root": "",
            "timestamp_prefix": False,
            "overwrite": False,
            "on_path_change": "keep",
            "active_batch": "",
            "active_batch_created_at": "",
        },
        "qc": {"enabled": True, "min_similarity": 0.80},
        "guard": {"budget_limit": 400},
        "prompt": {"version": "", "text_wrapper": "【】", "normalize_negative": False, "use_simple": False},
        "prompt_optimizer": {
            "enabled": True,
            "api_key": "",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-flash",
        },
        "postprocess": {"whiten_background": True, "threshold": 45},
        # 印刷 TIF 导出（app/print_export/）
        #   放在默认值里，`GET /api/settings` 才会返回完整字段 ——
        #   否则前端首次打开时拿到的是空对象，表单显示不出当前值。
        "print": {
            "enabled": True,          # 批次完成后自动导出
            "width_cm": 60.0,         # 贴纸实际宽度
            "dpi": 300,               # 目标印刷 DPI
            "bleed_mm": 3.0,          # 刀模出血
            "icc_path": "",           # CMYK ICC；留空则找 config/icc/ 或系统默认
            "white_ink": True,        # 白墨层（玻璃静电贴必做）
            "white_ink_invert": False,  # 白墨极性：False=不透明区印白墨（印刷惯例）
            "dieline": True,          # 刀模线层
            "cutout": "auto",         # 去背方式：auto | rembg | fallback
            "spot_channel": False,    # 专色通道（仅留开关）
            "max_pixels": 8000,       # 单边像素上限（防内存爆）
            "keep_work": True,        # 保留 _work/ 母版
            "auto_clean_work": False,  # 印刷确认后自动清理（需手动触发）
        },
    }
