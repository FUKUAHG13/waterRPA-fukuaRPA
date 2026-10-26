import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from fukua_rpa.bulk_task_settings import (
    combined_batch_capabilities,
    common_batch_capabilities,
    filter_bulk_updates,
    mixed_bulk_fields,
)
from fukua_rpa.commands import (
    SETTING_CLICK_MOTION,
    SETTING_CLICK_RETURN,
    SETTING_CLICK_SPATIAL_JITTER,
    SETTING_COORDINATE_PATH,
    SETTING_DELAYED_SECOND_CLICK,
    SETTING_IMAGE_RECOGNITION,
    SETTING_KEY_HOLD,
    SETTING_MOUSE_TRAIL,
)
from fukua_rpa.ui.task_settings_dialog import TaskConfigDialog
from tests.qt_test_support import QtCallbackGuardMixin, create_test_window


class BulkTaskSettingsModelTests(unittest.TestCase):
    def test_simple_mode_capabilities_are_the_true_intersection(self):
        settings, _controls = common_batch_capabilities(
            [
                {"type": 1.0, "value": "10,20"},
                {"type": 1.0, "value": "button.png"},
            ]
        )
        self.assertNotIn(SETTING_COORDINATE_PATH, settings)
        self.assertNotIn(SETTING_IMAGE_RECOGNITION, settings)
        self.assertIn(SETTING_DELAYED_SECOND_CLICK, settings)

    def test_advanced_capabilities_are_the_relevant_union(self):
        settings, controls = combined_batch_capabilities(
            [
                {"type": 1.0, "value": "10,20"},
                {"type": 5.0, "value": "1"},
            ]
        )

        self.assertIn(SETTING_COORDINATE_PATH, settings)
        self.assertNotIn(SETTING_IMAGE_RECOGNITION, settings)
        self.assertTrue(controls)

    def test_advanced_click_and_key_fields_are_filtered_per_step(self):
        click = {"type": 1.0, "value": "10,20"}
        key = {"type": 7.0, "value": "Ctrl+A"}
        waiting = {"type": 5.0, "value": "1"}
        settings, _controls = combined_batch_capabilities([click, key, waiting])

        self.assertTrue(
            {
                SETTING_CLICK_SPATIAL_JITTER,
                SETTING_CLICK_MOTION,
                SETTING_CLICK_RETURN,
                SETTING_MOUSE_TRAIL,
                SETTING_KEY_HOLD,
            }.issubset(settings)
        )

        updates = {
            "click_spatial_jitter": "3",
            "move_curve_strength": "0.5",
            "click_return": True,
            "mouse_trail_mode": "on",
            "key_hold_duration": "0.2",
        }
        self.assertEqual(
            filter_bulk_updates(click, updates),
            {
                "click_spatial_jitter": "3",
                "move_curve_strength": "0.5",
                "click_return": True,
                "mouse_trail_mode": "on",
            },
        )
        self.assertEqual(
            filter_bulk_updates(key, updates),
            {"key_hold_duration": "0.2"},
        )
        self.assertEqual(filter_bulk_updates(waiting, updates), {})

        drag = {"type": 10.0, "value": "10,20 -> 30,40"}
        self.assertEqual(
            filter_bulk_updates(drag, updates),
            {
                "move_curve_strength": "0.5",
                "mouse_trail_mode": "on",
            },
        )

    def test_mixed_fields_reports_only_values_that_differ(self):
        tasks = [
            {"repeat_count": "1", "step_note": "相同"},
            {"repeat_count": "2", "step_note": "相同"},
        ]

        self.assertEqual(
            mixed_bulk_fields(tasks, ("repeat_count", "step_note")),
            {"repeat_count"},
        )

    def test_updates_are_filtered_per_target_kind(self):
        updates = {
            "coord_step_en": True,
            "step_region_en": True,
            "step_region": "1,2,3,4",
        }
        coordinate = filter_bulk_updates(
            {"type": 1.0, "value": "10,20"}, updates
        )
        image = filter_bulk_updates(
            {"type": 1.0, "value": "button.png"}, updates
        )
        self.assertEqual(coordinate, {"coord_step_en": True})
        self.assertNotIn("coord_step_en", image)
        self.assertTrue(image["step_region_en"])


class BulkTaskSettingsUiTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def create_window(self, base_dir):
        window = create_test_window(self, base_dir)
        advanced = window.settings_mode_combo.findData("advanced")
        window.settings_mode_combo.setCurrentIndex(advanced)
        window.apply_settings_mode()
        return window

    def test_advanced_batch_dialog_applies_each_field_only_where_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row(
                {"type": 1.0, "value": "10,20"},
                record_undo=False,
                select=False,
            )
            window.add_row(
                {"type": 1.0, "value": "button.png"},
                record_undo=False,
                select=False,
            )
            window.task_list.select_item_with_modifiers(
                window.task_list.item(0), Qt.NoModifier
            )
            window.task_list.select_item_with_modifiers(
                window.task_list.item(1), Qt.ControlModifier
            )

            def edit_dialog(dialog):
                dialog.coord_step_chk.setChecked(True)
                dialog.step_region_chk.setChecked(True)
                dialog.step_region_edit.setText("1,2,300,200")
                return QDialog.Accepted

            with mock.patch.object(TaskConfigDialog, "exec", edit_dialog):
                self.assertTrue(window.workflow_editor.open_batch_task_settings())

            coordinate, image = window.snapshot_tasks()
            self.assertTrue(coordinate["coord_step_en"])
            self.assertFalse(coordinate["step_region_en"])
            self.assertFalse(image["coord_step_en"])
            self.assertTrue(image["step_region_en"])
            self.assertEqual(image["step_region"], "1,2,300,200")

    def test_advanced_batch_dialog_persists_click_and_key_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row(
                {"type": 1.0, "value": "10,20"},
                record_undo=False,
                select=False,
            )
            window.add_row(
                {"type": 7.0, "value": "Ctrl+A"},
                record_undo=False,
                select=False,
            )
            window.add_row(
                {"type": 10.0, "value": "10,20 -> 30,40"},
                record_undo=False,
                select=False,
            )
            window.add_row(
                {"type": 5.0, "value": "1"},
                record_undo=False,
                select=False,
            )
            window.task_list.select_item_with_modifiers(
                window.task_list.item(0), Qt.NoModifier
            )
            window.task_list.select_item_with_modifiers(
                window.task_list.item(1), Qt.ControlModifier
            )
            window.task_list.select_item_with_modifiers(
                window.task_list.item(2), Qt.ControlModifier
            )
            window.task_list.select_item_with_modifiers(
                window.task_list.item(3), Qt.ControlModifier
            )

            def edit_dialog(dialog):
                self.assertFalse(dialog.click_form_section.isHidden())
                self.assertFalse(dialog.mouse_trail_section.isHidden())
                self.assertFalse(dialog.key_hold_section.isHidden())
                dialog.click_spatial_jitter_edit.setText("3")
                dialog.move_curve_strength_edit.setText("0.5")
                dialog.click_return_chk.setChecked(True)
                dialog.mouse_trail_mode_combo.setCurrentIndex(
                    dialog.mouse_trail_mode_combo.findData("on")
                )
                dialog.key_hold_duration_edit.setText("0.2")
                return QDialog.Accepted

            with mock.patch.object(TaskConfigDialog, "exec", edit_dialog):
                self.assertTrue(window.workflow_editor.open_batch_task_settings())

            click, key, drag, waiting = window.snapshot_tasks()
            self.assertEqual(click["click_spatial_jitter"], "3")
            self.assertEqual(click["move_curve_strength"], "0.5")
            self.assertTrue(click["click_return"])
            self.assertEqual(click["mouse_trail_mode"], "on")
            self.assertNotIn("key_hold_duration", click)

            self.assertEqual(key["key_hold_duration"], "0.2")
            self.assertNotIn("click_spatial_jitter", key)
            self.assertNotIn("move_curve_strength", key)
            self.assertNotIn("click_return", key)
            self.assertNotIn("mouse_trail_mode", key)

            self.assertNotIn("click_spatial_jitter", drag)
            self.assertEqual(drag["move_curve_strength"], "0.5")
            self.assertNotIn("click_return", drag)
            self.assertEqual(drag["mouse_trail_mode"], "on")

            for field in (
                "click_spatial_jitter",
                "move_curve_strength",
                "click_return",
                "mouse_trail_mode",
                "key_hold_duration",
            ):
                self.assertNotIn(field, waiting)

    def test_batch_cleared_optional_fields_remove_keys(self):
        """FR-20260809-1002: 批量对话框清空可选覆盖字段后，键必须从步骤移除
        （哨兵机制），而非静默保留旧值——与单步清空语义一致。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            for value in ("10,20", "30,40"):
                window.add_row(
                    {
                        "type": 1.0,
                        "value": value,
                        "click_spatial_jitter": "5",
                        "move_curve_strength": "0.3",
                        "move_jitter": "2",
                        "click_hld_jitter": "3",
                    },
                    record_undo=False,
                    select=False,
                )
            window.task_list.select_item_with_modifiers(
                window.task_list.item(0), Qt.NoModifier
            )
            window.task_list.select_item_with_modifiers(
                window.task_list.item(1), Qt.ControlModifier
            )

            def edit_dialog(dialog):
                # 清空全部可选覆盖字段（非空才写 → final 中这些键缺失）。
                dialog.click_spatial_jitter_edit.setText("")
                dialog.move_curve_strength_edit.setText("")
                dialog.move_jitter_edit.setText("")
                dialog.click_hld_jitter_edit.setText("")
                return QDialog.Accepted

            with mock.patch.object(TaskConfigDialog, "exec", edit_dialog):
                self.assertTrue(window.workflow_editor.open_batch_task_settings())

            first, second = window.snapshot_tasks()
            for step in (first, second):
                for key in (
                    "click_spatial_jitter",
                    "move_curve_strength",
                    "move_jitter",
                    "click_hld_jitter",
                ):
                    self.assertNotIn(key, step, f"{key} 应被批量清空移除")


if __name__ == "__main__":
    unittest.main()
