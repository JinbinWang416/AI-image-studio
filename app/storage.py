# -*- coding: utf-8 -*-
"""
存储层：创建 23 个输出文件夹、保存图片、维护目录结构。

命名规则：
  - 默认：`01_水龙头.png`（语义清晰，便于检索）
  - 可选：`20260918_164130_01_水龙头.png`（加时间戳前缀，TIMESTAMP_PREFIX=true）

**后处理**：保存前会把贴纸外部的背景刷成纯白
（模型对「白图 + 彩色背景」有固有偏好，提示词压不住，见 `app/postprocess.py`）。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .logging_setup import get_logger
from .models import Job, Store
from .postprocess import whiten_background

log = get_logger("storage")

# 历史版本归档目录（位于生成图或效果图文件夹内）
HISTORY_DIR = "_history"
GENERATED_DIR_SUFFIX = "生成图"
EFFECT_DIR_SUFFIX = "效果图"

# Windows 文件名非法字符
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    """把任意字符串转为安全的文件名片段。"""
    cleaned = _ILLEGAL.sub("_", name).strip().rstrip(".")
    return cleaned or "unnamed"


class Storage:
    """输出目录与文件的落地管理。"""

    def __init__(
        self,
        output_root: Path | str,
        timestamp_prefix: bool = False,
        whiten_bg: bool = True,
        whiten_threshold: int = 45,
    ):
        self.output_root = Path(output_root)
        self.timestamp_prefix = timestamp_prefix
        self.whiten_bg = whiten_bg
        self.whiten_threshold = whiten_threshold

    # ------------------------------------------------------------ 目录
    def prepare_directories(self, stores: list[Store]) -> dict[str, Path]:
        """创建门店根目录及其“生成图 / 效果图”子目录。

        旧版本把 PNG 直接保存到门店根目录。保留读取兼容，但所有新文件都会
        落在两个清晰的子目录中，方便交付和人工查看。
        """
        self.output_root.mkdir(parents=True, exist_ok=True)
        mapping: dict[str, Path] = {}
        for s in stores:
            d = self.output_root / safe_name(s.output_dir)
            d.mkdir(parents=True, exist_ok=True)
            self.generated_dir(s.output_dir).mkdir(parents=True, exist_ok=True)
            self.effect_dir(s.output_dir).mkdir(parents=True, exist_ok=True)
            mapping[s.output_dir] = d
        log.info("已准备 %d 个门店目录 → %s", len(mapping), self.output_root)
        return mapping

    def store_dir(self, output_dir: str) -> Path:
        return self.output_root / safe_name(output_dir)

    def generated_dir(self, output_dir: str) -> Path:
        """保存原始贴纸成品的目录。"""
        root = self.store_dir(output_dir)
        return root / f"{safe_name(output_dir)}{GENERATED_DIR_SUFFIX}"

    def effect_dir(self, output_dir: str) -> Path:
        """保存贴在门店玻璃上的效果图目录。"""
        root = self.store_dir(output_dir)
        return root / f"{safe_name(output_dir)}{EFFECT_DIR_SUFFIX}"

    # ------------------------------------------------------------ 文件
    def target_path(self, job: Job, stamp: datetime | None = None) -> Path:
        """计算某任务的目标文件路径（尚未写入）。"""
        d = self.generated_dir(job.output_dir)
        if self.timestamp_prefix:
            ts = (stamp or datetime.now()).strftime("%Y%m%d_%H%M%S")
            return d / f"{ts}_{job.file_name}"
        return d / safe_name(job.file_name)

    def exists(self, job: Job) -> bool:
        """目标文件是否已存在（含时间戳模式下的模糊匹配）。"""
        target = self.target_path(job)
        if target.exists():
            return True
        # 旧批次兼容：生成图还在门店根目录时，仍可继续当前批次。
        legacy = self.store_dir(job.output_dir) / safe_name(job.file_name)
        if legacy.exists():
            return True
        if not self.timestamp_prefix:
            return False
        d = self.generated_dir(job.output_dir)
        stem = Path(job.file_name).stem
        if d.exists() and any(d.glob(f"*_{stem}.png")):
            return True
        legacy_dir = self.store_dir(job.output_dir)
        return legacy_dir.exists() and any(legacy_dir.glob(f"*_{stem}.png"))

    def archive_existing(self, path: Path, tag: str = "") -> Path | None:
        """把已存在的图片归档到同目录的 `_history/`，避免覆盖丢失。

        归档文件名：`{原名}__{标签}__{时间戳}.png`
        例如：`01_楼房线稿__v6_qwen-image-plus__20260918_205412.png`
        """
        if not path.exists():
            return None
        hist = path.parent / HISTORY_DIR
        hist.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"__{safe_name(tag)}" if tag else ""
        dest = hist / f"{path.stem}{suffix}__{ts}{path.suffix}"
        n = 1
        while dest.exists():
            dest = hist / f"{path.stem}{suffix}__{ts}_{n}{path.suffix}"
            n += 1
        path.replace(dest)
        log.info("已归档旧版本 → %s", dest.name)
        return dest

    def save_image(
        self,
        job: Job,
        data: bytes,
        stamp: datetime | None = None,
        version_tag: str = "",
    ) -> Path:
        """保存图片二进制（含背景刷白后处理 + 旧版本归档）。

        Args:
            version_tag: 归档标签，通常为 `{提示词版本}_{模型}`，便于事后对比
        """
        raw_size = len(data)
        if self.whiten_bg:
            try:
                data = whiten_background(data, self.whiten_threshold)
            except Exception:                     # 后处理失败不影响主流程
                log.warning("背景刷白失败，保存原图：%s", job.file_name)

        path = self.target_path(job, stamp)
        path.parent.mkdir(parents=True, exist_ok=True)

        # 旧图先归档，不直接覆盖
        archived = self.archive_existing(path, version_tag)
        if archived:
            job.archived_to = str(archived)

        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)          # 原子替换
        job.image_path = str(path)

        if self.whiten_bg and len(data) != raw_size:
            log.debug(
                "背景已刷白：%s（%d → %d 字节）", path.name, raw_size, len(data)
            )
        return path

    def effect_target_path(self, job: Job, source_path: Path | str | None = None) -> Path:
        """返回与生成图一一对应的效果图路径。"""
        source_name = Path(source_path or job.image_path or self.target_path(job)).name
        return self.effect_dir(job.output_dir) / f"{Path(source_name).stem}_效果图.png"

    def save_effect_image(
        self,
        job: Job,
        data: bytes,
        source_path: Path | str | None = None,
        version_tag: str = "glass_mockup",
    ) -> Path:
        """保存玻璃门店效果图，并在同目录保留旧效果图历史。"""
        path = self.effect_target_path(job, source_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_existing(path, version_tag)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path

    # ------------------------------------------------------------ 历史版本
    def list_history(self, output_dir: str, file_name: str) -> list[dict]:
        """列出某张图的历史版本（新 → 旧）。"""
        stem = Path(file_name).stem
        # 新版归档在“生成图/_history”；再回退到旧版根目录。
        hist = self.generated_dir(output_dir) / HISTORY_DIR
        if not hist.exists():
            hist = self.store_dir(output_dir) / HISTORY_DIR
        if not hist.exists():
            return []

        out = []
        for p in sorted(hist.glob(f"{stem}__*"), reverse=True):
            name = p.stem
            tag = ""
            ts = ""
            parts = name.split("__")
            if len(parts) >= 3:
                tag = parts[1]
                ts = parts[2]
            elif len(parts) == 2:
                ts = parts[1]
            out.append({
                "file_name": p.name,
                "tag": tag,
                "timestamp": ts,
                "size": p.stat().st_size,
                "path": str(p),
            })
        return out

    # ------------------------------------------------------------ 统计
    def count_images(self) -> int:
        if not self.output_root.exists():
            return 0
        return sum(
            1 for p in self.output_root.rglob(f"*{GENERATED_DIR_SUFFIX}/*.png")
            if HISTORY_DIR not in p.parts and not p.name.startswith("_")
        )

    def scan_existing(self, stores: list[Store]) -> dict[str, list[str]]:
        """扫描输出目录，返回 {output_dir: [已存在的文件名]}。"""
        result: dict[str, list[str]] = {}
        for s in stores:
            d = self.generated_dir(s.output_dir)
            legacy = self.store_dir(s.output_dir)
            if d.exists():
                result[s.output_dir] = sorted(
                    p.name for p in d.glob("*.png") if not p.name.startswith("_")
                )
            elif legacy.exists():
                result[s.output_dir] = sorted(
                    p.name for p in legacy.glob("*.png") if not p.name.startswith("_")
                )
            else:
                result[s.output_dir] = []
        return result
