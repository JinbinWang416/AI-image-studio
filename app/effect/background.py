# -*- coding: utf-8 -*-
"""AI 生成真实感门店玻璃背景。

## 为什么需要

2026-09-20 对 1688 电商主图的调研结论：效果图的自然感主要取决于
**背景是不是真实照片**。程序化绘制（PIL 画几何图形）无论怎么调，
都缺乏真实照片的信息密度。

## 约束遵守（AGENTS.md）

- 只生成**空背景**（店内环境 + 玻璃），**不含任何贴纸、文字、Logo**；
  贴纸仍由本地 `effect_renderer.py` 合成，因此不违反
  「生成图是唯一素材前景、中文文字与图案不被重绘」。
- 生成结果写入背景资产库时带 ``extra.kind = "ai_generated_background"``，
  与用户实拍（``real_photo``）**严格区分**，界面与 manifest 都不得把
  AI 背景描述为实拍。

## 关键经验（踩坑记录）

1. **背景必须与生成图同比例**：否则 ``ImageOps.fit`` 会裁掉边缘，
   导致 ``glass_region`` 标定错位（实测裁掉 33%）。
2. **模型会自动补全成双开门**：即使提示写「单开」，也常生成中间有竖框的门。
   实测最有效的描述是「**一整块落地玻璃，占画面 80%，中间无任何竖框**」。
"""
from __future__ import annotations

import base64
import io
import json
import time
from pathlib import Path
from typing import Awaitable, Callable

from ..providers import create_provider
from ..providers.base import GenerateRequest, ProviderError
from ..state.assets import EffectBackgroundStore

# 整块落地玻璃在画面中的归一化区域（留出四周门框，贴纸绝不跨框）
GLASS_REGION_SINGLE = (0.030, 0.020, 0.950, 0.930)
# 双开门时只贴左扇
GLASS_REGION_DOUBLE = (0.075, 0.045, 0.478, 0.925)

PROMPT_TEMPLATE = (
    "真实手机拍摄的门店玻璃照片，{camera}，1:1 正方形构图。"
    "画面主体是{door_desc}，玻璃通透，可以清楚地看到店内环境："
    "{interior}。"
    "{lighting}"
    "{ambience}"
    "{realism}"
    "玻璃表面保持干净通透，不要出现任何贴纸、文字、图案、标识、logo 或宣传物料。"
)

# 明暗反差与氛围（2026-09-20 修正，第 5 版）
#
# 依据用户真实产品安装照的量化实测：亮度 ≈85、对比度 ≈64、暖度 ≈1.63、饱和 ≈110。
# 后续对标拼多多「东东窗花店」8 张商品效果图（亮度 77、暖度 2.40、饱和 152.8），
# 发现其**环境更暗、店内暖光更突出**，氛围感明显更强。
#
# 迭代记录：
#   v1「均匀明亮的店内」      → 亮度 126、对比 46（反差不足，贴纸显"浮"）
#   v2「低调（low-key）影调」 → 亮度 34、对比 29（模型理解成整体很暗，丢了"店内明亮"）
#   v3 强调"玻璃暗 + 店内亮"的**层次**，并禁止整体压暗 → 对比 63，但暖度不足
#   v4 v3 + 暖黄 3200K        → 对比 63、暖度 1.73、饱和 105（三项达标）
#   v5 傍晚/夜晚            → 亮度 62~66、对比 57（好），但**四角过暗（12~15）**
#                             拼多多的四角其实是"店铺外立面"（木墙/灯笼/招牌，亮 34~88），
#                             而夜景版把角落压成近黑，画面发闷。
#   v6（本版）改为**傍晚·天未全黑**：保留暗玻璃 + 亮店内的反差，但环境仍有自然光
# 2026-10-05 改版：用户要求效果图「明亮真实」（参考明亮白天的窗户实拍）。
# 旧版刻意做「傍晚偏暗」的氛围影调（亮度 62~66），与「明亮」诉求相反，故整体改为白天明亮自然光。
LIGHTING = (
    "拍摄时间是白天上午，光线充足明亮，室外是晴朗的自然光，天空通透；"
    "玻璃通透干净、几乎不吸光，可以清楚地看到明亮的店内全貌；"
    "店内灯光明亮温暖，与室外自然光自然融合，整体明亮、通透、干净；"
    "店内景深前后都清晰，不要虚化、不要模糊；"
    "街道、树影与对面建筑在玻璃上有清晰的浅色倒影。"
    "整体明亮自然、暗部保留细节，不要发暗、不要偏黄发闷，也不要过曝死白。"
)

DOOR_STYLES = {
    "single": (
        "店面的一块完整落地玻璃（整片橱窗或单扇玻璃门），"
        "这整块玻璃占据画面约 80% 的面积，"
        "画面中间没有任何竖框、门缝、中挺或分隔条，只有四周一圈细门框；"
        "整幅画面从左侧门框到右侧门框之间必须是**一整片连续通透的玻璃**，"
        "不允许出现任何垂直的立柱、边框、分隔线或第二扇门"
    ),
    "double": "一扇双开铝合金框玻璃门，中间有一道竖框",
}

INTERIORS = {
    "房屋中介": (
        "房产中介门店的店内真实场景：浅木色或白色的接待台与办公桌椅、"
        "墙上贴满房源信息展示板与小区楼盘照片墙、"
        "接待台上摆着电脑、房源资料册与名片、"
        "角落有几盆绿植，顶部是明亮温暖的吊灯"
    ),
    "租房服务": "简约的接待区、沙发茶几、墙上的钥匙柜、暖色壁灯",
    "学生托管": "明亮的教室、课桌椅、书架、墙上的学习园地、绿植",
    "干洗护理": "整齐的挂衣架、成排的衣物、柜台、明亮的白色照明",
    "修鞋服务": "修鞋工作台、工具墙、鞋架、暖色工作灯",
    "裁缝改衣": "裁缝工作台、布料卷、缝纫机、量尺挂件、暖色台灯",
    "洗鞋护理": "清洁工作台、鞋架、洗护用品架、明亮白光",
    "电动车维修": "维修工位、工具架、待修车辆、明亮的工业照明",
    "汽车补胎": "轮胎架、维修工位、工具车、工业照明",
    "汽车换油": "养护工位、机油展示架、工具台、专业照明",
    "轮胎维修": "轮胎陈列架、拆装工位、工具墙、工业照明",
    "水电维修": "工具墙、水管电线陈列架、工作台、明亮照明",
    "电机维修": "电机设备、工作台、工具架、工业照明",
    "水泵维修": "水泵设备、管道陈列、工作台、工业照明",
    "机电维修": "机电设备、齿轮零件架、工作台、工业照明",
    "铝合金门窗": "门窗样品展示、型材架、明亮的展厅照明",
    "瓷砖建材": "瓷砖样品墙、地面铺贴展示、展厅射灯",
    "木地板销售": "木地板样品墙、铺装展示区、暖色展厅灯",
    "灯饰照明": "各款灯具展示、暖色灯光效果、家居陈设",
    "装修材料": "建材货架、样品展示、明亮的仓储式照明",
    "建筑材料": "建材堆放、水泥袋与钢材、仓库照明",
    "砂石水泥": "砂石堆、水泥袋、工程车辆、仓库照明",
    "彩钢钢构": "彩钢板材、钢结构构件、工程图纸台、工业照明",
}

DEFAULT_INTERIOR = "整洁的店内空间、货架与陈列柜、暖色照明、少量绿植"

# ================================================================ 不重样 + 真实感递增
#
# 用户要求（2026-10-05）：每次重新生成背景，都要**比之前更有真实感**，而且**不重样**。
# 参考 `app/generation/regeneration.py` 对「生成图」的成熟做法（轮次计数 +
# 变体轮转 + 感知哈希去重），这里给「背景图」实现同一套模式：
#   · 变体轮转 → 每次换机位 / 光线 / 环境细节，连续生成绝不重样
#   · 真实感阶梯 → 按该门店已生成过的背景张数逐级抬升，单调递增、到顶保持
#   · 感知哈希 → 兜底挡掉几乎相同的成图
BACKGROUND_ROUND_FILE = "effect_background_round.json"

# (机位, 光线/时段, 环境细节) —— 按轮次轮转，保证连续生成不重样
BACKGROUND_VARIATIONS: tuple[tuple[str, str, str], ...] = (
    ("正面平视视角，镜头高度约 1.5 米",
     "拍摄时间是上午，光线充足明亮，室外是晴朗的自然光",
     "门口有脚垫、门边摆着一盆绿植，街道、树影与对面建筑在玻璃上有清晰的浅色倒影。"),
    ("正面平视视角，镜头稍低，约 1.2 米",
     "拍摄时间是午后，自然光偏暖，天空通透",
     "玻璃表面有淡淡的层次反光，店内暖光与室外光自然融合。"),
    ("略微斜侧约 12 度的视角",
     "拍摄时间是上午，光线充足明亮",
     "街对面的建筑与行道树在玻璃上有清晰倒影，画面有自然的透视纵深。"),
    ("正面平视视角，镜头略高，约 1.7 米",
     "拍摄时间是阴天，柔和均匀的散射光，没有硬阴影",
     "店内暖光显得更突出，整体通透干净、暗部细节丰富。"),
    ("略微斜侧约 8 度的视角，镜头高度约 1.4 米",
     "拍摄时间是傍晚前，斜射的暖阳照在玻璃上",
     "门框有细微的金属反光，街道上偶有行人虚影。"),
    ("正面平视视角，镜头高度约 1.6 米",
     "拍摄时间是上午，光线充足明亮，天空通透",
     "画面一侧有行道树的影子落在玻璃上，店内陈设层次分明。"),
)

# 真实感阶梯：第 N 张叠加第 N 级，单调递增；到顶后保持最高级（不回退）。
BACKGROUND_REALISM_LADDER: tuple[str, ...] = (
    "照片质感真实：全幅清晰锐利，店内陈设与墙面细节都能看清，"
    "不要景深虚化、不要大光圈虚化、不要背景模糊；只有轻微的传感器噪点与自然的高光。",
    "照片质感更强：保留真实的材质纹理（木纹、金属、玻璃厚度感）与轻微镜头畸变，"
    "高光自然溢出，画面不追求完美对称；全幅依然清晰，不要虚化。",
    "照片质感很强：具备真实手机照片的宽容度与色彩响应，暗部保留细节、"
    "高光平滑过渡，玻璃表面有极淡的清洁痕迹，依然是全幅清晰不虚化。",
    "照片质感接近实拍原片：包含真实的拍摄痕迹（极轻的手持抖动、自然的噪声分布、"
    "玻璃边缘的真实折射），色温与白平衡随环境自然变化，全幅清晰不虚化。",
    "照片质感等同专业实拍：宽容度、噪点结构与材质反射都符合真实光学规律，"
    "没有任何 AI 生成的过度平滑感；暗部有细节、高光不溢出，全幅清晰不虚化。",
)


def _round_state_path(output_base_root) -> Path:
    return Path(output_base_root) / BACKGROUND_ROUND_FILE


def reserve_background_round(output_base_root) -> int:
    """原子递增背景生成轮次，服务重启后也不会重复使用上一轮变体。"""
    path = _round_state_path(output_base_root)
    counter = 0
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            counter = int(payload.get("counter", 0) or 0)
        except (OSError, ValueError, json.JSONDecodeError):
            counter = 0
    counter += 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"counter": counter}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        pass
    return counter


def variation_for_round(round_no: int) -> tuple[str, str, str]:
    """按轮次取机位/光线/环境变体。第 1 轮就是第 1 个变体，之后轮转不重样。"""
    index = (max(1, int(round_no or 1)) - 1) % len(BACKGROUND_VARIATIONS)
    return BACKGROUND_VARIATIONS[index]


def realism_for_round(round_no: int) -> tuple[int, str]:
    """按轮次取真实感等级，单调递增、到顶保持。"""
    top = len(BACKGROUND_REALISM_LADDER)
    level = max(1, min(top, int(round_no or 1)))
    return level, BACKGROUND_REALISM_LADDER[level - 1]


def _count_store_backgrounds(output_base_root, store_name: str) -> int:
    """该门店已生成过多少张 AI 背景（决定真实感阶梯爬到第几级）。"""
    if not store_name:
        return 0
    try:
        store = EffectBackgroundStore(output_base_root)
        assets = [
            a for a in store.list()
            if a.extra.get("kind") == "ai_generated_background"
            and str(a.extra.get("store_hint") or "") == store_name
        ]
    except Exception:  # noqa: BLE001
        return 0
    return len(assets)


def _existing_hashes(output_base_root, store_name: str) -> list[str]:
    """该门店已有背景的感知哈希，用于挡住几乎相同的连续成图。"""
    if not store_name:
        return []
    try:
        store = EffectBackgroundStore(output_base_root)
        return [
            str(a.extra.get("phash"))
            for a in store.list()
            if a.extra.get("kind") == "ai_generated_background"
            and str(a.extra.get("store_hint") or "") == store_name
            and a.extra.get("phash")
        ]
    except Exception:  # noqa: BLE001
        return []


def perceptual_hash(data: bytes) -> str:
    """64 位 dHash。与 ``app/generation/regeneration.py`` 同一算法，可互相比较。"""
    try:
        from PIL import Image as _Image

        with _Image.open(io.BytesIO(data)) as src:
            img = src.convert("L").resize((9, 8), _Image.Resampling.LANCZOS)
        pixels = list(img.getdata())
    except Exception:  # noqa: BLE001
        return ""
    value = 0
    for y in range(8):
        row = pixels[y * 9:(y + 1) * 9]
        for x in range(8):
            value = (value << 1) | int(row[x] > row[x + 1])
    return f"{value:016x}"


def hash_distance(left: str, right: str) -> int:
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except (TypeError, ValueError):
        return 64


# 与已有背景的感知哈希距离小于该值即判定「重样」，会换变体重出一次
BACKGROUND_DUPLICATE_DISTANCE = 10
# 单张图最多的重出次数（每次重出都会换一个变体）
BACKGROUND_MAX_RETRY = 1

# 门店名后缀：批量流程传 `folder_name`（如「房屋中介门店」），
# 网页流程传 `main_title`（如「房屋中介」）。两者都必须命中同一个行业描述。
_STORE_SUFFIXES = ("门店", "店铺", "店面", "店", "服务中心", "服务部", "服务")


def resolve_interior_key(store_name: str) -> str | None:
    """把任意门店名归一化到 ``INTERIORS`` 的键，命中不到返回 ``None``。

    ⚠️ 2026-10-05 踩坑：此前直接 ``INTERIORS.get(store_name)``，传入
    「房屋中介门店」（文件夹名）时查不到 → 静默回退到 ``DEFAULT_INTERIOR``
    （「货架与陈列柜」），于是生成了**杂货铺**风格的背景，
    与贴纸的「房屋中介」主题完全不符。现在按三级匹配兜底：
      1. 完全相同
      2. 去掉「门店/店/服务」等后缀后相同（房屋中介门店 → 房屋中介）
      3. 互相包含（取最长的键，更具体）
    """
    name = (store_name or "").strip()
    if not name:
        return None
    if name in INTERIORS:
        return name

    # 2) 去后缀
    stripped = name
    for suffix in _STORE_SUFFIXES:
        if stripped.endswith(suffix) and len(stripped) > len(suffix):
            candidate = stripped[: -len(suffix)]
            if candidate in INTERIORS:
                return candidate
    # 去后缀后仍不中，再用去后缀的形式做包含匹配
    if stripped not in (name, ""):
        name = stripped
        if name in INTERIORS:
            return name

    # 3) 互相包含，取最长的键（更具体）
    hits = [k for k in INTERIORS if k in name or name in k]
    if hits:
        return max(hits, key=len)
    return None


def build_background_prompt(
    store_name: str,
    door: str = "single",
    *,
    variation: tuple[str, str, str] | None = None,
    realism: str = "",
) -> str:
    """构造背景生成提示词（不含任何贴纸/文字要求）。

    Args:
        variation: ``(机位, 光线/时段, 环境细节)``，见 ``BACKGROUND_VARIATIONS``；
            为空时取第 1 个变体（保持旧行为）。
        realism: 真实感要求，见 ``BACKGROUND_REALISM_LADDER``；为空时取第 1 级。
    """
    key = resolve_interior_key(store_name)
    interior = INTERIORS[key] if key else DEFAULT_INTERIOR
    door_desc = DOOR_STYLES.get(door, DOOR_STYLES["single"])
    camera, light, ambience = variation or BACKGROUND_VARIATIONS[0]
    # LIGHTING 是「明亮通透」的通用要求，光线时段由变体提供，两者拼接
    lighting = f"{light}，{LIGHTING}"
    return PROMPT_TEMPLATE.format(
        interior=interior,
        door_desc=door_desc,
        camera=camera,
        lighting=lighting,
        ambience=ambience,
        realism=realism or BACKGROUND_REALISM_LADDER[0],
    )


def pick_background_for_store(output_base_root, store_name: str):
    """按门店名挑选最合适的 **AI 生成** 背景资产。

    匹配优先级（只用 ``ai_generated_background``，不会自动套用用户实拍）：
      1. ``extra.store_hint`` 与门店名完全相同
      2. 门店名与 ``store_hint`` 互相包含（如「汽车补胎门店」↔「汽车补胎」）
      3. 命中不到 → 返回 ``None``（调用方回退到模拟背景）

    同一行业有多张时，取**最新创建**的一张。

    Returns:
        ``ReferenceAsset`` 或 ``None``
    """
    if not store_name:
        return None
    try:
        store = EffectBackgroundStore(output_base_root)
        assets = [
            a for a in store.list()
            if a.extra.get("kind") == "ai_generated_background"
        ]
    except Exception:  # noqa: BLE001
        return None

    if not assets:
        return None

    # 1) 完全匹配
    exact = [a for a in assets if a.extra.get("store_hint") == store_name]
    if exact:
        return max(exact, key=lambda a: a.created_at)

    # 2) 互相包含（处理「汽车补胎」↔「汽车补胎门店」这类差异）
    def contains(asset) -> bool:
        hint = str(asset.extra.get("store_hint") or "")
        if not hint:
            return False
        return hint in store_name or store_name in hint

    loose = [a for a in assets if contains(a)]
    if loose:
        # 包含匹配时取名字最长的（更具体）
        longest = max(len(str(a.extra.get("store_hint") or "")) for a in loose)
        best = [a for a in loose
                if len(str(a.extra.get("store_hint") or "")) == longest]
        return max(best, key=lambda a: a.created_at)

    return None


# ================================================================ 渲染选项解析
# 可写入设置 / 批次清单的背景元数据键（**不含 Base64 内容，不含 API Key**）
BACKGROUND_META_KEYS = (
    "id", "sha256", "file_name", "mime_type", "bytes", "created_at",
    # 附加元数据：区分实拍 / AI 生成，并传递玻璃区标定
    "kind", "label", "glass_region", "provider", "model", "store_hint", "door", "size",
    # 不重样 / 真实感递增的追溯信息
    "variation", "variation_index", "realism_level", "round",
)


def background_public(asset: object) -> dict:
    """只带出可写入设置 / 批次清单的背景元数据。

    ⚠️ ``kind`` 用于区分 ``real_photo``（用户实拍）与
    ``ai_generated_background``（AI 生成，非实拍），**必须保留**。
    """
    if not isinstance(asset, dict) or not asset.get("id"):
        return {}
    return {
        key: asset.get(key)
        for key in BACKGROUND_META_KEYS
        if asset.get(key) is not None
    }


def resolve_render_options(cfg, store_name: str = "") -> dict:
    """解析效果图合成的全部选项 —— **网页流程与批量流程共用这一处**。

    这是唯一的真相来源。历史上 ``app/orchestrator.py`` 曾直接读
    ``cfg.effect_background_asset``，绕过了背景自动匹配与参数传递，
    导致「网页预览好看、批量产出却是模拟背景」的不一致（2026-09-20 修复）。

    背景来源优先级：
      1. ``cfg.effect_background_asset`` —— 用户在设置 / 批次快照中明确选择
      2. 按门店名**自动匹配**同行业的 AI 生成背景
      3. 都没有 → 模拟背景（``simulated_storefront``，界面与清单明确标记）

    Returns:
        可直接展开传给 ``render_storefront_glass`` 的 kwargs。
    """
    from .renderer import EffectParams

    asset = cfg.effect_background_asset if isinstance(cfg.effect_background_asset, dict) else {}
    raw_path = str(asset.get("path") or "").strip()
    path = Path(raw_path) if raw_path else None
    if path and not path.is_file():
        path = None

    if path is None and store_name and getattr(cfg, "auto_match_effect_background", True):
        picked = pick_background_for_store(cfg.output_base_root, store_name)
        if picked is not None:
            asset = picked.public()
            path = picked.path

    region = asset.get("glass_region")
    glass_region = None
    if isinstance(region, (list, tuple)) and len(region) == 4:
        try:
            glass_region = tuple(float(x) for x in region)
        except (TypeError, ValueError):
            glass_region = None

    realism = cfg.realism_iteration
    try:
        realism = max(1, min(3, int(realism if realism is not None else 1)))
    except (TypeError, ValueError):
        realism = 1

    return {
        "background": path,
        "background_asset": background_public(asset),
        "realism_iteration": realism,
        "glass_region": glass_region,
        "params": EffectParams.from_dict(getattr(cfg, "effect_params", None)),
    }


def prompt_variant(base: str, index: int) -> str:
    """同一门店生成多张时，微调视角以获得不同构图。"""
    if index == 0:
        return base
    angles = ["正面平视视角", "略微斜侧 15 度视角", "正面平视视角，镜头稍低"]
    return base.replace("正面平视视角", angles[index % len(angles)], 1)


NEGATIVE_PROMPT = (
    "贴纸，文字，汉字，标语，logo，标识，水印，海报，宣传物料，"
    "人物，商品，货架上的商品特写，卡通，插画，3D渲染，塑料感，"
    "景深虚化，大光圈虚化，背景模糊，失焦，"
    "拼图，多张图片，模糊，低质量"
)

# ---------------------------------------------------------------- 竖梃自动避让
# ⚠️ 2026-10-05：Nano Banana Pro 即使被明确要求「画面中间没有任何竖框」，
#    仍会自动补一根**居中**的细竖梃（本文件顶部注释早有记录）。
#    竖梃横穿贴纸一眼就假 —— 违反「贴纸绝不跨过门框/竖梃」。
#    这里用「细窄 + 高对比 + 跨多行一致」三个特征把它挑出来，
#    再把 glass_region 收窄到较宽的那一扇玻璃。
_MULLION_SCAN_ROWS = (0.08, 0.14, 0.20, 0.28, 0.36, 0.64, 0.72, 0.80, 0.88)
_MULLION_PROBE = 6       # 邻域半径（512 宽工作图上的像素）
_MULLION_MIN_DIP = 12.0  # 与两侧的平均亮度落差阈值（0~255），低于此视为没有竖梃
_MULLION_GAP = 0.014     # 玻璃区与竖梃之间的留白（归一化）


def detect_pane_region(image: bytes, default=GLASS_REGION_SINGLE):
    """检测画面中央的竖梃，返回**较宽那一扇**玻璃的归一化区域。

    检测不到竖梃时原样返回 ``default``（即整块玻璃）。
    """
    try:
        from PIL import Image as _Image

        with _Image.open(io.BytesIO(image)) as im:
            g = im.convert("L").resize((512, 512), _Image.Resampling.BILINEAR)
    except Exception:  # noqa: BLE001
        return default

    px = g.load()
    work = 512
    rows = [int(work * r) for r in _MULLION_SCAN_ROWS]
    lo, hi = int(work * 0.30), int(work * 0.70)

    best_dip, best_x = 0.0, -1
    for x in range(lo, hi):
        if x - _MULLION_PROBE < 0 or x + _MULLION_PROBE >= work:
            continue
        total = 0.0
        for y in rows:
            neighbour = (px[x - _MULLION_PROBE, y] + px[x + _MULLION_PROBE, y]) / 2.0
            total += max(0.0, neighbour - px[x, y])
        dip = total / len(rows)
        if dip > best_dip:
            best_dip, best_x = dip, x

    if best_x < 0 or best_dip < _MULLION_MIN_DIP:
        return default

    mullion = best_x / work
    x0, y0, x1, y1 = default
    if not (x0 + 0.05 < mullion < x1 - 0.05):
        return default  # 偏离中央太远，多半不是中挺，保守不动

    if (mullion - x0) >= (x1 - mullion):
        return (x0, y0, round(mullion - _MULLION_GAP, 4), y1)
    return (round(mullion + _MULLION_GAP, 4), y0, x1, y1)


def _to_data_url(raw: bytes) -> str:
    """把服务商返回的图片字节转成**格式正确**的 data URL。

    踩坑记录（2026-10-05）：AI Hive 默认返回 **JPEG** 字节，早期这里写死
    声明 ``data:image/png``，被 ``EffectBackgroundStore.add_data_url`` 的
    文件头签名校验拦下（「参考图文件内容与声明格式不一致」），
    表现是「AI 明明生成成功、却一张都没存下来」。
    """
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        # 未知格式：统一转成 PNG，保证与资产库的签名校验一致
        mime = "image/png"
        try:
            import io as _io

            from PIL import Image as _Image

            with _Image.open(_io.BytesIO(raw)) as im:
                buf = _io.BytesIO()
                im.convert("RGB").save(buf, format="PNG")
                raw = buf.getvalue()
        except Exception:  # noqa: BLE001
            pass  # 保留原始字节，交给下游报出真实错误
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            # 转码也失败，退回 jpeg 声明（多数服务商返回 jpeg）
            mime = "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


ProgressFn = Callable[[dict], "Awaitable[None] | None"]


async def generate_backgrounds(
    cfg,
    store_name: str,
    count: int = 1,
    *,
    door: str = "single",
    size: str = "1024x1536",
    on_progress: ProgressFn | None = None,
    vary: bool = True,
) -> dict:
    """生成 AI 门店玻璃背景并写入背景资产库。

    默认开启「不重样 + 真实感递增」（``vary=True``）：
      · 机位/光线/环境细节按**全局轮次**轮转 → 连续生成绝不重样
      · 真实感等级按**该门店已有背景张数**逐级抬升 → 每次重新生成都更真实
      · 感知哈希兜底 → 与已有背景过于相似时自动换变体重出一次

    Returns:
        ``{"saved": int, "assets": [public...], "errors": [str...]}``
    """
    provider = create_provider(cfg)
    store = EffectBackgroundStore(cfg.output_base_root)
    region = GLASS_REGION_SINGLE if door == "single" else GLASS_REGION_DOUBLE

    # 轮次：
    #   · round_label  —— 全局单调计数，只作追溯（「第 N 轮」）
    #   · store_round  —— 该门店已有背景张数，驱动**变体轮转**与**真实感递增**
    # 变体索引按门店逐次 +1（步长 1 与变体数 6 互质）→ 连续生成必然换机位；
    # 若改用全局计数 + 门店计数一起推进，步长会变成 2，6 个机位只能用到 3 个。
    round_label = reserve_background_round(cfg.output_base_root) if vary else 1
    store_round = _count_store_backgrounds(cfg.output_base_root, store_name) if vary else 0
    known_hashes = _existing_hashes(cfg.output_base_root, store_name) if vary else []

    saved: list[dict] = []
    errors: list[str] = []

    async def emit(payload: dict) -> None:
        if on_progress is None:
            return
        result = on_progress(payload)
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]

    await emit({"type": "background_started", "store": store_name, "count": count,
                "provider": provider.describe(),
                "round": round_label, "store_round": store_round})

    try:
        for i in range(max(1, count)):
            await emit({"type": "background_progress", "index": i + 1, "total": count})
            t0 = time.perf_counter()

            # ---- 变体 + 真实感（每次重出都换一个变体）----
            attempt = 0
            result = None
            prompt = ""
            variation = ("", "", "")
            realism_level = 1
            variation_index = 0
            while True:
                # 变体：按门店逐次 +1 轮转，步长与变体数互质 → 不重样
                variation_index = (
                    (store_round + i + attempt) % len(BACKGROUND_VARIATIONS)
                    if vary else 0
                )
                variation = BACKGROUND_VARIATIONS[variation_index]
                # 真实感：按该门店已生成张数逐级抬升，到顶保持
                realism_level, realism = realism_for_round(
                    store_round + i + 1 if vary else 1
                )
                prompt = build_background_prompt(
                    store_name, door, variation=variation, realism=realism
                )
                try:
                    result = await provider.generate(
                        GenerateRequest(
                            prompt=prompt,
                            negative_prompt=NEGATIVE_PROMPT,
                            size=size,
                            n=1,
                        )
                    )
                except ProviderError as exc:
                    errors.append(str(exc))
                    await emit({"type": "background_failed", "index": i + 1,
                                "error": str(exc)})
                    if not getattr(exc, "retryable", True):
                        result = None
                    break

                if not result.ok or not result.images:
                    errors.append("服务商未返回图片")
                    result = None
                    break

                # ---- 感知哈希去重：太像就换变体重出 ----
                digest = perceptual_hash(result.images[0]) if vary else ""
                too_close = bool(digest) and any(
                    hash_distance(digest, h) < BACKGROUND_DUPLICATE_DISTANCE
                    for h in known_hashes
                )
                if not too_close or attempt >= BACKGROUND_MAX_RETRY:
                    if digest:
                        known_hashes.append(digest)
                    break
                attempt += 1
                await emit({
                    "type": "background_retry",
                    "index": i + 1,
                    "attempt": attempt,
                    "reason": "与已有背景过于相似，换变体重出",
                })

            if result is None or not result.ok or not result.images:
                continue

            elapsed = time.perf_counter() - t0
            stamp = time.strftime("%Y%m%d_%H%M%S")
            raw_image = result.images[0]
            # 自动避开模型擅自补上的居中竖梃（否则贴纸会横跨门框）
            pane = detect_pane_region(raw_image, default=region)
            if tuple(pane) != tuple(region):
                await emit({
                    "type": "background_pane_adjusted",
                    "index": i + 1,
                    "glass_region": list(pane),
                    "note": "检测到居中竖梃，已把贴纸区收窄到单扇玻璃",
                })
            data_url = _to_data_url(raw_image)
            try:
                asset = store.add_data_url(
                    data_url,
                    f"ai_{stamp}_{i + 1}.png",
                    extra={
                        "kind": "ai_generated_background",
                        "label": "AI 生成的门店玻璃背景（非实拍）",
                        "glass_region": list(pane),
                        "prompt": prompt,
                        "provider": cfg.provider,
                        "model": cfg.model,
                        "store_hint": store_name,
                        "size": size,
                        "door": door,
                        # 不重样 / 真实感递增的追溯信息
                        "variation": variation[0],
                        "variation_index": variation_index,
                        "realism_level": realism_level,
                        "round": round_label,
                        "phash": perceptual_hash(raw_image) if vary else "",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"写入资产库失败：{exc}")
                continue

            info = asset.public()
            saved.append(info)
            await emit({
                "type": "background_saved",
                "index": i + 1,
                "id": asset.id,
                "file_name": asset.file_name,
                "elapsed": round(elapsed, 2),
                "variation": variation[0],
                "realism_level": realism_level,
                "round": round_label,
            })
    finally:
        await provider.close()

    await emit({"type": "background_finished", "saved": len(saved), "errors": errors})
    return {"saved": len(saved), "assets": saved, "errors": errors}
