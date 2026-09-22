# -*- coding: utf-8 -*-
"""数据模型：门店、图片项、生成任务。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class JobStatus(str, Enum):
    """任务状态。"""

    PENDING = "pending"        # 待处理
    RUNNING = "running"        # 生成中
    SUCCESS = "success"        # 成功
    FAILED = "failed"          # 失败（已用尽重试）
    QC_FAILED = "qc_failed"    # 生成成功但质检不通过，待人工复检
    PAUSED = "paused"          # 账户额度/账单阻断，充值后可继续当前批次
    SKIPPED = "skipped"        # 跳过（文件已存在且未开启 overwrite）

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.QC_FAILED, JobStatus.SKIPPED)

    @property
    def is_ok(self) -> bool:
        return self in (JobStatus.SUCCESS, JobStatus.SKIPPED)


@dataclass
class StoreItem:
    """一张图片的提示词定义（含全部历史版本）。"""

    pic_index: str
    theme: str
    subject: str
    file_name: str
    positive_prompt: str
    negative_prompt: str
    expected_text: list[str] = field(default_factory=list)
    prompt_version: str = ""
    # {版本号: {"positive": ..., "negative": ...}}，保存历史版本供切换与对比
    prompt_history: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "StoreItem":
        import re

        history: dict[str, dict[str, str]] = {}
        for key, val in d.items():
            m = re.match(r"^positive_prompt_(v\d+)$", key)
            if m:
                ver = m.group(1)
                history[ver] = {
                    "positive": val,
                    "negative": d.get(f"negative_prompt_{ver}", ""),
                }
        return cls(
            pic_index=d["pic_index"],
            theme=d["theme"],
            subject=d["subject"],
            file_name=d["file_name"],
            positive_prompt=d["positive_prompt"],
            negative_prompt=d["negative_prompt"],
            expected_text=list(d.get("expected_text", [])),
            prompt_version=d.get("prompt_version", ""),
            prompt_history=history,
        )

    def prompt_for(self, version: str = "") -> tuple[str, str]:
        """按版本号取 (正向, 负向) 提示词；取不到时回退当前版本。"""
        if not version or version == self.prompt_version:
            return self.positive_prompt, self.negative_prompt
        h = self.prompt_history.get(version)
        if h:
            return h["positive"], h["negative"]
        return self.positive_prompt, self.negative_prompt


@dataclass
class Store:
    """一个门店（对应一个输出文件夹 + 6 张图）。"""

    folder_index: str
    folder_name: str
    output_dir: str
    pdd_title: str
    main_title: str
    sub_title: str
    color_theme: str
    compliance_note: str
    simple_prompt: str
    items: list[StoreItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Store":
        return cls(
            folder_index=d["folder_index"],
            folder_name=d["folder_name"],
            output_dir=d["output_dir"],
            pdd_title=d["pdd_title"],
            main_title=d["main_title"],
            sub_title=d["sub_title"],
            color_theme=d["color_theme"],
            compliance_note=d.get("compliance_note", ""),
            simple_prompt=d.get("simple_prompt", ""),
            items=[StoreItem.from_dict(it) for it in d.get("items", [])],
        )


@dataclass
class Job:
    """一次图片生成任务（门店 × 图片）。"""

    job_id: str
    store_index: str
    store_name: str
    output_dir: str
    pic_index: str
    theme: str
    file_name: str
    positive_prompt: str
    negative_prompt: str
    simple_prompt: str
    expected_text: list[str]
    prompt_version: str = ""
    main_title: str = ""
    sub_title: str = ""
    color_theme: str = ""
    subject: str = ""

    status: JobStatus = JobStatus.PENDING
    attempts: int = 0
    error: str = ""
    image_path: str = ""
    archived_to: str = ""      # 旧版本被归档到的路径（若有）
    started_at: str = ""
    finished_at: str = ""
    elapsed: float = 0.0
    qc_result: dict = field(default_factory=dict)
    runtime_metrics: dict = field(default_factory=dict)

    @property
    def uid(self) -> str:
        """全局唯一标识，形如 `01-03`。"""
        return f"{self.store_index}-{self.pic_index}"

    @property
    def display_name(self) -> str:
        return f"{self.store_name} / {self.pic_index}_{self.theme}"

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "store_index": self.store_index,
            "store_name": self.store_name,
            "output_dir": self.output_dir,
            "pic_index": self.pic_index,
            "theme": self.theme,
            "file_name": self.file_name,
            "status": self.status.value,
            "attempts": self.attempts,
            "error": self.error,
            "image_path": self.image_path,
            "archived_to": self.archived_to,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round(self.elapsed, 2),
            "qc_result": self.qc_result,
            "runtime_metrics": self.runtime_metrics,
        }
