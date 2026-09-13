"""采集层：统一后端接口、显示器（DXGI）与窗口（WGC）实现、能力探测。"""

from .base import (
    BackendCapability,
    CaptureBackend,
    CaptureError,
    SourceKind,
    SourceSpec,
    make_frame,
)
from .factory import (
    BACKEND_ORDER,
    COMPATIBILITY_MATRIX,
    DEFAULT_BACKEND,
    BackendResolution,
    CompatibilityRow,
    backend_display_name,
    capabilities_for,
    create_backend,
    probe_all,
    resolve_backend,
)
from .frame import Frame
from .sources import (
    find_by_identity,
    is_source_usable,
    list_monitors,
    list_sources,
    list_windows,
    monitor_identity,
    monitor_spec,
    parse_output_info,
    refresh_source,
    resolve_dxgi_output,
    window_identity,
    window_spec,
)

__all__ = [
    "BackendCapability", "CaptureBackend", "CaptureError", "SourceKind", "SourceSpec",
    "make_frame", "Frame",
    "BACKEND_ORDER", "COMPATIBILITY_MATRIX", "DEFAULT_BACKEND", "BackendResolution",
    "CompatibilityRow", "backend_display_name", "capabilities_for", "create_backend",
    "probe_all", "resolve_backend",
    "find_by_identity", "is_source_usable", "list_monitors", "list_sources", "list_windows",
    "monitor_identity", "monitor_spec", "parse_output_info", "refresh_source",
    "resolve_dxgi_output", "window_identity", "window_spec",
]
