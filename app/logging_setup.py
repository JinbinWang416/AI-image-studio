# -*- coding: utf-8 -*-
"""
日志系统：控制台 + 文件双输出。

文件日志按运行批次命名（run_YYYYMMDD_HHMMSS.log），便于事后追溯。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import LOG_DIR

_FMT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"
_DATEFMT = "%H:%M:%S"

_configured = False
_log_file: Path | None = None


def setup_logging(level: int = logging.INFO, log_dir: Path | None = None) -> Path:
    """初始化日志系统，返回本次运行的日志文件路径。"""
    global _configured, _log_file
    if _configured and _log_file:
        return _log_file

    log_dir = Path(log_dir or LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_file = log_dir / f"run_{stamp}.log"

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # 控制台。PyInstaller 的 --noconsole 桌面版没有 stdout，
    # 此时仅写 UTF-8 文件日志，避免日志处理器在首次生成时抛错。
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
        root.addHandler(console)

    # 文件（UTF-8，保留完整时间戳）
    file_handler = logging.FileHandler(_log_file, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s")
    )
    root.addHandler(file_handler)

    # 降噪
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    logging.getLogger("app").info("日志文件：%s", _log_file)
    return _log_file


def get_logger(name: str) -> logging.Logger:
    """获取带应用前缀的 logger。"""
    if not name.startswith("app"):
        name = f"app.{name}"
    return logging.getLogger(name)


def current_log_file() -> Path | None:
    return _log_file
