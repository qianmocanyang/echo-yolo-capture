"""设置页：应用行为、兼容性说明、运行信息。

这一页刻意把「兼容性矩阵」放在显眼位置。方案 §6.1 用一张表说明了两类后端在
各种场景下的行为差异（游戏失焦、被遮挡、最小化、锁屏……）。
这些边界如果只写在文档里，用户遇到时只会觉得"这工具坏了"。
把它直接摆在设置页，用户能自己判断"我这种情况本来就采不到"。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
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

# 释放采集源后、真正开始诊断前的缓冲。clear_source() 只是投递命令，
# 采集线程最坏要等一个采集间隔才会消化它并 close() 后端；缓冲给足余量，
# 避免诊断的第二路后端撞上还没关干净的第一路。
DIAG_RELEASE_GRACE_MS = 1200


class _DiagnoseWorker(QObject):
    """在后台线程跑 collect_report，主线程不被十来秒的抓帧试验卡住。"""

    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, runner, keyword: str | None) -> None:
        super().__init__()
        self._runner = runner
        self._keyword = keyword

    def run(self) -> None:  # noqa: N802  由 QThread.started 触发
        try:
            self.finished.emit(self._runner(self._keyword))
        except Exception as exc:
            log.exception("采集诊断失败")
            self.failed.emit(str(exc))


class SettingsPage(QWidget):
    def __init__(self, app: app_mod.EchoApp, parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self._loading = True
        self._diag_thread: QThread | None = None
        self._diag_worker: QObject | None = None
        self._diag_resume: tuple | None = None
        self._diag_report = ""

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
        scroll.add(self._card_diagnostics())
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

    # ---- 采集诊断 --------------------------------------------------------
    #
    # 「抓不到游戏画面」的排查现场在出问题的那台机器上，而打包版用户没有命令行，
    # 所以诊断入口必须长在界面里。与 tools/diagnose_capture.py 共用
    # echo.diagnose.collect_report()，避免两处逻辑漂移。
    #
    # 关键约束：诊断要**独占**采集后端。DXGI 的 Desktop Duplication 同一输出
    # 只有一份，而 dxcam 对同 device/output 返回的是同一个相机实例——如果不先
    # 释放主采集，诊断的第二路后端 close() 会把主采集正在用的相机一起放掉。
    # 所以这里先暂停并释放当前采集源，跑完再自动恢复。
    def _card_diagnostics(self) -> Card:
        card = Card()
        card.add(CardHeader(
            "采集诊断", icon="target",
            subtitle="在本机把采集链路完整问一遍：环境、后端、实际抓帧、是不是黑屏",
        ))
        card.add(hint(
            "诊断只读（不写图片、不改配置），约 10～20 秒。"
            "期间会临时释放当前采集源（后端需要独占），结束后自动恢复连接与采集状态。",
            card,
        ))
        card.add(hint(
            "报告会自动存一份到日志目录，遇到「我这台抓不到」可以直接把文件发过去。",
            card,
        ))

        self.diag_keyword = QLineEdit(card)
        self.diag_keyword.setPlaceholderText("可选：窗口名关键词（如 rust），留空测显示器与前几个窗口")
        card.add(self.diag_keyword)

        tools = ToolRow()
        self.diag_button = tools.add(
            "开始诊断", self._on_diagnose,
            icon="play", variant="primary",
            tooltip="释放当前采集源后跑一遍完整诊断，结束后自动恢复",
        )
        self.diag_copy = tools.add("复制报告", self._on_copy_report, icon="duplicate", enabled=False)
        self.diag_save = tools.add("另存为…", self._on_save_report, icon="download", enabled=False)
        tools.add("打开日志目录", lambda: self._open(log_dir()), icon="folder_open")
        tools.add_stretch()
        card.add(tools)

        self.diag_view = QPlainTextEdit(card)
        self.diag_view.setObjectName("Report")
        self.diag_view.setReadOnly(True)
        self.diag_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.diag_view.setPlainText(self.DIAG_PLACEHOLDER)
        card.add(self.diag_view)
        return card

    DIAG_PLACEHOLDER = "还没有运行诊断。点上面的「开始诊断」，结果会显示在这里。"

    # ==================================================================
    # 诊断流程：暂停/释放 → 后台跑报告 → 恢复
    # ==================================================================
    def _on_diagnose(self) -> None:
        if self._diag_thread is not None:
            self.app.notice.emit("warn", "诊断正在进行中，请等它跑完")
            return

        # 记住现场。恢复时用 select_source() 走正常入口，配置与状态保持一致。
        self._diag_resume = None
        pipeline = self.app.pipeline
        if pipeline.source is not None:
            self._diag_resume = (pipeline.source, pipeline.is_capturing)
            if pipeline.is_capturing:
                # 这里的暂停是诊断的实现细节，不是用户「主动暂停」——
                # 结束后恢复属于用户预期之内，与 §8.4 的「暂停后不自动恢复」不冲突。
                pipeline.pause_auto()
            pipeline.clear_source()

        self.diag_button.setEnabled(False)
        self.diag_view.setPlainText(
            "正在释放采集后端（诊断需要独占）…\n结束后会自动恢复原来的采集源。"
        )

        # 给采集线程留出真正 close() 的时间：命令队列最坏要等一个采集间隔才被消化。
        QTimer.singleShot(DIAG_RELEASE_GRACE_MS, self._launch_diag_worker)

    def _launch_diag_worker(self) -> None:
        from ...diagnose import collect_report  # 延迟导入：不影响首屏速度

        keyword = self.diag_keyword.text().strip() or None
        worker = _DiagnoseWorker(collect_report, keyword)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(lambda text: self._on_diag_done(text, ""))
        worker.failed.connect(lambda text: self._on_diag_done("", text))
        self._diag_worker = worker
        self._diag_thread = thread
        thread.start()
        self.diag_view.setPlainText("正在诊断…（会依次打开每个后端并实际抓几帧）")

    def _on_diag_done(self, report: str, error: str) -> None:
        thread, self._diag_thread = self._diag_thread, None
        if thread is not None:
            thread.quit()
            thread.wait(3000)
        self._diag_worker = None

        self._diag_report = report if not error else ""
        if error:
            self.diag_view.setPlainText(f"诊断失败：{error}")
            self.app.notice.emit("error", f"采集诊断失败：{error}")
        else:
            saved = self._write_report(report)
            self.diag_view.setPlainText(
                report + f"\n\n{'-' * 68}\n报告已自动保存：{saved}"
            )
            self.diag_copy.setEnabled(True)
            self.diag_save.setEnabled(True)
            self.app.notice.emit("ok", "采集诊断完成，报告已保存到日志目录")

        self._diag_restore()
        self.diag_button.setEnabled(True)

    def _diag_restore(self) -> None:
        resume, self._diag_resume = self._diag_resume, None
        if resume is None:
            return
        spec, was_capturing = resume
        ok, warning = self.app.select_source(spec)
        if not ok:
            self.app.notice.emit(
                "warn", f"诊断后未能恢复原来的采集源，请重新选择：{warning}"
            )
            return
        if warning:
            self.app.notice.emit("warn", warning)
        if was_capturing:
            ok, message = self.app.pipeline.start_auto()
            if ok:
                self.app.notice.emit("info", "诊断完成，已恢复采集")
            else:
                self.app.notice.emit("warn", f"诊断完成，但自动采集未能恢复：{message}")

    def _write_report(self, report: str) -> Path:
        """报告永远落一份到日志目录，打包版用户不用另找地方。"""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = log_dir() / f"diagnose-{stamp}.txt"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(report + "\n", encoding="utf-8")
        except OSError as exc:
            log.warning("保存诊断报告失败：%s", exc)
            self.app.notice.emit("warn", f"诊断报告保存失败：{exc}")
        return path

    def _on_copy_report(self) -> None:
        if not self._diag_report:
            return
        QApplication.clipboard().setText(self._diag_report)
        self.app.notice.emit("info", "诊断报告已复制到剪贴板")

    def _on_save_report(self) -> None:
        if not self._diag_report:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target, _ = QFileDialog.getSaveFileName(
            self, "保存诊断报告", str(Path.home() / f"echo-诊断报告-{stamp}.txt"),
            "文本文件 (*.txt)",
        )
        if not target:
            return
        try:
            Path(target).write_text(self._diag_report + "\n", encoding="utf-8")
            self.app.notice.emit("ok", f"已保存：{target}")
        except OSError as exc:
            self.app.notice.emit("error", f"保存失败：{exc}")

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
