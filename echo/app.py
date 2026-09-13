"""应用控制器：把配置、流水线、热键、数据集操作收拢到一个门面上。

界面只跟 :class:`EchoApp` 打交道，不直接碰 config / pipeline / db。
这样做的好处很实在：

* 状态变化只有一个出口（这里的信号），界面不需要去猜"我该在什么时候刷新"；
* 跨线程的边界集中在一处——UI 只调这里的方法，方法内部决定是直接改配置、
  还是通过 pipeline 的命令队列生效；
* 出错信息的展示口径统一（状态栏、托盘气泡、日志三处一致）。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from . import APP_DISPLAY_NAME, __version__
from .capture import (
    BackendCapability,
    CaptureError,
    SourceKind,
    SourceSpec,
    backend_display_name,
    capabilities_for,
    find_by_identity,
    is_source_usable,
    list_sources,
    probe_all,
    resolve_backend,
)
from .config import AppConfig, load_config, save_config
from .hotkeys import ACTIONS, HotkeyManager
from .logging_setup import get_logger
from .pipeline import CapturePipeline, CaptureState, PreviewPayload, RunMode, SavedImage
from .region import (
    SIZE_PRESETS,
    Region,
    RegionMode,
    apply_ratio_lock,
    center_region,
    parse_size,
    validate_size,
)

log = get_logger("app")


@dataclass(slots=True)
class SourceEntry:
    """界面上的一条采集源。"""

    spec: SourceSpec
    subtitle: str = ""

    @property
    def label(self) -> str:
        return self.spec.label


class EchoApp(QObject):
    """应用门面。所有公开方法都在主线程调用。"""

    state_changed = Signal(str, str)           # state.value, 原因
    counters_changed = Signal(dict)
    preview = Signal(object)                   # PreviewPayload
    image_saved = Signal(object)               # SavedImage
    notice = Signal(str, str)                  # level, text
    session_changed = Signal(str)
    sources_changed = Signal(str)              # kind.value
    config_changed = Signal()
    preconditions_changed = Signal()
    hotkey_error = Signal(str, str, str)       # action, combo, message
    backend_notice = Signal(str)
    toggle_window_requested = Signal()         # 快捷键触发的显示/收起窗口

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.config, self._config_notes = load_config()
        self.pipeline = CapturePipeline(self.config, self)
        self.hotkeys = HotkeyManager(self)

        self._sources: dict[SourceKind, list[SourceEntry]] = {
            SourceKind.MONITOR: [],
            SourceKind.WINDOW: [],
        }
        self._capabilities: dict[str, BackendCapability] = {}
        self._recovery_note = ""
        self._last_notice: tuple[str, float] = ("", 0.0)

        self._connect()

        self._config_timer = QTimer(self)
        self._config_timer.setSingleShot(True)
        self._config_timer.setInterval(400)
        self._config_timer.timeout.connect(self._flush_config)

        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.setInterval(50)
        self._notice_timer.timeout.connect(self._drain_notices)
        self._notice_queue: list[tuple[str, str]] = []

        self._source_refresh_timer = QTimer(self)
        self._source_refresh_timer.setInterval(5000)
        self._source_refresh_timer.timeout.connect(
            lambda: self.refresh_sources(notify=False)
        )

    # ==================================================================
    # 装配
    # ==================================================================
    def _connect(self) -> None:
        self.pipeline.state_changed.connect(self.state_changed.emit)
        self.pipeline.counters_changed.connect(self.counters_changed.emit)
        self.pipeline.preview_frame.connect(self.preview.emit)
        self.pipeline.image_saved.connect(self._on_image_saved)
        self.pipeline.notice.connect(self._queue_notice)
        self.pipeline.session_changed.connect(self._on_session_changed)
        self.pipeline.source_lost.connect(self._on_source_lost)

        self.hotkeys.triggered.connect(self._on_hotkey)
        self.hotkeys.binding_failed.connect(self.hotkey_error.emit)

    def start(self) -> list[str]:
        """启动流水线、注册热键、恢复上次的采集源。返回需要展示给用户的说明。"""
        problems: list[str] = []
        problems.extend(self._config_notes)

        self._capabilities = probe_all()

        # 1. 保存目录
        directory = (self.config.storage.save_directory or "").strip()
        if directory:
            ok, message = self.pipeline.attach_storage(directory)
            if not ok:
                problems.append(f"保存位置不可用：{message}")
                self.notice.emit("warn", f"上次的保存位置不可用，请重新选择：{message}")
            else:
                note = self.pipeline.recover()
                if note:
                    self._recovery_note = note
                    problems.append(f"已恢复上次的残留：{note}")

        # 2. 热键
        self.hotkeys.install()
        hotkey_problems = self.hotkeys.load(self.config.hotkeys.__dict__.copy())
        for item in hotkey_problems:
            problems.append(f"快捷键注册失败：{item}")
            self.notice.emit("warn", f"快捷键注册失败：{item}")

        # 3. 采集源：句柄不可跨重启复用，按身份串重新发现
        #
        #    这里必须发通知（notify=True）。MainWindow 是先建界面再调 start()，
        #    界面构造时读到的是空列表，会渲染成「没有找到可采集的显示器」。
        #    早先传 notify=False，等于刷了数据却不告诉界面，用户必须手动点一次
        #    「刷新列表」才能看到源——而这个按钮藏在一个空下拉框下方，很难发现。
        self.refresh_sources()
        self._restore_source()

        # 4. 采集范围
        self._apply_region_to_pipeline(announce=False)

        self.pipeline.start_threads()
        self._source_refresh_timer.start()
        log.info("%s %s 已启动", APP_DISPLAY_NAME, __version__)
        return problems

    def shutdown(self) -> None:
        self._source_refresh_timer.stop()
        try:
            self.hotkeys.uninstall()
        except Exception:  # pragma: no cover
            log.exception("释放热键失败")
        self._flush_config()
        self.pipeline.shutdown()

    def _restore_source(self) -> None:
        """按配置里的身份串重新发现采集源（方案 §9.1）。"""
        identity = self.config.capture.source_identity
        kind = self.source_kind()
        if not identity:
            return
        matches = find_by_identity(identity, kind)
        if not matches:
            self.notice.emit(
                "info",
                "上次使用的采集源这次没有找到，请重新选择。"
                "（窗口句柄不能跨重启复用，本工具会按进程名与窗口类名重新发现）",
            )
            return
        if len(matches) > 1:
            # 同一游戏开了多个窗口：让用户自己选，不替他决定（方案 §9.1）
            self.notice.emit(
                "info",
                f"发现 {len(matches)} 个匹配的窗口，请在上面选择要采集哪一个。",
            )
            return
        ok, warning = self.pipeline.set_source(matches[0], self.config.capture.backend)
        if not ok:
            self.notice.emit("warn", f"上次的采集源这次不可用：{warning}")
            return
        if warning:
            self.backend_notice.emit(warning)
        self._sync_backend_from_pipeline()

    # ==================================================================
    # 配置
    # ==================================================================
    def save_config_soon(self) -> None:
        self._config_timer.start()

    def _flush_config(self) -> None:
        try:
            save_config(self.config)
        except OSError as exc:  # pragma: no cover
            log.warning("保存配置失败：%s", exc)

    @property
    def recovery_note(self) -> str:
        return self._recovery_note

    # ==================================================================
    # 采集源
    # ==================================================================
    def source_kind(self) -> SourceKind:
        return (
            SourceKind.WINDOW
            if self.config.capture.source_type == "window"
            else SourceKind.MONITOR
        )

    def capabilities(self) -> dict[str, BackendCapability]:
        if not self._capabilities:
            self._capabilities = probe_all()
        return self._capabilities

    def backend_capability(self) -> BackendCapability | None:
        key = self.pipeline.backend_key or self.config.capture.backend
        return self.capabilities().get(key)

    def available_backends(self) -> list[str]:
        """当前采集源类型下、真正可用的后端。"""
        kind = self.source_kind()
        return [
            key for key, cap in self.capabilities().items()
            if cap.supports(kind) and cap.available
        ]

    def refresh_sources(self, *, notify: bool = True) -> None:
        kind = self.source_kind()
        try:
            entries = [SourceEntry(spec) for spec in list_sources(kind)]
        except Exception as exc:  # pragma: no cover
            log.warning("枚举采集源失败：%s", exc)
            entries = []
        entries.sort(key=lambda e: (e.spec.hwnd == 0, e.spec.label.lower()))
        self._sources[kind] = entries
        if notify:
            self.sources_changed.emit(kind.value)

    def sources(self, kind: SourceKind | None = None) -> list[SourceEntry]:
        return list(self._sources.get(kind or self.source_kind(), []))

    def set_source_kind(self, kind: SourceKind) -> None:
        if kind is self.source_kind():
            return
        self.config.capture.source_type = kind.value
        self.config.capture.source_identity = ""
        self.config.capture.backend = "wgc" if kind is SourceKind.WINDOW else "dxgi"
        self.pipeline.clear_source()
        self.refresh_sources()
        self.save_config_soon()
        self.config_changed.emit()
        self.preconditions_changed.emit()

    def select_source(self, spec: SourceSpec) -> tuple[bool, str]:
        ok, warning = self.pipeline.set_source(spec, self.config.capture.backend)
        if not ok:
            self.notice.emit("error", f"无法使用该采集源：{warning}")
            return False, warning

        self.config.capture.source_identity = spec.identity
        self.config.capture.monitor_device = spec.monitor_device
        self._sync_backend_from_pipeline()
        # 换源后把选区按新采集源重新居中，避免残留一个越界的偏移
        region = self._current_region()
        if not region.is_inside(spec.source_width, spec.source_height):
            self._set_region_internal(
                center_region(
                    spec.source_width, spec.source_height, region.width, region.height
                )
            )
        if warning:
            self.backend_notice.emit(warning)
            self.notice.emit("warn", warning)
        self.save_config_soon()
        self.config_changed.emit()
        self.preconditions_changed.emit()
        return True, warning

    def _sync_backend_from_pipeline(self) -> None:
        if self.pipeline.backend_key:
            self.config.capture.backend = self.pipeline.backend_key

    def switch_backend(self, key: str) -> tuple[bool, str]:
        if self.pipeline.source is None:
            self.config.capture.backend = key
            self.save_config_soon()
            self.config_changed.emit()
            return True, ""
        ok, warning = self.pipeline.set_source(self.pipeline.source, key)
        if ok:
            self._sync_backend_from_pipeline()
            self.save_config_soon()
            self.config_changed.emit()
            if warning:
                self.notice.emit("warn", warning)
        return ok, warning

    def _on_source_lost(self, reason: str) -> None:
        self.notice.emit("warn", f"采集源已失效：{reason}")
        self.refresh_sources(notify=False)

    # ==================================================================
    # 采集范围
    # ==================================================================
    def _current_region(self) -> Region:
        c = self.config.capture
        return Region(c.offset_x, c.offset_y, c.width, c.height)

    def _set_region_internal(self, region: Region) -> None:
        c = self.config.capture
        c.offset_x, c.offset_y = region.x, region.y
        c.width, c.height = region.width, region.height

    def _apply_region_to_pipeline(self, *, announce: bool = True) -> tuple[bool, str]:
        region = self._current_region()
        source = self.pipeline.source
        if source is None:
            return True, ""
        ok, message = self.pipeline.set_region(region)
        if not ok and announce:
            self.notice.emit("warn", message)
        return ok, message

    def set_size(self, width_text: str, height_text: str) -> tuple[bool, str]:
        """从输入框文本设置宽高。方案 §3.3：越界时阻止应用并给出可用上限。"""
        width = parse_size(width_text)
        height = parse_size(height_text)
        source = self.pipeline.source

        validation = validate_size(
            width or 0, height or 0,
            source.source_width if source else 0,
            source.source_height if source else 0,
        )
        if not validation.ok:
            return False, validation.message

        if self.config.capture.ratio_locked:
            # 比例取自当前已生效的宽高，而不是输入框里的两个值——
            # 其中一维是用户刚敲进来的新数字，用它算比例等于没锁。
            ref_w = self.config.capture.width
            ref_h = self.config.capture.height
            changed = "width" if width != ref_w else "height"
            source_w = source.source_width if source else 0
            source_h = source.source_height if source else 0
            width, height = apply_ratio_lock(
                width, height, changed, source_w, source_h,
                ref_width=ref_w, ref_height=ref_h,
            )
            # 锁定比例算出来的结果可能又越界了，再校验一次
            recheck = validate_size(width, height, source_w, source_h)
            if not recheck.ok:
                return False, recheck.message

        region = self._current_region().resized(width, height)
        region = region.clamp(
            source.source_width if source else width,
            source.source_height if source else height,
        )
        self._set_region_internal(region)
        self._apply_region_to_pipeline(announce=False)
        self.save_config_soon()
        self.config_changed.emit()
        self.preconditions_changed.emit()
        return True, ""

    def apply_preset(self, index: int) -> tuple[bool, str]:
        if not 0 <= index < len(SIZE_PRESETS):
            return False, "无效的预设"
        _label, width, height = SIZE_PRESETS[index]
        if width is None or height is None:
            source = self.pipeline.source
            if source is None:
                return False, "请先选择采集源"
            width, height = source.source_width, source.source_height
        return self.set_size(str(width), str(height))

    def set_ratio_locked(self, locked: bool) -> None:
        self.config.capture.ratio_locked = bool(locked)
        self.save_config_soon()

    def set_position_mode(self, mode: RegionMode) -> None:
        self.config.capture.region_mode = mode.value
        self.save_config_soon()
        self.config_changed.emit()

    def set_coords(self, x_text: str, y_text: str) -> tuple[bool, str]:
        x = parse_size(x_text)
        y = parse_size(y_text)
        if x is None or y is None:
            return False, "X 和 Y 必须是非负整数"
        source = self.pipeline.source
        region = Region(x, y, self.config.capture.width, self.config.capture.height)
        if source is not None and not region.is_inside(source.source_width, source.source_height):
            region = region.clamp(source.source_width, source.source_height)
        self._set_region_internal(region)
        self._apply_region_to_pipeline(announce=False)
        self.save_config_soon()
        self.config_changed.emit()
        return True, ""

    def apply_center(self) -> tuple[bool, str]:
        ok, message = self.pipeline.set_region_center()
        if ok:
            self._set_region_internal(self.pipeline.region)
            self.config.capture.region_mode = RegionMode.CENTER.value
            self.save_config_soon()
            self.config_changed.emit()
        return ok, message

    def on_drag_picked(self, region: Region) -> None:
        self._set_region_internal(region)
        self._apply_region_to_pipeline(announce=False)
        self.config.capture.region_mode = RegionMode.DRAG.value
        self.save_config_soon()
        self.config_changed.emit()
        self.notice.emit("info", f"选区已更新：{region.width}×{region.height} @({region.x}, {region.y})")

    # ==================================================================
    # 采集控制
    # ==================================================================
    def request_manual(self) -> tuple[bool, str]:
        ok, message = self.pipeline.request_manual()
        if not ok and message:
            self.notice.emit("warn", message)
        return ok, message

    def toggle_auto(self) -> tuple[bool, str]:
        ok, message = self.pipeline.toggle_auto()
        if not ok and message:
            self.notice.emit("warn", message)
        elif message:
            self.notice.emit("info", message)
        return ok, message

    def start_burst(self) -> tuple[bool, str]:
        ok, message = self.pipeline.start_burst(
            self.config.capture.burst_count, self.config.capture.burst_fps
        )
        if message:
            self.notice.emit("ok" if ok else "warn", message)
        return ok, message

    def set_interval(self, interval_ms: int) -> None:
        self.config.capture.interval_ms = int(interval_ms)
        self.pipeline.set_interval(interval_ms)
        self.save_config_soon()

    def set_burst(self, count: int, fps: int) -> None:
        self.config.capture.burst_count = int(count)
        self.config.capture.burst_fps = int(fps)
        self.save_config_soon()

    def set_preview(self, enabled: bool, show_source: bool | None = None) -> None:
        if show_source is not None:
            self.config.capture.preview_show_source = bool(show_source)
        self.pipeline.set_preview(enabled, self.config.capture.preview_show_source)
        self.save_config_soon()

    def clear_error(self) -> None:
        self.pipeline.clear_writer_error()
        self.notice.emit("info", "已清除保存异常状态，可以重新开始采集")

    def unmet_problems(self) -> list[str]:
        """当前还不能开始采集的原因，用于禁用主按钮并在字段旁说明。"""
        return self.pipeline._preconditions()

    # ==================================================================
    # 存储
    # ==================================================================
    def set_save_directory(self, directory: str) -> tuple[bool, str]:
        ok, message = self.pipeline.attach_storage(directory)
        if not ok:
            self.notice.emit("error", message)
            return False, message
        self.config.storage.save_directory = directory
        self.save_config_soon()
        note = self.pipeline.recover()
        if note:
            self.notice.emit("info", f"已恢复残留：{note}")
        self.config_changed.emit()
        self.preconditions_changed.emit()
        return True, ""

    def open_save_directory(self) -> None:
        directory = self.config.storage.save_directory
        if not directory:
            self.notice.emit("warn", "还没有设置保存位置")
            return
        path = Path(directory)
        if not path.exists():
            self.notice.emit("error", f"目录不存在：{directory}")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            self.notice.emit("error", f"无法打开目录：{exc}")

    def open_session_directory(self) -> None:
        layout = self.pipeline.layout()
        session = self.pipeline.session
        if layout is None or session is None:
            self.open_save_directory()
            return
        target = layout.session_dir(session.uid)
        try:
            if target.exists():
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                self.open_save_directory()
        except OSError as exc:
            self.notice.emit("error", f"无法打开目录：{exc}")

    def reveal_file(self, path: str) -> None:
        """在资源管理器中选中某个文件。"""
        target = Path(path)
        if not target.exists():
            self.notice.emit("warn", f"文件已不存在：{target.name}")
            return
        try:
            subprocess.Popen(["explorer", "/select,", str(target)])
        except OSError as exc:
            self.notice.emit("error", f"无法在资源管理器中定位：{exc}")

    def new_session(self) -> tuple[bool, str]:
        ok, message = self.pipeline.start_new_session()
        self.notice.emit("info" if ok else "warn", message)
        return ok, message

    def _on_session_changed(self, session_uid: str) -> None:
        self.config.last_session_uid = session_uid
        self.session_changed.emit(session_uid)
        self.save_config_soon()

    @property
    def project_name(self) -> str:
        raw = (self.config.storage.save_directory or "").strip()
        if not raw:
            return "未命名项目"
        return Path(raw).name or raw

    # ==================================================================
    # 热键
    # ==================================================================
    def bind_hotkey(self, action: str, combo: str) -> tuple[bool, str]:
        hint = self.hotkeys.conflict_hint(combo)
        if hint:
            self.notice.emit("warn", hint)
            return False, hint
        ok, message = self.hotkeys.bind(action, combo)
        if ok:
            setattr(self.config.hotkeys, action, combo)
            self.save_config_soon()
            self.config_changed.emit()
        else:
            self.notice.emit("error", f"快捷键绑定失败：{message}")
        return ok, message

    def unbind_hotkey(self, action: str) -> None:
        self.hotkeys.unbind(action)
        setattr(self.config.hotkeys, action, "")
        self.save_config_soon()
        self.config_changed.emit()

    def suspend_hotkeys(self) -> None:
        self.hotkeys.suspend()

    def resume_hotkeys(self) -> None:
        self.hotkeys.resume()

    def hotkey_labels(self) -> dict[str, str]:
        return dict(ACTIONS)

    def _on_hotkey(self, action: str) -> None:
        if action == "capture_once":
            self.request_manual()
        elif action == "toggle_auto_capture":
            self.toggle_auto()
        elif action == "toggle_window":
            self.toggle_window_requested.emit()

    # ==================================================================
    # 数据操作
    # ==================================================================
    @property
    def state_value(self) -> str:
        """当前采集状态的字符串值，供状态栏与自检使用。"""
        return self.pipeline.state.value

    def db(self):
        return self.pipeline.db()

    def layout(self):
        return self.pipeline.layout()

    def preview_fps(self) -> int:
        return 10

    def _on_image_saved(self, saved: SavedImage) -> None:
        self.image_saved.emit(saved)
        if self.config.ui.success_notification and self.config.ui.close_to_tray:
            # 后台成功通知默认关闭（方案 §7.2），只有用户开了才发
            self.notice.emit("info", f"已保存 {Path(saved.rel_path).name}")

    # ==================================================================
    # 提示去抖
    # ==================================================================
    def _queue_notice(self, level: str, text: str) -> None:
        """把提示串行化并做去抖。

        自动采集时同一条错误可能每秒来一次；如果每次都弹气泡，
        用户会被淹没。这里按 2 秒窗口合并重复文本。
        """
        import time

        now = time.monotonic()
        last_text, last_time = self._last_notice
        if text == last_text and now - last_time < 2.0:
            return
        self._last_notice = (text, now)
        self._notice_queue.append((level, text))
        # 50ms 合并窗口：同一瞬间产生的多条提示攒到一起再推给界面，
        # 状态栏不会连续跳动，也保证同帧内的提示按产生顺序到达。
        self._notice_timer.start()

    def _drain_notices(self) -> None:
        """定时器到点，把攒下的提示按顺序交给界面。"""
        pending, self._notice_queue = self._notice_queue, []
        for level, text in pending:
            self.notice.emit(level, text)
