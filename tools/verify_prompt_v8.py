# -*- coding: utf-8 -*-
"""验证 V8 提示词数据、历史回退和参考图驱动的构图覆盖。"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.prompt.templates import get_prompt, version_summary  # noqa: E402


def main() -> int:
    stores = json.loads((ROOT / "data" / "stores.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    layouts: set[str] = set()
    forbidden = ("不要", "不得", "禁止", "贴纸", "标签", "吊牌", "玻璃", "个汉字", "这组")

    for store in stores:
        for item in store["items"]:
            tag = f"{store['folder_index']}/{item['pic_index']}"
            pos, neg = get_prompt(item, "v8")
            if item.get("prompt_version") != "v8":
                errors.append(f"{tag} 生产版本不是 v8")
            for term in (store["main_title"], store["sub_title"], item["subject"], "1:1", "自由曲线圆角异形外轮廓"):
                if term not in pos:
                    errors.append(f"{tag} 缺少 {term}")
            for term in forbidden:
                if term in pos:
                    errors.append(f"{tag} 正向含禁用词 {term}")
            for term in ("拼图", "真实品牌logo", "错误文字"):
                if term not in neg:
                    errors.append(f"{tag} 负向缺少 {term}")
            if not item.get("positive_prompt_v7") or not item.get("negative_prompt_v7"):
                errors.append(f"{tag} 缺少 V7 历史提示词")
            layouts.add(item["pic_index"])

    versions = {row["version"]: row for row in version_summary(stores[0]["items"])}
    if not versions.get("v8", {}).get("is_current"):
        errors.append("版本管理未将 V8 标记为当前版本")
    if len(layouts) != 6:
        errors.append(f"构图模板覆盖数为 {len(layouts)}，应为 6")

    print(f"V8 提示词验证：{len(stores)} 套门店 / {sum(len(s['items']) for s in stores)} 条提示词")
    print(f"当前版本：{versions.get('v8', {}).get('version')}；V7 历史回退：已检查")
    print(f"构图模板：{len(layouts)} 种")
    if errors:
        for error in errors[:20]:
            print("❌", error)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
