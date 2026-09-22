# -*- coding: utf-8 -*-
"""V7 → V8：将参考图的版式语言落实为可控的门店贴纸提示词。

V8 保留 V6 验证过的纯白画布和后处理策略，并从参考图提取出：

* 自由曲线异形外轮廓，而不是普通圆角卡片；
* 主标题是最大的视觉信息，副标题放入对比色胶囊横幅；
* 行业主物件与标题共同构图，具备海报级层次；
* 主色块、点缀色与高光色形成有明确比例的配色；
* 六种轮换构图，避免同一门店的六张图只替换图标。

不调用图像模型，仅升级本地 data/stores.json。运行前自动留下 V7 快照。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "stores.json"
BACKUP = ROOT / "data" / "stores.v7.json"

# 主色 / 标题高对比色 / 高光色。保留每个行业原有的色彩语义。
PALETTES: dict[str, tuple[str, str, str]] = {
    "01": ("深海军蓝", "香槟金", "天际蓝"),
    "02": ("深青绿", "米白", "湖蓝"),
    "03": ("明亮天蓝", "暖黄", "草绿"),
    "04": ("灰蓝", "浅银灰", "湖蓝"),
    "05": ("深棕", "暖米色", "焦糖橙"),
    "06": ("砖红", "米白", "深棕"),
    "07": ("湖蓝", "活力橙", "天蓝"),
    "08": ("深蓝", "亮橙", "金属银灰"),
    "09": ("深灰", "正红", "金属银灰"),
    "10": ("石墨黑", "金色", "橙色"),
    "11": ("深灰", "工程橙", "纯黑"),
    "12": ("专业蓝", "亮橙", "中性灰"),
    "13": ("钢蓝", "亮橙", "深灰"),
    "14": ("灰蓝", "青绿", "深海军蓝"),
    "15": ("石墨黑", "专业蓝", "亮橙"),
    "16": ("银灰", "天蓝", "深海军蓝"),
    "17": ("暖米灰", "岩石棕", "浅米白"),
    "18": ("原木棕", "米白", "深胡桃棕"),
    "19": ("石墨黑", "香槟金", "奶油白"),
    "20": ("深灰", "建材橙", "原木色"),
    "21": ("工程灰", "明黄", "工程橙"),
    "22": ("深灰", "砂岩黄", "土棕"),
    "23": ("钢构蓝", "工程橙", "银灰"),
}

# 按图片序号轮换构图，使同一门店的六张图在视觉结构上也不同。
LAYOUTS: dict[str, tuple[str, str]] = {
    "01": (
        "天际线承托式",
        "主体在上半区形成行业轮廓或纵向结构，主标题置于下半区的大色块徽章中，副标题置于标题下方的圆角横幅。",
    ),
    "02": (
        "主物件海报式",
        "主体以大比例占据画面中心，主标题作为画面下方的高对比字标，副标题置于窄横幅，周围以少量行业符号平衡画面。",
    ),
    "03": (
        "徽章聚焦式",
        "主标题置于深色或主色徽章中，主体从徽章后方或中部伸出，形成前后层次；副标题使用亮色胶囊横幅。",
    ),
    "04": (
        "拱窗情景式",
        "以圆拱、弧线或柔和场景框承载主体，主标题叠放在视觉中心，副标题置于紧凑横幅；装饰元素沿轮廓自然生长。",
    ),
    "05": (
        "横幅陈列式",
        "主标题位于上部或中部的宽横幅中，主体作为下方或侧方的主视觉，使用小型行业符号、光芒或弧线补足节奏。",
    ),
    "06": (
        "门店场景式",
        "主体组织成有空间层次的行业场景插画，主标题以前景大字标出现，副标题置于底部对比色横幅，整体具有门店招贴气质。",
    ),
}

NEG_V8 = (
    "吊牌，吊绳，挂孔，圆孔，穿孔，铭牌底座，包装盒，产品展示图，"
    "玻璃窗实拍，桌面实拍，墙面实拍，环境摄影，摄影棚背景，景深，倒影，"
    "投影，外部阴影，低清晰度，灰蒙蒙，杂色噪点，过度渐变，复杂花纹背景，"
    "人物，人脸，手部，顾客，工作人员，真实品牌logo，水印，价格文字，"
    "电话号码，真实地址，绝对承诺文字，多余小字，错误文字，错别字，笔画缺失，"
    "繁体字，拼图，多张图片，裁切，元素溢出画面。"
)

POS_V8 = """高端商用门店标识平面设计稿，1:1 正方形，彩色商业插画与中文字体排版融合，印刷级清晰边缘。

画布四角和外部背景为纯白。中心是一枚自由曲线圆角异形外轮廓，外轮廓完整、闭合、边缘利落，整体占画布的主要面积，呈现可直接制作的门店视觉标识。

设计构图模板：{layout_name}。{layout_description}

主标题精确显示汉字：{main_title}。主标题为画面最大的中文信息，使用厚重中文展示字体，笔画完整清晰，字形饱满，字标占据显著视觉面积。
副标题精确显示汉字：{sub_title}。副标题使用清晰中文字体，置于圆角胶囊横幅或小型牌匾中，与主标题形成明确层级。

行业主视觉为：{subject}。主体与行业语义准确对应，具有高辨识度；主体采用精致商业插画表现，产品类主体具有适度材质细节和高光，场景类主体具有简洁空间层次，始终保持平面设计稿气质。

配色：{primary}为主导大色块，{accent}用于标题、横幅和高对比焦点，{highlight}用于边缘高光与小面积点缀。色彩饱和、对比明确、面积比例清晰。轮廓周围可使用与行业相符的弧线、星点、叶片、光芒、小图标或几何装饰，服务于主标题和主体构图。

成品感：标题、主体、横幅和装饰形成清晰前中后层次；四周保留整洁白色安全边距；整幅设计色彩统一、结构紧凑、细节精致，具有电商门店橱窗标识的吸引力。"""


def build_positive(store: dict, item: dict) -> str:
    primary, accent, highlight = PALETTES[store["folder_index"]]
    layout_name, layout_description = LAYOUTS[item["pic_index"]]
    return POS_V8.format(
        layout_name=layout_name,
        layout_description=layout_description,
        main_title=store["main_title"],
        sub_title=store["sub_title"],
        subject=item["subject"],
        primary=primary,
        accent=accent,
        highlight=highlight,
    )


def validate(stores: list[dict]) -> list[str]:
    errors: list[str] = []
    forbidden = ("不要", "不得", "禁止", "贴纸", "标签", "吊牌", "玻璃", "个汉字", "这组")
    for store in stores:
        for item in store["items"]:
            tag = f"{store['folder_index']}/{item['pic_index']}"
            pos = item["positive_prompt"]
            for term in forbidden:
                if term in pos:
                    errors.append(f"{tag} 正向提示词含禁用词：{term}")
            for term in (store["main_title"], store["sub_title"], item["subject"], "1:1", "自由曲线圆角异形外轮廓"):
                if term not in pos:
                    errors.append(f"{tag} 正向提示词缺少：{term}")
            if item.get("prompt_version") != "v8":
                errors.append(f"{tag} 版本不是 v8")
            if "拼图" not in item.get("negative_prompt", ""):
                errors.append(f"{tag} 负向提示词缺少拼图限制")
    return errors


def main() -> int:
    stores = json.loads(SRC.read_text(encoding="utf-8"))
    if not BACKUP.exists():
        shutil.copy2(SRC, BACKUP)
        print(f"已备份 V7 → {BACKUP.relative_to(ROOT)}")

    count = 0
    for store in stores:
        for item in store["items"]:
            item.setdefault("positive_prompt_v7", item["positive_prompt"])
            item.setdefault("negative_prompt_v7", item["negative_prompt"])
            item["positive_prompt"] = build_positive(store, item)
            item["negative_prompt"] = NEG_V8
            item["prompt_version"] = "v8"
            count += 1
        store["prompt_version"] = "v8"

    errors = validate(stores)
    if errors:
        print("V8 自检失败：")
        for error in errors[:20]:
            print(" -", error)
        return 1

    tmp = SRC.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(stores, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(SRC)
    print(f"V8 提示词升级完成：{count} 条")
    print("保留 V7 历史字段；当前生产版本：v8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
