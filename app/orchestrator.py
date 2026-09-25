# -*- coding: utf-8 -*-
"""
任务编排器 —— 把 138 个作业调度到生成服务商。

**「无人化」的四个工程保障都在这里实现：**

1. **断点续跑**：启动时读取 manifest，已成功的作业直接跳过，只补缺失/失败项；
2. **失败重试**：指数退避，并区分「可重试」（限流、网络、超时）与
   「不可重试」（内容违规、参数错误、余额不足）；
3. **成本守卫**：单次运行的调用次数上限，超出即暂停派发，防止异常烧钱；
4. **并发限流**：滑动窗口严格遵守服务商 RPM（阿里 20/分、腾讯 1 并发…），
   避免被服务端拒绝。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable

from .config import Config
from .batches import prompt_variant
from .effect_background import resolve_render_options
from .effect_renderer import EFFECT_RENDERER_VERSION, EffectRenderError, render_storefront_glass
from .logging_setup import get_logger
from .manifest import ManifestStore
from .models import Job, JobStatus
from .prompt_profiles import DEFAULT_QUALITY_TEMPLATE, build_effective_prompt
from .providers import BaseProvider, GenerateRequest, ProviderError
from .storage import Storage
from .store_repo import StoreRepository

log = get_logger("orchestrator")

EventHandler = Callable[[dict], "Awaitable[None] | None"]


# ---------------------------------------------------------------- 限流
class RateLimiter:
    """Evenly paced request limiter with a shared provider cooldown.

    A sliding-window limiter permits a burst of ``N`` calls at the beginning
    of a run.  DashScope applies its 20-RPM policy to a rolling account-wide
    window, so that burst can be rejected even when the local count is below
    20.  Spacing requests across the minute avoids the burst and lets a 429
    pause every waiting worker together.
    """

    def __init__(self, max_per_minute: int, safety_factor: float = 1.0):
        self.limit = max(0, int(max_per_minute))
        safe_rate = max(1.0, self.limit * max(0.1, min(safety_factor, 1.0)))
        self._interval = 60.0 / safe_rate if self.limit else 0.0
        self._next_slot = 0.0
        self._cooldown_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.limit <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                ready_at = max(self._next_slot, self._cooldown_until)
                wait = ready_at - now
                if wait <= 0:
                    self._next_slot = now + self._interval
                    return
            # Wake periodically so a provider cooldown added by another worker
            # takes effect before this caller sends its request.
            await asyncio.sleep(min(max(wait, 0.05), 1.0))

    async def cooldown(self, seconds: float) -> None:
        """Block future requests after an account-level 429 response."""
        if self.limit <= 0:
            return
        async with self._lock:
            until = time.monotonic() + max(0.0, seconds)
            self._cooldown_until = max(self._cooldown_until, until)
            self._next_slot = max(self._next_slot, self._cooldown_until)


# ---------------------------------------------------------------- 成本守卫
class BudgetGuard:
    """调用次数上限守卫。"""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self._lock = asyncio.Lock()

    async def consume(self) -> bool:
        """尝试消耗一次调用额度。返回 False 表示预算已耗尽。"""
        async with self._lock:
            if self.limit and self.used >= self.limit:
                return False
            self.used += 1
            return True

    @property
    def exhausted(self) -> bool:
        return bool(self.limit) and self.used >= self.limit


# ---------------------------------------------------------------- 运行统计
@dataclass
class RunStats:
    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    qc_failed: int = 0
    calls: int = 0
    retries: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    aborted: str = ""

    @property
    def done(self) -> int:
        return self.success + self.failed + self.skipped + self.qc_failed

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.monotonic()
        return end - self.started_at if self.started_at else 0.0

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "done": self.done,
            "success": self.success,
            "failed": self.failed,
            "skipped": self.skipped,
            "qc_failed": self.qc_failed,
            "calls": self.calls,
            "retries": self.retries,
            "elapsed": round(self.elapsed, 1),
            "aborted": self.aborted,
        }


# ---------------------------------------------------------------- 编排器
class Orchestrator:
    """批量生成编排器。"""

    def __init__(
        self,
        cfg: Config,
        provider: BaseProvider,
        repo: StoreRepository,
        storage: Storage,
        manifests: ManifestStore,
    ):
        self.cfg = cfg
        self.provider = provider
        self.repo = repo
        self.storage = storage
        self.manifests = manifests

        self.stats = RunStats()
        self._cancel = asyncio.Event()
        self._pause = asyncio.Event()
        self._pause_reason = ""
        self._pause_code = ""
        self._handlers: list[EventHandler] = []
        self._current: dict[str, Job] = {}
        # 印刷导出任务的 Future（批次结束后才有值；仅供测试/CLI 等待）
        self._print_future = None

    # ------------------------------------------------------------ 事件
    def on_event(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def _emit(self, event: dict) -> None:
        for h in self._handlers:
            try:
                r = h(event)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:  # 事件处理失败不能影响主流程
                log.exception("事件处理异常")

    # ------------------------------------------------------------ 控制
    def cancel(self) -> None:
        """请求停止（当前正在生成的图会跑完）。"""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    async def _pause_for_account_recovery(self, error: ProviderError) -> None:
        """Stop dispatching after a recoverable account/billing rejection."""
        if self._pause.is_set():
            return
        self._pause_reason = str(error)
        self._pause_code = error.code
        self._pause.set()
        self.stats.aborted = "账户额度不足，等待充值后继续"
        log.warning("当前批次因账户状态暂停：%s", error)
        await self._emit({
            "type": "run_paused",
            "reason": self._pause_reason,
            "code": self._pause_code,
            "action": "充值后，在设置中验证图片出图权限成功，再继续当前批次",
        })

    # ------------------------------------------------------------ 主流程
    async def run(
        self,
        store_indexes: list[str] | None = None,
        limit: int = 0,
        pic_indexes: list[str] | None = None,
        only_uids: list[str] | None = None,
    ) -> RunStats:
        """执行批量生成。

        Args:
            store_indexes: 只跑指定门店（如 ['01']），None = 全部 23 套
            limit:         最多生成多少张（用于小批量试跑），0 = 不限
            pic_indexes:   只跑指定图片序号（如 ['03']），用于单张重生成
            only_uids:     精确指定要跑的作业（如 ['01-03', '05-02']），用于重试失败项
        """
        jobs = self.repo.build_jobs(self.cfg.prompt_version)
        if store_indexes:
            wanted = set(store_indexes)
            jobs = [j for j in jobs if j.store_index in wanted]
        if pic_indexes:
            wanted_pic = set(pic_indexes)
            jobs = [j for j in jobs if j.pic_index in wanted_pic]
        if only_uids:
            wanted_uid = set(only_uids)
            jobs = [j for j in jobs if j.uid in wanted_uid]

        # 断点续跑：跳过已成功的
        pending: list[Job] = []
        for job in jobs:
            manifest = self._manifest_for(job)
            # Manifest success is only a record.  If the user moved or deleted
            # the image, continue mode must regenerate it instead of skipping.
            image_exists = self.storage.exists(job)
            if not self.cfg.overwrite and manifest.succeeded(job.pic_index) and image_exists:
                job.status = JobStatus.SKIPPED
                self.stats.skipped += 1
                continue
            if not self.cfg.overwrite and image_exists:
                job.status = JobStatus.SKIPPED
                self.stats.skipped += 1
                continue
            pending.append(job)

        if limit:
            pending = pending[:limit]

        self.stats = RunStats(
            total=len(jobs),
            skipped=self.stats.skipped,
            started_at=time.monotonic(),
        )

        await self._emit({
            "type": "run_started",
            "total": len(jobs),
            "pending": len(pending),
            "skipped": self.stats.skipped,
            "provider": self.provider.describe(),
        })
        log.info(
            "开始生成：共 %d 张，待生成 %d 张，跳过 %d 张 | %s",
            len(jobs), len(pending), self.stats.skipped, self.provider.describe(),
        )

        # Qwen's advertised 20 RPM limit is account-wide and rolling.  Keep a
        # 20% buffer so another recently finished run cannot cause a burst of
        # 429 responses when this run begins.
        safety_factor = 0.8 if self.cfg.provider == "qwen" else 1.0
        limiter = RateLimiter(self.cfg.rpm_limit or 0, safety_factor=safety_factor)
        budget = BudgetGuard(self.cfg.budget_limit)
        sem = asyncio.Semaphore(max(1, self.cfg.concurrency))

        async def worker(job: Job) -> None:
            async with sem:
                if self.cancelled or self.paused:
                    return
                await self._process(job, limiter, budget)

        try:
            await asyncio.gather(*(worker(j) for j in pending))
        except asyncio.CancelledError:
            self.stats.aborted = "用户取消"
            raise
        finally:
            self.stats.finished_at = time.monotonic()
            self.manifests.save_all()

        # 恰好用完预算且所有作业都完成并不是“中止”。
        if budget.exhausted and self.stats.done < self.stats.total:
            self.stats.aborted = f"已达成本守卫上限（{self.cfg.budget_limit} 次调用）"

        await self._emit({"type": "run_finished", **self.stats.to_dict()})
        log.info("生成结束：%s", self.stats.to_dict())

        # 印刷 TIF 导出：**异步**跑，不阻塞 run_finished 事件，
        # 失败也绝不会冒泡到主流程（导出是附加价值）。
        # Future 存到实例上，便于测试/CLI 等待（网页路径不需要等）。
        self._print_future = self._schedule_print_export(pending)
        return self.stats

    # ------------------------------------------------------------ 印刷导出
    def _schedule_print_export(self, jobs: list[Job]):
        """批次完成后，在线程池里异步导出印刷 TIF。

        为什么用 `run_in_executor` 而不是 `create_task`：

        · 去背 / LANCZOS 放大 / CMYK 转换全是 **CPU 密集**操作，
          直接放事件循环里会把整个 Web 服务卡住（其它请求全部超时）
        · 一张 2000px 图约 0.5~2 秒，批量几十张就是几十秒

        ⚠️ 整个函数体包在 try/except 里：印刷导出**任何**失败都不能影响主流程。

        Returns:
            `concurrent.futures.Future`（**调用方不需要等**）。
            返回它是为了让测试/CLI 能等待导出完成 ——
            `asyncio.run()` 结束时默认线程池会被 shutdown，
            不等的话任务会被取消（实测踩到）。
            拿不到任务时返回 None。
        """
        if not getattr(self.cfg, "print_export_enabled", True):
            log.info("印刷导出已在设置中关闭，跳过")
            return None
        try:
            from .print_export import collect_export_targets, export_after_batch

            targets = collect_export_targets(jobs, self.cfg.output_root)
            if not targets:
                log.info("没有可导出的图片（可能全部失败或被跳过）")
                return None

            loop = asyncio.get_running_loop()
            cfg = self.cfg

            def _run() -> None:
                try:
                    results = export_after_batch(cfg, targets)
                    ok = sum(1 for r in results if r.ok)
                    log.info("印刷导出完成：成功 %d / 共 %d", ok, len(results))
                except SystemExit:
                    log.error("印刷导出被 SystemExit 中断（通常是 rembg 缺 onnxruntime）")
                except Exception:
                    log.exception("印刷导出失败（已忽略，不影响主流程）")

            future = loop.run_in_executor(None, _run)
            log.info("印刷导出已排入后台：%d 个目标", len(targets))
            return future
        except Exception:
            log.exception("排入印刷导出任务失败（已忽略）")
            return None

    # ------------------------------------------------------------ 单张处理
    async def _process(self, job: Job, limiter: RateLimiter, budget: BudgetGuard) -> None:
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now().isoformat(timespec="seconds")
        t0 = time.monotonic()
        self._current[job.uid] = job
        await self._emit({
            "type": "job_started",
            "uid": job.uid,
            "store": job.store_index,
            "file": job.file_name,
            "display": job.display_name,
        })

        quality_template = self.cfg.optimized_quality_template if (
            self.cfg.use_optimized_quality_template and self.cfg.optimized_quality_template
        ) else (self.cfg.quality_template or DEFAULT_QUALITY_TEMPLATE)
        prompt = build_effective_prompt(job, quality_template, self.cfg.size, self.cfg.realism_iteration)
        job.runtime_metrics["realism_iteration"] = self.cfg.realism_iteration
        if self.cfg.batch_id:
            variant_number, variant_instruction = prompt_variant(self.cfg.batch_id, job.uid)
            prompt = (
                f"{prompt}\n\n"
                f"【本批次构图变体 {variant_number}】{variant_instruction}"
                "必须完整保留原提示词中指定的中文主标题、副标题和行业信息，不得替换文字。"
            )
            job.runtime_metrics["batch_id"] = self.cfg.batch_id
            job.runtime_metrics["batch_variant"] = variant_number
            job.runtime_metrics["batch_variant_instruction"] = variant_instruction

        reference_urls = [
            str(asset.get("data_url") or "")
            for asset in self.cfg.reference_assets
            if isinstance(asset, dict) and asset.get("data_url")
        ]
        safe_reference_assets = [
            {key: asset.get(key) for key in ("id", "sha256", "file_name", "mime_type", "bytes")}
            for asset in self.cfg.reference_assets if isinstance(asset, dict)
        ]
        if self.cfg.image_mode != "text":
            job.runtime_metrics["image_mode"] = self.cfg.image_mode
            job.runtime_metrics["reference_assets"] = safe_reference_assets
        job.runtime_metrics["size"] = self.cfg.size
        req = GenerateRequest(
            prompt=prompt,
            negative_prompt=job.negative_prompt if self.provider.supports_negative else "",
            size=self.cfg.size,
            n=1,
            extra={"image_mode": self.cfg.image_mode, "reference_images": reference_urls},
        )

        last_error = ""
        # ``retry_max`` comes from existing settings and may be one.  A 429 is
        # safe to retry once after the shared cooldown because it produces no
        # image and usually reflects a temporary account-wide window.
        max_attempts = max(1, self.cfg.retry_max)
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            job.attempts = attempt

            if not await budget.consume():
                job.status = JobStatus.FAILED
                job.error = f"已达成本守卫上限（{self.cfg.budget_limit} 次调用）"
                break

            await limiter.acquire()
            if self.paused or self.cancelled:
                self._current.pop(job.uid, None)
                return
            try:
                result = await self.provider.generate(req)
                self.stats.calls += 1

                if not result.ok:
                    raise ProviderError(result.error or "生成失败", retryable=True)

                # 落盘（旧图自动归档到 _history/，标签含提示词版本与模型）
                tag = f"{job.prompt_version or 'cur'}_{self.provider.model or self.provider.name}"
                path = self.storage.save_image(job, result.images[0], version_tag=tag)
                job.status = JobStatus.SUCCESS
                job.error = ""
                # 保留批次构图变体等请求侧元数据，再叠加服务商返回指标。
                metrics = dict(result.raw or {})
                metrics.update(job.runtime_metrics)
                job.runtime_metrics = metrics
                if result.model:
                    job.runtime_metrics.setdefault("model", result.model)
                if result.elapsed:
                    job.runtime_metrics.setdefault("provider_elapsed_seconds", round(result.elapsed, 3))
                # 本地合成效果图，不调用图像服务商，原始贴纸的中文和图案不会被重绘。
                #
                # ⚠️ 必须走 resolve_render_options（与网页 /api/effects/generate 共用同一处逻辑），
                #    否则批量产出会与网页预览不一致。早期这里直接读 cfg.effect_background_asset，
                #    导致三个问题：
                #      ① 背景自动匹配失效（永远退回模拟背景）
                #      ② glass_region 丢失（贴纸可能跨过门框/竖梃）
                #      ③ 用户在「效果图微调」里调的 17 个参数完全无效
                try:
                    effect = render_storefront_glass(
                        path,
                        job.store_name,
                        **resolve_render_options(self.cfg, job.store_name),
                    )
                    effect_path = self.storage.save_effect_image(job, effect.data, path)
                    job.runtime_metrics["effect_image"] = effect.metadata(effect_path)
                except EffectRenderError as exc:
                    job.runtime_metrics["effect_image"] = {
                        "status": "failed", "renderer": EFFECT_RENDERER_VERSION, "error": str(exc),
                    }
                    log.warning("效果图合成失败（不影响生成图）：%s", exc)
                job.elapsed = time.monotonic() - t0
                job.finished_at = datetime.now().isoformat(timespec="seconds")
                self.stats.success += 1
                self._record(job)

                log.info("[OK] %s → %s（%.1fs）", job.display_name, path.name, job.elapsed)
                await self._emit({
                    "type": "job_success",
                    "uid": job.uid,
                    "store": job.store_index,
                    "file": path.name,
                    "effect_file": job.runtime_metrics.get("effect_image", {}).get("file_name", ""),
                    "elapsed": round(job.elapsed, 2),
                    "attempts": attempt,
                })
                self._current.pop(job.uid, None)
                return

            except ProviderError as e:
                self.stats.calls += 1
                last_error = str(e)
                if e.requires_account_recovery:
                    await self._pause_for_account_recovery(e)
                    job.status = JobStatus.PAUSED
                    job.error = last_error
                    job.elapsed = time.monotonic() - t0
                    job.finished_at = datetime.now().isoformat(timespec="seconds")
                    self._record(job)
                    self._current.pop(job.uid, None)
                    await self._emit({
                        "type": "job_paused",
                        "uid": job.uid,
                        "store": job.store_index,
                        "error": last_error,
                        "attempts": job.attempts,
                    })
                    return
                if e.code == "RATE_LIMIT":
                    max_attempts = max(max_attempts, 2)
                    await limiter.cooldown(65.0)
                if not e.retryable:
                    log.error("[X] %s 不可重试：%s", job.display_name, e)
                    break
                if attempt < max_attempts:
                    backoff = self.cfg.retry_backoff[
                        min(attempt - 1, len(self.cfg.retry_backoff) - 1)
                    ]
                    self.stats.retries += 1
                    log.warning(
                        "↻ %s 第 %d 次失败（%s），%.0fs 后重试",
                        job.display_name, attempt, e, backoff,
                    )
                    await self._emit({
                        "type": "job_retry",
                        "uid": job.uid,
                        "attempt": attempt,
                        "error": last_error,
                    })
                    await asyncio.sleep(backoff)
            except Exception as e:  # 未知异常按可重试处理
                self.stats.calls += 1
                last_error = f"{type(e).__name__}: {e}"
                log.exception("[X] %s 未预期异常", job.display_name)
                if attempt < self.cfg.retry_max:
                    await asyncio.sleep(self.cfg.retry_backoff[0])

        # 用尽重试
        job.status = JobStatus.FAILED
        job.error = last_error or "未知错误"
        job.elapsed = time.monotonic() - t0
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        self.stats.failed += 1
        self._record(job)
        self._current.pop(job.uid, None)
        log.error("[X] %s 最终失败：%s", job.display_name, job.error)
        await self._emit({
            "type": "job_failed",
            "uid": job.uid,
            "store": job.store_index,
            "error": job.error,
            "attempts": job.attempts,
        })

    # ------------------------------------------------------------ 辅助
    def _manifest_for(self, job: Job):
        store = next(
            (s for s in self.repo.stores if s.folder_index == job.store_index), None
        )
        meta = {}
        if store:
            meta = {
                "folder_index": store.folder_index,
                "folder_name": store.folder_name,
                "main_title": store.main_title,
                "sub_title": store.sub_title,
                "color_theme": store.color_theme,
                "compliance_note": store.compliance_note,
                "pdd_title": store.pdd_title,
            }
        return self.manifests.for_store(job.output_dir, meta)

    def _record(self, job: Job) -> None:
        m = self._manifest_for(job)
        m.record(job, provider=self.provider.name, model=self.provider.model)
        # 每张图落盘后立即写 manifest —— 崩溃也不丢进度
        m.save()
