"""Unit tests for the framework-free scheduled-task data model.

These run without PySide/Qt because :mod:`fukua_rpa.scheduled_task_model`
imports only the standard library.
"""

from __future__ import annotations

import datetime
import unittest
from datetime import datetime as dt

from fukua_rpa.scheduled_task_model import (
    CONFLICT_CATCHUP_ONCE,
    MISSED_ASK,
    MISSED_CATCHUP_ONCE,
    FixedZoneClock,
    ScheduledRunHistory,
    ScheduledTask,
    ScheduledTaskClock,
    ScheduledTrigger,
    ScheduleStatus,
    TriggerKind,
    compute_future_runs,
    new_schedule_id,
    next_due_local,
    status_for,
)

UTC = datetime.UTC
CLOCK = FixedZoneClock(UTC)


def _daily(hour=9, minute=0):
    return ScheduledTrigger(
        kind=TriggerKind.DAILY,
        local_time=datetime.time(hour, minute),
    )


def _task(trigger, enabled=True, profile_id="p1", **kw):
    return ScheduledTask(
        schedule_id=kw.pop("schedule_id", new_schedule_id()),
        name=kw.pop("name", "t"),
        profile_id=profile_id,
        profile_name_snapshot=kw.pop("profile_name_snapshot", "P1"),
        enabled=enabled,
        trigger=trigger,
        **kw,
    )


# ---------------------------------------------------------------------------
# Enumerations & constants
# ---------------------------------------------------------------------------
class TestEnumerationsAndConstants(unittest.TestCase):
    def test_trigger_kind_values(self):
        self.assertEqual(TriggerKind.DAILY.value, "daily")
        self.assertEqual(TriggerKind.STARTUP.value, "startup")
        self.assertTrue(
            {s.value for s in ScheduleStatus}
            >= {
                "disabled",
                "waiting",
                "running",
                "pending_catchup",
                "profile_missing",
                "invalid_config",
                "plugin_or_capability_missing",
                "last_run_failed",
            }
        )

    def test_singleton_id_is_unique(self):
        self.assertNotEqual(new_schedule_id(), new_schedule_id())


# ---------------------------------------------------------------------------
# next_run_local per trigger kind
# ---------------------------------------------------------------------------
class TestNextRunLocal(unittest.TestCase):
    def test_daily_tomorrow_when_passed(self):
        now = dt(2026, 7, 19, 10, 0)
        n = _daily(9, 0).next_run_local(now, CLOCK)
        self.assertEqual(n, dt(2026, 7, 20, 9, 0))

    def test_daily_today_when_before(self):
        now = dt(2026, 7, 19, 8, 0)
        n = _daily(9, 0).next_run_local(now, CLOCK)
        self.assertEqual(n, dt(2026, 7, 19, 9, 0))

    def test_weekly_next_weekday(self):
        # 2026-07-19 is a Sunday (weekday() == 6). Monday is 2026-07-20.
        now = dt(2026, 7, 19, 12, 0)
        trig = ScheduledTrigger(
            kind=TriggerKind.WEEKLY,
            local_time=datetime.time(9, 0),
            weekdays=frozenset({0}),
        )
        n = trig.next_run_local(now, CLOCK)
        self.assertEqual(n, dt(2026, 7, 20, 9, 0))

    def test_monthly_clamps_to_last_day(self):
        # Feb 30 should clamp to Feb 28 (2026 is not a leap year).
        now = dt(2026, 2, 1, 0, 0)
        trig = ScheduledTrigger(
            kind=TriggerKind.MONTHLY,
            local_time=datetime.time(9, 0),
            day_of_month=30,
        )
        n = trig.next_run_local(now, CLOCK)
        self.assertEqual(n, dt(2026, 2, 28, 9, 0))

    def test_interval_alignment(self):
        anchor = dt(2026, 7, 19, 0, 0)
        anchor_utc = CLOCK.to_utc(anchor)
        trig = ScheduledTrigger(
            kind=TriggerKind.INTERVAL,
            interval_seconds=3600,
            anchor_at_utc=anchor_utc,
        )
        now = dt(2026, 7, 19, 2, 30)  # 2.5 h after anchor
        n = trig.next_run_local(now, CLOCK)
        # 3rd aligned slot = anchor + 3h
        self.assertEqual(n, dt(2026, 7, 19, 3, 0))

    def test_single_future_and_past(self):
        future = dt(2026, 8, 1, 9, 0)
        trig = ScheduledTrigger(kind=TriggerKind.SINGLE, once_at_local=future)
        self.assertEqual(trig.next_run_local(dt(2026, 7, 19, 0, 0), CLOCK), future)
        # Past single trigger yields no next run.
        past = ScheduledTrigger(kind=TriggerKind.SINGLE, once_at_local=dt(2026, 1, 1, 0, 0))
        self.assertIsNone(past.next_run_local(dt(2026, 7, 19, 0, 0), CLOCK))

    def test_startup_not_calendar_based(self):
        trig = ScheduledTrigger(kind=TriggerKind.STARTUP, startup_delay_seconds=5)
        self.assertIsNone(trig.next_run_local(dt(2026, 7, 19, 0, 0), CLOCK))


# ---------------------------------------------------------------------------
# next_due_local: enabled + active window
# ---------------------------------------------------------------------------
class TestNextDueLocal(unittest.TestCase):
    def test_next_due_local_disabled_is_none(self):
        self.assertIsNone(
            next_due_local(_task(_daily(), enabled=False), dt(2026, 7, 19, 8, 0), CLOCK)
        )

    def test_next_due_local_active_window(self):
        now = dt(2026, 7, 19, 8, 0)
        # active_until just before the next due -> None
        task = _task(
            _daily(9, 0),
            active_until=CLOCK.to_utc(dt(2026, 7, 19, 8, 30)),
        )
        self.assertIsNone(next_due_local(task, now, CLOCK))
        # active_from in the future keeps it None until window opens
        future_task = _task(
            _daily(9, 0),
            active_from=CLOCK.to_utc(dt(2026, 7, 20, 0, 0)),
        )
        self.assertIsNone(next_due_local(future_task, now, CLOCK))


# ---------------------------------------------------------------------------
# compute_future_runs preview
# ---------------------------------------------------------------------------
class TestComputeFutureRuns(unittest.TestCase):
    def test_compute_future_runs_count(self):
        now = dt(2026, 7, 19, 8, 0)
        runs = compute_future_runs(_daily(9, 0), now, CLOCK, count=3)
        self.assertEqual(len(runs), 3)
        self.assertEqual(runs[0], dt(2026, 7, 19, 9, 0))
        self.assertEqual(runs[1], dt(2026, 7, 20, 9, 0))
        self.assertEqual(runs[2], dt(2026, 7, 21, 9, 0))


# ---------------------------------------------------------------------------
# status_for decision tree
# ---------------------------------------------------------------------------
class TestStatusFor(unittest.TestCase):
    def test_status_for_tree(self):
        task = _task(_daily())
        self.assertEqual(status_for(task, True, True, True), ScheduleStatus.WAITING)
        self.assertEqual(status_for(task, False, True, True), ScheduleStatus.PROFILE_MISSING)
        self.assertEqual(status_for(task, True, False, True), ScheduleStatus.INVALID_CONFIG)
        self.assertEqual(
            status_for(task, True, True, False), ScheduleStatus.PLUGIN_OR_CAPABILITY_MISSING
        )
        self.assertEqual(
            status_for(task, True, True, True, running=True), ScheduleStatus.RUNNING
        )
        self.assertEqual(
            status_for(_task(_daily(), enabled=False), True, True, True),
            ScheduleStatus.DISABLED,
        )


# ---------------------------------------------------------------------------
# Serialization round-trips
# ---------------------------------------------------------------------------
class TestSerialization(unittest.TestCase):
    def test_task_round_trip(self):
        task = _task(
            ScheduledTrigger(
                kind=TriggerKind.WEEKLY,
                local_time=datetime.time(9, 30),
                weekdays=frozenset({0, 2}),
            ),
            conflict_policy=CONFLICT_CATCHUP_ONCE,
            missed_policy=MISSED_CATCHUP_ONCE,
        )
        restored = ScheduledTask.from_dict(task.to_dict())
        self.assertEqual(restored.schedule_id, task.schedule_id)
        self.assertEqual(restored.trigger.kind, TriggerKind.WEEKLY)
        self.assertEqual(restored.trigger.weekdays, frozenset({0, 2}))
        self.assertEqual(restored.trigger.local_time, datetime.time(9, 30))
        self.assertEqual(restored.conflict_policy, CONFLICT_CATCHUP_ONCE)
        self.assertEqual(restored.missed_policy, MISSED_CATCHUP_ONCE)

    def test_missed_policy_ask_round_trip(self):
        task = _task(
            _daily(9, 0),
            missed_policy=MISSED_ASK,
        )
        restored = ScheduledTask.from_dict(task.to_dict())
        self.assertEqual(restored.missed_policy, MISSED_ASK)

    def test_enabled_strict_boolean_parsing(self):
        """FR-20260812-D2-002: enabled 必须严格解析，bool("false")==True 不得成立。"""
        base = _task(_daily(9, 0)).to_dict()
        for raw, expected in [
            (True, True),
            (False, False),
            ("true", True),
            ("false", False),
            ("TRUE", True),
            ("FALSE", False),
            ("0", False),   # 非标准字符串一律停用
            ("yes", False),
            ("", False),
            (1, False),     # 非布尔非字符串一律停用
        ]:
            with self.subTest(raw=raw):
                d = dict(base)
                d["enabled"] = raw
                self.assertEqual(ScheduledTask.from_dict(d).enabled, expected)

    def test_history_round_trip(self):
        hist = ScheduledRunHistory(
            history_id="h1",
            schedule_id="s1",
            run_id="r1",
            profile_id="p1",
            due_at_utc=CLOCK.to_utc(dt(2026, 7, 19, 9, 0)),
            outcome="success",
            reason="ok",
        )
        restored = ScheduledRunHistory.from_dict(hist.to_dict())
        self.assertEqual(restored.outcome, "success")
        self.assertEqual(restored.due_at_utc, hist.due_at_utc)

    def test_history_from_dict_coerces_non_numeric_duration(self):
        """FR-20261011-008: a corrupted ``duration_ms`` degrades to ``None``.

        History files are read from disk and may be hand-edited; a string used to
        reach ``format_duration`` and raise ``TypeError`` in the log window.
        """
        base = {"history_id": "h1", "schedule_id": "s1", "run_id": "r1"}
        self.assertIsNone(
            ScheduledRunHistory.from_dict({**base, "duration_ms": "abc"}).duration_ms
        )
        self.assertIsNone(
            ScheduledRunHistory.from_dict({**base, "duration_ms": True}).duration_ms
        )
        self.assertIsNone(
            ScheduledRunHistory.from_dict({**base, "duration_ms": None}).duration_ms
        )
        self.assertEqual(
            ScheduledRunHistory.from_dict({**base, "duration_ms": 1500}).duration_ms,
            1500,
        )
        self.assertEqual(
            ScheduledRunHistory.from_dict({**base, "duration_ms": 1500.0}).duration_ms,
            1500,
        )

    def test_default_clock_is_dst_aware(self):
        # The default clock must not raise and must return a naive local datetime.
        self.assertIsInstance(ScheduledTaskClock().now_local(), dt)


class TestTriggerFieldStrictParsing(unittest.TestCase):
    """FR-20261003-601：weekdays / day_of_month 必须严格校验。

    畸形类型（字符串、布尔、非整数浮点）与越界值必须抛 ValueError，使
    ScheduledTaskStore.load / import_tasks 走既有的逐条跳过或 .corrupt 自愈
    路径，而不是把字符串透传到 next_run_local 后抛 TypeError。
    """

    def test_string_day_of_month_rejected(self):
        with self.assertRaises(ValueError):
            ScheduledTrigger.from_dict(
                {"kind": "monthly", "local_time": "09:00", "day_of_month": "15"}
            )

    def test_string_weekdays_rejected(self):
        with self.assertRaises(ValueError):
            ScheduledTrigger.from_dict(
                {"kind": "weekly", "local_time": "09:00", "weekdays": "12"}
            )

    def test_string_element_in_weekdays_rejected(self):
        with self.assertRaises(ValueError):
            ScheduledTrigger.from_dict(
                {"kind": "weekly", "local_time": "09:00", "weekdays": ["1", "2"]}
            )

    def test_bool_rejected(self):
        with self.assertRaises(ValueError):
            ScheduledTrigger.from_dict(
                {"kind": "monthly", "local_time": "09:00", "day_of_month": True}
            )

    def test_out_of_range_day_of_month_rejected(self):
        for bad in (0, 32, -1):
            with self.subTest(day=bad), self.assertRaises(ValueError):
                ScheduledTrigger.from_dict(
                    {"kind": "monthly", "local_time": "09:00", "day_of_month": bad}
                )

    def test_out_of_range_weekday_rejected(self):
        for bad in (7, -1, 99):
            with self.subTest(weekday=bad), self.assertRaises(ValueError):
                ScheduledTrigger.from_dict(
                    {"kind": "weekly", "local_time": "09:00", "weekdays": [bad]}
                )

    def test_valid_values_still_parse(self):
        monthly = ScheduledTrigger.from_dict(
            {"kind": "monthly", "local_time": "09:00", "day_of_month": 15}
        )
        self.assertEqual(monthly.day_of_month, 15)
        weekly = ScheduledTrigger.from_dict(
            {"kind": "weekly", "local_time": "09:00", "weekdays": [0, 2, 6]}
        )
        self.assertEqual(weekly.weekdays, frozenset({0, 2, 6}))
        # 整数值 float（JSON "15.0"）仍可接受，不误伤合法数据。
        floaty = ScheduledTrigger.from_dict(
            {"kind": "monthly", "local_time": "09:00", "day_of_month": 15.0}
        )
        self.assertEqual(floaty.day_of_month, 15)

    def test_absent_fields_default_to_unset(self):
        trigger = ScheduledTrigger.from_dict({"kind": "daily", "local_time": "09:00"})
        self.assertIsNone(trigger.day_of_month)
        self.assertEqual(trigger.weekdays, frozenset())

    def test_next_run_does_not_raise_for_valid_data(self):
        trigger = ScheduledTrigger.from_dict(
            {"kind": "monthly", "local_time": "09:00", "day_of_month": 15}
        )
        self.assertIsNotNone(trigger.next_run_local(dt(2026, 10, 4, 12, 0), CLOCK))


if __name__ == "__main__":
    unittest.main()
