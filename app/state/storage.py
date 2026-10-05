# -*- coding: utf-8 -*-
"""
存储层：创建 23 个输出文件夹、保存图片、维护目录结构。

## 命名规则

**生成图**：`JB31` + 6 位序号，如 `JB31000001.png`
**效果图**：同一序号 + 后缀，如 `JB31000001_效果图.png`

序号是**全局递增、永不重置**的（跨门店、跨批次连续），升序排列即生成顺序。
序号与门店/主题的对应关系记在各自目录的 manifest 里。

> 历史批次用的是 `01_水龙头.png` / `20260918_164130_01_水龙头.png` 这类
> 「语义命名」，**读取路径仍兼容**（`exists()` 会一并查找），不会因为改命名
> 就看不了旧批次。

## 体积上限

生成图与效果图**保存时压到 3 MB 以内**（`MAX_OUTPUT_BYTES`）。
⚠️ PNG 是无损格式，压体积只能**降分辨率** —— 降掉的清晰度由
`app/upscayl/` 的本地超分在印刷时补回来，所以展示体积与印刷质量不冲突。
**印刷链路不受这个上限约束。**

**后处理**：保存前会把贴纸外部的背景刷成纯白
（模型对「白图 + 彩色背景」有固有偏好，提示词压不住，见 `app/postprocess.py`）。
"""
from __future__ import annotations

import io
import re
import threading
from datetime import datetime
from pathlib import Path

from PIL import Image

from ..core.logging import get_logger
from ..core.models import Job, Store
from ..effect.postprocess import whiten_background

log = get_logger("storage")

# 历史版本归档目录（位于生成图或效果图文件夹内）
HISTORY_DIR = "_history"
GENERATED_DIR_SUFFIX = "生成图"
EFFECT_DIR_SUFFIX = "效果图"

# ---------------------------------------------------------------- 体积上限
# 生成图与效果图的单文件上限。印刷链路**不受**此限制 ——
# 压缩损失的分辨率由 `app/upscayl/` 的本地超分在印刷时补回来。
MAX_OUTPUT_BYTES = 3 * 1024 * 1024
# 压缩时的最小边下限：再小就没法看了，宁可超一点也保住可读性
_MIN_EDGE = 512

# Windows 文件名非法字符
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    """把任意字符串转为安全的文件名片段。"""
    cleaned = _ILLEGAL.sub("_", name).strip().rstrip(".")
    return cleaned or "unnamed"


def compress_png_to_limit(
    data: bytes,
    limit: int = MAX_OUTPUT_BYTES,
    min_edge: int = _MIN_EDGE,
) -> tuple[bytes, dict]:
    """把 PNG 压到 ``limit`` 字节以内，返回 `(数据, 说明)`。

    ⚠️ **为什么只能靠降分辨率**：PNG 是无损压缩，`optimize=True` 通常只能
       省几个百分点；转 JPEG 虽然小得多，但白底硬边贴纸会出现**边缘噪点**，
       印刷厂也不收有损格式。所以这里逐步等比缩小。

    ⚠️ 降掉的清晰度由 Upscayl 本地超分在印刷时补回来
       （见 `app/upscayl/`），所以**展示体积与印刷质量不冲突**。

    已经是 PNG 且未超限时**原样返回**，不做任何重编码 —— 避免无谓的画质损失。
    """
    info: dict = {"original_bytes": len(data), "compressed": False}
    if len(data) <= limit:
        info["final_bytes"] = len(data)
        return data, info

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            img = im.copy()
    except Exception as exc:  # noqa: BLE001 - 打不开就原样返回，交由上层决定
        log.warning("压缩前无法读取图片，跳过压缩：%s: %s", type(exc).__name__, exc)
        info["final_bytes"] = len(data)
        info["error"] = type(exc).__name__
        return data, info

    best = data
    for _ in range(12):
        w, h = img.size
        longest = max(w, h)
        if longest <= min_edge:
            break
        # 按目标字节数比例**保守**缩小（面积比 ≈ 字节比），每轮至少缩 5%
        ratio = max(0.05, min(0.9, (limit / max(1, len(best))) ** 0.5))
        new_long = max(min_edge, int(longest * ratio))
        if new_long >= longest:
            new_long = max(min_edge, longest - max(1, longest // 20))
        scale = new_long / longest
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

        buf = io.BytesIO()
        # RGBA 才带 alpha；其它模式按原模式存（P 模式转 RGBA 更安全）
        if img.mode == "P":
            img = img.convert("RGBA")
        img.save(buf, format="PNG", optimize=True)
        best = buf.getvalue()
        info["final_pixels"] = list(img.size)
        if len(best) <= limit:
            break

    info["compressed"] = True
    info["final_bytes"] = len(best)
    info["final_pixels"] = list(img.size)
    if len(best) > limit:
        # 到下限还超：如实记录，不静默装作成功
        info["over_limit"] = True
        log.warning("已缩到最小边长 %d 仍超过 %d 字节（实际 %d）",
                    min_edge, limit, len(best))
    else:
        log.info("生成图已压缩：%.2f MB → %.2f MB（%dx%d）",
                 len(data) / 1024 / 1024, len(best) / 1024 / 1024, *img.size)
    return best, info


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
        """保存图片二进制（背景刷白 → 体积压缩 → 旧版本归档）。

        Args:
            version_tag: 归档标签，通常为 `{提示词版本}_{模型}`，便于事后对比
        """
        raw_size = len(data)
        if self.whiten_bg:
            try:
                data = whiten_background(data, self.whiten_threshold)
            except Exception:                     # 后处理失败不影响主流程
                log.warning("背景刷白失败，保存原图：%s", job.file_name)

        # 体积上限（生成图 ≤ 3MB）。⚠️ 降掉的清晰度由 Upscayl 超分在印刷时补回。
        data, up_info = compress_png_to_limit(data, MAX_OUTPUT_BYTES)
        job.runtime_metrics["output_size"] = up_info

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

    def save_upscaled_image(
        self,
        source_path: Path | str,
        data: bytes,
        scale: int,
    ) -> Path:
        """把超分放大版另存为 `<stem>_up<倍数>x.png`。

        ⚠️ **绝不覆盖母版**（AGENTS.md 硬规则：批次目录文件绝不覆盖）。
           放大版与母版**并存**，由 `print_export.resolve_print_source()`
           在印刷时择优选用 —— 这样 1024px 的原始产出仍可追溯，
           而印刷可以拿到 4096px 的清晰源（effective_dpi 从 43 提到 173）。

        ⚠️ **幂等**：重复调用（重跑、续跑）只会覆盖同名放大版，
           不会堆出一串 `_up4x_up4x.png`。

        Args:
            source_path: 母版路径（用于推导放大版文件名）
            data: 放大后的 PNG 字节
            scale: 倍数，进文件名便于区分不同倍数的产物

        Returns:
            放大版路径
        """
        src = Path(source_path)
        path = src.with_name(f"{src.stem}_up{int(scale)}x{src.suffix or '.png'}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(path)              # 原子替换
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        log.info("超分放大版已另存：%s（%.1f MB）", path.name, len(data) / 1024 / 1024)
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
        # 体积上限（效果图同样 ≤ 3MB）；压缩失败就存原图，不阻断合成
        data, info = compress_png_to_limit(data, MAX_OUTPUT_BYTES)
        job.runtime_metrics["effect_size"] = info
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
