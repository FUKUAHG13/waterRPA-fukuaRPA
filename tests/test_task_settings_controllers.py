import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from fukua_rpa.constants import UNTIL_LIMIT_ACTIONS
from fukua_rpa.ui.components import HelpBtn
from fukua_rpa.ui.image_click_point_dialog import (
    ImageClickPointDialog as ExtractedImageClickPointDialog,
)
from fukua_rpa.ui.task_row import TaskRow
from fukua_rpa.ui.task_row_controllers import (
    TaskConfigDialogController,
    TemplateCaptureController,
)
from fukua_rpa.ui.task_settings_controllers import (
    BackgroundClickSettingsController,
    CoordinateSettingsController,
    DebugSettingsController,
    ImageSettingsController,
    UntilConditionController,
)
from fukua_rpa.ui.task_settings_dialog import (
    ImageClickPointDialog,
    TaskConfigDialog,
)
from tests.qt_test_support import QtCallbackGuardMixin, dispose_widget


class TaskSettingsControllerTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def create_dialog(self, data=None, **kwargs):
        dialog = TaskConfigDialog(
            None,
            data or {},
            base_dir=self.temp_dir.name,
            **kwargs,
        )
        self.addCleanup(dispose_widget, self.app, dialog)
        return dialog

    @staticmethod
    def action_row_is_visible(dialog, row):
        form = dialog.action_row_forms.get(row, dialog.control_form)
        return form.isRowVisible(row)

    def assert_row_controls_disabled_but_help_enabled(self, row):
        """A greyed-out row must disable its editors but keep HelpBtn clickable."""
        self.assertFalse(row.property("controlsEnabled"))
        help_buttons = row.findChildren(HelpBtn)
        for button in help_buttons:
            self.assertTrue(
                button.isEnabled(),
                "HelpBtn must stay clickable even when its row is greyed out",
            )
        disabled_editors = [
            widget
            for widget in row.findChildren(QWidget)
            if not isinstance(widget, HelpBtn) and not widget.isEnabled()
        ]
        self.assertTrue(
            disabled_editors,
            "greyed-out row must still disable its editor controls",
        )

    def test_dialog_composes_interactions_without_duplicating_state(self):
        dialog = self.create_dialog()

        self.assertIsInstance(
            dialog.until_condition_controller, UntilConditionController
        )
        self.assertIsInstance(dialog.debug_settings_controller, DebugSettingsController)
        self.assertIsInstance(dialog.image_settings_controller, ImageSettingsController)
        self.assertIsInstance(
            dialog.coordinate_settings_controller, CoordinateSettingsController
        )
        self.assertIsInstance(
            dialog.background_click_settings_controller,
            BackgroundClickSettingsController,
        )
        for controller in (
            dialog.until_condition_controller,
            dialog.debug_settings_controller,
            dialog.image_settings_controller,
            dialog.coordinate_settings_controller,
            dialog.background_click_settings_controller,
        ):
            self.assertIs(controller.dialog, dialog)
        self.assertIs(ImageClickPointDialog, ExtractedImageClickPointDialog)

    def test_task_row_composes_capture_and_dialog_lifecycle_controllers(self):
        row = TaskRow(lambda _row: None)
        self.addCleanup(dispose_widget, self.app, row)

        self.assertIsInstance(
            row.template_capture_controller, TemplateCaptureController
        )
        self.assertIsInstance(row.config_dialog_controller, TaskConfigDialogController)
        self.assertIs(row.template_capture_controller.row, row)
        self.assertIs(row.config_dialog_controller.row, row)

    def test_task_row_binding_preserves_unknown_and_inactive_action_fields(self):
        row = TaskRow(lambda _row: None)
        self.addCleanup(dispose_widget, self.app, row)
        source = {
            "type": 1.0,
            "value": "10,20",
            "custom_en": True,
            "point_limit_en": True,
            "coord_step_en": True,
            "future_extension": {"items": [1, 2]},
        }

        row.set_data(source)
        source["future_extension"]["items"].append(3)
        collected = row.get_data()

        self.assertTrue(collected["custom_en"])
        self.assertTrue(collected["point_limit_en"])
        self.assertTrue(collected["coord_step_en"])
        self.assertEqual(collected["future_extension"], {"items": [1, 2]})

    def test_simple_dialog_switches_action_sections_without_losing_draft(self):
        dialog = self.create_dialog(
            {
                "custom_en": True,
                "point_limit_en": True,
                "coord_step_en": True,
                "coord_step_every": "4",
            },
            image_settings_available=True,
            point_limit_available=True,
            coordinate_step_available=False,
            image_click_point_available=False,
            step_type="左键单击",
            step_value="target.png",
            settings_mode="simple",
        )

        self.assertFalse(dialog.enable_chk.isHidden())
        # FR-UISET-001: 点击形态区（高级补充）在简易模式不得泄漏。
        self.assertTrue(dialog.click_form_section.isHidden())
        self.assertFalse(self.action_row_is_visible(dialog, dialog.point_limit_row))
        self.assertTrue(self.action_row_is_visible(dialog, dialog.image_point_row))
        self.assertFalse(self.action_row_is_visible(dialog, dialog.step_region_row))
        self.assertFalse(self.action_row_is_visible(dialog, dialog.coord_every_row))
        self.assertFalse(self.action_row_is_visible(dialog, dialog.coord_sequence_box))
        self.assertTrue(dialog.coordinate_target_section.isHidden())
        self.assertTrue(dialog.image_target_section.is_expanded())
        self.assertFalse(dialog.coordinate_target_section.is_expanded())

        dialog.update_step_context(
            image_settings_available=False,
            point_limit_available=False,
            coordinate_step_available=True,
            base_coordinate=(10, 20),
            image_path="",
            image_click_point_available=False,
            step_type="左键单击",
            screenshot_settings_available=False,
            step_value="10,20",
        )

        self.assertTrue(dialog.image_target_section.isHidden())
        self.assertFalse(self.action_row_is_visible(dialog, dialog.point_limit_row))
        self.assertFalse(self.action_row_is_visible(dialog, dialog.coord_every_row))
        self.assertTrue(self.action_row_is_visible(dialog, dialog.coord_sequence_box))
        self.assertTrue(dialog.coord_sequence_box.isEnabled())
        self.assertFalse(dialog.image_target_section.is_expanded())
        self.assertTrue(dialog.coordinate_target_section.is_expanded())
        collected = dialog.get_data()
        self.assertTrue(collected["custom_en"])
        self.assertTrue(collected["point_limit_en"])
        self.assertTrue(collected["coord_step_en"])
        self.assertEqual(collected["coord_step_every"], "4")

    def test_simple_mode_hides_key_hold_section_for_system_key(self):
        # FR-UISET-002: 系统按键的"按住时长"高级属性在简易模式下不得泄漏。
        dialog = self.create_dialog(
            {},
            step_type="系统按键",
            step_value="",
            settings_mode="simple",
        )
        self.assertTrue(dialog.key_hold_section.isHidden())
        dialog.apply_settings_mode("advanced")
        self.assertFalse(dialog.key_hold_section.isHidden())

    def test_template_mask_detail_is_advanced_only(self):
        # FR-010: docs/步骤设置显隐矩阵.md:33 约定"已忽略区域计数详情"只在高级模式
        # 出现；简易模式仅保留编辑/清除/裁剪入口。
        image_path = os.path.join(self.temp_dir.name, "mask_target.png")
        with open(image_path, "wb") as handle:
            handle.write(b"stable-package-asset")
        dialog = self.create_dialog(
            {"image_path": image_path},
            step_type="左键单击",
            step_value=image_path,
            settings_mode="simple",
        )

        self.assertTrue(dialog.template_mask_info.isHidden())
        self.assertFalse(dialog.template_mask_edit_btn.isHidden())
        self.assertFalse(dialog.template_mask_clear_btn.isHidden())

        dialog.apply_settings_mode("advanced")
        self.assertFalse(dialog.template_mask_info.isHidden())

    def test_empty_click_keeps_both_target_sections_visible_but_read_only(self):
        dialog = self.create_dialog(
            {},
            step_type="左键单击",
            step_value="",
            settings_mode="simple",
        )

        self.assertFalse(dialog.image_target_section.isHidden())
        self.assertFalse(dialog.coordinate_target_section.isHidden())
        self.assertTrue(dialog.enable_chk.isHidden())
        self.assertTrue(dialog.form_widget.isHidden())
        self.assertFalse(dialog.image_settings_note.isEnabled())
        self.assertFalse(self.action_row_is_visible(dialog, dialog.point_limit_row))
        self.assertFalse(self.action_row_is_visible(dialog, dialog.coord_every_row))
        self.assertFalse(self.action_row_is_visible(dialog, dialog.coord_sequence_box))
        self.assertFalse(dialog.image_target_section.is_expanded())
        self.assertFalse(dialog.coordinate_target_section.is_expanded())
        self.assertIn("填写图片路径或屏幕坐标", dialog.context_note.text())

        dialog.apply_settings_mode("advanced")
        self.assertTrue(dialog.mouse_trail_section.isHidden())

    def test_custom_sequence_preview_drag_writes_directly_to_sequence_text(self):
        dialog = self.create_dialog(
            {
                "coord_sequence_en": True,
                "coord_sequence_points": "10,20; 30,40",
            },
            coordinate_step_available=True,
            base_coordinate=(10, 20),
            step_type="左键单击",
            step_value="10,20",
            settings_mode="simple",
        )

        points = dialog.coordinate_settings_controller.on_sequence_preview_point_moved(
            1, 55, 66
        )
        self.assertEqual(points, [(10, 20), (55, 66)])
        # Text-widget write-back is deferred to mouse release (v1.0.29 perf
        # fix); the callback returns the correct points immediately but the
        # expensive serialization no longer runs on every drag frame.
        self.assertEqual(dialog.coord_sequence_text.toPlainText(), "10,20; 30,40")

    def test_custom_sequence_batch_controls_are_saved_without_generic_repeat(self):
        dialog = self.create_dialog(
            {
                "coord_sequence_en": True,
                "coord_sequence_points": "10,20; 30,40; 50,60",
                "coord_sequence_batch_mode": "count",
                "coord_sequence_batch_count": "2",
                "repeat_mode": "无限重复",
            },
            coordinate_step_available=True,
            base_coordinate=(10, 20),
            step_type="左键单击",
            step_value="10,20",
            settings_mode="simple",
        )

        self.assertFalse(dialog.repeat_combo.isEnabled())
        self.assertTrue(dialog.coord_sequence_batch_count_edit.isEnabled())
        data = dialog.get_data()
        self.assertEqual(data["coord_sequence_batch_mode"], "count")
        self.assertEqual(data["coord_sequence_batch_count"], "2")
        self.assertEqual(data["repeat_mode"], "无限重复")

    def test_opening_a_second_coordinate_preview_closes_the_first(self):
        first = self.create_dialog(
            {"coord_sequence_en": True, "coord_sequence_points": "20,20"},
            coordinate_step_available=True,
            base_coordinate=(20, 20),
            step_type="左键单击",
            step_value="20,20",
        )
        second = self.create_dialog(
            {"coord_sequence_en": True, "coord_sequence_points": "40,40"},
            coordinate_step_available=True,
            base_coordinate=(40, 40),
            step_type="左键单击",
            step_value="40,40",
        )
        first.preview_coord_sequence()
        first_preview = first.coord_step_preview
        second.preview_coord_sequence()
        self.app.processEvents()

        self.assertFalse(isValid(first_preview))
        self.assertTrue(second.coord_step_preview.isVisible())

    def test_coordinate_preview_accepts_drag_from_the_native_hit_area(self):
        dialog = self.create_dialog(
            {"coord_sequence_en": True, "coord_sequence_points": "40,40"},
            coordinate_step_available=True,
            base_coordinate=(40, 40),
            step_type="左键单击",
            step_value="40,40",
        )
        dialog.preview_coord_sequence()
        preview = dialog.coord_step_preview
        start = preview.to_screen_point(40, 40)
        target = QPoint(start.x() + 30, start.y() + 25)
        QTest.mousePress(preview, Qt.LeftButton, pos=start)
        QTest.mouseMove(preview, target, delay=1)
        QTest.mouseRelease(preview, Qt.LeftButton, pos=target)
        self.app.processEvents()

        self.assertNotEqual(dialog.coord_sequence_points()[0], (40, 40))

    def test_background_click_binding_requires_explicit_window_selection(self):
        dialog = self.create_dialog(
            {},
            coordinate_step_available=True,
            base_coordinate=(320, 240),
            step_type="左键单击",
            step_value="320,240",
            settings_mode="advanced",
        )
        backend = mock.Mock()
        backend.create_binding_for_window_at_point.return_value = {
            "root_hwnd": 700,
            "root_title": "目标程序",
            "root_client_x": 120,
            "root_client_y": 80,
        }
        dialog.background_click_settings_controller.backend = backend

        dialog.on_background_window_selected("900,500")

        backend.create_binding_for_window_at_point.assert_called_once_with(
            900, 500, 320, 240
        )
        self.assertTrue(dialog.background_click_chk.isChecked())
        self.assertEqual(dialog.background_click_binding["source_x"], 320)
        self.assertEqual(dialog.background_click_binding["source_y"], 240)
        self.assertIn("已绑定：目标程序", dialog.background_click_status.text())

        dialog.update_step_context(
            image_settings_available=False,
            point_limit_available=False,
            coordinate_step_available=True,
            base_coordinate=(330, 250),
            image_path="",
            image_click_point_available=False,
            step_type="左键单击",
            screenshot_settings_available=False,
            step_value="330,250",
        )
        self.assertIn("坐标已变化", dialog.background_click_status.text())

    def test_advanced_dialog_hides_irrelevant_action_sections(self):
        dialog = self.create_dialog(
            {},
            image_settings_available=False,
            point_limit_available=False,
            coordinate_step_available=False,
            screenshot_settings_available=False,
            step_type="等待(秒)",
            step_value="1",
            settings_mode="advanced",
        )

        for row in (
            dialog.point_limit_row,
            dialog.image_point_row,
            dialog.step_region_row,
            dialog.capture_region_row,
            dialog.coord_every_row,
            dialog.coord_sequence_box,
        ):
            self.assertFalse(self.action_row_is_visible(dialog, row))
        self.assertTrue(dialog.image_target_section.isHidden())
        self.assertTrue(dialog.coordinate_target_section.isHidden())
        self.assertTrue(dialog.until_group.isHidden())

    def test_step_setting_form_labels_share_left_alignment(self):
        dialog = self.create_dialog(
            {},
            step_type="左键单击",
            step_value="10,20",
            settings_mode="advanced",
        )

        self.assertTrue(dialog.control_form.labelAlignment() & Qt.AlignLeft)
        self.assertTrue(
            dialog.coordinate_target_form.labelAlignment() & Qt.AlignLeft
        )
        self.assertTrue(dialog.image_target_form.labelAlignment() & Qt.AlignLeft)

    def test_drag_shows_curve_and_trail_but_hides_click_only_shape_fields(self):
        dialog = self.create_dialog(
            {},
            step_type="左键拖拽",
            step_value="10,20 -> 30,40",
            settings_mode="advanced",
        )

        self.assertFalse(dialog.click_form_section.isHidden())
        self.assertTrue(dialog.click_spatial_group.isHidden())
        self.assertFalse(dialog.move_curve_group.isHidden())
        self.assertTrue(dialog.click_return_group.isHidden())
        self.assertFalse(dialog.mouse_trail_section.isHidden())

    def test_until_settings_are_visible_only_for_the_until_step(self):
        waiting = self.create_dialog(
            {},
            step_type="等待(秒)",
            step_value="1",
            settings_mode="advanced",
        )
        self.assertTrue(waiting.until_group.isHidden())

        until = self.create_dialog(
            {},
            step_type="直到条件成立",
            step_value="",
            settings_mode="advanced",
        )
        self.assertFalse(until.until_group.isHidden())
        self.assertTrue(until.until_group.isEnabled())

    def test_until_on_limit_legacy_value_is_not_silently_rewritten(self):
        """FR-20261003-609: 旧方案保存的「按失败处理」不得被界面静默改写。

        修复前：下拉只加前两项，setCurrentText 在非可编辑 QComboBox 上找不到
        匹配项而静默落回「继续下一步」，用户打开设置即保存就把磁盘值改掉，
        且界面显示与保存值不一致（状态误导）。
        """
        legacy = "按失败处理"
        self.assertIn(legacy, UNTIL_LIMIT_ACTIONS)
        dialog = self.create_dialog(
            {"type": 15.0, "until_on_limit": legacy},
            step_type="直到条件成立",
            settings_mode="advanced",
        )
        combo = dialog.until_on_limit_combo
        # 显示与保存值一致
        self.assertEqual(combo.currentText(), legacy)
        # 作为只读占位项存在且被禁用（不可重新选为可执行选项）
        self.assertEqual(combo.count(), 3)
        placeholder = combo.model().item(combo.count() - 1)
        self.assertIsNotNone(placeholder)
        self.assertFalse(placeholder.isEnabled())
        # 打开并立即保存：值不被改写
        self.assertEqual(dialog.get_data().get("until_on_limit"), legacy)

    def test_until_on_limit_defaults_to_continue_for_new_step(self):
        """FR-20261003-609: 新步骤默认仍为「继续下一步」，界面不新增可执行旧取值。"""
        dialog = self.create_dialog(
            {"type": 15.0},
            step_type="直到条件成立",
            settings_mode="advanced",
        )
        combo = dialog.until_on_limit_combo
        self.assertEqual(combo.currentText(), "继续下一步")
        selectable = [
            combo.itemText(i)
            for i in range(combo.count())
            if combo.model().item(i).isEnabled()
        ]
        self.assertNotIn("按失败处理", selectable)

    def test_action_visibility_preserves_feature_level_enable_rules(self):
        image_dialog = self.create_dialog(
            {
                "point_limit_en": False,
                "image_click_point_en": True,
                "step_region_en": False,
            },
            image_settings_available=True,
            point_limit_available=True,
            coordinate_step_available=False,
            image_click_point_available=False,
            step_type="左键单击",
            step_value="missing.png",
            settings_mode="simple",
        )
        self.assertTrue(image_dialog.point_limit_chk.isEnabled())
        self.assertFalse(image_dialog.point_limit_count_edit.isEnabled())
        self.assertFalse(image_dialog.image_click_point_chk.isEnabled())
        self.assertFalse(image_dialog.image_click_point_select_btn.isEnabled())
        self.assertFalse(image_dialog.step_region_edit.isEnabled())

        coordinate_dialog = self.create_dialog(
            {"coord_step_en": False, "coord_sequence_en": False},
            image_settings_available=False,
            point_limit_available=False,
            coordinate_step_available=True,
            base_coordinate=(10, 20),
            step_type="左键单击",
            step_value="10,20",
            settings_mode="simple",
        )
        self.assertTrue(coordinate_dialog.coord_step_chk.isEnabled())
        self.assertFalse(coordinate_dialog.coord_step_every_edit.isEnabled())
        self.assertTrue(coordinate_dialog.coord_sequence_chk.isEnabled())
        self.assertFalse(coordinate_dialog.coord_sequence_text.isEnabled())

    def test_unknown_action_round_trip_is_preserved_and_blocked(self):
        row = TaskRow(lambda _row: None)
        self.addCleanup(dispose_widget, self.app, row)
        source = {
            "type": 999.0,
            "value": "future payload",
            "future_extension": {"items": [1, 2]},
        }

        row.set_data(source)
        self.assertIn("当前版本不支持", row.type_combo.currentText())
        self.assertTrue(row.value_input.isReadOnly())
        self.assertEqual(row.get_data()["type"], 999.0)
        self.assertEqual(
            row.get_data()["future_extension"], {"items": [1, 2]}
        )

        row.set_settings_mode("advanced")
        self.assertIn("当前版本不支持", row.type_combo.currentText())
        self.assertEqual(row.get_data()["type"], 999.0)

        row.type_combo.setCurrentText("左键单击")
        self.app.processEvents()
        self.assertFalse(row.value_input.isReadOnly())
        self.assertEqual(row.get_data()["type"], 1.0)

    def test_until_limit_combo_matches_documented_choices(self):
        """FR-SCOPE013-003 / FR-SCOPE001-001：界面不提供「按失败处理」。

        权威依据 `docs/步骤设置显隐矩阵.md`「直到条件成立 | … | 不叠加普通分支」。
        直到步骤的能力表已移除 failure_policy / failure_branch，该取值委托的失败分支
        在定义侧不可达，因此界面只提供「继续下一步」「停止脚本」。
        常量域仍保留第三项，供旧方案加载（校验侧断言见
        tests/test_validation_coverage.py::UntilOverlayAndStabilityValidationTests）。
        """
        dialog = self.create_dialog(
            {},
            step_type="直到条件成立",
            step_value="",
            settings_mode="advanced",
        )
        combo = dialog.until_on_limit_combo
        items = [combo.itemText(i) for i in range(combo.count())]

        self.assertEqual(items, UNTIL_LIMIT_ACTIONS[:2])
        self.assertNotIn("按失败处理", items)
        # 常量域不得因界面隐藏而删除该取值（否则旧方案会被校验器拒绝）。
        self.assertIn("按失败处理", UNTIL_LIMIT_ACTIONS)

    def test_until_condition_mode_and_region_share_dialog_controls(self):
        dialog = self.create_dialog(
            {"until_cond1_mode": "图片出现"},
            step_type="直到条件成立",
        )
        controls = dialog.until_condition_widgets[1]

        self.assertTrue(controls["image"].isEnabled())
        self.assertTrue(controls["conf"].isEnabled())
        self.assertFalse(controls["diff"].isEnabled())

        controls["mode"].setCurrentText("区域发生变化")
        self.app.processEvents()
        self.assertFalse(controls["image"].isEnabled())
        self.assertTrue(controls["diff"].isEnabled())
        self.assertEqual(controls["region"].placeholderText(), "必填区域 x,y,w,h")

        dialog.on_until_region_selected(1, (10, 20, 300, 180))
        self.assertEqual(controls["region"].text(), "10,20,300,180")

    def test_until_condition_sub_region_visible_only_for_region_changed(self):
        dialog = self.create_dialog(
            {"until_cond1_mode": "图片出现"},
            step_type="直到条件成立",
        )
        controls = dialog.until_condition_widgets[1]

        # 非“区域发生变化”模式时子区域行隐藏。
        self.assertTrue(controls["sub_region_row"].isHidden())

        controls["mode"].setCurrentText("区域发生变化")
        self.app.processEvents()
        self.assertFalse(controls["sub_region_row"].isHidden())

        # 先有大区域，再模拟在大区域内框选子区域：物理坐标 (110,120,50,60)
        # 相对大区域 (100,100,300,200) 应换算为 (10,20,50,60)。
        controls["region"].setText("100,100,300,200")
        dialog.until_condition_controller._on_sub_region_selected(
            1, (110, 120, 50, 60)
        )
        self.assertEqual(controls["sub_region"].text(), "10,20,50,60")
        self.assertEqual(
            dialog.get_data()["until_cond1_sub_region"], "10,20,50,60"
        )

    def test_image_region_and_debug_presets_update_original_dialog_state(self):
        dialog = self.create_dialog(
            {"debug_breakpoint": True},
            image_settings_available=True,
        )

        dialog.on_step_region_selected((5, 7, 90, 45))
        self.assertTrue(dialog.step_region_chk.isChecked())
        self.assertEqual(dialog.step_region_edit.text(), "5,7,90,45")

        dialog.debug_preset_combo.setCurrentText("从第 N 次循环开始")
        dialog.debug_preset_value_edit.setText("4")
        self.app.processEvents()
        self.assertEqual(dialog.debug_condition_edit.text(), "loop >= 4")
        self.assertEqual(dialog.get_data()["debug_condition"], "loop >= 4")

    def test_coordinate_manual_points_clear_when_path_count_changes(self):
        dialog = self.create_dialog(
            {
                "coord_step_en": True,
                "coord_step_direction": "移动到新点位",
                "coord_step_point": "40,0",
                "coord_step_max_steps": "4",
            },
            coordinate_step_available=True,
            base_coordinate=(0, 0),
            settings_mode="advanced",
        )
        dialog.coord_step_manual_points = {1: (12, 3)}
        dialog.update_coord_step_manual_ui()

        self.assertEqual(
            dialog.current_coord_step_options()["manual_points"], {1: (12, 3)}
        )
        self.assertIn("已手动修正", dialog.coord_step_manual_info.text())

        dialog.on_coord_step_count_changed("5")
        self.assertEqual(dialog.coord_step_manual_points, {})
        self.assertEqual(dialog.coord_step_manual_info.text(), "无手动修正点")


class Batch2301Regressions(QtCallbackGuardMixin, unittest.TestCase):
    """批次 2.3 回归：FR-20260809-1001/1002/1003（步骤设置往返）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_plugin_action_value_label_roundtrip(self):
        """FR-20260809-1001: 插件动作子对话框写入的 value/type 必须随 apply 回填，
        否则步骤行显示空/陈旧标签（修复前 value 被丢弃）。"""
        row = TaskRow(lambda _row: None)
        self.addCleanup(dispose_widget, self.app, row)
        row.open_custom_config()
        dialog = row.config_dialog
        dialog.data.update({
            "plugin_id": "com.fukua.test",
            "plugin_action_id": "test_action",
            "plugin_schema_version": 1,
            "plugin_config": {},
            "value": "测试插件：测试动作",
            "type": "27.0",
        })
        row.apply_custom_config(dialog)
        self.assertEqual(row.custom_data.get("value"), "测试插件：测试动作")
        self.assertEqual(row.custom_data.get("type"), "27.0")
        row.close_config_dialog()

    def test_cleared_optional_fields_remove_step_keys(self):
        """FR-20260809-1002: 清空点击偏移/移动曲线/按键按住时长（及随机延迟）后保存，
        键必须从步骤移除（引擎回退全局），不得保留旧值。"""
        row = TaskRow(lambda _row: None)
        self.addCleanup(dispose_widget, self.app, row)
        for key in (
            "click_spatial_jitter", "move_curve_strength", "key_hold_duration",
            "move_jitter", "click_hld_jitter",
        ):
            row.custom_data[key] = "5"
        row.sync_data()
        row.open_custom_config()
        dialog = row.config_dialog
        for key in (
            "click_spatial_jitter", "move_curve_strength", "key_hold_duration",
            "move_jitter", "click_hld_jitter",
        ):
            getattr(dialog, f"{key}_edit").setText("")
        row.apply_custom_config(dialog)
        for key in (
            "click_spatial_jitter", "move_curve_strength", "key_hold_duration",
            "move_jitter", "click_hld_jitter",
        ):
            self.assertNotIn(key, row.custom_data)
        row.close_config_dialog()

    def test_drag_target_manual_input_refreshes_point_controls(self):
        """FR-20260809-1003: 拖拽终点图片框手动输入路径后，图片内偏移编辑与选点
        按钮立即启用（与起点对称）；清空后恢复禁用（修复前仅按钮路径生效）。"""
        dialog = TaskConfigDialog(
            None, {}, step_type="左键拖拽", base_dir=self.temp_dir.name
        )
        self.addCleanup(dispose_widget, self.app, dialog)
        self.assertFalse(dialog.drag_target_point_edit.isEnabled())
        dialog.drag_target_img_edit.setText("C:/img/target.png")
        self.app.processEvents()
        self.assertTrue(dialog.drag_target_point_edit.isEnabled())
        self.assertTrue(dialog.drag_target_point_select_btn.isEnabled())
        dialog.drag_target_img_edit.setText("")
        self.app.processEvents()
        self.assertFalse(dialog.drag_target_point_edit.isEnabled())


class CoordinateNonFiniteInputTests(unittest.TestCase):
    """FR-20260812-D11-001: 坐标步进数值框的非有限值不得让 OverflowError 逃逸。"""

    class _FakeHost:
        def __init__(self):
            self.coord_step_every_edit = _FakeEdit("inf")
            self.coord_step_direction_combo = _FakeCombo()
            self.coord_step_distance_edit = _FakeEdit("1")
            self.coord_step_dx_edit = _FakeEdit("0")
            self.coord_step_dy_edit = _FakeEdit("0")
            self.coord_step_point_edit = _FakeEdit("")
            self.coord_step_max_steps_edit = _FakeEdit("1e309")
            self.coord_step_max_distance_edit = _FakeEdit("0")
            self.coord_step_reset_after_edit = _FakeEdit("inf")
            self.coord_step_manual_points = {}

        def manual_active(self):
            return False

    def test_non_finite_values_fall_back_without_overflow(self):
        ctrl = CoordinateSettingsController(self._FakeHost())
        # 不得抛 OverflowError；非有限值回退默认值。
        options = ctrl.current_options()
        self.assertEqual(options["every"], 1)
        self.assertEqual(options["max_steps"], 0)
        self.assertEqual(options["reset_after"], 0)


class _FakeEdit:
    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text


class _FakeCombo:
    def currentText(self):
        return "向下"


if __name__ == "__main__":
    unittest.main()
