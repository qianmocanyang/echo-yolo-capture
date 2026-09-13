"""图像转换与缩略图缓存。

numpy ↔ QImage 的转换看着简单，但有两个必须踩过的坑：

1. ``QImage`` 不会复制传入的缓冲区，而 numpy 数组一旦被回收，
   界面上就会显示成花屏或直接崩溃。所以 :func:`to_qimage` 一律 ``copy()``。
2. OpenCV 是 BGR，Qt 是 RGB。用 ``Format_BGR888`` 让 Qt 自己做通道重排，
   比先 ``cvtColor`` 再构造 QImage 少一次整图拷贝。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QImage, QPixmap

from ..logging_setup import get_logger

log = get_logger("ui.imaging")


def to_qimage(image: np.ndarray) -> QImage:
    """numpy BGR/BGRA/GRAY → QImage。总是产生独立副本。"""
    if image is None or image.size == 0:
        return QImage()

    array = np.ascontiguousarray(image)
    height, width = array.shape[:2]

    if array.ndim == 2:
        return QImage(
            array.data, width, height, width, QImage.Format_Grayscale8
        ).copy()
    channels = array.shape[2]
    if channels == 3:
        return QImage(
            array.data, width, height, width * 3, QImage.Format_BGR888
        ).copy()
    if channels == 4:
        return QImage(
            array.data, width, height, width * 4, QImage.Format_RGBA8888
        ).copy()
    return QImage()


def to_pixmap(image: np.ndarray) -> QPixmap:
    qimage = to_qimage(image)
    return QPixmap.fromImage(qimage) if not qimage.isNull() else QPixmap()


@lru_cache(maxsize=512)
def _cached_thumb(path_str: str, target: int, mtime: float) -> QPixmap:
    """按 (路径, 目标尺寸, 修改时间) 缓存缩略图。

    带 mtime 是为了让"重新标注/覆盖图片"后缓存自动失效，
    否则界面会一直显示旧图，是最难发现的一类 bug。
    """
    path = Path(path_str)
    if not path.exists():
        return QPixmap()

    image = QImage()
    if not image.loadFromData(path.read_bytes()):
        return QPixmap()

    scaled = image.scaled(
        QSize(target, target),
        Qt.KeepAspectRatio,
        Qt.SmoothTransformation,
    )
    return QPixmap.fromImage(scaled)


def thumbnail(path: Path, target: int = 128) -> QPixmap:
    """读取缩略图。文件缺失或损坏时返回空 QPixmap，由界面画占位。"""
    try:
        stat = path.stat()
    except OSError:
        return QPixmap()
    pixmap = _cached_thumb(str(path), target, stat.st_mtime)
    return pixmap


def clear_thumbnail_cache() -> None:
    _cached_thumb.cache_clear()


def placeholder(target: int, color: str) -> QPixmap:
    """文件缺失时的占位图。画一个浅色方块，避免界面出现空洞。"""
    pixmap = QPixmap(target, target)
    pixmap.fill(Qt.transparent)
    return pixmap
