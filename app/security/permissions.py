# -*- coding: utf-8 -*-
"""权限编码与角色的**唯一定义源**（文档 §4）。

## 原则

> 「建立唯一的权限定义源，前端导航、后台角色配置、接口校验和测试从该来源生成或校验。
>   不允许前后端分别维护不一致的角色名称或模块清单。」

因此：

- **服务端**用 `has_permission()` 校验
- **前端**通过 `/api/auth/me` 拿到权限码列表，据此渲染导航
- **角色配置界面**从 `PERMISSIONS` 生成可勾选项
- **测试**遍历 `PERMISSIONS` 确保每个权限都有对应校验点

## 权限分层（文档 §4，本项目简化版）

原文档为「模块—操作—数据范围—字段」四层。因已确认**单组织、数据共享**，
数据范围层简化为「全部」，保留模块/操作层与字段层（客户数据脱敏）。
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Permission",
    "PERMISSIONS",
    "PERMISSION_CODES",
    "ROLE_TEMPLATES",
    "BUILTIN_ROLE_CODES",
    "get_permission",
    "is_valid_permission",
    "role_default_permissions",
]


@dataclass(frozen=True)
class Permission:
    code: str
    name: str
    group: str
    high_risk: bool = False     # 高危：变更需复核、必须审计（文档 §5/§7.2）
    description: str = ""


# ---------------------------------------------------------------- 权限清单
PERMISSIONS: tuple[Permission, ...] = (
    # ── 批次与生成 ──
    Permission("batch.read", "查看批次与图片", "批次与生成",
               description="浏览批次列表、生成图与 manifest"),
    Permission("batch.create", "创建批次（消耗额度）", "批次与生成",
               high_risk=True,
               description="新建批次会调用付费图像 API，产生真实费用"),
    Permission("batch.delete", "删除批次", "批次与生成",
               high_risk=True,
               description="删除不可恢复"),
    Permission("batch.export", "导出与打包下载", "批次与生成",
               description="导出 zip、标题 CSV 等"),

    # ── 效果图 ──
    Permission("effect.read", "查看效果图", "效果图"),
    Permission("effect.generate", "生成效果图", "效果图",
               description="把设计图合成到门店玻璃上"),
    Permission("effect.background.manage", "管理 AI 背景库", "效果图",
               high_risk=True,
               description="生成/删除 AI 门店背景，会消耗额度"),
    Permission("effect.params.manage", "调整效果图参数", "效果图"),

    # ── 配置 ──
    Permission("settings.model.manage", "管理模型与 API Key", "配置",
               high_risk=True,
               description="涉及密钥与付费账号"),
    Permission("settings.path.manage", "修改输出路径", "配置",
               high_risk=True,
               description="改错可能导致数据写到意外位置"),
    Permission("prompt.template.manage", "修改提示词模板", "配置",
               description="影响所有后续生成结果"),
    Permission("store.manage", "编辑门店数据", "配置",
               description="主标题、副标题、配色、拼多多标题等"),

    # ── 客户数据（字段层）──
    Permission("customer.contact.read", "查看客户完整联系方式", "客户数据",
               description="未授权时联系方式脱敏显示"),

    # ── 系统 ──
    Permission("system.user.manage", "用户管理", "系统", high_risk=True),
    Permission("system.role.manage", "角色与权限管理", "系统", high_risk=True),
    Permission("system.session.manage", "查看与强制下线会话", "系统"),
    Permission("system.audit.read", "查看审计日志", "系统"),
    Permission("system.backup.manage", "备份与恢复", "系统", high_risk=True),
    Permission("system.health.read", "查看系统健康", "系统"),
)

PERMISSION_CODES: frozenset[str] = frozenset(p.code for p in PERMISSIONS)
_BY_CODE = {p.code: p for p in PERMISSIONS}


# ---------------------------------------------------------------- 角色模板（文档 §5）
# 值 = 该角色默认拥有的权限码；"*" 表示全部
ROLE_TEMPLATES: dict[str, dict] = {
    "admin": {
        "name": "管理员",
        "description": "负责人：拥有全部权限，包括用户与密钥管理",
        "permissions": "*",
        "builtin": True,
    },
    "designer": {
        "name": "设计师",
        "description": "生成与调整设计：可用批次与效果图，不能删除批次或改密钥",
        "permissions": [
            "batch.read", "batch.create", "batch.export",
            "effect.read", "effect.generate", "effect.params.manage",
            "prompt.template.manage", "store.manage",
        ],
        "builtin": True,
    },
    "operator": {
        "name": "客服 / 运营",
        "description": "查看与导出：只看结果，不做配置变更",
        "permissions": [
            "batch.read", "batch.export",
            "effect.read", "effect.generate",
        ],
        "builtin": True,
    },
    "auditor": {
        "name": "安全审计员",
        "description": "只读审计与健康信息，不能修改任何业务数据（文档 §5 职责分离）",
        "permissions": [
            "batch.read", "effect.read",
            "system.audit.read", "system.health.read",
        ],
        "builtin": True,
    },
    "ops": {
        "name": "系统运维",
        "description": "维护服务与备份，不含业务审批与密钥管理",
        "permissions": [
            "batch.read", "effect.read",
            "system.health.read", "system.backup.manage",
            "system.session.manage",
        ],
        "builtin": True,
    },
}

BUILTIN_ROLE_CODES: frozenset[str] = frozenset(ROLE_TEMPLATES)


# ---------------------------------------------------------------- 工具
def get_permission(code: str) -> Permission | None:
    return _BY_CODE.get(code)


def is_valid_permission(code: str) -> bool:
    return code in PERMISSION_CODES


def role_default_permissions(role_code: str) -> list[str]:
    """返回角色的默认权限码列表（展开 "*"）。"""
    tpl = ROLE_TEMPLATES.get(role_code)
    if not tpl:
        return []
    perms = tpl.get("permissions")
    if perms == "*":
        return sorted(PERMISSION_CODES)
    return [c for c in perms if c in PERMISSION_CODES]
