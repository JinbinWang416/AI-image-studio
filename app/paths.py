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
    """返回源码根目录或 PyInstaller 解包后的只读资源目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


PACKAGE_ROOT = _package_root()


def _state_root() -> Path:
    """返回保存 API Key、日志和出图的目录。"""
    if not getattr(sys, "frozen", False):
        return PACKAGE_ROOT
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / APP_NAME


STATE_ROOT = _state_root()
