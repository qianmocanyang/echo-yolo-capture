"""显示器采集后端（DXGI Desktop Duplication，经 DXcam 封装）。

方案 §6.2 把这一路的边界写得很清楚，代码里同样保留这些边界：

| 能力 | 本后端 |
| --- | --- |
| 全屏或固定区域捕获 | 支持 |
| 截到被其他窗口遮挡的内容 | **会**，因为拿到的是显示器的合成画面 |
| 游戏最小化 / 停止渲染后继续出图 | 不会，进入"等待画面" |
| 游戏失去前台时是否继续自动采集 | 由 pipeline 按配置暂停（默认暂停） |

关于"游戏最小化后 DXGI 是否还出帧"：DXGI Desktop Duplication 拿的是桌面合成输出，
游戏最小化后桌面照常有画面变化（鼠标、任务栏），所以 **grab() 可能仍然返回新帧**，
但那些帧里没有游戏画面。方案 §6.1 把这种情况归为「进入等待画面，不承诺持续采集」，
所以这里额外提供「前台校验」钩子，由 pipeline 决定是否采信。

区域坐标语义：dxcam 的 region 是相对该输出（显示器）左上角的物理像素，
而我们的 :class:`~echo.capture.base.SourceSpec` 对显示器模式恰好也以显示器左上角为原点，
所以可以直接传。这一点在 :meth:`_do_open` 里做了运行时校验——
校验不过就明确报错，绝不带猜地继续采集。
"""

from __future__ import annotations

import numpy as np

from .. import winapi
from ..logging_setup import get_logger
from ..region import Region
from .base import (
    DEFAULT_DELIVERY_INTERVAL_MS,
    BackendCapability,
    CaptureBackend,
    CaptureError,
    SourceKind,
    SourceSpec,
    make_frame,
)
from .frame import Frame
from .sources import parse_output_info, resolve_dxgi_output

log = get_logger("capture.dxgi")

BACKEND_KEY = "dxgi"
BACKEND_NAME = "DXGI 显示器捕获（DXcam）"


def capability() -> BackendCapability:
    notes = [
        "被其他窗口遮挡的内容会进入截图",
        "游戏最小化后不保证仍有新画面",
        "HDR 开启时可能偏白，需按 SDR 流程验收",
    ]
    try:
        import dxcam  # noqa: F401
    except Exception as exc:
        return BackendCapability(
            key=BACKEND_KEY, display_name=BACKEND_NAME,
            kinds=frozenset({SourceKind.MONITOR}),
            available=False, reason=f"DXcam 未安装或无法导入：{exc}", notes=tuple(notes),
        )
    try:
        outputs = parse_output_info(_raw_output_info())
    except Exception as exc:
        return BackendCapability(
            key=BACKEND_KEY, display_name=BACKEND_NAME,
            kinds=frozenset({SourceKind.MONITOR}),
            available=False, reason=f"枚举 DXGI 输出失败：{exc}", notes=tuple(notes),
        )
    if not outputs:
        return BackendCapability(
            key=BACKEND_KEY, display_name=BACKEND_NAME,
            kinds=frozenset({SourceKind.MONITOR}),
            available=False, reason="系统没有可用的 DXGI 输出设备", notes=tuple(notes),
        )
    return BackendCapability(
        key=BACKEND_KEY, display_name=BACKEND_NAME,
        kinds=frozenset({SourceKind.MONITOR}),
        available=True, notes=tuple(notes),
    )


def _raw_output_info() -> str:
    import dxcam

    return dxcam.output_info()


class DxgiMonitorBackend(CaptureBackend):
    key = BACKEND_KEY
    display_name = BACKEND_NAME
    kind = SourceKind.MONITOR

    def __init__(
        self, *, minimum_update_interval_ms: int = DEFAULT_DELIVERY_INTERVAL_MS
    ) -> None:
        super().__init__()
        self._camera = None
        self._output_index = 0
        self._device_index = 0
        self._last_backend_ts: int | None = None
        self._delivery_interval_ms = max(1, int(minimum_update_interval_ms))

    # ---- 打开 / 关闭 ----------------------------------------------------
    def _do_open(self, source: SourceSpec) -> None:
        import dxcam

        if source.kind is not SourceKind.MONITOR:
            raise CaptureError("DXGI 后端只支持显示器采集源，请改用窗口捕获（WGC）")

        monitor = None
        for mon in winapi.enum_monitors():
            if mon.device == source.monitor_device:
                monitor = mon
                break
        if monitor is None:
            raise CaptureError(f"找不到显示器 {source.monitor_device}，可能已断开")

        outputs = parse_output_info(_raw_output_info())
        chosen, reason = resolve_dxgi_output(monitor, outputs)
        if chosen is None:
            raise CaptureError(f"DXGI 后端不可用：{reason}")

        try:
            camera = dxcam.create(
                device_idx=chosen.device_index,
                output_idx=chosen.output_index,
                output_color="BGR",   # 直接给 OpenCV 可用的通道序，省一次转换
                backend="dxgi",
                processor_backend="cv2",
            )
        except Exception as exc:
            raise CaptureError(f"创建 DXGI 采集设备失败：{exc}", recoverable=True) from exc

        if camera is None:
            raise CaptureError("DXGI 采集设备创建后为空，该输出可能不可用", recoverable=True)

        # 运行时校验：dxcam 的 region 必须落在 (0,0)-(camera.width, camera.height) 内。
        # 显示器模式要求它正好等于显示器的物理尺寸，否则说明坐标语义和我们的假设不一致。
        if (camera.width, camera.height) != (monitor.width, monitor.height):
            camera.release()
            raise CaptureError(
                "DXGI 输出尺寸与显示器物理尺寸不一致"
                f"（DXGI {camera.width}×{camera.height} vs 显示器 {monitor.width}×{monitor.height}）。"
                "为避免截到错误区域，已停止使用该后端；请改用窗口捕获模式。"
            )

        # 显式启动并指定帧率。不这么做 dxcam 会用它的默认 target_fps=60：
        # 即使我们每秒只要一张，它也会以 60 fps 持续把整屏复制出来——1080p 上
        # 实测吃掉 97.8% 单核，这是采集期间游戏掉帧的主因；压到 10 fps 只需 5%。
        target_fps = max(1, min(240, int(1000 / self._delivery_interval_ms)))
        try:
            camera.start(target_fps=target_fps, video_mode=False)
        except Exception as exc:
            # 退回 grab() 的自动启动：能用，但开销大。如实记日志，不静默吞掉。
            log.warning(
                "DXGI 显式启动失败（%s），将退回 dxcam 默认帧率；"
                "采集期间 CPU 占用会明显偏高", exc,
            )
        else:
            log.info(
                "DXGI 交付节流 %d ms（target_fps=%d）",
                self._delivery_interval_ms, target_fps,
            )

        self._camera = camera
        self._output_index = chosen.output_index
        self._device_index = chosen.device_index
        self._last_backend_ts = None
        log.info(
            "DXGI 输出已选定：device=%s output=%s %s×%s rot=%s primary=%s",
            chosen.device_index, chosen.output_index,
            chosen.width, chosen.height, chosen.rotation, chosen.primary,
        )

    def _do_close(self) -> None:
        camera, self._camera = self._camera, None
        if camera is None:
            return
        try:
            camera.release()
        except Exception as exc:  # pragma: no cover
            log.warning("释放 DXGI 设备时出错：%s", exc)

    # ---- 取帧 -----------------------------------------------------------
    def _do_grab(self, region: Region, *, timeout: float, full_frame: bool) -> Frame | None:
        if self._camera is None or self._source is None:
            raise CaptureError("DXGI 设备未打开")

        source = self._source
        # 显示器模式下，采集源原点就是显示器原点，所以整幅画面的 region 就是显示器尺寸。
        if full_frame:
            target = Region(0, 0, source.source_width, source.source_height)
        else:
            if not region.is_inside(source.source_width, source.source_height):
                raise CaptureError(
                    f"选区 {region} 超出显示器范围 {source.source_width}×{source.source_height}"
                )
            target = region

        try:
            # 不带 region：dxcam 一旦显式 start() 过就不再接受 grab(region=...)，
            # 捕获区域是在 _do_open 的 start() 里定下的（整个输出）。
            #
            # new_frame_only=True：没有新帧时返回 None——这正是方案 §4.2 要的语义，
            # 宁可明确失败，也不把陈旧帧当成功结果。
            array = self._camera.grab(copy=True, new_frame_only=True)
        except Exception as exc:
            raise CaptureError(f"DXGI 取帧失败：{exc}", recoverable=True) from exc

        if array is None:
            return None

        # 裁剪放在这里做。crop_window() 给的是开区间 (left, top, right, bottom)，
        # 正好是 numpy 切片的语义：切片本身零拷贝，_ensure_bgr 里的
        # ascontiguousarray 才真正复制一次——比让 dxcam 在拷贝阶段裁还少一次搬运。
        if not full_frame:
            left, top, right, bottom = target.crop_window()
            if array.shape[0] < bottom or array.shape[1] < right:
                raise CaptureError(
                    f"选区 {target} 超出 DXGI 输出范围 "
                    f"{array.shape[1]}×{array.shape[0]}"
                )
            array = array[top:bottom, left:right]

        image = _ensure_bgr(array)
        if image.shape[0] != target.height or image.shape[1] != target.width:
            raise CaptureError(
                f"DXGI 返回的帧尺寸 {image.shape[1]}×{image.shape[0]} "
                f"与请求的 {target.width}×{target.height} 不一致，已丢弃该帧"
            )

        ticks = None
        try:
            ticks = int(self._camera.latest_frame_ticks)
        except Exception:  # pragma: no cover
            ticks = None

        return make_frame(
            image, target, source,
            backend=self.key, backend_timestamp=ticks, is_full_frame=full_frame,
        )

    def probe_source(self) -> SourceSpec | None:
        from .sources import refresh_source

        if self._source is None:
            return None
        return refresh_source(self._source)

    def describe(self) -> str:
        dev = getattr(self._camera, "device", None)
        return (
            f"{self.display_name} · device={self._device_index} output={self._output_index}"
            f"{f' · {dev}' if dev else ''}"
        )


def _ensure_bgr(array: np.ndarray) -> np.ndarray:
    """保证是连续内存的 BGR uint8 三通道数组。"""
    if array.ndim == 3 and array.shape[2] == 4:
        array = array[:, :, :3]
    elif array.ndim == 2:
        import cv2

        array = cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
    return np.ascontiguousarray(array)
