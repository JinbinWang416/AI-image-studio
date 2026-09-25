# -*- coding: utf-8 -*-
"""OpenAI 房屋中介 6 图再生成：变体轮换、去重和可恢复记录。"""
from __future__ import annotations

import asyncio
import io
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from PIL import Image

from ..effect.background import resolve_render_options
from ..effect.renderer import EFFECT_RENDERER_VERSION, EffectRenderError, render_storefront_glass
from ..state.manifest_store import ManifestStore
from ..core.models import Job, JobStatus, Store
from ..effect.postprocess import whiten_background
from ..prompt.profiles import realism_requirement
from ..prompt.optimizer import DeepSeekPromptOptimizer, PromptOptimizerError
from ..providers.base import BaseProvider, GenerateRequest, ProviderError
from ..state.storage import Storage
from ..state.store_repo import StoreRepository

HOUSE_STORE_INDEX = "01"
HOUSE_IMAGE_COUNT = 6
STATE_FILE_NAME = "_openai_house_regeneration.json"
NEAR_DUPLICATE_DISTANCE = 6
MAX_DUPLICATE_RETRIES = 1

# 每一轮 6 个主题采用不同构图组合，下一轮整体平移，保证连续点击的输入提示词不同。
VARIATION_PROFILES: tuple[tuple[str, str], ...] = (
    ("中心立体徽章", "以中央立体房屋徽章为视觉主体，顶部留出大标题区，使用层叠楼宇线稿和丝带"),
    ("对角动态构图", "以左下至右上的斜向动线组织楼宇、钥匙和定位符号，画面具有明显纵深"),
    ("双景层叠", "前景放置简洁服务图标，背景为现代住宅群，采用前中后景三层商业贴纸构图"),
    ("城市天际线", "突出城市住宅天际线和楼宇窗格，以圆弧色块、叶片和光带构建高密度背景"),
    ("钥匙服务主视觉", "突出钥匙、房门和地图定位组合，住宅元素作为有层次的辅助视觉"),
    ("门店场景海报", "突出明亮专业的房产咨询门店场景，玻璃门、接待台和住宅图标形成完整服务叙事"),
    ("环形信息构图", "以环形丝带围绕中心住宅图形，形成适合异形窗贴的完整封闭视觉"),
    ("高级留白构图", "保留清晰的中心文字安全区，周围用精致小型楼宇、钥匙和定位图形增强商业感"),
    ("轻透玻璃质感", "表现玻璃门窗静电贴的通透层叠质感，蓝金白商务配色仍保持高对比和可制作性"),
    ("几何折页构图", "使用克制的几何折页和层叠色块承载住宅和服务意象，画面干净但不单调"),
    ("城市服务地图", "把住宅群、钥匙和位置标记组合成城市服务地图意象，营造可信赖的房产咨询感"),
    ("庆典促销贴纸", "以精致商业促销贴纸层级呈现房产服务，轮廓鲜明、色彩饱满、信息层次清楚"),
)


def _run_state_path(root: Path) -> Path:
    return Path(root) / STATE_FILE_NAME


def reserve_run(root: Path) -> tuple[str, int]:
    """原子递增轮次，让服务重启后也不会重复使用上一轮变体组合。"""
    path = _run_state_path(root)
    counter = 0
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            counter = int(payload.get("counter", 0) or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            counter = 0
    counter += 1
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"openai-house-{stamp}-{counter:04d}"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"counter": counter, "last_run_id": run_id}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return run_id, counter


def variation_for(cycle: int, pic_offset: int, retry: int = 0) -> tuple[int, str, str]:
    index = (cycle * HOUSE_IMAGE_COUNT + pic_offset + retry * 5) % len(VARIATION_PROFILES)
    label, instruction = VARIATION_PROFILES[index]
    return index, label, instruction


def varied_prompt(base_prompt: str, run_id: str, profile: str, realism_iteration: int = 1) -> str:
    return (
        f"{base_prompt}\n\n"
        f"本次为新版本 {run_id}。视觉变体要求：{profile}。"
        "与此前版本采用明显不同的构图和主体关系；保留主标题与副标题的正确中文，"
        "保持 1:1 成品、完整异形外轮廓、白色画布边缘、无真实楼盘品牌、无价格和成交保证。"
        f"\n【逐批真实感要求】{realism_requirement(realism_iteration)}"
    )


def perceptual_hash(data: bytes) -> str:
    """64 位 dHash，用来挡住几乎相同的连续成图。"""
    with Image.open(io.BytesIO(data)) as source:
        image = source.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(image.get_flattened_data()) if hasattr(image, "get_flattened_data") else list(image.getdata())
    value = 0
    for y in range(8):
        row = pixels[y * 9:(y + 1) * 9]
        for x in range(8):
            value = (value << 1) | int(row[x] > row[x + 1])
    return f"{value:016x}"


def hash_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


async def _emit(handler: Callable[[dict], Awaitable[None] | None] | None, event: dict) -> None:
    if handler is None:
        return
    outcome = handler(event)
    if asyncio.iscoroutine(outcome):
        await outcome


def _record(manifest, job: Job, provider: BaseProvider) -> None:
    manifest.record(job, provider=provider.name, model=provider.model)
    manifest.save()


async def regenerate_openai_house(
    *,
    cfg,
    provider: BaseProvider,
    store: Store,
    root: Path,
    on_event: Callable[[dict], Awaitable[None] | None] | None = None,
) -> dict:
    """生成房屋中介 6 主题的新版本；只在确认不同于当前成图后再覆盖。"""
    if store.folder_index != HOUSE_STORE_INDEX or len(store.items) != HOUSE_IMAGE_COUNT:
        raise ValueError("OpenAI 专用再生成只支持门店 01 的 6 张主题")
    root = Path(root)
    run_id, cycle = reserve_run(root)
    started = time.monotonic()
    storage = Storage(root, cfg.timestamp_prefix, whiten_bg=cfg.whiten_background, whiten_threshold=cfg.whiten_threshold)
    manifests = ManifestStore(root)
    manifest = manifests.for_store(store.output_dir, {
        "index": store.folder_index, "name": store.folder_name,
    })
    jobs = [job for job in StoreRepository().build_jobs(cfg.prompt_version) if job.store_index == HOUSE_STORE_INDEX]
    if len(jobs) != HOUSE_IMAGE_COUNT:
        raise ValueError("房屋中介任务不是预期的 6 张")
    optimizer = None
    if cfg.prompt_optimizer_enabled and cfg.prompt_optimizer_api_key:
        optimizer = DeepSeekPromptOptimizer(
            api_key=cfg.prompt_optimizer_api_key,
            base_url=cfg.prompt_optimizer_base_url,
            model=cfg.prompt_optimizer_model,
            timeout=min(float(cfg.timeout), 60.0),
        )

    stats = {
        "total": HOUSE_IMAGE_COUNT, "success": 0, "failed": 0, "skipped": 0,
        "calls": 0, "retries": 0, "duplicate_retries": 0, "run_id": run_id,
        "variation_cycle": cycle, "aborted": "",
        "prompt_optimizer": "DeepSeek" if optimizer else "off",
        "prompt_optimizer_calls": 0, "prompt_optimizer_fallbacks": 0,
    }
    await _emit(on_event, {
        "type": "run_started", "total": HOUSE_IMAGE_COUNT, "pending": HOUSE_IMAGE_COUNT,
        "skipped": 0, "provider": provider.describe(), "run_id": run_id,
    })

    for offset, job in enumerate(jobs):
        job.started_at = datetime.now().isoformat(timespec="seconds")
        job.status = JobStatus.RUNNING
        previous_path = storage.target_path(job)
        if not previous_path.is_file():
            # 兼容升级前仍直接位于门店根目录的历史成品。
            legacy_path = storage.store_dir(job.output_dir) / job.file_name
            previous_path = legacy_path if legacy_path.is_file() else previous_path
        previous_hash = ""
        if previous_path.is_file():
            try:
                previous_hash = perceptual_hash(previous_path.read_bytes())
            except Exception:  # noqa: BLE001 - 旧文件损坏时仍允许重新生成
                previous_hash = ""
        await _emit(on_event, {
            "type": "job_started", "uid": job.uid, "store": job.store_index,
            "file": job.file_name, "display": job.display_name,
        })

        accepted: bytes | None = None
        accepted_meta: dict = {}
        last_error = ""
        for duplicate_try in range(MAX_DUPLICATE_RETRIES + 1):
            variation_index, variation_label, instruction = variation_for(cycle, offset, duplicate_try)
            prompt = varied_prompt(job.positive_prompt, run_id, instruction, cfg.realism_iteration)
            optimizer_meta: dict = {"enabled": bool(optimizer), "used": False}
            if optimizer:
                stats["prompt_optimizer_calls"] += 1
                try:
                    optimized = await optimizer.optimize(
                        prompt=prompt,
                        main_title=store.main_title,
                        sub_title=store.sub_title,
                        variation=instruction,
                    )
                    prompt = optimized.prompt
                    optimizer_meta = {
                        "enabled": True, "used": True,
                        "provider": "deepseek", "model": optimized.model,
                        "elapsed": round(optimized.elapsed, 3),
                    }
                except asyncio.CancelledError:
                    raise
                except PromptOptimizerError as exc:
                    # DeepSeek 只提升提示词；不可因为它暂不可用而阻断图片任务。
                    stats["prompt_optimizer_fallbacks"] += 1
                    optimizer_meta = {"enabled": True, "used": False, "fallback": str(exc)}
                    await _emit(on_event, {
                        "type": "log", "line": f"DeepSeek 提示词优化未使用：{exc}；继续使用原始提示词",
                    })
            response = None
            for attempt in range(1, max(1, cfg.retry_max) + 1):
                if stats["calls"] >= cfg.budget_limit:
                    last_error = f"调用上限 {cfg.budget_limit} 已达到，已停止继续请求"
                    break
                try:
                    response = await provider.generate(GenerateRequest(
                        prompt=prompt, size=cfg.size, n=1, extra={"quality": "max"},
                    ))
                    stats["calls"] += 1
                    break
                except asyncio.CancelledError:
                    raise
                except ProviderError as exc:
                    stats["calls"] += 1
                    last_error = str(exc)
                    if not exc.retryable or attempt >= max(1, cfg.retry_max):
                        break
                    stats["retries"] += 1
                    await _emit(on_event, {"type": "job_retry", "uid": job.uid, "attempt": attempt, "error": last_error})
                    wait = cfg.retry_backoff[min(attempt - 1, len(cfg.retry_backoff) - 1)] if cfg.retry_backoff else 0
                    if wait:
                        await asyncio.sleep(wait)
            if response is None:
                break

            candidate = response.images[0]
            if cfg.whiten_background:
                candidate = whiten_background(candidate, cfg.whiten_threshold)
            candidate_hash = perceptual_hash(candidate)
            distance = hash_distance(previous_hash, candidate_hash) if previous_hash else None
            if previous_hash and distance is not None and distance <= NEAR_DUPLICATE_DISTANCE:
                stats["duplicate_retries"] += 1
                last_error = f"新图与上一版过于接近（感知哈希距离 {distance}）"
                if duplicate_try < MAX_DUPLICATE_RETRIES:
                    await _emit(on_event, {"type": "job_retry", "uid": job.uid, "attempt": duplicate_try + 1, "error": last_error})
                    continue
                break

            accepted = candidate
            accepted_meta = {
                **dict(response.raw or {}),
                "run_id": run_id,
                "variation_cycle": cycle,
                "variation_index": variation_index,
                "variation_label": variation_label,
                "variation_instruction": instruction,
                "perceptual_hash": candidate_hash,
                "previous_hash_distance": distance,
                "duplicate_retries": duplicate_try,
                "prompt_optimizer": optimizer_meta,
                "request_prompt": prompt,
            }
            job.attempts = duplicate_try + 1
            job.elapsed = response.elapsed
            break

        if accepted is None:
            job.status = JobStatus.FAILED
            job.error = last_error or "未取得可用的新图"
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            job.runtime_metrics = {"run_id": run_id, "previous_perceptual_hash": previous_hash}
            _record(manifest, job, provider)
            stats["failed"] += 1
            await _emit(on_event, {"type": "job_failed", "uid": job.uid, "store": job.store_index, "file": job.file_name, "error": job.error})
            continue

        tag = f"{job.prompt_version or 'current'}_{provider.model}_{run_id}"
        path = storage.save_image(job, accepted, version_tag=tag)
        job.status = JobStatus.SUCCESS
        job.error = ""
        job.image_path = str(path)
        job.finished_at = datetime.now().isoformat(timespec="seconds")
        job.runtime_metrics = accepted_meta
        job.runtime_metrics["realism_iteration"] = cfg.realism_iteration
        try:
            # ⚠️ 必须走 resolve_render_options（与网页 / 批量流程共用同一处逻辑）。
            #    早期这里直接读 cfg.effect_background_asset，导致背景自动匹配失效、
            #    glass_region 丢失、用户在「效果图微调」里调的参数无效。
            effect = render_storefront_glass(
                path,
                job.store_name,
                **resolve_render_options(cfg, job.store_name),
            )
            effect_path = storage.save_effect_image(job, effect.data, path)
            job.runtime_metrics["effect_image"] = effect.metadata(effect_path)
        except EffectRenderError as exc:
            job.runtime_metrics["effect_image"] = {
                "status": "failed", "renderer": EFFECT_RENDERER_VERSION, "error": str(exc),
            }
        _record(manifest, job, provider)
        stats["success"] += 1
        await _emit(on_event, {
            "type": "job_success", "uid": job.uid, "store": job.store_index,
            "file": path.name,
            "effect_file": job.runtime_metrics.get("effect_image", {}).get("file_name", ""),
            "elapsed": round(job.elapsed, 2), "attempts": job.attempts,
        })

    stats["elapsed"] = round(time.monotonic() - started, 2)
    await _emit(on_event, {"type": "run_finished", **stats})
    return stats
