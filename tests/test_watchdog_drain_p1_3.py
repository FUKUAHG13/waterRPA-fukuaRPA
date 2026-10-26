"""Regression tests for P1-3: watchdog catch-up queue no longer silently dropped.

Background
----------
``ScheduledRunCoordinator`` owns a capacity-one catch-up queue (``_pending``)
for schedule ids whose due time collided with an in-flight run. The watchdog
release path (``_on_watchdog_timeout``) force-releases the run mutex when a run
never reports completion, then must still drain ``_pending`` so queued
catch-up work can proceed.

The P1-3 bug: ``_drain_pending`` used to *pop* an entry and then launch it, so
a pop-then-launch-failure permanently lost that catch-up entry (and could
cascade into dropping the whole queue). The fixed logic (coordinator lines
~203-222) only pops the entry on a successful launch
(``result == "running"``); on ``"failed"`` the entry is retained and the loop
breaks, so it is retried later when the slot frees up.

These tests pin two facts:

1. (FIXED BUG) Triggering ``_on_watchdog_timeout`` while the run slot is still
   busy (launch fails) must NOT drop the pending catch-up entries — they remain
   available for retry. With the old dead-drop logic ``_pending`` would have
   been emptied.

2. (RECOVERY DRAIN) After a watchdog force-release, the eventual
   ``_on_run_ended`` for the recovered run still drains the pending queue
   correctly (and the normal completion path drains it too).

No Qt event loop is started: ``QTimer`` is mocked at the module level and every
GUI / native dependency (``run_controller`` and ``execution_controller``) is a
``MagicMock``.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from datetime import datetime
from unittest import mock

from fukua_rpa.scheduled_run_coordinator import ScheduledRunCoordinator
from fukua_rpa.scheduled_task_model import (
    CONFLICT_CATCHUP_ONCE,
    ScheduledTask,
    ScheduledTrigger,
    TriggerKind,
)


def _make_task(sid, *, enabled=True, max_runtime=None, profile_id="prof1"):
    return ScheduledTask(
        schedule_id=sid,
        name=sid,
        profile_id=profile_id,
        profile_name_snapshot="snapshot-" + sid,
        enabled=enabled,
        trigger=ScheduledTrigger(kind=TriggerKind.DAILY, local_time=datetime.min.time()),
        conflict_policy=CONFLICT_CATCHUP_ONCE,
        max_runtime_seconds=max_runtime,
    )


class _CoordinatorFixture:
    """Shared fakes for coordinator tests; no Qt event loop is started."""

    def _make_coordinator(self, *, is_active, launch_return=None, launch_side_effect=None):
        """Build a coordinator with fully mocked GUI/native dependencies.

        ``launch_return`` / ``launch_side_effect`` control
        ``execution_controller.prepare_and_launch_profile`` so we can simulate
        the run slot being busy (launch fails -> "failed") or free (launch
        succeeds -> "running").
        """

        run_controller = mock.MagicMock()
        run_controller.is_active = is_active  # predicate stubbed to return True/False

        execution_controller = mock.MagicMock()
        if launch_side_effect is not None:
            execution_controller.prepare_and_launch_profile.side_effect = launch_side_effect
        else:
            execution_controller.prepare_and_launch_profile.return_value = (
                True if launch_return is None else launch_return
            )

        # task_resolver: returns the task for a known schedule id, else None.
        resolver_store = {}

        def resolve(sid):
            return resolver_store.get(sid)

        with mock.patch("fukua_rpa.scheduled_run_coordinator.QTimer"):
            coord = ScheduledRunCoordinator(run_controller, execution_controller, resolve)

        coord._resolver_store = resolver_store  # helper for tests to register tasks
        return coord, run_controller, execution_controller

    def _register(self, coord, *tasks):
        for t in tasks:
            coord._resolver_store[t.schedule_id] = t

    def _inject_pending(self, coord, **sid_to_due):
        for sid, due in sid_to_due.items():
            coord._pending[sid] = due


class WatchdogDrainP1RegressionTests(_CoordinatorFixture, unittest.TestCase):
    # ----- FIXED BUG: watchdog must not drop pending on launch failure -----
    def test_watchdog_timeout_keeps_pending_when_launch_fails(self):
        """P1-3 core: a pop-then-launch-failure must NOT lose the catch-up queue.

        The run slot is still busy when the watchdog fires (``is_active`` True),
        so the drain's launch fails. The old dead-drop logic popped the entry
        first and silently dropped it; the fixed logic keeps every entry so it
        can be retried once the slot frees.
        """

        coord, run_controller, execution_controller = self._make_coordinator(
            is_active=True, launch_return=False  # launch fails: slot still busy
        )
        # A run is in flight and two catch-up tasks are queued.
        coord._active = ("s1", "run-s1")
        self._register(
            coord,
            _make_task("p1"),
            _make_task("p2"),
        )
        self._inject_pending(
            coord,
            p1=datetime(2026, 1, 1, 8, 0, 0),
            p2=datetime(2026, 1, 1, 9, 0, 0),
        )

        coord._on_watchdog_timeout()

        # --- assertions: nothing was dropped ---
        # Both catch-up entries survive the watchdog drain. The old dead-drop
        # code would have emptied ``_pending`` here.
        self.assertEqual(set(coord._pending.keys()), {"p1", "p2"})
        self.assertEqual(len(coord._pending), 2)

        # The mutex was force-released and the recovery marker set, so the
        # eventual run_ended can still drain the preserved queue.
        self.assertIsNone(coord._active)
        self.assertEqual(coord._recovering_id, "s1")

        # The watchdog actually released the real slot.
        run_controller.stop.assert_called_once()

        # A drain attempt happened (one launch call) but the entry was kept.
        execution_controller.prepare_and_launch_profile.assert_called_once()

    # ----- RECOVERY DRAIN: run_ended still drains after watchdog release -----
    def test_on_run_ended_drains_pending_after_watchdog_recovery(self):
        """After a force-release, the recovered run's completion drains the queue.

        First launch (during watchdog drain) fails because the slot is still
        busy -> entry retained (P1-3). When the original worker then reports
        completion via ``_on_run_ended``, the coordinator is in its
        ``_recovering_id`` state and must drain the preserved pending entry,
        which now launches successfully.
        """

        # Launch fails on the first call (slot busy during watchdog drain),
        # then succeeds on the second call (slot freed by the later stop()).
        coord, _run_controller, execution_controller = self._make_coordinator(
            is_active=True,
            launch_side_effect=[False, True],
        )
        coord._active = ("s1", "run-s1")
        self._register(coord, _make_task("p1"))
        self._inject_pending(coord, p1=datetime(2026, 1, 1, 8, 0, 0))

        # 1) Watchdog fires while the slot is busy -> entry preserved.
        coord._on_watchdog_timeout()
        self.assertIn("p1", coord._pending)
        self.assertEqual(coord._recovering_id, "s1")

        # 2) The original worker finally reports completion.
        coord._on_run_ended({"schedule_id": "s1", "outcome": "success"})

        # The preserved catch-up entry is now drained and launched.
        self.assertEqual(coord._pending, {})
        self.assertIsNone(coord._recovering_id)
        # One failed attempt during the watchdog + one successful during recovery.
        self.assertEqual(execution_controller.prepare_and_launch_profile.call_count, 2)

    # ----- NORMAL PATH: run_ended drains pending without watchdog -----
    def test_normal_completion_drains_pending(self):
        """The normal completion path (no watchdog) also drains the queue.

        Confirms the catch-up queue is consumed when a run ends cleanly, which
        is the steady-state behaviour the watchdog recovery must preserve.
        """

        coord, _run_controller, execution_controller = self._make_coordinator(
            is_active=False, launch_return=True
        )
        coord._active = ("s1", "run-s1")
        self._register(coord, _make_task("p1"))
        self._inject_pending(coord, p1=datetime(2026, 1, 1, 8, 0, 0))

        coord._on_run_ended({"schedule_id": "s1", "outcome": "success"})

        self.assertEqual(coord._pending, {})
        # The drained catch-up task is now the active run.
        self.assertEqual(coord._active[0], "p1")
        execution_controller.prepare_and_launch_profile.assert_called_once()

    # ----- GUARD: a stale/foreign run_ended never drops the queue -----
    def test_foreign_run_ended_does_not_drain_or_drop(self):
        """A run_ended for an unrelated schedule id must not touch the queue.

        Only the run we are tracking (or recovering) may drain ``_pending``; a
        foreign id must leave the queue intact and untouched.
        """

        coord, _run_controller, execution_controller = self._make_coordinator(
            is_active=True, launch_return=False
        )
        coord._active = ("s1", "run-s1")
        self._register(coord, _make_task("p1"))
        self._inject_pending(coord, p1=datetime(2026, 1, 1, 8, 0, 0))

        # A completion signal for a different schedule id (e.g. a manual run).
        coord._on_run_ended({"schedule_id": "other", "outcome": "success"})

        # The queue is untouched and no drain was attempted.
        self.assertIn("p1", coord._pending)
        # No watchdog fired here, so there is no recovery in progress.
        self.assertIsNone(coord._recovering_id)
        execution_controller.prepare_and_launch_profile.assert_not_called()


class ManualRunDrainINF10Tests(_CoordinatorFixture, unittest.TestCase):
    """INF10: 手动运行结束也必须排空补跑队列。

    ``request_due`` 在 ``is_busy()`` 为真时把 CONFLICT_CATCHUP_ONCE 任务写入
    ``_pending``，而 ``is_busy()`` 对**手动运行**同样为真
    （``run_controller.is_active``）。修复前 ``_on_run_ended`` 在 ``schedule_id``
    为空时直接早退，补跑条目只能等下一次计划任务结束或看门狗才被排空——若此后
    再无计划活动，该补跑就静默丢失。
    """

    def test_manual_run_end_drains_pending(self):
        """手动运行结束（无 schedule_id）应立即排空补跑队列。"""
        coord, _run_controller, execution_controller = self._make_coordinator(
            is_active=False, launch_return=True
        )
        # 手动运行不占用协调器的 _active，但补跑已排在它后面。
        self.assertIsNone(coord._active)
        self._register(coord, _make_task("p1"))
        self._inject_pending(coord, p1=datetime(2026, 1, 1, 8, 0, 0))

        coord._on_run_ended({"outcome": "success"})  # 无 schedule_id = 手动运行

        self.assertEqual(coord._pending, {})
        self.assertEqual(coord._active[0], "p1")
        execution_controller.prepare_and_launch_profile.assert_called_once()

    def test_manual_run_end_empty_schedule_id_string_also_drains(self):
        """``schedule_id`` 为空串（而非缺键）走同一分支。"""
        coord, _run_controller, execution_controller = self._make_coordinator(
            is_active=False, launch_return=True
        )
        self._register(coord, _make_task("p1"))
        self._inject_pending(coord, p1=datetime(2026, 1, 1, 8, 0, 0))

        coord._on_run_ended({"schedule_id": "", "outcome": "success"})

        self.assertEqual(coord._pending, {})
        execution_controller.prepare_and_launch_profile.assert_called_once()

    def test_manual_run_end_does_not_drain_while_scheduled_run_active(self):
        """INVARIANT: 协调器仍持有计划运行时，手动结束信号不得触发第二次启动。"""
        coord, _run_controller, execution_controller = self._make_coordinator(
            is_active=True, launch_return=True
        )
        coord._active = ("s1", "run-s1")
        self._register(coord, _make_task("p1"))
        self._inject_pending(coord, p1=datetime(2026, 1, 1, 8, 0, 0))

        coord._on_run_ended({"schedule_id": "", "outcome": "success"})

        # 队列保持原样，单运行互斥未被破坏。
        self.assertIn("p1", coord._pending)
        self.assertEqual(coord._active, ("s1", "run-s1"))
        execution_controller.prepare_and_launch_profile.assert_not_called()

    def test_manual_run_end_with_empty_queue_is_noop(self):
        """无补跑条目时手动结束不应产生任何启动尝试。"""
        coord, _run_controller, execution_controller = self._make_coordinator(
            is_active=False, launch_return=True
        )
        coord._on_run_ended({"outcome": "success"})

        self.assertEqual(coord._pending, {})
        self.assertIsNone(coord._active)
        execution_controller.prepare_and_launch_profile.assert_not_called()

    def test_manual_run_end_preserves_recovering_marker(self):
        """手动结束不属于看门狗恢复路径，不得清除 ``_recovering_id``。"""
        coord, _run_controller, _execution_controller = self._make_coordinator(
            is_active=False, launch_return=True
        )
        coord._recovering_id = "s1"
        coord._on_run_ended({"outcome": "success"})
        self.assertEqual(coord._recovering_id, "s1")

    def test_drain_failure_with_idle_engine_clears_entry_and_emits(self):
        """FR-003：引擎空闲仍启动失败 = 永久失败，须记账并清条目终止静默重试。

        修复前 ``_drain_pending`` 对 "failed" 只保留条目并 break：不写历史、
        不 emit、UI 无提示，条目此后随每次 run_ended 静默重试一次，用户以为
        "补跑一次"仍会执行。
        """

        coord, _run_controller, _execution = self._make_coordinator(
            is_active=False, launch_return=False
        )
        due = datetime(2026, 1, 1, 8, 0, 0)
        self._register(coord, _make_task("p1"))
        self._inject_pending(coord, p1=due)

        seen = []
        coord.run_start_failed.connect(lambda sid, d, busy: seen.append((sid, d, busy)))

        coord._on_run_ended({"outcome": "success"})  # 手动运行结束 -> 排空

        self.assertEqual(seen, [("p1", due, False)])
        self.assertEqual(coord._pending, {}, "永久失败必须清条目，终止静默重试")

    def test_drain_failure_with_busy_engine_keeps_entry_and_emits_busy(self):
        """FR-003：引擎被占用时的失败属临时忙（P1-3），保留条目重试且不记账。"""

        coord, _run_controller, _execution = self._make_coordinator(
            is_active=True, launch_return=False
        )
        due = datetime(2026, 1, 1, 8, 0, 0)
        coord._active = ("s1", "run-s1")
        self._register(coord, _make_task("p1"))
        self._inject_pending(coord, p1=due)

        seen = []
        coord.run_start_failed.connect(lambda sid, d, busy: seen.append((sid, d, busy)))

        coord._on_watchdog_timeout()

        self.assertEqual(seen, [("p1", due, True)])
        self.assertEqual(set(coord._pending), {"p1"}, "临时忙必须保留条目供重试")

    def test_catchup_queued_behind_manual_run_is_not_stranded(self):
        """端到端：手动运行期间入队的补跑，在手动运行结束时即被启动。"""
        coord, run_controller, execution_controller = self._make_coordinator(
            is_active=True, launch_return=True
        )
        task = _make_task("p1")
        self._register(coord, task)

        # 1) 手动运行进行中，计划任务到点 -> 进入补跑队列。
        result = coord.request_due(task, datetime(2026, 1, 1, 8, 0, 0))
        self.assertEqual(result, "pending")
        self.assertEqual(set(coord._pending), {"p1"})
        execution_controller.prepare_and_launch_profile.assert_not_called()

        # 2) 手动运行结束，槽位释放。
        run_controller.is_active = False
        coord._on_run_ended({"outcome": "success"})

        # 3) 补跑立即启动，不必等下一次计划任务或看门狗。
        self.assertEqual(coord._pending, {})
        self.assertEqual(coord._active[0], "p1")
        execution_controller.prepare_and_launch_profile.assert_called_once()


if __name__ == "__main__":
    unittest.main()
