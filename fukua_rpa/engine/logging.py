# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""引擎日志适配服务。从 RPAEngine 提取的非执行职责。"""

from __future__ import annotations

import os
import time

from fukua_rpa.commands import IMAGE_TARGET_COMMANDS
from fukua_rpa.input_backend import INPUT_BACKEND_DEFAULT, create_input_backend
from fukua_rpa.log_policy import (
    LOG_ACTION,
    LOG_MODE_CUSTOM,
    LOG_MODE_LEVELS,
    LOG_PARAMETERS,
    LOG_STEP,
    LogPolicy,
    log_mode_from_level,
    normalize_log_categories,
    normalize_log_mode,
)
from fukua_rpa.log_telemetry import format_complete_payload
from fukua_rpa.logging_service import write_log


class EngineLoggingMixin:
    """日志策略配置与步骤日志格式化（Mixin，由 RPAEngine 继承）。"""

    def configure_input_backend(self, mode, *, key_scan_codes=False):
        """Swap the input backend for an explicitly selected compatibility mode.

        ``docs/设计基准.md``: the default path stays PyAutoGUI. This never runs
        on a fallback after a failure — a failing SendInput call surfaces as an
        input error instead of silently switching back mid-run.
        """

        self.input_backend_mode = str(mode or INPUT_BACKEND_DEFAULT)
        self.keyboard_scan_code = bool(key_scan_codes)
        self.input_backend = create_input_backend(
            self.input_backend_mode, key_scan_codes=self.keyboard_scan_code
        )
        return self.input_backend

    def configure_log_policy(self, mode, custom_categories=None):
        """设置日志模式与自定义分类，并返回生效的日志策略。

        Args:
            mode: 目标日志模式。
            custom_categories: 自定义模式下启用的日志分类；为 None 时沿用现有值。

        Returns:
            应用配置后的当前 LogPolicy。
        """
        self.log_mode = normalize_log_mode(mode, self.log_level)
        self.log_custom_categories = normalize_log_categories(
            custom_categories
            if custom_categories is not None
            else self.log_custom_categories
        )
        self.log_level = LOG_MODE_LEVELS[self.log_mode]
        self._log_policy_key = None
        return self.current_log_policy()

    def current_log_policy(self):
        """返回当前生效的日志策略。

        当日志级别为负（关闭）时返回 None；否则按当前模式与分类
        惰性重建并缓存 LogPolicy。

        Returns:
            当前 LogPolicy，或日志关闭时的 None。
        """
        if self.log_level < 0:
            return None
        mode = normalize_log_mode(self.log_mode, self.log_level)
        if mode != LOG_MODE_CUSTOM and LOG_MODE_LEVELS.get(mode) != int(self.log_level):
            mode = log_mode_from_level(self.log_level)
        custom_categories = normalize_log_categories(self.log_custom_categories)
        key = (mode, int(self.log_level), custom_categories)
        if key != self._log_policy_key:
            self._log_policy = LogPolicy.create(mode, custom_categories)
            self._log_policy_key = key
        return self._log_policy

    def log_enabled(self, category=LOG_ACTION, *, critical=False, message=""):
        """判断指定分类的日志在当前策略下是否允许输出。

        Args:
            category: 日志分类。
            critical: 是否为关键日志。
            message: 待输出消息，供策略按内容判断。

        Returns:
            允许输出返回 True，否则 False。
        """
        policy = self.current_log_policy()
        return bool(
            policy and policy.allows(category, critical=critical, message=message)
        )

    def verbose_log_enabled(self, category=LOG_ACTION, *, critical=False, message=""):
        """判断指定分类的详细（verbose）日志是否允许输出。

        Args:
            category: 日志分类。
            critical: 是否为关键日志。
            message: 待输出消息，供策略按内容判断。

        Returns:
            允许输出详细日志返回 True，否则 False。
        """
        policy = self.current_log_policy()
        return bool(
            policy
            and policy.allows_verbose(category, critical=critical, message=message)
        )

    def log(
        self,
        msg,
        category=LOG_STEP,
        *,
        critical=False,
        private_msg=None,
        extra=None,
    ):
        """在日志策略允许时输出一条运行日志。

        Args:
            msg: 面向用户的日志正文（受隐私保护）。
            category: 日志分类。
            critical: 是否为关键日志。
            private_msg: 仅在用户关闭日志隐私保护时输出的详细版本。
            extra: 可选的结构化字段字典（如 step_index / action_id /
                duration_ms）。仅写入可选的结构化 JSONL 输出，供诊断包和
                外部工具分析；不影响纯文本日志、界面日志面板或策略过滤。

        Returns:
            成功输出返回 True；被策略过滤返回 False。
        """
        policy = self.current_log_policy()
        if not policy or not policy.allows(category, critical=critical, message=msg):
            return False
        started_ns = time.perf_counter_ns()
        try:
            write_log(
                msg,
                self.callback_msg,
                private_message=private_msg,
                ui_timestamp=policy.timestamp_enabled,
                context=dict(getattr(self, "run_log_context", {}) or {}),
                structured=extra,
            )
        finally:
            self.performance.observe_since("log.dispatch", started_ns)
        return True

    def log_structured_event(self, event, *, category=LOG_STEP, **fields):
        """记录一条仅供机器分析的结构化事件（不进入界面日志面板）。

        与 ``log`` 不同，结构化事件不经过日志策略过滤、不触发界面回调，
        也不写入纯文本日志；它只携带稳定事件名和调用方提供的非敏感字段
        （如 step_index / action_id / duration_ms），写入可选的结构化
        JSONL 输出，供诊断包导出和外部工具关联分析。

        DESIGN: 事件正文固定为简短事件名，不包含工作流文本、变量值、窗口
        条件或路径，因此不受日志隐私保护开关影响；调用方不得把敏感值放进
        ``fields``。

        Args:
            event: 稳定的事件名（如 "step_start" / "action_exec" / "step_complete"）。
            category: 日志分类，仅作为结构化输出中的 category 标签。
            **fields: 附加的结构化字段。

        Returns:
            输出成功返回 True；日志被整体关闭（log_level < 0）时返回 False。
        """
        if self.log_level < 0:
            return False
        started_ns = time.perf_counter_ns()
        structured = {"event": str(event), "log_category": str(category)}
        for key, value in fields.items():
            structured[str(key)] = value
        try:
            context = dict(getattr(self, "run_log_context", {}) or {})
            context["log_category"] = str(category)
            write_log(
                str(event),
                None,
                ui_timestamp=False,
                context=context,
                structured=structured,
            )
        finally:
            self.performance.observe_since("log.dispatch", started_ns)
        return True

    def complete_runtime_parameters(self, task_count, *, privacy_protected=True):
        """汇总当前运行的完整参数快照。

        用于“完全”级别日志输出；privacy_protected 决定隐私相关字段
        是否以脱敏形式呈现。

        Args:
            task_count: 本次运行的步骤总数。
            privacy_protected: 是否启用隐私保护（默认 True）。

        Returns:
            按识别、原生核心、动作、流程、保护开关和隐私分组的参数字典。
        """
        policy = self.current_log_policy()
        return {
            "task_count": int(task_count),
            "logging": {
                "mode": policy.mode if policy else "disabled",
                "enabled_categories": (
                    list(policy.enabled_categories) if policy else []
                ),
                "ui_timestamp": bool(policy and policy.timestamp_enabled),
            },
            "recognition": {
                "confidence": self.confidence,
                "scale_min": self.min_scale,
                "scale_max": self.max_scale,
                "scale_step": self.scale_step,
                "grayscale": self.enable_grayscale,
                "detect_delay_seconds": self.detect_delay,
                "adaptive_backoff": self.adaptive_backoff,
                "scene_wake": self.scene_wake_enabled,
                "scene_wake_sensitivity": self.scene_wake_sensitivity,
                "scan_region": self.scan_region,
                "scan_regions": list(self.scan_regions),
            },
            "native": {
                "enabled": self.use_native_core,
                "parallel_mode": self.native_parallel_mode,
                "scale_memory_enabled": self.use_native_scale_hint,
                "scale_memory_tier": self.scale_memory_tier,
                "manual_preferred_scales": list(self.scale_memory_manual),
                "scale_memory_custom": self.scale_memory_custom_enabled,
                "preferred_limit": self.scale_memory_preferred_limit,
                "history_limit": self.scale_memory_history_limit,
                "fast_screenshot": self.use_fast_screenshot,
            },
            "actions": {
                "move_seconds": self.move_duration,
                "click_hold_seconds": self.click_hold,
                "mouse_trail": self.show_mouse_trail,
                "step_interval_seconds": self.settlement_wait,
                "timeout_seconds": self.timeout_val,
                "timeout_emergency_stop": self.timeout_stop,
                "dodge_enabled": self.enable_dodge,
                "double_dodge": self.enable_double_dodge,
                "dodge_points": [
                    [self.dodge_x1, self.dodge_y1],
                    [self.dodge_x2, self.dodge_y2],
                ],
                "dodge_click_action": self.dodge_click_action,
                "playback_speed": self.playback_speed,
            },
            "flow": {
                "start_step": self.start_step_index + 1,
                "loop_start": self.loop_start_round,
                "loop_end": self.loop_end_round,
                "loop_mode": self.loop_mode,
                "loop_value": self.loop_val,
                "multi_target_mode": self.multi_target_mode,
                "multi_target_order": self.multi_target_order,
                "multi_target_order_b": self.multi_target_order_b,
                "multi_collect_first_hit_s": self.multi_collect_first_hit_s,
                "multi_collect_last_hit_s": self.multi_collect_last_hit_s,
                "multi_collect_max_s": self.multi_collect_max_s,
                "multi_collect_max_targets": self.multi_collect_max_targets,
                "multi_collect_allow_partial": self.multi_collect_allow_partial,
                "input_backend_mode": self.input_backend_mode,
                "keyboard_scan_code": self.keyboard_scan_code,
                "mouse_move_settle_s": self.mouse_move_settle_s,
            },
            "failsafes": {
                "task_manager": self.enable_tm_stop,
                "top_right": self.enable_tr_stop,
                "escape_or_middle_button": self.enable_key_stop,
            },
            "privacy": {
                "log_privacy_protection": bool(privacy_protected),
                "step_parameter_payload_omits_raw_task_values": bool(privacy_protected),
                "external_clipboard_contents_logged": False,
                "ordinary_text_input_logged": not privacy_protected,
                "credential_values_logged": False,
                "secret_text_action_contents_logged": False,
                "window_conditions_logged": not privacy_protected,
                "full_paths_logged": not privacy_protected,
                "diagnostics_and_crash_reports_always_protected": True,
            },
        }

    def _log_complete_step_parameters(
        self,
        *,
        task,
        task_count,
        index,
        loop_count,
        attempt,
        success_count,
        policy,
        needs_recognition_wait,
        search_regions,
        extra_delay,
        wake_context,
        point_limit_enabled,
        coord_step_config,
        coord_sequence_config,
        image_click_config,
    ):
        if not self.verbose_log_enabled(LOG_PARAMETERS):
            return
        coordinate = self.parse_coordinate(policy.value)
        is_image_target = policy.command in IMAGE_TARGET_COMMANDS and coordinate is None
        target_summary = {
            "kind": (
                "coordinate"
                if coordinate is not None
                else "image"
                if is_image_target
                else "redacted_value"
            ),
            "coordinate": list(coordinate) if coordinate else None,
            "image_name_length": (
                len(os.path.basename(str(policy.value))) if is_image_target else 0
            ),
            "raw_value_length": len(str(policy.value or "")),
        }
        scale_options = (
            list(self.scale_options_for(policy.cache_key)) if is_image_target else None
        )
        step_parameter_payload = {
            "context": {
                "loop": loop_count,
                "step": index + 1,
                "command": self.get_cmd_name(policy.command),
                "attempt": attempt,
                "success_count": success_count,
                "total_steps": task_count,
            },
            "target": target_summary,
            "recognition": {
                "required": needs_recognition_wait,
                "confidence": policy.confidence,
                "grayscale": policy.use_grayscale,
                "scale_options": scale_options,
                "search_regions": search_regions or [],
                "adaptive_extra_delay_seconds": extra_delay,
                "scene_wake_eligible": bool(wake_context),
                "multi_target_mode": self.multi_target_mode,
                "multi_target_order": self.multi_target_order,
                "multi_target_order_b": self.multi_target_order_b,
                "multi_collect_first_hit_s": self.multi_collect_first_hit_s,
                "multi_collect_last_hit_s": self.multi_collect_last_hit_s,
                "multi_collect_max_s": self.multi_collect_max_s,
                "multi_collect_max_targets": self.multi_collect_max_targets,
                "multi_collect_allow_partial": self.multi_collect_allow_partial,
                "point_limit_enabled": point_limit_enabled,
                "point_limit_count": policy.point_limit_count,
                "image_click_point": image_click_config,
            },
            "execution": {
                "retry": policy.retry,
                "no_skip_wait": policy.no_skip_wait,
                "repeat_mode": policy.repeat_mode,
                "repeat_target": policy.target_successes,
                "failure_limit": policy.fail_limit,
                "run_execution_limit": policy.run_max_executions,
                "coordinate_step_enabled": bool(coord_step_config),
                "coordinate_sequence_enabled": bool(coord_sequence_config),
                "timeout_seconds": self.timeout_val,
                "timeout_emergency_stop": self.timeout_stop,
            },
            "branches": {
                "success_skip": policy.success_skip,
                "success_jump": policy.success_jump,
                "failure_skip": policy.fail_skip,
                "failure_jump": policy.fail_jump,
            },
            "privacy": (
                "raw task values, private text, window titles, credential values, "
                "and full paths are omitted from every log level"
            ),
        }
        private_target_summary = dict(target_summary)
        private_target_summary["raw_value"] = policy.value
        private_step_parameter_payload = {
            **step_parameter_payload,
            "target": private_target_summary,
            "raw_task": task,
            "privacy": (
                "log privacy protection disabled: ordinary workflow values and paths "
                "are included; credential secret values remain prohibited"
            ),
        }
        self.log(
            format_complete_payload(
                "完全/步骤参数",
                step_parameter_payload,
            ),
            LOG_PARAMETERS,
            private_msg=format_complete_payload(
                "完全/步骤参数",
                private_step_parameter_payload,
            ),
        )
