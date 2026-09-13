"""线性 SVG 图标集。

方案 §7.1 要求「图标采用同一套线性 SVG 风格，避免混用表情符号或不同笔画粗细」。
所以这里所有图标都是同一个模子：24×24 视框、无填充、圆头圆角、
统一 1.7 的描边宽度、同一套端点样式。视觉重量因此保持一致，
不会出现某个图标看起来比别的"重"。

用 SVG 而不是位图的原因很简单：需要按主题色和 DPI 动态着色与缩放，
位图做不到，而 QSvgRenderer 是 PySide6 自带的能力，不引入新依赖。
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# 每个条目是若干 SVG 图元。统一 24×24 视框、居中、留 2px 安全边距。
_ICON_DEFS: dict[str, str] = {
    # ---- 导航 ----
    "capture": (
        '<path d="M4 9V6.6A1.6 1.6 0 0 1 5.6 5H8"/>'
        '<path d="M16 5h2.4A1.6 1.6 0 0 1 20 6.6V9"/>'
        '<path d="M20 15v2.4a1.6 1.6 0 0 1-1.6 1.6H16"/>'
        '<path d="M8 19H5.6A1.6 1.6 0 0 1 4 17.4V15"/>'
        '<circle cx="12" cy="12" r="4"/>'
    ),
    "library": (
        '<rect x="3.5" y="3.5" width="7" height="7" rx="1.6"/>'
        '<rect x="13.5" y="3.5" width="7" height="7" rx="1.6"/>'
        '<rect x="3.5" y="13.5" width="7" height="7" rx="1.6"/>'
        '<rect x="13.5" y="13.5" width="7" height="7" rx="1.6"/>'
    ),
    "settings": (
        '<path d="M4 7h9"/><path d="M17 7h3"/><circle cx="15" cy="7" r="2"/>'
        '<path d="M4 17h3"/><path d="M11 17h9"/><circle cx="9" cy="17" r="2"/>'
    ),
    # ---- 播放控制 ----
    "play": '<path d="M8.5 5.6v12.8l10-6.4z"/>',
    "pause": '<path d="M9 5.5v13"/><path d="M15 5.5v13"/>',
    "stop": '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
    "burst": (
        '<path d="M4 8.5h6"/><path d="M4 12h9"/><path d="M4 15.5h6"/>'
        '<path d="M17.5 6.5v11"/><path d="M20.5 9v6"/>'
    ),
    "timer": (
        '<circle cx="12" cy="13" r="7.5"/>'
        '<path d="M12 9.5V13l2.5 1.8"/>'
        '<path d="M9.5 3.5h5"/>'
    ),
    # ---- 采集源 ----
    "monitor": (
        '<rect x="3" y="4.5" width="18" height="12" rx="2"/>'
        '<path d="M9 20h6"/><path d="M12 16.5V20"/>'
    ),
    "window": (
        '<rect x="4" y="4.5" width="16" height="15" rx="2"/>'
        '<path d="M4 9h16"/>'
        '<circle cx="7" cy="6.8" r=".6"/>'
    ),
    "crosshair": (
        '<circle cx="12" cy="12" r="7"/>'
        '<path d="M12 2.5v4"/><path d="M12 17.5v4"/>'
        '<path d="M2.5 12h4"/><path d="M17.5 12h4"/>'
    ),
    "drag": (
        '<rect x="6" y="6" width="12" height="12" rx="2" stroke-dasharray="3 2.4"/>'
        '<path d="M12 3.5V6"/><path d="M12 18v2.5"/>'
        '<path d="M3.5 12H6"/><path d="M18 12h2.5"/>'
    ),
    "coords": (
        '<path d="M4.5 19.5 10 14"/>'
        '<path d="M14 4.5h5.5V10"/>'
        '<path d="M19.5 4.5 13 11"/>'
        '<circle cx="6" cy="18" r="1.6"/>'
    ),
    # ---- 文件 ----
    "folder": (
        '<path d="M3.5 7.6A1.6 1.6 0 0 1 5.1 6h3.6l2 2.4h8.2a1.6 1.6 0 0 1 1.6 1.6v7.4'
        'a1.6 1.6 0 0 1-1.6 1.6H5.1a1.6 1.6 0 0 1-1.6-1.6z"/>'
    ),
    "folder_open": (
        '<path d="M3.5 18.4V7.6A1.6 1.6 0 0 1 5.1 6h3.6l2 2.4h8.2'
        'a1.6 1.6 0 0 1 1.6 1.6v1.4"/>'
        '<path d="M3.5 18.4 6.2 12h15.3l-2.7 6.4z"/>'
    ),
    "image": (
        '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/>'
        '<circle cx="9" cy="10" r="1.6"/>'
        '<path d="M4.5 17.5 10 13l3.5 2.6L17 12l3 3.5"/>'
    ),
    "download": (
        '<path d="M12 4v11"/>'
        '<path d="M7.8 10.8 12 15l4.2-4.2"/>'
        '<path d="M4.5 19.5h15"/>'
    ),
    "upload": (
        '<path d="M12 15V4"/>'
        '<path d="M7.8 8.2 12 4l4.2 4.2"/>'
        '<path d="M4.5 19.5h15"/>'
    ),
    "refresh": (
        '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/>'
        '<path d="M19.8 4.5v4.6h-4.6"/>'
    ),
    "trash": (
        '<path d="M5.5 7.5h13"/>'
        '<path d="M10 7.5V5.6A1.1 1.1 0 0 1 11.1 4.5h1.8A1.1 1.1 0 0 1 14 5.6v1.9"/>'
        '<path d="M7 7.5l.9 10.4A1.6 1.6 0 0 0 9.5 19.4h5a1.6 1.6 0 0 0 1.6-1.5L17 7.5"/>'
    ),
    # ---- 状态 ----
    "check": '<path d="M5 12.5 9.5 17 19 7"/>',
    "close": '<path d="M6.5 6.5 17.5 17.5"/><path d="M17.5 6.5 6.5 17.5"/>',
    "warning": (
        '<path d="M12 4.6 20.4 19H3.6z"/>'
        '<path d="M12 10v4"/><circle cx="12" cy="16.6" r=".7"/>'
    ),
    "info": (
        '<circle cx="12" cy="12" r="8"/>'
        '<path d="M12 11v5"/><circle cx="12" cy="8.2" r=".7"/>'
    ),
    "eye": (
        '<path d="M2.8 12S6.4 5.8 12 5.8 21.2 12 21.2 12 17.6 18.2 12 18.2 2.8 12 2.8 12z"/>'
        '<circle cx="12" cy="12" r="2.8"/>'
    ),
    "eye_off": (
        '<path d="M4 4l16 16"/>'
        '<path d="M8.4 6.5A9.6 9.6 0 0 1 12 5.8c5.6 0 9.2 6.2 9.2 6.2a17 17 0 0 1-3 3.7"/>'
        '<path d="M6.1 8.2A17.6 17.6 0 0 0 2.8 12S6.4 18.2 12 18.2a9.4 9.4 0 0 0 3.3-.6"/>'
        '<path d="M9.6 10.2a2.8 2.8 0 0 0 4 4"/>'
    ),
    "duplicate": (
        '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/>'
        '<path d="M15.5 8.5V6.6A1.6 1.6 0 0 0 13.9 5H6.1A1.6 1.6 0 0 0 4.5 6.6v7.8'
        'a1.6 1.6 0 0 0 1.6 1.6h1.9"/>'
    ),
    "keyboard": (
        '<rect x="2.8" y="6.5" width="18.4" height="11" rx="2"/>'
        '<path d="M6.5 10h.01"/><path d="M9.6 10h.01"/><path d="M12.7 10h.01"/>'
        '<path d="M15.8 10h.01"/><path d="M8 14h8"/>'
    ),
    "target": (
        '<circle cx="12" cy="12" r="8.2"/>'
        '<circle cx="12" cy="12" r="4"/>'
        '<circle cx="12" cy="12" r=".9"/>'
    ),
    "layers": (
        '<path d="M12 3.6 20 8l-8 4.4L4 8z"/>'
        '<path d="M4 12.4 12 16.8l8-4.4"/>'
        '<path d="M4 16.4 12 20.8l8-4.4"/>'
    ),
    "chevron_down": '<path d="M7 10l5 5 5-5"/>',
    "chevron_right": '<path d="M10 7l5 5-5 5"/>',
    "plus": '<path d="M12 5.5v13"/><path d="M5.5 12h13"/>',
    "wand": (
        '<path d="M4.5 19.5 15 9"/>'
        '<path d="M13.6 7.6 16.4 10.4"/>'
        '<path d="M17.5 4.5v3"/><path d="M16 6h3"/>'
        '<path d="M19 14.5v2"/><path d="M18 15.5h2"/>'
    ),
    "shield": (
        '<path d="M12 3.8 19 6.6v5.1c0 4-3 7.3-7 8.5-4-1.2-7-4.5-7-8.5V6.6z"/>'
        '<path d="M9.2 12l2 2 3.6-3.8"/>'
    ),
}


def svg_markup(name: str, color: str, stroke: float = 1.7) -> str:
    """生成着色后的完整 SVG 文档。"""
    body = _ICON_DEFS.get(name) or _ICON_DEFS["info"]
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'width="24" height="24" fill="none" stroke="{color}" '
        f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">'
        f'<g fill="{color if name in _SOLID_ICONS else "none"}">{body}</g>'
        "</svg>"
    )


# 这些图标的图元是实心的（描边会让它们看起来像空框）
_SOLID_ICONS = {"play", "stop"}


@lru_cache(maxsize=256)
def _renderer(name: str, color: str, stroke: float) -> QSvgRenderer:
    return QSvgRenderer(QByteArray(svg_markup(name, color, stroke).encode("utf-8")))


def pixmap(
    name: str,
    color: str,
    size: int = 20,
    *,
    stroke: float = 1.7,
    ratio: float = 2.0,
) -> QPixmap:
    """按 DPI 倍率渲染成位图，保证高缩放屏上不发虚。"""
    actual = max(1, int(round(size * ratio)))
    canvas = QPixmap(actual, actual)
    canvas.setDevicePixelRatio(ratio)
    canvas.fill(Qt.transparent)

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    try:
        _renderer(name, color, stroke).render(painter)
    finally:
        painter.end()
    return canvas


def qicon(name: str, color: str, size: int = 20, *, stroke: float = 1.7) -> QIcon:
    icon = QIcon()
    for ratio in (1.0, 1.5, 2.0, 3.0):
        icon.addPixmap(pixmap(name, color, size, stroke=stroke, ratio=ratio))
    return icon


def icon_names() -> list[str]:
    return sorted(_ICON_DEFS)


# --------------------------------------------------------------------------
# 应用图标（任务栏 / Alt+Tab / 窗口左上角）
# --------------------------------------------------------------------------

_APP_ICON_CACHE: QIcon | None = None


def app_icon(color: str, size: int = 64) -> QIcon:
    """应用图标。

    打包后优先用 ``assets/echo.ico``：它是 16→256 的多尺寸位图，Windows 在
    任务栏、Alt+Tab、开始菜单各处会挑最合适的一档，不会像单张矢量重渲染
    那样在小尺寸下糊掉。源码运行且 ico 不存在时退回矢量图标，
    这样不跑 ``tools/make_icon.py`` 也能直接开发。
    """
    global _APP_ICON_CACHE
    if _APP_ICON_CACHE is not None:
        return _APP_ICON_CACHE

    from ..paths import resource_root

    icon_path = resource_root() / "assets" / "echo.ico"
    if icon_path.exists():
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            _APP_ICON_CACHE = icon
            return icon

    _APP_ICON_CACHE = qicon("capture", color, size)
    return _APP_ICON_CACHE


# --------------------------------------------------------------------------
# echo 字标（方案 §7.3）
# --------------------------------------------------------------------------

ECHO_MARK_TEXT = "echo"


def echo_mark_markup(color: str, height: int = 14) -> str:
    """把 echo 字标做成矢量，避免字号在不同 DPI 下变形。

    方案 §7.3：全小写、不使用大写变体、不加水印式描边。
    这里不做额外装饰——它就是一行字。
    """
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 60 {height}" width="60" height="{height}">'
        f'<text x="0" y="{int(height * 0.82)}" '
        'font-family="Microsoft YaHei UI, Segoe UI, sans-serif" '
        f'font-size="{int(height * 1.02)}" letter-spacing="2.6" '
        f'fill="{color}">echo</text>'
        "</svg>"
    )
