"""Pure scheduling helpers shared by the execution engine and tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .task_model import task_is_disabled


def positive_int(value: Any, default: int = 1) -> int:
    """Coerce *value* to an int clamped to >= 1; fall back to *default* on failure."""
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return int(default)


def non_negative_int(value: Any, default: int = 0) -> int:
    """Coerce *value* to an int clamped to >= 0; fall back to *default* on failure."""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return int(default)


def task_active_in_loop(task: Mapping[str, Any] | None, loop_count: int) -> bool:
    """Return True if *task* should execute during loop round *loop_count*.

    Respects the per-step loop range (step_loop_start / step_loop_end) and
    the disabled flag. A task with end == 0 has no upper bound.
    """
    if not task or task_is_disabled(task):
        return False
    start = positive_int(task.get("step_loop_start", 1), 1)
    end = non_negative_int(task.get("step_loop_end", 0), 0)
    return loop_count >= start and not (end > 0 and loop_count > end)


def loop_number_allowed(
    loop_count: int,
    *,
    loop_mode: str,
    loop_value: float,
    loop_end_round: int,
) -> bool:
    """Check whether loop round *loop_count* is permitted by the global loop policy.

    Enforces the end-round cap, single-run mode, and counted-mode limit.
    Time-limited modes are governed externally by the engine's clock.
    """
    if loop_end_round > 0 and loop_count > loop_end_round:
        return False
    if loop_mode == "单次" and loop_count > 1:
        return False
    return not (loop_mode == "指定次数" and loop_count > loop_value)


def next_runnable_loop(
    tasks: Sequence[Mapping[str, Any]],
    after_loop: int,
    *,
    start_step_index: int,
    loop_start_round: int,
    loop_end_round: int,
    loop_mode: str,
    loop_value: float,
    exhausted: Callable[[Mapping[str, Any], int], bool],
) -> int | None:
    """Find the earliest future loop round with at least one runnable task.

    Scans tasks from *start_step_index* onward, skipping disabled and
    exhausted steps, and returns the smallest loop number > *after_loop*
    that satisfies both per-step and global loop constraints. Returns None
    when no future round can execute any task (e.g. single-run mode).

    Args:
        tasks: Full ordered task list.
        after_loop: The loop round just completed.
        start_step_index: First task index to consider (0-based).
        loop_start_round: Global earliest allowed round.
        loop_end_round: Global latest allowed round (0 = unbounded).
        loop_mode: One of the recognized loop mode strings.
        loop_value: Numeric parameter for counted/time modes.
        exhausted: Predicate (task, 1-based-step-number) -> True if the
            step can never run again.

    Returns:
        The next valid loop round number, or None.
    """
    if loop_mode == "单次" or not tasks:
        return None
    start_index = min(max(int(start_step_index), 0), len(tasks) - 1)
    candidates: list[int] = []
    for index in range(start_index, len(tasks)):
        task = tasks[index]
        if task_is_disabled(task) or exhausted(task, index + 1):
            continue
        step_start = positive_int(task.get("step_loop_start", 1), 1)
        step_end = non_negative_int(task.get("step_loop_end", 0), 0)
        candidate = max(int(after_loop) + 1, loop_start_round, step_start)
        if step_end > 0 and candidate > step_end:
            continue
        if loop_number_allowed(
            candidate,
            loop_mode=loop_mode,
            loop_value=loop_value,
            loop_end_round=loop_end_round,
        ):
            candidates.append(candidate)
    return min(candidates) if candidates else None


def is_wait_command(command: Any) -> bool:
    """Return True if *command* is the wait/sleep step type (code 5.0)."""
    try:
        return float(command) == 5.0
    except (TypeError, ValueError):
        return False
