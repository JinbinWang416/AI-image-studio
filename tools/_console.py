# -*- coding: utf-8 -*-
"""让脚本在 Windows 默认（GBK）控制台下也能安全打印 emoji 与中文。

## 问题

Windows 默认控制台代码页是 GBK（cp936），而本项目大量脚本会用
`print("✅ ...")` 汇报结果 —— 这些字符编不进 GBK，于是：

    UnicodeEncodeError: 'gbk' codec can't encode character '\u2705'

**整个脚本会因此以退出码 1 结束**，看起来像「自检失败」，实际只是打印失败。
按 README 原样执行 `python tools/run_selfcheck.py` 就会撞上（实测复现）。

## 用法

在脚本的 import 段之后加两行：

    from _console import enable_safe_output
    enable_safe_output()

`tools/` 下的脚本是独立入口，`sys.path` 已含本目录，直接 import 即可。

## 设计取舍

只改 `errors`，**不改 `encoding`**：

- 若把 encoding 强改成 `utf-8`，在 GBK 控制台里会显示成乱码（虽然不报错）
- 保持原编码 + `errors="replace"` 至少能读：编不出的字符显示成 `?`

真正的根治办法是让终端本身用 UTF-8（`chcp 65001`），但那不该由脚本
替用户决定，所以这里只做「不炸」这一层保证。
"""
from __future__ import annotations

import sys


def enable_safe_output() -> None:
    """把 stdout/stderr 的编码错误处理改成 replace，避免打印 emoji 时崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")      # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            # 老版本 Python 没有 reconfigure；或流已被重定向成不支持的对象
            pass


# 导入即生效，方便只想 `import _console` 的场景
enable_safe_output()
