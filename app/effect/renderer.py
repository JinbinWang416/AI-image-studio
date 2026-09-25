# -*- coding: utf-8 -*-
"""把贴纸成品合成到门店玻璃上的本地效果图 —— **四层合成架构**。

## 为什么是四层

2026-09-20 对 1688 电商主图（20+ 张实拍主图逐张目视）的调研结论：

> **自然感不来自贴纸抠得干净，而来自玻璃反光层与景深分层。**
> 看起来自然的样本都有玻璃反射或明显景深结构；
> 看起来「贴上去」的样本，都是「背景模糊 + 贴纸最顶层」的两层结构。

因此合成栈从 2 层改为 4 层 —— 关键是**贴纸夹在背景与反射层之间**，
而不是盖在最上面，这样玻璃反光才能真正压在贴纸上：

    ① 背景层     景深模糊 + 色温基调
    ② 贴纸层     亮度归一化 + 轻微透光 + 边缘羽化
    ③ 玻璃反射层  柔和高光（压在贴纸上）
    ④ 前景层     暗角 + 色调统一 + 拍摄颗粒

## 构图参数（对齐竞品实测）

| 参数 | 竞品实测 | 本模块取值 |
|------|----------|-----------|
| 贴纸占玻璃宽度 | 55%~85%，中位数 ≈70% | 70% |
| 贴纸占玻璃高度 | 45%~75% | 60% |
| 垂直中心位置 | 画面高度 45%~50% | 47% |
| 玻璃四周留白 | 10%~20% | 15% |
| 背景模糊半径 | 画面宽 1.5%~3% | 2% |
| 室内色温 | 3200~3800K 暖光 | 3500K 等效 |

## 不可破坏的约束

生成图始终是唯一前景素材，中文文字与图案**不会被任何模型重绘**；
没有上传实拍照片时，只能输出并明确标记 ``simulated_storefront``，
不得描述为真实门店实拍。
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageStat

EFFECT_RENDERER_VERSION = "four-layer-glass-v4"

# ---------------------------------------------------------------- 可调参数
# 所有参数都可以在网页「设置 → 效果图微调」里调整；下面是经过实测的默认值。
# 取值区间见 PARAM_SPEC（供前端渲染滑块）。


@dataclass
class EffectParams:
    """效果图合成参数。

    分成四组，对应渲染的四层结构；每组都能单独调节并即时预览。
    """

    # ---- 构图 ----
    sticker_width_ratio: float = 0.70    # 贴纸宽度 / 玻璃宽度（竞品中位数 ≈70%）
    sticker_height_ratio: float = 0.60   # 贴纸高度 / 玻璃高度
    vertical_center: float = 0.47        # 贴纸中心落在玻璃区高度的比例
    glass_margin: float = 0.15           # 模拟背景下玻璃区四周留白
    perspective: float = 0.0             # 透视剪切强度（0 = 关闭，真实拍摄有轻微透视）

    # ---- 背景层 ----
    background_blur_ratio: float = 0.003  # 背景模糊半径 / 画面宽（景深）
    # 暖光叠加强度（**基准值**，实际会随贴纸色系 ×0.75~1.25，见 _estimate_color_profile）
    # 基准取 0.16：暖色贴纸（餐饮类）→ 实际 0.20；冷色贴纸（如蓝色房屋中介）→ 实际 0.12
    warm_strength: float = 0.16

    # ---- 贴纸层 ----
    brightness_match: float = 0.82        # 亮度向环境归一化的强度
    transmission: float = 0.07            # 透光率（背景透出比例）
    feather_ratio: float = 0.0018         # 边缘羽化半径 / 画面宽
    # 贴纸整体柔化：真实拍摄中贴纸边缘能量实测 31~44，而纯矢量合成可达 87，
    # 适度柔化更接近实拍观感（0 = 完全不柔化，保持像素级锐利）
    # 贴纸整体柔化
    # ⚠️ 方向修正（2026-09-20）：此前按"真实照片整体清晰度低"把 softness 提到 0.16，
    #    但拼多多「东东窗花店」8 张商品效果图实测：贴纸中心清晰度高达 67.1
    #    （印刷品近距离拍摄，本来就锐利）。0.16 会让贴纸发糊，故回调到 0.05。
    softness: float = 0.05

    # ---- 光学层 ----
    # 玻璃反光强度。
    # ⚠️ 2026-09-20 方向修正：拼多多「东东窗花店」8 张商品效果图显示，
    #    真实玻璃反光是**环境倒影**（树枝、招牌、灯串的具体影像），
    #    而不是程式化的白色斜带；且我们的 AI 背景本身已带反光与倒影。
    #    因此把程式化反光带压到很低（只留极轻的装饰性高光），
    #    避免"画上去的斜带"暴露合成痕迹。
    reflection_strength: float = 0.06
    vignette_strength: float = 0.06       # 暗角强度
    grain_level: int = 5                  # 拍摄颗粒强度 0~10

    # ---- 输出调色（依据真实产品安装照校准，2026-09-20）----
    # 实测：真实产品照 亮度≈85 / 对比≈64 / 饱和≈110；
    #       AI 背景本身色彩正确（暖度 1.67、饱和 110），
    #       但合成后亮度被贴纸抬到 139、饱和度被暖光层压到 92 —— 故在此补偿。
    # ---- 输出调色（依据真实产品安装照 + 拼多多商品图校准，2026-09-20）----
    #
    # 实测参照：
    #   用户真实产品照  亮度 85.1 / 对比 64.4 / 暖度 1.63 / 饱和 110.5
    #   拼多多商品效果图 亮度 77.0 / 对比 56.4 / 暖度 2.40 / 饱和 152.8
    #   AI 背景（白天版） 亮度 ≈130 → 合成后会被贴纸抬到 139（过亮）
    #   AI 背景（夜景版） 亮度 ≈63  → 若再乘 0.70 会压到 44（过暗）
    #
    # 因此曝光不再用固定倍率，而是 **按背景亮度自动归一**（见 _auto_exposure），
    # 这里只保留用户的手动微调倍率（1.0 = 完全交给自动曝光）。
    exposure: float = 1.00                # 手动曝光微调倍率
    contrast: float = 1.50                # 对比度倍率
    # ⚠️ 提高对比度会连带拉高饱和度（实测 1.42 对比度使饱和从 117 涨到 150），
    #    故此处需配合调整。目标：落在拼多多 152.8 与用户产品 110.5 之间。
    # 饱和度倍率（**基准值**，实际会随贴纸自身鲜艳度 ×0.88~1.22，见 _estimate_color_profile）
    saturation: float = 0.80

    @classmethod
    def from_dict(cls, data: dict | None) -> "EffectParams":
        """从设置字典构造，非法值自动回落到默认。"""
        if not isinstance(data, dict):
            return cls()
        out = cls()
        for key, default in out.to_dict().items():
            if key not in data:
                continue
            raw = data[key]
            try:
                value = int(raw) if isinstance(default, int) and not isinstance(default, bool) else float(raw)
            except (TypeError, ValueError):
                continue
            # 夹到合法区间
            lo, hi = PARAM_SPEC.get(key, (None, None))[0], PARAM_SPEC.get(key, (None, None))[1]
            if isinstance(default, int) and not isinstance(default, bool):
                value = int(value)
            if lo is not None:
                value = max(lo, value)
            if hi is not None:
                value = min(hi, value)
            setattr(out, key, value)
        return out

    def to_dict(self) -> dict:
        return {
            "sticker_width_ratio": self.sticker_width_ratio,
            "sticker_height_ratio": self.sticker_height_ratio,
            "vertical_center": self.vertical_center,
            "glass_margin": self.glass_margin,
            "perspective": self.perspective,
            "background_blur_ratio": self.background_blur_ratio,
            "warm_strength": self.warm_strength,
            "brightness_match": self.brightness_match,
            "transmission": self.transmission,
            "feather_ratio": self.feather_ratio,
            "softness": self.softness,
            "reflection_strength": self.reflection_strength,
            "vignette_strength": self.vignette_strength,
            "grain_level": self.grain_level,
            "exposure": self.exposure,
            "contrast": self.contrast,
            "saturation": self.saturation,
        }


# 每个参数的 (最小值, 最大值, 步长, 中文名, 所属分组, 说明)
PARAM_SPEC: dict[str, tuple] = {
    "sticker_width_ratio": (0.30, 0.98, 0.01, "贴纸宽度占比", "构图",
                            "贴纸宽度 ÷ 玻璃宽度。真实产品实测 78%（标志类）~94%（满幅类）"),
    "sticker_height_ratio": (0.25, 0.98, 0.01, "贴纸高度占比", "构图",
                             "贴纸高度 ÷ 玻璃高度。满幅类可接近 0.95"),
    "vertical_center": (0.25, 0.75, 0.01, "垂直位置", "构图",
                        "贴纸中心在玻璃区的高度比例。0.47 约等于离地 1.2~1.5m"),
    "glass_margin": (0.05, 0.30, 0.01, "玻璃留白", "构图",
                     "仅模拟背景生效：玻璃区四周留白比例"),
    "perspective": (0.0, 0.12, 0.005, "透视强度", "构图",
                    "轻微透视剪切，模拟斜拍。0 = 完全正面"),
    "background_blur_ratio": (0.0, 0.06, 0.002, "背景模糊", "背景层",
                              "景深虚化强度 ÷ 画面宽。竞品实测 1.5%~3%"),
    "warm_strength": (0.0, 0.35, 0.01, "暖光强度", "背景层",
                      "整体向 3500K 暖光靠拢的程度"),
    "brightness_match": (0.0, 1.0, 0.02, "贴纸亮度匹配", "贴纸层",
                         "把贴纸亮度向环境归一化。越高越不容易『自带发光』"),
    "transmission": (0.0, 0.25, 0.01, "贴纸透光率", "贴纸层",
                     "静电膜的透光程度，背景会略微透出"),
    "feather_ratio": (0.0, 0.006, 0.0002, "边缘羽化", "贴纸层",
                      "贴纸边缘柔化，避免数学级锐利"),
    "softness": (0.0, 1.0, 0.05, "贴纸柔化", "贴纸层",
                 "贴纸整体柔化程度（模拟拍摄失焦）。真实实拍图比纯合成更柔和"),
    "reflection_strength": (0.0, 0.45, 0.01, "玻璃反光", "光学层",
                            "玻璃环境反射强度。竞品约 60% 无此层，0 = 关闭"),
    "vignette_strength": (0.0, 0.30, 0.01, "暗角强度", "光学层",
                          "画面四周压暗，模拟手机镜头"),
    "grain_level": (0, 10, 1, "拍摄颗粒", "光学层",
                    "传感器噪点强度，增加真实感"),
    "exposure": (0.55, 1.30, 0.01, "曝光微调", "调色",
                 "手动微调倍率。实际曝光会按背景亮度**自动归一**到 ≈80，这里只做叠加微调（1.0 = 全自动）"),
    "contrast": (0.80, 1.60, 0.01, "对比度", "调色",
                 "真实产品照对比度（≈64）高于未调色的合成结果，故默认 1.12"),
    "saturation": (0.70, 1.60, 0.01, "饱和度", "调色",
                   "暖光叠加会降低饱和度，这里做补偿。真实产品照饱和≈110"),
}

DEFAULT_PARAMS = EffectParams()

# 3500K 暖光等效色（固定值，只调节强度）
WARM_TINT = (255, 208, 156)


# ---------------------------------------------------------------- 参数预设
# 依据用户提供的**真实产品安装照**测量得出（2026-09-20），
# 与竞品目测值相比：贴纸占比更大、背景更清晰、玻璃反光更明显。
PARAM_PRESETS: dict[str, dict] = {
    "logo": {
        "label": "标志类（圆形/方形标志）",
        "note": "贴纸占单扇玻璃约 78%，垂直居中略偏上。适用于门贴标志、租售牌等。",
        "params": {
            "sticker_width_ratio": 0.78,
            "sticker_height_ratio": 0.56,
            "vertical_center": 0.42,
            "background_blur_ratio": 0.010,
            "reflection_strength": 0.22,
            "warm_strength": 0.13,
            "vignette_strength": 0.10,
        },
    },
    "full": {
        "label": "满幅类（铺满整扇玻璃）",
        "note": "贴纸几乎贴满整扇玻璃（约 94%）。适用于餐饮菜单、促销海报类门贴。",
        "params": {
            "sticker_width_ratio": 0.94,
            "sticker_height_ratio": 0.92,
            "vertical_center": 0.48,
            "background_blur_ratio": 0.008,
            "reflection_strength": 0.26,
            "warm_strength": 0.10,
            "vignette_strength": 0.08,
        },
    },
}


class EffectRenderError(RuntimeError):
    """效果图本地合成失败。原始生成图不受影响。"""


@dataclass(frozen=True)
class EffectRenderResult:
    data: bytes
    source_sha256: str
    width: int
    height: int
    background: dict = field(default_factory=dict)
    realism_iteration: int = 1
    reflection_scale: float = 1.0      # 反光强度倍率（随贴纸类型联动，见 _estimate_reflection_scale）
    color_scale: tuple[float, float] = (1.0, 1.0)   # (暖光, 饱和度) 倍率，随贴纸色系联动

    def metadata(self, path: Path) -> dict:
        background = dict(self.background)
        return {
            "status": "success",
            "file_name": path.name,
            "path": str(path),
            "renderer": EFFECT_RENDERER_VERSION,
            "source_sha256": self.source_sha256,
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "width": self.width,
            "height": self.height,
            "scene": background.get("scene", "simulated_storefront"),
            "background": background,
            "realism_iteration": self.realism_iteration,
            "reflection_scale": self.reflection_scale,
            "warm_scale": self.color_scale[0],
            "saturation_scale": self.color_scale[1],
            "layers": ["background", "sticker", "glass_reflection", "foreground"],
        }


# ================================================================ 颜色工具
def _clamp(value: float) -> int:
    return max(0, min(255, int(round(value))))


def _shade(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """factor < 1 变暗，> 1 变亮。"""
    return tuple(_clamp(c * factor) for c in color)  # type: ignore[return-value]


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(_clamp(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def _stable_palette(key: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """按门店名稳定地取一组配色，保证同一门店每次渲染一致。"""
    palettes = (
        ((26, 46, 58), (196, 214, 210)),
        ((44, 40, 36), (214, 198, 172)),
        ((30, 52, 46), (186, 212, 190)),
        ((38, 36, 52), (206, 198, 224)),
        ((48, 42, 34), (222, 206, 176)),
    )
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return palettes[int(digest[:2], 16) % len(palettes)]


def _stable_randoms(key: str, count: int, low: float, high: float) -> list[float]:
    """由 key 派生一组稳定的伪随机数（避免每次渲染结果抖动）。"""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    out: list[float] = []
    for i in range(count):
        byte = digest[(i * 3) % len(digest)]
        byte2 = digest[(i * 3 + 1) % len(digest)]
        ratio = ((byte << 8) | byte2) / 65535.0
        out.append(low + ratio * (high - low))
    return out


# ================================================================ 层 ① 背景
def _draw_simulated_storefront(canvas: Image.Image, name: str,
                               params: EffectParams) -> tuple[int, int, int, int]:
    """绘制**三层景深**的模拟门店橱窗，返回玻璃区域 (x0, y0, x1, y1)。

    结构模仿真实手机实拍：
      远景 —— 店内环境（强模糊：货架横条、暖色灯斑、地面反光）
      中景 —— 玻璃门（半透明压暗 + 清晰门框 + 门把手）
      前景 —— 地面与轻微暗角（由 _foreground_layer 完成）

    刻意**不画**任何文字、Logo 或品牌元素。
    """
    width, height = canvas.size
    dark, light = _stable_palette(name)
    rnd = _stable_randoms(name, 24, 0.0, 1.0)

    # ---------------- 远景：店内环境 ----------------
    far = Image.new("RGBA", (width, height), (*_shade(dark, 0.85), 255))
    fd = ImageDraw.Draw(far, "RGBA")

    # 天花板与地面分区
    fd.rectangle((0, 0, width, int(height * 0.10)), fill=(*_shade(dark, 0.62), 255))
    fd.rectangle((0, int(height * 0.78), width, height), fill=(*_shade(dark, 1.35), 255))

    # 货架 / 柜体的横向层次（用亮暗交替暗示纵深）
    for i in range(5):
        y = int(height * (0.16 + i * 0.135))
        band_h = int(height * (0.030 + rnd[i] * 0.030))
        tone = _shade(light, 0.42 + rnd[i + 5] * 0.35)
        fd.rectangle(
            (int(width * (0.02 + rnd[i] * 0.05)), y,
             int(width * (0.80 + rnd[i] * 0.17)), y + band_h),
            fill=(*tone, 205),
        )

    # 暖色灯斑（bokeh）—— 真实店内照明的散景
    for i in range(9):
        cx = int(width * (0.10 + rnd[i] * 0.80))
        cy = int(height * (0.12 + rnd[i + 9] * 0.42))
        radius = int(width * (0.045 + rnd[i + 3] * 0.075))
        alpha = int(70 + rnd[i + 12] * 95)
        tone = _mix((255, 214, 150), light, rnd[i + 15] * 0.35)
        fd.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                   fill=(*tone, alpha))

    # 地板反光
    fd.rectangle((0, int(height * 0.80), width, height), fill=(*_shade(light, 0.52), 150))

    # 远景强模糊 —— 这是「景深」的第一层
    far = far.filter(ImageFilter.GaussianBlur(max(6, int(width * 0.026))))
    canvas.alpha_composite(far)

    # ---------------- 中景：玻璃 + 门框 ----------------
    gx0 = int(width * params.glass_margin)
    gy0 = int(height * 0.13)
    gx1 = int(width * (1 - params.glass_margin))
    gy1 = int(height * 0.90)

    # 玻璃本身：把透视过来的店内环境压暗（玻璃吸光）
    glass_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(glass_layer, "RGBA").rectangle(
        (gx0, gy0, gx1, gy1), fill=(*_shade(dark, 0.55), 96)
    )
    canvas.alpha_composite(glass_layer)

    # 门框：清晰的深色框条（与外景形成清晰/模糊对比）
    frame = max(6, int(width * 0.014))
    draw = ImageDraw.Draw(canvas, "RGBA")
    frame_color = (*_shade(dark, 0.45), 255)
    draw.rectangle((gx0 - frame, gy0 - frame, gx1 + frame, gy1 + frame),
                   outline=frame_color, width=frame)
    # 竖梃（中挺）—— 真实玻璃门常见，为贴纸定位提供参照
    mullion_x = gx0 + int((gx1 - gx0) * 0.5)
    if width >= 720:
        pass  # 单扇门方案：不加竖梃，避免贴纸被迫跨框（竞品约束）
    # 门把手：金属质感暗示
    handle_x = gx1 - int(width * 0.045)
    handle_top = gy0 + int((gy1 - gy0) * 0.42)
    handle_bottom = handle_top + int((gy1 - gy0) * 0.16)
    draw.rounded_rectangle(
        (handle_x, handle_top, handle_x + max(3, width // 150), handle_bottom),
        radius=max(2, width // 300), fill=(*_mix(light, (255, 255, 255), 0.35), 210),
    )

    return gx0, gy0, gx1, gy1


def _load_background(background: bytes | Path | str, width: int, height: int) -> Image.Image:
    """加载用户上传的实拍门店玻璃照片，裁切到画布尺寸。"""
    raw = Path(background).read_bytes() if isinstance(background, (Path, str)) else background
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        fitted = ImageOps.fit(
            image.convert("RGB"), (width, height), Image.Resampling.LANCZOS, centering=(0.5, 0.5)
        )
    fitted = ImageEnhance.Color(fitted).enhance(0.98)
    fitted = ImageEnhance.Contrast(fitted).enhance(1.02)
    return fitted.convert("RGBA")


def _apply_depth_blur(canvas: Image.Image, glass: tuple[int, int, int, int],
                      params: EffectParams) -> None:
    """对背景施加景深模糊：玻璃区保留细节，四周更强模糊。

    竞品实测：背景整体统一高斯模糊，半径约画面宽 1.5%~3%。
    实拍照片另有景深梯度，这里用「整体模糊 + 玻璃区回贴」模拟。
    """
    width, height = canvas.size
    if params.background_blur_ratio <= 0:
        return
    # 注意：不要用 max(2, ...) 这类硬下限 —— 实测它会让背景清晰度卡在 14，
    # 明显低于实拍区间（小红书样本中位数 24）。
    radius = int(width * params.background_blur_ratio)
    if radius < 1:
        return
    blurred = canvas.filter(ImageFilter.GaussianBlur(radius))
    canvas.paste(blurred, (0, 0))

    # 玻璃区域内保留相对清晰（贴纸所在的焦平面）
    gx0, gy0, gx1, gy1 = glass
    sharp = canvas.crop((gx0, gy0, gx1, gy1))
    sharp = sharp.filter(ImageFilter.GaussianBlur(max(0.5, radius * 0.35)))
    mask = Image.new("L", sharp.size, 0)
    ImageDraw.Draw(mask).rectangle((0, 0, sharp.width, sharp.height), fill=200)
    mask = mask.filter(ImageFilter.GaussianBlur(max(3, radius * 3)))
    canvas.paste(sharp, (gx0, gy0), mask)


# ================================================================ 层 ② 贴纸
def _remove_outer_white(sticker: Image.Image) -> Image.Image:
    """仅把与边缘连通的近白底变透明，保留贴纸内部白色文字与高光。"""
    image = sticker.convert("RGBA")
    width, height = image.size
    pixels = image.load()
    visited: set[tuple[int, int]] = set()
    stack: list[tuple[int, int]] = []

    def near_white(x: int, y: int) -> bool:
        r, g, b, a = pixels[x, y]
        return a > 0 and min(r, g, b) >= 235 and max(r, g, b) - min(r, g, b) <= 24

    for x in range(width):
        stack.extend(((x, 0), (x, height - 1)))
    for y in range(height):
        stack.extend(((0, y), (width - 1, y)))
    while stack:
        x, y = stack.pop()
        if (x, y) in visited or not (0 <= x < width and 0 <= y < height) or not near_white(x, y):
            continue
        visited.add((x, y))
        stack.extend(((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)))

    for x, y in visited:
        r, g, b, _ = pixels[x, y]
        pixels[x, y] = (r, g, b, 0)
    return image


def _feather_alpha(sticker: Image.Image, radius: float) -> Image.Image:
    """边缘羽化 0.5~2px：真实贴纸边缘不是数学级锐利的矢量边。"""
    if radius <= 0:
        return sticker
    alpha = sticker.getchannel("A")
    feathered = alpha.filter(ImageFilter.GaussianBlur(radius))
    out = sticker.copy()
    out.putalpha(feathered)
    return out


def _match_brightness(sticker: Image.Image, canvas: Image.Image,
                      box: tuple[int, int, int, int], params: EffectParams) -> Image.Image:
    """把贴纸亮度向环境光归一化。

    竞品破绽 #2：合成图的贴纸亮度往往高于环境，像自带背光 ——
    这是「贴上去」感最直接的来源。做法是按背景平均亮度把贴纸压到接近环境。
    """
    region = canvas.crop(box).convert("L")
    if region.width < 4 or region.height < 4 or params.brightness_match <= 0:
        return sticker
    env_mean = ImageStat.Stat(region).mean[0]

    sticker_gray = sticker.convert("L")
    sticker_mean = ImageStat.Stat(sticker_gray).mean[0]
    if sticker_mean <= 1:
        return sticker

    # 目标：贴纸平均亮度向环境亮度靠拢（保留一定对比，不完全压平）
    target = env_mean * (1.0 + (1.0 - params.brightness_match) * 0.9)
    factor = target / sticker_mean
    factor = max(0.55, min(1.25, factor))          # 夹住，避免过曝或压死
    factor = 1.0 - (1.0 - factor) * params.brightness_match

    rgb = sticker.convert("RGB")
    adjusted = ImageEnhance.Brightness(rgb).enhance(factor)
    out = adjusted.convert("RGBA")
    out.putalpha(sticker.getchannel("A"))
    return out


def _apply_transmission(sticker: Image.Image, canvas: Image.Image,
                        position: tuple[int, int], params: EffectParams) -> Image.Image:
    """轻微透光：静电膜不是完全不透明的，背景亮度会透出一点点。"""
    if params.transmission <= 0:
        return sticker
    x, y = position
    region = canvas.crop((x, y, x + sticker.width, y + sticker.height)).convert("RGB")
    base = sticker.convert("RGB")
    blended = Image.blend(base, region, params.transmission)
    out = blended.convert("RGBA")
    out.putalpha(sticker.getchannel("A"))
    return out


# ================================================================ 层 ③ 反射
def _estimate_reflection_scale(sticker: Image.Image) -> float:
    """依据贴纸自身的「视觉重量」估计玻璃反光强度倍率。

    依据 2026-09-20 小红书 20 张门店实拍样本的观察：

    · 深色大色块 / 卡通满幅贴  → 玻璃反光**最强**（样本中可达"几乎镜像"级别）
    · 白色细线条贴            → 玻璃反光**最弱**
    · 磨砂材质                → **几乎不产生镜面反光**

    因此反光强度不应是常数，而应与贴纸的覆盖率、明暗联动。
    实测区间：细线条浅色贴约 0.6，满幅深色贴约 1.4。
    """
    try:
        alpha = sticker.getchannel("A")
        coverage = ImageStat.Stat(alpha).mean[0] / 255.0        # 0~1，不透明像素占比
        if coverage <= 0.01:
            return 1.0
        gray = sticker.convert("RGB").convert("L")
        # 用 alpha 作掩码，只统计贴纸本体亮度（忽略透明区）
        mean_lum = ImageStat.Stat(gray, alpha).mean[0]
        darkness = 1.0 - (mean_lum / 255.0)                     # 0~1，越大越暗
        scale = 0.45 + 0.75 * coverage + 0.45 * darkness
        return max(0.45, min(1.60, round(scale, 3)))
    except Exception:  # noqa: BLE001
        return 1.0


def _reflection_layer(canvas: Image.Image, glass: tuple[int, int, int, int],
                      has_real_photo: bool, params: EffectParams,
                      scale: float = 1.0) -> Image.Image:
    """玻璃反射层：柔和的环境反射 + 线性高光，压在贴纸之上。

    竞品实测：约 60% 的合成图**完全没有反光**（这是最大破绽），
    所以反光不是必需项；但「看起来自然」的样本大多有。
    这里做成**柔和、低透明度**的层，避免生硬白带。设为 0 即完全关闭。
    """
    width, height = canvas.size
    gx0, gy0, gx1, gy1 = glass
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if params.reflection_strength <= 0:
        return layer
    draw = ImageDraw.Draw(layer, "RGBA")

    strength = params.reflection_strength * scale * (1.0 if has_real_photo else 0.85)

    # 主线：从左上到右下的柔和光带（真实玻璃反光常见形态）
    band_w = int(width * 0.30)
    pts = [
        (gx0 - band_w, gy0),
        (gx0 + int(width * 0.22), gy0),
        (gx0 + int(width * 0.62), gy1),
        (gx0 + int(width * 0.30), gy1),
    ]
    draw.polygon(pts, fill=(232, 244, 250, int(255 * strength * 0.42)))

    # 副带：更细、更淡
    pts2 = [
        (gx0 + int(width * 0.44), gy0),
        (gx0 + int(width * 0.56), gy0),
        (gx0 + int(width * 0.88), gy1),
        (gx0 + int(width * 0.78), gy1),
    ]
    draw.polygon(pts2, fill=(240, 248, 252, int(255 * strength * 0.26)))

    # 顶部边缘的一条极窄高光（玻璃与框的接触高光）
    draw.line((gx0, gy0 + 1, gx1, gy0 + 1),
              fill=(255, 255, 255, int(255 * strength * 0.34)),
              width=max(1, width // 400))

    return layer.filter(ImageFilter.GaussianBlur(max(3, int(width * 0.012))))


# ================================================================ 层 ④ 前景
# 输出亮度的目标值：用户真实产品照 85.1、拼多多商品图 77.0，取其中值。
TARGET_LUMA = 80.0
# 暗角上限：拼多多 8 张商品图的四角亮度在 34~88（均值 56），
# 而我们早期版本四角仅 12.8 —— 过强的暗角会让画面"发闷"。
VIGNETTE_FLOOR = 20.0


def _auto_exposure(canvas: Image.Image, params: EffectParams) -> float:
    """按背景自身亮度自动求出曝光系数，使输出落回真实拍摄区间。

    为什么需要：AI 背景的明暗差异极大（白天版 ≈130、夜景版 ≈63），
    固定倍率必然顾此失彼 —— 白天版压不到、夜景版压过头。
    用户上传的实拍照片亮度天然正确，此时自适应系数会接近 1，不影响结果。

    Returns:
        最终曝光倍率（已与用户的手动 ``exposure`` 相乘并夹在安全区间内）
    """
    try:
        mean = ImageStat.Stat(canvas.convert("L")).mean[0]
    except Exception:  # noqa: BLE001
        mean = 0.0
    if mean <= 1.0:
        return params.exposure
    auto = TARGET_LUMA / mean
    # 夹住自适应倍率，避免把极端亮/暗的背景硬拉（那样会丢层次）
    factor = params.exposure * max(0.55, min(1.45, auto))
    return max(0.45, min(1.60, factor))


def _estimate_color_profile(sticker: Image.Image) -> tuple[float, float]:
    """依据贴纸自身的色系，估计「暖光」与「饱和度」的适配倍率。

    依据 2026-09-20 拼多多「东东窗花店」8 张商品效果图实测：
      · 色系统计中**暖红/橙/黄出现 36 次，零蓝色系**
      · 整图暖度 R/B ≈ 2.40、饱和度 ≈ 152.8（明显高于通用水平）
    原因是餐饮类贴纸本身是暖色，整张效果图随之呈现高暖高饱和。

    但冷色系贴纸（如蓝色调的房屋中介、绿色调的维修类）若强行套用同样暖度会**偏色**，
    故让这两项随贴纸主色联动，而不是全局写死。

    Returns:
        ``(warm_scale, saturation_scale)`` —— 暖色贴纸偏高，冷色贴纸偏低。
    """
    try:
        alpha = sticker.getchannel("A")
        if ImageStat.Stat(alpha).mean[0] / 255.0 <= 0.01:
            return 1.0, 1.0
        hsv = sticker.convert("RGB").convert("HSV")
        hue = ImageStat.Stat(hsv.getchannel("H"), alpha).mean[0] / 255.0 * 360.0
        sat = ImageStat.Stat(hsv.getchannel("S"), alpha).mean[0] / 255.0

        # 色系判定：暖 330~360/0~70 · 绿 70~170 · 冷 170~330
        if hue >= 330.0 or hue <= 70.0:
            warmth = 1.0
        elif hue <= 170.0:
            warmth = 0.35
        else:
            warmth = 0.0

        warm_scale = 0.75 + 0.50 * warmth          # 0.75 ~ 1.25
        sat_scale = 0.88 + 0.34 * sat              # 0.88 ~ 1.22（本身越艳，越允许提饱和）
        return round(warm_scale, 3), round(sat_scale, 3)
    except Exception:  # noqa: BLE001
        return 1.0, 1.0


def _foreground_layer(canvas: Image.Image, level: int, params: EffectParams,
                      color_scale: tuple[float, float] = (1.0, 1.0)) -> None:
    """前景层：色调统一（暖光）→ 输出调色 → 拍摄颗粒 → 暗角。

    调色顺序刻意如此：暖光叠加会拉低饱和度，所以先补饱和度，再提对比度，
    最后调整曝光 —— 这样三个参数互不干扰，实测结果最接近真实产品安装照。
    """
    width, height = canvas.size
    warm_scale, sat_scale = color_scale

    # --- 色调统一：整体向 3500K 暖光靠拢（强度随贴纸色系联动）---
    effective_warm = max(0.0, min(0.45, params.warm_strength * warm_scale))
    if effective_warm > 0:
        warm = Image.new(
            "RGBA", (width, height), (*WARM_TINT, int(255 * effective_warm))
        )
        canvas.alpha_composite(warm)

    # --- 输出调色：饱和度 → 对比度 → 自动曝光 ---
    effective_sat = max(0.5, min(1.6, params.saturation * sat_scale))
    rgb = canvas.convert("RGB")
    if abs(effective_sat - 1.0) > 1e-6:
        rgb = ImageEnhance.Color(rgb).enhance(effective_sat)
    if abs(params.contrast - 1.0) > 1e-6:
        rgb = ImageEnhance.Contrast(rgb).enhance(params.contrast)
    canvas.paste(rgb, (0, 0))

    # 曝光放在最后、按**当前画面**亮度自动归一 ——
    # 这样无论 AI 背景是白天版还是夜景版、或用户上传的实拍照片，
    # 输出亮度都会落回真实拍摄区间（≈80），不需要为每种背景手调。
    factor = _auto_exposure(canvas, params)
    if abs(factor - 1.0) > 1e-6:
        canvas.paste(
            ImageEnhance.Brightness(canvas.convert("RGB")).enhance(factor), (0, 0)
        )

    # --- 拍摄颗粒 ---
    if params.grain_level > 0:
        noise = Image.effect_noise((width, height), 5 + level * 2).convert("L")
        grain = Image.new("RGBA", (width, height), (255, 255, 255, 0))
        cap = 10 + level * 2 + params.grain_level
        grain.putalpha(noise.point(lambda v: max(0, min(cap, (v - 120) // 8))))
        canvas.alpha_composite(grain)

    # --- 暗角（带亮度保护）---
    strength = params.vignette_strength
    if strength > 0:
        # 亮度保护：拼多多 8 张效果图的四角亮度在 34~88，
        # 若当前画面四角已经偏暗（例如夜景背景），再压暗角会让画面发闷，
        # 故按四角亮度比例收敛暗角强度。
        probe = canvas.convert("L").crop((0, 0, max(2, width // 8), max(2, height // 8)))
        corner = ImageStat.Stat(probe).mean[0]
        if corner < VIGNETTE_FLOOR:
            strength *= max(0.0, corner / VIGNETTE_FLOOR)

    if strength > 0:
        vignette = Image.new("L", (width, height), 0)
        vdraw = ImageDraw.Draw(vignette)
        border = max(4, min(width, height) // 16)
        for step in range(border):
            alpha = int((border - step) / border * (255 * strength))
            vdraw.rectangle((step, step, width - 1 - step, height - 1 - step), outline=alpha)
        shade = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        shade.putalpha(vignette)
        canvas.alpha_composite(shade)


# ================================================================ 元数据
def _background_metadata(background: object, asset: dict | None) -> dict:
    """生成背景来源元数据。

    ⚠️ **三种来源必须严格区分，不得混淆**（AGENTS.md 约束）：
      - ``real_photo``             用户本机上传的门店玻璃实拍照片
      - ``ai_generated_background`` AI 生成的模拟门店（**不是实拍**）
      - ``simulated``               算法绘制的占位场景（**不是实拍**）
    """
    safe_asset = asset if isinstance(asset, dict) else {}
    kind = str(safe_asset.get("kind") or "")

    if background is None:
        return {
            "mode": "simulated",
            "scene": "simulated_storefront",
            "label": "模拟门店背景（未提供任何背景照片）",
            "notice": "此图为算法生成的模拟场景，不是真实门店实拍，请勿作为实拍图使用。",
        }

    common = {
        "asset_id": str(safe_asset.get("id") or ""),
        "file_name": str(safe_asset.get("file_name") or ""),
        "sha256": str(safe_asset.get("sha256") or ""),
    }

    if kind == "ai_generated_background":
        return {
            "mode": "ai_generated",
            "scene": "ai_generated_storefront",
            "label": "AI 生成的门店玻璃背景（非实拍）",
            "notice": "此背景由 AI 生成，仅用于展示安装效果，不是真实门店实拍，请勿作为实拍图使用。",
            **common,
        }

    return {
        "mode": "real_photo",
        "scene": "real_storefront_photo",
        "label": "用户上传的实拍门店玻璃背景",
        **common,
    }


# ================================================================ 主入口
def render_storefront_glass(
    source: bytes | Path | str,
    store_name: str = "",
    *,
    background: bytes | Path | str | None = None,
    background_asset: dict | None = None,
    realism_iteration: int = 1,
    glass_region: tuple[float, float, float, float] | None = None,
    params: "EffectParams | None" = None,
) -> EffectRenderResult:
    """将成品贴纸按四层架构合成到真实或模拟门店玻璃，输出与原图同尺寸 PNG。

    Args:
        source: 生成图（PNG 字节或路径）—— 唯一前景素材，不会被重绘
        store_name: 门店名，用于稳定配色
        background: 用户上传或 AI 生成的门店玻璃背景；为空则使用模拟背景
        background_asset: 背景资产元信息（用于 manifest 追溯）
        realism_iteration: 真实感等级 V1~V3
        glass_region: 玻璃区域，归一化坐标 ``(x0, y0, x1, y1)``（0~1）。
            **竞品约束要求贴纸绝不跨过门框/竖梃**，所以双开门照片必须指定单扇玻璃。
            为空时按画面 10%~90% 估算。
    """
    # ---------------- 读取生成图 ----------------
    try:
        raw = Path(source).read_bytes() if isinstance(source, (Path, str)) else source
        source_sha256 = hashlib.sha256(raw).hexdigest()
        with Image.open(io.BytesIO(raw)) as source_image:
            source_image.load()
            original = source_image.convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        raise EffectRenderError(f"无法读取生成图：{exc}") from exc

    width, height = original.size
    if width < 32 or height < 32:
        raise EffectRenderError("生成图尺寸过小，无法合成门店效果图")

    level = max(1, min(3, int(realism_iteration or 1)))
    metadata = _background_metadata(background, background_asset)
    has_real_photo = background is not None
    p = params or DEFAULT_PARAMS

    # ---------------- 层 ① 背景 ----------------
    try:
        if not has_real_photo:
            canvas = Image.new("RGBA", (width, height), (214, 216, 214, 255))
            glass = _draw_simulated_storefront(canvas, store_name, p)
        else:
            canvas = _load_background(background, width, height)  # type: ignore[arg-type]
            if glass_region:
                rx0, ry0, rx1, ry1 = glass_region
            else:
                rx0, ry0, rx1, ry1 = 0.10, 0.14, 0.90, 0.90
            glass = (
                int(width * max(0.0, min(1.0, rx0))),
                int(height * max(0.0, min(1.0, ry0))),
                int(width * max(0.0, min(1.0, rx1))),
                int(height * max(0.0, min(1.0, ry1))),
            )
            if glass[2] - glass[0] < 32 or glass[3] - glass[1] < 32:
                raise EffectRenderError("指定的玻璃区域过小，请重新标定")
    except EffectRenderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EffectRenderError(f"无法准备门店背景：{exc}") from exc

    gx0, gy0, gx1, gy1 = glass
    glass_w, glass_h = gx1 - gx0, gy1 - gy0

    # 景深模糊（背景层的关键）
    _apply_depth_blur(canvas, glass, p)

    # ---------------- 层 ② 贴纸 ----------------
    sticker = _remove_outer_white(original)
    # 反光强度与贴纸类型联动：深色满幅贴反光强、白色细线条贴反光弱
    reflection_scale = _estimate_reflection_scale(sticker)
    # 暖光与饱和度与贴纸色系联动：暖色贴纸高暖高饱和，冷色贴纸避免偏色
    color_scale = _estimate_color_profile(sticker)

    max_w = int(glass_w * p.sticker_width_ratio)
    max_h = int(glass_h * p.sticker_height_ratio)
    scale = min(max_w / sticker.width, max_h / sticker.height)
    if scale <= 0:
        raise EffectRenderError("贴纸与玻璃区域比例异常，无法合成")
    sticker = sticker.resize(
        (max(1, int(sticker.width * scale)), max(1, int(sticker.height * scale))),
        Image.Resampling.LANCZOS,
    )

    # 可选透视：模拟斜拍（0 = 完全正面）
    if p.perspective > 0:
        k = p.perspective
        sticker = sticker.transform(
            sticker.size,
            Image.Transform.AFFINE,
            (1, -k, sticker.width * k, k * 0.34, 1, 0),
            resample=Image.Resampling.BICUBIC,
        )

    # 位置：水平居中、垂直按参数（默认 0.47，竞品实测区间中值）
    x = gx0 + (glass_w - sticker.width) // 2
    y = gy0 + int(glass_h * p.vertical_center - sticker.height / 2)
    x = max(gx0, min(x, gx1 - sticker.width))
    y = max(gy0, min(y, gy1 - sticker.height))

    # 亮度归一化 + 透光 + 边缘羽化
    sticker = _match_brightness(
        sticker, canvas, (x, y, x + sticker.width, y + sticker.height), p
    )
    sticker = _apply_transmission(sticker, canvas, (x, y), p)
    sticker = _feather_alpha(sticker, max(0.0, width * p.feather_ratio))

    # 贴纸整体柔化：纯矢量合成的边缘能量远高于实拍照片，适度柔化更自然
    if p.softness > 0:
        radius = max(0.3, width * 0.005 * p.softness)
        sticker = sticker.filter(ImageFilter.GaussianBlur(radius))

    # 贴纸外侧极轻的接触阴影（让贴纸"压"在玻璃上，而不是飘着）
    contact = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    alpha = sticker.getchannel("A").filter(ImageFilter.GaussianBlur(max(2, width // 260)))
    shadow_layer = Image.new("RGBA", sticker.size, (0, 0, 0, 70))
    shadow_layer.putalpha(alpha.point(lambda v: v // 4))
    contact.alpha_composite(shadow_layer, (x + max(1, width // 260), y + max(2, height // 320)))
    canvas.alpha_composite(contact)

    # 贴纸落位（夹在背景与反射层之间 —— 四层架构的关键）
    canvas.alpha_composite(sticker, (x, y))

    # ---------------- 层 ③ 玻璃反射（压在贴纸上）----------------
    canvas.alpha_composite(
        _reflection_layer(canvas, glass, has_real_photo, p, reflection_scale)
    )

    # ---------------- 层 ④ 前景 ----------------
    _foreground_layer(canvas, level, p, color_scale)

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG", optimize=True)
    return EffectRenderResult(
        out.getvalue(), source_sha256, width, height, metadata, level,
        reflection_scale, color_scale,
    )
