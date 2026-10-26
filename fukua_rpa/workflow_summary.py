"""Qt-free step summaries shared by drag previews, search hits and settings.

The summary text is pure data formatting: it must stay importable without
PySide6 so that controllers which only need a summary do not pull the editable
row widget (and, through it, the step-settings dialog) into startup. See ADR
0040.
"""

from .commands import SETTING_COORDINATE_PATH, command_name, step_settings_capabilities
from .constants import TASK_TYPE_MULTI_IMAGE_ACTION, TASK_TYPE_UNTIL
from .multi_image_actions import multi_image_action_summary
from .task_model import (
    config_bool,
    normalize_coord_step_direction,
    parse_coord_step_manual_points,
    until_condition_summary,
)
from .workflow_metadata import normalize_step_note

__all__ = ["task_drag_summary", "unsupported_action_label"]


def unsupported_action_label(raw_type):
    """Label used for action types this build does not register."""

    detail = str(raw_type).replace("\r", " ").replace("\n", " ").strip()
    if len(detail) > 48:
        detail = detail[:45] + "..."
    return f"当前版本不支持（类型 {detail or '空'}）"


def task_drag_summary(data, index, settings_mode="simple"):
    """Build a row summary without constructing a Qt editor widget."""

    task = data if isinstance(data, dict) else {}
    try:
        task_type = float(task.get("type", 1.0))
    except (TypeError, ValueError):
        task_type = 1.0
    value = str(task.get("value", "") or "").replace("\n", " ").strip()
    # FR-20260812 建议-体验-2: 未注册动作类型在拖拽摘要中显示与实体行一致的
    # unsupported 标签（"当前版本不支持（类型 X）"），避免摘要显示"未知操作"
    # 而实体行显示不同文案。
    type_label = command_name(task_type)
    if type_label == "未知操作":
        type_label = unsupported_action_label(task.get("type", ""))
    if task_type == TASK_TYPE_UNTIL:
        value = until_condition_summary(task)
    elif task_type == TASK_TYPE_MULTI_IMAGE_ACTION:
        value = multi_image_action_summary(task.get("multi_image_actions", []))
    if len(value) > 80:
        value = value[:77] + "..."
    marks = []
    if config_bool(task.get("step_disabled", False)):
        marks.append("停")
    if normalize_step_note(task.get("step_note", "")):
        marks.append("注")
    if str(settings_mode) == "advanced" and config_bool(
        task.get("editor_hidden", False)
    ):
        marks.append("隐")
    if str(settings_mode) == "advanced" and task.get("editor_groups"):
        marks.append("组")
    if str(settings_mode) == "advanced" and config_bool(
        task.get("debug_breakpoint", False)
    ):
        marks.append("断")
    if (
        SETTING_COORDINATE_PATH
        in step_settings_capabilities(task_type, task.get("value", ""))
        and config_bool(task.get("coord_step_en", False))
        and normalize_coord_step_direction(
            str(task.get("coord_step_direction", ""))
        )
        == "移动到新点位"
        and bool(
            parse_coord_step_manual_points(task.get("coord_step_manual_points", "{}"))
        )
    ):
        marks.append("修")
    mark = f" [{'|'.join(marks)}]" if marks else ""
    return f"{int(index)}.{mark} {type_label} | {value}"
