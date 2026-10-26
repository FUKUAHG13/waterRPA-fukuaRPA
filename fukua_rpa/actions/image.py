# pyright: reportAttributeAccessIssue=false
"""Screenshot / multi-image / UI Automation actions.

Mechanical extraction from the former monolithic ``engine_actions.py``.
"""

import logging
import os
import time
from dataclasses import replace

from fukua_rpa.constants import (
    TASK_TYPE_MULTI_IMAGE_ACTION,
    TASK_TYPE_UIA_CLICK,
    TASK_TYPE_UIA_READ_VALUE,
    TASK_TYPE_UIA_SET_VALUE,
)
from fukua_rpa.expressions import ExpressionError
from fukua_rpa.log_policy import (
    LOG_ACTION,
    LOG_CRITICAL,
    LOG_RECOGNITION,
)
from fukua_rpa.multi_image_actions import (
    MULTI_IMAGE_ACTION_BY_CODE,
    MULTI_IMAGE_POINT_ACTIONS,
    normalize_multi_image_rules,
)
from fukua_rpa.privacy import (
    private_debug_value,
    safe_error_name,
    sensitive_text_metadata,
)
from fukua_rpa.task_model import config_bool, parse_region_text

from .shared import (
    ACTION_HANDLERS,
    action_handler,
)

logger = logging.getLogger(__name__)
class ActionsImageMixin:
    """Action primitives for the image family."""

    @action_handler(TASK_TYPE_MULTI_IMAGE_ACTION)
    def _execute_multi_image_action(self, context):
        """Handle the multi-image command: run the first matching rule's action.

        Args:
            context: StepActionContext for the multi-image step.

        Returns:
            Outcome string: "success", "not_found", "stopped", or "error".
        """
        rules = normalize_multi_image_rules(
            (context.task or {}).get("multi_image_actions", [])
        )
        allow_inverted = config_bool(
            (context.task or {}).get("inverse_match_en", False)
        )
        for rule_index, rule in enumerate(rules, 1):
            if self.check_stop_flag():
                return "stopped"
            path = str(rule.get("image", "") or "").strip()
            if not path:
                continue
            try:
                confidence = float(rule.get("confidence") or context.confidence)
            except (TypeError, ValueError) as exc:
                logger.debug("多图动作置信度解析失败: %s", exc, exc_info=True)
                confidence = float(context.confidence)
            region = parse_region_text(rule.get("region", ""))
            search_regions = [region] if region else context.search_regions
            # FR-SCOPE012-001: 规则里的 cache_key **不被信任**。它可能来自旧版本
            # 或手工编辑，与当前任务的倍率设置不符；原样转发会让多倍率搜索静默
            # 退化为单倍率。这里始终按当前设置重建，并登记倍率表。
            s_min, s_max, s_step = (
                self.min_scale,
                self.max_scale,
                self.scale_step,
            )
            if config_bool((context.task or {}).get("custom_en", False)):
                s_min = float((context.task or {}).get("custom_scale_min", s_min))
                s_max = float((context.task or {}).get("custom_scale_max", s_max))
                s_step = float((context.task or {}).get("custom_scale_step", s_step))
            # FR-SCOPE013-006: 与预计算共用同一构造函数。
            cache_key = self.multi_image_rule_cache_key(
                path, s_min, s_max, s_step, context.use_gray
            )
            self.scale_options_cache[str(cache_key)] = (s_min, s_max, s_step)
            found = self.find_target_optimized(
                path,
                cache_key,
                confidence,
                context.use_gray,
                search_regions,
                allow_inverted,
            )
            if not found:
                # FR-20261008-401: a cancelled search returns an empty result;
                # that must not be treated as a rule miss (which would keep
                # iterating and finally report not_found).
                if self.check_stop_flag():
                    return "stopped"
                continue
            action_code = str(rule.get("action", "left_click") or "left_click")
            if action_code not in MULTI_IMAGE_ACTION_BY_CODE:
                return "error"
            if self.log_level >= 1:
                self.log(
                    f"    -> 多图动作第 {rule_index} 条命中，执行“"
                    f"{MULTI_IMAGE_ACTION_BY_CODE[action_code].label}”",
                    LOG_RECOGNITION,
                )
            x, y = float(found[0]), float(found[1])
            if action_code in MULTI_IMAGE_POINT_ACTIONS:
                if action_code == "hover":
                    with self._input_action_lock:
                        if not self._move_mouse_interruptibly(
                            x,
                            y,
                            max(0.0, self.move_duration + self._jitter_delay(self.move_duration_jitter)),
                        ):
                            return "stopped"
                    self.report_click_indicator(x, y, "多图动作悬停")
                    return "success"
                button = "right" if action_code == "right_click" else "left"
                click_times = 2 if action_code == "left_double_click" else 1
                clicked = self.perform_mouse_click(
                    x,
                    y,
                    click_times,
                    button,
                    MULTI_IMAGE_ACTION_BY_CODE[action_code].label,
                )
                return (
                    "success"
                    if clicked
                    else ("stopped" if self.check_stop_flag() else "error")
                )

            child_commands = {
                "text": 4.0,
                "wait": 5.0,
                "scroll": 6.0,
                "hotkey": 7.0,
                "prompt": 12.0,
                "stop": 13.0,
                "sound": 14.0,
            }
            child_command = child_commands[action_code]
            child_context = replace(
                context,
                command=child_command,
                value=rule.get("value", ""),
                task={},
                search_regions=None,
            )
            return ACTION_HANDLERS[child_command](self, child_context)
        # FR-20261008-401: distinguish a user stop from a real miss.
        if self.check_stop_flag():
            return "stopped"
        if self.log_level >= 1:
            self.log(
                "<font color='orange'>    [多图动作] 本次没有规则图片匹配成功。</font>",
                LOG_RECOGNITION,
            )
        return "not_found"

    def _execute_uia_action(self, context, operation, value=""):
        """Perform a UI Automation control operation via the window backend.

        Args:
            context: StepActionContext carrying the uia_binding.
            operation: UIA operation name (e.g. "activate", "set_value").
            value: Value argument for set operations.

        Returns:
            Result dict from the backend, marked cancelled when stopped.
        """
        binding = (context.task or {}).get("uia_binding")
        result = self.task_window_backend().uia_control_action(
            binding,
            operation,
            value,
            stop_requested=self.check_stop_flag,
        )
        if result.get("cancelled") or self.check_stop_flag():
            return {**result, "cancelled": True}
        if result.get("success"):
            if self.log_level >= 1:
                method = result.get("method") or "UI Automation"
                self.log(f"    -> 控件操作成功（{method}）", LOG_ACTION)
            return result
        if self.log_level >= 1:
            error = result.get("error") or "目标控件不支持此操作"
            self.log(
                f"<font color='orange'>    [控件操作失败] 详情已脱敏（{sensitive_text_metadata(error)}）</font>",
                LOG_ACTION,
                private_msg=(
                    "<font color='orange'>    [控件操作失败] "
                    f"{private_debug_value(error)}</font>"
                ),
            )
        return result

    def _uia_result_status(self, result, *, unknown_message):
        """Classify a UIA action result dict into a status string.

        SAFETY: an outcome-unknown side effect stops the whole run. It must not
        enter ordinary failure branching, which could execute or retry another
        side effect. Returns "stopped" (cancelled or outcome-unknown after stop()),
        "outcome_unknown", "success", or "not_found".
        """
        if isinstance(result, dict) and result.get("cancelled"):
            return "stopped"
        if isinstance(result, dict) and result.get("outcome_unknown"):
            self.log(
                f"<font color='red'><b>    {unknown_message}</b></font>",
                LOG_CRITICAL,
                critical=True,
            )
            self.stop()
            return "stopped"
        if isinstance(result, dict) and result.get("success"):
            return "success"
        return "not_found"

    @action_handler(TASK_TYPE_UIA_CLICK)
    def _execute_uia_click(self, context):
        """Handle the UIA-click command: activate a bound control.

        Args:
            context: StepActionContext for the UIA click step.

        Returns:
            Outcome string from _uia_result_status.
        """
        result = self._execute_uia_action(context, "activate")
        return self._uia_result_status(
            result, unknown_message="[控件结果未知] 为避免重复点击，脚本已停止。"
        )

    @action_handler(TASK_TYPE_UIA_SET_VALUE)
    def _execute_uia_set_value(self, context):
        """Handle the UIA-set-value command: write a value to a bound control.

        Args:
            context: StepActionContext whose value is written to the control.

        Returns:
            Outcome string from _uia_result_status.
        """
        result = self._execute_uia_action(context, "set_value", context.value)
        return self._uia_result_status(
            result, unknown_message="[控件结果未知] 为避免重复写入，脚本已停止。"
        )

    @action_handler(TASK_TYPE_UIA_READ_VALUE)
    def _execute_uia_read_value(self, context):
        """Handle the UIA-read-value command: read a control into a variable.

        Args:
            context: StepActionContext whose value is the target variable name.

        Returns:
            Outcome string: "success", "not_found", "stopped", or "error".
        """
        result = self._execute_uia_action(context, "read_value")
        status = self._uia_result_status(
            result, unknown_message="[控件结果未知] 为避免重复读取，脚本已停止。"
        )
        if status != "success":
            return status
        try:
            name, _value = self.store_runtime_variable(
                context.value, str(result.get("value", ""))[:4096]
            )
        except ExpressionError as error:
            self.log(
                f"<font color='red'>    [变量错误] {safe_error_name(error)}</font>",
                LOG_CRITICAL,
                critical=True,
            )
            return "error"
        if self.log_level >= 1:
            self.log(
                f"    -> 控件文本已保存到变量 {name}",
                LOG_ACTION,
                private_msg=(
                    f"    -> 控件文本已保存到变量 {name}，值为 "
                    f"{private_debug_value(result.get('value', ''))}"
                ),
            )
        return "success"

    @action_handler(9.0)
    def _execute_screenshot(self, context):
        """Handle command 9.0: capture a screenshot and save it to disk.

        Args:
            context: StepActionContext whose value is the save path or directory.

        Returns:
            Always "success".

        Raises:
            ValueError: If the path is empty, the region is invalid, or mode is bad.
            FileNotFoundError: If the target directory does not exist.
        """
        path = str(context.value or "").strip()
        if not path:
            raise ValueError("截图保存路径不能为空")
        if os.path.isdir(path):
            milliseconds = int(time.time_ns() // 1_000_000) % 1000
            path = os.path.join(
                path, f"ss_{time.strftime('%Y%m%d_%H%M%S')}_{milliseconds:03d}.png"
            )
        parent_dir = os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(parent_dir):
            raise FileNotFoundError(f"截图保存目录不存在：{parent_dir}")
        capture_mode = str(
            (context.task or {}).get("capture_region_mode", "global")
        )
        if capture_mode == "full":
            region = None
        elif capture_mode == "custom":
            region = parse_region_text(
                (context.task or {}).get("capture_region", "")
            )
            if region is None:
                raise ValueError("本步骤的自定义截图区域无效")
        elif capture_mode == "global":
            regions = self.normalized_regions(getattr(self, "scan_regions", []))
            region = self.region_bounding_rect(regions) if regions else self.scan_region
        else:
            raise ValueError("截图范围模式无效")
        screenshot, _offset_x, _offset_y = self.capture_screenshot(region)
        screenshot.save(path)
        if self.log_level >= 1:
            self.log(
                "    -> 截图已保存到用户指定位置",
                LOG_ACTION,
                private_msg=f"    -> 截图已保存到：{private_debug_value(path)}",
            )
        return "success"
