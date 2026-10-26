"""Automation execution engine; this module must not access Qt widgets directly."""

import logging
import os
import threading
import time
from dataclasses import replace
from typing import Any

from fukua_rpa.commands import (
    CLICK_COMMANDS,
    IMAGE_TARGET_COMMANDS,
)
from fukua_rpa.constants import (
    DEFAULT_CONFIDENCE,
    TASK_TYPE_MULTI_IMAGE_ACTION,
    TASK_TYPE_UNTIL,
)
from fukua_rpa.debug_session import DebugSession
from fukua_rpa.input_backend import (
    INPUT_BACKEND_DEFAULT,
    PyAutoGUIInputBackend,
)
from fukua_rpa.jump_resolver import JumpResolverMixin
from fukua_rpa.log_policy import (
    DEFAULT_CUSTOM_LOG_CATEGORIES,
    LOG_CRITICAL,
    LOG_FLOW,
    LOG_MODE_SIMPLE,
    LOG_RECOGNITION,
    LOG_STEP,
    LOG_TIMING,
    LogPolicy,
)
from fukua_rpa.log_telemetry import (
    complete_step_timing_payload,
    format_complete_payload,
)
from fukua_rpa.loop_scheduler import LoopSchedulerMixin
from fukua_rpa.params import TaskOnceParams
from fukua_rpa.performance import PerformanceMetrics
from fukua_rpa.run_lifecycle_glue import RunLifecycleMixin
from fukua_rpa.runtime_state import RunLifecycle
from fukua_rpa.runtime_trace import RunTrace
from fukua_rpa.step_runtime import (
    StepAttemptDirective,
    StepAttemptState,
    StepExecutionContext,
    attempt_failed,
    step_attempt_label,
    step_status_display,
)
from fukua_rpa.task_model import (
    format_region_text,
)
from fukua_rpa.vision.core import NativeVisionCore
from fukua_rpa.vision.scale_memory import ScaleMemoryStore

from .actions_exec import ActionExecutionMixin
from .conditions import UntilConditionMixin
from .coordinates import CoordinatePathMixin
from .expressions import ExpressionExecutionMixin
from .logging import EngineLoggingMixin
from .performance import EnginePerformanceMixin
from .timing import EngineTimingMixin
from .vision_exec import VisionExecutionMixin

logger = logging.getLogger(__name__)


def _force_execute_once(policy) -> Any:
    """Force a step to run exactly once regardless of its stored repeat_mode.

    Flow / wait / stop commands complete on a condition or a single action
    rather than on a success count, and their repetition is driven by the
    outer workflow jump/loop. Honouring an infinite ``repeat_mode`` on them
    would make the inner attempt loop never reach its target and hang (only an
    emergency stop could break it). The UI never exposes ``repeat_mode`` for
    these command types (``commands.py`` discards ``CONTROL_REPEAT`` for them),
    so this only guards against crafted / imported task data. Forcing
    ``执行一次`` is idempotent with the default UI policy and with
    ``effective_task_settings`` normalisation on the standard run path.
    """
    return replace(
        policy,
        repeat_mode="执行一次",
        repeat_count=1,
        target_successes=1,
    )


class RPAEngine(
    EnginePerformanceMixin,
    EngineTimingMixin,
    EngineLoggingMixin,
    VisionExecutionMixin,
    UntilConditionMixin,
    CoordinatePathMixin,
    ExpressionExecutionMixin,
    ActionExecutionMixin,
    LoopSchedulerMixin,
    JumpResolverMixin,
    RunLifecycleMixin,
):
    """Coordinate one runtime without depending on Qt widgets.

    Action, condition, coordinate, expression, vision, loop-scheduling, jump
    resolution and run-lifecycle details belong in the composed mixins. The
    remaining methods here focus on single-step execution. Constructor values
    are safe bootstrap defaults; each run receives its authoritative frozen
    ``EngineRunConfig`` before execution.
    """
    def __init__(self, base_dir=None, defer_backends=False) -> None:
        """Initialize the engine with safe bootstrap defaults.

        Args:
            base_dir: Base directory for resolving relative asset paths.
            defer_backends: When True, skip immediate input/vision backend
                initialization (useful for unit tests).
        """
        self.base_dir = os.path.abspath(base_dir) if base_dir else None
        self.lifecycle = RunLifecycle()
        self._injected_input_lock = threading.RLock()
        self._input_action_lock = threading.RLock()
        self._vision_lock = threading.RLock()
        self._injected_mouse_buttons = set()
        self._injected_keys = []
        self._prompt_lock = threading.Lock()
        self._active_prompt_request = None
        self.auxiliary_click_scheduler = None
        self._auxiliary_queue_warning_logged = False
        self.performance = PerformanceMetrics()
        self.last_performance_report = {}
        self.run_trace = RunTrace()
        self.last_run_trace = {}
        self.debug_session = DebugSession()
        self.min_scale = 1.0
        self.max_scale = 1.0
        self.scale_step = 0.05
        self.enable_grayscale = True
        self.confidence = DEFAULT_CONFIDENCE
        self.scan_region = None
        self.scan_regions = []
        self.dodge_x1 = 100
        self.dodge_y1 = 100
        self.dodge_x2 = 200
        self.dodge_y2 = 100
        self.enable_dodge = False
        self.enable_double_dodge = False
        self.double_dodge_wait = 0.015
        self.dodge_click_action = "none"
        self.move_duration = 0.0
        self.move_duration_jitter = 0.0
        self.click_hold = 0.04
        self.click_hold_jitter = 0.0
        self.settlement_wait = 0.0
        self.timeout_val = 0.0
        self.timeout_stop = False
        self.detect_delay = 0.0
        self.adaptive_backoff = True
        self.activate_before_click = False
        self.scene_wake_enabled = True
        self.scene_wake_sensitivity = "balanced"
        self.show_click_indicator = True
        self.show_mouse_trail = False
        self._run_config_applied = False
        self.use_native_core = True
        self.native_parallel_mode = "auto"
        self.use_native_scale_hint = True
        self.scale_memory_tier = "balanced"
        self.scale_memory_manual = ()
        self.scale_memory_custom_enabled = False
        self.scale_memory_preferred_limit = 3
        self.scale_memory_history_limit = 64
        self.use_fast_screenshot = True
        self.playback_speed = 1.0
        self.start_step_index = 0
        self.initial_start_step_index = None
        self.loop_start_round = 1
        self.loop_end_round = 0
        self.multi_target_mode = "快速一个"
        self.multi_target_order = "从上到下"
        self.multi_target_order_b = "不启用"
        self.multi_collect_first_hit_s = 0.0
        self.multi_collect_last_hit_s = 0.0
        self.multi_collect_max_s = 0.0
        self.multi_collect_max_targets = 0
        self.multi_collect_allow_partial = True
        self.loop_mode = "单次"
        self.loop_val = 1.0
        self.time_limit_stop_mode = "finish_loop"
        self.log_level = 0
        # Explicit per-run attribution used by the logging mixin.  A single
        # engine can execute only one run at a time, and WorkerThread clears the
        # mapping when that run ends.
        self.run_log_context: dict[str, object] = {}
        self.log_mode = LOG_MODE_SIMPLE
        self.log_custom_categories = tuple(DEFAULT_CUSTOM_LOG_CATEGORIES)
        self._log_policy = LogPolicy.create(self.log_mode, self.log_custom_categories)
        self._log_policy_key = None
        self.enable_tm_stop = True
        self.enable_tr_stop = True
        self.enable_key_stop = True

        self.callback_msg = None
        self.callback_status = None
        self.callback_click_indicator = None
        self.callback_debug = None
        self.callback_prompt = None
        self.callback_input_state = None
        self.input_backend = PyAutoGUIInputBackend()
        # 输入方式兼容开关（全局设置）：默认保持 PyAutoGUI 路径。
        self.input_backend_mode = INPUT_BACKEND_DEFAULT
        self.keyboard_scan_code = False
        self.mouse_move_settle_s = 0.0
        self.opencv_available = False
        self.opencv_threads = 0
        self.native_core = NativeVisionCore(base_dir=self.base_dir)
        self.native_core_logged = False
        self._task_window_backend = None
        self._credential_store = None
        self.scale_memory_store = ScaleMemoryStore()
        self._mss_instance = None
        self.global_deadline = None
        self.time_limit_reached = False
        self._reset_run_state()
        self.backends_ready = False
        self._backend_init_lock = threading.Lock()

        if not defer_backends:
            self.initialize_backends()

    def apply_scan_region(self, scan_region=None, scan_regions=None) -> None:
        """集中设置识别区域，避免外部直接写引擎内部字段（P5a 收口）。

        scan_region / scan_regions 是引擎的运行时态；配置的真相源在 profile
        配置，运行时由 runner_process / settings_binding 从配置加载到引擎。
        调用方应在传参前完成区域规范化（normalize）。
        """
        if scan_regions:
            self.scan_regions = list(scan_regions)
            self.scan_region = scan_regions[0] if len(scan_regions) == 1 else None
        else:
            self.scan_regions = []
            self.scan_region = scan_region

    def _debug_allows_step(self, task, step_number, loop_count) -> bool:
        """Check debug breakpoints and pause state before executing a step.

        Args:
            task: Normalized task dictionary for the current step.
            step_number: One-based step index.
            loop_count: Current loop iteration number.

        Returns:
            True if execution may proceed, False if stopped during pause.
        """
        breakpoint_enabled = self.as_bool(task.get("debug_breakpoint", False))
        breakpoint_active = breakpoint_enabled
        breakpoint_reason = "breakpoint"
        breakpoint_detail = ""
        condition = str(task.get("debug_condition", "") or "").strip()
        if breakpoint_enabled and condition:
            breakpoint_active, breakpoint_detail = self.evaluate_breakpoint_condition(
                condition,
                {
                    "step": step_number,
                    "loop": loop_count,
                    "attempt": 1,
                    "success_count": 0,
                },
            )
            breakpoint_reason = (
                "breakpoint_condition_error"
                if breakpoint_detail
                else "conditional_breakpoint"
            )
        debug_allowed, paused_seconds = self.debug_session.before_step(
            step=step_number,
            loop=loop_count,
            command=self.get_cmd_name(task.get("type")),
            breakpoint=breakpoint_active,
            breakpoint_reason=breakpoint_reason,
            breakpoint_detail=breakpoint_detail,
            stop_requested=lambda: self.lifecycle.stop_requested,
            callback=self.emit_debug_event,
        )
        if self.global_deadline is not None and paused_seconds > 0:
            # GIL-SAFETY: global_deadline is a simple float attribute. Under CPython GIL,
            # single attribute read/write is atomic. Auxiliary click threads reading this
            # value will see either the old or new value, never a torn read. Worst case:
            # a click thread uses a slightly stale deadline for one iteration (benign).
            self.global_deadline += paused_seconds
        return debug_allowed


    def _build_step_execution_context(
        self,
        *,
        task,
        task_count,
        index,
        loop_count,
        policy,
    ) -> StepExecutionContext:
        """Build the frozen execution context for a single step.

        Args:
            task: Normalized task dictionary.
            task_count: Total number of steps in the workflow.
            index: Zero-based step index.
            loop_count: Current loop iteration number.
            policy: Resolved StepPolicy for this step.

        Returns:
            A StepExecutionContext dataclass instance.
        """
        command = policy.command
        value = policy.value
        coordinate = self.parse_coordinate(value)
        coordinate_click = command in CLICK_COMMANDS and coordinate is not None
        image_click = command in CLICK_COMMANDS and coordinate is None
        coord_step_config = self.coord_step_options(task) if coordinate_click else None
        coord_sequence_config = (
            self.coord_sequence_options(task) if coordinate_click else None
        )
        if coord_sequence_config:
            coord_step_config = None
            # A custom-point visit owns its own batch size. Keeping the generic
            # repeat loop active here would multiply two independent counters.
            policy = _force_execute_once(policy)
        # FR-20260809-D1-④: 流程/等待类命令的强制单次已统一收进
        # StepExecutionPolicy.from_task（_FLOW_ONCE_COMMANDS，含 engine 与
        # jump_resolver/run_lifecycle_glue 所有构造路径）；此处不再重复实现。
        return StepExecutionContext(
            task=task,
            task_count=task_count,
            index=index,
            loop_count=loop_count,
            policy=policy,
            command_name=self.get_cmd_name(command),
            image_target=(
                command in IMAGE_TARGET_COMMANDS and coordinate is None
            )
            or command == TASK_TYPE_MULTI_IMAGE_ACTION,
            point_limit_enabled=(policy.point_limit_requested and image_click),
            coord_step_config=coord_step_config,
            coord_sequence_config=coord_sequence_config,
            image_click_config=(
                self.image_click_point_options(task) if image_click else None
            ),
            search_regions=self.step_search_regions(task, command, value),
        )


    def _stop_for_step_timeout(self, step_number) -> None:
        """Log a critical timeout message and stop the entire run.

        Args:
            step_number: One-based step index that timed out.
        """
        if self.log_enabled(LOG_CRITICAL, critical=True):
            self.log(
                "<font color='red'><b>    -> [超时急停] "
                f"步骤 {step_number} 达到单步超时，已停止全部循环。"
                "</b></font>",
                LOG_CRITICAL,
                critical=True,
            )
        self.stop()


    def _step_attempt_limit_directive(self, context, state) -> StepAttemptDirective:
        """Check execution-count and timeout limits before the next attempt.

        Args:
            context: StepExecutionContext for the current step.
            state: Mutable StepAttemptState tracking attempt progress.

        Returns:
            A StepAttemptDirective indicating PROCEED, COMPLETE, or STOP.
        """
        policy = context.policy
        if (
            policy.run_max_executions > 0
            and self.step_execution_counts.get(context.step_number, 0)
            >= policy.run_max_executions
        ):
            state.skipped_without_branch = True
            if self.log_level >= 0:
                self.log(
                    f"<font color='gray'>循环 #{context.loop_count} "
                    f"步 {context.step_number} ({context.command_name}) "
                    f"已达到本次运行上限 {policy.run_max_executions} 次，"
                    "跳过本步。</font>",
                    LOG_FLOW,
                )
            return StepAttemptDirective.COMPLETE

        timed_out = (
            policy.no_skip_wait
            and self.timeout_val > 0
            and time.monotonic() - state.started_at > self.timeout_val
        )
        if not timed_out:
            return StepAttemptDirective.PROCEED
        duration = time.monotonic() - state.started_at
        if self.log_level >= 0:
            self.log(
                f"<font color='orange'>循环 #{context.loop_count} "
                f"步 {context.step_number} ({context.command_name}) "
                f"禁止跳过等待超时，耗时: {duration:.2f}s</font>",
                LOG_STEP,
            )
        if self.timeout_stop:
            self._stop_for_step_timeout(context.step_number)
            return StepAttemptDirective.STOP
        state.failed_branch = True
        return StepAttemptDirective.COMPLETE


    def _step_recognition_wait_details(self, context, step_info) -> tuple[float, dict[str, Any] | None]:
        """Compute adaptive delay and scene-wake context for image targets.

        Args:
            context: StepExecutionContext for the current step.
            step_info: Dictionary with step/loop/attempt metadata.

        Returns:
            Tuple of (extra_delay_seconds, wake_context_or_None).
        """
        if not context.image_target:
            return 0.0, None
        policy = context.policy
        if policy.command == TASK_TYPE_MULTI_IMAGE_ACTION:
            return 0.0, None
        extra_delay = self.adaptive_extra_delay(policy.value, step_info)
        if extra_delay <= 0.0:
            return extra_delay, None
        wake_context = self.recognition_wake_context(
            policy.value,
            policy.cache_key,
            policy.confidence,
            policy.use_grayscale,
            context.search_regions,
            allow_fast_probe=(
                not context.point_limit_enabled and self.multi_target_mode != "全部匹配"
            ),
            allow_inverted=self.as_bool(
                context.task.get("inverse_match_en", False)
            ),
        )
        return extra_delay, wake_context


    def _apply_step_attempt_status(self, context, state, status) -> StepAttemptDirective:
        """Update attempt state based on the result of one execution.

        Args:
            context: StepExecutionContext for the current step.
            state: Mutable StepAttemptState tracking attempt progress.
            status: Result string from execute_task_once.

        Returns:
            A StepAttemptDirective indicating PROCEED, COMPLETE, RETRY, or STOP.
        """
        policy = context.policy
        if status == "stopped":
            return StepAttemptDirective.STOP
        if status == "skipped":
            state.skipped_without_branch = True
            return StepAttemptDirective.COMPLETE
        if status == "timeout" and self.timeout_stop:
            self._stop_for_step_timeout(context.step_number)
            return StepAttemptDirective.STOP

        if attempt_failed(policy.command, status):
            if policy.no_skip_wait and status != "timeout":
                if self.log_level >= 1:
                    self.log(
                        "    -> 本步骤已启用禁止跳过，将继续等待本步骤成功。",
                        LOG_FLOW,
                    )
                return StepAttemptDirective.RETRY
            state.consecutive_failures += 1
            if state.consecutive_failures >= policy.fail_limit:
                state.failed_branch = True
                if self.log_level >= 0:
                    self.log(
                        "<font color='#9C27B0'><b>    -> [条件分支] "
                        f"步骤 {context.step_number} 连续失败 "
                        f"{state.consecutive_failures} 次，结束本步。</b></font>",
                        LOG_FLOW,
                    )
                return StepAttemptDirective.COMPLETE
            return StepAttemptDirective.PROCEED

        state.success_count += 1
        state.consecutive_failures = 0
        return StepAttemptDirective.PROCEED


    def _execute_single_step_attempt(self, context, state) -> str | None:
        """Execute one attempt of a step, dispatching to the action handler.

        Args:
            context: StepExecutionContext for the current step.
            state: Mutable StepAttemptState tracking attempt progress.

        Returns:
            Status string from the action handler, or None if stopped.
        """
        policy = context.policy
        state.attempts += 1
        attempt_label = step_attempt_label(
            policy.target_successes,
            state.attempts,
            state.success_count,
        )
        status_command_name = f"{context.command_name} {attempt_label}".strip()
        step_info = {
            "step": context.step_number,
            "loop": context.loop_count,
            "cmd": status_command_name,
            "attempt": state.attempts,
            "success_count": state.success_count,
        }
        step_start_time = time.monotonic()
        collect_step_timing = self.verbose_log_enabled(LOG_TIMING)
        metrics_before = self.performance_snapshot() if collect_step_timing else None
        self.report_status(
            context.loop_count,
            context.step_number,
            context.task_count,
            status_command_name,
        )
        # 结构化事件：仅写入可选 JSONL 输出，供诊断包/外部工具关联分析；
        # 不进入界面日志面板，也不受日志策略过滤。
        self.log_structured_event(
            "step_start",
            category=LOG_STEP,
            step_index=context.step_number,
            action_id=policy.command,
            loop_index=context.loop_count,
        )

        needs_recognition_wait = (
            context.image_target or policy.command == TASK_TYPE_UNTIL
        )
        extra_delay, wake_context = self._step_recognition_wait_details(
            context,
            step_info,
        )
        self._log_complete_step_parameters(
            task=context.task,
            task_count=context.task_count,
            index=context.index,
            loop_count=context.loop_count,
            attempt=state.attempts,
            success_count=state.success_count,
            policy=policy,
            needs_recognition_wait=needs_recognition_wait,
            search_regions=context.search_regions,
            extra_delay=extra_delay,
            wake_context=wake_context,
            point_limit_enabled=context.point_limit_enabled,
            coord_step_config=context.coord_step_config,
            coord_sequence_config=context.coord_sequence_config,
            image_click_config=context.image_click_config,
        )
        pre_wait_started_ns = time.perf_counter_ns()
        pre_execute_wait_ms = 0.0
        if needs_recognition_wait or policy.no_skip_wait:
            if not self.wait_recognition_interval(extra_delay, wake_context):
                return None
            pre_execute_wait_ms = (
                time.perf_counter_ns() - pre_wait_started_ns
            ) / 1_000_000.0
        if context.search_regions and self.verbose_log_enabled(LOG_RECOGNITION):
            self.log(
                "    <font color='gray'>本步识别区域: "
                f"{format_region_text(context.search_regions[0])}</font>",
                LOG_RECOGNITION,
            )

        execute_started_ns = time.perf_counter_ns()
        self.log_structured_event(
            "action_exec",
            category=LOG_STEP,
            step_index=context.step_number,
            action_id=policy.command,
            attempt=state.attempts,
        )
        status = self.execute_task_once(TaskOnceParams(
            cmd=policy.command,
            val=policy.value,
            retry=policy.retry,
            step_info=step_info,
            cache_key=policy.cache_key,
            task_conf=policy.confidence,
            use_gray=policy.use_grayscale,
            point_limit_en=context.point_limit_enabled,
            point_limit_count=policy.point_limit_count,
            coord_step_config=context.coord_step_config,
            image_click_config=context.image_click_config,
            task=context.task,
            coord_sequence_config=context.coord_sequence_config,
            search_regions=context.search_regions,
        ))
        execute_ms = (time.perf_counter_ns() - execute_started_ns) / 1_000_000.0
        state.last_status = status
        if status != "skipped":
            self.last_step_success = status in ("success", "condition_true")
            state.made_progress = True
            self.step_execution_counts[context.step_number] = (
                self.step_execution_counts.get(context.step_number, 0) + 1
            )

        step_duration = time.monotonic() - step_start_time
        if collect_step_timing:
            self.log(
                format_complete_payload(
                    "完全/步骤耗时",
                    complete_step_timing_payload(
                        metrics_before,
                        self.performance_snapshot(),
                        total_ms=step_duration * 1000.0,
                        pre_execute_wait_ms=pre_execute_wait_ms,
                        execute_ms=execute_ms,
                    ),
                ),
                LOG_TIMING,
            )
        self.run_trace.record(
            "step_result",
            loop=context.loop_count,
            step=context.step_number,
            command=context.command_name,
            status=status,
            duration_ms=step_duration * 1000.0,
            attempt=state.attempts,
        )
        self.log_structured_event(
            "step_complete",
            category=LOG_STEP,
            step_index=context.step_number,
            action_id=policy.command,
            duration_ms=round(step_duration * 1000.0, 1),
            attempt=state.attempts,
            status=status,
        )
        if self.log_level >= 0:
            status_text, color = step_status_display(
                status,
                no_skip_wait=policy.no_skip_wait,
                timeout_emergency_stop=self.timeout_stop,
            )
            repeat_suffix = f" 第{attempt_label}次" if attempt_label else ""
            self.log(
                f"<font color='{color}'>循环 #{context.loop_count} "
                f"步 {context.step_number} ({context.command_name})"
                f"{repeat_suffix} {status_text}，耗时: "
                f"{step_duration:.2f}s</font>",
                LOG_STEP,
            )
        return status


    def _execute_step_attempts(self, context) -> Any:
        """Run the attempt loop for a single step until target or limit.

        Args:
            context: StepExecutionContext for the current step.

        Returns:
            A StepAttemptResult summarizing the outcome.
        """
        policy = context.policy
        command = policy.command
        target_successes = policy.target_successes
        state = StepAttemptState(started_at=time.monotonic())

        while not state.target_reached(target_successes):
            if self.check_stop_flag():
                return state.as_result(stop_run=True)
            limit_directive = self._step_attempt_limit_directive(context, state)
            if limit_directive is StepAttemptDirective.STOP:
                return state.as_result(stop_run=True)
            if limit_directive is StepAttemptDirective.COMPLETE:
                break
            status = self._execute_single_step_attempt(context, state)
            if status is None:
                return state.as_result(stop_run=True)

            status_directive = self._apply_step_attempt_status(
                context,
                state,
                status,
            )
            if status_directive is StepAttemptDirective.STOP:
                return state.as_result(stop_run=True)
            if status_directive is StepAttemptDirective.COMPLETE:
                break

            if not state.target_reached(target_successes) and not self.is_wait_command(command) and not self.wait_step_interval():
                return state.as_result(stop_run=True)
        return state.as_result()
