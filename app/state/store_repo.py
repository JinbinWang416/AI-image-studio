# -*- coding: utf-8 -*-
"""
数据仓库：加载与校验 `data/stores.json`，并展开成生成任务列表。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..core.config import DATA_FILE
from ..core.models import Job, Store

EXPECTED_STORES = 23
EXPECTED_ITEMS_PER_STORE = 6
EXPECTED_TOTAL = EXPECTED_STORES * EXPECTED_ITEMS_PER_STORE


class StoreRepository:
    """门店提示词数据仓库。"""

    def __init__(self, path: Path | str = DATA_FILE):
        self.path = Path(path)
        self._stores: list[Store] = []

    # ------------------------------------------------------------ 加载
    def load(self) -> list[Store]:
        """从磁盘加载数据。"""
        if not self.path.exists():
            raise FileNotFoundError(f"数据文件不存在：{self.path}")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"数据格式错误：顶层应为数组，实际为 {type(raw).__name__}")
        self._stores = [Store.from_dict(d) for d in raw]
        return self._stores

    @property
    def stores(self) -> list[Store]:
        if not self._stores:
            self.load()
        return self._stores

    # ------------------------------------------------------------ 校验
    def validate(self) -> list[str]:
        """校验数据完整性，返回问题清单（空 = 全部正常）。"""
        problems: list[str] = []
        stores = self.stores

        if len(stores) != EXPECTED_STORES:
            problems.append(f"门店数为 {len(stores)}，应为 {EXPECTED_STORES}")

        seen_dirs: set[str] = set()
        pairs: list[tuple[str, str]] = []
        total = 0

        for s in stores:
            tag = s.folder_index
            if s.output_dir in seen_dirs:
                problems.append(f"{tag} 输出目录重复：{s.output_dir}")
            seen_dirs.add(s.output_dir)

            if not s.output_dir.startswith(tag + "_"):
                problems.append(f"{tag} 输出目录未以序号开头：{s.output_dir}")

            if len(s.pdd_title) != 30:
                problems.append(f"{tag} 拼多多标题 {len(s.pdd_title)} 字（应为 30）")

            if len(s.items) != EXPECTED_ITEMS_PER_STORE:
                problems.append(
                    f"{tag} 图片数为 {len(s.items)}，应为 {EXPECTED_ITEMS_PER_STORE}"
                )
            total += len(s.items)

            for it in s.items:
                pairs.append((s.output_dir, it.file_name))
                if s.main_title not in it.positive_prompt:
                    problems.append(f"{tag}/{it.pic_index} 提示词缺主标题")
                if s.sub_title not in it.positive_prompt:
                    problems.append(f"{tag}/{it.pic_index} 提示词缺副标题")

        if total != EXPECTED_TOTAL:
            problems.append(f"提示词总数为 {total}，应为 {EXPECTED_TOTAL}")

        if len(pairs) != len(set(pairs)):
            problems.append("存在重复的（输出目录, 文件名）组合")

        return problems

    # ------------------------------------------------------------ 展开任务
    def build_jobs(self, prompt_version: str = "") -> list[Job]:
        """把 23 套 × 6 张展开为 138 个生成任务。

        Args:
            prompt_version: 指定提示词版本（如 "v6"），留空则用当前生产版本
        """
        jobs: list[Job] = []
        for s in self.stores:
            for it in s.items:
                pos, neg = it.prompt_for(prompt_version)
                jobs.append(
                    Job(
                        job_id=f"{s.folder_index}-{it.pic_index}",
                        store_index=s.folder_index,
                        store_name=s.folder_name,
                        output_dir=s.output_dir,
                        pic_index=it.pic_index,
                        theme=it.theme,
                        file_name=it.file_name,
                        positive_prompt=pos,
                        negative_prompt=neg,
                        simple_prompt=s.simple_prompt,
                        expected_text=list(it.expected_text),
                        prompt_version=prompt_version or it.prompt_version,
                        main_title=s.main_title,
                        sub_title=s.sub_title,
                        color_theme=s.color_theme,
                        subject=it.subject,
                    )
                )
        return jobs

    # ------------------------------------------------------------ 统计
    def stats(self) -> dict:
        stores = self.stores
        return {
            "stores": len(stores),
            "images": sum(len(s.items) for s in stores),
            "expected_stores": EXPECTED_STORES,
            "expected_images": EXPECTED_TOTAL,
            "path": str(self.path),
        }
