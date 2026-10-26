"""UI-independent validation for executable task lists."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from typing import Any

from .commands import (
    CLICK_COMMANDS,
    COMMAND_BY_CODE,
    IMAGE_TARGET_COMMANDS,
    effective_task_settings,
)
from .constants import (
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    MAX_MULTI_IMAGE_ACTION_RULES,
    MAX_SINGLE_TEMPLATE_CACHE_BYTES,
    MAX_TEMPLATE_CACHE_BYTES,
    MAX_TIMELINE_EVENT_SECONDS,
    MAX_TIMELINE_EVENTS,
    SCALE_MAX,
    SCALE_MIN,
    TASK_TYPE_ACTIVATE_WINDOW,
    TASK_TYPE_AUTO_CLICKER,
    TASK_TYPE_CLOSE_WINDOW,
    TASK_TYPE_EXPRESSION,
    TASK_TYPE_INPUT_TIMELINE,
    TASK_TYPE_LAUNCH_APP,
    TASK_TYPE_LEFT_DRAG,
    TASK_TYPE_MULTI_IMAGE_ACTION,
    TASK_TYPE_PLUGIN_ACTION,
    TASK_TYPE_POPUP_NOTIFY,
    TASK_TYPE_RIGHT_DRAG,
    TASK_TYPE_SCREENSHOT_SAVE,
    TASK_TYPE_SCROLL,
    TASK_TYPE_SECRET_TEXT,
    TASK_TYPE_SET_VARIABLE,
    TASK_TYPE_SOUND_ALERT,
    TASK_TYPE_STOP_RUN,
    TASK_TYPE_UIA_CLICK,
    TASK_TYPE_UIA_READ_VALUE,
    TASK_TYPE_UIA_SET_VALUE,
    TASK_TYPE_UNTIL,
    TASK_TYPE_WAIT,
    TASK_TYPE_WAIT_WINDOW,
    UNTIL_CONDITION_LOGICS,
    UNTIL_CONDITION_MODES,
    UNTIL_LIMIT_ACTIONS,
    UNTIL_OVERLAY_SCALE_DEFAULT,
    UNTIL_OVERLAY_SCALE_MAX,
    UNTIL_OVERLAY_SCALE_MIN,
    UNTIL_OVERLAY_THRESHOLD_DEFAULT,
)
from .expressions import (
    ExpressionError,
    compile_expression,
    parse_assignment,
    validate_variable_name,
)
from .multi_image_actions import (
    MULTI_IMAGE_ACTION_BY_CODE,
    MULTI_IMAGE_REQUIRED_VALUE_ACTIONS,
    normalize_multi_image_rules,
)
from .plugin_actions import validate_plugin_action_fields
from .run_config import EngineRunConfig, RunConfigError
from .task_model import (
    config_bool,
    normalize_coord_step_direction,
    parse_coordinate_sequence,
    parse_coordinate_text,
    parse_region_text,
    task_is_disabled,
    until_condition_list_from_data,
)
from .vision.core import (  # pyright: ignore[reportUnknownVariableType]
    build_scale_values,
    estimate_template_cache_bytes,
    template_detail_status,
)

# 坐标上界与 ``actions/shared.py`` 的 ``ActionsSharedMixin._MAX_ABS_COORDINATE``
# 保持一致：预运行校验与运行期解析必须用同一口径，否则放行的值会在运行期被
# 判为越界并静默降级（FR-20261012-001）。
_MAX_ABS_COORDINATE = 1_000_000


def _is_ascii_digits(value: str) -> bool:
    """Return True only for non-empty ASCII digit strings.

    ``str.isdigit()`` also accepts non-ASCII decimal digits (e.g. Arabic-Indic
    digits) that ``int()`` would silently parse; integer step fields must be
    ASCII digits only.
    """

    return value.isascii() and value.isdigit()


def _region_outside(outer_text: str, inner_text: str) -> bool:
    """Return True when *inner_text* is a well-formed region outside *outer_text*.

    Shared containment predicate for every "子区域必须落在大区域内" check
    (``until_cond*_sub_region`` and ``until_cond*_overlay_rect``).
    FR-20261006-005 / ADV-20261006-001: the two checks previously drifted —
    ``overlay_rect`` validated containment while ``sub_region`` only checked
    syntax, so an out-of-bounds sub-region silently evaluated as永远 False at
    run time (a forbidden silent degradation per docs/设计基准.md).

    An empty *inner_text* means "not set" and is never outside. An unparsable or
    unset *outer_text* yields ``False`` here so the caller can still report the
    more specific missing/format error first.
    """

    inner_text = str(inner_text).strip()
    if not inner_text:
        return False
    outer = parse_region_text(str(outer_text).strip())
    inner = parse_region_text(inner_text)
    if outer is None or inner is None:
        return False
    ix, iy, iw, ih = inner
    _ox, _oy, outer_w, outer_h = outer
    return ix < 0 or iy < 0 or ix + iw > outer_w or iy + ih > outer_h


_TIMELINE_MAX_COORDINATE = 100_000


def _valid_point(item: Mapping[str, Any]) -> bool:
    """Return True when x/y are integers inside the supported coordinate range."""
    try:
        x = int(item.get("x"))
        y = int(item.get("y"))
    except (TypeError, ValueError):
        return False
    return abs(x) <= _TIMELINE_MAX_COORDINATE and abs(y) <= _TIMELINE_MAX_COORDINATE


class TaskListValidator:
    """UI-independent validator for executable task lists.

    Validates every enabled step against the run config, checking field ranges,
    image paths, expression syntax, and aggregate template cache budget.
    Instantiate with the raw config mapping, then call ``validate`` or
    ``validate_all``.
    """

    def __init__(self, config: Mapping[str, Any]):
        """Initialize with the raw run-config mapping used for global bounds."""
        self.config = config
        self.estimated_cache_total = 0
        self.estimated_cache_keys: set[tuple[str, bool, tuple[float, ...]]] = set()

    def _add_template_budget(
        self, path: str, use_gray: bool, scale_values: Sequence[float], step_no: int
    ) -> str | None:
        key = (os.path.abspath(path), bool(use_gray), tuple(scale_values))
        if key in self.estimated_cache_keys:
            return None
        self.estimated_cache_keys.add(key)
        try:
            estimated = estimate_template_cache_bytes(path, use_gray, scale_values)
        except ValueError as error:
            return f"第 {step_no} 步模板无法读取：{error}"
        if estimated > MAX_SINGLE_TEMPLATE_CACHE_BYTES:
            return (
                f"第 {step_no} 步图片预计需要 {estimated / 1024 / 1024:.1f} MB 模板缓存，"
                f"超过单图上限 {MAX_SINGLE_TEMPLATE_CACHE_BYTES / 1024 / 1024:.0f} MB。\n"
                "请缩小模板图片或缩放范围。"
            )
        self.estimated_cache_total += estimated
        if self.estimated_cache_total > MAX_TEMPLATE_CACHE_BYTES:
            return (
                f"全部图片预计需要超过 {MAX_TEMPLATE_CACHE_BYTES / 1024 / 1024:.0f} MB 模板缓存。\n"
                "请减少图片数量、缩放范围或缩放档位。"
            )
        return None

    def validate(self, tasks: Sequence[Mapping[str, Any]]) -> str | None:
        """Return the first validation error, or None if all steps are valid."""
        errors = self.validate_all(tasks)
        return errors[0] if errors else None

    def validate_all(self, tasks: Sequence[Mapping[str, Any]]) -> list[str]:
        """Collect the first actionable error from every invalid step."""

        self.estimated_cache_total = 0
        self.estimated_cache_keys.clear()
        if not tasks:
            return ["脚本中没有可执行步骤。"]
        errors: list[str] = []
        for index, task in enumerate(tasks):
            if not isinstance(task, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
                errors.append(f"第 {index + 1} 步不是有效的步骤对象。")
        valid_tasks = [task for task in tasks if isinstance(task, Mapping)]  # pyright: ignore[reportUnnecessaryIsInstance]
        # FR-301-001: 预运行校验同时核对权威 *_target_id 引用（#45）。先收集
        # 全部 step_id，供 _validate_jump_fields 判断悬空引用。
        valid_step_ids = {
            str(task.get("step_id"))
            for task in valid_tasks
            if str(task.get("step_id") or "").strip()
        }
        if not any(not task_is_disabled(task) for task in valid_tasks):
            errors.append("脚本中没有已启用的步骤。")
        try:
            run_config = EngineRunConfig.from_mapping(self.config, len(tasks))
        except RunConfigError as error:
            errors.append(str(error))
            return errors

        global_scale_values: list[float] = build_scale_values(  # pyright: ignore[reportUnknownVariableType]
            run_config.min_scale, run_config.max_scale, run_config.scale_step
        )
        global_use_gray = run_config.enable_grayscale

        for index, task in enumerate(tasks):
            step_no = index + 1
            if not isinstance(task, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
                continue
            if task_is_disabled(task):
                continue
            error = self._validate_task(
                task,
                step_no,
                len(tasks),
                global_scale_values,
                global_use_gray,
                run_config.confidence,
                valid_step_ids,
            )
            if error:
                errors.append(error)
        return errors

    def _validate_task(
        self,
        task: Mapping[str, Any],
        step_no: int,
        task_count: int,
        global_scale_values: Sequence[float],
        global_use_gray: bool,
        global_confidence: float,
        valid_step_ids: set[str],
    ) -> str | None:
        # DESIGN: validate the same effective copy the engine receives. Stale
        # drafts for another action must neither block this run nor be erased.
        task = effective_task_settings(task)
        raw_task_type = task.get("type")
        if raw_task_type is None:
            return f"第 {step_no} 步的指令类型无效：{raw_task_type}"
        try:
            task_type = float(raw_task_type)
        except (TypeError, ValueError):
            return f"第 {step_no} 步的指令类型无效：{raw_task_type}"
        if task_type not in COMMAND_BY_CODE:
            return f"第 {step_no} 步使用了未注册的指令类型：{raw_task_type}"

        value = str(task.get("value", "")).strip()
        coordinate = parse_coordinate_text(value)

        error = self._validate_common_fields(
            task, step_no, task_count, task_type, coordinate, valid_step_ids
        )
        if error:
            return error

        if task_type in IMAGE_TARGET_COMMANDS:
            return self._validate_click_task(
                task, step_no, task_type, value, coordinate,
                global_scale_values, global_use_gray, global_confidence,
            )
        if task_type in (TASK_TYPE_UNTIL, TASK_TYPE_WAIT_WINDOW) or task_type in (TASK_TYPE_WAIT, TASK_TYPE_SCROLL):
            return self._validate_wait_task(
                task, step_no, task_type, value, task_count,
                global_scale_values, global_use_gray,
            )
        if task_type in (
            TASK_TYPE_SET_VARIABLE,
            TASK_TYPE_EXPRESSION,
            TASK_TYPE_UIA_CLICK,
            TASK_TYPE_UIA_SET_VALUE,
            TASK_TYPE_UIA_READ_VALUE,
            TASK_TYPE_MULTI_IMAGE_ACTION,
        ):
            return self._validate_variable_task(
                task, step_no, task_type, value,
                global_scale_values, global_use_gray, global_confidence,
            )
        if task_type == TASK_TYPE_INPUT_TIMELINE:
            return self._validate_timeline_task(task, step_no)
        if task_type == TASK_TYPE_PLUGIN_ACTION:
            error = validate_plugin_action_fields(task)
            if error:
                return f"第 {step_no} 步插件动作校验失败：{error}"
            return None
        return self._validate_flow_task(task, step_no, task_type, value)

    def _validate_common_fields(
        self,
        task: Mapping[str, Any],
        step_no: int,
        task_count: int,
        task_type: float,
        coordinate: tuple[int, int] | None,
        valid_step_ids: set[str],
    ) -> str | None:
        """Validate common gear fields shared by all task types."""
        value = str(task.get("value", "")).strip()
        image_click_point_en = config_bool(task.get("image_click_point_en", False))
        image_click_point_rx = str(task.get("image_click_point_rx", "0.5")).strip()
        image_click_point_ry = str(task.get("image_click_point_ry", "0.5")).strip()
        background_click_en = config_bool(
            task.get("background_click_en", False)
        )
        background_click_binding = task.get("background_click_binding", {})
        debug_breakpoint = config_bool(task.get("debug_breakpoint", False))
        debug_condition = str(task.get("debug_condition", "") or "").strip()
        image_confirm_click_en = config_bool(
            task.get("image_confirm_click_en", False)
        )
        delayed_second_click_en = config_bool(
            task.get("delayed_second_click_en", False)
        )

        if debug_breakpoint and debug_condition:
            try:
                compile_expression(debug_condition)
            except ExpressionError as error:
                return f"第 {step_no} 步小齿轮里的'断点条件'无效：{error}"

        error = self._validate_jump_fields(task, step_no, task_count, valid_step_ids)
        if error:
            return error

        for enabled, key, label in (
            (
                image_confirm_click_en,
                "image_confirm_click_delay",
                "识别确认补点延时",
            ),
            (
                delayed_second_click_en,
                "delayed_second_click_delay",
                "延时二次点击延时",
            ),
        ):
            if not enabled:
                continue
            try:
                delay = float(task.get(key, "0.5"))
                if not math.isfinite(delay) or not 0.0 <= delay <= 300.0:
                    raise ValueError
            except (TypeError, ValueError):
                return f"第 {step_no} 步小齿轮里的\u201c{label}\u201d必须是 0 到 300 秒之间的有限数字！"

        if background_click_en:
            if task_type not in CLICK_COMMANDS or coordinate is None:
                return (
                    f"第 {step_no} 步小齿轮里的'后台点击（仅坐标）'"
                    "只能用于直接坐标的左键、双击或右键点击。"
                )
            if not isinstance(background_click_binding, Mapping) or not background_click_binding:
                return f"第 {step_no} 步已启用后台点击，但尚未选择目标窗口。"
            binding: Mapping[str, Any] = background_click_binding  # pyright: ignore[reportUnknownVariableType]
            sx = binding.get("source_x")
            sy = binding.get("source_y")
            rcx = binding.get("root_client_x")
            rcy = binding.get("root_client_y")
            if sx is None or sy is None or rcx is None or rcy is None:
                return f"第 {step_no} 步后台点击的窗口绑定不完整，请重新选择目标窗口。"
            try:
                source = (int(sx), int(sy))
                int(rcx)
                int(rcy)
            except (TypeError, ValueError):
                return f"第 {step_no} 步后台点击的窗口绑定不完整，请重新选择目标窗口。"
            if source != (int(coordinate[0]), int(coordinate[1])):
                return (
                    f"第 {step_no} 步坐标已在选择窗口后发生变化。"
                    "请打开小齿轮并重新选择后台点击目标窗口。"
                )
        if image_click_point_en:
            if task_type not in CLICK_COMMANDS or coordinate or not os.path.isfile(value):
                return f"第 {step_no} 步小齿轮里的'图片内点击点'仅能用于左键/右键图片点击步骤，且参数必须是存在的图片路径。\n填入内容: {value}"
            try:
                rx = float(image_click_point_rx)
                ry = float(image_click_point_ry)
                if not (0.0 <= rx <= 1.0 and 0.0 <= ry <= 1.0):
                    raise ValueError
            except (TypeError, ValueError):
                return f"第 {step_no} 步小齿轮里的'图片内点击点'相对位置必须是 0 到 1 之间的数字！\n填入内容: X={image_click_point_rx}, Y={image_click_point_ry}"

        error = self._validate_region_fields(task, step_no, task_type, coordinate)
        if error:
            return error

        error = self._validate_coord_step_fields(task, step_no, task_type, coordinate)
        if error:
            return error

        return None

    def _validate_jump_fields(
        self,
        task: Mapping[str, Any],
        step_no: int,
        task_count: int,
        valid_step_ids: set[str],
    ) -> str | None:
        """Validate jump, loop, and repetition fields."""
        success_skip = str(task.get("success_skip", "0")).strip()
        success_jump = str(task.get("success_jump", "0")).strip()
        fail_skip = str(task.get("fail_skip", "0")).strip()
        fail_jump = str(task.get("fail_jump", "0")).strip()
        call_step = str(task.get("call_step", "0")).strip()
        repeat_mode = str(task.get("repeat_mode", "执行一次"))
        repeat_count = str(task.get("repeat_count", "1")).strip()
        step_loop_start = str(task.get("step_loop_start", "1")).strip()
        step_loop_end = str(task.get("step_loop_end", "0")).strip()
        fail_limit = str(task.get("fail_limit", "1")).strip()
        point_limit_en = config_bool(task.get("point_limit_en", False))
        point_limit_count = str(task.get("point_limit_count", "0")).strip()
        run_max_executions = str(task.get("run_max_executions", "0")).strip()

        if success_skip and not _is_ascii_digits(success_skip):
            return f"第 {step_no} 步小齿轮里的'成功后跳过'必须是整数！\n填入内容: {success_skip}"
        if fail_skip and not _is_ascii_digits(fail_skip):
            return f"第 {step_no} 步小齿轮里的'失败后跳过'必须是整数！\n填入内容: {fail_skip}"
        if success_jump and (not _is_ascii_digits(success_jump) or int(success_jump) > task_count):
            return f"第 {step_no} 步小齿轮里的'成功后跳至'必须是 0 到 {task_count} 之间的整数！\n填入内容: {success_jump}"
        if fail_jump and (not _is_ascii_digits(fail_jump) or int(fail_jump) > task_count):
            return f"第 {step_no} 步小齿轮里的'失败后跳至'必须是 0 到 {task_count} 之间的整数！\n填入内容: {fail_jump}"
        if call_step and (
            not _is_ascii_digits(call_step) or int(call_step) > task_count
        ):
            return f"第 {step_no} 步小齿轮里的'先执行指定步骤'必须是 0 到 {task_count} 之间的整数！\n填入内容: {call_step}"
        if call_step and int(call_step) == step_no:
            return f"第 {step_no} 步不能把自己设置为'先执行指定步骤'，请填写其他步号或 0。"
        if fail_limit and (not _is_ascii_digits(fail_limit) or int(fail_limit) < 1):
            return f"第 {step_no} 步小齿轮里的'连续失败次数'必须是大于等于 1 的整数！\n填入内容: {fail_limit}"
        if repeat_mode == "指定次数" and (not _is_ascii_digits(repeat_count) or int(repeat_count) < 1):
            return f"第 {step_no} 步小齿轮里的'重复次数'必须是大于等于 1 的整数！\n填入内容: {repeat_count}"
        if run_max_executions and (
            not _is_ascii_digits(run_max_executions) or int(run_max_executions) < 0
        ):
            return f"第 {step_no} 步小齿轮里的'本次运行最多执行'必须是大于等于 0 的整数！\n填入内容: {run_max_executions}"
        if not _is_ascii_digits(step_loop_start) or int(step_loop_start) < 1:
            return f"第 {step_no} 步小齿轮里的'循环范围起始'必须是大于等于 1 的整数！\n填入内容: {step_loop_start}"
        if not _is_ascii_digits(step_loop_end) or int(step_loop_end) < 0:
            return f"第 {step_no} 步小齿轮里的'循环范围停止'必须是大于等于 0 的整数！\n填入内容: {step_loop_end}"
        if int(step_loop_end) > 0 and int(step_loop_end) < int(step_loop_start):
            return f"第 {step_no} 步小齿轮里的'循环范围停止'不能小于起始循环！\n起始循环: {step_loop_start}，停止循环: {step_loop_end}"
        if point_limit_en and point_limit_count and not _is_ascii_digits(point_limit_count):
            return f"第 {step_no} 步小齿轮里的'同点点击上限'必须是大于等于 0 的整数！\n填入内容: {point_limit_count}"

        # FR-301-001: 权威跳转身份由 step_id 与 *_target_id 保存（#45）。预运行
        # 校验必须核对 *_target_id 存在性，否则悬空引用在运行期会被静默清除跳转
        # （jump='0'，顺序下落），用户预期跳转失效却无任何告警。legacy 数据无
        # target_id 字段（空值）不受影响，仍按数字字段校验。
        for target_field, label in (
            ("success_target_id", "成功后跳至"),
            ("fail_target_id", "失败后跳至"),
            ("call_target_id", "先执行指定步骤"),
            ("until_true_target_id", "满足条件跳至"),
            ("until_false_target_id", "未满足跳回"),
        ):
            target_id = str(task.get(target_field, "") or "").strip()
            if target_id and target_id not in valid_step_ids:
                return (
                    f"第 {step_no} 步的“{label}”目标步骤不存在"
                    f"（step_id={target_id} 悬空，运行时会跳过该跳转）。\n"
                    "请重新选择目标步骤或清除该跳转。"
                )

        return None

    def _validate_region_fields(
        self,
        task: Mapping[str, Any],
        step_no: int,
        task_type: float,
        coordinate: tuple[int, int] | None,
    ) -> str | None:
        """Validate per-step recognition region fields."""
        step_region_en = config_bool(task.get("step_region_en", False))
        step_region = str(task.get("step_region", "")).strip()

        if step_region_en:
            if (
                task_type not in IMAGE_TARGET_COMMANDS
                and task_type != TASK_TYPE_MULTI_IMAGE_ACTION
            ) or (task_type != TASK_TYPE_MULTI_IMAGE_ACTION and coordinate):
                return f"第 {step_no} 步小齿轮里的'本步识别区域'仅能用于图片点击/图片悬停步骤。"
            if not parse_region_text(step_region):
                return f"第 {step_no} 步小齿轮里的'本步识别区域'格式错误，应为 x,y,w,h！\n填入内容: {step_region}"

        return None

    def _validate_coord_step_fields(
        self,
        task: Mapping[str, Any],
        step_no: int,
        task_type: float,
        coordinate: tuple[int, int] | None,
    ) -> str | None:
        """Validate coordinate-step and coordinate-sequence fields."""
        coord_step_en = config_bool(task.get("coord_step_en", False))
        coord_step_every = str(task.get("coord_step_every", "1")).strip()
        coord_step_direction = str(task.get("coord_step_direction", "向下")).strip()
        coord_step_distance = str(task.get("coord_step_distance", "0")).strip()
        coord_step_dx = str(task.get("coord_step_dx", "0")).strip()
        coord_step_dy = str(task.get("coord_step_dy", "0")).strip()
        coord_step_point = str(task.get("coord_step_point", "")).strip()
        coord_step_max_steps = str(task.get("coord_step_max_steps", "0")).strip()
        coord_step_max_distance = str(task.get("coord_step_max_distance", "0")).strip()
        coord_step_reset_after = str(task.get("coord_step_reset_after", "0")).strip()
        coord_sequence_en = config_bool(task.get("coord_sequence_en", False))
        coord_sequence_points = str(task.get("coord_sequence_points", "")).strip()
        coord_sequence_end_action = str(
            task.get("coord_sequence_end_action", "点完后跳过本步")
        ).strip()
        coord_sequence_batch_mode = str(
            task.get("coord_sequence_batch_mode", "one")
        ).strip()
        coord_sequence_batch_count = str(
            task.get("coord_sequence_batch_count", "1")
        ).strip()

        if coord_step_en and task_type in CLICK_COMMANDS and coordinate:
            error = self._validate_coordinate_step(
                step_no,
                coord_step_every,
                coord_step_direction,
                coord_step_distance,
                coord_step_dx,
                coord_step_dy,
                coord_step_point,
                coord_step_max_steps,
                coord_step_max_distance,
                coord_step_reset_after,
            )
            if error:
                return error

        if coord_sequence_en:
            if task_type not in CLICK_COMMANDS or not coordinate:
                return f"第 {step_no} 步小齿轮里的'自定义点位'仅能用于直接坐标的左键/右键点击步骤。"
            if coord_sequence_end_action not in (
                "点完后跳过本步",
                "点完后停在最后一个",
                "点完后循环",
            ):
                return f"第 {step_no} 步小齿轮里的'自定义点位结束后'设置无效！\n填入内容: {coord_sequence_end_action}"
            if coord_sequence_batch_mode not in ("one", "count", "all"):
                return (
                    f"第 {step_no} 步小齿轮里的'每次进入本步骤'设置无效！"
                    f"\n填入内容: {coord_sequence_batch_mode}"
                )
            if coord_sequence_batch_mode == "count" and (
                not _is_ascii_digits(coord_sequence_batch_count)
                or int(coord_sequence_batch_count) < 1
            ):
                return (
                    f"第 {step_no} 步小齿轮里的'自定义点位批次数量'"
                    "必须是大于等于 1 的整数！"
                    f"\n填入内容: {coord_sequence_batch_count}"
                )
            if not parse_coordinate_sequence(coord_sequence_points):
                return f"第 {step_no} 步小齿轮里的'自定义点位'至少要包含一个 x,y 坐标！\n填入内容: {coord_sequence_points}"

        return None

    def _validate_click_task(
        self,
        task: Mapping[str, Any],
        step_no: int,
        task_type: float,
        value: str,
        coordinate: tuple[int, int] | None,
        global_scale_values: Sequence[float],
        global_use_gray: bool,
        global_confidence: float,
    ) -> str | None:
        """Validate image-target click/hover tasks."""
        if not value:
            return f"第 {step_no} 步参数不能为空！"
        if not coordinate:
            if not os.path.isfile(value):
                return f"第 {step_no} 步找图错误：图片路径不存在或坐标格式错误 (坐标应为 x,y)\n填入内容: {value}"
            custom_enabled = config_bool(task.get("custom_en", False))
            try:
                task_confidence = (
                    float(task.get("custom_conf", global_confidence))
                    if custom_enabled
                    else global_confidence
                )
                if not math.isfinite(task_confidence) or not CONFIDENCE_MIN <= task_confidence <= CONFIDENCE_MAX:  # constants.CONFIDENCE_MIN/MAX
                    raise ValueError
            except (TypeError, ValueError):
                return f"第 {step_no} 步小齿轮里的独立相似度必须在 {CONFIDENCE_MIN:g} 到 {CONFIDENCE_MAX:g} 之间！"
            try:
                if custom_enabled:
                    scale_min = float(task.get("custom_scale_min", self.config.get("scale_min", "0.8")))
                    scale_max = float(task.get("custom_scale_max", self.config.get("scale_max", "1.2")))
                    scale_step = float(task.get("custom_scale_step", self.config.get("scale_step", "0.05")))
                    if scale_min < SCALE_MIN or scale_max > SCALE_MAX:  # constants.SCALE_MIN/MAX
                        raise ValueError(f"缩放范围必须在 {SCALE_MIN:g} 到 {SCALE_MAX:g} 之间")
                    scale_values: Sequence[float] = build_scale_values(scale_min, scale_max, scale_step)  # pyright: ignore[reportUnknownVariableType]
                    use_gray = config_bool(task.get("custom_gray", global_use_gray))
                else:
                    scale_values = global_scale_values
                    use_gray = global_use_gray
            except (TypeError, ValueError) as error:
                return f"第 {step_no} 步小齿轮里的独立缩放设置无效：{error}"
            valid, reason = template_detail_status(value, use_gray)
            if not valid:
                return f"第 {step_no} 步模板无法安全识别：{reason}"
            return self._add_template_budget(value, use_gray, scale_values, step_no)  # pyright: ignore[reportUnknownArgumentType]
        return None

    def _validate_wait_task(
        self,
        task: Mapping[str, Any],
        step_no: int,
        task_type: float,
        value: str,
        task_count: int,
        global_scale_values: Sequence[float],
        global_use_gray: bool,
    ) -> str | None:
        """Validate wait, until-condition, and wait-window tasks."""
        if task_type == TASK_TYPE_UNTIL:
            return self._validate_until_task(
                task, step_no, task_count, global_scale_values, global_use_gray
            )
        if not value:
            return f"第 {step_no} 步参数不能为空！"
        if task_type == TASK_TYPE_WAIT_WINDOW:
            try:
                window_timeout = float(task.get("window_timeout", 0) or 0)
                if (
                    not math.isfinite(window_timeout)
                    or not 0 <= window_timeout <= 86_400
                ):
                    raise ValueError
            except (TypeError, ValueError):
                return (
                    f"第 {step_no} 步窗口等待时间必须是 0 到 86400 秒之间的有限数字！"
                )
            return None
        if task_type in (TASK_TYPE_WAIT, TASK_TYPE_SCROLL):
            try:
                number_value = float(value)
                if (
                    not math.isfinite(number_value)
                    or (task_type == TASK_TYPE_WAIT and not 0 <= number_value <= 86_400)
                ):
                    raise ValueError
            except (TypeError, ValueError):
                if task_type == TASK_TYPE_WAIT:
                    return (
                        f"第 {step_no} 步等待时间必须是 0 到 86400 秒之间的有限数字！"
                    )
                return f"第 {step_no} 步参数必须是有限数字！"
        return None

    def _validate_timeline_task(
        self,
        task: Mapping[str, Any],
        step_no: int,
    ) -> str | None:
        """Validate a recorded input timeline step.

        The timeline exists to preserve real timing, so structural errors
        must block the run instead of being silently dropped: an unmatched
        press would leave a key held after playback, and a non-monotonic
        timestamp would reorder events.
        """
        events = task.get("input_events")
        if not isinstance(events, (list, tuple)) or not events:
            return f"第 {step_no} 步输入时序为空：录制数据缺失，无法回放。"
        if len(events) > MAX_TIMELINE_EVENTS:
            return (
                f"第 {step_no} 步输入时序事件数 {len(events)} 超过上限 "
                f"{MAX_TIMELINE_EVENTS}，请重新录制。"
            )
        previous = -1.0
        pressed: set[str] = set()
        buttons: set[str] = set()
        for index, item in enumerate(events, start=1):
            if not isinstance(item, Mapping):
                return f"第 {step_no} 步输入时序第 {index} 个事件格式无效。"
            try:
                moment = float(item.get("t", -1))
            except (TypeError, ValueError):
                return f"第 {step_no} 步输入时序第 {index} 个事件时间无效。"
            if (
                not math.isfinite(moment)
                or moment < 0
                or moment > MAX_TIMELINE_EVENT_SECONDS
            ):
                return (
                    f"第 {step_no} 步输入时序第 {index} 个事件时间必须在 "
                    f"0 到 {MAX_TIMELINE_EVENT_SECONDS:g} 秒之间。"
                )
            if moment < previous:
                return f"第 {step_no} 步输入时序第 {index} 个事件时间早于前一个事件，顺序已损坏。"
            previous = moment
            kind = str(item.get("kind", "")).strip().lower()
            if kind == "key":
                action = str(item.get("action", "")).strip().lower()
                key = str(item.get("key", "")).strip().lower()
                if action not in ("down", "up", "repeat") or not key:
                    return f"第 {step_no} 步输入时序第 {index} 个事件缺少按键或动作无效。"
                if action == "down":
                    pressed.add(key)
                elif action == "up":
                    pressed.discard(key)
                elif key not in pressed:
                    return (
                        f"第 {step_no} 步输入时序第 {index} 个事件是自动重复，"
                        "但此前没有对应的按下事件（回放会导致按键残留）。"
                    )
            elif kind == "mouse":
                action = str(item.get("action", "")).strip().lower()
                button = str(item.get("button", "")).strip().lower()
                if action not in ("down", "up") or not button:
                    return f"第 {step_no} 步输入时序第 {index} 个鼠标事件缺少按钮或动作无效。"
                if not _valid_point(item):
                    return f"第 {step_no} 步输入时序第 {index} 个鼠标事件坐标无效。"
                if action == "down":
                    buttons.add(button)
                else:
                    buttons.discard(button)
            elif kind == "move":
                if not _valid_point(item):
                    return f"第 {step_no} 步输入时序第 {index} 个移动事件坐标无效。"
            elif kind == "wheel":
                try:
                    delta = int(item.get("delta", 0))
                except (TypeError, ValueError):
                    return f"第 {step_no} 步输入时序第 {index} 个滚轮事件增量无效。"
                if delta == 0:
                    return f"第 {step_no} 步输入时序第 {index} 个滚轮事件增量为 0。"
            else:
                return f"第 {step_no} 步输入时序第 {index} 个事件类型不受支持。"
        if buttons:
            names = "、".join(sorted(buttons))
            return (
                f"第 {step_no} 步输入时序结束时仍有鼠标按钮未释放（{names}），"
                "回放会导致按钮残留，请重新录制。"
            )
        if pressed:
            names = "、".join(sorted(pressed))
            return (
                f"第 {step_no} 步输入时序结束时仍有按键未释放（{names}），"
                "回放会导致按键残留，请重新录制。"
            )
        return None

    def _validate_flow_task(
        self,
        task: Mapping[str, Any],
        step_no: int,
        task_type: float,
        value: str,
    ) -> str | None:
        """Validate flow-control tasks (screenshot, drag, launch, window, secret, loop markers)."""
        if task_type == TASK_TYPE_SECRET_TEXT and not value:
            return f"第 {step_no} 步【输入秘密文本】必须填写凭据名称。"
        if task_type in (
            TASK_TYPE_LAUNCH_APP,
            TASK_TYPE_ACTIVATE_WINDOW,
            TASK_TYPE_CLOSE_WINDOW,
        ) and not value:
            return f"第 {step_no} 步参数不能为空！"
        if not value and task_type not in (
            TASK_TYPE_POPUP_NOTIFY,
            TASK_TYPE_STOP_RUN,
            TASK_TYPE_SOUND_ALERT,
            TASK_TYPE_AUTO_CLICKER,
        ):
            return f"第 {step_no} 步参数不能为空！"
        if task_type == TASK_TYPE_AUTO_CLICKER and str(task.get("ac_center_mode", "current")) == "fixed":
            _fx = (task.get("ac_fixed_x") or "").strip()
            _fy = (task.get("ac_fixed_y") or "").strip()
            try:
                _fx_value = float(_fx)
                _fy_value = float(_fy)
            except (TypeError, ValueError):
                return (
                    f"第 {step_no} 步【连点器】中心模式为“固定坐标”时，"
                    "必须填写有效的 X / Y 坐标（屏幕像素）。"
                )
            # SAFETY: ``inf``/``nan``/``1e999`` 能过上面的 ``float()``，运行期
            # 却被解析为 None 并静默改为“跟随鼠标”模式，用户指定的点击点被
            # 悄悄换掉。非有限值与越界值必须在预运行阶段报错
            # （FR-20261012-001），口径与运行期解析一致。
            if (
                not math.isfinite(_fx_value)
                or not math.isfinite(_fy_value)
                or abs(_fx_value) > _MAX_ABS_COORDINATE
                or abs(_fy_value) > _MAX_ABS_COORDINATE
            ):
                return (
                    f"第 {step_no} 步【连点器】中心模式为“固定坐标”时，"
                    f"X / Y 坐标必须是 -{_MAX_ABS_COORDINATE} 到 "
                    f"{_MAX_ABS_COORDINATE} 之间的有限数字（屏幕像素）。\n"
                    f"填入内容: X={_fx or '(空)'} / Y={_fy or '(空)'}"
                )
        if task_type == TASK_TYPE_SCREENSHOT_SAVE:
            capture_mode = str(task.get("capture_region_mode", "global"))
            if capture_mode not in {"global", "full", "custom"}:
                return f"第 {step_no} 步截图范围模式无效！"
            if capture_mode == "custom" and not parse_region_text(
                task.get("capture_region", "")
            ):
                return (
                    f"第 {step_no} 步选择了本步自定义截图区域，"
                    "但小齿轮里的区域为空或格式无效！"
                )
            # DESIGN: 校验接受文件路径是为了兼容手动输入和未来扩展；
            # UI 对话框仅暴露文件夹选择是有意简化。参见 docs/设计基准.md。
            if not os.path.isdir(value):
                extension = os.path.splitext(value)[1].lower()
                if extension not in (".png", ".jpg", ".jpeg", ".bmp"):
                    return f"第 {step_no} 步截图保存路径必须是已存在的文件夹，或以 .png/.jpg/.jpeg/.bmp 结尾的文件路径！\n填入内容: {value}"
                parent_dir = os.path.dirname(os.path.abspath(value))
                if not os.path.isdir(parent_dir):
                    return f"第 {step_no} 步截图保存目录不存在！\n目录: {parent_dir}"
        elif task_type in (TASK_TYPE_LEFT_DRAG, TASK_TYPE_RIGHT_DRAG):
            if "->" not in value:
                return f"第 {step_no} 步拖拽参数错误，需包含 '->' 符号，例如: 100,100 -> 200,200"
            parts = value.split("->")
            if len(parts) != 2 or not parse_coordinate_text(parts[0]) or not parse_coordinate_text(parts[1]):
                return f"第 {step_no} 步拖拽坐标格式异常，无法解析出首尾坐标！"
            try:
                recorded_duration = float(task.get("recorded_duration", 0) or 0)
                if (
                    not math.isfinite(recorded_duration)
                    or recorded_duration < 0
                    or recorded_duration > 300
                ):
                    raise ValueError
            except (TypeError, ValueError):
                return f"第 {step_no} 步录制拖拽时长必须是 0 到 300 秒之间的有限数字！"
            drag_target_img = str(task.get("drag_target_img", "") or "").strip()
            if drag_target_img and not os.path.isfile(drag_target_img):
                return f"第 {step_no} 步拖拽终点图片不存在！\n路径: {drag_target_img}"
            drag_source_img = str(task.get("drag_source_img", "") or "").strip()
            if drag_source_img and not os.path.isfile(drag_source_img):
                return f"第 {step_no} 步拖拽起点图片不存在！\n路径: {drag_source_img}"
        return None

    def _validate_variable_task(
        self,
        task: Mapping[str, Any],
        step_no: int,
        task_type: float,
        value: str,
        global_scale_values: Sequence[float],
        global_use_gray: bool,
        global_confidence: float,
    ) -> str | None:
        """Validate variable, expression, UIA, and multi-image-action tasks."""
        if task_type == TASK_TYPE_MULTI_IMAGE_ACTION:
            return self._validate_multi_image_action(
                task,
                step_no,
                global_scale_values,
                global_use_gray,
                global_confidence,
            )
        if task_type == TASK_TYPE_SET_VARIABLE:
            try:
                parse_assignment(value)
            except ExpressionError as error:
                return f"第 {step_no} 步【设置变量】无效：{error}"
            return None
        if task_type == TASK_TYPE_EXPRESSION:
            try:
                compile_expression(value)
            except ExpressionError as error:
                return f"第 {step_no} 步【判断表达式】无效：{error}"
            return None

        if task_type in (
            TASK_TYPE_UIA_CLICK,
            TASK_TYPE_UIA_SET_VALUE,
            TASK_TYPE_UIA_READ_VALUE,
        ):
            binding = task.get("uia_binding")
            if not isinstance(binding, Mapping) or not binding:
                return f"第 {step_no} 步尚未选择目标窗口控件，请点击步骤右侧的\u201c控\u201d。"
            if task_type == TASK_TYPE_UIA_READ_VALUE:
                try:
                    validate_variable_name(value)
                except ExpressionError as error:
                    return f"第 {step_no} 步【读取控件文本】的变量名无效：{error}"
            return None
        return None
    @staticmethod
    def _validate_coordinate_step(
        step_no: int,
        every: str,
        direction: str,
        distance: str,
        dx: str,
        dy: str,
        point: str,
        max_steps: str,
        max_distance: str,
        reset_after: str,
    ) -> str | None:
        # DEC-20260930-002-04: 已知且可验证的固定别名是迁移入口，必须先归一
        # 再判定，否则旧方案的乱码方向会在预运行校验被拒，别名表形同虚设。
        # 未知值由归一函数原样返回，因此仍会走到下方的 else 分支报错，
        # 错误信息里用户看到的仍是自己的原值。
        direction = normalize_coord_step_direction(direction)
        if not _is_ascii_digits(every) or int(every) < 1:
            return f"第 {step_no} 步小齿轮里的'步进频率'必须是大于等于 1 的整数！\n填入内容: {every}"
        if max_steps and (not _is_ascii_digits(max_steps) or int(max_steps) < 0):
            return f"第 {step_no} 步小齿轮里的'最大偏移次数'必须是大于等于 0 的整数！\n填入内容: {max_steps}"
        if reset_after and (not _is_ascii_digits(reset_after) or int(reset_after) < 0):
            return f"第 {step_no} 步小齿轮里的'重置循环'必须是大于等于 0 的整数！\n填入内容: {reset_after}"
        if direction != "移动到新点位":
            try:
                max_distance_value = float(max_distance or 0)
                if not math.isfinite(max_distance_value) or max_distance_value < 0:
                    raise ValueError
            except (TypeError, ValueError):
                return f"第 {step_no} 步小齿轮里的'最大偏移距离'必须是大于等于 0 的数字！\n填入内容: {max_distance}"
        if direction in ("向上", "向下", "向左", "向右"):
            try:
                if not math.isfinite(float(distance)):
                    raise ValueError
            except (TypeError, ValueError):
                return f"第 {step_no} 步小齿轮里的'步进距离'必须是有限数字！\n填入内容: {distance}"
        elif direction == "自定义偏移":
            try:
                if not math.isfinite(float(dx)) or not math.isfinite(float(dy)):
                    raise ValueError
            except (TypeError, ValueError):
                return f"第 {step_no} 步小齿轮里的'自定义偏移 dx/dy'必须是数字！\n填入内容: dx={dx}, dy={dy}"
        elif direction == "移动到新点位":
            if not parse_coordinate_text(point):
                return f"第 {step_no} 步小齿轮里的'目标点位'必须是 x,y 坐标格式！\n填入内容: {point}"
            if int(max_steps or 0) == 1:
                return f"第 {step_no} 步小齿轮里的'路径点位数'不能填 1。\n填 0 表示只使用起点和目标点；填 2 或更大表示从起点到目标点一共点击多少个点位。"
        else:
            return f"第 {step_no} 步小齿轮里的'步进方向'无效！\n填入内容: {direction}"
        return None

    def _validate_multi_image_action(
        self,
        task: Mapping[str, Any],
        step_no: int,
        global_scale_values: Sequence[float],
        global_use_gray: bool,
        global_confidence: float,
    ) -> str | None:
        raw_rules: Any = task.get("multi_image_actions", [])
        if not isinstance(raw_rules, list):
            return f"第 {step_no} 步【多图动作映射】的规则列表格式无效。"
        rules_list: list[Any] = raw_rules  # pyright: ignore[reportUnknownVariableType]
        if len(rules_list) > MAX_MULTI_IMAGE_ACTION_RULES:
            return (
                f"第 {step_no} 步【多图动作映射】最多允许 "
                f"{MAX_MULTI_IMAGE_ACTION_RULES} 条规则。"
            )
        if any(not isinstance(rule, Mapping) for rule in rules_list):
            return f"第 {step_no} 步【多图动作映射】包含无效规则。"
        rules = normalize_multi_image_rules(rules_list)
        if not rules:
            return f"第 {step_no} 步【多图动作映射】至少要添加一条规则。"

        if config_bool(task.get("custom_en", False)):
            try:
                scale_min = float(task.get("custom_scale_min", self.config.get("scale_min", "0.8")))
                scale_max = float(task.get("custom_scale_max", self.config.get("scale_max", "1.2")))
                scale_step = float(task.get("custom_scale_step", self.config.get("scale_step", "0.05")))
                if scale_min < SCALE_MIN or scale_max > SCALE_MAX:  # constants.SCALE_MIN/MAX
                    raise ValueError(f"缩放范围必须在 {SCALE_MIN:g} 到 {SCALE_MAX:g} 之间")
                scale_values: Sequence[float] = build_scale_values(scale_min, scale_max, scale_step)  # pyright: ignore[reportUnknownVariableType]
            except (TypeError, ValueError) as error:
                return f"第 {step_no} 步【多图动作映射】的独立缩放设置无效：{error}"
            use_gray = config_bool(task.get("custom_gray", global_use_gray))
        else:
            scale_values = global_scale_values
            use_gray = global_use_gray

        for rule_index, rule in enumerate(rules, 1):
            image = str(rule.get("image", "") or "").strip()
            if not image or not os.path.isfile(image):
                return (
                    f"第 {step_no} 步【多图动作映射】第 {rule_index} 条规则的"
                    f"图片路径不存在！\n填入内容: {image}"
                )
            action = str(rule.get("action", "") or "")
            if action not in MULTI_IMAGE_ACTION_BY_CODE:
                return (
                    f"第 {step_no} 步【多图动作映射】第 {rule_index} 条规则的"
                    f"命中动作无效：{action}"
                )
            region = str(rule.get("region", "") or "").strip()
            if region and not parse_region_text(region):
                return (
                    f"第 {step_no} 步【多图动作映射】第 {rule_index} 条规则的"
                    "识别区域格式错误，应为 x,y,w,h。"
                )
            try:
                confidence = float(rule.get("confidence") or global_confidence)
                if not math.isfinite(confidence) or not CONFIDENCE_MIN <= confidence <= CONFIDENCE_MAX:  # constants.CONFIDENCE_MIN/MAX
                    raise ValueError
            except (TypeError, ValueError):
                return (
                    f"第 {step_no} 步【多图动作映射】第 {rule_index} 条规则的"
                    f"相似度必须在 {CONFIDENCE_MIN:g} 到 {CONFIDENCE_MAX:g} 之间。"
                )
            value = str(rule.get("value", "") or "")
            if action in MULTI_IMAGE_REQUIRED_VALUE_ACTIONS and not value.strip():
                return (
                    f"第 {step_no} 步【多图动作映射】第 {rule_index} 条规则的"
                    "命中动作需要填写动作参数。"
                )
            if action == "wait":
                try:
                    wait_seconds = float(value)
                    if not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 86_400:
                        raise ValueError
                except (TypeError, ValueError):
                    return (
                        f"第 {step_no} 步【多图动作映射】第 {rule_index} 条规则的"
                        "等待时间必须在 0 到 86400 秒之间。"
                    )
            elif action == "scroll":
                try:
                    if not math.isfinite(float(value)):
                        raise ValueError
                except (TypeError, ValueError):
                    return (
                        f"第 {step_no} 步【多图动作映射】第 {rule_index} 条规则的"
                        "滚轮参数必须是有限数字。"
                    )
            valid, reason = template_detail_status(image, use_gray)
            if not valid:
                return (
                    f"第 {step_no} 步【多图动作映射】第 {rule_index} 条模板"
                    f"无法安全识别：{reason}"
                )
            budget_error = self._add_template_budget(
                image, use_gray, scale_values, step_no  # pyright: ignore[reportUnknownArgumentType]
            )
            if budget_error:
                return budget_error
        return None

    def _validate_until_task(
        self,
        task: Mapping[str, Any],
        step_no: int,
        task_count: int,
        global_scale_values: Sequence[float],
        global_use_gray: bool,
    ) -> str | None:
        conditions = until_condition_list_from_data(task)
        if not conditions:
            return f"第 {step_no} 步【直到条件成立】至少要启用一个条件。"
        logic = str(task.get("until_logic", "全部满足"))
        if logic not in UNTIL_CONDITION_LOGICS:
            return f"第 {step_no} 步【直到条件成立】的条件关系无效：{logic}"
        action = str(task.get("until_on_limit", "继续下一步"))
        if action not in UNTIL_LIMIT_ACTIONS:
            return f"第 {step_no} 步【直到条件成立】的达到上限后处理方式无效：{action}"
        for key, label in (("until_false_jump", "未满足跳回"), ("until_true_jump", "满足后跳至")):
            raw = str(task.get(key, "0")).strip() or "0"
            if not _is_ascii_digits(raw) or int(raw) > task_count:
                return f"第 {step_no} 步【直到条件成立】里的\u201c{label}\u201d必须是 0 到 {task_count} 之间的整数！\n填入内容: {raw}"
        max_checks = str(task.get("until_max_checks", "0")).strip() or "0"
        if not _is_ascii_digits(max_checks) or int(max_checks) < 0:
            return f"第 {step_no} 步【直到条件成立】里的\u201c最多检查次数\u201d必须是大于等于 0 的整数！\n填入内容: {max_checks}"
        try:
            max_seconds = float(str(task.get("until_max_seconds", "0")).strip() or "0")
            if not math.isfinite(max_seconds) or max_seconds < 0:
                raise ValueError
        except (TypeError, ValueError):
            return f"第 {step_no} 步【直到条件成立】里的\u201c最多等待秒数\u201d必须是大于等于 0 的数字！"

        for condition in conditions:
            condition_no = condition.get("index")
            # FR-20260930-012: 必须校验**原始** until_cond{i}_mode，而不是
            # until_condition_list_from_data 的改写结果——否则越界取值会被静默
            # 替换为「图片出现」，属于 docs/设计基准.md 明文禁止的静默降级，
            # 并使 engine/conditions.py 的「未知条件类型」防御分支不可达。
            # 默认值与解析器同源（缺键 → UNTIL_CONDITION_MODES[0]，即「图片出现」），
            # 且不做 strip：保证「校验接受的取值」与「解析器不改写的取值」一致。
            raw_mode = str(
                task.get(f"until_cond{condition_no}_mode", UNTIL_CONDITION_MODES[0])
            )
            if raw_mode not in UNTIL_CONDITION_MODES:
                return (
                    f"第 {step_no} 步【直到条件成立】的条件{condition_no}模式无效：{raw_mode}"
                    f"\n可选值: {'、'.join(UNTIL_CONDITION_MODES)}"
                )
            mode = condition.get("mode")
            image = str(condition.get("image", "")).strip()
            region_text = str(condition.get("region", "")).strip()
            if mode in ("图片出现", "图片消失", "区域变成指定图片") and (not image or not os.path.isfile(image)):
                return f"第 {step_no} 步【直到条件成立】的条件{condition_no}图片路径不存在！\n填入内容: {image}"
            if region_text and not parse_region_text(region_text):
                return f"第 {step_no} 步【直到条件成立】的条件{condition_no}区域格式错误，应为 x,y,w,h！\n填入内容: {region_text}"
            if mode in (
                "区域发生变化",
                "区域变成指定图片",
                "区域不变",
                # FR-SCOPE013-001：覆盖匹配的消费侧第一步就是 condition_region()，
                # 缺区域会直接返回 (False, "未设置大区域") 使条件恒假。
                "覆盖匹配",
            ) and not parse_region_text(region_text):
                return f"第 {step_no} 步【直到条件成立】的条件{condition_no}必须填写或框选区域，格式为 x,y,w,h。"
            for key, label, minimum, maximum in (
                ("conf", "图片相似度", CONFIDENCE_MIN, CONFIDENCE_MAX),  # constants.CONFIDENCE_MIN/MAX
                ("diff", "变化阈值", 0.0, 100.0),
                ("similarity", "区域相似度", 0.0, 100.0),
            ):
                try:
                    value = float(condition.get(key, "0"))
                    if not math.isfinite(value) or not minimum <= value <= maximum:
                        raise ValueError
                except (TypeError, ValueError):
                    return f"第 {step_no} 步【直到条件成立】的条件{condition_no}{label}必须在 {minimum:g} 到 {maximum:g} 之间！"
            # FR-SCOPE013-002：非法 stable_seconds / sub_region 此前被放行，运行期分别
            # 静默退化为 0.0（首帧即判稳定 → 条件提前成立）与 None（退化为整区域比较）。
            # 与上面 until_max_seconds 的校验采用同一口径：必须是有限且 >= 0 的数字。
            try:
                stable_seconds = float(
                    str(condition.get("stable_seconds", "0")).strip() or "0"
                )
                if not math.isfinite(stable_seconds) or stable_seconds < 0:
                    raise ValueError
            except (TypeError, ValueError):
                return f"第 {step_no} 步【直到条件成立】的条件{condition_no}\u201c稳定秒数\u201d必须是大于等于 0 的数字！"
            sub_region_text = str(condition.get("sub_region", "")).strip()
            if sub_region_text and not parse_region_text(sub_region_text):
                return f"第 {step_no} 步【直到条件成立】的条件{condition_no}子区域格式错误，应为 x,y,w,h！\n填入内容: {sub_region_text}"
            # FR-20261006-005：``sub_region`` 此前只校验格式，越界值被放行，
            # 运行期 ``crop_to_sub_region`` 返回 ``None`` 使条件**静默恒假**，
            # 属 docs/设计基准.md 明文禁止的静默降级。同函数内 ``overlay_rect``
            # 早已有包含关系校验，判据不对称是根因。
            # ADV-20261006-001：两条判据共用 ``_region_outside``，避免第三次漂移。
            if _region_outside(region_text, sub_region_text):
                region_rect = parse_region_text(region_text)
                size = (
                    f"（大区域尺寸 {region_rect[2]}x{region_rect[3]}）"
                    if region_rect is not None
                    else ""
                )
                return (
                    f"第 {step_no} 步【直到条件成立】的条件{condition_no}子区域"
                    f"必须完全落在大区域内{size}！\n填入内容: {sub_region_text}"
                )
            # FR-SCOPE013-001：覆盖匹配的必填项此前完全未校验，缺失/非法时
            # evaluate_overlay_match_condition() 恒返回 False，条件静默恒假。
            if mode == "覆盖匹配":
                overlay_image = str(condition.get("until_overlay_image", "")).strip()
                if not overlay_image or not os.path.isfile(overlay_image):
                    return f"第 {step_no} 步【直到条件成立】的条件{condition_no}覆盖小图路径不存在！\n填入内容: {overlay_image}"
                overlay_rect_text = str(condition.get("until_overlay_rect", "")).strip()
                if not parse_region_text(overlay_rect_text):
                    return f"第 {step_no} 步【直到条件成立】的条件{condition_no}必须填写覆盖小图的子区域位置，格式为 x,y,w,h。\n填入内容: {overlay_rect_text}"
                # FR-20261005-101: 覆盖小图子区域必须完全落在大区域内。此前只校验
                # 格式与数值域，越界配置会放行，随后 evaluate_overlay_match_condition()
                # 因裁剪越界直接返回 False（条件静默恒假），属 docs/设计基准.md
                # 「放行后在运行期静默恒假属于禁止的静默降级」。
                # ADV-20261006-001：与 sub_region 共用同一包含关系判据，
                # 避免两条子区域校验再次漂移。
                if _region_outside(region_text, overlay_rect_text):
                    _region_rect = parse_region_text(region_text)
                    _size = (
                        f"（大区域尺寸 {_region_rect[2]}x{_region_rect[3]}）"
                        if _region_rect is not None
                        else ""
                    )
                    return (
                        f"第 {step_no} 步【直到条件成立】的条件{condition_no}覆盖小图子区域"
                        f"必须完全落在大区域内{_size}！"
                        f"\n填入内容: {overlay_rect_text}"
                    )
                for key, label, minimum, maximum, default in (
                    (
                        "until_overlay_scale",
                        "覆盖小图缩放",
                        UNTIL_OVERLAY_SCALE_MIN,
                        UNTIL_OVERLAY_SCALE_MAX,
                        UNTIL_OVERLAY_SCALE_DEFAULT,
                    ),
                    (
                        "until_overlay_threshold",
                        "覆盖匹配阈值",
                        CONFIDENCE_MIN,
                        CONFIDENCE_MAX,
                        UNTIL_OVERLAY_THRESHOLD_DEFAULT,
                    ),
                ):
                    try:
                        value = float(
                            str(condition.get(key, default)).strip() or default
                        )
                        if not math.isfinite(value) or not minimum <= value <= maximum:
                            raise ValueError
                    except (TypeError, ValueError):
                        return f"第 {step_no} 步【直到条件成立】的条件{condition_no}{label}必须在 {minimum:g} 到 {maximum:g} 之间！"
            if mode in ("图片出现", "图片消失"):
                valid, reason = template_detail_status(image, global_use_gray)
                if not valid:
                    return f"第 {step_no} 步【直到条件成立】的条件{condition_no}模板不安全：{reason}"
                budget_error = self._add_template_budget(
                    image, global_use_gray, global_scale_values, step_no
                )
                if budget_error:
                    return budget_error
            elif mode == "区域变成指定图片":
                budget_error = self._add_template_budget(image, False, (), step_no)
                if budget_error:
                    return budget_error
        return None


def validate_task_list(tasks: Sequence[Mapping[str, Any]], config: Mapping[str, Any]) -> str | None:
    """Validate a task list and return the first error, or None if valid.

    Convenience wrapper around TaskListValidator for callers that only need
    a single actionable message.
    """
    if not isinstance(config, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        return "运行配置不是有效的配置对象。"
    if not isinstance(tasks, Sequence) or isinstance(tasks, (str, bytes, bytearray)):  # pyright: ignore[reportUnnecessaryIsInstance]
        return "步骤列表格式错误。"
    try:
        return TaskListValidator(config).validate(tasks)
    except (TypeError, ValueError, OverflowError) as error:
        return f"步骤配置格式错误：{error}"


def validate_task_list_all(
    tasks: Sequence[Mapping[str, Any]], config: Mapping[str, Any]
) -> list[str]:
    """Validate a task list and collect all errors (one per invalid step).

    Returns an empty list when every step passes validation.
    """
    if not isinstance(config, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        return ["运行配置不是有效的配置对象。"]
    if not isinstance(tasks, Sequence) or isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]
        tasks, (str, bytes, bytearray)
    ):
        return ["步骤列表格式错误。"]
    try:
        return TaskListValidator(config).validate_all(tasks)
    except (TypeError, ValueError, OverflowError) as error:
        return [f"步骤配置格式错误：{error}"]
