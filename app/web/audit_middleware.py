# -*- coding: utf-8 -*-
"""业务操作审计中间件（文档 §8）。

## 为什么用中间件而不是逐接口埋点

文档 §8 要求记录「模块、动作、目标对象、请求 ID、操作结果、错误分类」。
若在每个接口里手写 `audit.log(...)`：

- 46 个接口 × 每处 8 行 = 大量重复代码
- **容易漏**：新增接口时忘记加，就丢了审计
- 改字段要改 46 处

中间件自动覆盖**所有写操作**（POST / PUT / PATCH / DELETE），
只读请求默认不记（避免日志爆炸），可通过 `AUDITED_READS` 按需登记。

## 与本模块的关系

这里是**通用**审计（谁在什么时候调了什么接口、结果如何）。
业务语义更强的审计（如「停用了哪个账号」）由 `SecurityService` 负责，
两者通过 `request_id` 关联。
"""
from __future__ import annotations

import re
import time

from ..security.audit import (
    RESULT_ERROR,
    RESULT_SUCCESS,
    current_request_id,
)

__all__ = ["should_audit", "describe_action", "audit_module_of"]

# 只读但值得留痕的路径（如导出下载、审计查询本身）
AUDITED_READS: frozenset[str] = frozenset({
    "/api/admin/audit",          # 查审计这件事本身也要留痕（文档 §7.8）
    "/api/export/",              # 导出属高敏，即便 GET 也记
})

# 不需要审计的路径（噪声大且无安全价值）
_SKIP_PREFIXES: tuple[str, ...] = (
    "/static/",
    "/api/events",        # SSE 长连接，每次连上都记会淹没日志
    "/api/logs",
    "/api/auth/status",   # 前端每次启动都问，噪声大
)

_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# 路径 → (模块, 动作) 的语义映射；未命中则用通用描述
_SEMANTIC: tuple[tuple[str, str, str], ...] = (
    ("/api/run/new-batch", "batch", "create_new_batch"),
    ("/api/run", "batch", "resume_batch"),
    ("/api/stop", "batch", "stop_run"),
    ("/api/regenerate", "batch", "regenerate"),
    ("/api/openai/house-regenerate", "batch", "regenerate_openai"),
    ("/api/retry-failures", "batch", "retry_failures"),
    ("/api/history/restore", "batch", "restore_history"),
    ("/api/effects/generate", "effect", "generate_effects"),
    ("/api/effect-backgrounds/generate", "effect", "generate_backgrounds"),
    ("/api/effect-background-assets", "effect", "upload_background"),
    ("/api/effect-params/preview", "effect", "preview_params"),
    ("/api/effect-params", "effect", "save_params"),
    ("/api/settings/reset", "settings", "reset_settings"),
    ("/api/settings/test-image", "settings", "test_image"),
    ("/api/settings/test", "settings", "test_connection"),
    ("/api/settings/validate-path", "settings", "validate_path"),
    ("/api/settings/open-dir", "settings", "open_dir"),
    ("/api/settings", "settings", "save_settings"),
    ("/api/reference-assets", "reference", "upload_reference"),
    ("/api/prompt-profile/optimize", "prompt", "optimize_template"),
    ("/api/prompt-profile/confirm-run", "prompt", "confirm_and_run"),
    ("/api/export/", "export", "export"),
    ("/api/local-validation/run", "validation", "run_local_validation"),
    ("/api/professional-validation/run", "validation", "run_professional"),
    ("/api/admin/", "system", "admin"),
)

# 从 body 里提取「目标对象」的字段名
_TARGET_KEYS = ("batch", "batch_id", "store_index", "store_indexes", "id",
                "asset_id", "provider", "name", "login_name", "user_id")


def audit_module_of(path: str) -> str:
    """从路径推断模块名。"""
    p = (path or "").rstrip("/")
    parts = [x for x in p.split("/") if x]
    if len(parts) >= 2 and parts[0] == "api":
        return parts[1].split("-")[0][:24] or "api"
    return "api"


def describe_action(method: str, path: str) -> tuple[str, str]:
    """返回 ``(模块, 动作)``。"""
    p = (path or "").rstrip("/")
    verb = (method or "").upper()
    for prefix, module, action in _SEMANTIC:
        if p.startswith(prefix.rstrip("/")):
            # 对 /api/admin/* 这类泛化前缀，补上末段以区分具体动作
            if action == "admin":
                tail = p.split("/api/admin/")[-1].replace("/", "_")[:32]
                return "admin", f"{tail}_{verb.lower()}"
            return module, action
    tail = p.split("/")[-1].replace("/", "_")[:40] or "root"
    return audit_module_of(p), f"{tail}_{verb.lower()}"


def should_audit(method: str, path: str) -> bool:
    """是否需要审计这个请求。"""
    p = (path or "").rstrip("/") or "/"
    if any(p.startswith(prefix) for prefix in _SKIP_PREFIXES):
        return False
    if (method or "").upper() in _METHODS:
        return True
    return any(p.startswith(prefix) for prefix in AUDITED_READS)


def extract_target(body: object, path: str) -> tuple[str, str]:
    """从请求体里提取 ``(目标类型, 目标 ID)``，供审计展示。"""
    if not isinstance(body, dict):
        return "path", (path or "")[:128]
    for key in _TARGET_KEYS:
        if key in body:
            value = body[key]
            if isinstance(value, (list, tuple)):
                return key, ",".join(str(v) for v in value[:8])[:128]
            return key, str(value)[:128]
    return "path", (path or "")[:128]


def classify_error(status_code: int, detail: str = "") -> str:
    """把 HTTP 状态归类成可统计的错误类型（文档 §8「错误分类」）。"""
    d = (detail or "").lower()
    if status_code == 401:
        return "unauthenticated"
    if status_code == 403:
        return "forbidden"
    if status_code == 404:
        return "not_found"
    if status_code == 409:
        return "conflict"
    if status_code == 429 or "限流" in d or "rate" in d:
        return "rate_limited"
    if any(k in d for k in ("额度", "quota", "欠费", "arrearage")):
        return "quota"
    if any(k in d for k in ("超时", "timeout")):
        return "timeout"
    if any(k in d for k in ("拒绝", "refus", "content")):
        return "content_rejected"
    if status_code >= 500:
        return "server_error"
    if status_code >= 400:
        return "client_error"
    return ""


def summarize_body(body: object, limit: int = 20) -> dict:
    """把请求体摘要成可审计的字典（脱敏由 AuditLogger 负责）。"""
    if not isinstance(body, dict):
        return {}
    out: dict = {}
    for i, (k, v) in enumerate(body.items()):
        if i >= limit:
            out["…"] = f"还有 {len(body) - limit} 个字段"
            break
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)] = v
        elif isinstance(v, (list, tuple)):
            out[str(k)] = f"[{len(v)} 项]"
        elif isinstance(v, dict):
            out[str(k)] = f"{{{len(v)} 字段}}"
        else:
            out[str(k)] = type(v).__name__
    return out
