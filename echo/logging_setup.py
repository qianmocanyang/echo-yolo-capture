"""日志初始化：控制台 + 轮转文件。

方案要求"崩溃后扫描残留临时文件和未登记原图，提供恢复"，所以日志需要落在磁盘上，
并按天轮转，方便定位长时间运行中的问题。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .paths import log_dir

_LOGGER_NAME = "echo"
_configured = False

_FMT = "%(asctime)s [%(levelname)7s] %(name)-22s | %(message)s"


def setup_logging(level: int = logging.INFO, to_console: bool = True) -> logging.Logger:
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    formatter = logging.Formatter(_FMT, datefmt="%Y-%m-%d %H:%M:%S")

    try:
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_dir() / "echo.log",
            when="midnight",
            backupCount=7,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # 日志写不进去不能拖垮应用本身。
        pass

    if to_console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(level)
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    _configured = True
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")
