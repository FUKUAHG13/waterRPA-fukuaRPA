"""Stable Win32 low-level hook adapter for UI capture and recording."""

from __future__ import annotations

import contextlib
import ctypes
import threading
import time
from collections.abc import Callable
from typing import Any

from .native_services import HOOK_SERVICE_CONTRACT_VERSION, BackendServiceDescriptor
from .timing_constants import POLL_INTERVAL_S
from .win32_api import kernel32, user32

# Win32 low-level hook type identifiers for SetWindowsHookExW.
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14


class Win32HookBackend:
    """Install and track Win32 low-level keyboard/mouse hooks via ctypes.

    The backend retains a reference to each installed callback so the
    ctypes callable is not garbage-collected while the OS may still invoke
    it. Uninstalling removes the reference only after the hook is confirmed
    removed and a short safety delay has elapsed.
    """

    def __init__(self) -> None:
        self._callback_lock = threading.RLock()
        self._installed_callbacks: dict[int, Any] = {}

    @staticmethod
    def _handle_key(handle: Any) -> int:
        """Normalize a ctypes or plain-integer hook handle to an int key."""
        return int(getattr(handle, "value", handle) or 0)

    @property
    def has_active_hooks(self) -> bool:
        """是否有已安装的 Hook 回调（公开访问器，替代私有属性访问）。"""
        return bool(self._installed_callbacks)

    def install_keyboard(self, callback: Callable[..., Any]) -> Any:
        """Install a low-level keyboard hook (WH_KEYBOARD_LL).

        Args:
            callback: A ctypes-compatible HOOKPROC invoked by the OS on
                every keyboard event.

        Returns:
            The hook handle (truthy on success, falsy on failure).
        """
        handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            callback,
            kernel32.GetModuleHandleW(None),
            0,
        )
        if handle:
            with self._callback_lock:
                self._installed_callbacks[self._handle_key(handle)] = callback
        return handle

    def install_mouse(self, callback: Callable[..., Any]) -> Any:
        """Install a low-level mouse hook (WH_MOUSE_LL).

        Args:
            callback: A ctypes-compatible HOOKPROC invoked by the OS on
                every mouse event.

        Returns:
            The hook handle (truthy on success, falsy on failure).
        """
        handle = user32.SetWindowsHookExW(
            WH_MOUSE_LL,
            callback,
            kernel32.GetModuleHandleW(None),
            0,
        )
        if handle:
            with self._callback_lock:
                self._installed_callbacks[self._handle_key(handle)] = callback
        return handle

    def uninstall(self, handle: Any) -> bool:
        """Remove a previously installed hook and release its callback reference.

        Args:
            handle: The value returned by ``install_keyboard`` or
                ``install_mouse``; falsy values are treated as no-ops.

        Returns:
            True when the hook was successfully removed (or was already
            absent), False when UnhookWindowsHookEx reported failure.
        """
        if not handle:
            return True
        removed = bool(user32.UnhookWindowsHookEx(handle))
        if removed:
            # SAFETY: 短暂等待，确保正在执行中的低级钩子回调完成后再移除引用，
            # 避免回调对象被 GC 导致访问违规。
            # 回调内仅有有界集合操作（add/discard/emit），50ms 远超其执行耗时，
            # 足以覆盖 OS 线程调度延迟，属启发式安全余量而非精确上界。
            time.sleep(POLL_INTERVAL_S)
            with self._callback_lock:
                self._installed_callbacks.pop(self._handle_key(handle), None)
        return removed

    def call_next(self, handle: Any, code: int, wparam: int, lparam: int) -> int:
        """Forward to CallNextHookEx so other hooks in the chain still fire."""
        return user32.CallNextHookEx(handle, code, wparam, lparam)

    def service_snapshot(self, *, requested: bool = True) -> dict[str, Any]:
        """Return a versioned descriptor of the hook backend's capabilities."""
        return BackendServiceDescriptor(
            service="hook",
            contract_version=HOOK_SERVICE_CONTRACT_VERSION,
            backend="win32.ctypes",
            requested=bool(requested),
            available=bool(hasattr(ctypes, "windll")),
            native=True,
            capabilities=(
                "keyboard.low_level",
                "mouse.low_level",
                "recording.control_keys",
            ),
            cancellation="cooperative",
        ).snapshot()


win32_hook_backend = Win32HookBackend()


def uninstall_all_hooks() -> None:
    """SAFETY: atexit 兜底——卸载所有已安装的低级 Hook。

    正常退出路径由录制器/热键管理器显式卸载各自的 Hook；
    此函数仅在异常退出时确保不遗留全局钩子。
    """
    backend = win32_hook_backend
    with backend._callback_lock:
        handles = list(backend._installed_callbacks.keys())
    for handle_key in handles:
        with contextlib.suppress(Exception):
            user32.UnhookWindowsHookEx(handle_key)
    with backend._callback_lock:
        backend._installed_callbacks.clear()
