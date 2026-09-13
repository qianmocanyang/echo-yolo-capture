"""采集流水线：调度、有界队列、异步写盘、状态机。

对应方案 §8.3 / §8.4 的全部要求。线框：

```
 主线程(UI)                采集线程                     写盘线程
 ─────────                ────────                     ────────
 控制命令 ──队列──▶ 在安全边界生效
 请求手动截图 ─────▶ 取新鲜帧
                     裁剪 + 质量指纹
                     去重判定(命中即丢弃)
                     PNG 编码 ──有界队列──▶ 临时文件 → 重命名 → DB 提交
                                           相似图标记
                          ◀──信号(跨线程排队)──── 保存结果
```

关键决策与方案条款的对应：

| 条款 | 落点 |
| --- | --- |
| §8.3 UI 主线程只处理交互 | 采集与写盘都在独立线程，UI 只收信号 |
| §8.3 预览默认最多每秒 10 次，后台暂停 | :data:`PREVIEW_FPS` + ``set_preview`` |
| §8.3 自动采集默认每秒 2 张 | 默认 ``interval_ms=500`` |
| §8.3 手动任务优先，队列满时明确拒绝 | :meth:`SaveTaskQueue.put_manual` |
| §8.3 自动任务积压时丢帧并计数 | :attr:`Counters.dropped_auto` |
| §8.3 异步保存前复制图像数据 | 编码在采集线程完成，队列里只有不可变的 bytes |
| §8.3 停止采集后完成已接收的写入 | ``shutdown()`` 先排空队列再退出写盘线程 |
| §8.3 关键配置变化在安全边界生效 | :meth:`CapturePipeline._drain_commands` |
| §4.2 不把陈旧帧当成功结果 | ``timeout`` 超时即失败并提示 |
| §3.3 分辨率变化后暂停并重新确认 | :meth:`_CaptureWorker._check_source_change` |
| §8.4 保留自动采集意图，源恢复后继续 | :attr:`_waiting_reason` + 重连逻辑 |

队列按"字节预算 + 条数上限"双限，而不是只限条数：
选区是 320×320 时 240 张也才 15 MB，但选区是全屏 2560×1440 时 8 张就 40 MB。
只限条数会让内存随选区尺寸失控。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import numpy as np
from PySide6.QtCore import QObject, Signal

from .capture import (
    CaptureError,
    SourceKind,
    SourceSpec,
    create_backend,
    is_source_usable,
    resolve_backend,
)
from .config import AppConfig
from .logging_setup import get_logger
from .quality import QualityReport, find_similar, measure
from .region import Region, center_region, validate_size
from .storage import (
    Database,
    ImageRecord,
    ManifestWriter,
    ProjectLayout,
    ReviewStatus,
    SaveError,
    SessionId,
    TriggerKind,
    atomic_write_bytes,
    check_directory_writable,
    check_space,
    encode_png,
    image_filename,
    next_session_id,
    parse_filename,
)
from .storage import recover_orphans

log = get_logger("pipeline")

PREVIEW_FPS = 10                 # 方案 §8.3
IDLE_BLOCK_SECONDS = 0.25
MANUAL_FRAME_TIMEOUT = 0.7       # 手动截图等"新鲜帧"的上限
SOURCE_POLL_SECONDS = 1.0        # 检测窗口移动 / 分辨率变化的间隔
RECONNECT_SECONDS = 2.0          # 采集源掉线后的重连尝试间隔

QUEUE_MAX_BYTES = 192 * 1024 * 1024
QUEUE_MAX_ITEMS = 240

SIMILAR_LOOKBACK = 200
PREVIEW_MAX_SIDE = 640


class CaptureState(str, Enum):
    """方案 §8.4 的状态表。"""

    UNCONFIGURED = "unconfigured"
    IDLE = "idle"
    CAPTURING = "capturing"
    PAUSED = "paused"
    WAITING = "waiting"
    SAVE_ERROR = "save_error"

    @property
    def label(self) -> str:
        return {
            CaptureState.UNCONFIGURED: "未配置",
            CaptureState.IDLE: "待命",
            CaptureState.CAPTURING: "采集中",
            CaptureState.PAUSED: "已暂停",
            CaptureState.WAITING: "等待画面",
            CaptureState.SAVE_ERROR: "保存异常",
        }[self]

    @property
    def is_problem(self) -> bool:
        return self in {CaptureState.SAVE_ERROR, CaptureState.WAITING}


class RunMode(str, Enum):
    """用户的采集意图。

    刻意与"当前状态"分开：方案 §8.4 要求「等待画面时可以保留用户的自动采集意图，
    源恢复且区域未变化后继续；用户主动暂停后不得自动恢复」。
    只有把意图单独存下来，才可能区分这两种"没在采集"。
    """

    IDLE = "idle"
    AUTO = "auto"
    PAUSED = "paused"


@dataclass(slots=True)
class Counters:
    saved: int = 0          # **真实落盘成功**的数量，不含入队（方案 §5.2）
    skipped_dup: int = 0
    dropped_auto: int = 0
    failed: int = 0
    manual_failed: int = 0
    similar: int = 0
    pending_bytes: int = 0
    pending_items: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "saved": self.saved,
            "skipped_dup": self.skipped_dup,
            "dropped_auto": self.dropped_auto,
            "failed": self.failed,
            "manual_failed": self.manual_failed,
            "similar": self.similar,
            "pending_bytes": self.pending_bytes,
            "pending_items": self.pending_items,
        }


@dataclass(slots=True)
class SaveTask:
    """一个待落盘的任务。队列里只放编码后的 bytes，避免大数组在队列里堆积。"""

    payload: bytes
    rel_path: str
    session_uid: str
    captured_at: datetime
    trigger: str
    backend: str
    source_type: str
    source_label: str
    source_w: int
    source_h: int
    region: tuple[int, int, int, int]
    output_w: int
    output_h: int
    report: QualityReport
    bypass_dedup: bool
    is_burst: bool = False
    sequence: int = 0
    priority: int = 1          # 0 = 手动，1 = 自动/连拍


@dataclass(slots=True)
class SavedImage:
    """成功落盘的一张图，交给界面刷新缩略图与计数。"""

    image_id: int
    rel_path: str
    abs_path: str
    session_uid: str
    captured_at: datetime
    trigger: str
    width: int
    height: int
    size_bytes: int
    flags: list[str] = field(default_factory=list)
    similar_group: int | None = None
    backend: str = ""
    source_label: str = ""
    sequence: int = 0


@dataclass(slots=True)
class PreviewPayload:
    """预览帧。已经缩小过，避免每帧在信号里搬几 MB。"""

    image: np.ndarray
    scale: float
    is_full_frame: bool
    region_in_preview: tuple[int, int, int, int] | None
    output_w: int
    output_h: int
    source_w: int
    source_h: int


# --------------------------------------------------------------------------
# 有界优先级队列
# --------------------------------------------------------------------------

class SaveTaskQueue:
    """手动优先的有界队列。

    不复用 :mod:`queue.PriorityQueue` 的原因：它的"满"是硬拒绝，
    而这里需要的行为是"队列满时先挤掉一个自动任务给手动任务腾位置"，
    并且要按字节预算而不是按条数计账。
    """

    def __init__(self, max_bytes: int = QUEUE_MAX_BYTES, max_items: int = QUEUE_MAX_ITEMS):
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)
        self._manual: deque[SaveTask] = deque()
        self._auto: deque[SaveTask] = deque()
        self._bytes = 0
        self._items = 0
        self._max_bytes = max_bytes
        self._max_items = max_items
        self._dropped_auto = 0

    # ---- 入队 -----------------------------------------------------------
    def put_manual(self, task: SaveTask) -> tuple[bool, str]:
        """手动任务入队。队列满时挤掉最旧的自动任务；挤不掉就明确拒绝。"""
        task.priority = 0
        size = len(task.payload)
        with self._ready:
            if self._over_limit(size):
                self._evict_auto_for(size)
            if self._over_limit(size):
                return False, "写入队列已满且没有可丢弃的自动任务，本次手动截图未被接受"
            self._manual.append(task)
            self._bytes += size
            self._items += 1
            self._ready.notify()
        return True, ""

    def put_auto(self, task: SaveTask) -> bool:
        """自动任务入队。满则丢弃并计数，不阻塞采集线程（方案 §8.3）。"""
        task.priority = 1
        size = len(task.payload)
        with self._ready:
            if self._over_limit(size):
                self._dropped_auto += 1
                return False
            self._auto.append(task)
            self._bytes += size
            self._items += 1
            self._ready.notify()
        return True

    def _over_limit(self, incoming: int) -> bool:
        return self._items >= self._max_items or self._bytes + incoming > self._max_bytes

    def _evict_auto_for(self, incoming: int) -> int:
        """丢弃自动任务腾空间，返回丢弃数量。"""
        dropped = 0
        while self._auto and self._over_limit(incoming):
            victim = self._auto.popleft()
            self._bytes -= len(victim.payload)
            self._items -= 1
            self._dropped_auto += 1
            dropped += 1
        return dropped

    # ---- 出队 -----------------------------------------------------------
    def get(self, timeout: float = 0.2) -> SaveTask | None:
        deadline = time.monotonic() + timeout
        with self._ready:
            while not self._manual and not self._auto:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._ready.wait(remaining)
            task = self._manual.popleft() if self._manual else self._auto.popleft()
            self._bytes -= len(task.payload)
            self._items -= 1
            return task

    def clear_auto(self) -> int:
        """丢弃全部自动任务，返回丢弃数量。保存异常后用它停止继续堆积。"""
        with self._ready:
            count = len(self._auto)
            for task in self._auto:
                self._bytes -= len(task.payload)
                self._items -= 1
            self._auto.clear()
            return count

    def clear_all(self) -> int:
        with self._ready:
            count = self._items
            self._manual.clear()
            self._auto.clear()
            self._bytes = 0
            self._items = 0
            return count

    def notify_all(self) -> None:
        with self._ready:
            self._ready.notify_all()

    # ---- 只读属性 -------------------------------------------------------
    @property
    def size(self) -> int:
        with self._lock:
            return self._items

    @property
    def bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def manual_size(self) -> int:
        with self._lock:
            return len(self._manual)

    @property
    def dropped_auto(self) -> int:
        with self._lock:
            return self._dropped_auto


# --------------------------------------------------------------------------
# 控制命令（主线程 → 采集线程）
# --------------------------------------------------------------------------

@dataclass(slots=True)
class _OpenSource:
    source: SourceSpec
    backend_key: str
    region: Region


@dataclass(slots=True)
class _CloseSource:
    pass


@dataclass(slots=True)
class _SetRegion:
    region: Region


@dataclass(slots=True)
class _SetInterval:
    interval_ms: int


@dataclass(slots=True)
class _SetPreview:
    enabled: bool
    show_source: bool


@dataclass(slots=True)
class _StartBurst:
    count: int
    fps: int


# --------------------------------------------------------------------------
# 流水线
# --------------------------------------------------------------------------

class CapturePipeline(QObject):
    """采集流水线。

    公开方法只在主线程调用；采集线程与写盘线程通过命令队列和信号往返。
    """

    state_changed = Signal(str, str)          # state.value, 原因
    counters_changed = Signal(dict)
    preview_frame = Signal(object)            # PreviewPayload
    image_saved = Signal(object)              # SavedImage
    notice = Signal(str, str)                 # level(info/warn/error), 文本
    session_changed = Signal(str)             # session_uid
    source_lost = Signal(str)                 # 原因

    def __init__(self, config: AppConfig, parent: QObject | None = None):
        super().__init__(parent)
        self._config = config
        self._layout: ProjectLayout | None = None
        self._db: Database | None = None
        self._manifest: ManifestWriter | None = None

        # ---- 意图状态（主线程写，采集线程只读） ----
        self._source: SourceSpec | None = None
        self._backend_key: str = ""
        self._region: Region = Region(0, 0, 320, 320)
        self._interval_ms: int = 500
        self._burst_fps: int = 8
        self._mode = RunMode.IDLE
        self._preview_enabled = False
        self._preview_show_source = False
        self._session: SessionId | None = None
        self._writer_error = ""

        # ---- 跨线程共享 ----
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._state_lock = threading.RLock()
        self._commands: deque[Any] = deque()
        self._shutdown = False
        self._manual_pending = 0
        self._counters = Counters()
        self._queue = SaveTaskQueue()
        self._hashes: dict[str, int] = {}
        self._pending_hashes: set[str] = set()
        self._recent: deque[tuple[int, str]] = deque(maxlen=SIMILAR_LOOKBACK)
        self._index_counter = 0

        self._state = CaptureState.UNCONFIGURED
        self._state_reason = "尚未选择采集源"
        self._waiting_reason = ""

        self._capture_thread: threading.Thread | None = None
        self._writer_thread: threading.Thread | None = None

    # ==================================================================
    # 只读属性
    # ==================================================================
    @property
    def state(self) -> CaptureState:
        with self._state_lock:
            return self._state

    @property
    def state_reason(self) -> str:
        with self._state_lock:
            return self._state_reason

    @property
    def session(self) -> SessionId | None:
        return self._session

    @property
    def counters(self) -> Counters:
        return self._counters

    @property
    def source(self) -> SourceSpec | None:
        return self._source

    @property
    def region(self) -> Region:
        return self._region

    @property
    def backend_key(self) -> str:
        return self._backend_key

    @property
    def mode(self) -> RunMode:
        return self._mode

    @property
    def is_capturing(self) -> bool:
        return self._mode is RunMode.AUTO

    @property
    def writer_error(self) -> str:
        return self._writer_error

    def db(self) -> Database | None:
        return self._db

    def layout(self) -> ProjectLayout | None:
        return self._layout

    # ==================================================================
    # 线程生命周期
    # ==================================================================
    def start_threads(self) -> None:
        if self._capture_thread is not None:
            return
        self._shutdown = False
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="echo-writer", daemon=True
        )
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="echo-capture", daemon=True
        )
        self._writer_thread.start()
        self._capture_thread.start()
        log.info("采集与写盘线程已启动")

    def shutdown(self, timeout: float = 8.0) -> None:
        """停止接受新任务，完成已接收的保存任务并释放资源（方案 §6.1）。"""
        with self._cond:
            self._shutdown = True
            self._manual_pending = 0
            self._cond.notify_all()
        self._queue.notify_all()

        for thread in (self._capture_thread, self._writer_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)
                if thread.is_alive():  # pragma: no cover
                    log.warning("线程 %s 未在 %.1fs 内退出", thread.name, timeout)
        self._capture_thread = None
        self._writer_thread = None

        if self._db is not None:
            if self._session is not None:
                try:
                    self._db.close_session(self._session.uid)
                except Exception as exc:  # pragma: no cover
                    log.warning("关闭批次记录失败：%s", exc)
            try:
                self._db.close()
            except Exception as exc:  # pragma: no cover
                log.warning("关闭数据库失败：%s", exc)
            self._db = None
        log.info("流水线已关闭")

    # ==================================================================
    # 存储
    # ==================================================================
    def attach_storage(self, directory: str) -> tuple[bool, str]:
        """绑定保存目录。会执行可写性检查（方案 §5.1）。"""
        from pathlib import Path

        raw = (directory or "").strip()
        if not raw:
            return False, "请先选择保存位置"
        try:
            root = Path(raw)
        except (TypeError, ValueError) as exc:
            return False, f"路径无效：{exc}"

        ok, message = check_directory_writable(root)
        if not ok:
            return False, message

        layout = ProjectLayout(root).ensure()
        if self._db is not None:
            try:
                self._db.close()
            except Exception:  # pragma: no cover
                pass
        try:
            db = Database(layout.db_path)
        except Exception as exc:
            return False, f"无法打开项目数据库：{exc}"

        self._layout = layout
        self._db = db
        self._manifest = ManifestWriter(layout.manifest_path)
        self._reset_session_state()
        self._writer_error = ""
        self._refresh_state()
        return True, ""

    def recover(self) -> str:
        """崩溃残留与未登记原图的恢复（方案 §5.2）。"""
        if self._layout is None or self._db is None:
            return ""
        try:
            report = recover_orphans(self._layout.images_dir, self._db, delete_temp=False)
        except Exception as exc:  # pragma: no cover
            log.warning("恢复扫描失败：%s", exc)
            return ""
        if report.empty:
            return ""
        log.info("恢复扫描：%s", report.summary())
        return report.summary()

    # ==================================================================
    # 采集源
    # ==================================================================
    def set_source(self, source: SourceSpec, backend_key: str) -> tuple[bool, str]:
        """选择采集源。返回 (是否成功, 需要展示给用户的提示)。"""
        resolution = resolve_backend(backend_key, source.kind)
        if not resolution.ok:
            return False, resolution.reason

        usable, reason = is_source_usable(source)
        if not usable:
            return False, reason

        ok, message = self._validate_region_for(source)
        if not ok:
            return False, message

        self._source = source
        self._backend_key = resolution.key
        self._waiting_reason = ""
        # 采集源或后端变了，批次要重开：不同源的图不该混在一个批次里
        if self._session is not None and self._db is not None:
            self._db.close_session(self._session.uid)
            self._reset_session_state()
        self._push_command(
            _OpenSource(source=source, backend_key=resolution.key, region=self._region)
        )
        self._refresh_state()
        return True, resolution.warning

    def clear_source(self) -> None:
        self._source = None
        self._push_command(_CloseSource())
        self._refresh_state()

    def set_region(self, region: Region) -> tuple[bool, str]:
        ok, message = self._validate_region_for(self._source, region)
        if not ok:
            return False, message
        self._region = region
        self._push_command(_SetRegion(region))
        return True, ""

    def set_region_center(self) -> tuple[bool, str]:
        if self._source is None:
            return False, "请先选择采集源"
        return self.set_region(
            center_region(
                self._source.source_width, self._source.source_height,
                self._region.width, self._region.height,
            )
        )

    def set_size(self, width: int, height: int) -> tuple[bool, str]:
        validation = validate_size(
            width, height,
            self._source.source_width if self._source else 0,
            self._source.source_height if self._source else 0,
        )
        if not validation.ok:
            return False, validation.message
        return self.set_region(Region(self._region.x, self._region.y, width, height))

    def _validate_region_for(
        self, source: SourceSpec | None, region: Region | None = None
    ) -> tuple[bool, str]:
        target = region or self._region
        if source is None:
            return False, "请先选择采集源"
        validation = validate_size(
            target.width, target.height, source.source_width, source.source_height
        )
        if not validation.ok:
            return False, validation.message
        return True, ""

    # ==================================================================
    # 采集控制
    # ==================================================================
    def set_interval(self, interval_ms: int) -> None:
        self._interval_ms = max(33, min(int(interval_ms), 60_000))
        self._push_command(_SetInterval(self._interval_ms))

    def set_preview(self, enabled: bool, show_source: bool = False) -> None:
        self._preview_enabled = bool(enabled)
        self._preview_show_source = bool(show_source)
        self._push_command(_SetPreview(self._preview_enabled, self._preview_show_source))

    def start_auto(self) -> tuple[bool, str]:
        problems = self._preconditions()
        if problems:
            return False, problems[0]
        if self._writer_error:
            return False, "上次保存出现异常，请检查保存位置后重新开始"
        if self._ensure_session() is None:
            return False, "无法创建采集批次"

        with self._lock:
            self._manual_pending = 0
        self._mode = RunMode.AUTO
        self._waiting_reason = ""
        self._push_command(_SetInterval(self._interval_ms))
        self._refresh_state()
        return True, ""

    def pause_auto(self) -> None:
        """用户主动暂停。此后不得自动恢复（方案 §8.4）。"""
        self._mode = RunMode.PAUSED
        self._waiting_reason = ""
        with self._lock:
            self._manual_pending = 0
        self._refresh_state()

    def toggle_auto(self) -> tuple[bool, str]:
        if self._mode is RunMode.AUTO:
            self.pause_auto()
            return True, "已暂停采集"
        return self.start_auto()

    def request_manual(self) -> tuple[bool, str]:
        """请求单张截图（热键或按钮）。"""
        problems = self._preconditions()
        if problems:
            return False, problems[0]
        if self._writer_error:
            return False, "上次保存出现异常，请检查保存位置后重新开始"
        if self._ensure_session() is None:
            return False, "无法创建采集批次"
        with self._cond:
            self._manual_pending += 1
            self._cond.notify_all()
        return True, ""

    def start_burst(self, count: int, fps: int) -> tuple[bool, str]:
        """短时连拍（方案 §2 P1）。"""
        problems = self._preconditions()
        if problems:
            return False, problems[0]
        if self._ensure_session() is None:
            return False, "无法创建采集批次"
        fps = max(1, min(int(fps), 60))
        count = max(1, min(int(count), 2000))
        self._burst_fps = fps
        self._push_command(_StartBurst(count=count, fps=fps))
        return True, f"开始连拍 {count} 张（约 {fps} 张/秒）"

    def clear_writer_error(self) -> None:
        self._writer_error = ""
        self._refresh_state()

    def _preconditions(self) -> list[str]:
        problems: list[str] = []
        if self._layout is None:
            problems.append("请先设置保存位置")
        else:
            enough, reason = check_space(self._layout.root)
            if not enough:
                problems.append(reason)
        if self._source is None:
            problems.append("请先选择游戏窗口或显示器")
        else:
            usable, reason = is_source_usable(self._source)
            if not usable:
                problems.append(reason)
            ok, message = self._validate_region_for(self._source)
            if not ok:
                problems.append(message)
        if not self._backend_key:
            problems.append("尚未确定采集后端")
        return problems

    # ==================================================================
    # 批次
    # ==================================================================
    def _reset_session_state(self) -> None:
        self._session = None
        with self._lock:
            self._hashes.clear()
            self._pending_hashes.clear()
            self._recent.clear()
            self._index_counter = 0

    def _ensure_session(self) -> SessionId | None:
        """确保存在一个有效批次。主线程与采集线程都可能调用。"""
        if self._session is not None:
            return self._session
        if self._db is None or self._layout is None or self._source is None:
            return None

        now = datetime.now()
        try:
            session = next_session_id(self._db.all_session_uids(), now)
            self._layout.session_dir(session.uid).mkdir(parents=True, exist_ok=True)
            self._db.upsert_session(
                session.uid,
                started_at=now,
                source_type=self._source.kind.value,
                source_label=self._source.label,
                source_id=self._source.source_id,
                backend=self._backend_key,
                region=(self._region.x, self._region.y, self._region.width, self._region.height),
                output_size=(self._region.width, self._region.height),
            )
        except Exception as exc:
            log.exception("创建采集批次失败")
            self.notice.emit("error", f"创建采集批次失败：{exc}")
            return None

        self._session = session
        self._index_counter = self._scan_next_index(session.uid)
        self._load_session_hashes(session.uid)
        log.info("新批次：%s（起始序号 %d）", session.uid, self._index_counter)
        self.session_changed.emit(session.uid)
        return session

    def start_new_session(self) -> tuple[bool, str]:
        """在当前任务安全结束后切换批次（方案 §5.1）。不移动已保存的图片。"""
        if self._session is not None and self._db is not None:
            self._db.close_session(self._session.uid)
        self._reset_session_state()
        session = self._ensure_session()
        if session is None:
            return False, "无法创建新批次"
        return True, f"已开始新批次 {session.uid}"

    def _scan_next_index(self, session_uid: str) -> int:
        """目录里已有同名批次的图时，序号要接着往下数，绝不能覆盖。"""
        if self._layout is None:
            return 0
        directory = self._layout.session_dir(session_uid)
        highest = 0
        try:
            for entry in directory.iterdir():
                if not entry.is_file():
                    continue
                parsed = parse_filename(entry.name)
                if parsed and parsed["session_uid"] == session_uid:
                    highest = max(highest, parsed["index"])
        except OSError:
            return 0
        return highest

    def _load_session_hashes(self, session_uid: str) -> None:
        """把本批次已有的像素哈希读进内存，让去重判定不必每次都查库。"""
        if self._db is None:
            return
        with self._lock:
            self._hashes.clear()
            self._pending_hashes.clear()
            self._recent.clear()
        try:
            rows = self._db.images_by_session(session_uid)
        except Exception as exc:  # pragma: no cover
            log.warning("读取批次哈希失败：%s", exc)
            return
        with self._lock:
            for row in rows:
                if row["image_hash"]:
                    self._hashes[row["image_hash"]] = int(row["id"])
                if row["dhash"]:
                    self._recent.append((int(row["id"]), row["dhash"]))

    # ==================================================================
    # 命令通道
    # ==================================================================
    def _push_command(self, command: Any) -> None:
        with self._cond:
            self._commands.append(command)
            self._cond.notify_all()

    def _drain_commands(self, worker: "_CaptureWorker") -> None:
        while True:
            with self._cond:
                if not self._commands:
                    return
                command = self._commands.popleft()
            try:
                worker.apply(command)
            except CaptureError as exc:
                log.warning("应用控制命令失败：%s", exc)
                self.notice.emit("warn", str(exc))
            except Exception as exc:  # pragma: no cover
                log.exception("应用控制命令时发生未预期错误")
                self.notice.emit("error", f"配置未能生效：{exc}")

    # ==================================================================
    # 状态
    # ==================================================================
    def _refresh_state(self, reason: str = "") -> None:
        """重新计算并广播状态。规则见方案 §8.4。

        优先级：保存异常 > 未配置 > 已暂停 > 等待画面 > 采集中 > 待命。
        「已暂停」排在「等待画面」之前，是因为用户主动暂停时，
        显示"用户主动暂停"比显示"等待画面"更接近事实。
        """
        with self._state_lock:
            waiting = self._waiting_reason
        if self._writer_error:
            new_state, new_reason = CaptureState.SAVE_ERROR, self._writer_error
        elif self._source is None or self._layout is None:
            missing = []
            if self._source is None:
                missing.append("采集源")
            if self._layout is None:
                missing.append("保存位置")
            new_state = CaptureState.UNCONFIGURED
            new_reason = reason or ("请设置" + "、".join(missing))
        elif self._mode is RunMode.PAUSED:
            new_state, new_reason = CaptureState.PAUSED, "用户主动暂停"
        elif waiting:
            new_state, new_reason = CaptureState.WAITING, waiting
        elif self._mode is RunMode.AUTO:
            new_state, new_reason = CaptureState.CAPTURING, "自动采样与保存运行中"
        else:
            new_state, new_reason = CaptureState.IDLE, "可响应单次截图"

        with self._state_lock:
            if new_state is self._state and new_reason == self._state_reason:
                return
            self._state = new_state
            self._state_reason = new_reason
        log.debug("状态变更：%s（%s）", new_state.label, new_reason)
        self.state_changed.emit(new_state.value, new_reason)

    def _set_waiting(self, reason: str) -> None:
        """由采集线程调用：进入 / 退出「等待画面」。"""
        with self._state_lock:
            if self._waiting_reason == reason:
                return
            self._waiting_reason = reason
        self._refresh_state()

    def _emit_counters(self) -> None:
        self._counters.pending_bytes = self._queue.bytes
        self._counters.pending_items = self._queue.size
        self._counters.dropped_auto = self._queue.dropped_auto
        self.counters_changed.emit(self._counters.as_dict())

    # ==================================================================
    # 采集线程
    # ==================================================================
    def _capture_loop(self) -> None:
        worker = _CaptureWorker(self)
        while not self._shutdown:
            try:
                self._drain_commands(worker)
                if self._shutdown:
                    break
                worker.tick()
            except Exception as exc:  # 采集线程绝不能死，否则快捷键就哑了
                log.exception("采集循环出错")
                self.notice.emit("error", f"采集循环异常：{exc}")
                worker.reset(str(exc))
                time.sleep(0.3)
        try:
            worker.teardown()
        except Exception:  # pragma: no cover
            log.exception("释放采集后端时出错")
        log.info("采集线程已退出")

    # ==================================================================
    # 写盘线程
    # ==================================================================
    def _writer_loop(self) -> None:
        while True:
            if self._shutdown and self._queue.size == 0:
                break
            try:
                task = self._queue.get(timeout=0.2)
            except Exception as exc:  # pragma: no cover
                log.exception("写盘线程取任务失败")
                self._set_writer_error(f"写盘线程异常：{exc}")
                break
            if task is None:
                continue
            try:
                self._persist(task)
            except Exception as exc:  # pragma: no cover
                log.exception("写盘线程处理任务失败")
                self._set_writer_error(f"写盘线程异常：{exc}")
                break
        log.info("写盘线程已退出")

    def _set_writer_error(self, message: str) -> None:
        self._writer_error = message
        self.notice.emit("error", message)
        self._refresh_state()

    def _persist(self, task: SaveTask) -> None:
        if self._layout is None or self._db is None:
            return

        # --- 1. 完全重复过滤（方案 §10.1，只在自动采集时生效） ---
        if not task.bypass_dedup and self._config.quality.exact_dedup_auto_capture:
            with self._lock:
                existing = self._hashes.get(task.report.image_hash)
            if existing is not None:
                self._finish_task(task, skipped=True)
                return

        # --- 2. 磁盘空间再确认，避免写到一半没空间 ---
        enough, reason = check_space(self._layout.root)
        if not enough:
            self._fail_task(task, reason)
            return

        absolute = self._layout.absolute(task.rel_path)

        # --- 3. 原子写盘：临时文件 → 同目录重命名（方案 §5.2） ---
        try:
            atomic_write_bytes(absolute, task.payload)
        except SaveError as exc:
            self._fail_task(task, str(exc), fatal=exc.fatal)
            return
        except OSError as exc:
            self._fail_task(task, f"写入失败：{exc}", fatal=True)
            return

        # --- 4. 提交数据库。顺序不能颠倒（方案 §5.2） ---
        record = ImageRecord(
            rel_path=task.rel_path,
            session_uid=task.session_uid,
            captured_at=task.captured_at,
            trigger_kind=task.trigger,
            backend=task.backend,
            source_type=task.source_type,
            source_label=task.source_label,
            source_w=task.source_w,
            source_h=task.source_h,
            region_x=task.region[0],
            region_y=task.region[1],
            region_w=task.region[2],
            region_h=task.region[3],
            output_w=task.output_w,
            output_h=task.output_h,
            image_hash=task.report.image_hash,
            dhash=task.report.dhash,
            ahash=task.report.ahash,
            phash=task.report.phash,
            quality_flags=task.report.flags_csv,
            mean_luma=task.report.mean_luma,
            sharpness=task.report.sharpness,
            review_status=ReviewStatus.PENDING.value,
            save_result="saved",
            is_burst=task.is_burst,
        )
        try:
            image_id = self._db.insert_image(record)
        except Exception as exc:
            # 文件已落盘、数据库没登记：不是数据丢失，recover_orphans 能补回来
            log.error("图片已保存但数据库登记失败：%s", exc)
            self.notice.emit("warn", f"图片已保存但索引登记失败（可自动恢复）：{exc}")
            self._finish_task(task, skipped=False)
            return

        # --- 5. 相似图标记（只标记，不删除，方案 §10.1） ---
        similar_group: int | None = None
        if self._config.quality.similarity_action == "mark" and task.report.dhash:
            with self._lock:
                candidates = list(self._recent)
            match_id, _distance = find_similar(
                task.report, candidates, self._config.quality.similarity_threshold
            )
            if match_id is not None:
                try:
                    similar_group = self._db.next_similar_group()
                    self._db.mark_similar(image_id, similar_group, match_id)
                except Exception as exc:  # pragma: no cover
                    log.warning("标记相似图失败：%s", exc)
                    similar_group = None

        # --- 6. 可移植清单 ---
        if self._manifest is not None:
            self._manifest.append_record(
                record, image_id=image_id, similar_group=similar_group,
                size_bytes=len(task.payload),
            )

        # --- 7. 更新内存索引与计数 ---
        with self._lock:
            self._pending_hashes.discard(task.report.image_hash)
            self._hashes[task.report.image_hash] = image_id
            if task.report.dhash:
                self._recent.append((image_id, task.report.dhash))
            self._counters.saved += 1
            if similar_group is not None:
                self._counters.similar += 1

        self.image_saved.emit(
            SavedImage(
                image_id=image_id,
                rel_path=task.rel_path,
                abs_path=str(absolute),
                session_uid=task.session_uid,
                captured_at=task.captured_at,
                trigger=task.trigger,
                width=task.output_w,
                height=task.output_h,
                size_bytes=len(task.payload),
                flags=list(task.report.flags),
                similar_group=similar_group,
                backend=task.backend,
                source_label=task.source_label,
                sequence=task.sequence,
            )
        )
        self._emit_counters()

    def _finish_task(self, task: SaveTask, *, skipped: bool) -> None:
        with self._lock:
            self._pending_hashes.discard(task.report.image_hash)
            if skipped:
                self._counters.skipped_dup += 1
        self._emit_counters()

    def _fail_task(self, task: SaveTask, message: str, *, fatal: bool = True) -> None:
        with self._lock:
            self._pending_hashes.discard(task.report.image_hash)
            self._counters.failed += 1
            if task.priority == 0:
                self._counters.manual_failed += 1
        dropped = self._queue.clear_auto()
        if fatal:
            self._writer_error = message
        detail = message + (f"（已丢弃 {dropped} 个待写入的自动任务）" if dropped else "")
        log.error("保存失败：%s", detail)
        self.notice.emit("error", detail)
        self._refresh_state()
        self._emit_counters()


# --------------------------------------------------------------------------
# 采集工作单元（只在采集线程内使用）
# --------------------------------------------------------------------------

class _CaptureWorker:
    """采集线程内部状态机。

    单独成类，是为了让"后端生命周期"与"命令生效时机"集中在一处：
    后端对象只被采集线程触碰，主线程通过命令队列间接影响它（方案 §8.3）。
    """

    def __init__(self, pipeline: CapturePipeline):
        self.p = pipeline
        self.backend = None
        self.backend_key = ""
        self.source: SourceSpec | None = None
        self.region = Region(0, 0, 320, 320)
        self.interval_ms = 500
        self.preview_enabled = False
        self.preview_show_source = False
        self.preview_dirty = False
        self.burst_fps = 8
        self._next_auto_at = 0.0
        self._next_preview_at = 0.0
        self._next_source_check = 0.0
        self._next_reconnect = 0.0
        self._manual_deadline = 0.0
        self._burst_until = 0.0

    # ---- 命令 -----------------------------------------------------------
    def apply(self, command: Any) -> None:
        if isinstance(command, _OpenSource):
            self._open_source(command)
        elif isinstance(command, _CloseSource):
            self._close_source()
        elif isinstance(command, _SetRegion):
            self.region = command.region
            self._next_auto_at = 0.0
        elif isinstance(command, _SetInterval):
            self.interval_ms = command.interval_ms
        elif isinstance(command, _SetPreview):
            self.preview_enabled = command.enabled
            self.preview_show_source = command.show_source
            self.preview_dirty = True
        elif isinstance(command, _StartBurst):
            self.burst_fps = command.fps
            self._burst_until = time.monotonic() + command.count / max(1, command.fps)
            self._next_auto_at = 0.0

    def _open_source(self, command: _OpenSource) -> None:
        self._close_source()
        self.source = command.source
        self.backend_key = command.backend_key
        self.region = command.region
        try:
            backend = create_backend(command.backend_key)
            backend.open(command.source)
        except CaptureError as exc:
            log.warning("启动采集后端失败：%s", exc)
            self.p.notice.emit("error", f"启动采集失败：{exc}")
            self.p._set_waiting(f"采集源不可用：{exc}")
            return
        except Exception as exc:  # pragma: no cover
            log.exception("启动采集后端失败")
            self.p.notice.emit("error", f"启动采集失败：{exc}")
            return

        self.backend = backend
        self._next_auto_at = 0.0
        self._next_preview_at = 0.0
        self._next_source_check = time.monotonic() + SOURCE_POLL_SECONDS
        self.p._set_waiting("")
        self.p.notice.emit("info", f"已连接：{backend.describe()}")

    def _close_source(self) -> None:
        backend, self.backend = self.backend, None
        if backend is not None:
            try:
                backend.close()
            except Exception as exc:  # pragma: no cover
                log.warning("关闭采集后端失败：%s", exc)

    def teardown(self) -> None:
        self._close_source()

    def reset(self, reason: str = "") -> None:
        self._close_source()
        if reason:
            self.p._set_waiting(f"采集已重置：{reason}")

    # ---- 主循环 ---------------------------------------------------------
    def tick(self) -> None:
        now = time.monotonic()

        if self.source is None:
            self._wait_for_command()
            return

        if self.backend is None:
            self._try_reconnect(now)
            if self.backend is None:
                self._wait_for_command(RECONNECT_SECONDS)
            return

        manual_pending = self._manual_pending()
        rate = self._desired_fps(manual_pending)
        if rate <= 0:
            self._wait_for_command()
            return

        timeout = max(0.005, min(0.25, 1.0 / rate))
        try:
            frame = self.backend.grab(
                self.region,
                timeout=timeout,
                full_frame=self.preview_enabled and self.preview_show_source,
            )
        except CaptureError as exc:
            self._handle_capture_error(exc)
            return

        if frame is None:
            self._on_no_new_frame(manual_pending)
            return

        if now >= self._next_source_check:
            self._next_source_check = now + SOURCE_POLL_SECONDS
            if not self._check_source_change():
                return

        self.p._set_waiting("")
        self._manual_deadline = 0.0

        if self.preview_enabled and (now >= self._next_preview_at or self.preview_dirty):
            self._next_preview_at = now + 1.0 / PREVIEW_FPS
            self.preview_dirty = False
            self._emit_preview(frame)

        if manual_pending > 0 and self._take_manual():
            self._enqueue(frame, TriggerKind.MANUAL, bypass_dedup=True)
            return

        if self._burst_active(now):
            self._enqueue(frame, TriggerKind.BURST, bypass_dedup=False, is_burst=True)
            self._burst_until -= 1.0 / max(1, self.burst_fps)
            return

        if self.p._mode is RunMode.AUTO and now >= self._next_auto_at:
            self._next_auto_at = now + max(0.033, self.interval_ms / 1000.0)
            self._enqueue(frame, TriggerKind.TIMER, bypass_dedup=False)

    # ---- 重连 -----------------------------------------------------------
    def _try_reconnect(self, now: float) -> None:
        """采集源掉线后按间隔重试，保留用户的自动采集意图（方案 §8.4）。

        用户主动暂停时**不**重连——"暂停"就是别动。
        """
        if self.p._mode is RunMode.PAUSED:
            return
        if now < self._next_reconnect:
            return
        self._next_reconnect = now + RECONNECT_SECONDS
        if self.source is None:
            return

        usable, reason = is_source_usable(self.source)
        if not usable:
            self.p._set_waiting(f"等待画面：{reason}")
            return

        try:
            backend = create_backend(self.backend_key or self.p._backend_key)
            backend.open(self.source)
        except CaptureError as exc:
            self.p._set_waiting(f"等待画面：{exc}")
            return
        except Exception as exc:  # pragma: no cover
            log.debug("重连失败：%s", exc)
            return

        self.backend = backend
        self._next_auto_at = 0.0
        self._next_source_check = now + SOURCE_POLL_SECONDS
        self.p._set_waiting("")
        self.p.notice.emit("info", f"采集源已恢复：{backend.describe()}")

    # ---- 速率与等待 -----------------------------------------------------
    def _desired_fps(self, manual_pending: int) -> float:
        if manual_pending > 0:
            return 60.0
        if self._burst_active(time.monotonic()):
            return float(self.burst_fps)
        if self.p._mode is RunMode.AUTO:
            return max(1.0, 1000.0 / max(33, self.interval_ms))
        if self.preview_enabled:
            return float(PREVIEW_FPS)
        return 0.0

    def _manual_pending(self) -> int:
        with self.p._lock:
            return self.p._manual_pending

    def _take_manual(self) -> bool:
        with self.p._lock:
            if self.p._manual_pending <= 0:
                return False
            self.p._manual_pending -= 1
            return True

    def _burst_active(self, now: float) -> bool:
        return now < self._burst_until

    def _wait_for_command(self, timeout: float = IDLE_BLOCK_SECONDS) -> None:
        """无采集需求时阻塞等待，不空转烧 CPU。"""
        with self.p._cond:
            if self.p._shutdown:
                return
            if (
                self.p._commands
                or self.p._manual_pending > 0
                or self.p._mode is RunMode.AUTO
                or self.preview_enabled
            ):
                return
            self.p._cond.wait(timeout)

    # ---- 事件 -----------------------------------------------------------
    def _on_no_new_frame(self, manual_pending: int) -> None:
        """没有新帧。

        方案 §4.2：不把陈旧帧当作成功结果。所以手动截图会明确失败并提示，
        自动采集则安静地等下一轮，并把状态切到"等待画面"。
        """
        now = time.monotonic()
        if manual_pending > 0:
            if self._manual_deadline <= 0:
                self._manual_deadline = now + MANUAL_FRAME_TIMEOUT
                self.p._set_waiting("正在等待新画面…")
            elif now >= self._manual_deadline:
                self._manual_deadline = 0.0
                with self.p._lock:
                    self.p._manual_pending = 0
                    self.p._counters.manual_failed += 1
                self.p.notice.emit(
                    "warn",
                    "没有取到新画面，本次截图未保存。"
                    "常见原因：游戏已最小化或停止渲染，或该后端此时不产出新帧。",
                )
                self.p._emit_counters()
                self.p._set_waiting("等待新画面")
        else:
            self._manual_deadline = 0.0
            self.p._set_waiting("等待画面更新")

    def _handle_capture_error(self, exc: CaptureError) -> None:
        log.warning("采集出错：%s", exc)
        self.p.notice.emit("error", str(exc))
        self._close_source()
        self._next_reconnect = time.monotonic() + RECONNECT_SECONDS
        if exc.recoverable:
            self.p._set_waiting(f"等待画面：{exc}")
        else:
            self.p._set_waiting(f"采集源不可用：{exc}")
            self.p.source_lost.emit(str(exc))

    def _check_source_change(self) -> bool:
        """检测采集源几何变化。返回 False 表示本轮不再继续采集。"""
        if self.backend is None or self.source is None:
            return False
        try:
            refreshed = self.backend.probe_source()
        except Exception as exc:  # pragma: no cover
            log.debug("探测采集源失败：%s", exc)
            return True

        if refreshed is None:
            self.p.notice.emit("warn", "采集源已不可用（窗口关闭或显示器断开），已停止采集")
            self.p.source_lost.emit("采集源已不可用")
            self._close_source()
            self.p._set_waiting("采集源已不可用")
            return False

        old = self.source
        size_changed = (
            refreshed.source_width != old.source_width
            or refreshed.source_height != old.source_height
        )
        self.source = refreshed

        if size_changed:
            # 方案 §3.3：分辨率改变后暂停采集并重新确认范围，确认后开启新批次
            self.p.notice.emit(
                "warn",
                f"采集源尺寸已从 {old.source_width}×{old.source_height} 变为 "
                f"{refreshed.source_width}×{refreshed.source_height}。"
                "已暂停采集，请重新确认选区范围后再开始；确认后将从新批次继续。",
            )
            if self.p._mode is RunMode.AUTO:
                self.p._mode = RunMode.PAUSED
            clamped = self.region.clamp(refreshed.source_width, refreshed.source_height)
            self.region = clamped
            self.p._region = clamped
            self._rotate_session()
            self.p._set_waiting("")
            self.p._refresh_state()
            return False
        return True

    def _rotate_session(self) -> None:
        """尺寸变化后开新批次：不同尺寸的图混在一个批次里会污染划分。"""
        if self.p._db is not None and self.p._session is not None:
            try:
                self.p._db.close_session(self.p._session.uid)
            except Exception as exc:  # pragma: no cover
                log.warning("关闭批次失败：%s", exc)
        self.p._reset_session_state()

    # ---- 预览 -----------------------------------------------------------
    def _emit_preview(self, frame) -> None:
        image = frame.image
        if image is None or image.size == 0:
            return
        source = self.source
        target = self.region
        region_in_preview: tuple[int, int, int, int] | None = None

        if frame.is_full_frame and source is not None:
            dx, dy = source.client_offset
            left, top = target.left + dx, target.top + dy
            preview, scale = _downscale(image, PREVIEW_MAX_SIDE)
            region_in_preview = (
                int(left * scale), int(top * scale),
                int((left + target.width) * scale), int((top + target.height) * scale),
            )
        else:
            preview, scale = _downscale(image, PREVIEW_MAX_SIDE)

        self.p.preview_frame.emit(
            PreviewPayload(
                image=preview,
                scale=scale,
                is_full_frame=bool(frame.is_full_frame),
                region_in_preview=region_in_preview,
                output_w=target.width,
                output_h=target.height,
                source_w=source.source_width if source else target.width,
                source_h=source.source_height if source else target.height,
            )
        )

    # ---- 入队 -----------------------------------------------------------
    def _enqueue(
        self,
        frame,
        trigger: TriggerKind,
        *,
        bypass_dedup: bool,
        is_burst: bool = False,
    ) -> None:
        pipeline = self.p
        layout = pipeline._layout
        source = self.source
        if layout is None or source is None:
            return

        session = pipeline._session or pipeline._ensure_session()
        if session is None:
            return

        image = frame.image

        # 尺寸校验放在最前面：宁可不存，也不存一张尺寸不对的训练图
        if image.shape[1] != self.region.width or image.shape[0] != self.region.height:
            log.error(
                "丢弃尺寸不符的帧：请求 %d×%d，实际 %d×%d",
                self.region.width, self.region.height, image.shape[1], image.shape[0],
            )
            pipeline.notice.emit(
                "warn",
                f"取到的画面尺寸 {image.shape[1]}×{image.shape[0]} 与设定 "
                f"{self.region.width}×{self.region.height} 不一致，已丢弃该帧。",
            )
            return

        report = measure(image)

        # 完全重复：自动采集时直接丢弃，省掉一次编码与磁盘写入。
        # 手动截图默认绕过去重（方案 §10.1）——按下快捷键就是明确要这张。
        if (
            not bypass_dedup
            and pipeline._config.quality.exact_dedup_auto_capture
            and report.image_hash
        ):
            with pipeline._lock:
                duplicated = (
                    report.image_hash in pipeline._hashes
                    or report.image_hash in pipeline._pending_hashes
                )
                if duplicated:
                    pipeline._counters.skipped_dup += 1
            if duplicated:
                pipeline._emit_counters()
                return

        try:
            payload = encode_png(image)
        except SaveError as exc:
            log.warning("编码失败：%s", exc)
            pipeline.notice.emit("warn", f"该帧编码失败，已跳过：{exc}")
            return

        with pipeline._lock:
            pipeline._index_counter += 1
            index = pipeline._index_counter
            pipeline._pending_hashes.add(report.image_hash)

        captured_at = datetime.fromtimestamp(frame.captured_at)
        filename = image_filename(session, captured_at, index)
        rel_path = layout.session_rel(session.uid, filename)

        task = SaveTask(
            payload=payload,
            rel_path=rel_path,
            session_uid=session.uid,
            captured_at=captured_at,
            trigger=trigger.value,
            backend=self.backend_key or frame.backend,
            source_type=source.kind.value,
            source_label=source.label,
            source_w=source.source_width,
            source_h=source.source_height,
            region=(self.region.x, self.region.y, self.region.width, self.region.height),
            output_w=int(image.shape[1]),
            output_h=int(image.shape[0]),
            report=report,
            bypass_dedup=bypass_dedup,
            is_burst=is_burst,
            sequence=frame.sequence,
        )

        if bypass_dedup:
            ok, message = pipeline._queue.put_manual(task)
            if not ok:
                with pipeline._lock:
                    pipeline._pending_hashes.discard(report.image_hash)
                    pipeline._counters.manual_failed += 1
                pipeline.notice.emit("warn", message)
        else:
            # 自动任务丢弃是有意的、有计数的（方案 §8.3），不再逐次弹提示
            if not pipeline._queue.put_auto(task):
                with pipeline._lock:
                    pipeline._pending_hashes.discard(report.image_hash)
        pipeline._emit_counters()


def _downscale(image: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    """等比缩小到最长边不超过 max_side。返回 (图, 缩放比)。"""
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest <= max_side:
        return image, 1.0
    scale = max_side / float(longest)
    import cv2

    resized = cv2.resize(
        image,
        (max(1, int(width * scale)), max(1, int(height * scale))),
        interpolation=cv2.INTER_AREA,
    )
    return resized, scale
