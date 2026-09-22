# -*- coding: utf-8 -*-
"""用户模型与存储（文档 §3 账号与登录安全）。

## 关键设计

| 文档要求 | 实现 |
|---------|------|
| §3.1 稳定且不可变内部 ID | `id` 用 UUID4，创建后永不改变；改名不影响 |
| §3.2 区分内部ID/登录名/显示名 | 三个独立字段 |
| §3.3 登录名规范化防同形账号 | NFKC + 去空白 + 转小写；唯一性按**规范化后**判定 |
| §3.4 显示姓名不作为身份凭据 | 权限只认 `id`，不认 display_name |
| §3.5 账号状态机 | pending / active / frozen / disabled |
| §3.6 禁用立即生效 | `is_active()` 每次请求都查当前状态 |
"""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .store import JsonStore

__all__ = [
    "User",
    "UserStore",
    "normalize_login_name",
    "validate_login_name",
    "UsernameError",
]


class UsernameError(ValueError):
    """登录名不符合规范。"""


# 账号状态（文档 §3.5）
STATUS_PENDING = "pending"      # 待激活
STATUS_ACTIVE = "active"        # 正常
STATUS_FROZEN = "frozen"        # 冻结（可解冻）
STATUS_DISABLED = "disabled"    # 离职停用（不可自行恢复）

VALID_STATUSES = frozenset({STATUS_PENDING, STATUS_ACTIVE, STATUS_FROZEN, STATUS_DISABLED})

# 允许的登录名字符：字母、数字、下划线、连字符、点（规范化后）
_LOGIN_ALLOWED = re.compile(r"^[a-z0-9._-]{3,32}$")
# 连续分隔符会让人误读（如 a..b、a--b）
_LOGIN_UGLY = re.compile(r"[._-]{2,}")


def normalize_login_name(raw: str) -> str:
    """登录名规范化（文档 §3.3）。

    处理链：
      1. **NFKC 规范化** —— 全角 `ａｄｍｉｎ` 与半角 `admin` 归一（防同形账号）
      2. 去除**所有**空白（含首尾与中间，避免 `ad min` 与 `admin` 被当成两个账号）
      3. 转小写（登录名大小写不敏感）

    Returns:
        规范化后的登录名
    """
    s = unicodedata.normalize("NFKC", str(raw or ""))
    s = re.sub(r"\s+", "", s)
    # 常见同形字符手工归一（NFKC 不覆盖全部）
    s = s.replace("０", "0").replace("１", "1").replace("ｌ", "l").replace("Ｉ", "i")
    return s.lower()


def validate_login_name(raw: str) -> str:
    """校验并返回规范化登录名。

    Raises:
        UsernameError: 不符合规范
    """
    normalized = normalize_login_name(raw)
    if not normalized:
        raise UsernameError("登录名不能为空")
    if not _LOGIN_ALLOWED.match(normalized):
        raise UsernameError(
            "登录名只能包含小写字母、数字、下划线、连字符和点，长度 3~32"
        )
    if _LOGIN_UGLY.search(normalized):
        raise UsernameError("登录名中不能出现连续的分隔符")
    if normalized.startswith((".", "_", "-")) or normalized.endswith((".", "_", "-")):
        raise UsernameError("登录名不能以分隔符开头或结尾")
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class User:
    id: str
    login_name: str          # 规范化后
    display_name: str
    password_hash: str
    status: str = STATUS_ACTIVE
    roles: list[str] = field(default_factory=list)
    mfa_secret: str = ""            # 空 = 未启用 MFA
    mfa_enabled: bool = False
    must_change_password: bool = False
    created_at: str = ""
    updated_at: str = ""
    last_login_at: str = ""
    failed_attempts: int = 0
    locked_until: str = ""

    # ---------------------------------------------------------------- 序列化
    def to_dict(self, *, include_secret: bool = False) -> dict:
        data = {
            "id": self.id,
            "login_name": self.login_name,
            "display_name": self.display_name,
            "password_hash": self.password_hash,
            "status": self.status,
            "roles": list(self.roles),
            "mfa_enabled": self.mfa_enabled,
            "must_change_password": self.must_change_password,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_login_at": self.last_login_at,
            "failed_attempts": self.failed_attempts,
            "locked_until": self.locked_until,
        }
        if include_secret:
            data["mfa_secret"] = self.mfa_secret
        return data

    def public(self) -> dict:
        """对外返回（**绝不含 password_hash / mfa_secret**，文档 §3.7）。"""
        return {
            "id": self.id,
            "login_name": self.login_name,
            "display_name": self.display_name,
            "status": self.status,
            "roles": list(self.roles),
            "mfa_enabled": self.mfa_enabled,
            "must_change_password": self.must_change_password,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "User":
        return cls(
            id=str(d.get("id") or ""),
            login_name=str(d.get("login_name") or ""),
            display_name=str(d.get("display_name") or ""),
            password_hash=str(d.get("password_hash") or ""),
            status=str(d.get("status") or STATUS_ACTIVE),
            roles=[str(r) for r in (d.get("roles") or [])],
            mfa_secret=str(d.get("mfa_secret") or ""),
            mfa_enabled=bool(d.get("mfa_enabled")),
            must_change_password=bool(d.get("must_change_password")),
            created_at=str(d.get("created_at") or ""),
            updated_at=str(d.get("updated_at") or ""),
            last_login_at=str(d.get("last_login_at") or ""),
            failed_attempts=int(d.get("failed_attempts") or 0),
            locked_until=str(d.get("locked_until") or ""),
        )

    # ---------------------------------------------------------------- 状态
    def is_active(self) -> bool:
        """是否允许登录/继续使用（文档 §3.5/§3.6）。

        注意：**每次请求都要重新查**，不能缓存在会话里 ——
        否则停用账号后旧会话仍能继续用。
        """
        return self.status == STATUS_ACTIVE

    def is_locked(self) -> bool:
        if not self.locked_until:
            return False
        try:
            until = datetime.fromisoformat(self.locked_until)
        except ValueError:
            return False
        now = datetime.now(until.tzinfo) if until.tzinfo else datetime.now()
        return now < until


class UserStore:
    """用户与角色的 JSON 存储。

    数据结构（`data/security/security.json`）：

        {
          "version": 1,
          "users": { "<user_id>": {...} },
          "roles": { "<role_code>": {"name":..., "permissions":[...]} }
        }
    """

    DEFAULT_REL = Path("data") / "security" / "security.json"

    def __init__(self, path: Path | str | None = None):
        if path is None:
            # 相对项目根（app/security/users.py → 上溯三层）
            root = Path(__file__).resolve().parent.parent.parent
            path = root / self.DEFAULT_REL
        self._store = JsonStore(path, default={"version": 1, "users": {}, "roles": {}})

    @classmethod
    def default(cls) -> "UserStore":
        return cls()

    @property
    def path(self) -> Path:
        return self._store.path

    # ---------------------------------------------------------------- 查询
    def _load(self) -> dict:
        data = self._store.load()
        data.setdefault("users", {})
        data.setdefault("roles", {})
        return data

    def has_any_user(self) -> bool:
        return bool(self._load().get("users"))

    def count(self) -> int:
        return len(self._load().get("users") or {})

    def list_users(self) -> list[User]:
        users = self._load().get("users") or {}
        return sorted(
            (User.from_dict(v) for v in users.values()),
            key=lambda u: u.created_at,
        )

    def get(self, user_id: str) -> User | None:
        raw = (self._load().get("users") or {}).get(str(user_id or ""))
        return User.from_dict(raw) if isinstance(raw, dict) else None

    def get_by_login(self, login_name: str) -> User | None:
        """按**规范化后**的登录名查找（文档 §3.3）。"""
        target = normalize_login_name(login_name)
        if not target:
            return None
        for u in self.list_users():
            if u.login_name == target:
                return u
        return None

    def login_exists(self, login_name: str) -> bool:
        return self.get_by_login(login_name) is not None

    # ---------------------------------------------------------------- 写入
    def create(
        self,
        login_name: str,
        display_name: str,
        password_hash: str,
        *,
        roles: list[str] | None = None,
        status: str = STATUS_ACTIVE,
        must_change_password: bool = False,
        mfa_secret: str = "",
    ) -> User:
        """创建用户。登录名按规范化后判重。"""
        normalized = validate_login_name(login_name)
        if not display_name or not str(display_name).strip():
            raise UsernameError("显示姓名不能为空")
        if status not in VALID_STATUSES:
            raise UsernameError(f"未知账号状态：{status}")

        now = _now()
        user = User(
            id=str(uuid.uuid4()),          # §3.1 稳定不可变 ID
            login_name=normalized,
            display_name=str(display_name).strip()[:64],
            password_hash=password_hash,
            status=status,
            roles=list(roles or []),
            mfa_secret=mfa_secret,
            mfa_enabled=False,
            must_change_password=must_change_password,
            created_at=now,
            updated_at=now,
        )

        def mutate(data: dict) -> None:
            users = data.setdefault("users", {})
            # 唯一性：按规范化登录名判重
            for existing in users.values():
                if normalize_login_name(existing.get("login_name", "")) == normalized:
                    raise UsernameError(f"登录名已存在：{normalized}")
            users[user.id] = user.to_dict(include_secret=True)

        self._store.update(mutate)
        return user

    def update(self, user: User) -> User:
        """整条更新（调用方负责先取到最新对象）。"""
        user.updated_at = _now()

        def mutate(data: dict) -> None:
            users = data.setdefault("users", {})
            if user.id not in users:
                raise UsernameError("用户不存在")
            users[user.id] = user.to_dict(include_secret=True)

        self._store.update(mutate)
        return user

    def update_fields(self, user_id: str, **fields) -> User | None:
        """原子更新若干字段。"""
        holder: dict = {}

        def mutate(data: dict) -> None:
            users = data.setdefault("users", {})
            raw = users.get(str(user_id))
            if not isinstance(raw, dict):
                return
            u = User.from_dict(raw)
            for k, v in fields.items():
                if hasattr(u, k):
                    setattr(u, k, v)
            u.updated_at = _now()
            users[u.id] = u.to_dict(include_secret=True)
            holder["user"] = u

        self._store.update(mutate)
        return holder.get("user")

    def delete(self, user_id: str) -> bool:
        removed = {"ok": False}

        def mutate(data: dict) -> None:
            users = data.setdefault("users", {})
            if str(user_id) in users:
                users.pop(str(user_id))
                removed["ok"] = True

        self._store.update(mutate)
        return removed["ok"]

    # ---------------------------------------------------------------- 角色存储
    def list_roles(self) -> dict:
        return dict(self._load().get("roles") or {})

    def save_role(self, code: str, name: str, permissions: list[str], description: str = "") -> None:
        def mutate(data: dict) -> None:
            roles = data.setdefault("roles", {})
            roles[str(code)] = {
                "name": str(name),
                "description": str(description),
                "permissions": sorted(set(str(p) for p in permissions)),
            }

        self._store.update(mutate)

    def delete_role(self, code: str) -> bool:
        removed = {"ok": False}

        def mutate(data: dict) -> None:
            roles = data.setdefault("roles", {})
            if str(code) in roles:
                roles.pop(str(code))
                removed["ok"] = True

        self._store.update(mutate)
        return removed["ok"]

    # ---------------------------------------------------------------- 迁移（Q4）
    def claim_legacy_data(self, admin_user_id: str) -> int:
        """把历史批次的 owner 指向首个管理员（文档 Q4 决策）。

        Returns:
            处理的批次数量
        """
        root = self.path.parent.parent.parent  # 项目根
        batch_root = root / "output"
        if not batch_root.is_dir():
            return 0
        touched = 0
        for manifest in batch_root.glob("batch_*/**/_manifest.json"):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("owner_user_id"):
                continue
            data["owner_user_id"] = admin_user_id
            data["owner_claimed"] = True
            try:
                manifest.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                touched += 1
            except OSError:
                continue
        return touched
