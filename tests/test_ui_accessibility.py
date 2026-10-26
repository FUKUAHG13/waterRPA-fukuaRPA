import os
import unittest
from ctypes import wintypes
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from fukua_rpa.coordinates import ScreenCoordinateMapper
from fukua_rpa.ui import input_tools
from fukua_rpa.ui import overlays as overlay_module
from fukua_rpa.ui.input_tools import MultiPointPickerUI, RegionWindow
from fukua_rpa.ui.lifecycle import schedule_widget_callback
from fukua_rpa.ui.overlays import (
    ClickPointOverlay,
    CoordinateStepPreviewOverlay,
    RecognitionMatchOverlay,
    RunningStatusOverlay,
)
from tests.qt_test_support import QtCallbackGuardMixin


class _FakeScreen:
    def __init__(self, name, geometry, ratio):
        self._name = name
        self._geometry = QRect(*geometry)
        self._ratio = float(ratio)

    def name(self):
        return self._name

    def geometry(self):
        return QRect(self._geometry)

    def devicePixelRatio(self):
        return self._ratio


class ScreenCoordinateRegressionTests(unittest.TestCase):
    def test_gdi_capture_handles_are_pointer_sized(self):
        for function, expected in (
            (input_tools.user32.GetDC, wintypes.HDC),
            (input_tools.gdi32.CreateCompatibleDC, wintypes.HDC),
            (input_tools.gdi32.CreateDIBSection, wintypes.HANDLE),
            (input_tools.gdi32.SelectObject, wintypes.HANDLE),
        ):
            self.assertIs(function.restype, expected)

    def test_mixed_dpi_three_monitor_layout_round_trips_physical_points(self):
        screens = [
            _FakeScreen("PRIMARY", (0, 0, 1920, 1080), 1.0),
            _FakeScreen("LEFT", (-1280, 0, 1280, 1024), 1.25),
            _FakeScreen("RIGHT", (1920, 0, 1280, 720), 2.0),
        ]
        physical = [
            {"name": "PRIMARY", "left": 0, "top": 0, "width": 1920, "height": 1080},
            {"name": "LEFT", "left": -1600, "top": 0, "width": 1600, "height": 1280},
            {"name": "RIGHT", "left": 1920, "top": 0, "width": 2560, "height": 1440},
        ]
        mapper = ScreenCoordinateMapper(
            screens=screens,
            physical_monitors=physical,
        )

        self.assertEqual(mapper.virtual_rect, QRect(-1280, 0, 4480, 1080))
        for physical_point in (
            (-1599, 1),
            (-800, 500),
            (0, 0),
            (1919, 1079),
            (1920, 0),
            (4478, 1438),
        ):
            local = mapper.physical_to_local(*physical_point)
            restored = mapper.local_to_physical(local)
            self.assertLessEqual(abs(restored[0] - physical_point[0]), 2)
            self.assertLessEqual(abs(restored[1] - physical_point[1]), 2)

    def test_rect_conversion_preserves_negative_monitor_coordinates(self):
        mapper = ScreenCoordinateMapper(
            screens=[_FakeScreen("LEFT", (-1280, -720, 1280, 720), 1.5)],
            physical_monitors=[
                {
                    "name": "LEFT",
                    "left": -1920,
                    "top": -1080,
                    "width": 1920,
                    "height": 1080,
                }
            ],
        )
        converted = mapper.local_rect_to_physical(QRect(100, 80, 400, 200))
        self.assertEqual(converted, (-1770, -960, 600, 300))

    def test_system_dpi_region_picker_returns_input_space_rect(self):
        holder = SimpleNamespace(
            _physical_origin=None,
            screen_mapper=SimpleNamespace(
                virtual_rect=QRect(-2880, 0, 5440, 1620)
            ),
        )
        converted = RegionWindow.physical_rect(
            holder,
            QRect(1440, 810, 300, 150),
        )
        self.assertEqual(converted, (-1440, 810, 300, 150))

    def test_overlay_boundary_converts_input_and_physical_once(self):
        mapper = mock.Mock()
        mapper.physical_to_local.return_value = QRect(10, 20, 1, 1).topLeft()
        mapper.local_to_physical.return_value = (-960, 540)
        point = QRect(30, 40, 1, 1).topLeft()
        with (
            mock.patch.object(
                overlay_module,
                "input_to_physical_coordinates",
                return_value=(-960, 540),
            ) as to_physical,
            mock.patch.object(
                overlay_module,
                "physical_to_input_coordinates",
                return_value=(-1440, 810),
            ) as to_input,
        ):
            local = overlay_module._input_to_local(mapper, -1440, 810)
            restored = overlay_module._local_to_input(mapper, point)
        self.assertEqual((local.x(), local.y()), (10, 20))
        self.assertEqual(restored, (-1440, 810))
        to_physical.assert_called_once_with(-1440, 810)
        to_input.assert_called_once_with(-960, 540)


class TopLevelLifecycleTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def flush_deletes(self):
        self.app.processEvents()
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_owner_bound_delayed_callback_is_cancelled_after_delete(self):
        calls = []
        widget = QWidget()
        widget.setAttribute(Qt.WA_DeleteOnClose)
        widget.show()
        schedule_widget_callback(widget, 25, lambda _owner: calls.append(True))
        widget.close()
        self.flush_deletes()
        QTest.qWait(45)
        self.flush_deletes()
        self.assertEqual(calls, [])

    def test_overlay_can_close_before_auto_close_and_capture_exclusion(self):
        overlay = ClickPointOverlay(10, 10, "测试", duration_ms=200)
        overlay.close()
        self.flush_deletes()
        QTest.qWait(230)
        self.flush_deletes()

    def test_preview_overlays_can_close_before_their_delayed_callbacks(self):
        previews = [
            CoordinateStepPreviewOverlay(
                [(10, 10), (30, 30)],
                auto_close_ms=200,
            ),
            RecognitionMatchOverlay(
                20,
                20,
                12,
                10,
                20,
                20,
                duration_ms=1000,
            ),
        ]
        for preview in previews:
            preview.close()
        self.flush_deletes()
        QTest.qWait(230)
        self.flush_deletes()

    def test_region_picker_can_be_destroyed_without_emitting_a_selection(self):
        selected = []
        picker = RegionWindow(purpose="recognition")
        picker.region_selected.connect(selected.append)
        picker.close()
        self.flush_deletes()
        self.assertEqual(selected, [])

    def test_running_status_timer_stops_when_overlay_is_closed(self):
        overlay = RunningStatusOverlay()
        overlay.start_overlay("右上角")
        self.assertTrue(overlay.timer.isActive())
        overlay.close()
        self.assertFalse(overlay.timer.isActive())
        self.flush_deletes()

    def test_multi_point_finish_callback_runs_once(self):
        completed = []
        picker = MultiPointPickerUI(
            lambda _point: None,
            lambda points: completed.append(list(points)),
        )
        picker.points.extend([(1, 2), (3, 4)])
        picker.close()
        picker.close()
        self.flush_deletes()
        self.assertEqual(completed, [[(1, 2), (3, 4)]])


class StepRowAccessibilityTests(unittest.TestCase):
    """FR-20261008-803：步骤行图标按钮必须对屏幕阅读器有可访问名称。

    步骤行是运行期动态创建的，而 ``apply_accessible_metadata`` 只在窗口构造期执行
    一次（``main_window`` / ``settings_view`` / ``task_settings_view``），因此齿轮
    （``cfg_btn``）与删除（``del_btn``）这两个**无文字**的图标按钮此前暴露空名称。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_apply_accessible_metadata_names_the_icon_only_buttons(self):
        from PySide6.QtGui import QAccessible

        from fukua_rpa.ui.accessibility import apply_accessible_metadata
        from fukua_rpa.ui.task_row import TaskRow

        row = TaskRow(lambda _row: None)
        self.addCleanup(row.deleteLater)

        before = QAccessible.queryAccessibleInterface(row.cfg_btn).text(
            QAccessible.Name
        )
        self.assertEqual(before, "", "前置：未调用前齿轮按钮应无名称")

        apply_accessible_metadata(row, root_name="步骤 1")

        for button in (row.cfg_btn, row.del_btn):
            name = QAccessible.queryAccessibleInterface(button).text(QAccessible.Name)
            self.assertNotEqual(name, "", f"{button} 缺少可访问名称")

    def test_row_creation_path_applies_accessible_metadata(self):
        """静态断言：动态行创建路径必须调用 ``apply_accessible_metadata``。"""
        import inspect

        from fukua_rpa.ui.controllers.workflow_step_edit import (
            WorkflowStepEditMixin,
        )

        source = inspect.getsource(WorkflowStepEditMixin.restore_row_widget)
        self.assertIn(
            "apply_accessible_metadata",
            source,
            "restore_row_widget 未给动态创建的步骤行补充可访问名称",
        )

    def test_row_accessible_name_follows_reindexed_position(self):
        """FR-20261008-904：拖拽重排后行级可访问名称必须跟随新序号。

        行级名称只在创建时写入一次（``accessibility.py`` 的
        ``not root.accessibleName()`` 守卫使已有名称永不更新），而拖拽重排
        仅拆散被拖行 widget、其余行原地保留并只刷新可见序号标签。此前存续行
        的读屏名称停留在创建时的旧序号——可见序号正确、读屏播报错误。
        """
        from PySide6.QtGui import QAccessible

        from fukua_rpa.ui.accessibility import apply_accessible_metadata
        from fukua_rpa.ui.task_row import TaskRow

        row = TaskRow(lambda _row: None)
        self.addCleanup(row.deleteLater)

        apply_accessible_metadata(row, root_name="步骤 2")
        row.set_index(4)

        name = QAccessible.queryAccessibleInterface(row).text(QAccessible.Name)
        self.assertEqual(name, "步骤 4")


if __name__ == "__main__":
    unittest.main()
