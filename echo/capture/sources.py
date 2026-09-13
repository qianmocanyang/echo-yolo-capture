"""采集源枚举与"重新发现"。

方案 §9.1 的一句关键约束：**窗口句柄不能跨程序重启直接复用，应重新发现并确认目标**。
所以配置里存的是 *身份串*，不是句柄。身份串刻意排除窗口标题——
很多游戏会在标题里实时显示帧率或地图名，用标题做键会导致"每次重启都认不出来"。

身份串定义：
* 显示器：``monitor|<设备名>``，例如 ``monitor|\\\\.\\DISPLAY1``
* 窗口：``window|<进程名>|<窗口类名>``，例如 ``window|Subnautica2.exe|UnrealWindow``
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import winapi
from ..logging_setup import get_logger
from .base import SourceKind, SourceSpec

log = get_logger("capture.sources")


def monitor_identity(device: str) -> str:
    return f"monitor|{device}"


def window_identity(process_name: str, class_name: str) -> str:
    return f"window|{process_name}|{class_name}"


# --------------------------------------------------------------------------
# 显示器
# --------------------------------------------------------------------------

def monitor_spec(mon: winapi.MonitorInfo, index: int) -> SourceSpec:
    """把一个 Win32 显示器转成采集源。

    显示器模式下裁剪基准与后端帧原点**都是**显示器左上角，所以 client_offset 恒为 (0,0)。
    """
    label = f"显示器 {index + 1}"
    if mon.primary:
        label += "（主）"
    label += f" · {mon.width}×{mon.height}"
    if abs(mon.scale - 1.0) > 0.01:
        label += f" · {mon.scale * 100:.0f}%"

    return SourceSpec(
        kind=SourceKind.MONITOR,
        source_id=f"monitor:{mon.device}",
        label=label,
        origin_left=mon.left,
        origin_top=mon.top,
        source_width=mon.width,
        source_height=mon.height,
        frame_left=mon.left,
        frame_top=mon.top,
        monitor_device=mon.device,
        monitor_handle=mon.handle,
        monitor_index=index,
        identity=monitor_identity(mon.device),
    )


def list_monitors() -> list[SourceSpec]:
    return [monitor_spec(mon, index) for index, mon in enumerate(winapi.enum_monitors())]


# --------------------------------------------------------------------------
# 窗口
# --------------------------------------------------------------------------

def window_spec(info: winapi.WindowInfo, monitor_lookup: dict[str, winapi.MonitorInfo] | None = None) -> SourceSpec:
    """把一个 Win32 窗口转成采集源。

    * 裁剪基准 = **客户区**（客户区左上角在虚拟桌面中的物理坐标 + 客户区尺寸）；
    * 后端帧原点 = 窗口可见区域左上角（DWM 扩展边框，不含阴影）。

    两者之差就是 :attr:`SourceSpec.client_offset`，后续所有选区换算都用它。
    """
    client_left = info.client_left
    client_top = info.client_top
    client_width = max(0, info.client_width)
    client_height = max(0, info.client_height)

    return SourceSpec(
        kind=SourceKind.WINDOW,
        source_id=f"window:{info.hwnd}",
        label=winapi.window_display_name(info),
        origin_left=client_left,
        origin_top=client_top,
        source_width=client_width,
        source_height=client_height,
        frame_left=info.left,
        frame_top=info.top,
        hwnd=info.hwnd,
        window_left=info.left,
        window_top=info.top,
        window_width=info.width,
        window_height=info.height,
        process_name=info.process_name,
        title=info.title,
        monitor_device=_device_for_handle(info.hmonitor, monitor_lookup),
        monitor_handle=info.hmonitor,
        identity=window_identity(info.process_name, info.class_name),
    )


def _device_for_handle(handle: int, lookup: dict[str, winapi.MonitorInfo] | None) -> str:
    if lookup is None:
        return ""
    for mon in lookup.values():
        if mon.handle == handle:
            return mon.device
    return ""


def list_windows(include_minimized: bool = True) -> list[SourceSpec]:
    monitors = {str(m.handle): m for m in winapi.enum_monitors()}
    specs: list[SourceSpec] = []
    for info in winapi.enum_windows(include_minimized=include_minimized):
        spec = window_spec(info, monitors)
        if spec.source_width > 0 and spec.source_height > 0:
            specs.append(spec)
    return specs


def list_sources(kind: SourceKind, include_minimized: bool = True) -> list[SourceSpec]:
    if kind is SourceKind.MONITOR:
        return list_monitors()
    return list_windows(include_minimized=include_minimized)


def find_by_identity(identity: str, kind: SourceKind | None = None) -> list[SourceSpec]:
    """按身份串重新发现采集源，可能返回多个（同一个游戏开了两个窗口）。

    方案 §9.1：同一游戏存在多个窗口时让用户选择，所以这里返回列表而不是单个。
    """
    if not identity:
        return []
    if identity.startswith("monitor|"):
        return [spec for spec in list_monitors() if spec.identity == identity]
    if identity.startswith("window|"):
        return [spec for spec in list_windows() if spec.identity == identity]
    return []


def refresh_source(spec: SourceSpec) -> SourceSpec | None:
    """重新读取采集源当前几何。返回 None 表示采集源已经不可用。

    用途有两个：
    1. 窗口被移动 / 缩放后，客户区相对屏幕的位置变了，选区要不要跟着走；
    2. 检测分辨率变化，触发方案 §3.3 的「暂停采集并重新确认范围」。
    """
    if spec.kind is SourceKind.MONITOR:
        for mon in winapi.enum_monitors():
            if mon.device == spec.monitor_device:
                return monitor_spec(mon, spec.monitor_index)
        return None

    if not winapi.is_window(spec.hwnd):
        return None
    info = winapi.refresh_window(spec.hwnd)
    if info is None:
        return None
    monitors = {str(m.handle): m for m in winapi.enum_monitors()}
    refreshed = window_spec(info, monitors)

    # 身份串不同（进程名/类名变了）说明这个句柄已经被系统复用了，绝不能继续用。
    if spec.identity and refreshed.identity != spec.identity:
        log.warning(
            "窗口已变化，句柄 %s 被复用（%s → %s），停止采集",
            spec.hwnd, spec.identity, refreshed.identity,
        )
        return None
    return refreshed


def is_source_usable(spec: SourceSpec) -> tuple[bool, str]:
    """判断采集源当前是否可用，给出不可用的原因供界面展示。"""
    if spec.kind is SourceKind.MONITOR:
        for mon in winapi.enum_monitors():
            if mon.device == spec.monitor_device:
                return True, ""
        return False, "该显示器已断开"

    if not winapi.is_window(spec.hwnd):
        return False, "窗口已关闭"
    info = winapi.refresh_window(spec.hwnd)
    if info is None:
        return False, "窗口已关闭"
    if info.minimized:
        return False, "窗口已最小化，无法采集到新画面"
    if info.client_width <= 0 or info.client_height <= 0:
        return False, "窗口客户区尺寸为 0"
    return True, ""


# --------------------------------------------------------------------------
# dxcam 输出索引解析
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DxgiOutput:
    device_index: int
    output_index: int
    width: int
    height: int
    rotation: int
    primary: bool


def parse_output_info(text: str) -> list[DxgiOutput]:
    """解析 ``dxcam.output_info()`` 的文本。

    返回的是形如 ``Device[0] Output[0]: Res:(2560, 1440) Rot:0 Primary:True`` 的行。
    自己解析而不依赖库的私有结构，是因为这里只用于**挑选**采集设备，
    挑不到时应当明确报不可用，而不是猜。
    """
    import re

    pattern = re.compile(
        r"Device\[(?P<dev>\d+)\]\s*Output\[(?P<out>\d+)\]:\s*"
        r"Res:\((?P<w>\d+),\s*(?P<h>\d+)\)\s*"
        r"Rot:(?P<rot>-?\d+)\s*"
        r"Primary:(?P<primary>True|False)",
        re.IGNORECASE,
    )
    outputs: list[DxgiOutput] = []
    for match in pattern.finditer(text or ""):
        outputs.append(
            DxgiOutput(
                device_index=int(match.group("dev")),
                output_index=int(match.group("out")),
                width=int(match.group("w")),
                height=int(match.group("h")),
                rotation=int(match.group("rot")),
                primary=match.group("primary").lower() == "true",
            )
        )
    return outputs


def resolve_dxgi_output(
    monitor: winapi.MonitorInfo, outputs: list[DxgiOutput]
) -> tuple[DxgiOutput | None, str]:
    """把 Win32 显示器匹配到 dxcam 的输出索引。

    匹配依据是「分辨率 + 是否主显示器」。不唯一时倾向主屏匹配项，
    仍然不唯一才放弃——放弃时返回原因，由上层把后端标为不可用。
    """
    if not outputs:
        return None, "没有检测到可用的 DXGI 输出"
    if abs(monitor.scale - 1.0) > 0.01:
        # 高 DPI 缩放下 GetMonitorInfo 给的是物理分辨率，DXGI 给的也是物理分辨率，
        # 两者一致，所以这里只记录不作判断。
        pass

    exact = [
        o for o in outputs
        if o.width == monitor.width and o.height == monitor.height
    ]
    if not exact:
        # 显示方向为 90/270 时 DXGI 报告的宽高是旋转后的，做一次交换再试。
        exact = [
            o for o in outputs
            if o.width == monitor.height and o.height == monitor.width
        ]
    if not exact:
        return None, (
            f"DXGI 输出里没有 {monitor.width}×{monitor.height} 的显示设备"
            f"（可用：{', '.join(f'{o.width}×{o.height}' for o in outputs) or '无'}）"
        )
    if len(exact) == 1:
        return exact[0], ""

    primaries = [o for o in exact if o.primary == monitor.primary]
    if len(primaries) == 1:
        return primaries[0], ""
    return None, "多个 DXGI 输出分辨率相同且无法区分，请改用窗口捕获模式"
