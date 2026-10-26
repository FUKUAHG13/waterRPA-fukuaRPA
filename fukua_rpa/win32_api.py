"""Typed Win32 declarations and hotkey helpers used across the application."""

import ctypes
import logging
from ctypes import wintypes
from typing import cast

from PySide6.QtCore import Qt

logger = logging.getLogger(__name__)

# pyright: reportAttributeAccessIssue=false

GetAsyncKeyState = ctypes.windll.user32.GetAsyncKeyState
# Declare the signature so ctypes marshals arguments/return correctly instead
# of assuming the platform default (which happens to work but is unspecified).
GetAsyncKeyState.argtypes = [ctypes.c_int]
GetAsyncKeyState.restype = ctypes.c_short
try:
    GetCurrentProcessorNumber = ctypes.windll.kernel32.GetCurrentProcessorNumber
    GetCurrentProcessorNumber.restype = ctypes.c_ulong
    HAS_KERNEL_CPU = True
except Exception:
    GetCurrentProcessorNumber = None
    HAS_KERNEL_CPU = False

VK_MAP = {
    0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x10: "shift", 0x11: "ctrl",
    0x12: "alt", 0x14: "capslock", 0x1B: "esc", 0x20: "space", 0x21: "pageup",
    0x22: "pagedown", 0x23: "end", 0x24: "home", 0x25: "left", 0x26: "up",
    0x27: "right", 0x28: "down", 0x2C: "printscreen", 0x2D: "insert", 0x2E: "delete",
}
for index in range(65, 91):
    VK_MAP[index] = chr(index).lower()
for index in range(48, 58):
    VK_MAP[index] = chr(index)
for index in range(112, 124):
    VK_MAP[index] = f"f{index - 111}"
# INVARIANT: 小键盘必须用 "num0".."num9"，与 HOTKEY_NAME_TO_VK 保持一致。
# 若映射为 "0".."9"，hotkey_text_from_pressed_vks 经 VK_MAP 取名后走
# parse_hotkey_text 会得到 vk 0x30（顶排数字）而非 0x60：录入时物理键与文本
# 不符，已配置的裸键映射 "num0" 在 Hook 匹配中永不命中（映射模式 Hook 是裸键
# 的唯一后端）——属静默失效（审查 FR-006）。顶排数字仍由 0x30-0x39 映射为
# "0".."9"，既有配置解析不受影响。
for index in range(0x60, 0x6A):
    VK_MAP[index] = f"num{index - 0x60}"
VK_MAP.update({
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/", 0xC0: "`",
    0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
})

# Named virtual-key codes used by the recording hotkeys (code review L-23):
# F8 toggles recording, Escape cancels it.
VK_ESCAPE = 0x1B
VK_F8 = 0x77

HOTKEY_NAME_TO_VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "capslock": 0x14, "esc": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C,
    "insert": 0x2D, "delete": 0x2E, ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD,
    ".": 0xBE, "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
for index in range(65, 91):
    HOTKEY_NAME_TO_VK[chr(index).lower()] = index
for index in range(48, 58):
    HOTKEY_NAME_TO_VK[chr(index)] = index
for index in range(112, 124):
    HOTKEY_NAME_TO_VK[f"f{index - 111}"] = index
for index in range(0x60, 0x6A):
    HOTKEY_NAME_TO_VK[f"num{index - 0x60}"] = index

HOTKEY_ALIAS = {
    "control": "ctrl", "ctl": "ctrl", "cmd": "win", "command": "win", "meta": "win",
    "windows": "win", "escape": "esc", "return": "enter", "del": "delete",
    "pgup": "pageup", "pgdn": "pagedown", "page down": "pagedown", "page up": "pageup",
    "prtsc": "printscreen", "print": "printscreen", "ins": "insert", "bksp": "backspace",
}
MOD_NAME_TO_MASK = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NAME_TO_VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}
MOD_NAME_TO_VKS = {
    "ctrl": frozenset((0x11, 0xA2, 0xA3)),
    "alt": frozenset((0x12, 0xA4, 0xA5)),
    "shift": frozenset((0x10, 0xA0, 0xA1)),
    "win": frozenset((0x5B, 0x5C)),
}
MODIFIER_VKS = frozenset().union(*MOD_NAME_TO_VKS.values())
MOD_DISPLAY = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
MOD_ORDER = ("ctrl", "alt", "shift", "win")
SAFE_BARE_GLOBAL_KEYS = {f"f{index}" for index in range(1, 13)} | {
    "printscreen", "insert", "delete", "home", "end", "pageup", "pagedown"
}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

HWND_TOPMOST = wintypes.HWND(-1)
HWND_NOTOPMOST = wintypes.HWND(-2)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_NOOWNERZORDER = 0x0200
WM_HOTKEY = 0x0312
WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
WM_SIZING = 0x0214
WM_MOVING = 0x0216
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_RBUTTONDBLCLK = 0x0206
PM_NOREMOVE = 0x0000
MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
GA_ROOT = 2
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
HOTKEY_ID_START = 0x5741
HOTKEY_ID_STOP = 0x5742
HOTKEY_ID_AUTO_CLICKER_START = 0x5743
HOTKEY_ID_AUTO_CLICKER_STOP = 0x5744
HOTKEY_ID_MAPPING_BASE = 0x5800
HOTKEY_ID_PROFILE_START_BASE = 0x5900
HOTKEY_ID_PROFILE_STOP_BASE = 0x5A00

# GetClassLongPtr(GCL_STYLE) 索引与类风格标志：用于判断目标窗口是否声明
# CS_DBLCLKS（系统会自动把连续两次单击合成为 WM_*BUTTONDBLCLK）。
GCL_STYLE = -26
CS_DBLCLKS = 0x0008


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, ctypes.c_void_p)


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HANDLE,
    wintypes.HDC,
    ctypes.POINTER(RECT),
    wintypes.LPARAM,
)


def _safe_configure(func, argtypes, restype, name) -> None:
    """Configure argtypes/restype for a single Win32 function.

    Failures are logged but do not prevent other functions from being configured
    (e.g. security software may hook individual function attributes).

    FR-20261008-402 / ADV-20261008-402: the failure notice used to be written to
    ``sys.stderr``. In a frozen windowed build ``sys.stderr`` is ``None``, so the
    handler itself raised ``AttributeError`` and escaped ``_configure_function_signatures``
    — turning one hooked function into a module-import failure that takes the whole
    application down. Log through the module logger instead, which never raises.

    DOC-20261009-001: these records are NOT routed through the project log policy
    (``fukua_rpa.log_policy``). They stay on the stdlib module logger, which has no
    handler configured here, so they are neither filtered nor categorised by the
    project policy; their visibility in a frozen windowed build (``sys.stderr`` is
    ``None``) is pending real-machine verification
    (REVIEW-20261009-001 ``RM-20261009-001``).
    """
    try:
        func.argtypes = argtypes
        func.restype = restype
    except Exception as error:  # noqa: BLE001
        logger.warning("Win32 signature setup failed for %s: %s", name, error)


def _configure_function_signatures() -> None:
    _safe_configure(user32.SetWindowPos, [
        wintypes.HWND, wintypes.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    ], wintypes.BOOL, "SetWindowPos")
    _safe_configure(user32.RegisterHotKey,
                    [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT],
                    wintypes.BOOL, "RegisterHotKey")
    _safe_configure(user32.UnregisterHotKey,
                    [wintypes.HWND, ctypes.c_int],
                    wintypes.BOOL, "UnregisterHotKey")
    _safe_configure(user32.WindowFromPoint, [POINT],
                    wintypes.HWND, "WindowFromPoint")
    _safe_configure(user32.ScreenToClient,
                    [wintypes.HWND, ctypes.POINTER(POINT)],
                    wintypes.BOOL, "ScreenToClient")
    _safe_configure(user32.PostMessageW,
                    [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
                    wintypes.BOOL, "PostMessageW")
    _safe_configure(user32.GetAncestor, [wintypes.HWND, wintypes.UINT],
                    wintypes.HWND, "GetAncestor")
    _safe_configure(user32.GetParent, [wintypes.HWND],
                    wintypes.HWND, "GetParent")
    _safe_configure(user32.GetWindowRect,
                    [wintypes.HWND, ctypes.POINTER(RECT)],
                    wintypes.BOOL, "GetWindowRect")
    _safe_configure(user32.GetClientRect,
                    [wintypes.HWND, ctypes.POINTER(RECT)],
                    wintypes.BOOL, "GetClientRect")
    _safe_configure(user32.IsWindowVisible, [wintypes.HWND],
                    wintypes.BOOL, "IsWindowVisible")
    _safe_configure(user32.IsWindowEnabled, [wintypes.HWND],
                    wintypes.BOOL, "IsWindowEnabled")
    _safe_configure(user32.IsWindow, [wintypes.HWND],
                    wintypes.BOOL, "IsWindow")
    _safe_configure(user32.ClientToScreen,
                    [wintypes.HWND, ctypes.POINTER(POINT)],
                    wintypes.BOOL, "ClientToScreen")
    _safe_configure(user32.GetWindowThreadProcessId,
                    [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                    wintypes.DWORD, "GetWindowThreadProcessId")
    _safe_configure(user32.GetClassNameW,
                    [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int],
                    ctypes.c_int, "GetClassNameW")
    _safe_configure(user32.GetWindowTextLengthW, [wintypes.HWND],
                    ctypes.c_int, "GetWindowTextLengthW")
    _safe_configure(user32.GetWindowTextW,
                    [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int],
                    ctypes.c_int, "GetWindowTextW")
    _safe_configure(user32.GetClassLongPtrW,
                    [wintypes.HWND, ctypes.c_int],
                    ctypes.c_void_p, "GetClassLongPtrW")
    _safe_configure(user32.GetDlgCtrlID, [wintypes.HWND],
                    ctypes.c_int, "GetDlgCtrlID")
    _safe_configure(user32.GetDlgItem, [wintypes.HWND, ctypes.c_int],
                    wintypes.HWND, "GetDlgItem")
    _safe_configure(user32.EnumWindows, [WNDENUMPROC, wintypes.LPARAM],
                    wintypes.BOOL, "EnumWindows")
    _safe_configure(user32.ShowWindow, [wintypes.HWND, ctypes.c_int],
                    wintypes.BOOL, "ShowWindow")
    _safe_configure(user32.SetForegroundWindow, [wintypes.HWND],
                    wintypes.BOOL, "SetForegroundWindow")
    # SAFETY: HWND is a 64-bit handle on Win64. Without this restype ctypes
    # truncates the return to 32 bits, corrupting the handle passed to
    # get_window_title / window checks (see worker.py / uia_smoke.py callers).
    _safe_configure(user32.GetForegroundWindow, [],
                    wintypes.HWND, "GetForegroundWindow")
    _safe_configure(user32.EnumChildWindows,
                    [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM],
                    wintypes.BOOL, "EnumChildWindows")
    _safe_configure(user32.SetWindowsHookExW,
                    [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD],
                    ctypes.c_void_p, "SetWindowsHookExW")
    _safe_configure(user32.UnhookWindowsHookEx, [ctypes.c_void_p],
                    wintypes.BOOL, "UnhookWindowsHookEx")
    _safe_configure(user32.CallNextHookEx,
                    [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, ctypes.c_void_p],
                    LRESULT, "CallNextHookEx")
    _safe_configure(user32.GetMessageW,
                    [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT],
                    wintypes.BOOL, "GetMessageW")
    _safe_configure(user32.PeekMessageW,
                    [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT, wintypes.UINT],
                    wintypes.BOOL, "PeekMessageW")
    _safe_configure(user32.TranslateMessage,
                    [ctypes.POINTER(wintypes.MSG)],
                    wintypes.BOOL, "TranslateMessage")
    _safe_configure(user32.DispatchMessageW,
                    [ctypes.POINTER(wintypes.MSG)],
                    LRESULT, "DispatchMessageW")
    _safe_configure(user32.PostThreadMessageW,
                    [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
                    wintypes.BOOL, "PostThreadMessageW")
    _safe_configure(user32.EnumDisplayMonitors, [
        wintypes.HDC, ctypes.POINTER(RECT), MONITORENUMPROC, wintypes.LPARAM
    ], wintypes.BOOL, "EnumDisplayMonitors")
    _safe_configure(user32.GetMonitorInfoW,
                    [wintypes.HANDLE, ctypes.POINTER(MONITORINFOEXW)],
                    wintypes.BOOL, "GetMonitorInfoW")
    _safe_configure(kernel32.GetModuleHandleW, [wintypes.LPCWSTR],
                    ctypes.c_void_p, "GetModuleHandleW")
    _safe_configure(kernel32.GetCurrentThreadId, [],
                    wintypes.DWORD, "GetCurrentThreadId")


_configure_function_signatures()


def get_window_title(hwnd: int) -> str:
    """获取窗口标题（统一实现，替代散落的 ctypes GetWindowTextW 调用）。"""
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def key_event_to_hotkey_text(event) -> str:
    key = event.key()
    modifiers = event.modifiers()
    modifier_flags = Qt.ControlModifier | Qt.AltModifier | Qt.ShiftModifier | Qt.MetaModifier
    if key == Qt.Key_Escape and not (modifiers & modifier_flags):
        return ""

    parts = []
    if modifiers & Qt.ControlModifier and key != Qt.Key_Control:
        parts.append("ctrl")
    if modifiers & Qt.AltModifier and key != Qt.Key_Alt:
        parts.append("alt")
    if modifiers & Qt.ShiftModifier and key != Qt.Key_Shift:
        parts.append("shift")
    if modifiers & Qt.MetaModifier and key != Qt.Key_Meta:
        parts.append("win")

    special = {
        Qt.Key_Backspace: "backspace", Qt.Key_Tab: "tab", Qt.Key_Return: "enter",
        Qt.Key_Enter: "enter", Qt.Key_Escape: "esc", Qt.Key_Space: "space",
        Qt.Key_PageUp: "pageup", Qt.Key_PageDown: "pagedown", Qt.Key_End: "end",
        Qt.Key_Home: "home", Qt.Key_Left: "left", Qt.Key_Up: "up", Qt.Key_Right: "right",
        Qt.Key_Down: "down", Qt.Key_Print: "printscreen", Qt.Key_Insert: "insert",
        Qt.Key_Delete: "delete", Qt.Key_Control: "ctrl", Qt.Key_Alt: "alt",
        Qt.Key_Shift: "shift", Qt.Key_Meta: "win",
    }
    for index in range(1, 13):
        special[getattr(Qt, f"Key_F{index}")] = f"f{index}"

    if key in special:
        key_text = special[key]
    else:
        text = event.text()
        key_text = text.lower() if text and text.strip() else ""
        if not key_text and Qt.Key_A <= key <= Qt.Key_Z:
            key_text = chr(ord("a") + key - Qt.Key_A)
        elif not key_text and Qt.Key_0 <= key <= Qt.Key_9:
            key_text = chr(ord("0") + key - Qt.Key_0)
    if not key_text:
        return ""
    if key_text not in parts:
        parts.append(key_text)
    return "+".join(parts)


def normalize_hotkey_token(token) -> str:
    token = str(token or "").strip().lower().replace("＋", "+")
    token = HOTKEY_ALIAS.get(token, token)
    if token.startswith("numpad") and token[6:].isdigit():
        token = f"num{token[6:]}"
    return token


def hotkey_display_text(text) -> str:
    raw = str(text or "").strip().replace("＋", "+")
    chunks = [normalize_hotkey_token(part) for part in raw.split("+") if str(part).strip()]
    if not chunks:
        return ""
    display_parts = []
    display_names = {
        "esc": "Esc", "enter": "Enter", "space": "Space", "tab": "Tab",
        "backspace": "Backspace", "capslock": "CapsLock", "pageup": "PageUp",
        "pagedown": "PageDown", "printscreen": "PrintScreen", "insert": "Insert",
        "delete": "Delete", "home": "Home", "end": "End", "left": "Left",
        "right": "Right", "up": "Up", "down": "Down",
    }
    for chunk in chunks:
        if chunk in MOD_DISPLAY:
            display_parts.append(MOD_DISPLAY[chunk])
        elif chunk.startswith("f") and chunk[1:].isdigit():
            display_parts.append(chunk.upper())
        elif chunk.startswith("num") and chunk[3:].isdigit():
            display_parts.append(f"Num{chunk[3:]}")
        elif len(chunk) == 1:
            display_parts.append(chunk.upper())
        else:
            display_parts.append(display_names.get(chunk, chunk))
    return "+".join(display_parts)


def parse_hotkey_text(text) -> dict[str, str | int | list[str] | bool] | None:
    raw = str(text or "").strip().replace("＋", "+")
    if not raw:
        return None
    chunks = [normalize_hotkey_token(part) for part in raw.split("+") if str(part).strip()]
    if not chunks:
        return None
    modifier_names = []
    key_name = None
    for chunk in chunks:
        if chunk in MOD_NAME_TO_MASK:
            if chunk not in modifier_names:
                modifier_names.append(chunk)
            continue
        if key_name is not None:
            return None
        key_name = chunk
    if not key_name or key_name in MOD_NAME_TO_MASK:
        return None
    if len(key_name) == 1:
        key_name = key_name.lower()
    virtual_key = HOTKEY_NAME_TO_VK.get(key_name)
    if virtual_key is None:
        return None
    ordered_modifiers = [name for name in MOD_ORDER if name in modifier_names]
    modifiers = 0
    for name in ordered_modifiers:
        modifiers |= MOD_NAME_TO_MASK[name]
    canonical = "+".join(ordered_modifiers + [key_name])
    return {
        "text": canonical,
        "display": hotkey_display_text(canonical),
        "key": key_name,
        "vk": virtual_key,
        "modifiers": modifiers,
        "mod_names": ordered_modifiers,
        "bare": modifiers == 0,
    }


def is_safe_global_hotkey(parsed) -> bool:
    return bool(parsed and ((not parsed.get("bare")) or parsed.get("key") in SAFE_BARE_GLOBAL_KEYS))


def hotkey_signature(parsed) -> tuple[int, int] | None:
    if not parsed:
        return None
    return int(parsed.get("modifiers", 0)), int(parsed.get("vk", 0))


def modifier_is_down(name) -> bool:
    """Whether *any* virtual key of that modifier is currently held.

    FR-SCOPE003-002: a modifier has left/right/generic variants, and users press
    the right one all the time. Querying only the generic VK (or only the left one)
    made ``win`` fail when just the right Win (0x5C) was down, so a binding the
    user had actually pressed looked inactive. The whole set must be queried.
    """
    virtual_keys = MOD_NAME_TO_VKS.get(name)
    if not virtual_keys:
        return False
    return any(GetAsyncKeyState(vk) & 0x8000 for vk in virtual_keys)


def hotkey_is_down(parsed) -> bool:
    if not parsed:
        return False
    required_modifiers = set(parsed.get("mod_names", []))
    if any(not modifier_is_down(name) for name in required_modifiers):
        return False
    if any(name not in required_modifiers and modifier_is_down(name) for name in MOD_ORDER):
        return False
    return bool(GetAsyncKeyState(parsed["vk"]) & 0x8000)


def hotkey_text_from_pressed_vks(virtual_key, pressed_vks) -> str:
    """Build a hotkey from Hook-maintained key state, without async-state races."""

    virtual_key = int(virtual_key)
    if virtual_key in MODIFIER_VKS:
        return ""
    key_name = VK_MAP.get(virtual_key)
    if not key_name or key_name in MOD_NAME_TO_MASK:
        return ""
    down = {int(vk) for vk in (pressed_vks or ())}
    parts = [
        name
        for name in MOD_ORDER
        if any(vk in down for vk in MOD_NAME_TO_VKS[name])
    ]
    parts.append(key_name)
    parsed = parse_hotkey_text("+".join(parts))
    return cast(str, parsed["text"]) if parsed else ""


def pressed_hotkey_display_text(pressed_vks) -> str:
    """Render the complete Hook-maintained key state, including modifiers only."""

    down = {int(vk) for vk in (pressed_vks or ())}
    parts = [
        name
        for name in MOD_ORDER
        if any(vk in down for vk in MOD_NAME_TO_VKS[name])
    ]
    key_names = [
        VK_MAP[vk]
        for vk in sorted(down)
        if vk not in MODIFIER_VKS and vk in VK_MAP
    ]
    parts.extend(key_names)
    return hotkey_display_text("+".join(parts)) if parts else ""


def make_mouse_lparam(x, y) -> wintypes.LPARAM:
    # WM_*BUTTON* 的 lParam 坐标以有符号 16 位（SHORT）解释。坐标超过 ±32767 时
    # 直接 & 0xFFFF 会回绕成错误的有符号值（例如 x=40000 被解释为 -25536），导致
    # 点击落到错误位置（P3-3）。先饱和到 [-32768, 32767]，再取低 16 位，保证两字节
    # 补码表示正确。正常显示器 client 坐标均落在此范围内，本保护不改变既有行为。
    x = max(-32768, min(32767, int(x)))
    y = max(-32768, min(32767, int(y)))
    return wintypes.LPARAM(((y & 0xFFFF) << 16) | (x & 0xFFFF))


def post_double_click_to_window_at_point(screen_x, screen_y, button="left") -> bool:
    """向前台双击落点处的窗口补发一次 ``WM_*BUTTONDBLCLK``。

    DESIGN: 前台双击原本以两次 down/up 物理注入模拟，对未声明 ``CS_DBLCLKS`` 类风格
    或自行实现双击计时的目标窗口不会收到 ``WM_LBUTTONDBLCLK``，与后台 PostMessage
    路径（mapping_backend）语义不一致（见 P3-1）。本函数在物理点击之后对落点窗口
    显式补发一次 DBLCLK 使前后台一致；若窗口已声明 ``CS_DBLCLKS``（系统会自动
    合成 DBLCLK）则跳过，避免对这类窗口重复触发。

    坐标为输入坐标（PMv2 下即物理像素；System-DPI 回退下与 WindowFromPoint/
    GetWindowRect 的虚拟化空间同一致，FR-401-001）。返回值仅指示“是否成功补发/已
    无需补发”，失败时静默返回 False（物理点击仍已完成，不影响主流程）。

    Args:
        screen_x: 点击点屏幕横坐标（输入坐标）。
        screen_y: 点击点屏幕纵坐标（输入坐标）。
        button: 鼠标按键，"left" 或 "right"。
    """
    try:
        point = POINT(int(screen_x), int(screen_y))
        hwnd = user32.WindowFromPoint(point)
        if not hwnd:
            return False
        # FR-WIN-001: 向落点控件（而非顶层窗口）补发 DBLCLK。顶层窗口带标题栏
        # 外框，向它发 WM_*BUTTONDBLCLK 不会转成子控件的 BN_DBLCLK；且按根窗口
        # GetWindowRect 算出的 client 坐标含外框偏移而错位。
        target = hwnd
        # 已声明 CS_DBLCLKS 的窗口由系统自动合成 DBLCLK，无需补发。
        style = int(user32.GetClassLongPtrW(target, GCL_STYLE) or 0)
        if style & CS_DBLCLKS:
            return True
        # 用 ScreenToClient 把屏幕点换算为落点窗口客户区坐标，避免外框偏移。
        client = POINT(int(screen_x), int(screen_y))
        if not user32.ScreenToClient(target, ctypes.byref(client)):
            return False
        lparam = make_mouse_lparam(int(client.x), int(client.y))
        if button == "right":
            msg, flag = WM_RBUTTONDBLCLK, MK_RBUTTON
        else:
            msg, flag = WM_LBUTTONDBLCLK, MK_LBUTTON
        user32.PostMessageW(target, msg, flag, lparam)
        return True
    except Exception as exc:  # noqa: BLE001
        # FR-20261008-402 / DOC-20261008-401: log instead of writing to
        # ``sys.stderr`` (``None`` in a frozen windowed build), and return False so
        # the documented "失败时静默返回 False" contract holds on this path too.
        logger.warning("post_double_click_to_window_at_point 失败: %s", exc)
        return False
