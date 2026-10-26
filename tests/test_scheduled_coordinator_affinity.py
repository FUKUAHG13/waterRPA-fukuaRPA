"""Regression tests for R1: ScheduledRunCoordinator thread-affinity contract.

R1 documents that every entry point and connected slot must run on the
coordinator's own Qt thread, since ``_active``/``_pending`` are lock-free. The
guard (``_check_thread_affinity``) must be wired into the entry points and must
be non-fatal in the valid same-thread case (it only warns, once, on a real
violation).
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from fukua_rpa.scheduled_run_coordinator import ScheduledRunCoordinator

UTC = UTC


class ScheduledCoordinatorAffinityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_coordinator(self):
        run_controller = MagicMock()
        run_controller.is_active = False
        run_controller.run_ended = MagicMock()
        execution_controller = MagicMock()
        execution_controller.prepare_and_launch_profile.return_value = True
        resolver = MagicMock(return_value=None)
        return ScheduledRunCoordinator(run_controller, execution_controller, resolver)

    def test_affinity_guard_non_fatal_same_thread(self):
        coord = self._make_coordinator()
        # On the coordinator's own thread this must not raise and must not flag
        # a violation (the contract is precisely that this is allowed).
        coord._check_thread_affinity()
        self.assertFalse(coord._warned_affinity)

    def test_request_due_runs_guard_without_warning(self):
        coord = self._make_coordinator()
        task = SimpleNamespace(
            schedule_id="s1",
            conflict_policy="skip",
            profile_id="p1",
            max_runtime_seconds=60,
        )
        result = coord.request_due(task, datetime(2025, 1, 1, 10, 0, tzinfo=UTC))
        self.assertEqual(result, "running")
        self.assertEqual(coord.active_schedule_id, "s1")
        # The guard fired as part of the entry point but produced no warning.
        self.assertFalse(coord._warned_affinity)

    def test_on_run_ended_runs_guard_without_warning(self):
        coord = self._make_coordinator()
        coord._active = ("s1", "run-1")
        # Must be callable on the same thread without raising or warning.
        coord._on_run_ended({"schedule_id": "s1", "outcome": "success"})
        self.assertFalse(coord._warned_affinity)

    def test_is_busy_detects_clicker_input_ownership(self):
        """FR-20260809-1506: 连点器运行（输入所有权被占）时 is_busy() 必须为 True，
        使计划任务到点进入 conflict_policy 分支而非被 _launch 静默丢弃。"""
        coord = self._make_coordinator()
        # 默认：无运行、无连点器（MagicMock().auto_clicker_is_running() is True 为 False）。
        self.assertFalse(coord.is_busy())

        coord._execution_controller.auto_clicker_is_running.return_value = True
        self.assertTrue(coord.is_busy())

        coord._execution_controller.auto_clicker_is_running.return_value = False
        self.assertFalse(coord.is_busy())

    def test_request_due_honors_conflict_policy_when_clicker_running(self):
        """FR-20260809-1506: 连点器运行期间到点，request_due 走 conflict_policy
        （skip → "skipped"，catch-up → "pending"），而不是 "failed" 静默丢弃。"""
        coord = self._make_coordinator()
        coord._execution_controller.auto_clicker_is_running.return_value = True
        task = SimpleNamespace(
            schedule_id="s1",
            conflict_policy="skip",
            profile_id="p1",
            max_runtime_seconds=60,
        )
        result = coord.request_due(task, datetime(2025, 1, 1, 10, 0, tzinfo=UTC))
        self.assertEqual(result, "skipped")
        self.assertIsNone(coord.active_schedule_id)  # 未启动

        coord._execution_controller.auto_clicker_is_running.return_value = False
        coord._execution_controller.prepare_and_launch_profile.return_value = True
        result = coord.request_due(task, datetime(2025, 1, 1, 10, 0, tzinfo=UTC))
        self.assertEqual(result, "running")  # 连点器停止后可正常启动

    def test_cross_thread_call_warns_once(self):
        # R1 core logic: an entry point invoked off the coordinator's own Qt
        # thread must trip the guard exactly once (non-fatal, idempotent flag).
        coord = self._make_coordinator()
        self.assertFalse(coord._warned_affinity)

        def off_thread():
            # coord was created on the main thread, so calling its guard from
            # this worker thread is a genuine affinity violation.
            coord._check_thread_affinity()

        worker = _OffThreadRunner(off_thread)
        worker.start()
        worker.wait()
        self.assertIsNone(worker.error.get("exc"))
        self.assertTrue(
            coord._warned_affinity,
            "off-thread call should have flagged an affinity violation",
        )

        # A second off-thread call must not re-warn (the flag is sticky).
        worker2 = _OffThreadRunner(off_thread)
        worker2.start()
        worker2.wait()
        self.assertIsNone(worker2.error.get("exc"))
        self.assertTrue(coord._warned_affinity)


class _OffThreadRunner(QThread):
    """Run a callable on a fresh worker thread so ``QThread.currentThread()``
    differs from the coordinator's (main-thread) affinity."""

    def __init__(self, target):
        super().__init__()
        self._target = target
        self.error: dict = {}

    def run(self):
        try:
            self._target()
        except Exception as exc:  # pragma: no cover - defensive
            self.error["exc"] = exc


if __name__ == "__main__":
    unittest.main()
