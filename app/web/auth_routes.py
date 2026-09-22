# -*- coding: utf-8 -*-
"""认证与授权路由（文档 §3 + §4）。

## 设计原则

- **唯一权限入口**：`require_permission()` 是所有接口的权限闸门
- **默认拒绝**：未显式放行的接口一律需要登录（文档 §2.5）
- **审计留痕**：登录、登出、改密、被拒访问都写审计
- **分状态提示**：401（未登录/过期）与 403（无权限）返回**不同**状态码与提示，
  前端据此区分展示（文档 §6：不得统一表现为菜单消失）

## Cookie 安全属性（文档 §3.11）

| 属性 | 值 | 理由 |
|------|----|------|
| `httponly` | True | 阻止 JS 读取，缓解 XSS 窃取 |
| `samesite` | `lax` | 阻止跨站请求携带（CSRF 基础防护） |
| `secure` | 跟随请求协议 | HTTPS 下必须开启 |
| `path` | `/` | 全站可用 |
"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..security.audit import RESULT_DENIED, current_request_id
from ..security.service import SecurityService
from ..security.users import User

__all__ = ["router", "security", "current_user", "optional_user", "require_permission",
           "SESSION_COOKIE"]

SESSION_COOKIE = "shs_session"      # shop sticker session

# 全局单例（测试时可替换）
security = SecurityService()

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ================================================================ 依赖
def _client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or ""


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    from ..security.auth import SessionStore

    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SessionStore.ABSOLUTE_TIMEOUT_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def optional_user(request: Request) -> User | None:
    """解析当前用户；未登录返回 None（不抛异常）。"""
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    return security.resolve(token)


def current_user(request: Request) -> User:
    """要求已登录。

    Raises:
        HTTPException 401: 未登录或会话已过期
    """
    user = optional_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期，请重新登录")
    return user


def require_permission(code: str) -> Callable[[Request], User]:
    """生成权限校验依赖（文档 §2.4：所有权限必须服务端校验）。

    ⚠️ 被拒绝的访问**必须写审计**（文档 §8：越权访问及连续拒绝告警）。

    Usage:
        @app.post("/api/run/new-batch")
        async def handler(user: User = Depends(require_permission("batch.create"))):
            ...
    """

    def dependency(request: Request) -> User:
        user = optional_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="未登录或会话已过期，请重新登录")

        if not security.has_permission(user, code):
            security.audit.log(
                action="access.denied",
                module=code.split(".")[0] if "." in code else code,
                result=RESULT_DENIED,
                actor_id=user.id,
                actor_name=user.display_name,
                actor_roles=user.roles,
                target_type="endpoint",
                target_id=f"{request.method} {request.url.path}",
                error_kind="forbidden",
                ip=_client_ip(request),
            )
            raise HTTPException(
                status_code=403, detail=f"没有权限执行该操作（需要 {code}）"
            )
        return user

    return dependency


# ================================================================ 状态
@router.get("/status")
async def auth_status(request: Request) -> JSONResponse:
    """前端启动时调用：判断是否需要初始化、是否已登录。

    **无需登录即可访问** —— 否则前端无法知道该显示「创建管理员」还是「登录」。
    只返回布尔与公开信息，不泄露任何账号细节。
    """
    user = optional_user(request)
    return JSONResponse({
        "needs_setup": security.needs_setup(),
        "authenticated": user is not None,
        "user": user.public() if user else None,
        "permissions": sorted(security.permissions_of(user)) if user else [],
    })


@router.post("/setup")
async def auth_setup(payload: dict | None = None, request: Request = None) -> JSONResponse:
    """首次初始化：创建管理员账号（文档 Q4 决策：同时接管历史批次）。

    ⚠️ 仅在**系统无任何账号**时可用；一旦有账号立即关闭此入口。
    """
    payload = payload or {}
    if not security.needs_setup():
        raise HTTPException(status_code=409, detail="系统已初始化，无法重复创建管理员")

    login_name = str(payload.get("login_name") or "").strip()
    display_name = str(payload.get("display_name") or "").strip()
    password = str(payload.get("password") or "")

    try:
        user = security.create_initial_admin(
            login_name, display_name or login_name, password,
            claim_legacy=bool(payload.get("claim_legacy", True)),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    token, _ = security.sessions.create(
        user.id, ip=_client_ip(request), user_agent=request.headers.get("user-agent", "")
    )
    response = JSONResponse({
        "ok": True,
        "user": user.public(),
        "permissions": sorted(security.permissions_of(user)),
    })
    _set_session_cookie(response, token, request)
    return response


# ================================================================ 登录
@router.post("/login")
async def auth_login(payload: dict | None = None, request: Request = None) -> JSONResponse:
    """登录。

    失败时统一返回「登录名或密码不正确」——不区分账号是否存在（文档 §3.8）。
    """
    payload = payload or {}
    result = security.login(
        str(payload.get("login_name") or ""),
        str(payload.get("password") or ""),
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
        mfa_code=str(payload.get("mfa_code") or ""),
    )

    if not result.ok:
        # 需要 MFA 时返回 200 + 标志，让前端弹验证码输入框（不是错误）
        if result.needs_mfa:
            return JSONResponse({
                "ok": False, "needs_mfa": True, "error": result.error,
            })
        raise HTTPException(status_code=401, detail=result.error)

    response = JSONResponse({
        "ok": True,
        "user": result.user.public(),
        "permissions": sorted(security.permissions_of(result.user)),
    })
    _set_session_cookie(response, result.token, request)
    return response


@router.post("/logout")
async def auth_logout(request: Request) -> JSONResponse:
    token = request.cookies.get(SESSION_COOKIE, "")
    user = optional_user(request)
    security.logout(token, user=user, ip=_client_ip(request))
    response = JSONResponse({"ok": True})
    _clear_session_cookie(response)
    return response


# ================================================================ 当前用户
@router.get("/me")
async def auth_me(user: User = Depends(current_user)) -> JSONResponse:
    """返回当前用户与**权限清单** —— 前端据此渲染导航（文档 §4 唯一来源）。"""
    return JSONResponse({
        "user": user.public(),
        "permissions": sorted(security.permissions_of(user)),
        "roles": [
            {"code": code, **(security.users.list_roles().get(code) or {})}
            for code in user.roles
        ],
    })


@router.post("/password")
async def auth_change_password(
    payload: dict | None = None,
    request: Request = None,
    user: User = Depends(current_user),
) -> JSONResponse:
    """修改自己的密码。

    成功后**除当前会话外全部失效**（文档 §3.13）。
    """
    payload = payload or {}
    token = request.cookies.get(SESSION_COOKIE, "")
    try:
        security.change_password(
            user,
            str(payload.get("old_password") or ""),
            str(payload.get("new_password") or ""),
            keep_session_token=token,
            ip=_client_ip(request),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "message": "密码已修改，其它设备上的登录已失效"})


# ================================================================ 我的设备
@router.get("/sessions")
async def auth_sessions(request: Request, user: User = Depends(current_user)) -> JSONResponse:
    """列出自己的登录设备（文档 §3.12）。"""
    token = request.cookies.get(SESSION_COOKIE, "")
    current = security.sessions.resolve(token)
    items = [s.public() for s in security.sessions.list_for_user(user.id)]
    for item in items:
        item["current"] = bool(current and item["id"] == current.id)
    return JSONResponse({"sessions": items})


@router.delete("/sessions/{session_id}")
async def auth_revoke_session(
    session_id: str, request: Request, user: User = Depends(current_user)
) -> JSONResponse:
    """撤销指定会话（只能撤销自己的）。"""
    target = security.sessions.get(session_id)
    if target is None or target.user_id != user.id:
        # 不区分「不存在」与「不属于你」，避免探测他人会话 ID
        raise HTTPException(status_code=404, detail="会话不存在")

    security.sessions.revoke(session_id)
    security.audit.log(
        action="auth.revoke_session", module="auth", result="success",
        actor_id=user.id, actor_name=user.display_name, actor_roles=user.roles,
        target_type="session", target_id=session_id, ip=_client_ip(request),
    )
    return JSONResponse({"ok": True})


# ================================================================ MFA
@router.post("/mfa/setup")
async def auth_mfa_setup(user: User = Depends(current_user)) -> JSONResponse:
    """生成 TOTP 密钥（此时尚未启用，需用 /mfa/enable 确认）。"""
    from ..security.auth import generate_totp_secret, totp_uri

    if user.mfa_enabled:
        raise HTTPException(status_code=409, detail="已启用动态验证码")

    secret = generate_totp_secret()
    security.users.update_fields(user.id, mfa_secret=secret)
    return JSONResponse({
        "ok": True,
        "secret": secret,
        "uri": totp_uri(secret, user.login_name),
        "message": "请用认证器 App 扫描或手动录入，然后调用 /api/auth/mfa/enable 并提交一次验证码确认",
    })


@router.post("/mfa/enable")
async def auth_mfa_enable(
    payload: dict | None = None, user: User = Depends(current_user)
) -> JSONResponse:
    """用一次正确的验证码确认启用 MFA。"""
    from ..security.auth import verify_totp

    payload = payload or {}
    fresh = security.users.get(user.id)
    if fresh is None or not fresh.mfa_secret:
        raise HTTPException(status_code=400, detail="请先调用 /api/auth/mfa/setup 生成密钥")
    if fresh.mfa_enabled:
        raise HTTPException(status_code=409, detail="已启用动态验证码")
    if not verify_totp(fresh.mfa_secret, str(payload.get("code") or "")):
        raise HTTPException(status_code=400, detail="验证码不正确")

    security.users.update_fields(user.id, mfa_enabled=True)
    security.audit.log(
        action="auth.mfa_enabled", module="auth", result="success",
        actor_id=user.id, actor_name=user.display_name, actor_roles=user.roles,
        target_type="user", target_id=user.id,
    )
    return JSONResponse({"ok": True, "message": "已启用动态验证码"})


@router.post("/mfa/disable")
async def auth_mfa_disable(
    payload: dict | None = None, user: User = Depends(current_user)
) -> JSONResponse:
    """关闭 MFA（需提供一次有效验证码，防止会话被盗后直接关闭）。"""
    from ..security.auth import verify_totp

    payload = payload or {}
    fresh = security.users.get(user.id)
    if fresh is None or not fresh.mfa_enabled:
        raise HTTPException(status_code=400, detail="当前未启用动态验证码")
    if not verify_totp(fresh.mfa_secret, str(payload.get("code") or "")):
        raise HTTPException(status_code=400, detail="验证码不正确")

    security.users.update_fields(user.id, mfa_enabled=False, mfa_secret="")
    security.audit.log(
        action="auth.mfa_disabled", module="auth", result="success",
        actor_id=user.id, actor_name=user.display_name, actor_roles=user.roles,
        target_type="user", target_id=user.id,
    )
    return JSONResponse({"ok": True, "message": "已关闭动态验证码"})
