"""Stable input-injection adapter used by the execution engine."""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Any, cast

from .native_services import (
    INPUT_SERVICE_CONTRACT_VERSION,
    BackendServiceDescriptor,
)
from .pyautogui_runtime import pyautogui as _pyautogui_proxy

# INVARIANT: the INPUT family has exactly one definition in the process
# (fukua_rpa/win32_input.py). A second, structurally equal class here
# would make the shared SendInput argtypes reject our array
# (FR-20261016-SUP-001).
from .win32_input import INPUT, KEYBDINPUT, MOUSEINPUT

# Private aliases: probes and evidence identify the backend's event type
# as ``input_backend._INPUT``; keep that name resolving (FR-20261016-SUP-001).
_INPUT = INPUT
_KEYBDINPUT = KEYBDINPUT
_MOUSEINPUT = MOUSEINPUT

# The lazy proxy's __getattr__ returns object; cast to Any so pyright
# accepts method calls without per-line suppression.
pyautogui = cast(Any, _pyautogui_proxy)

logger = logging.getLogger(__name__)

# 输入后端模式（全局设置 `input_backend_mode`）。
INPUT_BACKEND_DEFAULT = "默认"
INPUT_BACKEND_SENDINPUT = "SendInput"

# Win32 SendInput 常量。
_INPUT_MOUSE = 0
_INPUT_KEYBOARD = 1
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_ABSOLUTE = 0x8000
# 绝对坐标默认映射主显示器；置位后映射整个虚拟桌面（多屏/负原点必需）。
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_SCANCODE = 0x0008
_KEYEVENTF_EXTENDEDKEY = 0x0001
# 一个滚轮格数的原始增量。调用方（actions/flow._execute_scroll）把格数换算成
# 该增量后传入后端 scroll()；后端必须原样发送，不得再次放大（FR-001）。
_WHEEL_DELTA = 120
# 需要扩展位（0xE0 前缀）的键；缺少它时方向键等会与数字键盘混淆。
# LWin/RWin 的物理扫描码同样带 E0 前缀（FR-RISK-20261010-2102）。
_EXTENDED_VK = frozenset(
    {
        0x21,  # PageUp
        0x22,  # PageDown
        0x23,  # End
        0x24,  # Home
        0x25,  # Left
        0x26,  # Up
        0x27,  # Right
        0x28,  # Down
        0x2D,  # Insert
        0x2E,  # Delete
        0x5B,  # LWin
        0x5C,  # RWin
        0x6F,  # NumpadDivide
        0x90,  # NumLock
        0x2C,  # PrintScreen
    }
)
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_MAPVK_VK_TO_VSC = 0
# EX 版本在高字节返回 E0/E1 前缀，是还原扩展键扫描码的唯一可靠来源
# （FR-20261012-637）。
_MAPVK_VK_TO_VSC_EX = 4
_KEYEVENT_NAME_TO_VK = {
    "enter": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "tab": 0x09,
    "backspace": 0x08,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "win": 0x5B,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "insert": 0x2D,
    "delete": 0x2E,
    "capslock": 0x14,
    "printscreen": 0x2C,
    "volumeup": 0xAF,
    "volumedown": 0xAE,
    "volumemute": 0xAD,
    # 标点键名与 win32_api.HOTKEY_NAME_TO_VK 的权威 OEM 映射保持一致；缺少它们
    # 时单字符回退会把 ASCII 码当 VK 注入完全不同的键（FR-20261010-2302：
    # '.'→Delete、'['→LWin、'''→Right 等）。
    ";": 0xBA,
    "=": 0xBB,
    ",": 0xBC,
    "-": 0xBD,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "[": 0xDB,
    "\\": 0xDC,
    "]": 0xDD,
    "'": 0xDE,
    # 小键盘键名必须与 win32_api.HOTKEY_NAME_TO_VK 的权威映射一致
    # （FR-20261011-009）：缺少它们时 SendInput 模式对 num0..num9 抛
    # InputInjectionError，而默认 PyAutoGUI 后端接受同样的键名。
    "num0": 0x60,
    "num1": 0x61,
    "num2": 0x62,
    "num3": 0x63,
    "num4": 0x64,
    "num5": 0x65,
    "num6": 0x66,
    "num7": 0x67,
    "num8": 0x68,
    "num9": 0x69,
}
_MOUSE_BUTTON_FLAGS = {
    "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
    "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
    "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
}


class InputInjectionError(RuntimeError):
    """Raised when an explicit input backend could not inject an event.

    DESIGN: a failed injection must never look like success. The engine's action
    layer already converts exceptions into a step error, so the user sees a real
    failure instead of a silent no-op, and no fallback backend is used mid-run
    (see ``docs/设计基准.md``).
    """



def virtual_desktop_absolute(x, y, metrics=None):
    """Map a virtual-desktop pixel coordinate to SendInput's 0..65535 space.

    ``metrics`` is ``(left, top, width, height)`` of the virtual desktop; it is
    read from Windows when omitted. Values are clamped so a coordinate outside
    the virtual desktop cannot overflow the 16-bit fields (negative-coordinate
    and multi-monitor layouts included).
    """

    if metrics is None:
        # DESIGN: 缺省时读取真实虚拟桌面边界，兑现 docstring 承诺
        # （FR-20261010-2301）；类在下方定义，调用时解析。
        metrics = SendInputInputBackend._virtual_desktop_metrics()
    left, top, width, height = metrics
    width = max(1, int(width))
    height = max(1, int(height))
    relative_x = min(max(int(x) - int(left), 0), width - 1)
    relative_y = min(max(int(y) - int(top), 0), height - 1)
    abs_x = int(round(relative_x * 65535 / (width - 1))) if width > 1 else 0
    abs_y = int(round(relative_y * 65535 / (height - 1))) if height > 1 else 0
    return abs_x, abs_y


def key_scan_codes(key, *, scan_codes):
    """Return ``(vk, scan, extra_flags)`` for one key name or hex virtual key."""

    name = str(key or "").strip().lower()
    vk = _KEYEVENT_NAME_TO_VK.get(name)
    if vk is None:
        # 单字符回退仅限字母/数字（其 ASCII 码与 VK 一致）；其余单字符显式
        # 返回 None 交由后端抛 InputInjectionError，不得把任意 ASCII 当 VK
        # （FR-20261010-2302）。
        if (
            len(name) == 1
            and name.isascii()
            and (name.isdigit() or name.isalpha())
        ):
            vk = ord(name.upper())
        elif name.startswith("f") and name[1:].isdigit():
            number = int(name[1:])
            if 1 <= number <= 24:
                vk = 0x6F + number
        elif name.startswith("0x"):
            try:
                parsed = int(name, 16)
            except ValueError:
                parsed = None
            # SAFETY: ``wVk`` 是 WORD，超出 16 位的值会被 ctypes 静默截断成
            # 另一个合法键（0x10000→0、0x10041→'A'），使失败看起来像成功。
            # 越界必须明确返回 None 由后端抛错（FR-20261012-638）。
            vk = parsed if parsed is not None and 0 <= parsed <= 0xFFFF else None
    if vk is None:
        return None
    flags = _KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VK else 0
    scan = 0
    if scan_codes:
        # DESIGN: 扫描码模式必须用 EX 查询还原物理扫描码的 E0 前缀
        # （FR-20261012-637）。音量三键等不在 ``_EXTENDED_VK`` 内的 VK，其
        # 物理扫描码带 E0；用非 EX 查询得到的 0x30/0x2E/0x20 会被系统译为
        # 'B'/'C'/'D'。仅高字节为 0xE0 时取低字节并补扩展位；0xE1（Pause）
        # 无法用单个扩展位表达，维持非 EX 查询值以免与 Ctrl 混淆。
        mapped = int(
            ctypes.windll.user32.MapVirtualKeyW(int(vk), _MAPVK_VK_TO_VSC_EX)
        )
        prefix = (mapped >> 8) & 0xFF
        if prefix == 0xE1:
            scan = int(
                ctypes.windll.user32.MapVirtualKeyW(int(vk), _MAPVK_VK_TO_VSC)
            )
        else:
            scan = mapped & 0xFF
            if prefix == 0xE0:
                flags |= _KEYEVENTF_EXTENDEDKEY
        flags |= _KEYEVENTF_SCANCODE
    return int(vk), scan, flags


class PyAutoGUIInputBackend:
    """Keep current behavior behind a contract that can gain a native backend.

    Every method delegates directly to pyautogui; the class exists so the
    engine codes against a stable interface and a native SendInput
    implementation can replace it without touching call sites.
    """

    name = "python.pyautogui"

    def move_to(self, x: int, y: int, *, duration: float = 0) -> None:
        """Move the mouse cursor to absolute screen coordinates."""
        return pyautogui.moveTo(x, y, duration=duration)

    def position(self) -> tuple[int, int]:
        """Return the current cursor position as ``(x, y)``."""
        return pyautogui.position()

    def mouse_down(self, *, button: str) -> None:
        """Press and hold a mouse button (``"left"``, ``"right"`` or ``"middle"``)."""
        return pyautogui.mouseDown(button=button)

    def mouse_up(self, *, button: str) -> None:
        """Release a previously pressed mouse button."""
        return pyautogui.mouseUp(button=button)

    def key_down(self, key: str) -> None:
        """Press and hold a keyboard key by pyautogui key name."""
        return pyautogui.keyDown(key)

    def key_up(self, key: str) -> None:
        """Release a previously pressed keyboard key."""
        return pyautogui.keyUp(key)

    def scroll(self, amount: int) -> None:
        """Send a raw wheel delta event (positive = up).

        ``amount`` is the raw ``mouse_event`` wheel delta (one notch = 120);
        pyautogui's Windows ``_scroll`` forwards it as ``dwData`` verbatim, and
        the SendInput backend must accept the same unit (one notch = 120).
        """
        return pyautogui.scroll(amount)

    def service_snapshot(self) -> dict[str, Any]:
        """Return a versioned descriptor of the input backend's capabilities."""
        return BackendServiceDescriptor(
            service="input",
            contract_version=INPUT_SERVICE_CONTRACT_VERSION,
            backend=self.name,
            requested=True,
            available=True,
            native=False,
            capabilities=(
                "keyboard.down_up",
                "mouse.down_up",
                "mouse.move",
                "mouse.scroll",
            ),
            cancellation="cooperative",
        ).snapshot()


class SendInputInputBackend:
    """Explicit opt-in Win32 SendInput backend for standard input compatibility.

    DESIGN: this is a compatibility attempt, not a promise to bypass a target
    program's protections (see ``docs/设计基准.md``). It is only used when the
    user selects the SendInput mode; the default path stays PyAutoGUI, and a
    failing call never silently switches back mid-run. ``key_scan_codes`` sends
    scan codes (``KEYEVENTF_SCANCODE``) with the extended-key flag for keys that
    need it, leaving the ordinary name-based path available.
    """

    name = "win32.sendinput"

    def __init__(self, *, key_scan_codes: bool = False):
        self.key_scan_codes = bool(key_scan_codes)

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _virtual_desktop_metrics():
        user32 = ctypes.windll.user32
        return (
            user32.GetSystemMetrics(_SM_XVIRTUALSCREEN),
            user32.GetSystemMetrics(_SM_YVIRTUALSCREEN),
            user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN),
            user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN),
        )

    @staticmethod
    def _send(*inputs) -> bool:
        if not inputs:
            return True
        array = (_INPUT * len(inputs))(*inputs)
        sent = ctypes.windll.user32.SendInput(
            len(inputs), array, ctypes.sizeof(_INPUT)
        )
        if sent != len(inputs):
            # SAFETY: 注入数量不足时必须明确失败，不能静默当作已发送。
            raise InputInjectionError(
                f"SendInput 只发送了 {sent}/{len(inputs)} 个输入事件，"
                "目标程序可能因权限或系统限制拒绝输入"
            )
        return True

    def _mouse_input(self, flags, *, data=0, dx=0, dy=0):
        item = _INPUT()
        item.type = _INPUT_MOUSE
        item.mi = _MOUSEINPUT(dx, dy, data, flags, 0, None)
        return item

    def _key_input(self, key, *, key_up: bool):
        mapped = key_scan_codes(key, scan_codes=self.key_scan_codes)
        if mapped is None:
            # SAFETY: 未识别的键名必须明确失败，不能静默丢键后仍让步骤报成功。
            raise InputInjectionError(
                f"SendInput 无法识别按键名称“{key}”；请改用 F1-F12、"
                "字母数字或本机组合键，或切回默认输入方式"
            )
        vk, scan, flags = mapped
        if key_up:
            flags |= _KEYEVENTF_KEYUP
        item = _INPUT()
        item.type = _INPUT_KEYBOARD
        item.ki = _KEYBDINPUT(vk if not self.key_scan_codes else 0, scan, flags, 0, None)
        return item

    # -- backend contract ------------------------------------------------
    def move_to(self, x: int, y: int, *, duration: float = 0) -> None:
        abs_x, abs_y = virtual_desktop_absolute(x, y, self._virtual_desktop_metrics())
        self._send(
            self._mouse_input(
                _MOUSEEVENTF_MOVE
                | _MOUSEEVENTF_ABSOLUTE
                | _MOUSEEVENTF_VIRTUALDESK,
                dx=abs_x,
                dy=abs_y,
            )
        )

    def position(self) -> tuple[int, int]:
        point = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            # SAFETY: 读取失败必须显式失败，让引擎的瞬时移动退化路径生效
            # （FR-RISK-20261010-2101）；静默返回 (0,0) 会构建错误方向的移动。
            raise OSError("GetCursorPos 失败，无法读取当前鼠标位置")
        return int(point.x), int(point.y)

    def mouse_down(self, *, button: str) -> None:
        flags = _MOUSE_BUTTON_FLAGS.get(str(button), _MOUSE_BUTTON_FLAGS["left"])[0]
        self._send(self._mouse_input(flags))

    def mouse_up(self, *, button: str) -> None:
        flags = _MOUSE_BUTTON_FLAGS.get(str(button), _MOUSE_BUTTON_FLAGS["left"])[1]
        self._send(self._mouse_input(flags))

    def key_down(self, key: str) -> None:
        self._send(self._key_input(key, key_up=False))

    def key_up(self, key: str) -> None:
        self._send(self._key_input(key, key_up=True))

    def scroll(self, amount: int) -> None:
        """Send a raw wheel delta event (positive = up).

        ``amount`` is already the raw ``mouse_event`` wheel delta: the caller
        converts notches to deltas (one notch = ``_WHEEL_DELTA``) before calling
        (see ``actions/flow._execute_scroll``). pyautogui's Windows ``_scroll``
        forwards ``clicks`` as ``dwData`` verbatim, so both backends must accept
        the same unit here; multiplying again would amplify scrolling 120x
        (REVIEW-20261010-001 FR-001).
        """
        self._send(self._mouse_input(_MOUSEEVENTF_WHEEL, data=int(amount)))

    def service_snapshot(self) -> dict[str, Any]:
        return BackendServiceDescriptor(
            service="input",
            contract_version=INPUT_SERVICE_CONTRACT_VERSION,
            backend=self.name,
            requested=True,
            available=True,
            native=True,
            capabilities=(
                "keyboard.down_up",
                "keyboard.scan_codes" if self.key_scan_codes else "keyboard.virtual_keys",
                "mouse.down_up",
                "mouse.move",
                "mouse.scroll",
            ),
            cancellation="cooperative",
        ).snapshot()


def create_input_backend(mode, *, key_scan_codes: bool = False):
    """Build the configured input backend; unknown modes keep PyAutoGUI."""

    if str(mode or "").strip() == INPUT_BACKEND_SENDINPUT:
        return SendInputInputBackend(key_scan_codes=key_scan_codes)
    return PyAutoGUIInputBackend()
