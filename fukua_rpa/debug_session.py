"""Thread-safe breakpoint and step-over coordination for one engine run."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DebugSnapshot:
    """Immutable point-in-time view of the debug session state.

    Returned by :meth:`DebugSession.snapshot` for safe cross-thread inspection
    without holding the session lock.
    """

    active: bool
    paused: bool
    pause_requested: bool
    pause_before_next: bool
    step: int
    loop: int
    command: str
    reason: str
    detail: str


class DebugSession:
    """Coordinate breakpoint pauses, step-over and continue for one engine run.

    All public methods are thread-safe.  The engine thread calls
    :meth:`before_step` at each step boundary; the UI thread calls
    :meth:`request_pause`, :meth:`continue_run` and :meth:`step_over`
    to control execution flow.
    """

    def __init__(self) -> None:
        """Create an inactive debug session with all flags cleared."""
        self._condition = threading.Condition(threading.RLock())
        self._active = False
        self._paused = False
        self._pause_requested = False
        self._pause_before_next = False
        self._cancelled = False
        self._step = 0
        self._loop = 0
        self._command = ""
        self._reason = ""
        self._detail = ""

    def reset(self) -> None:
        """Activate the session for a new run, clearing all pause state.

        Wakes any threads waiting in :meth:`before_step` so they can
        re-evaluate the new active state.
        """
        with self._condition:
            self._active = True
            self._paused = False
            self._pause_requested = False
            self._pause_before_next = False
            self._cancelled = False
            self._step = 0
            self._loop = 0
            self._command = ""
            self._reason = ""
            self._detail = ""
            self._condition.notify_all()

    def finish(self) -> None:
        """Deactivate the session at normal run completion.

        Releases any thread blocked in :meth:`before_step`.
        """
        with self._condition:
            self._active = False
            self._paused = False
            self._pause_requested = False
            self._pause_before_next = False
            self._condition.notify_all()

    def cancel(self) -> None:
        """Abort the session, releasing any paused engine thread.

        Sets the cancelled flag so that :meth:`before_step` returns
        ``allowed=False`` on the next check.
        """
        with self._condition:
            self._cancelled = True
            self._active = False
            self._paused = False
            self._condition.notify_all()

    def request_pause(self) -> bool:
        """Request the engine to pause at the next step boundary.

        Returns:
            True if the pause request was accepted (session active, not
            already paused or cancelled); False otherwise.
        """
        with self._condition:
            if not self._active or self._cancelled or self._paused:
                return False
            self._pause_requested = True
            return True

    def continue_run(self) -> bool:
        """Resume execution after a pause, running freely until next pause.

        Clears the step-over flag so the engine will not re-pause at the
        following step boundary.

        Returns:
            True if the session was paused and is now resumed; False otherwise.
        """
        with self._condition:
            if not self._active or not self._paused:
                return False
            self._pause_before_next = False
            self._paused = False
            self._condition.notify_all()
            return True

    def step_over(self) -> bool:
        """Resume execution for exactly one step, then re-pause.

        Sets the ``pause_before_next`` flag so the engine pauses again
        at the subsequent step boundary.

        Returns:
            True if the session was paused and step-over was armed; False
            otherwise.
        """
        with self._condition:
            if not self._active or not self._paused:
                return False
            self._pause_before_next = True
            self._paused = False
            self._condition.notify_all()
            return True

    def before_step(
        self,
        *,
        step: int,
        loop: int,
        command: str,
        breakpoint: bool,
        breakpoint_reason: str = "breakpoint",
        breakpoint_detail: str = "",
        stop_requested: Callable[[], bool] | None = None,
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[bool, float]:
        """Engine-side checkpoint called before each step executes.

        Evaluates pause conditions (user pause request, step-over flag,
        breakpoint hit) and blocks the calling thread while paused.  Emits
        ``paused`` and ``resumed``/``cancelled`` events via *callback*.

        Args:
            step: Zero-based index of the current step.
            loop: Current loop iteration count.
            command: Action command name for display.
            breakpoint: Whether a breakpoint is set on this step.
            breakpoint_reason: Short label for the breakpoint type.
            breakpoint_detail: Extended detail (truncated to 500 chars).
            stop_requested: Optional callable checked during the pause wait;
                returning True aborts the session.
            callback: Optional callable receiving state-change event dicts.

        Returns:
            A tuple ``(allowed, duration)`` where *allowed* is True if
            execution may proceed and *duration* is the seconds spent paused.
        """
        with self._condition:
            if not self._active or self._cancelled:
                return not self._cancelled, 0.0
            reason = ""
            if self._pause_requested:
                reason = "pause_requested"
                self._pause_requested = False
            elif self._pause_before_next:
                reason = "step_over"
                self._pause_before_next = False
            elif breakpoint:
                reason = str(breakpoint_reason or "breakpoint")
            if not reason:
                return True, 0.0
            self._paused = True
            self._step = max(0, int(step))
            self._loop = max(0, int(loop))
            self._command = str(command or "")
            self._reason = reason
            self._detail = str(breakpoint_detail or "")[:500]
            paused_event: dict[str, Any] = {
                "state": "paused",
                "step": self._step,
                "loop": self._loop,
                "command": self._command,
                "reason": reason,
                "detail": self._detail,
            }

        self._emit(callback, paused_event)
        started = time.monotonic()
        with self._condition:
            while self._paused and self._active and not self._cancelled:
                if stop_requested and stop_requested():
                    self._cancelled = True
                    self._active = False
                    self._paused = False
                    break
                self._condition.wait(0.1)
            allowed = self._active and not self._cancelled
            duration = max(0.0, time.monotonic() - started)
            resumed_event: dict[str, Any] = {
                "state": "resumed" if allowed else "cancelled",
                "step": self._step,
                "loop": self._loop,
                "command": self._command,
                "reason": self._reason,
                "detail": self._detail,
                "paused_ms": duration * 1000.0,
            }
            if not self._paused:
                self._reason = ""
                self._detail = ""
        self._emit(callback, resumed_event)
        return allowed, duration

    def snapshot(self) -> DebugSnapshot:
        """Return an immutable snapshot of the current session state.

        Safe to call from any thread; the returned dataclass will not
        change even if the session is subsequently modified.
        """
        with self._condition:
            return DebugSnapshot(
                active=self._active,
                paused=self._paused,
                pause_requested=self._pause_requested,
                pause_before_next=self._pause_before_next,
                step=self._step,
                loop=self._loop,
                command=self._command,
                reason=self._reason,
                detail=self._detail,
            )

    def as_dict(self) -> dict[str, Any]:
        """Serialize the current snapshot as a plain dict for UI transport.

        Returns:
            Dictionary with the same fields as :class:`DebugSnapshot`.
        """
        return asdict(self.snapshot())

    @staticmethod
    def _emit(
        callback: Callable[[dict[str, Any]], None] | None, payload: dict[str, Any]
    ) -> None:
        """Invoke the state-change callback with a defensive copy of *payload*.

        Swallows and logs any exception raised by the callback so a faulty
        UI subscriber can never break the engine's pause/resume coordination.
        """
        if not callback:
            return
        try:
            callback(dict(payload))
        except Exception as exc:
            logger.warning("调试会话回调失败: %s", exc, exc_info=True)
