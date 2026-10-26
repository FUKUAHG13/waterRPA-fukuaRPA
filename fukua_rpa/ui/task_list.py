# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Drag-aware, virtualized list used by the workflow editor."""

import contextlib

from PySide6.QtCore import QEvent, QItemSelectionModel, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QLabel,
    QListWidget,
    QWidget,
)

from .lifecycle import is_runtime_editing_locked
from .task_list_mixins import DragGestureMixin, VirtualizationMixin


class DropIndicatorOverlay(QWidget):
    """Paint a narrow insertion boundary above materialized task-row widgets."""

    BAND_HEIGHT = 20

    def __init__(self, parent):
        super().__init__(parent)
        self._indicator_y = None
        self._line_width = 6
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.hide()

    @property
    def indicator_y(self):
        return self._indicator_y

    def sync_geometry(self):
        parent = self.parentWidget()
        if parent is None or self._indicator_y is None:
            return False
        self._indicator_y = max(
            7,
            min(int(self._indicator_y), max(7, parent.height() - 8)),
        )
        top = max(0, self._indicator_y - self.BAND_HEIGHT // 2)
        height = min(self.BAND_HEIGHT, max(1, parent.height() - top))
        target = QRect(0, top, parent.width(), height)
        if self.geometry() == target:
            return False
        self.setGeometry(target)
        return True

    @property
    def line_width(self):
        return self._line_width

    def show_at(self, y, line_width=6):
        parent = self.parentWidget()
        if parent is None:
            return False
        target_y = max(7, min(int(y), max(7, parent.height() - 8)))
        target_width = max(4, min(8, int(line_width)))
        unchanged = (
            self._indicator_y == target_y
            and self._line_width == target_width
            and self.isVisible()
        )
        self._indicator_y = target_y
        self._line_width = target_width
        geometry_changed = self.sync_geometry()
        if unchanged and not geometry_changed:
            return False
        if not self.isVisible():
            self.show()
            self.raise_()
        self.update()
        return True

    def clear(self):
        self._indicator_y = None
        self.hide()

    def paintEvent(self, event):
        if self._indicator_y is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        y = self._indicator_y - self.y()
        width = self._line_width
        left = 10
        right = max(left + 1, self.width() - 10)

        painter.setPen(
            QPen(
                QColor(37, 99, 235, 68),
                width + 7,
                Qt.SolidLine,
                Qt.RoundCap,
            )
        )
        painter.drawLine(left, y, right, y)
        painter.setPen(
            QPen(QColor(255, 255, 255, 245), width + 3, Qt.SolidLine, Qt.RoundCap)
        )
        painter.drawLine(left, y, right, y)
        painter.setPen(
            QPen(
                QColor(37, 99, 235, 250),
                width,
                Qt.SolidLine,
                Qt.RoundCap,
            )
        )
        painter.drawLine(left, y, right, y)


class DraggableListWidget(DragGestureMixin, VirtualizationMixin, QListWidget):
    VIRTUALIZATION_THRESHOLD = 80
    VIRTUAL_BUFFER_ROWS = 8
    VIRTUAL_REFRESH_INTERVAL_MS = 50
    BASE_ROW_HEIGHT = 52
    MANUAL_DRAG_UPDATE_NS = 33_000_000
    AUTO_SCROLL_INTERVAL_MS = 40
    AUTO_SCROLL_MARGIN = 36

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.DragOnly)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDropIndicatorShown(False)
        self.setAcceptDrops(False)
        self.viewport().setAcceptDrops(False)
        # Dragging is managed by an explicit gesture state machine in
        # mousePressEvent/mouseMoveEvent. Disabling Qt's built-in drag stops
        # ExtendedSelection from treating a press-drag as a drag-select.
        self.setDragEnabled(False)
        self.drop_line_row = None
        self.drop_line_after = False
        self.drop_indicator = DropIndicatorOverlay(self.viewport())
        self._drop_indicator_key = None
        self._virtual_refreshing = False
        self._last_target_materialized = set()
        self._drag_placeholder_items = []
        self._drag_active = False
        self._drag_source_row = None
        self._drop_line_width = 4
        self._drag_pending_pos = None
        self._drag_last_update_ns = 0
        self._drag_autoscroll_timer = QTimer(self)
        self._drag_autoscroll_timer.setInterval(
            self.AUTO_SCROLL_INTERVAL_MS
        )
        self._drag_autoscroll_timer.timeout.connect(
            self._auto_scroll_manual_drag
        )
        self.drag_source_badge = QLabel(self.viewport())
        self.drag_source_badge.setObjectName("taskDragSourceBadge")
        self.drag_source_badge.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.drag_source_badge.hide()
        self._virtual_row_height = self.BASE_ROW_HEIGHT
        self._virtual_min_row_height = 42
        self._virtual_group_header_height = 32
        self._virtual_refresh_timer = QTimer(self)
        self._virtual_refresh_timer.setSingleShot(True)
        self._virtual_refresh_timer.timeout.connect(self.refresh_virtual_widgets)
        # DESIGN: incremental materialized-row counter.  Updated in
        # refresh_virtual_widgets (+1 per restore, -1 per removeItemWidget)
        # so materialized_row_count() is O(1) during continuous scrolling.
        # Invalidated (set to None) when the item count changes, because
        # external code (ensure_task_item_widget) can materialize widgets
        # without notifying this class; the next call falls back to a full
        # scan and re-establishes the baseline.
        self._materialized_count: int | None = None
        self._materialized_count_items: int = -1
        self._selection_anchor_row = None
        self._press_button = None
        self._press_pos = None
        self._press_row = None
        self._press_on_control = False
        self._press_modifiers = Qt.NoModifier
        self._press_was_selected = False
        self._gesture = None
        self.verticalScrollBar().valueChanged.connect(
            lambda _value: self.schedule_virtual_refresh()
        )

    def selected_row_indexes(self):
        return [row for row in range(self.count()) if self.item(row).isSelected()]

    def close_and_detach_row_widget(self, row):
        """Close a row's config dialog and detach its widget before takeItem.

        SAFETY (FR-20260809-1101): ``QListWidget.takeItem`` unbinds the row
        widget from its item (the widget survives as an orphan on the viewport,
        and re-inserting the item does not restore the binding).  Reordering
        paths (drag commit / move up-down) must therefore close the config
        dialog and hide+deleteLater the detached widget, mirroring the delete
        path in ``workflow_step_edit``.  Otherwise a NonModal dialog bound to
        the orphan row keeps accepting edits that are silently written to a row
        no longer shown in the list.
        """
        if not (0 <= row < self.count()):
            return
        item = self.item(row)
        if item is None:
            return
        widget = self.itemWidget(item)
        if widget is None:
            return
        try:
            close = getattr(widget, "close_config_dialog", None)
            if callable(close):
                close()
        except RuntimeError:
            pass  # C++ object already deleted; nothing left to close.
        # 与 virtualization 去物化路径一致：分离前把行数据写回 item，避免
        # item 的 UserRole 数据与已分离的 widget 脱节。
        try:
            data = widget.get_data()
            summary = widget.drag_summary()
        except RuntimeError:
            data = None
            summary = None
        if data is not None:
            item.setData(Qt.UserRole, data)
        if summary is not None:
            item.setData(Qt.UserRole + 1, summary)
        self.removeItemWidget(item)
        # INVARIANT: removeItemWidget 不经过 refresh_virtual_widgets 的计数更新
        # 路径，必须显式失效 materialized 计数器，否则 materialized_row_count()
        # 高估（实测偏差 +1，违反 __init__ 中 DESIGN 注释声明的计数契约）。
        self._materialized_count = None
        self._materialized_count_items = -1
        with contextlib.suppress(RuntimeError):
            widget.hide()
        with contextlib.suppress(RuntimeError):
            widget.deleteLater()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.drop_indicator.sync_geometry()
        self._position_drag_source_badge()
        self.schedule_virtual_refresh()

    def closeEvent(self, event):
        self._release_viewport_grab()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.schedule_virtual_refresh()

    def keyPressEvent(self, event):
        if self._drag_active and event.key() == Qt.Key_Escape:
            self.cancel_manual_drag()
            event.accept()
            return
        owner = self.window()
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_F2):
            if is_runtime_editing_locked(owner):
                event.accept()
                return
            item = self.currentItem()
            restore = getattr(owner, "ensure_task_item_widget", None)
            widget = restore(item) if callable(restore) else self.itemWidget(item)
            opener = getattr(widget, "open_custom_config", None)
            if callable(opener):
                opener()
                event.accept()
                return
        if event.modifiers() & Qt.ControlModifier:
            shortcut_methods = {
                Qt.Key_C: "copy_selected_row",
                Qt.Key_X: "cut_selected_row",
                Qt.Key_V: "paste_row_after_selected",
                Qt.Key_Z: "undo_task_change",
                Qt.Key_Y: "redo_task_change",
                Qt.Key_D: "insert_row_before_selected",
            }
            callback = getattr(owner, shortcut_methods.get(event.key(), ""), None)
            if callable(callback):
                callback()
                event.accept()
                return
        if event.modifiers() == Qt.ControlModifier and event.key() in (
            Qt.Key_Up,
            Qt.Key_Down,
        ):
            move = getattr(owner, "move_selected_task", None)
            if callable(move) and move(-1 if event.key() == Qt.Key_Up else 1):
                event.accept()
                return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._press_button = event.button()
        self._press_pos = self._event_pos(event)
        self._press_modifiers = event.modifiers()
        self._press_was_selected = False
        item = self.itemAt(self._press_pos)
        self._press_row = self.row(item) if item is not None else None
        self._press_on_control = self._hit_control(self._press_pos)
        if self._press_modifiers & (Qt.ControlModifier | Qt.ShiftModifier):
            self._gesture = "modifier"
            super().mousePressEvent(event)
            return
        if self._press_on_control:
            self._gesture = "control"
            super().mousePressEvent(event)
            return
        if self._press_row is None:
            # Empty area: defer the rubber-band until the drag threshold is
            # crossed, so tiny slides don't churn selection and burn CPU.
            self._gesture = "box-pending"
            event.accept()
            return
        # Press on a row body. Windows-folder behaviour:
        #  - a press on an already-selected step starts a MOVE of the
        #    selection once the drag threshold is crossed;
        #  - a press on an unselected step (or a step outside the current
        #    selection) starts a rubber-band box-select instead, and a plain
        #    click without dragging selects just that step on release.
        self._press_was_selected = bool(item.isSelected())
        if self._press_was_selected:
            self._gesture = "pending"
        else:
            self._gesture = "box-pending"
        event.accept()
        # Grabbing the viewport is deferred to _begin_manual_drag when the
        # drag threshold is actually crossed.  Grabbing here on every press
        # leaves a second grab that never has a matching release, risking a
        # permanent input lock if the window loses focus mid-gesture.

    def mouseMoveEvent(self, event):
        if self._drag_active:
            if event.buttons() & Qt.LeftButton:
                self._update_manual_drag(self._event_pos(event))
            else:
                self._finish_manual_drag(
                    commit=True,
                    pos=self._event_pos(event),
                )
            event.accept()
            return
        if self._gesture in ("modifier", "control", "box"):
            super().mouseMoveEvent(event)
            return
        if self._gesture == "box-pending":
            pos = self._event_pos(event)
            if event.buttons() & Qt.LeftButton and (
                self._press_pos - pos
            ).manhattanLength() >= QApplication.startDragDistance():
                # Begin the native rubber-band box-select from the threshold
                # point; tiny slides before this did nothing.
                self._gesture = "box"
                # The move event carries NoButton (buttons() holds LeftButton),
                # but Qt's rubber-band only starts on a LeftButton press, so
                # synthesize the press with the correct button. Start the band
                # at the original press point so it spans the full drag.
                replay = QMouseEvent(
                    QEvent.MouseButtonPress,
                    QPointF(self._press_pos),
                    self._press_pos,
                    Qt.LeftButton,
                    event.buttons(),
                    event.modifiers(),
                )
                super().mousePressEvent(replay)
                super().mouseMoveEvent(event)
            return
        if self._gesture == "pending":
            pos = self._event_pos(event)
            if event.buttons() & Qt.LeftButton and (
                self._press_pos - pos
            ).manhattanLength() >= QApplication.startDragDistance():
                item = self.item(self._press_row)
                if item is not None and not item.isSelected():
                    self.clearSelection()
                    item.setSelected(True)
                    self.setCurrentRow(
                        self._press_row, QItemSelectionModel.NoUpdate
                    )
                self._begin_manual_drag(self._press_row)
                self._gesture = None
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_active:
            self._finish_manual_drag(
                commit=event.button() == Qt.LeftButton,
                pos=self._event_pos(event),
            )
            event.accept()
            return
        if self._gesture in ("modifier", "control", "box"):
            super().mouseReleaseEvent(event)
            self._gesture = None
            return
        if self._gesture == "box-pending":
            if not (event.buttons() & Qt.LeftButton):
                # The drag never crossed the threshold, so the native rubber
                # band never started. Treat it as a plain click:
                #  - empty area -> clear the selection;
                #  - a step row   -> select just that step (Windows-folder).
                if self._press_row is not None:
                    item = self.item(self._press_row)
                    if item is not None:
                        self.clearSelection()
                        item.setSelected(True)
                        self.setCurrentItem(item, QItemSelectionModel.ClearAndSelect)
                else:
                    self.clearSelection()
                    self.setCurrentItem(None)
            self._gesture = None
            event.accept()
            return
        if self._gesture == "pending":
            # Below the drag threshold: treat as a plain click selection.
            with contextlib.suppress(RuntimeError):
                self.viewport().releaseMouse()
            if (
                self._press_row is not None
                and 0 <= self._press_row < self.count()
            ):
                item = self.item(self._press_row)
                self.clearSelection()
                item.setSelected(True)
                self.setCurrentItem(item, QItemSelectionModel.ClearAndSelect)
            self._gesture = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def focusOutEvent(self, event):
        if getattr(self, "_drag_active", False):
            self.cancel_manual_drag()
        super().focusOutEvent(event)

    def hideEvent(self, event):
        if getattr(self, "_drag_active", False):
            self.cancel_manual_drag()
        self._release_viewport_grab()
        super().hideEvent(event)

    def paintEvent(self, event):
        self._paint_list(event)

    def _paint_list(self, event):
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setRenderHint(QPainter.TextAntialiasing, True)
            font = QFont(self.font())
            font.setPointSizeF(max(10.0, font.pointSizeF() + 1.0))
            font.setWeight(QFont.DemiBold)
            painter.setFont(font)
            painter.setPen(QColor(152, 162, 179))
            painter.drawText(self.viewport().rect(), Qt.AlignCenter, "暂无步骤")
            return

    def startDrag(self, supported_actions):
        # Kept as the public entry point used by tests and any external caller.
        # Real user drags go through the gesture state machine in
        # mouseMoveEvent, which calls _begin_manual_drag directly. Qt never
        # invokes this itself because setDragEnabled(False) is set in __init__.
        del supported_actions
        self._begin_manual_drag(self.currentRow())
