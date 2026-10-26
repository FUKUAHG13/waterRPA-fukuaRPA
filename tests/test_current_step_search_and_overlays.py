"""回归测试：步骤查找、离屏拖动提示、首轮起始步和点击提示穿透。"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtWidgets import QApplication, QListWidgetItem

from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.engine import RPAEngine
from fukua_rpa.run_config import EngineRunConfig, RunConfigError
from fukua_rpa.ui.overlays import ClickPointOverlay, CoordinateStepPreviewOverlay
from fukua_rpa.ui.task_list import DraggableListWidget
from fukua_rpa.ui.task_row import TaskRow
from tests.qt_test_support import (
    QtCallbackGuardMixin,
    create_test_window,
    dispose_widget,
)


class RuntimeEntryTests(unittest.TestCase):
    def test_initial_start_step_is_optional_and_bounded(self):
        config = default_profile_config()
        runtime = EngineRunConfig.from_mapping(config, 3)
        self.assertIsNone(runtime.initial_start_step_index)

        config["initial_start_step"] = "3"
        runtime = EngineRunConfig.from_mapping(config, 3)
        self.assertEqual(runtime.initial_start_step_index, 2)

        config["initial_start_step"] = "4"
        with self.assertRaisesRegex(RunConfigError, "首轮临时起始步骤"):
            EngineRunConfig.from_mapping(config, 3)

    def test_initial_start_is_used_once_and_global_start_returns_next_round(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine()
        engine.log_level = -1
        engine.detect_delay = 0
        engine.settlement_wait = 0
        engine.loop_mode = "指定次数"
        engine.loop_val = 2
        engine.start_step_index = 0
        engine.initial_start_step_index = 1
        engine.load_and_precompute = lambda _tasks: True
        executed = []
        engine.execute_task_once = lambda params: (
            executed.append((params.val, params.step_info["loop"]))
            or "success"
        )

        engine.run_tasks(
            [
                {"type": 4.0, "value": "global"},
                {"type": 4.0, "value": "manual", "jump_only": True},
            ]
        )

        self.assertEqual(executed, [("manual", 1), ("global", 2)])


class UiEntryTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_step_index_reserves_width_for_three_digits(self):
        row = TaskRow(lambda *_args: None)
        self.addCleanup(dispose_widget, self.app, row)
        row.set_index(1, 250)
        narrow_text_width = row.index_label.sizeHint().width()
        row.set_index(200, 250)
        wide_text_width = row.index_label.sizeHint().width()
        self.assertGreaterEqual(wide_text_width, narrow_text_width)
        self.assertGreaterEqual(
            wide_text_width,
            row.index_label.fontMetrics().horizontalAdvance("250.") + 12,
        )

    def test_drag_placeholders_expose_offscreen_summaries_and_clear(self):
        widget = DraggableListWidget()
        self.addCleanup(dispose_widget, self.app, widget)
        for index in range(100):
            item = QListWidgetItem()
            item.setData(Qt.UserRole + 1, f"{index + 1}. 左键单击 | {index},0")
            widget.addItem(item)
        widget.setCurrentRow(0)
        widget.show()
        self.app.processEvents()

        widget.startDrag(Qt.MoveAction)
        self.assertTrue(widget._drag_active)
        self.assertTrue(any(widget.item(i).text() for i in range(80, 100)))
        widget.cancel_manual_drag()
        self.assertTrue(all(widget.item(i).text() == "" for i in range(100)))

    def test_step_search_navigates_without_hiding_or_reordering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = create_test_window(self, temp_dir)
            window.workflow_editor.add_row(
                {"type": 1.0, "value": "10,20"},
                record_undo=False,
            )
            window.workflow_editor.add_row(
                {"type": 6.0, "value": "1"},
                record_undo=False,
            )
            window.workflow_editor.add_row(
                {"type": 1.0, "value": "30,40"},
                record_undo=False,
            )
            before = [
                window.task_list.item(i).data(Qt.UserRole).get("type")
                for i in range(window.task_list.count())
            ]

            window.workflow_editor.show_task_search()
            window.task_search_edit.setText("滚轮滑动")
            self.app.processEvents()
            time.sleep(0.15)
            self.app.processEvents()
            self.assertEqual(window.task_list.currentRow(), 1)
            self.assertEqual(window.task_search_status.text(), "1/1")
            self.assertFalse(any(window.task_list.item(i).isHidden() for i in range(3)))

            window.task_search_edit.setText("左键单击")
            self.app.processEvents()
            time.sleep(0.15)
            self.app.processEvents()
            self.assertIn("/2", window.task_search_status.text())
            window.workflow_editor.find_task_match(1)
            self.assertIn(window.task_list.currentRow(), (0, 2))
            after = [
                window.task_list.item(i).data(Qt.UserRole).get("type")
                for i in range(window.task_list.count())
            ]
            self.assertEqual(before, after)

    def test_click_overlay_is_transparent_to_window_input(self):
        overlay = ClickPointOverlay(100, 100, duration_ms=5000)
        self.addCleanup(dispose_widget, self.app, overlay)
        self.assertTrue(
            bool(overlay.windowFlags() & Qt.WindowTransparentForInput)
        )
        self.assertTrue(overlay.testAttribute(Qt.WA_TransparentForMouseEvents))

    def test_running_status_overlay_is_click_through(self):
        from fukua_rpa.ui.overlays import RunningStatusOverlay

        overlay = RunningStatusOverlay()
        self.addCleanup(dispose_widget, self.app, overlay)
        # FR-20261008-802: WA_TransparentForMouseEvents alone does not become
        # WS_EX_TRANSPARENT on Windows, so the panel used to swallow clicks in
        # its rectangle. The real click-through flag must be present.
        self.assertTrue(
            bool(overlay.windowFlags() & Qt.WindowTransparentForInput)
        )
        self.assertTrue(overlay.testAttribute(Qt.WA_TransparentForMouseEvents))

    def test_running_status_overlay_side_padding_is_symmetric(self):
        """反馈项：运行状态提示框左右内外间距必须一致。

        旧实现同时固定外框宽度与标签宽度（280 / 248），但左右内边距没有按同一
        DPI/界面倍率缩放，高倍率下会在一侧留下额外空白。标签改为填满内容区后，
        两侧留白必须相等。"""
        from fukua_rpa.ui.overlays import RunningStatusOverlay

        previous = self.app.property("_fukua_ui_scale")
        self.app.setProperty("_fukua_ui_scale", 1.5)
        try:
            overlay = RunningStatusOverlay()
            self.addCleanup(dispose_widget, self.app, overlay)
            overlay.start_overlay("右上角")
            overlay.set_status({"cmd": "左键单击", "step": 3, "total": 9, "loop": 2})
            self.app.processEvents()

            geometry = overlay.label.geometry()
            left = geometry.left()
            right = overlay.bg.width() - geometry.right() - 1
            self.assertLessEqual(
                abs(left - right),
                1,
                f"运行状态提示框左右留白不对称: left={left}, right={right}",
            )
        finally:
            self.app.setProperty("_fukua_ui_scale", previous)

    def test_mouse_trail_overlay_is_click_through(self):
        from fukua_rpa.ui.overlays import MouseTrailOverlay

        overlay = MouseTrailOverlay()
        self.addCleanup(dispose_widget, self.app, overlay)
        # FR-20261008-801: the trail's last point sits exactly on the click
        # target, so a non-click-through overlay could swallow the click.
        self.assertTrue(
            bool(overlay.windowFlags() & Qt.WindowTransparentForInput)
        )
        self.assertTrue(overlay.testAttribute(Qt.WA_TransparentForMouseEvents))

    def test_coordinate_preview_overlay_ignores_nonfinite_screen_coordinates(self):
        overlay = CoordinateStepPreviewOverlay(
            [(float("inf"), 100)],
            auto_close_ms=0,
            close_on_left_blank=False,
        )
        self.addCleanup(dispose_widget, self.app, overlay)
        self.app.processEvents()

        local_point = overlay._screen_points()[0]
        self.assertLess(local_point.x(), -1000)
        self.assertLess(local_point.y(), -1000)

    def test_coordinate_preview_segment_drag_moves_scope_and_commits_once(self):
        class MouseEvent:
            def __init__(self, position, button=Qt.NoButton):
                self._position = QPointF(position)
                self._button = button
                self.accepted = False

            def position(self):
                return self._position

            def button(self):
                return self._button

            def accept(self):
                self.accepted = True

        callbacks = []
        overlay = CoordinateStepPreviewOverlay(
            [(100, 100), (200, 100), (300, 100)],
            auto_close_ms=0,
            close_on_left_blank=False,
            line_segments=[
                {
                    "from": 0,
                    "to": 1,
                    "style": "solid",
                    "move_indices": (0, 1),
                },
                {
                    "from": 1,
                    "to": 2,
                    "style": "solid",
                    "blocked_reason": "cross_group",
                },
            ],
        )
        self.addCleanup(dispose_widget, self.app, overlay)
        overlay.line_released_callback = (
            lambda segment, dx, dy: callbacks.append((segment, dx, dy))
        )
        first = overlay.to_screen_point(100, 100)
        second = overlay.to_screen_point(200, 100)
        midpoint = QPoint((first.x() + second.x()) // 2, first.y())
        moved = midpoint + QPoint(20, 12)

        overlay.mousePressEvent(MouseEvent(midpoint, Qt.LeftButton))
        overlay.mouseMoveEvent(MouseEvent(moved))
        overlay.mouseReleaseEvent(MouseEvent(moved, Qt.LeftButton))

        self.assertEqual(len(callbacks), 1)
        self.assertEqual(callbacks[0][0], 0)
        self.assertEqual(
            overlay.points[0][0] - 100,
            overlay.points[1][0] - 200,
        )
        self.assertEqual(
            overlay.points[0][1] - 100,
            overlay.points[1][1] - 100,
        )
        self.assertEqual(overlay.points[2], (300.0, 100.0))

        second = overlay.to_screen_point(*overlay.points[1])
        third = overlay.to_screen_point(*overlay.points[2])
        blocked_midpoint = QPoint(
            (second.x() + third.x()) // 2,
            (second.y() + third.y()) // 2,
        )
        overlay.mousePressEvent(MouseEvent(blocked_midpoint, Qt.LeftButton))
        self.assertIsNone(overlay.drag_segment_index)
        self.assertEqual(len(callbacks), 1)

    def test_total_coordinate_preview_writes_single_point_and_one_undo_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = create_test_window(self, temp_dir)
            window.workflow_editor.add_row(
                {"type": 1.0, "value": "10,20"},
                record_undo=False,
            )
            window.workflow_editor.undo_stack.clear()

            window.feedback_controller.show_all_coordinate_click_preview()
            plan = window.feedback_controller.all_points_preview_plan
            editable_index = next(
                index
                for index, source in enumerate(plan.point_sources)
                if source.editable
            )
            with mock.patch.object(
                window.workflow_editor, "log_task_edit"
            ) as audit, mock.patch.object(
                window, "mark_profile_dirty"
            ) as mark_dirty:
                window.feedback_controller.on_all_points_preview_point_released(
                    editable_index,
                    80,
                    90,
                )

            self.assertEqual(window.snapshot_tasks()[0]["value"], "80,90")
            self.assertEqual(len(window.workflow_editor.undo_stack), 1)
            audit.assert_called_once()
            self.assertEqual(audit.call_args.args[-1], "坐标总预览单点拖动")
            mark_dirty.assert_called_once()

    def test_total_coordinate_preview_group_drag_updates_only_its_scope(self):
        group_a = {"id": "A", "name": "A", "collapsed": False}
        group_b = {"id": "B", "name": "B", "collapsed": False}
        with tempfile.TemporaryDirectory() as temp_dir:
            window = create_test_window(self, temp_dir)
            for task in (
                {"type": 1.0, "value": "0,0", "editor_groups": [group_a]},
                {"type": 1.0, "value": "10,0", "editor_groups": [group_a]},
                {"type": 1.0, "value": "20,0", "editor_groups": [group_b]},
                {"type": 1.0, "value": "30,0", "editor_groups": [group_b]},
            ):
                window.workflow_editor.add_row(task, record_undo=False)
            window.workflow_editor.undo_stack.clear()

            window.feedback_controller.show_all_coordinate_click_preview()
            plan = window.feedback_controller.all_points_preview_plan
            group_a_segment = next(
                index
                for index, segment in enumerate(plan.line_segments)
                if {
                    ref.step_id for ref in segment.get("move_task_refs", ())
                }
                == {
                    plan.point_sources[0].task.step_id,
                    plan.point_sources[1].task.step_id,
                }
            )
            cross_segment = next(
                segment
                for segment in plan.line_segments
                if segment.get("blocked_reason") == "cross_group"
            )

            window.feedback_controller.on_all_points_preview_line_released(
                group_a_segment,
                5,
                7,
            )

            self.assertEqual(
                [task["value"] for task in window.snapshot_tasks()],
                ["5,7", "15,7", "20,0", "30,0"],
            )
            self.assertEqual(len(window.workflow_editor.undo_stack), 1)
            self.assertFalse(cross_segment.get("move_task_refs"))


if __name__ == "__main__":
    unittest.main()
