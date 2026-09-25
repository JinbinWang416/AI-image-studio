# -*- coding: utf-8 -*-
"""FLUX 本地样图验证的范围约束、运行清单和人工评分表。"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime
from pathlib import Path

from PIL import Image

from ..core.config import LOCAL_VALIDATION_ROOT
from ..state.manifest_store import ManifestStore
from ..core.models import Store
from ..state.storage import Storage

VALIDATION_STORE_INDEX = "01"
VALIDATION_PROMPT_VERSION = "v8"
VALIDATION_IMAGES = 6
REPORT_NAME = "_local_validation_report.json"
SCORE_NAME = "_人工评分表.csv"


def validation_store(stores: list[Store]) -> Store:
    for store in stores:
        if store.folder_index == VALIDATION_STORE_INDEX:
            return store
    raise ValueError("数据包中不存在门店 01")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _edge_white_ratio(path: Path) -> tuple[float, tuple[int, int], str]:
    with Image.open(path) as image:
        image.load()
        rgb = image.convert("RGB")
        width, height = rgb.size
        pixels = rgb.load()
        edge = [
            *(pixels[x, 0] for x in range(width)),
            *(pixels[x, height - 1] for x in range(width)),
            *(pixels[0, y] for y in range(1, height - 1)),
            *(pixels[width - 1, y] for y in range(1, height - 1)),
        ]
        white = sum(pixel == (255, 255, 255) for pixel in edge)
        return (white / len(edge) if edge else 0.0, (width, height), image.format or "")


def _read_scores(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return {row.get("文件名", ""): row for row in csv.DictReader(handle) if row.get("文件名")}
    except OSError:
        return {}


def _write_score_sheet(path: Path, store: Store, rows: list[dict]) -> None:
    previous = _read_scores(path)
    fields = [
        "文件名", "主题", "主标题", "副标题", "期望文字", "PNG校验", "边缘纯白",
        "主标题准确(是/否)", "副标题准确(是/否)", "行业主体评分(1-5)",
        "构图评分(1-5)", "白底评分(1-5)", "可制作(是/否)", "备注",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            old = previous.get(row["文件名"], {})
            writer.writerow({
                "文件名": row["文件名"], "主题": row["主题"], "主标题": store.main_title,
                "副标题": store.sub_title, "期望文字": row["期望文字"],
                "PNG校验": "通过" if row["png_valid"] else "不通过",
                "边缘纯白": "通过" if row["edge_white"] else "不通过",
                "主标题准确(是/否)": old.get("主标题准确(是/否)", ""),
                "副标题准确(是/否)": old.get("副标题准确(是/否)", ""),
                "行业主体评分(1-5)": old.get("行业主体评分(1-5)", ""),
                "构图评分(1-5)": old.get("构图评分(1-5)", ""),
                "白底评分(1-5)": old.get("白底评分(1-5)", ""),
                "可制作(是/否)": old.get("可制作(是/否)", ""), "备注": old.get("备注", ""),
            })


def _manual_grade(scores: dict[str, dict], rows: list[dict]) -> tuple[str, float | None]:
    if len(scores) < VALIDATION_IMAGES:
        return "实验可用", None
    ratings: list[float] = []
    for item in rows:
        score = scores.get(item["文件名"], {})
        if score.get("主标题准确(是/否)") not in {"是", "√", "Y", "y"}:
            return "实验可用", None
        if score.get("副标题准确(是/否)") not in {"是", "√", "Y", "y"}:
            return "实验可用", None
        try:
            ratings.append(float(score.get("构图评分(1-5)", "")))
        except ValueError:
            return "实验可用", None
    average = round(sum(ratings) / len(ratings), 2)
    return ("量产候选" if average >= 4.0 else "实验可用"), average


def build_validation_report(store: Store, root: Path = LOCAL_VALIDATION_ROOT) -> dict:
    root = Path(root)
    output_dir = root / store.output_dir
    generated_dir = Storage(root).generated_dir(store.output_dir)
    manifest = ManifestStore(root).for_store(store.output_dir)
    rows: list[dict] = []
    for item in store.items:
        path = generated_dir / item.file_name
        png_valid = False
        dimensions: tuple[int, int] | None = None
        edge_ratio = 0.0
        image_format = ""
        error = ""
        if path.exists():
            try:
                signature = path.read_bytes()[:8]
                edge_ratio, dimensions, image_format = _edge_white_ratio(path)
                png_valid = signature == b"\x89PNG\r\n\x1a\n" and image_format == "PNG" and dimensions == (1024, 1024)
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
        entry = manifest.entries.get(item.pic_index, {})
        rows.append({
            "pic_index": item.pic_index,
            "文件名": item.file_name,
            "主题": item.theme,
            "期望文字": " / ".join(item.expected_text),
            "path": str(path),
            "exists": path.exists(),
            "png_valid": png_valid,
            "dimensions": list(dimensions) if dimensions else None,
            "edge_white_ratio": round(edge_ratio, 6),
            "edge_white": edge_ratio == 1.0,
            "sha256": _sha256(path) if png_valid else "",
            "bytes": path.stat().st_size if path.exists() else 0,
            "status": entry.get("status", "pending"),
            "attempts": entry.get("attempts", 0),
            "elapsed": entry.get("elapsed", 0),
            "runtime_metrics": entry.get("runtime_metrics", {}),
            "error": error or entry.get("error", ""),
        })
    technical_ok = len(rows) == VALIDATION_IMAGES and all(
        row["status"] == "success" and row["png_valid"] and row["edge_white"] for row in rows
    )
    score_path = root / SCORE_NAME
    _write_score_sheet(score_path, store, rows)
    grade, average = _manual_grade(_read_scores(score_path), rows) if technical_ok else ("不通过", None)
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": {"store": VALIDATION_STORE_INDEX, "prompt_version": VALIDATION_PROMPT_VERSION, "images": VALIDATION_IMAGES, "concurrency": 1},
        "output_root": str(root), "store_dir": str(output_dir), "generated_dir": str(generated_dir), "technical_pass": technical_ok,
        "grade": grade, "composition_average": average, "rows": rows,
        "score_sheet": str(score_path),
        "criteria": {
            "量产候选": "6 张主标题与副标题均准确，平均构图评分不少于 4/5。",
            "实验可用": "技术链路完整，但人工文字或审美尚未达到量产门槛。",
            "不通过": "发生 OOM、无法稳定完成 6 张、PNG/边缘校验失败，或文字准确率不足。",
        },
    }


def write_validation_artifacts(store: Store, root: Path = LOCAL_VALIDATION_ROOT) -> dict:
    report = build_validation_report(store, root)
    target = Path(root) / REPORT_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
