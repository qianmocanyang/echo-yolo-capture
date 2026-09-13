"""采集页：中央预览 + 右侧设置区（方案 §7.2 的主体）。

方案对右侧设置区的要求是「采集源、宽高、定位、快捷键、保存目录，**按任务顺序排列**」。
所以卡片顺序就是用户做完一次采集的实际思维顺序：

1. 采集源 —— 截哪儿
2. 采集范围 —— 截多大
3. 定位 —— 截在哪一块
4. 采集节奏 —— 多久截一次
5. 快捷键 —— 怎么触发
6. 保存位置 —— 存哪儿

把"快捷键"排在"保存位置"之前是有意的：用户在游戏里按键之前，
需要先确认按键是哪个；而保存位置设一次就不用再动。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ... import app as app_mod
from ...capture import SourceKind
from ...region import SIZE_PRESETS, Region, RegionMode, parse_size
from .. import icons
from ..hotkey_edit import HotkeyEdit
from ..preview import PreviewPanel, ThumbStrip
from ..widgets import (
    Card,
    CardHeader,
    FieldRow,
    Segmented,
    ToolRow,
    caption,
    hint,
    title,
)
from ..theme import DARK, METRICS


class _IntField(QLineEdit):
    def __init__(self, value: int, parent: QWidget | None = None, *, width: int = 78, maximum: int = 16384):
        super().__init__(str(value), parent)
        self.setFixedWidth(width)
        self.setAlignment(Qt.AlignCenter)
        self.setValidator(QIntValidator(1, maximum, self))

    def value(self) -> int | None:
        return parse_size(self.text())

    def set_value(self, value: int) -> None:
        self.setText(str(value))


class CapturePage(QWidget):
    """采集主页面。"""

    def __init__(self, app: app_mod.EchoApp, parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self._loading = False
        self._source_entries: list = []

        # 顶栏上的两个主操作（方案 §7.2 的「开始/暂停主按钮」与「截图一次」）：
        # 状态与行为都属于采集页，所以按钮由本页创建；位置由主窗口摆到顶栏，
        # 因此这里只建不排——不加进本页布局，等主窗口 addWidget 时自动 reparent。
        self.btn_shot = QPushButton("截图一次", self)
        self.btn_shot.setProperty("variant", "ghost")
        self.btn_shot.setCursor(Qt.PointingHandCursor)
        self.btn_shot.setIcon(icons.qicon("capture", DARK.text_dim, 16))
        self.btn_shot.setIconSize(QSize(16, 16))

        self.btn_primary = QPushButton("开始采集", self)
        self.btn_primary.setProperty("variant", "primary")
        self.btn_primary.setProperty("size", "large")
        self.btn_primary.setCursor(Qt.PointingHandCursor)
        self.btn_primary.setIcon(icons.qicon("play", DARK.on_accent, 15))
        self.btn_primary.setIconSize(QSize(15, 15))
        self.btn_primary.setMinimumWidth(124)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(16)

        outer.addWidget(self._build_center(), 1)
        outer.addWidget(self._build_settings())

        self._wire()
        self.refresh_all()

    # ==================================================================
    # 中央区
    # ==================================================================
    def _build_center(self) -> QWidget:
        holder = QWidget(self)
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)

        self.preview = PreviewPanel(self)
        self.preview.mode_changed.connect(self._on_preview_mode)
        box.addWidget(self.preview, 1)

        recent_header = QHBoxLayout()
        recent_header.setSpacing(8)
        label = QLabel("最近截图", holder)
        label.setObjectName("CardTitle")
        recent_header.addWidget(label)
        self.recent_count = QLabel("", holder)
        self.recent_count.setObjectName("Caption")
        recent_header.addWidget(self.recent_count)
        recent_header.addStretch(1)

        self.btn_recent_folder = QPushButton("在文件夹中打开", holder)
        self.btn_recent_folder.setProperty("variant", "ghost")
        self.btn_recent_folder.setCursor(Qt.PointingHandCursor)
        recent_header.addWidget(self.btn_recent_folder)
        box.addLayout(recent_header)

        self.thumbs = ThumbStrip(holder)
        box.addWidget(self.thumbs)
        return holder

    # ==================================================================
    # 右侧设置区
    # ==================================================================
    def _build_settings(self) -> QWidget:
        from ..widgets import ScrollColumn

        holder = QWidget(self)
        holder.setFixedWidth(METRICS.settings_width)
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)

        scroll = ScrollColumn(holder, spacing=12)
        box.addWidget(scroll)

        scroll.add(self._card_source())
        scroll.add(self._card_size())
        scroll.add(self._card_position())
        scroll.add(self._card_pacing())
        scroll.add(self._card_hotkeys())
        scroll.add(self._card_storage())
        scroll.add_stretch()
        return holder

    # ---- 卡片 1：采集源 --------------------------------------------------
    def _card_source(self) -> Card:
        card = Card()
        card.add(CardHeader("采集源", icon="target", subtitle="选择要采集的游戏窗口或显示器"))

        self.kind_seg = Segmented(
            ["显示器", "游戏窗口"],
            icons_map={0: "monitor", 1: "window"},
        )
        self.kind_seg.changed.connect(self._on_kind_changed)
        card.add(self.kind_seg)

        self.source_combo = QComboBox()
        self.source_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.source_combo.currentIndexChanged.connect(self._on_source_selected)
        card.add(self.source_combo)

        self.source_note = hint("", card)
        card.add(self.source_note)

        tools = ToolRow()
        self.btn_refresh = tools.add(
            "刷新列表", self._on_refresh_sources, icon="refresh", tooltip="重新枚举可采集的窗口"
        )
        self.btn_backend = tools.add(
            "后端：自动", self._on_switch_backend, icon="layers",
            tooltip="采集后端决定「失焦后能否继续采集」等能力",
        )
        tools.add_stretch()
        card.add(tools)

        self.backend_detail = hint("", card)
        card.add(self.backend_detail)
        return card

    # ---- 卡片 2：采集范围 ------------------------------------------------
    def _card_size(self) -> Card:
        card = Card()
        card.add(CardHeader("采集范围", icon="crosshair", subtitle="从画面中原尺寸裁剪，不做缩放"))

        row = QHBoxLayout()
        row.setSpacing(8)
        self.width_field = _IntField(320)
        self.height_field = _IntField(320)
        row.addWidget(QLabel("宽", card))
        row.addWidget(self.width_field)
        row.addWidget(QLabel("高", card))
        row.addWidget(self.height_field)
        self.width_field.textChanged.connect(self._on_size_edited)
        self.height_field.textChanged.connect(self._on_size_edited)
        card.add_layout(row)

        grid = QGridLayout()
        grid.setSpacing(6)
        self.preset_buttons: list[QPushButton] = []
        for index, (label, _w, _h) in enumerate(SIZE_PRESETS):
            button = QPushButton(label)
            button.setCursor(Qt.PointingHandCursor)
            button.setProperty("variant", "ghost")
            button.clicked.connect(lambda _=False, i=index: self._on_preset(i))
            grid.addWidget(button, index // 2, index % 2)
            self.preset_buttons.append(button)
        card.add_layout(grid)

        self.ratio_box = QPushButton("锁定宽高比", card)
        self.ratio_box.setCheckable(True)
        self.ratio_box.setCursor(Qt.PointingHandCursor)
        self.ratio_box.toggled.connect(self._on_ratio_toggled)
        card.add(self.ratio_box)

        self.size_note = hint("", card)
        card.add(self.size_note)
        return card

    # ---- 卡片 3：定位 ----------------------------------------------------
    def _card_position(self) -> Card:
        card = Card()
        card.add(CardHeader("定位", icon="coords", subtitle="决定选区落在画面的哪一块"))

        self.pos_seg = Segmented(
            ["居中裁剪", "拖拽定位", "精确坐标"],
            icons_map={0: "target", 1: "drag", 2: "coords"},
            compact=True,
        )
        self.pos_seg.changed.connect(self._on_position_mode)
        card.add(self.pos_seg)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.x_field = _IntField(0, width=70, maximum=65535)
        self.y_field = _IntField(0, width=70, maximum=65535)
        self.x_field.textChanged.connect(self._on_coords_edited)
        self.y_field.textChanged.connect(self._on_coords_edited)
        row.addWidget(QLabel("X"))
        row.addWidget(self.x_field)
        row.addWidget(QLabel("Y"))
        row.addWidget(self.y_field)
        card.add_layout(row)

        self.btn_drag = QPushButton("拖拽选择区域", card)
        self.btn_drag.setProperty("variant", "primary")
        self.btn_drag.setCursor(Qt.PointingHandCursor)
        self.btn_drag.clicked.connect(self._on_drag_requested)
        card.add(self.btn_drag)

        self.pos_note = hint("", card)
        card.add(self.pos_note)
        return card

    # ---- 卡片 4：采集节奏 ------------------------------------------------
    def _card_pacing(self) -> Card:
        card = Card()
        card.add(CardHeader("采集节奏", icon="timer"))

        row = QHBoxLayout()
        row.setSpacing(8)
        self.interval_field = _IntField(500, width=74, maximum=60000)
        row.addWidget(QLabel("间隔"))
        row.addWidget(self.interval_field)
        row.addWidget(QLabel("ms"))
        row.addStretch(1)
        card.add_layout(row)
        card.add(hint("500 ms = 每秒 2 张；实际速度受限于游戏帧率与写入速度。", card))

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.burst_count_field = _IntField(20, width=62, maximum=2000)
        self.burst_fps_field = _IntField(8, width=56, maximum=60)
        row2.addWidget(QLabel("连拍"))
        row2.addWidget(self.burst_count_field)
        row2.addWidget(QLabel("张 @"))
        row2.addWidget(self.burst_fps_field)
        row2.addWidget(QLabel("张/秒"))
        row2.addStretch(1)
        card.add_layout(row2)

        self.btn_burst = QPushButton("开始连拍", card)
        self.btn_burst.setCursor(Qt.PointingHandCursor)
        self.btn_burst.clicked.connect(self._on_burst)
        card.add(self.btn_burst)

        self.interval_field.editingFinished.connect(self._on_interval_changed)
        self.burst_count_field.editingFinished.connect(self._on_burst_changed)
        self.burst_fps_field.editingFinished.connect(self._on_burst_changed)
        return card

    # ---- 卡片 5：快捷键 --------------------------------------------------
    def _card_hotkeys(self) -> Card:
        card = Card()
        card.add(CardHeader("快捷键", icon="keyboard", subtitle="工具失去焦点后仍然生效"))
        card.add(hint("点击输入框后按下想要的组合键；Esc 取消，× 解除绑定。", card))

        self.hotkey_edits: dict[str, HotkeyEdit] = {}
        labels = self.app.hotkey_labels()
        for action, label in labels.items():
            row = QHBoxLayout()
            row.setSpacing(8)
            text = QLabel(label, card)
            text.setObjectName("FieldLabel")
            row.addWidget(text, 1)
            editor = HotkeyEdit("", card)
            editor.recorded.connect(lambda combo, a=action: self._on_hotkey_recorded(a, combo))
            editor.cleared.connect(lambda a=action: self._on_hotkey_cleared(a))
            editor.editing_started.connect(self.app.suspend_hotkeys)
            editor.editing_finished.connect(self.app.resume_hotkeys)
            row.addWidget(editor)
            card.add_layout(row)
            self.hotkey_edits[action] = editor
        return card

    # ---- 卡片 6：保存位置 ------------------------------------------------
    def _card_storage(self) -> Card:
        card = Card()
        header = CardHeader("保存位置", icon="folder")
        self.session_badge = QLabel("", card)
        self.session_badge.setObjectName("Caption")
        header.add_right(self.session_badge)
        card.add(header)

        self.path_label = QLabel("尚未设置", card)
        self.path_label.setObjectName("Mono")
        self.path_label.setWordWrap(True)
        card.add(self.path_label)

        tools = ToolRow()
        tools.add("选择文件夹", self._on_pick_directory, icon="folder")
        self.btn_open = tools.add("打开", self.app.open_save_directory, icon="folder_open", tooltip="在资源管理器中打开")
        tools.add_stretch()
        card.add(tools)

        tools2 = ToolRow()
        self.btn_new_session = tools2.add(
            "新建批次", self._on_new_session, icon="plus",
            tooltip="后续截图写入新的批次目录，已保存的图片不会被移动",
        )
        tools2.add_stretch()
        card.add(tools2)

        self.storage_note = hint("", card)
        card.add(self.storage_note)
        return card

    # ==================================================================
    # 信号接线
    # ==================================================================
    def _wire(self) -> None:
        app = self.app
        app.sources_changed.connect(lambda _kind: self._reload_sources())
        app.config_changed.connect(self.refresh_all)
        app.state_changed.connect(lambda *_: self._refresh_buttons_soon())
        app.hotkey_error.connect(self._on_hotkey_error)
        app.preview.connect(self._on_preview)
        app.image_saved.connect(self._on_image_saved)
        app.session_changed.connect(lambda _uid: self.refresh_all())
        app.backend_notice.connect(lambda text: self.backend_detail.setText(text))

        self.btn_recent_folder.clicked.connect(app.open_session_directory)
        self.btn_shot.clicked.connect(self._on_shot_clicked)
        self.btn_primary.clicked.connect(self._on_primary_clicked)
        self.thumbs.clicked.connect(self._on_thumb_clicked)
        self.thumbs.request_reveal.connect(lambda row: app.reveal_file(row["rel_path"]))
        self.thumbs.request_exclude.connect(self._on_thumb_exclude)

    def refresh_all(self) -> None:
        """按当前配置与状态把界面刷一遍。"""
        app = self.app
        self._loading = True
        try:
            kind = app.source_kind()
            self.kind_seg.set_index(0 if kind is SourceKind.MONITOR else 1)

            self._reload_sources()
            self._reload_hotkeys()

            region = app.pipeline.region
            self.width_field.set_value(region.width)
            self.height_field.set_value(region.height)
            self.x_field.set_value(region.x)
            self.y_field.set_value(region.y)
            self.interval_field.set_value(app.config.capture.interval_ms)
            self.burst_count_field.set_value(app.config.capture.burst_count)
            self.burst_fps_field.set_value(app.config.capture.burst_fps)
            self.ratio_box.setChecked(app.config.capture.ratio_locked)

            try:
                mode = RegionMode(app.config.capture.region_mode)
            except ValueError:
                mode = RegionMode.CENTER
            self.pos_seg.set_index(
                {RegionMode.CENTER: 0, RegionMode.DRAG: 1, RegionMode.MANUAL: 2}[mode]
            )
        finally:
            self._loading = False

        self._refresh_storage()
        self._refresh_backend()
        self._refresh_notes()
        self._refresh_recent()
        self._refresh_buttons()

    def _reload_sources(self) -> None:
        entries = self.app.sources()
        self._source_entries = entries
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        if not entries:
            self.source_combo.addItem(
                "没有找到可采集的" + ("显示器" if self.app.source_kind() is SourceKind.MONITOR else "窗口"),
                None,
            )
            self.source_combo.setEnabled(False)
        else:
            self.source_combo.setEnabled(True)
            for entry in entries:
                text = entry.label
                if entry.spec.kind is SourceKind.MONITOR:
                    text += f"   {entry.spec.source_width}×{entry.spec.source_height}"
                self.source_combo.addItem(text, entry)

            identity = self.app.config.capture.source_identity
            current = self.app.pipeline.source
            target_index = 0
            for index, entry in enumerate(entries):
                if current is not None and entry.spec.source_id == current.source_id:
                    target_index = index
                    break
                if identity and entry.spec.identity == identity:
                    target_index = index
            self.source_combo.setCurrentIndex(target_index)
        self.source_combo.blockSignals(False)

        if entries and self.app.pipeline.source is None:
            # 有可用源但还没选：不自动替用户决定，但把提示写清楚
            self.source_note.setText(
                "已找到采集源，点上面的下拉框选一个即可开始预览。"
            )
        elif not entries:
            self.source_note.setText(
                "没有找到可采集的窗口。请确认游戏已经启动，然后点「刷新列表」。"
                if self.app.source_kind() is SourceKind.WINDOW
                else "没有检测到显示器。"
            )

    def _reload_hotkeys(self) -> None:
        for action, editor in self.hotkey_edits.items():
            combo = getattr(self.app.config.hotkeys, action, "")
            editor.set_combo(combo)
            active = self.app.hotkeys.combo_for(action)
            if combo and active != combo:
                editor.set_error(f"「{combo}」当前未能注册，可能已被其他程序占用")

    def _refresh_storage(self) -> None:
        directory = self.app.config.storage.save_directory
        self.path_label.setText(directory or "尚未设置")
        session = self.app.pipeline.session
        self.session_badge.setText(session.uid if session else "尚未开始批次")
        writable = self.app.pipeline.layout() is not None
        self.btn_new_session.setEnabled(writable and session is not None)
        note = self.app.recovery_note
        self.storage_note.setText(note or "")

    def _refresh_backend(self) -> None:
        capability = self.app.backend_capability()
        key = self.app.pipeline.backend_key or self.app.config.capture.backend
        available = self.app.available_backends()
        if capability is None:
            self.btn_backend.setText("后端：未知")
            self.backend_detail.setText("")
            return
        self.btn_backend.setText(f"后端：{capability.display_name.split('（')[0]}")
        self.btn_backend.setEnabled(len(available) > 1)
        parts = [capability.summary]
        if capability.notes:
            parts.append(" · ".join(capability.notes))
        if not capability.available:
            parts.append(capability.reason)
        self.backend_detail.setText("｜".join(p for p in parts if p))

    def _refresh_notes(self) -> None:
        """把"为什么还不能开始"写进对应字段旁（方案 §7.2）。"""
        app = self.app
        source = app.pipeline.source
        problem = ""

        if source is None:
            self.size_note.setText("选择采集源后可用尺寸上限会显示在这里。")
            self.pos_note.setText("")
        else:
            self.size_note.setText(
                f"当前采集源可用范围：{source.source_width} × {source.source_height} px"
                "（物理像素，已按 DPI 换算）"
            )
            self.pos_note.setText(
                f"选区左上角相对采集源为 ({app.pipeline.region.x}, {app.pipeline.region.y})，"
                f"输出 {app.pipeline.region.width} × {app.pipeline.region.height}"
            )

        for message in app.unmet_problems():
            if "采集源" in message or "窗口" in message:
                self.source_note.setText(message)
                problem = message
            elif "尺寸" in message or "范围" in message or "超出" in message:
                self.size_note.setText(message)
                problem = problem or message
            elif "保存" in message:
                self.storage_note.setText(message)
                problem = problem or message

        if problem:
            self.source_note.setStyleSheet(f"color: {DARK.warn};")

    def _refresh_recent(self) -> None:
        db, layout = self.app.db(), self.app.layout()
        if db is None or layout is None:
            self.thumbs.clear()
            self.recent_count.setText("")
            return
        try:
            rows = db.recent_images(limit=8)
        except Exception:
            rows = []
        self.thumbs.set_rows(rows, lambda row: layout.absolute(row["rel_path"]))
        self.recent_count.setText(f"{len(rows)} 张" if rows else "")

    def _refresh_buttons(self) -> None:
        from ...pipeline import CaptureState, RunMode

        capturing = self.app.pipeline.mode is RunMode.AUTO
        self.btn_primary.setText("暂停采集" if capturing else "开始采集")
        self.main_toggle_hint = capturing

        problems = self.app.unmet_problems()
        self.btn_primary.setEnabled(not problems or capturing)
        tooltip = problems[0] if problems else (
            "暂停自动采集" if capturing else "开始自动采集"
        )
        if not self.btn_primary.isEnabled():
            tooltip = f"无法开始：{problems[0]}"
        self.btn_primary.setToolTip(tooltip)

        if self.app.pipeline.state is CaptureState.SAVE_ERROR:
            self.btn_primary.setText("保存异常，点击了解")
            self.btn_primary.setEnabled(True)
            self._set_primary_icon("warning")
        else:
            self._set_primary_icon("pause" if capturing else "play")
        self._refresh_buttons_soon()

    def _set_primary_icon(self, name: str) -> None:
        """主按钮图标跟随文案变化，避免出现「暂停采集」配播放图标。"""
        self.btn_primary.setIcon(icons.qicon(name, DARK.on_accent, 15))

    def _refresh_buttons_soon(self) -> None:
        # 状态在异步流程里变化，这里只保证按钮文案跟得上
        from ...pipeline import RunMode

        if self.app.pipeline.state.value == "save_error":
            self.btn_primary.setText("保存异常，点击了解")
            self._set_primary_icon("warning")
            return
        capturing = self.app.pipeline.mode is RunMode.AUTO
        self.btn_primary.setText("暂停采集" if capturing else "开始采集")
        self._set_primary_icon("pause" if capturing else "play")

    # ==================================================================
    # 交互
    # ==================================================================
    def _on_shot_clicked(self) -> None:
        """手动截图一次。失败原因直接转成提示，不让按钮看起来「点了没反应」。"""
        ok, message = self.app.request_manual()
        if not ok and message:
            self.app.notice.emit("warn", message)

    def _on_primary_clicked(self) -> None:
        """主按钮：开始/暂停自动采集；保存异常时改为把用户带到保存目录。"""
        from ...pipeline import CaptureState

        if self.app.pipeline.state is CaptureState.SAVE_ERROR:
            self.app.open_save_directory()
            return
        ok, message = self.app.toggle_auto()
        if not ok and message:
            self.app.notice.emit("warn", message)
    def _on_kind_changed(self, index: int) -> None:
        if self._loading:
            return
        self.app.set_source_kind(
            SourceKind.MONITOR if index == 0 else SourceKind.WINDOW
        )

    def _on_refresh_sources(self) -> None:
        self.app.refresh_sources()
        self.app.notice.emit("info", f"已刷新采集源列表（{len(self.app.sources())} 项）")

    def _on_source_selected(self, index: int) -> None:
        if self._loading or index < 0:
            return
        entry = self.source_combo.itemData(index)
        if entry is None:
            return
        self.app.select_source(entry.spec)
        self.refresh_all()

    def _on_switch_backend(self) -> None:
        available = self.app.available_backends()
        if len(available) < 2:
            self.app.notice.emit("info", "当前采集源类型只有一个可用后端。")
            return
        current = self.app.pipeline.backend_key or self.app.config.capture.backend
        nxt = available[(available.index(current) + 1) % len(available)] if current in available else available[0]
        ok, _message = self.app.switch_backend(nxt)
        if ok:
            cap = self.app.capabilities().get(nxt)
            self.app.notice.emit(
                "info", f"已切换到 {cap.display_name if cap else nxt}"
            )
        self._refresh_backend()

    def _on_size_edited(self) -> None:
        if self._loading:
            return
        ok, message = self.app.set_size(self.width_field.text(), self.height_field.text())
        if not ok:
            self.size_note.setText(message)
            self.size_note.setStyleSheet(f"color: {DARK.danger};")
            self.width_field.setProperty("state", "error")
            self.height_field.setProperty("state", "error")
        else:
            self.size_note.setStyleSheet("")
            self.width_field.setProperty("state", "")
            self.height_field.setProperty("state", "")
        for field in (self.width_field, self.height_field):
            field.style().unpolish(field)
            field.style().polish(field)
        # 比例锁定可能改了另一边的值，同步回输入框（但不触发新一轮编辑）
        region = self.app.config.capture
        if ok:
            self._loading = True
            if parse_size(self.width_field.text()) != region.width:
                self.width_field.set_value(region.width)
            if parse_size(self.height_field.text()) != region.height:
                self.height_field.set_value(region.height)
            self._loading = False
            self.pos_note.setText(
                f"选区左上角 ({region.offset_x}, {region.offset_y})，"
                f"输出 {region.width} × {region.height}"
            )

    def _on_preset(self, index: int) -> None:
        ok, message = self.app.apply_preset(index)
        if not ok:
            self.app.notice.emit("warn", message)
        self.refresh_all()

    def _on_ratio_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self.app.set_ratio_locked(checked)

    def _on_position_mode(self, index: int) -> None:
        if self._loading:
            return
        mode = [RegionMode.CENTER, RegionMode.DRAG, RegionMode.MANUAL][index]
        self.app.set_position_mode(mode)
        if mode is RegionMode.CENTER:
            self.app.apply_center()
        self.refresh_all()

    def _on_coords_edited(self) -> None:
        if self._loading:
            return
        self.app.set_coords(self.x_field.text(), self.y_field.text())
        self.pos_note.setText(
            f"选区左上角 ({self.app.pipeline.region.x}, {self.app.pipeline.region.y})，"
            f"输出 {self.app.pipeline.region.width} × {self.app.pipeline.region.height}"
        )

    def _on_drag_requested(self) -> None:
        self.drag_requested.emit()

    drag_requested = Signal()

    def _on_interval_changed(self) -> None:
        value = self.interval_field.value()
        if value is None:
            self.interval_field.set_value(self.app.config.capture.interval_ms)
            return
        self.app.set_interval(value)

    def _on_burst_changed(self) -> None:
        count = self.burst_count_field.value() or self.app.config.capture.burst_count
        fps = self.burst_fps_field.value() or self.app.config.capture.burst_fps
        self.app.set_burst(count, fps)

    def _on_burst(self) -> None:
        self._on_burst_changed()
        self.app.start_burst()

    def _on_pick_directory(self) -> None:
        self.pick_directory_requested.emit()

    pick_directory_requested = Signal()

    def _on_new_session(self) -> None:
        self.app.new_session()
        self.refresh_all()

    def _on_hotkey_recorded(self, action: str, combo: str) -> None:
        ok, message = self.app.bind_hotkey(action, combo)
        editor = self.hotkey_edits.get(action)
        if editor is not None:
            editor.finish_recording(ok)
            if not ok:
                editor.set_error(message)

    def _on_hotkey_cleared(self, action: str) -> None:
        self.app.unbind_hotkey(action)

    def _on_hotkey_error(self, action: str, combo: str, message: str) -> None:
        editor = self.hotkey_edits.get(action)
        if editor is not None:
            editor.set_error(message)

    def _on_preview_mode(self, show_source: bool) -> None:
        self.app.set_preview(True, show_source)

    def _on_preview(self, payload) -> None:
        from ..imaging import to_qimage

        image = to_qimage(payload.image)
        if payload.is_full_frame:
            chip = f"源画面 {payload.source_w}×{payload.source_h} · 输出 {payload.output_w}×{payload.output_h}"
        else:
            chip = f"输出 {payload.output_w} × {payload.output_h}"
        self.preview.canvas.set_image(image, chip=chip)
        self.preview.canvas.set_overlay_region(payload.region_in_preview)
        self.preview.set_footer(
            "拖拽定位时这里显示的是源画面与选区范围；"
            if payload.is_full_frame else
            "预览即最终保存的画面，尺寸与画质与 PNG 原图一致。"
        )

    def _on_image_saved(self, saved) -> None:
        db, layout = self.app.db(), self.app.layout()
        if db is None or layout is None:
            return
        try:
            row = db.get_image(saved.image_id)
        except Exception:
            row = None
        if row is not None:
            self.thumbs.prepend(row, layout.absolute(row["rel_path"]))
        else:
            self._refresh_recent()

    def _on_thumb_clicked(self, row) -> None:
        layout = self.app.layout()
        if layout is None:
            return
        self.app.reveal_file(str(layout.absolute(row["rel_path"])))

    def _on_thumb_exclude(self, row) -> None:
        from ...storage import ReviewStatus

        db = self.app.db()
        if db is None:
            return
        db.set_review_status([int(row["id"])], ReviewStatus.EXCLUDED)
        self.app.notice.emit("info", f"已把 {Path(row['rel_path']).name} 标记为已排除")
        self._refresh_recent()

    # 主按钮由主窗口放到顶栏（方案 §7.2），本页只负责它的状态与行为
    def show_empty_state(self, text: str, hint_text: str = "") -> None:
        self.preview.canvas.set_empty(text, hint_text)
