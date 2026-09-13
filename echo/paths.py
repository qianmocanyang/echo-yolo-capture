"""应用级路径。

配置与日志放在 %APPDATA%\\echo 下；图片数据一律放在用户选择的保存目录，
不写入应用目录，便于整目录搬移与训练机直接读取。

设置环境变量 ``ECHO_DATA_DIR`` 可把配置与日志改到指定目录——便携模式、
多套配置并行、以及自动化冒烟测试都靠它，不去动用户真实的 ``%APPDATA%``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME


def app_data_dir() -> Path:
    """%APPDATA%\\echo，用于存放 config.json 与日志。

    若设置了 ``ECHO_DATA_DIR``，则以该目录为准。
    """
    override = (os.environ.get("ECHO_DATA_DIR") or "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return app_data_dir() / "config.json"


def log_dir() -> Path:
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的目录分发模式中。"""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """打包后为 exe 所在目录，开发时为仓库根目录。

    用途是「用户能看见、能搬走」的位置——日志、导出、快捷方式指向。
    **只读资源不要用这个**，见 ``resource_root()``。
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """只读资源（图标等）所在目录。

    PyInstaller 6.0 起，``datas`` 默认被收进 exe 旁边的 ``_internal/``
    子目录，不再平铺在 exe 旁边；``sys._MEIPASS`` 指向的正是这个目录。
    资源路径必须走这里，否则打包后必然找不到文件——而源码运行时一切正常，
    属于只在打包后才炸的那类问题。
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent
