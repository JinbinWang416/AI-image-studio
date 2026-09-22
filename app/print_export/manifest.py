# -*- coding: utf-8 -*-
"""印刷导出：manifest 读写。

两个不同的 manifest，别搞混：

| 文件 | 归属 | 内容 |
|------|------|------|
| `印刷TIF/print_manifest.json` | **本模块** | 单张图的印刷参数（尺寸/DPI/ICC/出血/图层） |
| `<门店>/_manifest.json` | **既有批次快照** | 追加一个 `print_export` 字段记录导出状态 |

⚠️ 写批次 `_manifest.json` 时必须**只增不改**：读出 → 加字段 → 原子写回，
   绝不覆盖或删除既有键（AGENTS.md：批次 manifest 不可破坏）。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

__all__ = [
    "PRINT_VERSION",
    "PRINT_DIR_NAME",
    "WORK_DIR_NAME",
    "PREVIEW_DIR_NAME",
    "PrintManifest",
    "write_print_manifest",
    "read_print_manifest",
    "update_batch_manifest",
    "ErrorCode",
]

# 处理流水线版本号：任何影响输出的逻辑变更都应递增，便于追溯旧文件
PRINT_VERSION = "print-v1"

PRINT_DIR_NAME = "印刷TIF"
WORK_DIR_NAME = "_work"
PREVIEW_DIR_NAME = "预览"
MANIFEST_NAME = "print_manifest.json"


class ErrorCode:
    """结构化错误码（写进 manifest，前端据此显示原因）。"""

    OK = "ok"
    SOURCE_MISSING = "source_missing"          # 源 PNG 不存在
    SOURCE_UNREADABLE = "source_unreadable"    # 源 PNG 打不开/损坏
    ICC_MISSING = "icc_missing"                # ICC 缺失（已降级）
    CUTOUT_FAILED = "cutout_failed"            # 去背失败
    RESIZE_FAILED = "resize_failed"            # 放大失败（可能内存不足）
    DIELINE_FAILED = "dieline_failed"          # 刀模轮廓生成失败
    STORAGE_FULL = "storage_full"              # 磁盘满
    STORAGE_ERROR = "storage_error"            # 其它写盘错误
    DISABLED = "disabled"                      # 设置里关闭了导出
    UNKNOWN = "unknown"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class PrintManifest:
    """单张图的印刷参数记录。"""

    source_png: str = ""                       # 相对项目根的路径（不写绝对路径以外的敏感内容）
    source_pixels: tuple[int, int] = (0, 0)
    output_pixels: tuple[int, int] = (0, 0)
    width_cm: float = 0.0
    height_cm: float = 0.0
    dpi: int = 300
    effective_dpi: float = 0.0                 # 按源图像素推算的真实清晰度
    bleed_mm: float = 3.0
    bleed_px: int = 0

    icc: dict = field(default_factory=dict)     # {"name":..., "path":..., "fallback":bool}
    layers: dict = field(default_factory=dict)  # 各层文件名与存在性
    options: dict = field(default_factory=dict)  # 白墨/刀模/专色等开关

    status: str = "success"
    error_code: str = ""
    error_message: str = ""
    warnings: list[str] = field(default_factory=list)

    exported_at: str = ""
    version: str = PRINT_VERSION

    def to_dict(self) -> dict:
        d = {
            "version": self.version,
            "status": self.status,
            "exported_at": self.exported_at or _now(),
            "source": {
                "png": self.source_png,
                "pixels": list(self.source_pixels),
            },
            "output": {
                "pixels": list(self.output_pixels),
                "width_cm": round(self.width_cm, 2),
                "height_cm": round(self.height_cm, 2),
                "dpi": self.dpi,
                "effective_dpi": round(self.effective_dpi, 1),
                "note": (
                    f"模板按 {self.dpi}DPI 输出；源图 {self.source_pixels[0]}px，"
                    f"实际清晰度等效约 {round(self.effective_dpi, 1)}DPI。"
                    if self.effective_dpi and self.effective_dpi < self.dpi * 0.6
                    else ""
                ),
            },
            "bleed": {"mm": self.bleed_mm, "px": self.bleed_px},
            "icc": self.icc,
            "layers": self.layers,
            "options": self.options,
            "processor": {"name": "print_export", "version": PRINT_VERSION},
        }
        if self.warnings:
            d["warnings"] = list(self.warnings)
        if self.error_code and self.error_code != ErrorCode.OK:
            d["error"] = {"code": self.error_code, "message": self.error_message}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PrintManifest":
        src = d.get("source") or {}
        out = d.get("output") or {}
        bl = d.get("bleed") or {}
        err = d.get("error") or {}
        return cls(
            source_png=str(src.get("png") or ""),
            source_pixels=tuple(src.get("pixels") or (0, 0)),  # type: ignore[arg-type]
            output_pixels=tuple(out.get("pixels") or (0, 0)),  # type: ignore[arg-type]
            width_cm=float(out.get("width_cm") or 0),
            height_cm=float(out.get("height_cm") or 0),
            dpi=int(out.get("dpi") or 300),
            effective_dpi=float(out.get("effective_dpi") or 0),
            bleed_mm=float(bl.get("mm") or 0),
            bleed_px=int(bl.get("px") or 0),
            icc=dict(d.get("icc") or {}),
            layers=dict(d.get("layers") or {}),
            options=dict(d.get("options") or {}),
            status=str(d.get("status") or "unknown"),
            error_code=str(err.get("code") or ""),
            error_message=str(err.get("message") or ""),
            warnings=list(d.get("warnings") or []),
            exported_at=str(d.get("exported_at") or ""),
            version=str(d.get("version") or PRINT_VERSION),
        )


def _atomic_write(path: Path, text: str) -> None:
    """原子写（临时文件 + os.replace），避免写一半被杀留下坏文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_print_manifest(print_dir: Path, manifest: PrintManifest) -> Path:
    """写 `印刷TIF/print_manifest.json`。"""
    path = Path(print_dir) / MANIFEST_NAME
    _atomic_write(path, json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n")
    return path


def read_print_manifest(print_dir: Path) -> dict | None:
    """读 `印刷TIF/print_manifest.json`；不存在或损坏返回 None。"""
    path = Path(print_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def update_batch_manifest(store_dir: Path, patch: dict) -> bool:
    """把 `print_export` 字段**追加**到既有 `<门店>/_manifest.json`。

    ⚠️ **只增不改**：先读出全部既有内容，深合并新字段后原子写回。
      绝不删除或覆盖既有键（批次快照不可破坏）。

    Returns:
        True 表示已写入；False 表示 manifest 不存在（不新建，避免污染旧批次）
    """
    path = Path(store_dir) / "_manifest.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
    except (OSError, json.JSONDecodeError):
        return False

    data["print_export"] = patch
    data["updated_at"] = _now()
    try:
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        return True
    except OSError:
        return False
