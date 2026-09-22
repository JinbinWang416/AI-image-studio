# -*- coding: utf-8 -*-
"""桌面启动器：在本地启动 FastAPI 服务并自动打开浏览器。

设计要点：
- 绑定 127.0.0.1（本机回环地址）。main.py 的安全护栏在监听本机地址时直接放行，
  因此桌面模式**不需要** --i-know-its-public，也不会在无账号时被拦。
- 默认 PROVIDER=mock，零配置、离线即可跑通整套界面与生成流程。
- 弹出一个轻量 Tk 窗口，提供「打开应用 / 退出」；关闭窗口即停服务。
- 既可直接 `python run_desktop.py` 本地体验，也可作为 PyInstaller 打包的入口。

路径说明（详见 app/paths.py）：
- 冻结后 PACKAGE_ROOT = sys._MEIPASS（打包进去的只读资源：data/、assets/、app/web/static）
- 冻结后 STATE_ROOT = %LOCALAPPDATA%/门店贴纸图片生成智能体（出图、API Key、日志落本机用户目录）
"""
from __future__ import annotations

import os
import sys
import time
import socket
import threading
import webbrowser

# 桌面模式默认 mock：免 API Key、离线可用
os.environ.setdefault("PROVIDER", "mock")

try:
    import tkinter as tk
except Exception:  # noqa: BLE001 - 极少数无 tk 环境兜底
    tk = None

from app.logging_setup import setup_logging
from app.web.server import app
import uvicorn

HOST = "127.0.0.1"
PORT = int(os.environ.get("APP_PORT", "8000"))


def find_free_port(host: str, start: int) -> int:
    """从 start 起找一个空闲端口，避免 8000 被占用时启动失败。"""
    for p in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return start


def wait_ready(host: str, port: int, timeout: float = 30.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/", timeout=1):
                return True
        except Exception:
            time.sleep(0.3)
    return False


class _DesktopUI:
    """无显示异常时的兜底：纯控制台跑。"""

    def __init__(self, server: "uvicorn.Server", port: int) -> None:
        self.server = server
        self.port = port
        self.root = tk.Tk()
        self.root.title("门店贴纸图片生成智能体 · 本地服务")
        self.root.geometry("400x180")
        self.root.resizable(False, False)
        self.label = tk.Label(
            self.root, text="正在启动本地服务…", padx=20, pady=14, justify="left"
        )
        self.label.pack()
        self.btn_open = tk.Button(
            self.root, text="打开应用", command=self.open_browser, width=18
        )
        self.btn_open.pack(pady=4)
        self.btn_quit = tk.Button(
            self.root, text="退出", command=self.quit, width=18
        )
        self.btn_quit.pack(pady=2)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        self.server.run()
        # run() 返回代表服务已停止
        try:
            self.root.after(0, self.root.destroy)
        except Exception:
            pass

    def open_browser(self) -> None:
        webbrowser.open(f"http://{HOST}:{self.port}")

    def _on_ready(self) -> None:
        self.label.config(
            text=f"服务已启动\nhttp://{HOST}:{self.port}\n浏览器已自动打开"
        )
        self.open_browser()

    def run(self) -> None:
        def poll() -> None:
            if wait_ready(HOST, self.port):
                self.root.after(0, self._on_ready)
            else:
                self.root.after(500, poll)

        self.root.after(300, poll)
        self.root.mainloop()

    def quit(self) -> None:
        try:
            self.server.should_exit = True
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)


def main() -> None:
    setup_logging()
    port = find_free_port(HOST, PORT)
    config = uvicorn.Config(app, host=HOST, port=port, log_level="info")
    server = uvicorn.Server(config)

    if tk is None:
        threading.Thread(target=server.run, daemon=True).start()
        url = f"http://{HOST}:{port}"
        print(f"服务已启动：{url}")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.should_exit = True
        return

    _DesktopUI(server, port).run()


if __name__ == "__main__":
    main()
