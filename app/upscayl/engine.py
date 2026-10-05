# -*- coding: utf-8 -*-
"""Upscayl 引擎封装：调 `upscayl-bin.exe` 做本地超分。

## 两个必须遵守的实测结论

**① `-t 0`（auto tile）会输出全黑图，绝对禁止。**
   实测 2048px 输入 + `-t 0` 触发 `vkQueueSubmit failed -4`，
   输出尺寸正常但 `mean=0 / stddev=0`（全黑）。显式 `-t 64/128/256` 都正常。
   这里的 `_build_cmd()` **永远写死一个 ≥32 的整数**，调用方传 0 也会被纠正。

**② 关于 alpha**：项目交接文档称「CLI 会丢弃 PNG alpha」，但本次实测
   （`realesr-animevideov3-x2`，透明背景贴纸 44.7% → 44.4%）**alpha 被完整保留**，
   甚至还生成了抗锯齿边缘。不过印刷链路（`print_export.cutout()` 之后）确实带 alpha，
   所以仍按原方案走 `alpha.py` 的拆合处理 —— 它无论如何都安全。

## 失败一律降级

缺二进制、无 Vulkan、超时、OOM —— 全部返回 `None` 并记 warning，
由调用方回落 LANCZOS。**绝不因为超分失败而中断批次。**
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Sequence

from ..core.paths import PACKAGE_ROOT
from .models import (
    ALLOWED_SCALES,
    DEFAULT_TILE,
    UPSCAYL_MODELS,
    get_model,
)

log = logging.getLogger(__name__)

# CLI 相对项目根的位置（冻结后 PACKAGE_ROOT = sys._MEIPASS）
DEFAULT_REL = Path("tools") / "upscayl"
MIN_TILE = 32
DEFAULT_TIMEOUT = 120.0


class UpscaylError(RuntimeError):
    """超分失败。调用方应捕获并降级，而不是让它冒泡中断批次。"""


def resolve_binary(explicit: str = "") -> Path | None:
    """定位 `upscayl-bin.exe`。

    Args:
        explicit: 用户在设置里指定的路径；为空时用默认位置。

    Returns:
        可执行文件路径；找不到返回 ``None``（不是抛错 —— 缺二进制是**预期情况**，
        比如用户从没装过，此时应该静默降级 LANCZOS）。
    """
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        log.warning("设置里指定的 Upscayl 路径不存在：%s", p)

    base = PACKAGE_ROOT / DEFAULT_REL
    for name in ("upscayl-bin.exe", "upscayl-bin"):
        cand = base / name
        if cand.is_file():
            return cand
    return None


def resolve_models_dir(binary: Path | None = None) -> Path | None:
    """模型目录（与二进制同级的 `models/`）。"""
    if binary is not None:
        cand = binary.parent / "models"
        if cand.is_dir():
            return cand
    cand = PACKAGE_ROOT / DEFAULT_REL / "models"
    return cand if cand.is_dir() else None


class UpscaylEngine:
    """一次性的超分执行器。

    ``runner`` 可注入，测试时传假函数即可，**不必真调 CLI**。
    """

    def __init__(
        self,
        binary: str = "",
        models_dir: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self._binary_override = binary
        self._models_override = models_dir
        self.timeout = timeout
        self._runner = runner or subprocess.run
        self._binary_cache: Path | None | bool = False   # False = 还没探测
        self._models_cache: Path | None | bool = False

    # ---------------------------------------------------------------- 探测
    @property
    def binary(self) -> Path | None:
        """二进制路径（探测结果缓存 —— 避免每次 subprocess 都碰磁盘）。"""
        if self._binary_cache is False:
            self._binary_cache = resolve_binary(self._binary_override)
        return self._binary_cache  # type: ignore[return-value]

    @property
    def models_dir(self) -> Path | None:
        if self._models_cache is False:
            self._models_cache = resolve_models_dir(self.binary)
        return self._models_cache  # type: ignore[return-value]

    @property
    def available(self) -> bool:
        """二进制与模型目录都在，才认为可用。"""
        return self.binary is not None and self.models_dir is not None

    def unavailable_reason(self) -> str:
        if self.binary is None:
            return f"未找到 Upscayl 可执行文件（期望位置：{PACKAGE_ROOT / DEFAULT_REL}）"
        if self.models_dir is None:
            return f"未找到模型目录（期望与可执行文件同级：{self.binary.parent / 'models'}）"
        return ""

    # ---------------------------------------------------------------- 命令行
    def _build_cmd(
        self,
        src: Path,
        dst: Path,
        model: str,
        scale: int,
        tile: int,
    ) -> list[str]:
        """构造 CLI 参数。

        ⚠️ 这里是「tile 永不为 0」这条硬规则的**唯一落点** ——
           调用方传 0 / 负数 / 过小值都会在这里被纠正，而不是抛错。
           因为 `-t 0` 的输出是**尺寸正常但全黑**的图，比报错更危险：
           报错会被发现，全黑图可能一路流到印刷环节。
        """
        if self.binary is None:
            raise UpscaylError(self.unavailable_reason())
        if self.models_dir is None:
            raise UpscaylError(self.unavailable_reason())

        if model not in UPSCAYL_MODELS:
            raise UpscaylError(f"未知模型：{model}（可选：{', '.join(sorted(UPSCAYL_MODELS))}）")
        if scale not in ALLOWED_SCALES:
            raise UpscaylError(f"倍数只支持 {ALLOWED_SCALES}，收到 {scale}")

        safe_tile = int(tile or 0)
        if safe_tile < MIN_TILE:
            log.warning("tile=%s 不安全（-t 0 会输出全黑），已强制改为 %s", tile, DEFAULT_TILE)
            safe_tile = DEFAULT_TILE

        return [
            str(self.binary),
            "-i", str(src),
            "-o", str(dst),
            "-n", model,
            "-s", str(scale),
            "-t", str(safe_tile),
            "-m", str(self.models_dir),
        ]

    # ---------------------------------------------------------------- 执行
    def upscale_image(
        self,
        src: Path,
        dst: Path,
        model: str,
        scale: int,
        tile: int = DEFAULT_TILE,
    ) -> Path | None:
        """跑一次超分。

        Returns:
            成功返回 ``dst``；失败返回 ``None``（已记 warning）。
        """
        if not Path(src).is_file():
            log.warning("Upscayl 输入不存在：%s", src)
            return None

        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)

        try:
            cmd = self._build_cmd(Path(src), dst, model, scale, tile)
        except UpscaylError as exc:
            log.warning("Upscayl 参数不合法，跳过：%s", exc)
            return None

        for attempt, use_tile in enumerate((tile, DEFAULT_TILE), start=1):
            if attempt == 2 and int(tile or 0) <= DEFAULT_TILE:
                break                      # 本来就用的小 tile，重试没意义
            try:
                cmd = self._build_cmd(Path(src), dst, model, scale, use_tile)
            except UpscaylError:
                return None

            t0 = time.monotonic()
            try:
                proc = self._runner(
                    cmd,
                    capture_output=True,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired:
                log.warning("Upscayl 超时（%.0fs，tile=%s），放弃", self.timeout, use_tile)
                return None
            except (OSError, ValueError) as exc:
                log.warning("Upscayl 无法执行：%s: %s", type(exc).__name__, exc)
                return None

            elapsed = time.monotonic() - t0
            code = getattr(proc, "returncode", 1)
            if code == 0 and dst.is_file() and dst.stat().st_size > 0:
                log.info("Upscayl 完成：%s ×%s tile=%s（%.1fs）",
                         model, scale, use_tile, elapsed)
                return dst

            blob = (getattr(proc, "stderr", b"") or b"")
            text = blob.decode("utf-8", errors="replace") if isinstance(blob, bytes) else str(blob)
            log.warning("Upscayl 失败（退出码 %s，tile=%s）：%s",
                        code, use_tile, text.strip()[-300:])

        return None

    def upscale_to_target(
        self,
        src: Path,
        dst: Path,
        target_px: int,
        model: str,
        tile: int = DEFAULT_TILE,
    ) -> Path | None:
        """放大到「至少 target_px」，再由调用方下采样到精确尺寸。

        ⚠️ 为什么先超到更大再缩回来：Upscayl 只有 2/3/4 倍三种整数倍率，
           而目标尺寸（如 7087）不是整数倍。先超到 ≥ 目标（8192），
           再用 LANCZOS 缩到精确值，比直接 LANCZOS 从 2048 拉伸清晰得多
           —— 细节是超分模型补的，缩回来只是定尺寸。
        """
        if not Path(src).is_file():
            return None
        from PIL import Image

        try:
            with Image.open(src) as im:
                cur = max(im.size)
        except OSError as exc:
            log.warning("Upscayl 读不到输入尺寸：%s: %s", type(exc).__name__, exc)
            return None

        if cur <= 0:
            return None

        info = get_model(model)
        scales: Sequence[int] = info.scales if info else ALLOWED_SCALES
        need = target_px / cur
        pick = next((s for s in sorted(scales) if s >= need), None)
        if pick is None:
            pick = max(scales)
            log.info("Upscayl 最大倍数 %s× 仍不足以达到 %s px（当前 %s），尽力而为",
                     pick, target_px, cur)

        return self.upscale_image(src, dst, model, pick, tile)

    # ---------------------------------------------------------------- 自检
    def selftest(self) -> tuple[bool, str]:
        """给设置页「测试」按钮用：真跑一次小图，本地免费。

        Returns:
            ``(是否可用, 说明文字)``
        """
        if not self.available:
            return False, self.unavailable_reason()

        from PIL import Image

        # ⚠️ 用**默认模型**自检，不要用清单里的第一个 ——
        #    清单第一项是 realesr-animevideov3-x2，实测它会输出平铺伪影。
        #    拿一个坏模型做「可用性自检」会得出误导性的结论。
        from .models import DEFAULT_MODEL_GENERATED

        model = (DEFAULT_MODEL_GENERATED if DEFAULT_MODEL_GENERATED in UPSCAYL_MODELS
                 else next(iter(UPSCAYL_MODELS)))
        tmp_dir = PACKAGE_ROOT / "output" / "_upscayl_selftest"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        src = tmp_dir / "in.png"
        dst = tmp_dir / "out.png"
        try:
            Image.new("RGB", (64, 64), (128, 128, 128)).save(src)
        except OSError as exc:
            return False, f"无法生成测试图：{type(exc).__name__}"

        if dst.exists():
            dst.unlink(missing_ok=True)
        info = get_model(model)
        scale = info.scales[0] if info else 2
        got = self.upscale_image(src, dst, model, scale, DEFAULT_TILE)
        if got is None:
            return False, "CLI 执行失败（可能是缺少 Vulkan 驱动或显存不足），详见日志"

        try:
            with Image.open(got) as im:
                w, h = im.size
        except OSError:
            return False, "输出不是有效图片"
        return True, f"可用：{model} ×{scale} 测试 64×64 → {w}×{h}"
