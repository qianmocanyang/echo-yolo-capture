"""全局快捷键：注册、录制、去抖、冲突处理。

方案 §4.2 的每一条都在代码里有对应的落点：

| 方案要求 | 实现 |
| --- | --- |
| 优先使用 `RegisterHotKey`，通过原生事件过滤器处理 `WM_HOTKEY` | :class:`HotkeyEventFilter` |
| 使用无重复触发标志，并加入应用层去抖 | 注册时带 `MOD_NOREPEAT`，再加 :data:`DEBOUNCE_SECONDS` |
| 注册失败提示"不可用或已被占用"，保留原有效绑定 | :meth:`HotkeyManager.bind` 先注册新 id 成功后再释放旧 id |
| 后台/托盘时保留热键，退出时释放 | 热键常驻，:meth:`release_all` 只在退出时调用 |
| 录制热键期间临时抑制采集动作 | :meth:`HotkeyManager.suspend` / :meth:`resume` |
| 截图命令不得激活主窗口或弹保存框 | 触发只发信号，不做任何窗口操作 |

为什么要用"新 id 先注册、成功后再释放旧 id"这个顺序：
`RegisterHotKey` 是按 (窗口, id) 记账的，同一个组合键被自己占用时再注册也会失败。
所以更换绑定时绝不能先 `Unregister` 旧键——一旦新键注册失败（被别的程序占了），
用户就会既没了新键也丢了旧键。先占新 id，成功后再退旧 id，任何一步失败都不影响现状。
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

from . import winapi
from .logging_setup import get_logger

log = get_logger("hotkeys")

# 应用层去抖。MOD_NOREPEAT 已经挡住了长按重复，这里挡的是连续快速敲击。
# 250 ms 对"打一枪截一张"的节奏足够快，又能避免误触连拍。
DEBOUNCE_SECONDS = 0.25

# 自用 id 起始值。避开 0x0000–0xBFFF 这些常被输入法、显卡驱动、录屏软件占用的区间。
ID_BASE = 0xE000

ACTIONS: dict[str, str] = {
    "capture_once": "单张截图",
    "toggle_auto_capture": "开始 / 暂停定时采集",
    "toggle_window": "显示 / 收起主窗口",
}

_VK_NAMED: dict[str, int] = {
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "RETURN": 0x0D,
    "ESC": 0x1B, "ESCAPE": 0x1B, "SPACE": 0x20,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22, "END": 0x23, "HOME": 0x24,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "INSERT": 0x2D, "DELETE": 0x2E, "DEL": 0x2E,
    "PRINTSCREEN": 0x2C, "CAPSLOCK": 0x14, "NUMLOCK": 0x90, "SCROLLLOCK": 0x91,
    "PAUSE": 0x13, "APPS": 0x5D,
    "`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
    "NUMPAD0": 0x60, "NUMPAD1": 0x61, "NUMPAD2": 0x62, "NUMPAD3": 0x63,
    "NUMPAD4": 0x64, "NUMPAD5": 0x65, "NUMPAD6": 0x66, "NUMPAD7": 0x67,
    "NUMPAD8": 0x68, "NUMPAD9": 0x69,
    "NUMPADMULTIPLY": 0x6A, "NUMPADADD": 0x6B, "NUMPADSUBTRACT": 0x6D,
    "NUMPADDECIMAL": 0x6E, "NUMPADDIVIDE": 0x6F,
}

_VK_TO_NAME: dict[int, str] = {}
for _name, _code in _VK_NAMED.items():
    _VK_TO_NAME.setdefault(_code, _name)

for _index in range(1, 25):
    # F1..F24 是连续码，但对每个键都显式建表，方便查询反查名
    _code = 0x6F + _index
    _VK_NAMED[f"F{_index}"] = _code
    _VK_TO_NAME[_code] = f"F{_index}"

for _letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _VK_NAMED[_letter] = ord(_letter)
    _VK_TO_NAME[ord(_letter)] = _letter

for _digit in "0123456789":
    _VK_NAMED[_digit] = ord(_digit)
    _VK_TO_NAME[ord(_digit)] = _digit

# 修饰键自身的 VK，录制时用来过滤"只按了 Ctrl"这种无效输入
PURE_MODIFIER_VKS = {
    0x10, 0x11, 0x12,          # Shift / Ctrl / Alt
    0x5B, 0x5C,                # LWin / RWin
    0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5,  # 左右 Shift / Ctrl / Alt
}

# 这些键作为单键（无修饰）注册会被系统拒绝或意义不明，录制时就拦下来
FORBIDDEN_SOLO = {"TAB", "ESC", "ESCAPE", "ENTER", "RETURN", "BACKSPACE", "DELETE", "DEL"}


def parse_combo(text: str) -> tuple[int, int, str]:
    """解析 "Ctrl+Alt+E" 这类组合键。

    返回 ``(修饰键位掩码, 虚拟键码, 错误说明)``。成功时错误说明为空串。
    """
    if not text or not text.strip():
        return 0, 0, "快捷键为空"

    normalized = text.replace("＋", "+").replace("－", "-")
    parts = [part.strip() for part in normalized.split("+") if part.strip()]
    if not parts:
        return 0, 0, "快捷键为空"

    mods = 0
    key_token = ""
    for part in parts:
        upper = part.upper()
        if upper in {"CTRL", "CONTROL"}:
            mods |= winapi.MOD_CONTROL
        elif upper == "ALT":
            mods |= winapi.MOD_ALT
        elif upper == "SHIFT":
            mods |= winapi.MOD_SHIFT
        elif upper in {"WIN", "META", "SUPER"}:
            mods |= winapi.MOD_WIN
        else:
            if key_token:
                return 0, 0, f"只能包含一个主键，收到「{key_token}」和「{part}」"
            key_token = upper

    if not key_token:
        return 0, 0, "快捷键必须包含一个主键，不能只有修饰键"

    vk = _VK_NAMED.get(key_token)
    if vk is None and len(key_token) == 1:
        vk = ord(key_token)
    if vk is None:
        return 0, 0, f"无法识别的按键「{key_token}」"

    if mods == 0 and key_token in FORBIDDEN_SOLO:
        return 0, 0, f"「{key_token}」不能单独作为快捷键，请配合 Ctrl / Alt / Shift"

    return mods, vk, ""


def format_combo(mods: int, vk: int) -> str:
    parts: list[str] = []
    if mods & winapi.MOD_CONTROL:
        parts.append("Ctrl")
    if mods & winapi.MOD_ALT:
        parts.append("Alt")
    if mods & winapi.MOD_SHIFT:
        parts.append("Shift")
    if mods & winapi.MOD_WIN:
        parts.append("Win")
    name = _VK_TO_NAME.get(vk)
    if name is None:
        if 0x30 <= vk <= 0x5A:
            name = chr(vk)
        else:
            name = f"VK({vk:#04x})"
    parts.append(name)
    return "+".join(parts)


def normalize_combo(text: str) -> str:
    """把用户输入的组合键规范化成统一写法；解析不了时原样返回。"""
    mods, vk, error = parse_combo(text)
    if error:
        return (text or "").strip()
    return format_combo(mods, vk)


def combo_from_qt(modifiers, key: int) -> tuple[str, str]:
    """把 Qt 的按键事件转成组合键字符串。返回 ``(组合键, 错误说明)``。"""
    from PySide6.QtCore import Qt

    if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta,
               Qt.Key_AltGr, Qt.Key_CapsLock, Qt.Key_NumLock, Qt.Key_ScrollLock):
        return "", ""  # 只按了修饰键，继续等

    mods = 0
    if modifiers & Qt.ControlModifier:
        mods |= winapi.MOD_CONTROL
    if modifiers & Qt.AltModifier:
        mods |= winapi.MOD_ALT
    if modifiers & Qt.ShiftModifier:
        mods |= winapi.MOD_SHIFT
    if modifiers & Qt.MetaModifier:
        mods |= winapi.MOD_WIN

    vk = qt_key_to_vk(int(key))
    if vk is None:
        return "", "该按键暂不支持绑定为快捷键"

    token = _VK_TO_NAME.get(vk, "")
    if mods == 0 and token in FORBIDDEN_SOLO:
        return "", f"「{token}」不能单独作为快捷键，请配合 Ctrl / Alt / Shift"

    return format_combo(mods, vk), ""


def qt_key_to_vk(key: int) -> int | None:
    """Qt::Key → Windows 虚拟键码。"""
    from PySide6.QtCore import Qt

    if Qt.Key_F1 <= key <= Qt.Key_F24:
        return 0x6F + (key - Qt.Key_F1) + 1
    if Qt.Key_A <= key <= Qt.Key_Z:
        return ord("A") + (key - Qt.Key_A)
    if Qt.Key_0 <= key <= Qt.Key_9:
        return ord("0") + (key - Qt.Key_0)
    mapping = {
        Qt.Key_Space: 0x20, Qt.Key_Tab: 0x09, Qt.Key_Return: 0x0D,
        Qt.Key_Enter: 0x0D, Qt.Key_Backspace: 0x08, Qt.Key_Delete: 0x2E,
        Qt.Key_Insert: 0x2D, Qt.Key_Home: 0x24, Qt.Key_End: 0x23,
        Qt.Key_PageUp: 0x21, Qt.Key_PageDown: 0x22,
        Qt.Key_Left: 0x25, Qt.Key_Up: 0x26, Qt.Key_Right: 0x27, Qt.Key_Down: 0x28,
        Qt.Key_Escape: 0x1B,
        Qt.Key_Print: 0x2C, Qt.Key_Pause: 0x13,
        Qt.Key_QuoteLeft: 0xC0, Qt.Key_Minus: 0xBD, Qt.Key_Equal: 0xBB,
        Qt.Key_BracketLeft: 0xDB, Qt.Key_BracketRight: 0xDD, Qt.Key_Backslash: 0xDC,
        Qt.Key_Semicolon: 0xBA, Qt.Key_Apostrophe: 0xDE, Qt.Key_Comma: 0xBC,
        Qt.Key_Period: 0xBE, Qt.Key_Slash: 0xBF,
    }
    return mapping.get(key)


# --------------------------------------------------------------------------
# 原生事件过滤
# --------------------------------------------------------------------------

class HotkeyEventFilter(QAbstractNativeEventFilter):
    """抓 ``WM_HOTKEY``。

    方案 §4.2 要求"通过应用原生事件过滤器处理 WM_HOTKEY"。
    ``RegisterHotKey(None, ...)`` 会把消息投递到注册线程的消息队列，
    也就是 Qt 的主线程，所以这个过滤器装在 QApplication 上是正确的。
    """

    def __init__(self, on_hotkey: Callable[[int], None]) -> None:
        super().__init__()
        self._on_hotkey = on_hotkey

    def nativeEventFilter(self, event_type, message):  # noqa: N802 (Qt 命名)
        if not winapi.IS_WINDOWS:
            return False, 0
        try:
            msg = ctypes.cast(int(message), ctypes.POINTER(winapi.MSG)).contents
        except (TypeError, ValueError):
            return False, 0
        if msg.message == winapi.WM_HOTKEY:
            try:
                self._on_hotkey(int(msg.wParam))
            except Exception as exc:  # 回调里抛异常会污染 Qt 事件循环
                log.warning("处理热键消息时出错：%s", exc)
            return True, 0
        # 返回 (False, 0) 让 Qt 继续正常派发消息；否则会吃掉按键、输入框打不了字
        return False, 0


# --------------------------------------------------------------------------
# 热键管理
# --------------------------------------------------------------------------

@dataclass(slots=True)
class _Binding:
    action: str
    combo: str
    mods: int
    vk: int
    hotkey_id: int


class HotkeyManager(QObject):
    """全局热键的注册与派发。只在主线程使用。"""

    triggered = Signal(str)              # 参数是 action
    binding_changed = Signal(str, str)   # action, combo
    binding_failed = Signal(str, str, str)  # action, 尝试的组合键, 原因

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._bindings: dict[str, _Binding] = {}
        self._id_to_action: dict[int, str] = {}
        self._next_id = ID_BASE
        self._last_fire: dict[str, float] = {}
        self._suspended = False
        self._filter: HotkeyEventFilter | None = None
        self._debounce = DEBOUNCE_SECONDS

    # ---- 安装 / 卸载 ----------------------------------------------------
    def install(self) -> None:
        """把原生事件过滤器挂到应用上。重复调用是幂等的。"""
        from PySide6.QtCore import QCoreApplication

        app = QCoreApplication.instance()
        if app is None:
            raise RuntimeError("QApplication 尚未创建，无法安装热键过滤器")
        if self._filter is None:
            self._filter = HotkeyEventFilter(self._handle_id)
            app.installNativeEventFilter(self._filter)

    def uninstall(self) -> None:
        from PySide6.QtCore import QCoreApplication

        app = QCoreApplication.instance()
        if app is not None and self._filter is not None:
            app.removeNativeEventFilter(self._filter)
        self._filter = None
        self.release_all()

    # ---- 录制期间的抑制 -------------------------------------------------
    def suspend(self) -> None:
        """进入热键录制状态：暂时不派发采集动作（方案 §4.2）。

        刻意**不**注销系统热键：注销/重注册之间用户按下的键会丢失，
        而且某些组合在重注册时可能被别人抢走。
        """
        self._suspended = True

    def resume(self) -> None:
        self._suspended = False
        self._last_fire.clear()

    @property
    def suspended(self) -> bool:
        return self._suspended

    # ---- 绑定 -----------------------------------------------------------
    def load(self, mapping: dict[str, str]) -> list[str]:
        """启动时按配置批量注册。返回失败说明列表（空列表表示全部成功）。"""
        problems: list[str] = []
        for action, combo in mapping.items():
            ok, message = self.bind(action, combo)
            if not ok:
                label = ACTIONS.get(action, action)
                problems.append(f"{label}：{message}")
        return problems

    def bind(self, action: str, combo: str) -> tuple[bool, str]:
        """注册或更换一个绑定的组合键。

        顺序是「先用新的系统 id 注册 → 成功后再释放旧 id」，
        所以失败时原绑定完好无损（方案 §4.2）。
        """
        mods, vk, error = parse_combo(combo)
        if error:
            self.binding_failed.emit(action, combo, error)
            return False, error

        current = self._bindings.get(action)
        if current is not None and current.combo == format_combo(mods, vk):
            return True, ""  # 没变化，不必折腾系统

        ok, message = self._register_system(action, format_combo(mods, vk), mods, vk)
        if not ok:
            self.binding_failed.emit(action, combo, message)
            return False, message

        # 到这里新键已经在手，安全地退掉旧的
        if current is not None:
            self._unregister_system(current.hotkey_id)

        self.binding_changed.emit(action, self._bindings[action].combo)
        return True, ""

    def unbind(self, action: str) -> None:
        binding = self._bindings.pop(action, None)
        if binding is None:
            return
        self._unregister_system(binding.hotkey_id)
        self.binding_changed.emit(action, "")
        log.info("已解除热键绑定：%s", ACTIONS.get(action, action))

    def release_all(self) -> None:
        for action in list(self._bindings):
            binding = self._bindings.pop(action)
            self._unregister_system(binding.hotkey_id)
        log.info("已释放全部热键")

    def combo_for(self, action: str) -> str:
        binding = self._bindings.get(action)
        return binding.combo if binding else ""

    def is_bound(self, action: str) -> bool:
        return action in self._bindings

    def bindings(self) -> dict[str, str]:
        return {action: b.combo for action, b in self._bindings.items()}

    def conflict_hint(self, combo: str) -> str:
        """检查一个组合键是否已被本工具的其它动作占用。"""
        target = format_combo(*parse_combo(combo)[:2]) if not parse_combo(combo)[2] else ""
        if not target:
            return ""
        for action, binding in self._bindings.items():
            if binding.combo == target:
                return f"该组合键已分配给「{ACTIONS.get(action, action)}」"
        return ""

    # ---- 内部 -----------------------------------------------------------
    def _register_system(self, action: str, combo: str, mods: int, vk: int) -> tuple[bool, str]:
        if not winapi.IS_WINDOWS:
            return False, "当前系统不支持全局热键"

        hotkey_id = self._next_id
        self._next_id += 1

        # MOD_NOREPEAT：长按不重复触发（方案 §4.2 / 验收 T05）
        flags = mods | winapi.MOD_NOREPEAT
        result = winapi.user32.RegisterHotKey(None, hotkey_id, flags, vk)
        if not result:
            error_code = ctypes.get_last_error()
            if error_code == winapi.ERROR_HOTKEY_ALREADY_REGISTERED:
                message = f"「{combo}」已被其他程序占用"
            elif error_code == 0:
                message = f"「{combo}」不可用，可能已被系统或其他程序占用"
            else:
                message = f"「{combo}」注册失败（错误码 {error_code}）"
            log.warning("热键注册失败：%s action=%s", message, action)
            return False, message

        self._bindings[action] = _Binding(
            action=action, combo=combo, mods=mods, vk=vk, hotkey_id=hotkey_id
        )
        self._id_to_action[hotkey_id] = action
        log.info("热键已注册：%s → %s（id=%#x）", ACTIONS.get(action, action), combo, hotkey_id)
        return True, ""

    def _unregister_system(self, hotkey_id: int) -> None:
        if not winapi.IS_WINDOWS:
            return
        try:
            winapi.user32.UnregisterHotKey(None, hotkey_id)
        except Exception as exc:  # pragma: no cover
            log.warning("注销热键失败 id=%#x：%s", hotkey_id, exc)
        self._id_to_action.pop(hotkey_id, None)

    def _handle_id(self, hotkey_id: int) -> None:
        if self._suspended:
            return
        action = self._id_to_action.get(hotkey_id)
        if action is None:
            return

        # 应用层去抖
        now = time.monotonic()
        last = self._last_fire.get(action, 0.0)
        if now - last < self._debounce:
            return
        self._last_fire[action] = now

        log.debug("热键触发：%s", ACTIONS.get(action, action))
        self.triggered.emit(action)

    def set_debounce(self, seconds: float) -> None:
        self._debounce = max(0.0, float(seconds))
