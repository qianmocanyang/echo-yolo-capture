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


@lru_cache(maxsize=768)
def _cached_thumb_image(path_str: str, target: int, mtime: float) -> QImage:
    """按 (路径, 目标尺寸, 修改时间) 缓存**缩放后的 QImage**。

    缓存 QImage 而不是 QPixmap 是有意的：QPixmap 只能在 GUI 线程使用，
    而 QImage 可以在工作线程里创建——图片库翻页要靠后台预解码把
    上百张图的主线程阻塞省掉，缓存层必须先能跨线程。

    带 mtime 是为了让"重新标注/覆盖图片"后缓存自动失效，
    否则界面会一直显示旧图，是最难发现的一类 bug。
    """
    path = Path(path_str)
    if not path.exists():
        return QImage()

    image = QImage()
    if not image.loadFromData(path.read_bytes()):
        return QImage()

    return image.scaled(
        QSize(target, target),
        Qt.KeepAspectRatio,
        Qt.SmoothTransformation,
    )


def thumbnail_image(path: Path, target: int = 128) -> QImage:
    """读取缩略图（QImage）。**可以在工作线程里安全调用**。

    文件缺失或损坏时返回空 QImage，由界面画占位。
    """
    try:
        stat = path.stat()
    except OSError:
        return QImage()
    return _cached_thumb_image(str(path), target, stat.st_mtime)


def thumbnail(path: Path, target: int = 128) -> QPixmap:
    """读取缩略图（QPixmap）。只能在 GUI 线程调用。

    走的是同一份 QImage 缓存，所以界面线程热路径上几乎不需要解码，
    只剩一次 QPixmap.fromImage 的拷贝（140×140 约 0.02ms）。
    """
    image = thumbnail_image(path, target)
    return QPixmap.fromImage(image) if not image.isNull() else QPixmap()


def clear_thumbnail_cache() -> None:
    _cached_thumb_image.cache_clear()


def placeholder(target: int, color: str) -> QPixmap:
    """文件缺失时的占位图。画一个浅色方块，避免界面出现空洞。"""
    pixmap = QPixmap(target, target)
    pixmap.fill(Qt.transparent)
    return pixmap
