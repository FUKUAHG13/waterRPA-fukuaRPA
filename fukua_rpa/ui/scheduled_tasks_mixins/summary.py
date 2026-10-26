# pyright: reportAttributeAccessIssue=false
"""Summary-panel Mixin for ScheduledTasksWindow (God Class split phase 1).

Groups the live summary refresh methods. Delegates to ``self._service`` and
writes into ``self._summary`` widgets owned by the window. No state of its own.
"""

from __future__ import annotations

from ..controllers.scheduled_tasks_controller import (
    format_epoch,
    format_eta,
    format_uptime,
)
from ._timefmt import _fmt_local, _now_local


class SummaryMixin:
    """Live service-summary refresh (4 methods)."""

    def _update_summary(self) -> None:
        s = self._service.summary()
        status = s.get("service_status", "paused")
        self._summary["service"].setText("运行中" if status == "running" else "已暂停")
        self._summary["uptime"].setText(format_uptime(s.get("uptime_seconds", 0)))
        self._summary["counts"].setText(
            f"{s.get('total', 0)} / {s.get('enabled', 0)} / {s.get('errors', 0)}"
        )
        self._summary["last_check"].setText(format_epoch(s.get("last_check_ts", 0)))

        next_task = s.get("next_task")
        if next_task and next_task.get("due_local") is not None:
            self._summary["next"].setText(
                f"{next_task.get('name', '')} @ {_fmt_local(next_task['due_local'])}"
            )
        else:
            self._summary["next"].setText("无")

        current = s.get("current_running")
        self._summary["running"].setText(current.get("name", "无") if current else "无")

    def _tick_eta(self) -> None:
        """Update ONLY the ETA text — never the table or the whole panel."""
        s = self._service.summary()
        next_task = s.get("next_task")
        if not next_task or next_task.get("due_local") is None:
            self._summary["eta"].setText("—")
            return
        delta = (next_task["due_local"] - _now_local()).total_seconds()
        self._summary["eta"].setText(format_eta(delta))

    def _tick_live_fields(self) -> None:
        """Refresh only second-level text without resetting the task table."""

        self._summary["uptime"].setText(
            format_uptime(self._service.uptime_seconds())
        )
        self._tick_eta()
        self._controller.model().refresh_running_durations()

    def _refresh_all(self) -> None:
        """Explicit refresh updates both the model and the summary panel."""

        self._controller.refresh()
        self._update_summary()
        self._tick_eta()
