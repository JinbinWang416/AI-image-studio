# -*- coding: utf-8 -*-
"""
读取 .docx 的纯文本内容（不依赖 python-docx，直接解析 OOXML）。

用法：
    .\\.venv\\Scripts\\python.exe tools\\read_docx.py <文件路径> [输出路径]
"""
from __future__ import annotations

import pathlib
import re
import sys
import zipfile


def docx_text(path: pathlib.Path) -> str:
    with zipfile.ZipFile(path) as z:
        try:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        except KeyError:
            return "（无法读取 word/document.xml）"

    # 段落 → 换行；表格单元格 → 制表符分隔
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"</w:tc>", "\t", xml)
    xml = re.sub(r"</w:tr>", "\n", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    # 去掉所有标签
    text = re.sub(r"<[^>]+>", "", xml)
    # 实体还原
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&apos;", "'")):
        text = text.replace(a, b)
    # 压缩多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python tools/read_docx.py <文件路径> [输出路径]")
        return 1
    src = pathlib.Path(sys.argv[1])
    if not src.is_file():
        print(f"❌ 文件不存在：{src}")
        return 1
    text = docx_text(src)
    if len(sys.argv) >= 3:
        out = pathlib.Path(sys.argv[2])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"✅ 已提取 {len(text)} 字符 → {out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
