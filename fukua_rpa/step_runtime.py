"""Typed, side-effect-free parsing for one step's execution controls."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .constants import (
    TASK_TYPE_EXPRESSION,
    TASK_TYPE_STOP_RUN,
    TASK_TYPE_UNTIL,
    TASK_TYPE_WAIT,
    TASK_TYPE_WAIT_WINDOW,
)
from .scheduler import non_negative_int, positive_int
from .task_model import config_bool

# FR-20260809-D1-④: 流程/等待类命令——内层尝试循环强制单次（单一真相源，
# engine/core.py 不再自行实现同一强制）。
_FLOW_ONCE_COMMANDS = frozenset(
    {
        TASK_TYPE_UNTIL,
        TASK_TYPE_EXPRESSION,
        TASK_TYPE_WAIT,
        TASK_TYPE_STOP_RUN,
        TASK_TYPE_WAIT_WINDOW,
    }
)

ENTRY_SEQUENTIAL = "sequential"
ENTRY_BRANCH = "branch"
ENTRY_CALL = "call"
ENTRY_DEBUG = "debug"
ENTRY_MANUAL = "manual_start"


@dataclass(frozen=True)
class StepExecutionPolicy:
    """Immutable execution parameters for a single workflow step.

    Encapsulates confidence, repeat policy, branch offsets, and image-cache
    key resolved from the raw task mapping. Consumed by the engine dispatch
    loop to drive attempt/retry/transition logic without re-parsing config.
    """

    command: float | None
    value: object
    retry: object
    no_skip_wait: bool
    success_skip: int
    success_jump: int
    fail_skip: int
    fail_jump: int
    point_limit_requested: bool
    point_limit_count: int
    confidence: float
    use_grayscale: bool
    cache_key: str
    repeat_mode: str
    repeat_count: int
    fail_limit: int
    run_max_executions: int
    target_successes: int | None

    @classmethod
    def from_task(
        cls,
        task: Mapping[str, Any],
        *,
        default_confidence: float,
        default_grayscale: bool,
        min_scale: float,
        max_scale: float,
        scale_step: float,
        cache_key_override: str | None = None,
    ) -> StepExecutionPolicy:
        """Extract per-step execution parameters from a raw task mapping.

        Resolves confidence, grayscale matching, repeat policy, branch
        offsets, and image-cache key into a frozen typed policy consumed
        by the engine dispatch loop.

        Args:
            task: Raw task dictionary containing step configuration fields.
            default_confidence: Global default match confidence threshold.
            default_grayscale: Global default for grayscale matching.
            min_scale: Minimum image search scale factor.
            max_scale: Maximum image search scale factor.
            scale_step: Scale increment between multi-scale attempts.
            cache_key_override: Explicit cache key replacing the derived one.

        Returns:
            Immutable policy object for single-step execution control.
        """
        command = task.get("type")
        value = task.get("value")
        custom_enabled = config_bool(task.get("custom_en", False))
        if custom_enabled:
            try:
                confidence = float(task.get("custom_conf", default_confidence))
            except (TypeError, ValueError, OverflowError):
                confidence = float(default_confidence)
            use_grayscale = config_bool(task.get("custom_gray", default_grayscale))
        else:
            confidence = float(default_confidence)
            use_grayscale = bool(default_grayscale)

        repeat_mode = str(task.get("repeat_mode", "执行一次"))
        repeat_count = positive_int(task.get("repeat_count", 1), 1)
        if repeat_mode == "指定次数":
            target_successes: int | None = repeat_count
        elif repeat_mode == "无限重复":
            target_successes = None
        else:
            target_successes = 1
        # FR-20260809-D1-④: 流程/等待类命令强制单次在此统一归一化（修复前
        # engine/core.py 的 _force_execute_once 是第二处实现，新增命令类型时易
        # 漏配）。这些命令靠条件或单次动作完成，内层尝试循环只需执行一次；
        # UI 对它们本就不暴露 repeat_mode（commands.py 对流程命令 discard
        # CONTROL_REPEAT），正常构造数据不受影响。同步覆盖 repeat_mode/
        # repeat_count 标签（对齐 _force_execute_once 的日志呈现，crafted/
        # 导入数据如 repeat_mode="无限重复" 的流程命令不再显示误导标签）。
        if command in _FLOW_ONCE_COMMANDS:
            target_successes = 1
            repeat_mode = "执行一次"
            repeat_count = 1

        default_cache_key = (
            f"{value}_{min_scale}_{max_scale}_{scale_step}_{use_grayscale}"
        )
        resolved_cache_key = (
            cache_key_override
            if cache_key_override is not None
            else task.get("cache_key", default_cache_key)
        )
        return cls(
            command=command,
            value=value,
            retry=task.get("retry", 1),
            no_skip_wait=config_bool(task.get("no_skip_wait", False)),
            success_skip=non_negative_int(task.get("success_skip", 0), 0),
            success_jump=non_negative_int(task.get("success_jump", 0), 0),
            fail_skip=non_negative_int(task.get("fail_skip", 0), 0),
            fail_jump=non_negative_int(task.get("fail_jump", 0), 0),
            point_limit_requested=config_bool(task.get("point_limit_en", False)),
            point_limit_count=non_negative_int(task.get("point_limit_count", 0), 0),
            confidence=confidence,
            use_grayscale=use_grayscale,
            cache_key=resolved_cache_key,
            repeat_mode=repeat_mode,
            repeat_count=repeat_count,
            fail_limit=positive_int(task.get("fail_limit", 1), 1),
            run_max_executions=non_negative_int(task.get("run_max_executions", 0), 0),
            target_successes=target_successes,
        )


@dataclass(frozen=True)
class StepTransition:
    """Computed next-step decision produced by the transition resolver.

    Carries the target index, branch-handling flags, and an optional
    stop signal consumed by the engine loop to advance execution.
    """

    next_index: int
    condition_branch_handled: bool
    failed_branch: bool
    stop_run: bool = False
    entry_reason: str = ENTRY_SEQUENTIAL


@dataclass(frozen=True)
class StepAttemptResult:
    """Immutable snapshot of one step's attempt outcomes.

    Produced from mutable :class:`StepAttemptState` and consumed by the
    dispatch loop to decide retry, branch, and transition behavior.
    """

    last_status: str | None
    attempts: int
    success_count: int
    consecutive_failures: int
    failed_branch: bool
    skipped_without_branch: bool
    made_progress: bool
    stop_run: bool = False


class StepAttemptDirective(Enum):
    """Control signal produced while executing one repeated step."""

    PROCEED = "proceed"
    RETRY = "retry"
    COMPLETE = "complete"
    STOP = "stop"


@dataclass(frozen=True)
class StepExecutionContext:
    """Per-step context handed to action executors during dispatch.

    Bundles the raw task mapping, its resolved policy, and the derived
    image/coordinate configuration so executors can run without re-deriving
    settings from the engine.
    """

    task: Mapping[str, Any]
    task_count: int
    index: int
    loop_count: int
    policy: StepExecutionPolicy
    command_name: str
    image_target: bool
    point_limit_enabled: bool
    coord_step_config: Mapping[str, Any] | None
    coord_sequence_config: object | None
    image_click_config: Mapping[str, float] | None
    search_regions: Sequence[tuple[float, float, float, float]] | None

    @property
    def step_number(self) -> int:
        """One-based step position derived from the zero-based index."""
        return self.index + 1


@dataclass
class StepAttemptState:
    """Mutable accumulator tracking one step's attempt progress.

    Updated in place across repeated attempts, then frozen into a
    :class:`StepAttemptResult` for transition decisions.
    """

    started_at: float
    last_status: str | None = None
    attempts: int = 0
    success_count: int = 0
    consecutive_failures: int = 0
    failed_branch: bool = False
    skipped_without_branch: bool = False
    made_progress: bool = False

    def target_reached(self, target_successes: int | None) -> bool:
        """Check whether the success count meets the required target.

        Args:
            target_successes: Required successes, or None for infinite repeat.

        Returns:
            True if the bounded repeat target has been reached.
        """
        return target_successes is not None and self.success_count >= target_successes

    def as_result(self, *, stop_run: bool = False) -> StepAttemptResult:
        """Snapshot mutable attempt state into an immutable result object.

        Args:
            stop_run: Whether the engine should halt after this step.

        Returns:
            Frozen result consumed by the dispatch loop for transition logic.
        """
        return StepAttemptResult(
            last_status=self.last_status,
            attempts=self.attempts,
            success_count=self.success_count,
            consecutive_failures=self.consecutive_failures,
            failed_branch=self.failed_branch,
            skipped_without_branch=self.skipped_without_branch,
            made_progress=self.made_progress,
            stop_run=stop_run,
        )


@dataclass(frozen=True)
class LoopExecutionPlan:
    """Decision object describing whether to start another loop round.

    Produced by the loop scheduler and consumed by the engine to either
    begin a new round at *start_index* or terminate the run.
    """

    execute: bool = False
    stop_run: bool = False
    next_loop: int | None = None
    start_index: int = 0
    entry_reason: str = ENTRY_SEQUENTIAL


@dataclass(frozen=True)
class LoopExecutionResult:
    """Outcome of executing a single loop round.

    Reports whether any step made progress and whether the run should
    stop, used by the scheduler to decide subsequent rounds.
    """

    made_progress: bool = False
    stop_run: bool = False


@dataclass(frozen=True)
class StepDispatchResult:
    """Result of dispatching a single step within a loop round.

    Carries the next step index along with progress and stop flags so the
    engine can advance or terminate the dispatch loop.
    """

    next_index: int
    made_progress: bool = False
    stop_run: bool = False
    entry_reason: str = ENTRY_SEQUENTIAL


def step_attempt_label(
    target_successes: int | None,
    attempts: int,
    success_count: int,
) -> str:
    """Format a human-readable progress label for the current attempt.

    Returns an empty string for single-execution steps, a fraction like
    ``2/5`` for bounded repeats, or ``n/∞`` for infinite repeats.

    Args:
        target_successes: Required successes, or None for infinite repeat.
        attempts: Total attempts executed so far.
        success_count: Successful completions so far.

    Returns:
        Display label for UI status rendering.
    """
    if target_successes is None:
        return f"{attempts}/∞"
    if target_successes > 1:
        return f"{success_count + 1}/{target_successes}"
    return ""


def step_status_display(
    status: str,
    *,
    no_skip_wait: bool,
    timeout_emergency_stop: bool,
) -> tuple[str, str]:
    """Map an internal step status to a localized display label and color.

    Args:
        status: Internal status string (e.g. ``"success"``, ``"timeout"``).
        no_skip_wait: Whether the step is in wait-until-found mode.
        timeout_emergency_stop: Whether timeout triggers an emergency stop.

    Returns:
        Tuple of (display_text, color_name) for the UI status column.
    """
    if status == "timeout":
        return (
            "超时急停" if timeout_emergency_stop else "超时失败",
            "red" if timeout_emergency_stop else "orange",
        )
    if status == "not_found":
        return (
            "未找目标，继续等待" if no_skip_wait else "未找目标失败",
            "orange",
        )
    return {
        "success": ("完成", "gray"),
        "condition_true": ("条件满足", "green"),
        "condition_false": ("条件未满足", "orange"),
        "skipped": ("已跳过", "gray"),
        "error": ("执行异常", "red"),
        "stopped": ("已停止", "red"),
    }.get(status, ("完成", "gray"))


def attempt_failed(command: float | None, status: str) -> bool:
    """Determine whether a single attempt outcome counts as a failure.

    An attempt is failed when the status is timeout, not_found, or error;
    additionally, a condition expression evaluating to false is treated as
    failure for branch-strategy purposes.

    Args:
        command: The step command type identifier.
        status: Outcome status string from the action executor.

    Returns:
        True if the attempt should be counted as a failure.
    """
    return status in {"timeout", "not_found", "error"} or (
        command == TASK_TYPE_EXPRESSION and status == "condition_false"
    )
