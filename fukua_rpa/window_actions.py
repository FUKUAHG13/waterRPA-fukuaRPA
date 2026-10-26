"""Bounded Win32 application and top-level window actions."""

from __future__ import annotations

import logging
import math
import os
import subprocess
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

from .timing_constants import WINDOW_POLL_INTERVAL_S
from .win32_api import WNDENUMPROC, get_window_title, user32

logger = logging.getLogger(__name__)

WM_CLOSE = 0x0010
SW_RESTORE = 9
MAX_WINDOW_QUERY_LENGTH = 500
MAX_WINDOW_WAIT_SECONDS = 86_400.0

# 项 33：跟踪由 launch_application 启动的子进程，避免孤儿进程。
# key 为 PID，value 为对应的 Popen 对象；在运行结束等合适生命周期点调用
# join_launched_application 回收。
_LAUNCHED_PROCESSES: dict[int, subprocess.Popen] = {}
_LAUNCHED_LOCK = threading.Lock()


def _in_job_object() -> bool:
    """Return True when the current process belongs to any Windows Job Object.

    FR-701-001: 独立 Runner 的 Job 守卫（KILL_ON_JOB_CLOSE）默认使子进程继承 Job，
    工作流启动的外部程序若留在 Job 内，运行结束/主程序退出时会被误杀。只有父进程在
    Job 内时才有必要传 CREATE_BREAKAWAY_FROM_JOB；该标志在无 Job 的环境会让
    CreateProcess 失败（Windows 8+ 语义），因此必须探测后再决定。探测失败按"不在
    Job 内"处理，不阻断启动。
    """
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        in_job = wintypes.BOOL(False)
        # JobHandle 为 NULL 表示"是否位于任意 Job"。
        if not kernel32.IsProcessInJob(None, None, ctypes.byref(in_job)):
            return False
        return bool(in_job.value)
    except Exception:  # noqa: BLE001 - 探测失败不阻断启动
        return False


def join_launched_application(pid: int, timeout: float | None = None) -> bool:
    """Wait for a process started by launch_application to exit.

    Args:
        pid: Process identifier returned by launch_application.
        timeout: Maximum seconds to wait; None waits indefinitely.

    Returns:
        True if the process exited (or was already gone), False on timeout.
    """

    with _LAUNCHED_LOCK:
        process = _LAUNCHED_PROCESSES.get(pid)
    if process is None:
        return True  # 未知或已回收
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    with _LAUNCHED_LOCK:
        _LAUNCHED_PROCESSES.pop(pid, None)
    return True


def join_all_launched_applications(timeout: float | None = 0.0) -> int:
    """回收全部由 launch_application 启动且已退出的子进程。

    P3-2: launch_application 仅把 Popen 存入 ``_LAUNCHED_PROCESSES``，原先没有任何
    运行期/结束点调用 ``join_launched_application``，导致 Popen 引用与句柄持续累积。
    本函数在运行结束（``finish_run``）与急停（``stop``）等生命周期点调用，轮询并
    移除已退出的进程以释放引用与句柄；仍在运行的进程保持跟踪（不强制终止，避免
    误杀用户有意保留的长期应用）。

    Args:
        timeout: 对每个仍在运行的进程额外等待的秒数；0（默认）表示仅轮询已退出
            进程而不阻塞。

    Returns:
        本次回收（移除跟踪）的进程数量。
    """
    with _LAUNCHED_LOCK:
        items = list(_LAUNCHED_PROCESSES.items())
    removed = 0
    for pid, process in items:
        try:
            if process.poll() is not None:
                # 已退出：poll 已回收退出码并关闭句柄，直接移除跟踪即可。
                with _LAUNCHED_LOCK:
                    if _LAUNCHED_PROCESSES.get(pid) is process:
                        _LAUNCHED_PROCESSES.pop(pid, None)
                removed += 1
            elif timeout and timeout > 0:
                try:
                    process.wait(timeout=timeout)
                    with _LAUNCHED_LOCK:
                        if _LAUNCHED_PROCESSES.get(pid) is process:
                            _LAUNCHED_PROCESSES.pop(pid, None)
                    removed += 1
                except subprocess.TimeoutExpired:
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug("join_all_launched_applications 回收 %s 失败: %s", pid, exc)
    return removed


def normalize_window_query(value: object) -> tuple[str, bool]:
    """Normalize a user-supplied window title query.

    A leading ``=`` prefix selects exact matching; otherwise substring
    (case-insensitive) matching is used.

    Args:
        value: Raw query string from the workflow step.

    Returns:
        A (normalized_text, exact) tuple.

    Raises:
        ValueError: If the query is empty or exceeds MAX_WINDOW_QUERY_LENGTH.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("窗口标题不能为空")
    if len(text) > MAX_WINDOW_QUERY_LENGTH:
        raise ValueError(f"窗口标题不能超过 {MAX_WINDOW_QUERY_LENGTH} 个字符")
    exact = text.startswith("=")
    return (text[1:].strip() if exact else text), exact


def _window_title(hwnd: int) -> str:
    return get_window_title(hwnd)


def find_window(query: object) -> int:
    """Find the first visible top-level window matching *query*.

    Args:
        query: Window title query (see normalize_window_query).

    Returns:
        The HWND of the first match, or 0 if no window matches.
    """
    expected, exact = normalize_window_query(query)
    folded = expected.casefold()
    matches: list[int] = []

    def callback(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _window_title(hwnd)
            if title and (
                title.casefold() == folded if exact else folded in title.casefold()
            ):
                matches.append(int(hwnd))
                return False
        except Exception as exc:
            logger.warning("枚举窗口回调失败: %s", exc, exc_info=True)
        return True

    enum_proc = WNDENUMPROC(callback)
    user32.EnumWindows(enum_proc, 0)
    return matches[0] if matches else 0


def window_title_at_point(x: int, y: int) -> str:
    """Return the title of the root window at the given screen coordinates.

    Args:
        x: Horizontal screen coordinate.
        y: Vertical screen coordinate.

    Returns:
        Stripped window title text.

    Raises:
        ValueError: If the window at the point has no usable title.
    """
    from .win32_api import GA_ROOT, POINT

    hwnd = user32.WindowFromPoint(POINT(int(x), int(y)))
    root_hwnd = user32.GetAncestor(hwnd, GA_ROOT) if hwnd else 0
    title = _window_title(root_hwnd or hwnd) if (root_hwnd or hwnd) else ""
    if not title.strip():
        raise ValueError("所选窗口没有可用标题，请改为手动输入窗口标题。")
    return title.strip()


def wait_for_window(
    query: object, timeout: float, stop_requested: Callable[[], bool] = lambda: False
) -> int:
    """Poll for a window matching *query* until found or timeout.

    Args:
        query: Window title query (see normalize_window_query).
        timeout: Maximum seconds to wait (0 to MAX_WINDOW_WAIT_SECONDS).
        stop_requested: Callable returning True to abort early.

    Returns:
        The HWND of the matched window, or 0 on timeout/cancellation.

    Raises:
        ValueError: If *timeout* is not a finite number in the valid range.
    """
    duration = float(timeout)
    if not math.isfinite(duration) or not 0.0 <= duration <= MAX_WINDOW_WAIT_SECONDS:
        raise ValueError(
            f"窗口等待时间必须是 0 到 {MAX_WINDOW_WAIT_SECONDS:g} 秒之间的有限数字"
        )
    deadline = time.monotonic() + duration
    while True:
        hwnd = find_window(query)
        if hwnd:
            return hwnd
        if stop_requested() or time.monotonic() >= deadline:
            return 0
        time.sleep(min(WINDOW_POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))


def activate_window(query: object) -> bool:
    """Restore and bring the first matching window to the foreground.

    Args:
        query: Window title query (see normalize_window_query).

    Returns:
        True if the window was successfully activated, False otherwise.
    """
    hwnd = find_window(query)
    if not hwnd:
        return False
    user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
    return bool(user32.SetForegroundWindow(wintypes.HWND(hwnd)))


def close_window(query: object) -> bool:
    """Post WM_CLOSE to the first matching window.

    Args:
        query: Window title query (see normalize_window_query).

    Returns:
        True if the message was posted, False if no window matched.
    """
    hwnd = find_window(query)
    return bool(hwnd and user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


def launch_application(command: object) -> int:
    """Start an external application and track its process.

    If *command* is an existing file path it is launched directly;
    otherwise it is passed to the shell for resolution.

    Args:
        command: Executable path or shell command string.

    Returns:
        The PID of the newly started process.

    Raises:
        ValueError: If the command is empty or exceeds 32768 characters.
    """
    text = str(command or "").strip()
    if not text:
        raise ValueError("程序路径或启动命令不能为空")
    if len(text) > 32_768:
        raise ValueError("启动命令过长")
    logger.debug("launch_application: %s", text)
    # FR-701-001: 独立 Runner 的 Job 守卫下，外部程序默认继承 Job，运行结束/退出时
    # 会被 KILL_ON_JOB_CLOSE 误杀；仅在父进程位于 Job 内时传 CREATE_BREAKAWAY_FROM_JOB
    # 使其脱离（该标志在无 Job 环境会让 CreateProcess 失败，故必须探测）。
    creationflags = subprocess.CREATE_BREAKAWAY_FROM_JOB if _in_job_object() else 0
    if os.path.isfile(text):
        process = subprocess.Popen(
            [os.path.abspath(text)], creationflags=creationflags
        )
    else:
        process = subprocess.Popen(text, creationflags=creationflags)
    pid = int(process.pid)
    with _LAUNCHED_LOCK:
        _LAUNCHED_PROCESSES[pid] = process
    return pid
