# -*- coding: utf-8 -*-
"""安全数据备份与恢复（块 1-b）。

## 保护什么

| 内容 | 为什么重要 |
|------|-----------|
| `data/security/*.json` | 账号、角色、会话 —— **丢了没人能登录** |
| `logs/audit/*.jsonl` | 审计日志 —— 事后追溯的唯一依据 |
| `config/settings.json` | 含 API Key 与全部业务配置 |
| `output/batch_*/**/_manifest.json` | 批次清单 —— 记录产出与来源标记 |

**不备份 `output/` 里的图片**：体积大、属不可变产物，且丢失不影响安全与可恢复性。

## 加密（文档 §2.3：不自行设计密码学算法）

- 算法：**AES-256-GCM**（`cryptography` 库，AEAD 同时保证机密性与完整性）
- 密钥（32 字节）来源优先级：
  1. 环境变量 `SHS_BACKUP_KEY`（base64 编码）
  2. 本机保护文件 `data/security/.backup_key`（首次自动生成，权限收紧）
- 每次加密用**全新随机 nonce**（12 字节，GCM 标准长度）
- 文件格式：`[magic 8B][version 2B][nonce 12B][ciphertext||tag]`

> ⚠️ **密钥丢失 = 备份永久无法解密**。生成时会明确提示用户另行保管。

## 快照 vs 备份（两者用途不同，都要有）

| | 明文快照（`store.py`） | 加密备份（本模块） |
|---|---|---|
| 触发 | 每次写安全数据前 | 手动 / 定时 / 变更后 |
| 位置 | 数据文件同级 `.snapshots/` | `backups/` |
| 用途 | **秒级自愈**：文件写坏时恢复 | **离线保管**：误删 / 整体损坏 / 回滚 |
| 加密 | 否（依赖文件系统权限） | **是**（可安全复制到别处） |
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["BackupManager", "BackupError", "BackupInfo", "RestoreResult",
           "MAGIC", "FORMAT_VERSION"]

MAGIC = b"SHSBAK01"          # 8 字节
FORMAT_VERSION = 1
KEY_ENV = "SHS_BACKUP_KEY"
KEY_FILE_REL = Path("data") / "security" / ".backup_key"
BACKUP_DIR_REL = Path("backups")
KEY_BYTES = 32               # AES-256
NONCE_BYTES = 12             # GCM 推荐长度
DEFAULT_KEEP = 10


class BackupError(RuntimeError):
    """备份或恢复失败。"""

    code = "backup_error"


@dataclass
class BackupInfo:
    id: str
    path: Path
    created_at: str
    bytes: int
    encrypted: bool
    label: str = ""
    file_count: int = 0
    sha256: str = ""

    def public(self) -> dict:
        """对外表示（**绝不含密钥或内容**）。"""
        return {
            "id": self.id,
            "file_name": self.path.name,
            "created_at": self.created_at,
            "bytes": self.bytes,
            "size_kb": round(self.bytes / 1024, 1),
            "encrypted": self.encrypted,
            "label": self.label,
            "file_count": self.file_count,
            "sha256": self.sha256[:16],
        }


@dataclass
class RestoreResult:
    ok: bool
    restored_files: list[str] = field(default_factory=list)
    pre_restore_backup: str = ""      # 「还原前备份」的 ID，供回滚
    message: str = ""


# ================================================================ 密钥
def _key_path(root: Path) -> Path:
    return root / KEY_FILE_REL


def load_or_create_key(root: Path) -> tuple[bytes, bool]:
    """获取备份密钥。

    Returns:
        ``(key, created)`` —— created=True 表示本次是新生成的（调用方应提示用户保管）
    """
    env = os.environ.get(KEY_ENV, "").strip()
    if env:
        try:
            raw = base64.b64decode(env, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise BackupError(f"环境变量 {KEY_ENV} 不是合法的 base64") from exc
        if len(raw) != KEY_BYTES:
            raise BackupError(f"环境变量 {KEY_ENV} 解码后应为 {KEY_BYTES} 字节")
        return raw, False

    path = _key_path(root)
    if path.is_file():
        try:
            raw = base64.b64decode(path.read_text(encoding="utf-8").strip(), validate=True)
            if len(raw) == KEY_BYTES:
                return raw, False
        except Exception:  # noqa: BLE001
            pass
        # 密钥文件损坏：不静默覆盖，明确报错（覆盖会导致历史备份全废）
        raise BackupError(
            f"备份密钥文件损坏：{path}。修复或删除它后重试；"
            f"若删除，已有备份将无法解密。"
        )

    key = os.urandom(KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = base64.b64encode(key).decode("ascii")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)
    _harden(path)
    return key, True


def _harden(path: Path) -> None:
    """尽量收紧密钥文件权限（Windows 上退化为只读属性尝试）。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ================================================================ 加密
def _encrypt(plain: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plain, MAGIC)
    return MAGIC + FORMAT_VERSION.to_bytes(2, "big") + nonce + sealed


def _decrypt(blob: bytes, key: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(blob) < len(MAGIC) + 2 + NONCE_BYTES + 16:
        raise BackupError("备份文件过短或已损坏")
    if blob[:len(MAGIC)] != MAGIC:
        raise BackupError("不是本系统生成的备份文件")
    version = int.from_bytes(blob[len(MAGIC):len(MAGIC) + 2], "big")
    if version != FORMAT_VERSION:
        raise BackupError(f"备份格式版本不支持：{version}")
    off = len(MAGIC) + 2
    nonce = blob[off:off + NONCE_BYTES]
    body = blob[off + NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, body, MAGIC)
    except InvalidTag as exc:
        raise BackupError("备份解密失败：密钥不匹配或文件被篡改") from exc


# ================================================================ 管理器
class BackupManager:
    """备份 / 恢复 / 列表。"""

    def __init__(self, root: Path | str | None = None, *, keep: int = DEFAULT_KEEP,
                 include_audit: bool = False):
        if root is None:
            root = Path(__file__).resolve().parent.parent.parent
        self.root = Path(root)
        self.dir = self.root / BACKUP_DIR_REL
        self.keep = max(1, int(keep))
        # 恢复时是否覆盖审计日志。默认 False —— 审计是只追加的事实记录，
        # 回滚它等于销毁"谁做过什么"的历史（实测踩到过，见 _safe_target 注释）。
        self._include_audit = bool(include_audit)

    # ---------------------------------------------------------------- 收集
    def _collect(self) -> list[tuple[Path, str]]:
        """返回 ``[(绝对路径, 归档内相对路径), ...]``。"""
        out: list[tuple[Path, str]] = []

        def add(p: Path, arc: str) -> None:
            try:
                if p.is_file() and p.stat().st_size > 0:
                    out.append((p, arc))
            except OSError:
                pass

        # ① 安全数据
        sec = self.root / "data" / "security"
        if sec.is_dir():
            for p in sorted(sec.glob("*.json")):
                add(p, f"data/security/{p.name}")

        # ② 审计日志
        audit = self.root / "logs" / "audit"
        if audit.is_dir():
            for p in sorted(audit.glob("*.jsonl")):
                add(p, f"logs/audit/{p.name}")

        # ③ 业务配置（含 API Key，因此必须加密）
        add(self.root / "config" / "settings.json", "config/settings.json")

        # ④ 各批次的 manifest（不含图片）
        output = self.root / "output"
        if output.is_dir():
            for p in sorted(output.glob("batch_*/**/_manifest.json")):
                try:
                    rel = p.relative_to(output)
                except ValueError:
                    continue
                add(p, f"output/{rel.as_posix()}")

        return out

    # ---------------------------------------------------------------- 创建
    def create(self, *, label: str = "manual", encrypt: bool = True) -> BackupInfo:
        """创建一份备份。

        Args:
            label: 备注（manual / auto / pre-restore 等）
            encrypt: 是否加密（默认必须加密）
        """
        entries = self._collect()
        # 打包到内存
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for src, arc in entries:
                try:
                    z.write(src, arc)
                except OSError:
                    continue
        plain = buf.getvalue()

        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)

        created_key = False
        if encrypt:
            key, created_key = load_or_create_key(self.root)
            blob = _encrypt(plain, key)
            name = f"backup_{stamp}_{label}.shsbak"
        else:  # pragma: no cover - 仅测试/排障使用
            blob = plain
            name = f"backup_{stamp}_{label}.zip"

        path = self.dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, path)

        info = BackupInfo(
            id=path.stem,
            path=path,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            bytes=len(blob),
            encrypted=encrypt,
            label=label,
            file_count=len(entries),
            sha256=hashlib.sha256(blob).hexdigest(),
        )
        self._prune()
        if created_key:
            # 让调用方能提示用户「密钥已生成，请另行保管」
            setattr(info, "key_created", True)
        return info

    def _prune(self) -> None:
        """保留最近 N 份（按文件名时间戳排序）。"""
        try:
            items = sorted(self.dir.glob("backup_*.*"), key=lambda p: p.name, reverse=True)
        except OSError:
            return
        for old in items[self.keep:]:
            try:
                old.unlink()
            except OSError:
                continue

    # ---------------------------------------------------------------- 列表
    def list(self) -> list[BackupInfo]:
        if not self.dir.is_dir():
            return []
        out: list[BackupInfo] = []
        for p in sorted(self.dir.glob("backup_*.*"), key=lambda x: x.name, reverse=True):
            if p.suffix == ".tmp":
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            # backup_YYYYmmdd_HHMMSS_label.ext
            parts = p.stem.split("_")
            created = ""
            if len(parts) >= 3:
                created = f"{parts[1][:4]}-{parts[1][4:6]}-{parts[1][6:8]} " \
                          f"{parts[2][:2]}:{parts[2][2:4]}:{parts[2][4:6]}"
            label = "_".join(parts[3:]) if len(parts) > 3 else ""
            out.append(BackupInfo(
                id=p.stem, path=p, created_at=created,
                bytes=st.st_size, encrypted=p.suffix == ".shsbak",
                label=label,
            ))
        return out

    def get(self, backup_id: str) -> BackupInfo | None:
        for b in self.list():
            if b.id == backup_id:
                return b
        return None

    def freshness(self) -> dict:
        """备份新鲜度（给 /api/admin/health 用）。"""
        items = self.list()
        if not items:
            return {"count": 0, "latest_at": "", "latest_kb": 0, "age_hours": None,
                    "stale": True}
        latest = items[0]
        age = None
        try:
            ts = time.mktime(time.strptime(latest.created_at, "%Y-%m-%d %H:%M:%S"))
            age = round((time.time() - ts) / 3600, 1)
        except (ValueError, OverflowError):
            pass
        return {
            "count": len(items),
            "latest_at": latest.created_at,
            "latest_kb": round(latest.bytes / 1024, 1),
            "age_hours": age,
            "stale": bool(age is None or age > 24),
        }

    # ---------------------------------------------------------------- 恢复
    def read_archive(self, backup_id: str) -> dict[str, bytes]:
        """解密并读出归档内容（**不落盘**）。"""
        info = self.get(backup_id)
        if info is None:
            raise BackupError(f"备份不存在：{backup_id}")
        blob = info.path.read_bytes()
        plain = _decrypt(blob, load_or_create_key(self.root)[0]) if info.encrypted else blob
        try:
            with zipfile.ZipFile(io.BytesIO(plain)) as z:
                names = [n for n in z.namelist() if not n.endswith("/")]
                return {n: z.read(n) for n in names}
        except zipfile.BadZipFile as exc:
            raise BackupError("备份内容不是有效的压缩包（可能已损坏）") from exc

    def restore(self, backup_id: str, *, confirm: str = "",
                allow_auto_pre_restore: bool = True,
                include_audit: bool | None = None) -> RestoreResult:
        """恢复备份（带二次确认与「还原前备份」）。

        Args:
            backup_id: 目标备份 ID
            confirm: 必须与 ``backup_id`` **完全一致**（防误操作）
            allow_auto_pre_restore: 恢复前自动存一份当前状态，便于回滚
            include_audit: 是否连审计日志一起覆盖。默认为构造时的设置（False）。
                审计是只追加的事实记录，**回滚它会销毁操作历史**，
                因此只有人工排障时才应显式开启。
        """
        if not confirm or confirm != backup_id:
            raise BackupError("二次确认失败：confirm 必须与备份 ID 完全一致")

        info = self.get(backup_id)
        if info is None:
            raise BackupError(f"备份不存在：{backup_id}")

        files = self.read_archive(backup_id)
        if not files:
            raise BackupError("备份内容为空，已中止恢复")

        # ① 还原前备份（回滚点）
        pre_id = ""
        if allow_auto_pre_restore:
            try:
                pre = self.create(label="pre-restore")
                pre_id = pre.id
            except Exception as exc:  # noqa: BLE001
                raise BackupError(f"无法创建「还原前备份」，已中止恢复：{exc}") from exc

        # ② 逐文件原子写回
        prev_audit = self._include_audit
        if include_audit is not None:
            self._include_audit = bool(include_audit)
        try:
            restored: list[str] = []
            skipped_audit = 0
            for arc, data in sorted(files.items()):
                if arc.startswith("logs/audit/") and not self._include_audit:
                    skipped_audit += 1
                    continue
                target = self._safe_target(arc)
                if target is None:
                    continue
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_suffix(target.suffix + ".restore.tmp")
                    tmp.write_bytes(data)
                    os.replace(tmp, target)
                    restored.append(arc)
                except OSError as exc:
                    raise BackupError(
                        f"恢复 {arc} 失败：{exc.__class__.__name__}"
                        f"（已恢复 {len(restored)} 个文件）"
                    ) from exc
        finally:
            self._include_audit = prev_audit

        note = ""
        if skipped_audit:
            note = f"；已跳过 {skipped_audit} 个审计日志文件（审计不参与回滚）"
        return RestoreResult(
            ok=True,
            restored_files=restored,
            pre_restore_backup=pre_id,
            message=f"已恢复 {len(restored)} 个文件" + note
                    + (f"；还原前备份：{pre_id}" if pre_id else ""),
        )

    def _safe_target(self, arc: str) -> Path | None:
        """把归档内路径映射回本机路径，并**拒绝目录穿越**。

        ## 为什么不恢复 `logs/audit/`

        审计日志是**只追加的事实记录**。若恢复时覆盖它，会出现两个问题：

        1. **抹掉恢复点之后的痕迹** —— 用于回滚的备份，反而销毁了"谁在什么时候
           做了恢复"这段历史（实测踩到过）
        2. 违背文档 §8「审计日志防篡改」的要求

        因此默认**不恢复审计日志**（备份里仍然包含它，灾难时可人工取用）。
        需要强制作覆盖时传 ``include_audit=True``（仅供人工排障）。
        """
        arc = arc.replace("\\", "/")
        if arc.startswith("/") or ".." in arc.split("/"):
            return None
        target = (self.root / arc).resolve()
        try:
            target.relative_to(self.root.resolve())
        except ValueError:
            return None

        # 审计日志默认不动
        if arc.startswith("logs/audit/") and not self._include_audit:
            return None

        allowed = ("data/security/", "logs/audit/", "output/")
        if not (arc in ("config/settings.json",) or arc.startswith(allowed)):
            return None
        if not arc.endswith((".json", ".jsonl")):
            return None
        return target
