import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QItemSelectionModel, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QListWidgetItem,
)

from fukua_rpa.ui.task_list import DraggableListWidget
from tests.qt_test_support import QtCallbackGuardMixin, dispose_widget


def _mouse_press(pos, button=Qt.LeftButton):
    return QMouseEvent(
        QEvent.MouseButtonPress, QPointF(pos), pos, button, button, Qt.NoModifier
    )


def _mouse_move(pos, buttons=Qt.LeftButton):
    return QMouseEvent(
        QEvent.MouseMove, QPointF(pos), pos, Qt.NoButton, buttons, Qt.NoModifier
    )


def _mouse_release(pos, button=Qt.LeftButton):
    return QMouseEvent(
        QEvent.MouseButtonRelease, QPointF(pos), pos, button, Qt.NoButton, Qt.NoModifier
    )


class DragInsertPreviewTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_list(self, count=5):
        widget = DraggableListWidget()
        self.addCleanup(dispose_widget, self.app, widget)
        for index in range(count):
            widget.addItem(QListWidgetItem(f"step {index + 1}"))
        return widget

    def test_upward_drop_reports_resulting_step_number(self):
        widget = self.make_list()
        widget.setCurrentRow(3)
        widget.drop_line_row = 0
        widget.drop_line_after = False
        self.assertEqual(widget._insertion_index(), 0)

    def test_downward_drop_accounts_for_removed_source_row(self):
        widget = self.make_list()
        widget.setCurrentRow(1)
        widget.drop_line_row = 4
        widget.drop_line_after = True
        self.assertEqual(widget._insertion_index(), 4)

    def test_manual_drag_reorders_without_native_drop_session(self):
        widget = self.make_list()
        widget.resize(480, 320)
        widget.show()
        self.app.processEvents()
        widget.setCurrentRow(3)

        widget.startDrag(Qt.MoveAction)
        self.assertTrue(widget._drag_active)
        self.assertTrue(widget.drag_source_badge.isVisible())
        self.assertIn("step 4", widget.drag_source_badge.text())
        # Native drag is disabled so the gesture state machine in
        # mousePressEvent/mouseMoveEvent owns press/drag handling (no Qt
        # auto drag-select). Reordering still works via the manual drag path
        # started by _begin_manual_drag, exercised above and below.
        self.assertFalse(widget.dragEnabled())
        self.assertFalse(widget.acceptDrops())
        self.assertTrue(
            widget._finish_manual_drag(commit=True, pos=QPoint(8, 1))
        )

        self.assertEqual(
            [widget.item(index).text() for index in range(widget.count())],
            ["step 4", "step 1", "step 2", "step 3", "step 5"],
        )
        self.assertEqual(widget.currentRow(), 0)
        self.assertFalse(widget._drag_active)
        self.assertFalse(widget.drag_source_badge.isVisible())

    def test_manual_drag_moves_all_selected_rows_in_order(self):
        widget = self.make_list()
        widget.resize(480, 320)
        widget.show()
        self.app.processEvents()
        widget.item(1).setSelected(True)
        widget.item(2).setSelected(True)
        widget.setCurrentRow(1, QItemSelectionModel.NoUpdate)

        widget.startDrag(Qt.MoveAction)
        self.assertIn("2 个步骤", widget.drag_source_badge.text())
        self.assertTrue(
            widget._finish_manual_drag(commit=True, pos=QPoint(8, 1))
        )

        self.assertEqual(
            [widget.item(index).text() for index in range(widget.count())],
            ["step 2", "step 3", "step 1", "step 4", "step 5"],
        )

    def test_manual_drag_can_be_cancelled_without_reordering(self):
        widget = self.make_list()
        widget.resize(480, 320)
        widget.show()
        self.app.processEvents()
        before = [widget.item(index).text() for index in range(widget.count())]
        widget.setCurrentRow(2)

        widget.startDrag(Qt.MoveAction)
        widget._update_manual_drag(QPoint(8, 1), force=True)
        self.assertFalse(widget.cancel_manual_drag())

        self.assertEqual(
            [widget.item(index).text() for index in range(widget.count())],
            before,
        )
        self.assertFalse(widget._drag_active)

    def test_manual_drag_coalesces_high_frequency_target_updates(self):
        widget = self.make_list()
        widget.resize(480, 320)
        widget.show()
        self.app.processEvents()
        widget.setCurrentRow(2)
        widget.startDrag(Qt.MoveAction)

        with mock.patch.object(
            widget,
            "_update_drop_line_at",
            wraps=widget._update_drop_line_at,
        ) as update_target:
            for offset in range(12):
                widget._update_manual_drag(QPoint(8, 10 + offset))

        update_target.assert_not_called()
        widget.cancel_manual_drag()

    def test_manual_drag_does_not_start_qt_native_drag_loop(self):
        widget = self.make_list()
        widget.resize(480, 320)
        widget.show()
        self.app.processEvents()
        widget.setCurrentRow(1)

        with mock.patch("PySide6.QtGui.QDrag.exec") as native_drag_exec:
            widget.startDrag(Qt.MoveAction)

        native_drag_exec.assert_not_called()
        self.assertTrue(widget._drag_active)
        widget.cancel_manual_drag()

    def test_drop_indicator_is_a_top_level_viewport_overlay(self):
        widget = self.make_list(2)
        widget.resize(480, 220)
        row_widget = QFrame()
        row_widget.setStyleSheet("background: white;")
        widget.setItemWidget(widget.item(0), row_widget)
        widget.show()
        self.app.processEvents()

        widget.drop_indicator.show_at(30)
        self.app.processEvents()

        self.assertIs(widget.drop_indicator.parentWidget(), widget.viewport())
        self.assertEqual(
            widget.drop_indicator.width(), widget.viewport().width()
        )
        self.assertLessEqual(
            widget.drop_indicator.height(),
            widget.drop_indicator.BAND_HEIGHT,
        )
        self.assertTrue(
            widget.drop_indicator.geometry().contains(
                widget.viewport().width() // 2,
                widget.drop_indicator.indicator_y,
            )
        )
        self.assertTrue(widget.drop_indicator.isVisible())
        pixel = widget.viewport().grab().toImage().pixelColor(
            widget.viewport().width() // 2,
            widget.drop_indicator.indicator_y,
        )
        self.assertGreater(pixel.blue(), pixel.red())

        widget.drop_indicator.clear()
        self.assertFalse(widget.drop_indicator.isVisible())

    def test_repeated_indicator_position_is_a_no_op(self):
        widget = self.make_list(2)
        widget.resize(480, 220)
        widget.show()
        self.app.processEvents()

        self.assertTrue(widget.drop_indicator.show_at(30, 6))
        geometry = widget.drop_indicator.geometry()

        self.assertFalse(widget.drop_indicator.show_at(30, 6))
        self.assertEqual(widget.drop_indicator.geometry(), geometry)
        self.assertTrue(widget.drop_indicator.show_at(30, 8))
        self.assertEqual(widget.drop_indicator.line_width, 8)

    def test_drop_indicator_grows_as_pointer_nears_boundary(self):
        widget = self.make_list(2)
        widget.resize(480, 220)
        widget.show()
        self.app.processEvents()
        rect = widget.visualItemRect(widget.item(0))

        widget._update_drop_line_at(QPoint(10, rect.center().y() - 1))
        far_width = widget.drop_indicator.line_width
        widget._update_drop_line_at(QPoint(10, rect.top() + 1))
        near_width = widget.drop_indicator.line_width

        self.assertGreater(near_width, far_width)

    def test_repeated_drop_target_does_not_request_another_overlay_update(self):
        widget = self.make_list(2)
        widget.resize(480, 220)
        widget.show()
        self.app.processEvents()
        widget.drop_line_row = 0
        widget.drop_line_after = False

        with mock.patch.object(
            widget.drop_indicator,
            "show_at",
            wraps=widget.drop_indicator.show_at,
        ) as show_at:
            widget._update_drop_indicator()
            widget._update_drop_indicator()

        show_at.assert_called_once()

    def test_empty_area_drag_starts_box_selection(self):
        # #1 regression: a left-button drag that *begins* on empty space (not
        # on a step body) must start the native rubber-band box-select, not be
        # swallowed. The gesture is deferred until the drag threshold so a tiny
        # slide does nothing, then a replayed LeftButton press kicks off the
        # Qt rubber-band from the press point.
        widget = self.make_list(5)
        widget.resize(480, 400)
        widget.show()
        self.app.processEvents()
        # There must be empty space below the items to begin the drag there.
        self.assertGreater(widget.viewport().height(), widget.count() * 40)

        press = QPoint(12, widget.viewport().height() - 12)
        self.assertIsNone(widget.itemAt(press), "press point should be empty area")

        widget.mousePressEvent(_mouse_press(press))
        self.assertEqual(widget._gesture, "box-pending")

        # A sub-threshold jitter must NOT start a selection.
        widget.mouseMoveEvent(_mouse_move(QPoint(14, widget.viewport().height() - 10)))
        self.assertEqual(widget._gesture, "box-pending")
        self.assertEqual(len(widget.selectedIndexes()), 0)

        # Crossing the drag threshold replays a LeftButton press and starts the
        # native rubber-band (gesture becomes "box").
        drag_to = QPoint(240, 8)
        widget.mouseMoveEvent(_mouse_move(drag_to))
        self.assertEqual(widget._gesture, "box")

        widget.mouseMoveEvent(_mouse_move(drag_to))
        widget.mouseReleaseEvent(_mouse_release(drag_to))

        # The sweep across all rows selected them (rubber-band behaviour kept).
        self.assertGreater(len(widget.selectedIndexes()), 0)
        self.assertEqual(widget._gesture, None)

    def test_row_body_small_jitter_is_click_not_drag(self):
        # #2 regression: a press on an *unselected* step body followed by a
        # tiny move must be treated as a plain click (select that one step),
        # never as a drag that would sweep-select many steps above/below.
        # An unselected press now starts a box-select gesture (Windows-folder
        # style) but a sub-threshold jitter never crosses the drag threshold.
        widget = self.make_list(5)
        widget.resize(480, 400)
        widget.show()
        self.app.processEvents()

        rect = widget.visualItemRect(widget.item(2))
        press = rect.center()
        self.assertIsNotNone(widget.itemAt(press))

        widget.mousePressEvent(_mouse_press(press))
        self.assertEqual(widget._gesture, "box-pending")

        # Sub-threshold jitter: stays box-pending, selects nothing yet.
        widget.mouseMoveEvent(_mouse_move(QPoint(press.x() + 1, press.y() + 1)))
        self.assertEqual(widget._gesture, "box-pending")
        self.assertFalse(widget._drag_active)
        self.assertEqual(len(widget.selectedIndexes()), 0)

        # Release without crossing the threshold -> single click selection.
        widget.mouseReleaseEvent(_mouse_release(press))
        self.assertEqual(widget._gesture, None)
        self.assertFalse(widget._drag_active)
        self.assertEqual(len(widget.selectedIndexes()), 1)
        self.assertEqual(widget.selectedIndexes()[0].row(), 2)
        self.assertEqual(
            [widget.item(i).text() for i in range(widget.count())],
            ["step 1", "step 2", "step 3", "step 4", "step 5"],
        )

    def test_row_body_drag_reorders(self):
        # Windows-folder behaviour (#3): a step must be selected first; then
        # pressing it and dragging past the threshold begins a manual reorder
        # (move). An unselected press would instead start a box-select, so the
        # test selects the step before dragging.
        widget = self.make_list(5)
        widget.resize(480, 400)
        widget.show()
        self.app.processEvents()

        widget.item(3).setSelected(True)
        rect = widget.visualItemRect(widget.item(3))
        press = rect.center()
        widget.mousePressEvent(_mouse_press(press))
        self.assertEqual(widget._gesture, "pending")

        widget.mouseMoveEvent(_mouse_move(QPoint(press.x() + 1, press.y() + 1)))
        self.assertEqual(widget._gesture, "pending")

        # Big move crosses the drag threshold -> manual reorder starts.
        widget.mouseMoveEvent(_mouse_move(QPoint(press.x(), 4)))
        self.assertTrue(widget._drag_active)
        self.assertEqual(widget._gesture, None)

        # Settle the drop line at the top, then release to commit.
        widget.mouseMoveEvent(_mouse_move(QPoint(8, 4)))
        widget.mouseReleaseEvent(_mouse_release(QPoint(8, 4)))
        self.assertFalse(widget._drag_active)
        self.assertEqual(
            [widget.item(i).text() for i in range(widget.count())],
            ["step 4", "step 1", "step 2", "step 3", "step 5"],
        )
        self.assertEqual(widget.currentRow(), 0)


if __name__ == "__main__":
    unittest.main()
