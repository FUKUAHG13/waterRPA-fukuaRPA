# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Per-step configuration-dialog lifecycle for an editable task row."""

import copy
import logging

from PySide6.QtWidgets import QDialog

from ...task_model import parse_coordinate_text
from ..lazy_dialogs import load_task_config_dialog

logger = logging.getLogger(__name__)


class TaskConfigDialogController:
    def __init__(self, row):
        self.row = row

    def open(self):
        host = self.row
        if getattr(host, "config_dialog", None) and host.config_dialog.isVisible():
            self.refresh_context()
            host.config_dialog.show()
            host.config_dialog.raise_()
            host.config_dialog.activateWindow()
            return

        # 导入必须在写入任何对话框状态之前：失败时不得留下半初始化的编辑
        # 快照，否则 clear() 只在 finished 时才清，行会一直以为自己处于编辑中
        # （独立 B 审核第 4 项）。启动期不需要该模块（ADR 0040）。
        try:
            TaskConfigDialog = load_task_config_dialog()
        except Exception as error:  # noqa: BLE001 - 导入失败要如实记录并放弃本次打开
            logger.critical("步骤设置对话框模块导入失败：%s", error)
            return None

        host._config_dialog_touched_value = False
        host._config_edit_snapshot = host.task_snapshot()
        host._config_value_before_dialog_change = None
        host._config_dialog_last_value = None
        main_window = host.window()
        dialog = TaskConfigDialog(
            None,
            host.custom_data,
            host.image_settings_available(),
            host.point_limit_available(),
            host.coordinate_step_available(),
            host.direct_coordinate_tuple(),
            host.resolved_image_path(),
            host.image_click_point_available(),
            host.on_config_base_coordinate_changed,
            host.current_step_index(),
            host.type_combo.currentText(),
            getattr(main_window, "base_dir", None),
            settings_mode=(
                main_window.current_settings_mode()
                if hasattr(main_window, "current_settings_mode")
                else "simple"
            ),
            screenshot_settings_available=host.screenshot_settings_available(),
            step_value=host.value_input.text(),
        )
        host.config_dialog = dialog
        dialog.accepted.connect(lambda d=dialog: host.apply_custom_config(d))
        dialog.finished.connect(
            lambda result, d=dialog: host.clear_custom_config_dialog(d, result)
        )
        if hasattr(main_window, "register_task_config_dialog"):
            main_window.register_task_config_dialog(dialog)
        if hasattr(main_window, "apply_ui_scale_to_widget"):
            main_window.apply_ui_scale_to_widget(dialog)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def apply(self, dialog):
        host = self.row
        edit_snapshot = getattr(host, "_config_edit_snapshot", None)
        host.commit_task_snapshot(edit_snapshot)
        updated = copy.deepcopy(host.custom_data)
        data = dialog.get_data()
        # Preserve plugin action settings edited inside the plugin sub-dialog.
        # collect_task_settings() only reads UI controls and omits the plugin_*
        # keys (plugin_id/plugin_action_id/plugin_schema_version/plugin_config),
        # which PluginActionSettings writes into the dialog's working copy
        # (self.data). Re-merge them so the edit is not silently dropped.
        # FR-20260809-1001: value（插件子对话框写入的显示标签）同样不在
        # collect_task_settings 中，必须一并回填，否则步骤行显示空/陈旧标签。
        for _key in (
            "plugin_id",
            "plugin_action_id",
            "plugin_schema_version",
            "plugin_config",
            "value",
            "type",
        ):
            if _key in dialog.data:
                data[_key] = dialog.data[_key]
        updated.update(data)
        # 步骤级随机延迟 / 可选覆盖字段：清空字段时移除键，使引擎回退到全局设置。
        # FR-20260809-1002: task_settings_binding 对 click_spatial_jitter /
        # move_curve_strength / key_hold_duration 同样"非空才写入"，清空后若不移除
        # 键，updated.update(data) 会保留旧值（静默继续应用步骤级覆盖，与
        # "留空时沿用全局设置"的界面提示冲突）。
        for _jitter_key in (
            "move_jitter",
            "click_hld_jitter",
            "click_spatial_jitter",
            "move_curve_strength",
            "key_hold_duration",
        ):
            if _jitter_key not in data and _jitter_key in updated:
                del updated[_jitter_key]
        main_window = host.window()
        if hasattr(main_window, "resolve_task_reference_edits"):
            updated = main_window.resolve_task_reference_edits(host, updated)
        host.custom_data = updated
        if host.type_combo.currentText() == "直到条件成立":
            host.show_until_condition_summary()
        elif host.type_combo.currentText() == "多图动作映射":
            host.show_multi_image_action_summary()
        host.sync_data()
        host.log_committed_task_edit(edit_snapshot, "小齿轮设置")

    def refresh_context(self):
        host = self.row
        dialog = getattr(host, "config_dialog", None)
        if not dialog:
            return
        try:
            dialog.update_step_context(
                host.image_settings_available(),
                host.point_limit_available(),
                host.coordinate_step_available(),
                host.direct_coordinate_tuple(),
                host.resolved_image_path(),
                host.image_click_point_available(),
                host.current_step_index(),
                host.type_combo.currentText(),
                host.screenshot_settings_available(),
                host.value_input.text(),
            )
            if hasattr(dialog, "refresh_reference_numbers"):
                dialog.refresh_reference_numbers(host.custom_data)
        except RuntimeError:
            host.config_dialog = None

    def on_base_coordinate_changed(self, value):
        host = self.row
        if not getattr(host, "_config_dialog_touched_value", False):
            host._config_value_before_dialog_change = host.value_input.text()
        host._config_dialog_touched_value = True
        host._config_dialog_last_value = str(value)
        host.value_input.setText(str(value))
        if getattr(host, "config_dialog", None):
            host.config_dialog.base_coordinate = parse_coordinate_text(value)
            host.config_dialog.update_coord_step_ui()
        host.sync_data()

    def clear(self, dialog, result=None):
        host = self.row
        if result != QDialog.Accepted and getattr(
            host, "_config_dialog_touched_value", False
        ) and host.value_input.text() == str(
            getattr(host, "_config_dialog_last_value", "")
        ):
            host.value_input.setText(
                str(getattr(host, "_config_value_before_dialog_change", "") or "")
            )
            host.sync_data()
        for attr in [
            "_config_dialog_touched_value",
            "_config_value_before_dialog_change",
            "_config_dialog_last_value",
        ]:
            if hasattr(host, attr):
                delattr(host, attr)
        if hasattr(host, "_config_edit_snapshot"):
            del host._config_edit_snapshot
        if getattr(host, "config_dialog", None) is dialog:
            host.config_dialog = None
        main_window = host.window()
        if hasattr(main_window, "unregister_task_config_dialog"):
            main_window.unregister_task_config_dialog(dialog)
        dialog.deleteLater()

    def close(self):
        host = self.row
        dialog = getattr(host, "config_dialog", None)
        if dialog:
            try:
                dialog.close()
            except RuntimeError:
                host.config_dialog = None
