# -*- coding: utf-8 -*-
"""轻量级定时生图调度器。

设计要点：
  · 零新依赖（仅标准库 + asyncio）。
  · 任务持久化到 JSON（store_path，默认 ``STATE_ROOT/schedules.json``）。
  · 两种模式：
      - ``once``    ：``run_at`` 一次性触发，触发后自动删除。
      - ``interval``：按 分钟 / 小时 / 天 循环；``day`` 可额外指定 ``at="HH:MM"``。
  · 到点回调 ``on_fire(job)``：由 server 注入，通常调用
    ``_start_full_run(create_new_batch=True)``（B2：每次出全新图）。
  · 去重：调度器只负责「到点触发」；「运行中去重跳过」由 server 回调判定
    ``STATE.running`` 实现（D1 约定）。

本模块不依赖 server，避免循环导入：回调在 server 侧装配。
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..core.config import STATE_ROOT


def _now() -> datetime:
    return datetime.now()


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class ScheduleJob:
    """一条定时任务。"""

    id: str = field(default_factory=lambda: __import__("uuid").uuid4().hex)
    name: str = ""
    mode: str = "once"                 # once | interval
    provider: str = ""                 # 空 = 使用当前活动服务商
    run_at: str = ""                   # once: ISO 时间
    unit: str = "day"                  # interval: minute | hour | day
    every: int = 1                     # interval: 间隔数量
    at: str = ""                       # interval + day: "HH:MM"
    enabled: bool = True
    created_at: str = field(default_factory=lambda: _now().isoformat(timespec="seconds"))
    next_run: str = ""                 # interval: 下次触发 ISO（自动维护）
    last_run: str = ""
    last_result: str = ""

    def compute_next(self, from_time: Optional[datetime] = None) -> Optional[datetime]:
        base = from_time or _now()
        if self.mode == "once":
            return _parse(self.run_at)
        # interval
        if self.unit == "minute":
            nxt = base + timedelta(minutes=self.every)
        elif self.unit == "hour":
            nxt = base + timedelta(hours=self.every)
        else:  # day
            if self.at:
                try:
                    h, m = (int(x) for x in self.at.split(":"))
                except ValueError:
                    h, m = 0, 0
                nxt = base.replace(hour=h, minute=m, second=0, microsecond=0)
                if nxt <= base:
                    nxt += timedelta(days=self.every)
            else:
                nxt = base + timedelta(days=self.every)
        return nxt

    def to_dict(self) -> dict:
        return asdict(self)


class ScheduleManager:
    """管理定时任务：内存为源、JSON 持久化、asyncio 循环触发。"""

    def __init__(self, store_path: Optional[Path] = None):
        self.store_path = Path(store_path) if store_path else (STATE_ROOT / "schedules.json")
        self.jobs: list[ScheduleJob] = []
        self.on_fire: Optional[Callable[["ScheduleJob"], Awaitable[None]]] = None
        self._task: Optional[asyncio.Task] = None
        self.load()

    # ---------------------------------------------------------- 持久化
    def load(self) -> None:
        try:
            if self.store_path.exists():
                data = __import__("json").loads(
                    self.store_path.read_text(encoding="utf-8")
                )
                self.jobs = [ScheduleJob(**j) for j in data.get("jobs", [])]
        except Exception:
            self.jobs = []

    def save(self) -> None:
        try:
            import json

            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"jobs": [j.to_dict() for j in self.jobs]}
            self.store_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            # 持久化失败不应中断调度（内存态仍有效）
            pass

    # ---------------------------------------------------------- CRUD
    def add(self, job: ScheduleJob) -> ScheduleJob:
        if job.mode == "interval" and not job.next_run:
            nxt = job.compute_next()
            job.next_run = nxt.isoformat(timespec="seconds") if nxt else ""
        self.jobs.append(job)
        self.save()
        return job

    def remove(self, job_id: str) -> bool:
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if j.id != job_id]
        changed = len(self.jobs) != before
        if changed:
            self.save()
        return changed

    def list_jobs(self) -> list[ScheduleJob]:
        return list(self.jobs)

    # ---------------------------------------------------------- 循环
    async def start(self) -> None:
        """常驻循环：每 10 秒检查一次到期任务。"""
        while True:
            try:
                await self._tick()
            except Exception:
                # 单轮异常不应终止调度循环
                pass
            await asyncio.sleep(10)

    async def _tick(self) -> None:
        if not self.on_fire:
            return
        now = _now()
        fired: list[ScheduleJob] = []
        for job in self.jobs:
            if not job.enabled:
                continue
            target = _parse(job.run_at) if job.mode == "once" else _parse(job.next_run)
            if target and now >= target:
                fired.append(job)

        for job in fired:
            try:
                await self.on_fire(job)
            except Exception:
                # 回调异常由 server 侧记录到 job.last_result，这里兜底不崩
                pass
            # 触发后维护任务状态
            if job.mode == "once":
                self.remove(job.id)
            else:
                nxt = job.compute_next(from_time=now)
                job.next_run = nxt.isoformat(timespec="seconds") if nxt else ""
                job.last_run = now.isoformat(timespec="seconds")
                self.save()
