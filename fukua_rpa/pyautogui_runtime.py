"""Lazy PyAutoGUI access so OpenCV/NumPy do not block the first UI frame."""

from __future__ import annotations

import importlib
import threading
import types
from typing import Any

_module: types.ModuleType | None = None
_lock = threading.Lock()


def get_pyautogui() -> types.ModuleType:
    """Import and cache the pyautogui module on first use.

    Thread-safe double-checked locking ensures the heavy OpenCV/NumPy
    import happens at most once, deferred until the first mouse or
    keyboard action rather than at application startup.

    Returns:
        The imported ``pyautogui`` module with FAILSAFE disabled and
        PAUSE set to zero.
    """
    global _module
    if _module is not None:
        return _module
    with _lock:
        if _module is None:
            module = importlib.import_module("pyautogui")
            module.FAILSAFE = False  # pyright: ignore[reportAttributeAccessIssue]
            module.PAUSE = 0  # pyright: ignore[reportAttributeAccessIssue]
            _module = module
    return _module


class _LazyPyAutoGUI:
    """Proxy that forwards attribute access to the lazily-imported module."""

    def __getattr__(self, name: str) -> Any:
        """Delegate attribute lookup to the real pyautogui module."""
        return getattr(get_pyautogui(), name)

    def __dir__(self) -> list[str]:
        """Merge proxy and real module attributes for introspection."""
        return sorted(set(super().__dir__()) | set(dir(get_pyautogui())))


pyautogui = _LazyPyAutoGUI()
