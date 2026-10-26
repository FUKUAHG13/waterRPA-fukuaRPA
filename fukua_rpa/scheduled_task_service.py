"""Application-level scheduled-task service.

Lives for the whole app lifetime (owned by the main window). It is the single
source of truth for scheduling; the UI only displays state and never maintains
a second copy (design §4.5). Scheduling uses ONE application-level ``QTimer``
pointed at the nearest due time (design §4.4) — never a fixed high-frequency
poll that rebuilds the table or repaints the whole UI.

Lifecycle hooks the host must call:
* ``start()`` once the app is ready (profiles loaded, engine ready).
* ``on_profile_deleted(profile_id)`` so orphaned tasks flip to "profile missing"
  and are disabled (design §4.3).
* ``shutdown()`` to stop the timer on exit.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

from PySide6.QtCore import QObject, QTimer, Signal  # noqa: E402

from .logging_service import (  # noqa: E402
    flush_logs,
    register_processed_log_sink,
    unregister_processed_log_sink,
)
from .missed_run_detector import MissedRunCallbacks, MissedRunDetector  # noqa: E402
from .scheduled_task_logs import ScheduledTaskLogStore  # noqa: E402
from .scheduled_task_model import (  # noqa: E402
    ScheduledRunHistory,
    ScheduledTask,
    ScheduledTaskClock,
    ScheduleStatus,
    TriggerKind,
    compute_future_runs,
    new_schedule_id,
    next_due_local,
    status_for,
)
from .scheduled_task_store import ScheduledTaskStore  # noqa: E402

# DESIGN (D-07): the main scheduling timer is single-shot and only armed while a
# task is due, so it cannot notice a system sleep/wake on its own. A periodic
# heartbeat fills that gap: when the OS resumes from sleep/hibernation the
# wall-clock gap between two heartbeat ticks far exceeds the cadence, which we
# treat as a "time jump" and re-evaluate due tasks. The cadence bounds how long
# a resumed system may keep stale state; the threshold (3x) tolerates ordinary
# scheduler jitter and event-loop stalls without false positives.
_HEARTBEAT_INTERVAL_S = 60.0
_WAKE_JUMP_FACTOR = 3.0


class ScheduledTaskService(QObject):
    """Application-level scheduler and single source of truth for task state.

    Owned by the main window for the whole app lifetime. The UI only displays
    state emitted here and never keeps a second copy (design §4.5). Scheduling
    uses ONE single-shot ``QTimer`` aimed at the nearest due time plus a periodic
    heartbeat that detects system sleep/wake (design §4.4 / D-07) — never a
    fixed high-frequency poll.

    Signals:
        tasks_changed: Task list or per-task state changed (refresh views).
        summary_changed: Aggregate summary changed (next due, counts).
        run_started: A run began; payload is the schedule_id.
        run_finished: A run ended; payloads are schedule_id and outcome.
        missed_detected: An enabled "ask"-policy task missed a trigger while the
            app was closed; payloads are schedule_id and task name.
    """

    tasks_changed = Signal()
    summary_changed = Signal()
    run_started = Signal(str)  # schedule_id
    run_finished = Signal(str, str)  # schedule_id, outcome
    # Emitted at startup when an enabled task missed a trigger while the app was
    # closed/hibernating and its missed_policy is "ask": (schedule_id, task_name).
    missed_detected = Signal(str, str)

    def __init__(
        self,
        run_controller,
        execution_controller,
        base_dir: str,
        app_version: str = "",
        parent: QObject | None = None,
    ):
        """Initialize the service and its persistent store (does not start it).

        Args:
            run_controller: Coordinator-facing run controller used to launch runs.
            execution_controller: Source of profile health queries.
            base_dir: Directory holding scheduled_tasks.json and history files.
            app_version: Application version recorded for diagnostics.
            parent: Optional QObject parent for ownership.
        """
        super().__init__(parent)
        self._run_controller = run_controller
        self._execution_controller = execution_controller
        self._base_dir = base_dir
        self._app_version = app_version
        self._clock = ScheduledTaskClock()

        self._tasks_path = os.path.join(base_dir, "scheduled_tasks.json")
        self._history_path = os.path.join(base_dir, "scheduled_task_history.json")
        self._logs_path = os.path.join(base_dir, "scheduled_task_logs.jsonl")
        self._store = ScheduledTaskStore(self._tasks_path, self._history_path)
        self._log_store = ScheduledTaskLogStore(self._logs_path)
        self._processed_log_sink = self._log_store.append_processed
        self._log_sink_registered = False

        self._coordinator = None  # created in start() to bind run_ended
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timer)

        # D-07: repeating heartbeat that detects system sleep/wake (see _on_heartbeat).
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(int(_HEARTBEAT_INTERVAL_S * 1000))
        self._heartbeat.timeout.connect(self._on_heartbeat)
        self._last_heartbeat_ts: float | None = None

        self._ready = False
        # INVARIANT: 退出一旦开始就不得再发起任何运行。shutdown() 置位后
        # _recompute / _evaluate_due / _run_startup / request_due 全部短路，
        # 保证"退出主程序必须停止计时器并禁止新的补跑"（ARCHITECTURE #85）。
        # 不能只停计时器：退出等待窗口内在飞运行完成会触发 run_finished ->
        # _recompute 重挂计时器，已排队的 STARTUP singleShot 也会到点发起运行。
        self._shutdown_requested = False
        self._startup_fired = False
        self._app_start_ts = time.time()
        self._last_check_ts = 0.0
        self._next_due: datetime | None = None
        self._next_task_cache: tuple | None = None  # (schedule_id, name, due_local)
        self._running: dict[str, dict] = {}  # schedule_id -> run info
        self._last_failed: dict[str, bool] = {}  # schedule_id -> had failure
        # STARTUP tasks that already received their single delayed retry after a
        # "skipped" (busy) result, so we never retry more than once per launch.
        self._startup_retried: set = set()
        # FR-20260930-007: SINGLE 到期点若已被启动错过扫描按策略处置（该策略可能
        # 不写历史），记下 (schedule_id, due_utc)，避免 _evaluate_due 再次请求运行
        # 并记入一条并不存在的 skipped_while_busy。
        #
        # FR-SCOPE006-001：启动错过扫描与到期评估会同时命中开区间 (now-2s, now] 内的
        # 同一个到期点——扫描的参考点是"最近一次历史结束时刻"，评估的参考点是
        # now-2s，两窗重叠且互不排斥。错过扫描已按用户策略处理过的到期点在此登记，
        # _evaluate_due 不再对它们发起真实运行，避免 skip/ask 策略被绕过（用户选了
        # "跳过/询问"却仍被真实启动，可能在未预期时刻产生点击/输入副作用）。
        # INVARIANT: 键为 (schedule_id, due_utc)，即"同一调度周期互斥"；一个到期点
        # 一旦被策略处置（跳过/待询问/已补跑）就不得再被评估发起运行。作用域不限于
        # 紧随 start() 的那一次评估：同一起始窗口内的后续评估（计时器/心跳唤醒）
        # 同样不得复活该到期点；下一个到期点的键不同，稳态触发不受影响。
        # 用户显式覆盖不受本集合限制——resolve_missed_ask(run_now=True) 直接调用
        # 协调器代理，不经此集合。
        # 该集合只在 start() 的启动错过扫描期间填充（start() 幂等，故每会话最多
        # 每任务一项），因此不会无界增长。
        self._startup_handled_dues: set[tuple[str, object]] = set()
        self._collecting_startup_dues = False
        # FR-20261011-004: 周期分支的**稳态**去重。`_startup_handled_dues` 只在
        # start() 的启动错过扫描期间填充（`_collecting_startup_dues` 门控），因此
        # 稳态下 2 秒回看窗会让同一个到期点在下一次 tick 被再次交给协调器：任务仍
        # 在运行且冲突策略为 catchup_once 时返回 "pending"，运行结束后
        # `_drain_pending` 会用同一个 due 再跑一次，同一触发被重复执行。
        # INVARIANT: 周期到期点按时间单调递增，因此每个 schedule_id 只保留“最近已
        # 处置的到期点”即可判定重复；条目数上界等于本会话评估过的计划数，有界。
        self._handled_due_by_schedule: dict[str, object] = {}
        # Missed-trigger detection is a self-contained domain; the detector owns
        # the one-shot guard and the calendar walk and reports every side effect
        # through injected callbacks so the missed policy can be unit-tested in
        # isolation. INVARIANT: clock/store/path are handed over as providers,
        # not values — this service stays their single owner, and the coordinator
        # only exists after start(), so request_due must resolve it at call time.
        self._missed_detector = MissedRunDetector(
            lambda: self._clock,
            lambda: self._store,
            lambda: self._tasks_path,
            MissedRunCallbacks(
                record_miss=self._record_miss,
                record_miss_placeholder=self._record_miss_placeholder,
                record_run_start=self._record_run_start,
                emit_missed_detected=self.missed_detected.emit,
                request_due=lambda task, due_utc, **kw: self._request_due_from_detector(
                    task, due_utc, **kw
                ),
            ),
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Activate scheduling once the app is ready (profiles/engine loaded).

        Idempotent: subsequent calls are no-ops. Builds the coordinator proxy,
        recomputes the nearest due time, fires STARTUP tasks, runs the one-shot
        missed-trigger pass, evaluates already-due tasks, and arms the heartbeat.
        """
        if self._ready:
            return
        self._ready = True
        if not self._log_sink_registered:
            register_processed_log_sink(self._processed_log_sink)
            self._log_sink_registered = True
        self._coordinator = _CoordinatorProxy(self._run_controller, self._execution_controller, self._store)
        # FR-20260928-002：信号现携带 due_at_utc，直接绑定方法以透传全部参数，
        # 不用 lambda 丢弃第三个参数（历史记录需要它作为原定到期锚点）。
        self._coordinator.run_started.connect(self._on_coord_run_started)
        self._coordinator.run_finished.connect(lambda sid, rid, oc: self._on_coord_run_finished(sid, rid, oc))
        self._coordinator.watchdog_timeout.connect(lambda sid, rid: self._on_coord_watchdog_timeout(sid, rid))
        self._coordinator.run_start_failed.connect(
            lambda sid, due, busy: self._on_drain_start_failed(sid, due, busy)
        )
        self._recompute()
        self._fire_startup_tasks()
        # FR-SCOPE006-001: 记录启动错过扫描实际处理过的到期点，供紧随其后的
        # _evaluate_due 去重。收集范围严格限于本次扫描，稳态评估不受影响。
        self._collecting_startup_dues = True
        try:
            self._missed_detector.check()
        finally:
            self._collecting_startup_dues = False
        # FIX(P1-2): Evaluate due tasks immediately after startup passes.
        # A SINGLE task whose once_at is already in the past would otherwise
        # never fire: _recompute arms no timer (next_due_local returns None
        # for past once_at), the missed detector skips it (ctime >= once_at
        # yields no missed window), and start() previously never called
        # _evaluate_due. This single call closes the gap.
        self._evaluate_due()
        # D-07: arm the sleep/wake heartbeat for the rest of the session.
        self._last_heartbeat_ts = time.time()
        self._heartbeat.start()

    def shutdown(self) -> None:
        """Stop all timers and prevent new runs from launching during exit.

        Clears the coordinator's pending catch-up queue so a late run_ended
        signal cannot drain it and start a new run while the app is closing.
        """
        self._timer.stop()
        self._heartbeat.stop()
        # Clear pending catch-up queue to prevent _drain_pending from
        # launching new runs during shutdown (run_ended signal may still fire).
        # self._coordinator is a _CoordinatorProxy; the real coordinator (which
        # owns the _pending dict) lives at its _coordinator attribute.
        if self._coordinator is not None:
            inner = getattr(self._coordinator, "_coordinator", None)
            pending = getattr(inner, "_pending", None)
            if pending is not None:
                pending.clear()
            # 终态标志：仅停计时器不够，退出等待窗口内的 run_finished 回调与已
            # 排队的 STARTUP singleShot 仍会走到发起运行的路径（审查 FR-002）。
            self._coordinator.mark_shutdown()
        self._shutdown_requested = True

    def finalize_logging(self, timeout: float = 2.0) -> None:
        """Flush terminal run logs, then detach the scheduled-log sink.

        ``shutdown`` intentionally leaves the sink registered because the main
        window may still be waiting for an active Runner to stop.  The shutdown
        controller calls this second phase only after no worker remains, so the
        stop reason and final step messages cannot be lost at process exit.
        """

        if not self._log_sink_registered:
            return
        flush_logs(timeout=max(0.0, float(timeout)))
        unregister_processed_log_sink(self._processed_log_sink)
        self._log_sink_registered = False

    def on_profile_deleted(self, profile_id: str) -> None:
        """Disable and unlink every task bound to a deleted profile.

        Per ADR 0035 orphaned tasks flip to PROFILE_MISSING and are disabled;
        clearing profile_id keeps them in that state if re-enabled without a
        valid profile.

        Args:
            profile_id: Identifier of the profile that was deleted.
        """
        # DESIGN (ADR 0035): orphaned tasks flip to PROFILE_MISSING and are
        # disabled.  Clearing profile_id makes _health report profile_exists=False
        # so status_for yields PROFILE_MISSING (rather than staying WAITING) if the
        # task is ever re-enabled without reassigning a valid profile.
        changed = False
        for task in self._store.all():
            if task.profile_id == profile_id:
                task.enabled = False
                task.profile_id = ""
                self._store.update(task)
                changed = True
        if changed:
            self.tasks_changed.emit()
            self._recompute()

    # ------------------------------------------------------------------
    # health queries (delegated to execution controller)
    # ------------------------------------------------------------------
    def _health(self, profile_id: str | None):
        if not profile_id:
            return (False, False, True)
        if hasattr(self._execution_controller, "scheduled_health"):
            return self._execution_controller.scheduled_health(profile_id)
        return (True, True, True)

    def _resolve_task(self, sid: str) -> ScheduledTask | None:
        return self._store.get(sid)

    # ------------------------------------------------------------------
    # scheduling
    # ------------------------------------------------------------------
    def _recompute(self) -> None:
        # SAFETY: 退出期间不得重挂计时器（ARCHITECTURE #85，审查 FR-002）。
        if not self._ready or self._shutdown_requested:
            return
        now = self._clock.now_local()
        nearest: datetime | None = None
        nearest_task: ScheduledTask | None = None
        for task in self._store.enabled():
            n = next_due_local(task, now, self._clock)
            if n is not None and (nearest is None or n < nearest):
                nearest = n
                nearest_task = task
            # FIX(P2-4): If the task's next trigger is suppressed solely by
            # active_from (the trigger itself is valid but falls before the
            # window opens), arm the timer at active_from so _on_timer fires
            # when the window opens and _evaluate_due picks up the task.
            # Without this, a task with no sibling timers would never re-arm.
            elif n is None and task.active_from is not None:
                raw = task.trigger.next_run_local(now, self._clock)
                if raw is not None and self._clock.to_utc(raw) < task.active_from:
                    af_local = self._clock.from_utc(task.active_from)
                    if af_local > now and (nearest is None or af_local < nearest):
                        nearest = af_local
                        nearest_task = task
        self._next_due = nearest
        if nearest_task is not None:
            self._next_task_cache = (nearest_task.schedule_id, nearest_task.name, nearest)
        else:
            self._next_task_cache = None
        self._arm_timer(nearest, now)
        self.summary_changed.emit()

    def _arm_timer(self, due_local: datetime | None, now: datetime) -> None:
        if due_local is None:
            self._timer.stop()
            return
        delta = (due_local - now).total_seconds()
        if delta < 0:
            delta = 0
        # Cap the single-shot at 24h; recompute will re-arm afterwards.
        interval_ms = int(min(delta, 24 * 3600) * 1000)
        self._timer.start(interval_ms)

    def _on_timer(self) -> None:
        self._evaluate_due()
        self._recompute()

    def _on_heartbeat(self) -> None:
        # DESIGN (D-07): the startup missed pass (MissedRunDetector.check) runs
        # exactly once (guarded by its missed_checked flag) during start(), so a
        # trigger whose window falls while the system is asleep/hibernating would
        # be silently dropped — the single-shot scheduling timer is suspended
        # along with the OS and never notices the resume. The heartbeat ticks on
        # a fixed cadence instead; on resume the wall-clock gap between two ticks
        # far exceeds that cadence. Detect that jump and re-run the due
        # evaluation (the repeatable equivalent of the startup missed pass — we
        # must NOT call the detector here, its one-shot guard would skip it).
        now_ts = time.time()
        last = self._last_heartbeat_ts
        self._last_heartbeat_ts = now_ts
        if last is None:
            return
        if now_ts - last <= _HEARTBEAT_INTERVAL_S * _WAKE_JUMP_FACTOR:
            return
        # Wall-clock jumped forward: the system resumed from sleep/hibernation.
        # FR-20260809-1501: _evaluate_due 只回看 2 秒，对休眠期间错过的周期任务
        # 完全不可见（next_due_local 返回的下一次 > now 即 continue，静默丢弃且
        # 无历史记录）。此处以"休眠前最后一次活动心跳"为参考点扫描最近一次错过
        # 并按 missed_policy 补跑一次/记录跳过（复用 MissedRunDetector 的日历
        # 步进与策略应用；必须直接调用其方法，detector.check() 的一次性守卫会
        # 跳过第二次调用）。
        sleep_gap = now_ts - last
        reference_local = self._clock.now_local() - timedelta(seconds=sleep_gap)
        self._evaluate_missed_since(reference_local)
        self._evaluate_due()
        self._recompute()

    def _evaluate_missed_since(self, reference_local) -> None:
        """Apply ``missed_policy`` to periodic tasks missed in (reference, now-2s].

        FR-20260809-1501: heartbeat-jump counterpart of the startup missed pass.
        Only the single most recent missed trigger is acted upon per task
        (collapsed by ``MissedRunDetector.latest_missed_due`` so catch-up runs
        at most once — 实施方案 #155).  Currently-running tasks are skipped.

        The most recent 2 seconds are intentionally left to ``_evaluate_due``
        (whose lookback reference is ``now - 2s``): overlapping the two windows
        would double-process the same due — a catch-up task would run twice and
        a SKIP task would be started by ``_evaluate_due`` right after its miss
        was recorded.  A trigger that fires within the 2s window is treated as
        on-time, matching the existing design trade-off documented in
        ``_evaluate_due`` (event-loop blocking >2s may miss a trigger).
        """
        window_end = self._clock.now_local() - timedelta(seconds=2)
        for task in self._store.enabled():
            if task.trigger.kind == TriggerKind.STARTUP:
                continue
            if task.schedule_id in self._running:
                continue
            missed_due = self._missed_detector.latest_missed_due(
                task, reference_local, window_end
            )
            if missed_due is None:
                continue
            self._missed_detector.apply_policy(task, missed_due)

    def _evaluate_due(self) -> None:
        # SAFETY: 退出期间不得评估并发起新运行（ARCHITECTURE #85，审查 FR-002）。
        if self._shutdown_requested:
            return
        now = self._clock.now_local()
        self._last_check_ts = time.time()
        # DESIGN (ADR 0035 / 实施方案 §4.3): next_due_local returns the first
        # trigger strictly after its reference argument.  Using `now` as reference
        # would always yield a future time, so no task could ever be detected as
        # due.  Instead we look back 2 seconds: if the next trigger after
        # (now - 2s) is <= now, the task has arrived within the timer's firing
        # window. The 2-second lookback window balances timer precision (QTimer
        # ~15ms on Windows) against missed-trigger risk. SINGLE tasks bypass this
        # window entirely (see below). For periodic tasks, event-loop blocking
        # >2s may cause a silent miss; this is an accepted trade-off documented in
        # the design spec.
        reference = now - timedelta(seconds=2)
        for task in self._store.enabled():
            # DESIGN: SINGLE tasks must not depend on the 2-second lookback
            # window. If the event loop is blocked > 2 s the timer fires late
            # and once_at falls outside the window, permanently losing the
            # trigger. Instead: fire whenever once_at <= now and the task has
            # no successful run history yet.
            if task.trigger.kind == TriggerKind.SINGLE:
                once_at = task.trigger.once_at_local
                if once_at is not None and once_at <= now:
                    # Skip if already running (avoids false miss accumulation)
                    if task.schedule_id in self._running:
                        continue
                    hist = self._store.history_for(task.schedule_id)
                    # FIX(P1-1): Any history record with a non-empty outcome
                    # means the SINGLE task reached a terminal state — success,
                    # failure (failed/crashed/forced_stop/startup_failed), or an
                    # explicit skip/miss placeholder. Previously only
                    # success/stopped/user_stopped and specific reasons were
                    # recognized, so failure terminals were re-triggered by
                    # sibling timers calling _evaluate_due again.
                    has_handled = any(h.outcome or h.reason for h in hist)
                    due_utc = self._clock.to_utc(once_at)
                    # FR-20260930-007: 启动扫描已按错过策略处置过的到期点不得二次
                    # 请求——那种策略不一定留下历史记录，缺这一判断会凭空多出一条
                    # skipped_while_busy。
                    if not has_handled and (task.schedule_id, due_utc) not in (
                        self._startup_handled_dues
                    ):
                        # FR-20261012-541: SINGLE 分支同样必须遵守
                        # active_from / active_until 生效窗口。``next_due_local``
                        # 对周期任务统一应用窗口，SINGLE 此前完全绕过，窗口外
                        # 仍会真实启动，而 UI“下次执行”因 next_due_local 返回
                        # None 而始终为空。窗口外的到期点不请求运行，但要留痕
                        # （而非静默跳过），让用户知道该次未执行。
                        if (
                            task.active_from is not None
                            and due_utc < task.active_from
                        ) or (
                            task.active_until is not None
                            and due_utc > task.active_until
                        ):
                            self._record_miss(task, due_utc, "outside_active_window")
                            continue
                        result = self._coordinator.request_due(task, due_utc)
                        if result == "running":
                            self._record_run_start(task, due_utc)
                        elif result == "skipped":
                            self._record_miss(task, due_utc, "skipped_while_busy")
                        elif result == "failed":
                            # FR-20260811-D6-001: 启动失败显式记账。否则 SINGLE
                            # 任务因无历史记录被 has_handled 判未处理，反复触发
                            # 空转且用户无感知；记账后进入终态，不再重试。
                            self._record_miss(
                                task, due_utc, "startup_failed", outcome="failed"
                            )
                continue
            n = next_due_local(task, reference, self._clock)
            if n is None or n > now:
                continue
            due_utc = self._clock.to_utc(n)
            # FR-20260930-007: 启动窗口已按策略处置过的到期点不得二次请求。
            if (task.schedule_id, due_utc) in self._startup_handled_dues:
                continue
            # FR-20261011-004: 稳态下同一到期点只处置一次。2 秒回看窗会让下一次
            # tick 再次命中同一 due；若不拦截，catchup_once 会把同一到期点排入
            # 待补队列并在当前运行结束后重复执行。
            last_handled = self._handled_due_by_schedule.get(task.schedule_id)
            if last_handled is not None and due_utc <= last_handled:
                continue
            self._handled_due_by_schedule[task.schedule_id] = due_utc
            result = self._coordinator.request_due(task, due_utc)
            if result == "running":
                self._record_run_start(task, due_utc)
            elif result == "skipped":
                self._record_miss(task, due_utc, "skipped_while_busy")
            elif result == "failed":
                # FR-20260811-D6-001: 启动失败显式记账（同 SINGLE 分支），
                # 避免周期任务启动失败静默无痕。
                self._record_miss(task, due_utc, "startup_failed", outcome="failed")
            # FR-20261005-008: catchup_once 走协调器时若此刻忙碌，会返回
            # "pending"（容量为一的待补队列），运行结束会真正补跑。此前该分支
            # "无需记账"，于是这次到期点既没有运行记录也没有错过记录；下一次启动的
            # 错过扫描会用同一个 due_at_utc 再次回溯并重复请求，同一到期点被反复
            # 处置。记一条显式 outcome，使该到期点进入历史并被 _startup_handled_dues
            # 去重。不复用 _record_miss_placeholder：其 outcome 为 pending_ask，
            # 会被 ask 决策路径误读为"等待用户回答"。
            elif result == "pending":
                self._record_miss(
                    task, due_utc, "catchup_pending_while_busy", outcome="pending_catchup"
                )

    # ------------------------------------------------------------------
    # startup triggers
    # ------------------------------------------------------------------
    def _fire_startup_tasks(self) -> None:
        if self._startup_fired:
            return
        self._startup_fired = True
        for task in self._store.enabled():
            if task.trigger.kind != TriggerKind.STARTUP:
                continue
            delay = task.trigger.startup_delay_seconds or 0.0
            if delay > 0:
                QTimer.singleShot(int(delay * 1000), lambda t=task: self._run_startup(t))
            else:
                self._run_startup(task)

    def _run_startup(self, task: ScheduledTask) -> None:
        # SAFETY: 退出期间到点的 STARTUP singleShot 不得发起运行
        # （ARCHITECTURE #85，审查 FR-002）。
        if self._shutdown_requested:
            return
        if not task.enabled or self._resolve_task(task.schedule_id) is None:
            return
        due_utc = self._clock.to_utc(self._clock.now_local())
        result = self._coordinator.request_due(task, due_utc)
        if result == "running":
            self._record_run_start(task, due_utc)
        elif result == "skipped" and task.schedule_id not in self._startup_retried:
            # The coordinator was busy (e.g. a catch-up run preempted startup).
            # Retry once after a short delay so STARTUP tasks are not silently
            # dropped; the _startup_retried set guarantees at most one retry.
            self._startup_retried.add(task.schedule_id)
            QTimer.singleShot(2000, lambda t=task: self._run_startup(t))

    # ------------------------------------------------------------------
    # missed-task detection (startup)
    # ------------------------------------------------------------------
    # The whole missed domain (one-shot guard, calendar walk and the
    # skip/ask/catchup_once policy table, ADR 0035 / 实施方案 §4.3) lives in
    # MissedRunDetector; this service only owns the callbacks it injects at
    # construction time and the single start() invocation.

    # ------------------------------------------------------------------
    # run bookkeeping
    # ------------------------------------------------------------------
    def _on_coord_run_started(
        self, schedule_id: str, run_id: str, due_utc: object = None
    ) -> None:
        # FR-20260928-002: 协调器直接发起的补跑/ask-run_now 不经 _record_run_start，
        # 其 due_at_utc 只随信号传递；缺失会让历史 due_at 为 None，错过检测失去
        # 原定到期锚点。已有值时以信号携带的为准（同一调度周期应唯一）。
        due_at = due_utc
        if due_at is None:
            due_at = self._running.get(schedule_id, {}).get("due_at")
        self._running[schedule_id] = {
            "run_id": run_id,
            "started_at": datetime.now(UTC),
            "due_at": due_at,
        }
        self.run_started.emit(schedule_id)
        self.tasks_changed.emit()

    def _on_coord_run_finished(self, schedule_id: str, run_id: str, outcome: str) -> None:
        info = self._running.pop(schedule_id, None)
        # FR-SCHED-001: #73 合法完成终局 "finished" 纳入非失败集，避免成功路径误判失败。
        self._last_failed[schedule_id] = outcome not in (
            "success", "finished", "stopped", "user_stopped"
        )
        if info:
            ended = datetime.now(UTC)
            started = info.get("started_at", ended)
            duration_ms = int((ended - started).total_seconds() * 1000)
            self._store.add_history(
                ScheduledRunHistory(
                    history_id=new_schedule_id(),
                    schedule_id=schedule_id,
                    run_id=run_id,
                    profile_id=self._store.get(schedule_id).profile_id if self._store.get(schedule_id) else None,
                    due_at_utc=info.get("due_at"),
                    started_at_utc=started,
                    ended_at_utc=ended,
                    duration_ms=duration_ms,
                    outcome=outcome,
                    reason="",
                    application_log_reference=f"scheduled_task_logs.jsonl#{run_id}",
                )
            )
        self.run_finished.emit(schedule_id, outcome)
        self.tasks_changed.emit()
        self._recompute()

    def _on_coord_watchdog_timeout(self, schedule_id: str, run_id: str) -> None:
        """Handle a watchdog force-release: the run never reported completion.

        Without this handler the service's _running entry would persist forever,
        causing the task to display as RUNNING in the UI indefinitely.
        """
        info = self._running.pop(schedule_id, None)
        self._last_failed[schedule_id] = True
        if info:
            ended = datetime.now(UTC)
            started = info.get("started_at", ended)
            duration_ms = int((ended - started).total_seconds() * 1000)
            task = self._store.get(schedule_id)
            self._store.add_history(
                ScheduledRunHistory(
                    history_id=new_schedule_id(),
                    schedule_id=schedule_id,
                    run_id=run_id,
                    profile_id=task.profile_id if task else None,
                    due_at_utc=info.get("due_at"),
                    started_at_utc=started,
                    ended_at_utc=ended,
                    duration_ms=duration_ms,
                    outcome="watchdog_timeout",
                    reason="run_never_reported_completion",
                    application_log_reference=f"scheduled_task_logs.jsonl#{run_id}",
                )
            )
        self.run_finished.emit(schedule_id, "watchdog_timeout")
        self.tasks_changed.emit()
        self._recompute()

    def _on_drain_start_failed(
        self, schedule_id: str, due_utc, engine_busy: bool
    ) -> None:
        """补跑排空路径的启动失败记账（审查 FR-003）。

        协调器已按 engine_busy 区分两种失败并自行决定是否清条目：
        - engine_busy（引擎仍被手动运行/看门狗强停占用）：属 P1-3 的临时忙，
          条目保留待槽位释放后重试。此处**不记账**——否则每次重试都会写入
          一条失败历史，且 SINGLE 任务会因 has_handled 见到 outcome 而误判
          终态，反而丢掉这次补跑。
        - 引擎空闲仍失败：永久失败，条目已被清除，按 startup_failed 记账，
          与主路径 _evaluate_due 的语义保持一致（设计基准：错过执行、休眠
          恢复和启动触发必须留下可追踪结果）。
        """

        if engine_busy or self._shutdown_requested:
            return
        task = self._resolve_task(schedule_id)
        if task is None:
            return
        self._record_miss(task, due_utc, "startup_failed", outcome="failed")
        self.tasks_changed.emit()

    def _record_run_start(self, task: ScheduledTask, due_utc) -> None:
        self._running.setdefault(task.schedule_id, {})["due_at"] = due_utc

    def _request_due_from_detector(self, task: ScheduledTask, due_utc, **kwargs) -> str:
        """Coordinator request made by the missed-run detector.

        FR-SCOPE006-001: 启动错过扫描发起的补跑请求也登记其到期点，使随后的
        `_evaluate_due` 不会对同一到期点重复请求（修复前该重叠会让 catchup_once
        补跑两次）。

        仅在协调器**接受**请求（"running"/"pending"）时登记。请求失败
        （"failed"）时由 missed_run_detector 的 failed 分支直接经
        `_record_miss(outcome="failed")` 记账，且该到期点随 `_record_miss`
        进入 `_startup_handled_dues`——`_evaluate_due` 不再重试该点，最终
        恰好一条 failed 记录、无重复（早期版本依赖 `_evaluate_due` 重试的
        描述已随该分支引入而过时）。
        """
        result = self._coordinator.request_due(task, due_utc, **kwargs)
        if self._collecting_startup_dues and result in ("running", "pending"):
            self._startup_handled_dues.add((task.schedule_id, due_utc))
        return result

    def _record_miss(
        self, task: ScheduledTask, due_utc, reason: str, outcome: str = "skipped"
    ) -> None:
        # FR-SCOPE006-001: 启动错过扫描按 skip/未知策略处理过的到期点在此登记，
        # 供紧随其后的 _evaluate_due 去重（见 start()）。
        if self._collecting_startup_dues:
            self._startup_handled_dues.add((task.schedule_id, due_utc))
        # FR-20260811-D6-001: outcome 参数化——startup_failed 属 failure 终态，
        # 必须记 outcome="failed"，避免 UI 把启动失败误显示为"跳过"且历史数据
        # 永久失真（scheduled_tasks_controller 按 outcome 渲染）。
        # FR-20260812-D6-001: outcome=="failed" 时同步更新 _last_failed，
        # 使 task_status 返回 LAST_RUN_FAILED、异常过滤与 summary()["errors"]
        # 与结果列一致，避免 SINGLE 任务启动失败后状态显示 WAITING。
        if outcome == "failed":
            self._last_failed[task.schedule_id] = True
        self._store.add_history(
            ScheduledRunHistory(
                history_id=new_schedule_id(),
                schedule_id=task.schedule_id,
                run_id="",
                profile_id=task.profile_id,
                due_at_utc=due_utc,
                started_at_utc=None,
                ended_at_utc=None,
                duration_ms=None,
                outcome=outcome,
                reason=reason,
                application_log_reference="",
            )
        )

    def _record_miss_placeholder(self, task: ScheduledTask, due_utc, reason: str) -> None:
        """Record a placeholder history entry for a missed trigger whose handling
        is still pending (e.g. an ask-policy trigger awaiting the user's decision).
        The "pending_ask" outcome is recognized by _evaluate_due's has_handled
        check via its reason so the task is not auto-run before the user responds.
        """
        # FR-SCOPE006-001: 同 _record_miss，ask 策略处理过的到期点也须去重。
        # 用户选了"询问"却在同一起始窗口又被到期评估真实启动，等于没问。
        if self._collecting_startup_dues:
            self._startup_handled_dues.add((task.schedule_id, due_utc))
        self._store.add_history(
            ScheduledRunHistory(
                history_id=new_schedule_id(),
                schedule_id=task.schedule_id,
                run_id="",
                profile_id=task.profile_id,
                due_at_utc=due_utc,
                started_at_utc=None,
                ended_at_utc=None,
                duration_ms=None,
                outcome="pending_ask",
                reason=reason,
                application_log_reference="",
            )
        )

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def task_status(self, task: ScheduledTask) -> ScheduleStatus:
        """Compute the live status of a task, including runtime refinements.

        Args:
            task: Scheduled task to evaluate.

        Returns:
            ScheduleStatus refined with RUNNING / PENDING_CATCHUP /
            LAST_RUN_FAILED on top of the static base status.
        """
        running = task.schedule_id in self._running
        exists, config_valid, plugin_ok = self._health(task.profile_id)
        base = status_for(task, exists, config_valid, plugin_ok, running)
        if base == ScheduleStatus.WAITING:
            if self._coordinator is not None and task.schedule_id in self._coordinator.pending_ids:
                return ScheduleStatus.PENDING_CATCHUP
            if self._last_failed.get(task.schedule_id):
                return ScheduleStatus.LAST_RUN_FAILED
        return base

    def next_due_local(self, task: ScheduledTask) -> datetime | None:
        """Return the task's next due wall-clock time, or None if disabled."""
        if not task.enabled:
            return None
        return next_due_local(task, self._clock.now_local(), self._clock)

    def future_runs(self, task: ScheduledTask, count: int = 5) -> list[datetime]:
        """Preview up to ``count`` upcoming fire times for display."""
        return compute_future_runs(task.trigger, self._clock.now_local(), self._clock, count)

    def task_by_id(self, schedule_id: str) -> ScheduledTask | None:
        """Return the current task definition for an editor or log view."""

        return self._store.get(schedule_id)

    def history_for(self, schedule_id: str) -> list[ScheduledRunHistory]:
        """Return persisted run summaries for one scheduled task."""

        return self._store.history_for(schedule_id)

    def resolve_missed_ask(self, schedule_id: str, run_now: bool) -> bool:
        """Resolve a pending MISSED_ASK decision made by the user.

        FR-b01-004: ``missed_detected`` 信号此前无生产接线，"询问"策略从不真正
        询问用户、任务悬挂于 pending_ask。UI 连接该信号弹出询问后调用本方法：
        ``run_now=True`` 立即按错过的到期时间补跑一次（catchup_once，容量一）；
        否则记录为跳过（missed_ask_skipped），结束待确认状态。任务已删除或停用
        时按跳过处理，避免悬挂与误启动。

        Returns:
            True if the decision was applied; False if the task no longer exists.
        """
        task = self.task_by_id(schedule_id)
        if task is None:
            return False
        pending = [
            h for h in self.history_for(schedule_id) if h.outcome == "pending_ask"
        ]
        due_utc = pending[-1].due_at_utc if pending else self._clock.to_utc(
            self._clock.now_local()
        )
        if run_now and task.enabled:
            decision = self._coordinator.request_due(task, due_utc, "catchup_once")
            return decision in ("running", "pending")
        self._record_miss(task, due_utc, "missed_ask_skipped")
        return True

    def log_entries_for(
        self,
        schedule_id: str,
        run_id: str | None = None,
    ) -> list[dict]:
        """Return bounded, privacy-filtered detailed logs for one plan/run."""

        return self._log_store.entries_for(schedule_id, run_id)

    def log_revision(self) -> int:
        """Return the detailed-log revision used by visible log windows."""

        return self._log_store.revision()

    def running_info_for(self, schedule_id: str) -> dict | None:
        """Return a snapshot of one currently running scheduled execution."""

        info = self._running.get(schedule_id)
        return dict(info) if info else None

    @property
    def has_running_tasks(self) -> bool:
        """FR-20260812-D13-001: 是否有正在运行的计划任务。

        供 UI 每秒 tick 短路：无运行任务时无需全量 list_view 拉取健康状态。
        """
        return bool(self._running)

    def uptime_seconds(self) -> int:
        """Return uptime without rebuilding aggregate task health state."""

        return max(0, int(time.time() - self._app_start_ts))

    # ------------------------------------------------------------------
    # UI model
    # ------------------------------------------------------------------
    def list_view(self) -> list[dict]:
        """Build the per-task rows displayed by the scheduled-task UI.

        Returns:
            List of dicts, each with keys ``task`` (ScheduledTask), ``status``
            (ScheduleStatus), ``next_due`` (naive local datetime or None), and
            ``last_run`` (dict with outcome/started_at/duration_ms or None). A
            currently running task overrides last_run with a live "running" row.
        """
        rows = []
        now = datetime.now(UTC)
        for task in self._store.all():
            running_info = self._running.get(task.schedule_id)
            last_run = self._last_run_info(task.schedule_id)
            if running_info and running_info.get("started_at"):
                elapsed_ms = int((now - running_info["started_at"]).total_seconds() * 1000)
                last_run = {
                    "outcome": "running",
                    "started_at": running_info["started_at"],
                    "duration_ms": elapsed_ms,
                }
            rows.append(
                {
                    "task": task,
                    "status": self.task_status(task),
                    "next_due": self.next_due_local(task),
                    "last_run": last_run,
                }
            )
        return rows

    def _last_run_info(self, schedule_id: str) -> dict | None:
        hist = self._store.history_for(schedule_id)
        if not hist:
            return None
        last = hist[-1]
        return {
            "outcome": last.outcome,
            "started_at": last.started_at_utc,
            "duration_ms": last.duration_ms,
        }

    def summary(self) -> dict:
        """Build the aggregate dashboard summary.

        Returns:
            Dict with service_status, app_start_ts, uptime_seconds, total,
            enabled, errors (count of unhealthy tasks), last_check_ts,
            next_task (id/name/due_local or None), and current_running.
        """
        tasks = self._store.all()
        enabled = [t for t in tasks if t.enabled]
        errors = 0
        for t in tasks:
            st = self.task_status(t)
            if st in (
                ScheduleStatus.PROFILE_MISSING,
                ScheduleStatus.INVALID_CONFIG,
                ScheduleStatus.PLUGIN_OR_CAPABILITY_MISSING,
                ScheduleStatus.LAST_RUN_FAILED,
            ):
                errors += 1
        now_ts = time.time()
        next_task = None
        if self._next_task_cache is not None:
            sid, name, due = self._next_task_cache
            next_task = {
                "schedule_id": sid,
                "name": name,
                "due_local": due,
            }
        current = None
        if self._running:
            sid = next(iter(self._running))
            t = self._store.get(sid)
            current = {"schedule_id": sid, "name": t.name if t else sid}
        return {
            "service_status": "running" if self._ready else "paused",
            "app_start_ts": self._app_start_ts,
            "uptime_seconds": int(now_ts - self._app_start_ts),
            "total": len(tasks),
            "enabled": len(enabled),
            "errors": errors,
            "last_check_ts": self._last_check_ts,
            "next_task": next_task,
            "current_running": current,
        }

    # ------------------------------------------------------------------
    # mutations
    # ------------------------------------------------------------------
    def add_task(self, task: ScheduledTask) -> None:
        """Persist a new task, notify views, and recompute scheduling."""
        self._store.add(task)
        self.tasks_changed.emit()
        self._recompute()

    def update_task(self, task: ScheduledTask) -> None:
        """Persist changes to an existing task and recompute scheduling."""
        self._store.update(task)
        self.tasks_changed.emit()
        self._recompute()

    def remove_task(self, schedule_id: str) -> None:
        """Delete a task and drop any in-flight run tracking for it."""
        self._store.remove(schedule_id)
        self._running.pop(schedule_id, None)
        self.tasks_changed.emit()
        self._recompute()

    def set_enabled(self, schedule_id: str, enabled: bool) -> None:
        """Enable or disable a task and recompute scheduling."""
        self._store.set_enabled(schedule_id, enabled)
        self.tasks_changed.emit()
        self._recompute()

    def set_enabled_many(self, schedule_ids: list[str], enabled: bool) -> None:
        """FR-20260812 建议-架构-5: 批量启用/停用。

        逐条 ``set_enabled`` 会对每条触发一次全量写盘 + tasks_changed + 重算；
        批量操作（多选行右键启停）用单次落盘与单次通知，避免 N 条批量操作
        触发 N 次 emit+recompute 的 UI 刷新放大。
        """
        if not schedule_ids:
            return
        self._store.set_enabled_many(schedule_ids, enabled)
        self.tasks_changed.emit()
        self._recompute()

    def remove_many(self, schedule_ids: list[str]) -> None:
        """FR-20260812 建议-架构-5: 批量删除。

        与 set_enabled_many 同理，一次落盘 + 单次通知；同时清理 in-flight
        运行跟踪。
        """
        if not schedule_ids:
            return
        self._store.remove_many(schedule_ids)
        for schedule_id in schedule_ids:
            self._running.pop(schedule_id, None)
        self.tasks_changed.emit()
        self._recompute()

    def duplicate_task(self, schedule_id: str) -> ScheduledTask | None:
        """Create a disabled copy of a task with a new id and "(副本)" name.

        Args:
            schedule_id: Identifier of the task to copy.

        Returns:
            The newly created ScheduledTask, or None if the source is missing.
        """
        src = self._store.get(schedule_id)
        if src is None:
            return None
        import copy
        new = ScheduledTask.from_dict(copy.deepcopy(src.to_dict()))
        new.schedule_id = new_schedule_id()
        new.name = f"{src.name} (副本)"
        new.enabled = False  # imported/duplicated tasks stay disabled by default
        self._store.add(new)
        self.tasks_changed.emit()
        self._recompute()
        return new

    def export_task(self, schedule_id: str) -> dict | None:
        """Return a task's serializable dict, or None if it does not exist."""
        task = self._store.get(schedule_id)
        return task.to_dict() if task else None

    def import_tasks(
        self,
        dicts: list[dict],
        default_disabled: bool = True,
        report_errors: list[str] | None = None,
    ) -> list[ScheduledTask]:
        """Import scheduled tasks from serialized dicts.

        Per design §4.8 / §11 imported tasks default to DISABLED and a
        conflicting schedule_id is regenerated (never overwrites an existing
        task). Malformed entries are skipped and logged, not raised.

        Args:
            dicts: List of task dicts as produced by ScheduledTask.to_dict.
            default_disabled: Force imported tasks to the disabled state.
            report_errors: FR-20260812 建议-体验-3: 可选收集跳过明细的列表；
                传入后本方法把每条被跳过条目的原因追加进去，供 UI 在成功
                提示中披露，避免"已导入 N 个"隐藏了被跳过的条目。不传则
                行为与旧版一致（仅日志记录）。

        Returns:
            List of successfully imported ScheduledTask objects.
        """
        imported: list[ScheduledTask] = []
        errors: list[str] = []
        # FR-20260812-D13-003: 两阶段导入。先全量解析/校验/冲突再生，全部
        # 通过后再一次性批量落盘（store.add_many 只写一次盘），避免逐条 add
        # 在中途失败时留下部分导入、以及重复导入累积副本。
        pending: list[ScheduledTask] = []
        for i, d in enumerate(dicts):
            try:
                task = ScheduledTask.from_dict(d)
            except (KeyError, ValueError, TypeError) as exc:
                errors.append(f"第 {i + 1} 条: {exc}")
                continue
            if default_disabled:
                task.enabled = False
            if self._store.get(task.schedule_id) is not None:
                task.schedule_id = new_schedule_id()
            pending.append(task)
        if pending:
            self._store.add_many(pending)
            imported.extend(pending)
        if errors:
            logger.warning("导入计划任务时 %d 条格式错误被跳过: %s", len(errors), errors[:3])
            # FR-20260812 建议-体验-3: 调用方传入 report_errors 时披露跳过明细。
            if report_errors is not None:
                report_errors.extend(errors)
        if imported:
            self.tasks_changed.emit()
            self._recompute()
        return imported

    def clear_history(self) -> None:
        """Delete run summaries and detailed scheduled logs, then notify views."""
        self._store.clear_history()
        self._log_store.clear()
        self.tasks_changed.emit()


class _CoordinatorProxy:
    """Thin wrapper so the service can talk to the run coordinator uniformly.

    Kept internal to avoid leaking the coordinator's run_controller coupling
    into the public service API. Emits the same run_started / run_finished
    signals the service expects and forwards scheduling decisions unchanged.
    """

    def __init__(self, run_controller, execution_controller, store):
        """Wrap a freshly created ScheduledRunCoordinator and mirror its signals.

        Args:
            run_controller: Run controller forwarded to the coordinator.
            execution_controller: Execution controller forwarded to the coordinator.
            store: Task store; its ``get`` method backs schedule lookups.
        """
        # Imported lazily to avoid a circular import at module load time.
        from .scheduled_run_coordinator import ScheduledRunCoordinator

        self._coordinator = ScheduledRunCoordinator(
            run_controller, execution_controller, store.get
        )
        self.run_started = self._coordinator.run_started
        self.run_finished = self._coordinator.run_finished
        self.watchdog_timeout = self._coordinator.watchdog_timeout
        self.run_start_failed = self._coordinator.run_start_failed
        # 退出期间拒绝一切发起运行的请求（ARCHITECTURE #85，审查 FR-002）。
        self._shutdown_requested = False

    def mark_shutdown(self) -> None:
        """置终态：此后 request_due 一律返回 "failed"，不再转发给协调器。"""

        self._shutdown_requested = True

    @property
    def pending_ids(self) -> set:
        """Set of schedule_ids currently queued for catch-up execution."""
        return self._coordinator.pending_ids

    def request_due(self, task, due_utc, conflict_policy_override=None):
        """Forward a due-trigger request to the coordinator.

        Returns:
            Coordinator decision string: "running", "pending", "skipped", or
            "failed".
        """
        # SAFETY: 退出期间一律拒绝发起运行。这是外部调用方（如 UI 直接调用
        # request_due）的最后一道防线；内部路径已由 _evaluate_due / _run_startup
        # 短路，正常不会到达这里（ARCHITECTURE #85，审查 FR-002）。
        if self._shutdown_requested:
            return "failed"
        return self._coordinator.request_due(task, due_utc, conflict_policy_override)
