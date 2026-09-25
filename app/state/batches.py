# -*- coding: utf-8 -*-
"""全量生图批次管理。

每次“重新生成（新批次）”都在用户设定的输出根目录下创建独立文件夹，
从而保留旧批次的图片、manifest 与人工检查结果。
"""
from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..core.config import Config
from .settings_store import SettingsStore

_SAFE_BATCH = re.compile(r"^[A-Za-z0-9_.-]+$")
BATCH_INFO_FILE = "_batch.json"

_VARIANTS = (
    "采用左下至右上的丝带动线，主标题处于视觉第一层，主体物件从右侧向中心聚焦。",
    "采用中心主视觉构图，主体物件放大并由环形色块、星芒与标签围绕，标题层级清晰。",
    "采用不对称分层贴纸构图，右上角做醒目促销角标，主体物件与标题形成前后景深。",
    "采用斜向大标题与圆弧色块构图，主体物件位于下半区，信息模块以清晰卡片形式排列。",
    "采用徽章式中心构图，外层粗描边异形轮廓，主体物件与辅助图标形成均衡的商业海报层级。",
    "采用上标题下主体的高密度商业贴纸构图，加入克制的装饰图形和色块层次，保留充足白底边缘。",
)


def is_safe_batch_id(value: str) -> bool:
    return bool(_SAFE_BATCH.fullmatch(value or ""))


def _part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip("-_.")
    return cleaned[:48] or "model"


def prompt_variant(batch_id: str, job_uid: str) -> tuple[int, str]:
    """为同一批次的每个主题稳定分配构图变体。"""
    digest = hashlib.sha256(f"{batch_id}:{job_uid}".encode("utf-8")).digest()
    index = digest[0] % len(_VARIANTS)
    return index + 1, _VARIANTS[index]


@dataclass(frozen=True)
class BatchInfo:
    batch_id: str
    path: Path
    created_at: str
    provider: str
    model: str

    @property
    def label(self) -> str:
        return self.batch_id.replace("batch_", "批次 ", 1)


def load_batch_snapshot(output_root: Path | str) -> dict:
    """读取批次的不可变运行快照；旧批次没有快照时返回空字典。"""
    path = Path(output_root) / BATCH_INFO_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def create_new_batch(
    cfg: Config,
    store: SettingsStore,
    snapshot: dict | None = None,
) -> BatchInfo:
    """创建并激活一个永不复用的批次，并写入本次范围和生成参数快照。"""
    base = Path(cfg.output_base_root).resolve()
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"batch_{stamp}_{_part(cfg.provider)}_{_part(cfg.model)}"
    batch_id = prefix
    serial = 2
    while (base / batch_id).exists():
        batch_id = f"{prefix}_{serial}"
        serial += 1

    target = base / batch_id
    target.mkdir(parents=True, exist_ok=False)
    created_at = datetime.now().isoformat(timespec="seconds")
    info = BatchInfo(
        batch_id=batch_id,
        path=target,
        created_at=created_at,
        provider=cfg.provider,
        model=cfg.model,
    )
    (target / BATCH_INFO_FILE).write_text(
        json.dumps(
            {
                "schema": 2,
                "batch_id": info.batch_id,
                "created_at": info.created_at,
                "provider": info.provider,
                "provider_label": cfg.provider_label,
                "model": info.model,
                "prompt_version": cfg.prompt_version or "current",
                "prompt_variant_strategy": "每个主题按批次编号和主题编号稳定分配 6 组构图变体",
                "base_output_root": str(base),
                "run_snapshot": snapshot or {},
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    store.save(
        {
            "output": {
                "active_batch": info.batch_id,
                "active_batch_created_at": info.created_at,
            }
        }
    )
    return info
