"""快捷键录制输入框。

方案 §4.1 的交互：点击输入框 → 显示"请按下快捷键" → 用户按键后展示格式化结果；
Esc 取消；清除按钮解除绑定。

配合 :class:`~echo.hotkeys.HotkeyManager` 的 ``suspend()`` / ``resume()`` 使用：
进入录制时挂起派发，退出时恢复。否则用户按下 F8 想绑定，
结果真的触发了一次截图——这是这类工具最常见的自伤。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

from .. import hotkeys as hotkeys_mod
from . import icons
from .theme import DARK


class HotkeyEdit(QWidget):
    """可录制的快捷键输入框。"""

    # 用户在录制态按下了一个有效组合键（尚未提交）
    recorded = Signal(str)
    # 用户清除了绑定
    cleared = Signal()
    editing_started = Signal()
    editing_finished = Signal()

    def __init__(
        self,
        combo: str = "",
        parent: QWidget | None = None,
        *,
        width: int = 150,
    ):
        super().__init__(parent)
        self._editing = False
        self._combo = combo
        self._pending = ""
        self._error = ""

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        self._field = QLineEdit(self)
        self._field.setReadOnly(True)
        self._field.setAlignment(Qt.AlignCenter)
        self._field.setFixedWidth(width)
        self._field.setCursor(Qt.PointingHandCursor)
        self._field.installEventFilter(self)
        self._field.setText(combo or "未绑定")
        self._field.setToolTip("点击后按下想要的组合键；Esc 取消")
        row.addWidget(self._field)

        self._clear = QPushButton(self)
        self._clear.setIcon(icons.qicon("close", DARK.text_faint, 14))
        self._clear.setIconSize(QSize(14, 14))
        self._clear.setFixedSize(26, 26)
        self._clear.setProperty("variant", "ghost")
        self._clear.setToolTip("解除绑定")
        self._clear.setCursor(Qt.PointingHandCursor)
        self._clear.clicked.connect(self._on_clear)
        row.addWidget(self._clear)

        self.setFocusProxy(self._field)

    # ---- 对外 -----------------------------------------------------------
    def combo(self) -> str:
        return self._combo

    def set_combo(self, combo: str) -> None:
        self._combo = combo or ""
        self._error = ""
        self._field.setProperty("state", "")
        self._refresh()
        self._restyle()

    def set_error(self, message: str) -> None:
        """注册失败时把原绑定显示回来，并给出原因（方案 §4.2 要求保留旧绑定）。"""
        self._error = message
        self._field.setProperty("state", "error")
        self._restyle()
        self._field.setToolTip(message or "点击后按下想要的组合键；Esc 取消")

    def is_editing(self) -> bool:
        return self._editing

    # ---- 录制 -----------------------------------------------------------
    def start_editing(self) -> None:
        if self._editing:
            return
        self._editing = True
        self._pending = ""
        self._error = ""
        self._field.setProperty("state", "")
        self._field.setText("请按下快捷键")
        self._field.setStyleSheet(
            f"border-color: {DARK.accent}; color: {DARK.accent};"
        )
        self.editing_started.emit()

    def cancel_editing(self) -> None:
        if not self._editing:
            return
        self._editing = False
        self._pending = ""
        self._field.setStyleSheet("")
        self._refresh()
        self._restyle()
        self.editing_finished.emit()

    def eventFilter(self, watched, event):  # noqa: N802
        if watched is self._field:
            if event.type() == event.Type.MouseButtonPress and not self._editing:
                self.start_editing()
                return True
            if event.type() == event.Type.KeyPress and self._editing:
                self._handle_key(event)
                return True
            if event.type() == event.Type.FocusOut and self._editing:
                self.cancel_editing()
                return False
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._editing:
            self._handle_key(event)
            return
        super().keyPressEvent(event)

    def _handle_key(self, event) -> None:
        key = event.key()

        if key == Qt.Key_Escape:
            self.cancel_editing()
            return
        if key in (Qt.Key_Backspace, Qt.Key_Delete):
            self.cancel_editing()
            self._on_clear()
            return

        combo, error = hotkeys_mod.combo_from_qt(event.modifiers(), key)
        if error:
            self._pending = ""
            self._field.setText(error)
            self._field.setStyleSheet(f"color: {DARK.warn};")
            return
        if not combo:
            # 只按了修饰键：继续等主键
            modifiers = []
            if event.modifiers() & Qt.ControlModifier:
                modifiers.append("Ctrl")
            if event.modifiers() & Qt.AltModifier:
                modifiers.append("Alt")
            if event.modifiers() & Qt.ShiftModifier:
                modifiers.append("Shift")
            if event.modifiers() & Qt.MetaModifier:
                modifiers.append("Win")
            self._field.setText("+".join(modifiers) + "+…" if modifiers else "请按下快捷键")
            return

        self._pending = combo
        self._field.setText(combo)
        self._field.setStyleSheet(f"color: {DARK.accent}; border-color: {DARK.accent};")

        # 交给上层去真正注册；成功后会调用 set_combo 并结束录制
        self.recorded.emit(combo)

    def finish_recording(self, accepted: bool) -> None:
        """上层注册完成后回调。失败时保留原绑定。"""
        was_editing = self._editing
        self._editing = False
        self._field.setStyleSheet("")
        if accepted:
            self._combo = self._pending
        self._pending = ""
        self._refresh()
        self._restyle()
        if was_editing:
            self.editing_finished.emit()

    # ---- 内部 -----------------------------------------------------------
    def _on_clear(self) -> None:
        self._combo = ""
        self._pending = ""
        self._error = ""
        self._refresh()
        self.cleared.emit()

    def _refresh(self) -> None:
        self._field.setText(self._combo or "未绑定")
        self._field.setStyleSheet(
            f"color: {DARK.text_faint};" if not self._combo else ""
        )

    def _restyle(self) -> None:
        self._field.style().unpolish(self._field)
        self._field.style().polish(self._field)
