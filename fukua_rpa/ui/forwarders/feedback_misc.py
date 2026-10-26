# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""Feedback/region/plugin/misc compatibility forwarders (batch 6).

Delegates to ``self.feedback_controller``, ``region_selection`` module,
``plugin_helpers`` module, QMessageBox/QToolTip helpers, and misc widget/state
accessors.
"""

from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QMessageBox, QToolTip

from .. import plugin_helpers, region_selection


class FeedbackMiscForwarderMixin:
    """Thin forwarders for feedback, region, plugin and misc (39 methods)."""

    # ------------------------------------------------------------------
    # Properties (run / recording controllers via getattr)
    # ------------------------------------------------------------------

    @property
    def worker(self):
        controller = getattr(self, "run_controller", None)
        return controller.worker if controller else None

    @property
    def recorder_ui(self):
        controller = getattr(self, "recording_controller", None)
        return controller.recorder if controller else None

    # ------------------------------------------------------------------
    # Feedback controller
    # ------------------------------------------------------------------

    def start_dodge_point_pick(self, point_index):
        """兼容转发 → feedback_controller.start_dodge_point_pick"""
        return self.feedback_controller.start_dodge_point_pick(point_index)

    def on_dodge_point_picked(self, point_index, value):
        """兼容转发 → feedback_controller.on_dodge_point_picked"""
        return self.feedback_controller.on_dodge_point_picked(point_index, value)

    def update_running_status_overlay(self, data):
        return self.feedback_controller.update_running_status_overlay(data)

    def hide_running_status_overlay(self, activity_name=None):
        return self.feedback_controller.hide_running_status_overlay(activity_name)

    # FR-20260809-D8-⑤: 由 main_window 内联收拢至本 mixin（与 update/hide 同域）。
    def show_running_status_overlay(self, position_name, activity_name="脚本"):
        return self.feedback_controller.show_running_status_overlay(
            position_name, activity_name
        )

    def collect_coordinate_click_preview_points(self, max_points=800):
        return self.feedback_controller.collect_coordinate_click_preview_points(
            max_points
        )

    def show_script_prompt(self, request):
        return self.feedback_controller.show_script_prompt(request)

    def cancel_active_script_prompt(self):
        return self.feedback_controller.cancel_active_script_prompt()

    def show_click_indicator_overlay(self, data):
        return self.feedback_controller.show_click_indicator_overlay(data)

    def test_task_image_recognition(self, task_row):
        return self.feedback_controller.test_task_image_recognition(task_row)

    def on_task_image_recognition_finished(self, result):
        return self.feedback_controller.on_task_image_recognition_finished(result)

    def show_all_coordinate_click_preview(self):
        return self.feedback_controller.show_all_coordinate_click_preview()

    def clear_all_coordinate_click_preview(self, *_):
        return self.feedback_controller.clear_all_coordinate_click_preview()

    def close_all_coordinate_click_preview(self):
        return self.feedback_controller.close_all_coordinate_click_preview()

    # ------------------------------------------------------------------
    # Task config dialogs
    # ------------------------------------------------------------------

    def unregister_task_config_dialog(self, dialog):
        self.task_config_dialogs.discard(dialog)

    # ------------------------------------------------------------------
    # Plugin helpers
    # ------------------------------------------------------------------

    def _plugin_usage_resolver(self, plugin_id: str):
        return plugin_helpers._plugin_usage_resolver(self, plugin_id)

    def init_scheduler_and_plugins(self):
        return plugin_helpers.init_scheduler_and_plugins(self)

    def _build_tools_menu(self):
        return plugin_helpers._build_tools_menu(self)

    def _list_profiles_for_scheduler(self):
        return plugin_helpers._list_profiles_for_scheduler(self)

    def _plugin_enabled_status(self, plugin_id):
        return plugin_helpers._plugin_enabled_status(self, plugin_id)

    # ------------------------------------------------------------------
    # Misc widget state
    # ------------------------------------------------------------------

    def update_multi_target_ui(self, _=None):
        enabled = self.multi_mode_combo.currentText() == "全部匹配"
        self.multi_order_combo.setEnabled(enabled)
        # 主顺序为随机顺序时第二条件不生效，组合框同步禁用以免误导。
        self.multi_order_b_combo.setEnabled(
            enabled and self.multi_order_combo.currentText() != "随机顺序"
        )
        self.multi_collect_first_edit.setEnabled(enabled)
        self.multi_collect_last_edit.setEnabled(enabled)
        self.multi_collect_max_edit.setEnabled(enabled)
        self.multi_collect_targets_edit.setEnabled(enabled)
        self.multi_collect_partial_chk.setEnabled(
            enabled and self._multi_collect_targets_value() > 0
        )

    def _multi_collect_targets_value(self):
        """Parse the target-count field leniently for UI enablement only."""

        try:
            return max(0, int(float(self.multi_collect_targets_edit.text().strip() or 0)))
        except (TypeError, ValueError, OverflowError):
            # OverflowError：inf/1e999 可被 float 接受但 int() 溢出；容错口径与
            # 运行前校验 parse_float_text 一致（FR-20261010-2701）。
            return 0

    def update_native_optimization_ui(self, _=None):
        enabled = self.native_core_chk.isChecked()
        self.native_parallel_combo.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Region selection
    # ------------------------------------------------------------------

    def normalize_region_list(self, regions):
        return region_selection.normalize_region_list(self, regions)

    def update_region_label(self):
        return region_selection.update_region_label(self)

    def apply_scan_region_config(self, scan_region=None, scan_regions=None):
        return region_selection.apply_scan_region_config(self, scan_region, scan_regions)

    def open_region_selector(self):
        return region_selection.open_region_selector(self)

    def open_multi_region_selector(self):
        return region_selection.open_multi_region_selector(self)

    def on_region_selected(self, rect_tuple):
        return region_selection.on_region_selected(self, rect_tuple)

    def on_regions_selected(self, rects):
        return region_selection.on_regions_selected(self, rects)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        return self.shutdown_controller.close_event(event)

    # ------------------------------------------------------------------
    # Message boxes / tooltips
    # ------------------------------------------------------------------

    def show_information_message(self, title, text):
        QMessageBox.information(self, title, text)

    def show_critical_message(self, title, text):
        QMessageBox.critical(self, title, text)

    def show_warning_message(self, title, text):
        QMessageBox.warning(self, title, text)

    def show_debug_pause_tooltip(self, text):
        QToolTip.showText(QCursor.pos(), text, self.debug_pause_btn)

    def show_start_tooltip(self, text):
        QToolTip.showText(QCursor.pos(), text, self.start_btn)

    def show_single_step_tooltip(self, text):
        QToolTip.showText(QCursor.pos(), text, self.single_step_btn)

    # ------------------------------------------------------------------
    # Runtime editing lock state
    # ------------------------------------------------------------------

    def set_runtime_editing_locked_state(self, locked):
        self._runtime_editing_locked = bool(locked)

    def is_runtime_editing_locked_state(self):
        return bool(self._runtime_editing_locked)

    def close_after_worker(self):
        return bool(self._close_after_worker)
