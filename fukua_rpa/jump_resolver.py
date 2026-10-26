"""Branch / jump / call-step transition resolution for RPAEngine.

Extracted verbatim from engine.py; methods call each other (and the rest of the
engine) through ``self``, so behavior is unchanged."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .commands import (
    effective_task_settings,
)
from .constants import (
    TASK_TYPE_UNTIL,
)
from .log_policy import (
    LOG_CRITICAL,
    LOG_FLOW,
)
from .log_telemetry import (
    format_complete_payload,
)
from .scheduler import (
    non_negative_int,
)
from .step_runtime import (
    ENTRY_BRANCH,
    ENTRY_CALL,
    ENTRY_SEQUENTIAL,
    StepAttemptResult,
    StepDispatchResult,
    StepExecutionContext,
    StepExecutionPolicy,
    StepTransition,
)

if TYPE_CHECKING:
    from .runtime_trace import RunTrace

logger = logging.getLogger(__name__)


class JumpResolverMixin:
    """Mixin providing branch, jump, and call-step transition resolution."""

    # Attributes provided by RPAEngine.__init__ or sibling mixins at runtime.
    log_level: int
    settlement_wait: float
    confidence: float
    enable_grayscale: bool
    min_scale: float
    max_scale: float
    scale_step: float
    run_trace: RunTrace

    if TYPE_CHECKING:
        def log(self, msg: str, category: str, *, critical: bool = False, private_msg: str | None = None) -> None: ...
        def log_enabled(self, category: str = ..., *, critical: bool = False, message: str = "") -> bool: ...
        def verbose_log_enabled(self, category: str = ..., *, critical: bool = False, message: str = "") -> bool: ...
        def stop(self) -> None: ...
        def reset_until_runtime(self, step_info: dict[str, Any]) -> None: ...
        def until_false_runtime(self, task: Mapping[str, Any], step_info: dict[str, Any]) -> tuple[bool, str, int, float]: ...
        def non_negative_int_value(self, value: Any, default: int = 0) -> int: ...
        def _task_skipped_for_loop(self, task: Mapping[str, Any], index: int, loop_count: int, entry_reason: str = ...) -> bool: ...
        def _debug_allows_step(self, task: Mapping[str, Any], step_number: int, loop_count: int) -> bool: ...
        def _image_target_cache_key_override(self, task: Mapping[str, Any]) -> Any: ...
        def _build_step_execution_context(self, *, task: Mapping[str, Any], task_count: int, index: int, loop_count: int, policy: StepExecutionPolicy) -> StepExecutionContext: ...
        def _execute_step_attempts(self, context: StepExecutionContext) -> StepAttemptResult: ...

    def _resolve_until_true_transition(self, task: Mapping[str, Any], index: int) -> StepTransition:
        """Resolve the transition when an until-condition becomes true.

        Resets the until runtime state and jumps to the configured target step
        or falls through to the next sequential step.

        Args:
            task: Step configuration dictionary.
            index: Zero-based index of the current step.

        Returns:
            A StepTransition describing the next execution target.
        """
        step_info: dict[str, Any] = {"step": index + 1}
        self.reset_until_runtime(step_info)
        true_jump = non_negative_int(task.get("until_true_jump", 0), 0)
        if true_jump > 0:
            if self.log_level >= 0:
                self.log(
                    "<font color='#4CAF50'><b>    -> [直到条件成立] "
                    f"条件满足，跳至第 {true_jump} 步继续执行。</b></font>",
                    LOG_FLOW,
                )
            return StepTransition(
                true_jump - 1,
                True,
                False,
                entry_reason=ENTRY_BRANCH,
            )
        if self.log_level >= 1:
            self.log(
                "<font color='#4CAF50'>    -> [直到条件成立] "
                "条件满足，继续下一步。</font>",
                LOG_FLOW,
            )
        return StepTransition(index + 1, True, False)

    def _resolve_until_false_transition(self, task: Mapping[str, Any], index: int) -> StepTransition:
        """Resolve the transition when an until-condition remains false.

        Evaluates the until runtime state; if the condition is not met, jumps
        back to the configured retry step. If the protection limit is reached,
        applies the configured limit action (stop, fail, or continue).

        Args:
            task: Step configuration dictionary.
            index: Zero-based index of the current step.

        Returns:
            A StepTransition describing the next execution target.
        """
        step_info: dict[str, Any] = {"step": index + 1}
        reached, reason, false_count, elapsed = self.until_false_runtime(
            task,
            step_info,
        )
        if not reached:
            # FR-20261008-303 / DEC-20260930-002-03（用户消息 2026-10-07 裁定）：
            # 现行默认值统一为 0，与 ``until_true_jump`` 及 task_model /
            # validation / workflow_analysis 同口径。旧缺省 1 会让缺键步骤凭空
            # 跳回第 1 步；正常路径由 ``materialize_runtime_references`` 写入 "0"
            # 故当前不可达，这里仅作防御性一致化。
            false_jump = non_negative_int(task.get("until_false_jump", 0), 0)
            if false_jump > 0:
                if self.log_level >= 0:
                    self.log(
                        "<font color='#FF9800'><b>    -> [直到条件成立] "
                        f"条件未满足（第 {false_count} 次，已等待 {elapsed:.1f}s），"
                        f"跳回第 {false_jump} 步。</b></font>",
                        LOG_FLOW,
                    )
                return StepTransition(
                    false_jump - 1,
                    True,
                    False,
                    entry_reason=ENTRY_BRANCH,
                )
            self.reset_until_runtime(step_info)
            if self.log_level >= 1:
                self.log(
                    "<font color='#FF9800'>    -> [直到条件成立] "
                    "条件未满足，但未设置跳回步骤，继续下一步。</font>",
                    LOG_FLOW,
                )
            return StepTransition(index + 1, True, False)

        action = str(task.get("until_on_limit", "继续下一步"))
        self.reset_until_runtime(step_info)
        if self.log_level >= 0:
            self.log(
                "<font color='orange'><b>    -> [直到条件成立] "
                f"{reason}，达到保护上限，处理方式：{action}。</b></font>",
                LOG_FLOW,
            )
        if action == "停止脚本":
            self.stop()
            return StepTransition(index + 1, True, False, True)
        if action == "按失败处理":
            return StepTransition(index + 1, False, True)
        return StepTransition(index + 1, True, False)

    def _resolve_branch_transition(self, policy: StepExecutionPolicy, index: int, failed_branch: bool) -> StepTransition:
        """Resolve the transition for success/failure branch jumps and skips.

        Applies the configured jump-to or skip-ahead behavior based on whether
        the step's branch outcome was success or failure.

        Args:
            policy: The step execution policy containing jump/skip settings.
            index: Zero-based index of the current step.
            failed_branch: Whether the step ended on a failure branch.

        Returns:
            A StepTransition describing the next execution target.
        """
        next_index = index + 1
        if failed_branch:
            if policy.fail_jump > 0:
                next_index = policy.fail_jump - 1
                if self.log_level >= 0:
                    self.log(
                        "<font color='#9C27B0'><b>    -> [条件分支] "
                        f"失败后跳至第 {policy.fail_jump} 步继续执行</b></font>",
                        LOG_FLOW,
                    )
            elif policy.fail_skip > 0:
                next_index = index + policy.fail_skip + 1
                if self.log_level >= 0:
                    self.log(
                        "<font color='#9C27B0'><b>    -> [条件分支] "
                        f"失败后跳过后续 {policy.fail_skip} 步指令</b></font>",
                        LOG_FLOW,
                    )
            return StepTransition(
                next_index,
                False,
                True,
                entry_reason=(
                    ENTRY_BRANCH
                    if policy.fail_jump > 0 or policy.fail_skip > 0
                    else ENTRY_SEQUENTIAL
                ),
            )

        if policy.success_jump > 0:
            next_index = policy.success_jump - 1
            if self.log_level >= 0:
                self.log(
                    "<font color='#4CAF50'><b>    -> [条件分支] "
                    f"成功后跳至第 {policy.success_jump} 步继续执行</b></font>",
                    LOG_FLOW,
                )
        elif policy.success_skip > 0:
            next_index = index + policy.success_skip + 1
            if self.log_level >= 0:
                self.log(
                    "<font color='#4CAF50'><b>    -> [条件分支] "
                    f"成功后跳过后续 {policy.success_skip} 步指令</b></font>",
                    LOG_FLOW,
                )
        return StepTransition(
            next_index,
            False,
            False,
            entry_reason=(
                ENTRY_BRANCH
                if policy.success_jump > 0 or policy.success_skip > 0
                else ENTRY_SEQUENTIAL
            ),
        )

    def _resolve_step_transition(
        self,
        *,
        task: Mapping[str, Any],
        policy: StepExecutionPolicy,
        index: int,
        last_status: str | None,
        failed_branch: bool,
        skipped_without_branch: bool,
    ) -> StepTransition:
        """Resolve the overall step transition after execution completes.

        Coordinates until-condition handling and branch resolution to produce
        the final transition decision for the step.

        Args:
            task: Step configuration dictionary.
            policy: The step execution policy.
            index: Zero-based index of the current step.
            last_status: The final status string from step execution.
            failed_branch: Whether the step ended on a failure branch.
            skipped_without_branch: Whether the step was skipped without branching.

        Returns:
            A StepTransition describing the next execution target.
        """
        if skipped_without_branch:
            return StepTransition(index + 1, False, failed_branch)

        if (
            policy.command == TASK_TYPE_UNTIL
            and not failed_branch
            and last_status in {"condition_true", "condition_false"}
        ):
            if last_status == "condition_true":
                return self._resolve_until_true_transition(task, index)
            until_transition = self._resolve_until_false_transition(task, index)
            if until_transition.stop_run or until_transition.condition_branch_handled:
                return until_transition
            failed_branch = until_transition.failed_branch

        return self._resolve_branch_transition(policy, index, failed_branch)

    def _record_step_transition(
        self,
        *,
        tasks: Sequence[Mapping[str, Any]],
        context: StepExecutionContext,
        attempt_result: StepAttemptResult,
        transition: StepTransition,
        interval_waited: bool,
        transition_started_ns: int,
        called_step: int = 0,
    ) -> None:
        """Record telemetry and trace data for a completed step transition.

        Emits a verbose log payload with timing details and records a
        structured trace entry for post-run diagnostics.

        Args:
            tasks: List of step configuration dictionaries.
            context: The execution context for the completed step.
            attempt_result: The attempt outcome snapshot.
            transition: The resolved transition decision.
            interval_waited: Whether a step-interval wait was applied.
            transition_started_ns: Monotonic timestamp when transition began.
            called_step: The 1-based called step number if this was a call, else 0.
        """
        next_index = transition.next_index
        if self.verbose_log_enabled(LOG_FLOW):
            self.log(
                format_complete_payload(
                    "完全/步骤转移",
                    {
                        "loop": context.loop_count,
                        "step": context.step_number,
                        "status": str(attempt_result.last_status or ""),
                        "attempts": attempt_result.attempts,
                        "success_count": attempt_result.success_count,
                        "consecutive_failures": (attempt_result.consecutive_failures),
                        "failed_branch": transition.failed_branch,
                        "condition_branch_handled": (
                            transition.condition_branch_handled
                        ),
                        "next_step": (
                            next_index + 1 if 0 <= next_index < len(tasks) else 0
                        ),
                        "next_entry_reason": transition.entry_reason,
                        "called_step": int(called_step or 0),
                        "step_interval_applied": interval_waited,
                        "configured_step_interval_seconds": self.settlement_wait,
                        "transition_elapsed_ms": round(
                            (time.perf_counter_ns() - transition_started_ns)
                            / 1_000_000.0,
                            3,
                        ),
                    },
                ),
                LOG_FLOW,
            )
        self.run_trace.record(
            "transition",
            loop=context.loop_count,
            step=context.step_number,
            command=context.command_name,
            status=str(attempt_result.last_status or ""),
            next_step=(next_index + 1 if 0 <= next_index < len(tasks) else 0),
            entry_reason=transition.entry_reason,
            called_step=int(called_step or 0),
        )

    def _call_target_index(self, task: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]], caller_index: int) -> int | None:
        """Resolve the target index for a call-step invocation.

        Validates that the configured call target is within bounds and is not
        the caller itself.

        Args:
            task: Step configuration dictionary of the calling step.
            tasks: List of all step configuration dictionaries.
            caller_index: Zero-based index of the calling step.

        Returns:
            The zero-based target index, or None if invalid.
        """
        call_step = self.non_negative_int_value(task.get("call_step", 0), 0)
        if call_step <= 0:
            return None
        target_index = call_step - 1
        if target_index == caller_index or not 0 <= target_index < len(tasks):
            if self.log_enabled(LOG_CRITICAL, critical=True):
                self.log(
                    f"<font color='orange'>第 {caller_index + 1} 步的指定执行目标无效，"
                    "本次已忽略。</font>",
                    LOG_CRITICAL,
                    critical=True,
                )
            return None
        return target_index

    def _execute_called_step(
        self, tasks: Sequence[Mapping[str, Any]], target_index: int, loop_count: int
    ) -> StepDispatchResult | None:
        """Execute a called step and return to the caller upon completion.

        Builds an execution policy and context for the target step, runs its
        attempts, and reports the outcome. Nested call-step invocations within
        the called step are ignored.

        Args:
            tasks: List of all step configuration dictionaries.
            target_index: Zero-based index of the step to call.
            loop_count: Current loop round number.

        Returns:
            A StepDispatchResult with the call outcome, or None if the target
            step was skipped or is not executable.
        """
        task = tasks[target_index]
        if self._task_skipped_for_loop(
            task,
            target_index,
            loop_count,
            ENTRY_CALL,
        ):
            if self.log_level >= 1:
                self.log(
                    f"<font color='gray'>    [指定执行] 第 {target_index + 1} 步"
                    "当前不可执行，已返回调用步骤。</font>",
                    LOG_FLOW,
                )
            return None
        if not self._debug_allows_step(task, target_index + 1, loop_count):
            return StepDispatchResult(
                target_index,
                False,
                True,
                ENTRY_CALL,
            )

        effective_task = effective_task_settings(task)
        policy = StepExecutionPolicy.from_task(
            effective_task,
            default_confidence=self.confidence,
            default_grayscale=self.enable_grayscale,
            min_scale=self.min_scale,
            max_scale=self.max_scale,
            scale_step=self.scale_step,
            cache_key_override=self._image_target_cache_key_override(effective_task),
        )
        context = self._build_step_execution_context(
            task=effective_task,
            task_count=len(tasks),
            index=target_index,
            loop_count=loop_count,
            policy=policy,
        )
        if self.log_level >= 0:
            self.log(
                f"<font color='#2563EB'><b>    -> [指定执行] 临时执行第 "
                f"{target_index + 1} 步；完成后返回调用步骤，忽略本步跳转。</b></font>",
                LOG_FLOW,
            )
        attempt_result = self._execute_step_attempts(context)
        if self.verbose_log_enabled(LOG_FLOW):
            self.log(
                format_complete_payload(
                    "完全/指定步骤返回",
                    {
                        "called_step": target_index + 1,
                        "status": str(attempt_result.last_status or ""),
                        "attempts": attempt_result.attempts,
                        "success_count": attempt_result.success_count,
                        "failed": attempt_result.failed_branch,
                        "nested_call_ignored": bool(
                            self.non_negative_int_value(
                                effective_task.get("call_step", 0), 0
                            )
                        ),
                    },
                ),
                LOG_FLOW,
            )
        return StepDispatchResult(
            target_index,
            attempt_result.made_progress,
            attempt_result.stop_run,
            ENTRY_CALL,
        )
