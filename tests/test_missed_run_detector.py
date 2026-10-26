"""Standalone unit tests for MissedRunDetector.

These exercise the missed-trigger domain *without* ScheduledTaskService, a Qt
application, the coordinator or the timer machinery — which is the whole point
of the extraction: the skip / ask / catchup_once decision table and the calendar
walk must be verifiable on their own.

Integration-level coverage (the detector wired into the real service) stays in
test_scheduled_task_service.py / test_scheduled_task_service_coverage.py.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fukua_rpa.missed_run_detector import MissedRunCallbacks, MissedRunDetector
from fukua_rpa.scheduled_task_model import (
    CONFLICT_CATCHUP_ONCE,
    MISSED_ASK,
    MISSED_CATCHUP_ONCE,
    MISSED_SKIP,
    ScheduledRunHistory,
    ScheduledTask,
    ScheduledTaskClock,
    ScheduledTrigger,
    TriggerKind,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeClock(ScheduledTaskClock):
    """Clock with a controllable 'now' (naive local <-> UTC without a tz db)."""

    def __init__(self, fixed_now: datetime):
        self._now = fixed_now

    def now_local(self) -> datetime:
        return self._now

    def to_utc(self, naive_local: datetime) -> datetime:
        return naive_local.replace(tzinfo=UTC)

    def from_utc(self, utc: datetime) -> datetime:
        return utc.replace(tzinfo=None)


class _FakeStore:
    """Minimal store: only enabled() and history_for() are used by the detector."""

    def __init__(self, tasks=(), history=None):
        self._tasks = list(tasks)
        self._history = dict(history or {})

    def enabled(self):
        return [t for t in self._tasks if t.enabled]

    def history_for(self, schedule_id):
        return list(self._history.get(schedule_id, []))


class _Recorder:
    """Collects every callback invocation so assertions read as a trace."""

    def __init__(self, request_result="running"):
        self.misses: list[tuple[str, datetime, str]] = []
        self.miss_outcomes: list[tuple[str, object]] = []
        self.placeholders: list[tuple[str, datetime, str]] = []
        self.run_starts: list[tuple[str, datetime]] = []
        self.detected: list[tuple[str, str]] = []
        self.requests: list[tuple[ScheduledTask, datetime, dict]] = []
        self.request_result = request_result

    def _record_miss(self, task, due_utc, reason, kwargs):
        self.misses.append((task.schedule_id, due_utc, reason))
        self.miss_outcomes.append((task.schedule_id, kwargs.get("outcome")))

    def callbacks(self) -> MissedRunCallbacks:
        return MissedRunCallbacks(
            record_miss=lambda t, due, reason, **kwargs: self._record_miss(
                t, due, reason, kwargs
            ),
            record_miss_placeholder=lambda t, due, reason: self.placeholders.append(
                (t.schedule_id, due, reason)
            ),
            record_run_start=lambda t, due: self.run_starts.append(
                (t.schedule_id, due)
            ),
            emit_missed_detected=lambda sid, name: self.detected.append((sid, name)),
            request_due=self._request_due,
        )

    def _request_due(self, task, due_utc, **kwargs):
        self.requests.append((task, due_utc, kwargs))
        return self.request_result


def _daily(schedule_id="s1", hour=9, **kw):
    return ScheduledTask(
        schedule_id=schedule_id,
        name=kw.pop("name", "daily-task"),
        profile_id="p1",
        profile_name_snapshot="P1",
        enabled=kw.pop("enabled", True),
        trigger=ScheduledTrigger(
            kind=TriggerKind.DAILY, local_time=datetime(2025, 1, 1, hour, 0).time()
        ),
        **kw,
    )


def _startup(schedule_id="s-startup", **kw):
    return ScheduledTask(
        schedule_id=schedule_id,
        name="startup-task",
        profile_id="p1",
        profile_name_snapshot="P1",
        enabled=True,
        trigger=ScheduledTrigger(kind=TriggerKind.STARTUP, startup_delay_seconds=0.0),
        **kw,
    )


class _DetectorTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._tasks_path = os.path.join(self._tmp_dir, "scheduled_tasks.json")
        with open(self._tasks_path, "w", encoding="utf-8") as fh:
            fh.write("{}")
        self._now = datetime(2025, 6, 1, 12, 0, 0)
        self._clock = _FakeClock(self._now)

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _make(self, store, recorder=None):
        recorder = recorder or _Recorder()
        detector = MissedRunDetector(
            lambda: self._clock,
            lambda: store,
            lambda: self._tasks_path,
            recorder.callbacks(),
        )
        return detector, recorder


# ---------------------------------------------------------------------------
# check(): one-shot guard and task filtering
# ---------------------------------------------------------------------------


class TestCheckGuardAndFiltering(_DetectorTestBase):
    def test_second_check_is_a_no_op(self):
        task = _daily(hour=9, missed_policy=MISSED_SKIP)
        history = {
            "s1": [
                ScheduledRunHistory(
                    history_id="h1",
                    schedule_id="s1",
                    run_id="r1",
                    profile_id="p1",
                    due_at_utc=datetime(2025, 5, 30, 9, 0, tzinfo=UTC),
                    started_at_utc=datetime(2025, 5, 30, 9, 0, tzinfo=UTC),
                    ended_at_utc=datetime(2025, 5, 30, 10, 0, tzinfo=UTC),
                    duration_ms=1,
                    outcome="success",
                    reason="",
                    application_log_reference="",
                )
            ]
        }
        detector, rec = self._make(_FakeStore([task], history))

        detector.check()
        self.assertEqual(len(rec.misses), 1)
        self.assertTrue(detector.missed_checked)

        detector.check()
        self.assertEqual(len(rec.misses), 1, "second pass must not re-record")

    def test_startup_tasks_are_excluded(self):
        """STARTUP tasks short-circuit before any reference lookup.

        next_due_local() already returns None for STARTUP triggers, so dropping
        the kind check would not change the *outcome* — which is exactly why the
        assertion targets the short-circuit itself: the detector must not even
        ask for a reference, otherwise every launch would stat the store file
        once per STARTUP task for nothing.
        """
        detector, rec = self._make(_FakeStore([_startup()]))
        with patch.object(
            detector, "reference_local", wraps=detector.reference_local
        ) as spy:
            detector.check()
        spy.assert_not_called()
        self.assertEqual(rec.misses, [])
        self.assertEqual(rec.requests, [])

    def test_disabled_tasks_never_reach_the_detector(self):
        detector, rec = self._make(_FakeStore([_daily(enabled=False)]))
        with patch.object(
            detector, "reference_local", wraps=detector.reference_local
        ) as spy:
            detector.check()
        spy.assert_not_called()
        self.assertEqual(rec.misses, [])

    def test_missing_store_file_is_survivable(self):
        """reference_local() -> None must skip the task, not crash the pass.

        This half of the guard IS load-bearing: latest_missed_due() would walk
        the calendar from ``None`` and raise.
        """
        detector, rec = self._make(_FakeStore([_daily(missed_policy=MISSED_SKIP)]))
        self._tasks_path = os.path.join(self._tmp_dir, "gone.json")
        detector.check()
        self.assertEqual(rec.misses, [])
        self.assertTrue(detector.missed_checked)

    def test_reference_in_the_future_skips_task(self):
        """A task whose file/history baseline is newer than now cannot have missed."""
        task = _daily(hour=9, missed_policy=MISSED_SKIP)
        history = {
            "s1": [
                ScheduledRunHistory(
                    history_id="h1",
                    schedule_id="s1",
                    run_id="r1",
                    profile_id="p1",
                    due_at_utc=datetime(2025, 6, 2, 9, 0, tzinfo=UTC),
                    started_at_utc=None,
                    ended_at_utc=datetime(2025, 6, 2, 9, 0, tzinfo=UTC),
                    duration_ms=0,
                    outcome="missed",
                    reason="",
                    application_log_reference="",
                )
            ]
        }
        detector, rec = self._make(_FakeStore([task], history))
        detector.check()
        self.assertEqual(rec.misses, [])


# ---------------------------------------------------------------------------
# reference_local()
# ---------------------------------------------------------------------------


class TestReferenceLocal(_DetectorTestBase):
    def test_prefers_last_history_end_time(self):
        ended = datetime(2025, 5, 31, 10, 0, tzinfo=UTC)
        history = {
            "s1": [
                ScheduledRunHistory(
                    history_id="h1",
                    schedule_id="s1",
                    run_id="r1",
                    profile_id="p1",
                    due_at_utc=datetime(2025, 5, 31, 9, 0, tzinfo=UTC),
                    started_at_utc=datetime(2025, 5, 31, 9, 0, tzinfo=UTC),
                    ended_at_utc=ended,
                    duration_ms=1,
                    outcome="success",
                    reason="",
                    application_log_reference="",
                )
            ]
        }
        task = _daily()
        detector, _ = self._make(_FakeStore([task], history))
        self.assertEqual(detector.reference_local(task), datetime(2025, 5, 31, 10, 0))

    def test_falls_back_to_due_time_when_run_never_ended(self):
        history = {
            "s1": [
                ScheduledRunHistory(
                    history_id="h1",
                    schedule_id="s1",
                    run_id="",
                    profile_id="p1",
                    due_at_utc=datetime(2025, 5, 31, 9, 0, tzinfo=UTC),
                    started_at_utc=None,
                    ended_at_utc=None,
                    duration_ms=0,
                    outcome="missed",
                    reason="missed_while_closed",
                    application_log_reference="",
                )
            ]
        }
        task = _daily()
        detector, _ = self._make(_FakeStore([task], history))
        self.assertEqual(detector.reference_local(task), datetime(2025, 5, 31, 9, 0))

    def test_falls_back_to_store_file_ctime(self):
        task = _daily()
        detector, _ = self._make(_FakeStore([task]))
        self.assertIsNotNone(detector.reference_local(task))

    def test_prefers_task_created_at_over_file_ctime(self):
        """FR-20260812-D6-002: 任务自身 created_at_utc 优先于文件 ctime——
        ctime 随每次原子写刷新变化，可能早于或晚于任务创建；created_at_utc
        两方向偏差都消除。"""
        created = datetime(2025, 5, 20, 8, 0, tzinfo=UTC)
        task = _daily(created_at_utc=created)
        detector, _ = self._make(_FakeStore([task]))
        self.assertEqual(
            detector.reference_local(task), datetime(2025, 5, 20, 8, 0)
        )

    def test_returns_none_when_store_file_missing(self):
        task = _daily()
        detector, _ = self._make(_FakeStore([task]))
        self._tasks_path = os.path.join(self._tmp_dir, "nope.json")
        self.assertIsNone(detector.reference_local(task))

    def test_provider_sees_owner_updates(self):
        """INVARIANT: the path is read through the provider, never cached."""
        task = _daily()
        detector, _ = self._make(_FakeStore([task]))
        self.assertIsNotNone(detector.reference_local(task))
        self._tasks_path = os.path.join(self._tmp_dir, "moved-away.json")
        self.assertIsNone(detector.reference_local(task))


# ---------------------------------------------------------------------------
# latest_missed_due(): collapse, never replay
# ---------------------------------------------------------------------------


class TestLatestMissedDue(_DetectorTestBase):
    def test_collapses_multiple_missed_occurrences_to_the_latest(self):
        task = _daily(hour=9)
        detector, _ = self._make(_FakeStore([task]))
        reference = datetime(2025, 5, 28, 12, 0)
        latest = detector.latest_missed_due(task, reference, self._now)
        # 5/29 09:00, 5/30 09:00, 5/31 09:00 and 6/1 09:00 all elapsed; only the
        # last one is ever acted upon (实施方案 #155).
        self.assertEqual(latest, datetime(2025, 6, 1, 9, 0))

    def test_returns_none_when_next_trigger_is_still_ahead(self):
        task = _daily(hour=18)
        detector, _ = self._make(_FakeStore([task]))
        reference = datetime(2025, 6, 1, 11, 0)
        self.assertIsNone(detector.latest_missed_due(task, reference, self._now))


# ---------------------------------------------------------------------------
# apply_policy(): the decision table
# ---------------------------------------------------------------------------


class TestApplyPolicy(_DetectorTestBase):
    def _apply(self, policy):
        task = _daily(missed_policy=policy, name="pol-task")
        detector, rec = self._make(_FakeStore([task]))
        detector.apply_policy(task, datetime(2025, 6, 1, 9, 0))
        return rec

    def test_skip_records_a_single_miss_and_never_runs(self):
        rec = self._apply(MISSED_SKIP)
        self.assertEqual(rec.misses, [("s1", datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
                                       "missed_while_closed")])
        self.assertEqual(rec.requests, [])
        self.assertEqual(rec.detected, [])

    def test_ask_emits_and_writes_a_pending_placeholder(self):
        rec = self._apply(MISSED_ASK)
        self.assertEqual(rec.detected, [("s1", "pol-task")])
        self.assertEqual(rec.placeholders, [("s1", datetime(2025, 6, 1, 9, 0, tzinfo=UTC),
                                             "missed_ask_pending")])
        self.assertEqual(rec.requests, [], "ask must never auto-run")

    def test_catchup_once_overrides_the_conflict_policy(self):
        task = _daily(missed_policy=MISSED_CATCHUP_ONCE, conflict_policy="skip")
        detector, rec = self._make(_FakeStore([task]))
        detector.apply_policy(task, datetime(2025, 6, 1, 9, 0))

        self.assertEqual(len(rec.requests), 1)
        passed_task, _due, kwargs = rec.requests[0]
        self.assertEqual(kwargs.get("conflict_policy_override"), CONFLICT_CATCHUP_ONCE)
        # P3-8: the shared Store object must not be mutated.
        self.assertIs(passed_task, task)
        self.assertEqual(task.conflict_policy, "skip")
        self.assertEqual(len(rec.run_starts), 1)

    def test_catchup_once_pending_records_a_pending_catchup_outcome(self):
        """FR-20261008-503：忙碌时返回 pending 必须记账，且不得记 run_start。

        此前该分支不写任何历史，于是同一起点在下一次会话被再次回溯并重复请求
        （容量一的待补队列在退出时被清空）。现在与 ``_evaluate_due`` 对称，
        记 ``outcome="pending_catchup"`` 使该到期点进入历史并被去重。
        """
        task = _daily(missed_policy=MISSED_CATCHUP_ONCE)
        detector, rec = self._make(_FakeStore([task]), _Recorder(request_result="pending"))
        detector.apply_policy(task, datetime(2025, 6, 1, 9, 0))
        self.assertEqual(len(rec.requests), 1)
        self.assertEqual(rec.run_starts, [])
        self.assertEqual(
            rec.misses,
            [("s1", datetime(2025, 6, 1, 9, 0, tzinfo=UTC), "catchup_pending_while_busy")],
        )
        self.assertEqual(rec.miss_outcomes, [("s1", "pending_catchup")])

    def test_catchup_once_failed_records_a_startup_failed_outcome(self):
        """FR-20261008-901：补跑请求被拒为 failed 必须显式记账。

        与 ``_evaluate_due`` 的 ``result == "failed"`` 分支对称（同记
        ``startup_failed`` / ``outcome="failed"``）。此前该分支不写任何历史，
        错过补跑的启动失败零痕迹（设计基准「计划任务」可追踪性要求被违反），
        且唤醒路径锚点不推进导致同一到期点被反复请求。注：带 catchup_once
        覆盖时协调器忙碌只返回 pending，``skipped`` 在本路径不可达。
        """
        task = _daily(missed_policy=MISSED_CATCHUP_ONCE)
        detector, rec = self._make(
            _FakeStore([task]), _Recorder(request_result="failed")
        )
        detector.apply_policy(task, datetime(2025, 6, 1, 9, 0))
        self.assertEqual(len(rec.requests), 1)
        self.assertEqual(rec.run_starts, [])
        self.assertEqual(
            rec.misses,
            [("s1", datetime(2025, 6, 1, 9, 0, tzinfo=UTC), "startup_failed")],
        )
        self.assertEqual(rec.miss_outcomes, [("s1", "failed")])

    def test_unknown_policy_fails_safe_to_skip(self):
        rec = self._apply("bogus_policy")
        self.assertEqual(len(rec.misses), 1)
        self.assertEqual(rec.misses[0][2], "missed_while_closed")
        self.assertEqual(rec.requests, [])


# ---------------------------------------------------------------------------
# end-to-end through check()
# ---------------------------------------------------------------------------


class TestCheckEndToEnd(_DetectorTestBase):
    def test_offline_gap_triggers_exactly_one_catchup(self):
        task = _daily(hour=9, missed_policy=MISSED_CATCHUP_ONCE)
        history = {
            "s1": [
                ScheduledRunHistory(
                    history_id="h1",
                    schedule_id="s1",
                    run_id="r1",
                    profile_id="p1",
                    due_at_utc=datetime(2025, 5, 28, 9, 0, tzinfo=UTC),
                    started_at_utc=datetime(2025, 5, 28, 9, 0, tzinfo=UTC),
                    ended_at_utc=datetime(2025, 5, 28, 9, 5, tzinfo=UTC),
                    duration_ms=1,
                    outcome="success",
                    reason="",
                    application_log_reference="",
                )
            ]
        }
        detector, rec = self._make(_FakeStore([task], history))
        detector.check()

        self.assertEqual(len(rec.requests), 1, "4 elapsed days must collapse to 1 run")
        _task, due_utc, _kw = rec.requests[0]
        self.assertEqual(due_utc, datetime(2025, 6, 1, 9, 0, tzinfo=UTC))

    def test_clock_provider_is_not_cached(self):
        """INVARIANT: swapping the owner's clock changes what the detector sees.

        With the base clock (2025) the store file's ctime is already in the
        "future", so nothing can be missed. Pushing "now" past the ctime opens a
        real missed window — which only happens if the clock is re-read.
        """
        task = _daily(hour=9, missed_policy=MISSED_SKIP)
        detector, rec = self._make(_FakeStore([task]))

        probe, probe_rec = self._make(_FakeStore([task]))
        probe.check()
        self.assertEqual(probe_rec.misses, [], "baseline: nothing missed yet")

        ctime_local = datetime.fromtimestamp(os.path.getctime(self._tasks_path))
        self._clock = _FakeClock(ctime_local + timedelta(days=3))
        detector.check()
        self.assertEqual(len(rec.misses), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
