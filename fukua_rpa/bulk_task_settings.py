"""Capability-aware field filtering for batch step edits."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .commands import (
    CLICK_COMMANDS,
    CONTROL_CALL_STEP,
    CONTROL_FAILURE_BRANCH,
    CONTROL_FAILURE_POLICY,
    CONTROL_JUMP_ONLY,
    CONTROL_LOOP_RANGE,
    CONTROL_REPEAT,
    CONTROL_RUN_MAX,
    CONTROL_SUCCESS_BRANCH,
    DRAG_COMMANDS,
    SETTING_BACKGROUND_CLICK,
    SETTING_CAPTURE_REGION,
    SETTING_CLICK_MOTION,
    SETTING_CLICK_RETURN,
    SETTING_CLICK_SPATIAL_JITTER,
    SETTING_COORDINATE_PATH,
    SETTING_DELAYED_SECOND_CLICK,
    SETTING_IMAGE_CLICK_POINT,
    SETTING_IMAGE_CONFIRM_CLICK,
    SETTING_IMAGE_MASK,
    SETTING_IMAGE_POINT_LIMIT,
    SETTING_IMAGE_RECOGNITION,
    SETTING_INVERTED_MATCH,
    SETTING_KEY_HOLD,
    SETTING_MOUSE_TRAIL,
    SETTING_MULTI_IMAGE_ACTIONS,
    SETTING_STEP_REGION,
    SETTING_UNTIL_CONDITIONS,
    step_control_capabilities,
    step_settings_capabilities,
)

ALWAYS_BATCH_FIELDS = frozenset(
    {
        "step_note",
        "step_disabled",
        "editor_hidden",
        "debug_breakpoint",
        "debug_condition",
    }
)

# DESIGN: 步骤级随机延迟只在鼠标点击与拖拽命令中被执行引擎消费
# （engine_actions.py 中 ``command in CLICK_COMMANDS or command in DRAG_COMMANDS``）。
# 该适用范围没有独立的 settings 能力标识，因此这里按命令编号直接判定，
# 保持与引擎的点击/拖拽随机延迟路径一致。
JITTER_FIELDS = frozenset({"move_jitter", "click_hld_jitter"})
JITTER_COMMANDS = CLICK_COMMANDS | DRAG_COMMANDS

SETTING_FIELD_CAPABILITY = {
    "custom_en": SETTING_IMAGE_RECOGNITION,
    "custom_conf": SETTING_IMAGE_RECOGNITION,
    "custom_scale_min": SETTING_IMAGE_RECOGNITION,
    "custom_scale_max": SETTING_IMAGE_RECOGNITION,
    "custom_scale_step": SETTING_IMAGE_RECOGNITION,
    "custom_gray": SETTING_IMAGE_RECOGNITION,
    "point_limit_en": SETTING_IMAGE_POINT_LIMIT,
    "point_limit_count": SETTING_IMAGE_POINT_LIMIT,
    "image_click_point_en": SETTING_IMAGE_CLICK_POINT,
    "image_click_point_rx": SETTING_IMAGE_CLICK_POINT,
    "image_click_point_ry": SETTING_IMAGE_CLICK_POINT,
    "step_region_en": SETTING_STEP_REGION,
    "step_region": SETTING_STEP_REGION,
    "inverse_match_en": SETTING_INVERTED_MATCH,
    "image_confirm_click_en": SETTING_IMAGE_CONFIRM_CLICK,
    "image_confirm_click_delay": SETTING_IMAGE_CONFIRM_CLICK,
    "template_mask_regions": SETTING_IMAGE_MASK,
    "delayed_second_click_en": SETTING_DELAYED_SECOND_CLICK,
    "delayed_second_click_delay": SETTING_DELAYED_SECOND_CLICK,
    "multi_image_actions": SETTING_MULTI_IMAGE_ACTIONS,
    "capture_region_mode": SETTING_CAPTURE_REGION,
    "capture_region": SETTING_CAPTURE_REGION,
    "coord_step_en": SETTING_COORDINATE_PATH,
    "coord_step_every": SETTING_COORDINATE_PATH,
    "coord_step_direction": SETTING_COORDINATE_PATH,
    "coord_step_distance": SETTING_COORDINATE_PATH,
    "coord_step_dx": SETTING_COORDINATE_PATH,
    "coord_step_dy": SETTING_COORDINATE_PATH,
    "coord_step_point": SETTING_COORDINATE_PATH,
    "coord_step_max_steps": SETTING_COORDINATE_PATH,
    "coord_step_max_distance": SETTING_COORDINATE_PATH,
    "coord_step_stop": SETTING_COORDINATE_PATH,
    "coord_step_reset_after": SETTING_COORDINATE_PATH,
    "coord_step_manual_points": SETTING_COORDINATE_PATH,
    "coord_sequence_en": SETTING_COORDINATE_PATH,
    "coord_sequence_points": SETTING_COORDINATE_PATH,
    "coord_sequence_end_action": SETTING_COORDINATE_PATH,
    "coord_sequence_batch_mode": SETTING_COORDINATE_PATH,
    "coord_sequence_batch_count": SETTING_COORDINATE_PATH,
    "background_click_en": SETTING_BACKGROUND_CLICK,
    "background_click_binding": SETTING_BACKGROUND_CLICK,
    "click_spatial_jitter": SETTING_CLICK_SPATIAL_JITTER,
    "move_curve_strength": SETTING_CLICK_MOTION,
    "click_return": SETTING_CLICK_RETURN,
    "mouse_trail_mode": SETTING_MOUSE_TRAIL,
    "key_hold_duration": SETTING_KEY_HOLD,
}

CONTROL_FIELD_CAPABILITY = {
    "repeat_mode": CONTROL_REPEAT,
    "repeat_count": CONTROL_REPEAT,
    "step_loop_start": CONTROL_LOOP_RANGE,
    "step_loop_end": CONTROL_LOOP_RANGE,
    "fail_limit": CONTROL_FAILURE_POLICY,
    "no_skip_wait": CONTROL_FAILURE_POLICY,
    "run_max_executions": CONTROL_RUN_MAX,
    "success_skip": CONTROL_SUCCESS_BRANCH,
    "success_jump": CONTROL_SUCCESS_BRANCH,
    "fail_skip": CONTROL_FAILURE_BRANCH,
    "fail_jump": CONTROL_FAILURE_BRANCH,
    "call_step": CONTROL_CALL_STEP,
    "jump_only": CONTROL_JUMP_ONLY,
}


def common_batch_capabilities(
    tasks: Sequence[Mapping[str, Any]],
) -> tuple[frozenset[str], frozenset[str]]:
    """Compute the capability intersection across all selected steps.

    Only capabilities shared by every step in the selection are returned,
    ensuring that bulk-edit UI only exposes fields applicable to all targets.

    Args:
        tasks: Sequence of step configuration dicts in the current selection.

    Returns:
        A tuple (settings_capabilities, control_capabilities) where each is a
        frozenset of capability identifiers common to all given steps. Both are
        empty when tasks is empty.
    """
    if not tasks:
        return frozenset(), frozenset()
    setting_sets = [
        set(step_settings_capabilities(task.get("type"), task.get("value", "")))
        for task in tasks
    ]
    control_sets = [
        set(step_control_capabilities(task.get("type"), task.get("value", "")))
        for task in tasks
    ]
    settings = set.intersection(*setting_sets) if setting_sets else set()
    controls = set.intersection(*control_sets) if control_sets else set()
    return frozenset(settings), frozenset(controls)


def combined_batch_capabilities(
    tasks: Sequence[Mapping[str, Any]],
) -> tuple[frozenset[str], frozenset[str]]:
    """Return capabilities used by at least one selected step.

    Advanced batch editing may expose a field that applies to only part of the
    selection, but must not expose fields unrelated to every selected step.
    ``filter_bulk_updates`` remains responsible for per-step write filtering.
    """

    settings: set[str] = set()
    controls: set[str] = set()
    for task in tasks:
        settings.update(
            step_settings_capabilities(task.get("type"), task.get("value", ""))
        )
        controls.update(
            step_control_capabilities(task.get("type"), task.get("value", ""))
        )
    return frozenset(settings), frozenset(controls)


def bulk_field_applies(
    field: str,
    task: Mapping[str, Any],
    settings: frozenset[str] | None = None,
    controls: frozenset[str] | None = None,
) -> bool:
    """Determine whether a bulk-edit field applies to a specific step.

    Fields in ALWAYS_BATCH_FIELDS apply unconditionally. Jitter fields apply
    only to click/drag commands. Remaining fields are checked against the
    step's settings or control capability sets.

    Args:
        field: The configuration field name to check.
        task: Step configuration dict.
        settings: Pre-computed settings capabilities (computed if None).
        controls: Pre-computed control capabilities (computed if None).

    Returns:
        True if the field is meaningful for the given step, False otherwise.
    """
    if field in ALWAYS_BATCH_FIELDS:
        return True
    if field in JITTER_FIELDS:
        try:
            command = float(task.get("type"))  # pyright: ignore[reportArgumentType]
        except (TypeError, ValueError):
            return False
        return command in JITTER_COMMANDS
    if settings is None:
        settings = step_settings_capabilities(task.get("type"), task.get("value", ""))
    if controls is None:
        controls = step_control_capabilities(task.get("type"), task.get("value", ""))
    if field.startswith("until_"):
        return SETTING_UNTIL_CONDITIONS in settings
    capability = SETTING_FIELD_CAPABILITY.get(field)
    if capability is not None:
        return capability in settings
    capability = CONTROL_FIELD_CAPABILITY.get(field)
    if capability is not None:
        return capability in controls
    return False


def filter_bulk_updates(
    task: Mapping[str, Any], updates: Mapping[str, Any]
) -> dict[str, Any]:
    """Filter a bulk-update dict to only fields applicable to the target step.

    Computes the step's capability sets once and uses them to gate every field
    in updates through bulk_field_applies, producing the effective update subset
    safe to apply without corrupting inapplicable parameters.

    Args:
        task: Target step configuration dict.
        updates: Candidate field-value pairs from the bulk-edit dialog.

    Returns:
        Dict containing only the field-value pairs applicable to this step.
    """
    # 能力集对同一个 task 只计算一次，避免在每个字段上重复计算。
    settings = step_settings_capabilities(task.get("type"), task.get("value", ""))
    controls = step_control_capabilities(task.get("type"), task.get("value", ""))
    return {
        field: value
        for field, value in updates.items()
        if bulk_field_applies(field, task, settings, controls)
    }


def mixed_bulk_fields(
    tasks: Sequence[Mapping[str, Any]], fields: Sequence[str]
) -> set[str]:
    """Identify fields whose current values differ across selected steps.

    Used by the bulk-edit UI to show a "mixed" placeholder instead of a
    concrete value when the selection is heterogeneous.

    Args:
        tasks: Sequence of step configuration dicts in the current selection.
        fields: Field names to compare.

    Returns:
        Set of field names that have non-uniform values across tasks. Empty
        when fewer than 2 tasks are selected.
    """
    if len(tasks) < 2:
        return set()
    mixed: set[str] = set()
    first = tasks[0]
    for field in fields:
        initial = first.get(field)
        if any(task.get(field) != initial for task in tasks[1:]):
            mixed.add(field)
    return mixed
