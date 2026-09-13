"""窗口 / 显示器采集后端（Windows Graphics Capture）。

方案 §6.2 特别强调：「WGC 的窗口选择、帧池及尺寸变化处理由专用适配层实现；
**不能仅把库的后端名称切成 `winrt` 就视为已完成按窗口捕获**」。
所以这里没有"换个参数"了事，而是老老实实做了四件事：

1. **目标绑定**：按 HWND 绑定窗口（标题会变，句柄在单次运行内稳定）。
2. **客户区对齐**：WGC 交付的是整个窗口（含标题栏与边框），
   而选区基准是客户区，所以要在 ``client_offset`` 上做修正，并在打开时**校验**
   帧尺寸与窗口/客户区尺寸的关系；对不上就明确报错，不做"大概差不多"。
3. **零拷贝安全**：回调里的 ``frame_buffer`` 是底层映射内存的视图，回调返回即失效。
   因此裁剪与 ``copy`` 全部在回调内完成，绝不把视图交给别的线程。
4. **新帧语义**：用"已交付序号"实现 ``grab()`` 的 pull 模型——
   调用方每要一帧，就等回调交付一个**新的**帧，超时即返回 None。

性能上做了一个关键取舍：回调不做无条件的整帧拷贝。
只有流水线明确"要一帧"时（``_want`` 为真）才裁剪并拷贝，
空闲时回调只累加计数器。2560×1440 的 BGRA 整帧拷贝约 14 MB，
按显示刷新率无条件拷贝会白白吃掉可观的带宽。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

from .. import winapi
from ..logging_setup import get_logger
from ..region import Region
from .base import BackendCapability, CaptureBackend, CaptureError, SourceKind, SourceSpec, make_frame
from .frame import Frame

log = get_logger("capture.wgc")

BACKEND_KEY = "wgc"
BACKEND_NAME = "Windows Graphics Capture"

# 打开后等待首帧的上限。首帧是校验帧尺寸的唯一依据，等不到就不能认为打开成功。
FIRST_FRAME_TIMEOUT = 3.0


def capability() -> BackendCapability:
    notes = [
        "游戏最小化后不能保证继续出图",
        "独占全屏 / 部分反作弊保护的游戏可能无法捕获",
        "窗口捕获在游戏失焦时通常仍可用，需按实际游戏验证",
    ]
    try:
        from windows_capture import WindowsCapture  # noqa: F401
    except Exception as exc:
        return BackendCapability(
            key=BACKEND_KEY, display_name=BACKEND_NAME,
            kinds=frozenset({SourceKind.WINDOW, SourceKind.MONITOR}),
            available=False, reason=f"windows-capture 未安装或无法导入：{exc}",
            notes=tuple(notes),
        )
    return BackendCapability(
        key=BACKEND_KEY, display_name=BACKEND_NAME,
        kinds=frozenset({SourceKind.WINDOW, SourceKind.MONITOR}),
        available=True, notes=tuple(notes),
    )


@dataclass(slots=True)
class _Slot:
    """回调与 grab() 之间传递的一帧。"""

    image: np.ndarray
    width: int
    height: int
    timespan: int
    captured_at: float
    monotonic: float


class WgcBackend(CaptureBackend):
    """WGC 后端，同时支持按窗口与按显示器捕获。"""

    key = BACKEND_KEY
    display_name = BACKEND_NAME

    def __init__(self, *, minimum_update_interval_ms: int = 16) -> None:
        super().__init__()
        self.kind = SourceKind.WINDOW
        self._capture = None
        self._control = None

        self._cond = threading.Condition()
        self._slot: _Slot | None = None
        self._delivered = 0            # 回调成功交付的帧数
        self._want = False             # 流水线是否正在等一帧
        self._want_region: tuple[int, int, int, int] | None = None
        self._want_full_frame = False
        self._last_frame_any = 0.0     # 最近一次收到任意帧（含未交付的）的时刻
        self._last_frame_size: tuple[int, int] | None = None  # 回调报告的帧尺寸
        self._frame_counter = 0        # 服务器侧出帧计数，用于判断"源是否还在渲染"
        self._closed_by_system = False
        self._failure: str = ""
        self._client_offset: tuple[int, int] = (0, 0)
        self._minimum_update_interval_ms = max(0, int(minimum_update_interval_ms))

    # ---- 打开 -----------------------------------------------------------
    def _do_open(self, source: SourceSpec) -> None:
        try:
            from windows_capture import WindowsCapture
        except Exception as exc:  # pragma: no cover
            raise CaptureError(f"Windows Graphics Capture 不可用：{exc}") from exc

        self.kind = source.kind
        self._closed_by_system = False
        self._failure = ""
        self._delivered = 0
        self._slot = None
        self._want = False
        self._frame_counter = 0
        self._last_frame_any = 0.0
        self._last_frame_size = None

        kwargs: dict = {
            "cursor_capture": False,     # 数据集不要系统鼠标指针
            "draw_border": False,        # 不要黄色捕获边框
            "secondary_window": None,
            "dirty_region": None,
        }
        if self._minimum_update_interval_ms > 0:
            kwargs["minimum_update_interval"] = self._minimum_update_interval_ms

        if source.kind is SourceKind.WINDOW:
            kwargs["window_hwnd"] = int(source.hwnd)
        else:
            # windows-capture 的 monitor_index 是 **1 起始**的
            # （底层会报 "The monitor index must be greater than zero"），
            # 而本工具的 monitor_index 是 0 起始的枚举下标，所以这里加一。
            # 加了之后到底选中哪块屏，由打开时的帧尺寸校验确认，不靠假设。
            kwargs["monitor_index"] = int(source.monitor_index) + 1

        try:
            capture = WindowsCapture(**kwargs)
        except Exception as exc:
            raise CaptureError(f"创建 WGC 捕获对象失败：{exc}") from exc

        # 注意：windows_capture 的 event() 是按函数的 __name__ 分派的，
        # 所以这两个局部函数的**名字不能改**。
        def on_frame_arrived(frame, _control):  # noqa: N802
            try:
                self._on_frame(frame)
            except Exception as exc:  # 回调里绝不能抛异常，否则会杀掉捕获会话
                log.warning("WGC 帧回调处理出错（已忽略该帧）：%s", exc)

        def on_closed():  # noqa: N802
            with self._cond:
                self._closed_by_system = True
                self._cond.notify_all()
            log.info("WGC 捕获会话被系统关闭（窗口可能已销毁）")

        capture.event(on_frame_arrived)
        capture.event(on_closed)
        self._capture = capture

        try:
            self._control = capture.start_free_threaded()
        except Exception as exc:
            self._capture = None
            raise CaptureError(f"启动 WGC 捕获线程失败：{exc}", recoverable=True) from exc

        # 等首帧，用它校验帧尺寸与客户区的关系。
        if not self._wait_first_frame(FIRST_FRAME_TIMEOUT):
            reason = self._failure or "WGC 在超时时间内没有交出任何帧"
            self._teardown()
            raise CaptureError(
                f"{reason}。常见原因：该窗口不允许捕获（独占全屏、受保护内容）、"
                "窗口被最小化，或游戏尚未开始渲染。",
                recoverable=True,
            )

        self._client_offset = self._resolve_client_offset(source)
        log.info(
            "WGC 已就绪：%s | 帧尺寸 %s×%s | 客户区偏移 %s",
            source.kind.label, source.source_width, source.source_height, self._client_offset,
        )

    def _wait_first_frame(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._last_frame_any <= 0.0:
                if self._closed_by_system or self._failure:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(0.1, remaining))
        return True

    def _resolve_client_offset(self, source: SourceSpec) -> tuple[int, int]:
        """确定客户区左上角相对 WGC 帧左上角的偏移，并校验尺寸关系。

        只接受能被验证的情形，按可信度从高到低：

        A. 帧尺寸 == 客户区尺寸 → WGC 只交了客户区内容，偏移 = (0, 0)。
           无边框窗口（多数全屏游戏）走的就是这一条。
        B. 帧尺寸 == 窗口尺寸且偏移分量非负 → 帧从窗口左上角开始，
           偏移 = 客户区屏幕坐标 − 窗口屏幕坐标。
        C. 其余情况一律报错。

        为什么把 A 排在 B 前面、并且拒绝负偏移：
        实际机器上见过 `ApplicationFrameHost` 这类宿主窗口，DWM 扩展边框比客户区还小，
        减出来偏移是负数。此时按 B 处理会把裁剪窗口推到帧外，
        静默钳制之后得到的是**位置错一截**的训练图——这比直接失败糟得多。
        """
        with self._cond:
            slot = self._slot
            # 优先用回调无条件记录的帧尺寸：打开阶段流水线还没开始要帧，
            # 回调不会走拷贝分支，_slot 必然是空的。
            frame_size = self._last_frame_size or (
                (slot.width, slot.height) if slot else (0, 0)
            )

        window_size = (source.window_width, source.window_height)
        client_size = (source.source_width, source.source_height)

        if frame_size == client_size:
            return (0, 0)

        if frame_size == window_size:
            offset = source.client_offset
            if offset[0] >= 0 and offset[1] >= 0:
                return offset
            raise CaptureError(
                f"窗口的左/上边框比客户区还小（客户区偏移为 {offset}），"
                "无法确定客户区在帧中的位置。该窗口通常不是游戏窗口，"
                "请改用显示器捕获模式。"
            )

        if source.kind is SourceKind.MONITOR:
            raise CaptureError(
                f"WGC 交出的画面是 {frame_size[0]}×{frame_size[1]}，"
                f"与所选显示器 {window_size[0]}×{window_size[1]} 不符——"
                "说明 WGC 的显示器顺序与本工具不一致。请改用 DXGI 后端做显示器捕获。"
            )

        raise CaptureError(
            f"WGC 交付的帧尺寸 {frame_size[0]}×{frame_size[1]} 与窗口尺寸 "
            f"{window_size[0]}×{window_size[1]} 和客户区尺寸 "
            f"{client_size[0]}×{client_size[1]} 都不一致。"
            "无法确定客户区在帧中的位置，为避免截到错误区域已停止使用该后端。"
        )

    # ---- 回调 -----------------------------------------------------------
    def _on_frame(self, frame) -> None:
        """在 WGC 自己的线程上执行。"""
        height = int(frame.height)
        width = int(frame.width)
        timespan = int(getattr(frame, "timespan", 0))
        buffer = frame.frame_buffer
        if buffer is None or height <= 0 or width <= 0:
            return

        now_mono = time.monotonic()
        now_wall = time.time()

        with self._cond:
            self._frame_counter += 1
            self._last_frame_any = now_mono
            # 帧尺寸必须**无条件**记录：打开时的客户区对齐校验依赖它，
            # 而那一刻流水线还没开始要帧，回调并不会走拷贝分支。
            self._last_frame_size = (width, height)
            want = self._want
            region_window = self._want_region
            full_frame = self._want_full_frame
            self._cond.notify_all()

        if not want or region_window is None:
            return  # 空闲：不拷贝，直接丢。这是本后端最重要的性能取舍。

        # BGRA → BGR，并只保留需要的部分。裁剪在拷贝之前完成，
        # 这样拷贝量从"整帧"降到"选区"（320×320 约 300 KB，而不是 14 MB）。
        left, top, right, bottom = region_window
        left = max(0, min(left, width))
        top = max(0, min(top, height))
        right = max(left, min(right, width))
        bottom = max(top, min(bottom, height))
        if right - left <= 0 or bottom - top <= 0:
            raise CaptureError(
                f"裁剪窗口 ({left},{top},{right},{bottom}) 落在 {width}×{height} 帧之外"
            )

        cropped = buffer[top:bottom, left:right, :3]
        image = np.ascontiguousarray(cropped)  # 这一步才是真正的拷贝，且立刻脱离底层缓冲

        slot = _Slot(
            image=image,
            width=right - left,
            height=bottom - top,
            timespan=timespan,
            captured_at=now_wall,
            monotonic=now_mono,
        )
        with self._cond:
            self._slot = slot
            self._delivered += 1
            self._cond.notify_all()

    # ---- 取帧 -----------------------------------------------------------
    def _do_grab(self, region: Region, *, timeout: float, full_frame: bool) -> Frame | None:
        source = self._source
        if source is None:
            raise CaptureError("WGC 后端未打开")
        if self._failure:
            raise CaptureError(self._failure, recoverable=True)
        if self._closed_by_system:
            raise CaptureError("捕获会话已被系统关闭（窗口可能已销毁）", recoverable=True)

        if full_frame:
            target = Region(0, 0, source.source_width, source.source_height)
        else:
            target = region
        dx, dy = self._client_offset
        window = (target.left + dx, target.top + dy, target.right + dx, target.bottom + dy)

        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            aim = self._delivered + 1
            self._want = True
            self._want_region = window
            self._want_full_frame = full_frame
            try:
                while self._delivered < aim:
                    if self._failure:
                        raise CaptureError(self._failure, recoverable=True)
                    if self._closed_by_system:
                        return None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None  # 无新帧：明确失败，不退回旧帧
                    self._cond.wait(remaining)
                slot = self._slot
                self._slot = None
            finally:
                self._want = False
                self._want_region = None

        if slot is None:
            return None
        return make_frame(
            slot.image, target, source,
            backend=self.key, backend_timestamp=slot.timespan, is_full_frame=full_frame,
        )

    # ---- 状态 -----------------------------------------------------------
    def seconds_since_last_frame(self) -> float:
        with self._cond:
            last = self._last_frame_any
        if last <= 0:
            return float("inf")
        return time.monotonic() - last

    def frame_counter(self) -> int:
        with self._cond:
            return self._frame_counter

    def probe_source(self) -> SourceSpec | None:
        from .sources import refresh_source

        if self._source is None:
            return None
        return refresh_source(self._source)

    def describe(self) -> str:
        return (
            f"{self.display_name} · {self.kind.label}捕获 · "
            f"更新间隔上限 {self._minimum_update_interval_ms} ms · "
            f"客户区偏移 {self._client_offset[0]},{self._client_offset[1]}"
        )

    # ---- 关闭 -----------------------------------------------------------
    def _teardown(self) -> None:
        control, self._control = self._control, None
        self._capture = None
        with self._cond:
            self._want = False
            self._slot = None
            self._cond.notify_all()
        if control is None:
            return
        try:
            control.stop()
        except Exception as exc:  # pragma: no cover
            log.warning("停止 WGC 捕获线程失败：%s", exc)
            return
        try:
            control.wait()
        except Exception as exc:  # pragma: no cover
            log.warning("等待 WGC 捕获线程退出失败：%s", exc)

    def _do_close(self) -> None:
        self._teardown()
