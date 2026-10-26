"""Worker thread bridge and emergency-stop watchdog."""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QThread, Signal

from .log_policy import (
    LOG_CRITICAL,
    LOG_RUN,
)
from .logging_service import GLOBAL_CONFIG, write_log
from .privacy import private_error_text, safe_error_name, safe_exception_report
from .pyautogui_runtime import pyautogui
from .win32_api import GetAsyncKeyState, get_window_title

if TYPE_CHECKING:
    from .engine.core import RPAEngine

logger = logging.getLogger(__name__)

# P2-4: GetForegroundWindow 在 64 位 Windows 上返回 64 位 HWND。ctypes 默认
# restype 为 c_int（32 位有符号），会截断句柄，导致"任务管理器前台"急停检测
# 静默失效。在导入时声明正确返回类型，使所有调用方都拿到完整 HWND。
ctypes.windll.user32.GetForegroundWindow.restype = ctypes.c_void_p

# 右上角急停热区边距（像素）。注意：pyautogui.position()/size() 返回的是
# 逻辑坐标，因此该阈值在逻辑坐标系下生效，高 DPI 缩放下无需额外换算。
_CORNER_STOP_MARGIN_PX = 10

# Note: Watchdog reads engine attributes directly (enable_key_stop, etc.)
# without snapshot. This is intentional: the watchdog runs in a daemon thread
# and only reads boolean flags that change monotonically during a run.
class FailsafeWatchdog(threading.Thread):
    """Daemon thread that polls emergency-stop conditions and halts the engine.

    Monitors three independent detectors (hotkey, screen-corner, task-manager
    foreground) in a tight loop.  When any enabled detector fires, the watchdog
    triggers an engine stop and terminates itself.
    """

    def __init__(self, engine: RPAEngine, log_context: dict | None = None) -> None:
        """Initialize the watchdog bound to *engine*.

        Args:
            engine: The execution engine instance exposing ``enable_key_stop``,
                ``enable_tr_stop``, ``enable_tm_stop``, ``stop_requested``,
                ``log()`` and ``stop()`` attributes/methods.
            log_context: Explicit run-scoped fields copied onto watchdog logs.
        """
        super().__init__(daemon=True, name="fukuaRPA-failsafe")
        self.engine = engine
        self.log_context = dict(log_context or {})
        self.stop_event = threading.Event()
        self._next_task_manager_check = 0.0
        self._retry_after: dict[str, float] = {}
        self._screen_size: tuple[int, int] | None = None
        # FR-20260809-D1-②: detectors 提升为实例属性——修复前每个轮询迭代（20ms）
        # 重建元组；引擎标志在运行期间单调变化，启动时快照语义一致。
        self._detectors = (
            ("按键急停", self.engine.enable_key_stop, self._key_stop_reason),
            ("右上角急停", self.engine.enable_tr_stop, self._corner_stop_reason),
            (
                "任务管理器急停",
                self.engine.enable_tm_stop,
                self._task_manager_stop_reason,
            ),
        )
        self._last_error_log: dict[str, float] = {}

    def _key_stop_reason(self) -> str | None:
        """Return a hotkey stop reason if ESC or middle mouse is held down."""
        if GetAsyncKeyState(0x1B) & 0x8000:
            return "用户按下了【ESC键】"
        if GetAsyncKeyState(0x04) & 0x8000:
            return "用户按下了【鼠标中键】"
        return None

    def _corner_stop_reason(self) -> str | None:
        """Return a stop reason if the pointer sits in the top-right hot corner."""
        x, y = pyautogui.position()
        if self._screen_size is None:
            # FR-20260809-D1-②: 屏幕尺寸运行期不变，首次查询后缓存（修复前每帧
            # 50Hz 轮询调用 pyautogui.size() 引入无关系统调用）。
            self._screen_size = pyautogui.size()
        width, _height = self._screen_size
        if x > (width - _CORNER_STOP_MARGIN_PX) and y < _CORNER_STOP_MARGIN_PX:
            return "检测到鼠标【右上角急停】"
        return None

    def _task_manager_stop_reason(self) -> str | None:
        """Return a stop reason if Task Manager is the foreground window.

        Checks are throttled to at most once every 100 ms to keep the polling
        loop cheap.
        """
        now = time.monotonic()
        if now < self._next_task_manager_check:
            return None
        self._next_task_manager_check = now + 0.1
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        title = get_window_title(hwnd)
        if not title:
            return None
        if "任务管理器" in title or "Task Manager" in title:
            return "检测到【任务管理器】前台"
        return None

    def _run_detector(self, name: str, detector: Callable[[], str | None]) -> bool:
        """Run one detector, rate-limiting retries and error logging.

        Args:
            name: Detector label used in log messages and retry bookkeeping.
            detector: Zero-arg callable returning a stop reason or None.

        Returns:
            True if the detector fired and a stop was triggered.
        """
        now = time.monotonic()
        if now < self._retry_after.get(name, 0.0):
            return False
        try:
            reason = detector()
        except Exception as error:
            self._retry_after[name] = now + 1.0
            if now >= self._last_error_log.get(name, 0.0) + 5.0:
                self._last_error_log[name] = now
                write_log(
                    f"看门狗{name}异常，其他急停检测继续运行: "
                    f"{safe_error_name(error)}",
                    private_message=(
                        f"看门狗{name}异常，其他急停检测继续运行: "
                        f"{private_error_text(error)}"
                    ),
                    context=self.log_context,
                             category=LOG_RUN,
                             critical=True,
                         )
            return False
        if not reason:
            return False
        self.trigger_stop(reason)
        return True

    def run(self) -> None:
        """Poll all enabled detectors in a loop until stop or trigger.

        Each iteration checks every detector whose engine flag is enabled.
        If a detector fires, ``trigger_stop`` is called and the loop exits.
        The loop sleeps 20 ms between iterations to limit CPU usage.
        """
        write_log(">>> 看门狗线程启动", context=self.log_context,
                     category=LOG_RUN,
                 )
        while not self.stop_event.is_set():
            for name, enabled, detector in self._detectors:
                if enabled and self._run_detector(name, detector):
                    return
            self.stop_event.wait(0.02)

    def trigger_stop(self, reason: str) -> None:
        """Signal the engine to stop and alert the user.

        Logs the stop reason at critical level, calls ``engine.stop()``,
        and plays a system beep.  No-op if a stop was already requested.

        Args:
            reason: Human-readable description of what triggered the stop.
        """
        if not self.engine.stop_requested:
            write_log(f">>> 看门狗触发: {reason}", context=self.log_context,
                         category=LOG_RUN,
                         critical=True,
                     )
            self.engine.log(
                f"<font color='red'><b>!!! {reason} -> 停止 !!!</b></font>",
                LOG_CRITICAL,
                critical=True,
            )
            self.engine.stop()
            try:
                ctypes.windll.user32.MessageBeep(0xFFFFFFFF)
            except Exception as exc:
                logger.warning("播放提示音失败: %s", exc, exc_info=True)

    def kill(self) -> None:
        """Signal the watchdog polling loop to exit at the next iteration."""
        self.stop_event.set()

class WorkerThread(QThread):
    """QThread that bridges the execution engine to Qt signals.

    Runs the engine's ``run_tasks`` in a dedicated thread, forwarding log,
    status, click, debug and prompt events to the UI via Qt signals.  A
    :class:`FailsafeWatchdog` is started alongside and cleaned up on exit.
    """

    log_signal = Signal(str)
    status_signal = Signal(dict)
    click_signal = Signal(dict)
    debug_signal = Signal(dict)
    prompt_signal = Signal(object)
    error_signal = Signal(str)

    def __init__(
        self,
        engine: RPAEngine,
        tasks: list[dict[str, Any]],
        run_id: int,
        log_context: dict | None = None,
    ) -> None:
        """Create a worker thread for one execution run.

        Args:
            engine: The execution engine that implements ``run_tasks``.
            tasks: List of task dictionaries to execute.
            run_id: Unique identifier for this run, used for lifecycle checks.
            log_context: Explicit run-scoped fields copied onto engine logs.
        """
        super().__init__()
        self.engine = engine
        self.tasks = tasks
        self.run_id = run_id
        self.log_context = dict(log_context or {})
        self.watchdog: FailsafeWatchdog | None = None

    def run(self) -> None:
        """Execute tasks via the engine, guarded by a failsafe watchdog.

        Starts the watchdog, delegates to ``engine.run_tasks``, and ensures
        the watchdog is stopped and the run is finalized regardless of outcome.
        Unhandled exceptions are reported through ``error_signal``.
        """
        try:
            self.engine.run_log_context = dict(self.log_context)
            self.watchdog = FailsafeWatchdog(self.engine, self.log_context)
            self.watchdog.start()
            self.engine.run_tasks(
                self.tasks,
                self.log_callback,
                self.status_callback,
                self.click_callback,
                self.debug_callback,
                self.prompt_callback,
                self.run_id,
            )
        except Exception as error:
            details = safe_exception_report(type(error), error.__traceback__)
            write_log(
                f"WorkerThread 未处理异常: {details}",
                private_message=(
                    f"WorkerThread 未处理异常: {private_error_text(error)}\n{details}"
                ),
                context=self.log_context,
                         category=LOG_RUN,
                         critical=True,
                     )
            self.error_signal.emit(safe_error_name(error))
            self.engine.finish_run(self.run_id, "worker_error", details)
        finally:
            if self.watchdog:
                self.watchdog.kill()
                self.watchdog.join(timeout=1.0)
            if self.engine.active_run_matches(self.run_id):
                self.engine.finish_run(self.run_id)
            self.engine.run_log_context = {}

    def log_callback(self, msg: str) -> None:
        """Forward a log message to the UI if UI logging is enabled.

        Args:
            msg: Formatted log string from the engine.
        """
        if GLOBAL_CONFIG["log_to_ui"]:
            self.log_signal.emit(msg)

    def status_callback(self, data: dict[str, Any]) -> None:
        """Emit a status update to the UI.

        Args:
            data: Status payload (e.g. current step index, progress).
        """
        self.status_signal.emit(data)

    def click_callback(self, data: dict[str, Any]) -> None:
        """Emit a click-event notification to the UI.

        Args:
            data: Click coordinates and metadata for visual feedback.
        """
        self.click_signal.emit(data)

    def debug_callback(self, data: dict[str, Any]) -> None:
        """Emit a debug-session event to the UI.

        Args:
            data: Debug state payload (paused, resumed, cancelled, etc.).
        """
        self.debug_signal.emit(data)

    def prompt_callback(self, request: object) -> None:
        """Emit an interactive prompt request to the UI.

        Args:
            request: Prompt request object for user input during execution.
        """
        self.prompt_signal.emit(request)
