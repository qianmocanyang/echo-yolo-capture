"""系统托盘。

方案 §6.1 规定的托盘行为：

* 点关闭按钮默认收起到托盘，**首次要提示这个行为**——不提示的话用户会以为程序没退干净。
* 托盘菜单：打开主界面、截图、开始 / 暂停、打开保存目录、退出。
* 图标用状态变化区分待命 / 采集中 / 异常；工具提示同步显示数量与保存位置。
* 用户选择退出时**必须真正结束后台进程**，不做"假退出"。

图标是运行时用矢量画的，不依赖外部 .ico 文件：
托盘图标在 16×16 下能用的细节非常有限，所以用最简单的图形——
一个圆环 + 中心点，靠颜色区分状态，比塞进复杂的图形更清楚。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .. import APP_DISPLAY_NAME, __version__
from ..logging_setup import get_logger
from ..pipeline import CaptureState
from .theme import DARK

log = get_logger("ui.tray")


def make_tray_icon(color: str, *, filled: bool = False, size: int = 64) -> QIcon:
    """画一个环形 + 中心点的托盘图标。"""
    icon = QIcon()
    for ratio in (1, 2):
        canvas = QPixmap(int(size * ratio), int(size * ratio))
        canvas.setDevicePixelRatio(ratio)
        canvas.fill(Qt.transparent)

        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(color))
        pen.setWidthF(size * 0.13)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)

        margin = size * 0.2
        painter.drawEllipse(
            int(margin), int(margin),
            int(size - margin * 2), int(size - margin * 2),
        )
        if filled:
            painter.setBrush(QColor(color))
            painter.setPen(Qt.NoPen)
            dot = size * 0.16
            painter.drawEllipse(
                int(size / 2 - dot / 2), int(size / 2 - dot / 2), int(dot), int(dot)
            )
        painter.end()

        icon.addPixmap(canvas)
    return icon


class TrayIcon(QSystemTrayIcon):
    """托盘图标与菜单。"""

    open_requested = Signal()
    shot_requested = Signal()
    toggle_capture_requested = Signal()
    open_folder_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = CaptureState.UNCONFIGURED
        self._counters_text = ""
        self._session_text = ""

        self._menu = QMenu()
        self._act_open = QAction("打开主界面", self._menu)
        self._act_shot = QAction("截图一次", self._menu)
        self._act_toggle = QAction("开始采集", self._menu)
        self._act_folder = QAction("打开保存目录", self._menu)
        self._act_quit = QAction("退出", self._menu)

        self._menu.addAction(self._act_open)
        self._menu.addSeparator()
        self._menu.addAction(self._act_shot)
        self._menu.addAction(self._act_toggle)
        self._menu.addAction(self._act_folder)
        self._menu.addSeparator()
        self._menu.addAction(self._act_quit)
        self.setContextMenu(self._menu)

        self._act_open.triggered.connect(self.open_requested.emit)
        self._act_shot.triggered.connect(self.shot_requested.emit)
        self._act_toggle.triggered.connect(self.toggle_capture_requested.emit)
        self._act_folder.triggered.connect(self.open_folder_requested.emit)
        self._act_quit.triggered.connect(self.quit_requested.emit)

        self.activated.connect(self._on_activated)
        self.setIcon(make_tray_icon(DARK.text_faint))
        self.setToolTip(APP_DISPLAY_NAME)
        self.update_state(CaptureState.UNCONFIGURED, "未配置")

    # ---- 状态 -----------------------------------------------------------
    def update_state(self, state: CaptureState, reason: str) -> None:
        self._state = state
        color, filled = _visual(state)
        self.setIcon(make_tray_icon(color, filled=filled))
        self._refresh_tooltip()
        if state is CaptureState.CAPTURING:
            self._act_toggle.setText("暂停采集")
            self._act_toggle.setEnabled(True)
        elif state is CaptureState.PAUSED:
            self._act_toggle.setText("继续采集")
            self._act_toggle.setEnabled(True)
        elif state is CaptureState.SAVE_ERROR:
            self._act_toggle.setText("保存异常")
            self._act_toggle.setEnabled(False)
        else:
            self._act_toggle.setText("开始采集")
            self._act_toggle.setEnabled(state is not CaptureState.UNCONFIGURED)

        self._act_shot.setEnabled(state not in {CaptureState.UNCONFIGURED, CaptureState.SAVE_ERROR})

    def update_counters(self, counters: dict, session_text: str = "") -> None:
        self._counters_text = (
            f"已保存 {counters.get('saved', 0)}"
            f" · 去重 {counters.get('skipped_dup', 0)}"
            f" · 待写入 {counters.get('pending_items', 0)}"
        )
        self._session_text = session_text
        self._refresh_tooltip()

    def _refresh_tooltip(self) -> None:
        lines = [f"{APP_DISPLAY_NAME} — {self._state.label}"]
        if self._counters_text:
            lines.append(self._counters_text)
        if self._session_text:
            lines.append(f"批次 {self._session_text}")
        saved = self._counters_text or ""
        self.setToolTip("\n".join(lines))

    # ---- 交互 -----------------------------------------------------------
    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            self.open_requested.emit()
        elif reason == QSystemTrayIcon.Trigger:
            # 单击也打开：Windows 托盘的用户预期就是单击唤起
            self.open_requested.emit()

    def notify(self, title: str, text: str, level: str = "info") -> None:
        icon = {
            "info": QSystemTrayIcon.Information,
            "warn": QSystemTrayIcon.Warning,
            "error": QSystemTrayIcon.Critical,
            "ok": QSystemTrayIcon.Information,
        }.get(level, QSystemTrayIcon.Information)
        try:
            self.showMessage(title, text, icon, 4000)
        except Exception as exc:  # pragma: no cover
            log.debug("托盘提示失败：%s", exc)

    def show_tray_tip_once(self, already_shown: bool) -> bool:
        """首次收起到托盘时提示一次（方案 §6.1）。返回是否已经提示过。"""
        if already_shown:
            return True
        self.notify(
            "已收起到托盘",
            f"{APP_DISPLAY_NAME} 仍在后台运行，快捷键与采集继续有效。"
            "要真正退出，请右键托盘图标选择「退出」。",
        )
        return True


def _visual(state: CaptureState) -> tuple[str, bool]:
    if state is CaptureState.CAPTURING:
        return DARK.accent, True
    if state is CaptureState.PAUSED:
        return DARK.warn, False
    if state is CaptureState.SAVE_ERROR:
        return DARK.danger, True
    if state is CaptureState.WAITING:
        return DARK.warn, False
    if state is CaptureState.IDLE:
        return DARK.text_dim, False
    return DARK.text_faint, False
