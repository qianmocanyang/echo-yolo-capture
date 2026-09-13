"""拖拽定位覆盖层（方案 §3.2 的第二种定位方式）。

交互：先设好 320×320 这类尺寸，再拖动**固定大小**的矩形确定位置。
所以这个覆盖层不做八向缩放手柄——尺寸由输入框决定，这里只管位置。
这看起来少了功能，其实是刻意的：允许在这里改尺寸会让"尺寸"和"位置"
两个概念混淆，而方案 §3.1 明确要求二者不能互相暗示。

坐标口径：整个覆盖层工作在 **物理像素** 上。
窗口几何由 :func:`echo.dpi.physical_rect_to_logical_qt` 换算成 Qt 逻辑坐标，
而鼠标位置要先乘回缩放比再得到物理像素坐标。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..dpi import mapping_for_name
from ..logging_setup import get_logger
from ..region import Region

log = get_logger("ui.region_picker")


class RegionPickerOverlay(QWidget):
    """全屏透明覆盖层，用于拖动固定尺寸的选区。

    构造时传入采集源（显示器或窗口）的物理几何，以及当前选区。
    确认后发出 :attr:`picked`，携带换算回"相对采集源"的物理坐标选区。
    """

    picked = Signal(object)   # Region
    cancelled = Signal()

    def __init__(
        self,
        *,
        source_left: int,
        source_top: int,
        source_width: int,
        source_height: int,
        region: Region,
        screen_name: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._source_left = int(source_left)
        self._source_top = int(source_top)
        self._source_width = int(source_width)
        self._source_height = int(source_height)
        self._region = region
        self._screen_name = screen_name

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setMouseTracking(True)
        self.setCursor(Qt.SizeAllCursor)
        self.setFocusPolicy(Qt.StrongFocus)

        mapping = mapping_for_name(screen_name) if screen_name else None
        self._ratio = mapping.device_pixel_ratio if mapping else 1.0

        # 覆盖层铺满采集源所在的**显示器**（而不是只铺采集源），
        # 这样用户能看清选区相对整块屏的位置。
        self._place_overlay()

        self._dragging = False
        self._grab_offset = QPoint(0, 0)

    # ---- 几何换算 -------------------------------------------------------
    def _place_overlay(self) -> None:
        """把窗口铺到采集源所在显示器上。"""
        from .. import winapi
        from .theme import DARK

        monitor_left = self._source_left
        monitor_top = self._source_top
        monitor_w = self._source_width
        monitor_h = self._source_height

        for mon in winapi.enum_monitors():
            if (
                mon.left <= self._source_left < mon.right
                and mon.top <= self._source_top < mon.bottom
            ):
                monitor_left, monitor_top = mon.left, mon.top
                monitor_w, monitor_h = mon.width, mon.height
                break

        self._monitor_left = monitor_left
        self._monitor_top = monitor_top

        from ..dpi import physical_rect_to_logical_qt

        x, y, w, h = physical_rect_to_logical_qt(
            monitor_left, monitor_top, monitor_w, monitor_h
        )
        self.setGeometry(QRect(x, y, w, h))

    def _to_local_physical(self, pos: QPoint) -> tuple[int, int]:
        """鼠标位置 → 相对采集源的物理像素坐标。"""
        return (
            self._monitor_left + int(round(pos.x() * self._ratio)),
            self._monitor_top + int(round(pos.y() * self._ratio)),
        )

    def _region_to_local(self, region: Region) -> QRect:
        """选区 → 覆盖层内的逻辑坐标矩形。"""
        x = (self._source_left + region.x - self._monitor_left) / self._ratio
        y = (self._source_top + region.y - self._monitor_top) / self._ratio
        return QRect(
            int(round(x)), int(round(y)),
            max(1, int(round(region.width / self._ratio))),
            max(1, int(round(region.height / self._ratio))),
        )

    # ---- 交互 -----------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        rect = self._region_to_local(self._region)
        if rect.contains(event.position().toPoint()):
            self._dragging = True
            self._grab_offset = event.position().toPoint() - rect.topLeft()
        else:
            # 点空白处 = 直接把选区中心移到点击位置，省一次拖动
            self._dragging = True
            self._grab_offset = QPoint(
                min(rect.width() // 2, max(0, rect.width() - 1)),
                min(rect.height() // 2, max(0, rect.height() - 1)),
            )
            self._move_to(event.position().toPoint())
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._move_to(event.position().toPoint())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._dragging = False
            self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._confirm()

    def _move_to(self, pos: QPoint) -> None:
        top_left = pos - self._grab_offset
        phys_x, phys_y = self._to_local_physical(top_left)
        # 选区以采集源左上角为原点
        rel_x = phys_x - self._source_left
        rel_y = phys_y - self._source_top
        self._region = Region(
            rel_x, rel_y, self._region.width, self._region.height
        ).clamp(self._source_width, self._source_height)
        self.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        step = 1 if event.modifiers() & Qt.ControlModifier else 8
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._confirm()
            return
        if key == Qt.Key_Escape:
            self.cancelled.emit()
            self.close()
            return
        if key == Qt.Key_Left:
            self._nudge(-step, 0)
        elif key == Qt.Key_Right:
            self._nudge(step, 0)
        elif key == Qt.Key_Up:
            self._nudge(0, -step)
        elif key == Qt.Key_Down:
            self._nudge(0, step)
        else:
            super().keyPressEvent(event)

    def _nudge(self, dx: int, dy: int) -> None:
        self._region = self._region.moved_by(dx, dy).clamp(
            self._source_width, self._source_height
        )
        self.update()

    def _confirm(self) -> None:
        self.picked.emit(self._region)
        self.close()

    # ---- 绘制 -----------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        from .theme import DARK

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)

        rect = self._region_to_local(self._region)

        # 整屏压暗，选区挖空（方案 §3.3：普通采集期间不显示悬浮框，
        # 但定位阶段必须看得见，所以这里的可见性是必要的）
        dim = QColor(8, 10, 14, 168)
        painter.fillRect(self.rect(), dim)
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.fillRect(rect, Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

        # 选区边框：强调色，2px，加一圈外描边保证在浅色画面上也看得清
        painter.setPen(QPen(QColor(0, 0, 0, 120), 4))
        painter.drawRect(rect.adjusted(-1, -1, 1, 1))
        painter.setPen(QPen(QColor(DARK.accent), 2))
        painter.drawRect(rect)

        # 尺寸与坐标标签
        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)
        text = f"{self._region.width} × {self._region.height}   ({self._region.x}, {self._region.y})"
        metrics = painter.fontMetrics()
        text_width = metrics.horizontalAdvance(text) + 18
        text_height = metrics.height() + 8

        label_y = rect.top() - text_height - 6
        if label_y < 6:
            label_y = rect.bottom() + 6
        label_rect = QRect(rect.left(), label_y, text_width, text_height)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(20, 24, 33, 235))
        painter.drawRoundedRect(label_rect, 6, 6)
        painter.setPen(QColor(DARK.text))
        painter.drawText(label_rect.adjusted(9, 0, -9, 0), Qt.AlignVCenter, text)

        # 底部操作提示
        tip = "拖动移动选区 · 方向键微调（按住 Ctrl 更精细）· 双击或回车确认 · Esc 取消"
        tip_width = metrics.horizontalAdvance(tip) + 24
        tip_rect = QRect(
            (self.width() - tip_width) // 2, self.height() - 52, tip_width, 30
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(20, 24, 33, 225))
        painter.drawRoundedRect(tip_rect, 8, 8)
        painter.setPen(QColor(DARK.text_dim))
        painter.drawText(tip_rect, Qt.AlignCenter, tip)
        painter.end()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.activateWindow()
        self.raise_()
        self.setFocus(Qt.OtherFocusReason)


def pick_region(
    *,
    source_left: int,
    source_top: int,
    source_width: int,
    source_height: int,
    region: Region,
    screen_name: str,
    on_picked,
    on_cancelled=None,
) -> RegionPickerOverlay:
    """便捷入口：创建并显示覆盖层。"""
    overlay = RegionPickerOverlay(
        source_left=source_left,
        source_top=source_top,
        source_width=source_width,
        source_height=source_height,
        region=region,
        screen_name=screen_name,
    )
    overlay.picked.connect(on_picked)
    if on_cancelled is not None:
        overlay.cancelled.connect(on_cancelled)
    overlay.show()
    return overlay
