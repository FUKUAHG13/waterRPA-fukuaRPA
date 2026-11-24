# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.
"""v1.1.17 修复 2：真实步骤行小齿轮编辑轨迹并一次写回。"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from fukua_rpa.constants import TASK_TYPE_MOUSE_TRAJECTORY  # noqa: E402
from fukua_rpa.ui.task_row import TaskRow  # noqa: E402
from tests.qt_test_support import dispose_widget  # noqa: E402

RAW = "[[0,100,100],[144,130,109]]"


class TrajectoryRowE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _row(self):
        row = TaskRow(lambda _row: None)
        self.addCleanup(dispose_widget, self.app, row)
        row.set_data({
            "type": TASK_TYPE_MOUSE_TRAJECTORY,
            "value": "鼠标轨迹：2 点 / 0.144 秒",
            "trajectory": RAW,
            "timing_group_id": "seg-1",
            "pre_delay_ms": "25",
        })
        return row

    def test_gear_edit_updates_trajectory_and_summary_only(self):
        row = self._row()
        row.open_custom_config()
        dialog = row.config_dialog
        self.assertTrue(dialog.is_mouse_trajectory())
        self.assertTrue(dialog.trajectory_section.isVisible())
        rows = dialog.trajectory_controller.rows
        self.assertEqual(len(rows), 2)
        rows[1][0].setText("220")
        rows[1][1].setText("160")
        rows[1][2].setText("120")
        # 真实路径：点“保存”→ dialog.accept() → accepted 信号 → apply_custom_config。
        dialog.accept()
        data = row.get_data()
        self.assertEqual(data["trajectory"], "[[0,100,100],[220,160,120]]")
        self.assertIn("0.220 秒", data["value"])
        # 相邻字段与段身份保持不变。
        self.assertEqual(data.get("timing_group_id"), "seg-1")
        self.assertEqual(data.get("pre_delay_ms"), "25")
        row.close_config_dialog()

    def test_summary_alone_is_not_execution_source(self):
        # 主行摘要文字被手动改动不影响 trajectory 回放数据。
        row = self._row()
        row.value_input.setText("999,999")
        # 齿轮中的采样点仍来自 trajectory 字段，不被摘要污染。
        row.open_custom_config()
        dialog = row.config_dialog
        points = dialog.trajectory_controller.points
        self.assertEqual(points, [(0, 100, 100), (144, 130, 109)])
        row.close_config_dialog()


if __name__ == "__main__":
    unittest.main()
