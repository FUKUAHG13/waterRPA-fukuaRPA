"""Startup missed-trigger detection for scheduled tasks.

Extracted from :class:`~fukua_rpa.scheduled_task_service.ScheduledTaskService`
so the missed-policy decision table (skip / ask / catchup_once) can be driven
in isolation, without standing up the timer, the coordinator proxy or the Qt
signal plumbing.

The detector owns the one-shot per-session guard and the calendar walk. Every
side effect (history bookkeeping, the ``missed_detected`` signal, coordinator
run requests) leaves through an injected callback, so this module never imports
the service and has no opinion about how a run is actually launched.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .scheduled_task_model import (
    CONFLICT_CATCHUP_ONCE,
    MISSED_ASK,
    MISSED_CATCHUP_ONCE,
    MISSED_SKIP,
    ScheduledTask,
    ScheduledTaskClock,
    TriggerKind,
    next_due_local,
)

# Bounded walk: a tiny recurrence interval combined with a long offline gap
# could otherwise iterate without limit; 100k steps is far beyond any realistic
# case (a 1-minute task offline for ~70 days).
_MAX_CALENDAR_STEPS = 100_000


@dataclass(frozen=True)
class MissedRunCallbacks:
    """Side-effect seams the owner (ScheduledTaskService) provides.

    Attributes:
        record_miss: Append a "missed" history record (task, due_utc, reason);
            accepts an optional ``outcome`` keyword (see
            ``ScheduledTaskService._record_miss``).
        record_miss_placeholder: Append a pending-ask placeholder history record.
        record_run_start: Remember the due time of a run that just started.
        emit_missed_detected: Notify the UI (schedule_id, task_name).
        request_due: Ask the coordinator to run a task; returns the coordinator's
            result string ("running" / "pending" / "skipped" / "failed").
    """

    record_miss: Callable[..., None]
    record_miss_placeholder: Callable[[ScheduledTask, datetime, str], None]
    record_run_start: Callable[[ScheduledTask, datetime], None]
    emit_missed_detected: Callable[[str, str], None]
    request_due: Callable[..., str]


class MissedRunDetector:
    """One-shot startup scan for triggers that elapsed while the app was closed.

    INVARIANT: :meth:`check` performs at most one calendar scan per session.
    ``ScheduledTaskService._on_heartbeat`` deliberately does *not* call it on
    sleep/wake — it re-runs ``_evaluate_due`` instead (D-07).
    """

    def __init__(
        self,
        clock_provider: Callable[[], ScheduledTaskClock],
        store_provider: Callable[[], object],
        tasks_path_provider: Callable[[], str],
        callbacks: MissedRunCallbacks,
    ):
        """Wire the detector to its data sources and side-effect callbacks.

        INVARIANT: clock, store and tasks path are read through providers, never
        cached. The owning service stays the single source of truth for them; a
        cached copy here would silently desync if the owner ever re-pointed one
        (tests, for instance, swap in a fake clock after construction).

        Args:
            clock_provider: Returns the current local/UTC clock.
            store_provider: Returns the task store exposing ``enabled()`` and
                ``history_for()``.
            tasks_path_provider: Returns the path of scheduled_tasks.json; its
                creation time is the conservative baseline when a task has no
                history at all.
            callbacks: Side-effect seams, see :class:`MissedRunCallbacks`.
        """
        self._clock_provider = clock_provider
        self._store_provider = store_provider
        self._tasks_path_provider = tasks_path_provider
        self._callbacks = callbacks
        # One-shot guard: flipped by the first check() of the session.
        self.missed_checked = False

    # ------------------------------------------------------------------
    # missed-task detection (startup)
    # ------------------------------------------------------------------
    # DESIGN (ADR 0035 / 实施方案 §4.3): _evaluate_due only looks back 2 seconds
    # to detect tasks that come due while the app is running. A trigger whose
    # window fell while the app was closed, hibernating or locked therefore
    # never lands in that 2s window and would be silently lost. This startup
    # pass is the *separate* missed path: once per session it scans enabled
    # calendar tasks for a due point that lies between the last known activity
    # and "now", then applies the task's missed_policy:
    #   * skip         -> record a single miss (no execution)
    #   * catchup_once -> run exactly once via the coordinator (capacity-one
    #                     pending queue dedupes; we NEVER replay every missed
    #                     occurrence — 实施方案 #155 禁止逐次补齐)
    #   * ask          -> emit missed_detected so the UI asks the user
    # STARTUP tasks are excluded (they already fire on every launch).
    def check(self) -> None:
        """Run the one-shot missed-trigger pass (no-op after the first call)."""
        if self.missed_checked:
            return
        self.missed_checked = True
        now = self._clock_provider().now_local()
        for task in self._store_provider().enabled():
            if task.trigger.kind == TriggerKind.STARTUP:
                continue
            reference = self.reference_local(task)
            if reference is None or reference >= now:
                continue
            missed_due = self.latest_missed_due(task, reference, now)
            if missed_due is None:
                continue
            self.apply_policy(task, missed_due)

    def reference_local(self, task: ScheduledTask) -> datetime | None:
        """Local wall-clock time from which to start looking for missed runs.

        Prefers the last history record's end time (a recorded miss falls back
        to its due time). With no history at all we use the store file's
        creation time as a conservative baseline so a freshly installed task is
        not retroactively treated as having missed years of runs.
        """
        hist = self._store_provider().history_for(task.schedule_id)
        for record in reversed(hist):
            anchor = record.ended_at_utc or record.due_at_utc
            if anchor is not None:
                return self._clock_provider().from_utc(anchor)
        # FR-20260812-D6-002: 优先用任务自身 created_at_utc（真实创建时刻）。
        # 文件 ctime 会随每次原子写刷新而变化（任务增删改立即保存），既可能
        # 早于任务创建（误报"错过"）也可能晚于任务创建（反向漏检）；用
        # created_at_utc 两方向都消除。仅当任务无创建时间（旧数据）时回退
        # 文件 ctime 作为保守基线。
        if task.created_at_utc is not None:
            return self._clock_provider().from_utc(task.created_at_utc)
        try:
            ctime = os.path.getctime(self._tasks_path_provider())
        except OSError:
            return None
        if ctime <= 0:
            return None
        return datetime.fromtimestamp(ctime)

    def latest_missed_due(
        self, task: ScheduledTask, reference: datetime, now: datetime
    ) -> datetime | None:
        """The most recent trigger point in (reference, now], or None.

        Walks the calendar forward from ``reference`` and keeps the last
        occurrence that is still <= now. Only that single, latest missed point
        is ever acted upon — earlier missed occurrences are intentionally
        collapsed so catch-up runs at most once (实施方案 #155).
        """
        latest: datetime | None = None
        cursor = reference
        clock = self._clock_provider()
        # Bounded walk: a tiny interval plus a long offline gap could otherwise
        # iterate without limit; 100k steps is far beyond any realistic case.
        for _ in range(_MAX_CALENDAR_STEPS):
            nxt = next_due_local(task, cursor, clock)
            if nxt is None or nxt > now:
                break
            latest = nxt
            cursor = nxt + timedelta(seconds=1)
        return latest

    def apply_policy(self, task: ScheduledTask, missed_due_local: datetime) -> None:
        """Apply ``task.missed_policy`` to a single detected missed trigger."""
        due_utc = self._clock_provider().to_utc(missed_due_local)
        policy = task.missed_policy
        if policy == MISSED_SKIP:
            self._callbacks.record_miss(task, due_utc, "missed_while_closed")
            return
        if policy == MISSED_ASK:
            # The decision belongs to the user; the UI layer owns the dialog and
            # may trigger a normal run afterwards. The service never auto-runs
            # an "ask" task behind the user's back.
            self._callbacks.emit_missed_detected(task.schedule_id, task.name)
            # Write a placeholder history so _evaluate_due's has_handled check
            # recognizes this trigger as awaiting the user's decision and does
            # not auto-run it (e.g. a SINGLE task whose once_at has passed)
            # before the user responds.
            self._callbacks.record_miss_placeholder(task, due_utc, "missed_ask_pending")
            return
        if policy == MISSED_CATCHUP_ONCE:
            # The coordinator only enqueues a busy request when conflict_policy
            # is catchup_once. A *missed* catch-up must not be dropped just
            # because the runtime conflict policy is "skip", so for this single
            # request we override the conflict policy via a parameter. The shared
            # Store task object is never mutated (P3-8).
            result = self._callbacks.request_due(
                task, due_utc, conflict_policy_override=CONFLICT_CATCHUP_ONCE
            )
            if result == "running":
                self._callbacks.record_run_start(task, due_utc)
            elif result == "pending":
                # FR-20261008-503: the symmetric ``_evaluate_due`` path records a
                # ``pending_catchup`` outcome when the coordinator is busy, so the
                # trigger enters history and is de-duplicated on the next scan.
                # This startup/sleep-wake scan path was left recording nothing, so
                # the same due point was re-requested on every later session (the
                # capacity-one pending queue is cleared on shutdown). Record the
                # same outcome here to close the asymmetry.
                self._callbacks.record_miss(
                    task, due_utc, "catchup_pending_while_busy", outcome="pending_catchup"
                )
            elif result == "failed":
                # FR-20261008-901: a rejected catch-up must still leave a
                # traceable record. ``_evaluate_due`` records the same
                # ``startup_failed``/``outcome="failed"`` pair for this result;
                # staying silent here lost the missed trigger entirely (no
                # history, no plan-log entry) and re-requested the same due
                # point on every later wake-up. Note: with the catchup_once
                # override the coordinator answers "pending" (never
                # "skipped") while busy, so "skipped" is unreachable here.
                self._callbacks.record_miss(
                    task, due_utc, "startup_failed", outcome="failed"
                )
            return
        # Unknown policy value: fail safe to "skip" semantics.
        self._callbacks.record_miss(task, due_utc, "missed_while_closed")
