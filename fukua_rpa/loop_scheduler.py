"""Loop scheduling and step iteration for RPAEngine.

Extracted verbatim from engine.py; methods call each other (and the rest of the
engine) through ``self``, so behavior is unchanged."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_warned_missing_run_config = False
# FR-20260809-D1-①: 按轮累积的下轮可运行索引表容量上限（防单次运行轮数
# 极多时无界增长；超限整体重建、惰性重算）。
_MAX_NEXT_RUNNABLE_ROUND_TABLES = 512
from collections.abc import Callable, Mapping, Sequence  # noqa: E402
from typing import TYPE_CHECKING, Any  # noqa: E402

from .commands import (  # noqa: E402
    IMAGE_TARGET_COMMANDS,
    command_name,
)
from .log_policy import (  # noqa: E402
    LOG_CRITICAL,
    LOG_FLOW,
    LOG_RUN,
)
from .privacy import private_error_text, safe_error_name  # noqa: E402
from .profile_paths import resolve_task_image_paths  # noqa: E402
from .scheduler import (  # noqa: E402
    is_wait_command as scheduler_is_wait_command,
)
from .scheduler import (  # noqa: E402
    loop_number_allowed as scheduler_loop_number_allowed,
)
from .scheduler import (  # noqa: E402
    next_runnable_loop as scheduler_next_runnable_loop,
)
from .scheduler import (  # noqa: E402
    non_negative_int,
    positive_int,
)
from .scheduler import (  # noqa: E402
    task_active_in_loop as scheduler_task_active_in_loop,
)
from .step_runtime import (  # noqa: E402
    ENTRY_MANUAL,
    ENTRY_SEQUENTIAL,
    LoopExecutionPlan,
    LoopExecutionResult,
    StepDispatchResult,
)
from .task_model import (  # noqa: E402
    parse_coordinate_sequence,
    parse_coordinate_text,
    parse_float_text,
)

if TYPE_CHECKING:
    from .mapping_backend import WindowMappingBackend
    from .runtime_trace import RunTrace

logger = logging.getLogger(__name__)


class LoopSchedulerMixin:
    """Mixin providing loop scheduling and step iteration logic for RPAEngine."""

    # Attributes provided by RPAEngine.__init__ or sibling mixins at runtime.
    base_dir: str | None
    loop_mode: str
    loop_val: float
    loop_end_round: int
    loop_start_round: int
    start_step_index: int
    settlement_wait: float
    log_level: int
    step_execution_counts: dict[int, int]
    coord_sequence_states: dict[int, Any]
    run_trace: RunTrace
    _task_window_backend: WindowMappingBackend | None

    if TYPE_CHECKING:
        def as_bool(self, value: Any) -> bool: ...
        def log(self, msg: str, category: str, *, critical: bool = False, private_msg: str | None = None) -> None: ...
        def report_status(self, loop_count: int = 1, step: int = 0, total: int = 0, cmd: str = "") -> None: ...
        def check_stop_flag(self) -> bool: ...
        def global_time_limit_reached(self) -> bool: ...
        def image_target_cache_key(self, task: Mapping[str, Any]) -> Any: ...
        def _debug_allows_step(self, task: Mapping[str, Any], step_number: int, loop_count: int) -> bool: ...
        def _dispatch_step(self, tasks: Sequence[Mapping[str, Any]], task: Mapping[str, Any], index: int, loop_count: int) -> StepDispatchResult: ...
        def _prepare_run(self, tasks: Sequence[Mapping[str, Any]], **kwargs: Any) -> Any: ...
        def _finalize_run_runtime(self, **kwargs: Any) -> None: ...

    def positive_int_value(self, value: Any, default: int = 1) -> int:
        """Normalize an arbitrary value to a positive integer.

        Args:
            value: The value to parse.
            default: Fallback positive integer on parse failure.

        Returns:
            A positive integer result.
        """
        return positive_int(value, default)

    def non_negative_int_value(self, value: Any, default: int = 0) -> int:
        """Normalize an arbitrary value to a non-negative integer.

        Args:
            value: The value to parse.
            default: Fallback non-negative integer on parse failure.

        Returns:
            A non-negative integer result.
        """
        return non_negative_int(value, default)

    def is_wait_command(self, cmd: Any) -> bool:
        """Check whether a command code represents a wait/sleep command.

        Args:
            cmd: The command code to check.

        Returns:
            True if the command is a wait command, False otherwise.
        """
        return scheduler_is_wait_command(cmd)

    def task_active_in_loop(self, task: Mapping[str, Any], loop_count: int) -> bool:
        """Determine whether a step is active in the given loop round.

        Args:
            task: Step configuration dictionary.
            loop_count: Current loop round number.

        Returns:
            True if the step is active in this round, False otherwise.
        """
        return scheduler_task_active_in_loop(task, loop_count)

    def task_exhausted_for_run(
        self,
        task: Mapping[str, Any],
        step_no: int,
        *,
        allow_jump_only: bool = False,
    ) -> bool:
        """Determine whether a step is exhausted and should no longer execute.

        A step is considered exhausted when it is disabled, jump-only, has
        reached its per-run execution cap, or its coordinate sequence has been
        fully consumed with a "skip after exhaustion" end action.

        Args:
            task: Step configuration dictionary.
            step_no: Step sequence number (1-based).

        Returns:
            True if the step is exhausted, False otherwise.
        """
        if self.as_bool(task.get("step_disabled", False)):
            return True
        if (
            self.as_bool(task.get("jump_only", False))
            and not allow_jump_only
        ):
            return True
        run_max = self.non_negative_int_value(task.get("run_max_executions", 0), 0)
        if run_max > 0 and self.step_execution_counts.get(int(step_no), 0) >= run_max:
            return True

        if self.as_bool(task.get("coord_sequence_en", False)):
            end_action = str(task.get("coord_sequence_end_action", "点完后跳过本步"))
            if end_action == "点完后跳过本步":
                points = parse_coordinate_sequence(
                    task.get("coord_sequence_points", "")
                )
                state = self.coord_sequence_states.get(int(step_no))
                if points and state and int(state.get("index", 0)) >= len(points):
                    return True
        return False

    def task_runnable_in_loop(self, task: Mapping[str, Any], step_no: int, loop_count: int) -> bool:
        """Determine whether a step is runnable in the given loop round.

        Args:
            task: Step configuration dictionary.
            step_no: Step sequence number (1-based).
            loop_count: Current loop round number.

        Returns:
            True if the step is active and not exhausted, False otherwise.
        """
        return self.task_active_in_loop(
            task, loop_count
        ) and not self.task_exhausted_for_run(task, step_no)

    def loop_number_allowed(self, loop_count: int) -> bool:
        """Check whether a given loop round is allowed under the current mode.

        Args:
            loop_count: The loop round number to check.

        Returns:
            True if the round is allowed to execute, False otherwise.
        """
        return scheduler_loop_number_allowed(
            loop_count,
            loop_mode=self.loop_mode,
            loop_value=self.loop_val,
            loop_end_round=self.loop_end_round,
        )

    def next_runnable_loop(self, tasks: Sequence[Mapping[str, Any]], after_loop: int) -> int | None:
        """Find the next loop round that still has runnable steps.

        Args:
            tasks: List of step configuration dictionaries.
            after_loop: The round to search after (exclusive).

        Returns:
            The next runnable loop round, or None if none exists.
        """
        return scheduler_next_runnable_loop(
            tasks,
            after_loop,
            start_step_index=self.start_step_index,
            loop_start_round=self.loop_start_round,
            loop_end_round=self.loop_end_round,
            loop_mode=self.loop_mode,
            loop_value=self.loop_val,
            exhausted=self.task_exhausted_for_run,
        )

    # ----- next-interval-task look-ahead cache (S1) -----------------------

    def _get_next_runnable_tables(self, n: int) -> dict[int, list]:
        """Return the per-loop-round next-runnable index tables, creating the
        container if needed.

        Keyed by ``loop_count``; each value is a list ``table`` where
        ``table[i]`` is the first runnable step index ``>= i`` (or ``None``).
        The tables are rebuilt lazily and self-correct for steps whose runnable
        state changes mid-round (e.g. ``run_max_executions`` / coord-sequence
        exhaustion) via live re-validation in :meth:`next_interval_task`.
        """
        tables = getattr(self, "_next_runnable_tables", None)
        if tables is None:
            tables = {}
            self._next_runnable_tables = tables
        elif len(tables) >= _MAX_NEXT_RUNNABLE_ROUND_TABLES:
            # FR-20260809-D1-①: 索引表按轮累积，超上限整体重建（修复前单次
            # 运行轮数极多时无界增长）；重建后惰性重算，行为自洽。
            tables.clear()
        return tables

    def _next_runnable_index_from(
        self,
        tasks: Sequence[Mapping[str, Any]],
        start: int,
        loop_count: int,
        table: list,
    ) -> int | None:
        """Live forward scan for the first runnable step at or after ``start``.

        Fills the skip cache (``table[start..found]``) so subsequent queries in
        the same window are O(1). Used both for cold misses and for re-validation
        after a cached hint turned out stale (dynamic step exhaustion).
        """
        n = len(tasks)
        i = start
        while i < n:
            if self.task_runnable_in_loop(tasks[i], i + 1, loop_count):
                for j in range(start, i + 1):
                    if j < len(table):
                        table[j] = i
                return i
            i += 1
        for j in range(start, n):
            if j < len(table):
                table[j] = None
        return None

    def next_interval_task(
        self, tasks: Sequence[Mapping[str, Any]], next_idx: int, loop_count: int
    ) -> Mapping[str, Any] | None:
        """Find the next runnable step to determine step-interval wait need.

        Searches forward from *next_idx* in the current round; if nothing is
        found and the loop has not ended, searches from the start step in the
        next round.

        S1: the per-round next-runnable index is memoized so the repeated
        per-step look-ahead that ``should_wait_step_interval`` performs does not
        degrade to O(n^2) on large workflows. The cache is self-correcting:
        cached hints are re-validated against the live runnable predicate, and a
        stale hint triggers a bounded rescan that also repairs the cache.

        Args:
            tasks: List of step configuration dictionaries.
            next_idx: Starting search index.
            loop_count: Current loop round number.

        Returns:
            The next runnable step dictionary, or None if none exists.
        """
        n = len(tasks)
        start = max(0, int(next_idx))
        tables = self._get_next_runnable_tables(n)

        def _lookup(round_no: int, from_idx: int) -> int | None:
            tbl = tables.get(round_no)
            if tbl is None:
                tbl = [None] * n
                tables[round_no] = tbl
            if from_idx >= n:
                return None
            hint = tbl[from_idx]
            if hint is not None and self.task_runnable_in_loop(
                tasks[hint], hint + 1, round_no
            ):
                return hint
            # Cold miss or stale hint (dynamic step exhaustion) -> live scan,
            # which also repairs the cache for the whole scanned window.
            return self._next_runnable_index_from(tasks, from_idx, round_no, tbl)

        found = _lookup(loop_count, start)
        if found is None:
            next_loop = loop_count + 1
            if self.loop_mode == "单次":
                return None
            if self.loop_end_round > 0 and next_loop > self.loop_end_round:
                return None
            start_idx = min(
                max(int(getattr(self, "start_step_index", 0)), 0),
                max(n - 1, 0),
            )
            found = _lookup(next_loop, start_idx)
            if found is None:
                return None
        return tasks[found]

    def should_wait_step_interval(
        self, tasks: Sequence[Mapping[str, Any]], current_cmd: Any, next_idx: int, loop_count: int
    ) -> bool:
        """Determine whether a step-interval wait is needed after the current step.

        No wait is applied when settlement_wait is unset, or when the current
        or next runnable step is a wait command.

        Args:
            tasks: List of step configuration dictionaries.
            current_cmd: Current step command code.
            next_idx: Starting index for the next step search.
            loop_count: Current loop round number.

        Returns:
            True if a wait should be applied, False otherwise.
        """
        if max(0.0, float(self.settlement_wait or 0.0)) <= 0:
            return False
        if self.is_wait_command(current_cmd):
            return False
        next_task = self.next_interval_task(tasks, next_idx, loop_count)
        if not next_task:
            return False
        return not self.is_wait_command(next_task.get("type"))

    def get_cmd_name(self, cmd_val: Any) -> str:
        """Return the human-readable name for a command code.

        Args:
            cmd_val: The command code.

        Returns:
            The display name string for the command.
        """
        return command_name(cmd_val)

    def parse_coordinate(self, val: Any) -> tuple[int, int] | None:
        """Parse text into a coordinate point.

        Args:
            val: Coordinate text such as "x,y".

        Returns:
            An (x, y) tuple, or None if the text is not a valid coordinate.
        """
        return parse_coordinate_text(val)

    def parse_float_value(self, value: Any, default: float = 0.0) -> float:
        """Parse text into a float, returning a default on failure.

        Args:
            value: The value to parse.
            default: Fallback value on parse failure.

        Returns:
            The parsed float result.
        """
        return parse_float_text(value, default)

    def task_window_backend(self) -> WindowMappingBackend:
        """Return the window mapping backend, creating it lazily on first call.

        Returns:
            The WindowMappingBackend instance.
        """
        backend = self._task_window_backend
        if backend is None:
            from .mapping_backend import WindowMappingBackend

            backend = WindowMappingBackend()
            self._task_window_backend = backend
        return backend

    def close_task_window_backend(self) -> None:
        """Close and release the window mapping backend instance if present."""
        backend = self._task_window_backend
        self._task_window_backend = None
        if backend is not None:
            backend.close()

    def _loop_limit_reached(self, loop_count: int) -> bool:
        """Check whether the global loop limit has been reached.

        Args:
            loop_count: Current loop round number.

        Returns:
            True if the run should stop due to a loop limit, False otherwise.
        """
        if self.global_time_limit_reached():
            return True
        if self.loop_end_round > 0 and loop_count > self.loop_end_round:
            if self.log_level >= 0:
                self.log(
                    "<font color='green'>>>> 提示: 已达到全局循环停止轮次 "
                    f"({self.loop_end_round})，任务正常结束</font>",
                    LOG_RUN,
                )
            return True
        if self.loop_mode == "单次" and loop_count > 1:
            return True
        if self.loop_mode == "指定次数" and loop_count > self.loop_val:
            if self.log_level >= 0:
                self.log(
                    "<font color='green'>>>> 提示: 已达到指定循环次数 "
                    f"({int(self.loop_val)}次)，任务正常结束</font>",
                    LOG_RUN,
                )
            return True
        return False

    def _prepare_loop_execution(self, tasks: Sequence[Mapping[str, Any]], loop_count: int) -> LoopExecutionPlan:
        """Decide how to proceed for a given loop round before step execution.

        Checks loop limits, skips rounds below the start range, and determines
        whether any runnable steps exist in this round or a future one.

        Args:
            tasks: List of step configuration dictionaries.
            loop_count: Current loop round number.

        Returns:
            A LoopExecutionPlan describing the action to take.
        """
        if self._loop_limit_reached(loop_count):
            return LoopExecutionPlan(stop_run=True)
        if loop_count < self.loop_start_round:
            # SAFETY: 停止标志必须在“等待循环范围”分支生效；否则无限循环模式下
            # 异常大的起始轮次会让停止/急停永久失效（审查 ENG-FR-001）。
            if self.check_stop_flag():
                return LoopExecutionPlan(stop_run=True)
            self.report_status(loop_count, 0, len(tasks), "等待循环范围")
            if self.log_level >= 1:
                self.log(
                    f"<font color='gray'>循环 #{loop_count} 低于全局起始循环 "
                    f"{self.loop_start_round}，本轮不执行步骤。</font>",
                    LOG_FLOW,
                )
            return LoopExecutionPlan()

        self.report_status(loop_count, 0, len(tasks), "")
        initial_start_index = getattr(self, "initial_start_step_index", None)
        uses_initial_start = (
            initial_start_index is not None
            and loop_count == max(1, int(self.loop_start_round))
        )
        configured_start_index = (
            initial_start_index
            if uses_initial_start
            else getattr(self, "start_step_index", 0)
        )
        start_index = min(
            max(int(configured_start_index), 0),
            max(len(tasks) - 1, 0),
        )
        # FR-20260809-D1-③: has_runnable_task 复用已构建的下轮可运行索引表——
        # 该轮表存在时 O(1) 判定（table[i] 是 >= i 的第一个可运行步骤索引）；
        # 首次未构建时回退全表扫描（与索引构建同判据，行为一致）。
        tables = getattr(self, "_next_runnable_tables", None)
        table = tables.get(loop_count) if tables else None
        manual_target_runnable = bool(
            uses_initial_start
            and tasks
            and self.task_active_in_loop(tasks[start_index], loop_count)
            and not self.task_exhausted_for_run(
                tasks[start_index],
                start_index + 1,
                allow_jump_only=True,
            )
        )
        if manual_target_runnable:
            has_runnable_task = True
        elif table is not None:
            has_runnable_task = table[start_index] is not None
        else:
            has_runnable_task = any(
                self.task_runnable_in_loop(tasks[index], index + 1, loop_count)
                for index in range(start_index, len(tasks))
            )
        if has_runnable_task:
            return LoopExecutionPlan(
                execute=True,
                start_index=start_index,
                entry_reason=(
                    ENTRY_MANUAL if uses_initial_start else ENTRY_SEQUENTIAL
                ),
            )

        next_loop = self.next_runnable_loop(tasks, loop_count)
        if next_loop is not None:
            if self.log_level >= 1:
                self.log(
                    f"<font color='gray'>循环 #{loop_count} 没有可执行步骤，"
                    f"直接跳至第 {next_loop} 次循环。</font>",
                    LOG_FLOW,
                )
            return LoopExecutionPlan(next_loop=next_loop)
        if self.log_level >= 0:
            self.log(
                "<font color='green'>>>> 提示: 本次运行已没有当前或未来"
                "可执行的步骤，任务正常结束</font>",
                LOG_RUN,
            )
        return LoopExecutionPlan(stop_run=True)

    def _task_skipped_for_loop(
        self,
        task: Mapping[str, Any],
        index: int,
        loop_count: int,
        entry_reason: str = ENTRY_SEQUENTIAL,
    ) -> bool:
        """Determine whether a step should be skipped in the current loop round.

        Checks disabled state, jump-only restriction, and per-step loop range.

        Args:
            task: Step configuration dictionary.
            index: Zero-based step index.
            loop_count: Current loop round number.
            entry_reason: How the step was entered (sequential, branch, call).

        Returns:
            True if the step should be skipped, False otherwise.
        """
        step_number = index + 1
        if self.as_bool(task.get("step_disabled", False)):
            if self.log_level >= 2:
                self.log(
                    f"<font color='gray'>循环 #{loop_count} 步 {step_number} "
                    "已暂时禁用，顺序略过。</font>",
                    LOG_FLOW,
                )
            return True
        if (
            self.as_bool(task.get("jump_only", False))
            and entry_reason == ENTRY_SEQUENTIAL
        ):
            if self.log_level >= 1:
                self.log(
                    f"<font color='gray'>循环 #{loop_count} 步 {step_number} "
                    "仅允许被跳转或指定执行，正常顺序已略过。</font>",
                    LOG_FLOW,
                )
            return True
        step_loop_start = self.positive_int_value(
            task.get("step_loop_start", 1),
            1,
        )
        if loop_count < step_loop_start:
            if self.log_level >= 2:
                self.log(
                    f"<font color='gray'>循环 #{loop_count} 步 {step_number} "
                    f"尚未到起始循环 {step_loop_start}，跳过本步。</font>",
                    LOG_FLOW,
                )
            return True
        step_loop_end = self.non_negative_int_value(
            task.get("step_loop_end", 0),
            0,
        )
        if step_loop_end > 0 and loop_count > step_loop_end:
            if self.log_level >= 2:
                self.log(
                    f"<font color='gray'>循环 #{loop_count} 步 {step_number} "
                    f"已超过结束循环 {step_loop_end}，跳过本步。</font>",
                    LOG_FLOW,
                )
            return True
        return False

    def _image_target_cache_key_override(self, task: Mapping[str, Any]) -> Any:
        """Return the precompute-compatible key for an image target step.

        Args:
            task: Step configuration dictionary.

        Returns:
            A cache key if the step is an image-target command, else None.
        """
        try:
            command = float(task.get("type", 0))
            if command not in IMAGE_TARGET_COMMANDS:
                return None
            return self.image_target_cache_key(task)
        except (TypeError, ValueError, OverflowError):
            return None

    def _execute_loop_steps(
        self,
        tasks: Sequence[Mapping[str, Any]],
        loop_count: int,
        start_index: int,
        callback_msg: Callable[[str], None] | None,
        entry_reason: str = ENTRY_SEQUENTIAL,
    ) -> LoopExecutionResult:
        """Execute steps sequentially within a single loop round.

        Args:
            tasks: List of step configuration dictionaries.
            loop_count: Current loop round number.
            start_index: Zero-based index of the first step to execute.
            callback_msg: Optional callback for user-facing messages.

        Returns:
            A LoopExecutionResult indicating progress and stop status.
        """
        made_progress = False
        index = start_index
        while index < len(tasks):
            if self.check_stop_flag():
                if callback_msg:
                    callback_msg("任务已停止")
                return LoopExecutionResult(made_progress, True)
            task = tasks[index]
            if self._task_skipped_for_loop(
                task,
                index,
                loop_count,
                entry_reason,
            ):
                index += 1
                entry_reason = ENTRY_SEQUENTIAL
                continue
            if not self._debug_allows_step(task, index + 1, loop_count):
                return LoopExecutionResult(made_progress, True)

            dispatched = self._dispatch_step(tasks, task, index, loop_count)
            made_progress = made_progress or dispatched.made_progress
            if dispatched.stop_run:
                return LoopExecutionResult(made_progress, True)
            index = dispatched.next_index
            entry_reason = dispatched.entry_reason
        return LoopExecutionResult(made_progress)

    def _plan_after_no_progress(self, tasks: Sequence[Mapping[str, Any]], loop_count: int) -> LoopExecutionPlan:
        """Decide the next action when a loop round made no progress.

        Searches for a future runnable loop; if none exists the run ends.

        Args:
            tasks: List of step configuration dictionaries.
            loop_count: Current loop round number.

        Returns:
            A LoopExecutionPlan either jumping to a future round or stopping.
        """
        next_loop = self.next_runnable_loop(tasks, loop_count)
        if next_loop is not None:
            if self.log_level >= 1:
                self.log(
                    f"<font color='gray'>循环 #{loop_count} 的步骤均已永久跳过，"
                    f"直接跳至第 {next_loop} 次循环。</font>",
                    LOG_FLOW,
                )
            return LoopExecutionPlan(next_loop=next_loop)
        if self.log_level >= 0:
            self.log(
                "<font color='green'>>>> 提示: 所有步骤均已完成或达到"
                "本次运行上限，任务正常结束</font>",
                LOG_RUN,
            )
        return LoopExecutionPlan(stop_run=True)

    def _run_loop_iteration(
        self,
        tasks: Sequence[Mapping[str, Any]],
        loop_count: int,
        callback_msg: Callable[[str], None] | None,
    ) -> LoopExecutionPlan:
        """Execute a single loop iteration: plan, execute, and decide next.

        Args:
            tasks: List of step configuration dictionaries.
            loop_count: Current loop round number.
            callback_msg: Optional callback for user-facing messages.

        Returns:
            A LoopExecutionPlan describing the next action.
        """
        plan = self._prepare_loop_execution(tasks, loop_count)
        if plan.stop_run or plan.next_loop is not None or not plan.execute:
            return plan
        result = self._execute_loop_steps(
            tasks,
            loop_count,
            plan.start_index,
            callback_msg,
            plan.entry_reason,
        )
        if result.stop_run:
            return LoopExecutionPlan(stop_run=True)
        if result.made_progress:
            return LoopExecutionPlan()
        return self._plan_after_no_progress(tasks, loop_count)

    def run_tasks(
        self,
        tasks: Sequence[Mapping[str, Any]],
        callback_msg: Callable[[str], None] | None = None,
        callback_status: Callable[..., None] | None = None,
        callback_click_indicator: Callable[..., None] | None = None,
        callback_debug: Callable[..., None] | None = None,
        callback_prompt: Callable[..., None] | None = None,
        run_id: int | None = None,
    ) -> None:
        """Execute the full task list as the main run entry point.

        Handles run reservation, resource pre-loading, loop-mode iteration,
        and finalization including performance reporting and resource cleanup.
        Exceptions during execution are caught and reported as error outcomes.

        Args:
            tasks: List of step configuration dictionaries.
            callback_msg: Log message callback.
            callback_status: Run progress status callback.
            callback_click_indicator: Click indicator callback.
            callback_debug: Debug event callback.
            callback_prompt: Popup prompt callback.
            run_id: Pre-reserved run ID; None triggers internal reservation.
        """
        tasks = resolve_task_image_paths(tasks, self.base_dir or os.getcwd())
        # S1: reset the per-run next-runnable look-ahead cache so stale round
        # tables from a previous run never leak into this one.
        self._next_runnable_tables = {}
        global _warned_missing_run_config
        if tasks and not getattr(self, "_run_config_applied", False) and not _warned_missing_run_config:
            # S3: run_tasks depends on the caller having applied the run config
            # first; a forgotten apply_to would silently fall back to engine
            # defaults. Surface that loudly instead of failing silently.
            _warned_missing_run_config = True
            logger.warning(
                "engine.run_tasks 在 EngineRunConfig.apply_to 未先应用的情况下执行，"
                "将使用引擎默认配置而非用户设定；若非有意为之，请确认调用方已调用 apply_to。"
            )
        run_id = self._prepare_run(
            tasks,
            run_id=run_id,
            callback_msg=callback_msg,
            callback_status=callback_status,
            callback_click_indicator=callback_click_indicator,
            callback_debug=callback_debug,
            callback_prompt=callback_prompt,
        )
        if run_id is None:
            return
        loop_count = max(0, int(self.loop_start_round) - 1)

        run_outcome = "finished"
        run_error = ""
        try:
            while True:
                loop_count += 1
                plan = self._run_loop_iteration(
                    tasks,
                    loop_count,
                    callback_msg,
                )
                if plan.stop_run:
                    break
                if plan.next_loop is not None:
                    loop_count = plan.next_loop - 1

        except Exception as e:
            run_outcome = "error"
            run_error = safe_error_name(e)
            self.log(
                f"<font color='red'>引擎异常: {safe_error_name(e)}</font>",
                LOG_CRITICAL,
                critical=True,
                private_msg=(
                    f"<font color='red'>引擎异常: {private_error_text(e)}</font>"
                ),
            )
        finally:
            self._finalize_run_runtime(
                run_id=run_id,
                run_outcome=run_outcome,
                run_error=run_error,
                callback_msg=callback_msg,
            )
