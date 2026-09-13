"""后端选择与能力探测。

方案 §6.2 的红线：「后端切换需让用户知晓，**不能静默改成显示器截图并继续收集被遮挡画面**」。
所以这个模块的职责不是"尽力找到一个能跑的后端"，而是：

* 把每个后端的可用性和已知边界如实报出来（包括"可用但有边界"）；
* 用户要求窗口捕获时，只允许真正支持窗口捕获的后端；找不到就如实失败；
* 任何降级都返回一条 ``warning`` 文本，由界面展示给用户，绝不悄悄换掉。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..logging_setup import get_logger
from .base import (
    DEFAULT_DELIVERY_INTERVAL_MS,
    BackendCapability,
    CaptureBackend,
    CaptureError,
    SourceKind,
)

log = get_logger("capture.factory")

BACKEND_ORDER: tuple[str, ...] = ("dxgi", "wgc")

# 每种采集源的默认后端
DEFAULT_BACKEND = {
    SourceKind.MONITOR: "dxgi",
    SourceKind.WINDOW: "wgc",
}


def _dxgi_capability() -> BackendCapability:
    from . import dxgi_monitor

    return dxgi_monitor.capability()


def _wgc_capability() -> BackendCapability:
    from . import wgc_window

    return wgc_window.capability()


def probe_all() -> dict[str, BackendCapability]:
    """探测全部后端。首次运行的自检与设置页面都用它。"""
    result: dict[str, BackendCapability] = {}
    for key in BACKEND_ORDER:
        try:
            result[key] = _dxgi_capability() if key == "dxgi" else _wgc_capability()
        except Exception as exc:  # 探测本身不能把应用拖垮
            log.warning("探测后端 %s 时出错：%s", key, exc)
            result[key] = BackendCapability(
                key=key, display_name=key.upper(), kinds=frozenset(),
                available=False, reason=f"探测失败：{exc}",
            )
    return result


def capabilities_for(kind: SourceKind) -> dict[str, BackendCapability]:
    return {k: c for k, c in probe_all().items() if c.supports(kind)}


@dataclass(frozen=True, slots=True)
class BackendResolution:
    """后端选择结果。``warning`` 非空时必须展示给用户。"""

    key: str
    capability: BackendCapability
    warning: str = ""

    @property
    def ok(self) -> bool:
        return self.capability.available

    @property
    def reason(self) -> str:
        return self.capability.reason


def resolve_backend(requested: str, kind: SourceKind) -> BackendResolution:
    """按用户选择与采集源类型定后端。

    规则：
    1. 用户指定的后端支持该采集源类型且可用 → 直接用。
    2. 用户指定的后端不可用 → 只有在**存在同类型替代后端**时才降级，并给出 warning；
       窗口捕获没有替代方案时直接失败。
    3. 用户没指定（空串）→ 用该采集源类型的默认后端。
    """
    caps = probe_all()
    requested = (requested or "").strip().lower()

    if requested and requested in caps:
        cap = caps[requested]
        if not cap.supports(kind):
            # 例如：用户选了 dxgi 但采集源是窗口。这不是"不可用"，是"用错了"。
            fallback_key = DEFAULT_BACKEND.get(kind, "")
            fallback = caps.get(fallback_key)
            if fallback is not None and fallback.available and fallback.supports(kind):
                return BackendResolution(
                    key=fallback_key, capability=fallback,
                    warning=(
                        f"{cap.display_name} 不支持{kind.label}采集，"
                        f"已改为 {fallback.display_name}。"
                    ),
                )
            return BackendResolution(
                key=requested, capability=BackendCapability(
                    key=cap.key, display_name=cap.display_name, kinds=cap.kinds,
                    available=False,
                    reason=f"{cap.display_name} 不支持{kind.label}采集，且没有可用的替代后端",
                ),
            )
        if cap.available:
            return BackendResolution(key=requested, capability=cap)

        # 指定的后端不可用：找同类型的替代，但必须告知
        for key in BACKEND_ORDER:
            if key == requested:
                continue
            other = caps.get(key)
            if other is None or not other.available or not other.supports(kind):
                continue
            # 窗口 → 显示器这种降级是不允许悄悄做的
            if kind is SourceKind.WINDOW and key == "dxgi":
                continue
            return BackendResolution(
                key=key, capability=other,
                warning=(
                    f"{cap.display_name} 当前不可用（{cap.reason}），"
                    f"已切换到 {other.display_name}。请确认画面内容仍符合预期。"
                ),
            )
        return BackendResolution(key=requested, capability=cap)

    # 未指定：挑第一个可用的、且支持该类型的后端
    preferred = DEFAULT_BACKEND.get(kind, BACKEND_ORDER[0])
    cap = caps.get(preferred)
    if cap is not None and cap.available and cap.supports(kind):
        return BackendResolution(key=preferred, capability=cap)
    for key in BACKEND_ORDER:
        cap = caps.get(key)
        if cap is not None and cap.available and cap.supports(kind):
            warning = ""
            if key != preferred:
                warning = (
                    f"默认后端 {preferred} 不可用"
                    f"（{caps.get(preferred).reason if caps.get(preferred) else '未安装'}），"
                    f"已使用 {cap.display_name}。"
                )
            return BackendResolution(key=key, capability=cap, warning=warning)
    return BackendResolution(
        key=preferred, capability=BackendCapability(
            key=preferred, display_name=preferred, kinds=frozenset(),
            available=False, reason=f"没有可用于{kind.label}采集的后端",
        ),
    )


def create_backend(
    key: str, *, minimum_update_interval_ms: int = DEFAULT_DELIVERY_INTERVAL_MS
) -> CaptureBackend:
    """按 key 建后端。

    ``minimum_update_interval_ms`` 是**交付节流**，别随手用默认值以外的数：

    * DXGI 走 dxcam 的 ``target_fps``，不显式指定时是 60 —— 实测在 1080p 上
      光是把画面复制出来就吃掉 97.8% 单核，而定时采集往往只要 1 张/秒；
    * WGC 走 ``minimum_update_interval``，默认 16 ms 同样是 60 fps 的交付量。

    调用方（pipeline）会按当前实际采集需求算出这个值。此处保留一个温和的
    默认值，是为了即使有人漏传也不会把 CPU 吃满。
    """
    if key == "dxgi":
        from .dxgi_monitor import DxgiMonitorBackend

        return DxgiMonitorBackend(minimum_update_interval_ms=minimum_update_interval_ms)
    if key == "wgc":
        from .wgc_window import WgcBackend

        return WgcBackend(minimum_update_interval_ms=minimum_update_interval_ms)
    raise CaptureError(f"未知的采集后端：{key}")


def backend_display_name(key: str) -> str:
    if key == "dxgi":
        return "DXGI 显示器捕获（DXcam）"
    if key == "wgc":
        return "Windows Graphics Capture"
    return key or "未指定"


# --------------------------------------------------------------------------
# 兼容性矩阵（方案 §6.1）
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CompatibilityRow:
    """方案 §6.1 的行为矩阵，界面上的"兼容性说明"直接读这里，避免文档与代码漂移。"""

    scenario: str
    monitor_backend: str
    window_backend: str


COMPATIBILITY_MATRIX: tuple[CompatibilityRow, ...] = (
    CompatibilityRow("工具失去焦点", "快捷键继续有效，采集继续", "同左"),
    CompatibilityRow("工具最小化 / 收起托盘", "继续运行，暂停预览以降低占用", "同左"),
    CompatibilityRow("游戏失去焦点但仍渲染", "默认暂停自动采集（可配置）", "继续采集，按新帧状态判断"),
    CompatibilityRow("游戏被其他窗口遮挡", "遮挡内容会进入截图", "由游戏自身渲染决定，需实测"),
    CompatibilityRow("游戏最小化 / 停止渲染", "进入等待画面，不承诺采集", "进入等待画面，不承诺采集"),
    CompatibilityRow("系统锁屏 / 休眠 / 显示切换", "暂停并释放或重建资源", "同左"),
)
