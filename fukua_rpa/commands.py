"""Persisted command identities and action-capability rules.

The capability tables deliberately separate stored drafts from settings that
have meaning for the action currently selected. See ADR 0025.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .constants import (
    TASK_TYPE_ACTIVATE_WINDOW,
    TASK_TYPE_AUTO_CLICKER,
    TASK_TYPE_CLOSE_WINDOW,
    TASK_TYPE_EXPRESSION,
    TASK_TYPE_INPUT_TIMELINE,
    TASK_TYPE_LAUNCH_APP,
    TASK_TYPE_MULTI_IMAGE_ACTION,
    TASK_TYPE_PLUGIN_ACTION,
    TASK_TYPE_SECRET_TEXT,
    TASK_TYPE_SET_VARIABLE,
    TASK_TYPE_UIA_CLICK,
    TASK_TYPE_UIA_READ_VALUE,
    TASK_TYPE_UIA_SET_VALUE,
    TASK_TYPE_UNTIL,
    TASK_TYPE_WAIT_WINDOW,
)
from .task_model import config_bool, parse_coordinate_text

SETTING_IMAGE_RECOGNITION = "image_recognition"
SETTING_IMAGE_POINT_LIMIT = "image_point_limit"
SETTING_IMAGE_CLICK_POINT = "image_click_point"
SETTING_STEP_REGION = "step_region"
SETTING_COORDINATE_PATH = "coordinate_path"
SETTING_BACKGROUND_CLICK = "background_click"
SETTING_CAPTURE_REGION = "capture_region"
SETTING_UNTIL_CONDITIONS = "until_conditions"
SETTING_INVERTED_MATCH = "inverted_match"
SETTING_IMAGE_CONFIRM_CLICK = "image_confirm_click"
SETTING_IMAGE_MASK = "image_mask"
SETTING_DELAYED_SECOND_CLICK = "delayed_second_click"
SETTING_MULTI_IMAGE_ACTIONS = "multi_image_actions"
# 点击落点随机偏移只对单击/双击生效；拖拽只共享下方的移动曲线能力。
SETTING_CLICK_SPATIAL_JITTER = "click_spatial_jitter"
SETTING_CLICK_MOTION = "click_motion"
SETTING_CLICK_RETURN = "click_return"
SETTING_MOUSE_TRAIL = "mouse_trail"
SETTING_KEY_HOLD = "key_hold"

CONTROL_REPEAT = "repeat"
CONTROL_LOOP_RANGE = "loop_range"
CONTROL_FAILURE_POLICY = "failure_policy"
CONTROL_RUN_MAX = "run_max"
CONTROL_SUCCESS_BRANCH = "success_branch"
CONTROL_FAILURE_BRANCH = "failure_branch"
CONTROL_CALL_STEP = "call_step"
CONTROL_JUMP_ONLY = "jump_only"

GENERIC_CONTROL_CAPABILITIES = frozenset(
    {
        CONTROL_REPEAT,
        CONTROL_LOOP_RANGE,
        CONTROL_FAILURE_POLICY,
        CONTROL_RUN_MAX,
        CONTROL_SUCCESS_BRANCH,
        CONTROL_FAILURE_BRANCH,
        CONTROL_CALL_STEP,
        CONTROL_JUMP_ONLY,
    }
)

IMAGE_SETTING_CAPABILITIES = frozenset(
    {
        SETTING_IMAGE_RECOGNITION,
        SETTING_IMAGE_POINT_LIMIT,
        SETTING_IMAGE_CLICK_POINT,
        SETTING_STEP_REGION,
        SETTING_INVERTED_MATCH,
        SETTING_IMAGE_CONFIRM_CLICK,
        SETTING_IMAGE_MASK,
    }
)

ALL_SETTING_CAPABILITIES = frozenset(
    {
        SETTING_IMAGE_RECOGNITION,
        SETTING_IMAGE_POINT_LIMIT,
        SETTING_IMAGE_CLICK_POINT,
        SETTING_STEP_REGION,
        SETTING_COORDINATE_PATH,
        SETTING_BACKGROUND_CLICK,
        SETTING_CAPTURE_REGION,
        SETTING_UNTIL_CONDITIONS,
        SETTING_INVERTED_MATCH,
        SETTING_IMAGE_CONFIRM_CLICK,
        SETTING_IMAGE_MASK,
        SETTING_DELAYED_SECOND_CLICK,
        SETTING_MULTI_IMAGE_ACTIONS,
        SETTING_CLICK_SPATIAL_JITTER,
        SETTING_CLICK_MOTION,
        SETTING_CLICK_RETURN,
        SETTING_MOUSE_TRAIL,
        SETTING_KEY_HOLD,
    }
)


@dataclass(frozen=True)
class CommandSpec:
    """Immutable registry entry describing one persisted step action type.

    Each spec carries the numeric code serialized into workflows, the
    display name, UI category, and capability flags that determine which
    settings and target kinds are meaningful for the action.
    """

    code: float
    name: str
    category: str
    accepts_coordinate: bool = False
    accepts_image: bool = False
    advanced_only: bool = False
    tool_only: bool = False
    settings: frozenset[str] = frozenset()


_IMAGE_CLICK_SETTINGS = frozenset(
    {
        SETTING_IMAGE_RECOGNITION,
        SETTING_IMAGE_POINT_LIMIT,
        SETTING_IMAGE_CLICK_POINT,
        SETTING_STEP_REGION,
        SETTING_COORDINATE_PATH,
        SETTING_BACKGROUND_CLICK,
        SETTING_INVERTED_MATCH,
        SETTING_IMAGE_CONFIRM_CLICK,
        SETTING_IMAGE_MASK,
        SETTING_DELAYED_SECOND_CLICK,
        SETTING_CLICK_SPATIAL_JITTER,
        SETTING_CLICK_MOTION,
        SETTING_CLICK_RETURN,
        SETTING_MOUSE_TRAIL,
    }
)


# INVARIANT: ``code`` is serialized into every workflow. Add new codes at the
# end and never repurpose an existing value; the tuple order is UI order only.
COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec(1.0, "左键单击", "mouse", True, True, settings=_IMAGE_CLICK_SETTINGS),
    CommandSpec(2.0, "左键双击", "mouse", True, True, settings=_IMAGE_CLICK_SETTINGS),
    CommandSpec(3.0, "右键单击", "mouse", True, True, settings=_IMAGE_CLICK_SETTINGS),
    CommandSpec(4.0, "输入文本", "keyboard"),
    CommandSpec(5.0, "等待(秒)", "flow"),
    CommandSpec(6.0, "滚轮滑动", "mouse"),
    CommandSpec(7.0, "系统按键", "keyboard", settings=frozenset({SETTING_KEY_HOLD})),
    CommandSpec(
        8.0,
        "鼠标悬停",
        "mouse",
        accepts_coordinate=True,
        accepts_image=True,
        advanced_only=True,
        settings=frozenset(
            {SETTING_IMAGE_RECOGNITION, SETTING_STEP_REGION, SETTING_INVERTED_MATCH}
            | {SETTING_IMAGE_MASK, SETTING_MOUSE_TRAIL}
        ),
    ),
    CommandSpec(
        9.0,
        "截图保存",
        "capture",
        settings=frozenset({SETTING_CAPTURE_REGION}),
    ),
    CommandSpec(
        10.0,
        "左键拖拽",
        "mouse",
        settings=frozenset({SETTING_CLICK_MOTION, SETTING_MOUSE_TRAIL}),
    ),
    CommandSpec(
        11.0,
        "右键拖拽",
        "mouse",
        advanced_only=True,
        settings=frozenset({SETTING_CLICK_MOTION, SETTING_MOUSE_TRAIL}),
    ),
    CommandSpec(12.0, "弹窗提醒", "notification"),
    CommandSpec(13.0, "停止运行", "flow"),
    CommandSpec(14.0, "声音提示", "notification"),
    CommandSpec(
        TASK_TYPE_UNTIL,
        "直到条件成立",
        "flow",
        settings=frozenset({SETTING_UNTIL_CONDITIONS}),
    ),
    CommandSpec(TASK_TYPE_SET_VARIABLE, "设置变量", "flow", advanced_only=True),
    CommandSpec(TASK_TYPE_EXPRESSION, "判断表达式", "flow", advanced_only=True),
    CommandSpec(TASK_TYPE_LAUNCH_APP, "启动程序", "window", advanced_only=True),
    CommandSpec(TASK_TYPE_WAIT_WINDOW, "等待窗口", "window", advanced_only=True),
    CommandSpec(TASK_TYPE_ACTIVATE_WINDOW, "激活窗口", "window", advanced_only=True),
    CommandSpec(TASK_TYPE_CLOSE_WINDOW, "关闭窗口", "window", advanced_only=True),
    CommandSpec(TASK_TYPE_SECRET_TEXT, "输入秘密文本", "keyboard", advanced_only=True),
    CommandSpec(TASK_TYPE_UIA_CLICK, "点击窗口控件", "window", advanced_only=True),
    CommandSpec(TASK_TYPE_UIA_SET_VALUE, "设置控件文本", "window", advanced_only=True),
    CommandSpec(TASK_TYPE_UIA_READ_VALUE, "读取控件文本", "window", advanced_only=True),
    CommandSpec(
        TASK_TYPE_MULTI_IMAGE_ACTION,
        "多图动作映射",
        "flow",
        advanced_only=True,
            settings=frozenset(
            {
                SETTING_IMAGE_RECOGNITION,
                SETTING_STEP_REGION,
                SETTING_INVERTED_MATCH,
                SETTING_MULTI_IMAGE_ACTIONS,
            }
        ),
    ),
    CommandSpec(
        TASK_TYPE_PLUGIN_ACTION,
        "插件动作",
        "plugin",
        advanced_only=True,
    ),
    # 连点器（原生指令）已移出步骤命令列表，改由“工具”菜单的独立窗口承载
    # （见 ui/auto_clicker_tool.py）：点击打开设置窗口，配置参数后点“开始”立即
    # 运行，不作为脚本步骤。故标记 tool_only=True，从步骤类型选择器中排除，
    # 但 COMMAND_BY_CODE 仍保留 28.0 以便旧版工作流中的连点器步骤正常加载与运行。
    CommandSpec(TASK_TYPE_AUTO_CLICKER, "连点器", "mouse", tool_only=True),
    # 录制时序（批次 2）：由录制自动生成，不提供手工新建入口，故 tool_only。
    CommandSpec(TASK_TYPE_INPUT_TIMELINE, "输入时序", "keyboard", tool_only=True),
)

COMMAND_BY_CODE: dict[float, CommandSpec] = {spec.code: spec for spec in COMMAND_SPECS}
COMMAND_BY_NAME: dict[str, CommandSpec] = {spec.name: spec for spec in COMMAND_SPECS}
CLICK_COMMANDS: frozenset[float] = frozenset({1.0, 2.0, 3.0})
IMAGE_TARGET_COMMANDS: frozenset[float] = frozenset(
    spec.code for spec in COMMAND_SPECS if spec.accepts_image
)
COORDINATE_TARGET_COMMANDS: frozenset[float] = frozenset(
    spec.code for spec in COMMAND_SPECS if spec.accepts_coordinate
)
DRAG_COMMANDS: frozenset[float] = frozenset({10.0, 11.0})


def command_name(code: Any, default: str = "未知操作") -> str:
    """Resolve a persisted command code to its display name.

    Args:
        code: Numeric command code (or value convertible to float).
        default: Fallback name when the code is unrecognized.

    Returns:
        Human-readable action name from the registry.
    """
    try:
        normalized = float(code)
    except (TypeError, ValueError):
        return default
    spec = COMMAND_BY_CODE.get(normalized)
    return spec.name if spec else default


def command_code(name: Any, default: float | None = None) -> float | None:
    """Resolve a display name to its persisted numeric command code.

    Args:
        name: Action display name (coerced to str for lookup).
        default: Value returned when the name is not in the registry.

    Returns:
        The numeric code, or *default* if unrecognized.
    """
    spec = COMMAND_BY_NAME.get(str(name))
    return spec.code if spec else default


def step_settings_capabilities(command: Any, value: Any = "") -> frozenset[str]:
    """Resolve settings that currently have runtime meaning for an action.

    DESIGN: an unset target intentionally enables neither coordinate nor image
    settings. Their stored drafts remain untouched and become editable again
    after the user supplies the corresponding target. See ADR 0025.
    """

    spec = None
    if isinstance(command, str):
        spec = COMMAND_BY_NAME.get(command)
    if spec is None:
        try:
            spec = COMMAND_BY_CODE.get(float(command))
        except (TypeError, ValueError):
            spec = None
    if spec is None:
        return frozenset()

    capabilities = set(spec.settings)
    target_kind = step_target_kind(spec, value)
    coordinate = target_kind == "coordinate"
    if coordinate:
        capabilities.difference_update(IMAGE_SETTING_CAPABILITIES)
    else:
        capabilities.discard(SETTING_COORDINATE_PATH)
        capabilities.discard(SETTING_BACKGROUND_CLICK)
        if target_kind == "unset" and spec.accepts_image:
            capabilities.difference_update(IMAGE_SETTING_CAPABILITIES)
            capabilities.discard(SETTING_DELAYED_SECOND_CLICK)
            # 未指定目标时点击形态/返回原位无意义（仍沿用全局），按 DESIGN 隐藏。
            capabilities.discard(SETTING_CLICK_SPATIAL_JITTER)
            capabilities.discard(SETTING_CLICK_MOTION)
            capabilities.discard(SETTING_CLICK_RETURN)
            capabilities.discard(SETTING_MOUSE_TRAIL)
    return frozenset(capabilities)


def command_spec(command: Any) -> CommandSpec | None:
    """Normalize a command reference (code, name, or spec) to a CommandSpec.

    Args:
        command: A CommandSpec instance, a display-name string, or a numeric
            code (or value convertible to float).

    Returns:
        The matching CommandSpec, or None if unrecognized.
    """
    if isinstance(command, CommandSpec):
        return command
    if isinstance(command, str):
        spec = COMMAND_BY_NAME.get(command)
        if spec is not None:
            return spec
    try:
        return COMMAND_BY_CODE.get(float(command))
    except (TypeError, ValueError):
        return None


def step_target_kind(command: Any, value: Any = "") -> str:
    """Classify a target field without treating an empty value as an image."""

    spec = command_spec(command)
    if spec is None or not (spec.accepts_coordinate or spec.accepts_image):
        return "none"
    text = str(value or "").strip()
    if not text:
        return "unset"
    if spec.accepts_coordinate and parse_coordinate_text(text) is not None:
        return "coordinate"
    if spec.accepts_image:
        return "image"
    return "invalid"


def step_control_capabilities(command: Any, value: Any = "") -> frozenset[str]:
    """Return generic controls that have runtime meaning for one action."""

    spec = command_spec(command)
    if spec is None:
        return frozenset()
    capabilities = set(GENERIC_CONTROL_CAPABILITIES)

    if spec.code in {5.0, 13.0, TASK_TYPE_UNTIL, TASK_TYPE_EXPRESSION, TASK_TYPE_WAIT_WINDOW}:
        capabilities.discard(CONTROL_REPEAT)
    if spec.code in {5.0, 12.0, 13.0, 14.0, TASK_TYPE_UNTIL}:
        capabilities.discard(CONTROL_FAILURE_POLICY)
        capabilities.discard(CONTROL_FAILURE_BRANCH)
    if spec.code in {13.0, TASK_TYPE_UNTIL}:
        capabilities.discard(CONTROL_RUN_MAX)
        capabilities.discard(CONTROL_SUCCESS_BRANCH)
    if spec.code == 13.0:
        capabilities.discard(CONTROL_CALL_STEP)
    return frozenset(capabilities)


def effective_task_settings(task: Mapping[str, Any]) -> dict[str, Any]:
    """Return a runtime copy with action-irrelevant settings disabled.

    INVARIANT: never clear these fields in the persisted task. Switching an
    action away and back must restore the user's draft; only this detached copy
    is allowed to suppress validation and execution. See ADR 0025.
    """
    # NOTE: Returns a shallow copy (dict(task)). Nested values (lists/dicts)
    # share references with the original. Callers should not mutate nested
    # structures in-place; assign new values to top-level keys instead.

    effective = dict(task)
    capabilities = step_settings_capabilities(
        task.get("type"), task.get("value", "")
    )
    if SETTING_IMAGE_RECOGNITION not in capabilities:
        effective["custom_en"] = False
    if SETTING_IMAGE_POINT_LIMIT not in capabilities:
        effective["point_limit_en"] = False
    if SETTING_IMAGE_CLICK_POINT not in capabilities:
        effective["image_click_point_en"] = False
    if SETTING_STEP_REGION not in capabilities:
        effective["step_region_en"] = False
    if SETTING_INVERTED_MATCH not in capabilities:
        effective["inverse_match_en"] = False
    if SETTING_IMAGE_CONFIRM_CLICK not in capabilities:
        effective["image_confirm_click_en"] = False
    if SETTING_IMAGE_MASK not in capabilities:
        effective["template_mask_regions"] = []
    if SETTING_DELAYED_SECOND_CLICK not in capabilities:
        effective["delayed_second_click_en"] = False
    if SETTING_MULTI_IMAGE_ACTIONS not in capabilities:
        effective["multi_image_actions"] = []
    if SETTING_COORDINATE_PATH not in capabilities:
        effective["coord_step_en"] = False
        effective["coord_sequence_en"] = False
    elif config_bool(effective.get("coord_sequence_en")):
        # DESIGN: the custom sequence owns point order for this run. Preserve
        # the step-path draft, but do not multiply two coordinate generators.
        effective["coord_step_en"] = False
    if SETTING_BACKGROUND_CLICK not in capabilities:
        effective["background_click_en"] = False
    if SETTING_CLICK_SPATIAL_JITTER not in capabilities:
        effective["click_spatial_jitter"] = ""
    if SETTING_CLICK_MOTION not in capabilities:
        effective["move_curve_strength"] = ""
    if SETTING_CLICK_RETURN not in capabilities:
        effective["click_return"] = False
    if SETTING_MOUSE_TRAIL not in capabilities:
        effective["mouse_trail_mode"] = "inherit"
    if SETTING_KEY_HOLD not in capabilities:
        effective["key_hold_duration"] = ""

    controls = step_control_capabilities(task.get("type"), task.get("value", ""))
    if CONTROL_REPEAT not in controls:
        effective["repeat_mode"] = "执行一次"
        effective["repeat_count"] = "1"
    if CONTROL_LOOP_RANGE not in controls:
        effective["step_loop_start"] = "1"
        effective["step_loop_end"] = "0"
    if CONTROL_FAILURE_POLICY not in controls:
        effective["fail_limit"] = "1"
        effective["no_skip_wait"] = False
    if CONTROL_RUN_MAX not in controls:
        effective["run_max_executions"] = "0"
    if CONTROL_SUCCESS_BRANCH not in controls:
        effective["success_skip"] = "0"
        effective["success_jump"] = "0"
        effective["success_target_id"] = ""
    if CONTROL_FAILURE_BRANCH not in controls:
        effective["fail_skip"] = "0"
        effective["fail_jump"] = "0"
        effective["fail_target_id"] = ""
    if CONTROL_CALL_STEP not in controls:
        effective["call_step"] = "0"
        effective["call_target_id"] = ""
    if CONTROL_JUMP_ONLY not in controls:
        effective["jump_only"] = False
    return effective


def command_names(include_advanced: bool = True, preserve_names: Iterable[str] = ()) -> list[str]:
    """Return ordered display names for the step-type selector.

    Tool-only commands are always excluded. Advanced commands are included
    unless *include_advanced* is False, in which case only names listed in
    *preserve_names* survive the filter (used to keep a currently-selected
    advanced action visible after switching to simple mode).

    Args:
        include_advanced: Whether to include advanced-only actions.
        preserve_names: Names to keep even when advanced actions are hidden.

    Returns:
        Ordered list of display names matching COMMAND_SPECS order.
    """
    preserved = {str(name) for name in preserve_names if str(name)}
    return [
        spec.name
        for spec in COMMAND_SPECS
        if not spec.tool_only
        and (include_advanced or not spec.advanced_only or spec.name in preserved)
    ]
