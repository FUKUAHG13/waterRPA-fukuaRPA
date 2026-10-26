# pyright: reportAttributeAccessIssue=false
"""DraggableListWidget 的拖拽手势辅助 Mixin（God Class 拆分）。

从 ``DraggableListWidget`` 提取的方法组：命中判定、修饰键选择、
落点/插入线计算、拖拽源视觉与自动滚动、以及手动拖拽手势的
开始/更新/结束/取消辅助逻辑。Qt 事件处理器入口仍保留在主控件，
经 MRO 调用本 Mixin。实例状态（``_gesture``、``_drag_active``、
``_press_*``、``drop_line_*``、``_drag_autoscroll_timer`` 等）仍由主类
``__init__`` 创建，本 Mixin 不持有任何状态。

INVARIANT: ``_begin_manual_drag``/``_update_manual_drag``/
``_finish_manual_drag``/``_reset_manual_drag_state`` 之间的调用关系、
grabMouse/releaseMouse/releaseKeyboard 时序与 ``_drag_active`` 守卫
必须逐字保留，手势状态机语义零变化。
"""

import contextlib
import time

from PySide6.QtCore import QItemSelectionModel, QPoint, QPointF, Qt
from PySide6.QtGui import QCursor, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QLineEdit,
    QSlider,
)

from ..lifecycle import is_runtime_editing_locked
from ..performance_hooks import ui_metrics


class DragGestureMixin:
    def _hit_control(self, pos):
        """Return True when the press landed on an interactive row control.

        Presses on buttons/inputs/combos must keep their native behaviour
        instead of starting a step drag.
        """

        item = self.itemAt(pos)
        if item is None:
            return False
        widget = self.itemWidget(item)
        if widget is None:
            return False
        local = widget.mapFrom(self.viewport(), pos)
        child = widget.childAt(local)
        while child is not None:
            if isinstance(
                child,
                (
                    QAbstractButton,
                    QComboBox,
                    QLineEdit,
                    QAbstractSpinBox,
                    QSlider,
                ),
            ):
                return True
            if child is widget:
                break
            child = child.parent()
        return False

    def select_item_with_modifiers(self, item, modifiers=Qt.NoModifier):
        """Apply desktop-style Ctrl/Shift selection from an embedded row widget."""

        row = self.row(item)
        if row < 0:
            return
        modifiers = Qt.KeyboardModifiers(modifiers)
        if modifiers & Qt.ShiftModifier:
            anchor = self._selection_anchor_row
            if anchor is None or not 0 <= anchor < self.count():
                current = self.currentRow()
                anchor = current if current >= 0 else row
            if not modifiers & Qt.ControlModifier:
                self.clearSelection()
            start, end = sorted((anchor, row))
            for index in range(start, end + 1):
                candidate = self.item(index)
                if not candidate.isHidden():
                    candidate.setSelected(True)
            self.setCurrentRow(row, QItemSelectionModel.NoUpdate)
            return
        if modifiers & Qt.ControlModifier:
            item.setSelected(not item.isSelected())
            self.setCurrentRow(row, QItemSelectionModel.NoUpdate)
            self._selection_anchor_row = row
            return
        self.clearSelection()
        self.setCurrentItem(item, QItemSelectionModel.ClearAndSelect)
        self._selection_anchor_row = row

    def _release_viewport_grab(self):
        """Release any lingering mouse grab from an interrupted drag gesture.

        If the window loses focus or is hidden while a drag is in progress,
        the release event may never arrive, leaving the viewport with a
        permanent grab that blocks all mouse input (including title-bar
        buttons, menus, and other widgets).  Calling this from hide/close
        ensures the grab is always cleaned up.
        """
        try:
            vp = self.viewport()
            if vp is not None:
                vp.releaseMouse()
        except RuntimeError:
            pass

    def _event_pos(self, event):
        if hasattr(event, "position"):
            return event.position().toPoint()
        return event.pos()

    def route_row_mouse_event(self, event, row_widget, kind):
        """Re-dispatch a row-widget mouse event through the gesture machine.

        Row widgets sit on top of list items and swallow mouse events, so the
        list's own mousePressEvent/Move/Release would never fire for presses on
        a step body. The TaskRow forwards those events here after mapping the
        coordinates back into viewport space, letting the gesture state machine
        (pending/box/modifier/control) run exactly as if the list received them.
        """

        if row_widget is None:
            return
        pos = row_widget.mapTo(self.viewport(), event.pos())
        global_pos = (
            event.globalPosition().toPoint()
            if hasattr(event, "globalPosition")
            else event.globalPos()
        )
        mapped = QMouseEvent(
            event.type(),
            QPointF(pos),
            global_pos,
            event.button(),
            event.buttons(),
            event.modifiers(),
        )
        if kind == "press":
            self.mousePressEvent(mapped)
        elif kind == "move":
            self.mouseMoveEvent(mapped)
        elif kind == "release":
            self.mouseReleaseEvent(mapped)

    def _update_drop_line_at(self, pos):
        item = self.itemAt(pos)
        if item is None:
            self.drop_line_row = self.count() - 1 if self.count() else 0
            self.drop_line_after = True
            self._drop_line_width = 4
            self._update_drop_indicator()
            return

        row = self.row(item)
        rect = self.visualItemRect(item)
        self.drop_line_row = row
        self.drop_line_after = pos.y() > rect.center().y()
        boundary_y = rect.bottom() + 1 if self.drop_line_after else rect.top()
        distance = abs(int(pos.y()) - int(boundary_y))
        range_px = max(12, rect.height() // 2)
        proximity = max(0.0, 1.0 - distance / range_px)
        self._drop_line_width = 4 + round(proximity * 4)
        self._update_drop_indicator()

    def _insertion_index(self):
        if self.drop_line_row is None:
            return 0
        insertion = self.drop_line_row + (1 if self.drop_line_after else 0)
        moving_rows = self._drag_rows()
        insertion -= sum(1 for row in moving_rows if row < insertion)
        return max(0, min(insertion, max(0, self.count() - len(moving_rows))))

    def _drag_rows(self):
        source = (
            self._drag_source_row
            if self._drag_source_row is not None
            else self.currentRow()
        )
        selected = self.selected_row_indexes()
        if source is not None and source in selected:
            return selected
        return [source] if source is not None else []

    def _update_drop_indicator(self):
        if self.drop_line_row is None or not self.count():
            self._drop_indicator_key = None
            self.drop_indicator.clear()
            return
        row = max(0, min(self.drop_line_row, self.count() - 1))
        rect = self.visualItemRect(self.item(row))
        y = rect.bottom() + 1 if self.drop_line_after else rect.top()
        indicator_key = (
            row,
            bool(self.drop_line_after),
            int(y),
            int(self._drop_line_width),
        )
        if (
            indicator_key == self._drop_indicator_key
            and self.drop_indicator.isVisible()
        ):
            return
        self._drop_indicator_key = indicator_key
        self.drop_indicator.show_at(y, self._drop_line_width)

    def _position_drag_source_badge(self):
        badge = self.drag_source_badge
        if not badge.isVisible():
            return
        maximum = max(120, self.viewport().width() - 24)
        badge.setMaximumWidth(maximum)
        badge.adjustSize()
        badge.move(12, 10)
        badge.raise_()

    def _set_drag_source_visual(self, active):
        source = self._drag_source_row
        if source is not None and 0 <= source < self.count():
            item = self.item(source)
            widget = self.itemWidget(item)
            if widget is None and active:
                restore = getattr(self.window(), "ensure_task_item_widget", None)
                widget = restore(item) if callable(restore) else None
            if widget is not None:
                widget.setProperty("dragSource", bool(active))
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                widget.update()
            if active:
                moving_rows = self._drag_rows()
                if len(moving_rows) > 1:
                    first, last = moving_rows[0] + 1, moving_rows[-1] + 1
                    summary = f"{len(moving_rows)} 个步骤（第 {first} 至 {last} 步）"
                else:
                    summary = str(
                        item.data(Qt.UserRole + 1)
                        or item.text()
                        or f"第 {source + 1} 步"
                    )
                self.drag_source_badge.setText(f"正在移动：{summary}")
                self.drag_source_badge.show()
                self._position_drag_source_badge()
                return
        self.drag_source_badge.hide()

    def _update_manual_drag(self, pos, *, force=False):
        if not self._drag_active:
            return False
        self._drag_pending_pos = QPoint(pos)
        now = time.perf_counter_ns()
        if (
            not force
            and self._drag_last_update_ns
            and now - self._drag_last_update_ns
            < self.MANUAL_DRAG_UPDATE_NS
        ):
            metrics = ui_metrics(self)
            if metrics is not None:
                metrics.increment("ui.task_drag.updates_coalesced")
            return False
        self._drag_last_update_ns = now
        self._update_drop_line_at(self._drag_pending_pos)
        metrics = ui_metrics(self)
        if metrics is not None:
            metrics.increment("ui.task_drag.target_updates")
        return True

    def _auto_scroll_manual_drag(self):
        if not self._drag_active:
            self._drag_autoscroll_timer.stop()
            return
        viewport = self.viewport()
        pos = viewport.mapFromGlobal(QCursor.pos())
        self._drag_pending_pos = QPoint(pos)
        scroll_bar = self.verticalScrollBar()
        step = max(12, scroll_bar.singleStep())
        old_value = scroll_bar.value()
        if pos.y() < self.AUTO_SCROLL_MARGIN:
            scroll_bar.setValue(old_value - step)
        elif pos.y() > viewport.height() - self.AUTO_SCROLL_MARGIN:
            scroll_bar.setValue(old_value + step)
        scrolled = scroll_bar.value() != old_value
        self._update_manual_drag(pos, force=scrolled)

    def _reset_manual_drag_state(self):
        self._drag_autoscroll_timer.stop()
        self._set_drag_source_visual(False)
        self.clear_drag_placeholders()
        with contextlib.suppress(RuntimeError):
            self.viewport().releaseMouse()
        self.viewport().unsetCursor()
        self._drag_active = False
        self._drag_source_row = None
        self._drag_pending_pos = None
        self._drag_last_update_ns = 0
        self.drop_line_row = None
        self._drop_indicator_key = None
        self.drop_indicator.clear()
        with contextlib.suppress(AttributeError):
            self.setState(QAbstractItemView.NoState)

    def _finish_manual_drag(self, *, commit, pos=None):
        if not self._drag_active:
            return False
        if pos is not None:
            self._update_manual_drag(pos, force=True)
        source = self._drag_source_row
        moving_rows = self._drag_rows()
        target = self._insertion_index()
        self._reset_manual_drag_state()
        if (
            not commit
            or source is None
            or source < 0
            or source >= self.count()
            or not moving_rows
        ):
            self.schedule_virtual_refresh()
            return False

        original = [self.item(index) for index in range(self.count())]
        moving_set = set(moving_rows)
        moving_items = [original[index] for index in moving_rows]
        moving_step_ids = [
            str((item.data(Qt.UserRole) or {}).get("step_id", ""))
            for item in moving_items
            if isinstance(item.data(Qt.UserRole), dict)
        ]
        remaining = [
            item for index, item in enumerate(original) if index not in moving_set
        ]
        reordered = remaining[:target] + moving_items + remaining[target:]
        if reordered == original:
            self.schedule_virtual_refresh()
            return False

        owner = self.window()
        push_undo = getattr(owner, "push_undo_state", None)
        if callable(push_undo):
            push_undo()
        # SAFETY (FR-20260809-1101): close each moving row's config dialog and
        # detach its widget before takeItem, mirroring the delete path.  A
        # NonModal dialog left open would otherwise bind to the orphaned row
        # after takeItem unbinds item<->widget (see close_and_detach_row_widget).
        for row in reversed(moving_rows):
            self.close_and_detach_row_widget(row)
            self.takeItem(row)
        for offset, item in enumerate(moving_items):
            self.insertItem(target + offset, item)
            item.setSelected(True)
        self.setCurrentItem(moving_items[0], QItemSelectionModel.NoUpdate)
        reordered = getattr(owner, "handle_tasks_reordered", None)
        if callable(reordered):
            reordered(moving_step_ids, previous_rows=moving_rows)
        else:
            update_indexes = getattr(owner, "update_indexes", None)
            if callable(update_indexes):
                update_indexes()
        self.scrollToItem(moving_items[0])
        self.schedule_virtual_refresh(immediate=True)
        metrics = ui_metrics(self)
        if metrics is not None:
            metrics.increment("ui.task_drag.committed")
        return True

    def cancel_manual_drag(self):
        return self._finish_manual_drag(commit=False)

    def _begin_manual_drag(self, source_row):
        if self._drag_active:
            return
        owner = self.window()
        if is_runtime_editing_locked(owner):
            return
        if source_row is None or source_row < 0 or source_row >= self.count():
            return
        self._drag_active = True
        self._drag_source_row = source_row
        self._set_drag_source_visual(True)
        # Virtual widgets stay frozen during drag to keep pointer movement
        # cheap. Lightweight item summaries preserve off-screen context while
        # auto-scrolling without constructing hundreds of row editors.
        self.show_drag_placeholders()
        self._drag_last_update_ns = 0
        with contextlib.suppress(AttributeError):
            self.setState(QAbstractItemView.DraggingState)
        self.viewport().setCursor(Qt.ClosedHandCursor)
        with contextlib.suppress(RuntimeError):
            self.viewport().grabMouse()
        cursor_pos = self.viewport().mapFromGlobal(QCursor.pos())
        self._update_manual_drag(cursor_pos, force=True)
        self._drag_autoscroll_timer.start()
        metrics = ui_metrics(self)
        if metrics is not None:
            metrics.increment("ui.task_drag.started")
