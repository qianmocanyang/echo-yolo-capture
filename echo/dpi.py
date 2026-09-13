"""DPI 缩放与 Qt 逻辑坐标 ↔ Win32 物理坐标的换算。

方案 §3.2 明确要求「坐标以物理像素保存，界面逻辑坐标必须经过 DPI 换算」。
Qt 6 的控件尺寸是**逻辑像素**，而 Win32/DXGI/WGC/PNG 都是**物理像素**。
两者在 125% / 150% 缩放下相差整整一个倍率，混用就会得到位置错一截的截图。

本模块只做换算，不做任何窗口操作，便于单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QGuiApplication, QScreen

from . import winapi
from .logging_setup import get_logger
from .region import Region

log = get_logger("dpi")


@dataclass(frozen=True, slots=True)
class ScreenMapping:
    """一个 Qt 屏幕与一个 Win32 显示器之间的对应关系。"""

    screen_name: str          # QScreen.name()，Windows 上形如 "\\\\.\\DISPLAY1"
    monitor_device: str       # GetMonitorInfoW 的 szDevice，同一台机器上两者字符串一致
    device_pixel_ratio: float
    # 物理像素下该显示器的矩形
    physical_left: int
    physical_top: int
    physical_width: int
    physical_height: int
    is_primary: bool
    matched: bool             # False 表示没能在 Win32 侧找到同名显示器，只能按比例推断

    @property
    def physical_rect(self) -> tuple[int, int, int, int]:
        return (
            self.physical_left,
            self.physical_top,
            self.physical_left + self.physical_width,
            self.physical_top + self.physical_height,
        )

    @property
    def scale(self) -> float:
        return self.device_pixel_ratio


def screen_mappings() -> list[ScreenMapping]:
    """建立 Qt 屏幕 ↔ Win32 显示器的映射表。

    匹配键是设备名。Qt 在 Windows 上用 ``QScreen.name()`` 返回 ``\\\\.\\DISPLAYn``，
    与 ``MONITORINFOEXW.szDevice`` 完全一致，所以这是可靠的匹配方式。
    匹配不上时退化为「用缩放比把物理矩形反推出来」，并标记 ``matched=False``，
    让界面可以提示"该显示器的坐标可能不准确"。
    """
    monitors = {m.device: m for m in winapi.enum_monitors()}
    result: list[ScreenMapping] = []

    for screen in QGuiApplication.screens():
        name = screen.name()
        ratio = float(screen.devicePixelRatio() or 1.0)
        geo = screen.geometry()  # 逻辑像素，虚拟桌面坐标
        mon = monitors.get(name)

        if mon is not None:
            result.append(
                ScreenMapping(
                    screen_name=name,
                    monitor_device=mon.device,
                    device_pixel_ratio=ratio,
                    physical_left=mon.left,
                    physical_top=mon.top,
                    physical_width=mon.width,
                    physical_height=mon.height,
                    is_primary=mon.primary,
                    matched=True,
                )
            )
        else:
            log.warning("显示器 %s 未能在 Win32 侧匹配，按缩放比推断几何", name)
            result.append(
                ScreenMapping(
                    screen_name=name,
                    monitor_device=name,
                    device_pixel_ratio=ratio,
                    physical_left=int(round(geo.x() * ratio)),
                    physical_top=int(round(geo.y() * ratio)),
                    physical_width=int(round(geo.width() * ratio)),
                    physical_height=int(round(geo.height() * ratio)),
                    is_primary=screen is QGuiApplication.primaryScreen(),
                    matched=False,
                )
            )

    result.sort(key=lambda m: (m.physical_left, m.physical_top))
    return result


def screen_for_name(name: str) -> QScreen | None:
    for screen in QGuiApplication.screens():
        if screen.name() == name:
            return screen
    return None


def mapping_for_name(name: str) -> ScreenMapping | None:
    for mapping in screen_mappings():
        if mapping.screen_name == name or mapping.monitor_device == name:
            return mapping
    return None


def logical_size(physical_px: int, ratio: float) -> int:
    """物理像素 → 逻辑像素（向下取整，保证不超出）。"""
    if ratio <= 0:
        return physical_px
    return int(physical_px / ratio)


def logical_point(x: int, y: int, ratio: float) -> tuple[int, int]:
    if ratio <= 0:
        return x, y
    return int(x / ratio), int(y / ratio)


def physical_size(logical_px: int, ratio: float) -> int:
    """逻辑像素 → 物理像素（四舍五入）。"""
    return int(round(logical_px * (ratio or 1.0)))


# --------------------------------------------------------------------------
# 物理坐标 → 逻辑坐标（用于把原生矩形画到 Qt 窗口上）
# --------------------------------------------------------------------------

def physical_rect_to_logical_qt(
    left: int, top: int, width: int, height: int
) -> tuple[int, int, int, int]:
    """把 Win32 物理像素矩形换算成 Qt 虚拟桌面的逻辑像素矩形。

    多显示器且各屏缩放比不同时，Qt 的虚拟桌面逻辑坐标是**各屏逻辑矩形拼接**的结果，
    不能简单地用单一倍率去除。做法是：先找到该物理矩形所属的显示器，
    用它自己的倍率换算，再加上 Qt 该屏逻辑原点的偏移。
    """
    if not QGuiApplication.instance():
        return (left, top, width, height)

    monitors = winapi.enum_monitors()
    owner = None
    cx, cy = left + width // 2, top + height // 2
    for mon in monitors:
        if mon.left <= cx < mon.right and mon.top <= cy < mon.bottom:
            owner = mon
            break
    if owner is None:
        handle = winapi.monitor_containing_point(cx, cy)
        for mon in monitors:
            if mon.handle == handle:
                owner = mon
                break
    if owner is None:
        screen = QGuiApplication.primaryScreen()
        ratio = float(screen.devicePixelRatio() or 1.0)
        return (
            int(round(left / ratio)),
            int(round(top / ratio)),
            int(round(width / ratio)),
            int(round(height / ratio)),
        )

    screen = screen_for_name(owner.device) or QGuiApplication.primaryScreen()
    ratio = float(screen.devicePixelRatio() or 1.0)
    geo = screen.geometry()

    local_x = (left - owner.left) / ratio
    local_y = (top - owner.top) / ratio
    return (
        int(round(geo.x() + local_x)),
        int(round(geo.y() + local_y)),
        int(round(width / ratio)),
        int(round(height / ratio)),
    )


def current_screen() -> QScreen | None:
    window = QGuiApplication.focusWindow()
    if window is not None:
        return window.screen()
    return QGuiApplication.primaryScreen()


def current_screen_ratio() -> float:
    screen = current_screen()
    if screen is None:
        return 1.0
    return float(screen.devicePixelRatio() or 1.0)


def region_to_logical(
    region: Region, source_left: int, source_top: int, ratio: float
) -> tuple[int, int, int, int]:
    """把「相对采集源」的物理选区换算成 Qt 逻辑坐标。

    source_left / source_top 是采集源左上角在虚拟桌面中的物理坐标。
    """
    abs_x = source_left + region.x
    abs_y = source_top + region.y
    return physical_rect_to_logical_qt(abs_x, abs_y, region.width, region.height)
