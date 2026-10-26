"""Single source of truth for the Win32 ``SendInput`` INPUT ABI.

INVARIANT: every caller in this project must build its events from the ``INPUT``
class defined here, and ``argtypes`` must be bound only in this module.

Two structurally identical but distinct ctypes classes both feeding the
process-wide cached ``ctypes.windll.user32.SendInput`` make argument marshalling
fail with ``expected LP_INPUT instance instead of ...``. That is the failure
behind FR-20261016-SUP-001: ``text_input`` bound the shared function's
``argtypes`` to its own ``INPUT``, and ``input_backend`` then passed an array of
its own separate ``_INPUT``. Because ctypes caches the function object per DLL,
"which module imported first" silently decided whether the explicit opt-in
SendInput backend worked at all.

Keeping one definition and one binding makes the result independent of import
order and of thread interleaving. Call sites deliberately stay in their own
modules so existing tests can still stub them (``text_input.SendInput`` for the
Unicode path, ``ctypes.windll.user32.SendInput`` for the backend path).

Scope note: this module owns only the SendInput INPUT ABI. ``fukua_rpa/win32_api.py``
owns cross-application Win32 prototypes and hotkey helpers, and imports PySide6
at module scope, so it must not be used here — pulling Qt into the input path
would break the deliberately Qt-free import boundary of ``text_input`` and
``input_backend``.
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes


class MOUSEINPUT(ctypes.Structure):
    """Win32 MOUSEINPUT structure for SendInput mouse events."""

    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        # INVARIANT: must stay ctypes.c_void_p. tests/test_window_diagnostics.py
        # asserts this exact type identity, and ctypes marshals the field from it.
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    """Win32 KEYBDINPUT structure for SendInput keyboard events."""

    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class HARDWAREINPUT(ctypes.Structure):
    """Win32 HARDWAREINPUT structure for SendInput hardware events."""

    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class INPUT_UNION(ctypes.Union):
    """Tagged union inside the Win32 INPUT structure."""

    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    """Win32 INPUT structure passed to SendInput."""

    _anonymous_ = ("union",)
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


# The Win32 INPUT struct must match the OS layout or SendInput reads garbage.
# On x64 the largest union member (MOUSEINPUT) forces sizeof(INPUT)==40; on
# x86 it is 28. Fail fast at import if the ctypes layout is wrong.
_EXPECTED_INPUT_SIZE = 40 if struct.calcsize("P") == 8 else 28
assert ctypes.sizeof(INPUT) == _EXPECTED_INPUT_SIZE, (
    f"INPUT size mismatch: {ctypes.sizeof(INPUT)} != {_EXPECTED_INPUT_SIZE}"
)


# The single binding for the whole process. ``argtypes`` is set here and nowhere
# else, so no module can re-point the cached function at a different INPUT type.
SendInput = ctypes.windll.user32.SendInput
SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
SendInput.restype = wintypes.UINT
