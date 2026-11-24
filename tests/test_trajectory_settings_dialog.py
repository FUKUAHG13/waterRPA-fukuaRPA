# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.
"""v1.1.17 修复 2：轨迹编辑经真实步骤设置对话框写回与校验。"""
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from fukua_rpa.trajectory_editor import serialize_trajectory  # noqa: E402
from fukua_rpa.ui.task_settings_dialog import TaskConfigDialog  # noqa: E402
from tests.qt_test_support import dispose_widget  # noqa: E402

RAW = "[[0,100,100],[144,130,109]]"


class TrajectorySettingsDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _dialog(self, raw=RAW):
        data = {"trajectory": raw, "value": "鼠标轨迹：2 点 / 0.144 秒"}
        dialog = TaskConfigDialog(
            None,
            data,
            base_dir=self.temp_dir.name,
            step_type="鼠标轨迹",
        )
        self.addCleanup(dispose_widget, self.app, dialog)
        return dialog, data

    def test_edit_second_point_commits_to_dialog_data(self):
        dialog, data = self._dialog()
        rows = dialog.trajectory_controller.rows
        # 第二个点改为 [220,160,120]。
        rows[1][0].setText("220")
        rows[1][1].setText("160")
        rows[1][2].setText("120")
        dialog.accept()
        self.assertEqual(dialog.result(), TaskConfigDialog.Accepted)
        self.assertEqual(dialog.data["trajectory"], "[[0,100,100],[220,160,120]]")
        self.assertIn("2 点", dialog.data["value"])
        self.assertIn("0.220 秒", dialog.data["value"])

    def test_cancel_keeps_original_data(self):
        dialog, data = self._dialog()
        rows = dialog.trajectory_controller.rows
        rows[1][1].setText("999")
        dialog.reject()
        self.assertNotIn("999", dialog.data.get("trajectory", ""))
        self.assertEqual(dialog.data["trajectory"], RAW)

    def test_validation_failure_refuses_accept_and_keeps_original(self):
        dialog, data = self._dialog()
        rows = dialog.trajectory_controller.rows
        # 时间倒序：把首点设为很大的时刻，使第二点早于首点（第 2 行）。
        rows[0][0].setText("500")
        dialog.accept()
        self.assertNotEqual(dialog.result(), TaskConfigDialog.Accepted)
        self.assertEqual(dialog.data["trajectory"], RAW)
        self.assertIn("第 2 行", dialog.trajectory_controller.status_label.text())

    def test_add_and_remove_point(self):
        dialog, data = self._dialog()
        controller = dialog.trajectory_controller
        controller.add_point()
        self.assertEqual(len(controller.rows), 3)
        controller.remove_last_point()
        self.assertEqual(len(controller.rows), 2)

    def test_long_trajectory_keeps_all_points_in_model(self):
        # 长轨迹草稿点完整保存在模型中；序列化保留全部点，编辑不丢数据。
        import json

        points = [[i * 2, i % 7, i % 11] for i in range(200)]
        raw = json.dumps(points, ensure_ascii=False, separators=(",", ":"))
        dialog, data = self._dialog(raw)
        controller = dialog.trajectory_controller
        self.assertEqual(len(controller.points), 200)
        # 修改末点后仍保留 200 点、末点更新。
        controller.rows[-1][1].setText("999")
        self.assertEqual(controller.sync_from_rows(), "")
        serialized = serialize_trajectory(controller.points)
        self.assertEqual(len(json.loads(serialized)), 200)
        self.assertEqual(json.loads(serialized)[-1][1], 999)

    def test_non_trajectory_step_hides_section(self):
        dialog = TaskConfigDialog(
            None,
            {"value": "100,100"},
            base_dir=self.temp_dir.name,
            step_type="左键单击",
        )
        self.addCleanup(dispose_widget, self.app, dialog)
        self.assertFalse(dialog.is_mouse_trajectory())
        # 未对普通动作写回 trajectory。
        dialog.accept()
        self.assertNotIn("trajectory", dialog.data)


if __name__ == "__main__":
    unittest.main()
