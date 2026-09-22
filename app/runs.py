# -*- coding: utf-8 -*-
"""
运行历史记录。

每次批量生成结束后追加一条记录到 `logs/runs.json`，
用于回答「上一次跑是什么时候、用的什么模型和提示词版本、成功了多少张、花了多少钱」。
"""
from __future__ import annotations

import json
from datetime import datetime

from .paths import PACKAGE_ROOT, STATE_ROOT

ROOT = PACKAGE_ROOT
RUNS_FILE = STATE_ROOT / "logs" / "runs.json"
MAX_RECORDS = 200


def append_run(
    *,
    provider: str,
    provider_label: str,
    model: str,
    prompt_version: str,
    price_per_image: float = 0.0,
    stats: dict | None = None,
    scope: str = "全量",
    output_root: str = "",
) -> dict:
    """追加一条运行记录，返回该记录。"""
    stats = stats or {}
    calls = int(stats.get("calls", 0) or 0)

    record = {
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scope": scope,
        "provider": provider,
        "provider_label": provider_label,
        "model": model,
        "prompt_version": prompt_version,
        "total": int(stats.get("total", 0) or 0),
        "success": int(stats.get("success", 0) or 0),
        "failed": int(stats.get("failed", 0) or 0),
        "skipped": int(stats.get("skipped", 0) or 0),
        "qc_failed": int(stats.get("qc_failed", 0) or 0),
        "calls": calls,
        "retries": int(stats.get("retries", 0) or 0),
        "elapsed": round(float(stats.get("elapsed", 0) or 0), 1),
        "cost": round(calls * float(price_per_image or 0), 2),
        "aborted": stats.get("aborted", ""),
        "output_root": output_root,
    }

    records = load_runs()
    records.append(record)
    records = records[-MAX_RECORDS:]

    RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RUNS_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(RUNS_FILE)
    return record


def load_runs() -> list[dict]:
    """读取全部运行记录。"""
    if not RUNS_FILE.exists():
        return []
    try:
        data = json.loads(RUNS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def summary() -> dict:
    """汇总统计：总运行次数、总成功张数、总花费。"""
    records = load_runs()
    return {
        "runs": len(records),
        "total_success": sum(r.get("success", 0) for r in records),
        "total_calls": sum(r.get("calls", 0) for r in records),
        "total_cost": round(sum(r.get("cost", 0) for r in records), 2),
        "total_elapsed": round(sum(r.get("elapsed", 0) for r in records), 1),
        "last_run": records[-1] if records else None,
    }
