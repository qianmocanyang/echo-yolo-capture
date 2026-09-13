"""帧数据结构。

方案 §8.3 要求「异步保存前复制需保留的图像数据，避免底层环形缓冲区覆盖导致保存错帧」。
所以 :class:`Frame` 里的 ``image`` **一定**是调用方独占的内存：

* DXcam 走 ``copy=True``；
* WGC 回调里的 ``frame_buffer`` 是底层映射内存的零拷贝视图，
  回调返回后就失效，必须在回调内先裁剪再 ``.copy()``。

这两点都在各自的 backend 里强制做掉，不依赖调用方自觉。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from ..region import Region


@dataclass(slots=True)
class Frame:
    """一帧已经裁剪好的画面，BGR uint8，HxWx3。"""

    image: np.ndarray
    region: Region
    source_width: int
    source_height: int
    sequence: int
    backend: str
    source_id: str
    # wall clock 时间，写进文件名与数据库
    captured_at: float
    # 单调时钟，用于新鲜度判断与节流，不受系统时间调整影响
    monotonic: float
    # 采集后端自己给的时间戳（DXcam 用 QPC 计数，WGC 用 timespan / 100ns）。
    # 方案 §6.2：新帧时间戳与图像相似度是两件事，分开处理。
    backend_timestamp: int | None = None
    # 是否采集源全画面（预览"查看源画面"模式需要）
    is_full_frame: bool = False

    @property
    def size(self) -> tuple[int, int]:
        return (self.image.shape[1], self.image.shape[0])

    def age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.monotonic)
