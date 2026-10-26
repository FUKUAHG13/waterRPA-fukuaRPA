"""Bounded delayed-action scheduler used by click supplements."""

from __future__ import annotations

import heapq
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuxiliaryClickRequest:
    """Immutable description of one delayed supplementary click.

    Instances are queued on :class:`AuxiliaryClickScheduler` and later passed
    verbatim to the scheduler's callback; the scheduler never inspects the
    payload itself.

    Attributes:
        kind: Discriminator selecting the click strategy. Current values are
            ``"delayed_second_click"`` (re-click without re-recognition) and
            ``"image_confirm_click"`` (re-verify the template before clicking).
        x: Absolute screen X coordinate of the click target, in pixels.
        y: Absolute screen Y coordinate of the click target, in pixels.
        button: Mouse button to press, e.g. ``"left"`` or ``"right"``.
        label: Human-readable step label used for logging/diagnostics only.
        binding: Optional read-only window/control binding the click should be
            scoped to; ``None`` means a plain screen-space click.
        verification: Optional read-only template-verification parameters used
            by the ``"image_confirm_click"`` strategy; ``None`` for strategies
            that do not re-recognize.
        trail_enabled: Optional per-step visual-trail override inherited from
            the originating step.
    """

    kind: str
    x: float
    y: float
    button: str
    label: str
    binding: Mapping[str, Any] | None = None
    verification: Mapping[str, Any] | None = None
    trail_enabled: bool | None = None


@dataclass(order=True)
class _ScheduledRequest:
    due_at: float
    sequence: int
    request: AuxiliaryClickRequest = field(compare=False)


class AuxiliaryClickScheduler:
    """Execute delayed click requests on one daemon thread with prompt cancellation.

    Supplementary clicks ("delayed second click" and "image confirm click")
    must never block the main run loop and must be cancellable when the script
    stops, so this scheduler keeps a single bounded priority queue serviced by
    one daemon thread. Requests are fired in due-time order; closing the
    scheduler can drop every still-pending request immediately.
    """

    def __init__(
        self,
        callback: Callable[[AuxiliaryClickRequest], None],
        *,
        error_callback: Callable[[BaseException], None] | None = None,
        max_pending: int = 256,
        name: str = "fukuaRPA-auxiliary-clicks",
    ) -> None:
        """Start the worker thread.

        Args:
            callback: Invoked on the worker thread for each due request.
            error_callback: Optional sink for exceptions raised by *callback*;
                when omitted, callback errors are swallowed silently.
            max_pending: Upper bound on queued requests; further ``schedule``
                calls fail until the queue drains. Values below 1 are clamped.
            name: Thread name used for diagnostics.
        """
        self._callback = callback
        self._error_callback = error_callback
        self._max_pending = max(1, int(max_pending))
        self._condition = threading.Condition()
        self._queue: list[_ScheduledRequest] = []
        self._sequence = 0
        self._active = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def pending_count(self) -> int:
        """Return the number of queued plus in-flight requests."""
        with self._condition:
            return len(self._queue) + self._active

    def schedule(self, request: AuxiliaryClickRequest, delay_seconds: float) -> bool:
        """Queue *request* for execution after *delay_seconds*.

        Args:
            request: Payload passed unchanged to the worker callback.
            delay_seconds: Non-negative delay measured with the monotonic
                clock; ``0`` fires on the next worker wake-up.

        Returns:
            ``True`` when the request was accepted, ``False`` when the delay
            is non-finite/negative, the scheduler is closed, or the queue is
            already at ``max_pending``.
        """
        try:
            delay = float(delay_seconds)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(delay) or delay < 0.0:
            return False
        with self._condition:
            if self._closed or len(self._queue) >= self._max_pending:
                return False
            self._sequence += 1
            heapq.heappush(
                self._queue,
                _ScheduledRequest(time.monotonic() + delay, self._sequence, request),
            )
            self._condition.notify_all()
            return True

    def wait_until_idle(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        poll_seconds: float = 0.05,
    ) -> bool:
        """Block until every queued and in-flight request has settled.

        Args:
            stop_requested: Optional cancellation predicate polled on each
                wake-up; returning ``True`` aborts the wait.
            poll_seconds: Maximum time spent waiting on the condition between
                cancellation checks; clamped to at least 10 ms.

        Returns:
            ``True`` when the scheduler became idle, ``False`` when the wait
            was aborted by *stop_requested* or by the scheduler closing.
        """
        with self._condition:
            while self._queue or self._active:
                if self._closed or (stop_requested and stop_requested()):
                    return False
                self._condition.wait(max(0.01, float(poll_seconds)))
            return True

    def close(self, *, cancel_pending: bool = True, wait: bool = True) -> None:
        """Shut the scheduler down and optionally drop pending requests.

        Args:
            cancel_pending: When ``True`` (default) every still-queued request
                is discarded; an in-flight callback is always allowed to finish.
            wait: When ``True`` (default) join the worker thread for up to one
                second. Joining is skipped automatically when called from the
                worker thread itself to avoid a self-deadlock.
        """
        with self._condition:
            if cancel_pending:
                self._queue.clear()
            self._closed = True
            self._condition.notify_all()
        if wait and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)

    cancel = close

    def _run(self) -> None:
        while True:
            with self._condition:
                while True:
                    if self._closed:
                        return
                    if not self._queue:
                        self._condition.wait()
                        continue
                    remaining = self._queue[0].due_at - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(remaining)
                        continue
                    scheduled = heapq.heappop(self._queue)
                    self._active += 1
                    break
            try:
                self._callback(scheduled.request)
            except BaseException as error:
                if self._error_callback is not None:
                    try:
                        self._error_callback(error)
                    except Exception as exc:
                        logger.warning("辅助点击错误回调失败: %s", exc, exc_info=True)
            finally:
                with self._condition:
                    self._active = max(0, self._active - 1)
                    self._condition.notify_all()
