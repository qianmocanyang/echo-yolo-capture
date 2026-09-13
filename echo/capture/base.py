"""采集后端接口与采集源描述。

方案 §6.2 要求「实现统一 CaptureBackend 接口，将显示器捕获和按窗口捕获隔离」。
这里的抽象就服务于这一点：流水线只认识 :class:`CaptureBackend`，
不知道背后是 DXGI 还是 WGC，也不知道窗口句柄、显示器设备名这些细节。

采集源（Source）与"裁剪基准"的关系是整个模块的核心概念：

* 显示器：裁剪基准 = 该显示器的物理矩形，后端帧的原点也是它 → 偏移 (0, 0)。
* 窗口  ：裁剪基准 = 该窗口的 **客户区**（方案 §3.2「以选定游戏客户区中心为中心」），
  而后端交上来的帧是**整个窗口**（含标题栏边框），
  所以选区坐标要先加上客户区相对窗口原点的偏移，才能落到帧里的正确位置。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ..logging_setup import get_logger
from ..region import Region
from .frame import Frame

log = get_logger("capture")


class SourceKind(str, Enum):
    MONITOR = "monitor"
    WINDOW = "window"

    @property
    def label(self) -> str:
        return "显示器" if self is SourceKind.MONITOR else "游戏窗口"


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """一个采集源的完整描述，几何全部是物理像素。"""

    kind: SourceKind
    source_id: str                 # monitor:DISPLAY1 / window:123456
    label: str                     # 界面显示名

    # ---- 裁剪基准：选区的 (0,0) 在这里 ----
    origin_left: int
    origin_top: int
    source_width: int
    source_height: int

    # ---- 后端帧的原点：后端交上来的图从哪开始 ----
    frame_left: int = 0
    frame_top: int = 0

    # ---- 显示器模式 ----
    monitor_device: str = ""
    monitor_handle: int = 0
    monitor_index: int = 0

    # ---- 窗口模式 ----
    hwnd: int = 0
    window_left: int = 0
    window_top: int = 0
    window_width: int = 0
    window_height: int = 0
    process_name: str = ""
    title: str = ""

    # 跨重启稳定的身份串（句柄不能复用，方案 §9.1）
    identity: str = ""

    @property
    def client_offset(self) -> tuple[int, int]:
        """客户区左上角相对**后端帧**左上角的偏移。"""
        return (self.origin_left - self.frame_left, self.origin_top - self.frame_top)

    def frame_size_hint(self) -> tuple[int, int]:
        """后端帧的预期尺寸。"""
        if self.kind is SourceKind.MONITOR:
            return (self.source_width, self.source_height)
        return (self.window_width, self.window_height)

    def region_to_frame_window(self, region: Region) -> tuple[int, int, int, int]:
        """把「相对客户区」的选区换算成在**后端帧**中的裁剪窗口。

        返回 (left, top, right, bottom)，右/下为开区间。
        """
        dx, dy = self.client_offset
        return (region.left + dx, region.top + dy, region.right + dx, region.bottom + dy)

    def describe(self) -> str:
        return (
            f"{self.kind.label} · {self.label} · 源 {self.source_width}×{self.source_height}px"
        )

    def identity_matches(self, other: "SourceSpec") -> bool:
        return bool(self.identity) and self.identity == other.identity


@dataclass(frozen=True, slots=True)
class BackendCapability:
    """后端的可用性与已知边界。不可用时必须给出原因，不能静默降级。"""

    key: str
    display_name: str
    kinds: frozenset[SourceKind]
    available: bool
    reason: str = ""
    notes: tuple[str, ...] = ()

    def supports(self, kind: SourceKind) -> bool:
        return kind in self.kinds

    @property
    def summary(self) -> str:
        if self.available:
            return "可用" if not self.notes else "可用（有已知边界）"
        return f"不可用：{self.reason}"


class CaptureError(RuntimeError):
    """采集相关的错误。"""

    def __init__(self, message: str, *, recoverable: bool = False):
        super().__init__(message)
        self.recoverable = recoverable


class CaptureBackend(ABC):
    """所有采集后端的公共接口。

    生命周期：``open()`` → 反复 ``grab()`` → ``close()``。
    实例由采集线程独占，不跨线程共享（方案 §8.3「不直接跨线程修改采集对象」）。
    """

    key: str = "abstract"
    display_name: str = "抽象后端"
    kind: SourceKind = SourceKind.MONITOR

    def __init__(self) -> None:
        self._source: SourceSpec | None = None
        self._sequence = 0
        self._opened = False

    # ---- 必须实现 -------------------------------------------------------
    @abstractmethod
    def _do_open(self, source: SourceSpec) -> None:
        ...

    @abstractmethod
    def _do_grab(self, region: Region, *, timeout: float, full_frame: bool) -> Frame | None:
        ...

    @abstractmethod
    def _do_close(self) -> None:
        ...

    @abstractmethod
    def probe_source(self) -> SourceSpec | None:
        """重新读取采集源当前几何，用于检测窗口移动 / 分辨率变化。"""

    @abstractmethod
    def describe(self) -> str:
        """一行自述，写进日志与状态栏，方便定位"到底用的哪个后端"。"""

    # ---- 公共流程 -------------------------------------------------------
    @property
    def source(self) -> SourceSpec | None:
        return self._source

    @property
    def is_open(self) -> bool:
        return self._opened

    def open(self, source: SourceSpec) -> None:
        if self._opened:
            self.close()
        self._do_open(source)
        self._source = source
        self._opened = True
        self._sequence = 0
        log.info("采集后端已就绪：%s | %s", self.describe(), source.describe())

    def grab(
        self, region: Region, *, timeout: float = 0.0, full_frame: bool = False
    ) -> Frame | None:
        """取一帧。

        ``timeout > 0`` 时会等待到有**新帧**为止；返回 None 表示超时无新帧，
        调用方必须把它当作"失败"处理，不得退回使用旧帧（方案 §4.2）。
        """
        if not self._opened or self._source is None:
            raise CaptureError("采集后端尚未打开")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            frame = self._do_grab(region, timeout=timeout, full_frame=full_frame)
            if frame is not None:
                self._sequence += 1
                frame.sequence = self._sequence
                return frame
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.001)

    def close(self) -> None:
        if not self._opened:
            return
        try:
            self._do_close()
        except Exception as exc:  # pragma: no cover - 关闭失败也要把状态复位
            log.warning("关闭采集后端时出错：%s", exc)
        finally:
            self._opened = False
            self._source = None

    def __enter__(self) -> "CaptureBackend":
        if self._source is None:
            raise CaptureError("请先 open(source)")
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def make_frame(
    image,
    region: Region,
    source: SourceSpec,
    *,
    backend: str,
    backend_timestamp: int | None = None,
    is_full_frame: bool = False,
) -> Frame:
    """统一构造 Frame，保证时间戳口径一致。"""
    now = time.time()
    return Frame(
        image=image,
        region=region,
        source_width=source.source_width,
        source_height=source.source_height,
        sequence=0,
        backend=backend,
        source_id=source.source_id,
        captured_at=now,
        monotonic=time.monotonic(),
        backend_timestamp=backend_timestamp,
        is_full_frame=is_full_frame,
    )
