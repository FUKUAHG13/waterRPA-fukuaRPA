"""Breakpoint preset interaction logic for per-step settings."""

import re


def infer_debug_preset(expression):
    source = str(expression or "").strip()
    if not source:
        return "每次经过都暂停", "1"
    loop_match = re.fullmatch(r"loop\s*>=\s*(\d+)", source)
    if loop_match:
        return "从第 N 次循环开始", loop_match.group(1)
    execution_match = re.fullmatch(r"execution_count\s*>=\s*(\d+)", source)
    if execution_match:
        return "本步骤执行 N 次后", execution_match.group(1)
    if source == "last_success":
        return "上一步成功时", "1"
    if source == "not last_success":
        return "上一步失败时", "1"
    return "高级表达式", "1"


class DebugSettingsController:
    def __init__(self, dialog):
        self.dialog = dialog

    def remember_custom_expression(self, text):
        host = self.dialog
        if host.debug_preset_combo.currentText() == "高级表达式":
            host._debug_custom_expression = str(text or "")

    def on_preset_changed(self, mode):
        host = self.dialog
        if host._debug_preset_mode == "高级表达式":
            host._debug_custom_expression = host.debug_condition_edit.text().strip()
        host._debug_preset_mode = str(mode or "")
        if host._debug_preset_mode == "高级表达式":
            host.debug_condition_edit.setText(host._debug_custom_expression)
        else:
            self.sync_condition_from_preset()
        self.update_preset_ui()

    def sync_condition_from_preset(self, _=None):
        host = self.dialog
        mode = host.debug_preset_combo.currentText()
        if mode == "高级表达式":
            return
        value = host.debug_preset_value_edit.text().strip() or "1"
        expressions = {
            "每次经过都暂停": "",
            "从第 N 次循环开始": f"loop >= {value}",
            "本步骤执行 N 次后": f"execution_count >= {value}",
            "上一步成功时": "last_success",
            "上一步失败时": "not last_success",
        }
        host.debug_condition_edit.setText(expressions.get(mode, ""))

    def update_preset_ui(self, _=None):
        host = self.dialog
        mode = host.debug_preset_combo.currentText()
        needs_value = mode in (
            "从第 N 次循环开始",
            "本步骤执行 N 次后",
        )
        advanced = mode == "高级表达式"
        host.debug_preset_value_group.setVisible(needs_value)
        host.debug_expression_label.setVisible(advanced)
        host.debug_expression_row.setVisible(advanced)
        self.update_breakpoint_ui()

    def update_breakpoint_ui(self, _=None):
        host = self.dialog
        enabled = host.debug_breakpoint_chk.isChecked()
        mode = host.debug_preset_combo.currentText()
        host.debug_preset_combo.setEnabled(enabled)
        host.debug_preset_value_edit.setEnabled(
            enabled and mode in ("从第 N 次循环开始", "本步骤执行 N 次后")
        )
        host.debug_condition_edit.setEnabled(enabled and mode == "高级表达式")
