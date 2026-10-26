"""Coordinates actual execution of due scheduled tasks.

Design constraints honoured here (design §3.2 / §4.3 / §4.5 / §10):
* Only ONE script may control the mouse/keyboard at a time. We reuse the
  existing ``RunController`` single-active semantics as the mutex — no second
  Worker, engine or stop state is created here.
* When a script is already running at a task's due time: ``skip`` records the
  miss; ``catchup_once`` remembers the task in a CAPACITY-ONE pending map (one
  entry per schedule id) so it is run at most once after the current run ends.
* We never simulate a UI button click. Launch goes through
  ``ExecutionController.prepare_and_launch_profile`` which reuses the existing
  run path.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

logger = logging.getLogger(__name__)
from .scheduled_task_model import (  # noqa: E402
    CONFLICT_CATCHUP_ONCE,
    ScheduledTask,
    new_schedule_id,
)

# DESIGN: fallback watchdog budget when a task does not declare
# max_runtime_seconds. Generous enough not to kill legitimately long runs, but
# bounded so a deadlocked worker cannot wedge the single-run mutex forever.
DEFAULT_WATCHDOG_SECONDS = 3600


class ScheduledRunCoordinator(QObject):
    """Own the single-run mutex and capacity-one catch-up queue.

    Ensures at most one scheduled script controls input at any time. When a
    due task arrives while another is running, the conflict policy determines
    whether the task is skipped or queued for a single catch-up run.

    Thread-affinity contract (R1): every public entry point (``request_due``)
    and every slot connected to cross-thread signals (``_on_run_ended`` from the
    run controller, ``_on_watchdog_timeout`` from the ``QTimer``) must run on
    *this* coordinator's Qt thread. ``_active`` / ``_pending`` are accessed and
    mutated without an explicit lock because the contract guarantees single-
    threaded access. The coordinator must be created and live on the Qt thread
    that owns the application event loop; violating this contract (e.g. calling
    ``request_due`` from a worker thread) is a latent race and is surfaced by
    :meth:`_check_thread_affinity` rather than silently accepted.
    """

    # FR-20260928-002: run_started 携带 due_at_utc（补跑排空与 ask-run_now
    # 路径不经 service._record_run_start，若不随信号传递，历史 due_at_utc
    # 恒为 None，错过检测失去原定到期锚点）。
    run_started = Signal(str, str, object)  # schedule_id, run_id, due_at_utc
    run_finished = Signal(str, str, str)  # schedule_id, run_id, outcome
    # Emitted when the watchdog fires because a run never reported completion
    # (worker deadlock / hung run). Carries the schedule_id and run_id that were
    # force-released so the UI can surface a warning. This is a last-resort
    # safety net — the normal path always clears _active via _on_run_ended.
    watchdog_timeout = Signal(str, str)  # schedule_id, run_id
    # FR-003: 补跑排空路径的启动失败此前完全无痕（不记账、不终止），条目会随
    # 每次 run_ended 静默重试。参数：(schedule_id, due_at_utc, engine_busy)。
    # engine_busy 为真表示引擎仍被占用（手动运行或看门狗强停后槽位未释放），
    # 属 P1-3 的临时忙，条目保留待重试且不记账；为假表示引擎空闲仍失败，属
    # 永久失败，调用方记账后条目已被清除。
    run_start_failed = Signal(str, object, bool)

    def __init__(
        self,
        run_controller: Any,
        execution_controller: Any,
        task_resolver: Callable[[str], ScheduledTask | None],
        parent: QObject | None = None,
    ) -> None:
        """Initialize the coordinator with run and execution controllers.

        Args:
            run_controller: Controller providing is_active and stop() for the
                single-run mutex.
            execution_controller: Controller providing
                prepare_and_launch_profile() to start a profile run.
            task_resolver: Callable that resolves a schedule_id to its
                ScheduledTask, or None if not found.
            parent: Optional Qt parent object.
        """
        super().__init__(parent)
        # Store the native Python thread identity instead of retaining/comparing
        # transient PySide QThread wrappers. The coordinator is never moved to a
        # different Qt thread after construction, so this is the stable form of
        # its documented single-thread ownership contract.
        self._owner_thread_ident = threading.get_ident()
        self._run_controller = run_controller
        self._execution_controller = execution_controller
        self._resolve = task_resolver
        self._active: tuple[str, str] | None = None  # (schedule_id, run_id)
        self._pending: dict[str, datetime] = {}  # schedule_id -> due_at_utc (capacity one)
        # Set when the watchdog force-released a run; lets _on_run_ended drain
        # the catch-up queue once the (now stopped) worker actually finishes,
        # instead of silently dropping the pending task (P1-3).
        self._recovering_id: str | None = None
        # R1: guard against cross-thread access of the unguarded _active/_pending
        # state. Set the first time a thread-affinity violation is observed so
        # the warning is emitted once instead of spamming the log.
        self._warned_affinity: bool = False

        # INVARIANT: the watchdog is a single-shot safety net that force-releases
        # _active if a run never reports completion (worker deadlock). It is only
        # ever armed while _active is set; the normal completion path stops it in
        # _on_run_ended before it can fire.
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._on_watchdog_timeout)

        # Design §4.9 / §7: the run controller reports end-of-run with metadata
        # so we can tell manual runs apart from scheduled ones.
        if hasattr(self._run_controller, "run_ended"):
            self._run_controller.run_ended.connect(self._on_run_ended)

    # ----- introspection -----
    def is_busy(self) -> bool:
        """Return True if a scheduled/manual run or the clicker is active.

        FR-20260809-1506: the standalone clicker holds the input ownership
        mutex (the launch boundary rejects scripts while it runs).  The
        coordinator must see that state too, otherwise a scheduled trigger
        firing while the clicker runs bypasses the conflict policy and is
        silently dropped by ``_launch`` (returns "failed" with no history).
        With this check the request enters conflict_policy: catch-up is
        enqueued, other policies record a miss.
        """
        if bool(self._active) or bool(self._run_controller.is_active):
            return True
        try:
            checker = getattr(
                self._execution_controller, "auto_clicker_is_running", None
            )
            return bool(checker and checker() is True)
        except Exception:
            # A broken/missing accessor must not stall the scheduler; treat it
            # as not-busy (the launch boundary still enforces the mutex).
            return False

    @property
    def active_schedule_id(self) -> str | None:
        """Return the schedule_id of the currently active run, or None."""
        return self._active[0] if self._active else None

    @property
    def pending_ids(self) -> set[str]:
        """Return the set of schedule_ids waiting in the catch-up queue."""
        return set(self._pending.keys())

    # ----- R1: thread-affinity contract guard -----
    def _check_thread_affinity(self) -> None:
        """Warn once if a mutating entry point runs off the coordinator's thread.

        ``_active`` / ``_pending`` are intentionally lock-free (see the class
        docstring); this guard verifies the documented single-thread contract is
        honoured. It is a development/diagnostic aid, not a runtime gate: a
        violation is logged (once) rather than raising, so a misbehaving caller
        in production degrades loudly but does not crash the scheduler.
        """
        if self._warned_affinity:
            return
        app = QCoreApplication.instance()
        if app is None:
            # No Qt application running (e.g. headless unit tests) — affinity is
            # meaningless here, so do not fabricate a warning.
            return
        if threading.get_ident() != self._owner_thread_ident:
            self._warned_affinity = True
            logger.warning(
                "ScheduledRunCoordinator 在所属 Qt 线程之外被调用（线程亲和性契约被破坏）；"
                "_active/_pending 的非线程安全访问可能导致竞态，请确认所有入口与信号槽均运行于同一线程。"
            )

    # ----- entry point from the service -----
    def request_due(self, task: ScheduledTask, due_at_utc: datetime, conflict_policy_override: str | None = None) -> str:
        """Ask to run ``task`` whose due time is ``due_at_utc``.

        ``conflict_policy_override`` lets a caller (e.g. the missed-task
        catch-up path) substitute the conflict policy for this single request
        without mutating the shared task object held by the Store (P3-8).

        Args:
            task: The scheduled task to run.
            due_at_utc: The UTC datetime at which the task became due.
            conflict_policy_override: Optional one-shot conflict policy that
                replaces the task's own policy for this request only.

        Returns:
            One of: ``"running"``, ``"pending"``, ``"skipped"``, ``"failed"``.
        """
        self._check_thread_affinity()
        if self.is_busy():
            policy = conflict_policy_override if conflict_policy_override is not None else task.conflict_policy
            if policy == CONFLICT_CATCHUP_ONCE:
                # Capacity one: keep only the latest due time per schedule id.
                self._pending[task.schedule_id] = due_at_utc
                return "pending"
            return "skipped"
        return self._launch(task, due_at_utc)

    # ----- launch -----
    def _launch(self, task: ScheduledTask, due_at_utc: datetime) -> str:
        """Launch a scheduled task run and arm the watchdog timer.

        Args:
            task: The scheduled task to launch.
            due_at_utc: The UTC datetime at which the task became due.

        Returns:
            ``"running"`` on success, ``"failed"`` if launch raised or returned
            a falsy result.
        """
        self._check_thread_affinity()
        run_id = new_schedule_id()
        self._active = (task.schedule_id, run_id)
        try:
            ok = self._execution_controller.prepare_and_launch_profile(
                task.profile_id, "scheduled", task.schedule_id, run_id, due_at_utc,
                max_runtime_seconds=task.max_runtime_seconds,
            )
        except Exception as exc:
            logger.warning("启动计划任务失败 (%s): %s", task.schedule_id, exc, exc_info=True)
            ok = False
        if not ok:
            self._active = None
            return "failed"
        # Arm the watchdog only after a confirmed launch. Budget comes from the
        # task's max_runtime_seconds when declared, else the default fallback.
        budget_s = task.max_runtime_seconds or DEFAULT_WATCHDOG_SECONDS
        self._watchdog.start(int(budget_s) * 1000)
        self.run_started.emit(task.schedule_id, run_id, due_at_utc)
        return "running"

    # ----- completion -----
    def _on_run_ended(self, meta: dict[str, Any]) -> None:
        """Handle the run-ended signal from the run controller.

        Releases the single-run mutex for scheduled runs and drains the catch-up
        queue. A manual run (no schedule_id) does not own the coordinator's
        mutex, but it does make :meth:`is_busy` true, so it can still have
        catch-up work queued behind it; its completion therefore drains the
        queue too. Log attribution belongs to the concrete Runner and is not
        stored as process-global state.

        Args:
            meta: Run completion metadata containing at least ``schedule_id``
                and optionally ``outcome``.
        """
        self._check_thread_affinity()
        schedule_id = meta.get("schedule_id") or ""
        if not schedule_id:
            # INF10: a manual run is not ours to track, but request_due() queues
            # CONFLICT_CATCHUP_ONCE tasks whenever is_busy() is true — and
            # is_busy() is true for manual runs as well (run_controller.is_active).
            # Without this drain the catch-up entry would sit until the next
            # *scheduled* completion or the watchdog, so a manual run followed by
            # no further schedule activity silently loses the catch-up.
            # INVARIANT: only drain when we own no active scheduled run. If
            # `_active` is set, that scheduled run is still in flight and
            # `_drain_pending` -> `_launch` would start a second concurrent run,
            # breaking the single-run mutex. `_recovering_id` is left untouched:
            # it belongs to the scheduled watchdog recovery path.
            if self._active is None and self._pending:
                self._drain_pending()
            return
        if self._active is None:
            # Either a watchdog-recovered run whose mutex was already
            # force-released (P1-3), or a stale/duplicate signal. Only drain the
            # catch-up queue when this completion belongs to the run we are
            # recovering from; otherwise ignore it.
            if self._recovering_id == schedule_id:
                self._recovering_id = None
                self._drain_pending()
            return
        if self._active[0] != schedule_id:
            return
        outcome = meta.get("outcome", "success")
        sid, rid = self._active
        # Normal completion path: disarm the watchdog before releasing the mutex
        # so it can never fire for a run that already finished cleanly.
        self._watchdog.stop()
        self._active = None
        self.run_finished.emit(sid, rid, outcome)
        # Tidy any stale recovery marker (e.g. a previous watchdog recovery
        # whose original worker finished after the catch-up run launched).
        self._recovering_id = None
        self._drain_pending()

    def _on_watchdog_timeout(self) -> None:
        """Last-resort recovery for a run that never reported completion.

        A deadlocked worker would otherwise leave ``_active`` set forever,
        making ``is_busy()`` permanently true and causing every subsequent due
        task to be skipped or stuck in the pending queue. Force-release the
        mutex, log a warning, notify the UI, then drain pending so queued
        catch-up work can proceed.
        """
        self._check_thread_affinity()
        if self._active is None:
            # Defensive: nothing to release (shouldn't happen — the timer is
            # stopped on the normal path).
            return
        sid, rid = self._active
        import logging
        logging.getLogger(__name__).warning(
            "Scheduled run watchdog timeout: schedule_id=%s run_id=%s never "
            "reported completion; force-releasing the run mutex.",
            sid, rid,
        )
        self._active = None
        # Mark the recovery BEFORE releasing the slot (P1-3): if stop()
        # synchronously fires run_ended, the recovery drain must not be skipped
        # while `_recovering_id` is still None.
        self._recovering_id = sid
        # P2-2: actually release the real execution slot. Clearing `_active`
        # alone only frees the logical mutex; the worker/engine keeps running
        # and `run_controller.is_active` stays True, so `is_busy()` remains True
        # and the catch-up queue can never launch. Stopping the run frees the
        # slot so the pending task can proceed once the worker finishes.
        self._run_controller.stop()
        self.watchdog_timeout.emit(sid, rid)
        self._drain_pending()

    def _drain_pending(self) -> None:
        """Launch at most one pending task from the catch-up queue.

        Picks the earliest-due valid entry. Entries whose task no longer exists
        or is disabled are discarded so a single stale entry cannot strand the
        rest of the queue (P3-7).
        """
        self._check_thread_affinity()
        while self._pending:
            sid, due = min(self._pending.items(), key=lambda kv: kv[1])
            task = self._resolve(sid)
            if task is None or not task.enabled:
                self._pending.pop(sid, None)
                continue
            result = self._launch(task, due)
            if result == "running":
                self._pending.pop(sid, None)
            elif result == "failed":
                # FR-003: 修复前此分支只保留条目并 break，不记账也不终止——
                # 条目此后随每次 run_ended 静默重试一次，用户以为"补跑一次"
                # 仍会执行。这里按"引擎是否仍被占用"区分两种失败：
                # - engine_busy（手动运行/看门狗强停后槽位未释放）：P1-3 的
                #   临时忙，保留条目待槽位释放后重试，不记账，避免污染历史并
                #   误把 SINGLE 任务判为终态（has_handled 依赖历史 outcome）；
                # - 引擎空闲仍失败：永久失败，清除条目以终止静默重试，并通知
                #   调用方按 startup_failed 记账，与主路径 _evaluate_due 同义。
                engine_busy = bool(self._run_controller.is_active)
                if not engine_busy:
                    self._pending.pop(sid, None)
                self.run_start_failed.emit(sid, due, engine_busy)
            # "skipped"/"pending" 不会在此出现（它们是 request_due 的返回值，
            # _launch 只返回 "running"/"failed"）。
            break
