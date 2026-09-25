# -*- coding: utf-8 -*-
"""
提示词版本管理。

数据文件里每张图都保存了**全部历史版本**：

```
item = {
    "positive_prompt": "...",       # 当前生产版本
    "negative_prompt": "...",
    "positive_prompt_v1": "...",    # 历史版本
    "negative_prompt_v1": "...",
    ...
    "prompt_version": "v7"
}
```

本模块负责：列出可用版本、按版本号取出提示词、统计各版本的差异。
"""
from __future__ import annotations

import re

_SUFFIX_RE = re.compile(r"^positive_prompt_(v\d+)$")

# 版本说明（供界面展示）
VERSION_NOTES: dict[str, str] = {
    "v1": "原始 txt 模板（单句长串）",
    "v2": "分 6 个语义段 + 23 套专属色板",
    "v3": "加入「不要有背景环境」（实测翻车，背景变玻璃窗）",
    "v4": "否定句全部移出正向提示词",
    "v5": "去掉「标签」等产品联想词（避免吊灯牌形状）",
    "v6": "去掉指令式数量词 + 程序化背景刷白",
    "v7": "彩色扁平插画 + 三色板 + 质量递进声明（当前生产版）",
    "v8": "参考图驱动：异形轮廓、标题主视觉、行业主物件与多构图模板（当前生产版）",
}


def current_version(items: list[dict]) -> str:
    """从数据中读取当前版本号。"""
    for it in items:
        v = it.get("prompt_version")
        if v:
            return v
    return "v1"


def available_versions(items: list[dict]) -> list[str]:
    """返回数据里实际存在的版本列表（新 → 旧）。"""
    found: set[str] = set()
    for it in items:
        for k in it:
            m = _SUFFIX_RE.match(k)
            if m:
                found.add(m.group(1))

    ordered = sorted(found, key=lambda v: int(v[1:]), reverse=True)
    cur = current_version(items)
    # 当前版本置顶（它存在无后缀字段里）
    return [cur] + [v for v in ordered if v != cur]


def get_prompt(item: dict, version: str) -> tuple[str, str]:
    """取出指定版本的 (正向, 负向) 提示词。

    取不到时回退到当前版本。
    """
    cur = item.get("prompt_version", "")
    if not version or version in ("current", cur):
        return item.get("positive_prompt", ""), item.get("negative_prompt", "")

    pos = item.get(f"positive_prompt_{version}")
    neg = item.get(f"negative_prompt_{version}")
    if pos:
        return pos, neg or item.get("negative_prompt", "")
    return item.get("positive_prompt", ""), item.get("negative_prompt", "")


def version_summary(items: list[dict]) -> list[dict]:
    """给界面用的版本清单：[{version, note, is_current, available}]。"""
    cur = current_version(items)
    versions = available_versions(items)
    out = []
    for v in versions:
        out.append({
            "version": v,
            "note": VERSION_NOTES.get(v, ""),
            "is_current": v == cur,
            "available": True,
        })
    # 补充尚未生成数据的版本说明
    for v, note in VERSION_NOTES.items():
        if v not in versions:
            out.append({"version": v, "note": note, "is_current": False, "available": False})
    return out


def preview(item: dict, version: str, max_len: int = 400) -> str:
    """取某版本的提示词预览（截断）。"""
    pos, _ = get_prompt(item, version)
    return pos if len(pos) <= max_len else pos[:max_len] + "…"
