# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Load and collect workflow task mappings for an editable task row."""

import copy

from ..commands import COMMAND_BY_CODE, command_code, command_names
from ..constants import TASK_TYPE_MULTI_IMAGE_ACTION, TASK_TYPE_SCROLL, TASK_TYPE_UNTIL, TASK_TYPE_WAIT
from ..multi_image_actions import multi_image_action_summary
from ..task_model import TASK_DATA_DEFAULTS, until_condition_summary
from ..workflow_summary import unsupported_action_label as _unsupported_action_label


def apply_task_row_data(row, data):
    row.value_input.blockSignals(True)
    row.value_input.setText(str(data.get("value", "")))
    row.value_input.blockSignals(False)

    row.custom_data = {
        key: copy.deepcopy(data[key])
        if key in data
        else copy.deepcopy(default)
        for key, default in TASK_DATA_DEFAULTS.items()
    }
    for key, value in data.items():
        if key not in ("type", "value") and key not in row.custom_data:
            row.custom_data[key] = copy.deepcopy(value)
    row.update_markers()

    raw_task_type = data.get("type", 1.0)
    try:
        task_type = float(raw_task_type)
    except (TypeError, ValueError):
        task_type = None
    if task_type in COMMAND_BY_CODE:
        row._unsupported_task_type = None
        row._unsupported_task_label = ""
        row._unsupported_task_payload = None
        type_name = COMMAND_BY_CODE[task_type].name
        spec = COMMAND_BY_CODE[task_type]
        if row.type_combo.findText(type_name) < 0:
            row.type_combo.blockSignals(True)
            if spec.tool_only:
                # FR-20261006-004 / 维护者裁决 A1（方案 a 只读占位项）：连点器
                # （28.0）是 tool_only，从步骤类型下拉中排除，但 ``COMMAND_BY_CODE``
                # 仍保留它以便旧方案能加载运行。
                #
                # 旧代码在此**重建整个下拉**后 ``setCurrentText(type_name)``。
                # ``command_names`` 无条件排除 tool_only，``preserve_names`` 对它无效，
                # 所以匹配必然失败；而非可编辑下拉匹配不到时会**静默落回第一项**——
                # 用户只是打开设置再保存，磁盘上的 28.0 就被改写成 1.0，且界面
                # 显示与保存值长期不一致。
                #
                # 现在改为把该项作为**禁用占位项**加入：可见（与磁盘值一致）、
                # 不可选（用户不能把连点器当成新的步骤类型），参数原样保留。
                # 新建行不含该项——占位项只在加载旧方案时按需追加。
                row.type_combo.addItem(type_name)
                placeholder_index = row.type_combo.count() - 1
                row.type_combo.model().item(placeholder_index).setEnabled(False)
            else:
                row.type_combo.clear()
                row.type_combo.addItems(
                    command_names(
                        include_advanced=row.settings_mode == "advanced",
                        preserve_names=(type_name,),
                    )
                )
            row.type_combo.blockSignals(False)
        row._loading_data = True
        try:
            row.type_combo.setCurrentText(type_name)
        finally:
            row._loading_data = False
        if type_name == "直到条件成立":
            row.show_until_condition_summary()
        elif type_name == "多图动作映射":
            row.show_multi_image_action_summary()
        return

    # SAFETY: preserve an unknown action mapping and keep it visibly
    # unsupported. Silently coercing it to the combo's first item could turn an
    # imported future action into a real mouse click.
    label = _unsupported_action_label(raw_task_type)
    row._unsupported_task_type = copy.deepcopy(raw_task_type)
    row._unsupported_task_label = label
    row._unsupported_task_payload = copy.deepcopy(dict(data))
    row._loading_data = True
    row.type_combo.blockSignals(True)
    try:
        row.type_combo.clear()
        row.type_combo.addItem(label)
        row.type_combo.addItems(
            command_names(include_advanced=row.settings_mode == "advanced")
        )
        row.type_combo.setCurrentText(label)
    finally:
        row.type_combo.blockSignals(False)
        row._loading_data = False
    row.on_type_changed(label)


def collect_task_row_data(row):
    value = row.value_input.text()
    current_name = row.type_combo.currentText()
    unsupported = bool(
        row._unsupported_task_label
        and current_name == row._unsupported_task_label
    )
    if unsupported:
        data = copy.deepcopy(row._unsupported_task_payload or {})
        data["type"] = copy.deepcopy(row._unsupported_task_type)
        data["value"] = value
        data.update(
            {k: copy.deepcopy(v) for k, v in row.custom_data.items() if not k.startswith("_")}
        )
        return data

    task_type = command_code(current_name)
    if task_type is None:
        # A malformed combo state must fail validation instead of becoming a
        # real mouse click.
        task_type = current_name
    if task_type in [TASK_TYPE_WAIT, TASK_TYPE_SCROLL] and not value:
        value = "0"
    if task_type == TASK_TYPE_UNTIL:
        value = until_condition_summary(row.custom_data)
    elif task_type == TASK_TYPE_MULTI_IMAGE_ACTION:
        value = multi_image_action_summary(row.custom_data.get("multi_image_actions", []))

    data = {"type": task_type, "value": value}
    # INVARIANT: 下划线前缀键为 UI 内部草稿状态，不得进入执行/持久化数据
    data.update(
        {k: copy.deepcopy(v) for k, v in row.custom_data.items() if not k.startswith("_")}
    )
    return data
