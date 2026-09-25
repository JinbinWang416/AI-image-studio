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
from pathlib import Path

# ⚠️ **必须在任何第三方库被 import 之前修好 stdout/stderr。**
#
#    PyInstaller 的 `--windowed` 模式**没有控制台**，于是 `sys.stdout` 与
#    `sys.stderr` 都是 `None`。而 uvicorn 的默认日志格式器会调用
#    `sys.stdout.isatty()`：
#
#        AttributeError: 'NoneType' object has no attribute 'isatty'
#        → ValueError: Unable to configure formatter 'default'
#
#    现象就是「双击图标，窗口闪一下没了」，而且 `--windowed` 下**没有任何输出**
#    可看。实测排查了很久，最后靠崩溃日志才定位到。
#
#    这里给它们接一个 os.devnull —— 文件对象自带 `isatty()`（返回 False），
#    所以任何写 stdout/stderr 的库都不会再崩。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115

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


def _write_crash_log(exc: BaseException) -> str:
    """把启动失败的现场写到用户数据目录，返回文件路径。

    ⚠️ 打包成桌面版时用的是 ``--windowed``（**没有控制台**），
       一旦启动过程中抛异常，用户看到的现象就是「双击图标，窗口闪一下没了」，
       完全不知道为什么。这个函数就是为了让那种情况留下可查的证据。

       故意**不依赖** logging（可能还没初始化完），直接写文件；
       路径也刻意避开 STATE_ROOT 的创建逻辑，失败时退回临时目录。
    """
    import traceback
    from datetime import datetime

    try:
        from app.paths import STATE_ROOT

        base = Path(STATE_ROOT) / "logs"
        base.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 - 连路径都拿不到就退回临时目录
        import tempfile

        base = Path(tempfile.gettempdir())

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = base / f"desktop-crash-{stamp}.txt"
    try:
        target.write_text(
            "桌面版启动失败\n"
            f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"Python：{sys.version}\n"
            f"frozen：{getattr(sys, 'frozen', False)}\n"
            f"可执行文件：{sys.executable}\n"
            f"MEIPASS：{getattr(sys, '_MEIPASS', '(无)')}\n"
            f"工作目录：{os.getcwd()}\n"
            f"异常类型：{type(exc).__name__}\n"
            f"异常信息：{exc}\n"
            "\n---- 调用栈 ----\n"
            + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            encoding="utf-8",
        )
        return str(target)
    except OSError:
        return ""


if __name__ == "__main__":
    # ⚠️ 桌面版是「双击运行」的：异常必须留下证据，不能静默退出。
    #    实测踩过：打包后双击毫无反应，而 --windowed 模式没有任何输出可看。
    try:
        main()
    except BaseException as _exc:  # noqa: BLE001 - 包括 SystemExit/KeyboardInterrupt
        _path = _write_crash_log(_exc)
        if _path:
            try:
                import tkinter.messagebox as _mb
                import tkinter as _tk

                _r = _tk.Tk()
                _r.withdraw()
                _mb.showerror(
                    "启动失败",
                    f"程序启动时出错：\n\n{type(_exc).__name__}: {_exc}\n\n"
                    f"详细信息已写入：\n{_path}",
                )
                _r.destroy()
            except Exception:  # noqa: BLE001 - 连弹窗都失败就算了
                pass
        raise

