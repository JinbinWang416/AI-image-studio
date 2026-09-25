# -*- coding: utf-8 -*-
"""用 PyInstaller 把项目打包成 Windows 桌面 exe（单目录模式）。

用法：
    python build_desktop.py

产物：
    desktop/dist/门店贴纸图片生成智能体/门店贴纸图片生成智能体.exe

说明：
- 采用 --onedir（单目录，启动快、排错易）。若坚持要"单个 exe 文件"，
  把下方 --onedir 改成 --onefile 即可（代价：每次启动需把 ~1GB 依赖解压到临时目录，较慢）。
- 只读资源 data/、assets/、app/web/static 会打进 _MEIPASS，运行时由 app/paths.py 正确解析。
- 依赖很重（opencv / onnxruntime / rembg），产物较大（约数百 MB ~ 1GB+），构建耗时 5~15 分钟。
"""
from __future__ import annotations

import os
import sys

import PyInstaller.__main__  # noqa: F401

SEP = os.pathsep  # Windows 下为 ';'

# 只读资源 -> 打进 _MEIPASS（运行时对应 PACKAGE_ROOT）
# 注意：必须传绝对路径。PyInstaller 的 --specpath 会让相对路径以 spec 所在目录为基准，
# 导致找不到资源。字体不走 assets/（professional_local.py 直接用 C:\Windows\Fonts 雅黑），
# 故此处只打包 data 与前端静态目录。
_BASE = os.path.dirname(os.path.abspath(__file__))
ADD_DATA = [
    (os.path.join(_BASE, "data"), "data"),
    (os.path.join(_BASE, "app", "web", "static"), "app/web/static"),
]


def main() -> None:
    sep = SEP
    add_data_args: list[str] = []
    for src, dst in ADD_DATA:
        add_data_args += ["--add-data", f"{src}{sep}{dst}"]

    opts = [
        "run_desktop.py",
        "--name=门店贴纸图片生成智能体",
        "--onedir",
        "--windowed",   # 无控制台窗口，桌面应用观感
        "--noupx",
        "--distpath", "desktop/dist",
        "--workpath", "desktop/build",
        "--specpath", "desktop",
        # uvicorn 的懒加载子模块
        "--hidden-import", "uvicorn",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.loops.uvloop",
        "--hidden-import", "uvicorn.loops.asyncio",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.http.h11_impl",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.auto",
        "--hidden-import", "uvicorn.middleware.proxy_headers",
        # tkinter（桌面窗口 + 目录选择框）
        "--hidden-import", "tkinter",
        "--hidden-import", "tkinter.filedialog",
        "--hidden-import", "tkinter.messagebox",
        # 其它显式依赖
        "--hidden-import", "argon2",
        "--hidden-import", "pyotp",
        "--hidden-import", "cryptography",
        "--hidden-import", "httpx",
        "--hidden-import", "starlette",
        "--hidden-import", "fastapi",
        # 重依赖：整包收集，避免漏掉动态导入的模块/数据
        "--collect-all", "rembg",
        "--collect-all", "onnxruntime",
        "--collect-all", "cv2",
        "--collect-all", "numpy",
        "--collect-all", "PIL",
        # 排除无关 GUI 框架，减小体积
        "--exclude-module", "PyQt5",
        "--exclude-module", "PySide2",
        "--exclude-module", "PyQt6",
    ]
    opts += add_data_args
    PyInstaller.__main__.run(opts)


if __name__ == "__main__":
    main()
