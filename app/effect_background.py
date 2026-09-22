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
import time
from pathlib import Path
from typing import Awaitable, Callable

from .providers import create_provider
from .providers.base import GenerateRequest, ProviderError
from .reference_assets import EffectBackgroundStore

# 整块落地玻璃在画面中的归一化区域（留出四周门框，贴纸绝不跨框）
GLASS_REGION_SINGLE = (0.030, 0.020, 0.950, 0.930)
# 双开门时只贴左扇
GLASS_REGION_DOUBLE = (0.075, 0.045, 0.478, 0.925)

PROMPT_TEMPLATE = (
    "真实手机拍摄的门店玻璃照片，正面平视视角，1:1 正方形构图。"
    "画面主体是{door_desc}，玻璃通透，可以清楚地看到店内环境："
    "{interior}。"
    "{lighting}"
    "照片质感真实：有轻微的传感器噪点、自然的景深虚化、略微过曝的高光。"
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
LIGHTING = (
    "拍摄时间是傍晚，天色尚未全黑，室外仍有余晖与自然光，街道与建筑的轮廓清晰可辨；"
    "玻璃表面呈现中等的深色调，映出街景、树影与对面建筑的倒影；"
    "而店内灯光明亮温暖，暖黄灯光透过玻璃透出来，是画面中最亮的部分 —— "
    "两者形成清晰的明暗层次与温暖的傍晚氛围。"
    "环境明亮通透、暗部保留细节，不要死黑，也不要整体过曝发白。"
    "室内为暖黄灯光（色温约 3200K），玻璃上有清晰的环境倒影与街灯反射。"
)

DOOR_STYLES = {
    "single": (
        "店面的一块完整落地玻璃（整片橱窗或单扇玻璃门），"
        "这整块玻璃占据画面约 80% 的面积，"
        "画面中间没有任何竖框、门缝、中挺或分隔条，只有四周一圈细门框"
    ),
    "double": "一扇双开铝合金框玻璃门，中间有一道竖框",
}

INTERIORS = {
    "房屋中介": "简洁的办公桌椅、接待台、墙上的房源展示板、几盆绿植、吊灯",
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


def build_background_prompt(store_name: str, door: str = "single") -> str:
    """构造背景生成提示词（不含任何贴纸/文字要求）。"""
    interior = INTERIORS.get(store_name, DEFAULT_INTERIOR)
    door_desc = DOOR_STYLES.get(door, DOOR_STYLES["single"])
    return PROMPT_TEMPLATE.format(
        interior=interior, door_desc=door_desc, lighting=LIGHTING
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
    from .effect_renderer import EffectParams

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
    "拼图，多张图片，模糊，低质量"
)

ProgressFn = Callable[[dict], "Awaitable[None] | None"]


async def generate_backgrounds(
    cfg,
    store_name: str,
    count: int = 1,
    *,
    door: str = "single",
    size: str = "1024x1536",
    on_progress: ProgressFn | None = None,
) -> dict:
    """生成 AI 门店玻璃背景并写入背景资产库。

    Returns:
        ``{"saved": int, "assets": [public...], "errors": [str...]}``
    """
    provider = create_provider(cfg)
    store = EffectBackgroundStore(cfg.output_base_root)
    base_prompt = build_background_prompt(store_name, door)
    region = GLASS_REGION_SINGLE if door == "single" else GLASS_REGION_DOUBLE

    saved: list[dict] = []
    errors: list[str] = []

    async def emit(payload: dict) -> None:
        if on_progress is None:
            return
        result = on_progress(payload)
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]

    await emit({"type": "background_started", "store": store_name, "count": count,
                "provider": provider.describe()})

    try:
        for i in range(max(1, count)):
            prompt = prompt_variant(base_prompt, i)
            await emit({"type": "background_progress", "index": i + 1, "total": count})
            t0 = time.perf_counter()
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
                await emit({"type": "background_failed", "index": i + 1, "error": str(exc)})
                if not getattr(exc, "retryable", True):
                    break
                continue

            if not result.ok or not result.images:
                errors.append("服务商未返回图片")
                continue

            elapsed = time.perf_counter() - t0
            stamp = time.strftime("%Y%m%d_%H%M%S")
            data_url = "data:image/png;base64," + base64.b64encode(result.images[0]).decode("ascii")
            try:
                asset = store.add_data_url(
                    data_url,
                    f"ai_{stamp}_{i + 1}.png",
                    extra={
                        "kind": "ai_generated_background",
                        "label": "AI 生成的门店玻璃背景（非实拍）",
                        "glass_region": list(region),
                        "prompt": prompt,
                        "provider": cfg.provider,
                        "model": cfg.model,
                        "store_hint": store_name,
                        "size": size,
                        "door": door,
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
            })
    finally:
        await provider.close()

    await emit({"type": "background_finished", "saved": len(saved), "errors": errors})
    return {"saved": len(saved), "assets": saved, "errors": errors}
