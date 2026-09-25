# -*- coding: utf-8 -*-
"""
生成记录（manifest）读写。

每个门店目录下有一个 `_manifest.json`，记录该门店 6 张图的生成元数据：
状态、尝试次数、耗时、质检结果、实际使用的参数。

**断点续跑的核心依据**：启动时扫描 manifest，已成功的任务直接跳过。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..core.models import Job, JobStatus

MANIFEST_NAME = "_manifest.json"
MANIFEST_VERSION = 2


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Manifest:
    """单个门店目录的生成记录。"""

    def __init__(self, store_dir: Path, store_meta: dict | None = None):
        self.store_dir = Path(store_dir)
        self.path = self.store_dir / MANIFEST_NAME
        self.store_meta = store_meta or {}
        self.entries: dict[str, dict] = {}
        self.created_at = _now()
        self.updated_at = self.created_at

    # ------------------------------------------------------------ 读写
    @classmethod
    def load_or_create(cls, store_dir: Path, store_meta: dict | None = None) -> "Manifest":
        m = cls(store_dir, store_meta)
        if m.path.exists():
            try:
                data = json.loads(m.path.read_text(encoding="utf-8"))
                m.entries = data.get("entries", {})
                m.created_at = data.get("created_at", m.created_at)
                m.store_meta = data.get("store", m.store_meta)
            except (json.JSONDecodeError, OSError):
                # 记录损坏则重建，不阻塞流程
                m.entries = {}
        return m

    def save(self) -> None:
        self.updated_at = _now()
        payload = {
            "version": MANIFEST_VERSION,
            "store": self.store_meta,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "summary": self.summary(),
            "entries": self.entries,
        }
        self.store_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tmp.replace(self.path)   # 原子替换，避免写一半损坏

    # ------------------------------------------------------------ 记录
    def record(self, job: Job, provider: str = "", model: str = "") -> None:
        """写入/更新一条任务记录。"""
        self.entries[job.pic_index] = {
            **job.to_dict(),
            "provider": provider,
            "model": model,
            "expected_text": job.expected_text,
        }

    def get_status(self, pic_index: str) -> JobStatus | None:
        e = self.entries.get(pic_index)
        if not e:
            return None
        try:
            return JobStatus(e.get("status", "pending"))
        except ValueError:
            return None

    def succeeded(self, pic_index: str) -> bool:
        """该图此前是否已成功（用于断点续跑跳过）。"""
        return self.get_status(pic_index) == JobStatus.SUCCESS

    def failed_indexes(self) -> list[str]:
        return [
            k for k, v in self.entries.items()
            if v.get("status") in (JobStatus.FAILED.value, JobStatus.QC_FAILED.value)
        ]

    # ------------------------------------------------------------ 汇总
    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for e in self.entries.values():
            st = e.get("status", "pending")
            counts[st] = counts.get(st, 0) + 1
        return {
            "total": len(self.entries),
            "success": counts.get(JobStatus.SUCCESS.value, 0),
            "failed": counts.get(JobStatus.FAILED.value, 0),
            "qc_failed": counts.get(JobStatus.QC_FAILED.value, 0),
            "paused": counts.get(JobStatus.PAUSED.value, 0),
            "skipped": counts.get(JobStatus.SKIPPED.value, 0),
            "by_status": counts,
        }


class ManifestStore:
    """管理全部 23 个门店的 manifest。"""

    def __init__(self, output_root: Path):
        self.output_root = Path(output_root)
        self._manifests: dict[str, Manifest] = {}

    def for_store(self, output_dir: str, store_meta: dict | None = None) -> Manifest:
        if output_dir not in self._manifests:
            self._manifests[output_dir] = Manifest.load_or_create(
                self.output_root / output_dir, store_meta
            )
        return self._manifests[output_dir]

    def save_all(self) -> None:
        for m in self._manifests.values():
            m.save()

    def overall_summary(self) -> dict:
        agg = {"total": 0, "success": 0, "failed": 0, "qc_failed": 0, "paused": 0, "skipped": 0}
        for m in self._manifests.values():
            s = m.summary()
            for k in agg:
                agg[k] += s.get(k, 0)
        return agg
