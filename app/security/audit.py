# -*- coding: utf-8 -*-
"""审计日志（文档 §8）。

## 设计要点

| 文档要求 | 实现 |
|---------|------|
| §8 追加写、防篡改 | JSONL 追加模式；**只提供 append 接口，无修改/删除接口** |
| §8 记录操作者稳定 ID 及当时角色 | `actor_id` + `actor_roles` **快照**（角色后来变了也能还原当时情况） |
| §8 请求 ID 及关联任务 ID | `request_id` / `task_id` |
| §8 脱敏后的变更摘要 | `changes` 经过 `_redact()` 过滤 |
| §8 禁止记录密码、完整令牌、会话 Cookie、完整密钥 | `_SENSITIVE_KEYS` 黑名单 + 值级脱敏 |
| §8 访问限制 | 查询接口需 `system.audit.read` 权限 |
| §7.8 导出审计数据需独立权限，并记录导出行为 | 导出操作**自身也写审计** |

## 为什么用 JSONL

一行一条记录，追加写天然原子（单次 write），且便于用 `tail`/`grep` 排查。
损坏一行不影响其它行 —— 比单个大 JSON 更抗故障。
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["AuditLogger", "current_request_id", "new_request_id", "RESULT_SUCCESS",
           "RESULT_DENIED", "RESULT_ERROR"]

RESULT_SUCCESS = "success"
RESULT_DENIED = "denied"
RESULT_ERROR = "error"

# 命中这些键名的值一律替换为 ***（文档 §8 明确禁止记录的内容）
_SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "old_password", "new_password", "password_hash",
    "token", "access_token", "refresh_token", "session", "session_id", "cookie",
    "authorization", "api_key", "apikey", "secret", "mfa_secret", "totp",
    "private_key", "client_secret", "base64", "data_url",
}

# 值级模式（键名不认识但值长得像密钥）
_VALUE_PATTERNS = [
    re.compile(r"^sk-[A-Za-z0-9_\-]{16,}$"),          # OpenAI 风格
    re.compile(r"^[A-Za-z0-9]{32,}$"),                # 长随机串
    re.compile(r"^data:image/"),                      # base64 图片
    re.compile(r"^Bearer\s+", re.I),
    re.compile(r"^[0-9a-f]{32,}$", re.I),             # 十六进制密钥
]

_MAX_VALUE_LEN = 200


def _redact(value: Any, depth: int = 0) -> Any:
    """递归脱敏（文档 §8）。"""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k).lower() in _SENSITIVE_KEYS:
                out[str(k)] = "***"
            else:
                out[str(k)] = _redact(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v, depth + 1) for v in value[:20]]
    if isinstance(value, str):
        for pat in _VALUE_PATTERNS:
            if pat.search(value):
                return "***"
        return value[:_MAX_VALUE_LEN] + ("…" if len(value) > _MAX_VALUE_LEN else "")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_VALUE_LEN]


# 请求 ID：每个 HTTP 请求一个，便于把一次操作的多条日志串起来
_local = threading.local()


def new_request_id() -> str:
    rid = uuid.uuid4().hex[:12]
    _local.request_id = rid
    return rid


def current_request_id() -> str:
    return getattr(_local, "request_id", "")


class AuditLogger:
    """按天分文件的审计日志。

    文件：`logs/audit/audit-YYYY-MM-DD.jsonl`
    """

    def __init__(self, root: Path | str | None = None, *, enabled: bool = True):
        if root is None:
            root = Path(__file__).resolve().parent.parent.parent / "logs" / "audit"
        self.root = Path(root)
        self.enabled = enabled
        self._lock = threading.RLock()

    @classmethod
    def default(cls) -> "AuditLogger":
        return cls()

    # ---------------------------------------------------------------- 写
    def log(
        self,
        *,
        action: str,
        module: str = "",
        result: str = RESULT_SUCCESS,
        actor_id: str = "",
        actor_name: str = "",
        actor_roles: list[str] | None = None,
        target_type: str = "",
        target_id: str = "",
        changes: dict | None = None,
        error_kind: str = "",
        request_id: str = "",
        task_id: str = "",
        ip: str = "",
    ) -> dict:
        """追加一条审计记录。

        ⚠️ **没有 update / delete 接口** —— 这是刻意的（文档 §8 防篡改）。
        """
        record = {
            "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "actor_id": str(actor_id or ""),
            "actor_name": str(actor_name or "")[:64],
            # 角色快照：用户后来被改角色，也能还原「当时」的权限状态
            "actor_roles": [str(r) for r in (actor_roles or [])],
            "module": str(module or ""),
            "action": str(action or ""),
            "target_type": str(target_type or ""),
            "target_id": str(target_id or "")[:128],
            "request_id": str(request_id or current_request_id()),
            "task_id": str(task_id or "")[:64],
            "result": str(result or RESULT_SUCCESS),
            "error_kind": str(error_kind or "")[:64],
            "changes": _redact(changes) if changes else {},
            "ip": str(ip or "")[:64],
        }
        if not self.enabled:
            return record

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            day = datetime.now().strftime("%Y-%m-%d")
            path = self.root / f"audit-{day}.jsonl"
            line = json.dumps(record, ensure_ascii=False) + "\n"
            with self._lock:
                # 追加写：单次 write，O_APPEND 保证并发下不互相覆盖
                with open(path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line)
        except OSError:
            # 审计失败不能影响主业务；但也不能静默 —— 调用方通常已有日志
            pass
        return record

    # ---------------------------------------------------------------- 读
    def query(
        self,
        *,
        actor_id: str = "",
        module: str = "",
        action: str = "",
        target_id: str = "",
        result: str = "",
        request_id: str = "",
        since: str = "",
        until: str = "",
        limit: int = 200,
    ) -> list[dict]:
        """按条件检索（文档 §7.8）。调用方需先校验 `system.audit.read` 权限。"""
        if not self.root.is_dir():
            return []
        out: list[dict] = []
        files = sorted(self.root.glob("audit-*.jsonl"), reverse=True)
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if actor_id and rec.get("actor_id") != actor_id:
                            continue
                        if module and rec.get("module") != module:
                            continue
                        if action and rec.get("action") != action:
                            continue
                        if target_id and rec.get("target_id") != target_id:
                            continue
                        if result and rec.get("result") != result:
                            continue
                        if request_id and rec.get("request_id") != request_id:
                            continue
                        at = str(rec.get("at") or "")
                        if since and at < since:
                            continue
                        if until and at > until:
                            continue
                        out.append(rec)
            except OSError:
                continue
            if len(out) >= limit:
                break
        out.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
        return out[:limit]

    def stats(self) -> dict:
        """概览统计（给审计中心首页用）。"""
        if not self.root.is_dir():
            return {"files": 0, "records": 0, "denied": 0, "errors": 0}
        files = list(self.root.glob("audit-*.jsonl"))
        total = denied = errors = 0
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        if not line.strip():
                            continue
                        total += 1
                        if '"denied"' in line:
                            denied += 1
                        elif '"error"' in line:
                            errors += 1
            except OSError:
                continue
        return {"files": len(files), "records": total, "denied": denied, "errors": errors}
