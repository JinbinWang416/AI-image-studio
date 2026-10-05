# -*- coding: utf-8 -*-
"""印刷导出：流水线编排。

## 目录约定（Q1 = A：原地不动 + 硬链接）

```
output/<批次>/<门店>/
├── <门店>生成图/          ← 原 PNG **原地保留**（不改、不移、不删）
├── <门店>效果图/          ← 保留不动
├── _work/                 ← 母版区：用**硬链接**指向原 PNG（不占额外空间）
│   └── <源文件名>
├── 印刷TIF/               ← 对外交付
│   ├── <门店>_CMYK.tif
│   ├── <门店>_白墨.tif
│   ├── <门店>_刀模.tif
│   ├── <门店>_合并预览.tif
│   └── print_manifest.json
├── 预览/                  ← 前端展示（JPEG，浏览器打不开 TIF）
│   └── <门店>_预览.jpg
└── _manifest.json         ← 追加 print_export 字段
```

**为什么用硬链接而不是移动**：
- 移动会让 `api_image(view="generated")` 找不到文件 → **前端全部裂图**
- 移动会改动既有目录结构 → 历史批次与新批次不一致
- 硬链接在 **Windows 同分区下不需要管理员权限**（符号链接才需要），
  且与原件共享同一份数据，**空间不翻倍**

## 失败处理

任何一步失败都**不抛异常**，而是返回带 `error_code` 的结果，
由 orchestrator 写进批次 manifest 并记日志 —— **绝不阻断主流程**。
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .cmyk import find_icc_profile, protect_colors, to_cmyk
from .dieline import bleed_pixels, draw_dieline, find_outline
from .layers import CutoutSession, clean_alpha, cutout, make_white_ink, tighten_alpha
from .manifest import (
    ErrorCode,
    PRINT_DIR_NAME,
    PREVIEW_DIR_NAME,
    WORK_DIR_NAME,
    PrintManifest,
    update_batch_manifest,
    write_print_manifest,
)
from .preview import make_jpeg_preview, make_merged_preview

__all__ = [
    "PrintExporter",
    "PrintExportResult",
    "export_after_batch",
    "collect_export_targets",
    "write_batch_status",
]

log = logging.getLogger("app.print_export")

# 保存 TIF 时的压缩方式。LZW 无损且 RIP 兼容性好；若遇兼容问题可改 None（不压缩）
TIFF_COMPRESSION = "tiff_lzw"


@dataclass
class PrintExportResult:
    """单店导出结果。"""

    ok: bool = False
    store_dir: Path | None = None
    print_dir: Path | None = None
    preview_dir: Path | None = None
    files: list[str] = field(default_factory=list)
    manifest: PrintManifest | None = None
    error_code: str = ErrorCode.OK
    error_message: str = ""
    elapsed: float = 0.0

    def to_manifest_patch(self) -> dict:
        """转成写进批次 `_manifest.json` 的 `print_export` 字段。"""
        patch: dict = {
            "status": "success" if self.ok else "failed",
            "exported_at": (self.manifest.exported_at if self.manifest else ""),
            "dir": PRINT_DIR_NAME,
            "files": list(self.files),
            "version": (self.manifest.version if self.manifest else ""),
        }
        if not self.ok:
            patch["error"] = {"code": self.error_code, "message": self.error_message}
        if self.manifest and self.manifest.warnings:
            patch["warnings"] = list(self.manifest.warnings)
        return patch


def _link_or_copy(src: Path, dst: Path) -> str:
    """优先硬链接，失败则复制。

    Returns:
        ``"hardlink"`` / ``"copy"`` / ``"exists"`` / ``"failed"``
    """
    if dst.exists():
        try:
            if dst.stat().st_size == src.stat().st_size:
                return "exists"
        except OSError:
            pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)            # 硬链接：同分区、无需管理员
        return "hardlink"
    except OSError:
        pass
    try:
        shutil.copy2(src, dst)       # 跨分区或文件系统不支持时退化为复制
        return "copy"
    except OSError as exc:
        log.debug("母版链接/复制失败：%s", exc)
        return "failed"


def resolve_print_source(source_png: Path) -> tuple[Path, bool]:
    """有超分放大版就用它做印刷源，否则用原图。

    ⚠️ 这一步是让 `effective_dpi` 真正上去的**关键**：

       `effective_dpi` 按**源图像素**推算（`src_w / (width_cm/2.54)`）。
       拿 1024px 的原图印 60cm，算出来只有 43 DPI，会触发清晰度警告、
       诱发人工重跑白烧 API 费；而超分放大版是 4096px，算出来就是 173 DPI。

       **母版 1024 一个字节都不动** —— 只是换了个更清晰的源来印刷
       （批次目录文件绝不覆盖，AGENTS.md 硬规则）。

    放大版由 P3 的生成图放大链路产出，命名 `<stem>_up<倍数>x.png`，
    例如 `20260919_014534_03_房屋门牌_up4x.png`。

    Returns:
        ``(要用的源路径, 是否用了放大版)``
    """
    src = Path(source_png)
    if not src.is_file():
        return src, False
    try:
        cands = [
            p for p in src.parent.glob(f"{src.stem}_up*x.png")
            if p.is_file() and p != src
        ]
    except OSError:
        return src, False
    if not cands:
        return src, False

    best, best_px = src, 0
    for p in cands:
        try:
            with Image.open(p) as im:
                px = im.size[0] * im.size[1]
        except OSError:
            continue
        if px > best_px:
            best, best_px = p, px

    if best is src:
        return src, False
    log.info("印刷使用超分放大版：%s", best.name)
    return best, True


class PrintExporter:
    """印刷导出器。

    ⚠️ **批量时复用同一个实例** —— `CutoutSession` 会缓存 rembg 模型，
       每张重建会话会让单张耗时从 1~2 秒涨到 5 秒以上。
    """

    def __init__(self, cfg, *, session: CutoutSession | None = None) -> None:
        self.cfg = cfg

        # ---- 去背方式（settings.print.cutout）----
        #   auto     有 rembg 就用（默认）
        #   rembg    强制用；不可用时记警告并降级（**不报错**，导出不能因此失败）
        #   fallback 跳过 rembg，直接用纯色去背（快约 6 倍）
        mode = str(getattr(cfg, "print_cutout", "auto") or "auto").strip().lower()
        if mode not in ("auto", "rembg", "fallback"):
            log.warning("未知的去背方式 %r，回落到 auto", mode)
            mode = "auto"
        self.cutout_mode = mode

        if mode == "fallback":
            self.session: CutoutSession | None = None
        else:
            self.session = session if session is not None else CutoutSession()
            if mode == "rembg" and self.session is not None and not self.session.available:
                log.warning(
                    "配置要求 rembg，但它不可用（%s）—— 已降级为纯色去背",
                    self.session.last_error,
                )
        # 实际会用到的引擎（写进 manifest，便于追溯）
        self.engine = (
            "fallback" if self.session is None or not self.session.available else "rembg"
        )

        self._icc_path, self._icc_source = find_icc_profile(
            getattr(cfg, "print_icc_path", "") or ""
        )

    # ---------------------------------------------------------------- 分辨率提升
    def _upscale_rgba(self, rgba: Image.Image, target: tuple[int, int],
                      mf) -> tuple[Image.Image, dict]:
        """把 RGBA 放大到目标尺寸，返回 `(图, upscaler 信息)`。

        ⚠️ 优先用 Upscayl 本地超分（免费，细节比插值好得多），
           **失败或未开启时静默回落 LANCZOS** —— 这是硬性约束：
           超分是个可选的增强，不能因为它把整条印刷链路搞停。

           回落时记 `UPSCAYL_FAILED` 到 manifest，否则「这张图为什么偏糊」
           事后无从追溯。
        """
        target_w, target_h = target
        cfg = getattr(self.cfg, "upscayl", None)
        want = bool(cfg and getattr(cfg, "enabled", False)
                    and getattr(cfg, "upscale_print", False))
        if not want:
            return rgba.resize((target_w, target_h), Image.LANCZOS), {}

        info: dict = {}
        try:
            from ..upscayl import DEFAULT_TILE, UpscaylEngine, upscale_rgba_to_target

            engine = UpscaylEngine(binary=str(getattr(cfg, "binary_path", "") or ""))
            if not engine.available:
                raise RuntimeError(engine.unavailable_reason())

            import tempfile

            with tempfile.TemporaryDirectory(prefix="upscayl_print_") as tmp:
                tmp_in = Path(tmp) / "in.png"
                tmp_out = Path(tmp) / "out.png"
                rgba.save(tmp_in)
                got = upscale_rgba_to_target(
                    engine, tmp_in, tmp_out,
                    max(target_w, target_h),
                    str(getattr(cfg, "model_print", "") or "realesrgan-x4plus"),
                    int(getattr(cfg, "tile", DEFAULT_TILE) or DEFAULT_TILE),
                )
                if got is None:
                    raise RuntimeError("Upscayl 执行失败")
                with Image.open(got) as im:
                    up = im.convert("RGBA")
                info = {
                    "engine": "upscayl",
                    "model": str(getattr(cfg, "model_print", "")),
                    "tile": int(getattr(cfg, "tile", DEFAULT_TILE) or DEFAULT_TILE),
                    "from": list(rgba.size),
                    "to": list(up.size),
                }
            # 超分的倍数不一定是目标尺寸，缩到精确值（细节已由超分补上）
            if up.size != (target_w, target_h):
                up = up.resize((target_w, target_h), Image.LANCZOS)
            log.info("印刷超分完成：%s", info.get("model"))
            return up, info
        except Exception as exc:  # noqa: BLE001 - 任何失败都要降级，不能中断导出
            log.warning("Upscayl 超分失败，回落 LANCZOS：%s: %s", type(exc).__name__, exc)
            mf.error_code = mf.error_code or ErrorCode.UPSCAYL_FAILED
            mf.warnings.append(f"Upscayl 超分失败，已回落 LANCZOS：{type(exc).__name__}")
            return rgba.resize((target_w, target_h), Image.LANCZOS), {}

    # ---------------------------------------------------------------- 工具
    def _target_size(self, src_w: int, src_h: int) -> tuple[int, int]:
        """按实际尺寸 + DPI 算目标像素，并受 `print_max_pixels` 限制。"""
        width_cm = float(getattr(self.cfg, "print_width_cm", 60.0) or 60.0)
        dpi = int(getattr(self.cfg, "print_dpi", 300) or 300)
        max_px = int(getattr(self.cfg, "print_max_pixels", 8000) or 8000)

        target_w = max(1, round(width_cm / 2.54 * dpi))
        ratio = (src_h / src_w) if src_w else 1.0
        target_h = max(1, round(target_w * ratio))

        longest = max(target_w, target_h)
        if longest > max_px:
            scale = max_px / longest
            target_w = max(1, round(target_w * scale))
            target_h = max(1, round(target_h * scale))
            log.info("目标尺寸受 print_max_pixels=%d 限制，缩到 %dx%d",
                     max_px, target_w, target_h)
        return target_w, target_h

    # ---------------------------------------------------------------- 单店导出
    def export_store(self, store_dir: Path, source_png: Path, *,
                     store_name: str = "", dry_run: bool = False
                     ) -> PrintExportResult:
        """对一张 PNG 跑完整流水线。

        Args:
            store_dir: `<批次>/<门店>/` 目录
            source_png: 源 PNG 路径
            store_name: 门店名（用于文件命名）；空则取目录名
            dry_run: 只算尺寸与路径，不写文件（用于自检）
        """
        t0 = time.monotonic()
        store_dir = Path(store_dir)
        source_png = Path(source_png)
        name = store_name or store_dir.name
        result = PrintExportResult(store_dir=store_dir)

        # ⚠️ 有超分放大版就换它当源（母版不动）—— 这一步直接决定 effective_dpi
        source_png, used_upscaled = resolve_print_source(source_png)

        print_dir = store_dir / PRINT_DIR_NAME
        preview_dir = store_dir / PREVIEW_DIR_NAME
        work_dir = store_dir / WORK_DIR_NAME
        result.print_dir = print_dir
        result.preview_dir = preview_dir

        mf = PrintManifest()
        mf.source_png = self._rel(source_png)
        if used_upscaled:
            mf.options["source_is_upscaled"] = True

        # ---- 源文件检查 ----
        if not source_png.is_file():
            return self._fail(result, mf, ErrorCode.SOURCE_MISSING,
                              f"源 PNG 不存在：{source_png.name}", t0)

        try:
            src = Image.open(source_png)
            src.load()
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, mf, ErrorCode.SOURCE_UNREADABLE,
                              f"源 PNG 无法打开：{type(exc).__name__}", t0)

        mf.source_pixels = src.size
        width_cm = float(getattr(self.cfg, "print_width_cm", 60.0) or 60.0)
        mf.width_cm = width_cm
        # 有效 DPI：按源图像素与实际尺寸推算的真实清晰度
        mf.effective_dpi = (src.size[0] / (width_cm / 2.54)) if width_cm else 0.0

        if dry_run:
            tw, th = self._target_size(*src.size)
            mf.output_pixels = (tw, th)
            mf.dpi = int(getattr(self.cfg, "print_dpi", 300) or 300)
            result.ok = True
            result.manifest = mf
            result.elapsed = time.monotonic() - t0
            return result

        # ---- ① 去背 + 收边 ----
        try:
            rgba, method = cutout(src, self.session)
            rgba = clean_alpha(rgba)
            rgba = tighten_alpha(rgba, 1)     # 1px 收边，去掉印刷杂边
            mf.options["cutout"] = method
            mf.options["cutout_mode"] = self.cutout_mode   # 配置意图
            mf.options["cutout_engine"] = self.engine      # 实际引擎
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, mf, ErrorCode.CUTOUT_FAILED,
                              f"去背失败：{type(exc).__name__}", t0)

        # ---- ⑤ 分辨率提升（在生成各层之前做，保证各层尺寸一致）----
        target_w, target_h = self._target_size(*rgba.size)
        mf.output_pixels = (target_w, target_h)
        mf.dpi = int(getattr(self.cfg, "print_dpi", 300) or 300)
        mf.height_cm = round(width_cm * target_h / max(1, target_w), 2)
        try:
            big, up_info = self._upscale_rgba(rgba, (target_w, target_h), mf)
            if up_info:
                mf.options["upscaler"] = up_info
        except Exception as exc:  # noqa: BLE001 - 通常是内存不足
            return self._fail(result, mf, ErrorCode.RESIZE_FAILED,
                              f"放大到 {target_w}x{target_h} 失败：{type(exc).__name__}", t0)

        bleed_mm = float(getattr(self.cfg, "print_bleed_mm", 3.0) or 0.0)
        mf.bleed_mm = bleed_mm
        mf.bleed_px = bleed_pixels(bleed_mm, mf.dpi)

        if not dry_run:
            try:
                print_dir.mkdir(parents=True, exist_ok=True)
                preview_dir.mkdir(parents=True, exist_ok=True)
                work_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return self._fail(result, mf, self._os_code(exc),
                                  f"创建输出目录失败：{type(exc).__name__}", t0)

        # ---- 母版归档（硬链接，原地不动）----
        if getattr(self.cfg, "print_keep_work", True):
            link_mode = _link_or_copy(source_png, work_dir / source_png.name)
            mf.options["work_link"] = link_mode

        # ---- ② 白墨层 ----
        white_path = print_dir / f"{name}_白墨.tif"
        if getattr(self.cfg, "print_white_ink", True):
            try:
                white = make_white_ink(
                    big,
                    invert=bool(getattr(self.cfg, "print_white_ink_invert", False)),
                )
                white.save(white_path, format="TIFF", compression=TIFF_COMPRESSION)
                mf.layers["white_ink"] = white_path.name
                mf.options["white_ink"] = True
                mf.options["white_ink_invert"] = bool(
                    getattr(self.cfg, "print_white_ink_invert", False)
                )
            except OSError as exc:
                return self._fail(result, mf, self._os_code(exc),
                                  f"白墨层写入失败：{type(exc).__name__}", t0)
            except Exception as exc:  # noqa: BLE001
                mf.warnings.append(f"白墨层生成失败：{type(exc).__name__}")
        else:
            mf.options["white_ink"] = False
            white = None

        # ---- ④ 刀模线 + 出血 ----
        dieline_path = print_dir / f"{name}_刀模.tif"
        if getattr(self.cfg, "print_dieline", True):
            try:
                outline = find_outline(big, bleed_px=mf.bleed_px)
                if not outline:
                    mf.warnings.append(f"刀模轮廓生成失败：{outline.error}")
                else:
                    line = draw_dieline((target_w, target_h), outline)
                    line.save(dieline_path, format="TIFF", compression=TIFF_COMPRESSION)
                    mf.layers["dieline"] = dieline_path.name
                    mf.options["dieline"] = True
                    mf.options["dieline_closed"] = bool(outline.closed)
                    mf.options["dieline_points"] = int(outline.points)
                    if not outline.closed:
                        mf.warnings.append("刀模轮廓首尾未完全闭合，已强制闭合绘制，建议人工核对")
            except OSError as exc:
                return self._fail(result, mf, self._os_code(exc),
                                  f"刀模层写入失败：{type(exc).__name__}", t0)
            except Exception as exc:  # noqa: BLE001
                mf.warnings.append(f"刀模层生成失败：{type(exc).__name__}")
        else:
            mf.options["dieline"] = False

        # ---- ③ RGB → CMYK + 偏色保护 ----
        cmyk_path = print_dir / f"{name}_CMYK.tif"
        try:
            cmyk_img, icc_info = to_cmyk(big, self._icc_path)
            icc_info["source"] = self._icc_source
            mf.icc = icc_info
            if icc_info.get("fallback"):
                mf.warnings.append(icc_info.get("note") or "ICC 缺失，已降级为朴素转换")

            cmyk_img, notes = protect_colors(cmyk_img, big)
            if notes:
                mf.warnings.extend(f"偏色保护 · {n}" for n in notes)
                mf.options["color_protection"] = notes

            cmyk_img.save(cmyk_path, format="TIFF", compression=TIFF_COMPRESSION)
            mf.layers["cmyk"] = cmyk_path.name
        except OSError as exc:
            return self._fail(result, mf, self._os_code(exc),
                              f"CMYK 层写入失败：{type(exc).__name__}", t0)
        except Exception as exc:  # noqa: BLE001
            return self._fail(result, mf, ErrorCode.UNKNOWN,
                              f"CMYK 转换失败：{type(exc).__name__}", t0)

        # ---- ⑥ 合并预览 TIF + JPEG 缩略图（**可关闭**）----
        #
        # ⚡ 性能优化：合并预览只用于**人工核对**，不需要印刷分辨率。
        #    7087px 的三层合并要 ~5 秒（LZW 压缩大头），缩到 2000px 后 <1 秒。
        #    彩色层与各图层仍保持完整分辨率，不受影响。
        #
        # ⚠️ 交付给印刷厂时这两个都**没用**（H-2003E + Caldera RIP 只吃
        #    CMYK / 白墨 / 刀模三层），默认关闭。想人工核对时再打开。
        #
        #    读的是**子配置**（`cfg.print.keep_*`）而不是扁平属性：
        #    `FLAT_TO_GROUP` 是「旧字段名 → 子配置」的兼容映射，只登记旧字段；
        #    新字段直接用子配置，否则 `getattr` 永远拿不到值（想开也开不了）。
        #
        # ⚠️ 注意**不要**顺手把 `white_ink` / `dieline` 也关掉 ——
        #    UV 打印在透明介质上必须白墨打底，刀模是模切必需的。
        print_cfg = getattr(self.cfg, "print", None)
        merged_path: Path | None = None
        if getattr(print_cfg, "keep_merged_preview", False):
            merged_path = print_dir / f"{name}_合并预览.tif"
            try:
                preview_edge = min(int(getattr(self.cfg, "print_preview_px", 2000) or 2000),
                                   max(target_w, target_h))
                ratio = preview_edge / max(1, max(target_w, target_h))
                mw, mh = max(1, round(target_w * ratio)), max(1, round(target_h * ratio))

                merged = make_merged_preview(
                    cmyk_img,
                    white if getattr(self.cfg, "print_white_ink", True) else None,
                    Image.open(dieline_path) if dieline_path.is_file() else None,
                    size=(target_w, target_h),
                )
                if (mw, mh) != (target_w, target_h):
                    merged = merged.resize((mw, mh), Image.LANCZOS)
                merged.save(merged_path, format="TIFF", compression=TIFF_COMPRESSION)
                mf.layers["merged_preview"] = merged_path.name
                mf.options["merged_preview_px"] = [mw, mh]
            except Exception as exc:  # noqa: BLE001
                mf.warnings.append(f"合并预览生成失败：{type(exc).__name__}")
                merged_path = None

        preview_path: Path | None = None
        if getattr(print_cfg, "keep_preview_jpg", False):
            preview_path = preview_dir / f"{name}_预览.jpg"
            try:
                jpg = make_jpeg_preview(cmyk_img)
                jpg.save(preview_path, format="JPEG", quality=85, optimize=True)
                mf.layers["preview_jpeg"] = str(
                    preview_path.relative_to(store_dir).as_posix()
                )
            except Exception as exc:  # noqa: BLE001
                mf.warnings.append(f"JPEG 预览生成失败：{type(exc).__name__}")
                preview_path = None

        # ---- ⑦ manifest ----
        mf.status = "success"
        try:
            write_print_manifest(print_dir, mf)
        except OSError as exc:
            return self._fail(result, mf, self._os_code(exc),
                              f"manifest 写入失败：{type(exc).__name__}", t0)

        result.ok = True
        result.manifest = mf
        # ⚠️ 只列**本次产出**的文件，不能 `iterdir()` 整个目录 ——
        #    一个门店有 6 张图，共享同一个 印刷TIF/ 目录，
        #    列全目录会让"重跑一次"的文件数越滚越多（实测 5 → 25）。
        #
        # ⚠️ 交付给印刷厂的是 **CMYK + 白墨 + 刀模** 三层：
        #      · CMYK —— 印刷主文件
        #      · 白墨 —— UV 打印在透明介质（玻璃贴纸）上必须打底，
        #                少了它颜色是透明的、贴上去看不见
        #      · 刀模 —— 模切轮廓
        #    合并预览与 JPEG 缩略图默认不生成（见 ⑥），所以这里可能为 None。
        produced = [cmyk_path, white_path, dieline_path]
        if merged_path is not None:
            produced.append(merged_path)
        produced.append(print_dir / "print_manifest.json")
        result.files = [p.name for p in produced if p is not None and p.is_file()]
        result.elapsed = time.monotonic() - t0
        log.info("印刷导出完成：%s（%d 个文件，%.1fs，去背=%s）",
                 name, len(result.files), result.elapsed, mf.options.get("cutout", "?"))
        return result

    # ---------------------------------------------------------------- 辅助
    def _rel(self, p: Path) -> str:
        """转成相对项目根的路径，避免把用户绝对路径写进 manifest。"""
        try:
            root = Path(__file__).resolve().parent.parent.parent
            return p.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            return p.name

    @staticmethod
    def _os_code(exc: OSError) -> str:
        """把 OSError 归类成结构化错误码。"""
        errno = getattr(exc, "errno", None)
        winerr = getattr(exc, "winerror", None)
        if errno in (28, 112) or winerr in (28, 112) or "space" in str(exc).lower():
            return ErrorCode.STORAGE_FULL
        return ErrorCode.STORAGE_ERROR

    def _fail(self, result: PrintExportResult, mf: PrintManifest,
              code: str, message: str, t0: float) -> PrintExportResult:
        """统一失败出口：写 manifest（尽力而为）+ 返回结果，**不抛异常**。"""
        mf.status = "failed"
        mf.error_code = code
        mf.error_message = message
        result.ok = False
        result.error_code = code
        result.error_message = message
        result.manifest = mf
        result.elapsed = time.monotonic() - t0
        log.warning("印刷导出失败 [%s] %s：%s", code, result.store_dir, message)
        # 失败也留一份 manifest，便于排障（写不了就算了，不能因为写日志再抛错）
        if result.print_dir is not None:
            try:
                result.print_dir.mkdir(parents=True, exist_ok=True)
                write_print_manifest(result.print_dir, mf)
            except Exception:  # noqa: BLE001
                pass
        return result


# ================================================================ 批次级入口
def collect_export_targets(jobs, output_root: Path | str | None = None
                           ) -> list[tuple[Path, Path, str]]:
    """从 Job 列表挑出可导出的目标。

    ⚠️ **路径拼接必须走 `Storage.store_dir()`**：

        `job.output_dir` 是**门店标识**（如 `01_房屋中介门店`），
        不是完整路径。完整路径 = `output_root / output_dir`。
        早先这里直接 `Path(job.output_dir)`，得到的是**相对路径**，
        导致导出写到了 CWD 下的同名目录（或直接找不到），
        批次里反而没有产物 —— 实测踩到。

    ⚠️ **需求写的是 `<门店>_CMYK.tif`，但一个门店有 6 张图（6 个主题）** ——
       按字面命名会互相覆盖。所以文件名带上图片序号：

           <门店>_<pic_index>_CMYK.tif     例：01_房屋中介门店_03_CMYK.tif

       这样"每张可印刷的贴纸一套文件"，语义才正确。

    Args:
        jobs: Job 列表
        output_root: 输出根（含批次）；None 时从 job 的 image_path 反推

    Returns:
        ``[(store_dir, source_png, name_prefix), ...]``
    """
    from pathlib import Path as _P

    seen: set[tuple[str, str]] = set()
    out: list[tuple[Path, Path, str]] = []

    root = _P(output_root) if output_root else None

    for job in jobs:
        img = getattr(job, "image_path", "") or ""
        if not img:
            continue
        src = _P(img)
        if not src.is_file():
            continue

        # 门店标识 → 完整目录
        out_dir = str(getattr(job, "output_dir", "") or "")
        if root is not None and out_dir:
            store_dir = root / out_dir
        elif out_dir and _P(out_dir).is_absolute():
            store_dir = _P(out_dir)
        else:
            # 兜底：PNG 在 <门店>/<门店>生成图/ 下，上溯两级即门店目录
            store_dir = src.parent.parent

        if not store_dir.is_dir():
            # 再兜底：按门店标识在父目录里找同名前缀的目录
            cands = [p for p in store_dir.parent.iterdir()
                     if p.is_dir() and p.name == out_dir] if store_dir.parent.is_dir() else []
            if not cands:
                log.debug("跳过无法定位门店目录的 job：%s（output_dir=%s）",
                          src.name, out_dir)
                continue
            store_dir = cands[0]

        store_name = getattr(job, "store_name", "") or store_dir.name
        pic_index = str(getattr(job, "pic_index", "") or "").strip()

        # 命名：门店名 + 图片序号（无序号时退回原文件名主干）
        prefix = f"{store_name}_{pic_index}" if pic_index else src.stem
        key = (str(store_dir), prefix)
        if key in seen:
            continue
        seen.add(key)
        out.append((store_dir, src, prefix))

    return out


def export_after_batch(cfg, store_dirs_and_sources, *, session: CutoutSession | None = None
                       ) -> list[PrintExportResult]:
    """批次完成后的导出入口（供 orchestrator 调用）。

    ⚠️ **本函数绝不抛异常** —— 印刷导出是"附加价值"，
       失败不能让整批生成任务显示为失败。

    Args:
        cfg: 运行时配置
        store_dirs_and_sources: ``[(store_dir, source_png, name_prefix), ...]``
    """
    results: list[PrintExportResult] = []
    if not getattr(cfg, "print_export_enabled", True):
        log.info("印刷导出已在设置中关闭，跳过")
        return results

    exporter = PrintExporter(cfg, session=session)
    for store_dir, source_png, name_prefix in store_dirs_and_sources:
        store_dir = Path(store_dir)
        try:
            res = exporter.export_store(store_dir, Path(source_png),
                                        store_name=name_prefix)
        except SystemExit as exc:
            # ⚠️ rembg 缺后端时会 sys.exit()，必须在此拦住 —— 否则整个服务退出
            log.error("印刷导出触发 SystemExit（通常是 rembg 缺 onnxruntime）：%s", exc)
            res = PrintExportResult(
                ok=False, store_dir=store_dir, error_code=ErrorCode.CUTOUT_FAILED,
                error_message="去背库不可用（rembg 缺少 onnxruntime 后端）",
            )
        except Exception as exc:  # noqa: BLE001 - 兜底：任何意外都不能冒泡
            log.exception("印刷导出发生未预期错误：%s", store_dir)
            res = PrintExportResult(
                ok=False, store_dir=store_dir, error_code=ErrorCode.UNKNOWN,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        results.append(res)

    # 状态回写批次 manifest（只增不改）。同一门店多个 prefix 时，
    # 汇总成一条 print_export 记录（files 合并），避免后写覆盖先写。
    try:
        write_batch_status(results)
    except Exception as exc:  # noqa: BLE001
        log.debug("回写批次 manifest 失败（忽略）：%s", exc)

    ok = sum(1 for r in results if r.ok)
    log.info("印刷导出批次完成：成功 %d / 共 %d", ok, len(results))
    return results


def write_batch_status(results: list[PrintExportResult]) -> None:
    """按门店汇总导出状态，写进各自的 `<门店>/_manifest.json`。

    公开接口：`export_after_batch()` 与网页的手动重跑都会调用它，
    保证两条路径写出的状态一致（早先手动重跑漏了这步，导致状态不更新）。
    """
    by_store: dict[Path, list[PrintExportResult]] = {}
    for r in results:
        if r.store_dir is not None:
            by_store.setdefault(Path(r.store_dir), []).append(r)

    for store_dir, group in by_store.items():
        ok_n = sum(1 for r in group if r.ok)
        failed = [r for r in group if not r.ok]
        all_files: list[str] = []
        warn: list[str] = []
        for r in group:
            all_files.extend(r.files)
            if r.manifest and r.manifest.warnings:
                warn.extend(r.manifest.warnings)

        patch: dict = {
            "status": "success" if not failed else ("partial" if ok_n else "failed"),
            "exported_at": next(
                (r.manifest.exported_at for r in group if r.manifest), ""
            ),
            "dir": PRINT_DIR_NAME,
            "count": {"total": len(group), "success": ok_n, "failed": len(failed)},
            "files": all_files,
            "version": next((r.manifest.version for r in group if r.manifest), ""),
        }
        if failed:
            patch["error"] = {
                "code": failed[0].error_code,
                "message": failed[0].error_message,
            }
        if warn:
            patch["warnings"] = warn[:20]      # 避免 manifest 无限膨胀

        update_batch_manifest(store_dir, patch)
