# -*- coding: utf-8 -*-
"""资源与可写用户数据目录。

普通源码运行时，资源与生成数据都在项目目录中。PyInstaller 冻结后，
资源位于应用安装目录，而 API 设置、日志与出图必须留在用户的
``%LOCALAPPDATA%`` 中，避免升级安装程序时被覆盖。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "门店贴纸图片生成智能体"


def _package_root() -> Path:
    """返回源码根目录或 PyInstaller 解包后的只读资源目录。

    ⚠️ **不要用 `Path(__file__).parent.parent`** 这种「按文件深度上溯」的写法 ——
       Phase 1 把本文件从 `app/paths.py` 移到了 `app/core/paths.py`，
       深度一变，`parent.parent` 就从「项目根」变成了「app/」，
       导致 `data/`、`output/`、`config/` 全部指错（实测踩到，24 个测试报
       `数据文件不存在：.../app/data/stores.json`）。

       正确做法是**锚定 `app` 包**：不管本文件在哪一层，
       `app` 包的位置是确定的，项目根就是它的上一级。
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]

    # 通过 app 包定位，与当前文件的层级无关
    try:
        import app as _app_pkg

        pkg_file = getattr(_app_pkg, "__file__", "") or ""
        if pkg_file:
            return Path(pkg_file).resolve().parent.parent
    except Exception:  # noqa: BLE001 - 极端情况下退回上溯
        pass

    # 兜底：app/core/paths.py → 上溯三级到项目根
    return Path(__file__).resolve().parent.parent.parent


PACKAGE_ROOT = _package_root()


def _state_root() -> Path:
    """返回保存 API Key、日志和出图的目录。"""
    if not getattr(sys, "frozen", False):
        return PACKAGE_ROOT
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / APP_NAME


STATE_ROOT = _state_root()
