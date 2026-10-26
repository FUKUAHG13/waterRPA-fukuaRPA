"""Regression tests for the S1 next-runnable look-ahead cache in
``loop_scheduler.LoopSchedulerMixin.next_interval_task``.

The cache must return exactly the same result as the original linear scan
(implemented here as ``ref_next_interval_index``), including for steps whose
runnable state changes mid-run (``run_max_executions`` exhaustion), where the
cache must self-correct rather than return a stale hint.
"""

from __future__ import annotations

import random
import unittest

from fukua_rpa.loop_scheduler import LoopSchedulerMixin


class FakeScheduler(LoopSchedulerMixin):
    """Minimal stand-in exposing only what ``next_interval_task`` needs."""

    def __init__(
        self,
        loop_mode="指定次数",
        loop_val=5,
        loop_end_round=0,
        loop_start_round=1,
        start_step_index=0,
    ) -> None:
        self.loop_mode = loop_mode
        self.loop_val = float(loop_val)
        self.loop_end_round = loop_end_round
        self.loop_start_round = loop_start_round
        self.start_step_index = start_step_index
        self.settlement_wait = 1.0
        self.step_execution_counts: dict[int, int] = {}
        self.coord_sequence_states: dict[int, dict] = {}

    def as_bool(self, value) -> bool:
        return bool(value)


def ref_next_interval_index(sched, tasks, next_idx, loop_count):
    """Reimplementation of the ORIGINAL linear-scan ``next_interval_task``.

    Uses the same live ``task_runnable_in_loop`` predicate so the only
    structural difference vs the cached version is the scan vs table lookup.
    """
    n = len(tasks)
    for look in range(max(0, int(next_idx)), n):
        if sched.task_runnable_in_loop(tasks[look], look + 1, loop_count):
            return look
    next_loop = loop_count + 1
    if sched.loop_mode == "单次":
        return None
    if sched.loop_end_round > 0 and next_loop > sched.loop_end_round:
        return None
    start_idx = min(
        max(int(getattr(sched, "start_step_index", 0)), 0), max(n - 1, 0)
    )
    for look in range(start_idx, n):
        if sched.task_runnable_in_loop(tasks[look], look + 1, next_loop):
            return look
    return None


def act_next_interval_index(sched, tasks, next_idx, loop_count):
    # NOTE: tasks are equal dicts, so list.index() would match the wrong
    # (first equal) element. Match by object identity to recover the true
    # position returned by the scheduler.
    t = sched.next_interval_task(tasks, next_idx, loop_count)
    if t is None:
        return None
    for i, other in enumerate(tasks):
        if other is t:
            return i
    return None


def _make_task(disable=False, jump_only=False, run_max=0, loop_start=1, loop_end=0):
    return {
        "type": 1.0,
        "step_disabled": disable,
        "jump_only": jump_only,
        "run_max_executions": run_max,
        "step_loop_start": loop_start,
        "step_loop_end": loop_end,
    }


class NextIntervalCacheEquivalenceTests(unittest.TestCase):
    def test_all_runnable_sequential(self):
        sched = FakeScheduler()
        tasks = [_make_task() for _ in range(10)]
        for i in range(10):
            self.assertEqual(
                act_next_interval_index(sched, tasks, i, 1),
                ref_next_interval_index(sched, tasks, i, 1),
            )

    def test_disabled_steps_skipped(self):
        sched = FakeScheduler()
        tasks = [_make_task(disable=(j % 2 == 1)) for j in range(8)]
        for i in range(8):
            self.assertEqual(
                act_next_interval_index(sched, tasks, i, 1),
                ref_next_interval_index(sched, tasks, i, 1),
            )

    def test_single_mode_no_next_round(self):
        sched = FakeScheduler(loop_mode="单次")
        tasks = [_make_task(disable=True) for _ in range(5)]
        # current round has nothing runnable -> None (no next round in single mode)
        self.assertEqual(
            act_next_interval_index(sched, tasks, 0, 1),
            ref_next_interval_index(sched, tasks, 0, 1),
        )
        self.assertIsNone(act_next_interval_index(sched, tasks, 0, 1))

    def test_next_round_scan(self):
        sched = FakeScheduler(loop_mode="指定次数", loop_val=3, loop_end_round=0)
        tasks = [_make_task(disable=True) for _ in range(5)]
        # nothing runnable in round 1; next round also nothing -> None, but the
        # next-round branch must still be exercised without crashing.
        self.assertEqual(
            act_next_interval_index(sched, tasks, 0, 1),
            ref_next_interval_index(sched, tasks, 0, 1),
        )
        # Make a step active only in round 2 via step_loop_start.
        tasks[3] = _make_task(loop_start=2)
        self.assertEqual(
            act_next_interval_index(sched, tasks, 0, 1),
            ref_next_interval_index(sched, tasks, 0, 1),
        )

    def test_start_step_index_offset(self):
        sched = FakeScheduler(start_step_index=2)
        tasks = [_make_task() for _ in range(6)]
        tasks[0] = _make_task(disable=True)
        tasks[1] = _make_task(disable=True)
        # next-round scan should start from index 2
        self.assertEqual(
            act_next_interval_index(sched, tasks, 5, 1),
            ref_next_interval_index(sched, tasks, 5, 1),
        )

    def test_fuzz_equivalence(self):
        rng = random.Random(20260730)
        for _ in range(3000):
            n = rng.randint(0, 25)
            tasks = []
            for _ in range(n):
                tasks.append(
                    _make_task(
                        disable=rng.random() < 0.3,
                        jump_only=rng.random() < 0.1,
                        run_max=rng.choice([0, 0, 0, 2, 3]),
                        loop_start=rng.choice([1, 1, 1, 2, 3]),
                        loop_end=rng.choice([0, 0, 0, 4, 5]),
                    )
                )
            loop_mode = rng.choice(["单次", "指定次数"])
            sched = FakeScheduler(
                loop_mode=loop_mode,
                loop_val=rng.choice([1, 2, 3, 5]),
                loop_end_round=rng.choice([0, 0, 4, 6]),
                start_step_index=rng.randint(0, max(0, n - 1)),
            )
            loop_count = rng.randint(1, 4)
            for i in range(n + 1):
                self.assertEqual(
                    act_next_interval_index(sched, tasks, i, loop_count),
                    ref_next_interval_index(sched, tasks, i, loop_count),
                    msg=f"n={n} i={i} loop={loop_count} mode={loop_mode}",
                )

    def test_dynamic_exhaustion_self_corrects(self):
        # A step with run_max_executions becomes exhausted after enough runs.
        # The cache must not return it once exhausted.
        sched = FakeScheduler()
        tasks = [_make_task(run_max=1), _make_task()]
        # Initially both runnable; query from 0 should find index 0.
        self.assertEqual(act_next_interval_index(sched, tasks, 0, 1), 0)
        # Simulate step 0 having executed its single allowed run.
        sched.step_execution_counts[1] = 1
        # Now index 0 is exhausted; the cache must self-correct to index 1.
        self.assertEqual(act_next_interval_index(sched, tasks, 0, 1), 1)
        # And from index 1 we still find index 1.
        self.assertEqual(act_next_interval_index(sched, tasks, 1, 1), 1)

    def test_large_list_no_crash_and_correct(self):
        sched = FakeScheduler()
        tasks = [_make_task(disable=(j % 3 == 0)) for j in range(5000)]
        # spot-check a few lookups against the reference
        for i in (0, 1000, 2500, 4999, 4998):
            self.assertEqual(
                act_next_interval_index(sched, tasks, i, 1),
                ref_next_interval_index(sched, tasks, i, 1),
            )


if __name__ == "__main__":
    unittest.main()
