# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.
"""v1.1.19：轨迹采样点固定表头、常驻说明、滚动条显隐对齐验收。"""
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from fukua_rpa.constants import TASK_TYPE_MOUSE_TRAJECTORY  # noqa: E402
from tests.qt_test_support import QtCallbackGuardMixin, create_test_window  # noqa: E402

EXPECTED_HEADERS = ("轨迹内时刻（毫秒）", "屏幕 X（横向）", "屏幕 Y（纵向）")


class TrajectoryHeaderTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _open_dialog(self, n_points, *, width=900, height=720):
        window = create_test_window(self, tempfile.mkdtemp())
        points = [[i * 6, 100 + i, 200 - i] for i in range(n_points)]
        import json
        window.add_row(
            {
                "type": TASK_TYPE_MOUSE_TRAJECTORY,
                "value": "鼠标轨迹",
                "trajectory": json.dumps(points),
                "fidelity": "raw",
            },
            record_undo=False,
            select=False,
        )
        window.resize(width, height)
        window.show()
        self.app.processEvents()
        item = window.task_list.item(0)
        row_widget = window.ensure_task_item_widget(item)
        row_widget.open_custom_config()
        dialog = row_widget.config_dialog
        self.assertTrue(dialog.is_mouse_trajectory())
        self.app.processEvents()
        return window, row_widget, dialog

    def _column_geometry(self, dialog):
        controller = dialog.trajectory_controller
        headers = controller.header_labels
        self.assertEqual(len(headers), 3)
        first_row = controller.rows[0]
        header_geo = [(h.mapToGlobal(h.rect().topLeft()).x(), h.width()) for h in headers]
        row_geo = [(e.mapToGlobal(e.rect().topLeft()).x(), e.width()) for e in first_row]
        return header_geo, row_geo

    def test_header_text_objectnames_and_explainer(self):
        window, row_widget, dialog = self._open_dialog(4)
        self.addCleanup(window.close)
        controller = dialog.trajectory_controller
        self.assertEqual(
            [label.text() for label in controller.header_labels],
            list(EXPECTED_HEADERS),
        )
        self.assertEqual(
            [label.objectName() for label in controller.header_labels],
            ["trajectory_header_t", "trajectory_header_x", "trajectory_header_y"],
        )
        self.assertTrue(controller.scroll_area is not None)

    def test_columns_aligned_static(self):
        window, row_widget, dialog = self._open_dialog(4)
        self.addCleanup(window.close)
        header_geo, row_geo = self._column_geometry(dialog)
        for (hx, hw), (rx, rw) in zip(header_geo, row_geo, strict=True):
            self.assertLessEqual(abs(hx - rx), 1, f"列左边界未对齐: {hx} vs {rx}")
            self.assertLessEqual(abs(hw - rw), 1, f"列宽未对齐: {hw} vs {rw}")

    def test_columns_aligned_on_scrollbar_show_hide(self):
        # 少点（无滚动条）→ 多点（滚动条出现）→ 删除到少点（消失）三态对齐
        window, row_widget, dialog = self._open_dialog(2)
        self.addCleanup(window.close)
        controller = dialog.trajectory_controller

        def assert_aligned(tag):
            controller.sync_header_margin()
            self.app.processEvents()
            hx, rx = self._column_geometry(dialog)
            for (h_left, _hw), (r_left, _rw) in zip(hx, rx, strict=True):
                self.assertLessEqual(abs(h_left - r_left), 1, f"{tag} 列未对齐")

        assert_aligned("无滚动条")
        scroll_bar = controller.scroll_area.verticalScrollBar()
        self.assertFalse(scroll_bar.isVisible())
        for _ in range(94):
            controller.add_point()
        for _ in range(4):
            self.app.processEvents()
        self.assertTrue(controller.scroll_area.verticalScrollBar().isVisible())
        assert_aligned("滚动条出现")
        for _ in range(94):
            controller.remove_last_point()
        for _ in range(4):
            self.app.processEvents()
        self.assertFalse(controller.scroll_area.verticalScrollBar().isVisible())
        assert_aligned("滚动条消失")

    def test_explainer_mentions_negative_coordinate(self):
        window, row_widget, dialog = self._open_dialog(2)
        self.addCleanup(window.close)
        from PySide6.QtWidgets import QLabel
        texts = [label.text() for label in dialog.trajectory_section.findChildren(QLabel)]
        joined = " ".join(texts)
        self.assertIn("每行是一个采样点", joined)
        self.assertIn("1948, 543", joined)
        self.assertIn("负值", joined)


if __name__ == "__main__":
    unittest.main()
