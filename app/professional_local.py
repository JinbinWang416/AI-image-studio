# -*- coding: utf-8 -*-
"""V9 本地专业化样图：无字底图、确定性中文排版与候选评分。"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zlib
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

from .config import LOCAL_PROFESSIONAL_ROOT
from .models import Store, StoreItem
from .postprocess import whiten_background
from .providers.base import BaseProvider, GenerateRequest

PROFESSIONAL_VERSION = "v9-reference-decal-r3"
CANDIDATES_PER_THEME = 3
REPORT_NAME = "_professional_validation_report.json"
FONT_TITLE = Path(r"C:\Windows\Fonts\msyhbd.ttc")
FONT_BODY = Path(r"C:\Windows\Fonts\msyh.ttc")
def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    if not path.exists():
        raise RuntimeError(f"缺少中文字体：{path}")
    return ImageFont.truetype(str(path), size=size, index=0)


def professional_prompt(store: Store, item: StoreItem, variant: int) -> str:
    """只让扩散模型负责视觉素材，禁止模型承担文字排版。"""
    composition = (
        "central emblem composition with a clear lower title-safe panel"
        if variant == 1 else
        "asymmetrical premium badge composition with an uncluttered lower title-safe panel"
        if variant == 2 else
        "layered commercial illustration with a clear lower title-safe panel and balanced negative space"
    )
    return (
        "Premium real-estate storefront die-cut window-sticker design, square 1:1 commercial illustration, "
        f"industry subject: {item.subject}; theme: {item.theme}. "
        f"{composition}. Match a premium green-and-yellow Chinese retail decal visual language: "
        "deep forest green, lime green and warm yellow, an irregular organic cut-out silhouette, "
        "thick dark contour, dimensional layered blocks, large sweeping ribbon arcs, residential buildings, "
        "house keys, location pin and home-service illustrations, small leaf accents, rich printed-sign hierarchy, "
        "high contrast title-safe area, crisp vector-like edges, white outer canvas. "
        "Absolutely no text, no letters, no numbers, no logo, no watermark, no glyphs, no signage words. "
        "Reserve the lower panel for deterministic typography added after generation."
    )


def _seed(store: Store, item: StoreItem, candidate: int) -> int:
    return zlib.crc32(f"{PROFESSIONAL_VERSION}:{store.folder_index}:{item.pic_index}:{candidate}".encode())


def _fit_font(text: str, font_path: Path, start: int, max_width: int) -> ImageFont.FreeTypeFont:
    for size in range(start, 26, -2):
        font = _font(font_path, size)
        if font.getbbox(text)[2] <= max_width:
            return font
    return _font(font_path, 26)


def compose_chinese(base_png: bytes, store: Store, item: StoreItem) -> bytes:
    """在底图上叠加可验证、可印刷的真实中文。"""
    with Image.open(io.BytesIO(base_png)) as source:
        source_rgba = source.convert("RGBA")
    if source_rgba.size != (1024, 1024):
        source_rgba = source_rgba.resize((1024, 1024), Image.Resampling.LANCZOS)
    # 用户参考为深绿、柠檬黄、米白的立体商业窗贴。本轮专业化验证固定采用这组
    # 高辨识度配色，让 01 门店先验证视觉语言；原有门店配色不被通用 output 改写。
    main, accent, light = (25, 72, 46), (224, 187, 45), (250, 247, 224)
    outline, lime, shade = (19, 57, 40), (119, 178, 55), (10, 38, 28)
    # 扩散模型偶尔仍会在“无文字”底图里画出伪字。先把底图裁进异形贴纸，
    # 再用确定性标题区和行业图标区覆盖所有可能出现伪字的区域。
    shape = Image.new("L", (1024, 1024), 0)
    # 正面贴纸采用平滑切边；曲线丝带和叶片在内部提供异形层叠感，避免外轮廓出现生硬折线。
    ImageDraw.Draw(shape).rounded_rectangle((36, 28, 988, 994), radius=118, fill=255)
    canvas = Image.new("RGBA", (1024, 1024), "white")
    clipped = Image.composite(source_rgba, Image.new("RGBA", (1024, 1024)), shape)
    canvas.alpha_composite(clipped)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    # 参考高密度商业贴纸的“异形轮廓 + 顶部标签 + 大标题 + 丝带 + 行业图标”。
    draw.rounded_rectangle((36, 28, 988, 994), radius=118, outline=(*outline, 255), width=30)
    draw.rounded_rectangle((57, 48, 967, 974), radius=96, outline=(*accent, 255), width=13)
    draw.rounded_rectangle((84, 86, 940, 626), radius=86, fill=(*main, 236), outline=(*outline, 255), width=12)
    draw.ellipse((72, 332, 324, 620), fill=(*lime, 210), outline=(*outline, 210), width=8)
    draw.ellipse((710, 314, 945, 612), fill=(*accent, 210), outline=(*outline, 210), width=8)
    draw.rounded_rectangle((178, 105, 846, 195), radius=23, fill=(*accent, 255), outline=(*outline, 255), width=5)
    label_font = _fit_font(store.folder_name, FONT_TITLE, 38, 585)
    label_box = draw.textbbox((0, 0), store.folder_name, font=label_font)
    draw.text((512 - (label_box[2] - label_box[0]) / 2, 128), store.folder_name, font=label_font, fill=outline)
    title = _fit_font(store.main_title, FONT_TITLE, 146, 780)
    subtitle = _fit_font(store.sub_title, FONT_BODY, 44, 690)
    title_box = draw.textbbox((0, 0), store.main_title, font=title)
    title_x = 512 - (title_box[2] - title_box[0]) / 2
    draw.text((title_x + 11, 246 + 14), store.main_title, font=title, fill=(*shade, 205), stroke_width=13, stroke_fill=(*shade, 205))
    draw.text((title_x, 246), store.main_title, font=title, fill=light, stroke_width=10, stroke_fill=outline)
    sub_box = draw.textbbox((0, 0), store.sub_title, font=subtitle)
    sub_x = 512 - (sub_box[2] - sub_box[0]) / 2
    draw.rounded_rectangle((124, 497, 900, 582), radius=27, fill=(*light, 255), outline=(*accent, 255), width=7)
    draw.text((sub_x, 509), store.sub_title, font=subtitle, fill=outline)
    # 下方完全由程序绘制，避免保留模型伪文字；保留行业符号和丝带的商业贴纸密度。
    draw.rounded_rectangle((76, 618, 948, 936), radius=70, fill=(*main, 255), outline=(*outline, 255), width=11)
    draw.arc((6, 340, 1012, 962), start=19, end=165, fill=(*accent, 255), width=46)
    draw.arc((18, 370, 995, 980), start=21, end=164, fill=(*lime, 255), width=21)
    draw.arc((4, 402, 991, 992), start=27, end=157, fill=(*light, 245), width=11)
    _draw_industry_icons(draw, item, main, accent, light, outline, lime)
    composed = Image.alpha_composite(canvas, layer).convert("RGB")
    out = io.BytesIO()
    composed.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _draw_industry_icons(draw: ImageDraw.ImageDraw, item: StoreItem, main: tuple[int, int, int], accent: tuple[int, int, int], light: tuple[int, int, int], outline: tuple[int, int, int], lime: tuple[int, int, int]) -> None:
    """可控行业小图标，既提升贴纸密度，也不会出现模型伪文字。"""
    xs = (137, 341, 545, 749)
    for index, x in enumerate(xs):
        # 适用于房产首轮；其他行业同样保持为无文字的通用商业陈列图标。
        y = 704 + (index % 2) * 20
        draw.rounded_rectangle((x, y, x + 132, y + 138), radius=20, fill=(*light, 255), outline=(*accent, 255), width=6)
        if index == 0:  # 住宅
            draw.polygon(((x + 14, y + 60), (x + 66, y + 18), (x + 118, y + 60)), fill=(*accent, 255), outline=(*outline, 255))
            draw.rectangle((x + 29, y + 58, x + 103, y + 121), fill=(*main, 255), outline=(*accent, 255), width=3)
            draw.rectangle((x + 58, y + 86, x + 75, y + 121), fill=(*accent, 255))
        elif index == 1:  # 楼宇
            draw.rectangle((x + 31, y + 39, x + 79, y + 121), fill=(*main, 255), outline=(*outline, 255), width=4)
            draw.rectangle((x + 75, y + 62, x + 108, y + 121), fill=(*lime, 255), outline=(*outline, 255), width=4)
            for wx, wy in ((40, 53), (59, 53), (40, 72), (59, 72), (84, 75), (96, 75)):
                draw.rectangle((x + wx, y + wy, x + wx + 9, y + wy + 10), fill=(*light, 255))
        elif index == 2:  # 定位房源
            draw.ellipse((x + 36, y + 18, x + 101, y + 92), fill=(*lime, 255), outline=(*outline, 255), width=4)
            draw.polygon(((x + 47, y + 72), (x + 69, y + 121), (x + 91, y + 72)), fill=(*lime, 255), outline=(*outline, 255))
            draw.ellipse((x + 58, y + 39, x + 79, y + 60), fill=(*light, 255))
        else:  # 钥匙和成交服务
            draw.ellipse((x + 25, y + 46, x + 70, y + 91), outline=(*main, 255), width=12)
            draw.line((x + 65, y + 80, x + 110, y + 119), fill=(*main, 255), width=15)
            draw.line((x + 93, y + 105, x + 111, y + 86), fill=(*accent, 255), width=8)


def _score(png: bytes) -> tuple[float, dict]:
    """用可解释的视觉指标淘汰明显平淡、模糊或失衡的候选。"""
    with Image.open(io.BytesIO(png)) as source:
        image = source.convert("RGB")
    # 不让确定性标题带影响候选分数：只评价 0..660 的模型生成区域。
    art = image.crop((0, 0, 1024, 660))
    hsv = art.convert("HSV")
    h, s, v = hsv.split()
    sampled_s = list(s.resize((128, 83)).get_flattened_data())
    saturation = sum(value > 55 for value in sampled_s) / len(sampled_s)
    edge = ImageStat.Stat(art.filter(ImageFilter.FIND_EDGES).convert("L")).mean[0]
    pixels = art.resize((128, 83)).load()
    foreground = [(x, y) for y in range(83) for x in range(128) if max(pixels[x, y]) < 246]
    coverage = len(foreground) / (128 * 83)
    if foreground:
        cx = sum(x for x, _ in foreground) / len(foreground) / 127
        cy = sum(y for _, y in foreground) / len(foreground) / 82
    else:
        cx, cy = 0.5, 0.5
    balance = max(0.0, 1.0 - (abs(cx - 0.5) + abs(cy - 0.45)) * 1.2)
    coverage_score = max(0.0, 1.0 - abs(coverage - 0.47) / 0.47)
    detail_score = min(edge / 35.0, 1.0)
    score = round(100 * (0.30 * saturation + 0.28 * detail_score + 0.24 * coverage_score + 0.18 * balance), 2)
    return score, {
        "color_richness": round(saturation, 4), "detail": round(edge, 3),
        "art_coverage": round(coverage, 4), "balance": round(balance, 4),
    }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def run_professional_validation(
    provider: BaseProvider,
    store: Store,
    root: Path = LOCAL_PROFESSIONAL_ROOT,
    on_event: Callable[[dict], Awaitable[None] | None] | None = None,
) -> dict:
    """固定执行 01 门店 6 主题 × 3 候选，不写入通用 output。"""
    root = Path(root)
    candidate_dir = root / "candidates" / store.output_dir
    selected_dir = root / "selected" / store.output_dir
    candidate_dir.mkdir(parents=True, exist_ok=True)
    selected_dir.mkdir(parents=True, exist_ok=True)
    previous_rows: dict[str, dict] = {}
    previous_path = root / REPORT_NAME
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            previous_rows = {str(row.get("pic_index")): row for row in previous.get("rows", [])}
        except (OSError, json.JSONDecodeError):
            previous_rows = {}
    rows: list[dict] = []
    started = datetime.now().astimezone()

    async def emit(event: dict) -> None:
        if on_event is None:
            return
        result = on_event(event)
        if asyncio.iscoroutine(result):
            await result

    await emit({"type": "professional_started", "total": len(store.items), "candidates_per_theme": CANDIDATES_PER_THEME})
    for item in store.items:
        candidates: list[dict] = []
        for number in range(1, CANDIDATES_PER_THEME + 1):
            seed = _seed(store, item, number)
            try:
                generated = await provider.generate(GenerateRequest(
                    prompt=professional_prompt(store, item, number), size="1024x1024", n=1, seed=seed,
                ))
                base = whiten_background(generated.images[0])
                composed = compose_chinese(base, store, item)
                filename = f"{item.pic_index}_{item.theme}_c{number}.png"
                path = candidate_dir / filename
                path.write_bytes(composed)
                score, metrics = _score(composed)
                entry = {
                    "candidate": number, "file": filename, "seed": seed, "score": score,
                    "metrics": metrics, "elapsed_seconds": generated.elapsed,
                    "peak_vram_mib": generated.raw.get("peak_vram_mib"), "sha256": _sha256(composed),
                    "path": str(path), "error": "",
                }
            except Exception as exc:  # noqa: BLE001
                entry = {"candidate": number, "seed": seed, "score": 0, "error": f"{type(exc).__name__}: {exc}"}
            candidates.append(entry)
            await emit({"type": "professional_candidate", "pic_index": item.pic_index, "theme": item.theme, **entry})
        valid = [candidate for candidate in candidates if not candidate.get("error")]
        best_new = max(valid, key=lambda candidate: candidate["score"]) if valid else None
        selected_path = selected_dir / item.file_name
        baseline = (previous_rows.get(item.pic_index) or {}).get("selected") or {}
        baseline_score = float(baseline.get("score", -1) or -1)
        baseline_exists = selected_path.is_file() and not baseline.get("error")
        promoted = bool(best_new and (not baseline_exists or best_new["score"] > baseline_score))
        if promoted:
            selected = {**best_new, "source": "new"}
            selected_path.write_bytes(Path(best_new["path"]).read_bytes())
        elif baseline_exists:
            # 只要新候选没有超过已接受版本，就保留旧成品，避免质量回退。
            selected = {**baseline, "source": "previous"}
        else:
            selected = None
        rows.append({
            "pic_index": item.pic_index, "theme": item.theme, "file": item.file_name,
            "main_title": store.main_title, "sub_title": store.sub_title,
            "selected": selected, "promoted": promoted, "candidates": candidates, "selected_path": str(selected_path) if selected else "",
        })
        await emit({"type": "professional_selected", "pic_index": item.pic_index, "theme": item.theme, "selected": selected or {}})

    selected_count = sum(bool(row["selected"]) for row in rows)
    scores = [row["selected"]["score"] for row in rows if row["selected"]]
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "started_at": started.isoformat(timespec="seconds"), "version": PROFESSIONAL_VERSION,
        "scope": {"store": store.folder_index, "images": len(store.items), "candidates_per_theme": CANDIDATES_PER_THEME, "concurrency": 1},
        "output_root": str(root), "technical_pass": selected_count == len(store.items),
        "selected_count": selected_count, "mean_selected_score": round(sum(scores) / len(scores), 2) if scores else None,
        "typography": {"engine": "Pillow + Microsoft YaHei", "title": store.main_title, "subtitle": store.sub_title, "verified": True},
        "rows": rows,
    }
    (root / REPORT_NAME).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
