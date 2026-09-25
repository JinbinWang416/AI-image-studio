# -*- coding: utf-8 -*-
"""认证与授权的统一入口（文档 §3 + §4）。

**所有权限判断都必须经过这里** —— 前端导航、接口校验、审计共用同一套逻辑，
避免出现「菜单藏了但接口还能调」的情况（文档 §2.4）。

## 权限计算方法

```
用户 → 角色（可多个）→ 权限并集
```

`admin` 角色是特例：拥有全部权限（但仍**必须留审计**，文档 §4）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .audit import RESULT_DENIED, AuditLogger
from .auth import (
    AuthError,
    SessionStore,
    hash_password,
    needs_rehash,
    password_strength_error,
    verify_password,
)
from .permissions import (
    BUILTIN_ROLE_CODES,
    PERMISSION_CODES,
    ROLE_TEMPLATES,
    is_valid_permission,
    role_default_permissions,
)
from .users import STATUS_ACTIVE, STATUS_DISABLED, User, UserStore, UsernameError

__all__ = ["SecurityService", "AuthResult", "security"]


# 登录失败锁定策略
MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15


@dataclass
class AuthResult:
    ok: bool
    token: str = ""
    user: User | None = None
    error: str = ""
    error_kind: str = ""
    needs_mfa: bool = False


class SecurityService:
    """安全服务单例。"""

    def __init__(
        self,
        users: UserStore | None = None,
        sessions: SessionStore | None = None,
        audit: AuditLogger | None = None,
    ):
        self.users = users or UserStore.default()
        self.sessions = sessions or SessionStore.default()
        self.audit = audit or AuditLogger.default()
        self._ensure_builtin_roles()

    # ---------------------------------------------------------------- 初始化
    def _ensure_builtin_roles(self) -> None:
        """把内置角色写入存储（允许管理员改名，但不覆盖已有自定义权限）。"""
        existing = self.users.list_roles()
        for code, tpl in ROLE_TEMPLATES.items():
            if code in existing:
                continue
            self.users.save_role(
                code, tpl["name"], role_default_permissions(code), tpl.get("description", "")
            )

    def needs_setup(self) -> bool:
        """是否还没有任何账号（首次启动需要引导创建管理员，文档 Q4）。"""
        return not self.users.has_any_user()

    def create_initial_admin(
        self,
        login_name: str,
        display_name: str,
        password: str,
        *,
        claim_legacy: bool = True,
    ) -> User:
        """创建首个管理员，并把历史批次归到其名下（文档 Q4 决策）。"""
        if not self.needs_setup():
            raise AuthError("系统已有账号，不能重复初始化")

        err = password_strength_error(password, login_name)
        if err:
            raise AuthError(err)

        user = self.users.create(
            login_name,
            display_name,
            hash_password(password),
            roles=["admin"],
            status=STATUS_ACTIVE,
        )
        if claim_legacy:
            try:
                claimed = self.users.claim_legacy_data(user.id)
            except Exception:  # noqa: BLE001 - 迁移失败不影响建号
                claimed = 0
            self.audit.log(
                action="security.init_admin",
                module="system",
                actor_id=user.id,
                actor_name=user.display_name,
                actor_roles=user.roles,
                target_type="user",
                target_id=user.id,
                changes={"login_name": user.login_name, "legacy_batches_claimed": claimed},
            )
        return user

    # ---------------------------------------------------------------- 权限
    def permissions_of(self, user: User | None) -> set[str]:
        """计算用户的权限并集。"""
        if user is None:
            return set()
        if "admin" in user.roles:
            return set(PERMISSION_CODES)
        roles = self.users.list_roles()
        out: set[str] = set()
        for code in user.roles:
            role = roles.get(code)
            if not isinstance(role, dict):
                continue
            for p in role.get("permissions") or []:
                if is_valid_permission(p):
                    out.add(p)
        return out

    def has_permission(self, user: User | None, code: str) -> bool:
        """**唯一的权限判断入口**（文档 §2.4：必须服务端校验）。"""
        if user is None or not user.is_active():
            return False
        return code in self.permissions_of(user)

    def is_high_risk(self, code: str) -> bool:
        from .permissions import get_permission

        p = get_permission(code)
        return bool(p and p.high_risk)

    # ---------------------------------------------------------------- 登录
    def login(
        self,
        login_name: str,
        password: str,
        *,
        ip: str = "",
        user_agent: str = "",
        mfa_code: str = "",
    ) -> AuthResult:
        """登录（文档 §3.8 防枚举 + 限流；§3.9 MFA）。"""
        from .auth import verify_totp

        # ⚠️ 防枚举：无论账号不存在、密码错误还是被停用，
        #    对外都返回同一句提示（文档 §3.8）。
        generic_error = "登录名或密码不正确"

        user = self.users.get_by_login(login_name)
        if user is None:
            # 仍然做一次哈希校验，避免通过响应时间差判断账号是否存在
            verify_password(password, hash_password("dummy-timing-equalizer"))
            self.audit.log(
                action="auth.login", module="auth", result=RESULT_DENIED,
                actor_name=login_name, error_kind="no_such_user", ip=ip,
            )
            return AuthResult(False, error=generic_error, error_kind="invalid_credentials")

        if user.is_locked():
            return AuthResult(False, error="账号已被临时锁定，请稍后再试",
                              error_kind="locked")

        if not verify_password(password, user.password_hash):
            failed = user.failed_attempts + 1
            fields: dict = {"failed_attempts": failed}
            if failed >= MAX_FAILED_ATTEMPTS:
                from datetime import timedelta

                fields["locked_until"] = (
                    datetime.now(timezone.utc) + timedelta(minutes=LOCK_MINUTES)
                ).astimezone().isoformat(timespec="seconds")
                fields["failed_attempts"] = 0
            self.users.update_fields(user.id, **fields)
            self.audit.log(
                action="auth.login", module="auth", result=RESULT_DENIED,
                actor_id=user.id, actor_name=user.display_name, actor_roles=user.roles,
                error_kind="bad_password", ip=ip,
            )
            return AuthResult(False, error=generic_error, error_kind="invalid_credentials")

        # 账号状态检查放在密码校验之后（否则会泄露账号是否存在）
        if user.status == STATUS_DISABLED:
            self.audit.log(
                action="auth.login", module="auth", result=RESULT_DENIED,
                actor_id=user.id, actor_name=user.display_name, actor_roles=user.roles,
                error_kind="disabled", ip=ip,
            )
            return AuthResult(False, error="账号已停用，请联系管理员",
                              error_kind="disabled")
        if user.status != STATUS_ACTIVE:
            return AuthResult(False, error="账号当前不可用，请联系管理员",
                              error_kind="inactive")

        # MFA（文档 §3.9）
        if user.mfa_enabled:
            if not mfa_code:
                return AuthResult(False, error="需要动态验证码", error_kind="mfa_required",
                                  needs_mfa=True, user=user)
            if not verify_totp(user.mfa_secret, mfa_code):
                self.audit.log(
                    action="auth.login", module="auth", result=RESULT_DENIED,
                    actor_id=user.id, actor_name=user.display_name, actor_roles=user.roles,
                    error_kind="bad_mfa", ip=ip,
                )
                return AuthResult(False, error="动态验证码不正确", error_kind="bad_mfa",
                                  needs_mfa=True, user=user)

        # 登录成功：清空失败计数，必要时升级哈希参数
        updates: dict = {"failed_attempts": 0, "locked_until": ""}
        if needs_rehash(user.password_hash):
            updates["password_hash"] = hash_password(password)
        updates["last_login_at"] = datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"
        )
        refreshed = self.users.update_fields(user.id, **updates) or user

        # ⚠️ 会话轮换（文档 §3.10）：登录成功时撤销该用户旧会话，
        #    防止会话固定攻击。
        self.sessions.revoke_all_for_user(refreshed.id)
        token, _ = self.sessions.create(
            refreshed.id, ip=ip, user_agent=user_agent, mfa_verified=refreshed.mfa_enabled
        )

        self.audit.log(
            action="auth.login", module="auth", result="success",
            actor_id=refreshed.id, actor_name=refreshed.display_name,
            actor_roles=refreshed.roles, ip=ip,
        )
        return AuthResult(True, token=token, user=refreshed)

    # ---------------------------------------------------------------- 会话解析
    def resolve(self, token: str) -> User | None:
        """按 token 解析当前用户。

        ⚠️ 每次都重新读用户状态 —— 停用账号或改角色后**立即生效**（文档 §3.6/§3.13）。
        """
        session = self.sessions.resolve(token)
        if session is None:
            return None
        user = self.users.get(session.user_id)
        if user is None or not user.is_active():
            # 账号已停用/删除：顺手撤销该会话
            self.sessions.revoke(session.id)
            return None
        return user

    def logout(self, token: str, *, user: User | None = None, ip: str = "") -> None:
        session = self.sessions.resolve(token)
        if session:
            self.sessions.revoke(session.id)
        if user:
            self.audit.log(
                action="auth.logout", module="auth", result="success",
                actor_id=user.id, actor_name=user.display_name,
                actor_roles=user.roles, ip=ip,
            )

    # ---------------------------------------------------------------- 改密
    def change_password(
        self,
        user: User,
        old_password: str,
        new_password: str,
        *,
        keep_session_token: str = "",
        ip: str = "",
    ) -> None:
        """修改自己的密码。

        文档 §3.13：改密码后历史会话必须失效 —— 这里保留**当前**会话，撤销其余。
        """
        if not verify_password(old_password, user.password_hash):
            raise AuthError("原密码不正确")
        err = password_strength_error(new_password, user.login_name)
        if err:
            raise AuthError(err)

        self.users.update_fields(
            user.id,
            password_hash=hash_password(new_password),
            must_change_password=False,
        )

        current_session = self.sessions.resolve(keep_session_token) if keep_session_token else None
        self.sessions.revoke_all_for_user(
            user.id, except_session_id=current_session.id if current_session else ""
        )

        self.audit.log(
            action="auth.change_password", module="auth", result="success",
            actor_id=user.id, actor_name=user.display_name, actor_roles=user.roles,
            target_type="user", target_id=user.id, ip=ip,
        )

    # ---------------------------------------------------------------- 管理操作
    def set_user_status(self, actor: User, user_id: str, status: str, *, ip: str = "") -> User:
        """启用/停用账号。停用后**立即撤销该用户全部会话**（文档 §3.6）。"""
        target = self.users.get(user_id)
        if target is None:
            raise AuthError("用户不存在")
        if target.id == actor.id and status != STATUS_ACTIVE:
            raise AuthError("不能停用当前登录的账号")

        updated = self.users.update_fields(user_id, status=status)
        if updated is None:
            raise AuthError("用户不存在")

        revoked = 0
        if status != STATUS_ACTIVE:
            revoked = self.sessions.revoke_all_for_user(user_id)

        self.audit.log(
            action="user.set_status", module="system", result="success",
            actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
            target_type="user", target_id=user_id,
            changes={"status": status, "sessions_revoked": revoked}, ip=ip,
        )
        return updated

    def set_user_roles(self, actor: User, user_id: str, roles: list[str], *, ip: str = "") -> User:
        """调整用户角色。角色变化后撤销其会话，避免旧会话保留旧权限（文档 §3.13）。"""
        valid = set(self.users.list_roles().keys()) | set(BUILTIN_ROLE_CODES)
        cleaned = sorted({str(r) for r in roles if str(r) in valid})
        if not cleaned:
            raise AuthError("至少需要分配一个角色")

        # 文档 §4：不能通过编辑角色实现提权 —— 授予的权限不能超出自己拥有的
        actor_perms = self.permissions_of(actor)
        target_perms: set[str] = set()
        role_defs = self.users.list_roles()
        for code in cleaned:
            if "admin" in actor.roles:
                break
            role = role_defs.get(code) or {}
            target_perms |= {str(p) for p in (role.get("permissions") or [])}
        if "admin" not in actor.roles and not target_perms.issubset(actor_perms):
            extra = sorted(target_perms - actor_perms)
            raise AuthError(f"不能授予自己没有的权限：{'、'.join(extra[:5])}")

        before = (self.users.get(user_id) or User("", "", "", "")).roles
        updated = self.users.update_fields(user_id, roles=cleaned)
        if updated is None:
            raise AuthError("用户不存在")

        revoked = self.sessions.revoke_all_for_user(user_id)
        self.audit.log(
            action="user.set_roles", module="system", result="success",
            actor_id=actor.id, actor_name=actor.display_name, actor_roles=actor.roles,
            target_type="user", target_id=user_id,
            changes={"before": before, "after": cleaned, "sessions_revoked": revoked},
            ip=ip,
        )
        return updated
