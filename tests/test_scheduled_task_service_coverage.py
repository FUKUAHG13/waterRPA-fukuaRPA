"""Coverage-oriented tests for scheduled_task_service.py.

Targets uncovered blocks: on_profile_deleted, _health, _recompute, _arm_timer,
_on_timer, _on_heartbeat, _evaluate_due (periodic path), _fire_startup_tasks,
_run_startup, MissedRunDetector (check/reference_local/apply_policy),
_on_coord_run_started/finished/watchdog_timeout, task_status refinements,
next_due_local, future_runs, list_view, _last_run_info, summary, update_task,
remove_task, duplicate_task, export_task, import_tasks, clear_history.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time as time_mod
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fukua_rpa.scheduled_task_model import (
    CONFLICT_CATCHUP_ONCE,
    MISSED_ASK,
    MISSED_CATCHUP_ONCE,
    MISSED_SKIP,
    ScheduledRunHistory,
    ScheduledTask,
    ScheduledTaskClock,
    ScheduledTrigger,
    ScheduleStatus,
    TriggerKind,
    new_schedule_id,
)
from fukua_rpa.scheduled_task_service import ScheduledTaskService, _CoordinatorProxy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeClock(ScheduledTaskClock):
    """Clock with a controllable 'now' for deterministic tests."""

    def __init__(self, fixed_now: datetime):
        self._now = fixed_now

    def now_local(self) -> datetime:
        return self._now

    def to_utc(self, naive_local: datetime) -> datetime:
        return naive_local.replace(tzinfo=UTC)

    def from_utc(self, utc: datetime) -> datetime:
        return utc.replace(tzinfo=None)


def _make_task(kind=TriggerKind.DAILY, **kw):
    """Create a task with sensible defaults for testing."""
    defaults = {
        "schedule_id": kw.pop("schedule_id", new_schedule_id()),
        "name": kw.pop("name", "test-task"),
        "profile_id": kw.pop("profile_id", "p1"),
        "profile_name_snapshot": kw.pop("profile_name_snapshot", "P1"),
        "enabled": kw.pop("enabled", True),
    }
    if kind == TriggerKind.DAILY:
        trigger = ScheduledTrigger(
            kind=TriggerKind.DAILY,
            local_time=kw.pop("local_time", datetime(2025, 1, 1, 9, 0).time()),
        )
    elif kind == TriggerKind.SINGLE:
        trigger = ScheduledTrigger(
            kind=TriggerKind.SINGLE,
            once_at_local=kw.pop("once_at_local", datetime(2025, 6, 1, 10, 0)),
        )
    elif kind == TriggerKind.STARTUP:
        trigger = ScheduledTrigger(
            kind=TriggerKind.STARTUP,
            startup_delay_seconds=kw.pop("startup_delay_seconds", 0.0),
        )
    elif kind == TriggerKind.INTERVAL:
        trigger = ScheduledTrigger(
            kind=TriggerKind.INTERVAL,
            interval_seconds=kw.pop("interval_seconds", 3600),
            anchor_at_utc=kw.pop("anchor_at_utc", datetime(2025, 1, 1, 0, 0, tzinfo=UTC)),
        )
    else:
        trigger = ScheduledTrigger(kind=kind)
    defaults.update(kw)
    return ScheduledTask(trigger=trigger, **defaults)


class _ServiceTestBase(unittest.TestCase):
    """Base class providing a service with fake clock and mocked coordinator."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._now = datetime(2025, 6, 1, 12, 0, 0)
        self._clock = _FakeClock(self._now)

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _make_service(self, ready=True):
        run_controller = MagicMock()
        run_controller.is_active = False
        run_controller.run_ended = MagicMock()
        execution_controller = MagicMock()
        execution_controller.prepare_and_launch_profile.return_value = True
        execution_controller.scheduled_health.return_value = (True, True, True)

        svc = ScheduledTaskService(
            run_controller=run_controller,
            execution_controller=execution_controller,
            base_dir=self._tmp_dir,
        )
        svc._clock = self._clock
        if ready:
            svc._ready = True
            svc._coordinator = MagicMock()
            svc._coordinator.request_due.return_value = "running"
            svc._coordinator.pending_ids = set()
        return svc


# ---------------------------------------------------------------------------
# on_profile_deleted (lines 224-233)
# ---------------------------------------------------------------------------

class TestOnProfileDeleted(_ServiceTestBase):

    def test_disables_and_unlinks_matching_tasks(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s1", profile_id="prof-x", name="t1")
        t2 = _make_task(schedule_id="s2", profile_id="prof-y", name="t2")
        t3 = _make_task(schedule_id="s3", profile_id="prof-x", name="t3")
        svc._store.add(t1)
        svc._store.add(t2)
        svc._store.add(t3)

        signals = []
        svc.tasks_changed.connect(lambda: signals.append("changed"))

        svc.on_profile_deleted("prof-x")

        # t1 and t3 disabled and unlinked
        self.assertFalse(svc._store.get("s1").enabled)
        self.assertEqual(svc._store.get("s1").profile_id, "")
        self.assertFalse(svc._store.get("s3").enabled)
        self.assertEqual(svc._store.get("s3").profile_id, "")
        # t2 untouched
        self.assertTrue(svc._store.get("s2").enabled)
        self.assertEqual(svc._store.get("s2").profile_id, "prof-y")
        # signal emitted
        self.assertIn("changed", signals)

    def test_no_change_when_no_match(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s1", profile_id="prof-a")
        svc._store.add(t1)

        signals = []
        svc.tasks_changed.connect(lambda: signals.append("changed"))
        svc.on_profile_deleted("prof-nonexist")

        self.assertTrue(svc._store.get("s1").enabled)
        self.assertEqual(signals, [])


# ---------------------------------------------------------------------------
# _health (lines 240, 243)
# ---------------------------------------------------------------------------

class TestHealth(_ServiceTestBase):

    def test_empty_profile_returns_false_false_true(self):
        svc = self._make_service(ready=False)
        result = svc._health("")
        self.assertEqual(result, (False, False, True))

    def test_none_profile_returns_false_false_true(self):
        svc = self._make_service(ready=False)
        result = svc._health(None)
        self.assertEqual(result, (False, False, True))

    def test_delegates_to_scheduled_health(self):
        svc = self._make_service(ready=False)
        svc._execution_controller.scheduled_health = MagicMock(
            return_value=(True, False, True)
        )
        result = svc._health("p1")
        self.assertEqual(result, (True, False, True))
        svc._execution_controller.scheduled_health.assert_called_once_with("p1")

    def test_fallback_when_no_scheduled_health(self):
        svc = self._make_service(ready=False)
        # Remove scheduled_health if it exists as a MagicMock auto-attr
        if hasattr(svc._execution_controller, "scheduled_health"):
            del svc._execution_controller.scheduled_health
        result = svc._health("p1")
        self.assertEqual(result, (True, True, True))


# ---------------------------------------------------------------------------
# _recompute + _arm_timer (lines 258-273, 276, 286-291)
# ---------------------------------------------------------------------------

class TestRecomputeAndArmTimer(_ServiceTestBase):

    def test_recompute_finds_nearest_due(self):
        svc = self._make_service(ready=True)
        # Task due at 13:00 (1 hour from now)
        t1 = _make_task(
            schedule_id="s1",
            kind=TriggerKind.DAILY,
            local_time=datetime(2025, 6, 1, 13, 0).time(),
        )
        # Task due at 14:00 (2 hours from now)
        t2 = _make_task(
            schedule_id="s2",
            kind=TriggerKind.DAILY,
            local_time=datetime(2025, 6, 1, 14, 0).time(),
        )
        svc._store.add(t1)
        svc._store.add(t2)

        svc._recompute()

        self.assertIsNotNone(svc._next_due)
        self.assertEqual(svc._next_due, datetime(2025, 6, 1, 13, 0))
        self.assertEqual(svc._next_task_cache[0], "s1")

    def test_recompute_no_enabled_tasks(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s1", enabled=False)
        svc._store.add(t1)

        svc._recompute()

        self.assertIsNone(svc._next_due)
        self.assertIsNone(svc._next_task_cache)

    def test_recompute_not_ready_is_noop(self):
        svc = self._make_service(ready=False)
        svc._recompute()
        self.assertIsNone(svc._next_due)

    def test_arm_timer_with_future_due(self):
        svc = self._make_service(ready=True)
        due = self._now + timedelta(hours=1)
        svc._arm_timer(due, self._now)
        self.assertTrue(svc._timer.isActive())

    def test_arm_timer_with_none_stops(self):
        svc = self._make_service(ready=True)
        svc._timer.start(5000)
        svc._arm_timer(None, self._now)
        self.assertFalse(svc._timer.isActive())

    def test_arm_timer_past_due_uses_zero(self):
        svc = self._make_service(ready=True)
        due = self._now - timedelta(seconds=10)
        svc._arm_timer(due, self._now)
        self.assertTrue(svc._timer.isActive())

    def test_recompute_active_from_suppressed_trigger(self):
        """Lines 267-273: task whose trigger is suppressed by active_from."""
        # Use a clock at 08:00 so next_run_local returns today 09:00,
        # but next_due_local returns None because 09:00 UTC < active_from 14:00 UTC.
        self._clock._now = datetime(2025, 6, 1, 8, 0, 0)
        svc = self._make_service(ready=True)
        active_from = datetime(2025, 6, 1, 14, 0, tzinfo=UTC)
        t1 = _make_task(
            schedule_id="s-af",
            kind=TriggerKind.DAILY,
            local_time=datetime(2025, 6, 1, 9, 0).time(),
            active_from=active_from,
        )
        svc._store.add(t1)

        svc._recompute()

        # next_due_local returns None (suppressed), but _recompute arms at active_from
        self.assertIsNotNone(svc._next_due)
        self.assertEqual(svc._next_due, datetime(2025, 6, 1, 14, 0))
        self.assertEqual(svc._next_task_cache[0], "s-af")


# ---------------------------------------------------------------------------
# _on_timer (lines 294-295)
# ---------------------------------------------------------------------------

class TestOnTimer(_ServiceTestBase):

    def test_on_timer_calls_evaluate_and_recompute(self):
        svc = self._make_service(ready=True)
        with patch.object(svc, "_evaluate_due") as mock_eval, \
             patch.object(svc, "_recompute") as mock_recomp:
            svc._on_timer()
            mock_eval.assert_called_once()
            mock_recomp.assert_called_once()


# ---------------------------------------------------------------------------
# _on_heartbeat (lines 307-316)
# ---------------------------------------------------------------------------

class TestOnHeartbeat(_ServiceTestBase):

    def test_heartbeat_no_jump_is_noop(self):
        svc = self._make_service(ready=True)
        svc._last_heartbeat_ts = time_mod.time()
        with patch.object(svc, "_evaluate_due") as mock_eval:
            svc._on_heartbeat()
            mock_eval.assert_not_called()

    def test_heartbeat_detects_wake_jump(self):
        svc = self._make_service(ready=True)
        # Simulate last heartbeat was 5 minutes ago (well beyond 3x60s threshold)
        svc._last_heartbeat_ts = time_mod.time() - 300.0
        with patch.object(svc, "_evaluate_due") as mock_eval, \
             patch.object(svc, "_recompute") as mock_recomp:
            svc._on_heartbeat()
            mock_eval.assert_called_once()
            mock_recomp.assert_called_once()

    def test_heartbeat_first_call_no_last(self):
        svc = self._make_service(ready=True)
        svc._last_heartbeat_ts = None
        with patch.object(svc, "_evaluate_due") as mock_eval:
            svc._on_heartbeat()
            mock_eval.assert_not_called()
        # Should have set _last_heartbeat_ts
        self.assertIsNotNone(svc._last_heartbeat_ts)

    def test_heartbeat_wake_jump_applies_missed_policy_to_periodic_task(self):
        """FR-20260809-1501: 心跳跳变（休眠唤醒）后，休眠期间错过的 DAILY 任务
        按 missed_policy 补跑一次（修复前 request_due 调用 0 次、静默丢弃）。
        触发点 11:00 在 2 秒窗口外（now=12:00），由错过扫描处理。"""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-missed-daily",
            kind=TriggerKind.DAILY,
            local_time=datetime(2025, 6, 1, 11, 0).time(),  # 1 小时前到点
            missed_policy=MISSED_CATCHUP_ONCE,
        )
        svc._store.add(t1)
        svc._last_heartbeat_ts = time_mod.time() - 5 * 3600.0
        svc._coordinator.request_due.return_value = "running"
        with patch.object(svc, "_evaluate_due") as mock_eval, \
             patch.object(svc, "_recompute") as mock_recomp:
            svc._on_heartbeat()
        # 补跑路径经 request_due 触发并记账（修复前 request_due 未被调用）。
        svc._coordinator.request_due.assert_called()
        self.assertIn("s-missed-daily", svc._running)
        mock_eval.assert_called_once()
        mock_recomp.assert_called_once()

    def test_heartbeat_wake_jump_records_miss_for_skip_policy(self):
        """FR-20260809-1501: missed_policy=SKIP 时心跳跳变记录 missed_while_closed，
        留下可追溯痕迹（修复前无任何历史记录）。"""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-missed-skip",
            kind=TriggerKind.DAILY,
            local_time=datetime(2025, 6, 1, 11, 0).time(),  # 1 小时前到点
            missed_policy=MISSED_SKIP,
        )
        svc._store.add(t1)
        svc._last_heartbeat_ts = time_mod.time() - 5 * 3600.0
        with patch.object(svc, "_evaluate_due"), patch.object(svc, "_recompute"):
            svc._on_heartbeat()
        hist = svc._store.history_for("s-missed-skip")
        self.assertTrue(any(h.reason == "missed_while_closed" for h in hist))


# ---------------------------------------------------------------------------
# _evaluate_due periodic path (lines 361-369)
# ---------------------------------------------------------------------------

class TestEvaluateDuePeriodic(_ServiceTestBase):

    def test_periodic_task_due_fires(self):
        """A DAILY task whose trigger falls within the 2s lookback window."""
        svc = self._make_service(ready=True)
        # Task due at 12:00:00 - now is 12:00:00, reference = 11:59:58
        # next_due_local(task, reference) should return 12:00:00 which is <= now
        t1 = _make_task(
            schedule_id="s-daily",
            kind=TriggerKind.DAILY,
            local_time=datetime(2025, 6, 1, 12, 0).time(),
        )
        svc._store.add(t1)
        svc._coordinator.request_due.return_value = "running"

        svc._evaluate_due()

        svc._coordinator.request_due.assert_called_once()
        self.assertIn("s-daily", svc._running)

    def test_periodic_same_due_point_requested_only_once(self):
        """FR-20261011-004: a periodic due point is handled once per session.

        The 2-second lookback window keeps the same due point "due" on the next
        tick; without steady-state dedup it was re-requested, and with
        ``catchup_once`` the drain re-ran the very same trigger.
        """
        svc = self._make_service(ready=True)
        svc._store.add(
            _make_task(
                schedule_id="s-dup",
                kind=TriggerKind.DAILY,
                local_time=datetime(2025, 6, 1, 12, 0).time(),
            )
        )
        svc._coordinator.request_due.return_value = "running"

        svc._evaluate_due()
        self._clock._now = datetime(2025, 6, 1, 12, 0, 1)  # still inside lookback
        svc._evaluate_due()

        dues = [call.args[1] for call in svc._coordinator.request_due.call_args_list]
        self.assertEqual(dues, [datetime(2025, 6, 1, 12, 0, tzinfo=UTC)])

    def test_periodic_later_due_point_still_fires_after_dedup(self):
        """FR-20261011-004 reverse check: dedup must not suppress the next day."""
        svc = self._make_service(ready=True)
        svc._store.add(
            _make_task(
                schedule_id="s-next",
                kind=TriggerKind.DAILY,
                local_time=datetime(2025, 6, 1, 12, 0).time(),
            )
        )
        svc._coordinator.request_due.return_value = "running"

        svc._evaluate_due()  # handles 2025-06-01 12:00:00
        self._clock._now = datetime(2025, 6, 1, 12, 0, 2)
        svc._evaluate_due()  # same due point -> suppressed
        self._clock._now = datetime(2025, 6, 2, 12, 0, 0)
        svc._evaluate_due()  # next day's due -> must fire

        dues = [call.args[1] for call in svc._coordinator.request_due.call_args_list]
        self.assertEqual(
            dues,
            [
                datetime(2025, 6, 1, 12, 0, tzinfo=UTC),
                datetime(2025, 6, 2, 12, 0, tzinfo=UTC),
            ],
        )

    def test_periodic_task_skipped_records_miss(self):
        """A periodic task that is skipped (busy) records a miss."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-daily2",
            kind=TriggerKind.DAILY,
            local_time=datetime(2025, 6, 1, 12, 0).time(),
        )
        svc._store.add(t1)
        svc._coordinator.request_due.return_value = "skipped"

        svc._evaluate_due()

        hist = svc._store.history_for("s-daily2")
        self.assertTrue(any(h.reason == "skipped_while_busy" for h in hist))

    def test_periodic_task_not_due_skipped(self):
        """A periodic task whose next trigger is in the future is not fired."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-future",
            kind=TriggerKind.DAILY,
            local_time=datetime(2025, 6, 1, 18, 0).time(),  # 6pm, far future
        )
        svc._store.add(t1)

        svc._evaluate_due()

        svc._coordinator.request_due.assert_not_called()

    def test_single_task_running_skipped(self):
        """SINGLE task already in _running is skipped (line 343)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-single-run",
            kind=TriggerKind.SINGLE,
            once_at_local=datetime(2025, 6, 1, 10, 0),
        )
        svc._store.add(t1)
        svc._running["s-single-run"] = {"run_id": "r1"}

        svc._evaluate_due()

        svc._coordinator.request_due.assert_not_called()

    def test_single_task_skipped_records_miss(self):
        """SINGLE task with result 'skipped' records skipped_while_busy (line 357-359)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-single-skip",
            kind=TriggerKind.SINGLE,
            once_at_local=datetime(2025, 6, 1, 10, 0),
        )
        svc._store.add(t1)
        svc._coordinator.request_due.return_value = "skipped"

        svc._evaluate_due()

        hist = svc._store.history_for("s-single-skip")
        self.assertTrue(any(h.reason == "skipped_while_busy" for h in hist))

    def test_single_task_running_records_start(self):
        """SINGLE task with result 'running' records run start (line 357)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-single-run2",
            kind=TriggerKind.SINGLE,
            once_at_local=datetime(2025, 6, 1, 10, 0),
        )
        svc._store.add(t1)
        svc._coordinator.request_due.return_value = "running"

        svc._evaluate_due()

        svc._coordinator.request_due.assert_called_once()
        self.assertIn("s-single-run2", svc._running)

    def test_single_task_outside_active_window_is_not_launched(self):
        """SINGLE 到期点落在 active_from/active_until 之外时不得真实启动
        （FR-20261012-541），且必须留痕而不是静默跳过。

        ``next_due_local`` 对周期任务统一应用窗口，SINGLE 分支此前完全绕过，
        导致窗口外仍启动、UI“下次执行”为空。
        """
        cases = (
            (
                "window_closed",
                "s-single-closed",
                datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
                datetime(2025, 6, 1, 9, 30, tzinfo=UTC),
            ),
            (
                "window_not_started",
                "s-single-future",
                datetime(2025, 6, 1, 11, 0, tzinfo=UTC),
                None,
            ),
        )
        for label, schedule_id, active_from, active_until in cases:
            with self.subTest(case=label):
                svc = self._make_service(ready=True)
                task = _make_task(
                    schedule_id=schedule_id,
                    kind=TriggerKind.SINGLE,
                    once_at_local=datetime(2025, 6, 1, 10, 0),
                    active_from=active_from,
                    active_until=active_until,
                )
                svc._store.add(task)

                svc._evaluate_due()

                svc._coordinator.request_due.assert_not_called()
                self.assertNotIn(schedule_id, svc._running)
                history = svc._store.history_for(schedule_id)
                self.assertTrue(
                    history,
                    f"{label}: 窗口外的 SINGLE 到期点必须留痕，不得静默跳过",
                )
                self.assertTrue(
                    any(h.reason == "outside_active_window" for h in history),
                    f"{label}: 历史原因应为 outside_active_window，实际 {[h.reason for h in history]}",
                )

    def test_single_task_inside_active_window_launches(self):
        """窗口覆盖到期点的 SINGLE 任务必须正常启动（不得误伤）。"""
        svc = self._make_service(ready=True)
        task = _make_task(
            schedule_id="s-single-in-window",
            kind=TriggerKind.SINGLE,
            once_at_local=datetime(2025, 6, 1, 10, 0),
            active_from=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
            active_until=datetime(2025, 6, 1, 11, 0, tzinfo=UTC),
        )
        svc._store.add(task)

        svc._evaluate_due()

        svc._coordinator.request_due.assert_called_once()
        self.assertIn("s-single-in-window", svc._running)

    def test_single_task_without_window_launches_unchanged(self):
        """未设置生效窗口的 SINGLE 任务行为不变（无回归）。"""
        svc = self._make_service(ready=True)
        task = _make_task(
            schedule_id="s-single-no-window",
            kind=TriggerKind.SINGLE,
            once_at_local=datetime(2025, 6, 1, 10, 0),
        )
        svc._store.add(task)

        svc._evaluate_due()

        svc._coordinator.request_due.assert_called_once()
        self.assertIn("s-single-no-window", svc._running)


# ---------------------------------------------------------------------------
# _fire_startup_tasks + _run_startup (lines 377, 380-386, 389-400)
# ---------------------------------------------------------------------------

class TestStartupTasks(_ServiceTestBase):

    def test_fire_startup_tasks_immediate(self):
        """STARTUP task with delay=0 fires immediately."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-startup",
            kind=TriggerKind.STARTUP,
            startup_delay_seconds=0.0,
        )
        svc._store.add(t1)
        svc._coordinator.request_due.return_value = "running"

        svc._fire_startup_tasks()

        svc._coordinator.request_due.assert_called_once()
        self.assertIn("s-startup", svc._running)

    def test_fire_startup_tasks_idempotent(self):
        """_fire_startup_tasks only fires once (line 377)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-startup2",
            kind=TriggerKind.STARTUP,
            startup_delay_seconds=0.0,
        )
        svc._store.add(t1)
        svc._coordinator.request_due.return_value = "running"

        svc._fire_startup_tasks()
        svc._fire_startup_tasks()  # second call is no-op

        self.assertEqual(svc._coordinator.request_due.call_count, 1)

    def test_fire_startup_skips_non_startup_tasks(self):
        """Non-STARTUP tasks are skipped in _fire_startup_tasks (line 381)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-daily-mix",
            kind=TriggerKind.DAILY,
        )
        t2 = _make_task(
            schedule_id="s-startup-mix",
            kind=TriggerKind.STARTUP,
            startup_delay_seconds=0.0,
        )
        svc._store.add(t1)
        svc._store.add(t2)
        svc._coordinator.request_due.return_value = "running"

        svc._fire_startup_tasks()

        # Only the STARTUP task should trigger request_due
        self.assertEqual(svc._coordinator.request_due.call_count, 1)
        self.assertNotIn("s-daily-mix", svc._running)
        self.assertIn("s-startup-mix", svc._running)

    def test_fire_startup_with_delay_uses_timer(self):
        """STARTUP task with delay > 0 uses QTimer.singleShot (line 384)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-startup-delay",
            kind=TriggerKind.STARTUP,
            startup_delay_seconds=5.0,
        )
        svc._store.add(t1)

        with patch("fukua_rpa.scheduled_task_service.QTimer.singleShot") as mock_ss:
            svc._fire_startup_tasks()
            mock_ss.assert_called_once()
            args = mock_ss.call_args[0]
            self.assertEqual(args[0], 5000)  # 5s in ms

    def test_run_startup_disabled_task_noop(self):
        """_run_startup skips disabled task (line 389)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-disabled",
            kind=TriggerKind.STARTUP,
            enabled=False,
        )
        svc._store.add(t1)

        svc._run_startup(t1)

        svc._coordinator.request_due.assert_not_called()

    def test_run_startup_removed_task_noop(self):
        """_run_startup skips task no longer in store (line 389)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-removed",
            kind=TriggerKind.STARTUP,
        )
        # Do NOT add to store

        svc._run_startup(t1)

        svc._coordinator.request_due.assert_not_called()

    def test_run_startup_skipped_retries_once(self):
        """_run_startup retries once on 'skipped' (lines 395-400)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-retry",
            kind=TriggerKind.STARTUP,
        )
        svc._store.add(t1)
        svc._coordinator.request_due.return_value = "skipped"

        with patch("fukua_rpa.scheduled_task_service.QTimer.singleShot") as mock_ss:
            svc._run_startup(t1)
            mock_ss.assert_called_once()
            args = mock_ss.call_args[0]
            self.assertEqual(args[0], 2000)  # 2s retry delay

        # Second call should NOT retry again
        with patch("fukua_rpa.scheduled_task_service.QTimer.singleShot") as mock_ss2:
            svc._run_startup(t1)
            mock_ss2.assert_not_called()


# ---------------------------------------------------------------------------
# MissedRunDetector.check + reference_local
# ---------------------------------------------------------------------------

class TestCheckMissedTasks(_ServiceTestBase):

    def test_missed_check_idempotent(self):
        """MissedRunDetector.check runs only once (missed_checked guard)."""
        svc = self._make_service(ready=True)
        svc._missed_detector.missed_checked = True
        # Should return immediately
        svc._missed_detector.check()
        # No error, no action

    def test_missed_check_skips_startup_tasks(self):
        """STARTUP tasks are excluded from missed check (line 425)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-startup-miss",
            kind=TriggerKind.STARTUP,
        )
        svc._store.add(t1)

        svc._missed_detector.check()

        svc._coordinator.request_due.assert_not_called()

    def test_missed_check_reference_in_future_skips(self):
        """Task with reference >= now is skipped (line 428)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-future-ref",
            kind=TriggerKind.DAILY,
            local_time=datetime(2025, 6, 1, 9, 0).time(),
        )
        svc._store.add(t1)

        # Mock reference to be in the future
        with patch.object(svc._missed_detector, "reference_local",
                          return_value=self._now + timedelta(hours=1)):
            svc._missed_detector.check()

        svc._coordinator.request_due.assert_not_called()

    def test_missed_check_no_missed_due_skips(self):
        """Task with no missed trigger point is skipped (line 431)."""
        svc = self._make_service(ready=True)
        # Task due at 18:00, reference at 11:00 (before now=12:00),
        # next trigger after 11:00 is 18:00 which is > now, so no missed
        t1 = _make_task(
            schedule_id="s-no-miss",
            kind=TriggerKind.DAILY,
            local_time=datetime(2025, 6, 1, 18, 0).time(),
        )
        svc._store.add(t1)

        with patch.object(svc._missed_detector, "reference_local",
                          return_value=datetime(2025, 6, 1, 11, 0)):
            svc._missed_detector.check()

        svc._coordinator.request_due.assert_not_called()

    def test_missed_reference_from_history(self):
        """reference_local uses last history record."""
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-hist-ref", kind=TriggerKind.DAILY)
        svc._store.add(t1)

        ended = datetime(2025, 5, 31, 10, 0, tzinfo=UTC)
        svc._store.add_history(ScheduledRunHistory(
            history_id="h1",
            schedule_id="s-hist-ref",
            run_id="r1",
            profile_id="p1",
            due_at_utc=datetime(2025, 5, 31, 9, 0, tzinfo=UTC),
            started_at_utc=datetime(2025, 5, 31, 9, 0, tzinfo=UTC),
            ended_at_utc=ended,
            duration_ms=60000,
            outcome="success",
            reason="",
            application_log_reference="",
        ))

        ref = svc._missed_detector.reference_local(t1)
        self.assertIsNotNone(ref)
        # FakeClock.from_utc strips tzinfo
        self.assertEqual(ref, datetime(2025, 5, 31, 10, 0))

    def test_missed_reference_from_file_ctime(self):
        """reference_local falls back to file ctime."""
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-ctime-ref", kind=TriggerKind.DAILY)
        svc._store.add(t1)
        # No history records

        ref = svc._missed_detector.reference_local(t1)
        # Should return something based on file ctime (the tasks file exists)
        self.assertIsNotNone(ref)

    def test_missed_reference_no_file_returns_none(self):
        """reference_local returns None when file missing."""
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-nofile", kind=TriggerKind.DAILY)
        svc._store.add(t1)
        # Point to a nonexistent path
        svc._tasks_path = os.path.join(self._tmp_dir, "nonexist.json")

        ref = svc._missed_detector.reference_local(t1)
        self.assertIsNone(ref)

    def test_missed_reference_zero_ctime_returns_none(self):
        """reference_local returns None when ctime <= 0."""
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-zero-ctime", kind=TriggerKind.DAILY)
        svc._store.add(t1)

        with patch("os.path.getctime", return_value=0):
            ref = svc._missed_detector.reference_local(t1)
        self.assertIsNone(ref)


# ---------------------------------------------------------------------------
# MissedRunDetector.apply_policy
# ---------------------------------------------------------------------------

class TestApplyMissedPolicy(_ServiceTestBase):

    def test_missed_skip_records_miss(self):
        """MISSED_SKIP policy records a miss (lines 481-482)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-skip-pol",
            kind=TriggerKind.DAILY,
            missed_policy=MISSED_SKIP,
        )
        svc._store.add(t1)
        missed_due = datetime(2025, 6, 1, 9, 0)

        svc._missed_detector.apply_policy(t1, missed_due)

        hist = svc._store.history_for("s-skip-pol")
        self.assertTrue(any(h.reason == "missed_while_closed" for h in hist))

    def test_missed_catchup_once_runs(self):
        """MISSED_CATCHUP_ONCE policy requests a run (lines 494-507)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-catchup",
            kind=TriggerKind.DAILY,
            missed_policy=MISSED_CATCHUP_ONCE,
        )
        svc._store.add(t1)
        svc._coordinator.request_due.return_value = "running"
        missed_due = datetime(2025, 6, 1, 9, 0)

        svc._missed_detector.apply_policy(t1, missed_due)

        svc._coordinator.request_due.assert_called_once()
        call_kwargs = svc._coordinator.request_due.call_args
        self.assertEqual(call_kwargs[1].get("conflict_policy_override") or
                         (call_kwargs[0][2] if len(call_kwargs[0]) > 2 else None),
                         CONFLICT_CATCHUP_ONCE)

    def test_missed_ask_emits_signal(self):
        """MISSED_ASK policy emits missed_detected (lines 483-493)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-ask-pol",
            kind=TriggerKind.DAILY,
            missed_policy=MISSED_ASK,
            name="ask-task",
        )
        svc._store.add(t1)
        missed_due = datetime(2025, 6, 1, 9, 0)

        signals = []
        svc.missed_detected.connect(lambda sid, name: signals.append((sid, name)))

        svc._missed_detector.apply_policy(t1, missed_due)

        self.assertEqual(signals, [("s-ask-pol", "ask-task")])
        hist = svc._store.history_for("s-ask-pol")
        self.assertTrue(any(h.reason == "missed_ask_pending" for h in hist))

    def test_unknown_policy_falls_back_to_skip(self):
        """Unknown policy value falls back to skip semantics (line 509)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-unknown-pol",
            kind=TriggerKind.DAILY,
            missed_policy="bogus_policy",
        )
        svc._store.add(t1)
        missed_due = datetime(2025, 6, 1, 9, 0)

        svc._missed_detector.apply_policy(t1, missed_due)

        hist = svc._store.history_for("s-unknown-pol")
        self.assertTrue(any(h.reason == "missed_while_closed" for h in hist))


# ---------------------------------------------------------------------------
# _on_coord_run_started (lines 515-521)
# ---------------------------------------------------------------------------

class TestOnCoordRunStarted(_ServiceTestBase):

    def test_records_running_and_emits(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-run-start", kind=TriggerKind.DAILY)
        svc._store.add(t1)

        started_signals = []
        changed_signals = []
        svc.run_started.connect(lambda sid: started_signals.append(sid))
        svc.tasks_changed.connect(lambda: changed_signals.append(True))

        svc._on_coord_run_started("s-run-start", "run-001")

        self.assertIn("s-run-start", svc._running)
        self.assertEqual(svc._running["s-run-start"]["run_id"], "run-001")
        self.assertEqual(started_signals, ["s-run-start"])
        self.assertEqual(len(changed_signals), 1)


# ---------------------------------------------------------------------------
# _on_coord_run_finished (lines 524-547)
# ---------------------------------------------------------------------------

class TestOnCoordRunFinished(_ServiceTestBase):

    def test_records_history_and_emits(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-run-fin", kind=TriggerKind.DAILY, profile_id="p1")
        svc._store.add(t1)

        # Simulate a running entry
        started_at = datetime(2025, 6, 1, 11, 0, tzinfo=UTC)
        svc._running["s-run-fin"] = {
            "run_id": "run-002",
            "started_at": started_at,
            "due_at": datetime(2025, 6, 1, 11, 0, tzinfo=UTC),
        }

        finished_signals = []
        svc.run_finished.connect(lambda sid, oc: finished_signals.append((sid, oc)))

        svc._on_coord_run_finished("s-run-fin", "run-002", "success")

        self.assertNotIn("s-run-fin", svc._running)
        self.assertFalse(svc._last_failed["s-run-fin"])
        self.assertEqual(finished_signals, [("s-run-fin", "success")])

        hist = svc._store.history_for("s-run-fin")
        self.assertTrue(len(hist) >= 1)
        self.assertEqual(hist[-1].outcome, "success")
        self.assertEqual(hist[-1].run_id, "run-002")

    def test_failed_outcome_sets_last_failed(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-run-fail", kind=TriggerKind.DAILY)
        svc._store.add(t1)
        svc._running["s-run-fail"] = {
            "run_id": "run-003",
            "started_at": datetime(2025, 6, 1, 11, 0, tzinfo=UTC),
            "due_at": None,
        }

        svc._on_coord_run_finished("s-run-fail", "run-003", "crashed")

        self.assertTrue(svc._last_failed["s-run-fail"])

    def test_finished_without_running_info(self):
        """run_finished for unknown schedule_id still emits (line 524 pop None)."""
        svc = self._make_service(ready=True)

        finished_signals = []
        svc.run_finished.connect(lambda sid, oc: finished_signals.append((sid, oc)))

        svc._on_coord_run_finished("s-ghost", "run-x", "success")

        self.assertEqual(finished_signals, [("s-ghost", "success")])


# ---------------------------------------------------------------------------
# _on_coord_watchdog_timeout (lines 555-579)
# ---------------------------------------------------------------------------

class TestOnCoordWatchdogTimeout(_ServiceTestBase):

    def test_watchdog_records_history_and_emits(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-watchdog", kind=TriggerKind.DAILY, profile_id="p1")
        svc._store.add(t1)

        started_at = datetime(2025, 6, 1, 10, 0, tzinfo=UTC)
        svc._running["s-watchdog"] = {
            "run_id": "run-wd",
            "started_at": started_at,
            "due_at": datetime(2025, 6, 1, 10, 0, tzinfo=UTC),
        }

        finished_signals = []
        svc.run_finished.connect(lambda sid, oc: finished_signals.append((sid, oc)))

        svc._on_coord_watchdog_timeout("s-watchdog", "run-wd")

        self.assertNotIn("s-watchdog", svc._running)
        self.assertTrue(svc._last_failed["s-watchdog"])
        self.assertEqual(finished_signals, [("s-watchdog", "watchdog_timeout")])

        hist = svc._store.history_for("s-watchdog")
        self.assertTrue(len(hist) >= 1)
        self.assertEqual(hist[-1].outcome, "watchdog_timeout")
        self.assertEqual(hist[-1].reason, "run_never_reported_completion")

    def test_watchdog_without_running_info(self):
        """Watchdog for unknown schedule_id still emits."""
        svc = self._make_service(ready=True)

        finished_signals = []
        svc.run_finished.connect(lambda sid, oc: finished_signals.append((sid, oc)))

        svc._on_coord_watchdog_timeout("s-ghost-wd", "run-gw")

        self.assertTrue(svc._last_failed["s-ghost-wd"])
        self.assertEqual(finished_signals, [("s-ghost-wd", "watchdog_timeout")])


# ---------------------------------------------------------------------------
# task_status refinements (lines 641, 643)
# ---------------------------------------------------------------------------

class TestTaskStatusRefinements(_ServiceTestBase):

    def test_pending_catchup_status(self):
        """WAITING task in coordinator pending_ids -> PENDING_CATCHUP (line 641)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-pending", kind=TriggerKind.DAILY)
        svc._store.add(t1)
        svc._coordinator.pending_ids = {"s-pending"}

        status = svc.task_status(t1)
        self.assertEqual(status, ScheduleStatus.PENDING_CATCHUP)

    def test_last_run_failed_status(self):
        """WAITING task with _last_failed -> LAST_RUN_FAILED (line 643)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-failed", kind=TriggerKind.DAILY)
        svc._store.add(t1)
        svc._coordinator.pending_ids = set()
        svc._last_failed["s-failed"] = True

        status = svc.task_status(t1)
        self.assertEqual(status, ScheduleStatus.LAST_RUN_FAILED)

    def test_running_status(self):
        """Task in _running -> RUNNING."""
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-running", kind=TriggerKind.DAILY)
        svc._store.add(t1)
        svc._running["s-running"] = {"run_id": "r1"}

        status = svc.task_status(t1)
        self.assertEqual(status, ScheduleStatus.RUNNING)


# ---------------------------------------------------------------------------
# FR-20260812 建议-架构-5: 批量启停/删除（单次通知 + 单次重算）
# ---------------------------------------------------------------------------

class TestBatchMutations(_ServiceTestBase):
    def test_set_enabled_many_persists_and_emits_once(self):
        svc = self._make_service(ready=True)
        for sid in ("s-1", "s-2", "s-3"):
            svc._store.add(
                _make_task(schedule_id=sid, kind=TriggerKind.DAILY)
            )
        signals = []
        svc.tasks_changed.connect(lambda: signals.append(True))
        svc.set_enabled_many(["s-1", "s-2"], False)
        self.assertEqual(len(signals), 1)
        self.assertFalse(svc._store.get("s-1").enabled)
        self.assertFalse(svc._store.get("s-2").enabled)
        self.assertTrue(svc._store.get("s-3").enabled)

    def test_set_enabled_many_empty_is_noop(self):
        svc = self._make_service(ready=True)
        signals = []
        svc.tasks_changed.connect(lambda: signals.append(True))
        svc.set_enabled_many([], True)
        self.assertEqual(signals, [])

    def test_remove_many_persists_and_cleans_running(self):
        svc = self._make_service(ready=True)
        for sid in ("s-a", "s-b", "s-c"):
            svc._store.add(
                _make_task(schedule_id=sid, kind=TriggerKind.DAILY)
            )
        svc._running["s-a"] = {"run_id": "r1"}
        signals = []
        svc.tasks_changed.connect(lambda: signals.append(True))
        svc.remove_many(["s-a", "s-c"])
        self.assertEqual(len(signals), 1)
        self.assertIsNone(svc._store.get("s-a"))
        self.assertIsNotNone(svc._store.get("s-b"))
        self.assertIsNone(svc._store.get("s-c"))
        self.assertNotIn("s-a", svc._running)

    def test_import_tasks_reports_skipped_entries(self):
        """FR-20260812 建议-体验-3: import_tasks 通过 report_errors 披露
        被跳过的条目原因，供 UI 在成功提示中展示。"""
        svc = self._make_service(ready=True)
        good = _make_task(schedule_id="s-ok", kind=TriggerKind.DAILY).to_dict()
        bad = {"schedule_id": "s-bad", "name": "缺 trigger"}  # 缺 trigger → from_dict 抛 KeyError
        skipped: list[str] = []
        imported = svc.import_tasks(
            [good, bad], default_disabled=True, report_errors=skipped
        )
        self.assertEqual(len(imported), 1)
        self.assertEqual(len(skipped), 1)
        self.assertIn("第 2 条", skipped[0])
        self.assertIn("trigger", skipped[0])


# ---------------------------------------------------------------------------
# next_due_local + future_runs (lines 649, 654)
# ---------------------------------------------------------------------------

class TestNextDueLocalAndFutureRuns(_ServiceTestBase):

    def test_next_due_local_disabled_returns_none(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-dis", kind=TriggerKind.DAILY, enabled=False)
        svc._store.add(t1)

        result = svc.next_due_local(t1)
        self.assertIsNone(result)

    def test_next_due_local_enabled(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-en",
            kind=TriggerKind.DAILY,
            local_time=datetime(2025, 6, 1, 18, 0).time(),
        )
        svc._store.add(t1)

        result = svc.next_due_local(t1)
        self.assertIsNotNone(result)
        self.assertEqual(result, datetime(2025, 6, 1, 18, 0))

    def test_future_runs(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(
            schedule_id="s-fr",
            kind=TriggerKind.DAILY,
            local_time=datetime(2025, 6, 1, 9, 0).time(),
        )
        svc._store.add(t1)

        runs = svc.future_runs(t1, count=3)
        self.assertEqual(len(runs), 3)
        # First run should be tomorrow 09:00 (today's 09:00 already passed)
        self.assertEqual(runs[0], datetime(2025, 6, 2, 9, 0))


# ---------------------------------------------------------------------------
# list_view + _last_run_info (lines 709-710, 729-730)
# ---------------------------------------------------------------------------

class TestListViewAndLastRunInfo(_ServiceTestBase):

    def test_list_view_basic(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-lv1", kind=TriggerKind.DAILY)
        svc._store.add(t1)

        rows = svc.list_view()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task"].schedule_id, "s-lv1")
        self.assertIn("status", rows[0])
        self.assertIn("next_due", rows[0])
        self.assertIn("last_run", rows[0])

    def test_list_view_running_override(self):
        """Running task shows live 'running' last_run (lines 709-710)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-lv-run", kind=TriggerKind.DAILY)
        svc._store.add(t1)
        svc._running["s-lv-run"] = {
            "run_id": "r1",
            "started_at": datetime(2025, 6, 1, 11, 59, tzinfo=UTC),
            "due_at": None,
        }

        rows = svc.list_view()
        self.assertEqual(rows[0]["last_run"]["outcome"], "running")

    def test_last_run_info_from_history(self):
        """_last_run_info returns last history record (lines 729-730)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-lri", kind=TriggerKind.DAILY)
        svc._store.add(t1)

        svc._store.add_history(ScheduledRunHistory(
            history_id="h1",
            schedule_id="s-lri",
            run_id="r1",
            profile_id="p1",
            due_at_utc=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
            started_at_utc=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
            ended_at_utc=datetime(2025, 6, 1, 9, 5, tzinfo=UTC),
            duration_ms=300000,
            outcome="success",
            reason="",
            application_log_reference="",
        ))

        info = svc._last_run_info("s-lri")
        self.assertIsNotNone(info)
        self.assertEqual(info["outcome"], "success")
        self.assertEqual(info["duration_ms"], 300000)

    def test_last_run_info_empty_history(self):
        svc = self._make_service(ready=True)
        info = svc._last_run_info("s-nonexist")
        self.assertIsNone(info)


# ---------------------------------------------------------------------------
# summary (lines 755, 759-760, 767-769)
# ---------------------------------------------------------------------------

class TestSummary(_ServiceTestBase):

    def test_summary_basic(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-sum1", kind=TriggerKind.DAILY)
        t2 = _make_task(schedule_id="s-sum2", kind=TriggerKind.DAILY, enabled=False)
        svc._store.add(t1)
        svc._store.add(t2)

        result = svc.summary()
        self.assertEqual(result["service_status"], "running")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["enabled"], 1)
        self.assertIn("uptime_seconds", result)
        self.assertIn("last_check_ts", result)

    def test_summary_with_next_task_cache(self):
        """summary includes next_task from cache (lines 759-760)."""
        svc = self._make_service(ready=True)
        svc._next_task_cache = ("s1", "my-task", datetime(2025, 6, 1, 14, 0))

        result = svc.summary()
        self.assertIsNotNone(result["next_task"])
        self.assertEqual(result["next_task"]["schedule_id"], "s1")
        self.assertEqual(result["next_task"]["name"], "my-task")

    def test_summary_with_running_task(self):
        """summary includes current_running (lines 767-769)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-run-sum", kind=TriggerKind.DAILY, name="runner")
        svc._store.add(t1)
        svc._running["s-run-sum"] = {"run_id": "r1"}

        result = svc.summary()
        self.assertIsNotNone(result["current_running"])
        self.assertEqual(result["current_running"]["schedule_id"], "s-run-sum")
        self.assertEqual(result["current_running"]["name"], "runner")

    def test_summary_errors_count(self):
        """summary counts unhealthy tasks (line 755)."""
        svc = self._make_service(ready=True)
        # Task with no profile -> PROFILE_MISSING
        t1 = _make_task(schedule_id="s-err", kind=TriggerKind.DAILY, profile_id="")
        svc._store.add(t1)

        result = svc.summary()
        self.assertGreaterEqual(result["errors"], 1)


# ---------------------------------------------------------------------------
# Mutations: update_task, remove_task (lines 793-795, 799-802)
# ---------------------------------------------------------------------------

class TestMutations(_ServiceTestBase):

    def test_update_task(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-upd", kind=TriggerKind.DAILY, name="old-name")
        svc._store.add(t1)

        t1.name = "new-name"
        signals = []
        svc.tasks_changed.connect(lambda: signals.append(True))

        svc.update_task(t1)

        self.assertEqual(svc._store.get("s-upd").name, "new-name")
        self.assertEqual(len(signals), 1)

    def test_remove_task(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-rm", kind=TriggerKind.DAILY)
        svc._store.add(t1)
        svc._running["s-rm"] = {"run_id": "r1"}

        signals = []
        svc.tasks_changed.connect(lambda: signals.append(True))

        svc.remove_task("s-rm")

        self.assertIsNone(svc._store.get("s-rm"))
        self.assertNotIn("s-rm", svc._running)
        self.assertEqual(len(signals), 1)

    def test_set_enabled(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-tog", kind=TriggerKind.DAILY, enabled=True)
        svc._store.add(t1)

        svc.set_enabled("s-tog", False)
        self.assertFalse(svc._store.get("s-tog").enabled)

        svc.set_enabled("s-tog", True)
        self.assertTrue(svc._store.get("s-tog").enabled)


# ---------------------------------------------------------------------------
# duplicate_task (lines 819-830)
# ---------------------------------------------------------------------------

class TestDuplicateTask(_ServiceTestBase):

    def test_duplicate_creates_disabled_copy(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-orig", kind=TriggerKind.DAILY, name="original")
        svc._store.add(t1)

        new_task = svc.duplicate_task("s-orig")

        self.assertIsNotNone(new_task)
        self.assertNotEqual(new_task.schedule_id, "s-orig")
        self.assertEqual(new_task.name, "original (副本)")
        self.assertFalse(new_task.enabled)
        # Original still exists
        self.assertIsNotNone(svc._store.get("s-orig"))
        # New task is in store
        self.assertIsNotNone(svc._store.get(new_task.schedule_id))

    def test_duplicate_nonexistent_returns_none(self):
        svc = self._make_service(ready=True)
        result = svc.duplicate_task("s-nonexist")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# export_task (lines 834-835)
# ---------------------------------------------------------------------------

class TestExportTask(_ServiceTestBase):

    def test_export_existing(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-exp", kind=TriggerKind.DAILY, name="export-me")
        svc._store.add(t1)

        result = svc.export_task("s-exp")
        self.assertIsNotNone(result)
        self.assertEqual(result["schedule_id"], "s-exp")
        self.assertEqual(result["name"], "export-me")

    def test_export_nonexistent_returns_none(self):
        svc = self._make_service(ready=True)
        result = svc.export_task("s-nope")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# import_tasks (lines 851-870)
# ---------------------------------------------------------------------------

class TestImportTasks(_ServiceTestBase):

    def test_import_valid_tasks(self):
        svc = self._make_service(ready=True)
        task_dict = _make_task(
            schedule_id="s-imp1",
            kind=TriggerKind.DAILY,
            name="imported",
        ).to_dict()

        imported = svc.import_tasks([task_dict])

        self.assertEqual(len(imported), 1)
        self.assertFalse(imported[0].enabled)  # default_disabled=True
        self.assertIsNotNone(svc._store.get(imported[0].schedule_id))

    def test_import_conflicting_id_regenerated(self):
        """Conflicting schedule_id is regenerated (line 862)."""
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-exist", kind=TriggerKind.DAILY)
        svc._store.add(t1)

        task_dict = _make_task(
            schedule_id="s-exist",
            kind=TriggerKind.DAILY,
            name="conflict",
        ).to_dict()

        imported = svc.import_tasks([task_dict])

        self.assertEqual(len(imported), 1)
        # New id should differ from existing
        self.assertNotEqual(imported[0].schedule_id, "s-exist")
        # Original still intact
        self.assertIsNotNone(svc._store.get("s-exist"))

    def test_import_malformed_skipped(self):
        """Malformed entries are skipped and logged (lines 856-858)."""
        svc = self._make_service(ready=True)
        bad_dict = {"no_schedule_id": True}  # missing required keys
        good_dict = _make_task(schedule_id="s-good", kind=TriggerKind.DAILY).to_dict()

        imported = svc.import_tasks([bad_dict, good_dict])

        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0].schedule_id, "s-good")

    def test_import_empty_list(self):
        svc = self._make_service(ready=True)
        imported = svc.import_tasks([])
        self.assertEqual(imported, [])

    def test_import_not_disabled_when_flag_false(self):
        """default_disabled=False preserves enabled state."""
        svc = self._make_service(ready=True)
        task_dict = _make_task(
            schedule_id="s-imp-en",
            kind=TriggerKind.DAILY,
            enabled=True,
        ).to_dict()

        imported = svc.import_tasks([task_dict], default_disabled=False)

        self.assertEqual(len(imported), 1)
        self.assertTrue(imported[0].enabled)


# ---------------------------------------------------------------------------
# clear_history (lines 874-876)
# ---------------------------------------------------------------------------

class TestClearHistory(_ServiceTestBase):

    def test_clear_history(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-clr", kind=TriggerKind.DAILY)
        svc._store.add(t1)
        svc._store.add_history(ScheduledRunHistory(
            history_id="h1",
            schedule_id="s-clr",
            run_id="r1",
            profile_id="p1",
            due_at_utc=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
            started_at_utc=datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
            ended_at_utc=datetime(2025, 6, 1, 9, 5, tzinfo=UTC),
            duration_ms=300000,
            outcome="success",
            reason="",
            application_log_reference="",
        ))

        signals = []
        svc.tasks_changed.connect(lambda: signals.append(True))

        svc.clear_history()

        self.assertEqual(svc._store.history_for("s-clr"), [])
        self.assertEqual(len(signals), 1)


# ---------------------------------------------------------------------------
# start() idempotent (line 154)
# ---------------------------------------------------------------------------

class TestStartIdempotent(_ServiceTestBase):

    def test_start_twice_is_noop(self):
        """Second start() call returns immediately (line 154)."""
        svc = self._make_service(ready=False)
        # Patch heavy internals to avoid side effects
        with patch.object(svc, "_recompute"), \
             patch.object(svc, "_fire_startup_tasks"), \
             patch.object(svc._missed_detector, "check"), \
             patch.object(svc, "_evaluate_due"), \
             patch("fukua_rpa.scheduled_task_service.register_processed_log_sink"), \
             patch("fukua_rpa.scheduled_task_service._CoordinatorProxy"):
            svc.start()
            self.assertTrue(svc._ready)
            # Second call
            svc.start()
            # _recompute called only once (from first start)
            self.assertEqual(svc._recompute.call_count, 1)


# ---------------------------------------------------------------------------
# shutdown (lines 183-193)
# ---------------------------------------------------------------------------

class TestShutdown(_ServiceTestBase):

    def test_shutdown_stops_timers_and_clears_pending(self):
        svc = self._make_service(ready=True)
        svc._timer.start(5000)
        svc._heartbeat.start()

        # Mock coordinator with inner _coordinator having _pending
        inner = MagicMock()
        inner._pending = {"s1": datetime(2025, 1, 1, tzinfo=UTC)}
        svc._coordinator._coordinator = inner

        svc.shutdown()

        self.assertFalse(svc._timer.isActive())
        self.assertFalse(svc._heartbeat.isActive())
        self.assertEqual(inner._pending, {})


# ---------------------------------------------------------------------------
# finalize_logging (lines 204-208)
# ---------------------------------------------------------------------------

class TestFinalizeLogging(_ServiceTestBase):

    def test_finalize_when_registered(self):
        svc = self._make_service(ready=False)
        svc._log_sink_registered = True

        with patch("fukua_rpa.scheduled_task_service.flush_logs") as mock_flush, \
             patch("fukua_rpa.scheduled_task_service.unregister_processed_log_sink") as mock_unreg:
            svc.finalize_logging(timeout=1.0)
            mock_flush.assert_called_once_with(timeout=1.0)
            mock_unreg.assert_called_once()
            self.assertFalse(svc._log_sink_registered)

    def test_finalize_when_not_registered_is_noop(self):
        svc = self._make_service(ready=False)
        svc._log_sink_registered = False

        with patch("fukua_rpa.scheduled_task_service.flush_logs") as mock_flush:
            svc.finalize_logging()
            mock_flush.assert_not_called()


# ---------------------------------------------------------------------------
# _resolve_task (line 246)
# ---------------------------------------------------------------------------

class TestResolveTask(_ServiceTestBase):

    def test_resolve_existing(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-resolve", kind=TriggerKind.DAILY)
        svc._store.add(t1)

        result = svc._resolve_task("s-resolve")
        self.assertIsNotNone(result)
        self.assertEqual(result.schedule_id, "s-resolve")

    def test_resolve_missing(self):
        svc = self._make_service(ready=True)
        result = svc._resolve_task("s-nonexist")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# running_info_for + uptime_seconds
# ---------------------------------------------------------------------------

class TestMiscQueries(_ServiceTestBase):

    def test_running_info_for_existing(self):
        svc = self._make_service(ready=True)
        svc._running["s1"] = {"run_id": "r1", "started_at": datetime.now(UTC)}

        info = svc.running_info_for("s1")
        self.assertIsNotNone(info)
        self.assertEqual(info["run_id"], "r1")

    def test_running_info_for_missing(self):
        svc = self._make_service(ready=True)
        info = svc.running_info_for("s-nonexist")
        self.assertIsNone(info)

    def test_uptime_seconds(self):
        svc = self._make_service(ready=True)
        svc._app_start_ts = time_mod.time() - 100
        self.assertGreaterEqual(svc.uptime_seconds(), 99)

    def test_task_by_id(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-byid", kind=TriggerKind.DAILY)
        svc._store.add(t1)
        self.assertIsNotNone(svc.task_by_id("s-byid"))
        self.assertIsNone(svc.task_by_id("s-nope"))

    def test_history_for(self):
        svc = self._make_service(ready=True)
        self.assertEqual(svc.history_for("s-empty"), [])

    def test_log_entries_for(self):
        svc = self._make_service(ready=True)
        entries = svc.log_entries_for("s-log")
        self.assertIsInstance(entries, list)

    def test_log_revision(self):
        svc = self._make_service(ready=True)
        rev = svc.log_revision()
        self.assertIsInstance(rev, int)


# ---------------------------------------------------------------------------
# add_task (lines 787-789)
# ---------------------------------------------------------------------------

class TestAddTask(_ServiceTestBase):

    def test_add_task_persists_and_emits(self):
        svc = self._make_service(ready=True)
        t1 = _make_task(schedule_id="s-add", kind=TriggerKind.DAILY, name="added")

        signals = []
        svc.tasks_changed.connect(lambda: signals.append(True))

        svc.add_task(t1)

        self.assertIsNotNone(svc._store.get("s-add"))
        self.assertEqual(len(signals), 1)


# ---------------------------------------------------------------------------
# FR-002: shutdown 必须是终态（ARCHITECTURE #85）
# ---------------------------------------------------------------------------

class TestShutdownTerminalState(_ServiceTestBase):
    """退出等待窗口内不得重挂计时器，也不得发起新运行。"""

    def test_recompute_after_shutdown_does_not_rearm_timer(self):
        svc = self._make_service(ready=True)
        # DAILY 默认 local_time=09:00，_now=12:00，下次触发在明日 -> 会重挂计时器
        svc._store.add(_make_task(kind=TriggerKind.DAILY, schedule_id="s-shut-0"))
        svc.shutdown()
        svc._recompute()
        self.assertFalse(
            svc._timer.isActive(),
            "shutdown 后 _recompute 不应重挂计时器（ARCHITECTURE #85）",
        )

    def test_evaluate_due_after_shutdown_does_not_request_runs(self):
        svc = self._make_service(ready=True)
        # SINGLE 默认 once_at_local=10:00，_now=12:00 -> 已到期且无历史，会发起运行
        task = _make_task(kind=TriggerKind.SINGLE, schedule_id="s-shut-1")
        svc._store.add(task)
        svc.shutdown()
        svc._evaluate_due()
        svc._coordinator.request_due.assert_not_called()

    def test_run_startup_after_shutdown_does_not_request_runs(self):
        svc = self._make_service(ready=True)
        task = _make_task(kind=TriggerKind.STARTUP, schedule_id="s-shut-2")
        svc._store.add(task)
        svc.shutdown()
        svc._run_startup(task)
        svc._coordinator.request_due.assert_not_called()

    def test_drain_start_failed_records_startup_failed_when_engine_idle(self):
        svc = self._make_service(ready=True)
        task = _make_task(kind=TriggerKind.DAILY, schedule_id="s-drain-1")
        svc._store.add(task)
        due = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

        svc._on_drain_start_failed("s-drain-1", due, False)

        hist = svc._store.history_for("s-drain-1")
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0].reason, "startup_failed")
        self.assertEqual(hist[0].outcome, "failed")

    def test_drain_start_failed_is_silent_when_engine_busy(self):
        """临时忙（P1-3）不得记账，否则 SINGLE 任务会被 has_handled 误判终态。"""

        svc = self._make_service(ready=True)
        task = _make_task(kind=TriggerKind.DAILY, schedule_id="s-drain-2")
        svc._store.add(task)
        due = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)

        svc._on_drain_start_failed("s-drain-2", due, True)

        self.assertEqual(svc._store.history_for("s-drain-2"), [])

    def test_request_due_after_shutdown_does_not_forward_to_coordinator(self):
        svc = self._make_service(ready=True)
        proxy = _CoordinatorProxy(MagicMock(), MagicMock(), svc._store)
        proxy._coordinator = MagicMock()
        proxy._coordinator.request_due.return_value = "running"
        svc._coordinator = proxy
        task = _make_task(kind=TriggerKind.STARTUP, schedule_id="s-shut-3")
        svc._store.add(task)

        svc.shutdown()

        self.assertTrue(
            proxy._shutdown_requested,
            "shutdown 必须把终态标志传到协调器代理（ARCHITECTURE #85）",
        )
        result = proxy.request_due(task, datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC))
        proxy._coordinator.request_due.assert_not_called()
        self.assertEqual(result, "failed")


# ---------------------------------------------------------------------------
# FR-SCOPE006-001: 启动窗口内「错过扫描」与「到期评估」的去重
# ---------------------------------------------------------------------------

# 06 路线阶段 2 判据②的矩阵：DAILY/WEEKLY × 三种策略 × 启动偏移 -5/-2/-1/0/+1 秒。
_STARTUP_WINDOW_OFFSETS = (-5, -2, -1, 0, 1)


class TestStartupMissedDueDeduplication(unittest.TestCase):
    """FR-SCOPE006-001: 启动窗口 (now-2s, now] 内的到期点不得被双重处理。

    权威缺陷（`05_权威报告.md` §3.6 / `06_改进路线.md` 阶段 2）：`start()`
    依次调用 `_missed_detector.check()`（错过扫描，参考点 = 最近一次历史结束
    时刻）与 `_evaluate_due()`（到期评估，参考点 = `now - 2 s`）。两者的时间窗
    在 `(now - 2 s, now]` 上重叠且互不排斥 ⇒ 策略为 `skip` / `ask` 的周期任务
    在错过扫描已记录「跳过 / 待询问」之后，仍被 `_evaluate_due` **真实启动**。

    本测试覆盖的边界（不得破坏）：

    * 真正的错过补跑（`catchup_once`）仍须触发，且每个到期点只触发一次；
    * `SINGLE` 任务原有的占位记录保护（修复前后均为 0 次）保持不变；
    * 去重只作用于 `start()` 的那一次评估，稳态到期触发不受影响。

    术语说明：`fukua_rpa/scheduled_task_model.py:93-95` 的错过策略取值域只有
    `skip` / `catchup_once` / `ask`，**不存在**名为 `run` 的取值；06 路线判据中
    的「`run` 仍触发」指「真正的错过补跑仍须触发」，故以 `catchup_once` 覆盖。

    计数口径为端到端：`execution_controller.prepare_and_launch_profile` 的调用
    次数，与 B 端到端集成探针 `evidence/fix_baseline/stage0/probe_startup_overlap.py`
    的 ARM 2 完全一致。修复前隔离副本实测（`stage0/stage2_fr006_red.txt`）：

    * `DAILY`/`WEEKLY` × `skip`/`ask` 在偏移 `-1 s` / `0` 各产生 **1 次真实启动**
      （策略被绕过）；`-5 s` / `-2 s` / `+1 s` 为 0。
    * `DAILY`/`WEEKLY` × `catchup_once` 在偏移 `-1 s` / `0` 仍只有 1 次真实启动
      （协调器的单运行互斥挡住了第二次请求），但会多出一条 `skipped_while_busy`
      的历史记录——即"同一起始窗口被评估两次"的直接痕迹。
    * `SINGLE` 的 `skip`/`ask` 全部为 0（占位记录保护），`catchup_once` 为 1。

    说明：`06_改进路线.md` 引用的 B 基准把 `catchup_once` 在 `-1 s`/`0` 记为
    "2 次真实启动"，那是 `request_due` 的调用次数（探针 ARM 1 的 spy 口径）。
    以真实启动为口径时该值恒为 1，本测试按端到端口径断言，并另行断言
    `skipped_while_busy` 痕迹消失。
    """

    def setUp(self):
        self._now = datetime(2026, 9, 23, 12, 0, 0)  # 周三

    def _start_service_with_task(self, kind: TriggerKind, policy: str, offset: int):
        """构造并 ``start()`` 一个只持有单个任务的服务。

        每个用例使用**独立**临时目录，避免 store 在用例之间累积任务而污染
        启动评估的计数。

        Returns:
            ``(svc, task, execution_controller)``。
        """
        tmp_dir = tempfile.mkdtemp(prefix="fr006-startup-")
        self.addCleanup(shutil.rmtree, tmp_dir, True)

        due = self._now + timedelta(seconds=offset)
        if kind == TriggerKind.DAILY:
            trigger = ScheduledTrigger(kind=TriggerKind.DAILY, local_time=due.time())
        elif kind == TriggerKind.WEEKLY:
            trigger = ScheduledTrigger(
                kind=TriggerKind.WEEKLY,
                local_time=due.time(),
                weekdays=frozenset({self._now.weekday()}),
            )
        elif kind == TriggerKind.SINGLE:
            trigger = ScheduledTrigger(kind=TriggerKind.SINGLE, once_at_local=due)
        else:  # pragma: no cover - 防御性分支
            raise ValueError(kind)

        task = ScheduledTask(
            schedule_id=f"{kind}-{policy}",
            name=f"{kind}-{policy}",
            profile_id="p1",
            profile_name_snapshot="P1",
            enabled=True,
            trigger=trigger,
            missed_policy=policy,
        )

        run_controller = MagicMock()
        run_controller.is_active = False
        run_controller.run_ended = MagicMock()
        execution_controller = MagicMock()
        execution_controller.prepare_and_launch_profile.return_value = True
        execution_controller.scheduled_health.return_value = (True, True, True)

        svc = ScheduledTaskService(
            run_controller=run_controller,
            execution_controller=execution_controller,
            base_dir=tmp_dir,
        )
        svc._clock = _FakeClock(self._now)
        svc._store.add(task)
        # 参考点 = 最近一次历史结束时刻（`MissedRunDetector.reference_local`）。
        # 置于 1 小时前，使 (now - 1h, now] 内的到期点成为「错过」候选。
        # outcome/reason 均为空：不构成 SINGLE 分支 has_handled 意义上的终态，
        # 否则 SINGLE 用例会被这条**测试脚手架**记录而非被测保护机制拦下。
        svc._store.add_history(
            ScheduledRunHistory(
                history_id="h1",
                schedule_id=task.schedule_id,
                run_id="",
                profile_id="p1",
                due_at_utc=(self._now - timedelta(hours=2)).replace(tzinfo=UTC),
                started_at_utc=None,
                ended_at_utc=(self._now - timedelta(hours=1)).replace(tzinfo=UTC),
                duration_ms=None,
                outcome="",
                reason="",
            )
        )
        self.addCleanup(svc.shutdown)
        with patch("fukua_rpa.scheduled_task_service.register_processed_log_sink"):
            svc.start()
        return svc, task, execution_controller

    def test_single_due_already_handled_at_startup_is_not_re_evaluated(self):
        """FR-20260930-007：SINGLE 到期点若已由启动错过扫描按策略处置（键在
        `_startup_handled_dues`），`_evaluate_due` 不得再次请求运行并记入一条
        并不存在的 `skipped_while_busy` 历史（修复前：协调器忙 → 二次请求 →
        虚假跳过记录）。"""

        tmp_dir = tempfile.mkdtemp(prefix="fr007-single-dedup-")
        self.addCleanup(shutil.rmtree, tmp_dir, True)

        due = self._now - timedelta(seconds=1)
        trigger = ScheduledTrigger(kind=TriggerKind.SINGLE, once_at_local=due)
        task = ScheduledTask(
            schedule_id="SINGLE-catchup",
            name="SINGLE-catchup",
            profile_id="p1",
            profile_name_snapshot="P1",
            enabled=True,
            trigger=trigger,
            missed_policy="catchup_once",
        )

        run_controller = MagicMock()
        run_controller.is_active = False
        run_controller.run_ended = MagicMock()
        execution_controller = MagicMock()
        execution_controller.prepare_and_launch_profile.return_value = True
        execution_controller.scheduled_health.return_value = (True, True, True)

        svc = ScheduledTaskService(
            run_controller=run_controller,
            execution_controller=execution_controller,
            base_dir=tmp_dir,
        )
        svc._clock = _FakeClock(self._now)
        svc._store.add(task)
        self.addCleanup(svc.shutdown)

        due_utc = svc._clock.to_utc(due)
        # 该到期点已由启动错过扫描按 catchup_once 处置并入队。
        svc._startup_handled_dues.add((task.schedule_id, due_utc))
        # 协调器忙碌：任何再次请求都会返回 skipped 并记 skipped_while_busy。
        svc._coordinator = MagicMock()
        svc._coordinator.request_due.return_value = "skipped"

        svc._evaluate_due()

        svc._coordinator.request_due.assert_not_called()
        hist = svc._store.history_for(task.schedule_id)
        self.assertFalse(
            any(h.reason == "skipped_while_busy" for h in hist),
            "已按策略处置的 SINGLE 到期点不得再产生虚假 skipped_while_busy",
        )

    # -- 判据②（前半）-----------------------------------------------------

    def test_skip_and_ask_never_launch_inside_startup_window(self):
        """`skip` / `ask` 在启动窗口内必须为 0 次真实启动。

        修复前（RED）：DAILY/WEEKLY × skip/ask 在偏移 `-1 s` 与 `0` 各产生
        1 次真实启动——用户明确选择「跳过 / 询问」却被绕过。
        """
        for kind in (TriggerKind.DAILY, TriggerKind.WEEKLY):
            for policy in (MISSED_SKIP, MISSED_ASK):
                for offset in _STARTUP_WINDOW_OFFSETS:
                    with self.subTest(kind=kind, policy=policy, offset=offset):
                        _svc, _task, exec_ctrl = self._start_service_with_task(
                            kind, policy, offset
                        )
                        self.assertEqual(
                            exec_ctrl.prepare_and_launch_profile.call_count,
                            0,
                            f"{kind}/{policy} 在启动偏移 {offset:+d}s 仍被真实启动："
                            "启动错过扫描与到期评估对同一起始窗口重复处理",
                        )

    def test_skip_and_ask_still_record_the_miss(self):
        """去重不得让「跳过 / 待询问」的记账消失（策略语义须仍被履行）。"""
        for kind in (TriggerKind.DAILY, TriggerKind.WEEKLY):
            with self.subTest(kind=kind, policy=MISSED_SKIP):
                svc, task, _exec = self._start_service_with_task(kind, MISSED_SKIP, 0)
                reasons = [h.reason for h in svc._store.history_for(task.schedule_id)]
                self.assertIn("missed_while_closed", reasons)
            with self.subTest(kind=kind, policy=MISSED_ASK):
                svc, task, _exec = self._start_service_with_task(kind, MISSED_ASK, 0)
                records = svc._store.history_for(task.schedule_id)
                self.assertIn("missed_ask_pending", [h.reason for h in records])
                self.assertIn("pending_ask", [h.outcome for h in records])

    # -- 判据②（后半，边界：真正的错过补跑仍须触发）------------------------

    def test_real_missed_catchup_still_launches_exactly_once(self):
        """`catchup_once` 仍须触发，且每个到期点只触发一次。

        该用例在修复前后**均为 1**（协调器的单运行互斥挡住了重复请求），属
        「不得破坏」的边界断言，不是红绿判别项。真正的红绿判别项是紧随其后的
        `test_real_missed_catchup_leaves_no_spurious_busy_skip`。
        """
        for kind in (TriggerKind.DAILY, TriggerKind.WEEKLY):
            for offset in _STARTUP_WINDOW_OFFSETS:
                with self.subTest(kind=kind, offset=offset):
                    _svc, _task, exec_ctrl = self._start_service_with_task(
                        kind, MISSED_CATCHUP_ONCE, offset
                    )
                    # 偏移 +1s 的到期点在 now 之后：既非错过也非到期 ⇒ 0 次。
                    expected = 0 if offset > 0 else 1
                    self.assertEqual(
                        exec_ctrl.prepare_and_launch_profile.call_count,
                        expected,
                        f"{kind}/catchup_once 在启动偏移 {offset:+d}s 的启动次数不符",
                    )

    def test_real_missed_catchup_leaves_no_spurious_busy_skip(self):
        """`catchup_once` 的到期点不得被评估第二次（红绿判别项）。

        修复前：补跑请求已让协调器进入"忙"状态，同一起始窗口的第二次请求被
        协调器判为 `skipped`，于是在历史里留下一条 `skipped_while_busy`——
        用户会看到一次并不存在的"因占用而跳过"。修复后该到期点被去重，痕迹消失。
        """
        for kind in (TriggerKind.DAILY, TriggerKind.WEEKLY):
            for offset in (-1, 0):
                with self.subTest(kind=kind, offset=offset):
                    svc, task, exec_ctrl = self._start_service_with_task(
                        kind, MISSED_CATCHUP_ONCE, offset
                    )
                    reasons = [h.reason for h in svc._store.history_for(task.schedule_id)]
                    self.assertEqual(
                        exec_ctrl.prepare_and_launch_profile.call_count, 1
                    )
                    self.assertNotIn(
                        "skipped_while_busy",
                        reasons,
                        "补跑的到期点被评估了第二次（同一起始窗口重复处理）",
                    )

    # -- 边界：SINGLE 占位记录保护 -----------------------------------------

    def test_single_placeholder_protection_is_preserved(self):
        """`SINGLE` 任务的既有保护（修复前后均 0 次）不得被破坏。

        SINGLE 分支在 `_evaluate_due` 中先于周期分支，靠 detector 写入的
        占位历史（`outcome`/`reason` 非空 ⇒ has_handled）拦住重复触发。
        """
        for policy in (MISSED_SKIP, MISSED_ASK):
            for offset in (-2, -1, 0):
                with self.subTest(policy=policy, offset=offset):
                    _svc, _task, exec_ctrl = self._start_service_with_task(
                        TriggerKind.SINGLE, policy, offset
                    )
                    self.assertEqual(
                        exec_ctrl.prepare_and_launch_profile.call_count,
                        0,
                        f"SINGLE/{policy} 在启动偏移 {offset:+d}s 被真实启动",
                    )

    def test_single_catchup_still_launches_once(self):
        """`SINGLE` + `catchup_once`：修复前后均为 1（边界断言，非红绿判别项）。"""
        for offset in (-5, -2, -1, 0):
            with self.subTest(offset=offset):
                _svc, _task, exec_ctrl = self._start_service_with_task(
                    TriggerKind.SINGLE, MISSED_CATCHUP_ONCE, offset
                )
                self.assertEqual(
                    exec_ctrl.prepare_and_launch_profile.call_count,
                    1,
                    f"SINGLE/catchup_once 在启动偏移 {offset:+d}s 的启动次数不符",
                )

    # -- 边界：去重不得泄漏到稳态 ------------------------------------------

    def test_steady_state_due_still_launches_after_startup(self):
        """去重只作用于 `start()` 的那一次评估；稳态到期仍须触发。

        这是本修复最可能的副作用：若把去重集合做成长期记忆，周期任务在下一
        个正常到期点会被永久静默丢弃。
        """
        svc, _task, exec_ctrl = self._start_service_with_task(
            TriggerKind.DAILY, MISSED_SKIP, 0
        )
        self.assertEqual(exec_ctrl.prepare_and_launch_profile.call_count, 0)

        # 前进到次日同一时刻：该到期点从未被启动错过扫描处理过。
        svc._clock = _FakeClock(self._now + timedelta(days=1))
        svc._evaluate_due()

        self.assertEqual(
            exec_ctrl.prepare_and_launch_profile.call_count,
            1,
            "稳态到期触发被启动去重集合误伤（去重作用域泄漏）",
        )

    def test_repeated_evaluate_due_after_startup_respects_miss_record(self):
        """同一起始窗口内的**后续**评估同样不得复活已被 `skip` 处理的到期点。

        `start()` 之后计时器与心跳仍会调用 `_evaluate_due()`，而它的参考点
        （`now - 2 s`）使该到期点在 2 秒内仍然"到期"。若把去重限制为
        `start()` 内的那一次调用，这条路径就会重新真实启动用户已选择跳过的任务。
        """
        svc, _task, exec_ctrl = self._start_service_with_task(
            TriggerKind.DAILY, MISSED_SKIP, 0
        )
        svc._evaluate_due()
        self.assertEqual(exec_ctrl.prepare_and_launch_profile.call_count, 0)


if __name__ == "__main__":
    unittest.main()
