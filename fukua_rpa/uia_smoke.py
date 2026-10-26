"""Real Win32 control smoke test shared by source and frozen releases."""

from __future__ import annotations

import ctypes
import multiprocessing
import os
import queue
from ctypes import wintypes
from typing import Any

from .uia_backend import UIAutomationBackend

WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_CHILD = 0x40000000
WS_TABSTOP = 0x00010000
BS_PUSHBUTTON = 0x00000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
BUTTON_ID = 1701
EDIT_ID = 1702
ES_AUTOHSCROLL = 0x0080


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _configure_win32() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    """Bind ctypes prototypes and return the user32 and kernel32 handles."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetDlgItem.restype = wintypes.HWND
    # P2-4: GetForegroundWindow 在 64 位 Windows 上返回 64 位 HWND。不声明
    # restype 时 ctypes 默认 c_int（32 位有符号）会截断句柄，破坏前台窗口
    # 比对（foreground_before/after 校验）。
    user32.GetForegroundWindow.restype = wintypes.HWND
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    return user32, kernel32


def _window_process(events: multiprocessing.Queue[Any]) -> None:
    """Child-process entry point: build a real window and pump its message loop.

    Args:
        events: Queue used to report ``ready``/``clicked``/``error`` events
            back to the parent process.
    """
    user32, kernel32 = _configure_win32()
    instance = kernel32.GetModuleHandleW(None)
    class_name = f"fukuaRPA_UIA_Smoke_{os.getpid()}"

    @WNDPROC
    def window_proc(
        hwnd: wintypes.HWND,
        message: wintypes.UINT,
        wparam: wintypes.WPARAM,
        lparam: wintypes.LPARAM,
    ):
        if message == WM_COMMAND and int(wparam) & 0xFFFF == BUTTON_ID:
            events.put(("clicked", int(hwnd)))
            return 0
        if message == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    window_class = WNDCLASSW()
    window_class.lpfnWndProc = window_proc
    window_class.hInstance = instance
    window_class.lpszClassName = class_name
    window_class.hbrBackground = wintypes.HBRUSH(6)
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        events.put(("error", f"RegisterClassW failed: {ctypes.get_last_error()}"))
        return

    root_hwnd = user32.CreateWindowExW(
        WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        class_name,
        "fukuaRPA UIA smoke",
        WS_OVERLAPPEDWINDOW,
        80,
        80,
        300,
        220,
        None,
        None,
        instance,
        None,
    )
    button_hwnd = user32.CreateWindowExW(
        0,
        "BUTTON",
        "Invoke test",
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | BS_PUSHBUTTON,
        45,
        45,
        180,
        50,
        root_hwnd,
        wintypes.HMENU(BUTTON_ID),
        instance,
        None,
    )
    edit_hwnd = user32.CreateWindowExW(
        0,
        "EDIT",
        "initial",
        WS_VISIBLE | WS_CHILD | WS_TABSTOP | ES_AUTOHSCROLL,
        45,
        110,
        180,
        28,
        root_hwnd,
        wintypes.HMENU(EDIT_ID),
        instance,
        None,
    )
    if not root_hwnd or not button_hwnd or not edit_hwnd:
        events.put(("error", f"CreateWindowExW failed: {ctypes.get_last_error()}"))
        return
    user32.ShowWindow(root_hwnd, SW_SHOWNOACTIVATE)
    rect = wintypes.RECT()
    user32.GetWindowRect(button_hwnd, ctypes.byref(rect))
    edit_rect = wintypes.RECT()
    user32.GetWindowRect(edit_hwnd, ctypes.byref(edit_rect))
    events.put(
        (
            "ready",
            int(root_hwnd),
            int(button_hwnd),
            (int(rect.left + rect.right) // 2, int(rect.top + rect.bottom) // 2),
            int(edit_hwnd),
            (
                int(edit_rect.left + edit_rect.right) // 2,
                int(edit_rect.top + edit_rect.bottom) // 2,
            ),
        )
    )
    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))


def run_uia_smoke() -> dict[str, Any]:
    """Invoke a real non-activating button and return a JSON-safe report.

    Returns:
        A report dict whose ``ok`` flag is True only when probe, activation,
        value round-trip, and foreground-unchanged checks all succeed.
    """

    multiprocessing.freeze_support()
    # FR-20260812-D4-001: 父进程直接调用 GetForegroundWindow 必须声明 64 位
    # restype，否则句柄 >2^31 时被截断，前台比对失效（restype 不跨进程继承）。
    ctypes.windll.user32.GetForegroundWindow.restype = wintypes.HWND
    # FR-SCOPE015D-002: 清理路径（finally）同样在父进程直接调用 PostMessageW。
    # 未声明 argtypes 时 ctypes 按 c_int 转换 HWND，root_hwnd >= 2^32 会在
    # finally 中抛 ctypes.ArgumentError；finally 的异常会**覆盖**正常返回值，
    # 使一次成功的冒烟变成异常退出。此处与 native_smoke._configure_win32 保持一致。
    ctypes.windll.user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    ctypes.windll.user32.PostMessageW.restype = wintypes.BOOL
    events = multiprocessing.Queue()
    process = multiprocessing.Process(
        target=_window_process,
        args=(events,),
        daemon=True,
    )
    backend = UIAutomationBackend()
    process_started = False
    root_hwnd = 0
    try:
        process.start()
        process_started = True
        ready = events.get(timeout=8.0)
        if ready[0] != "ready":
            raise RuntimeError(str(ready))
        _kind, root_hwnd, button_hwnd, point, edit_hwnd, edit_point = ready
        foreground_before = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        probe = backend.probe(
            root_hwnd,
            point[0],
            point[1],
            preferred_hwnd=button_hwnd,
        )
        if not probe.get("actionable"):
            raise RuntimeError(f"UIA probe found no action: {probe}")
        activated = backend.activate(
            root_hwnd,
            point[0],
            point[1],
            preferred_hwnd=button_hwnd,
        )
        if not activated.get("success"):
            raise RuntimeError(f"UIA activation failed: {activated}")
        clicked = events.get(timeout=4.0)
        if clicked[0] != "clicked":
            raise RuntimeError(f"button did not receive invoke: {clicked}")
        set_result = backend.set_value(
            root_hwnd,
            edit_point[0],
            edit_point[1],
            "UIA value round trip",
            preferred_hwnd=edit_hwnd,
        )
        if not set_result.get("success"):
            raise RuntimeError(f"UIA set value failed: {set_result}")
        read_result = backend.read_value(
            root_hwnd,
            edit_point[0],
            edit_point[1],
            preferred_hwnd=edit_hwnd,
        )
        if not read_result.get("success") or read_result.get("value") != "UIA value round trip":
            raise RuntimeError(f"UIA read value failed: {read_result}")
        foreground_after = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if foreground_before != foreground_after:
            raise RuntimeError(
                f"foreground window changed: {foreground_before} -> {foreground_after}"
            )
        control = activated.get("control", {})
        return {
            "format": "fukuaRPA_uia_smoke",
            "ok": True,
            "method": str(activated.get("method") or ""),
            "control_type": str(control.get("control_type") or ""),
            "nodes_scanned": int(activated.get("nodes_scanned", 0) or 0),
            "foreground_unchanged": True,
            "matched_bound_hwnd": bool(activated.get("matched_bound_hwnd")),
            "set_value_ok": True,
            "read_value_ok": True,
            "error": "",
        }
    except (queue.Empty, RuntimeError, OSError) as error:
        return {
            "format": "fukuaRPA_uia_smoke",
            "ok": False,
            "method": "",
            "control_type": "",
            "nodes_scanned": 0,
            "foreground_unchanged": False,
            "set_value_ok": False,
            "read_value_ok": False,
            "error": str(error),
        }
    finally:
        backend.close()
        if root_hwnd:
            ctypes.windll.user32.PostMessageW(root_hwnd, WM_CLOSE, 0, 0)
        if process_started:
            process.join(timeout=3.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2.0)
        events.close()
        events.join_thread()
