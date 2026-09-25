# -*- coding: utf-8 -*-
"""认证核心：密码哈希、会话管理、多因素认证（文档 §3）。

## 依赖选择（文档 §2.3「尽量复用成熟的认证和权限组件，不自行设计密码学算法」）

| 用途 | 库 | 说明 |
|------|----|------|
| 密码哈希 | `argon2-cffi` | argon2id，2015 年密码哈希竞赛冠军 |
| MFA | `pyotp` | TOTP（RFC 6238），兼容 Google Authenticator |
| 随机数 | `secrets` | 标准库 CSPRNG |

## 会话安全（文档 §3.10~3.13）

- 会话 ID 用 `secrets.token_urlsafe(32)`（256 bit 熵）
- **登录成功后轮换会话 ID**（防会话固定攻击）
- 空闲超时 + 绝对有效期**双重限制**
- 存储**仅保留 token 的 SHA-256**，数据库泄露也无法直接冒用
- 停用账号 / 改密码 / 改角色 → **立即撤销该用户全部会话**
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
import pyotp

from .store import JsonStore

__all__ = [
    "AuthError",
    "hash_password",
    "verify_password",
    "needs_rehash",
    "SessionStore",
    "Session",
    "generate_totp_secret",
    "verify_totp",
    "totp_uri",
    "password_strength_error",
]

# ---------------------------------------------------------------- 密码
# argon2id 默认参数（RFC 9106 推荐档），单次约 50ms
_hasher = PasswordHasher(
    time_cost=2,        # 迭代次数
    memory_cost=65536,  # 64 MiB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

MIN_PASSWORD_LENGTH = 10
# 常见弱口令（小样本，够用即可；完整字典应外置）
_WEAK_PASSWORDS = {
    "password", "password1", "12345678", "123456789", "1234567890",
    "qwertyuiop", "admin12345", "administrator", "iloveyou123",
    "abc123456", "1111111111", "0000000000", "a123456789",
}


class AuthError(RuntimeError):
    """认证相关错误。"""


def password_strength_error(password: str, login_name: str = "") -> str:
    """检查密码强度，返回错误说明（空字符串 = 通过）。

    文档未规定具体策略，这里取「长度 + 复杂度 + 弱口令 + 与登录名相关性」四项。
    """
    pw = str(password or "")
    if len(pw) < MIN_PASSWORD_LENGTH:
        return f"密码至少需要 {MIN_PASSWORD_LENGTH} 位"
    if pw.lower() in _WEAK_PASSWORDS:
        return "该密码过于常见，请更换"
    if login_name and login_name.lower() in pw.lower():
        return "密码不能包含登录名"
    kinds = sum([
        any(c.islower() for c in pw),
        any(c.isupper() for c in pw),
        any(c.isdigit() for c in pw),
        any(not c.isalnum() for c in pw),
    ])
    if kinds < 3:
        return "密码需包含大写字母、小写字母、数字、符号中的至少三类"
    return ""


def hash_password(password: str) -> str:
    """生成 argon2id 哈希（文档 §3.7：不可明文保存、查询或恢复）。"""
    return _hasher.hash(str(password))


def verify_password(password: str, stored_hash: str) -> bool:
    """校验密码。失败一律返回 False，不区分原因（防枚举）。"""
    if not stored_hash:
        return False
    try:
        return _hasher.verify(stored_hash, str(password))
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False
    except Exception:  # noqa: BLE001 - 参数不匹配等异常一律视为验证失败
        return False


def needs_rehash(stored_hash: str) -> bool:
    """哈希参数是否已过时（升级 argon2 参数后自动迁移）。"""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- MFA
def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, login_name: str, issuer: str = "AI图片生成") -> str:
    """生成 otpauth:// URI（供认证器 App 扫描）。"""
    return pyotp.TOTP(secret).provisioning_uri(name=login_name, issuer_name=issuer)


def verify_totp(secret: str, code: str, valid_window: int = 1) -> bool:
    """校验 TOTP 验证码。valid_window=1 容忍前后各 30 秒时钟偏差。"""
    if not secret or not code:
        return False
    cleaned = "".join(ch for ch in str(code) if ch.isdigit())
    if len(cleaned) != 6:
        return False
    try:
        return pyotp.TOTP(secret).verify(cleaned, valid_window=valid_window)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 会话
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def _hash_token(token: str) -> str:
    """只存 token 的哈希 —— 即使存储泄露也无法直接冒用会话。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Session:
    id: str                 # 会话 ID（对外可见，用于「我的设备」列表）
    token_hash: str         # token 的 SHA-256
    user_id: str
    created_at: str
    last_seen_at: str
    expires_at: str         # 绝对有效期
    ip: str = ""
    user_agent: str = ""
    revoked_at: str = ""
    mfa_verified: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "token_hash": self.token_hash,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "expires_at": self.expires_at,
            "ip": self.ip,
            "user_agent": self.user_agent,
            "revoked_at": self.revoked_at,
            "mfa_verified": self.mfa_verified,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            id=str(d.get("id") or ""),
            token_hash=str(d.get("token_hash") or ""),
            user_id=str(d.get("user_id") or ""),
            created_at=str(d.get("created_at") or ""),
            last_seen_at=str(d.get("last_seen_at") or ""),
            expires_at=str(d.get("expires_at") or ""),
            ip=str(d.get("ip") or ""),
            user_agent=str(d.get("user_agent") or "")[:200],
            revoked_at=str(d.get("revoked_at") or ""),
            mfa_verified=bool(d.get("mfa_verified")),
        )

    def public(self) -> dict:
        """给「我的登录设备」用（不含 token）。"""
        return {
            "id": self.id,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
            "ip": self.ip,
            "user_agent": self.user_agent,
            "current": False,
        }

    def is_expired(self, *, idle_minutes: int) -> bool:
        now = _now_utc()
        try:
            expires = datetime.fromisoformat(self.expires_at)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if now >= expires:
                return True
            last = datetime.fromisoformat(self.last_seen_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last >= timedelta(minutes=idle_minutes):
                return True
        except (ValueError, TypeError):
            return True
        return False


class SessionStore:
    """会话存储（`data/security/sessions.json`）。

    文档 §3.10：空闲超时 + 绝对有效期；
    文档 §3.11：安全的 Cookie 属性由调用方设置。
    """

    DEFAULT_REL = Path("data") / "security" / "sessions.json"

    # 空闲超时（分钟）与绝对有效期（小时）
    IDLE_TIMEOUT_MINUTES = 60
    ABSOLUTE_TIMEOUT_HOURS = 12

    def __init__(self, path: Path | str | None = None):
        if path is None:
            root = Path(__file__).resolve().parent.parent.parent
            path = root / self.DEFAULT_REL
        self._store = JsonStore(path, default={"version": 1, "sessions": {}})

    @classmethod
    def default(cls) -> "SessionStore":
        return cls()

    @property
    def path(self) -> Path:
        return self._store.path

    def _load(self) -> dict:
        data = self._store.load()
        data.setdefault("sessions", {})
        return data

    # ---------------------------------------------------------------- 创建
    def create(
        self,
        user_id: str,
        *,
        ip: str = "",
        user_agent: str = "",
        mfa_verified: bool = False,
    ) -> tuple[str, Session]:
        """创建会话。

        Returns:
            (明文 token, Session) —— **token 只在此刻返回一次**，之后无法取回。
        """
        token = secrets.token_urlsafe(32)
        now = _now_utc()
        session = Session(
            id=secrets.token_hex(8),
            token_hash=_hash_token(token),
            user_id=str(user_id),
            created_at=_iso(now),
            last_seen_at=_iso(now),
            expires_at=_iso(now + timedelta(hours=self.ABSOLUTE_TIMEOUT_HOURS)),
            ip=str(ip or "")[:64],
            user_agent=str(user_agent or "")[:200],
            mfa_verified=mfa_verified,
        )

        def mutate(data: dict) -> None:
            data.setdefault("sessions", {})[session.id] = session.to_dict()

        self._store.update(mutate)
        return token, session

    # ---------------------------------------------------------------- 校验
    def resolve(self, token: str) -> Session | None:
        """按 token 找有效会话；顺带更新 last_seen（滑动续期）。"""
        if not token:
            return None
        target = _hash_token(token)
        found: dict = {}

        def mutate(data: dict) -> None:
            sessions = data.setdefault("sessions", {})
            for sid, raw in list(sessions.items()):
                if not isinstance(raw, dict):
                    continue
                if not hmac.compare_digest(str(raw.get("token_hash") or ""), target):
                    continue
                s = Session.from_dict(raw)
                if s.revoked_at or s.is_expired(idle_minutes=self.IDLE_TIMEOUT_MINUTES):
                    # 过期/已撤销：顺手清理，避免文件无限增长
                    sessions.pop(sid, None)
                    return
                s.last_seen_at = _iso(_now_utc())
                sessions[sid] = s.to_dict()
                found["session"] = s
                return

        self._store.update(mutate)
        return found.get("session")

    def get(self, session_id: str) -> Session | None:
        raw = (self._load().get("sessions") or {}).get(str(session_id or ""))
        return Session.from_dict(raw) if isinstance(raw, dict) else None

    # ---------------------------------------------------------------- 撤销
    def revoke(self, session_id: str) -> bool:
        """撤销单个会话（用户「退出该设备」）。"""
        done = {"ok": False}

        def mutate(data: dict) -> None:
            sessions = data.setdefault("sessions", {})
            raw = sessions.get(str(session_id))
            if isinstance(raw, dict) and not raw.get("revoked_at"):
                raw["revoked_at"] = _iso(_now_utc())
                sessions[str(session_id)] = raw
                done["ok"] = True

        self._store.update(mutate)
        return done["ok"]

    def revoke_all_for_user(self, user_id: str, *, except_session_id: str = "") -> int:
        """撤销某用户的全部会话（文档 §3.6/§3.13）。

        用于：账号停用、改密码、改角色、管理员强制下线。
        """
        count = {"n": 0}
        now = _iso(_now_utc())

        def mutate(data: dict) -> None:
            sessions = data.setdefault("sessions", {})
            for sid, raw in sessions.items():
                if not isinstance(raw, dict):
                    continue
                if str(raw.get("user_id")) != str(user_id):
                    continue
                if except_session_id and sid == except_session_id:
                    continue
                if raw.get("revoked_at"):
                    continue
                raw["revoked_at"] = now
                count["n"] += 1

        self._store.update(mutate)
        return count["n"]

    def list_for_user(self, user_id: str) -> list[Session]:
        """列出某用户当前有效的会话（「我的登录设备」）。"""
        out: list[Session] = []
        for raw in (self._load().get("sessions") or {}).values():
            if not isinstance(raw, dict):
                continue
            if str(raw.get("user_id")) != str(user_id):
                continue
            s = Session.from_dict(raw)
            if s.revoked_at or s.is_expired(idle_minutes=self.IDLE_TIMEOUT_MINUTES):
                continue
            out.append(s)
        return sorted(out, key=lambda s: s.last_seen_at, reverse=True)

    def list_all(self) -> list[Session]:
        """列出全部有效会话（管理员会话监控，文档 §7.5）。"""
        out: list[Session] = []
        for raw in (self._load().get("sessions") or {}).values():
            if not isinstance(raw, dict):
                continue
            s = Session.from_dict(raw)
            if s.revoked_at or s.is_expired(idle_minutes=self.IDLE_TIMEOUT_MINUTES):
                continue
            out.append(s)
        return sorted(out, key=lambda s: s.last_seen_at, reverse=True)

    def cleanup(self) -> int:
        """清理过期与已撤销的会话记录。"""
        removed = {"n": 0}

        def mutate(data: dict) -> None:
            sessions = data.setdefault("sessions", {})
            for sid, raw in list(sessions.items()):
                if not isinstance(raw, dict):
                    sessions.pop(sid, None)
                    removed["n"] += 1
                    continue
                s = Session.from_dict(raw)
                if s.revoked_at or s.is_expired(idle_minutes=self.IDLE_TIMEOUT_MINUTES):
                    sessions.pop(sid, None)
                    removed["n"] += 1

        self._store.update(mutate)
        return removed["n"]
