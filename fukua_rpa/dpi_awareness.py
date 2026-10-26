"""Windows process DPI-awareness bootstrap without importing Qt.

This module is intentionally safe to import from ``fukuaRPA.py`` before
PySide6.  Screen capture, low-level mouse hooks and input injection all use
physical desktop pixels only when the process is per-monitor DPI aware.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

DPI_AWARENESS_PER_MONITOR_AWARE = 2
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

user32 = ctypes.windll.user32
try:
    shcore = ctypes.windll.shcore
except Exception:  # noqa: BLE001 - supported Windows versions normally provide it
    shcore = None


def _configure_signatures() -> None:
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        user32.GetThreadDpiAwarenessContext.argtypes = []
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
    except Exception:  # noqa: BLE001 - callers retain the shcore fallback
        pass
    if shcore is not None:
        try:
            shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
            shcore.SetProcessDpiAwareness.restype = ctypes.c_long
            shcore.GetProcessDpiAwareness.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ctypes.c_int),
            ]
            shcore.GetProcessDpiAwareness.restype = ctypes.c_long
        except Exception:  # noqa: BLE001
            pass


_configure_signatures()


def current_dpi_awareness() -> int | None:
    """Return the current thread/process DPI-awareness level when available."""

    try:
        context = user32.GetThreadDpiAwarenessContext()
        if context:
            value = int(user32.GetAwarenessFromDpiAwarenessContext(context))
            if value >= 0:
                return value
    except Exception:  # noqa: BLE001 - try the process-level API below
        pass
    if shcore is not None:
        try:
            value = ctypes.c_int(0)
            result = int(shcore.GetProcessDpiAwareness(None, ctypes.byref(value)))
            if result >= 0:
                return int(value.value)
        except Exception:  # noqa: BLE001
            pass
    return None


def _request_per_monitor_v2() -> bool:
    """Request PMv2, returning the direct Win32 result without interpreting it."""

    try:
        return bool(
            user32.SetProcessDpiAwarenessContext(
                DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            )
        )
    except Exception:  # noqa: BLE001
        return False


def _request_per_monitor_v1() -> bool:
    """Fallback for hosts where the PMv2 entry point cannot be called."""

    if shcore is None:
        return False
    try:
        return int(shcore.SetProcessDpiAwareness(DPI_AWARENESS_PER_MONITOR_AWARE)) >= 0
    except Exception:  # noqa: BLE001
        return False


def enable_per_monitor_v2() -> bool:
    """Ensure physical-pixel coordinate semantics before any Qt window exists.

    ``SetProcessDpiAwarenessContext`` returns ``FALSE`` with access denied when
    the host executable or an earlier bootstrap already selected PMv2.  That is
    not a failure, so the actual context is checked both before and after every
    request.  Treating the raw return value as authoritative caused the former
    mixed-DPI path to apply a second, incorrect scale conversion.
    """

    if current_dpi_awareness() == DPI_AWARENESS_PER_MONITOR_AWARE:
        return True
    requested = _request_per_monitor_v2()
    current = current_dpi_awareness()
    if current == DPI_AWARENESS_PER_MONITOR_AWARE:
        return True
    if requested and current is None:
        return True

    requested = _request_per_monitor_v1()
    current = current_dpi_awareness()
    if current == DPI_AWARENESS_PER_MONITOR_AWARE:
        return True
    return bool(requested and current is None)


__all__ = [
    "DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2",
    "DPI_AWARENESS_PER_MONITOR_AWARE",
    "current_dpi_awareness",
    "enable_per_monitor_v2",
]
