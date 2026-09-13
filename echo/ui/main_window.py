"""主窗口：侧栏、顶栏、页面栈、状态栏、托盘联动。

布局按方案 §7.2 逐项落实：

| 区域 | 实现 |
| --- | --- |
| 顶部 | 项目名称、当前状态、开始/暂停主按钮 |
| 左侧窄侧栏 | 采集 / 图片库 / 设置 三个入口，底部 echo 字标 |
| 中央预览区 | 在采集页里（:class:`~echo.ui.pages.capture_page.CapturePage`） |
| 右侧设置区 | 同上 |
| 底部最近截图 | 同上 |
| 底部状态栏 | 已保存、相似图数量、待写入数量、错误或等待原因 |

小窗口适配（方案 §7.1）：窗口宽度小于 :data:`COMPACT_WIDTH` 时侧栏自动收成图标条，
echo 字标从侧栏底部移到状态栏右下角——这正是方案给出的降级方案。
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCloseEvent, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME, __version__
from .. import app as app_mod
from ..logging_setup import get_logger
from ..paths import app_data_dir
from ..pipeline import CaptureState
from ..region import RegionMode
from . import icons
from .pages.capture_page import CapturePage
from .pages.library_page import LibraryPage
from .pages.settings_page import SettingsPage
from .region_picker import RegionPickerOverlay
from .theme import DARK, METRICS, build_qss, level_colors
from .tray import TrayIcon
from .widgets import StatePill

log = get_logger("ui.window")

COMPACT_WIDTH = 1000     # 窄于这个宽度就收拢侧栏（方案 §7.1）

NAV_ITEMS = (
    ("采集", "capture"),
    ("图片库", "library"),
    ("设置", "settings"),
)


class MainWindow(QMainWindow):
    def __init__(self, app: app_mod.EchoApp):
        super().__init__()
        self.app = app
        self._force_quit = False
        self._tray_tip_shown = app.config.ui.tray_notify_tip_shown
        # 用 None 而不是 False：首帧必须无条件同步一次，否则窗口一上来就是窄的
        # （小屏或上次拉窄退出）时，侧栏会停在展开态不收敛。
        self._compact: bool | None = None
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._reset_status_message)

        # 不写 APP_DISPLAY_NAME：Qt 会自动在标题后拼上 applicationDisplayName，
        # 两处都带就会显示成「echo · 游戏截图与数据集采集 - echo」。
        self.setWindowTitle("游戏截图与数据集采集")
        self.setMinimumSize(*METRICS.min_window)
        self.resize(*METRICS.default_window)
        self.setStyleSheet(build_qss())
        self.setWindowIcon(icons.app_icon(DARK.accent))

        self._build_ui()
        self._build_tray()
        self._wire()

        self.app.preview.connect(self._on_preview)
        self.app.notice.connect(self._on_notice)
        self.app.session_changed.connect(self._on_session_changed)

        QTimer.singleShot(0, self._after_show)

    # ==================================================================
    # 界面
    # ==================================================================
    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("RootSurface")
        self.setCentralWidget(root)

        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_sidebar())
        layout.addWidget(self._build_body(), 1)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget(self)
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(METRICS.sidebar_width)

        box = QVBoxLayout(sidebar)
        box.setContentsMargins(12, 14, 12, 14)
        box.setSpacing(4)

        self.nav_buttons: list[QPushButton] = []
        for index, (label, icon) in enumerate(NAV_ITEMS):
            button = QPushButton(f"  {label}", sidebar)
            button.setProperty("variant", "nav")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setIcon(icons.qicon(icon, DARK.text_dim, 17))
            button.setIconSize(QSize(17, 17))
            button.clicked.connect(lambda _=False, i=index: self._on_nav(i))
            box.addWidget(button)
            self.nav_buttons.append(button)
        self.nav_buttons[0].setChecked(True)

        box.addStretch(1)

        # echo 字标：方案 §7.3 要求全小写、柔和灰、视觉权重低于功能入口
        self.echo_mark = QLabel("echo", sidebar)
        self.echo_mark.setObjectName("EchoMark")
        self.echo_mark.setAlignment(Qt.AlignLeft)
        self.echo_mark.setContentsMargins(12, 0, 0, 0)
        self.echo_mark.setToolTip(f"{APP_DISPLAY_NAME} {__version__}")
        box.addWidget(self.echo_mark)

        self.sidebar = sidebar
        return sidebar

    def _build_body(self) -> QWidget:
        body = QWidget(self)
        box = QVBoxLayout(body)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        # 顺序有讲究：顶栏要放采集页持有的两个主操作按钮，所以页面必须先建好。
        self.stack = QStackedWidget(body)
        self.capture_page = CapturePage(self.app, self.stack)
        self.library_page = LibraryPage(self.app, self.stack)
        self.settings_page = SettingsPage(self.app, self.stack)
        self.stack.addWidget(self.capture_page)
        self.stack.addWidget(self.library_page)
        self.stack.addWidget(self.settings_page)

        box.addWidget(self._build_topbar())
        box.addWidget(self.stack, 1)
        box.addWidget(self._build_statusbar())
        return body

    def _build_topbar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("TopBar")
        bar.setFixedHeight(METRICS.topbar_height)

        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 0, 20, 0)
        row.setSpacing(12)

        self.project_label = QLabel(self.app.project_name, bar)
        self.project_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        row.addWidget(self.project_label)

        self.state_pill = StatePill(bar)
        row.addWidget(self.state_pill)

        self.state_reason = QLabel("", bar)
        self.state_reason.setObjectName("Caption")
        self.state_reason.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        row.addWidget(self.state_reason, 1)

        row.addWidget(self.capture_page.btn_shot)
        row.addWidget(self.capture_page.btn_primary)
        return bar

    def _build_statusbar(self) -> QWidget:
        bar = QWidget(self)
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(METRICS.statusbar_height)

        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 0, 20, 0)
        row.setSpacing(16)

        self.status_saved = self._status_item(row, "已保存 0")
        self.status_similar = self._status_item(row, "相似 0")
        self.status_pending = self._status_item(row, "待写入 0")
        self.status_dedup = self._status_item(row, "去重 0")

        self.status_message = QLabel("", bar)
        self.status_message.setObjectName("Caption")
        self.status_message.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.status_message.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self.status_message, 1)

        # 侧栏收起时 echo 字标搬到这里（方案 §7.3）
        self.echo_mark_small = QLabel("echo", bar)
        self.echo_mark_small.setObjectName("EchoMark")
        self.echo_mark_small.setVisible(False)
        row.addWidget(self.echo_mark_small)
        return bar

    def _status_item(self, row: QHBoxLayout, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Caption")
        row.addWidget(label)
        return label

    # ==================================================================
    # 托盘
    # ==================================================================
    def _build_tray(self) -> None:
        self.tray = TrayIcon(self)
        self.tray.open_requested.connect(self.show_from_tray)
        self.tray.shot_requested.connect(self.app.request_manual)
        self.tray.toggle_capture_requested.connect(self.app.toggle_auto)
        self.tray.open_folder_requested.connect(self.app.open_save_directory)
        self.tray.quit_requested.connect(self.quit_application)

        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        else:  # pragma: no cover
            log.warning("系统托盘不可用，关闭按钮将直接退出")
        self.app.toggle_window_requested.connect(self.toggle_window)
        self.tray.activated.connect(lambda _r: None)

    def _on_tray_message(self, *_args) -> None:
        pass

    # ==================================================================
    # 接线
    # ==================================================================
    def _wire(self) -> None:
        app = self.app
        app.state_changed.connect(self._on_state)
        app.counters_changed.connect(self._on_counters)
        app.config_changed.connect(self._on_config_changed)
        app.hotkey_error.connect(
            lambda action, combo, message: self._on_notice("warn", f"快捷键 {combo}：{message}")
        )

        self.capture_page.drag_requested.connect(self._on_drag_requested)
        self.capture_page.pick_directory_requested.connect(self._on_pick_directory)
        self.capture_page.block_reason.connect(self._on_block_reason)

        # 拖拽定位期间暂停预览刷新意义不大，但要让遮罩拿到焦点
        self._overlay: RegionPickerOverlay | None = None

    def _after_show(self) -> None:
        self._on_config_changed()
        problems = self.app.start()
        if self.app.recovery_note:
            self._on_notice("info", f"已恢复上次的残留：{self.app.recovery_note}")
        if problems:
            self._on_notice("warn", problems[0])
        if self.app.pipeline.source is None:
            self.capture_page.show_empty_state(
                "选择游戏窗口，开始采集第一张图片",
                "先在右侧「采集源」里选一个窗口或显示器；"
                "再把工具收起到托盘，用快捷键 F8 截图。",
            )
        self._refresh_compact()

    # ==================================================================
    # 状态
    # ==================================================================
    def _on_block_reason(self, text: str) -> None:
        """主按钮被前置条件禁用时，把原因写在顶栏说明位上（warn 色）。"""
        self.state_reason.setText(text)
        self.state_reason.setStyleSheet(f"color: {level_colors('warn')[0]};")

    def _on_state(self, state_value: str, reason: str) -> None:
        try:
            state = CaptureState(state_value)
        except ValueError:
            state = CaptureState.UNCONFIGURED
        self.state_pill.set_state(state_value, state.label)
        self.state_reason.setText(reason)
        self.state_reason.setStyleSheet(
            f"color: {level_colors('error' if state.is_problem else 'info')[0]};"
        )
        self.tray.update_state(state, reason)
        self.capture_page.refresh_all()

    def _on_counters(self, counters: dict) -> None:
        self.status_saved.setText(f"已保存 {counters.get('saved', 0)}")
        self.status_similar.setText(f"相似 {counters.get('similar', 0)}")
        self.status_pending.setText(f"待写入 {counters.get('pending_items', 0)}")
        dropped = counters.get("dropped_auto", 0)
        skipped = counters.get("skipped_dup", 0)
        self.status_dedup.setText(f"去重 {skipped} · 丢帧 {dropped}")
        # 方案 §7.2：界面「已保存」只统计真实落盘成功的数量，不把入队当成保存成功
        session = self.app.pipeline.session
        self.tray.update_counters(counters, session.uid if session else "")

    def _on_config_changed(self) -> None:
        self.project_label.setText(self.app.project_name)
        self._refresh_compact()

    def _on_session_changed(self, session_uid: str) -> None:
        if session_uid:
            self._show_status_message("info", f"当前批次 {session_uid}")

    def _on_preview(self, payload) -> None:
        self.capture_page._on_preview(payload)

    # ==================================================================
    # 提示
    # ==================================================================
    def _on_notice(self, level: str, text: str) -> None:
        self._show_status_message(level, text)
        if level in {"error", "warn"}:
            # 方案 §7.2：失败通过托盘提示或状态栏反馈，不弹阻塞对话框
            self.tray.notify(APP_DISPLAY_NAME, text, level)

    def _show_status_message(self, level: str, text: str) -> None:
        color, _ = level_colors(level)
        self.status_message.setText(text)
        self.status_message.setStyleSheet(f"color: {color};")
        self._notice_timer.start(6000 if level in {"error", "warn"} else 3000)

    def _reset_status_message(self) -> None:
        self.status_message.setText("")
        self.status_message.setStyleSheet("")

    # ==================================================================
    # 导航
    # ==================================================================
    def _on_nav(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if index == 0:
            self.app.set_preview(True)
        else:
            # 方案 §8.3：工具进入后台后暂停预览刷新；这里是离开采集页，同理
            self.app.set_preview(False)
        if index == 1:
            self.library_page.refresh()
        elif index == 2:
            self.settings_page.refresh()
        self._refresh_nav_icons()

    def _refresh_nav_icons(self) -> None:
        for index, (_label, icon) in enumerate(NAV_ITEMS):
            active = index == self.stack.currentIndex()
            color = DARK.accent if active else DARK.text_dim
            self.nav_buttons[index].setIcon(icons.qicon(icon, color, 17))

    # ==================================================================
    # 拖拽定位
    # ==================================================================
    def _on_drag_requested(self) -> None:
        source = self.app.pipeline.source
        if source is None:
            self._on_notice("warn", "请先选择采集源，再拖拽定位选区")
            return

        region = self.app.pipeline.region
        if not region.fits_within(source.source_width, source.source_height):
            self._on_notice(
                "warn",
                f"当前尺寸 {region.width}×{region.height} 超出采集源 "
                f"{source.source_width}×{source.source_height}，请先改小后再拖拽",
            )
            return

        window = self.windowHandle()
        screen_name = window.screen().name() if window and window.screen() else ""

        overlay = RegionPickerOverlay(
            source_left=source.origin_left,
            source_top=source.origin_top,
            source_width=source.source_width,
            source_height=source.source_height,
            region=region,
            screen_name=source.monitor_device or screen_name,
        )
        overlay.picked.connect(self._on_region_picked)
        overlay.cancelled.connect(lambda: self._on_notice("info", "已取消拖拽定位"))
        overlay.destroyed.connect(lambda: setattr(self, "_overlay", None))
        self._overlay = overlay
        self.hide()
        overlay.show()

    def _on_region_picked(self, region) -> None:
        self.app.on_drag_picked(region)
        self.show_from_tray()

    def _on_pick_directory(self) -> None:
        current = self.app.config.storage.save_directory or str(Path.home())
        directory = QFileDialog.getExistingDirectory(
            self, "选择保存位置", current,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )
        if not directory:
            return
        self.app.set_save_directory(directory)

    # ==================================================================
    # 窗口行为
    # ==================================================================
    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.stack.currentIndex() == 0:
            self.app.set_preview(True)

    def toggle_window(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
            self.app.set_preview(False)
        else:
            self.show_from_tray()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refresh_compact()

    def _refresh_compact(self) -> None:
        """低分辨率下收拢侧栏（方案 §7.1）。"""
        compact = self.width() < COMPACT_WIDTH
        if compact == self._compact:
            return
        self._compact = compact

        if compact:
            self.sidebar.setFixedWidth(METRICS.sidebar_width_compact)
            for index, (label, _icon) in enumerate(NAV_ITEMS):
                self.nav_buttons[index].setText("")
                self.nav_buttons[index].setToolTip(label)
                self.nav_buttons[index].setFixedHeight(40)
            self.echo_mark.setVisible(False)
            self.echo_mark_small.setVisible(True)
        else:
            self.sidebar.setFixedWidth(METRICS.sidebar_width)
            for index, (label, _icon) in enumerate(NAV_ITEMS):
                self.nav_buttons[index].setText(f"  {label}")
                self.nav_buttons[index].setToolTip("")
            self.echo_mark.setVisible(True)
            self.echo_mark_small.setVisible(False)

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        # 方案 §6.1：工具最小化后继续运行，但暂停实时预览以减少占用
        if event.type() == event.Type.WindowStateChange:
            if self.isMinimized():
                self.app.set_preview(False)
            elif self.isVisible() and self.stack.currentIndex() == 0:
                self.app.set_preview(True)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._force_quit or not self.app.config.ui.close_to_tray:
            self._shutdown_and_accept(event)
            return

        if not QSystemTrayIcon.isSystemTrayAvailable():  # pragma: no cover
            self._shutdown_and_accept(event)
            return

        event.ignore()
        self.hide()
        self.app.set_preview(False)
        self._tray_tip_shown = self.tray.show_tray_tip_once(self._tray_tip_shown)
        if self._tray_tip_shown and not self.app.config.ui.tray_notify_tip_shown:
            self.app.config.ui.tray_notify_tip_shown = True
            self.app.save_config_soon()

    def _shutdown_and_accept(self, event: QCloseEvent) -> None:
        self._force_quit = True
        try:
            self.app.shutdown()
        finally:
            event.accept()
            QGuiApplication.quit()

    def quit_application(self) -> None:
        """托盘菜单的「退出」：必须真正结束后台进程（方案 §6.1）。"""
        self._force_quit = True
        self.app.shutdown()
        self.tray.hide()
        QGuiApplication.quit()
