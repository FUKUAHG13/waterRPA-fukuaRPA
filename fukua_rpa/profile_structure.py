"""Qt-free structural validation for one profile configuration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from .constants import (
    MAX_KEY_MAPPING_FIELDS,
    MAX_KEY_MAPPINGS,
    MAX_PROFILE_JSON_BYTES,
    MAX_PROFILE_TOP_LEVEL_FIELDS,
    MAX_TASK_FIELDS,
    MAX_TASKS_PER_PROFILE,
)
from .json_safety import validate_json_structure


def validate_profile_config_data(config: Mapping[str, Any], label: str = "方案") -> str | None:
    """Validate a single profile configuration against structural constraints.

    Checks field types, value ranges, task list integrity, key-mapping
    structural validity (count bounds, per-slot field limits), and serialized
    size without requiring Qt or UI dependencies. 注意（FR-20260812 文档#2）：
    这里不校验 key_mapping_count 与 mapping 列表长度的语义一致性——语义校验
    由 profile_validation 与运行配置组装负责。

    Args:
        config: Raw profile configuration dictionary.
        label: Human-readable label for error messages.

    Returns:
        A validation error message, or ``None`` if the config is valid.
    """
    if not isinstance(config, dict):
        return f"{label}不是有效的配置对象"
    try:
        validate_json_structure(config, label=label)
    except ValueError as error:
        return str(error)
    if len(config) > MAX_PROFILE_TOP_LEVEL_FIELDS:
        return f"{label}包含过多顶层字段，最多允许 {MAX_PROFILE_TOP_LEVEL_FIELDS} 个"

    tasks = config.get("tasks", [])
    if not isinstance(tasks, list):
        return f"{label}的步骤列表格式错误"
    task_list = cast(list[Any], tasks)
    if len(task_list) > MAX_TASKS_PER_PROFILE:
        return f"{label}包含 {len(task_list)} 个步骤，超过上限 {MAX_TASKS_PER_PROFILE}"
    if any(not isinstance(task, dict) for task in task_list):
        return f"{label}包含格式错误的步骤"
    if any(len(task) > MAX_TASK_FIELDS for task in task_list):
        return f"{label}中的单个步骤字段过多，最多允许 {MAX_TASK_FIELDS} 个"
    if any(any(not isinstance(key, str) for key in task) for task in task_list):
        return f"{label}包含非文本字段名的步骤"

    mappings = config.get("key_mappings", [])
    if not isinstance(mappings, list):
        return f"{label}的按键映射格式错误"
    mapping_list = cast(list[Any], mappings)
    if len(mapping_list) > MAX_KEY_MAPPINGS:
        return f"{label}包含 {len(mapping_list)} 个按键映射，超过上限 {MAX_KEY_MAPPINGS}"
    if any(not isinstance(mapping, dict) for mapping in mapping_list):
        return f"{label}包含格式错误的按键映射"
    if any(len(mapping) > MAX_KEY_MAPPING_FIELDS for mapping in mapping_list):
        return f"{label}中的单个按键映射字段过多，最多允许 {MAX_KEY_MAPPING_FIELDS} 个"
    if any(any(not isinstance(key, str) for key in mapping) for mapping in mapping_list):
        return f"{label}包含非文本字段名的按键映射"
    try:
        desired_mapping_count = int(
            float(config.get("key_mapping_count", len(mapping_list)))
        )
    except (TypeError, ValueError, OverflowError):
        # OverflowError: int(float("inf"/"1e309")) —— 异常数值字符串需在进入
        # 界面前被拒绝（ARCHITECTURE #61），与 workflow_analysis._integer /
        # validation.validate_task_list 的捕获模式一致。
        return f"{label}的按键映射数量设置无效"
    if not 0 <= desired_mapping_count <= MAX_KEY_MAPPINGS:
        return f"{label}的按键映射槽位数量必须在 0 到 {MAX_KEY_MAPPINGS} 之间"

    try:
        # json.dumps here generates a full-size string for structure validation.
        # This is NOT a hot path (only called on profile save/load), so the
        # temporary memory allocation is acceptable.
        # Note: Multiple json.dumps calls on the same payload are intentional.
        # This is a cold path (save/migrate), not a hot loop. The redundancy
        # ensures each validation stage operates on its own canonical form.
        serialized = json.dumps(config, ensure_ascii=False)
    except (RecursionError, TypeError, ValueError) as error:
        return f"{label}包含无法保存的数据：{error}"
    serialized_bytes = len(serialized.encode("utf-8"))
    if serialized_bytes > MAX_PROFILE_JSON_BYTES:
        return (
            f"{label}序列化后大小为 {serialized_bytes / 1024 / 1024:.1f} MB，"
            f"超过 {MAX_PROFILE_JSON_BYTES / 1024 / 1024:.0f} MB 上限"
        )
    return None
