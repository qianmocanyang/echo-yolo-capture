"""中央预览区与底部最近截图条。

方案 §7.2 对预览的要求是「等比例展示实际输出画面，显示"输出 320×320"；
可切换查看源画面与裁剪范围」。所以这里有两种渲染状态：

* **裁剪预览**：显示最终会保存下来的那张图，下方标注输出尺寸。
  用户看到的即所得，没有"预览是一回事、保存是另一回事"的落差。
* **源画面**：显示采集源全画面，并在上面叠加当前选区的框。
  这一模式专门用来回答"我到底截的是屏幕哪一块"。

空状态文案按方案 §7.2 给定："选择游戏窗口，开始采集第一张图片"。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import icons, imaging
from .theme import DARK, METRICS


class PreviewCanvas(QWidget):
    """等比缩放显示一帧画面。自己绘制而不是用 QLabel，为了精确控制留白与叠加层。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._image: QImage = QImage()
        self._empty_text = "选择游戏窗口，开始采集第一张图片"
        self._empty_hint = ""
        self._chip_text = ""
        self._overlay_region: tuple[int, int, int, int] | None = None
        self.setObjectName("PreviewSurface")
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ---- 内容 -----------------------------------------------------------
    def set_image(self, image, *, chip: str = "") -> None:
        self._image = image if isinstance(image, QImage) else QImage()
        self._chip_text = chip
        self._overlay_region = None
        self.update()

    def set_pixmap(self, pixmap: QPixmap, *, chip: str = "") -> None:
        self._image = pixmap.toImage() if not pixmap.isNull() else QImage()
        self._chip_text = chip
        self._overlay_region = None
        self.update()

    def set_overlay_region(self, rect: tuple[int, int, int, int] | None) -> None:
        self._overlay_region = rect
        self.update()

    def set_empty(self, text: str, hint: str = "") -> None:
        self._image = QImage()
        self._empty_text = text
        self._empty_hint = hint
        self._chip_text = ""
        self._overlay_region = None
        self.update()

    def has_image(self) -> bool:
        return not self._image.isNull()

    # ---- 绘制 -----------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor(DARK.border_soft), 1))
        painter.setBrush(QColor(DARK.card_alt))
        painter.drawRoundedRect(rect, 8, 8)

        inner = rect.adjusted(10, 10, -10, -10)

        if self._image.isNull():
            self._draw_empty(painter, inner)
            painter.end()
            return

        target = self._fit_rect(self._image.size(), inner)
        painter.drawImage(target, self._image)

        # 画面边缘描一圈，避免深色画面与背景糊成一片
        painter.setPen(QPen(QColor(255, 255, 255, 26), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(target)

        if self._overlay_region is not None:
            rx, ry, rw, rh = self._overlay_region
            overlay = QRect(target.left() + rx, target.top() + ry, rw, rh)
            painter.setPen(QPen(QColor(0, 0, 0, 130), 4))
            painter.drawRect(overlay.adjusted(-1, -1, 1, 1))
            painter.setPen(QPen(QColor(DARK.accent), 2))
            painter.drawRect(overlay)

        if self._chip_text:
            self._draw_chip(painter, target, self._chip_text)
        painter.end()

    def _fit_rect(self, size, bounds: QRect) -> QRect:
        if size.width() <= 0 or size.height() <= 0:
            return bounds
        scale = min(
            bounds.width() / size.width(),
            bounds.height() / size.height(),
        )
        width = max(1, int(size.width() * scale))
        height = max(1, int(size.height() * scale))
        return QRect(
            bounds.left() + (bounds.width() - width) // 2,
            bounds.top() + (bounds.height() - height) // 2,
            width,
            height,
        )

    def _draw_chip(self, painter: QPainter, target: QRect, text: str) -> None:
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 16
        height = metrics.height() + 6
        chip = QRect(
            target.left() + 8,
            target.top() + 8,
            min(width, max(60, target.width() - 16)),
            height,
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(10, 12, 17, 200))
        painter.drawRoundedRect(chip, 6, 6)
        painter.setPen(QColor(DARK.text))
        painter.drawText(chip.adjusted(8, 0, -8, 0), Qt.AlignVCenter, text)

    def _draw_empty(self, painter: QPainter, inner: QRect) -> None:
        # 中心放一个淡淡的图标，让空状态看起来是"准备好了"而不是"坏了"
        icon_size = 40
        pixmap = icons.pixmap("capture", DARK.border, icon_size)
        icon_rect = QRect(
            inner.center().x() - icon_size // 2,
            inner.center().y() - icon_size - 18,
            icon_size,
            icon_size,
        )
        painter.setOpacity(0.75)
        painter.drawPixmap(icon_rect, pixmap)
        painter.setOpacity(1.0)

        title_font = QFont()
        title_font.setPointSize(11)
        painter.setFont(title_font)
        painter.setPen(QColor(DARK.text_dim))
        text_rect = QRect(
            inner.left() + 12, inner.center().y() + 2, inner.width() - 24, 26
        )
        painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop, self._empty_text)

        if self._empty_hint:
            hint_font = QFont()
            hint_font.setPointSize(9)
            painter.setFont(hint_font)
            painter.setPen(QColor(DARK.text_faint))
            hint_rect = QRect(
                inner.left() + 12, text_rect.bottom() + 4, inner.width() - 24, 40
            )
            painter.drawText(
                hint_rect, Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, self._empty_hint
            )


class PreviewPanel(QWidget):
    """预览区 + 模式切换。"""

    mode_changed = Signal(bool)   # True = 显示源画面

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._title = QLabel("画面预览", self)
        self._title.setObjectName("CardTitle")
        header.addWidget(self._title)
        header.addStretch(1)

        self._mode_label = QLabel("裁剪预览", self)
        self._mode_label.setObjectName("Caption")
        header.addWidget(self._mode_label)
        box.addLayout(header)

        self.canvas = PreviewCanvas(self)
        box.addWidget(self.canvas, 1)

        self._footer = QLabel("", self)
        self._footer.setObjectName("Caption")
        box.addWidget(self._footer)

    def set_mode_label(self, show_source: bool) -> None:
        self._mode_label.setText("源画面 + 选区" if show_source else "裁剪预览")

    def set_footer(self, text: str) -> None:
        self._footer.setText(text)


class ThumbStrip(QWidget):
    """底部最近截图条（方案 §7.2：最近 6～8 张缩略图，点击查看、可撤销最近一次保留）。

    "查看"和"撤销保留"是两件不同的事，所以左键查看、右键出菜单，
    不做"点一下既打开又删除"这种暧昧交互。
    """

    clicked = Signal(object)             # sqlite3.Row
    request_reveal = Signal(object)      # 在资源管理器中打开
    request_exclude = Signal(object)     # 标记为已排除
    request_delete = Signal(object)      # 删除记录与文件

    def __init__(self, parent: QWidget | None = None, *, capacity: int = 8):
        super().__init__(parent)
        self._capacity = capacity
        self._rows: list = []
        self._cells: list[_ThumbCell] = []
        self.setFixedHeight(METRICS.thumb_height)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._row = row
        self._empty = QLabel("还没有截图", self)
        self._empty.setObjectName("Hint")
        self._empty.setAlignment(Qt.AlignCenter)
        row.addWidget(self._empty, 1)
        self._empty.setVisible(True)

    def set_capacity(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._rebuild()

    def prepend(self, row_data, abs_path) -> None:
        self._rows.insert(0, (row_data, abs_path))
        del self._rows[self._capacity:]
        self._rebuild()

    def set_rows(self, rows: list, path_resolver) -> None:
        self._rows = [(row, path_resolver(row)) for row in rows[: self._capacity]]
        self._rebuild()

    def clear(self) -> None:
        self._rows = []
        self._rebuild()

    def _rebuild(self) -> None:
        for cell in self._cells:
            self._row.removeWidget(cell)
            cell.deleteLater()
        self._cells.clear()

        self._empty.setVisible(not self._rows)
        for row_data, abs_path in self._rows:
            cell = _ThumbCell(row_data, abs_path, self)
            cell.clicked.connect(self.clicked.emit)
            cell.reveal_requested.connect(self.request_reveal.emit)
            cell.exclude_requested.connect(self.request_exclude.emit)
            cell.delete_requested.connect(self.request_delete.emit)
            self._row.addWidget(cell)
            self._cells.append(cell)
        while self._row.count() < self._capacity + 1:
            self._row.addStretch(0)


class _ThumbCell(QWidget):
    """一张缩略图。角标显示质量标记与相似图提示。"""

    clicked = Signal(object)
    reveal_requested = Signal(object)
    exclude_requested = Signal(object)
    delete_requested = Signal(object)

    def __init__(self, row_data, abs_path, parent: QWidget | None = None):
        super().__init__(parent)
        self._row_data = row_data
        self._abs_path = abs_path
        self.setObjectName("ThumbCell")
        self.setFixedHeight(METRICS.thumb_height)
        self.setMinimumWidth(96)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(self._tooltip())
        self._pixmap = imaging.thumbnail(abs_path, METRICS.thumb_height - 30) if abs_path else QPixmap()

    def _tooltip(self) -> str:
        from ..storage import ReviewStatus

        try:
            rel = self._row_data["rel_path"]
            captured = str(self._row_data["captured_at"])[:19].replace("T", " ")
            trigger = {"manual": "手动", "timer": "定时", "burst": "连拍"}.get(
                self._row_data["trigger_kind"], self._row_data["trigger_kind"]
            )
            status = ReviewStatus.parse(self._row_data["review_status"]).label
            flags = self._row_data["quality_flags"] or ""
            lines = [rel, f"{captured} · {trigger} · {status}"]
            if flags:
                lines.append(f"标记：{flags}")
            if self._row_data["similar_group"] is not None:
                lines.append("与既有图片相似")
            return "\n".join(lines)
        except (KeyError, TypeError, IndexError):
            return ""

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        rect = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor(DARK.border_soft), 1))
        painter.setBrush(QColor(DARK.card_alt))
        painter.drawRoundedRect(rect, 8, 8)

        area = rect.adjusted(5, 5, -5, -22)
        if self._pixmap.isNull():
            painter.setPen(QColor(DARK.text_faint))
            font = QFont(); font.setPointSize(8); painter.setFont(font)
            painter.drawText(area, Qt.AlignCenter, "无法预览")
        else:
            scaled = self._pixmap.scaled(
                area.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            painter.drawPixmap(
                area.left() + (area.width() - scaled.width()) // 2,
                area.top() + (area.height() - scaled.height()) // 2,
                scaled,
            )

        badge_text, badge_color = self._badge()
        if badge_text:
            font = QFont(); font.setPointSize(7); painter.setFont(font)
            metrics = painter.fontMetrics()
            width = metrics.horizontalAdvance(badge_text) + 8
            badge = QRect(rect.left() + 5, rect.top() + 5, width, 14)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(badge_color))
            painter.drawRoundedRect(badge, 5, 5)
            painter.setPen(QColor(DARK.bg))
            painter.drawText(badge, Qt.AlignCenter, badge_text)

        try:
            captured = str(self._row_data["captured_at"])[11:19]
        except (KeyError, TypeError, IndexError):
            captured = ""
        font = QFont(); font.setPointSize(8); painter.setFont(font)
        painter.setPen(QColor(DARK.text_faint))
        painter.drawText(
            QRect(rect.left(), rect.bottom() - 19, rect.width(), 16),
            Qt.AlignCenter,
            captured,
        )
        painter.end()

    def _badge(self) -> tuple[str, str]:
        from ..quality import FLAG_BLACK, FLAG_BLUR, FLAG_DARK

        try:
            flags = (self._row_data["quality_flags"] or "").split(",")
            if FLAG_BLACK in flags:
                return "黑屏", DARK.danger
            if FLAG_BLUR in flags:
                return "模糊", DARK.warn
            if FLAG_DARK in flags:
                return "偏暗", DARK.warn
            if self._row_data["similar_group"] is not None:
                return "相似", DARK.info
        except (KeyError, TypeError, IndexError):
            return "", DARK.card_hover
        return "", DARK.card_hover

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._row_data)
        elif event.button() == Qt.RightButton:
            self._menu(event.globalPosition().toPoint())

    def _menu(self, position: QPoint) -> None:
        menu = QMenu(self)
        act_open = menu.addAction(icons.qicon("eye", DARK.text_dim, 15), "查看")
        act_reveal = menu.addAction(icons.qicon("folder_open", DARK.text_dim, 15), "在文件夹中显示")
        menu.addSeparator()
        act_exclude = menu.addAction(icons.qicon("close", DARK.warn, 15), "标记为已排除")
        act_delete = menu.addAction(icons.qicon("trash", DARK.danger, 15), "删除这张图")

        chosen = menu.exec(position)
        if chosen is act_open:
            self.clicked.emit(self._row_data)
        elif chosen is act_reveal:
            self.reveal_requested.emit(self._row_data)
        elif chosen is act_exclude:
            self.exclude_requested.emit(self._row_data)
        elif chosen is act_delete:
            self.delete_requested.emit(self._row_data)
