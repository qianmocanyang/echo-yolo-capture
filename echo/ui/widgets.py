"""通用界面控件。

方案 §7.1 的"统一间距与圆角、精致状态反馈"靠这些控件保证一致性：
所有卡片、字段行、标签都从这里出，界面代码里不再各自调 margin。

每个控件都刻意做得"能读"——状态类控件同时给出**文字**和**颜色**，
因为方案 §7.1 明确要求「状态用文字加图标表达，不只依靠颜色」。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Callable

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import icons
from .theme import DARK, METRICS, Palette, level_colors, status_colors


# --------------------------------------------------------------------------
# 基础构件
# --------------------------------------------------------------------------

class Divider(QFrame):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFixedHeight(1)


def caption(text: str, parent: QWidget | None = None, *, dim: bool = True) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName("Caption" if dim else "")
    label.setWordWrap(True)
    return label


def hint(text: str, parent: QWidget | None = None) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName("Hint")
    label.setWordWrap(True)
    return label


class ElidedLabel(QLabel):
    """长文本标签：放不下就在中间省略，并且**不把父容器撑宽**。

    存在的理由是 Windows 路径这类**没有空格的长串**。QLabel 即使开了
    ``setWordWrap(True)`` 也断不开它——整条路径是一个不可断词，
    ``minimumSizeHint()`` 仍然是整串的宽度（实测 540px）。放进固定宽度
    的右栏里，一条路径就能把整列的最小宽度顶到 590，而
    :class:`ScrollColumn` 关掉了水平滚动条，于是右栏右侧一大片控件被
    **静默裁掉**（存储卡要求 582px、视口只有 334px，精确坐标按钮、
    X/Y 输入框、锁定宽高比按钮全都看不见）。

    省略是中间省略：路径的头（盘符/用户目录）和尾（批次目录）都有用，
    中间那段最没信息量。完整值仍可从 ``fullText()`` 或 tooltip 拿到。
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        mode: Qt.TextElideMode = Qt.ElideMiddle,
    ):
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        # 水平方向声明为 Ignored：布局不再参考它的 sizeHint，
        # 它拿到多少宽度就画多少，宽度归零也不会撑破父容器。
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text or ""
        self.setToolTip(self._full)
        self._apply_elide()

    def fullText(self) -> str:  # noqa: N802
        """未被省略的完整文本（用于复制/日志）。"""
        return self._full

    def sizeHint(self) -> QSize:  # noqa: N802
        # 宽度给 0：让父布局按其他控件决定卡片宽度，自己不参与竞争。
        return QSize(0, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = self.width()
        plain = QLabel.text(self)
        if width <= 0 or not self._full:
            if plain != self._full:
                QLabel.setText(self, self._full)
            return
        elided = self.fontMetrics().elidedText(self._full, self._mode, width)
        # 只在结果真的变了才写回：否则 setText → 重排 → resizeEvent
        # → setText 会形成回环。
        if elided != plain:
            QLabel.setText(self, elided)


def title(text: str, parent: QWidget | None = None) -> QLabel:
    label = QLabel(text, parent)
    label.setObjectName("CardTitle")
    return label


class Card(QFrame):
    """带内边距的卡片容器。`body` 是内容布局。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        padding: int | None = None,
        spacing: int | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        pad = METRICS.card_padding if padding is None else padding
        self._layout.setContentsMargins(pad, pad, pad, pad)
        self._layout.setSpacing(METRICS.gap if spacing is None else spacing)

    @property
    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self._layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        self._layout.addLayout(layout)


class CardHeader(QWidget):
    """卡片标题行：图标 + 标题 + 右侧可选控件槽。"""

    def __init__(
        self,
        text: str,
        parent: QWidget | None = None,
        *,
        icon: str = "",
        icon_color: str = DARK.text_dim,
        subtitle: str = "",
    ):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        if icon:
            glyph = QLabel(self)
            glyph.setPixmap(icons.pixmap(icon, icon_color, 16))
            glyph.setFixedSize(16, 16)
            row.addWidget(glyph)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)
        column.addWidget(title(text, self))
        if subtitle:
            column.addWidget(hint(subtitle, self))
        row.addLayout(column)
        row.addStretch(1)
        self._row = row

    def add_right(self, widget: QWidget) -> QWidget:
        self._row.addWidget(widget)
        return widget


class FieldRow(QWidget):
    """一格表单：标签在左、控件在右（或标签在上、控件在下）。"""

    def __init__(
        self,
        label: str,
        widget: QWidget | None = None,
        parent: QWidget | None = None,
        *,
        vertical: bool = False,
        hint: str = "",
    ):
        super().__init__(parent)
        self._hint_text = hint
        self._widget = widget

        if vertical:
            box = QVBoxLayout(self)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(6)
            box.addWidget(self._make_label(label))
            if widget is not None:
                box.addWidget(widget)
            self._hint = hint_label = QLabel("", self)
            hint_label.setObjectName("Hint")
            hint_label.setWordWrap(True)
            hint_label.setVisible(False)
            box.addWidget(hint_label)
        else:
            row = QHBoxLayout(self)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(10)
            row.addWidget(self._make_label(label), 0, Qt.AlignVCenter)
            row.addStretch(1)
            if widget is not None:
                row.addWidget(widget, 0, Qt.AlignVCenter)
            self._hint = None

    def _make_label(self, text: str) -> QLabel:
        label = QLabel(text, self)
        label.setObjectName("FieldLabel")
        return label

    def set_hint(self, text: str, level: str = "warn") -> None:
        """在字段下方显示一行提示。方案 §7.2：设置不完整时在相关字段旁说明原因。"""
        self._hint_text = text
        if self._hint is None:
            return
        if not text:
            self._hint.setVisible(False)
            return
        color, _ = level_colors(level)
        self._hint.setText(text)
        self._hint.setStyleSheet(f"color: {color};")
        self._hint.setVisible(True)


class Badge(QLabel):
    """小圆角标签，用于状态、数量、质量标记。"""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        color: str | None = None,
        background: str | None = None,
        palette: Palette = DARK,
    ):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self._palette = palette
        self.set_color(color or palette.text_dim, background or palette.card_hover)

    def set_color(self, color: str, background: str) -> None:
        self.setStyleSheet(
            f"color: {color}; background: {background};"
            "border-radius: 9px; padding: 2px 9px; font-size: 11px;"
        )

    def set_state(self, state: str) -> None:
        color, background = status_colors(state, self._palette)
        self.set_color(color, background)

    def set_level(self, level: str) -> None:
        color, background = level_colors(level, self._palette)
        self.set_color(color, background)


class StatePill(QWidget):
    """状态胶囊：一个圆点 + 文字。颜色只是辅助，文字才是主信息。"""

    def __init__(self, parent: QWidget | None = None, palette: Palette = DARK):
        super().__init__(parent)
        self._palette = palette
        self.setObjectName("StatePill")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 3, 12, 3)
        row.setSpacing(7)

        self._dot = _Dot(palette.text_dim, self)
        row.addWidget(self._dot)

        self._label = QLabel("未配置", self)
        self._label.setObjectName("")
        row.addWidget(self._label)

        self.set_state("unconfigured", "未配置")

    def set_state(self, state: str, text: str) -> None:
        color, background = status_colors(state, self._palette)
        self._dot.set_color(color)
        self._label.setText(text)
        self._label.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.setStyleSheet(
            f"#StatePill {{ background: {background}; border-radius: 13px; }}"
        )


class _Dot(QWidget):
    def __init__(self, color: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedSize(8, 8)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(self._color)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(0, 0, 8, 8)
        painter.end()


class Segmented(QWidget):
    """分段选择器。比下拉框更省一次点击，也比单选按钮更紧凑。"""

    changed = Signal(int)

    def __init__(
        self,
        options: list[str],
        parent: QWidget | None = None,
        *,
        icons_map: dict[int, str] | None = None,
        compact: bool = False,
    ):
        super().__init__(parent)
        self._buttons: list[QPushButton] = []
        self._icon_names: dict[int, str] = dict(icons_map or {})
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)

        for index, text in enumerate(options):
            button = QPushButton(text, self)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.PointingHandCursor)
            height = 30 if compact else 32
            button.setFixedHeight(height)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.setStyleSheet(_segment_style(DARK))
            if index in self._icon_names:
                button.setIcon(icons.qicon(self._icon_names[index], DARK.text_dim, 15))
                button.setIconSize(QSize(15, 15))
            button.clicked.connect(lambda _=False, i=index: self._select(i))
            row.addWidget(button)
            self._buttons.append(button)

        self._index = 0
        if self._buttons:
            self._buttons[0].setChecked(True)
        self._update_icons()

    def _select(self, index: int) -> None:
        self.set_index(index)
        self.changed.emit(index)

    def set_index(self, index: int, *, emit: bool = False) -> None:
        index = max(0, min(index, len(self._buttons) - 1))
        if not self._buttons:
            return
        self._buttons[index].setChecked(True)
        changed = index != self._index
        self._index = index
        self._update_icons()
        if emit and changed:
            self.changed.emit(index)

    def index(self) -> int:
        return self._index

    def set_tooltips(self, tips: list[str]) -> None:
        for button, tip in zip(self._buttons, tips):
            button.setToolTip(tip)

    def set_enabled_at(self, index: int, enabled: bool) -> None:
        if 0 <= index < len(self._buttons):
            self._buttons[index].setEnabled(enabled)

    def _update_icons(self) -> None:
        """选中态换强调色，让当前项一眼可辨。"""
        for index, button in enumerate(self._buttons):
            name = self._icon_names.get(index)
            if not name:
                continue
            color = DARK.accent if index == self._index else DARK.text_dim
            button.setIcon(icons.qicon(name, color, 15))


def _segment_style(palette: Palette) -> str:
    return f"""
    QPushButton {{
        background: {palette.input};
        border: 1px solid transparent;
        border-radius: 7px;
        color: {palette.text_dim};
        font-size: 12px;
        padding: 0 10px;
    }}
    QPushButton:hover {{ background: {palette.card_hover}; color: {palette.text}; }}
    QPushButton:checked {{
        background: {palette.accent_soft};
        color: {palette.accent};
        border-color: rgba(139, 156, 255, 0.35);
        font-weight: 600;
    }}
    """


class LabeledSwitch(QWidget):
    """一行开关：标题 + 说明 + 右侧勾选框。"""

    toggled = Signal(bool)

    def __init__(
        self,
        text: str,
        parent: QWidget | None = None,
        *,
        description: str = "",
        checked: bool = False,
    ):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        label = QLabel(text, self)
        column.addWidget(label)
        if description:
            column.addWidget(hint(description, self))
        row.addLayout(column, 1)

        self._box = QCheckBox(self)
        self._box.setChecked(checked)
        self._box.toggled.connect(self.toggled.emit)
        row.addWidget(self._box, 0, Qt.AlignTop)

    def isChecked(self) -> bool:  # noqa: N802
        return self._box.isChecked()

    def setChecked(self, value: bool) -> None:  # noqa: N802
        self._box.setChecked(bool(value))

    def setEnabled(self, value: bool) -> None:  # noqa: N802
        super().setEnabled(value)
        self._box.setEnabled(value)


class ScrollColumn(QScrollArea):
    """竖向滚动容器。用于右侧设置区与设置页。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        spacing: int = 12,
        margins: tuple[int, int, int, int] = (0, 0, 8, 0),
    ):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        holder = QWidget(self)
        holder.setObjectName("ScrollHolder")
        self._column = QVBoxLayout(holder)
        self._column.setContentsMargins(*margins)
        self._column.setSpacing(spacing)
        self._column.addStretch(0)
        self.setWidget(holder)

    @property
    def column(self) -> QVBoxLayout:
        return self._column

    def add(self, widget: QWidget) -> QWidget:
        # 插到末尾的 stretch 之前
        self._column.insertWidget(self._column.count() - 1, widget)
        return widget

    def add_stretch(self) -> None:
        self._column.insertStretch(self._column.count() - 1, 1)

    def clear(self) -> None:
        while self._column.count() > 1:
            item = self._column.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()


class ToolRow(QWidget):
    """一行紧凑工具按钮。"""

    def __init__(self, parent: QWidget | None = None, *, spacing: int = 6):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(spacing)
        self._row = row

    def add(
        self,
        text: str,
        callback: Callable[[], None] | None = None,
        *,
        icon: str = "",
        variant: str = "",
        tooltip: str = "",
        enabled: bool = True,
    ) -> QPushButton:
        button = QPushButton(text, self)
        if icon:
            button.setIcon(icons.qicon(icon, DARK.text_dim, 15))
            button.setIconSize(QSize(15, 15))
        if variant:
            button.setProperty("variant", variant)
        if tooltip:
            button.setToolTip(tooltip)
        button.setEnabled(enabled)
        button.setCursor(Qt.PointingHandCursor)
        if callback is not None:
            button.clicked.connect(lambda _=False: callback())
        self._row.addWidget(button)
        return button

    def add_stretch(self) -> None:
        self._row.addStretch(1)

    def add_widget(self, widget: QWidget) -> QWidget:
        self._row.addWidget(widget)
        return widget


def hline(parent: QWidget | None = None) -> Divider:
    return Divider(parent)
