# -*- coding: utf-8 -*-
"""安全数据的 JSON 持久化 —— **带损坏自愈**。

## 为什么需要自愈

早期版本在解析失败时**静默返回默认值**：

    security.json 被写坏（断电 / 磁盘满 / 进程被杀）
        → json.JSONDecodeError
        → return {"users": {}, "roles": {}}      ← 空字典
        → 【所有账号凭空消失，无人能登录，且没有任何报错】

这比"文件损坏"本身更危险，因为它是**静默**的。

## 现在的行为

    load()
     ├─ 文件不存在        → 返回默认值（首次启动，正常路径）
     ├─ 解析成功          → 返回数据
     └─ 解析失败（损坏）
          ├─ 有可用快照  → 从**最近快照**恢复，记 WARNING，并保留损坏文件证据
          └─ 无快照      → 把损坏文件重命名为 `<name>.corrupt.<时间戳>`
                          记 ERROR，返回默认值
                          绝不静默丢弃

## 快照策略

写文件前，先把**当前内容**存一份快照到 `<name>.snapshots/`，
保留最近 `SNAPSHOT_KEEP` 份。快照是**明文 JSON**（与主文件同级权限），
加密备份由 `backup.py` 负责，两者用途不同：

    · 快照   —— 秒级自愈，防"文件写坏"
    · 加密备份 —— 离线保管，防"误删 / 整体损坏 / 需要回滚"

## 原子写

写临时文件 → fsync → `os.replace`（同分区下原子替换），
避免"写一半被杀"直接产生损坏文件。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

__all__ = ["JsonStore", "StorageFullError", "StorageError"]


class StorageError(RuntimeError):
    """安全数据存储层错误（结构化，供接口层映射为明确提示）。"""

    code = "storage_error"

    def __init__(self, message: str, *, path: str = ""):
        super().__init__(message)
        self.path = path


class StorageFullError(StorageError):
    """磁盘空间不足或写入被拒绝。"""

    code = "storage_full"


# 判定为"空间不足"的错误码（Windows 与 POSIX）
_ENOSPC_CODES = {28, 112}          # errno.ENOSPC / winerror 112
_EDQUOT_CODES = {122}              # 磁盘配额不足


def _is_full_error(exc: OSError) -> bool:
    err = getattr(exc, "errno", None)
    win = getattr(exc, "winerror", None)
    if err in _ENOSPC_CODES or win in _ENOSPC_CODES:
        return True
    if err in _EDQUOT_CODES or win in _EDQUOT_CODES:
        return True
    text = str(exc).lower()
    return "no space" in text or "disk full" in text or "空间不足" in text


class JsonStore:
    """带进程内锁、原子写、快照与损坏自愈的 JSON 存储。"""

    # 保留的快照份数
    SNAPSHOT_KEEP = 5
    # 快照目录后缀
    SNAPSHOT_DIR_SUFFIX = ".snapshots"
    # 损坏文件保存后缀
    CORRUPT_SUFFIX = ".corrupt"

    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: Path | str, default: dict | None = None,
                 *, snapshot: bool = True):
        self.path = Path(path)
        self._default = default if default is not None else {}
        self._snapshot_enabled = snapshot
        # 同一文件在多个实例间共享一把锁（模块级注册表）
        key = str(self.path)
        with JsonStore._locks_guard:
            self._lock = JsonStore._locks.setdefault(key, threading.RLock())

    # ---------------------------------------------------------------- 工具
    def _fresh_default(self) -> dict:
        return json.loads(json.dumps(self._default))

    @property
    def snapshot_dir(self) -> Path:
        return self.path.with_name(self.path.name + self.SNAPSHOT_DIR_SUFFIX)

    # ---------------------------------------------------------------- 读
    def load(self) -> dict:
        """读取内容；**损坏时自动自愈**，绝不静默丢失数据。"""
        with self._lock:
            if not self.path.is_file():
                return self._fresh_default()
            try:
                raw = self.path.read_text(encoding="utf-8")
            except OSError as exc:
                # 读不了：可能是权限或磁盘问题 —— 不掩盖，但不崩
                if _is_full_error(exc):
                    raise StorageFullError("磁盘空间不足，无法读取安全数据",
                                           path=str(self.path)) from exc
                return self._fresh_default()

            if not raw.strip():
                # 空文件：等同首次启动（保存过程中被中断会留下空文件）
                return self._fresh_default()

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = self._heal_corrupted(raw)

            if not isinstance(data, dict):
                data = self._fresh_default()
            for k, v in self._default.items():
                data.setdefault(k, json.loads(json.dumps(v)))
            return data

    def _heal_corrupted(self, raw: str) -> dict:
        """损坏自愈：优先用快照恢复，否则保留证据并返回默认值。

        ⚠️ 无论走哪条分支都**必须留下痕迹**（日志 + 文件），
        不能像早期版本那样静默返回空字典。
        """
        import logging

        log = logging.getLogger("app.security.store")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        evidence = self.path.with_name(f"{self.path.name}{self.CORRUPT_SUFFIX}.{stamp}")

        # ① 保留损坏文件原文（供人工排查）
        try:
            evidence.write_text(raw, encoding="utf-8")
        except OSError:
            evidence = None  # type: ignore[assignment]

        # ② 尝试从最近快照恢复
        recovered = self._load_latest_snapshot()
        if recovered is not None:
            log.warning(
                "安全数据文件已损坏，已从快照恢复：%s（损坏文件留存于 %s）",
                self.path.name, evidence.name if evidence else "（无法保存）",
            )
            # 立即把恢复的内容写回主文件，让后续读取走正常路径
            try:
                self._write_atomic(json.dumps(recovered, ensure_ascii=False, indent=2) + "\n")
            except StorageError:
                pass
            return recovered

        log.error(
            "安全数据文件已损坏且无可用快照：%s（损坏文件留存于 %s）",
            self.path.name, evidence.name if evidence else "（无法保存）",
        )
        return self._fresh_default()

    def _load_latest_snapshot(self) -> dict | None:
        """取最近一份可解析的快照。"""
        if not self._snapshot_enabled:
            return None
        d = self.snapshot_dir
        if not d.is_dir():
            return None
        try:
            snaps = sorted(
                (p for p in d.glob("*.json") if p.is_file()),
                key=lambda p: p.name, reverse=True,
            )
        except OSError:
            return None
        for p in snaps:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                return data
        return None

    # ---------------------------------------------------------------- 写
    def _write_atomic(self, payload: str) -> None:
        # ⚠️ mkdir 也必须在 try 内：父目录被占或盘满时它同样会抛 OSError，
        #    留在 try 外会让调用方收到裸异常（无 code 属性，前端无法分类提示）。
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
            )
        except OSError as exc:
            if _is_full_error(exc):
                raise StorageFullError("磁盘空间不足，无法写入安全数据",
                                       path=str(self.path)) from exc
            raise StorageError(f"无法创建临时文件：{exc.__class__.__name__}",
                               path=str(self.path)) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except OSError as exc:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            if _is_full_error(exc):
                raise StorageFullError("磁盘空间不足，安全数据未写入（原文件保持不变）",
                                       path=str(self.path)) from exc
            raise StorageError(f"写入安全数据失败：{exc.__class__.__name__}",
                               path=str(self.path)) from exc

    def _take_snapshot(self) -> None:
        """写前快照：保留当前（尚未被覆盖的）内容。"""
        if not self._snapshot_enabled or not self.path.is_file():
            return
        try:
            d = self.snapshot_dir
            d.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
            shutil.copy2(self.path, d / f"{stamp}.json")
            self._prune_snapshots(d)
        except OSError:
            # 快照失败不能阻断主流程（主流程已有原子写保护）
            pass

    def _prune_snapshots(self, d: Path) -> None:
        try:
            snaps = sorted(d.glob("*.json"), key=lambda p: p.name, reverse=True)
        except OSError:
            return
        for old in snaps[self.SNAPSHOT_KEEP:]:
            try:
                old.unlink()
            except OSError:
                continue

    def save(self, data: dict) -> None:
        """原子写入（写前先快照）。"""
        with self._lock:
            self._take_snapshot()
            payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            self._write_atomic(payload)

    # ---------------------------------------------------------------- 事务
    def update(self, mutator) -> dict:
        """读 → 改 → 写，全程持锁。"""
        with self._lock:
            data = self.load()
            result = mutator(data)
            if isinstance(result, dict):
                data = result
            self.save(data)
            return data

    # ---------------------------------------------------------------- 工具
    def exists(self) -> bool:
        return self.path.is_file()

    def snapshots(self) -> list[Path]:
        d = self.snapshot_dir
        if not d.is_dir():
            return []
        return sorted(d.glob("*.json"), key=lambda p: p.name, reverse=True)
