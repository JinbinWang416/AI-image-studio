# -*- coding: utf-8 -*-
"""
Web 服务（FastAPI）。

启动：python main.py web      →  http://127.0.0.1:8000

界面能力：
  - 23 个门店卡片总览 + 实时进度
  - 一键开始 / 停止 / 断点续跑
  - SSE 实时进度推送
  - 图片预览
  - 实时日志
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import LOCAL_PROFESSIONAL_ROOT, LOCAL_VALIDATION_ROOT, load_config
from ..local_validation import (
    REPORT_NAME,
    VALIDATION_IMAGES,
    VALIDATION_PROMPT_VERSION,
    validation_store,
    write_validation_artifacts,
)
from ..openai_regeneration import HOUSE_STORE_INDEX, regenerate_openai_house
from ..professional_local import (
    CANDIDATES_PER_THEME,
    PROFESSIONAL_VERSION,
    REPORT_NAME as PROFESSIONAL_REPORT_NAME,
    run_professional_validation,
)
from ..logging_setup import get_logger, setup_logging
from ..batches import create_new_batch as create_batch, load_batch_snapshot
from ..security.url_guard import UrlGuardError, validate_outbound_url
from ..effect_renderer import EFFECT_RENDERER_VERSION, EffectRenderError, render_storefront_glass
from ..manifest import ManifestStore
from ..orchestrator import Orchestrator
from ..providers import GenerateRequest, ProviderError, create_provider
from ..providers.catalog import PROVIDER_CATALOG
from ..paths import PACKAGE_ROOT
from ..prompt_profiles import DEFAULT_QUALITY_TEMPLATE, PromptProfileError, validate_template
from ..reference_assets import EffectBackgroundStore, ReferenceAssetError, ReferenceAssetStore, validate_mode_assets
from ..settings import get_store
from ..storage import Storage
from ..store_repo import StoreRepository

log = get_logger("web")

_STATIC_CANDIDATES = (
    # PyInstaller 冻结后 __file__ 指向 _internal/app/web/server.py；
    # 源码运行时也指向 app/web/server.py，因此这是首选位置。
    Path(__file__).resolve().parent / "static",
    PACKAGE_ROOT / "app" / "web" / "static",
)
STATIC_DIR = next((p for p in _STATIC_CANDIDATES if p.is_dir()), _STATIC_CANDIDATES[0])

app = FastAPI(title="AI图片生成", version="0.1.0")

# ---------------------------------------------------------------- 启动自检
# 校验权限码在「定义源 / 路径映射 / 接口引用 / 角色模板」四处是否一致。
# 不一致会导致**静默故障**（某接口永远 403，或权限失效），因此在启动时就拒绝。
# 紧急跳过：环境变量 SHS_SKIP_SELFCHECK=1
from ..security.selfcheck import run_startup_selfcheck  # noqa: E402

run_startup_selfcheck()


# ---------------------------------------------------------------- 存储异常
# 只注册**存储类**异常的处理器，不用 `Exception` 兜底 ——
# 否则会掩盖真实错误、改变 FastAPI 的默认行为。
from ..security.store import StorageError, StorageFullError  # noqa: E402


@app.exception_handler(StorageFullError)
async def _storage_full_handler(request: Request, exc: StorageFullError):
    """磁盘满 → 507，明确告知"数据未损坏，可重试"（不暴露路径与栈）。"""
    log.error("存储空间不足，操作未完成：%s", request.url.path)
    return JSONResponse(
        status_code=507,
        content={
            "detail": "磁盘空间不足，操作未完成。请清理磁盘后重试（原有数据未被破坏）。",
            "code": "storage_full",
            "retryable": True,
        },
    )


@app.exception_handler(StorageError)
async def _storage_error_handler(request: Request, exc: StorageError):
    """其它存储错误 → 500，同样不外泄内部细节。"""
    log.error("安全数据写入失败：%s（%s）", request.url.path, getattr(exc, "code", "?"))
    return JSONResponse(
        status_code=500,
        content={
            "detail": "安全数据写入失败，请查看服务日志。原有数据未被破坏。",
            "code": getattr(exc, "code", "storage_error"),
            "retryable": True,
        },
    )

# ---------------------------------------------------------------- 认证与授权
# 认证路由（登录/登出/初始化/改密/会话/我的设备/MFA）
from .auth_routes import (  # noqa: E402
    current_user,
    optional_user,
    require_permission,
    router as auth_router,
    security as security_service,
)

# 权限校验依赖的返回类型（印刷导出接口用 Depends(require_permission(...))）
from ..security.users import User  # noqa: E402

app.include_router(auth_router)

# 管理员后台（用户 / 角色 / 会话 / 审计 / 健康）
from .admin_routes import router as admin_router  # noqa: E402

app.include_router(admin_router)


@app.get("/admin", response_class=HTMLResponse)
async def admin_console(request: Request) -> HTMLResponse:
    """管理后台页面（文档 §7）。

    页面本身不含数据，所有内容都通过 `/api/admin/*` 拉取（那些接口各自校验权限）。
    这里做一道**入口检查**：没有任何管理权限的用户直接看到 403 页面，
    而不是进去后满屏空标签（文档 §6：不要统一表现为空白页）。
    """
    user = optional_user(request)
    if user is None:
        return HTMLResponse(
            '<meta charset="utf-8"><p>未登录，请先 <a href="/">登录</a>。</p>',
            status_code=401,
        )

    admin_perms = {
        "system.user.manage", "system.role.manage", "system.session.manage",
        "system.audit.read", "system.health.read",
    }
    if not any(security_service.has_permission(user, p) for p in admin_perms):
        return HTMLResponse(
            '<meta charset="utf-8"><h3>没有管理权限</h3>'
            '<p>你的账号没有任何后台管理权限。如需访问，请联系管理员分配相应角色。</p>'
            '<p><a href="/">返回主界面</a></p>',
            status_code=403,
        )

    page = STATIC_DIR / "admin.html"
    if not page.is_file():
        return HTMLResponse('<meta charset="utf-8"><p>管理页面文件缺失。</p>', status_code=500)

    # ⚠️ 必须禁用缓存：本页面由服务端**动态读取**，改动后若浏览器用缓存副本，
    #    会出现「服务端明明返回了新板块、界面上却看不到」的假象（实测踩到过，
    #    排查时以为是路由或权限问题，实际只是缓存）。
    return HTMLResponse(
        page.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )




# ---------------------------------------------------------------- 访问控制
@app.middleware("http")
async def _enforce_access(request, call_next):
    """统一的认证与授权闸门（文档 §2.4 / §2.5 / §4）。

    规则来自 `access_rules.PATH_PERMISSION_RULES`（集中映射，便于审计）：

    · 公开路径（登录页、静态资源）直接放行
    · **未登记的 `/api/*` 一律要求登录** —— 默认拒绝，新增接口不会意外裸露
    · 需要特定权限时，由 `security.has_permission` 判断（唯一入口）
    · 被拒绝的访问**写审计**（文档 §8）

    401 与 403 使用**不同状态码与提示**，前端据此区分展示
    （文档 §6：不得统一表现为菜单消失或空白页）。
    """
    from .access_rules import required_permission_for

    need_login, perm = required_permission_for(request.method, request.url.path)
    if not need_login:
        return await call_next(request)

    user = optional_user(request)
    if user is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "未登录或会话已过期，请重新登录", "code": "unauthenticated"},
        )

    if perm and not security_service.has_permission(user, perm):
        security_service.audit.log(
            action="access.denied",
            module=perm.split(".")[0] if "." in perm else "system",
            result="denied",
            actor_id=user.id,
            actor_name=user.display_name,
            actor_roles=user.roles,
            target_type="endpoint",
            target_id=f"{request.method} {request.url.path}",
            error_kind="forbidden",
            changes={"required": perm},
            ip=(request.client.host if request.client else ""),
        )
        return JSONResponse(
            status_code=403,
            content={"detail": f"没有权限执行该操作（需要 {perm}）", "code": "forbidden",
                     "required": perm},
        )

    return await call_next(request)


# ---------------------------------------------------------------- 业务审计
@app.middleware("http")
async def _audit_operations(request, call_next):
    """自动记录写操作的审计（文档 §8）。

    覆盖**所有** POST/PUT/PATCH/DELETE，无需逐接口埋点 ——
    这样新增接口时不会漏记。只读请求默认不记（避免日志爆炸）。

    ⚠️ 刻意**不读取 request body**：Starlette 的 body 只能消费一次，
    中间件读了接口就拿不到。变更摘要由各接口在业务语义处自行写审计，
    两者通过 `request_id` 关联。
    """
    from .audit_middleware import classify_error, describe_action, should_audit

    path = request.url.path
    if not should_audit(request.method, path):
        return await call_next(request)

    t0 = time.monotonic()
    response = await call_next(request)
    elapsed_ms = round((time.monotonic() - t0) * 1000)

    user = optional_user(request)
    module, action = describe_action(request.method, path)
    ok = response.status_code < 400

    security_service.audit.log(
        action=action,
        module=module,
        result="success" if ok else "error",
        actor_id=user.id if user else "",
        actor_name=user.display_name if user else "",
        actor_roles=user.roles if user else [],
        target_type="endpoint",
        target_id=f"{request.method} {path}",
        error_kind="" if ok else classify_error(response.status_code),
        changes={"status": response.status_code, "elapsed_ms": elapsed_ms},
        ip=(request.client.host if request.client else ""),
    )
    return response


# ---------------------------------------------------------------- 安全响应头
@app.middleware("http")
async def _security_headers(request, call_next):
    """为所有响应注入安全头（文档 §11「安全响应头与实际业务相匹配」）。

    · nosniff          —— 阻止浏览器把响应体当作其它类型解析
    · X-Frame-Options  —— 禁止被 iframe 嵌入（防点击劫持）
    · Referrer-Policy  —— 不向外部泄露本机地址
    · Permissions-Policy —— 关闭本项目不需要的浏览器能力

    说明：本项目前端是同源静态资源，因此未启用严格 CSP；
    若将来引入外部 CDN 或用户可控内容，需要在此补 CSP。
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    )
    return response


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------- 全局状态
class AppState:
    def __init__(self) -> None:
        self.running: bool = False
        self.orchestrator: Orchestrator | None = None
        self.task: asyncio.Task | None = None
        self.effect_task: asyncio.Task | None = None
        self.effects_running: bool = False
        self.provider_label: str = ""
        self.started_at: str = ""
        self.subscribers: set[asyncio.Queue] = set()
        self.history: deque[dict] = deque(maxlen=800)
        self.logs: deque[str] = deque(maxlen=400)
        # 账户欠费会阻断图片生成。保留暂停状态，避免用户在充值前误发更多任务。
        self.account_recovery: dict | None = None

    def publish(self, event: dict) -> None:
        event.setdefault("ts", datetime.now().strftime("%H:%M:%S"))
        self.history.append(event)
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self.subscribers.discard(q)

    def pause_for_account_recovery(
        self, provider: str, batch_id: str, reason: str, code: str,
    ) -> None:
        self.account_recovery = {
            "required": True,
            "verified": False,
            "provider": provider,
            "batch_id": batch_id,
            "reason": reason,
            "code": code,
            "paused_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    def recovery_for(self, provider: str, batch_id: str) -> dict:
        recovery = self.account_recovery
        if not recovery:
            return {"required": False, "verified": False}
        if recovery.get("provider") != provider or recovery.get("batch_id", "") != batch_id:
            return {"required": False, "verified": False}
        return dict(recovery)

    def confirm_account_recovery(self, provider: str) -> None:
        if self.account_recovery and self.account_recovery.get("provider") == provider:
            self.account_recovery["verified"] = True
            self.account_recovery["verified_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def clear_account_recovery(self, provider: str, batch_id: str) -> None:
        if self.recovery_for(provider, batch_id).get("required"):
            self.account_recovery = None


STATE = AppState()


# ---------------------------------------------------------------- 日志桥
class _WebLogHandler:
    """把 logging 记录转发到 Web 日志区。"""

    def __init__(self, state: AppState):
        self.state = state

    def write(self, text: str) -> None:
        line = text.rstrip()
        if line:
            self.state.logs.append(line)
            self.state.publish({"type": "log", "line": line})

    def flush(self) -> None:
        return None


# ---------------------------------------------------------------- 数据视图
def _repo() -> StoreRepository:
    return StoreRepository()


def _ordered_store_indexes(repo: StoreRepository, values) -> list[str]:
    """Normalize a requested scope in dataset order and reject unknown stores."""
    if not isinstance(values, list):
        return []
    requested = {str(value).strip() for value in values if str(value).strip()}
    known = {store.folder_index for store in repo.stores}
    invalid = requested - known
    if invalid:
        raise HTTPException(status_code=400, detail="生成范围包含不存在的门店：" + "、".join(sorted(invalid)))
    return [store.folder_index for store in repo.stores if store.folder_index in requested]


def _active_batch_snapshot(cfg) -> dict:
    """Read immutable metadata for the active batch; legacy outputs have no snapshot."""
    if not cfg.batch_id:
        return {}
    return (load_batch_snapshot(cfg.output_root).get("run_snapshot") or {})


def _scope_for_config(cfg, repo: StoreRepository, snapshot: dict | None = None) -> list[str]:
    snapshot = snapshot if snapshot is not None else _active_batch_snapshot(cfg)
    values = snapshot.get("store_indexes") if snapshot else cfg.selected_store_indexes
    if values is None:
        # Output created before selectable range was introduced must retain the
        # old all-138 behaviour when resumed.
        return [store.folder_index for store in repo.stores]
    return _ordered_store_indexes(repo, values)


def _reference_publics(cfg) -> list[dict]:
    return [
        {key: asset.get(key) for key in ("id", "sha256", "file_name", "mime_type", "bytes", "created_at")}
        for asset in cfg.reference_assets if isinstance(asset, dict) and asset.get("id")
    ]


# 可写入设置 / 批次清单的背景元数据键（**不含 Base64 内容，不含 API Key**）
_BACKGROUND_META_KEYS = (
    "id", "sha256", "file_name", "mime_type", "bytes", "created_at",
    # 附加元数据：区分实拍 / AI 生成，并传递玻璃区标定
    "kind", "label", "glass_region", "provider", "model", "store_hint", "door", "size",
)


def _effect_background_public(asset: object) -> dict:
    """仅把可写入设置 / 批次清单的背景图元数据带出。

    ⚠️ ``kind`` 用于区分 ``real_photo``（用户实拍）与
    ``ai_generated_background``（AI 生成，非实拍），**必须保留**，
    否则界面与清单无法正确标记来源。
    ``glass_region`` 用于保证贴纸不跨门框/竖梃。
    """
    if not isinstance(asset, dict) or not asset.get("id"):
        return {}
    return {
        key: asset.get(key)
        for key in _BACKGROUND_META_KEYS
        if asset.get(key) is not None
    }


def _realism_level(value: object, default: int = 1) -> int:
    try:
        return max(1, min(3, int(value if value is not None else default)))
    except (TypeError, ValueError):
        return max(1, min(3, default))


def _effect_render_options(cfg, store_name: str = "") -> dict:
    """效果图合成选项 —— 委托给 ``app.effect_background.resolve_render_options``。

    ⚠️ 实现已统一到 `app/effect_background.py`，与批量流程
    （`app/orchestrator.py`）共用同一处逻辑，避免两条路径产出不一致。
    详见该函数的文档。
    """
    from ..effect_background import resolve_render_options

    return resolve_render_options(cfg, store_name)


def _apply_snapshot_and_assets(cfg, snapshot: dict | None = None):
    """Return a run-safe config whose references are loaded only in memory."""
    snapshot = snapshot or {}
    image = snapshot.get("image_workflow") if isinstance(snapshot.get("image_workflow"), dict) else {}
    quality = snapshot.get("prompt_quality") if isinstance(snapshot.get("prompt_quality"), dict) else {}
    effect = snapshot.get("effect_workflow") if isinstance(snapshot.get("effect_workflow"), dict) else {}
    generation = snapshot.get("generation") if isinstance(snapshot.get("generation"), dict) else {}
    mode = str(image.get("mode") or cfg.image_mode or "text")
    refs = image.get("reference_assets") if image else cfg.reference_assets
    refs = refs if isinstance(refs, list) else []
    if mode == "text":
        refs = []
    effect_asset = effect.get("background_asset") if effect else cfg.effect_background_asset
    effect_asset = effect_asset if isinstance(effect_asset, dict) else {}
    realism_iteration = _realism_level(
        effect.get("realism_iteration") if effect else cfg.realism_iteration,
        cfg.realism_iteration,
    )
    selected = snapshot.get("store_indexes") if isinstance(snapshot.get("store_indexes"), list) else cfg.selected_store_indexes
    # ⚠️ 服务商与模型也来自快照，避免「续跑时换了模型」把批次搞串味。
    #    老批次（本次修复之前创建的）快照里没有这两个键，此时退回当前设置 ——
    #    不能因为缺字段就报错，否则历史批次全部无法续跑。
    snapshot_provider = str(snapshot.get("active_provider") or "").strip()
    snapshot_model = str(snapshot.get("model") or "").strip()
    cfg = dataclasses.replace(
        cfg,
        provider=snapshot_provider or cfg.provider,
        model=snapshot_model or cfg.model,
        selected_store_indexes=[str(x) for x in selected],
        image_mode=mode,
        reference_assets=list(refs),
        quality_template=str(quality.get("template") or cfg.quality_template or DEFAULT_QUALITY_TEMPLATE),
        optimized_quality_template=str(quality.get("optimized_template") or cfg.optimized_quality_template),
        use_optimized_quality_template=bool(quality.get("use_optimized", cfg.use_optimized_quality_template)),
        realism_iteration=realism_iteration,
        effect_background_asset=_effect_background_public(effect_asset),
        size=str(generation.get("size") or cfg.size),
        prompt_version=str(snapshot.get("prompt_version") or cfg.prompt_version),
    )
    store = ReferenceAssetStore(cfg.output_base_root)
    try:
        assets = store.resolve_many([str(item.get("id") or "") for item in refs if isinstance(item, dict)])
        validate_mode_assets(mode, assets)
    except ReferenceAssetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    runtime_refs = [{**asset.public(), "data_url": asset.data_url()} for asset in assets]
    runtime_effect_asset: dict = {}
    if effect_asset.get("id"):
        try:
            effect_asset_obj = EffectBackgroundStore(cfg.output_base_root).get(str(effect_asset["id"]))
        except ReferenceAssetError as exc:
            raise HTTPException(status_code=400, detail=f"实拍门店背景不可用：{exc}") from exc
        runtime_effect_asset = {**effect_asset_obj.public(), "path": str(effect_asset_obj.path)}
    if mode != "text" and cfg.provider not in {"qwen", "openai", "gemini", "seedream"}:
        raise HTTPException(status_code=400, detail="图生图和多图生图目前仅支持千问 3.0 系列、OpenAI GPT Image、Gemini 与 Seedream")
    if mode != "text" and cfg.provider == "qwen" and cfg.model not in {"qwen-image-3.0", "qwen-image-3.0-pro"}:
        raise HTTPException(status_code=400, detail="千问图像参考模式需要选择 qwen-image-3.0 或 qwen-image-3.0-pro")
    return dataclasses.replace(cfg, reference_assets=runtime_refs, effect_background_asset=runtime_effect_asset)


def _new_batch_snapshot(cfg, store_indexes: list[str]) -> dict:
    return {
        "store_indexes": store_indexes,
        "prompt_version": cfg.prompt_version or "current",
        # ⚠️ 必须冻结**服务商与模型**。
        #
        #    否则「继续当前批次」会拿**当前设置**去建 provider ——
        #    用户中途换了模型（或换了服务商）再续跑，同一个批次里就会混进
        #    两种模型的产物，而批次快照本该是不可变的。
        #    这正是 AGENTS.md「不可破坏的行为」第 1 条：
        #      「开始/继续当前批次」只执行当前批次快照中的未完成任务；
        #        它不能被后来修改的范围、模型或比例改变。
        "active_provider": cfg.provider,
        "model": cfg.model,
        "generation": {"size": cfg.size},
        "image_workflow": {"mode": cfg.image_mode, "reference_assets": _reference_publics(cfg)},
        "prompt_quality": {
            "template": cfg.quality_template or DEFAULT_QUALITY_TEMPLATE,
            "optimized_template": cfg.optimized_quality_template,
            "use_optimized": cfg.use_optimized_quality_template,
        },
        "effect_workflow": {
            "background_asset": _effect_background_public(cfg.effect_background_asset),
            "realism_iteration": _realism_level(cfg.realism_iteration),
        },
    }


def _providers_summary() -> dict:
    """各服务商的配置状态摘要（供顶栏只读展示与设置弹窗使用）。"""
    store = get_store()
    data = store.load()
    out: dict = {}
    for name, cat in PROVIDER_CATALOG.items():
        stored = (data.get("providers", {}) or {}).get(name, {}) or {}
        env_key = cat.get("env_key") or ""
        has_key = bool(stored.get("api_key")) or bool(env_key and os.environ.get(env_key))
        models = cat.get("models") or []
        out[name] = {
            "label": cat.get("label", name),
            "vendor": cat.get("vendor", ""),
            "kind": cat.get("kind", "cloud"),
            "badge": cat.get("badge", ""),
            "recommended": bool(cat.get("recommended")),
            "model": stored.get("model") or cat.get("default_model", ""),
            "price": (models[0].get("price", 0.0) if models else 0.0),
            "has_key": has_key,
        }
    return out


def _safe_manifest_png(raw_path: str, root: Path) -> Path | None:
    """读取 manifest 中的 PNG 路径，但只接受当前门店根目录内的文件。"""
    if not raw_path:
        return None
    try:
        candidate = Path(raw_path).resolve()
        candidate.relative_to(root.resolve())
        return candidate if candidate.is_file() and candidate.suffix.lower() == ".png" else None
    except (OSError, ValueError):
        return None


def _preview_image(store_dir: Path, generated_dir: Path, file_name: str, entry: dict) -> tuple[str, bool]:
    """Resolve the real image file for a card without trusting manifest paths.

    Timestamp-prefixed output deliberately does not use ``file_name`` as its
    on-disk name.  The manifest records the path written by ``Storage``; use
    it when it remains inside the active store folder, then fall back to a
    timestamp lookup for manifests produced by an older version.
    """
    root = store_dir.resolve()
    candidate = _safe_manifest_png(str(entry.get("image_path") or "").strip(), root)
    if candidate:
        return candidate.name, True

    canonical = generated_dir / file_name
    if canonical.is_file() and canonical.suffix.lower() == ".png":
        return canonical.name, True

    # 旧批次兼容：原图尚在门店根目录。
    canonical = store_dir / file_name
    if canonical.is_file() and canonical.suffix.lower() == ".png":
        return canonical.name, True

    stem = Path(file_name).stem
    matches: list[Path] = []
    if generated_dir.exists():
        matches.extend(p for p in generated_dir.glob(f"*_{stem}.png") if p.is_file())
    matches.extend(p for p in store_dir.glob(f"*_{stem}.png") if p.is_file())
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return (matches[0].name, True) if matches else (file_name, False)


def _preview_effect(store_dir: Path, effect_dir: Path, image_file: str, entry: dict) -> tuple[str, bool, str, str]:
    """返回效果图文件、存在性、状态和错误信息。"""
    effect_meta = (entry.get("runtime_metrics") or {}).get("effect_image") or {}
    candidate = _safe_manifest_png(str(effect_meta.get("path") or "").strip(), store_dir)
    if candidate and candidate.parent == effect_dir.resolve():
        return candidate.name, True, "success", ""
    if image_file:
        expected = effect_dir / f"{Path(image_file).stem}_效果图.png"
        if expected.is_file():
            return expected.name, True, "success", ""
    return (
        str(effect_meta.get("file_name") or ""),
        False,
        str(effect_meta.get("status") or "pending"),
        str(effect_meta.get("error") or ""),
    )


def _restore_account_recovery_from_manifests(cfg, manifests: ManifestStore, stores) -> None:
    """Restore a billing pause after the local web server is restarted.

    ``AppState`` intentionally lives in memory, while a paused job is persisted
    in the batch manifest.  Without this bridge, restarting the local server
    would accidentally remove the recovery guard and allow an unverified retry.
    A successful validation remains trusted only for the current server session;
    after a restart the user validates the image capability once more.
    """
    if STATE.running or STATE.recovery_for(cfg.provider, cfg.batch_id).get("required"):
        return
    for store in stores:
        manifest = manifests.for_store(store.output_dir)
        for entry in manifest.entries.values():
            if entry.get("status") != "paused":
                continue
            entry_provider = str(entry.get("provider") or cfg.provider)
            if entry_provider != cfg.provider:
                continue
            STATE.pause_for_account_recovery(
                cfg.provider,
                cfg.batch_id,
                str(entry.get("error") or "账户额度不足"),
                "ACCOUNT_ARREARAGE",
            )
            return


def build_state_payload(cfg=None) -> dict:
    """汇总当前整体状态，供前端渲染。"""
    cfg = cfg or load_config()
    repo = _repo()
    batch_snapshot = _active_batch_snapshot(cfg)
    effect_snapshot = batch_snapshot.get("effect_workflow") if isinstance(batch_snapshot.get("effect_workflow"), dict) else {}
    current_effect_asset = effect_snapshot.get("background_asset") if effect_snapshot else cfg.effect_background_asset
    current_realism = _realism_level(
        effect_snapshot.get("realism_iteration") if effect_snapshot else cfg.realism_iteration,
        cfg.realism_iteration,
    )
    active_scope = _scope_for_config(cfg, repo, batch_snapshot)
    # Keep the editable default separate from the immutable current-batch
    # snapshot.  The UI uses this for the next-batch button count.
    next_scope = _scope_for_config(cfg, repo, {})
    active_scope_set = set(active_scope)
    storage = Storage(cfg.output_root, cfg.timestamp_prefix)
    manifests = ManifestStore(cfg.output_root)
    _restore_account_recovery_from_manifests(cfg, manifests, repo.stores)

    stores_payload = []
    total_done = 0
    for s in repo.stores:
        m = manifests.for_store(s.output_dir)
        d = storage.store_dir(s.output_dir)
        generated_dir = storage.generated_dir(s.output_dir)
        effect_dir = storage.effect_dir(s.output_dir)
        items = []
        for it in s.items:
            entry = m.entries.get(it.pic_index, {})
            image_file, exists = _preview_image(d, generated_dir, it.file_name, entry)
            effect_file, effect_exists, effect_status, effect_error = _preview_effect(
                d, effect_dir, image_file, entry
            )
            recorded_status = entry.get("status")
            status = recorded_status or ("success" if exists else "pending")
            # A manifest alone must never make a missing image appear done.
            # The next "开始/继续" will regenerate this item.
            if status in ("success", "skipped") and not exists:
                status = "pending"
            items.append({
                "pic_index": it.pic_index,
                "theme": it.theme,
                "subject": it.subject,
                "prompt_preview": it.positive_prompt,
                "file_name": it.file_name,
                "image_file": image_file,
                "effect_file": effect_file,
                "effect_exists": effect_exists,
                "effect_status": effect_status,
                "effect_error": effect_error,
                "status": status,
                "attempts": entry.get("attempts", 0),
                "elapsed": entry.get("elapsed", 0),
                "error": entry.get("error", "") or (
                    "图片文件缺失；点击开始/继续会补生成" if recorded_status in ("success", "skipped") and not exists else ""
                ),
                "exists": exists,
            })
            if s.folder_index in active_scope_set and (status in ("success", "skipped") or exists):
                total_done += 1

        stores_payload.append({
            "folder_index": s.folder_index,
            "folder_name": s.folder_name,
            "output_dir": s.output_dir,
            "main_title": s.main_title,
            "sub_title": s.sub_title,
            "color_theme": s.color_theme,
            "pdd_title": s.pdd_title,
            "compliance_note": s.compliance_note,
            "items": items,
        })

    return {
        "running": STATE.running,
        "effects_running": STATE.effects_running,
        "config": {
            "provider": cfg.provider,
            "provider_label": cfg.provider_label,
            "model": cfg.model,
            "concurrency": cfg.concurrency,
            "rpm_limit": cfg.rpm_limit,
            "price_per_image": cfg.price_per_image,
            "output_root": str(cfg.output_root),
            "output_base_root": str(cfg.output_base_root),
            "batch_id": cfg.batch_id,
            "batch_created_at": cfg.batch_created_at,
            "timestamp_prefix": cfg.timestamp_prefix,
            "qc_enabled": cfg.qc_enabled,
            "api_key_ok": bool(cfg.api_key) or cfg.is_local,
            "run_limit": cfg.run_limit,
            "size": (batch_snapshot.get("generation") or {}).get("size") or cfg.size,
            "image_mode": ((batch_snapshot.get("image_workflow") or {}).get("mode") or cfg.image_mode),
            "realism_iteration": current_realism,
            "effect_background": _effect_background_public(current_effect_asset),
        },
        "providers": _providers_summary(),
        "progress": {
            "total": len(active_scope) * 6,
            "done": total_done,
            "percent": round(total_done / (len(active_scope) * 6) * 100, 1) if total_done and active_scope else 0.0,
            "complete": bool(active_scope) and total_done == len(active_scope) * 6,
        },
        "batch": {
            "id": cfg.batch_id,
            "label": (f"批次 {cfg.batch_id[6:]}" if cfg.batch_id.startswith("batch_") else "原始输出目录"),
            "created_at": cfg.batch_created_at,
            "is_legacy": not bool(cfg.batch_id),
            "base_root": str(cfg.output_base_root),
            "run_snapshot": batch_snapshot,
        },
        "scope": {
            "store_indexes": active_scope,
            "store_count": len(active_scope),
            "image_count": len(active_scope) * 6,
            "snapshot": bool(batch_snapshot),
        },
        "next_scope": {
            "store_indexes": next_scope,
            "store_count": len(next_scope),
            "image_count": len(next_scope) * 6,
        },
        "recovery": STATE.recovery_for(cfg.provider, cfg.batch_id),
        "stores": stores_payload,
    }


# ---------------------------------------------------------------- 路由
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    f = STATIC_DIR / "index.html"
    if not f.exists():
        return HTMLResponse("<h1>缺少前端文件 app/web/static/index.html</h1>", status_code=500)
    # The page is served from a long-lived local browser tab.  Give app.js a
    # content version and prevent the HTML shell from being reused after a
    # server restart, otherwise a deployed fix can appear not to have updated.
    #
    # ⚠️ 版本号**统一由文件 mtime 自动生成**，不再手写。
    #    起因：多次出现"改了 CSS 但忘记提升 ?v= → 浏览器用缓存 → 误判为修复无效"。
    #    自动生成后，改动任何静态资源都会立即生效，无需人工维护版本号。
    def _stamp(rel: str) -> str:
        p = STATIC_DIR / rel
        try:
            return str(p.stat().st_mtime_ns) if p.exists() else "0"
        except OSError:
            return "0"

    html = f.read_text(encoding="utf-8")
    html = re.sub(
        r"(/static/[A-Za-z0-9_.\-]+\.(?:css|js))\?v=[^\"']*",
        lambda m: f"{m.group(1)}?v={_stamp(m.group(1).rsplit('/', 1)[-1])}",
        html,
    )
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(build_state_payload())


@app.get("/api/logs")
async def api_logs() -> JSONResponse:
    return JSONResponse({"lines": list(STATE.logs)[-200:]})


@app.get("/api/events")
async def api_events() -> StreamingResponse:
    """SSE 事件流。"""
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    STATE.subscribers.add(q)

    async def gen():
        try:
            for e in list(STATE.history)[-60:]:
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
            while True:
                try:
                    e = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            STATE.subscribers.discard(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _request_run_limit(payload: dict, cfg, *, create_new_batch: bool) -> int:
    """Choose the scope limit for a web run.

    A normal continue run honors the Settings trial limit.  Creating a new
    batch is the explicit regeneration action and defaults to every image in
    its selected store scope.  An explicit request limit is retained for the internal API/test
    surface.
    """
    if create_new_batch:
        return int(payload["limit"]) if "limit" in payload else 0
    return int(payload.get("limit") or cfg.run_limit or 0)


def _require_confirmed_account_recovery(cfg) -> bool:
    """Require a successful image-capability test after a billing/credit pause.

    Returns ``True`` when this is the first run after successful recovery, so
    the caller can lift a small trial limit and finish the interrupted batch.
    """
    recovery = STATE.recovery_for(cfg.provider, cfg.batch_id)
    if not recovery.get("required"):
        return False
    if not recovery.get("verified"):
        raise HTTPException(
            status_code=409,
            detail="当前批次因账户欠费或额度不足而暂停。充值后请在设置中先测试连接，再验证图片出图权限；验证成功后再继续当前批次。",
        )
    # A new account block will create a new pause record and require a new test.
    STATE.clear_account_recovery(cfg.provider, cfg.batch_id)
    return True


def _require_usable_provider(cfg) -> None:
    """在**创建批次之前**确认当前服务商真的可用。

    ⚠️ 不要等到 `create_provider()` 才失败 —— 那个调用点在批次创建 / active_batch
       切换之后，一旦抛错就会留下一个空批次（用户反馈过：点了「重新生成（新批次）」
       看到报错，但活动批次已经被切走、真实感档位也已递增）。

    这里只做**解析**（与 `create_provider()` 同一条路径），不实例化、
       不读 Key、不产生任何副作用。
    """
    from ..providers import get_provider

    try:
        get_provider(cfg.provider)
    except ProviderError as exc:
        # 余额/鉴权之类不在这里；这里主要是「未知服务商」与
        # 「适配器尚未实现」（kling / zhipu）
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=f"服务商「{cfg.provider}」不可用：{exc}",
        ) from exc


async def _start_full_run(
    payload: dict | None = None,
    *,
    create_new_batch: bool = False,
) -> JSONResponse:
    """启动当前批次，或为当前勾选范围创建一个独立新批次。"""
    if STATE.running:
        raise HTTPException(status_code=409, detail="已有任务在运行中")

    payload = payload or {}
    provider_name = payload.get("provider")
    cfg = load_config(provider_name)

    if cfg.provider == "flux_local":
        raise HTTPException(
            status_code=400,
            detail="FLUX 本地服务商只能执行固定的门店 01 / V8 / 6 张验证，请使用“运行本地样图验证”。",
        )
    repo = _repo()
    issues = repo.validate()
    if issues:
        raise HTTPException(status_code=400, detail=f"数据包校验失败：{issues[0]}")

    batch = None
    if create_new_batch:
        cfg = _apply_snapshot_and_assets(cfg)
        # 新批次只向上提高真实感档位；达到 V3 后保持专业实拍级规则，避免
        # 在没有证据的情况下承诺无上限的“更真实”。
        next_realism = min(3, _realism_level(cfg.realism_iteration) + 1)
        cfg = dataclasses.replace(cfg, realism_iteration=next_realism)
        run_scope = _scope_for_config(cfg, repo, {})

        # ⚠️ 所有前置校验必须在**产生副作用之前**做完。
        #
        #    早先的顺序是「save(档位) → create_batch() → … → validate()」，
        #    于是「服务商没实现」「API Key 没填」这类失败会留下三样东西：
        #      ① 真实感档位已经被永久提高（写进了 settings.json）
        #      ② 空批次目录已落盘
        #      ③ active_batch 已切到这个空批次
        #    后续「继续当前批次」的语义随之被破坏 —— 用户点一次报错，
        #    批次却被换掉了。
        if not run_scope:
            raise HTTPException(status_code=400, detail="请先在“生成范围”至少勾选一个门店")
        problems = cfg.validate()
        if problems:
            raise HTTPException(status_code=400, detail="；".join(problems))
        _require_usable_provider(cfg)

        # ---- 校验全部通过，到这里才开始产生副作用 ----
        get_store().save({"prompt_quality": {"realism_iteration": next_realism}})
        batch = create_batch(cfg, get_store(), _new_batch_snapshot(cfg, run_scope))
        cfg = dataclasses.replace(
            cfg,
            output_root=batch.path,
            batch_id=batch.batch_id,
            batch_created_at=batch.created_at,
        )
    else:
        snapshot = _active_batch_snapshot(cfg)
        cfg = _apply_snapshot_and_assets(cfg, snapshot)
        run_scope = _scope_for_config(cfg, repo, snapshot)
        if not run_scope:
            raise HTTPException(status_code=400, detail="当前生成范围为空，请在设置中勾选门店后创建新批次")
        # 同上：校验先于任何副作用（这个分支没有副作用，但保持一致）
        problems = cfg.validate()
        if problems:
            raise HTTPException(status_code=400, detail="；".join(problems))
        _require_usable_provider(cfg)

    recovery_confirmed = _require_confirmed_account_recovery(cfg)

    storage = Storage(
        cfg.output_root, cfg.timestamp_prefix,
        whiten_bg=cfg.whiten_background, whiten_threshold=cfg.whiten_threshold,
    )
    selected_stores = [store for store in repo.stores if store.folder_index in set(run_scope)]
    storage.prepare_directories(selected_stores)
    manifests = ManifestStore(cfg.output_root)
    provider = create_provider(cfg)
    orch = Orchestrator(cfg, provider, repo, storage, manifests)

    STATE.orchestrator = orch
    STATE.provider_label = provider.describe()
    STATE.running = True
    STATE.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def on_event(ev: dict) -> None:
        if ev.get("type") == "run_paused":
            STATE.pause_for_account_recovery(
                cfg.provider, cfg.batch_id,
                str(ev.get("reason") or "账户额度不足"), str(ev.get("code") or ""),
            )
        STATE.publish(ev)

    orch.on_event(on_event)

    # A user who has recharged should not need to click through the old
    # "试跑 N 张" setting repeatedly.  Resume every remaining job in the
    # interrupted batch; new batches and normal runs keep their normal scope.
    resumed_after_recharge = recovery_confirmed and not create_new_batch
    limit = 0 if resumed_after_recharge else _request_run_limit(payload, cfg, create_new_batch=create_new_batch)

    async def runner() -> None:
        try:
            await asyncio.to_thread(setup_logging)
            stats = await orch.run(
                store_indexes=run_scope,
                limit=limit,
            )
            # 记录本次运行
            from ..runs import append_run
            append_run(
                provider=cfg.provider,
                provider_label=cfg.provider_label,
                model=cfg.model,
                prompt_version=cfg.prompt_version or "current",
                price_per_image=cfg.price_per_image,
                stats=stats.to_dict(),
                scope=(
                    "充值恢复续跑 · 补齐未完成项" if resumed_after_recharge else
                    (f"新批次 {batch.batch_id} · {len(run_scope) * 6} 张" if batch else f"当前批次范围 · {len(run_scope) * 6} 张")
                    if not limit
                    else f"试跑 {limit} 张"
                ),
                output_root=str(cfg.output_root),
            )
        except asyncio.CancelledError:
            STATE.publish({"type": "run_cancelled", "batch_id": cfg.batch_id})
            raise
        except Exception as e:                       # noqa: BLE001
            log.exception("运行失败")
            STATE.publish({"type": "run_error", "error": str(e)})
        finally:
            await provider.close()
            STATE.running = False
            STATE.orchestrator = None

    STATE.task = asyncio.create_task(runner())
    return JSONResponse({
        "ok": True,
        "provider": provider.describe(),
        "batch": (
            {"id": batch.batch_id, "label": batch.label, "output_root": str(batch.path)}
            if batch else
            {"id": cfg.batch_id, "output_root": str(cfg.output_root)}
        ),
        "scope": {"store_indexes": run_scope, "image_count": len(run_scope) * 6},
        "resumed_after_recharge": resumed_after_recharge,
    })


@app.post("/api/run")
async def api_run(payload: dict | None = None) -> JSONResponse:
    """继续当前批次：只补未完成或失败图片。"""
    return await _start_full_run(payload, create_new_batch=False)


@app.post("/api/run/new-batch")
async def api_run_new_batch(payload: dict | None = None) -> JSONResponse:
    """创建独立输出目录并执行当前勾选门店的图片任务。"""
    return await _start_full_run(payload, create_new_batch=True)


@app.post("/api/stop")
async def api_stop() -> JSONResponse:
    if not STATE.running:
        raise HTTPException(status_code=409, detail="当前没有运行中的任务")
    if STATE.orchestrator:
        STATE.orchestrator.cancel()
    if STATE.task:
        STATE.task.cancel()
    STATE.publish({"type": "stopping"})
    return JSONResponse({"ok": True})


@app.get("/api/image")
async def api_image(store: str, file: str, view: str = "generated") -> FileResponse:
    """图片预览。做了路径穿越防护。

    支持的 view：

        generated  生成图（PNG，原地保留，未被印刷导出改动）
        effect     玻璃效果图（PNG）
        preview    印刷预览（JPEG，来自 印刷TIF 同级的「预览/」目录）

    ⚠️ 印刷导出**不会移动或删除**原 PNG（采用硬链接归档），
       所以 generated / effect 两条路径始终可用，前端无需改动。
       preview 是**新增**的可选视图，浏览器打不开 TIF，所以另出一张 JPEG。
    """
    cfg = load_config()
    if Path(file).name != file or view not in {"generated", "effect", "preview"}:
        raise HTTPException(status_code=403, detail="非法路径")
    storage = Storage(cfg.output_root, cfg.timestamp_prefix)
    if store not in {s.output_dir for s in _repo().stores}:
        raise HTTPException(status_code=404, detail="门店不存在")

    if view == "preview":
        target = storage.store_dir(store) / "预览" / file
        if not target.is_file() or target.suffix.lower() not in {".jpg", ".jpeg"}:
            raise HTTPException(status_code=404, detail="预览图不存在")
        return FileResponse(target, media_type="image/jpeg")

    parent = storage.effect_dir(store) if view == "effect" else storage.generated_dir(store)
    target = parent / file
    # 迁移前的生成图仍可预览；效果图只读取专属目录。
    if view == "generated" and not target.exists():
        target = storage.store_dir(store) / file
    if not target.is_file() or target.suffix.lower() != ".png":
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(target, media_type="image/png")


def _missing_effect_count(cfg) -> int:
    """计算当前批次已有生成图但尚无效果图的数量。"""
    repo = _repo()
    selected = set(_scope_for_config(cfg, repo, _active_batch_snapshot(cfg)))
    storage = Storage(cfg.output_root, cfg.timestamp_prefix)
    manifests = ManifestStore(cfg.output_root)
    missing = 0
    for store in repo.stores:
        if store.folder_index not in selected:
            continue
        manifest = manifests.for_store(store.output_dir)
        root, generated, effects = (
            storage.store_dir(store.output_dir), storage.generated_dir(store.output_dir), storage.effect_dir(store.output_dir)
        )
        for item in store.items:
            file_name, exists = _preview_image(root, generated, item.file_name, manifest.entries.get(item.pic_index, {}))
            if not exists:
                continue
            _, effect_exists, _, _ = _preview_effect(root, effects, file_name, manifest.entries.get(item.pic_index, {}))
            missing += int(not effect_exists)
    return missing


def _generate_missing_effects(cfg, emit) -> dict:
    """为当前批次中已落盘的生成图补齐本地玻璃效果图。"""
    cfg = _apply_snapshot_and_assets(cfg, _active_batch_snapshot(cfg))
    repo = _repo()
    selected = set(_scope_for_config(cfg, repo, _active_batch_snapshot(cfg)))
    storage = Storage(cfg.output_root, cfg.timestamp_prefix)
    manifests = ManifestStore(cfg.output_root)
    stats = {"total": 0, "success": 0, "skipped": 0, "failed": 0}
    for store in repo.stores:
        if store.folder_index not in selected:
            continue
        manifest = manifests.for_store(store.output_dir)
        root, generated, effects = (
            storage.store_dir(store.output_dir), storage.generated_dir(store.output_dir), storage.effect_dir(store.output_dir)
        )
        jobs = {job.pic_index: job for job in repo.build_jobs(cfg.prompt_version) if job.store_index == store.folder_index}
        for item in store.items:
            entry = manifest.entries.get(item.pic_index, {})
            image_file, exists = _preview_image(root, generated, item.file_name, entry)
            if not exists:
                continue
            stats["total"] += 1
            effect_file, effect_exists, _, _ = _preview_effect(root, effects, image_file, entry)
            if effect_exists:
                stats["skipped"] += 1
                continue
            source = _safe_manifest_png(str(entry.get("image_path") or ""), root)
            if not source:
                candidate = generated / image_file
                source = candidate if candidate.is_file() else root / image_file
            job = jobs.get(item.pic_index)
            if not job or not source.is_file():
                stats["failed"] += 1
                continue
            try:
                rendered = render_storefront_glass(source, store.folder_name, **_effect_render_options(cfg, store.main_title))
                effect_path = storage.save_effect_image(job, rendered.data, source)
                runtime = dict(entry.get("runtime_metrics") or {})
                runtime["effect_image"] = rendered.metadata(effect_path)
                entry["runtime_metrics"] = runtime
                manifest.entries[item.pic_index] = entry
                manifest.save()
                stats["success"] += 1
                emit({"type": "effect_ready", "store": store.folder_index, "file": image_file, "effect_file": effect_path.name})
            except EffectRenderError as exc:
                runtime = dict(entry.get("runtime_metrics") or {})
                runtime["effect_image"] = {
                    "status": "failed", "renderer": EFFECT_RENDERER_VERSION, "error": str(exc),
                }
                entry["runtime_metrics"] = runtime
                manifest.entries[item.pic_index] = entry
                manifest.save()
                stats["failed"] += 1
                emit({"type": "effect_failed", "store": store.folder_index, "file": image_file, "error": str(exc)})
    return stats


@app.post("/api/effects/generate")
async def api_generate_missing_effects() -> JSONResponse:
    """本地补齐当前批次的效果图，不请求付费模型。"""
    if STATE.running:
        raise HTTPException(status_code=409, detail="图片生成正在运行，新图会自动创建效果图，请等待当前批次结束")
    if STATE.effects_running:
        raise HTTPException(status_code=409, detail="效果图正在生成，请稍候")
    cfg = load_config()
    missing = _missing_effect_count(cfg)
    if not missing:
        return JSONResponse({"ok": True, "started": False, "missing": 0, "message": "当前批次的效果图已经齐全"})
    STATE.effects_running = True
    STATE.publish({"type": "effect_run_started", "pending": missing})
    loop = asyncio.get_running_loop()

    def emit(event: dict) -> None:
        loop.call_soon_threadsafe(STATE.publish, event)

    async def runner() -> None:
        try:
            stats = await asyncio.to_thread(_generate_missing_effects, cfg, emit)
            STATE.publish({"type": "effect_run_finished", **stats})
        finally:
            STATE.effects_running = False

    STATE.effect_task = asyncio.create_task(runner())
    return JSONResponse({"ok": True, "started": True, "missing": missing})


# ================================================================ FLUX 本地样图验证
def _local_validation_config():
    """构造不可被网页常规设置放大的固定本地验证配置。"""
    cfg = load_config("flux_local")
    return dataclasses.replace(
        cfg,
        output_root=LOCAL_VALIDATION_ROOT,
        concurrency=1,
        retry_max=1,
        retry_backoff=[0.0],
        timeout=max(600.0, float(cfg.timeout)),
        prompt_version=VALIDATION_PROMPT_VERSION,
        budget_limit=VALIDATION_IMAGES,
        overwrite=True,
        timestamp_prefix=False,
        whiten_background=True,
    )


async def _local_flux_health(cfg) -> dict:
    provider = create_provider(cfg)
    try:
        return await provider.health()
    finally:
        await provider.close()


@app.get("/api/local-validation/state")
async def api_local_validation_state() -> JSONResponse:
    cfg = _local_validation_config()
    health = await _local_flux_health(cfg)
    repo = _repo()
    store = validation_store(repo.stores)
    report_path = LOCAL_VALIDATION_ROOT / REPORT_NAME
    report = None
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = None
    return JSONResponse({
        "running": STATE.running and STATE.provider_label.startswith("FLUX.2 klein 4B"),
        "health": health,
        "scope": {"store": store.folder_index, "store_name": store.folder_name, "prompt_version": VALIDATION_PROMPT_VERSION, "images": VALIDATION_IMAGES, "concurrency": 1},
        "output_root": str(LOCAL_VALIDATION_ROOT),
        "report": report,
    })


@app.post("/api/local-validation/run")
async def api_local_validation_run() -> JSONResponse:
    if STATE.running:
        raise HTTPException(status_code=409, detail="已有任务在运行中")
    cfg = _local_validation_config()
    repo = _repo()
    store = validation_store(repo.stores)
    if len(store.items) != VALIDATION_IMAGES:
        raise HTTPException(status_code=400, detail="门店 01 的样图数量不是预期的 6 张")
    if any(item.prompt_version != VALIDATION_PROMPT_VERSION and VALIDATION_PROMPT_VERSION not in item.prompt_history for item in store.items):
        raise HTTPException(status_code=400, detail="数据包缺少 V8 提示词，不能启动本地验证")
    health = await _local_flux_health(cfg)
    if not health.get("ok"):
        raise HTTPException(status_code=503, detail=health.get("message", "本地 FLUX 模型未就绪"))

    storage = Storage(
        cfg.output_root, cfg.timestamp_prefix,
        whiten_bg=cfg.whiten_background, whiten_threshold=cfg.whiten_threshold,
    )
    storage.prepare_directories([store])
    manifests = ManifestStore(cfg.output_root)
    provider = create_provider(cfg)
    orch = Orchestrator(cfg, provider, repo, storage, manifests)
    STATE.orchestrator = orch
    STATE.provider_label = provider.describe()
    STATE.running = True
    STATE.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    orch.on_event(lambda event: STATE.publish(event))

    async def runner() -> None:
        try:
            await asyncio.to_thread(setup_logging)
            stats = await orch.run(store_indexes=[store.folder_index], limit=VALIDATION_IMAGES)
            report = write_validation_artifacts(store, cfg.output_root)
            from ..runs import append_run
            append_run(
                provider=cfg.provider, provider_label=cfg.provider_label, model=cfg.model,
                prompt_version=VALIDATION_PROMPT_VERSION, price_per_image=0.0,
                stats=stats.to_dict(), scope="本地验证：门店 01 / V8 / 6 张 / 单并发",
                output_root=str(cfg.output_root),
            )
            STATE.publish({"type": "local_validation_finished", "grade": report["grade"], **stats.to_dict()})
        except Exception as exc:  # noqa: BLE001
            log.exception("FLUX 本地样图验证失败")
            STATE.publish({"type": "run_error", "error": str(exc)})
        finally:
            await provider.close()
            STATE.running = False
            STATE.orchestrator = None

    STATE.task = asyncio.create_task(runner())
    return JSONResponse({"ok": True, "provider": provider.describe(), "scope": "门店 01 / V8 / 6 张 / 单并发", "output_root": str(cfg.output_root)})


@app.get("/api/local-validation/image")
async def api_local_validation_image(file: str) -> FileResponse:
    store = validation_store(_repo().stores)
    allowed = {item.file_name for item in store.items}
    if file not in allowed:
        raise HTTPException(status_code=403, detail="不是本地验证范围内的图片")
    target = (Storage(LOCAL_VALIDATION_ROOT).generated_dir(store.output_dir) / file).resolve()
    root = LOCAL_VALIDATION_ROOT.resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise HTTPException(status_code=404, detail="本地验证图片不存在")
    return FileResponse(target, media_type="image/png")


# ================================================================ V9 专业化无字底图 + 中文排版
def _professional_config():
    cfg = _local_validation_config()
    return dataclasses.replace(cfg, output_root=LOCAL_PROFESSIONAL_ROOT, budget_limit=18)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, json.JSONDecodeError):
        return None


@app.get("/api/professional-validation/state")
async def api_professional_validation_state() -> JSONResponse:
    cfg = _professional_config()
    health = await _local_flux_health(cfg)
    store = validation_store(_repo().stores)
    return JSONResponse({
        "running": STATE.running and STATE.provider_label.startswith("FLUX V9"),
        "health": health,
        "scope": {"store": store.folder_index, "store_name": store.folder_name, "version": PROFESSIONAL_VERSION, "images": len(store.items), "candidates_per_theme": CANDIDATES_PER_THEME, "concurrency": 1},
        "output_root": str(LOCAL_PROFESSIONAL_ROOT),
        "report": _read_json(LOCAL_PROFESSIONAL_ROOT / PROFESSIONAL_REPORT_NAME),
    })


@app.post("/api/professional-validation/run")
async def api_professional_validation_run() -> JSONResponse:
    if STATE.running:
        raise HTTPException(status_code=409, detail="已有任务在运行中")
    cfg = _professional_config()
    repo = _repo()
    store = validation_store(repo.stores)
    health = await _local_flux_health(cfg)
    if not health.get("ok"):
        raise HTTPException(status_code=503, detail=health.get("message", "本地 FLUX 模型未就绪"))
    provider = create_provider(cfg)
    STATE.orchestrator = None
    STATE.provider_label = "FLUX V9 专业化本地验证"
    STATE.running = True
    STATE.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def runner() -> None:
        try:
            report = await run_professional_validation(provider, store, cfg.output_root, STATE.publish)
            from ..runs import append_run
            candidates = [candidate for row in report["rows"] for candidate in row["candidates"]]
            success = sum(not candidate.get("error") for candidate in candidates)
            append_run(
                provider=cfg.provider, provider_label="FLUX V9 专业化本地验证", model=cfg.model,
                prompt_version=PROFESSIONAL_VERSION, price_per_image=0.0,
                stats={"total": len(candidates), "success": success, "failed": len(candidates) - success, "calls": len(candidates), "elapsed": 0},
                scope="V9 专业化：门店 01 / 6 主题 / 每主题 3 候选 / 程序中文排版",
                output_root=str(cfg.output_root),
            )
            STATE.publish({"type": "professional_finished", "selected": report["selected_count"], "mean_score": report["mean_selected_score"], "technical_pass": report["technical_pass"]})
        except asyncio.CancelledError:
            STATE.publish({"type": "professional_cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("V9 专业化本地验证失败")
            STATE.publish({"type": "run_error", "error": str(exc)})
        finally:
            await provider.close()
            STATE.running = False
            STATE.orchestrator = None

    STATE.task = asyncio.create_task(runner())
    return JSONResponse({"ok": True, "scope": "V9 专业化：门店 01 / 6 主题 / 每主题 3 候选 / 程序中文排版", "output_root": str(cfg.output_root)})


@app.get("/api/professional-validation/image")
async def api_professional_validation_image(file: str) -> FileResponse:
    store = validation_store(_repo().stores)
    allowed = {item.file_name for item in store.items}
    if file not in allowed:
        raise HTTPException(status_code=403, detail="不是 V9 专业化验证范围内的图片")
    target = (LOCAL_PROFESSIONAL_ROOT / "selected" / store.output_dir / file).resolve()
    root = LOCAL_PROFESSIONAL_ROOT.resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise HTTPException(status_code=404, detail="V9 专业化图片不存在")
    return FileResponse(target, media_type="image/png")


@app.get("/api/config")
async def api_config(provider: str | None = None) -> JSONResponse:
    cfg = load_config(provider)
    return JSONResponse({
        "describe": cfg.describe(),
        "problems": cfg.validate(),
        "provider": cfg.provider,
    })


# ================================================================ 设置中心
@app.get("/api/catalog")
async def api_catalog() -> JSONResponse:
    """服务商与模型清单（含价格 / 负向词 / 限流标注，来自调研结论）。"""
    return JSONResponse(PROVIDER_CATALOG)


@app.get("/api/settings")
async def api_get_settings() -> JSONResponse:
    """读取全部设置（API Key 一律打码返回）。"""
    return JSONResponse(get_store().masked())


_ALLOWED_SIZES = {"1024x1024", "1024x1280", "1280x1024", "1024x1536", "1536x1024"}


def _normalise_size(value: object) -> str:
    size = str(value or "1024x1024").lower().replace("×", "x").strip()
    if size not in _ALLOWED_SIZES:
        raise HTTPException(status_code=400, detail="请选择受支持的比例：1:1、4:5、5:4、2:3 或 3:2")
    return size


@app.get("/api/reference-assets")
async def api_reference_assets() -> JSONResponse:
    """列出输出根目录独立保存的参考图片，不返回图像原始内容。"""
    cfg = load_config()
    assets = ReferenceAssetStore(cfg.output_base_root).list()
    return JSONResponse({"root": str(ReferenceAssetStore(cfg.output_base_root).root), "assets": [a.public() for a in assets]})


@app.get("/api/reference-assets/{asset_id}/raw")
async def api_reference_asset_raw(asset_id: str) -> FileResponse:
    """返回单张参考图的原始内容，供设置页显示缩略图。

    列表接口只返回元数据（不含 Base64），所以预览需要单独一个出口。
    安全：`asset_id` 是内容 SHA-256 的前 16 位十六进制 —— 严格校验格式，
    并再次确认解析后的路径仍在 `_references` 目录内。
    """
    import re as _re

    cfg = load_config()
    store = ReferenceAssetStore(cfg.output_base_root)

    if not _re.fullmatch(r"[0-9a-fA-F]{16}", str(asset_id or "")):
        raise HTTPException(status_code=400, detail="非法资产 ID")

    # ⚠️ store.get() 对「不存在」是**抛异常**而不是返回 None ——
    #    不捕获的话会变成 500（实测踩到）。
    try:
        asset = store.get(asset_id)
    except ReferenceAssetError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if asset is None or not asset.path.is_file():
        raise HTTPException(status_code=404, detail="参考图不存在")

    # 双保险：解析后必须仍在资产根目录内（防 ../ 之类的穿越）
    try:
        root = store.root.resolve()
        target = asset.path.resolve()
        target.relative_to(root)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=403, detail="非法路径") from exc

    # 缩略图是展示用途，让浏览器缓存（内容由 SHA-256 命名，天然不可变）
    return FileResponse(
        target,
        media_type=asset.mime_type or "image/png",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.post("/api/reference-assets")
async def api_reference_assets_upload(payload: dict | None = None) -> JSONResponse:
    """接收浏览器选择的参考图 data URL 并按 SHA-256 去重保存。"""
    payload = payload or {}
    cfg = load_config()
    try:
        asset = ReferenceAssetStore(cfg.output_base_root).add_data_url(
            str(payload.get("data_url") or ""), str(payload.get("file_name") or ""),
        )
    except ReferenceAssetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "asset": asset.public()})


@app.get("/api/effect-background-assets")
async def api_effect_background_assets() -> JSONResponse:
    """列出仅供本地玻璃合成使用的实拍门店背景。"""
    cfg = load_config()
    assets = EffectBackgroundStore(cfg.output_base_root).list()
    return JSONResponse({
        "root": str(EffectBackgroundStore(cfg.output_base_root).root),
        "assets": [asset.public() for asset in assets],
    })


@app.post("/api/effect-background-assets")
async def api_effect_background_assets_upload(payload: dict | None = None) -> JSONResponse:
    """保存手机实拍门店玻璃背景；该图片永不发送给图像模型。"""
    payload = payload or {}
    cfg = load_config()
    try:
        asset = EffectBackgroundStore(cfg.output_base_root).add_data_url(
            str(payload.get("data_url") or ""), str(payload.get("file_name") or ""),
        )
    except ReferenceAssetError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "asset": asset.public()})


@app.post("/api/effect-backgrounds/generate")
async def api_generate_effect_background(payload: dict | None = None) -> JSONResponse:
    """用图像服务商生成**真实感门店玻璃背景**。

    ⚠️ 只生成空背景（店内环境 + 玻璃），**不含任何贴纸/文字/Logo**；
    贴纸仍由本地 `effect_renderer` 合成，因此不违反「生成图是唯一前景素材」。
    生成结果标记为 ``ai_generated_background``，与用户实拍严格区分。
    """
    from ..effect_background import generate_backgrounds

    payload = payload or {}
    if STATE.running:
        raise HTTPException(status_code=409, detail="生成任务运行中，请稍后再试")

    store_name = str(payload.get("store_name") or "").strip() or "通用门店"
    try:
        count = max(1, min(3, int(payload.get("count") or 1)))
    except (TypeError, ValueError):
        count = 1
    door = str(payload.get("door") or "single")
    size = str(payload.get("size") or "1024x1536")
    provider_name = str(payload.get("provider") or "").strip() or None

    cfg = load_config(provider_name)
    problems = cfg.validate()
    if problems:
        raise HTTPException(status_code=400, detail="；".join(problems))

    async def on_progress(ev: dict) -> None:
        STATE.publish(ev)

    try:
        result = await generate_backgrounds(
            cfg, store_name, count, door=door, size=size, on_progress=on_progress
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("生成背景失败")
        raise HTTPException(status_code=500, detail=f"生成背景失败：{exc}") from exc

    return JSONResponse({"ok": True, **result})


# ================================================================ 效果图参数微调
PREVIEW_DIR_NAME = "_effect_preview"


def _effect_preview_dir(cfg) -> Path:
    d = Path(cfg.output_base_root) / PREVIEW_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pick_preview_source(cfg) -> Path | None:
    """挑一张**真实生成图**（>100KB，排除 mock 占位图）作为预览源。

    注意：``cfg.output_base_root`` 已经是输出根目录（如 ``output/``），
    批次目录是它下面的 ``batch_*``。
    """
    base = Path(cfg.output_base_root)
    batches: list[Path] = []
    if base.is_dir():
        batches = [p for p in sorted(base.glob("batch_*"), reverse=True) if p.is_dir()]
    # 兼容：当前批次目录可能直接配置成 output_root
    cur = Path(cfg.output_root)
    if cur.is_dir() and cur != base:
        batches.insert(0, cur)

    for batch in batches:
        cands: list[Path] = []
        for p in batch.rglob("*.png"):
            if "生成图" not in str(p):
                continue
            try:
                if p.stat().st_size > 100_000:
                    cands.append(p)
            except OSError:
                continue
        if cands:
            return sorted(cands)[0]
    return None


@app.get("/api/effect-params")
async def api_effect_params() -> JSONResponse:
    """返回当前效果图参数、实测默认值、可调范围与预设（供前端渲染滑块）。"""
    from ..effect_renderer import (
        DEFAULT_PARAMS,
        PARAM_PRESETS,
        PARAM_SPEC,
        EffectParams,
    )

    cfg = load_config()
    current = EffectParams.from_dict(cfg.effect_params)
    return JSONResponse({
        "current": current.to_dict(),
        "defaults": DEFAULT_PARAMS.to_dict(),
        "spec": {
            key: {
                "min": spec[0], "max": spec[1], "step": spec[2],
                "label": spec[3], "group": spec[4], "hint": spec[5],
            }
            for key, spec in PARAM_SPEC.items()
        },
        "presets": {
            key: {
                "label": preset["label"],
                "note": preset["note"],
                "params": preset["params"],
            }
            for key, preset in PARAM_PRESETS.items()
        },
        "has_source": _pick_preview_source(cfg) is not None,
    })


@app.post("/api/effect-params")
async def api_save_effect_params(payload: dict | None = None) -> JSONResponse:
    """保存效果图参数（写入 config/settings.json 的 effect.params）。"""
    from ..effect_renderer import EffectParams

    payload = payload or {}
    params = EffectParams.from_dict(payload.get("params") or {})
    get_store().save({"effect": {"params": params.to_dict()}})
    return JSONResponse({"ok": True, "params": params.to_dict()})


@app.post("/api/effect-params/preview")
async def api_effect_params_preview(payload: dict | None = None) -> JSONResponse:
    """用指定参数合成**一张预览图**（不写入任何批次）。"""
    from ..effect_renderer import EffectParams, render_storefront_glass

    payload = payload or {}
    cfg = load_config()
    params = EffectParams.from_dict(payload.get("params") or {})

    src = _pick_preview_source(cfg)
    if src is None:
        raise HTTPException(status_code=404, detail="没有可用的生成图作为预览源")

    store_name = str(payload.get("store_name") or "").strip()
    opts = _effect_render_options(cfg, store_name)
    opts["params"] = params

    try:
        result = render_storefront_glass(src, store_name, **opts)
    except EffectRenderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    out = _effect_preview_dir(cfg) / "preview.png"
    out.write_bytes(result.data)
    return JSONResponse({
        "ok": True,
        "url": "/api/effect-params/preview-image",
        "source": src.name,
        "width": result.width,
        "height": result.height,
        "background_mode": result.background.get("mode"),
        "params": params.to_dict(),
    })


@app.get("/api/effect-params/preview-image")
async def api_effect_params_preview_image() -> FileResponse:
    """读取最近一次生成的预览图。"""
    cfg = load_config()
    p = _effect_preview_dir(cfg) / "preview.png"
    if not p.is_file():
        raise HTTPException(status_code=404, detail="预览图不存在")
    return FileResponse(p, media_type="image/png")


@app.post("/api/prompt-profile/optimize")
async def api_optimize_prompt_profile(payload: dict | None = None) -> JSONResponse:
    """只优化一次项目质量模板，预览结果不会自动保存。"""
    from ..prompt_optimizer import DeepSeekPromptOptimizer, PromptOptimizerError

    payload = payload or {}
    try:
        template = validate_template(str(payload.get("template") or ""))
    except PromptProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cfg = load_config()
    if not cfg.prompt_optimizer_enabled:
        raise HTTPException(status_code=400, detail="请先在模型服务中启用 DeepSeek 提示词优化")
    optimizer = DeepSeekPromptOptimizer(
        cfg.prompt_optimizer_api_key,
        cfg.prompt_optimizer_base_url,
        cfg.prompt_optimizer_model,
    )
    try:
        result = await optimizer.optimize_quality_template(template)
    except PromptOptimizerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "template": result.prompt, "model": result.model, "elapsed": round(result.elapsed, 2)})


@app.post("/api/prompt-profile/confirm-run")
async def api_confirm_prompt_profile_and_run(payload: dict | None = None) -> JSONResponse:
    """保存范围、模板和参考图，然后以它们创建一个不可变的新批次。"""
    payload = payload or {}
    repo = _repo()
    stores = _ordered_store_indexes(repo, payload.get("store_indexes"))
    if not stores:
        raise HTTPException(status_code=400, detail="请至少选择一个门店")
    mode = str(payload.get("image_mode") or "text")
    cfg = load_config()
    asset_store = ReferenceAssetStore(cfg.output_base_root)
    try:
        assets = asset_store.resolve_many([str(x) for x in (payload.get("reference_asset_ids") or [])])
        validate_mode_assets(mode, assets)
        template = validate_template(str(payload.get("template") or DEFAULT_QUALITY_TEMPLATE))
        optimized = str(payload.get("optimized_template") or "").strip()
        use_optimized = bool(payload.get("use_optimized", False))
        if use_optimized:
            optimized = validate_template(optimized)
        size = _normalise_size(payload.get("size") or cfg.size)
        background_id = str(payload.get("effect_background_asset_id") or "").strip()
        background_asset = EffectBackgroundStore(cfg.output_base_root).get(background_id) if background_id else None
    except (ReferenceAssetError, PromptProfileError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_store().save({
        "scope": {"store_indexes": stores},
        "generation": {"size": size},
        "image_workflow": {"mode": mode, "reference_assets": [asset.public() for asset in assets]},
        "prompt_quality": {
            "template": template, "optimized_template": optimized, "use_optimized": use_optimized,
            "realism_iteration": _realism_level(payload.get("realism_iteration"), cfg.realism_iteration),
        },
        "effect_workflow": {"background_asset": background_asset.public() if background_asset else {}},
    })
    return await _start_full_run({}, create_new_batch=True)


# ---------------------------------------------------------------- 设置字段级授权
# ⚠️ `POST /api/settings` 会把整个 payload **深度合并**进 settings.json，
#    所以「能调这个接口」等于「能改任何设置」。
#    只在路由上挂一个权限码是不够的 —— 那会让 `settings.path.manage`
#    形同虚设：有「管理模型」权限的人可以顺手把输出路径也改掉。
#    这里按 payload 里**实际出现的字段**逐组校验。
_SETTINGS_FIELD_PERMISSION: dict[str, str] = {
    # 密钥与付费账号
    "active_provider": "settings.model.manage",
    "providers": "settings.model.manage",
    "prompt_optimizer": "settings.model.manage",
    # 输出路径：改错会把数据写到意外位置
    "output": "settings.path.manage",
    # 效果图参数与背景
    "effect": "effect.params.manage",
    "effect_workflow": "effect.params.manage",
    # 提示词模板 / 生成参数 / 范围 / 图生图 / 印刷（设计类变更）
    "prompt_quality": "prompt.template.manage",
    "generation": "prompt.template.manage",
    "scope": "prompt.template.manage",
    "image_workflow": "prompt.template.manage",
    "print": "prompt.template.manage",
}
# 未列出的字段（含以后新增的）落到最严的一档 ——
# 宁可让新字段默认「只有 admin 能改」，也不要默认放行。
_SETTINGS_DEFAULT_PERMISSION = "settings.model.manage"

# 这两个键只是回传/元信息，不构成配置变更，不需要额外权限
_SETTINGS_IGNORED_KEYS = frozenset({"version"})


def _require_settings_permissions(user: "User", payload: dict) -> None:
    """按 payload 的字段分组校验权限，不足则 403 并写审计。

    Raises:
        HTTPException 403: 缺少修改其中某些字段所需的权限
    """
    needed: set[str] = set()
    for key in payload:
        if key in _SETTINGS_IGNORED_KEYS:
            continue
        needed.add(_SETTINGS_FIELD_PERMISSION.get(key, _SETTINGS_DEFAULT_PERMISSION))

    missing = sorted(c for c in needed if not security_service.has_permission(user, c))
    if not missing:
        return

    security_service.audit.log(
        action="access.denied",
        module="settings",
        result="denied",
        actor_id=user.id,
        actor_name=user.display_name,
        actor_roles=user.roles,
        target_type="endpoint",
        target_id="POST /api/settings",
        error_kind="forbidden",
        detail=(
            f"缺少权限：{', '.join(missing)}；"
            f"提交字段：{', '.join(sorted(k for k in payload if k not in _SETTINGS_IGNORED_KEYS))}"
        ),
    )
    raise HTTPException(
        status_code=403,
        detail=f"没有修改这些设置的权限（需要 {'、'.join(missing)}）",
    )


@app.post("/api/settings")
async def api_save_settings(
    payload: dict | None = None,
    user: "User" = Depends(current_user),
) -> JSONResponse:
    """保存设置。

    自动处理 API Key 的掩码回传：值含 `****` 视为「未修改」，保持原 Key。

    ⚠️ 授权按**字段**分组（见 `_SETTINGS_FIELD_PERMISSION`），不是整个接口一刀切。
    """
    payload = payload or {}
    _require_settings_permissions(user, payload)
    generation = payload.get("generation")
    if isinstance(generation, dict) and "size" in generation:
        generation["size"] = _normalise_size(generation.get("size"))
    quality = payload.get("prompt_quality")
    if isinstance(quality, dict):
        quality["realism_iteration"] = _realism_level(quality.get("realism_iteration"))
    effect = payload.get("effect_workflow")
    if isinstance(effect, dict):
        asset = effect.get("background_asset")
        asset_id = str(asset.get("id") or "").strip() if isinstance(asset, dict) else ""
        try:
            effect["background_asset"] = EffectBackgroundStore(load_config().output_base_root).get(asset_id).public() if asset_id else {}
        except ReferenceAssetError as exc:
            raise HTTPException(status_code=400, detail=f"实拍门店背景不可用：{exc}") from exc
    try:
        get_store().save(payload)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"保存失败：{e}") from e

    cfg = load_config()
    STATE.publish({"type": "settings_saved", "provider": cfg.provider})
    return JSONResponse({
        "ok": True,
        "problems": cfg.validate(),
        "settings": get_store().masked(),
        "summary": {
            "provider": cfg.provider,
            "provider_label": cfg.provider_label,
            "model": cfg.model,
            "concurrency": cfg.concurrency,
            "output_root": str(cfg.output_root),
            "limit": cfg.run_limit,
        },
    })


@app.post("/api/settings/reset")
async def api_reset_settings() -> JSONResponse:
    """恢复出厂设置（保留已填写的 API Key 与 Base URL）。"""
    get_store().reset()
    return JSONResponse({"ok": True, "settings": get_store().masked()})


@app.post("/api/settings/test")
async def api_test_connection(payload: dict | None = None) -> JSONResponse:
    """测试服务商连通性（鉴权探测，不发真实生成请求）。"""
    import httpx

    payload = payload or {}
    name = (payload.get("provider") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="缺少 provider 参数")

    # DeepSeek 是提示词优化器而非图像服务商，因此不放进图片服务商目录。
    # 复用无写入的 /models 鉴权探测；少数账号不提供该端点时会明确提示。
    if name == "deepseek":
        from ..settings import is_masked

        optimizer = get_store().load().get("prompt_optimizer", {}) or {}
        api_key = (payload.get("api_key") or "").strip()
        if not api_key or is_masked(api_key):
            api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip() or optimizer.get("api_key", "")
        base_url = (payload.get("base_url") or "").strip() or optimizer.get("base_url", "") or "https://api.deepseek.com"
        if not api_key:
            return JSONResponse({"ok": False, "status": "no_key", "elapsed": 0.0, "message": "未填写 DeepSeek API Key"})
        # ⚠️ SSRF 防护（文档 §11）：DeepSeek 地址同样需要校验。
        try:
            base_url = validate_outbound_url(base_url, allow_loopback=False)
        except UrlGuardError as exc:
            return JSONResponse({
                "ok": False, "status": "blocked_url", "elapsed": 0.0,
                "message": f"地址未通过安全校验：{exc}",
            })
        t0 = time.monotonic()
        try:
            # ⚠️ 显式关闭重定向跟随，防止 302 绕过 URL 校验。
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
                response = await client.get(
                    base_url.rstrip("/") + "/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            elapsed = round(time.monotonic() - t0, 2)
            if response.status_code in (301, 302, 303, 307, 308):
                return JSONResponse({
                    "ok": False, "status": "redirect_blocked", "elapsed": elapsed,
                    "message": "该地址返回了跳转，出于安全考虑不予跟随；请直接填写最终地址",
                })
            if response.status_code == 200:
                return JSONResponse({"ok": True, "status": "ok", "elapsed": elapsed, "message": f"DeepSeek 连接成功（{elapsed}s）"})
            if response.status_code in (401, 403):
                return JSONResponse({"ok": False, "status": "invalid_key", "elapsed": elapsed, "message": "DeepSeek API Key 无效或无权限"})
            if response.status_code == 429:
                return JSONResponse({"ok": True, "status": "rate_limited", "elapsed": elapsed, "message": "DeepSeek Key 已识别，但当前触发限流"})
            if response.status_code == 404:
                return JSONResponse({"ok": True, "status": "unverified", "elapsed": elapsed, "message": "DeepSeek 服务可达；该账号不支持 /models 探测，生成时将自动使用原提示词回退"})
            return JSONResponse({"ok": False, "status": f"http_{response.status_code}", "elapsed": elapsed, "message": f"DeepSeek HTTP {response.status_code}：{response.text[:180]}"})
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "status": "network", "elapsed": round(time.monotonic() - t0, 2), "message": f"无法连接 DeepSeek：{type(exc).__name__}: {exc}"})

    cat = PROVIDER_CATALOG.get(name)
    if not cat:
        raise HTTPException(status_code=400, detail=f"未知服务商：{name}")

    kind = cat.get("kind", "cloud")
    if name == "flux_local":
        cfg = load_config("flux_local")
        # 当用户还未保存端点时，也允许用卡片输入即时检测。
        #
        # ⚠️ SSRF 防护（文档 §11）：本地模型服务需要访问回环地址，
        #    因此这里显式开启 allow_loopback —— 但仍会拦住非回环的内网段。
        requested_url = (payload.get("base_url") or "").strip()
        if requested_url:
            try:
                safe_url = validate_outbound_url(requested_url, allow_loopback=True)
            except UrlGuardError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            cfg = dataclasses.replace(cfg, base_url=safe_url)
        return JSONResponse(await _local_flux_health(cfg))
    if kind == "local" or name == "mock":
        return JSONResponse({
            "ok": True, "status": "local", "elapsed": 0.0,
            "message": "本地模拟器无需联网，可直接使用",
        })

    # 取 Key：请求体 > 已存设置 > 环境变量（掩码值视为「未修改」）
    from ..settings import is_masked

    store = get_store()
    stored = store.provider_runtime(name)
    api_key = (payload.get("api_key") or "").strip()
    if not api_key or is_masked(api_key):
        api_key = stored["api_key"]
    base_url = (payload.get("base_url") or "").strip() or stored["base_url"]

    if not api_key:
        return JSONResponse({
            "ok": False, "status": "no_key", "elapsed": 0.0,
            "message": "未填写 API Key",
        })
    if not base_url:
        return JSONResponse({
            "ok": False, "status": "no_url", "elapsed": 0.0,
            "message": "未填写 Base URL",
        })

    # ⚠️ SSRF 防护（文档 §11）：云端服务商地址**不允许回环与内网**。
    #    这里校验后才发起请求，避免前端填入内网地址让服务端代为探测。
    try:
        base_url = validate_outbound_url(base_url, allow_loopback=False)
    except UrlGuardError as exc:
        return JSONResponse({
            "ok": False, "status": "blocked_url", "elapsed": 0.0,
            "message": f"地址未通过安全校验：{exc}",
        })

    t0 = time.monotonic()
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    if name == "gemini":
        # Gemini 的 REST API 使用 x-goog-api-key，而不是 Bearer；/models 是
        # 不产生图像费用的账户与网络探测接口。
        headers = {"x-goog-api-key": api_key}
    try:
        # ⚠️ 显式关闭重定向跟随（文档 §11）：否则 302 可绕过上面的 URL 安全校验。
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            r = await client.get(url, headers=headers)
        elapsed = round(time.monotonic() - t0, 2)

        if r.status_code in (301, 302, 303, 307, 308):
            return JSONResponse({
                "ok": False, "status": "redirect_blocked", "elapsed": elapsed,
                "message": "该地址返回了跳转，出于安全考虑不予跟随；请直接填写最终地址",
            })
        if r.status_code == 200:
            # DashScope 的 /models 只能证明 API Key 和网络可用，不能证明
            # 图像模型的扣费账户已经恢复。欠费恢复必须由下面的实际出图
            # 权限验证确认，避免把继续按钮提前解锁。
            if name != "qwen":
                STATE.confirm_account_recovery(name)
            return JSONResponse({
                "ok": True,
                "status": "connection_only" if name == "qwen" else "ok",
                "elapsed": elapsed,
                "message": (
                    f"连接成功（{elapsed}s），但尚未验证图片模型的扣费账户；请点击“验证图片出图权限”。"
                    if name == "qwen" else f"连接成功（{elapsed}s）"
                ),
            })
        if r.status_code in (401, 403):
            return JSONResponse({
                "ok": False, "status": "invalid_key", "elapsed": elapsed,
                "message": "API Key 无效或无权限",
            })
        if r.status_code == 429:
            return JSONResponse({
                "ok": True, "status": "rate_limited", "elapsed": elapsed,
                "message": "Key 有效，但当前触发限流",
            })
        if r.status_code == 404:
            return JSONResponse({
                "ok": True, "status": "unverified", "elapsed": elapsed,
                "message": "服务可达，但该端点不支持 /models 探测，请直接试跑一张",
            })
        return JSONResponse({
            "ok": False, "status": f"http_{r.status_code}", "elapsed": elapsed,
            "message": f"HTTP {r.status_code}：{r.text[:180]}",
        })
    except Exception as e:                                   # noqa: BLE001
        return JSONResponse({
            "ok": False, "status": "network",
            "elapsed": round(time.monotonic() - t0, 2),
            "message": f"无法连通：{type(e).__name__}: {e}",
        })


@app.post("/api/settings/test-image")
async def api_test_image_capability(payload: dict | None = None) -> JSONResponse:
    """实际生成一张临时图片，确认 Qwen 图像模型的计费权限。

    该接口只能由设置页上明确标注的按钮发起。返回图片只在内存中验证，
    不写入 output、批次目录或历史记录；成功后才允许继续被欠费暂停的批次。
    """
    from ..settings import is_masked

    payload = payload or {}
    name = (payload.get("provider") or "").strip()
    if name != "qwen":
        raise HTTPException(status_code=400, detail="图片出图权限验证目前仅支持阿里云百炼")

    stored = get_store().provider_runtime("qwen")
    api_key = (payload.get("api_key") or "").strip()
    if not api_key or is_masked(api_key):
        api_key = stored["api_key"]
    base_url = (payload.get("base_url") or "").strip() or stored["base_url"]
    model = (payload.get("model") or "").strip() or stored["model"]
    if not api_key:
        return JSONResponse({"ok": False, "status": "no_key", "elapsed": 0.0, "message": "未填写 API Key"})
    if not base_url:
        return JSONResponse({"ok": False, "status": "no_url", "elapsed": 0.0, "message": "未填写 Base URL"})
    if not model:
        return JSONResponse({"ok": False, "status": "no_model", "elapsed": 0.0, "message": "未选择图像模型"})

    cfg = dataclasses.replace(
        load_config("qwen"), api_key=api_key, base_url=base_url, model=model,
    )
    provider = create_provider(cfg)
    started = time.monotonic()
    try:
        result = await provider.generate(GenerateRequest(
            prompt=(
                "一张 1024×1024 白色背景的简洁彩色商业贴纸测试图，"
                "主体是一座小房屋和钥匙图标，无可读文字、无水印。"
            ),
            negative_prompt="可读文字，水印，二维码，边框外杂物",
            size="1024x1024",
            n=1,
        ))
        elapsed = round(time.monotonic() - started, 2)
        if not result.ok or not result.images:
            return JSONResponse({
                "ok": False, "status": "image_failed", "elapsed": elapsed,
                "message": "图片出图权限未通过：服务未返回 PNG 图片",
            })
        # QwenProvider 已下载并校验图片字节；故意不将此验证图落盘。
        STATE.confirm_account_recovery("qwen")
        return JSONResponse({
            "ok": True, "status": "image_ready", "elapsed": elapsed,
            "message": "图片出图权限验证成功。可继续当前批次；本次验证图片未保存。",
        })
    except ProviderError as exc:
        return JSONResponse({
            "ok": False, "status": exc.code or "image_failed",
            "elapsed": round(time.monotonic() - started, 2),
            "message": f"图片出图权限未通过：{exc}",
        })
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({
            "ok": False, "status": "image_failed",
            "elapsed": round(time.monotonic() - started, 2),
            "message": f"图片出图权限未通过：{type(exc).__name__}: {exc}",
        })
    finally:
        await provider.close()


@app.post("/api/settings/validate-path")
async def api_validate_path(payload: dict | None = None) -> JSONResponse:
    """校验图片保存路径：能否创建、是否可写、已有多少张图。"""
    raw = ((payload or {}).get("path") or "").strip()
    if not raw:
        return JSONResponse({"ok": False, "message": "路径不能为空"})

    p = Path(raw).expanduser()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:                                   # noqa: BLE001
        return JSONResponse({"ok": False, "message": f"无法创建目录：{e}"})

    if not p.is_dir():
        return JSONResponse({"ok": False, "message": "该路径已存在但不是目录"})

    probe = p / ".write_test.tmp"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as e:                                   # noqa: BLE001
        return JSONResponse({"ok": False, "message": f"目录不可写：{e}"})

    try:
        png_count = sum(1 for _ in p.rglob("*.png"))
        store_dirs = sum(1 for x in p.iterdir() if x.is_dir())
    except OSError:
        png_count = store_dirs = 0

    return JSONResponse({
        "ok": True,
        "resolved": str(p),
        "message": f"路径可用（已有 {store_dirs} 个子目录、{png_count} 张 PNG）",
        "png_count": png_count,
        "store_dirs": store_dirs,
    })


@app.post("/api/settings/pick-dir")
async def api_pick_dir(payload: dict | None = None) -> JSONResponse:
    """弹出**系统目录选择框**，让用户挑一个已有文件夹。

    仅本机可用（服务与浏览器同机）。tkinter 在无显示环境会失败，
    此时返回明确原因，前端提示用户改用「新建」按钮。

    ⚠️ 对话框必须放在**线程池**里跑：`askdirectory()` 是阻塞调用，
       直接 await 会把整个事件循环卡住（其它请求全部超时）。
    """
    initial = str((payload or {}).get("path") or "").strip()

    def _pick() -> str:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)     # 避免被浏览器窗口挡住
        except Exception:  # noqa: BLE001
            pass
        try:
            # ⚠️ mustexist=False：允许用户在对话框里**新建文件夹**
            #    （True 时 Windows 对话框的「新建文件夹」按钮是灰的，
            #     用户就没法"在选中的盘里创建新目录"）
            kwargs = {"title": "选择图片保存目录（可在对话框内新建文件夹）",
                      "mustexist": False}
            if initial and Path(initial).expanduser().is_dir():
                kwargs["initialdir"] = str(Path(initial).expanduser())
            return filedialog.askdirectory(**kwargs) or ""
        finally:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass

    try:
        import asyncio as _asyncio

        chosen = await _asyncio.get_running_loop().run_in_executor(None, _pick)
    except Exception as exc:  # noqa: BLE001 - 无显示环境 / tkinter 缺失
        return JSONResponse({
            "ok": False,
            "message": f"无法弹出目录选择框（{type(exc).__name__}）。"
                       f"可直接用「新建」按钮，或手动把文件夹拖进来。",
        })

    if not chosen:
        return JSONResponse({"ok": False, "cancelled": True, "message": "已取消"})
    return JSONResponse({"ok": True, "path": str(Path(chosen)), "message": "已选择"})


# 目录名里不允许出现的字符（Windows 限制 + 路径分隔符）
_BAD_DIR_CHARS = set('\\/:*?"<>|')


@app.post("/api/settings/new-dir")
async def api_new_dir(payload: dict | None = None) -> JSONResponse:
    """新建一个保存目录。

    支持两种输入（**向后兼容**）：

    1. **纯目录名**（如 `贴纸输出A`）→ 在当前目录的**同级**创建
       —— 保存目录的语义是"这一批图片放在哪"，换目录 = 换平级位置；
       往里建子目录会让 `output/` 越套越深。

    2. **完整绝对路径**（如 `F:\\贴纸输出\\批次01`）→ **直接按该路径创建**
       —— 这样用户可以在**任意盘符**下新建目录并把输出切过去。

    ⚠️ 路径分隔符校验只针对**目录名片段**：绝对路径本身含 `\\` 和 `:`，
       若按"目录名"规则校验会被误拒。
    """
    payload = payload or {}
    raw = str(payload.get("name") or "").strip()
    base = str(payload.get("base") or "").strip()

    if not raw:
        return JSONResponse({"ok": False, "message": "目录名或路径不能为空"})

    candidate = Path(raw).expanduser()

    if candidate.is_absolute():
        # 绝对路径：逐段校验（跳过盘符/根，它天然含 : 与 \）
        parts = [p for p in candidate.parts if p not in ("\\", "/")][1:]
        for part in parts:
            if any(c in _BAD_DIR_CHARS for c in part):
                return JSONResponse({
                    "ok": False,
                    "message": '路径中不能包含 / : * ? " < > | 等字符',
                })
        if len(str(candidate)) > 240:
            return JSONResponse({"ok": False, "message": "路径过长（最多 240 字符）"})
        target = candidate
    else:
        # 纯目录名：保持"同级"语义
        if raw in (".", "..") or any(c in _BAD_DIR_CHARS for c in raw):
            return JSONResponse({
                "ok": False,
                "message": '目录名不能包含 \\ / : * ? " < > | 等字符',
            })
        if len(raw) > 80:
            return JSONResponse({"ok": False, "message": "目录名过长（最多 80 字符）"})
        cur = Path(base).expanduser() if base else Path(load_config().output_root)
        target = cur.parent / raw

    if target.exists():
        return JSONResponse({
            "ok": False,
            "message": f"目录已存在：{target.name}（可直接用「浏览」选它）",
        })

    try:
        target.mkdir(parents=True, exist_ok=False)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "message": f"创建失败：{exc}"})

    # 顺手写一个可写性探针，避免用户切过去才发现没权限
    try:
        probe = target / ".write_test.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({
            "ok": False,
            "message": f"目录已创建但不可写：{exc}",
            "path": str(target),
        })

    return JSONResponse({
        "ok": True,
        "path": str(target),
        "message": f"已创建 {target.name}",
    })


@app.post("/api/settings/open-dir")
async def api_open_dir(payload: dict | None = None) -> JSONResponse:
    """在系统文件管理器中打开目录。"""
    raw = ((payload or {}).get("path") or "").strip()
    p = Path(raw).expanduser() if raw else Path(load_config().output_root)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"目录不可用：{e}") from e

    try:
        if sys.platform == "win32" and hasattr(os, "startfile"):
            os.startfile(str(p))                             # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"打开失败：{e}") from e

    return JSONResponse({"ok": True, "path": str(p)})


@app.post("/api/export/checklist")
async def api_export_checklist() -> JSONResponse:
    """导出人工抽检清单（138 张图 + 期望文本对照，CSV）。

    ⚠️ 依据 TextPecker（CVPR 2026）：主流 OCR 对笔画级错误检测 F1=0.000，
    自动质检不能替代人工目检，因此提供此清单。
    """
    import csv

    cfg = load_config()
    repo = _repo()
    storage = Storage(cfg.output_root, cfg.timestamp_prefix)
    manifests = ManifestStore(cfg.output_root)

    out_dir = Path(cfg.output_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "_抽检清单.csv"

    rows = []
    for s in repo.stores:
        m = manifests.for_store(s.output_dir)
        for it in s.items:
            entry = m.entries.get(it.pic_index, {})
            target = storage.generated_dir(s.output_dir) / it.file_name
            rows.append({
                "门店序号": s.folder_index,
                "门店名称": s.folder_name,
                "图片序号": it.pic_index,
                "主题": it.theme,
                "文件名": it.file_name,
                "主标题": s.main_title,
                "副标题": s.sub_title,
                "期望文字": " / ".join(it.expected_text),
                "生成状态": entry.get("status", "未生成"),
                "尝试次数": entry.get("attempts", 0),
                "耗时(秒)": entry.get("elapsed", 0),
                "文件路径": str(target),
                "人工核对(请填 √/×)": "",
                "备注": "",
            })

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    return JSONResponse({"ok": True, "path": str(csv_path), "rows": len(rows)})


# ================================================================ 提示词版本
@app.get("/api/prompts/versions")
async def api_prompt_versions() -> JSONResponse:
    """列出可用的提示词版本（含说明与预览）。"""
    from ..config import DATA_FILE
    from ..prompts import current_version, preview, version_summary

    raw = json.loads(Path(DATA_FILE).read_text(encoding="utf-8"))
    items = raw[0]["items"] if raw else []
    versions = version_summary(items)
    sample = items[0] if items else {}

    return JSONResponse({
        "current": current_version(items),
        "versions": versions,
        "sample": {v["version"]: preview(sample, v["version"]) for v in versions},
    })


# ================================================================ OpenAI 房屋中介 6 图再生成
@app.post("/api/openai/house-regenerate")
async def api_openai_house_regenerate() -> JSONResponse:
    """用 OpenAI GPT Image 强制刷新房屋中介门店的 6 张图。"""
    if STATE.running:
        raise HTTPException(status_code=409, detail="已有任务在运行中")

    active_cfg = load_config()
    if active_cfg.provider != "openai":
        raise HTTPException(status_code=400, detail="请先在设置中选择 OpenAI GPT Image 服务商")
    cfg = dataclasses.replace(
        load_config("openai"),
        concurrency=1,
        overwrite=True,
        timestamp_prefix=False,
        budget_limit=12,  # 6 张正式图 + 每张最多一次相似图重试
    )
    cfg = _apply_snapshot_and_assets(cfg, _active_batch_snapshot(active_cfg))
    next_realism = min(3, _realism_level(cfg.realism_iteration) + 1)
    cfg = dataclasses.replace(cfg, realism_iteration=next_realism)
    get_store().save({"prompt_quality": {"realism_iteration": next_realism}})
    problems = cfg.validate()
    if problems:
        raise HTTPException(status_code=400, detail="；".join(problems))

    repo = _repo()
    store = next((item for item in repo.stores if item.folder_index == HOUSE_STORE_INDEX), None)
    if store is None or len(store.items) != 6:
        raise HTTPException(status_code=400, detail="房屋中介门店数据不是预期的 6 张主题")

    provider = create_provider(cfg)
    STATE.running = True
    STATE.orchestrator = None
    STATE.provider_label = provider.describe()
    STATE.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def runner() -> None:
        try:
            stats = await regenerate_openai_house(
                cfg=cfg, provider=provider, store=store, root=cfg.output_root,
                on_event=lambda event: STATE.publish(event),
            )
            from ..runs import append_run
            append_run(
                provider=cfg.provider,
                provider_label=cfg.provider_label,
                model=cfg.model,
                prompt_version=cfg.prompt_version or "current",
                price_per_image=cfg.price_per_image,
                stats=stats,
                scope=f"OpenAI 房屋中介 6 张新图 · {stats['run_id']}",
                output_root=str(cfg.output_root),
            )
            STATE.publish({"type": "openai_house_finished", **stats})
        except asyncio.CancelledError:
            STATE.publish({"type": "openai_house_cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("OpenAI 房屋中介再生成失败")
            STATE.publish({"type": "run_error", "error": str(exc)})
        finally:
            await provider.close()
            STATE.running = False
            STATE.orchestrator = None
            STATE.task = None

    STATE.task = asyncio.create_task(runner())
    return JSONResponse({
        "ok": True,
        "scope": "OpenAI：房屋中介门店 / 6 张新图 / 单并发 / 强制归档旧图",
        "model": cfg.model,
    })


# ================================================================ 单张重生成
@app.post("/api/regenerate")
async def api_regenerate(payload: dict | None = None) -> JSONResponse:
    """重新生成指定的单张图片（强制覆盖）。"""
    import dataclasses

    payload = payload or {}
    store_idx = str(payload.get("store", "")).strip()
    pic_idx = str(payload.get("pic", "")).strip()
    if not store_idx or not pic_idx:
        raise HTTPException(status_code=400, detail="缺少 store 或 pic 参数")
    if STATE.running:
        raise HTTPException(status_code=409, detail="批量任务运行中，请稍后再试")

    cfg = _apply_snapshot_and_assets(load_config(), _active_batch_snapshot(load_config()))
    if cfg.provider == "flux_local":
        raise HTTPException(status_code=400, detail="FLUX 本地模型只允许运行固定的 01 / V8 / 6 张验证，不支持单张重生成。")
    problems = cfg.validate()
    if problems:
        raise HTTPException(status_code=400, detail="；".join(problems))

    repo = _repo()
    storage = Storage(
        cfg.output_root, cfg.timestamp_prefix,
        whiten_bg=cfg.whiten_background, whiten_threshold=cfg.whiten_threshold,
    )
    manifests = ManifestStore(cfg.output_root)
    provider = create_provider(cfg)

    force_cfg = dataclasses.replace(cfg, overwrite=True)
    orch = Orchestrator(force_cfg, provider, repo, storage, manifests)
    orch.on_event(lambda ev: STATE.publish(ev))

    STATE.running = True
    STATE.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def runner() -> None:
        try:
            stats = await orch.run(store_indexes=[store_idx], pic_indexes=[pic_idx])
            STATE.publish({"type": "regen_finished", "store": store_idx,
                           "pic": pic_idx, **stats.to_dict()})
        except Exception as e:                       # noqa: BLE001
            log.exception("单张重生成失败")
            STATE.publish({"type": "run_error", "error": str(e)})
        finally:
            await provider.close()
            STATE.running = False

    asyncio.create_task(runner())
    return JSONResponse({"ok": True, "store": store_idx, "pic": pic_idx})


# ================================================================ 导出
@app.post("/api/export/titles")
async def api_export_titles() -> JSONResponse:
    """导出 23 条拼多多标题（CSV，含门店与配色信息，可直接用于上架）。"""
    import csv

    cfg = load_config()
    repo = _repo()
    out_dir = Path(cfg.output_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "_拼多多标题.csv"

    rows = []
    for s in repo.stores:
        rows.append({
            "序号": s.folder_index,
            "门店名称": s.folder_name,
            "商品标题(30字)": s.pdd_title,
            "标题字数": len(s.pdd_title),
            "主标题": s.main_title,
            "副标题": s.sub_title,
            "配色主题": s.color_theme,
            "合规约束": s.compliance_note,
            "图片数量": len(s.items),
            "图片目录": s.output_dir,
        })

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    return JSONResponse({"ok": True, "path": str(csv_path), "rows": len(rows)})


@app.post("/api/export/package")
async def api_export_package() -> JSONResponse:
    """把 output/ 下的图片打包成 ZIP（便于交付与上传）。"""
    import zipfile

    cfg = load_config()
    out_dir = Path(cfg.output_root)
    if not out_dir.exists():
        raise HTTPException(status_code=404, detail="输出目录不存在")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = out_dir.parent / f"门店贴纸_138张_{stamp}.zip"

    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(out_dir.rglob("*.png")):
            if "_history" in p.parts or p.name.startswith("_"):
                continue
            zf.write(p, p.relative_to(out_dir))
            count += 1
        for p in sorted(out_dir.rglob("_manifest.json")):
            zf.write(p, p.relative_to(out_dir))

    size_mb = round(zip_path.stat().st_size / 1024 / 1024, 2)
    return JSONResponse({
        "ok": True, "path": str(zip_path), "images": count, "size_mb": size_mb,
    })


# ================================================================ 版本对比
@app.get("/api/history")
async def api_history(store: str, file: str) -> JSONResponse:
    """列出某张图的历史版本（重新生成时旧图会自动归档到 _history/）。"""
    cfg = load_config()
    storage = Storage(cfg.output_root, cfg.timestamp_prefix)
    items = storage.list_history(store, file)
    return JSONResponse({"items": items, "count": len(items)})


@app.get("/api/history/image")
async def api_history_image(store: str, name: str) -> FileResponse:
    """读取历史版本图片。"""
    cfg = load_config()
    if Path(name).name != name:
        raise HTTPException(status_code=403, detail="非法路径")
    storage = Storage(cfg.output_root)
    target = storage.generated_dir(store) / "_history" / name
    if not target.exists():
        target = storage.store_dir(store) / "_history" / name
    if not target.exists() or target.suffix.lower() != ".png":
        raise HTTPException(status_code=404, detail="历史版本不存在")
    return FileResponse(target, media_type="image/png")


@app.post("/api/history/restore")
async def api_history_restore(payload: dict | None = None) -> JSONResponse:
    """把归档 PNG 恢复为当前版本，并先归档恢复前的当前版本。"""
    if STATE.running:
        raise HTTPException(status_code=409, detail="生成任务运行中，暂不能恢复历史图片")
    payload = payload or {}
    store = str(payload.get("store") or "").strip()
    file_name = str(payload.get("file") or "").strip()
    history_name = str(payload.get("history_file") or "").strip()
    if not store or not file_name or not history_name:
        raise HTTPException(status_code=400, detail="缺少门店、当前文件或历史文件")
    if Path(file_name).name != file_name or Path(history_name).name != history_name:
        raise HTTPException(status_code=403, detail="非法文件名")
    if not file_name.lower().endswith(".png") or not history_name.lower().endswith(".png"):
        raise HTTPException(status_code=400, detail="只能恢复 PNG 图片")
    if store not in {item.output_dir for item in _repo().stores}:
        raise HTTPException(status_code=404, detail="门店不存在")

    cfg = load_config()
    storage = Storage(cfg.output_root, timestamp_prefix=False)
    store_dir = storage.store_dir(store).resolve()
    generated_dir = storage.generated_dir(store).resolve()
    history_dir = (generated_dir / "_history").resolve()
    # 兼容旧批次历史文件。
    if not history_dir.exists():
        history_dir = (store_dir / "_history").resolve()
    target = (generated_dir / file_name).resolve()
    source = (history_dir / history_name).resolve()
    stem = Path(file_name).stem
    if target.parent != generated_dir or source.parent != history_dir:
        raise HTTPException(status_code=403, detail="非法路径")
    if not source.is_file() or not source.name.startswith(f"{stem}__"):
        raise HTTPException(status_code=404, detail="历史版本不存在或不属于该图片")

    archived_current = storage.archive_existing(target, "restore_current")
    try:
        source.replace(target)
    except Exception as exc:  # noqa: BLE001
        if archived_current and archived_current.exists() and not target.exists():
            archived_current.replace(target)
        raise HTTPException(status_code=500, detail=f"恢复历史版本失败：{exc}") from exc

    for item in _repo().stores:
        if item.output_dir != store:
            continue
        matched = next((x for x in item.items if x.file_name == file_name), None)
        if not matched:
            break
        manifest = ManifestStore(cfg.output_root).for_store(store, {"index": item.folder_index, "name": item.folder_name})
        entry = manifest.entries.get(matched.pic_index, {})
        entry.update({
            "status": "success", "file_name": file_name, "image_path": str(target),
            "archived_to": str(archived_current) if archived_current else "",
            "restored_from": history_name,
            "restored_at": datetime.now().isoformat(timespec="seconds"),
        })
        job = next((j for j in _repo().build_jobs(cfg.prompt_version)
                    if j.store_index == item.folder_index and j.pic_index == matched.pic_index), None)
        if job:
            try:
                render_cfg = _apply_snapshot_and_assets(cfg, _active_batch_snapshot(cfg))
                rendered = render_storefront_glass(target, item.folder_name, **_effect_render_options(render_cfg, item.main_title))
                effect_path = storage.save_effect_image(job, rendered.data, target, "history_restore")
                runtime = dict(entry.get("runtime_metrics") or {})
                runtime["effect_image"] = rendered.metadata(effect_path)
                entry["runtime_metrics"] = runtime
            except EffectRenderError as exc:
                entry.setdefault("runtime_metrics", {})["effect_image"] = {
                    "status": "failed", "renderer": EFFECT_RENDERER_VERSION, "error": str(exc),
                }
        manifest.entries[matched.pic_index] = entry
        manifest.save()
        break

    STATE.publish({"type": "history_restored", "store": store, "file": file_name})
    return JSONResponse({
        "ok": True, "file": file_name, "restored_from": history_name,
        "archived_current": str(archived_current) if archived_current else "",
    })


# ================================================================ 失败项管理
def _collect_pending(cfg, repo, manifests, storage) -> tuple[list[dict], list[dict]]:
    """收集失败项与缺失项。"""
    failed: list[dict] = []
    missing: list[dict] = []

    for s in repo.stores:
        m = manifests.for_store(s.output_dir)
        d = storage.store_dir(s.output_dir)
        generated_dir = storage.generated_dir(s.output_dir)
        for it in s.items:
            e = m.entries.get(it.pic_index) or {}
            st = e.get("status")
            _, exists = _preview_image(d, generated_dir, it.file_name, e)
            base = {
                "store": s.folder_index,
                "store_name": s.folder_name,
                "pic": it.pic_index,
                "theme": it.theme,
                "file_name": it.file_name,
            }
            if st in ("failed", "qc_failed", "paused"):
                failed.append({
                    **base, "status": st,
                    "attempts": e.get("attempts", 0),
                    "error": (e.get("error") or "")[:400],
                })
            elif not exists:
                missing.append({**base, "status": "missing"})
    return failed, missing


@app.get("/api/failures")
async def api_failures() -> JSONResponse:
    """列出失败项与尚未生成的图片。"""
    cfg = load_config()
    repo = _repo()
    storage = Storage(cfg.output_root, cfg.timestamp_prefix)
    manifests = ManifestStore(cfg.output_root)
    failed, missing = _collect_pending(cfg, repo, manifests, storage)
    return JSONResponse({
        "failed": failed, "failed_count": len(failed),
        "missing": missing, "missing_count": len(missing),
        "total_pending": len(failed) + len(missing),
    })


@app.post("/api/retry-failures")
async def api_retry_failures() -> JSONResponse:
    """一键重试全部失败项与缺失项。"""
    import dataclasses

    if STATE.running:
        raise HTTPException(status_code=409, detail="已有任务运行中，请稍后再试")

    cfg = load_config()
    if cfg.provider == "flux_local":
        raise HTTPException(status_code=400, detail="FLUX 本地模型只允许运行固定的 01 / V8 / 6 张验证。")
    problems = cfg.validate()
    if problems:
        raise HTTPException(status_code=400, detail="；".join(problems))
    _require_confirmed_account_recovery(cfg)

    repo = _repo()
    storage = Storage(
        cfg.output_root, cfg.timestamp_prefix,
        whiten_bg=cfg.whiten_background, whiten_threshold=cfg.whiten_threshold,
    )
    manifests = ManifestStore(cfg.output_root)

    failed, missing = _collect_pending(cfg, repo, manifests, storage)
    uids = [f"{x['store']}-{x['pic']}" for x in failed + missing]
    if not uids:
        return JSONResponse({"ok": True, "count": 0, "message": "没有需要重试的项"})

    # 清掉这些项的失败记录，让编排器重新处理
    uid_set = set(uids)
    for s in repo.stores:
        m = manifests.for_store(s.output_dir)
        dirty = False
        for it in s.items:
            if f"{s.folder_index}-{it.pic_index}" in uid_set:
                m.entries.pop(it.pic_index, None)
                dirty = True
        if dirty:
            m.save()

    provider = create_provider(cfg)
    force_cfg = dataclasses.replace(cfg, overwrite=True)
    orch = Orchestrator(force_cfg, provider, repo, storage, manifests)
    orch.on_event(lambda ev: STATE.publish(ev))

    STATE.running = True
    STATE.provider_label = provider.describe()
    STATE.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def runner() -> None:
        try:
            stats = await orch.run(only_uids=uids)
            from ..runs import append_run
            append_run(
                provider=cfg.provider, provider_label=cfg.provider_label,
                model=cfg.model, prompt_version=cfg.prompt_version,
                price_per_image=cfg.price_per_image, stats=stats.to_dict(),
                scope=f"重试 {len(uids)} 张", output_root=str(cfg.output_root),
            )
        except Exception as e:                       # noqa: BLE001
            log.exception("重试失败")
            STATE.publish({"type": "run_error", "error": str(e)})
        finally:
            await provider.close()
            STATE.running = False
            STATE.orchestrator = None

    STATE.task = asyncio.create_task(runner())
    return JSONResponse({"ok": True, "count": len(uids), "uids": uids[:20]})


# ================================================================ 运行历史
@app.get("/api/runs")
async def api_runs() -> JSONResponse:
    """运行历史与累计统计。"""
    from ..runs import load_runs, summary

    records = list(reversed(load_runs()))
    return JSONResponse({"runs": records[:50], "summary": summary()})


# ================================================================ 印刷导出
def _print_export_cfg():
    """取当前配置（印刷导出用）。"""
    return load_config()


def _resolve_store_dir(cfg, batch_id: str, store_index: str):
    """把 (batch_id, store_index) 解析成门店目录。

    Returns:
        ``(store_dir, store_name)``

    Raises:
        HTTPException: 批次/门店不存在
    """
    from ..batches import is_safe_batch_id

    if not is_safe_batch_id(batch_id):
        raise HTTPException(status_code=400, detail="非法的批次 ID")

    batch_dir = Path(cfg.output_base_root) / batch_id
    if not batch_dir.is_dir():
        raise HTTPException(status_code=404, detail="批次不存在")

    repo = _repo()
    store = next((s for s in repo.stores if s.folder_index == store_index), None)
    if store is None:
        raise HTTPException(status_code=404, detail="门店不存在")

    # ⚠️ 必须走 Storage.store_dir(store.output_dir) —— 这是项目里拼门店路径的**唯一正确方式**。
    #    早先这里误用了 `store.folder_name`，但真实目录名带序号前缀（`01_房屋中介门店`），
    #    folder_name 只是 `房屋中介门店`，导致拼出的路径不存在 →
    #    接口报「该批次里没有这个门店的产出」。
    cfg_now = _print_export_cfg()
    storage = Storage(cfg_now.output_base_root, cfg_now.timestamp_prefix)
    store_dir = Path(batch_dir) / storage.store_dir(store.output_dir).name
    if not store_dir.is_dir():
        # 兜底：按序号前缀在批次目录里找（历史批次的目录名可能略有差异）
        cands = [p for p in Path(batch_dir).iterdir()
                 if p.is_dir() and p.name.startswith(f"{store_index}_")]
        if not cands:
            raise HTTPException(status_code=404, detail="该批次里没有这个门店的产出")
        store_dir = cands[0]
    return store_dir, store_dir.name


@app.get("/api/print-export/{batch_id}")
async def api_print_export_status(
    batch_id: str,
    user: "User" = Depends(require_permission("batch.export")),
) -> JSONResponse:
    """查询批次的印刷导出状态（每店/每层的成功失败）。"""
    from ..print_export import PRINT_DIR_NAME, read_print_manifest

    cfg = _print_export_cfg()
    batch_dir = Path(cfg.output_base_root) / batch_id
    from ..batches import is_safe_batch_id

    if not is_safe_batch_id(batch_id) or not batch_dir.is_dir():
        raise HTTPException(status_code=404, detail="批次不存在")

    stores: list[dict] = []
    for store_dir in sorted(p for p in batch_dir.iterdir() if p.is_dir()):
        print_dir = store_dir / PRINT_DIR_NAME
        entry: dict = {
            "store": store_dir.name,
            "dir": str(store_dir.relative_to(batch_dir).as_posix()),
            "exported": print_dir.is_dir(),
            "files": [],
            "layers": {},
            "status": "none",
            "warnings": [],
            "error": None,
        }
        if print_dir.is_dir():
            entry["files"] = sorted(
                f.name for f in print_dir.iterdir() if f.is_file()
            )
            mf = read_print_manifest(print_dir)
            if mf:
                entry["status"] = mf.get("status") or "unknown"
                entry["layers"] = mf.get("layers") or {}
                entry["warnings"] = mf.get("warnings") or []
                entry["error"] = mf.get("error")
                entry["output"] = mf.get("output") or {}
                entry["version"] = mf.get("version") or ""
        # 批次快照里的状态优先（它记录的是最近一次导出的汇总结果）
        bmf = store_dir / "_manifest.json"
        if bmf.is_file():
            try:
                data = json.loads(bmf.read_text(encoding="utf-8"))
                pe = data.get("print_export")
                if isinstance(pe, dict):
                    entry["batch_status"] = pe.get("status")
                    entry["count"] = pe.get("count")
                    if pe.get("error"):
                        entry["error"] = pe["error"]
            except (OSError, json.JSONDecodeError):
                pass
        stores.append(entry)

    enabled = bool(getattr(cfg, "print_export_enabled", True))
    return JSONResponse({
        "batch_id": batch_id,
        "enabled": enabled,
        "settings": {
            "width_cm": getattr(cfg, "print_width_cm", 60.0),
            "dpi": getattr(cfg, "print_dpi", 300),
            "bleed_mm": getattr(cfg, "print_bleed_mm", 3.0),
            "white_ink": getattr(cfg, "print_white_ink", True),
            "dieline": getattr(cfg, "print_dieline", True),
        },
        "stores": stores,
    })


@app.post("/api/print-export/{batch_id}/{store_index}")
async def api_print_export_rerun(
    batch_id: str,
    store_index: str,
    request: Request,
    user: "User" = Depends(require_permission("batch.export")),
) -> JSONResponse:
    """手动重跑某个门店的印刷导出（幂等：重复跑结果一致）。

    失败不返回 5xx，而是把结构化错误放进 `results`，便于前端提示重试。
    """
    from ..print_export import PrintExporter, collect_export_targets

    cfg = _print_export_cfg()
    if not getattr(cfg, "print_export_enabled", True):
        raise HTTPException(status_code=409, detail="印刷导出已在设置中关闭")

    store_dir, store_name = _resolve_store_dir(cfg, batch_id, store_index)

    # 只导出该门店下的生成图
    sources = sorted((store_dir / f"{store_name}生成图").glob("*.png"))
    if not sources:
        # 兼容迁移前的旧结构：PNG 直接放在门店目录下
        sources = sorted(p for p in store_dir.glob("*.png"))
    if not sources:
        raise HTTPException(status_code=404, detail="该门店没有可导出的生成图")

    payload: dict = {}
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 请求体可空
        payload = {}
    only = str(payload.get("file") or "").strip()

    targets = []
    for src in sources:
        if only and src.name != only:
            continue
        # 序号从文件名里取（形如 20260920_144553_01_楼房线稿.png）
        parts = src.stem.split("_")
        pic_index = parts[2] if len(parts) > 2 and parts[2].isdigit() else ""
        prefix = f"{store_name}_{pic_index}" if pic_index else src.stem
        targets.append((store_dir, src, prefix))

    if not targets:
        raise HTTPException(status_code=404, detail="没有匹配的源图")

    results: list[dict] = []
    raw_results: list = []          # 保留 PrintExportResult，用于回写批次 manifest
    try:
        exporter = PrintExporter(cfg)
        for sd, sp, prefix in targets:
            import asyncio as _asyncio

            res = await _asyncio.get_running_loop().run_in_executor(
                None, lambda sd=sd, sp=sp, prefix=prefix:
                exporter.export_store(sd, sp, store_name=prefix)
            )
            raw_results.append(res)
            results.append({
                "file": sp.name,
                "ok": res.ok,
                "error_code": res.error_code,
                "error_message": res.error_message,
                "elapsed": round(res.elapsed, 2),
                "files": res.files,
                "warnings": (res.manifest.warnings if res.manifest else []),
            })
    except SystemExit:
        raise HTTPException(
            status_code=503,
            detail="去背库不可用（rembg 缺少 onnxruntime 后端），已回退到纯色去背",
        ) from None
    except Exception as exc:  # noqa: BLE001
        log.exception("手动重跑印刷导出失败")
        raise HTTPException(status_code=500,
                            detail=f"导出失败：{type(exc).__name__}") from exc

    # ⚠️ 手动重跑也必须回写批次 manifest —— 否则前端/接口看到的
    #    `print_export.status` 会停留在旧值（早先漏了这一步，实测踩到）。
    try:
        from ..print_export import write_batch_status

        write_batch_status(raw_results)
    except Exception:  # noqa: BLE001 - 回写失败不影响导出结果
        log.debug("回写批次 manifest 失败（忽略）", exc_info=True)

    ok_n = sum(1 for r in results if r["ok"])
    return JSONResponse({
        "ok": ok_n == len(results),
        "batch_id": batch_id,
        "store": store_name,
        "total": len(results),
        "success": ok_n,
        "results": results,
    })


# ================================================================ 服务商能力
@app.get("/api/providers/capabilities")
async def api_provider_capabilities(
    user: "User" = Depends(require_permission("batch.read")),
) -> JSONResponse:
    """各服务商的**生成能力**（前端据此渲染「文生图/图生图/多图生图」）。

    为什么需要这个接口：

      · 不是所有服务商都支持图生图（如 FLUX 本地验证只做文生图）
      · 支持的服务商里，可用的参考图张数也不同（qwen 3 张、OpenAI 1 张）
      · 早先前端**无条件显示** 3 个模式 tab，用户选了不支持的模式后
        要么跑到一半才报错，要么（更糟）参考图被静默丢弃

    本接口只读**类属性**，不创建 Provider 实例、不读 API Key、不发网络请求。

    返回结构::

        {
          "providers": {
            "qwen": {
              "label": "阿里云百炼",
              "supports_image": true,
              "supports_multi_image": true,
              "max_references": 3,
              "image_models": ["qwen-image-3.0", "qwen-image-3.0-pro"],
              "modes": ["text", "image", "multi"]
            },
            ...
          },
          "current": "qwen",
          "current_caps": {...},
          "notes": {"image": "图生图", "multi": "多图生图"}
        }
    """
    from ..providers import get_provider
    from ..providers.base import BaseProvider
    from ..providers.catalog import PROVIDER_CATALOG

    def provider_class(name: str):
        """取服务商类。

        用 `get_provider()`（与 `create_provider()` 同一条解析路径），
        而不是按模块名猜 —— 否则 `custom`（复用 OpenAI 适配器）
        以及只有配置、没有实现的服务商都会被误判。
        """
        try:
            cls = get_provider(name)
            return cls if (isinstance(cls, type)
                           and issubclass(cls, BaseProvider)) else None
        except Exception:  # noqa: BLE001 - 未实现的服务商抛 ValueError
            return None

    out: dict = {}
    for name, meta in PROVIDER_CATALOG.items():
        cls = provider_class(name)
        caps = {
            "supports_image": bool(getattr(cls, "supports_image", False)) if cls else False,
            "supports_multi_image": bool(getattr(cls, "supports_multi_image", False)) if cls else False,
            "max_references": int(getattr(cls, "max_references", 0) or 0) if cls else 0,
            "image_models": list(getattr(cls, "image_models", ()) or ()),
        }
        modes = ["text"]
        if caps["supports_image"]:
            modes.append("image")
        if caps["supports_multi_image"]:
            modes.append("multi")
        out[name] = {
            "label": meta.get("label", name),
            # 该服务商是否真的可用（kling / zhipu 目前只有配置、没有实现模块）
            "available": cls is not None,
            "supports_negative": bool(getattr(cls, "supports_negative", False)) if cls else False,
            "supports_n": bool(getattr(cls, "supports_n", False)) if cls else False,
            "max_n": int(getattr(cls, "max_n", 1) or 1) if cls else 1,
            "rpm_limit": int(getattr(cls, "rpm_limit", 0) or 0) if cls else 0,
            **caps,
            "modes": modes,
        }

    current = ""
    try:
        current = str(load_config().provider or "")
    except Exception:  # noqa: BLE001
        current = ""

    return JSONResponse({
        "providers": out,
        "current": current,
        "current_caps": out.get(current) or {},
        "notes": {
            "text": "文生图",
            "image": "图生图（1 张参考图）",
            "multi": "多图生图（多张参考图）",
        },
    })
