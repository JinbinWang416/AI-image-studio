# -*- coding: utf-8 -*-
"""路径 → 权限映射（文档 §4 唯一权限定义源的延伸）。

## 为什么用集中映射而不是逐个改接口签名

本项目有 46 个 API。逐个加 `Depends(require_permission(...))` 有两个问题：

1. **容易漏**：新增接口时忘记加，就成了越权入口
2. **难以审计**：权限散落在各处，无法一眼看出「谁能访问什么」

集中映射后：

- **默认拒绝**：不在豁免清单里的 `/api/*` 一律**要求登录**（文档 §2.5）
- **一眼审计**：这张表就是完整的访问控制清单
- **测试友好**：可遍历规则表逐条验证

## 与依赖注入的关系

中间件负责**粗粒度**（是否登录 + 模块级权限）；
个别接口如需更细判断（如「只能删自己创建的批次」），仍可在函数内再校验。
"""
from __future__ import annotations

from ..security.permissions import is_valid_permission

__all__ = [
    "PATH_PERMISSION_RULES",
    "PUBLIC_PATHS",
    "PUBLIC_PREFIXES",
    "is_public",
    "required_permission_for",
]


# ================================================================ 豁免（无需登录）
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/",                      # 首页 HTML（内含登录界面）
    "/favicon.ico",
    "/api/auth/status",       # 前端启动时要问「是否需要初始化 / 是否已登录」
    "/api/auth/setup",        # 首次创建管理员（内部还会再查 needs_setup）
    "/api/auth/login",
    "/api/auth/logout",
})

PUBLIC_PREFIXES: tuple[str, ...] = ("/static/", "/docs", "/redoc", "/openapi.json")


# ================================================================ 规则表
# (HTTP 方法 或 "*", 路径前缀, 所需权限)
#   权限为 None 表示「只需登录，无需特定权限」
# 匹配规则：先按方法过滤，再取**最长前缀**
PATH_PERMISSION_RULES: tuple[tuple[str, str, str | None], ...] = (
    # ── 读：批次与状态 ──
    ("GET", "/api/state", "batch.read"),
    ("GET", "/api/logs", "batch.read"),
    ("GET", "/api/events", "batch.read"),
    ("GET", "/api/runs", "batch.read"),
    ("GET", "/api/history", "batch.read"),
    ("GET", "/api/failures", "batch.read"),
    ("GET", "/api/image", "batch.read"),
    ("GET", "/api/prompts/versions", "batch.read"),

    # ── 前端启动必需的只读信息（登录即可）──
    ("GET", "/api/config", None),
    ("GET", "/api/catalog", None),

    # ── 参考图与背景资产 ──
    ("GET", "/api/reference-assets", "batch.read"),
    ("POST", "/api/reference-assets", "batch.create"),
    ("GET", "/api/effect-background-assets", "effect.read"),
    ("POST", "/api/effect-background-assets", "effect.background.manage"),
    ("POST", "/api/effect-backgrounds/generate", "effect.background.manage"),

    # ── 效果图参数 ──
    ("GET", "/api/effect-params/preview-image", "effect.read"),
    ("GET", "/api/effect-params", "effect.read"),
    ("POST", "/api/effect-params/preview", "effect.params.manage"),
    ("POST", "/api/effect-params", "effect.params.manage"),

    # ── 生成与批次写操作（消耗付费额度）──
    ("POST", "/api/run/new-batch", "batch.create"),
    ("POST", "/api/run", "batch.create"),
    ("POST", "/api/stop", "batch.create"),
    ("POST", "/api/regenerate", "batch.create"),
    ("POST", "/api/openai/house-regenerate", "batch.create"),
    ("POST", "/api/retry-failures", "batch.create"),
    ("POST", "/api/history/restore", "batch.create"),
    ("POST", "/api/prompt-profile/confirm-run", "batch.create"),
    ("POST", "/api/prompt-profile/optimize", "prompt.template.manage"),
    ("POST", "/api/local-validation/run", "batch.create"),
    ("POST", "/api/professional-validation/run", "batch.create"),
    ("GET", "/api/local-validation", "batch.read"),
    ("GET", "/api/professional-validation", "batch.read"),
    ("POST", "/api/effects/generate", "effect.generate"),

    # ── 配置（读=登录即可，写=高危权限）──
    ("GET", "/api/settings", None),
    ("POST", "/api/settings/reset", "settings.model.manage"),
    ("POST", "/api/settings/test-image", "settings.model.manage"),
    ("POST", "/api/settings/test", "settings.model.manage"),
    ("POST", "/api/settings/validate-path", "settings.path.manage"),
    ("POST", "/api/settings/open-dir", "settings.path.manage"),
    # ⚠️ pick-dir / new-dir 会**选择或创建输出目录**，属于路径变更。
    #    它们原先不在表里，于是命中下面 `POST /api/settings` 的前缀规则，
    #    被误判成「管理模型与 API Key」—— 权限码用错了。
    ("POST", "/api/settings/pick-dir", "settings.path.manage"),
    ("POST", "/api/settings/new-dir", "settings.path.manage"),
    # ⚠️ `POST /api/settings` 这里**故意不挂权限码**：
    #    它会把整个 payload 深度合并进 settings.json，可改字段横跨
    #    「模型 / 路径 / 提示词 / 效果图」四组权限 —— 一刀切必然不是过严就是过松
    #    （挂 settings.model.manage 会让有「改模板」权限的设计师连提示词都改不了，
    #     同时让有该权限的人顺手改掉输出路径，使 settings.path.manage 形同虚设）。
    #    改为在 `api_save_settings()` 内按**字段**逐组校验，见
    #    server.py 的 `_SETTINGS_FIELD_PERMISSION`。
    ("POST", "/api/settings", None),

    # ── 导出 ──
    ("*", "/api/export/", "batch.export"),

    # ── 印刷 TIF 导出（沿用 batch.export，不新增权限码）──
    ("POST", "/api/print-export/", "batch.export"),
    ("GET", "/api/print-export/", "batch.export"),
)


def _norm(path: str) -> str:
    return (path or "").rstrip("/") or "/"


def is_public(path: str) -> bool:
    """是否无需登录即可访问。"""
    p = _norm(path)
    if p in PUBLIC_PATHS:
        return True
    return any(p.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def required_permission_for(method: str, path: str) -> tuple[bool, str | None]:
    """返回 ``(是否需要登录, 所需权限码)``。

    - ``(False, None)`` —— 公开接口
    - ``(True, None)``  —— 只需登录
    - ``(True, "xxx")`` —— 需要特定权限

    ⚠️ 未在规则表中登记的 `/api/*` 路径一律**要求登录**（文档 §2.5 默认拒绝）——
    这样新增接口时即使忘记登记，也不会成为匿名可访问的越权入口。
    """
    p = _norm(path)

    if is_public(p):
        return False, None

    if not p.startswith("/api/"):
        return False, None

    verb = (method or "GET").upper()

    best_len = -1
    best_perm: str | None = None
    for rule_method, prefix, perm in PATH_PERMISSION_RULES:
        if rule_method != "*" and rule_method != verb:
            continue
        norm_prefix = _norm(prefix)
        if p == norm_prefix or p.startswith(norm_prefix + "/"):
            if len(norm_prefix) > best_len:
                best_len = len(norm_prefix)
                best_perm = perm

    if best_len < 0:
        return True, None

    if best_perm is not None and not is_valid_permission(best_perm):
        # 规则表写了不存在的权限码 —— 属编码错误，按最严格处理（必然拒绝）
        return True, best_perm

    return True, best_perm
