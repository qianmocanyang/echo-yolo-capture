"""设置页：应用行为、兼容性说明、运行信息。

这一页刻意把「兼容性矩阵」放在显眼位置。方案 §6.1 用一张表说明了两类后端在
各种场景下的行为差异（游戏失焦、被遮挡、最小化、锁屏……）。
这些边界如果只写在文档里，用户遇到时只会觉得"这工具坏了"。
把它直接摆在设置页，用户能自己判断"我这种情况本来就采不到"。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ... import APP_DISPLAY_NAME, __version__
from ... import app as app_mod
from ...capture import COMPATIBILITY_MATRIX
from ...logging_setup import get_logger
from ...paths import app_data_dir, config_path, log_dir
from .. import icons
from ..widgets import (
    Card,
    CardHeader,
    LabeledSwitch,
    ScrollColumn,
    ToolRow,
    caption,
    hint,
)
from ..theme import DARK, METRICS

log = get_logger("ui.settings")


class SettingsPage(QWidget):
    def __init__(self, app: app_mod.EchoApp, parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self._loading = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(0)

        scroll = ScrollColumn(self, spacing=12, margins=(0, 0, 12, 0))
        outer.addWidget(scroll)

        headline = QLabel("设置", self)
        headline.setObjectName("PageTitle")
        scroll.add(headline)

        scroll.add(self._card_behavior())
        scroll.add(self._card_hotkeys())
        scroll.add(self._card_compatibility())
        scroll.add(self._card_backends())
        scroll.add(self._card_paths())
        scroll.add(self._card_about())
        scroll.add_stretch()

        app.config_changed.connect(self.refresh)
        self._loading = False
        self.refresh()

    # ---- 应用行为 --------------------------------------------------------
    def _card_behavior(self) -> Card:
        card = Card()
        card.add(CardHeader("应用行为", icon="settings"))

        self.switch_tray = LabeledSwitch(
            "关闭按钮收起到系统托盘",
            card,
            description="开启后点关闭不会退出，热键与采集继续。退出请用托盘菜单的「退出」。",
        )
        self.switch_tray.toggled.connect(self._on_tray_toggled)
        card.add(self.switch_tray)

        self.switch_notify = LabeledSwitch(
            "后台保存成功时通知",
            card,
            description="默认关闭（方案 §7.2）。开启后每次落盘成功会在托盘弹出提示。",
        )
        self.switch_notify.toggled.connect(self._on_notify_toggled)
        card.add(self.switch_notify)

        self.switch_sound = LabeledSwitch(
            "截图音效",
            card,
            description="首版不播放音频，这里保留开关位以便后续接入，现在切换不会有声音。",
        )
        self.switch_sound.setEnabled(False)
        card.add(self.switch_sound)
        return card

    # ---- 快捷键 ----------------------------------------------------------
    def _card_hotkeys(self) -> Card:
        card = Card()
        card.add(CardHeader("快捷键说明", icon="keyboard"))
        card.add(hint(
            "快捷键在任何页面、以及工具收起到托盘后都有效。"
            "注册失败时界面会保留原绑定并说明原因，不会出现「新键没设上、旧键也没了」的情况。",
            card,
        ))
        card.add(hint(
            "少数游戏（独占全屏、带反作弊保护）可能拦截任意按键。"
            "如果热键在游戏里没反应，请把该游戏设为「无边框窗口」后重试。",
            card,
        ))
        return card

    # ---- 兼容性矩阵 ------------------------------------------------------
    def _card_compatibility(self) -> Card:
        card = Card()
        card.add(CardHeader(
            "采集兼容性", icon="shield",
            subtitle="这两类后端的能力边界是客观存在的，不是配置问题",
        ))
        for row in COMPATIBILITY_MATRIX:
            block = QVBoxLayout()
            block.setSpacing(3)
            line = QLabel(row.scenario, card)
            line.setStyleSheet(f"color: {DARK.text}; font-weight: 600;")
            block.addWidget(line)
            block.addWidget(hint(f"显示器（DXGI）：{row.monitor_backend}", card))
            block.addWidget(hint(f"窗口（WGC）：{row.window_backend}", card))
            card.add_layout(block)
        card.add(hint(
            "游戏最小化、系统锁屏或游戏停止渲染后，本工具不承诺仍能产生有效的新截图——"
            "这时会进入「等待画面」，不会用旧画面冒充新截图。",
            card,
        ))
        return card

    # ---- 后端能力 --------------------------------------------------------
    def _card_backends(self) -> Card:
        card = Card()
        header = CardHeader("采集后端", icon="layers")
        probe_button = QPushButton("重新探测", card)
        probe_button.setProperty("variant", "ghost")
        probe_button.setCursor(Qt.PointingHandCursor)
        probe_button.clicked.connect(self._on_probe)
        header.add_right(probe_button)
        card.add(header)

        self.backend_holder = QVBoxLayout()
        self.backend_holder.setSpacing(8)
        card.add_layout(self.backend_holder)
        return card

    def _fill_backends(self) -> None:
        while self.backend_holder.count():
            item = self.backend_holder.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for key, capability in self.app.capabilities().items():
            block = QVBoxLayout()
            block.setSpacing(3)
            line = QLabel(capability.display_name, None)
            color = DARK.ok if capability.available else DARK.danger
            line.setStyleSheet(f"color: {color}; font-weight: 600;")
            block.addWidget(line)
            kinds = "、".join(k.value for k in capability.kinds) or "无"
            block.addWidget(hint(f"支持：{kinds} · 状态：{capability.summary}", None))
            for note in capability.notes:
                block.addWidget(hint(f"· {note}", None))
            if not capability.available:
                block.addWidget(hint(f"原因：{capability.reason}", None))
            self.backend_holder.addLayout(block)

    # ---- 路径信息 --------------------------------------------------------
    def _card_paths(self) -> Card:
        card = Card()
        card.add(CardHeader("运行位置", icon="folder"))
        for label, value in (
            ("配置", str(config_path())),
            ("日志", str(log_dir() / "echo.log")),
            ("项目数据库", str(self.app.pipeline.layout().db_path) if self.app.pipeline.layout() else "（未设置保存位置）"),
            ("导出的数据集", str(self.app.pipeline.layout().exports_dir) if self.app.pipeline.layout() else "（未设置保存位置）"),
        ):
            row = QVBoxLayout()
            row.setSpacing(2)
            tag = QLabel(label, card)
            tag.setObjectName("FieldLabel")
            row.addWidget(tag)
            path = QLabel(value, card)
            path.setObjectName("Mono")
            path.setWordWrap(True)
            row.addWidget(path)
            card.add_layout(row)

        tools = ToolRow()
        tools.add("打开配置目录", lambda: self._open(app_data_dir()))
        tools.add("打开保存目录", self.app.open_save_directory)
        tools.add_stretch()
        card.add(tools)
        return card

    def _open(self, path: Path) -> None:
        import os

        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            self.app.notice.emit("error", f"无法打开：{exc}")

    # ---- 关于 ------------------------------------------------------------
    def _card_about(self) -> Card:
        card = Card()
        mark = QLabel("echo", card)
        mark.setStyleSheet(
            f"color: {DARK.echo}; font-size: 20px; letter-spacing: 4px;"
        )
        card.add(mark)
        card.add(hint(f"{APP_DISPLAY_NAME} {__version__} · 游戏截图与 YOLO 数据集采集工具", card))
        card.add(hint(
            "对应技术方案 v1.1。首版范围：自定义尺寸与定位、游戏画面捕获、全局快捷键、"
            "保存目录、后台托盘、PNG 保存、配置记忆，以及定时采集、连拍、批次管理、"
            "审核、去重标记、标注导入与 YOLO 导出。",
            card,
        ))
        card.add(hint(
            "内置画框标注与模型预标注属于后续版本；首版配合 CVAT 完成人工标注。",
            card,
        ))
        card.add(hint(
            "echo 字标只存在于本软件界面。PNG 原图、标注文件与训练数据中不含任何水印、"
            "选区边框或提示文字。",
            card,
        ))
        return card

    # ==================================================================
    # 交互
    # ==================================================================
    def _on_tray_toggled(self, value: bool) -> None:
        if self._loading:
            return
        self.app.config.ui.close_to_tray = bool(value)
        self.app.save_config_soon()

    def _on_notify_toggled(self, value: bool) -> None:
        if self._loading:
            return
        self.app.config.ui.success_notification = bool(value)
        self.app.save_config_soon()

    def _on_probe(self) -> None:
        self.app._capabilities = {}
        self._fill_backends()
        self.app.notice.emit("info", "已重新探测采集后端")

    def refresh(self) -> None:
        self._loading = True
        try:
            self.switch_tray.setChecked(self.app.config.ui.close_to_tray)
            self.switch_notify.setChecked(self.app.config.ui.success_notification)
            self.switch_sound.setChecked(self.app.config.ui.sound_feedback)
        finally:
            self._loading = False
        self._fill_backends()
