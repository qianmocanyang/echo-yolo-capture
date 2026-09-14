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
from PySide6.QtGui import QGuiApplication, QImage, QPixmap

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
def _cached_thumb_image(path_str: str, target: int, mtime: float, dpr: float) -> QImage:
    """按 (路径, 目标尺寸, 修改时间, 缩放比) 缓存**缩放后的 QImage**。

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

    # target 是**逻辑**尺寸。125% 缩放的屏幕上，按逻辑尺寸解码出来的图
    # 会被系统放大 1.25 倍画出来，细节直接糊掉——所以按物理像素解码
    # （140 → 175），再把 DPR 写回图像，Qt 就会按逻辑尺寸摆放。
    size = max(1, int(round(target * dpr)))
    scaled = image.scaled(
        QSize(size, size),
        Qt.KeepAspectRatio,
        Qt.SmoothTransformation,
    )
    if dpr != 1.0:
        scaled.setDevicePixelRatio(dpr)
    return scaled


def screen_dpr() -> float:
    """主屏缩放比（125% 缩放 → 1.25）。

    QScreen 不是线程安全的，所以只允许在 GUI 线程调用；
    后台线程要用的值由调用方在主线程先算好再传进去。
    """
    app = QGuiApplication.instance()
    if app is None:
        return 1.0
    screen = app.primaryScreen()
    if screen is None:
        return 1.0
    return max(1.0, float(screen.devicePixelRatio()))


def thumbnail_image(path: Path, target: int = 128, dpr: float = 1.0) -> QImage:
    """读取缩略图（QImage）。**可以在工作线程里安全调用**。

    dpr 由调用方从 GUI 线程取好传进来（见 :func:`screen_dpr`），
    这里不自己去查屏幕。文件缺失或损坏时返回空 QImage，由界面画占位。
    """
    try:
        stat = path.stat()
    except OSError:
        return QImage()
    return _cached_thumb_image(str(path), target, stat.st_mtime, max(1.0, float(dpr)))


def thumbnail(path: Path, target: int = 128, dpr: float = 0.0) -> QPixmap:
    """读取缩略图（QPixmap）。只能在 GUI 线程调用。

    ``dpr=0`` 表示自动按当前主屏缩放比解码（推荐）；显式传值用于
    已知目标屏幕的场合。走的是同一份 QImage 缓存，界面线程热路径上
    几乎不需要解码，只剩一次 QPixmap.fromImage 的拷贝。
    """
    scale = dpr if dpr > 0 else screen_dpr()
    image = thumbnail_image(path, target, scale)
    return QPixmap.fromImage(image) if not image.isNull() else QPixmap()


def clear_thumbnail_cache() -> None:
    _cached_thumb_image.cache_clear()


def placeholder(target: int, color: str) -> QPixmap:
    """文件缺失时的占位图。画一个浅色方块，避免界面出现空洞。"""
    pixmap = QPixmap(target, target)
    pixmap.fill(Qt.transparent)
    return pixmap
