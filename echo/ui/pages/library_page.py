"""图片库页：审核、批次筛选、标注导入与 YOLO 导出（方案 §10）。

页面的三个区各自回答一个问题：

* **左/中**：「我采到了什么、哪些还没处理」——缩略图网格 + 筛选。
* **右上**：「这批数据的进度到哪了」——审核状态统计。
* **右下**：「怎么把数据交给训练」——标注导入与 YOLO 导出。

导出与导入都是长耗时操作（几万张图时会跑几十秒），所以全部走后台线程，
界面给进度反馈，不阻塞。方案 §11.2 的 T16 要求"训练读取通过"，
所以导出前会做完整校验，并把问题写在报告里而不是静默跳过。
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ... import app as app_mod
from ... import winapi
from ...dataset import (
    collect_items,
    import_labels,
    plan_splits,
    validate_plan,
    write_export,
)
from ...dataset.exporter import ExportPlan
from ...logging_setup import get_logger
from ...quality import FLAG_LABELS
from ...storage import ReviewStatus
from .. import icons, imaging
from ..widgets import (
    Card,
    CardHeader,
    FieldRow,
    ScrollColumn,
    Segmented,
    ToolRow,
    caption,
    hint,
    title,
)
from ..theme import DARK, METRICS

log = get_logger("ui.library")

PAGE_SIZE = 96
GRID_CELL = 148


class _GridCell(QWidget):
    """网格里的一张图。左上角是勾选框，边框随选中态变化。"""

    toggled = Signal(int, bool)     # image_id, checked
    opened = Signal(object)         # row
    menu_requested = Signal(object, object)  # row, 全局坐标 QPoint

    def __init__(self, row, abs_path: Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.row = row
        self.image_id = int(row["id"])
        self.setObjectName("ThumbCell")
        self.setFixedSize(GRID_CELL, GRID_CELL + 26)
        self.setProperty("selected", "false")

        box = QVBoxLayout(self)
        box.setContentsMargins(6, 6, 6, 4)
        box.setSpacing(4)

        self._image = QLabel(self)
        self._image.setAlignment(Qt.AlignCenter)
        self._image.setFixedHeight(GRID_CELL)
        pixmap = imaging.thumbnail(abs_path, GRID_CELL - 8)
        if pixmap.isNull():
            self._image.setText("无法预览")
            self._image.setObjectName("Hint")
        else:
            self._image.setPixmap(pixmap)
        box.addWidget(self._image)

        footer = QHBoxLayout()
        footer.setSpacing(4)
        self._check = QCheckBox(self)
        self._check.toggled.connect(lambda v: self._on_check(v))
        footer.addWidget(self._check)
        self._status = QLabel(self)
        self._status.setObjectName("Caption")
        footer.addWidget(self._status, 1)
        box.addLayout(footer)

        self.setToolTip(self._tooltip())
        self._refresh_status()

    def _tooltip(self) -> str:
        row = self.row
        lines = [
            row["rel_path"],
            f"{str(row['captured_at'])[:19].replace('T', ' ')}",
            f"批次 {row['session_uid']} · 触发 {row['trigger_kind']}",
            f"输出 {row['output_w']}×{row['output_h']}（源 {row['source_w']}×{row['source_h']}）",
            f"状态 {ReviewStatus.parse(row['review_status']).label}",
        ]
        flags = [f for f in (row["quality_flags"] or "").split(",") if f]
        if flags:
            lines.append("质量标记：" + "、".join(FLAG_LABELS.get(f, f) for f in flags))
        if row["similar_group"] is not None:
            lines.append("与既有图片相似（仅标记，未删除）")
        if row["exact_dup_of"] is not None:
            lines.append("与另一张完全重复")
        return "\n".join(lines)

    def _refresh_status(self) -> None:
        status = ReviewStatus.parse(self.row["review_status"])
        self._status.setText(status.label)
        color = {
            ReviewStatus.PENDING: DARK.text_faint,
            ReviewStatus.TO_LABEL: DARK.warn,
            ReviewStatus.LABELED: DARK.ok,
            ReviewStatus.VERIFIED_EMPTY: DARK.info,
            ReviewStatus.EXCLUDED: DARK.text_faint,
        }[status]
        self._status.setStyleSheet(f"color: {color}; font-size: 11px;")

    def is_checked(self) -> bool:
        return self._check.isChecked()

    def set_checked(self, value: bool) -> None:
        self._check.setChecked(bool(value))

    def _on_check(self, value: bool) -> None:
        self.setProperty("selected", "true" if value else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.toggled.emit(self.image_id, value)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.opened.emit(self.row)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self.menu_requested.emit(self.row, event.globalPos())


class _Worker(QObject):
    """后台执行导入 / 导出，避免几万张图时界面卡死。"""

    finished = Signal(str, bool)     # 报告文本, 是否成功
    failed = Signal(str)

    def __init__(self, kind: str, payload: dict):
        super().__init__()
        self.kind = kind
        self.payload = payload

    def run(self) -> None:
        try:
            if self.kind == "import":
                report = import_labels(
                    self.payload["source"],
                    self.payload["db"],
                    self.payload["layout"],
                    self.payload["class_names"],
                    apply=True,
                )
                text = report.summary() + "\n\n"
                if report.class_map_note:
                    text += report.class_map_note + "\n\n"
                if report.unmatched_samples:
                    text += "未能对应的示例：" + "、".join(report.unmatched_samples[:10]) + "\n\n"
                if report.problems:
                    text += "问题：\n" + "\n".join(f"· {p}" for p in report.problems[:30])
                self.finished.emit(text, report.n_matched > 0)
            elif self.kind == "export":
                result = write_export(self.payload["plan"], self.payload["db"])
                self.finished.emit(result.report, result.ok)
            else:  # pragma: no cover
                self.failed.emit(f"未知任务：{self.kind}")
        except Exception as exc:  # pragma: no cover
            log.exception("后台任务失败")
            self.failed.emit(str(exc))


class LibraryPage(QWidget):
    """图片库与数据集页。"""

    def __init__(self, app: app_mod.EchoApp, parent: QWidget | None = None):
        super().__init__(parent)
        self.app = app
        self._cells: list[_GridCell] = []
        self._selected: set[int] = set()
        self._page = 0
        self._thread: QThread | None = None

        outer = QHBoxLayout(self)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(16)
        outer.addWidget(self._build_browser(), 1)
        outer.addWidget(self._build_side())

        app.image_saved.connect(lambda _saved: self._schedule_refresh())
        app.session_changed.connect(lambda _uid: self.refresh())

        self._refresh_timer = None
        self.refresh()

    # ==================================================================
    # 左：缩略图浏览器
    # ==================================================================
    def _build_browser(self) -> QWidget:
        holder = QWidget(self)
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        header = Card()
        header.body.setContentsMargins(16, 14, 16, 14)

        title_row = QHBoxLayout()
        line = QLabel("图片库", holder)
        line.setObjectName("PageTitle")
        title_row.addWidget(line)
        self.summary = QLabel("", holder)
        self.summary.setObjectName("Caption")
        title_row.addWidget(self.summary)
        title_row.addStretch(1)
        box.addLayout(title_row)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.batch_combo = QComboBox()
        self.batch_combo.setMinimumWidth(200)
        self.batch_combo.currentIndexChanged.connect(lambda _i: self.refresh())
        filters.addWidget(self.batch_combo, 1)

        self.status_filter = Segmented(
            ["全部", "待审核", "待标注", "已标注", "无目标", "已排除"],
            compact=True,
        )
        self.status_filter.changed.connect(lambda _i: self.refresh())
        filters.addWidget(self.status_filter, 2)
        box.addLayout(filters)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("按文件名、场景标签或备注搜索")
        self.search.returnPressed.connect(self.refresh)
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self._button("刷新", self.refresh, icon="refresh"))
        search_row.addWidget(self._button("显示重复项", lambda: self._quick_filter("dup")))
        search_row.addWidget(self._button("显示相似项", lambda: self._quick_filter("similar")))
        box.addLayout(search_row)

        self.grid_holder = QWidget(holder)
        self.grid = QGridLayout(self.grid_holder)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)
        box.addWidget(self.grid_holder, 1)

        self.empty_hint = QLabel("", holder)
        self.empty_hint.setObjectName("Hint")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        box.addWidget(self.empty_hint)

        pager = QHBoxLayout()
        pager.setSpacing(8)
        self.btn_prev = self._button("上一页", lambda: self._page_step(-1), icon="chevron_right")
        self.btn_next = self._button("下一页", lambda: self._page_step(1), icon="chevron_right")
        pager.addStretch(1)
        pager.addWidget(self.btn_prev)
        self.page_label = QLabel("", holder)
        self.page_label.setObjectName("Caption")
        pager.addWidget(self.page_label)
        pager.addWidget(self.btn_next)
        pager.addStretch(1)
        box.addLayout(pager)

        actions = Card()
        actions.body.setContentsMargins(16, 12, 16, 12)
        row = QHBoxLayout()
        row.setSpacing(6)
        self.selection_label = QLabel("未选择图片", actions)
        self.selection_label.setObjectName("Caption")
        row.addWidget(self.selection_label)
        row.addStretch(1)
        for label, status in (
            ("待审核", ReviewStatus.PENDING),
            ("待标注", ReviewStatus.TO_LABEL),
            ("已标注", ReviewStatus.LABELED),
            ("确认无目标", ReviewStatus.VERIFIED_EMPTY),
            ("排除", ReviewStatus.EXCLUDED),
        ):
            row.addWidget(
                self._button(f"标记为{label}", lambda s=status: self._set_status(s))
            )
        row.addWidget(self._button("全选本页", lambda: self._select_all(True)))
        row.addWidget(self._button("取消选择", lambda: self._select_all(False)))
        row.addWidget(self._button("复制路径", self._copy_paths, icon="duplicate"))
        row.addWidget(self._button("打开位置", self._open_selected_location, icon="folder_open"))
        self.btn_delete = self._button(
            "删除选中", self._delete_selected, icon="trash", variant="danger"
        )
        self.btn_delete.setToolTip("图片文件移入系统回收站，可在回收站还原")
        row.addWidget(self.btn_delete)
        actions.add_layout(row)
        box.addWidget(actions)
        return holder

    def _button(self, text: str, callback, *, icon: str = "", variant: str = "") -> QPushButton:
        button = QPushButton(text, self)
        if icon:
            button.setIcon(icons.qicon(icon, DARK.text_dim, 15))
            button.setIconSize(QSize(15, 15))
        if variant:
            button.setProperty("variant", variant)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(lambda _=False: callback())
        return button

    # ==================================================================
    # 右：审核 + 数据集
    # ==================================================================
    def _build_side(self) -> QWidget:
        holder = QWidget(self)
        holder.setFixedWidth(METRICS.settings_width)
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        scroll = ScrollColumn(holder, spacing=12)
        box.addWidget(scroll)

        # --- 审核统计 ---
        stats = Card()
        stats.add(CardHeader("审核进度", icon="shield", subtitle="未标注不等于没有目标"))
        self.stat_rows: dict[str, QLabel] = {}
        for status in ReviewStatus:
            row = QHBoxLayout()
            row.setSpacing(8)
            label = QLabel(status.label, stats)
            label.setObjectName("FieldLabel")
            row.addWidget(label, 1)
            value = QLabel("0", stats)
            value.setStyleSheet(f"color: {DARK.text};")
            row.addWidget(value)
            stats.add_layout(row)
            self.stat_rows[status.value] = value
        scroll.add(stats)

        # --- 类别表 ---
        classes = Card()
        classes.add(CardHeader("类别表", icon="layers", subtitle="编号从 0 开始，与训练时一致"))
        self.classes_field = QLineEdit()
        self.classes_field.setPlaceholderText("例如：fish, kelp, debris")
        self.classes_field.editingFinished.connect(self._on_classes_edited)
        classes.add(self.classes_field)
        classes.add(hint("用英文逗号分隔。导出的 data.yaml 与 classes.json 会按这里的顺序冻结。", classes))
        scroll.add(classes)

        # --- 标注导入 ---
        imp = Card()
        imp.add(CardHeader("导入标注", icon="upload", subtitle="支持 CVAT 的 YOLO 1.1 与 Ultralytics 目录"))
        tools = ToolRow()
        tools.add("选择标注目录并导入", self._on_import, icon="upload")
        tools.add_stretch()
        imp.add(tools)
        imp.add(hint(
            "按文件名（不含扩展名）与原图对应。若在 CVAT 里重命名了图片，"
            "请在导出时选择保留原始文件名。",
            imp,
        ))
        scroll.add(imp)

        # --- YOLO 导出 ---
        exp = Card()
        exp.add(CardHeader("导出 YOLO 数据集", icon="download", subtitle="按批次 80/10/10 划分"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.split_train = QLineEdit("80"); self.split_train.setFixedWidth(46)
        self.split_val = QLineEdit("10"); self.split_val.setFixedWidth(46)
        self.split_test = QLineEdit("10"); self.split_test.setFixedWidth(46)
        for label, field in (("train", self.split_train), ("val", self.split_val), ("test", self.split_test)):
            row.addWidget(QLabel(label, exp))
            row.addWidget(field)
        row.addStretch(1)
        exp.add_layout(row)

        tools2 = ToolRow()
        tools2.add("选择输出目录并导出", self._on_export, icon="download")
        tools2.add("检查", self._on_check_export, icon="check")
        tools2.add_stretch()
        exp.add(tools2)

        self.export_note = hint(
            "相邻帧、同源裁剪图不跨集合；划分刀口会留保护带，"
            "宁可少几张也不造成训练/验证泄漏。",
            exp,
        )
        scroll.add(exp)

        # --- 结果 ---
        result = Card()
        result.add(CardHeader("结果", icon="info"))
        self.result_view = QListWidget()
        self.result_view.setSelectionMode(QAbstractItemView.NoSelection)
        self.result_view.setMinimumHeight(190)
        self.result_view.setWordWrap(True)
        result.add(self.result_view)
        self.result_card = result
        scroll.add(result)

        scroll.add_stretch()
        return holder

    # ==================================================================
    # 数据加载
    # ==================================================================
    def refresh(self) -> None:
        db = self.app.db()
        if db is None:
            self.empty_hint.setText("请先在「采集」页设置保存位置。")
            return

        self._reload_batches(db)
        self._reload_stats(db)
        self._reload_classes()

        session = self.batch_combo.currentData()
        statuses = self._status_values()
        try:
            rows = db.query_images(
                session_uid=session if session else None,
                statuses=statuses,
                only_duplicates=self._quick_filter_mode == "dup",
                only_similar=self._quick_filter_mode == "similar",
                search=self.search.text().strip(),
                limit=PAGE_SIZE,
                offset=self._page * PAGE_SIZE,
                order="captured_at DESC",
            )
            total = self._count_all(db, session)
        except Exception as exc:
            log.warning("查询图片失败：%s", exc)
            rows, total = [], 0

        self._render(rows)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        self.page_label.setText(f"{self._page + 1} / {pages}")
        self.btn_prev.setEnabled(self._page > 0)
        self.btn_next.setEnabled(self._page + 1 < pages)
        self.summary.setText(f"共 {total} 张")

    _quick_filter_mode = ""

    def _quick_filter(self, mode: str) -> None:
        self._quick_filter_mode = "" if self._quick_filter_mode == mode else mode
        self._page = 0
        self.refresh()
        if self._quick_filter_mode:
            label = "完全重复" if mode == "dup" else "相似图"
            self.app.notice.emit("info", f"只显示{label}（首版只标记，不会自动删除）")

    def _count_all(self, db, session) -> int:
        try:
            return db.count_images(session if session else None)
        except Exception:
            return 0

    def _status_values(self) -> list[str] | None:
        index = self.status_filter.index()
        if index == 0:
            return None
        order = [
            ReviewStatus.PENDING, ReviewStatus.TO_LABEL, ReviewStatus.LABELED,
            ReviewStatus.VERIFIED_EMPTY, ReviewStatus.EXCLUDED,
        ]
        return [order[index - 1].value]

    def _reload_batches(self, db) -> None:
        current = self.batch_combo.currentData()
        self.batch_combo.blockSignals(True)
        self.batch_combo.clear()
        self.batch_combo.addItem("全部批次", "")
        try:
            for row in db.session_summaries():
                uid = row["session_uid"]
                started = str(row["started_at"] or "")[:16].replace("T", " ")
                self.batch_combo.addItem(
                    f"{uid}   {started}   {int(row['n_images'] or 0)} 张", uid
                )
        except Exception as exc:
            log.warning("读取批次列表失败：%s", exc)
        index = self.batch_combo.findData(current or "")
        self.batch_combo.setCurrentIndex(max(0, index))
        self.batch_combo.blockSignals(False)

    def _reload_stats(self, db) -> None:
        try:
            counts = db.status_counts()
        except Exception:
            counts = {}
        for key, label in self.stat_rows.items():
            label.setText(str(counts.get(key, 0)))

    def _reload_classes(self) -> None:
        names = self.app.config.dataset.class_names
        if not names:
            db = self.app.db()
            if db is not None:
                try:
                    names = db.get_classes()
                except Exception:
                    names = []
        self.classes_field.setText(", ".join(names))

    # ==================================================================
    # 网格渲染
    # ==================================================================
    def _render(self, rows) -> None:
        for cell in self._cells:
            self.grid.removeWidget(cell)
            cell.deleteLater()
        self._cells.clear()
        self._selected.clear()
        self._update_selection_label()

        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        layout = self.app.layout()
        if not rows or layout is None:
            self.empty_hint.setText(
                "这里还没有图片。回到「采集」页选好源、设好保存位置，按 F8 就能截第一张。"
            )
            return
        self.empty_hint.setText("")

        columns = max(2, self.width() // (GRID_CELL + 14) - 2)
        columns = min(columns, 8)
        for index, row in enumerate(rows):
            cell = _GridCell(row, layout.absolute(row["rel_path"]), self.grid_holder)
            cell.toggled.connect(self._on_cell_toggled)
            cell.opened.connect(lambda r: self.app.reveal_file(str(layout.absolute(r["rel_path"]))))
            cell.menu_requested.connect(self._on_cell_menu)
            self.grid.addWidget(cell, index // columns, index % columns)
            self._cells.append(cell)

        for column in range(columns):
            self.grid.setColumnStretch(column, 1)
        self.grid.setRowStretch(self.grid.rowCount(), 1)

    def _on_cell_toggled(self, image_id: int, checked: bool) -> None:
        if checked:
            self._selected.add(image_id)
        else:
            self._selected.discard(image_id)
        self._update_selection_label()

    def _update_selection_label(self) -> None:
        count = len(self._selected)
        self.selection_label.setText(
            f"已选择 {count} 张" if count else "未选择图片"
        )

    def _select_all(self, value: bool) -> None:
        for cell in self._cells:
            cell.set_checked(value)

    def _page_step(self, delta: int) -> None:
        self._page = max(0, self._page + delta)
        self.refresh()

    def _schedule_refresh(self) -> None:
        from PySide6.QtCore import QTimer

        if self._refresh_timer is None:
            self._refresh_timer = QTimer(self)
            self._refresh_timer.setSingleShot(True)
            self._refresh_timer.timeout.connect(self.refresh)
        self._refresh_timer.start(600)

    # ==================================================================
    # 审核操作
    # ==================================================================
    def _set_status(self, status: ReviewStatus) -> None:
        db = self.app.db()
        if db is None or not self._selected:
            self.app.notice.emit("warn", "请先勾选要修改的图片")
            return
        ids = sorted(self._selected)
        try:
            db.set_review_status(ids, status)
        except Exception as exc:
            self.app.notice.emit("error", f"更新审核状态失败：{exc}")
            return
        self.app.notice.emit("info", f"已把 {len(ids)} 张图标记为「{status.label}」")
        self.refresh()

    # ==================================================================
    # 删除 / 定位 / 复制路径
    # ==================================================================
    def _selected_paths(self) -> list[tuple[int, Path]]:
        """选中图片的 (image_id, 绝对路径) 列表，记录已删的自动跳过。"""
        db, layout = self.app.db(), self.app.layout()
        if db is None or layout is None:
            return []
        pairs: list[tuple[int, Path]] = []
        for image_id in sorted(self._selected):
            try:
                row = db.get_image(image_id)
            except Exception:
                row = None
            if row is not None:
                pairs.append((image_id, layout.absolute(row["rel_path"])))
        return pairs

    def _copy_paths(self) -> None:
        pairs = self._selected_paths()
        if not pairs:
            self.app.notice.emit("warn", "请先勾选要复制的图片")
            return
        text = "\n".join(str(path) for _id, path in pairs)
        QApplication.clipboard().setText(text)
        self.app.notice.emit("info", f"已复制 {len(pairs)} 个路径")

    def _open_selected_location(self) -> None:
        pairs = self._selected_paths()
        if not pairs:
            self.app.notice.emit("warn", "请先勾选图片")
            return
        self.app.reveal_file(str(pairs[0][1]))

    def _delete_paths_to_trash(self, paths: list[Path]) -> bool:
        """把文件移入回收站。失败时提示并返回 False（数据库不动）。"""
        if not paths:
            return True
        ok, message = winapi.recycle_paths([str(p) for p in paths])
        if not ok:
            self.app.notice.emit("error", f"{message}，已取消删除")
        return ok

    def _delete_selected(self) -> None:
        db = self.app.db()
        pairs = self._selected_paths()
        if db is None or not pairs:
            self.app.notice.emit("warn", "请先勾选要删除的图片")
            return
        missing = len(self._selected) - len(pairs)
        answer = QMessageBox.question(
            self,
            "删除图片",
            f"确定删除选中的 {len(pairs)} 张图片吗？\n\n"
            "· 图片文件会移入系统回收站，误删可在回收站还原\n"
            "· 对应的数据库记录会一并删除，导出时不再包含\n"
            "· 已有的标注文件不会被删除，但失去原图后导出会跳过"
            + (f"\n\n另有 {missing} 条记录已失效，将只清理索引。" if missing else ""),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        paths = [path for _id, path in pairs]
        if not self._delete_paths_to_trash(paths):
            return
        ids = [image_id for image_id, _path in pairs]
        try:
            removed = db.delete_images(ids)
        except Exception as exc:
            self.app.notice.emit("error", f"删除数据库记录失败：{exc}")
            return
        self._selected.clear()
        self._update_selection_label()
        self.app.notice.emit(
            "info", f"已删除 {len(removed)} 张图片，文件在回收站可还原"
        )
        self.refresh()

    def _on_cell_menu(self, row, global_pos) -> None:
        """单张图的右键菜单：定位、复制路径、状态标记、删除。"""
        layout = self.app.layout()
        image_id = int(row["id"])
        menu = QMenu(self)
        if layout is not None:
            abs_path = layout.absolute(row["rel_path"])
            menu.addAction(
                icons.qicon("folder_open", DARK.text_dim, 14),
                "在文件夹中显示",
                lambda: self.app.reveal_file(str(abs_path)),
            )
        menu.addAction(
            icons.qicon("duplicate", DARK.text_dim, 14),
            "复制文件路径",
            lambda: QApplication.clipboard().setText(str(layout.absolute(row["rel_path"])))
            if layout is not None
            else None,
        )
        menu.addSeparator()

        def mark(status: ReviewStatus) -> None:
            db = self.app.db()
            if db is None:
                return
            try:
                db.set_review_status([image_id], status)
            except Exception as exc:
                self.app.notice.emit("error", f"更新审核状态失败：{exc}")
                return
            self.refresh()

        status_menu = menu.addMenu("标记为")
        for status in ReviewStatus:
            status_menu.addAction(status.label, lambda s=status: mark(s))

        menu.addSeparator()
        menu.addAction(
            icons.qicon("trash", DARK.danger, 14),
            "删除（移入回收站）",
            lambda: self._delete_one(image_id),
        )
        menu.exec(global_pos)

    def _delete_one(self, image_id: int) -> None:
        db, layout = self.app.db(), self.app.layout()
        if db is None:
            return
        try:
            row = db.get_image(image_id)
        except Exception:
            row = None
        if row is None:
            return
        name = Path(str(row["rel_path"])).name
        answer = QMessageBox.question(
            self,
            "删除图片",
            f"确定删除 {name} 吗？\n文件会移入系统回收站，可在回收站还原。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if layout is not None:
            abs_path = layout.absolute(row["rel_path"])
            if abs_path.exists() and not self._delete_paths_to_trash([abs_path]):
                return
        try:
            db.delete_images([image_id])
        except Exception as exc:
            self.app.notice.emit("error", f"删除数据库记录失败：{exc}")
            return
        self._selected.discard(image_id)
        self.app.notice.emit("info", f"已删除 {name}")
        self.refresh()

    # ==================================================================
    # 类别表
    # ==================================================================
    def _on_classes_edited(self) -> None:
        raw = self.classes_field.text().replace("，", ",")
        names = [part.strip() for part in raw.split(",") if part.strip()]
        self.app.config.dataset.class_names = names
        self.app.save_config_soon()
        db = self.app.db()
        if db is not None:
            try:
                db.set_classes(names)
            except Exception as exc:  # pragma: no cover
                log.warning("写入类别表失败：%s", exc)
        self.classes_field.setText(", ".join(names))
        self.app.notice.emit("info", f"类别表已更新（{len(names)} 类）")

    # ==================================================================
    # 标注导入
    # ==================================================================
    def _on_import(self) -> None:
        db, layout = self.app.db(), self.app.layout()
        if db is None or layout is None:
            self.app.notice.emit("warn", "请先设置保存位置")
            return

        directory = QFileDialog.getExistingDirectory(self, "选择标注目录（CVAT 或 Ultralytics 导出）")
        if not directory:
            return

        names = self.app.config.dataset.class_names
        if not names:
            answer = QMessageBox.question(
                self,
                "尚未设置类别表",
                "项目里还没有类别表。导入时会尝试从标注目录里读取类别名。\n继续吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return

        self._run_worker(
            "import",
            {
                "source": Path(directory),
                "db": db,
                "layout": layout,
                "class_names": names,
            },
            "正在导入标注…",
        )

    # ==================================================================
    # YOLO 导出
    # ==================================================================
    def _ratios(self) -> tuple[float, float, float]:
        def parse(text: str, fallback: float) -> float:
            try:
                value = float(text)
                return value if value >= 0 else fallback
            except (TypeError, ValueError):
                return fallback

        return (
            parse(self.split_train.text(), 80.0),
            parse(self.split_val.text(), 10.0),
            parse(self.split_test.text(), 10.0),
        )

    def _build_plan(self, out_dir: Path) -> tuple[ExportPlan | None, list[str]]:
        db, layout = self.app.db(), self.app.layout()
        names = self.app.config.dataset.class_names
        if db is None or layout is None:
            return None, ["请先设置保存位置"]

        session = self.batch_combo.currentData() or None
        session_filter = [session] if session else None

        items, skipped = collect_items(db, layout, names, session_uids=session_filter)
        train, val, test = self._ratios()
        splits, purged, strategy = plan_splits(
            items,
            train=train / 100.0 if train > 1 else train,
            val=val / 100.0 if val > 1 else val,
            test=test / 100.0 if test > 1 else test,
            seed=self.app.config.dataset.split_seed,
        )

        plan = ExportPlan(
            root=layout.root,
            out_dir=out_dir,
            class_names=list(names),
            splits=splits,
            purged=purged,
            skipped=skipped,
            strategy=strategy,
            split_seed=self.app.config.dataset.split_seed,
        )
        plan.problems = validate_plan(plan)
        return plan, plan.problems

    def _on_check_export(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择导出目录（可先只做检查）")
        if not directory:
            return
        plan, problems = self._build_plan(Path(directory))
        if plan is None:
            self._show_result(problems or ["无法构建导出计划"], False)
            return
        lines = [
            f"划分策略：{plan.strategy}",
            f"train {len(plan.splits.get('train', []))} / "
            f"val {len(plan.splits.get('val', []))} / test {len(plan.splits.get('test', []))}",
            f"边界保护带未导出 {len(plan.purged)} 张",
            f"跳过的图片 {len(plan.skipped)} 张",
        ]
        if problems:
            lines.append("")
            lines.append(f"发现 {len(problems)} 个问题：")
            lines.extend(f"· {p}" for p in problems[:20])
        else:
            lines.append("")
            lines.append("校验通过，可以导出了。")
        self._show_result(lines, not problems)

    def _on_export(self) -> None:
        db = self.app.db()
        if db is None:
            self.app.notice.emit("warn", "请先设置保存位置")
            return
        directory = QFileDialog.getExistingDirectory(self, "选择导出目录")
        if not directory:
            return
        plan, problems = self._build_plan(Path(directory))
        if plan is None:
            self._show_result(problems or ["无法构建导出计划"], False)
            return
        if problems:
            text = "导出前校验未通过：\n\n" + "\n".join(f"· {p}" for p in problems[:12])
            answer = QMessageBox.question(
                self, "校验未通过", text + "\n\n仍然继续导出吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self._show_result(problems, False)
                return
        self._run_worker("export", {"plan": plan, "db": db}, "正在导出数据集…")

    # ==================================================================
    # 后台任务
    # ==================================================================
    def _run_worker(self, kind: str, payload: dict, busy_text: str) -> None:
        if self._thread is not None and self._thread.isRunning():
            self.app.notice.emit("warn", "上一个任务还在进行中，请稍候")
            return
        self._show_result([busy_text], True)

        thread = QThread(self)
        worker = _Worker(kind, payload)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(lambda text, ok: self._on_worker_done(text, ok, thread))
        worker.failed.connect(lambda text: self._on_worker_done(f"任务失败：{text}", False, thread))
        self._thread = thread
        thread.start()

    def _on_worker_done(self, text: str, ok: bool, thread: QThread) -> None:
        self._show_result(text.splitlines(), ok)
        self.app.notice.emit("ok" if ok else "warn", "任务完成" if ok else "任务有问题，请看结果区")
        thread.quit()
        thread.wait(3000)
        self._thread = None
        self.refresh()

    def _show_result(self, lines: list[str], ok: bool) -> None:
        self.result_view.clear()
        for line in lines:
            item = QListWidgetItem(line)
            item.setForeground(Qt.NoBrush)
            self.result_view.addItem(item)
        color = DARK.ok if ok else DARK.warn
        self.result_card.setStyleSheet(f"#Card {{ border-color: {color}; }}")
