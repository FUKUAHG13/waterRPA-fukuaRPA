"""Bounded, content-free execution trace for offline diagnostics."""

from __future__ import annotations

import threading
import time
from collections import deque

DEFAULT_TRACE_LIMIT = 2000


class RunTrace:
    def __init__(self, max_events=DEFAULT_TRACE_LIMIT):
        self.max_events = max(100, int(max_events))
        self._lock = threading.RLock()
        self.reset()

    def reset(self, task_count=0):
        with self._lock:
            self._started_ns = time.perf_counter_ns()
            self._events = deque(maxlen=self.max_events)
            self._total_events = 0
            self._task_count = max(0, int(task_count))

    def record(
        self,
        event,
        *,
        loop=0,
        step=0,
        command="",
        status="",
        duration_ms=None,
        attempt=0,
        next_step=None,
        entry_reason=None,
        called_step=None,
    ):
        now_ns = time.perf_counter_ns()
        with self._lock:
            item = {
                "t_ms": round((now_ns - self._started_ns) / 1_000_000.0, 3),
                "event": str(event),
                "loop": max(0, int(loop or 0)),
                "step": max(0, int(step or 0)),
                "command": str(command or ""),
                "status": str(status or ""),
                "attempt": max(0, int(attempt or 0)),
            }
            if duration_ms is not None:
                item["duration_ms"] = round(max(0.0, float(duration_ms)), 3)
            if next_step is not None:
                item["next_step"] = max(0, int(next_step))
            if entry_reason is not None:
                item["entry_reason"] = str(entry_reason or "")
            if called_step is not None:
                item["called_step"] = max(0, int(called_step or 0))
            self._total_events += 1
            self._events.append(item)

    def snapshot(self):
        with self._lock:
            events = list(self._events)
            return {
                "task_count": self._task_count,
                "max_events": self.max_events,
                "total_events": self._total_events,
                "dropped_events": max(0, self._total_events - len(events)),
                "events": events,
            }
