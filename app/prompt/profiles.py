# -*- coding: utf-8 -*-
"""项目级提示词质量模板与变量渲染。"""
from __future__ import annotations

from dataclasses import dataclass

from ..core.models import Job


# 说明（2026-09-20 依据拼多多「东东窗花店」77 张商品图的设计规范重写）：
#   旧版只笼统要求"异形贴纸轮廓、粗描边、彩色商业插画"，缺少**版式结构**，
#   导致生成结果各行其是。新版补入对方最稳定的几条套路：
#     · 五段式版式（主标题区 → 深色横幅卖点条 → 中央主食图 → 卖点小章 → 底部纹样收边）
#     · 主标题为超大号毛笔书法体（实测单字占贴纸宽 22~28%）
#     · 副标题固定为「深色横幅 + 金色细描边 + 白色小字」
#     · 外缘一圈米白/浅金的**双线模切描边**（该店全部商品的统一识别特征）
#     · 主体为超写实美食摄影级抠图，非插画
#     · 画面内不出现英文/拼音（实测该店 225 次暖色主色、几乎零英文）
DEFAULT_QUALITY_TEMPLATE = """在原始行业提示词基础上，制作彩色、高完成度的线下门店异形玻璃贴设计图。\n【文字内容】主标题必须逐字显示为「{{main_title}}」，副标题必须逐字显示为「{{sub_title}}」，不得翻译、错别字、删改或出现额外文字；画面中不出现任何英文、拼音、数字或其它字词。\n【版式结构】采用自上而下的五段式布局：① 顶部为主标题区，用超大号毛笔书法体书写主标题，整行占贴纸宽度的七至八成；② 主标题正下方为一条深色横幅，横幅内以白色小字横向排布副标题，横幅外沿带金色细描边；③ 画面中央为面积最大的主体区，聚焦 {{subject}}，采用超写实美食摄影级的抠图质感，油润有高光、带热气与蒸汽；④ 主体两侧或下方点缀二至四个小圆章或竖排小标签，内为二至三字的卖点短词；⑤ 底部以祥云纹或稻穗纹收边。\n【轮廓描边】整体为沿内容外缘自然延展的不规则模切轮廓（云朵状或花瓣状波浪边），外缘带一圈米白或浅金色的双线描边，模仿模切印刷标记；轮廓内不留大片空白。\n【配色】围绕 {{theme}} 建立清晰的主次层级，主色参考 {{color_theme}}；主标题使用金色或深红色书法字，带深色描边与浅色内发光，确保在任何底色上都清晰可读。\n【质感】按手机实拍级色彩管理还原真实材质：自然白平衡、明暗层次干净、边缘锐利、细节清楚；输出的是可印刷的贴纸设计稿，不出现手机边框、拍摄界面或模糊压缩痕迹。\n【画布】画布边缘保留充足纯白不透明背景；不出现真实品牌、价格、电话、二维码、人物肖像或夸张承诺；构图适配 {{aspect_ratio}} 画布。"""

REQUIRED_VARIABLES = ("{{main_title}}", "{{sub_title}}", "{{subject}}", "{{theme}}", "{{color_theme}}")


class PromptProfileError(ValueError):
    pass


def validate_template(template: str) -> str:
    cleaned = str(template or "").strip()
    if len(cleaned) < 30:
        raise PromptProfileError("质量模板至少需要 30 个字符")
    missing = [token for token in REQUIRED_VARIABLES if token not in cleaned]
    if missing:
        raise PromptProfileError("质量模板必须保留变量：" + "、".join(missing))
    return cleaned


def _ratio_label(size: str) -> str:
    try:
        width, height = (int(p) for p in size.lower().replace("×", "x").split("x", 1))
    except (TypeError, ValueError):
        return "自定义比例"
    if width == height:
        return "1:1 方形"
    if width < height:
        return "竖版"
    return "横版"


def render_template(template: str, job: Job, size: str) -> str:
    template = validate_template(template)
    values = {
        "{{main_title}}": job.main_title or (job.expected_text[0] if job.expected_text else job.store_name),
        "{{sub_title}}": job.sub_title or (job.expected_text[1] if len(job.expected_text) > 1 else "欢迎到店"),
        "{{subject}}": job.subject or job.theme,
        "{{theme}}": job.theme,
        "{{color_theme}}": job.color_theme or "门店既定配色",
        "{{aspect_ratio}}": _ratio_label(size),
    }
    # StoreRepository 的 expected_text 对旧数据可能为空；正向提示词中仍有店铺文字。
    for token, value in values.items():
        template = template.replace(token, str(value or ""))
    return template


def realism_requirement(iteration: int) -> str:
    """返回逐批提升的正向质量要求，供所有云端服务商共用。"""
    level = max(1, min(3, int(iteration or 1)))
    requirements = {
        1: "真实感 V1：颜色以印刷实物为准，避免高饱和溢色，保留干净的明暗层次与清晰轮廓。",
        2: "真实感 V2：在 V1 基础上强化商业成品质感；校准自然白平衡、纸材细节、边缘微阴影和克制的高光，颜色接近手机实拍，不使用塑料感滤镜。",
        3: "真实感 V3：在 V2 基础上采用专业门店成片标准；材质、色彩、微对比和光影要自然可信，保留印刷文字的锐利度与准确性，禁止过度磨皮、过锐化和失真的 HDR 效果。",
    }
    return requirements[level]


def build_effective_prompt(job: Job, template: str, size: str, realism_iteration: int = 1) -> str:
    rendered = render_template(template, job, size)
    locked = "、".join(f"「{x}」" for x in job.expected_text if x)
    lock_line = f"必须准确渲染的中文文案：{locked}。" if locked else "必须保留原始提示词中的中文主标题与副标题。"
    return (
        f"{job.positive_prompt}\n\n【项目质量模板】\n{rendered}\n"
        f"【逐批真实感要求】{realism_requirement(realism_iteration)}\n{lock_line}"
    )


@dataclass(frozen=True)
class PromptProfile:
    template: str
    optimized_template: str
    use_optimized: bool

    @property
    def active_template(self) -> str:
        return self.optimized_template if self.use_optimized and self.optimized_template else self.template
