"""Win32 原生接口绑定（ctypes）。

只封装本工具真正需要的部分，避免引入 pywin32 之外的额外依赖假设。
坐标约定：
  * 本模块返回的一切矩形都是 **物理像素**，位于 Windows 虚拟桌面坐标系中，
    多显示器时主屏左上角为 (0,0)，主屏左侧的显示器 X 为负数。
  * 要让 Win32 返回物理像素，进程必须是 Per-Monitor V2 DPI 感知。
    由 :func:`enable_per_monitor_dpi_awareness` 在创建 QApplication 之前调用；
    Qt 6 在 Windows 上默认已是 Per-Monitor V2，重复调用是幂等的。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import POINTER, Structure, WINFUNCTYPE, byref, c_int, c_uint, c_void_p, sizeof
from ctypes import wintypes
from dataclasses import dataclass

from .logging_setup import get_logger

log = get_logger("winapi")

IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

# SetProcessDpiAwarenessContext
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = c_void_p(-4)
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = c_void_p(-3)
DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = c_void_p(-2)

# GetWindowLong
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CHILD = 0x40000000
WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020

# GetWindow
GW_OWNER = 4

# RegisterHotKey 修饰键
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312

# DwmGetWindowAttribute
DWMWA_EXTENDED_FRAME_BOUNDS = 9

# GetSystemMetrics
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
SM_CMONITORS = 80

MONITOR_DEFAULTTONEAREST = 2

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

# OpenProcessToken / GetTokenInformation
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20


# --------------------------------------------------------------------------
# 结构体
# --------------------------------------------------------------------------

if IS_WINDOWS:
    RECT = wintypes.RECT
    POINT = wintypes.POINT
    HWND = wintypes.HWND
    DWORD = wintypes.DWORD
    LONG = wintypes.LONG
    LPARAM = wintypes.LPARAM
    WPARAM = wintypes.WPARAM
else:  # pragma: no cover - 仅在非 Windows 上用于导入期占位
    class RECT(Structure):
        _fields_ = [("left", c_int), ("top", c_int), ("right", c_int), ("bottom", c_int)]

    class POINT(Structure):
        _fields_ = [("x", c_int), ("y", c_int)]

    HWND = c_void_p
    DWORD = c_uint
    LONG = c_int
    LPARAM = ctypes.c_ssize_t
    WPARAM = ctypes.c_size_t


class MONITORINFOEXW(Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", DWORD),
        ("szDevice", ctypes.c_wchar * 32),
    ]


class MSG(Structure):
    _fields_ = [
        ("hwnd", HWND),
        ("message", c_uint),
        ("wParam", WPARAM),
        ("lParam", LPARAM),
        ("time", DWORD),
        ("pt_x", LONG),
        ("pt_y", LONG),
    ]


MONITORENUMPROC = WINFUNCTYPE(c_int, HWND, HWND, POINTER(RECT), LPARAM)
WNDENUMPROC = WINFUNCTYPE(c_int, HWND, LPARAM)


# --------------------------------------------------------------------------
# DLL 句柄
# --------------------------------------------------------------------------

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    except OSError:  # pragma: no cover
        dwmapi = None
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
    except OSError:  # pragma: no cover
        shcore = None
else:  # pragma: no cover
    user32 = kernel32 = dwmapi = shcore = advapi32 = shell32 = None


def _bind() -> None:
    """声明各函数签名。签名写错会静默截断 64 位句柄，因此必须逐个写明。"""
    if not IS_WINDOWS:
        return

    user32.SetProcessDpiAwarenessContext.argtypes = [c_void_p]
    user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL

    user32.GetDpiForWindow.argtypes = [HWND]
    user32.GetDpiForWindow.restype = c_uint

    user32.EnumDisplayMonitors.argtypes = [c_void_p, c_void_p, MONITORENUMPROC, LPARAM]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL

    user32.GetMonitorInfoW.argtypes = [c_void_p, POINTER(MONITORINFOEXW)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL

    user32.MonitorFromWindow.argtypes = [HWND, DWORD]
    user32.MonitorFromWindow.restype = c_void_p

    user32.MonitorFromPoint.argtypes = [POINT, DWORD]
    user32.MonitorFromPoint.restype = c_void_p

    user32.GetWindowRect.argtypes = [HWND, POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL

    user32.GetClientRect.argtypes = [HWND, POINTER(RECT)]
    user32.GetClientRect.restype = wintypes.BOOL

    user32.ClientToScreen.argtypes = [HWND, POINTER(POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL

    user32.EnumWindows.argtypes = [WNDENUMPROC, LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    user32.IsWindowVisible.argtypes = [HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL

    user32.IsIconic.argtypes = [HWND]
    user32.IsIconic.restype = wintypes.BOOL

    user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, c_int]
    user32.GetWindowTextW.restype = c_int

    user32.GetWindowTextLengthW.argtypes = [HWND]
    user32.GetWindowTextLengthW.restype = c_int

    user32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, c_int]
    user32.GetClassNameW.restype = c_int

    user32.GetWindowLongW.argtypes = [HWND, c_int]
    user32.GetWindowLongW.restype = LONG

    user32.GetWindow.argtypes = [HWND, c_uint]
    user32.GetWindow.restype = HWND

    user32.GetWindowThreadProcessId.argtypes = [HWND, POINTER(DWORD)]
    user32.GetWindowThreadProcessId.restype = DWORD

    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = HWND

    user32.RegisterHotKey.argtypes = [HWND, c_int, c_uint, c_uint]
    user32.RegisterHotKey.restype = wintypes.BOOL

    user32.UnregisterHotKey.argtypes = [HWND, c_int]
    user32.UnregisterHotKey.restype = wintypes.BOOL

    user32.GetSystemMetrics.argtypes = [c_int]
    user32.GetSystemMetrics.restype = c_int

    user32.SetWindowPos.argtypes = [HWND, HWND, c_int, c_int, c_int, c_int, c_uint]
    user32.SetWindowPos.restype = wintypes.BOOL

    kernel32.OpenProcess.argtypes = [DWORD, wintypes.BOOL, DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE

    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, DWORD, wintypes.LPWSTR, POINTER(DWORD)
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.GetCurrentProcessId.argtypes = []
    kernel32.GetCurrentProcessId.restype = DWORD

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, DWORD, POINTER(wintypes.HANDLE)
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL

    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, c_int, c_void_p, DWORD, POINTER(DWORD)
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    shell32.IsUserAnAdmin.argtypes = []
    shell32.IsUserAnAdmin.restype = wintypes.BOOL

    if dwmapi is not None:
        dwmapi.DwmGetWindowAttribute.argtypes = [HWND, DWORD, c_void_p, DWORD]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    if shcore is not None:
        shcore.GetDpiForMonitor.argtypes = [c_void_p, c_int, POINTER(c_uint), POINTER(c_uint)]
        shcore.GetDpiForMonitor.restype = ctypes.c_long


_bind()


# --------------------------------------------------------------------------
# DPI
# --------------------------------------------------------------------------

def enable_per_monitor_dpi_awareness() -> bool:
    """让 Win32 返回物理像素。必须在创建 QApplication 之前调用。"""
    if not IS_WINDOWS:
        return False
    try:
        if user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return True
    except Exception:  # pragma: no cover - 旧系统无此导出
        pass
    try:
        return bool(shcore.SetProcessDpiAwareness(2))  # type: ignore[union-attr]
    except Exception:  # pragma: no cover
        try:
            return bool(user32.SetProcessDPIAware())
        except Exception:
            return False


def get_dpi_for_window(hwnd: int | None) -> int:
    if not IS_WINDOWS:
        return 96
    try:
        dpi = user32.GetDpiForWindow(hwnd or 0)
        return int(dpi) or 96
    except Exception:  # pragma: no cover
        return 96


def get_dpi_for_monitor(hmonitor: int) -> int:
    if not IS_WINDOWS or shcore is None:
        return 96
    try:
        x, y = c_uint(0), c_uint(0)
        if shcore.GetDpiForMonitor(c_void_p(hmonitor), 0, byref(x), byref(y)) == 0:
            return int(x.value) or 96
    except Exception:  # pragma: no cover
        pass
    return 96


# --------------------------------------------------------------------------
# 显示器
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MonitorInfo:
    handle: int
    device: str          # 例如 "\\\\.\\DISPLAY1"，与 Qt 的 QScreen.name() 一致
    left: int
    top: int
    right: int
    bottom: int
    work_left: int
    work_top: int
    work_right: int
    work_bottom: int
    primary: bool
    dpi: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def scale(self) -> float:
        return self.dpi / 96.0


def enum_monitors() -> list[MonitorInfo]:
    """枚举所有显示器，返回物理像素矩形。"""
    if not IS_WINDOWS:
        return []
    result: list[MonitorInfo] = []

    def _cb(hmonitor, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmonitor, byref(info)):
            result.append(
                MonitorInfo(
                    handle=int(hmonitor) if hmonitor else 0,
                    device=info.szDevice,
                    left=info.rcMonitor.left,
                    top=info.rcMonitor.top,
                    right=info.rcMonitor.right,
                    bottom=info.rcMonitor.bottom,
                    work_left=info.rcWork.left,
                    work_top=info.rcWork.top,
                    work_right=info.rcWork.right,
                    work_bottom=info.rcWork.bottom,
                    primary=bool(info.dwFlags & 1),
                    dpi=get_dpi_for_monitor(int(hmonitor) if hmonitor else 0),
                )
            )
        return 1

    try:
        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
    except Exception as exc:  # pragma: no cover
        log.warning("枚举显示器失败：%s", exc)
    result.sort(key=lambda m: (m.left, m.top))
    return result


def primary_monitor() -> MonitorInfo | None:
    for mon in enum_monitors():
        if mon.primary:
            return mon
    mons = enum_monitors()
    return mons[0] if mons else None


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """整个虚拟桌面的物理像素范围 (left, top, right, bottom)。"""
    if not IS_WINDOWS:
        return (0, 0, 0, 0)
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return (left, top, left + width, top + height)


def monitor_from_window(hwnd: int) -> int:
    if not IS_WINDOWS:
        return 0
    return int(user32.MonitorFromWindow(HWND(hwnd), MONITOR_DEFAULTTONEAREST) or 0)


def monitor_containing_point(x: int, y: int) -> int:
    if not IS_WINDOWS:
        return 0
    return int(user32.MonitorFromPoint(POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST) or 0)


# --------------------------------------------------------------------------
# 窗口
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    process_name: str
    left: int
    top: int
    right: int
    bottom: int
    client_left: int
    client_top: int
    client_width: int
    client_height: int
    minimized: bool
    hmonitor: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def identity(self) -> str:
        """跨重启稳定的标识：进程名 + 窗口类名 + 标题。句柄不可复用。"""
        return f"{self.process_name}|{self.class_name}|{self.title}"


def window_display_name(info: WindowInfo) -> str:
    name = info.process_name or f"PID {info.pid}"
    if info.title:
        title = info.title if len(info.title) <= 48 else info.title[:45] + "…"
        return f"{name} — {title}"
    return name


def _is_real_window(hwnd: int) -> bool:
    if not user32.IsWindowVisible(HWND(hwnd)):
        return False
    style = user32.GetWindowLongW(HWND(hwnd), GWL_STYLE)
    ex_style = user32.GetWindowLongW(HWND(hwnd), GWL_EXSTYLE)
    if style & WS_CHILD:
        return False
    if ex_style & WS_EX_TOOLWINDOW:
        return False
    if ex_style & WS_EX_TRANSPARENT:
        return False
    # 有 owner 的一般是对话框/弹出面板，不作为采集源首选，但仍保留（部分游戏用 owner 窗口）。
    return True


def _process_name(pid: int) -> str:
    if not IS_WINDOWS or not pid:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = DWORD(512)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, byref(size)):
            from pathlib import Path

            return Path(buffer.value).name
    except Exception:  # pragma: no cover
        return ""
    finally:
        kernel32.CloseHandle(handle)
    return ""


def enum_windows(include_minimized: bool = True) -> list[WindowInfo]:
    """枚举可作为采集源的真实窗口（物理像素）。"""
    if not IS_WINDOWS:
        return []
    result: list[WindowInfo] = []
    own_pid = _own_pid()

    def _cb(hwnd, _lparam):
        try:
            if not _is_real_window(hwnd):
                return 1
            length = user32.GetWindowTextLengthW(HWND(hwnd))
            if length <= 0:
                return 1
            buf = ctypes.create_unicode_buffer(length + 2)
            user32.GetWindowTextW(HWND(hwnd), buf, length + 2)
            title = buf.value.strip()
            if not title:
                return 1

            pid = DWORD(0)
            user32.GetWindowThreadProcessId(HWND(hwnd), byref(pid))
            if pid.value == own_pid:
                return 1  # 不把工具自己的窗口列为采集源

            minimized = bool(user32.IsIconic(HWND(hwnd)))
            if minimized and not include_minimized:
                return 1

            rect = RECT()
            if not user32.GetWindowRect(HWND(hwnd), byref(rect)):
                return 1
            left, top, right, bottom = _visible_bounds(hwnd, rect)
            if right - left < 64 or bottom - top < 64:
                return 1

            crect = RECT()
            if not user32.GetClientRect(HWND(hwnd), byref(crect)):
                return 1
            origin = POINT(0, 0)
            user32.ClientToScreen(HWND(hwnd), byref(origin))

            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(HWND(hwnd), cls, 256)

            result.append(
                WindowInfo(
                    hwnd=int(hwnd) if hwnd else 0,
                    title=title,
                    class_name=cls.value,
                    pid=int(pid.value),
                    process_name=_process_name(int(pid.value)),
                    left=left,
                    top=top,
                    right=right,
                    bottom=bottom,
                    client_left=origin.x,
                    client_top=origin.y,
                    client_width=crect.right - crect.left,
                    client_height=crect.bottom - crect.top,
                    minimized=minimized,
                    hmonitor=monitor_from_window(int(hwnd) if hwnd else 0),
                )
            )
        except Exception as exc:  # pragma: no cover
            log.debug("枚举窗口时跳过一项：%s", exc)
        return 1

    try:
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception as exc:  # pragma: no cover
        log.warning("枚举窗口失败：%s", exc)

    result.sort(key=lambda w: (w.minimized, w.process_name.lower(), w.title.lower()))
    return result


def _own_pid() -> int:
    if not IS_WINDOWS:
        return 0
    try:
        return int(kernel32.GetCurrentProcessId())
    except Exception:  # pragma: no cover
        return 0


def _visible_bounds(hwnd: int, fallback: RECT) -> tuple[int, int, int, int]:
    """优先使用 DWM 的扩展边框，它排除掉了不可见的调整大小边框。

    否则窗口截图会比实际可见范围大出一圈，导致客户区偏移算错。
    """
    if dwmapi is not None:
        rect = RECT()
        try:
            hr = dwmapi.DwmGetWindowAttribute(
                HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, byref(rect), sizeof(RECT)
            )
            if hr == 0 and rect.right > rect.left and rect.bottom > rect.top:
                return (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:  # pragma: no cover
            pass
    return (fallback.left, fallback.top, fallback.right, fallback.bottom)


def refresh_window(hwnd: int) -> WindowInfo | None:
    """重新读取某个窗口的当前几何；窗口已销毁时返回 None。"""
    if not IS_WINDOWS or not hwnd:
        return None
    if not user32.IsWindow(HWND(hwnd)):
        return None
    for info in enum_windows():
        if info.hwnd == hwnd:
            return info
    return None


def foreground_window() -> int:
    if not IS_WINDOWS:
        return 0
    return int(user32.GetForegroundWindow() or 0)


def is_window(hwnd: int) -> bool:
    if not IS_WINDOWS or not hwnd:
        return False
    return bool(user32.IsWindow(HWND(hwnd)))


def client_rect_on_screen(hwnd: int) -> tuple[int, int, int, int]:
    """客户区在屏幕上的物理像素矩形 (left, top, right, bottom)。"""
    rect = RECT()
    if not user32.GetClientRect(HWND(hwnd), byref(rect)):
        raise OSError("GetClientRect 失败")
    origin = POINT(0, 0)
    if not user32.ClientToScreen(HWND(hwnd), byref(origin)):
        raise OSError("ClientToScreen 失败")
    return (origin.x, origin.y, origin.x + (rect.right - rect.left), origin.y + (rect.bottom - rect.top))


# --------------------------------------------------------------------------
# 进程提权状态（用于解释 WGC「Failed to convert item」）
# --------------------------------------------------------------------------

def is_self_elevated() -> bool:
    """本进程是否以管理员令牌运行。"""
    if not IS_WINDOWS:
        return False
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover
        return False


def is_process_elevated(pid: int) -> bool:
    """目标进程是否以管理员令牌运行。

    ``PROCESS_QUERY_LIMITED_INFORMATION`` 跨提权也能打开，所以这里对
    提权过的进程同样查得到。查不到（句柄/令牌失败）时返回 False，
    按「无权限冲突」处理——宁可少提示，也不能误报。
    """
    if not IS_WINDOWS or pid <= 0:
        return False
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, byref(token)):
            return False
        elevation = DWORD(0)
        returned = DWORD(0)
        if not advapi32.GetTokenInformation(
            token, TOKEN_ELEVATION, byref(elevation),
            sizeof(DWORD), byref(returned),
        ):
            return False
        return bool(elevation.value)
    except Exception:  # pragma: no cover
        log.exception("查询进程提权状态失败 pid=%s", pid)
        return False
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(handle)


def window_elevation_conflict(hwnd: int) -> bool:
    """目标窗口的进程提权而本进程未提权——Windows 禁止这种跨权限捕获。"""
    if not IS_WINDOWS or not hwnd:
        return False
    pid = DWORD(0)
    user32.GetWindowThreadProcessId(HWND(hwnd), byref(pid))
    if not pid.value:
        return False
    if is_self_elevated():
        return False
    return is_process_elevated(pid.value)
