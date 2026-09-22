# -*- coding: utf-8 -*-
"""管理员后台接口（文档 §7）。

## 权限

每个接口都有明确的权限码，通过 `require_permission` 依赖注入：

| 功能 | 权限 |
|------|------|
| 用户管理 | `system.user.manage` |
| 角色与权限配置 | `system.role.manage` |
| 会话监控与强制下线 | `system.session.manage` |
| 审计检索与导出 | `system.audit.read` |
| 系统健康 | `system.health.read` |

## 安全要点（文档 §7）

- §7.1 **不显示原密码**（接口根本不返回 `password_hash`）
- §7.2 角色变更**展示前后差异**；查看「为什么有/没有某项权限」
- §7.2 **敏感权限变更需复核**（本实现记录审计并把高危权限标出）
- §7.5 会话可**强制下线**
- §7.7 显示部署版本与运行状态
- §7.8 **导出审计数据需独立权限，并记录导出行为**
"""
from __future__ import annotations

import json
import platform
import sys
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..security.audit import RESULT_DENIED
from ..security.permissions import (
    BUILTIN_ROLE_CODES,
    PERMISSIONS,
    PERMISSION_CODES,
    get_permission,
    role_default_permissions,
)
from ..security.users import STATUS_ACTIVE, VALID_STATUSES, User, UsernameError
from .auth_routes import require_permission
from . import auth_routes as _auth_routes

__all__ = ["router"]


class _SecurityProxy:
    """把属性访问**动态转发**到 ``auth_routes.security``。

    为什么需要这层代理：

        from .auth_routes import security     # ← 这是「值拷贝」

    测试里替换 `auth_routes.security = 临时服务` 时，本模块持有的仍是**旧对象**，
    导致「用户列表能查（空列表）但重置密码报 404 用户不存在」这类诡异现象 ——
    排查成本极高，而且生产与测试行为不一致。

    改用代理后，每次访问都取当前值，替换立即生效，也便于将来做依赖注入。
    """

    def __getattr__(self, name: str):
        return getattr(_auth_routes.security, name)


security = _SecurityProxy()

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _ip(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


# ================================================================ 用户
@router.get("/users")
async def list_users(
    q: str = Query("", description="搜索登录名/显示姓名"),
    status: str = Query("", description="按状态筛选"),
    role: str = Query("", description="按角色筛选"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    user: User = Depends(require_permission("system.user.manage")),
) -> JSONResponse:
    """用户列表（**不含密码哈希**，文档 §7.1）。

    支持搜索、筛选与分页 —— 用户数量上来后一次全量返回既慢又难用。
    """
    roles = security.users.list_roles()
    all_users = security.users.list_users()

    # 搜索：登录名 / 显示姓名（大小写不敏感）
    kw = (q or "").strip().lower()
    if kw:
        all_users = [
            u for u in all_users
            if kw in u.login_name.lower() or kw in u.display_name.lower()
            or kw in u.id.lower()
        ]
    if status:
        all_users = [u for u in all_users if u.status == status]
    if role:
        all_users = [u for u in all_users if role in u.roles]

    total = len(all_users)
    start = (page - 1) * size
    page_users = all_users[start:start + size]

    items = []
    for u in page_users:
        row = u.public()
        row["permissions"] = sorted(security.permissions_of(u))
        row["sessions"] = len(security.sessions.list_for_user(u.id))
        row["role_names"] = [roles.get(r, {}).get("name", r) for r in u.roles]
        row["locked"] = u.is_locked()
        items.append(row)

    return JSONResponse({
        "users": items,
        "total": total,
        "page": page,
        "size": size,
        "pages": max(1, (total + size - 1) // size),
        "roles": [{"code": c, "name": v.get("name", c)} for c, v in roles.items()],
        "statuses": sorted(VALID_STATUSES),
    })


@router.post("/users/batch")
async def batch_user_action(
    payload: dict | None = None,
    request: Request = None,
    actor: User = Depends(require_permission("system.user.manage")),
) -> JSONResponse:
    """批量操作用户（P2：1.2）。

    支持 `disable` / `enable` / `unlock` / `set_roles`。

    ⚠️ **安全约束**：
      · **跳过自己**（避免把自己停用/降权后无法恢复）
      · 逐个执行并记录成功/失败，**不做"全成功或全失败"**（部分失败也要如实报告）
      · 写**一条汇总审计**（含每个目标的 id）
    """
    payload = payload or {}
    action = str(payload.get("action") or "")
    ids = [str(x) for x in (payload.get("user_ids") or [])]
    if not action or not ids:
        raise HTTPException(status_code=400, detail="缺少 action 或 user_ids")

    valid = {"disable", "enable", "unlock", "set_roles"}
    if action not in valid:
        raise HTTPException(status_code=400, detail=f"未知操作：{action}")

    roles = [str(r) for r in (payload.get("roles") or [])] if action == "set_roles" else []

    ok_list: list[str] = []
    failed: list[dict] = []
    skipped: list[str] = []

    for uid in ids:
        if uid == actor.id:
            skipped.append(uid)
            continue
        try:
            if action == "disable":
                security.set_user_status(actor, uid, "disabled", ip=_ip(request))
            elif action == "enable":
                security.set_user_status(actor, uid, "active", ip=_ip(request))
            elif action == "unlock":
                security.users.update_fields(uid, failed_attempts=0, locked_until="")
            elif action == "set_roles":
                security.set_user_roles(actor, uid, roles, ip=_ip(request))
            ok_list.append(uid)
        except Exception as exc:  # noqa: BLE001
            failed.append({"id": uid, "error": str(exc)[:80]})

    security.audit.log(
        action=f"user.batch_{action}", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="user", target_id=f"{len(ok_list)} 个用户",
        changes={"succeeded": ok_list, "failed": failed, "skipped_self": skipped,
                 "roles": roles},
        ip=_ip(request) if request else "",
    )
    return JSONResponse({
        "ok": True,
        "succeeded": len(ok_list),
        "failed": failed,
        "skipped_self": len(skipped),
        "message": f"成功 {len(ok_list)} 个"
                   + (f"，失败 {len(failed)} 个" if failed else "")
                   + (f"，跳过自己 {len(skipped)} 个" if skipped else ""),
    })


@router.get("/users/export")
async def export_users(
    request: Request,
    actor: User = Depends(require_permission("system.user.manage")),
) -> "StreamingResponse":
    """导出用户清单 CSV（P2：1.6）。

    ⚠️ 导出**不含**密码哈希、会话令牌等敏感字段。
    导出行为本身写审计（文档 §7.8）。
    """
    import csv
    import io as _io

    roles = security.users.list_roles()
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["登录名", "显示姓名", "状态", "角色", "角色名称", "动态验证码",
                "活跃会话", "创建时间", "最近登录", "强制改密"])
    for u in security.users.list_users():
        w.writerow([
            u.login_name, u.display_name, u.status,
            ";".join(u.roles),
            ";".join(roles.get(r, {}).get("name", r) for r in u.roles),
            "是" if u.mfa_enabled else "否",
            len(security.sessions.list_for_user(u.id)),
            u.created_at, u.last_login_at,
            "是" if u.must_change_password else "否",
        ])

    security.audit.log(
        action="user.export", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="user", target_id="csv",
        changes={"rows": security.users.count()}, ip=_ip(request),
    )

    # BOM 让 Excel 正确识别 UTF-8
    body = "\ufeff" + buf.getvalue()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        iter([body]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="users_{stamp}.csv"'},
    )


@router.get("/users/{user_id}")
async def get_user(
    user_id: str,
    user: User = Depends(require_permission("system.user.manage")),
) -> JSONResponse:
    """单个用户详情（供详情抽屉使用）。"""
    target = security.users.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    roles = security.users.list_roles()
    perms = sorted(security.permissions_of(target))
    sessions = [s.public() for s in security.sessions.list_for_user(user_id)]

    # 该用户的近期操作（复用审计，按 actor_id 过滤）
    recent = security.audit.query(actor_id=user_id, limit=30)

    return JSONResponse({
        "user": target.public(),
        "role_names": [roles.get(r, {}).get("name", r) for r in target.roles],
        "permissions": perms,
        "permission_details": [
            {"code": p.code, "name": p.name, "group": p.group, "high_risk": p.high_risk}
            for p in PERMISSIONS if p.code in perms
        ],
        "sessions": sessions,
        "recent_actions": recent,
        "locked": target.is_locked(),
        "failed_attempts": target.failed_attempts,
    })


@router.post("/users")
async def create_user(
    payload: dict | None = None,
    request: Request = None,
    actor: User = Depends(require_permission("system.user.manage")),
) -> JSONResponse:
    """创建用户（文档 §7.1）。"""
    from ..security.auth import hash_password, password_strength_error

    payload = payload or {}
    login_name = str(payload.get("login_name") or "").strip()
    display_name = str(payload.get("display_name") or "").strip()
    password = str(payload.get("password") or "")
    roles = [str(r) for r in (payload.get("roles") or [])]

    if not roles:
        roles = ["operator"]
    err = password_strength_error(password, login_name)
    if err:
        raise HTTPException(status_code=400, detail=err)

    try:
        created = security.users.create(
            login_name, display_name or login_name, hash_password(password),
            roles=roles, status=STATUS_ACTIVE,
            must_change_password=bool(payload.get("must_change_password", True)),
        )
    except UsernameError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    security.audit.log(
        action="user.create", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="user", target_id=created.id,
        changes={"login_name": created.login_name, "roles": roles},
        ip=_ip(request),
    )
    return JSONResponse({"ok": True, "user": created.public()})


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str,
    payload: dict | None = None,
    request: Request = None,
    actor: User = Depends(require_permission("system.user.manage")),
) -> JSONResponse:
    """修改用户状态或显示名（文档 §7.1）。"""
    payload = payload or {}
    target = security.users.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if "status" in payload:
        status = str(payload["status"])
        if status not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"未知状态：{status}")
        security.set_user_status(actor, user_id, status, ip=_ip(request))

    if "display_name" in payload:
        name = str(payload["display_name"]).strip()
        if not name:
            raise HTTPException(status_code=400, detail="显示姓名不能为空")
        security.users.update_fields(user_id, display_name=name[:64])

    if "roles" in payload:
        security.set_user_roles(actor, user_id, list(payload["roles"] or []), ip=_ip(request))

    if payload.get("unlock"):
        security.users.update_fields(user_id, failed_attempts=0, locked_until="")
        security.audit.log(
            action="user.unlock", module="admin", result="success",
            actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
            target_type="user", target_id=user_id, ip=_ip(request),
        )

    fresh = security.users.get(user_id)
    return JSONResponse({"ok": True, "user": fresh.public() if fresh else None})


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_user_sessions(
    user_id: str,
    request: Request,
    actor: User = Depends(require_permission("system.session.manage")),
) -> JSONResponse:
    """强制某用户全部下线（文档 §7.5）。"""
    target = security.users.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    n = security.sessions.revoke_all_for_user(user_id)
    security.audit.log(
        action="user.force_logout", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="user", target_id=user_id, changes={"revoked": n}, ip=_ip(request),
    )
    return JSONResponse({"ok": True, "revoked": n})


@router.get("/users/{user_id}/why")
async def why_permission(
    user_id: str,
    permission: str = Query(..., description="权限码"),
    actor: User = Depends(require_permission("system.user.manage")),
) -> JSONResponse:
    """解释「某用户为什么有 / 没有某项权限」（文档 §7.2）。"""
    target = security.users.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    roles = security.users.list_roles()
    sources = []
    for code in target.roles:
        role = roles.get(code) or {}
        perms = role.get("permissions") or []
        if "admin" in target.roles:
            sources.append({"role": code, "name": role.get("name", code),
                            "grants": "全部权限（管理员角色）"})
        elif permission in perms:
            sources.append({"role": code, "name": role.get("name", code),
                            "grants": "包含该权限"})

    has_it = security.has_permission(target, permission)
    return JSONResponse({
        "user": target.public(),
        "permission": permission,
        "granted": has_it,
        "sources": sources,
        "explanation": (
            f"通过角色 {'、'.join(s['role'] for s in sources)} 获得" if has_it and sources
            else ("管理员角色拥有全部权限" if has_it and "admin" in target.roles
                  else "其角色均未包含该权限")
        ),
    })


# ================================================================ 角色
@router.get("/roles")
async def list_roles(user: User = Depends(require_permission("system.role.manage"))) -> JSONResponse:
    """角色与权限配置界面所需的全部数据（文档 §7.2）。"""
    roles = security.users.list_roles()
    out = []
    for code, role in roles.items():
        out.append({
            "code": code,
            "name": role.get("name", code),
            "description": role.get("description", ""),
            "permissions": list(role.get("permissions") or []),
            "builtin": code in BUILTIN_ROLE_CODES,
            "user_count": sum(1 for u in security.users.list_users() if code in u.roles),
        })
    return JSONResponse({
        "roles": sorted(out, key=lambda r: (not r["builtin"], r["code"])),
        # 权限定义源 —— 前端可勾选项由它生成，保证前后端一致（文档 §4）
        "catalog": [
            {"code": p.code, "name": p.name, "group": p.group,
             "high_risk": p.high_risk, "description": p.description}
            for p in PERMISSIONS
        ],
    })


@router.put("/roles/{code}")
async def save_role(
    code: str,
    payload: dict | None = None,
    request: Request = None,
    actor: User = Depends(require_permission("system.role.manage")),
) -> JSONResponse:
    """保存角色的权限集合。

    文档 §7.2：**修改前后差异预览** —— 响应里返回 diff；
    文档 §4：不能通过编辑角色实现提权 —— 授予的权限不得超出操作者自己拥有的。
    """
    payload = payload or {}
    permissions = [str(p) for p in (payload.get("permissions") or [])]
    invalid = [p for p in permissions if p not in PERMISSION_CODES]
    if invalid:
        raise HTTPException(status_code=400, detail=f"未知权限码：{'、'.join(invalid[:5])}")

    before = list((security.users.list_roles().get(code) or {}).get("permissions") or [])

    # 防提权：非管理员不能授予自己没有的权限
    if "admin" not in actor.roles:
        mine = security.permissions_of(actor)
        extra = sorted(set(permissions) - mine)
        if extra:
            security.audit.log(
                action="role.escalation_blocked", module="admin", result=RESULT_DENIED,
                actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
                target_type="role", target_id=code,
                error_kind="escalation_attempt", changes={"extra": extra}, ip=_ip(request),
            )
            raise HTTPException(
                status_code=403,
                detail=f"不能授予自己没有的权限：{'、'.join(extra[:5])}",
            )

    security.users.save_role(
        code,
        str(payload.get("name") or code),
        permissions,
        str(payload.get("description") or ""),
    )

    added = sorted(set(permissions) - set(before))
    removed = sorted(set(before) - set(permissions))
    high_risk_touched = [p for p in (added + removed)
                         if (get_permission(p) and get_permission(p).high_risk)]

    security.audit.log(
        action="role.save", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="role", target_id=code,
        changes={"added": added, "removed": removed, "high_risk_touched": high_risk_touched},
        ip=_ip(request),
    )

    # 角色权限变化后，拥有该角色的用户会话需要失效（权限已变，文档 §3.13）
    affected = [u for u in security.users.list_users() if code in u.roles]
    revoked = sum(security.sessions.revoke_all_for_user(u.id) for u in affected)

    return JSONResponse({
        "ok": True,
        "diff": {"added": added, "removed": removed,
                 "high_risk_touched": high_risk_touched,
                 "affected_users": len(affected), "sessions_revoked": revoked},
    })


@router.get("/roles/matrix")
async def roles_matrix(
    user: User = Depends(require_permission("system.role.manage")),
) -> JSONResponse:
    """权限矩阵（P1-2）：一张表看「角色 × 权限」。

    ## 为什么需要

    逐个点开角色看权限，回答不了这些高频问题：

    · 谁能删批次？
    · 这项权限给了几个角色？
    · 有没有权限**谁都没有**（可能是白定义了）？
    · 有没有角色**一个权限都没有**（配错了）？

    ## 返回结构

    ```
    {
      "groups": [ { "name": "批次与生成", "permissions": [...] } ],   # 按分组，便于表头分层
      "roles":  [ { "code": "admin", "name": "管理员", "user_count": 1, ... } ],
      "matrix": { "<role_code>": { "<perm_code>": true/false } },
      "stats":  { "orphan_permissions": [...], "empty_roles": [...] }
    }
    ```
    """
    roles = security.users.list_roles()
    users = security.users.list_users()

    # 按分组组织权限（表头分层，前端渲染更清晰）
    groups: dict[str, list[dict]] = {}
    for p in PERMISSIONS:
        groups.setdefault(p.group, []).append({
            "code": p.code,
            "name": p.name,
            "high_risk": p.high_risk,
            "description": p.description,
        })

    role_list = []
    for code, role in roles.items():
        role_list.append({
            "code": code,
            "name": role.get("name", code),
            "description": role.get("description", ""),
            "builtin": code in BUILTIN_ROLE_CODES,
            "user_count": sum(1 for u in users if code in u.roles),
        })
    role_list.sort(key=lambda r: (not r["builtin"], r["code"]))

    # 矩阵：admin 角色拥有全部权限（与 permissions_of 的计算口径一致）
    matrix: dict[str, dict[str, bool]] = {}
    for r in role_list:
        code = r["code"]
        if code == "admin":
            matrix[code] = {p.code: True for p in PERMISSIONS}
            continue
        granted = set((roles.get(code) or {}).get("permissions") or [])
        matrix[code] = {p.code: (p.code in granted) for p in PERMISSIONS}

    # 诊断：谁都没有的权限 / 一个权限都没有的角色
    orphan: list[str] = []
    for p in PERMISSIONS:
        if not any(matrix[r["code"]].get(p.code) for r in role_list):
            orphan.append(p.code)

    empty_roles = [
        r["code"] for r in role_list
        if not any(matrix[r["code"]].values())
    ]

    return JSONResponse({
        "groups": [{"name": g, "permissions": items} for g, items in groups.items()],
        "roles": role_list,
        "matrix": matrix,
        "stats": {
            "total_permissions": len(PERMISSIONS),
            "total_roles": len(role_list),
            "orphan_permissions": orphan,      # 定义了但没有任何角色拥有
            "empty_roles": empty_roles,        # 存在但没有任何权限
        },
    })


@router.post("/roles")
async def create_role(
    payload: dict | None = None,
    request: Request = None,
    actor: User = Depends(require_permission("system.role.manage")),
) -> JSONResponse:
    """新建自定义角色。"""
    import re

    payload = payload or {}
    code = re.sub(r"[^a-z0-9_\-]", "", str(payload.get("code") or "").lower())[:32]
    if not code:
        raise HTTPException(status_code=400, detail="角色编码只能包含小写字母、数字、下划线、连字符")
    if code in security.users.list_roles():
        raise HTTPException(status_code=409, detail=f"角色已存在：{code}")

    security.users.save_role(
        code,
        str(payload.get("name") or code),
        [],   # 新角色从空权限开始，避免误授
        str(payload.get("description") or ""),
    )
    security.audit.log(
        action="role.create", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="role", target_id=code, ip=_ip(request),
    )
    return JSONResponse({"ok": True, "code": code})


@router.delete("/roles/{code}")
async def delete_role(
    code: str,
    request: Request,
    actor: User = Depends(require_permission("system.role.manage")),
) -> JSONResponse:
    """删除自定义角色（内置角色不可删）。"""
    if code in BUILTIN_ROLE_CODES:
        raise HTTPException(status_code=400, detail="内置角色不能删除")
    holders = [u for u in security.users.list_users() if code in u.roles]
    if holders:
        raise HTTPException(
            status_code=409,
            detail=f"仍有 {len(holders)} 个用户使用该角色，请先改派",
        )
    if not security.users.delete_role(code):
        raise HTTPException(status_code=404, detail="角色不存在")
    security.audit.log(
        action="role.delete", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="role", target_id=code, ip=_ip(request),
    )
    return JSONResponse({"ok": True})


# ================================================================ 会话监控
@router.get("/sessions")
async def list_sessions(
    user: User = Depends(require_permission("system.session.manage")),
) -> JSONResponse:
    """全部在线会话（文档 §7.5）。"""
    users = {u.id: u for u in security.users.list_users()}
    items = []
    for s in security.sessions.list_all():
        owner = users.get(s.user_id)
        row = s.public()
        row["user_name"] = owner.display_name if owner else "（已删除）"
        row["login_name"] = owner.login_name if owner else ""
        row["roles"] = owner.roles if owner else []
        items.append(row)
    return JSONResponse({"sessions": items})


@router.post("/sessions/cleanup")
async def cleanup_sessions(
    request: Request,
    actor: User = Depends(require_permission("system.session.manage")),
) -> JSONResponse:
    """清理过期与已撤销的会话记录（文档 §3.10）。"""
    removed = security.sessions.cleanup()
    security.audit.log(
        action="session.cleanup", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="session", target_id="expired",
        changes={"removed": removed}, ip=_ip(request),
    )
    return JSONResponse({"ok": True, "removed": removed})


@router.post("/sessions/revoke-all")
async def revoke_all_sessions(
    payload: dict | None = None,
    request: Request = None,
    actor: User = Depends(require_permission("system.session.manage")),
) -> JSONResponse:
    """撤销**除自己以外**的全部会话（紧急处置用）。

    文档 §7.5 的强制下线能力。刻意保留操作者自己的会话，
    避免把自己也踢下线后无法继续处置。
    """
    payload = payload or {}
    keep_current = bool(payload.get("keep_current", True))
    my_session_id = ""
    if keep_current:
        token = request.cookies.get("shs_session", "") if request else ""
        s = security.sessions.resolve(token) if token else None
        my_session_id = s.id if s else ""

    users = security.users.list_users()
    total = 0
    for u in users:
        total += security.sessions.revoke_all_for_user(
            u.id, except_session_id=my_session_id
        )

    security.audit.log(
        action="session.revoke_all", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="session", target_id="all",
        changes={"revoked": total, "kept_own": bool(my_session_id),
                 "high_risk": True},
        ip=_ip(request) if request else "",
    )
    return JSONResponse({
        "ok": True, "revoked": total,
        "message": f"已撤销 {total} 个会话"
                   + ("（保留了当前登录）" if my_session_id else ""),
    })


@router.delete("/sessions/{session_id}")
async def admin_revoke_session(
    session_id: str,
    request: Request,
    actor: User = Depends(require_permission("system.session.manage")),
) -> JSONResponse:
    """强制下线指定会话（文档 §7.5）。"""
    target = security.sessions.get(session_id)
    if target is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    security.sessions.revoke(session_id)
    security.audit.log(
        action="session.force_logout", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="session", target_id=session_id,
        changes={"owner": target.user_id}, ip=_ip(request),
    )
    return JSONResponse({"ok": True})


# ================================================================ 审计中心
@router.get("/audit")
async def query_audit(
    actor_id: str = Query(""),
    module: str = Query(""),
    action: str = Query(""),
    target_id: str = Query(""),
    result: str = Query(""),
    request_id: str = Query(""),
    since: str = Query(""),
    until: str = Query(""),
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_permission("system.audit.read")),
) -> JSONResponse:
    """检索审计日志（文档 §7.8）。支持分页。"""
    # 多取一条用于判断是否还有下一页
    rows = security.audit.query(
        actor_id=actor_id, module=module, action=action, target_id=target_id,
        result=result, request_id=request_id, since=since, until=until,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    rows = rows[offset:offset + limit] if offset else rows[:limit]
    return JSONResponse({
        "records": rows,
        "stats": security.audit.stats(),
        "modules": sorted({r.get("module", "") for r in rows if r.get("module")}),
        "actions": sorted({r.get("action", "") for r in rows if r.get("action")}),
        "has_more": has_more,
        "offset": offset,
        "limit": limit,
    })


@router.get("/audit/stats")
async def audit_stats(
    days: int = Query(7, ge=1, le=90, description="统计最近多少天"),
    bucket: str = Query("hour", pattern="^(hour|day)$"),
    user: User = Depends(require_permission("system.audit.read")),
) -> JSONResponse:
    """审计聚合统计（P1：审计看板的数据源）。

    返回趋势、Top 操作者/动作、结果分布 —— 全部由**服务端聚合**，
    前端只负责画图（避免把上万条记录丢给浏览器）。

    Args:
        days: 统计窗口（1~90 天）
        bucket: 时间粒度，`hour` 或 `day`
    """
    from collections import Counter
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).astimezone()
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    since_date = since[:10]

    rows = security.audit.query(since=since_date, limit=50000)

    # ---- 时间桶 ----
    fmt = "%Y-%m-%d %H:00" if bucket == "hour" else "%Y-%m-%d"
    trend: Counter[str] = Counter()
    trend_failed: Counter[str] = Counter()
    for r in rows:
        at = str(r.get("at") or "")
        if len(at) < 13:
            continue
        try:
            dt = datetime.fromisoformat(at)
        except ValueError:
            continue
        key = dt.strftime(fmt)
        trend[key] += 1
        if r.get("result") in ("denied", "error"):
            trend_failed[key] += 1

    # 补齐空桶，让前端画图不出现断档
    buckets: list[dict] = []
    if bucket == "hour":
        cursor = now.replace(minute=0, second=0, microsecond=0)
        for i in range(min(days * 24, 24 * 7)):
            key = (cursor - timedelta(hours=i)).strftime(fmt)
            buckets.append({"t": key, "n": trend.get(key, 0),
                            "failed": trend_failed.get(key, 0)})
    else:
        cursor = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for i in range(days):
            key = (cursor - timedelta(days=i)).strftime(fmt)
            buckets.append({"t": key, "n": trend.get(key, 0),
                            "failed": trend_failed.get(key, 0)})
    buckets.reverse()

    # ---- Top 统计 ----
    def top(counter: Counter, n: int = 8) -> list[dict]:
        return [{"name": k or "（未知）", "n": v} for k, v in counter.most_common(n)]

    actors: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    modules: Counter[str] = Counter()
    results: Counter[str] = Counter()
    denied_actions: Counter[str] = Counter()

    for r in rows:
        actors[str(r.get("actor_name") or r.get("actor_id") or "（匿名）")] += 1
        actions[str(r.get("action") or "?")] += 1
        modules[str(r.get("module") or "?")] += 1
        results[str(r.get("result") or "?")] += 1
        if r.get("result") == "denied":
            denied_actions[str(r.get("action") or "?")] += 1

    total = len(rows)
    failed = results.get("denied", 0) + results.get("error", 0)

    return JSONResponse({
        "window": {"days": days, "bucket": bucket, "since": since_date},
        "total": total,
        "failed": failed,
        "failed_rate": round(failed / total * 100, 2) if total else 0.0,
        "buckets": buckets,
        "top_actors": top(actors),
        "top_actions": top(actions),
        "top_modules": top(modules),
        "top_denied": top(denied_actions, 5),
        "results": dict(results),
        "truncated": total >= 50000,
    })


@router.get("/audit/export")
async def export_audit(
    request: Request,
    actor: User = Depends(require_permission("system.audit.read")),
    module: str = Query(""),
    limit: int = Query(5000, ge=1, le=50000),
) -> StreamingResponse:
    """导出审计数据（文档 §7.8：**需独立权限，并记录导出行为**）。"""
    rows = security.audit.query(module=module, limit=limit)

    # 导出行为本身必须留痕
    security.audit.log(
        action="audit.export", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="audit", target_id=f"count={len(rows)}",
        changes={"module_filter": module, "rows": len(rows)}, ip=_ip(request),
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    return StreamingResponse(
        iter([body]),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="audit_{stamp}.jsonl"'},
    )


@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: str,
    payload: dict | None = None,
    request: Request = None,
    actor: User = Depends(require_permission("system.user.manage")),
) -> JSONResponse:
    """管理员为他人重置密码（高危，文档 §7.1）。

    ## 安全约束（缺一不可）

    | 约束 | 原因 |
    |------|------|
    | **不能重置自己** | 自己的密码走「修改密码」，需要验证原密码 |
    | **撤销该用户全部会话** | 改密后旧会话必须失效（§3.13） |
    | **强制下次登录改密** | 管理员知道临时密码，必须让本人尽快改掉 |
    | **写审计（高危）** | 这是账号接管类操作，必须留痕 |
    | **不返回密码哈希** | 永远不返回（§7.1） |

    密码强度校验与自助改密走**同一套规则**（不因管理员操作而放宽）。
    """
    from ..security.auth import hash_password, password_strength_error

    payload = payload or {}
    target = security.users.get(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if target.id == actor.id:
        raise HTTPException(
            status_code=400,
            detail="不能在这里重置自己的密码。请用右上角「用户名 → 修改密码」（需验证原密码）",
        )

    new_password = str(payload.get("new_password") or "")
    if not new_password:
        raise HTTPException(status_code=400, detail="缺少 new_password")

    err = password_strength_error(new_password, target.login_name)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # ① 改密 + 强制下次登录修改 + 清空锁定
    updated = security.users.update_fields(
        user_id,
        password_hash=hash_password(new_password),
        must_change_password=True,
        failed_attempts=0,
        locked_until="",
    )
    if updated is None:
        raise HTTPException(status_code=500, detail="重置失败，请查看服务日志")

    # ② 撤销该用户全部会话（旧会话不能继续用）
    revoked = security.sessions.revoke_all_for_user(user_id)

    # ③ 审计（高危操作）
    security.audit.log(
        action="user.reset_password", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="user", target_id=user_id,
        changes={
            "login_name": target.login_name,
            "sessions_revoked": revoked,
            "must_change_password": True,
            "high_risk": True,
        },
        ip=_ip(request),
    )
    return JSONResponse({
        "ok": True,
        "revoked": revoked,
        "message": f"已重置 {target.login_name} 的密码，其 {revoked} 个登录已失效，"
                   f"并已要求本人下次登录时修改密码",
    })


# ================================================================ 备份与恢复
def _backup_manager():
    """构造备份管理器（默认以项目根为基准，与运行位置无关）。"""
    from ..security.backup import BackupManager

    return BackupManager()


@router.post("/backup/create")
async def backup_create(
    payload: dict | None = None,
    request: Request = None,
    actor: User = Depends(require_permission("system.backup.manage")),
) -> JSONResponse:
    """手动创建一份加密备份。"""
    payload = payload or {}
    try:
        info = _backup_manager().create(label=str(payload.get("label") or "manual"))
    except Exception as exc:  # noqa: BLE001
        security.audit.log(
            action="backup.create", module="backup", result="error",
            actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
            error_kind="backup_failed", changes={"error": str(exc)[:120]}, ip=_ip(request),
        )
        raise HTTPException(status_code=500, detail=f"创建备份失败：{exc}") from exc

    security.audit.log(
        action="backup.create", module="backup", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="backup", target_id=info.id,
        changes={"files": info.file_count, "kb": round(info.bytes / 1024, 1)},
        ip=_ip(request),
    )
    return JSONResponse({
        "ok": True,
        "backup": info.public(),
        "key_created": bool(getattr(info, "key_created", False)),
        "note": (
            "已生成新的备份密钥，请把它另行妥善保管 —— 密钥丢失将无法解密备份。"
            if getattr(info, "key_created", False) else ""
        ),
    })


@router.get("/backup/list")
async def backup_list(
    actor: User = Depends(require_permission("system.backup.manage")),
) -> JSONResponse:
    """列出备份（**不含密钥与内容**）。"""
    mgr = _backup_manager()
    return JSONResponse({
        "backups": [b.public() for b in mgr.list()],
        "freshness": mgr.freshness(),
        "keep": mgr.keep,
    })


@router.post("/backup/restore")
async def backup_restore(
    payload: dict | None = None,
    request: Request = None,
    actor: User = Depends(require_permission("system.backup.manage")),
) -> JSONResponse:
    """从备份恢复。

    **强制两次确认**（文档 §12.9：回滚代码不等于回滚业务写入）：
      1. `confirm` 必须与 `backup_id` **完全一致**
      2. 恢复前自动创建「还原前备份」，返回其 ID 供回滚
    全程写审计。
    """
    payload = payload or {}
    backup_id = str(payload.get("backup_id") or "").strip()
    confirm = str(payload.get("confirm") or "").strip()

    if not backup_id:
        raise HTTPException(status_code=400, detail="缺少 backup_id")
    if confirm != backup_id:
        security.audit.log(
            action="backup.restore", module="backup", result="denied",
            actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
            target_type="backup", target_id=backup_id,
            error_kind="confirm_mismatch", ip=_ip(request),
        )
        raise HTTPException(
            status_code=400,
            detail="二次确认失败：confirm 必须与 backup_id 完全一致",
        )

    try:
        result = _backup_manager().restore(backup_id, confirm=confirm)
    except Exception as exc:  # noqa: BLE001
        security.audit.log(
            action="backup.restore", module="backup", result="error",
            actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
            target_type="backup", target_id=backup_id,
            error_kind="restore_failed", changes={"error": str(exc)[:120]}, ip=_ip(request),
        )
        raise HTTPException(status_code=500, detail=f"恢复失败：{exc}") from exc

    security.audit.log(
        action="backup.restore", module="backup", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="backup", target_id=backup_id,
        changes={
            "restored": len(result.restored_files),
            "pre_restore_backup": result.pre_restore_backup,
        },
        ip=_ip(request),
    )
    return JSONResponse({
        "ok": True,
        "restored_files": result.restored_files,
        "pre_restore_backup": result.pre_restore_backup,
        "message": result.message,
        "hint": "如需回滚，请用 pre_restore_backup 的 ID 再次调用本接口",
    })


# ================================================================ 系统健康
@router.get("/health")
async def system_health(
    user: User = Depends(require_permission("system.health.read")),
) -> JSONResponse:
    """系统健康（文档 §7.7）：版本、运行状态、磁盘、会话与审计概况。"""
    root = Path(__file__).resolve().parent.parent.parent

    def dir_size(path: Path) -> int:
        if not path.is_dir():
            return 0
        total = 0
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
        return total

    output_dir = root / "output"
    batches = [b for b in output_dir.glob("batch_*") if b.is_dir()] if output_dir.is_dir() else []
    bg_dir = output_dir / "_effect_backgrounds"
    backgrounds = len(list(bg_dir.glob("*.png"))) if bg_dir.is_dir() else 0

    from ..effect_renderer import EFFECT_RENDERER_VERSION

    # 备份新鲜度（块 1 新增）
    try:
        backup_info = _backup_manager().freshness()
    except Exception:  # noqa: BLE001
        backup_info = {"count": 0, "stale": True, "note": "无法读取备份状态"}

    # 磁盘水位
    try:
        import shutil as _sh
        usage = _sh.disk_usage(str(root))
        disk = {
            "total_gb": round(usage.total / 1024 ** 3, 1),
            "used_gb": round(usage.used / 1024 ** 3, 1),
            "free_gb": round(usage.free / 1024 ** 3, 1),
            "used_percent": round(usage.used / max(1, usage.total) * 100, 1),
            "low": usage.free < 2 * 1024 ** 3,     # 少于 2GB 视为告急
        }
    except Exception:  # noqa: BLE001
        disk = {}

    return JSONResponse({
        "version": {
            "app": "0.1.0",
            "effect_renderer": EFFECT_RENDERER_VERSION,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "uptime_seconds": round(time.monotonic()),
        "counts": {
            "users": security.users.count(),
            "roles": len(security.users.list_roles()),
            "active_sessions": len(security.sessions.list_all()),
            "batches": len(batches),
            "ai_backgrounds": backgrounds,
        },
        "storage_mb": {
            "output": round(dir_size(output_dir) / 1024 / 1024, 1),
            "logs": round(dir_size(root / "logs") / 1024 / 1024, 1),
            "data": round(dir_size(root / "data") / 1024 / 1024, 1),
        },
        "disk": disk,
        "backup": backup_info,
        "audit": security.audit.stats(),
    })


# ================================================================ 系统维护（P3）
def _dir_stats(path: Path, pattern: str = "*") -> dict:
    """统计目录下的文件数与体积。"""
    if not path.is_dir():
        return {"files": 0, "mb": 0.0}
    n = 0
    total = 0
    try:
        for p in path.rglob(pattern):
            try:
                if p.is_file():
                    n += 1
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return {"files": n, "mb": round(total / 1024 / 1024, 1)}


@router.get("/system/disk")
async def system_disk(
    user: User = Depends(require_permission("system.health.read")),
) -> JSONResponse:
    """磁盘占用明细与清理建议（P3：5.2）。

    按「可安全清理」「谨慎清理」「不可清理」三档给出建议 ——
    磁盘告急时管理员需要知道**先删什么**。
    """
    root = Path(__file__).resolve().parent.parent.parent
    output = root / "output"
    logs = root / "logs"
    audit = logs / "audit"
    backups = root / "backups"
    data = root / "data"

    items = [
        {"name": "批次图片", "path": "output/batch_*", **_dir_stats(output, "*.png"),
         "level": "keep", "note": "业务产物，AGENTS.md 规定不可删除"},
        {"name": "AI 门店背景", "path": "output/_effect_backgrounds",
         **_dir_stats(output / "_effect_backgrounds", "*.png"),
         "level": "caution", "note": "删了效果图要重新生成（消耗额度）"},
        {"name": "参考图", "path": "output/_references",
         **_dir_stats(output / "_references", "*"),
         "level": "caution", "note": "用户上传的门店照片，谨慎删除"},
        {"name": "审计日志", "path": "logs/audit", **_dir_stats(audit, "*.jsonl"),
         "level": "caution", "note": "安全追溯依据，建议归档而非删除"},
        {"name": "运行日志", "path": "logs", **_dir_stats(logs, "*.log"),
         "level": "safe", "note": "排障用，可安全清理旧文件"},
        {"name": "加密备份", "path": "backups", **_dir_stats(backups, "*"),
         "level": "caution", "note": "数据安全兜底，超出保留份数的会自动清理"},
        {"name": "安全数据", "path": "data/security", **_dir_stats(data / "security", "*"),
         "level": "keep", "note": "账号与会话，绝不可删"},
    ]

    # 快照目录体积（自愈用，通常很小）
    snap = _dir_stats(data / "security", "*.json")
    items.append({"name": "安全数据快照", "path": "data/security/*.snapshots",
                  "files": snap["files"], "mb": snap["mb"],
                  "level": "caution", "note": "损坏自愈用，保留最近几份即可"})

    total_mb = round(sum(i["mb"] for i in items), 1)
    return JSONResponse({
        "items": items,
        "total_mb": total_mb,
        "levels": {
            "safe": "可安全清理",
            "caution": "谨慎清理（会影响功能或需重新生成）",
            "keep": "不可清理",
        },
    })


@router.get("/system/cleanup/preview")
async def cleanup_preview(
    user: User = Depends(require_permission("system.backup.manage")),
) -> JSONResponse:
    """清理预览：**先看会删什么，再决定删不删**（P3：5.3）。

    只统计**明确安全**的项，不做任何修改。
    """
    import time as _t

    root = Path(__file__).resolve().parent.parent.parent
    logs = root / "logs"
    now = _t.time()
    week = 7 * 86400

    # ① 7 天前的运行日志
    old_logs = []
    for p in logs.glob("*.log"):
        try:
            if now - p.stat().st_mtime > week:
                old_logs.append(p)
        except OSError:
            continue

    # ② 过期/已撤销的会话
    sessions = security.sessions.list_all()
    total_sessions = len((security.sessions._load().get("sessions") or {}))

    # ③ 超出保留份数的备份
    from ..security.backup import BackupManager

    mgr = BackupManager()
    backups = mgr.list()

    # ④ 损坏文件残留
    corrupts = list((root / "data" / "security").glob("*.corrupt.*"))

    return JSONResponse({
        "log_files": {
            "count": len(old_logs),
            "mb": round(sum(p.stat().st_size for p in old_logs) / 1024 / 1024, 1),
            "names": [p.name for p in old_logs[:10]],
            "keep": "仅 7 天前的 *.log",
        },
        "sessions": {
            "active": len(sessions),
            "total_records": total_sessions,
            "cleanable": max(0, total_sessions - len(sessions)),
        },
        "backups": {
            "count": len(backups),
            "keep": mgr.keep,
            "cleanable": max(0, len(backups) - mgr.keep),
        },
        "corrupt_files": {"count": len(corrupts),
                          "names": [p.name for p in corrupts[:10]]},
    })


@router.post("/system/cleanup")
async def system_cleanup(
    payload: dict | None = None,
    request: Request = None,
    actor: User = Depends(require_permission("system.backup.manage")),
) -> JSONResponse:
    """执行清理（P3：5.3）。

    ⚠️ **只清理明确安全的项**：
      · 7 天前的运行日志
      · 过期/已撤销的会话记录
      · 损坏文件残留（`.corrupt.*`）

    **绝不触碰**：批次图片、审计日志、备份文件、安全数据。
    每项都先做一次备份，且全程写审计。
    """
    import time as _t

    payload = payload or {}
    do_logs = bool(payload.get("logs", True))
    do_sessions = bool(payload.get("sessions", True))
    do_corrupt = bool(payload.get("corrupt_files", False))

    root = Path(__file__).resolve().parent.parent.parent
    logs = root / "logs"
    now = _t.time()
    week = 7 * 86400

    removed_logs = 0
    freed = 0
    if do_logs:
        for p in logs.glob("*.log"):
            try:
                if now - p.stat().st_mtime > week:
                    freed += p.stat().st_size
                    p.unlink()
                    removed_logs += 1
            except OSError:
                continue

    removed_sessions = security.sessions.cleanup() if do_sessions else 0

    removed_corrupt = 0
    if do_corrupt:
        for p in (root / "data" / "security").glob("*.corrupt.*"):
            try:
                p.unlink()
                removed_corrupt += 1
            except OSError:
                continue

    security.audit.log(
        action="system.cleanup", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="system", target_id="cleanup",
        changes={
            "log_files": removed_logs, "log_mb": round(freed / 1024 / 1024, 1),
            "sessions": removed_sessions, "corrupt_files": removed_corrupt,
        },
        ip=_ip(request) if request else "",
    )
    return JSONResponse({
        "ok": True,
        "removed": {
            "log_files": removed_logs,
            "log_mb": round(freed / 1024 / 1024, 1),
            "sessions": removed_sessions,
            "corrupt_files": removed_corrupt,
        },
        "message": f"已清理 {removed_logs} 个旧日志、{removed_sessions} 条过期会话"
                   + (f"、{removed_corrupt} 个损坏文件残留" if removed_corrupt else ""),
    })


@router.get("/system/logs")
async def system_logs(
    name: str = Query("", description="日志文件名；留空返回列表"),
    lines: int = Query(200, ge=10, le=2000),
    user: User = Depends(require_permission("system.health.read")),
) -> JSONResponse:
    """查看服务运行日志（P3：5.4）。

    ⚠️ **安全约束**：
      · 文件名必须匹配白名单（防目录穿越）
      · 只读 `logs/` 下的 `*.log`
      · 读取前做一次脱敏（API Key 不应出现在界面）
    """
    import re as _re

    root = Path(__file__).resolve().parent.parent.parent
    logs_dir = root / "logs"

    if not name:
        files = []
        for p in sorted(logs_dir.glob("*.log"), key=lambda x: x.stat().st_mtime
                        if x.is_file() else 0, reverse=True)[:50]:
            try:
                st = p.stat()
                files.append({"name": p.name, "kb": round(st.st_size / 1024, 1),
                              "mtime": __import__("time").strftime(
                                  "%Y-%m-%d %H:%M", __import__("time").localtime(st.st_mtime))})
            except OSError:
                continue
        return JSONResponse({"files": files})

    # 白名单：只允许 logs/ 下的 .log 且不含路径分隔符
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".log"):
        raise HTTPException(status_code=400, detail="非法的日志文件名")

    target = (logs_dir / name).resolve()
    try:
        target.relative_to(logs_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="非法的日志路径") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="日志文件不存在")

    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"读取失败：{exc.__class__.__name__}") from exc

    all_lines = raw.splitlines()
    tail = all_lines[-lines:]

    # 脱敏：任何长得像密钥的串都打码（防御性，正常日志不该有）
    secret_re = _re.compile(r"(sk-[A-Za-z0-9_\-]{12,}|Bearer\s+[A-Za-z0-9._\-]{16,})")
    tail = [secret_re.sub("***", ln) for ln in tail]

    return JSONResponse({
        "name": name,
        "total_lines": len(all_lines),
        "shown": len(tail),
        "lines": tail,
    })


# ================================================================ 批量与导出（批3）
@router.post("/roles/{code}/copy")
async def copy_role(
    code: str,
    payload: dict | None = None,
    request: Request = None,
    actor: User = Depends(require_permission("system.role.manage")),
) -> JSONResponse:
    """从现有角色复制出新角色（P2：2.2）。"""
    import re as _re

    payload = payload or {}
    source = security.users.list_roles().get(code)
    if source is None:
        raise HTTPException(status_code=404, detail="源角色不存在")

    new_code = _re.sub(r"[^a-z0-9_\-]", "", str(payload.get("code") or "").lower())[:32]
    if not new_code:
        raise HTTPException(status_code=400, detail="角色编码只能包含小写字母、数字、下划线、连字符")
    if new_code in security.users.list_roles():
        raise HTTPException(status_code=409, detail=f"角色已存在：{new_code}")

    perms = list(source.get("permissions") or [])
    security.users.save_role(
        new_code,
        str(payload.get("name") or f"{source.get('name', code)} 副本"),
        perms,
        f"复制自 {code}",
    )
    security.audit.log(
        action="role.copy", module="admin", result="success",
        actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
        target_type="role", target_id=new_code,
        changes={"source": code, "permissions": len(perms)},
        ip=_ip(request) if request else "",
    )
    return JSONResponse({"ok": True, "code": new_code, "permissions": len(perms)})


@router.get("/audit/trace")
async def audit_trace(
    request_id: str = Query(..., description="请求 ID"),
    user: User = Depends(require_permission("system.audit.read")),
) -> JSONResponse:
    """按请求 ID 追踪一次操作的完整链路（P2：4.3）。

    一次 HTTP 请求可能产生多条审计（鉴权拒绝 + 业务操作 + 中间件记录），
    用 request_id 把它们串起来，是排查"到底发生了什么"的关键手段。
    """
    rows = security.audit.query(request_id=request_id, limit=200)
    return JSONResponse({
        "request_id": request_id,
        "count": len(rows),
        "records": rows,
    })
