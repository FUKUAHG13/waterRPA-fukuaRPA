# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.

import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

# TODO: Split into focused test classes (ThemeTests, HotkeyTests, ProfileTests, etc.)
# when time permits. Current single-class structure is functional but hard to navigate.

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Tests involving engine execution, worker threads, or focus interaction
# hang in offscreen mode (no real event loop). Skip them in CI/headless.
_OFFSCREEN = os.environ.get("QT_QPA_PLATFORM", "") == "offscreen"

from PIL import Image
from PySide6.QtCore import QEvent, QPoint, QRect, QSettings, Qt
from PySide6.QtGui import QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolTip,
)

from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.config_store import profiles_signature
from fukua_rpa.constants import PROFILE_SCHEMA_VERSION
from fukua_rpa.log_policy import (
    LOG_ACTION,
    LOG_MODE_CUSTOM,
    LOG_MODE_DETAILED,
    LOG_RECOGNITION,
    LOG_TIMESTAMP,
)
from fukua_rpa.logging_service import GLOBAL_CONFIG
from fukua_rpa.paths import get_resource_path
from fukua_rpa.profile_package import build_profile_collection_payload
from fukua_rpa.profile_paths import MANAGED_DATA_PREFIX, resolve_profile_path
from fukua_rpa.ui.components import (
    HelpBtn,
    NoWheelComboBox,
    ReorderableComboBox,
    ResponsiveRow,
)
from fukua_rpa.ui.input_tools import KeyCaptureDialog, RegionWindow
from fukua_rpa.ui.main_window import RPAWindow, user32
from fukua_rpa.ui.overlays import ClickPointOverlay, RunningStatusOverlay
from fukua_rpa.ui.task_settings_dialog import TaskConfigDialog
from tests.qt_test_support import QtCallbackGuardMixin, create_test_window, dispose_widget


class ModernUiTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def create_window(self, base_dir):
        return create_test_window(self, base_dir)

    def test_click_indicator_uses_a_small_non_activating_window(self):
        overlay = ClickPointOverlay(100, 100, text="左键单击", duration_ms=5000)
        self.addCleanup(dispose_widget, self.app, overlay)
        self.app.processEvents()

        point = overlay.to_screen_point()
        self.assertLessEqual(overlay.width(), 220)
        self.assertEqual(overlay.height(), 64)
        self.assertTrue(0 <= point.x() < overlay.width())
        self.assertTrue(0 <= point.y() < overlay.height())
        self.assertTrue(bool(overlay.windowFlags() & Qt.WindowDoesNotAcceptFocus))
        self.assertTrue(overlay.testAttribute(Qt.WA_TransparentForMouseEvents))

    def test_running_status_overlay_does_not_accept_focus(self):
        overlay = RunningStatusOverlay()
        self.addCleanup(dispose_widget, self.app, overlay)
        self.assertTrue(bool(overlay.windowFlags() & Qt.WindowDoesNotAcceptFocus))
        self.assertTrue(overlay.testAttribute(Qt.WA_ShowWithoutActivating))

    def test_running_status_overlay_can_identify_the_auto_clicker(self):
        overlay = RunningStatusOverlay()
        self.addCleanup(dispose_widget, self.app, overlay)
        overlay.start_overlay("右上角", "连点器")
        overlay.set_status({"cmd": "鼠标左键连点", "detail": "按 F7 停止"})

        text = overlay.label.text()
        self.assertIn("连点器正在执行中", text)
        self.assertIn("鼠标左键连点", text)
        self.assertIn("按 F7 停止", text)

    def test_main_workspace_uses_modern_design_roles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertTrue(self.app.property("_fukua_modern_theme"))
            self.assertIn('QPushButton[variant="primary"]', self.app.styleSheet())
            self.assertEqual(window.centralWidget().objectName(), "appRoot")
            self.assertEqual(window.task_list.objectName(), "taskList")
            self.assertEqual(window.log_text.objectName(), "logView")
            self.assertEqual(window.start_btn.property("variant"), "success")
            self.assertEqual(window.stop_btn.property("variant"), "danger")
            self.assertGreaterEqual(window.minimumWidth(), 760)
            self.assertGreaterEqual(window.minimumHeight(), 620)
            self.assertTrue(
                bool(
                    window.settings_dialog.windowFlags()
                    & Qt.WindowMinimizeButtonHint
                )
            )
            self.assertTrue(
                bool(
                    window.settings_dialog.windowFlags()
                    & Qt.WindowMaximizeButtonHint
                )
            )

    def test_main_window_minimum_width_covers_every_fixed_command_row(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.show()
            self.app.processEvents()

            required = window.refresh_main_window_minimum_width()
            central_layout = window.centralWidget().layout()
            margins = central_layout.contentsMargins()
            content_width = max(
                window.profile_bar.minimumSizeHint().width(),
                window.command_bar.minimumSizeHint().width(),
                window.status_bar.minimumSizeHint().width(),
                window.run_controls_layout.minimumSize().width(),
            )
            self.assertGreaterEqual(
                required,
                content_width + margins.left() + margins.right(),
            )

            window.resize(1, window.height())
            self.app.processEvents()
            self.assertGreaterEqual(window.width(), required)
            self.assertGreaterEqual(
                window.command_bar.width(),
                window.command_bar.minimumSizeHint().width(),
            )

    def test_resource_status_is_always_single_line(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("advanced")
            )
            window.window_performance_controller._last_resource_sample.update(
                {
                    "system_usage": "100.0",
                    "process_usage": "999.9",
                    "source_label": "独立 Runner",
                }
            )
            window.window_performance_controller.refresh_resource_label_for_mode()

            self.assertFalse(window.cpu_label.wordWrap())
            self.assertNotIn("\n", window.cpu_label.text())
            self.assertGreaterEqual(
                window.minimumWidth(),
                window.status_bar.minimumSizeHint().width()
                + window.centralWidget().layout().contentsMargins().left()
                + window.centralWidget().layout().contentsMargins().right(),
            )

    def test_profile_commands_normalize_names_and_confirm_deletion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            original = window.current_profile_name
            with mock.patch.object(
                QInputDialog,
                "getText",
                return_value=("  临时方案  ", True),
            ):
                window.profile_controller.create_new_profile()
            self.assertEqual(window.current_profile_name, "临时方案")
            self.assertEqual(window.profile_combo.currentText(), "临时方案")

            with mock.patch.object(
                QMessageBox, "question", return_value=QMessageBox.No
            ):
                window.profile_controller.delete_current_profile()
            self.assertIn("临时方案", window.profiles_data)

            with mock.patch.object(
                QMessageBox, "question", return_value=QMessageBox.Yes
            ):
                window.profile_controller.delete_current_profile()
            self.assertNotIn("临时方案", window.profiles_data)
            self.assertEqual(window.current_profile_name, original)

    def test_new_profile_dialog_suggests_first_unused_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            with mock.patch.object(
                QInputDialog,
                "getText",
                side_effect=[("新建方案1", True), ("新建方案2", True)],
            ) as get_text:
                window.profile_controller.create_new_profile()
                window.profile_controller.create_new_profile()

            self.assertEqual(
                get_text.call_args_list[0].args[-1],
                "新建方案1",
            )
            self.assertEqual(
                get_text.call_args_list[1].args[-1],
                "新建方案2",
            )
            self.assertIn("新建方案1", window.profiles_data)
            self.assertIn("新建方案2", window.profiles_data)

    def test_settings_button_restores_minimized_dialog_without_losing_maximize(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            dialog = window.settings_dialog
            dialog.showMaximized()
            self.app.processEvents()
            dialog.showMinimized()
            self.app.processEvents()

            self.assertTrue(dialog.windowState() & Qt.WindowMinimized)
            self.assertTrue(dialog.windowState() & Qt.WindowMaximized)
            window.show_settings_dialog()
            self.app.processEvents()

            self.assertTrue(dialog.isVisible())
            self.assertFalse(dialog.windowState() & Qt.WindowMinimized)
            self.assertTrue(dialog.windowState() & Qt.WindowMaximized)

    def test_profile_save_failure_remains_visible_without_modal_loop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertTrue(window.profile_save_status_label.isHidden())
            window.profile_controller.mark_dirty(force=True)
            with mock.patch(
                "fukua_rpa.ui.controllers.profile_controller.persist_profiles",
                side_effect=OSError("disk unavailable"),
            ):
                self.assertFalse(window.persist_profiles_state(force=True))
            self.assertFalse(window.profile_save_status_label.isHidden())
            self.assertEqual(window.profile_save_status_label.text(), "方案未保存")
            self.assertIn("OSError", window.profile_save_status_label.toolTip())

    def test_simple_settings_mode_hides_only_advanced_controls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertEqual(window.settings_mode_combo.currentData(), "simple")
            self.assertTrue(all(widget.isHidden() for widget in window.advanced_setting_widgets))

            window.native_core_chk.setChecked(False)
            window.apply_settings_mode()
            self.assertIn("高级设置", window.advanced_settings_notice.text())

            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("advanced")
            )
            self.app.processEvents()
            self.assertTrue(all(not widget.isHidden() for widget in window.advanced_setting_widgets))

    def test_log_privacy_defaults_on_and_machine_local_opt_out_is_visible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertTrue(window.log_privacy_chk.isChecked())
            self.assertTrue(window.log_privacy_status_label.isHidden())
            self.assertTrue(GLOBAL_CONFIG["privacy_protected"])

            with mock.patch.object(
                QMessageBox, "warning", return_value=QMessageBox.Cancel
            ):
                window.log_privacy_chk.setChecked(False)
            self.assertTrue(window.log_privacy_chk.isChecked())
            self.assertTrue(GLOBAL_CONFIG["privacy_protected"])

            with mock.patch.object(
                QMessageBox, "warning", return_value=QMessageBox.Yes
            ):
                window.log_privacy_chk.setChecked(False)

            self.assertFalse(GLOBAL_CONFIG["privacy_protected"])
            self.assertFalse(
                window.settings.value(
                    "log_privacy_protected", True, type=bool
                )
            )
            self.assertFalse(window.log_privacy_status_label.isHidden())
            self.assertIn("隐私保护已关闭", window.advanced_settings_notice.text())

            window.log_level_combo.setCurrentIndex(
                window.log_level_combo.findData(LOG_MODE_DETAILED)
            )
            window.log_text.clear()
            window.append_application_log(
                "protected setting summary",
                private_msg="ordinary setting value",
            )
            self.assertIn("ordinary setting value", window.log_text.toPlainText())

            window.log_privacy_chk.setChecked(True)
            self.assertTrue(GLOBAL_CONFIG["privacy_protected"])
            window.log_text.clear()
            window.append_application_log(
                "protected setting summary",
                private_msg="ordinary setting value",
            )
            self.assertIn("protected setting summary", window.log_text.toPlainText())
            self.assertNotIn("ordinary setting value", window.log_text.toPlainText())

    def test_requested_settings_groups_are_advanced_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            advanced_widgets = (
                window.dodge_section,
                window.multi_target_section,
                window.credential_section,
                window.workflow_settings_section,
                window.diagnostic_settings_section,
                window.start_step_group,
                window.loop_range_group,
            )
            self.assertTrue(all(widget.isHidden() for widget in advanced_widgets))

            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("advanced")
            )
            self.app.processEvents()
            self.assertTrue(all(not widget.isHidden() for widget in advanced_widgets))

    def test_system_controls_are_split_into_named_categories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)

            self.assertEqual(window.system_settings_section.title, "系统与快捷键")
            self.assertEqual(window.display_settings_section.title, "界面与提示")
            self.assertEqual(
                window.workflow_settings_section.title, "运行范围与流程"
            )
            self.assertEqual(
                window.diagnostic_settings_section.title, "日志与诊断"
            )
            self.assertTrue(
                window.workflow_settings_section.isAncestorOf(
                    window.start_step_edit
                )
            )
            self.assertTrue(
                window.diagnostic_settings_section.isAncestorOf(
                    window.isolated_runner_chk
                )
            )

    def test_command_picker_filters_advanced_actions_without_changing_old_steps(self):
        advanced_commands = {
            "右键拖拽",
            "鼠标悬停",
            "设置变量",
            "判断表达式",
            "等待窗口",
            "激活窗口",
            "关闭窗口",
            "输入秘密文本",
            "点击窗口控件",
            "设置控件文本",
            "读取控件文本",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row({"type": 1.0, "value": "10,20"})
            simple_row = window.task_list.itemWidget(window.task_list.item(0))
            simple_options = {
                simple_row.type_combo.itemText(index)
                for index in range(simple_row.type_combo.count())
            }
            self.assertTrue(advanced_commands.isdisjoint(simple_options))
            self.assertNotIn("启动程序", simple_options)
            self.assertIn("直到条件成立", simple_options)

            window.add_row({"type": 16.0, "value": "count = 1"})
            existing_row = window.task_list.itemWidget(window.task_list.item(1))
            self.assertEqual(existing_row.type_combo.currentText(), "设置变量")
            self.assertEqual(existing_row.get_data()["type"], 16.0)
            preserved_options = {
                existing_row.type_combo.itemText(index)
                for index in range(existing_row.type_combo.count())
            }
            self.assertIn("设置变量", preserved_options)
            self.assertNotIn("判断表达式", preserved_options)

            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("advanced")
            )
            self.app.processEvents()
            advanced_options = {
                existing_row.type_combo.itemText(index)
                for index in range(existing_row.type_combo.count())
            }
            self.assertTrue(advanced_commands.issubset(advanced_options))

            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("simple")
            )
            self.app.processEvents()
            self.assertEqual(existing_row.type_combo.currentText(), "设置变量")
            self.assertEqual(existing_row.get_data()["type"], 16.0)

    def test_step_dialog_moves_requested_controls_to_advanced_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            dialog = TaskConfigDialog(
                None,
                {
                    "coord_step_en": True,
                    "coord_step_direction": "向下",
                },
                coordinate_step_available=True,
                image_settings_available=True,
                point_limit_available=True,
                base_coordinate=(100, 100),
                base_dir=temp_dir,
                settings_mode="simple",
            )
            self.addCleanup(dispose_widget, self.app, dialog)
            self.assertFalse(dialog.control_form.isRowVisible(dialog.step_loop_row))
            self.assertFalse(dialog.image_target_form.isRowVisible(dialog.point_limit_row))
            self.assertFalse(dialog.image_target_form.isRowVisible(dialog.step_region_row))
            self.assertFalse(dialog.coordinate_target_form.isRowVisible(dialog.coord_reset_row))
            self.assertFalse(dialog.control_form.isRowVisible(dialog.run_max_row))
            self.assertTrue(dialog.debug_section.isHidden())
            self.assertEqual(dialog.coord_step_direction_combo.currentText(), "向下")
            self.assertFalse(
                dialog.coordinate_target_form.isRowVisible(dialog.coord_every_row)
            )
            self.assertTrue(
                dialog.coordinate_target_form.isRowVisible(dialog.coord_sequence_box)
            )
            self.assertEqual(
                dialog.coordinate_target_form.labelForField(dialog.coord_sequence_box).text(),
                "自定义点位:",
            )

            dialog.apply_settings_mode("advanced")
            self.assertTrue(dialog.control_form.isRowVisible(dialog.step_loop_row))
            self.assertFalse(dialog.image_target_form.isRowVisible(dialog.point_limit_row))
            self.assertFalse(dialog.image_target_form.isRowVisible(dialog.step_region_row))
            self.assertTrue(dialog.coordinate_target_form.isRowVisible(dialog.coord_reset_row))
            self.assertTrue(dialog.control_form.isRowVisible(dialog.run_max_row))
            self.assertFalse(dialog.debug_section.isHidden())
            directions = {
                dialog.coord_step_direction_combo.itemText(index)
                for index in range(dialog.coord_step_direction_combo.count())
            }
            self.assertEqual(directions, set(TaskConfigDialog.COORD_STEP_DIRECTIONS))
            self.assertTrue(dialog.coord_step_direction_combo.isEnabled())

            dialog.coord_step_chk.setChecked(False)
            dialog.apply_settings_mode("simple")
            self.assertEqual(
                dialog.coord_step_direction_combo.count(),
                len(TaskConfigDialog.COORD_STEP_DIRECTIONS),
            )
            self.assertEqual(dialog.coord_step_direction_combo.currentText(), "向下")
            self.assertFalse(
                dialog.coordinate_target_form.isRowVisible(dialog.coord_every_row)
            )

    def test_screenshot_step_exposes_an_independent_capture_region(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            dialog = TaskConfigDialog(
                None,
                {
                    "capture_region_mode": "custom",
                    "capture_region": "10,20,300,200",
                },
                step_type="截图保存",
                base_dir=temp_dir,
                settings_mode="simple",
                screenshot_settings_available=True,
            )
            self.addCleanup(dispose_widget, self.app, dialog)

            self.assertTrue(
                dialog.control_form.isRowVisible(dialog.capture_region_row)
            )
            self.assertEqual(
                dialog.capture_region_mode_combo.currentData(), "custom"
            )
            self.assertTrue(dialog.capture_region_edit.isEnabled())
            self.assertEqual(
                dialog.get_data()["capture_region"], "10,20,300,200"
            )

            dialog.update_step_context(
                step_type="等待(秒)", screenshot_settings_available=False
            )
            self.assertFalse(
                dialog.control_form.isRowVisible(dialog.capture_region_row)
            )
            self.assertEqual(
                dialog.get_data()["capture_region"], "10,20,300,200"
            )

    def test_mode_switch_preserves_unsaved_step_dialog_draft(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("advanced")
            )
            window.add_row(
                {
                    "type": 1.0,
                    "value": "100,100",
                    "coord_step_direction": "向上",
                    "repeat_count": "1",
                }
            )
            row = window.task_list.itemWidget(window.task_list.item(0))
            row.open_custom_config()
            dialog = row.config_dialog
            dialog.repeat_combo.setCurrentText("指定次数")
            dialog.repeat_count_edit.setText("7")
            dialog.success_jump_edit.setText("1")
            dialog.step_disabled_chk.setChecked(True)
            dialog.debug_breakpoint_chk.setChecked(True)

            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("simple")
            )
            self.app.processEvents()
            self.assertEqual(dialog.repeat_count_edit.text(), "7")
            self.assertEqual(dialog.success_jump_edit.text(), "1")
            self.assertTrue(dialog.step_disabled_chk.isChecked())
            self.assertTrue(dialog.debug_breakpoint_chk.isChecked())
            self.assertEqual(dialog.coord_step_direction_combo.currentText(), "向上")
            self.assertTrue(dialog.debug_section.isHidden())
            self.assertFalse(row.custom_data["step_disabled"])

            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("advanced")
            )
            self.app.processEvents()
            self.assertEqual(dialog.repeat_count_edit.text(), "7")
            self.assertEqual(dialog.success_jump_edit.text(), "1")
            dialog.accept()
            self.app.processEvents()
            self.assertEqual(row.custom_data["repeat_count"], "7")
            self.assertEqual(row.custom_data["success_jump"], "1")
            self.assertTrue(row.custom_data["step_disabled"])
            self.assertTrue(row.custom_data["debug_breakpoint"])

    def test_task_value_edit_participates_in_undo_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row({"type": 1.0, "value": "10,10"})
            window.undo_stack.clear()
            window.redo_stack.clear()
            row = window.task_list.itemWidget(window.task_list.item(0))
            window.show()
            row.value_input.setFocus()
            self.app.processEvents()
            row.value_input.selectAll()
            QTest.keyClicks(row.value_input, "20,20")
            QTest.keyClick(row.value_input, Qt.Key_Return)
            self.app.processEvents()

            self.assertEqual(row.value_input.text(), "20,20")
            self.assertEqual(len(window.undo_stack), 1)
            window.undo_task_change()
            restored = window.task_list.itemWidget(window.task_list.item(0))
            self.assertEqual(restored.value_input.text(), "10,10")

    def test_cut_paste_undo_and_redo_are_atomic_and_logged(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            previous_log_to_ui = GLOBAL_CONFIG["log_to_ui"]
            self.addCleanup(
                GLOBAL_CONFIG.__setitem__, "log_to_ui", previous_log_to_ui
            )
            GLOBAL_CONFIG["log_to_ui"] = True
            window.add_row({"type": 1.0, "value": "10,10"})
            window.add_row({"type": 1.0, "value": "20,20"})
            window.undo_stack.clear()
            window.redo_stack.clear()
            window.log_text.clear()
            window.task_list.setCurrentRow(0)
            window.show()
            window.task_list.setFocus()
            self.app.processEvents()

            QTest.keyClick(window.task_list, Qt.Key_X, Qt.ControlModifier)
            self.app.processEvents()
            self.assertEqual(window.task_list.count(), 1)
            self.assertEqual(len(window.undo_stack), 1)
            window.paste_row_after_selected()
            self.assertEqual(window.task_list.count(), 2)
            window.undo_task_change()
            self.assertEqual(window.task_list.count(), 1)
            window.redo_task_change()
            self.assertEqual(window.task_list.count(), 2)

            text = window.log_text.toPlainText()
            self.assertIn("已剪切原第 1 步", text)
            self.assertIn("已粘贴为第 2 步", text)
            self.assertIn("已撤销", text)
            self.assertIn("已重做", text)

    def test_complete_log_records_committed_task_edits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            previous_log_to_ui = GLOBAL_CONFIG["log_to_ui"]
            self.addCleanup(
                GLOBAL_CONFIG.__setitem__, "log_to_ui", previous_log_to_ui
            )
            GLOBAL_CONFIG["log_to_ui"] = True
            window.log_level_combo.setCurrentIndex(
                window.log_level_combo.findData("complete")
            )
            window.add_row({"type": 1.0, "value": "10,10"})
            row = window.task_list.itemWidget(window.task_list.item(0))
            window.show()
            window.log_text.clear()

            row.value_input.setFocus()
            self.app.processEvents()
            row.value_input.setText("20,20")
            QTest.keyClick(row.value_input, Qt.Key_Return)
            row.open_custom_config()
            dialog = row.config_dialog
            dialog.repeat_combo.setCurrentText("指定次数")
            dialog.repeat_count_edit.setText("3")
            dialog.accept()
            self.app.processEvents()

            text = window.log_text.toPlainText()
            self.assertIn("已保存步骤内容修改", text)
            self.assertIn("已保存小齿轮设置修改", text)

    def test_complete_log_audits_settings_dialog_session_on_hide(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.log_level_combo.setCurrentIndex(
                window.log_level_combo.findData("complete")
            )
            window.log_privacy_chk.setChecked(True)
            window.log_text.clear()

            window.show_settings_dialog()
            self.app.processEvents()
            window.detect_delay.setText("0.37")
            window.close_settings_dialog()
            self.app.processEvents()

            text = window.log_text.toPlainText()
            self.assertIn("全局设置会话已保存", text)
            self.assertIn("detect_delay", text)
            self.assertNotIn("0.37", text)

    def test_secret_text_requires_an_existing_local_credential(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            task = {"type": 22.0, "value": "account"}
            cfg = window.get_current_ui_config()
            self.assertIn("不存在", window.workflow_review_controller.validate_tasks([task], cfg))

            window.credentials.set("account", "secret")
            self.assertIsNone(window.workflow_review_controller.validate_tasks([task], cfg))

    def test_task_rows_keep_content_and_selection_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row({"type": 1.0, "value": "100,200"})
            row = window.task_list.itemWidget(window.task_list.item(0))
            self.assertEqual(row.objectName(), "taskRow")
            self.assertEqual(row.value_input.text(), "100,200")
            row.set_selected(True)
            self.assertTrue(row.property("selected"))
            self.assertFalse(row.cfg_btn.icon().isNull())
            self.assertFalse(row.del_btn.icon().isNull())
            self.assertEqual(window.task_count_label.text(), "1 步")

    def test_task_metadata_multiselect_and_editor_hiding_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            for index in range(3):
                window.add_row(
                    {"type": 1.0, "value": f"{index},{index}"},
                    record_undo=False,
                    select=False,
                )
            first = window.task_list.item(0)
            third = window.task_list.item(2)
            window.task_list.select_item_with_modifiers(first, Qt.NoModifier)
            window.task_list.select_item_with_modifiers(
                third, Qt.ControlModifier
            )
            self.assertEqual(window.selected_row_indexes(), [0, 2])

            window.workflow_editor._apply_task_updates(
                [0, 2],
                {"step_note": "复核这两步", "marker_color": "orange"},
                "测试",
            )
            snapshot = window.snapshot_tasks()
            self.assertEqual(snapshot[0]["step_note"], "复核这两步")
            self.assertEqual(snapshot[2]["marker_color"], "orange")
            first_row = window.ensure_task_item_widget(first)
            self.assertEqual(first_row.property("markerColor"), "orange")
            self.assertEqual(first_row.toolTip(), "复核这两步")
            self.assertTrue(first_row.note_display.isHidden())

            window.show_step_notes_chk.setChecked(True)
            window.show()
            self.app.processEvents()
            self.assertFalse(first_row.note_display.isHidden())
            self.assertEqual(first_row.note_display.text(), "复核这两步")
            self.assertGreater(
                first_row.value_input.width(),
                first_row.note_display.width(),
            )

            advanced_index = window.settings_mode_combo.findData("advanced")
            window.settings_mode_combo.setCurrentIndex(advanced_index)
            window.apply_settings_mode()
            window.task_list.select_item_with_modifiers(first, Qt.NoModifier)
            window.task_list.select_item_with_modifiers(
                third, Qt.ControlModifier
            )
            self.assertEqual(window.workflow_editor.set_selected_hidden(True), 2)
            self.assertTrue(first.isHidden())
            self.assertTrue(third.isHidden())
            self.assertEqual(len(window.snapshot_tasks()), 3)
            self.assertIn("隐藏 2", window.task_count_label.text())

            window.workflow_editor.set_show_hidden_steps(True)
            self.assertFalse(first.isHidden())
            self.assertFalse(third.isHidden())
            self.assertTrue(
                window.ensure_task_item_widget(first).property("editorHidden")
            )
            effect = window.ensure_task_item_widget(first).graphicsEffect()
            self.assertIsNotNone(effect)
            self.assertTrue(effect.isEnabled())
            self.assertLess(effect.opacity(), 0.5)

    def test_image_action_button_consumes_note_width_not_value_width(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "target.png")
            Image.new("RGB", (12, 12), "white").save(image_path)
            window = self.create_window(temp_dir)
            window.add_row(
                {"type": 1.0, "value": "100,200", "step_note": "坐标备注"},
                record_undo=False,
                select=False,
            )
            window.add_row(
                {"type": 1.0, "value": image_path, "step_note": "图片备注"},
                record_undo=False,
                select=False,
            )
            window.show_step_notes_chk.setChecked(True)
            window.show()
            window.resize(max(window.width(), 1100), window.height())
            self.app.processEvents()

            coordinate_row = window.ensure_task_item_widget(
                window.task_list.item(0)
            )
            image_row = window.ensure_task_item_widget(window.task_list.item(1))
            self.app.processEvents()

            self.assertFalse(image_row.recognition_test_btn.isHidden())
            self.assertLessEqual(
                abs(
                    coordinate_row.value_input.width()
                    - image_row.value_input.width()
                ),
                2,
            )
            self.assertLess(
                image_row.note_display.width(),
                coordinate_row.note_display.width(),
            )

    def test_task_gear_hidden_state_is_advanced_only_and_round_trips(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            dialog = TaskConfigDialog(
                window,
                {"editor_hidden": True},
                settings_mode="advanced",
                base_dir=temp_dir,
            )
            self.addCleanup(dispose_widget, self.app, dialog)
            self.assertTrue(dialog.editor_hidden_chk.isChecked())
            self.assertFalse(dialog.editor_hidden_chk.isHidden())
            self.assertTrue(dialog.get_data()["editor_hidden"])
            self.assertTrue(
                bool(dialog.windowFlags() & Qt.WindowMinimizeButtonHint)
            )
            self.assertTrue(
                bool(dialog.windowFlags() & Qt.WindowMaximizeButtonHint)
            )

            dialog.apply_settings_mode("simple")
            self.assertTrue(dialog.editor_hidden_chk.isHidden())
            self.assertTrue(dialog.get_data()["editor_hidden"])

    def test_screen_template_capture_saves_and_fills_image_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row({"type": 1.0, "value": ""})
            row = window.task_list.itemWidget(window.task_list.item(0))
            self.assertFalse(row.template_capture_btn.isHidden())
            window.log_level_combo.setCurrentIndex(
                window.log_level_combo.findData("complete")
            )
            previous_privacy = GLOBAL_CONFIG["privacy_protected"]
            self.addCleanup(
                GLOBAL_CONFIG.__setitem__, "privacy_protected", previous_privacy
            )
            GLOBAL_CONFIG["privacy_protected"] = True
            window.undo_stack.clear()
            window.log_text.clear()

            template = Image.new("RGB", (16, 16), "white")
            for offset in range(16):
                template.putpixel((offset, offset), (0, 0, 0))
                template.putpixel((15 - offset, offset), (37, 99, 235))
            with mock.patch("PIL.ImageGrab.grab", return_value=template) as grab:
                row.save_template_capture((10, 20, 16, 16))
                # The capture runs on a background thread and reports back through
                # a queued signal. Wait (bounded) for the undo record to land while
                # the grab mock is still active, so the background thread's grab call
                # is observed by the mock.
                deadline = time.monotonic() + 5.0
                while not window.undo_stack and time.monotonic() < deadline:
                    self.app.processEvents()
            self.app.processEvents()

            saved_path = row.value_input.text()
            self.assertEqual(len(window.undo_stack), 1)
            self.assertEqual(
                window.log_text.toPlainText().count(
                    "第 1 步已保存快捷截图模板修改"
                ),
                1,
            )
            self.assertNotIn(saved_path, window.log_text.toPlainText())
            self.assertTrue(saved_path.startswith(MANAGED_DATA_PREFIX))
            self.assertTrue(
                os.path.isfile(resolve_profile_path(saved_path, temp_dir))
            )
            self.assertEqual(
                os.path.dirname(resolve_profile_path(saved_path, temp_dir)),
                os.path.join(temp_dir, "templates"),
            )
            self.assertFalse(row.recognition_test_btn.isHidden())
            with mock.patch.object(window, "test_task_image_recognition") as test_once:
                row.recognition_test_btn.click()
                test_once.assert_called_once_with(row)
            grab.assert_called_once_with(
                bbox=(10, 20, 26, 36),
                all_screens=True,
            )

            row.value_input.setText("10,20")
            self.assertTrue(row.recognition_test_btn.isHidden())

            row.type_combo.setCurrentText("等待(秒)")
            self.assertTrue(row.template_capture_btn.isHidden())

    def test_transparent_template_selection_warns_only_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row({"type": 1.0, "value": ""})
            row = window.task_list.itemWidget(window.task_list.item(0))
            image_path = os.path.join(temp_dir, "transparent.png")
            template = Image.new("RGBA", (12, 12), (255, 255, 255, 0))
            for offset in range(3, 9):
                template.putpixel((offset, offset), (20, 90, 220, 255))
                template.putpixel((11 - offset, offset), (230, 80, 40, 255))
            template.save(image_path)

            with mock.patch.object(
                QFileDialog,
                "getOpenFileName",
                return_value=(image_path, "Images (*.png)"),
            ), mock.patch.object(QMessageBox, "information") as information:
                row.select_file()
                row.select_file()

            self.assertEqual(information.call_count, 1)
            self.assertIn("透明图片", information.call_args.args[1])
            self.assertTrue(
                window.settings.value(
                    "onboarding/transparent_template_warning_v1_seen",
                    False,
                    type=bool,
                )
            )

    def test_until_summary_is_read_only_muted_and_draft_restored_on_type_change(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row({"type": 15.0, "value": "旧摘要"})
            row = window.task_list.itemWidget(window.task_list.item(0))

            self.assertEqual(row.type_combo.currentText(), "直到条件成立")
            self.assertTrue(row.value_input.isReadOnly())
            self.assertTrue(row.value_input.property("generatedSummary"))
            self.assertIn("请点击右侧小齿轮修改", row.value_input.text())

            row.set_data({"type": 15.0, "value": "另一个旧摘要"})
            self.assertIn("请点击右侧小齿轮修改", row.value_input.text())

            row.type_combo.setCurrentText("左键单击")
            self.assertFalse(row.value_input.isReadOnly())
            self.assertFalse(row.value_input.property("generatedSummary"))
            # P1-2 fix: draft is restored instead of cleared
            self.assertEqual(row.value_input.text(), "另一个旧摘要")

            row.set_data({"type": 1.0, "value": "120,240"})
            self.assertEqual(row.type_combo.currentText(), "左键单击")
            self.assertEqual(row.value_input.text(), "120,240")

    def test_breakpoint_and_expression_controls_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("advanced")
            )
            window.add_row(
                {
                    "type": 16.0,
                    "value": "count = 1",
                    "debug_breakpoint": True,
                    "debug_condition": "loop >= 2",
                }
            )
            item = window.task_list.item(0)
            row = window.task_list.itemWidget(item)
            self.assertEqual(row.type_combo.currentText(), "设置变量")
            self.assertTrue(row.has_breakpoint())
            self.assertFalse(row.breakpoint_mark_label.isHidden())
            self.assertTrue(row.get_data()["debug_breakpoint"])
            self.assertEqual(row.get_data()["debug_condition"], "loop >= 2")
            self.assertIn("loop >= 2", row.breakpoint_mark_label.toolTip())

            window.task_list.setCurrentItem(item)
            self.assertTrue(window.toggle_selected_breakpoint())
            self.assertFalse(row.has_breakpoint())
            self.assertTrue(row.breakpoint_mark_label.isHidden())
            self.assertFalse(row.get_data()["debug_breakpoint"])
            self.assertEqual(row.get_data()["debug_condition"], "loop >= 2")

            single_tasks, _config = window.build_single_step_request(
                {"type": 17.0, "value": "loop == 1", "debug_breakpoint": True},
                window.get_current_ui_config(),
            )
            self.assertFalse(single_tasks[0]["debug_breakpoint"])
            self.assertFalse(window.debug_pause_btn.isEnabled())
            self.assertFalse(window.debug_continue_btn.isEnabled())
            self.assertFalse(window.debug_next_btn.isEnabled())

    @unittest.skipIf(_OFFSCREEN, "requires real event loop for worker thread")
    def test_worker_breakpoint_flow_updates_real_debug_controls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("advanced")
            )
            window.add_row(
                {
                    "type": 16.0,
                    "value": "counter = 1",
                    "debug_breakpoint": True,
                }
            )
            window.start_task()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not window.debug_continue_btn.isEnabled():
                self.app.processEvents()
                QTest.qWait(10)
            self.assertTrue(window.debug_continue_btn.isEnabled())
            self.assertTrue(window.debug_next_btn.isEnabled())
            self.assertEqual(window.debug_paused_step, 1)
            self.assertEqual(window.debug_variable_values.get("step"), 1)
            row = window.task_list.itemWidget(window.task_list.item(0))
            self.assertTrue(row.property("debugPaused"))

            window.continue_debug_run()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and window.run_controller.is_active:
                self.app.processEvents()
                QTest.qWait(10)
            self.app.processEvents()
            self.assertFalse(window.run_controller.is_active)
            self.assertEqual(window.engine.runtime_variables.get("counter"), 1)
            self.assertFalse(row.property("debugPaused"))

    # FR-c01-002: 复核建议移除过度 skip 使不变量获得 CI 覆盖。离屏试跑验证（本机
    # 150s 超时未完成）确认该用例在离屏下不可稳定执行，维持 skip 并进入实机矩阵
    # RM-c01-001（真实桌面事件循环）；不因移除失败而放宽断言或删除测试。
    @unittest.skipIf(_OFFSCREEN, "requires real event loop for engine execution")
    def test_simple_mode_hides_and_suppresses_debug_state_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row(
                {
                    "type": 16.0,
                    "value": "simple_counter = 1",
                    "debug_breakpoint": True,
                }
            )
            row = window.task_list.itemWidget(window.task_list.item(0))
            window.add_row(
                {"type": 4.0, "value": "disabled", "step_disabled": True}
            )
            disabled_row = window.task_list.itemWidget(window.task_list.item(1))
            self.assertTrue(window.breakpoint_toggle_btn.isHidden())
            self.assertTrue(window.debug_toolbar_widget.isHidden())
            self.assertTrue(row.breakpoint_mark_label.isHidden())
            self.assertFalse(disabled_row.disabled_mark_label.isHidden())
            self.assertTrue(disabled_row.property("stepDisabled"))
            self.assertTrue(row.get_data()["debug_breakpoint"])
            self.assertTrue(disabled_row.get_data()["step_disabled"])

            runtime_tasks = window.tasks_for_current_settings_mode(
                [row.get_data(), disabled_row.get_data()]
            )
            self.assertFalse(runtime_tasks[0]["debug_breakpoint"])
            self.assertTrue(runtime_tasks[1]["step_disabled"])
            self.assertTrue(row.get_data()["debug_breakpoint"])

            with mock.patch.object(
                window.run_controller, "start", return_value=(True, "")
            ) as start:
                window.start_task()
            launched_tasks = start.call_args.args[0]
            self.assertFalse(launched_tasks[0]["debug_breakpoint"])
            self.assertTrue(launched_tasks[1]["step_disabled"])
            window.on_finish()

            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("advanced")
            )
            self.app.processEvents()
            self.assertFalse(window.breakpoint_toggle_btn.isHidden())
            self.assertFalse(window.debug_toolbar_widget.isHidden())
            self.assertFalse(row.breakpoint_mark_label.isHidden())

    def test_settings_and_ui_scaling_remain_available(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertGreaterEqual(window.settings_dialog.minimumWidth(), 720)
            self.assertGreaterEqual(window.settings_dialog.minimumHeight(), 520)
            window.settings_dialog.resize(720, 520)
            window.settings_dialog.show()
            self.app.processEvents()
            scroll = window.settings_dialog.findChild(QScrollArea)
            self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
            base_font = window.ui_scale_edit.font().pointSizeF()
            base_height = window.ui_scale_edit.sizeHint().height()
            window.apply_ui_scale(1.5)
            self.app.processEvents()
            self.assertAlmostEqual(window.ui_scale, 1.5)
            self.assertGreater(window.ui_scale_edit.font().pointSizeF(), base_font * 1.4)
            self.assertGreater(window.ui_scale_edit.sizeHint().height(), base_height * 1.4)
            window.apply_ui_scale(1.0)
            self.app.processEvents()
            self.assertAlmostEqual(window.ui_scale_edit.font().pointSizeF(), base_font)
            self.assertEqual(window.ui_scale_edit.sizeHint().height(), base_height)

    def test_dark_theme_round_trips_through_profile_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            dark_index = window.theme_mode_combo.findData("dark")
            window.theme_mode_combo.setCurrentIndex(dark_index)
            self.app.processEvents()

            self.assertEqual(self.app.property("_fukua_theme_mode"), "dark")
            self.assertIn("#111318", self.app.styleSheet())
            self.assertEqual(
                window.get_current_ui_config()["theme_mode"], "dark"
            )

            window.theme_mode_combo.setCurrentIndex(
                window.theme_mode_combo.findData("light")
            )
            self.app.processEvents()
            self.assertEqual(self.app.property("_fukua_theme_mode"), "light")

    # FR-20260809-1402: 移除过度 skipIf(_OFFSCREEN)——该测试的等价逻辑经
    # offscreen 实测可运行且断言成立（run_impacted_tests 恒设 offscreen，此前
    # 该测试从不执行，模式切换回归无覆盖）。
    def test_simple_mode_hides_advanced_speed_and_log_controls(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.move_spd.setText("0.25")
            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("simple")
            )
            self.app.processEvents()

            self.assertTrue(window.move_speed_group.isHidden())
            self.assertTrue(window.click_hold_group.isHidden())
            self.assertTrue(window.log_output_group.isHidden())
            self.assertEqual(window.move_spd.text(), "0.25")

            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("advanced")
            )
            self.app.processEvents()
            self.assertFalse(window.move_speed_group.isHidden())
            self.assertFalse(window.click_hold_group.isHidden())
            self.assertFalse(window.log_output_group.isHidden())

    def test_new_profiles_start_with_three_mapping_slots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertEqual(len(window.key_mapping_rows), 3)

    def test_settings_actions_reflow_without_clipping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.apply_ui_scale(1.5)
            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("advanced")
            )
            window.settings_dialog.resize(720, 520)
            window.settings_dialog.show()
            self.app.processEvents()

            row = window.settings_action_row
            action_widgets = [
                row.flow_layout.itemAt(index).widget()
                for index in range(row.flow_layout.count())
            ]
            self.assertEqual(len(action_widgets), 10)
            self.assertTrue(all(widget.isVisible() for widget in action_widgets))
            self.assertTrue(
                all(
                    widget.geometry().right() <= row.contentsRect().right()
                    for widget in action_widgets
                )
            )

            row.resize(360, row.height())
            row.flow_layout.setGeometry(row.rect())
            self.assertGreater(
                len({widget.geometry().top() for widget in action_widgets}),
                1,
            )

    def test_export_actions_explain_their_distinct_purposes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertEqual(window.export_json_btn.text(), "普通导出")
            self.assertEqual(window.export_portable_btn.text(), "完整迁移包")
            self.assertEqual(window.export_sanitized_btn.text(), "脱敏分享包")
            self.assertIn("不复制模板图片", window.export_json_btn.toolTip())
            self.assertIn("多台电脑", window.export_portable_btn.toolTip())
            self.assertIn("不适合直接运行", window.export_sanitized_btn.toolTip())
            self.assertIn(
                "凭据秘密值始终不会导出",
                window.export_help_btn.accessibleDescription(),
            )

    def test_export_actions_use_fixed_portable_and_sanitized_policies(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            controller = window.workflow_transfer_controller
            package_path = os.path.join(temp_dir, "workflow.zip")
            result = SimpleNamespace(
                privacy_protected=False,
                redacted_step_count=0,
            )
            with (
                mock.patch.object(
                    controller,
                    "confirm_portable_export_privacy",
                    return_value=True,
                ),
                mock.patch.object(
                    QFileDialog,
                    "getSaveFileName",
                    return_value=(package_path, ""),
                ),
                mock.patch(
                    "fukua_rpa.ui.controllers.workflow_transfer_controller.export_full_package",
                    return_value=result,
                ) as export_mock,
            ):
                controller.save_portable_package()
            self.assertFalse(export_mock.call_args.kwargs["privacy_protected"])

            result.privacy_protected = True
            result.redacted_step_count = 2
            with (
                mock.patch.object(
                    QFileDialog,
                    "getSaveFileName",
                    return_value=(package_path, ""),
                ),
                mock.patch.object(QMessageBox, "information"),
                mock.patch(
                    "fukua_rpa.ui.controllers.workflow_transfer_controller.export_full_package",
                    return_value=result,
                ) as export_mock,
            ):
                controller.save_sanitized_package()
            self.assertTrue(export_mock.call_args.kwargs["privacy_protected"])

    def test_region_help_and_help_behavior_are_consistent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            toolbar_layout = window.region_btn.parentWidget().layout()
            region_index = toolbar_layout.indexOf(window.region_btn)
            self.assertEqual(toolbar_layout.indexOf(window.region_help_btn), region_index + 1)
            self.assertEqual(toolbar_layout.indexOf(window.preview_points_btn), region_index + 2)
            self.assertEqual(
                toolbar_layout.indexOf(window.task_search_btn),
                toolbar_layout.indexOf(window.preview_points_btn) + 1,
            )
            self.assertEqual(
                toolbar_layout.indexOf(window.script_check_btn),
                toolbar_layout.indexOf(window.task_search_btn) + 1,
            )
            self.assertEqual(window.task_search_btn.text(), "")
            self.assertEqual(window.region_help_btn.toolTip(), "")
            self.assertEqual(window.region_help_btn.accessibleName(), "帮助")
            self.assertTrue(
                all(
                    button.accessibleDescription() == button.tip_text
                    for button in window.findChildren(HelpBtn)
                )
            )
            QTest.mouseClick(window.region_help_btn, Qt.LeftButton)
            self.app.processEvents()
            self.assertIsNotNone(window.region_help_btn._popup)
            self.assertTrue(window.region_help_btn._popup.isVisible())
            window.region_help_btn._popup.close()

    def test_region_picker_uses_contextual_blue_white_guidance(self):
        picker = RegionWindow(purpose="template")
        self.addCleanup(dispose_widget, self.app, picker)
        picker.setGeometry(0, 0, 1000, 700)
        picker.current_pos = QPoint(900, 80)
        self.assertEqual(picker.instruction_title(), "快捷选图")
        self.assertIn("自动保存模板", picker.instruction_lines()[0])
        self.assertNotIn("鼠标靠近", "".join(picker.instruction_lines()))
        self.assertGreaterEqual(picker.instruction_panel_rect().height(), 110)
        self.assertTrue(picker.instruction_panel_visible())

        panel = picker.instruction_panel_rect()
        picker.current_pos = panel.center()
        self.assertFalse(picker.instruction_panel_visible())

        picker.current_pos = QPoint(900, 80)
        picker.selection_rect = QRect(120, 100, 360, 220)
        picker.show()
        self.app.processEvents()
        border_pixel = picker.grab().toImage().pixelColor(120, 180)
        self.assertGreater(border_pixel.blue(), border_pixel.red())

    def test_text_controls_grow_and_setting_rows_reflow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertGreaterEqual(window.start_key_btn.minimumWidth(), 68)
            self.assertGreaterEqual(window.stop_key_btn.minimumWidth(), 68)
            self.assertEqual(
                window.run_status_pos_combo.sizeAdjustPolicy(),
                QComboBox.AdjustToContents,
            )
            mapping_action = window.key_mapping_rows[0]["action"]
            delete_btn = window.key_mapping_rows[0]["delete_btn"]
            inspect_btn = window.key_mapping_rows[0]["inspect_btn"]
            self.assertTrue(delete_btn.property("iconOnly"))
            self.assertTrue(inspect_btn.property("iconOnly"))
            self.assertEqual(delete_btn.minimumWidth(), inspect_btn.minimumWidth())
            self.assertEqual(mapping_action.minimumWidth(), 0)
            self.assertLess(mapping_action.sizeHint().width(), 130)
            self.assertEqual(mapping_action.sizeAdjustPolicy(), QComboBox.AdjustToContents)
            self.assertEqual(window.run_status_pos_combo.minimumWidth(), 0)
            self.assertLess(window.run_status_pos_combo.sizeHint().width(), 120)
            row = window.system_settings_row
            self.assertIsInstance(row, ResponsiveRow)
            self.assertGreater(
                row.flow_layout.heightForWidth(420),
                row.flow_layout.heightForWidth(1600),
            )
            window.apply_ui_scale(1.8)
            self.assertGreaterEqual(window.start_key_btn.minimumWidth(), 122)
            self.assertEqual(mapping_action.minimumWidth(), 0)
            self.assertGreater(mapping_action.sizeHint().width(), 130)

    def test_settings_rows_use_one_vertical_spacing_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertEqual(window.system_settings_row.flow_layout._vertical_spacing, 7)
            self.assertEqual(
                window.key_mapping_rows[0]["container"].flow_layout._vertical_spacing,
                7,
            )
            self.assertEqual(window.mapping_rows_layout.spacing(), 7)

    def _wait_layout_settled(self, widget, *, timeout_ms=3000):
        """Bounded wait until responsive layout actually settles.

        固定次数 ``processEvents`` 不保证布局完成：``ResponsiveRow`` 的高度同步由
        16/33 ms 定时器驱动，并会继续触发父级布局。这里以**几何证据**判定完成：
        所有行的 ``min == max == heightForWidth(contentsRect().width())``，且连续
        三次采样完全相同。超时即失败并输出真实诊断，不跳过、不降级断言。
        """
        from fukua_rpa.ui.components import ResponsiveRow

        deadline = time.monotonic() + timeout_ms / 1000.0
        samples = []
        rows = []
        while time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(10)
            rows = widget.findChildren(ResponsiveRow)
            pending = [
                index
                for index, row in enumerate(rows)
                if not (
                    row.minimumHeight()
                    == row.maximumHeight()
                    == row.heightForWidth(row.contentsRect().width())
                )
            ]
            scroll = widget.findChild(QScrollArea)
            bar = scroll.verticalScrollBar()
            sample = (
                scroll.widget().height(),
                bar.maximum(),
                bar.isVisible(),
                scroll.viewport().width(),
            )
            samples.append(sample)
            if not pending and len(samples) >= 3 and len(set(samples[-3:])) == 1:
                return samples[-1]
        scroll = widget.findChild(QScrollArea)
        bar = scroll.verticalScrollBar()
        details = [
            f"timeout_ms={timeout_ms}",
            f"window_width={widget.width()}",
            f"content_height={scroll.widget().height()}",
            f"scrollbar_max={bar.maximum()}",
            f"scrollbar_visible={bar.isVisible()}",
            f"viewport_width={scroll.viewport().width()}",
            f"last_samples={samples[-3:]}",
            "unsettled_rows="
            + str(
                [
                    (
                        index,
                        rows[index].minimumHeight(),
                        rows[index].maximumHeight(),
                        rows[index].heightForWidth(rows[index].contentsRect().width()),
                        rows[index].contentsRect().width(),
                    )
                    for index in pending
                ]
            ),
        ]
        raise AssertionError("布局未在限定时间内完成：" + "; ".join(details))

    def test_responsive_rows_propagate_height_to_settings_scrollbar(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            dialog = window.settings_dialog
            dialog.show()
            scroll = dialog.findChild(QScrollArea)
            measurements = []
            widths = []
            for width in (720, 1400):
                dialog.resize(width, 700)
                self._wait_layout_settled(dialog)
                widths.append(scroll.viewport().width())
                measurements.append(
                    (scroll.widget().height(), scroll.verticalScrollBar().maximum())
                )
            # 两档必须真的产生不同宽度，否则 resize 被最小宽度钳制时断言会失去意义。
            self.assertNotEqual(widths[0], widths[1])
            self.assertGreater(measurements[0][0], measurements[1][0])
            self.assertGreater(measurements[0][1], measurements[1][1])

    def test_responsive_groups_share_spacing_and_vertical_centers(self):
        row = ResponsiveRow()
        checkbox = QCheckBox("自适应降频")
        edit = QLineEdit("1.0")
        group = row.add_group("播放倍速:", edit, checkbox)
        self.addCleanup(dispose_widget, self.app, row)
        row.resize(500, 80)
        row.show()
        self.app.processEvents()
        self.assertEqual(
            group.layout().spacing(), row.flow_layout.horizontalSpacing()
        )
        children = (
            group.findChildren(QLabel)
            + group.findChildren(QLineEdit)
            + group.findChildren(QCheckBox)
        )
        centers = [
            child.geometry().center().y() for child in children if child.isVisible()
        ]
        self.assertGreaterEqual(len(centers), 3)
        self.assertLessEqual(max(centers) - min(centers), 1)

    def test_unfocused_combo_ignores_mouse_wheel(self):
        combo = NoWheelComboBox()
        self.addCleanup(dispose_widget, self.app, combo)
        combo.addItems(["一", "二"])
        event = mock.Mock()
        combo.clearFocus()
        combo.wheelEvent(event)
        event.ignore.assert_called_once_with()

    def test_resource_monitor_interval_round_trips_and_can_stop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertFalse(hasattr(window, "low_power_ui_chk"))
            self.assertEqual(window.cpu_refresh_combo.findData("auto"), -1)
            self.assertEqual(window.current_cpu_interval(), 2000)
            self.assertEqual(
                window.get_current_ui_config()["cpu_refresh_interval"], 2000
            )
            self.assertNotIn("low_power_ui", window.get_current_ui_config())
            self.assertEqual(
                window.hotkey_timer.interval(), window.HOTKEY_POLL_INTERVAL_MS
            )

            window.cpu_refresh_combo.setCurrentIndex(
                window.cpu_refresh_combo.findData(5000)
            )
            self.assertEqual(window.current_cpu_interval(), 5000)
            self.assertEqual(window.cpu_timer.interval(), 5000)
            self.assertTrue(window.cpu_timer.isActive())
            self.assertEqual(
                window.get_current_ui_config()["cpu_refresh_interval"], 5000
            )

            config = window.get_current_ui_config()
            config["cpu_refresh_interval"] = "0"
            window.apply_ui_config(config)
            self.assertFalse(window.cpu_timer.isActive())
            self.assertEqual(window.cpu_label.text(), "资源监测: 已关闭")

    def test_resource_monitor_labels_system_and_whole_process_usage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            process = mock.Mock()
            process.cpu_percent.return_value = 6.25
            window.current_process = process
            with mock.patch(
                "fukua_rpa.ui.controllers.window_performance_controller.HAS_PSUTIL",
                True,
            ), mock.patch(
                "fukua_rpa.ui.controllers.window_performance_controller.psutil.cpu_count",
                return_value=16,
            ), mock.patch(
                "fukua_rpa.ui.controllers.window_performance_controller.psutil.cpu_percent",
                return_value=12.5,
            ):
                window.update_cpu_info()

        self.assertEqual(
            window.cpu_label.text(),
            "本程序 CPU: 6.2%",
        )
        self.assertNotIn("系统 CPU", window.cpu_label.text())
        self.assertNotIn("逻辑处理器", window.cpu_label.text())
        self.assertNotIn("(单核)", window.cpu_label.text())
        self.assertIn("单核口径", window.cpu_label.toolTip())
        self.assertIn("系统共有 16 个逻辑处理器", window.cpu_label.toolTip())

        window.settings_mode_combo.setCurrentIndex(
            window.settings_mode_combo.findData("advanced")
        )
        self.app.processEvents()
        self.assertEqual(
            window.cpu_label.text(),
            "系统 CPU: 12.5% | 本程序 CPU: 6.2%",
        )

    def test_resource_monitor_follows_the_active_isolated_runner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            runner_process = mock.Mock()
            runner_process.cpu_percent.side_effect = [0.0, 87.5]
            window.run_controller.resource_process_target = mock.Mock(
                return_value=(43210, "独立 Runner")
            )
            with mock.patch(
                "fukua_rpa.ui.controllers.window_performance_controller.HAS_PSUTIL",
                True,
            ), mock.patch(
                "fukua_rpa.ui.controllers.window_performance_controller.psutil.Process",
                return_value=runner_process,
            ) as process_factory, mock.patch(
                "fukua_rpa.ui.controllers.window_performance_controller.psutil.cpu_count",
                return_value=24,
            ), mock.patch(
                "fukua_rpa.ui.controllers.window_performance_controller.psutil.cpu_percent",
                return_value=15.0,
            ):
                window.update_cpu_info()

        process_factory.assert_called_once_with(43210)
        self.assertEqual(window.cpu_label.text(), "独立 Runner CPU: 87.5%")
        self.assertIn("PID 43210", window.cpu_label.toolTip())
        self.assertIn("系统共有 24 个逻辑处理器", window.cpu_label.toolTip())

    def test_application_events_follow_active_timestamp_policy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.log_level_combo.setCurrentIndex(1)
            window.log_text.clear()
            window.append_application_log("<b>application-event</b>")
            self.assertRegex(
                window.log_text.toPlainText(),
                r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\] application-event$",
            )

            window.log_level_combo.setCurrentIndex(0)
            window.log_text.clear()
            window.append_application_log("simple-application-event")
            self.assertEqual(
                window.log_text.toPlainText(), "simple-application-event"
            )

    def test_custom_log_menu_round_trips_without_losing_custom_selection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertFalse(window.log_critical_action.isEnabled())
            self.assertTrue(window.log_critical_action.isChecked())
            self.assertFalse(window.log_category_actions[LOG_ACTION].isChecked())

            window.log_category_actions[LOG_ACTION].setChecked(True)
            self.assertEqual(window.current_log_mode(), LOG_MODE_CUSTOM)
            custom_categories = set(window.current_custom_log_categories())
            self.assertIn(LOG_ACTION, custom_categories)
            self.assertNotIn(LOG_TIMESTAMP, custom_categories)

            window.log_controller.select_preset(LOG_MODE_DETAILED)
            self.assertEqual(window.current_log_mode(), LOG_MODE_DETAILED)
            self.assertTrue(window.log_category_actions[LOG_TIMESTAMP].isChecked())
            window.log_level_combo.setCurrentIndex(
                window.log_level_combo.findData(LOG_MODE_CUSTOM)
            )
            self.assertEqual(
                set(window.current_log_policy().enabled_categories),
                custom_categories,
            )

            saved = window.get_current_ui_config()
            self.assertEqual(saved["log_mode"], LOG_MODE_CUSTOM)
            self.assertEqual(saved["log_level"], 2)
            self.assertEqual(
                set(saved["log_custom_categories"]), custom_categories
            )

            saved["log_custom_categories"] = [LOG_RECOGNITION, LOG_TIMESTAMP]
            window.apply_ui_config(saved)
            self.assertEqual(window.current_log_mode(), LOG_MODE_CUSTOM)
            self.assertEqual(
                set(window.current_custom_log_categories()),
                {LOG_RECOGNITION, LOG_TIMESTAMP},
            )
            self.assertTrue(
                window.log_category_actions[LOG_RECOGNITION].isChecked()
            )
            self.assertTrue(window.log_category_actions[LOG_TIMESTAMP].isChecked())

    def test_log_content_menu_stays_open_for_multiple_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.show()
            menu = window.log_content_menu
            menu.popup(window.mapToGlobal(QPoint(40, 40)))
            self.app.processEvents()

            action = window.log_category_actions[LOG_ACTION]
            QTest.mouseClick(
                menu,
                Qt.LeftButton,
                pos=menu.actionGeometry(action).center(),
            )
            self.app.processEvents()
            self.assertTrue(action.isChecked())
            self.assertTrue(menu.isVisible())

            recognition = window.log_category_actions[LOG_RECOGNITION]
            QTest.mouseClick(
                menu,
                Qt.LeftButton,
                pos=menu.actionGeometry(recognition).center(),
            )
            self.app.processEvents()
            self.assertTrue(recognition.isChecked())
            self.assertTrue(menu.isVisible())

            QTest.keyClick(menu, Qt.Key_Escape)
            self.app.processEvents()
            self.assertFalse(menu.isVisible())

    def test_180_percent_scale_keeps_main_command_text_visible(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.show()
            window.apply_ui_scale(1.8)
            for _index in range(3):
                self.app.processEvents()
                QTest.qWait(5)

            for text in ("新增步骤", "● 操作录制", "识别区域"):
                button = next(
                    item
                    for item in window.findChildren(QPushButton)
                    if item.text() == text
                )
                with self.subTest(text=text):
                    self.assertGreater(
                        int(button.property("_ui_base_text_button_w") or 0), 0
                    )
                    self.assertGreaterEqual(
                        button.minimumWidth(), button.sizeHint().width()
                    )
                    self.assertGreaterEqual(button.width(), button.sizeHint().width())

    def test_unsupported_startup_profile_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            settings = QSettings(config_path, QSettings.IniFormat)
            profile = default_profile_config()
            profile["_schema_version"] = PROFILE_SCHEMA_VERSION + 1
            profiles = {"Future": profile}
            serialized = json.dumps(profiles, ensure_ascii=False)
            settings.setValue("profiles_json", serialized)
            settings.setValue("current_profile", "Future")
            settings.setValue(
                "profiles_signature", profiles_signature(profiles, "Future")[1]
            )
            settings.sync()

            window = self.create_window(temp_dir)
            self.assertTrue(window._profiles_persistence_blocked)
            window.add_row({"type": 5.0, "value": "1"})
            self.assertTrue(window.persist_profiles_state(force=True))

            reloaded = QSettings(config_path, QSettings.IniFormat)
            self.assertEqual(reloaded.value("profiles_json"), serialized)

    def test_step_dialog_uses_compact_buttons_and_progressive_sections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            dialog = TaskConfigDialog(
                window,
                {},
                image_settings_available=True,
                point_limit_available=True,
                coordinate_step_available=True,
                base_coordinate=(100, 100),
                base_dir=temp_dir,
            )
            self.addCleanup(dispose_widget, self.app, dialog)
            dialog.show()
            self.app.processEvents()
            compact_buttons = [dialog.coord_step_pick_btn]
            compact_buttons.extend(
                widgets[key]
                for widgets in dialog.until_condition_widgets.values()
                for key in ("image_btn", "region_btn")
            )
            for button in compact_buttons:
                self.assertTrue(button.property("compactText"))
                required = button.fontMetrics().horizontalAdvance(button.text()) + 12
                self.assertGreaterEqual(button.width(), required)
            self.assertTrue(dialog.coord_sequence_details.isHidden())
            dialog.coordinate_target_section.set_expanded(True)
            dialog.coord_sequence_chk.setChecked(True)
            self.app.processEvents()
            self.assertTrue(dialog.coord_sequence_details.isVisible())
            self.assertFalse(dialog.debug_section.toggle_btn.isChecked())
            self.assertTrue(dialog.debug_section.content_widget.isHidden())
            self.assertTrue(dialog.execution_section.is_expanded())
            self.assertTrue(dialog.execution_section.content_widget.isVisible())

    def test_debug_presets_preserve_existing_and_advanced_conditions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            dialog = TaskConfigDialog(
                window,
                {"debug_breakpoint": True, "debug_condition": "loop >= 2"},
                base_dir=temp_dir,
            )
            self.addCleanup(dispose_widget, self.app, dialog)
            self.assertEqual(
                dialog.debug_preset_combo.currentText(),
                "从第 N 次循环开始",
            )
            self.assertEqual(dialog.debug_preset_value_edit.text(), "2")
            self.assertEqual(dialog.get_data()["debug_condition"], "loop >= 2")
            dialog.debug_preset_combo.setCurrentText("本步骤执行 N 次后")
            dialog.debug_preset_value_edit.setText("3")
            self.assertEqual(
                dialog.get_data()["debug_condition"],
                "execution_count >= 3",
            )
            dialog.debug_preset_combo.setCurrentText("高级表达式")
            dialog.debug_condition_edit.setText("counter > 10")
            dialog.remember_debug_custom_expression("counter > 10")
            dialog.debug_preset_combo.setCurrentText("每次经过都暂停")
            dialog.debug_preset_combo.setCurrentText("高级表达式")
            self.assertEqual(dialog.debug_condition_edit.text(), "counter > 10")

    def test_open_step_dialog_refreshes_only_unedited_jump_numbers(self):
        dialog = TaskConfigDialog(
            None,
            {"success_jump": "3", "fail_jump": "2"},
            False,
            False,
            False,
            None,
            "",
            False,
            None,
            2,
            "等待(秒)",
            None,
        )
        self.addCleanup(dispose_widget, self.app, dialog)
        dialog.refresh_reference_numbers(
            {"success_jump": "1", "fail_jump": "4"}
        )
        self.assertEqual(dialog.success_jump_edit.text(), "1")
        self.assertEqual(dialog.fail_jump_edit.text(), "4")

        dialog.success_jump_edit.setText("2")
        dialog.success_jump_edit.textEdited.emit("2")
        dialog.refresh_reference_numbers(
            {"success_jump": "5", "fail_jump": "1"}
        )
        self.assertEqual(dialog.success_jump_edit.text(), "2")
        self.assertEqual(dialog.fail_jump_edit.text(), "1")

    def test_mapping_status_is_the_rightmost_expandable_group(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            # 2026-10-19：映射编辑区迁到「工具 → 按键映射」，行控件随该窗口布局后才可度量。
            window.show_key_mapping_tool_window()
            tool = window._key_mapping_tool_window
            tool.resize(1500, 760)
            tool.show()
            self.app.processEvents()
            row_data = window.key_mapping_rows[0]
            row = row_data["container"]
            trailing = row.flow_layout.itemAt(row.flow_layout.count() - 1).widget()
            self.assertIs(trailing, row_data["binding_group"])
            self.assertTrue(trailing.property("flowTrailing"))
            self.assertLessEqual(
                abs(trailing.geometry().right() - row.contentsRect().right()), 1
            )
            row_data["window_binding"] = {
                "root_title": "完整显示的目标程序窗口名称",
            }
            window.mapping_click_mode_combo.setCurrentText("后台窗口点击(实验)")
            window.update_mapping_window_binding_ui(row_data)
            self.assertEqual(
                row_data["binding_status"].text(),
                "已绑定：完整显示的目标程序窗口名称",
            )

    def test_mapping_icon_buttons_match_after_secondary_creation_and_round_trip(self):
        from fukua_rpa.ui import theme
        from fukua_rpa.ui.controllers import window_performance_controller

        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            dialog = window.settings_dialog
            dialog.show()
            self.app.processEvents()
            manager = theme.install_per_screen_font_refresh(self.app)

            with (
                mock.patch.object(
                    theme, "window_visual_density", return_value=2 / 3
                ),
                mock.patch.object(
                    window_performance_controller,
                    "window_visual_density",
                    return_value=2 / 3,
                ),
            ):
                manager.refresh_widget(dialog)
                row = window.add_key_mapping_row(refresh=False)
                self.app.processEvents()
                direct_secondary = row["delete_btn"].width()

            with (
                mock.patch.object(
                    theme, "window_visual_density", return_value=1.0
                ),
                mock.patch.object(
                    window_performance_controller,
                    "window_visual_density",
                    return_value=1.0,
                ),
            ):
                manager.refresh_widget(dialog)

            with (
                mock.patch.object(
                    theme, "window_visual_density", return_value=2 / 3
                ),
                mock.patch.object(
                    window_performance_controller,
                    "window_visual_density",
                    return_value=2 / 3,
                ),
            ):
                manager.refresh_widget(dialog)
                self.app.processEvents()
                round_trip_secondary = row["delete_btn"].width()

            self.assertGreater(direct_secondary, 0)
            self.assertEqual(direct_secondary, round_trip_secondary)

    def test_debug_toolbar_and_playback_label_use_new_layout_terms(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.resize(1100, 760)
            window.show()
            self.app.processEvents()
            self.assertTrue(window.debug_toolbar_widget.isHidden())
            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("advanced")
            )
            self.app.processEvents()
            self.assertGreater(
                window.debug_caption.mapTo(window, window.debug_caption.rect().topLeft()).x(),
                window.width() // 2,
            )
            labels = [label.text() for label in window.settings_dialog.findChildren(QLabel)]
            self.assertIn("播放倍速:", labels)
            self.assertNotIn("录制倍速:", labels)

    def test_enter_in_ui_scale_does_not_activate_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.settings_dialog.show()
            self.app.processEvents()
            export_button = next(
                button
                for button in window.settings_dialog.findChildren(QPushButton)
                if button.property("variant") == "primary"
            )
            clicks = []
            export_button.clicked.connect(lambda: clicks.append(True))
            with mock.patch.object(QFileDialog, "getSaveFileName", return_value=("", "")):
                window.ui_scale_edit.setFocus()
                window.ui_scale_edit.selectAll()
                QTest.keyClicks(window.ui_scale_edit, "125")
                QTest.keyClick(window.ui_scale_edit, Qt.Key_Return)
                self.app.processEvents()
            self.assertEqual(clicks, [])
            self.assertAlmostEqual(window.ui_scale, 1.25)
            self.assertTrue(
                all(
                    not button.autoDefault()
                    for button in window.settings_dialog.findChildren(QPushButton)
                )
            )

    def test_hotkey_dialog_displays_live_modifier_progress(self):
        fake_thread = mock.Mock()
        fake_thread.keyboard_hook = 1
        fake_thread.isRunning.return_value = False
        with mock.patch(
            "fukua_rpa.ui.input_tools.HotkeyCaptureHookThread",
            return_value=fake_thread,
        ):
            dialog = KeyCaptureDialog(title="测试热键")
        self.addCleanup(dispose_widget, self.app, dialog)
        dialog.on_native_progress("Ctrl+Alt+Shift")
        self.assertEqual(dialog.preview_label.text(), "当前按下：Ctrl+Alt+Shift")
        dialog.on_native_captured("ctrl+alt+shift+a")
        self.assertEqual(dialog.preview_label.text(), "已录入：Ctrl+Alt+Shift+A")

    def test_responsive_setting_groups_are_vertically_centered(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.settings_dialog.resize(1180, 780)
            window.settings_dialog.show()
            self.app.processEvents()
            row = window.settings_dialog.findChildren(ResponsiveRow)[0]
            centers = []
            for index in range(row.flow_layout.count()):
                widget = row.flow_layout.itemAt(index).widget()
                if widget is not None and widget.isVisible():
                    centers.append(widget.geometry().center().y())
            self.assertGreater(len(centers), 1)
            self.assertLessEqual(max(centers) - min(centers), 1)

    def test_step_settings_wrap_and_keep_text_buttons_readable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            dialog = TaskConfigDialog(
                window,
                {},
                image_settings_available=True,
                point_limit_available=True,
                coordinate_step_available=True,
                image_click_point_available=True,
                base_coordinate=(100, 100),
                base_dir=temp_dir,
            )
            self.addCleanup(dispose_widget, self.app, dialog)
            self.assertGreaterEqual(dialog.image_click_point_select_btn.minimumWidth(), 68)
            self.assertGreaterEqual(dialog.image_click_point_preview_btn.minimumWidth(), 68)
            self.assertGreaterEqual(dialog.step_region_pick_btn.minimumWidth(), 68)
            self.assertGreaterEqual(dialog.step_region_clear_btn.minimumWidth(), 68)
            forms = dialog.findChildren(QFormLayout)
            self.assertTrue(forms)
            self.assertTrue(
                all(form.rowWrapPolicy() == QFormLayout.WrapLongRows for form in forms)
            )
            dialog.resize(900, 520)
            dialog.show()
            self.app.processEvents()
            scroll = dialog.findChild(QScrollArea)
            self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)

    def test_mapping_hotkey_capture_returns_focus_to_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.settings_dialog.show()
            self.app.processEvents()
            created = {}

            class FakeCaptureDialog:
                captured_text = "F8"

                def __init__(self, parent, title):
                    created["parent"] = parent
                    created["title"] = title

                def exec(self):
                    return QDialog.Accepted

            with mock.patch(
                "fukua_rpa.ui.controllers.mapping_mixins.hotkey_capture.KeyCaptureDialog",
                FakeCaptureDialog,
            ), mock.patch.object(window, "refresh_hotkey_backend"):
                window.capture_mapping_hotkey(0)

            self.assertIs(created["parent"], window.settings_dialog)
            self.assertEqual(created["title"], "录入映射1热键")
            self.assertEqual(window.key_mapping_rows[0]["hotkey"].text(), "F8")
            self.assertTrue(window.settings_dialog.isVisible())

    def test_mapping_state_signal_does_not_forward_qt_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            with mock.patch.object(
                window.hotkey_controller, "refresh_hotkey_backend"
            ) as refresh:
                window.mapping_mode_chk.setChecked(
                    not window.mapping_mode_chk.isChecked()
                )
                self.app.processEvents()

            refresh.assert_called()
            self.assertTrue(all(not call.args for call in refresh.call_args_list))

    def test_mapping_binding_looks_through_own_windows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            with mock.patch.object(user32, "WindowFromPoint", return_value=101), \
                    mock.patch.object(user32, "GetAncestor", return_value=101), \
                    mock.patch.object(user32, "EnumChildWindows", return_value=True), \
                    mock.patch.object(
                        window,
                        "hwnd_belongs_to_current_process",
                        side_effect=lambda hwnd: int(hwnd) == 101,
                    ), mock.patch.object(
                        window, "top_level_window_at_point", return_value=202
                    ) as find_under, mock.patch.object(
                        window, "hwnd_screen_area_at_point", return_value=120000
                    ):
                target = window.background_click_target_hwnd(
                    400, 300, exclude_current_process=True
                )

            self.assertEqual(int(target), 202)
            find_under.assert_called_once_with(
                400, 300, exclude_current_process=True
            )

    def test_window_selection_is_only_available_in_background_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            row = window.key_mapping_rows[0]
            self.assertFalse(row["bind_window"].isEnabled())
            self.assertFalse(row["inspect_btn"].isEnabled())
            window.mapping_click_mode_combo.setCurrentText("后台窗口点击(实验)")
            self.assertTrue(row["bind_window"].isEnabled())
            self.assertFalse(row["inspect_btn"].isEnabled())
            row["window_binding"] = {"root_hwnd": 700}
            window.update_mapping_window_binding_ui(row)
            self.assertTrue(row["inspect_btn"].isEnabled())
            window.mapping_click_mode_combo.setCurrentText("点击后返回原位")
            self.assertFalse(row["bind_window"].isEnabled())
            self.assertFalse(row["inspect_btn"].isEnabled())

    def test_coordinate_pick_does_not_guess_or_bind_a_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            row = window.key_mapping_rows[0]
            self.assertIsNone(window.mapping_backend)
            window.on_mapping_coordinate_picked_by_row(row, "320,240")
            self.assertEqual(row["coord"].text(), "320,240")
            self.assertEqual(row["window_binding"], {})
            self.assertIsNone(window.mapping_backend)

    def test_manual_window_pick_binds_selection_to_mapping_coordinate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            row = window.key_mapping_rows[0]
            row["coord"].setText("320,240")
            window.mapping_click_mode_combo.setCurrentText("后台窗口点击(实验)")
            binding = {
                "root_hwnd": 700,
                "root_title": "Target application",
                "root_class": "TargetWindow",
            }
            backend = mock.Mock()
            backend.create_binding_for_window_at_point.return_value = binding
            window.mapping_backend = backend
            self.assertTrue(
                window.on_mapping_window_picked_by_row(row, "900,500")
            )
            create_binding = backend.create_binding_for_window_at_point
            create_binding.assert_called_once_with(900, 500, 320, 240)
            self.assertEqual(row["window_binding"], binding)

    def test_background_click_failure_never_moves_the_real_mouse(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.mapping_click_mode_combo.setCurrentText("后台窗口点击(实验)")
            with mock.patch.object(
                window, "perform_mapping_background_click", return_value=False
            ), mock.patch.object(window, "perform_mapping_mouse_click") as foreground:
                result = window.perform_key_mapping_click(
                    100, 200, "left", 1, {"root_hwnd": 700}
                )
            self.assertIn("失败", result)
            foreground.assert_not_called()

    def test_application_icon_assets_are_ready_for_packaging(self):
        icon_path = get_resource_path(os.path.join("assets", "fukuaRPA.ico"))
        step_icon_path = get_resource_path(os.path.join("assets", "step_settings.svg"))
        search_icon_path = get_resource_path(os.path.join("assets", "search.svg"))
        self.assertTrue(os.path.isfile(icon_path))
        self.assertTrue(os.path.isfile(step_icon_path))
        self.assertTrue(os.path.isfile(search_icon_path))
        self.assertFalse(QIcon(icon_path).isNull())
        self.assertFalse(QIcon(step_icon_path).isNull())
        self.assertFalse(QIcon(search_icon_path).isNull())
        root = os.path.dirname(os.path.dirname(__file__))
        for spec_name in ("fukuaRPA_onedir.spec", "fukuaRPA_onefile.spec"):
            spec_path = os.path.join(root, spec_name)
            with open(spec_path, encoding="utf-8") as spec_file:
                spec = spec_file.read()
            self.assertIn('icon="assets/fukuaRPA.ico"', spec)
            self.assertIn('("assets/fukuaRPA.ico", "assets")', spec)
            self.assertIn('("assets/step_settings.svg", "assets")', spec)
            self.assertIn('("assets/search.svg", "assets")', spec)

    def test_usage_notices_are_available_from_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertEqual(window.usage_notices_btn.text(), "使用注意")
            self.assertTrue(window.usage_notices_btn.isEnabled())

    def test_json_profile_collection_import_keeps_file_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            profiles = {
                "第二方案": window.get_default_config_dict(),
                "第一方案": window.get_default_config_dict(),
            }
            path = os.path.join(temp_dir, "collection.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    build_profile_collection_payload(profiles),
                    handle,
                    ensure_ascii=False,
                )
            with mock.patch.object(
                QFileDialog,
                "getOpenFileName",
                return_value=(path, "JSON (*.json)"),
            ), mock.patch.object(
                window.workflow_transfer_controller,
                "review_imported_workflows",
                return_value="normal",
            ):
                window.workflow_transfer_controller.load()

            self.assertEqual(
                list(window.profiles_data)[-2:], ["第二方案", "第一方案"]
            )
            self.assertEqual(window.current_profile_name, "第二方案")

    def test_profile_collection_import_rolls_back_model_and_ui_together(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            original_name = window.current_profile_name
            original_profiles = list(window.profiles_data)
            profiles = {"待导入方案": window.get_default_config_dict()}
            path = os.path.join(temp_dir, "collection.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    build_profile_collection_payload(profiles),
                    handle,
                    ensure_ascii=False,
                )

            with mock.patch.object(
                QFileDialog,
                "getOpenFileName",
                return_value=(path, "JSON (*.json)"),
            ), mock.patch.object(
                window.workflow_transfer_controller,
                "review_imported_workflows",
                return_value="normal",
            ), mock.patch.object(
                window,
                "apply_ui_config",
                side_effect=[False, True],
            ) as apply_config, mock.patch.object(QMessageBox, "warning"):
                window.workflow_transfer_controller.load()

            self.assertEqual(list(window.profiles_data), original_profiles)
            self.assertEqual(window.current_profile_name, original_name)
            self.assertEqual(window.profile_combo.currentText(), original_name)
            self.assertEqual(apply_config.call_count, 2)
            self.assertEqual(
                apply_config.call_args_list[-1].args[0],
                window.profiles_data[original_name],
            )

    def test_main_window_has_stable_focus_order_and_accessible_icon_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertEqual(window.accessibleName(), "fukuaRPA 主窗口")
            self.assertEqual(window.task_list.accessibleName(), "脚本步骤列表")
            self.assertEqual(
                window.profile_combo.nextInFocusChain(), window.new_profile_btn
            )
            window.profile_combo.showPopup()
            self.app.processEvents()
            self.assertEqual(
                window.profile_combo._order_list.dragDropMode(),
                QAbstractItemView.InternalMove,
            )
            window.profile_combo.hidePopup()
            self.assertFalse(hasattr(window, "profile_up_btn"))
            self.assertFalse(hasattr(window, "profile_down_btn"))
            for button in (
                window.script_check_btn,
                window.task_search_btn,
                window.breakpoint_toggle_btn,
                window.single_step_btn,
                window.debug_pause_btn,
                window.debug_continue_btn,
                window.debug_next_btn,
                window.debug_variables_btn,
            ):
                self.assertTrue(button.accessibleName(), button.toolTip())
            self.assertEqual(window.task_search_btn.accessibleName(), "查找步骤")
            self.assertEqual(window.task_search_btn.shortcut().toString(), "Ctrl+F")

    def test_profile_selector_popup_reorders_without_changing_selection(self):
        combo = ReorderableComboBox()
        self.addCleanup(dispose_widget, self.app, combo)
        combo.addItems(["A", "B", "C"])
        combo.setCurrentText("B")
        emitted = []
        combo.orderChanged.connect(lambda names: emitted.append(list(names)))
        combo.show()
        combo.showPopup()
        self.app.processEvents()

        moved = combo._order_list.takeItem(0)
        combo._order_list.insertItem(2, moved)
        self.app.processEvents()

        self.assertEqual(combo.item_order(), ["B", "C", "A"])
        self.assertEqual(combo.currentText(), "B")
        self.assertEqual(emitted[-1], ["B", "C", "A"])
        self.assertTrue(combo._order_popup.isVisible())

    def test_form_labels_are_buddies_for_keyboard_and_screen_readers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            bound = [
                label
                for label in window.settings_dialog.findChildren(QLabel)
                if label.text().strip() and label.buddy() is not None
            ]
            self.assertGreaterEqual(len(bound), 8)
            self.assertTrue(all(label.buddy().accessibleName() for label in bound))

            dialog = TaskConfigDialog(
                window,
                {},
                image_settings_available=True,
                point_limit_available=True,
                coordinate_step_available=True,
                image_click_point_available=True,
                base_coordinate=(10, 20),
                base_dir=temp_dir,
            )
            self.addCleanup(dispose_widget, self.app, dialog)
            task_bound = [
                label
                for label in dialog.findChildren(QLabel)
                if label.text().strip() and label.buddy() is not None
            ]
            self.assertGreaterEqual(len(task_bound), 6)

    def test_task_list_keyboard_opens_settings_and_reorders_selected_step(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row({"type": 1.0, "value": "10,10"})
            window.add_row({"type": 1.0, "value": "20,20"})
            first_item = window.task_list.item(0)
            first_widget = window.ensure_task_item_widget(first_item)
            window.task_list.setCurrentItem(first_item)
            window.task_list.setFocus()

            with mock.patch.object(first_widget, "open_custom_config") as opener:
                QTest.keyClick(window.task_list, Qt.Key_Return)
            opener.assert_called_once_with()

            QTest.keyClick(window.task_list, Qt.Key_Down, Qt.ControlModifier)
            self.assertEqual(
                [task["value"] for task in window.snapshot_tasks()],
                ["20,20", "10,10"],
            )
            self.assertEqual(window.task_list.currentRow(), 1)

    def test_main_region_picker_reference_clears_after_picker_is_destroyed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.open_region_selector()
            picker = window.region_win
            self.assertIsNotNone(picker)
            picker.close()
            self.app.processEvents()
            QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()
            self.assertIsNone(window.region_win)

    def test_harmless_setting_values_are_not_marked_as_redacted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            previous_privacy = GLOBAL_CONFIG["privacy_protected"]
            self.addCleanup(
                GLOBAL_CONFIG.__setitem__, "privacy_protected", previous_privacy
            )
            GLOBAL_CONFIG["privacy_protected"] = True
            window.log_text.clear()

            window.log_setting_change("界面主题", "深色")
            window.log_setting_change("识别间隔(s)", "0.1")
            window.log_setting_change("避让坐标1 X", "1920")

            text = window.log_text.toPlainText()
            self.assertIn("界面主题 = 深色", text)
            self.assertIn("识别间隔(s) = 0.1", text)
            self.assertIn("避让坐标1 X = 1920", text)
            self.assertNotIn("值已脱敏", text)

    def test_offline_diagnostic_report_excludes_script_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            secret_value = "D:/private/secret-template.png"
            window.add_row({"type": 1.0, "value": secret_value})
            previous_privacy = GLOBAL_CONFIG["privacy_protected"]
            self.addCleanup(
                GLOBAL_CONFIG.__setitem__,
                "privacy_protected",
                previous_privacy,
            )
            GLOBAL_CONFIG["privacy_protected"] = False
            report = window.diagnostics_controller.collect_diagnostic_report()
            serialized = json.dumps(report, ensure_ascii=False)
            buttons = window.settings_dialog.findChildren(QPushButton)

            self.assertTrue(any(button.text() == "导出诊断" for button in buttons))
            self.assertEqual(report["application"]["task_count"], 1)
            self.assertIn("engine_last_run", report["performance"])
            self.assertNotIn(secret_value, serialized)
            self.assertNotIn(temp_dir, serialized)
            self.assertTrue(report["private_paths_redacted"])
            self.assertFalse(report["privacy"]["script_content_included"])

    def test_maintenance_actions_and_scale_step_are_advanced_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertTrue(window.open_config_dir_btn.isHidden())
            self.assertTrue(window.diagnostic_export_btn.isHidden())
            self.assertTrue(window.scale_step_group.isHidden())
            self.assertTrue(window.batch_export_btn.isHidden())
            self.assertTrue(window.run_status_group.isHidden())
            for widget in (
                window.merge_profile_btn,
                window.insert_step_btn,
                window.redo_btn,
                window.single_step_btn,
            ):
                self.assertTrue(widget.isHidden())

            window.settings_mode_combo.setCurrentIndex(
                window.settings_mode_combo.findData("advanced")
            )
            self.app.processEvents()
            self.assertFalse(window.open_config_dir_btn.isHidden())
            self.assertFalse(window.diagnostic_export_btn.isHidden())
            self.assertFalse(window.scale_step_group.isHidden())
            self.assertFalse(window.batch_export_btn.isHidden())
            self.assertFalse(window.run_status_group.isHidden())
            for widget in (
                window.merge_profile_btn,
                window.insert_step_btn,
                window.redo_btn,
                window.single_step_btn,
            ):
                self.assertFalse(widget.isHidden())

    def test_batch_export_select_none_fits_at_180_percent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.apply_ui_scale(1.8)
            dialog, _profile_list, _export_type = (
                window.workflow_transfer_controller._build_batch_export_dialog()
            )
            self.addCleanup(dispose_widget, self.app, dialog)
            dialog.show()
            for _index in range(3):
                self.app.processEvents()
                QTest.qWait(5)
            button = dialog.findChild(QPushButton, "batchSelectNone")
            self.assertIsNotNone(button)
            text_width = button.fontMetrics().horizontalAdvance(button.text())
            self.assertGreaterEqual(button.width(), text_width + 16)
            self.assertEqual(button.maximumWidth(), 16777215)

    def test_native_optimization_controls_round_trip_stable_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertFalse(window.isolated_runner_chk.isChecked())
            self.assertTrue(window.isolated_runner_group.isHidden())
            window.native_parallel_combo.setCurrentIndex(
                window.native_parallel_combo.findData("force")
            )
            window.native_scale_hint_chk.setChecked(False)
            window.scale_memory_tier_combo.setCurrentIndex(
                window.scale_memory_tier_combo.findData("aggressive")
            )
            window.scale_memory_manual_edit.setText("0.8, 1.0, 1.2")
            window.scale_memory_custom_chk.setChecked(True)
            window.scale_memory_preferred_spin.setValue(4)
            window.scale_memory_history_spin.setValue(80)
            window.isolated_runner_chk.setChecked(True)
            config = window.get_current_ui_config()
            self.assertEqual(config["native_parallel_mode"], "force")
            self.assertFalse(config["native_scale_hint_en"])
            self.assertEqual(config["scale_memory_tier"], "aggressive")
            self.assertEqual(config["scale_memory_manual"], "0.8, 1.0, 1.2")
            self.assertTrue(config["scale_memory_custom_en"])
            self.assertEqual(config["scale_memory_preferred_limit"], 4)
            self.assertEqual(config["scale_memory_history_limit"], 80)
            self.assertTrue(config["isolated_runner_en"])

            window.apply_ui_config(config)
            self.assertEqual(window.native_parallel_combo.currentData(), "force")
            self.assertFalse(window.native_scale_hint_chk.isChecked())
            self.assertEqual(window.scale_memory_tier_combo.currentData(), "aggressive")
            self.assertTrue(window.scale_memory_preferred_spin.isEnabled())
            self.assertTrue(window.isolated_runner_chk.isChecked())
            window.native_core_chk.setChecked(False)
            self.assertFalse(window.native_parallel_combo.isEnabled())
            self.assertTrue(window.native_scale_hint_chk.isEnabled())

    def test_native_parallel_combo_is_compact_and_keeps_longest_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.settings_dialog.resize(920, 700)
            window.settings_dialog.show()
            self.app.processEvents()
            combo = window.native_parallel_combo
            self.assertEqual(combo.minimumWidth(), combo.maximumWidth())
            self.assertLessEqual(combo.width(), combo.sizeHint().width() + 2)
            self.assertGreater(
                combo.width(), combo.fontMetrics().horizontalAdvance("关闭（单线程）")
            )

    def test_scale_memory_status_exposes_dynamic_decisions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertFalse(window.scale_memory_custom_chk.isChecked())
            self.assertFalse(window.scale_memory_preferred_spin.isEnabled())
            self.assertFalse(window.scale_memory_history_spin.isEnabled())
            policy = window.current_scale_memory_policy()
            key = ("template.png", 1)
            valid_scales = (0.8, 0.9, 1.0, 1.1, 1.2)
            for scale in (1.0, 1.2, 1.0, 0.8, 1.0):
                window.engine.scale_memory_store.record(
                    key,
                    "template.png",
                    valid_scales,
                    scale,
                    0.95,
                    policy,
                )
            window.refresh_scale_memory_status()
            status = window.scale_memory_status_label.text()
            self.assertIn("当前优先", status)
            self.assertIn("[1", status)
            self.assertIn("历史上限", status)

    def test_consecutive_delete_restores_danger_hover_without_tooltip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row({"type": 1.0, "value": "10,10"})
            window.add_row({"type": 1.0, "value": "20,20"})
            first = window.task_list.itemWidget(window.task_list.item(0))
            second = window.task_list.itemWidget(window.task_list.item(1))
            with mock.patch.object(
                QApplication, "widgetAt", return_value=second.del_btn
            ), mock.patch.object(QToolTip, "showText") as show_tooltip:
                window.del_row(first)
                QTest.qWait(40)
                self.app.processEvents()
            self.assertTrue(second.del_btn.property("syntheticHover"))
            show_tooltip.assert_not_called()
            self.assertIn(
                'QPushButton[variant="dangerGhost"][syntheticHover="true"]',
                self.app.styleSheet(),
            )

    def test_script_check_reports_non_exiting_self_jump_without_running(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            cfg = window.get_current_ui_config()
            tasks = [{"type": 5.0, "value": "0", "success_jump": "1", "fail_jump": "1"}]
            report = window.workflow_review_controller.script_check_report(tasks, cfg)
            buttons = window.findChildren(QPushButton)

            self.assertEqual(report["syntax_error"], "")
            self.assertTrue(
                any(item["code"] == "no_exit_path" for item in report["structure"])
            )
            self.assertTrue(report["loop_risks"])
            self.assertTrue(
                any("检查步骤语法" in button.toolTip() for button in buttons)
            )

    def test_script_check_collects_errors_from_multiple_steps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            cfg = window.get_current_ui_config()
            tasks = [
                {"type": 5.0, "value": ""},
                {"type": 5.0, "value": "not-a-number"},
                {"type": 999.0, "value": "x"},
            ]
            report = window.workflow_review_controller.script_check_report(tasks, cfg)

            self.assertGreaterEqual(len(report["syntax_errors"]), 3)
            self.assertTrue(any("第 1 步" in item for item in report["syntax_errors"]))
            self.assertTrue(any("第 2 步" in item for item in report["syntax_errors"]))
            self.assertTrue(any("第 3 步" in item for item in report["syntax_errors"]))

    def test_single_step_request_removes_loops_waits_and_branches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            cfg = window.get_current_ui_config()
            tasks, single_cfg = window.build_single_step_request(
                {
                    "type": 5.0,
                    "value": "0",
                    "retry": -1,
                    "repeat_mode": "无限重复",
                    "no_skip_wait": True,
                    "success_jump": "8",
                    "fail_jump": "3",
                },
                cfg,
            )
            task = tasks[0]
            self.assertEqual(task["retry"], 1)
            self.assertEqual(task["repeat_mode"], "执行一次")
            self.assertFalse(task["no_skip_wait"])
            self.assertEqual(task["success_jump"], "0")
            self.assertEqual(task["fail_jump"], "0")
            self.assertEqual(single_cfg["loop_mode"], "单次")
            self.assertEqual(single_cfg["start_step"], "1")

    @unittest.skipIf(_OFFSCREEN, "requires real event loop for engine execution")
    def test_single_step_button_launches_only_the_selected_step(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row(
                {
                    "type": 5.0,
                    "value": "0",
                    "repeat_mode": "无限重复",
                    "success_jump": "1",
                }
            )
            window.task_list.setCurrentRow(0)
            with mock.patch.object(
                window.run_controller, "start", return_value=(True, "")
            ) as start:
                window.run_selected_step_once()

            launched_tasks = start.call_args.args[0]
            self.assertEqual(len(launched_tasks), 1)
            self.assertEqual(launched_tasks[0]["repeat_mode"], "执行一次")
            self.assertEqual(launched_tasks[0]["success_jump"], "0")
            self.assertFalse(window.single_step_btn.isEnabled())
            window.on_finish()
            self.assertTrue(window.single_step_btn.isEnabled())

    @unittest.skipIf(_OFFSCREEN, "requires real event loop for focus interaction")
    def test_running_locks_editor_and_finish_does_not_steal_focus(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row({"type": 5.0, "value": "0"})
            row = window.task_list.itemWidget(window.task_list.item(0))
            original_drag_mode = window.task_list.dragDropMode()
            window.settings_dialog.show()
            self.app.processEvents()

            with mock.patch.object(
                window.run_controller, "start", return_value=(True, "")
            ), mock.patch.object(
                RPAWindow, "showNormal"
            ) as show_normal, mock.patch.object(
                RPAWindow, "activateWindow"
            ) as activate:
                window.start_task()

                self.assertTrue(window._runtime_editing_locked)
                self.assertFalse(window.profile_combo.isEnabled())
                self.assertFalse(window.add_step_btn.isEnabled())
                self.assertFalse(window.record_btn.isEnabled())
                self.assertFalse(window.settings_scroll.isEnabled())
                self.assertFalse(row.type_combo.isEnabled())
                self.assertFalse(row.value_input.isEnabled())
                self.assertEqual(
                    window.task_list.dragDropMode(),
                    QAbstractItemView.NoDragDrop,
                )
                window.add_row({"type": 5.0, "value": "1"})
                self.assertEqual(window.task_list.count(), 1)

                window.on_finish()

                self.assertFalse(window._runtime_editing_locked)
                self.assertTrue(window.profile_combo.isEnabled())
                self.assertTrue(window.add_step_btn.isEnabled())
                self.assertTrue(window.record_btn.isEnabled())
                self.assertTrue(window.settings_scroll.isEnabled())
                self.assertTrue(row.type_combo.isEnabled())
                self.assertTrue(row.value_input.isEnabled())
                self.assertEqual(window.task_list.dragDropMode(), original_drag_mode)
                show_normal.assert_not_called()
                activate.assert_not_called()

    def test_invalid_profile_preflight_does_not_change_current_ui(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.conf_edit.setText("0.77")
            window.add_row({"type": 5.0, "value": "0"})
            before_tasks = window.snapshot_tasks()
            invalid = default_profile_config()
            invalid["conf"] = "0.55"
            invalid["ui_scale"] = "not-a-number"

            applied = window.apply_ui_config(invalid)

            self.assertFalse(applied)
            self.assertEqual(window.conf_edit.text(), "0.77")
            self.assertEqual(window.snapshot_tasks(), before_tasks)
            self.assertIn("界面倍率", window._last_config_apply_error)

    def test_profile_apply_rolls_back_after_mid_apply_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.conf_edit.setText("0.77")
            window.add_row({"type": 5.0, "value": "0"})
            before_tasks = window.snapshot_tasks()
            target = default_profile_config()
            target["conf"] = "0.55"
            target["tasks"] = [{"type": 5.0, "value": "1"}]
            original_apply_region = window.apply_scan_region_config
            call_count = 0

            def fail_once(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("injected apply failure")
                return original_apply_region(*args, **kwargs)

            with mock.patch.object(
                window, "apply_scan_region_config", side_effect=fail_once
            ):
                applied = window.apply_ui_config(target)

            self.assertFalse(applied)
            self.assertGreaterEqual(call_count, 2)
            self.assertEqual(window.conf_edit.text(), "0.77")
            self.assertEqual(window.snapshot_tasks(), before_tasks)
            self.assertIn("injected apply failure", window._last_config_apply_error)

    def test_invalid_profile_switch_restores_model_combo_and_ui(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            original_name = window.current_profile_name
            window.conf_edit.setText("0.77")
            invalid = default_profile_config()
            invalid["conf"] = "0.55"
            invalid["ui_scale"] = "999"
            window.profiles_data["损坏方案"] = invalid
            window.profile_combo.addItem("损坏方案")

            with mock.patch.object(QMessageBox, "warning") as warning:
                window.profile_combo.setCurrentText("损坏方案")
                self.app.processEvents()

            self.assertEqual(window.current_profile_name, original_name)
            self.assertEqual(window.profile_combo.currentText(), original_name)
            self.assertEqual(window.conf_edit.text(), "0.77")
            warning.assert_called_once()
            self.assertIn("安全校验", warning.call_args.args[2])

    def test_import_rejects_semantically_invalid_profile_before_creation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            invalid_path = os.path.join(temp_dir, "invalid.json")
            invalid = default_profile_config()
            invalid["cpu_refresh_interval"] = 1234
            with open(invalid_path, "w", encoding="utf-8") as handle:
                json.dump(invalid, handle, ensure_ascii=False)
            original_profiles = list(window.profiles_data)

            with mock.patch.object(
                QFileDialog,
                "getOpenFileName",
                return_value=(invalid_path, "JSON (*.json)"),
            ), mock.patch.object(QMessageBox, "warning") as warning:
                window.workflow_transfer_controller.load()

            self.assertEqual(list(window.profiles_data), original_profiles)
            warning.assert_called_once()
            self.assertIn("刷新频率", warning.call_args.args[2])


    def test_coordinate_picker_reference_cleared_after_pick_completes(self):
        """Regression: picker success callback must clear the dangling reference."""
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row({"type": 1.0, "value": ""})
            row = window.task_list.itemWidget(window.task_list.item(0))

            fake_picker = mock.Mock()
            fake_picker.destroyed = mock.Mock()
            fake_picker.destroyed.connect = mock.Mock()
            with mock.patch(
                "fukua_rpa.ui.task_row_mixins.picker.CoordinatePickerUI",
                return_value=fake_picker,
            ):
                row.start_coordinate_pick()

            self.assertIs(row.coordinate_picker, fake_picker)
            # Simulate successful pick callback
            row.on_coordinate_picked("100,200")
            self.assertIsNone(row.coordinate_picker)

    def test_coordinate_picker_reference_cleared_on_destroyed_signal(self):
        """Regression: picker destroyed (cancel path) must clear the reference."""
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row({"type": 1.0, "value": ""})
            row = window.task_list.itemWidget(window.task_list.item(0))

            destroyed_callbacks = []
            fake_picker = mock.Mock()
            fake_picker.destroyed = mock.Mock()
            fake_picker.destroyed.connect = lambda cb: destroyed_callbacks.append(cb)
            with mock.patch(
                "fukua_rpa.ui.task_row_mixins.picker.CoordinatePickerUI",
                return_value=fake_picker,
            ):
                row.start_coordinate_pick()

            self.assertIs(row.coordinate_picker, fake_picker)
            # Simulate the destroyed signal firing (cancel/close path)
            self.assertTrue(destroyed_callbacks)
            destroyed_callbacks[0]()
            self.assertIsNone(row.coordinate_picker)

    def test_capture_done_and_failed_guard_against_dead_row(self):
        """Regression: async capture callbacks must not crash on deleted row."""
        from fukua_rpa.ui.task_row_controllers.template_capture_controller import (
            TemplateCaptureController,
        )

        mock_row = mock.Mock()
        controller = TemplateCaptureController(mock_row)
        controller._active_signals = mock.Mock()

        with mock.patch(
            "fukua_rpa.ui.task_row_controllers.template_capture_controller.widget_is_alive",
            return_value=False,
        ):
            # Should return silently without touching the dead row
            controller._on_capture_done("/fake/path.png", mock.Mock(), "/fake")
            controller._on_capture_failed("some error")

        # row.setToolTip should NOT have been called since widget is dead
        mock_row.setToolTip.assert_not_called()
        self.assertIsNone(controller._active_signals)

    def test_clear_window_identity_check_prevents_stale_clear(self):
        """Regression: old RegionWindow destroyed must not clear a newer window."""
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row({"type": 1.0, "value": ""})
            row = window.task_list.itemWidget(window.task_list.item(0))
            controller = row.template_capture_controller

            # Simulate first window
            old_window = mock.Mock()
            row.template_region_window = old_window

            # Simulate second (newer) window replacing the first
            new_window = mock.Mock()
            row.template_region_window = new_window

            # Old window's destroyed signal fires with identity check
            controller.clear_window(old_window)
            # The new window reference must NOT be cleared
            self.assertIs(row.template_region_window, new_window)

            # Current window's destroyed signal fires
            controller.clear_window(new_window)
            # Now it should be cleared
            self.assertIsNone(row.template_region_window)

    def test_plugin_boolean_param_string_values_parsed_explicitly(self):
        """FR-20260809-0901: 字符串形态 boolean 参数显式解析——"false"/"0" 不得被
        bool() 误判为 True（修复前 bool("false")==True，控件误勾选并回写 True）。"""
        from fukua_rpa.ui.plugin_action_settings import PluginActionSettings

        # 静态调用 _make_widget（该方法不依赖实例状态）。
        self.assertFalse(PluginActionSettings._make_widget(None, "boolean", "false").isChecked())
        self.assertFalse(PluginActionSettings._make_widget(None, "boolean", "0").isChecked())
        self.assertFalse(PluginActionSettings._make_widget(None, "boolean", "off").isChecked())
        self.assertFalse(PluginActionSettings._make_widget(None, "boolean", "").isChecked())
        self.assertTrue(PluginActionSettings._make_widget(None, "boolean", "true").isChecked())
        self.assertTrue(PluginActionSettings._make_widget(None, "boolean", "on").isChecked())
        self.assertTrue(PluginActionSettings._make_widget(None, "boolean", "1").isChecked())
        self.assertTrue(PluginActionSettings._make_widget(None, "boolean", True).isChecked())

    def test_plugin_array_object_and_enum_params_are_configurable(self):
        """FR-20261003-603 / FR-20261003-604：插件参数表单必须能产出合法 plugin_config。

        修复前：array/object 落 QLineEdit（收集为 str），且不读取 schema 的
        default/enum/minimum，导致界面默认态即被运行前校验阻断。
        """
        from PySide6.QtWidgets import QComboBox, QFormLayout, QLineEdit

        from fukua_rpa.plugin_actions import validate_plugin_config
        from fukua_rpa.ui.plugin_action_settings import PluginActionSettings

        schema = {
            "type": "object",
            "properties": {
                "target_text": {"type": "string", "title": "文字"},
                "targets": {"type": "array", "items": {"type": "string"}, "title": "候选"},
                "payload": {"type": "object", "title": "载荷"},
                "match_mode": {
                    "type": "string",
                    "enum": ["contains", "equals", "regex"],
                    "default": "contains",
                    "title": "模式",
                },
                "timeout_ms": {"type": "integer", "minimum": 100, "default": 30000, "title": "超时"},
            },
        }

        class _Action:
            input_schema = schema

        class _Registry:
            def get_action(self, plugin_id, action_id):
                return _Action()

        def build(current):
            obj = PluginActionSettings.__new__(PluginActionSettings)
            obj._param_layout = QFormLayout()
            obj._param_widgets = []
            obj._build_params(schema, current)
            return obj

        def widget_of(obj, key):
            return dict(obj._param_widgets)[key]

        # array/object → JSON 文本编辑器
        obj = build({})
        self.assertIsInstance(widget_of(obj, "targets"), QLineEdit)
        self.assertTrue(widget_of(obj, "targets").property("plugin_json_kind"))
        self.assertTrue(widget_of(obj, "payload").property("plugin_json_kind"))

        # enum → 下拉框，默认值来自 schema
        self.assertIsInstance(widget_of(obj, "match_mode"), QComboBox)
        self.assertEqual(widget_of(obj, "match_mode").currentData(), "contains")
        # minimum/default 生效
        self.assertGreaterEqual(widget_of(obj, "timeout_ms").minimum(), 100)
        self.assertEqual(widget_of(obj, "timeout_ms").value(), 30000)

        # 默认态通过运行前校验（修复前会因 "targets" 为空串而阻断）
        cfg = obj._collect_config()
        self.assertNotIn("targets", cfg)  # 留空按「未提供」处理
        self.assertIsNone(validate_plugin_config("p", "a", cfg, _Registry()))

        # 合法 JSON 数组被反序列化
        obj2 = build({})
        widget_of(obj2, "targets").setText('["a", "b"]')
        cfg2 = obj2._collect_config()
        self.assertEqual(cfg2["targets"], ["a", "b"])
        self.assertIsNone(validate_plugin_config("p", "a", cfg2, _Registry()))

        # 非法 JSON 给出 ValueError（不静默转换）
        obj3 = build({})
        widget_of(obj3, "targets").setText("not-json")
        with self.assertRaises(ValueError):
            obj3._collect_config()

        # 已有配置值优先于 default；历史越界 enum 值被保留不静默改写
        obj4 = build({"match_mode": "legacy", "timeout_ms": 500})
        self.assertEqual(widget_of(obj4, "match_mode").currentData(), "legacy")
        self.assertEqual(widget_of(obj4, "timeout_ms").value(), 500)

    def test_capture_and_apply_buttons_are_wired(self):
        """FR-20260809-0907: 系统设置区的热键录入按钮必须绑定到正确槽
        （修复前仅断言按钮宽度、未验证绑定接线）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            # 热键录入按钮 → capture_start_hotkey / capture_stop_hotkey
            with mock.patch.object(
                window, "capture_start_hotkey"
            ) as start_slot, mock.patch.object(
                window, "capture_stop_hotkey"
            ) as stop_slot:
                window.start_key_btn.click()
                start_slot.assert_called_once_with()
                window.stop_key_btn.click()
                stop_slot.assert_called_once_with()

    def test_three_system_settings_feed_dirty_marker(self):
        """FR-20260812-D12-002: step_adjust_follow / sound_alert_audio_path /
        sound_alert_play_seconds 必须接入 log_setting_change → mark_profile_dirty
        链，改动触发自动保存脏标记。"""
        from fukua_rpa.ui.controllers.log_controller import LogController

        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            LogController(window)
            with mock.patch.object(
                window, "mark_profile_dirty"
            ) as mark_dirty:
                # 复选框
                window.step_adjust_follow_chk.setChecked(False)
                # 音频路径（文本编辑）
                window.sound_alert_audio_path_edit.setText("C:/tmp/a.wav")
                window.sound_alert_audio_path_edit.editingFinished.emit()
                # 播放时长（文本编辑）
                window.sound_alert_play_seconds_edit.setText("45")
                window.sound_alert_play_seconds_edit.editingFinished.emit()
                self.assertGreaterEqual(mark_dirty.call_count, 1)

    def test_multi_target_and_input_settings_feed_dirty_marker(self):
        """同根因守护：多目标收集与输入方式相关的新设置必须接入
        log_setting_change → mark_profile_dirty 链，编辑后触发自动保存脏标记
        （否则崩溃/强杀会静默丢失这些设置）。"""
        from fukua_rpa.ui.controllers.log_controller import LogController

        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            LogController(window)
            with mock.patch.object(window, "mark_profile_dirty") as mark_dirty:
                for edit, value in (
                    (window.multi_collect_first_edit, "0.5"),
                    (window.multi_collect_last_edit, "0.5"),
                    (window.multi_collect_max_edit, "12"),
                    (window.multi_collect_targets_edit, "3"),
                    (window.mouse_move_settle, "0.05"),
                ):
                    edit.setText(value)
                    edit.editingFinished.emit()
                for combo, text in (
                    (window.multi_order_b_combo, "从右到左"),
                    (window.input_backend_combo, "SendInput"),
                ):
                    combo.setCurrentText(text)
                for chk in (
                    window.multi_collect_partial_chk,
                    window.keyboard_scan_code_chk,
                ):
                    chk.setChecked(not chk.isChecked())

                self.assertGreaterEqual(
                    mark_dirty.call_count,
                    9,
                    f"多目标/输入相关新设置必须全部触发脏标记，实际 {mark_dirty.call_count} 次",
                )

    def test_multi_collect_targets_value_tolerates_non_finite_text(self):
        """FR-20261010-2701: 「最多点击目标数」容错解析不得抛 OverflowError
        （inf/1e999 曾让 update_multi_target_ui 槽异常、partial 复选框启用态
        失真）；容错口径与运行前校验 parse_float_text 一致。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.multi_mode_combo.setCurrentText("全部匹配")
            for text, expect_enabled in (
                ("1e999", False),
                ("inf", False),
                ("-inf", False),
                ("abc", False),
                ("-5", False),
                ("3", True),
            ):
                window.multi_collect_targets_edit.setText(text)
                window.update_multi_target_ui()
                self.assertEqual(
                    window.multi_collect_partial_chk.isEnabled(),
                    expect_enabled,
                    f"输入 {text!r} 时“目标不足时点击现有目标”启用态应一致且不抛异常",
                )

    def test_plugin_uninstall_failure_shows_warning_not_escape(self):
        """FR-20261010-2602: 插件目录被占用等 OSError 不得从卸载槽逃逸到
        Qt——与 enable/disable 的 FR-20260809-1205 兜底同标准：失败必须出现
        可见警告，注册表与界面保持一致。"""
        import os

        from PySide6.QtCore import QSettings

        from fukua_rpa.plugin_registry import PluginRegistry
        from fukua_rpa.ui.controllers.plugin_controller import PluginController
        from fukua_rpa.ui.plugin_center_window import PluginCenterWindow

        with tempfile.TemporaryDirectory() as temp_dir:
            registry = PluginRegistry(temp_dir, "v1.0.171")
            controller = PluginController(registry, temp_dir)
            settings = QSettings(
                os.path.join(temp_dir, "ui.ini"), QSettings.Format.IniFormat
            )
            window = PluginCenterWindow(controller, temp_dir, settings)
            self.addCleanup(dispose_widget, self.app, window)
            with mock.patch.object(
                controller, "usage_count", return_value=0
            ), mock.patch.object(
                controller, "uninstall", side_effect=OSError("插件目录被占用")
            ), mock.patch.object(
                QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
            ) as question, mock.patch.object(
                QMessageBox, "warning"
            ) as warning:
                window._confirm_uninstall("demo")
            question.assert_called_once()
            warning.assert_called_once()

    def test_previous_unclean_shutdown_shows_notice(self):
        """FR-20260812 建议-架构-4: 上次会话未正常退出时，窗口显示后弹一次性
        提示；正常退出不弹。"""
        from fukua_rpa.ui.main_window import RPAWindow

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "fukua_rpa.ui.main_window.ApplicationSession"
        ) as session_type:
            session = session_type.return_value
            session.previous_unclean = True
            session_type.return_value.start.return_value = session
            with mock.patch.object(
                QMessageBox, "information"
            ) as info:
                window = RPAWindow(base_dir=temp_dir, defer_runtime=True)
                window._ensure_settings_dialog()  # C-1: 对话框改为懒构建
                self.addCleanup(dispose_widget, self.app, window)
                # 触发延迟 800ms 的提示
                QTest.qWait(1000)
                info.assert_called()
                text = " ".join(str(a) for call in info.call_args_list for a in call.args)
                self.assertIn("未正常退出", text)

    def test_clean_shutdown_does_not_show_notice(self):
        """FR-20260812 建议-架构-4: 上次会话正常退出时不弹提示（负路径）。"""
        from fukua_rpa.ui.main_window import RPAWindow

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "fukua_rpa.ui.main_window.ApplicationSession"
        ) as session_type:
            session = session_type.return_value
            session.previous_unclean = False
            session_type.return_value.start.return_value = session
            with mock.patch.object(QMessageBox, "information") as info:
                window = RPAWindow(base_dir=temp_dir, defer_runtime=True)
                window._ensure_settings_dialog()  # C-1: 对话框改为懒构建
                self.addCleanup(dispose_widget, self.app, window)
                QTest.qWait(1000)
                info.assert_not_called()

    def test_drag_summary_uses_unsupported_label_for_unknown_type(self):
        """FR-20260812 建议-体验-2: 拖拽摘要对未注册动作类型显示与实体行一致的
        unsupported 标签，不得显示"未知操作"。"""
        from fukua_rpa.ui.task_row import task_drag_summary

        summary = task_drag_summary(
            {"type": 999.0, "value": "legacy-params"}, 3
        )
        self.assertIn("当前版本不支持", summary)
        self.assertNotIn("未知操作", summary)
        # 已知类型保持原样
        normal = task_drag_summary({"type": 1.0, "value": "10,20"}, 1)
        self.assertIn("左键单击", normal)


class _ReviewHostStub:
    """Weak-referenceable minimal host: ``WorkflowReviewController`` 只保存弱引用。"""


class ScriptCheckStartStepParsingTests(unittest.TestCase):
    """REVIEW-20260913-001 FR-002（非阻断建议 FR-002-UI-START-STEP）。

    UI 静态检查路径的 ``start_step`` 此前用裸 ``int(float(...))`` 解析，仅捕获
    ``(TypeError, ValueError)``；``"inf"``/``"1e999"`` 经 ``float()`` 通过但 ``int()``
    抛 ``OverflowError``，会让静态检查报告整体失败。这里固定"非有限值回退 1、
    合法值保留"的行为，为该修复补上回归守护。
    """

    def _controller(self):
        from fukua_rpa.ui.controllers.workflow_review_controller import (
            WorkflowReviewController,
        )

        controller = WorkflowReviewController(_ReviewHostStub())
        controller.validate_tasks_all = lambda tasks, cfg: []
        controller.analyze_loop_risks = lambda tasks, cfg: []
        return controller

    def _report(self, cfg):
        from fukua_rpa.ui.controllers import workflow_review_controller as module

        controller = self._controller()
        captured = {}

        def fake_structure(tasks, start_step=None):
            captured["start_step"] = start_step
            return []

        with mock.patch.object(module, "analyze_workflow_structure", fake_structure):
            report = controller.script_check_report([], cfg)
        return captured.get("start_step"), report

    def test_non_finite_start_step_falls_back_to_one(self):
        for raw in ("inf", "-inf", "nan", "1e999", "Infinity", "abc", None):
            with self.subTest(start_step=raw):
                start_step, report = self._report({"start_step": raw})
                self.assertEqual(start_step, 1, f"start_step={raw!r} 必须回退为 1")
                self.assertEqual(report["syntax_error"], "")

    def test_valid_start_step_is_preserved(self):
        for raw, expected in (("3", 3), (2, 2), ("1", 1)):
            with self.subTest(start_step=raw):
                start_step, _report = self._report({"start_step": raw})
                self.assertEqual(start_step, expected)


if __name__ == "__main__":
    unittest.main()
