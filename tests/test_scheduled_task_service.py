"""Tests for scheduled-task service invariants.

Covers:
1. drain 后 worker 仍指向新 run、stop 有效 (finished_worker 捕获在 emit 之前)
2. SINGLE + missed_policy=ask 不自动执行
3. skipped_while_busy 只记录一次
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fukua_rpa.logging_service import (
    SCHEDULED_RUN_LOG_SOURCE,
    flush_logs,
    write_log,
)
from fukua_rpa.scheduled_run_coordinator import ScheduledRunCoordinator
from fukua_rpa.scheduled_task_model import (
    CONFLICT_CATCHUP_ONCE,
    CONFLICT_SKIP,
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
from fukua_rpa.scheduled_task_service import ScheduledTaskService

UTC = UTC


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _single_task(once_at_local, **kw):
    """Create a SINGLE-trigger task."""
    return ScheduledTask(
        schedule_id=kw.pop("schedule_id", new_schedule_id()),
        name=kw.pop("name", "test-task"),
        profile_id=kw.pop("profile_id", "p1"),
        profile_name_snapshot=kw.pop("profile_name_snapshot", "P1"),
        enabled=kw.pop("enabled", True),
        trigger=ScheduledTrigger(kind=TriggerKind.SINGLE, once_at_local=once_at_local),
        **kw,
    )


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


# ---------------------------------------------------------------------------
# Test 1: drain 后 worker 仍指向新 run、stop 有效
# ---------------------------------------------------------------------------

class TestDrainWorkerPointsToNewRun(unittest.TestCase):
    """验证 run_controller._on_finished 中 finished_worker 捕获在 emit 之前。

    worker_A 结束 → emit run_ended → 同步触发 drain → 启动 worker_B。
    emit 之后 self.worker 仍指向 worker_B（非 None），stop() 有效。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_coordinator_drain_sets_active_to_new_run(self):
        """_on_run_ended → _drain_pending → _launch: _active 指向新 run."""
        run_controller = MagicMock()
        run_controller.is_active = False
        run_controller.run_ended = MagicMock()

        execution_controller = MagicMock()
        execution_controller.prepare_and_launch_profile.return_value = True

        task_b = _single_task(
            datetime(2025, 1, 1, 10, 0),
            schedule_id="task-b",
            name="task-b",
        )
        resolver = MagicMock(return_value=task_b)

        coordinator = ScheduledRunCoordinator(
            run_controller, execution_controller, resolver
        )

        # Simulate: task A was running, task B is pending (catch-up queue)
        coordinator._active = ("task-a", "run-a-id")
        coordinator._pending["task-b"] = datetime(2025, 1, 1, 10, 0, tzinfo=UTC)

        # Fire run_ended for task A (worker_A finished)
        coordinator._on_run_ended({"schedule_id": "task-a", "outcome": "success"})

        # After drain, _active should point to the NEW run (task B), not None
        self.assertIsNotNone(coordinator._active)
        self.assertEqual(coordinator._active[0], "task-b")
        # Pending queue should be drained
        self.assertNotIn("task-b", coordinator._pending)
        # execution_controller was called to launch task B
        execution_controller.prepare_and_launch_profile.assert_called_once()

    def test_stop_valid_after_drain(self):
        """drain 后 run_controller.session 不为 None, stop() 有效。

        模拟 _on_finished 的完整流程：
        1. finished_worker = self.worker (捕获 worker_A)
        2. run_ended.emit → 同步 drain → self.worker = worker_B
        3. QTimer.singleShot → _release_session_refs(worker_A)
           因为 self.worker is not worker_A → 跳过清理
        """
        from fukua_rpa.ui.controllers.run_controller import RunController

        engine = MagicMock()
        rc = RunController(engine)

        worker_a = MagicMock()
        worker_b = MagicMock()
        session_a = MagicMock()
        session_b = MagicMock()
        session_b.is_active = True
        session_b.stop.return_value = True

        # worker_A is the current active worker
        rc.worker = worker_a
        rc.session = session_a
        rc._run_meta = {"schedule_id": "sched-1"}
        rc._run_failed = False

        # When run_ended is emitted, simulate drain starting worker_B
        def _drain_starts_new_run(meta):
            rc.worker = worker_b
            rc.session = session_b

        rc.run_ended.connect(_drain_starts_new_run)

        # Fire _on_finished for worker_A
        rc._on_finished(worker_a)

        # After emit, worker should be worker_B (drain replaced it)
        self.assertIs(rc.worker, worker_b)
        self.assertIs(rc.session, session_b)

        # stop() should work because session is not None
        self.assertTrue(rc.stop())
        session_b.stop.assert_called_once()

        # Process deferred QTimer.singleShot(0) — should NOT clear worker_B
        self.app.processEvents()
        self.assertIs(rc.worker, worker_b)
        self.assertIs(rc.session, session_b)

    def test_release_session_refs_skips_cleanup_when_new_worker(self):
        """_release_session_refs 不清理已被 drain 替换的 worker."""
        from fukua_rpa.ui.controllers.run_controller import RunController

        engine = MagicMock()
        rc = RunController(engine)

        worker_a = MagicMock()
        worker_b = MagicMock()
        session_b = MagicMock()

        # After drain, worker is already worker_B
        rc.worker = worker_b
        rc.session = session_b

        # _release_session_refs with finished_worker=worker_A should be a no-op
        rc._release_session_refs(worker_a)

        # worker and session should still be worker_B / session_b
        self.assertIs(rc.worker, worker_b)
        self.assertIs(rc.session, session_b)


# ---------------------------------------------------------------------------
# Test 2: SINGLE + ask 不自动执行
# ---------------------------------------------------------------------------

class TestSingleAskNotAutoExecuted(unittest.TestCase):
    """验证 missed_policy="ask" 的 SINGLE 任务在 _evaluate_due 中不被自动运行。

    启动时 MissedRunDetector.check 检测到遗漏 → 发射 missed_detected 信号 →
    写入 "missed_ask_pending" 占位历史 → _evaluate_due 识别 has_handled →
    不调用 coordinator.request_due。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._now = datetime(2025, 6, 1, 12, 0, 0)  # noon
        self._once_at = datetime(2025, 6, 1, 10, 0, 0)  # 2 hours ago

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _make_service(self):
        run_controller = MagicMock()
        run_controller.is_active = False
        run_controller.run_ended = MagicMock()
        execution_controller = MagicMock()
        execution_controller.prepare_and_launch_profile.return_value = True

        svc = ScheduledTaskService(
            run_controller=run_controller,
            execution_controller=execution_controller,
            base_dir=self._tmp_dir,
        )
        svc._clock = _FakeClock(self._now)
        return svc, execution_controller

    def test_missed_ask_emits_signal_and_blocks_auto_run(self):
        """missed_policy=ask: missed_detected 被发射, _evaluate_due 不自动运行."""
        svc, _ = self._make_service()

        task = _single_task(
            self._once_at,
            schedule_id="sched-ask",
            name="ask-task",
            missed_policy=MISSED_ASK,
            conflict_policy=CONFLICT_SKIP,
        )
        svc._store.add(task)

        # Mock _missed_reference_local to return a time before once_at
        # (simulates: last known activity was before the trigger fired)
        reference = self._once_at - timedelta(hours=1)
        with patch.object(
            svc._missed_detector, "reference_local", return_value=reference
        ):
            missed_signals = []
            svc.missed_detected.connect(
                lambda sid, name: missed_signals.append((sid, name))
            )

            svc._ready = True
            svc._missed_detector.check()

        # missed_detected should have been emitted
        self.assertEqual(len(missed_signals), 1)
        self.assertEqual(missed_signals[0], ("sched-ask", "ask-task"))

        # Verify placeholder history was written
        hist = svc._store.history_for("sched-ask")
        self.assertTrue(
            any(h.reason == "missed_ask_pending" for h in hist),
            "placeholder history with reason='missed_ask_pending' should exist",
        )

        # Now verify _evaluate_due does NOT auto-run the task
        svc._coordinator = MagicMock()
        svc._coordinator.request_due.return_value = "running"
        svc._coordinator.pending_ids = set()

        svc._evaluate_due()

        # request_due should NOT have been called (has_handled is True)
        svc._coordinator.request_due.assert_not_called()


# ---------------------------------------------------------------------------
# FR-b01-004: MISSED_ASK 的用户决定落实（resolve_missed_ask）
# ---------------------------------------------------------------------------

class TestResolveMissedAsk(unittest.TestCase):
    """FR-b01-004: missed_detected 信号已由主窗口接线询问用户后，resolve_missed_ask
    落实"立即补跑"或"跳过"；任务已删除/停用时不误启动。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._now = datetime(2025, 6, 1, 12, 0, 0)  # noon
        self._once_at = datetime(2025, 6, 1, 10, 0, 0)  # 2 hours ago

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _make_service(self):
        run_controller = MagicMock()
        run_controller.is_active = False
        run_controller.run_ended = MagicMock()
        execution_controller = MagicMock()
        execution_controller.prepare_and_launch_profile.return_value = True

        svc = ScheduledTaskService(
            run_controller=run_controller,
            execution_controller=execution_controller,
            base_dir=self._tmp_dir,
        )
        svc._clock = _FakeClock(self._now)
        return svc, execution_controller

    def _add_ask_task(self, svc, enabled=True):
        task = _single_task(
            self._once_at,
            schedule_id="sched-ask",
            name="ask-task",
            missed_policy=MISSED_ASK,
            conflict_policy=CONFLICT_SKIP,
            enabled=enabled,
        )
        svc._store.add(task)
        svc._coordinator = MagicMock()
        svc._coordinator.request_due.return_value = "running"
        svc._coordinator.pending_ids = set()
        return task

    def test_run_now_forwards_catchup_once(self):
        svc, _ = self._make_service()
        task = self._add_ask_task(svc)
        svc._record_miss_placeholder(task, svc._clock.to_utc(self._once_at), "missed_ask_pending")

        result = svc.resolve_missed_ask("sched-ask", run_now=True)

        self.assertTrue(result)
        svc._coordinator.request_due.assert_called_once()
        args = svc._coordinator.request_due.call_args
        self.assertEqual(args.args[0].schedule_id, "sched-ask")
        # "catchup_once" 以第三位置参数传入 conflict_policy_override
        self.assertEqual(args.args[2], "catchup_once")

    def test_skip_records_missed_without_running(self):
        svc, _ = self._make_service()
        task = self._add_ask_task(svc)
        svc._record_miss_placeholder(task, svc._clock.to_utc(self._once_at), "missed_ask_pending")

        result = svc.resolve_missed_ask("sched-ask", run_now=False)

        self.assertTrue(result)
        svc._coordinator.request_due.assert_not_called()
        hist = svc._store.history_for("sched-ask")
        self.assertTrue(any(h.reason == "missed_ask_skipped" for h in hist))

    def test_disabled_task_never_runs(self):
        svc, _ = self._make_service()
        task = self._add_ask_task(svc, enabled=False)
        svc._record_miss_placeholder(task, svc._clock.to_utc(self._once_at), "missed_ask_pending")

        result = svc.resolve_missed_ask("sched-ask", run_now=True)

        self.assertTrue(result)
        svc._coordinator.request_due.assert_not_called()
        hist = svc._store.history_for("sched-ask")
        self.assertTrue(any(h.reason == "missed_ask_skipped" for h in hist))

    def test_deleted_task_returns_false(self):
        svc, _ = self._make_service()
        self._add_ask_task(svc)
        svc._store.remove("sched-ask")

        result = svc.resolve_missed_ask("sched-ask", run_now=True)

        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Test 3: skipped_while_busy 只记录一次
# ---------------------------------------------------------------------------

class TestSkippedWhileBusyRecordedOnce(unittest.TestCase):
    """验证 SINGLE + conflict_policy=skip 在忙碌时只记录一次 skipped_while_busy。

    第一次 _evaluate_due: 无历史 → request_due 返回 "skipped" → 记录一次。
    后续 _evaluate_due: 历史中已有 "skipped_while_busy" → has_handled=True → 跳过。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._now = datetime(2025, 6, 1, 12, 0, 0)
        self._once_at = datetime(2025, 6, 1, 10, 0, 0)  # past

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_skipped_recorded_only_once(self):
        """连续多次 _evaluate_due, store.add_history 只被调用一次."""
        run_controller = MagicMock()
        run_controller.is_active = False
        run_controller.run_ended = MagicMock()
        execution_controller = MagicMock()

        svc = ScheduledTaskService(
            run_controller=run_controller,
            execution_controller=execution_controller,
            base_dir=self._tmp_dir,
        )
        svc._clock = _FakeClock(self._now)
        svc._ready = True

        task = _single_task(
            self._once_at,
            schedule_id="sched-skip",
            name="skip-task",
            missed_policy=MISSED_SKIP,
            conflict_policy=CONFLICT_SKIP,
        )
        svc._store.add(task)

        # Mock coordinator: request_due returns "skipped" (busy)
        svc._coordinator = MagicMock()
        svc._coordinator.request_due.return_value = "skipped"
        svc._coordinator.pending_ids = set()

        # Spy on store.add_history
        add_history_calls = []
        original_add_history = svc._store.add_history

        def _spy_add_history(record):
            add_history_calls.append(record)
            original_add_history(record)

        svc._store.add_history = _spy_add_history

        # Call _evaluate_due multiple times
        svc._evaluate_due()
        svc._evaluate_due()
        svc._evaluate_due()

        # add_history should be called exactly once
        self.assertEqual(len(add_history_calls), 1)
        self.assertEqual(add_history_calls[0].reason, "skipped_while_busy")
        self.assertEqual(add_history_calls[0].schedule_id, "sched-skip")
        self.assertEqual(add_history_calls[0].outcome, "skipped")

        # request_due should also only be called once (subsequent calls skip)
        self.assertEqual(svc._coordinator.request_due.call_count, 1)


class TestStartupFailedRecordedOnce(unittest.TestCase):
    """FR-20260811-D6-001: SINGLE 任务 request_due 返回 "failed" 时显式记账
    （startup_failed），否则无历史导致 has_handled=False 反复触发空转。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._now = datetime(2025, 6, 1, 12, 0, 0)
        self._once_at = datetime(2025, 6, 1, 10, 0, 0)  # past

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_failed_is_recorded_once_and_not_retriggered(self):
        run_controller = MagicMock()
        run_controller.is_active = False
        run_controller.run_ended = MagicMock()
        execution_controller = MagicMock()
        # FR-20260812-D6-001: task_status 经 _health 查询执行控制器健康，
        # 需返回可解包三元组。
        execution_controller.scheduled_health.return_value = (True, True, True)

        svc = ScheduledTaskService(
            run_controller=run_controller,
            execution_controller=execution_controller,
            base_dir=self._tmp_dir,
        )
        svc._clock = _FakeClock(self._now)
        svc._ready = True

        task = _single_task(
            self._once_at,
            schedule_id="sched-fail",
            name="fail-task",
            missed_policy=MISSED_SKIP,
            conflict_policy=CONFLICT_SKIP,
        )
        svc._store.add(task)

        svc._coordinator = MagicMock()
        svc._coordinator.request_due.return_value = "failed"
        svc._coordinator.pending_ids = set()

        add_history_calls = []
        original_add_history = svc._store.add_history

        def _spy_add_history(record):
            add_history_calls.append(record)
            original_add_history(record)

        svc._store.add_history = _spy_add_history

        svc._evaluate_due()
        svc._evaluate_due()

        self.assertEqual(len(add_history_calls), 1)
        self.assertEqual(add_history_calls[0].reason, "startup_failed")
        # P2 修复：startup_failed 属 failure 终态，outcome 必须为 "failed"
        # （而非默认 "skipped"），避免 UI 把启动失败误显示为"跳过"。
        self.assertEqual(add_history_calls[0].outcome, "failed")
        self.assertEqual(add_history_calls[0].schedule_id, "sched-fail")
        # failed 记账后进入终态，request_due 只应被调用一次（不再反复空转）。
        self.assertEqual(svc._coordinator.request_due.call_count, 1)
        # FR-20260812-D6-001: 启动失败必须同步 _last_failed，使 task_status
        # 返回 LAST_RUN_FAILED、异常过滤与 summary()["errors"] 与结果列一致，
        # 避免 SINGLE 任务启动失败后状态显示 WAITING。
        self.assertTrue(svc._last_failed.get("sched-fail"))
        self.assertIs(
            svc.task_status(task), ScheduleStatus.LAST_RUN_FAILED
        )
        self.assertEqual(svc.summary()["errors"], 1)

    def test_finished_run_not_marked_failed(self):
        # FR-SCHED-001: 独立 Runner 合法完成终局 "finished" 属成功，不得误判失败。
        run_controller = MagicMock()
        execution_controller = MagicMock()
        execution_controller.scheduled_health.return_value = (True, True, True)
        svc = ScheduledTaskService(
            run_controller=run_controller,
            execution_controller=execution_controller,
            base_dir=self._tmp_dir,
        )
        svc._ready = True
        task = _single_task(
            self._once_at,
            schedule_id="sched-ok",
            name="ok-task",
            missed_policy=MISSED_SKIP,
            conflict_policy=CONFLICT_SKIP,
        )
        svc._store.add(task)
        svc._on_coord_run_finished("sched-ok", "run-1", "finished")
        self.assertFalse(svc._last_failed.get("sched-ok"))


# ---------------------------------------------------------------------------
# Test 4 (P2-7): _drain_pending 丢弃 stale 条目但仍启动有效 task
# ---------------------------------------------------------------------------

class TestDrainPendingDiscardsStaleEntry(unittest.TestCase):
    """验证 _pending 同时含已删除/禁用 task 与有效 task 时:

    _on_run_ended → _drain_pending 丢弃 stale 条目, 启动有效 task,
    并最终清空 _pending。stale 条目不能阻塞有效条目 (P3-7)。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_stale_entry_discarded_and_valid_task_launched(self):
        run_controller = MagicMock()
        run_controller.is_active = False
        run_controller.run_ended = MagicMock()

        execution_controller = MagicMock()
        execution_controller.prepare_and_launch_profile.return_value = True

        valid_task = _single_task(
            datetime(2025, 1, 1, 11, 0),
            schedule_id="task-valid",
            name="valid",
        )

        # Resolver: stale id -> None (deleted); valid id -> the enabled task.
        def _resolver(sid):
            if sid == "task-valid":
                return valid_task
            return None  # stale / deleted

        coordinator = ScheduledRunCoordinator(
            run_controller, execution_controller, _resolver
        )

        # task A was running; two pending entries: a stale one (earlier due) and
        # a valid one. The stale entry sorts first and must be discarded, not
        # strand the valid one.
        coordinator._active = ("task-a", "run-a-id")
        coordinator._pending["task-stale"] = datetime(2025, 1, 1, 10, 0, tzinfo=UTC)
        coordinator._pending["task-valid"] = datetime(2025, 1, 1, 11, 0, tzinfo=UTC)

        # Fire run_ended for task A.
        coordinator._on_run_ended({"schedule_id": "task-a", "outcome": "success"})

        # The valid task should now be the active run.
        self.assertIsNotNone(coordinator._active)
        self.assertEqual(coordinator._active[0], "task-valid")
        # Both pending entries are gone: stale discarded, valid launched.
        self.assertEqual(coordinator._pending, {})
        self.assertNotIn("task-stale", coordinator._pending)
        # The valid task was actually launched.
        execution_controller.prepare_and_launch_profile.assert_called_once()

    def test_disabled_entry_discarded_and_valid_task_launched(self):
        """禁用 task 的 pending 条目同样被视为 stale 并丢弃。"""
        run_controller = MagicMock()
        run_controller.is_active = False
        run_controller.run_ended = MagicMock()

        execution_controller = MagicMock()
        execution_controller.prepare_and_launch_profile.return_value = True

        valid_task = _single_task(
            datetime(2025, 1, 1, 11, 0),
            schedule_id="task-valid",
            name="valid",
        )
        disabled_task = _single_task(
            datetime(2025, 1, 1, 10, 0),
            schedule_id="task-disabled",
            name="disabled",
            enabled=False,
        )

        def _resolver(sid):
            if sid == "task-valid":
                return valid_task
            if sid == "task-disabled":
                return disabled_task
            return None

        coordinator = ScheduledRunCoordinator(
            run_controller, execution_controller, _resolver
        )

        coordinator._active = ("task-a", "run-a-id")
        coordinator._pending["task-disabled"] = datetime(2025, 1, 1, 10, 0, tzinfo=UTC)
        coordinator._pending["task-valid"] = datetime(2025, 1, 1, 11, 0, tzinfo=UTC)

        coordinator._on_run_ended({"schedule_id": "task-a", "outcome": "success"})

        self.assertIsNotNone(coordinator._active)
        self.assertEqual(coordinator._active[0], "task-valid")
        self.assertEqual(coordinator._pending, {})
        execution_controller.prepare_and_launch_profile.assert_called_once()


# ---------------------------------------------------------------------------
# Test 5 (P3-6): missed-catchup 用 conflict_policy_override 且不修改 task
# ---------------------------------------------------------------------------

class TestMissedCatchupUsesConflictOverride(unittest.TestCase):
    """验证 missed_policy=catchup_once 的遗漏补齐路径:

    MissedRunDetector.apply_policy → request_due 以 conflict_policy_override=
    CONFLICT_CATCHUP_ONCE 被调用, 且共享 task.conflict_policy 保持原值
    (不被修改, P3-8)。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._now = datetime(2025, 6, 1, 12, 0, 0)
        self._once_at = datetime(2025, 6, 1, 10, 0, 0)  # 2 hours ago

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_request_due_called_with_override_and_task_unmutated(self):
        run_controller = MagicMock()
        run_controller.is_active = False
        run_controller.run_ended = MagicMock()
        execution_controller = MagicMock()
        execution_controller.prepare_and_launch_profile.return_value = True

        svc = ScheduledTaskService(
            run_controller=run_controller,
            execution_controller=execution_controller,
            base_dir=self._tmp_dir,
        )
        svc._clock = _FakeClock(self._now)

        # conflict_policy is "skip" but missed_policy is catchup_once: the
        # missed path must override the conflict policy for this one request.
        task = _single_task(
            self._once_at,
            schedule_id="sched-catchup",
            name="catchup-task",
            missed_policy=MISSED_CATCHUP_ONCE,
            conflict_policy=CONFLICT_SKIP,
        )
        svc._store.add(task)

        # Capture request_due calls on a mocked coordinator.
        svc._coordinator = MagicMock()
        svc._coordinator.request_due.return_value = "running"

        reference = self._once_at - timedelta(hours=1)
        with patch.object(
            svc._missed_detector, "reference_local", return_value=reference
        ):
            svc._ready = True
            svc._missed_detector.check()

        # request_due must have been called exactly once with the override.
        self.assertEqual(svc._coordinator.request_due.call_count, 1)
        args, kwargs = svc._coordinator.request_due.call_args
        self.assertEqual(
            kwargs.get("conflict_policy_override"), CONFLICT_CATCHUP_ONCE
        )
        # The passed task is the same shared object, still with its original
        # conflict_policy untouched.
        passed_task = args[0]
        self.assertIs(passed_task, task)
        self.assertEqual(task.conflict_policy, CONFLICT_SKIP)
        self.assertEqual(passed_task.conflict_policy, CONFLICT_SKIP)


class TestScheduledLogShutdownPhases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_shutdown_keeps_sink_until_terminal_logs_are_flushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_controller = MagicMock()
            run_controller.is_active = False
            run_controller.run_ended = MagicMock()
            execution_controller = MagicMock()
            service = ScheduledTaskService(
                run_controller,
                execution_controller,
                tmp,
            )
            service.start()
            self.addCleanup(service.finalize_logging)

            service.shutdown()
            context = {
                "log_source": SCHEDULED_RUN_LOG_SOURCE,
                "schedule_id": "shutdown-plan",
                "run_id": "shutdown-run",
            }
            self.assertTrue(write_log("停止阶段尾日志", context=context))
            self.assertTrue(flush_logs(timeout=2.0))
            self.assertEqual(
                [
                    row["message"]
                    for row in service.log_entries_for(
                        "shutdown-plan",
                        "shutdown-run",
                    )
                ],
                ["停止阶段尾日志"],
            )

            service.finalize_logging()
            self.assertTrue(write_log("注销后日志", context=context))
            self.assertTrue(flush_logs(timeout=2.0))
            self.assertEqual(
                len(service.log_entries_for("shutdown-plan", "shutdown-run")),
                1,
            )


# ---------------------------------------------------------------------------
# FR-20260928-002: 补跑排空与 ask-run_now 路径的历史 due_at_utc 不得为 None
# ---------------------------------------------------------------------------

class TestDrainRunRecordsDueAt(unittest.TestCase):
    """FR-20260928-002 回归测试（先红后绿）。

    忙时进入 catch-up 队列的任务在 run_ended 后由 _drain_pending 直接
    _launch；修复前该路径的 run_started 信号不携带 due_at，service 侧
    _running 条目 due_at 为 None，最终写入历史 due_at_utc=None。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._run_controller = MagicMock()
        self._run_controller.is_active = False
        self._run_controller.run_ended = MagicMock()
        self._execution_controller = MagicMock()
        self._execution_controller.prepare_and_launch_profile.return_value = True
        self._execution_controller.scheduled_health.return_value = (True, True, True)
        self._service = ScheduledTaskService(
            run_controller=self._run_controller,
            execution_controller=self._execution_controller,
            base_dir=self._tmp_dir,
        )
        # 使用真实协调器（与既有 drain 测试同款裸 ScheduledRunCoordinator；
        # service 的 _CoordinatorProxy 仅镜像同一信号对象并加退出守卫，
        # run_started → 记账契约等价），使 drain → _launch → run_started →
        # 记账全链路真实发生；不用 MagicMock 替换。
        coordinator = ScheduledRunCoordinator(
            self._run_controller,
            self._execution_controller,
            self._service._store.get,
        )
        coordinator.run_started.connect(self._service._on_coord_run_started)
        coordinator.run_finished.connect(self._service._on_coord_run_finished)
        self._service._coordinator = coordinator

    def tearDown(self):
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def test_drain_path_records_due_at_in_history(self):
        task = _single_task(
            datetime(2025, 1, 1, 11, 0),
            schedule_id="s-drain-due",
            name="drain-due",
        )
        self._service._store.add(task)
        coordinator = self._service._coordinator
        due = datetime(2025, 1, 1, 11, 0, tzinfo=UTC)

        # 忙时进入 catch-up 队列（不经 _record_run_start）。
        coordinator._active = ("other-run", "run-other-id")
        coordinator._pending["s-drain-due"] = due

        # 上一个运行结束 → 排空启动补跑。
        coordinator._on_run_ended({"schedule_id": "other-run", "outcome": "success"})
        self.assertIsNotNone(coordinator._active)
        self.assertEqual(coordinator._active[0], "s-drain-due")

        # 补跑运行结束 → 写历史。
        coordinator._on_run_ended({"schedule_id": "s-drain-due", "outcome": "finished"})

        hist = self._service.history_for("s-drain-due")
        self.assertEqual(len(hist), 1)
        self.assertIsNotNone(
            hist[0].due_at_utc,
            "补跑排空路径的历史 due_at_utc 不应为 None（FR-20260928-002）",
        )
        self.assertEqual(hist[0].due_at_utc, due)

    def test_ask_run_now_records_due_at_in_history(self):
        task = _single_task(
            datetime(2025, 6, 1, 10, 30),
            schedule_id="s-ask-due",
            name="ask-due",
        )
        self._service._store.add(task)
        coordinator = self._service._coordinator
        missed_due = datetime(2025, 6, 1, 10, 30, tzinfo=UTC)

        # 预置一条 pending_ask 历史（错过询问的到期锚点）。
        self._service._store.add_history(ScheduledRunHistory(
            history_id="h-ask-seed",
            schedule_id="s-ask-due",
            run_id="r-seed",
            profile_id="p1",
            due_at_utc=missed_due,
            started_at_utc=missed_due,
            ended_at_utc=missed_due,
            duration_ms=0,
            outcome="pending_ask",
            reason="",
            application_log_reference="",
        ))

        self.assertTrue(self._service.resolve_missed_ask("s-ask-due", True))
        self.assertIsNotNone(coordinator._active)
        self.assertEqual(coordinator._active[0], "s-ask-due")

        coordinator._on_run_ended({"schedule_id": "s-ask-due", "outcome": "finished"})

        run_hist = [
            h for h in self._service.history_for("s-ask-due")
            if h.outcome == "finished"
        ]
        self.assertEqual(len(run_hist), 1)
        self.assertIsNotNone(
            run_hist[0].due_at_utc,
            "ask-run_now 路径的历史 due_at_utc 不应为 None（FR-20260928-002）",
        )
        self.assertEqual(run_hist[0].due_at_utc, missed_due)


if __name__ == "__main__":
    unittest.main()
