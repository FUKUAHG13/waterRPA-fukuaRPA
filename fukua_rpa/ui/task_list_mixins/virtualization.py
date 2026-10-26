# pyright: reportAttributeAccessIssue=false
"""DraggableListWidget 的虚拟化 Mixin（God Class 拆分）。

从 ``DraggableListWidget`` 提取的方法组：虚拟行高提示、按窗口可见区间
计算物化目标行、以及在滚动时物化/去物化行控件的刷新逻辑。实例状态
（``_virtual_*``、``_materialized_*``、``_drag_active`` 守卫等）仍由主类
``__init__`` 创建，本 Mixin 不持有任何状态，跨 Mixin 调用经 MRO 解析。
"""

import time

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtWidgets import QApplication

from ..performance_hooks import ui_metrics


class VirtualizationMixin:
    def show_drag_placeholders(self):
        """Expose summaries for non-materialized rows during a manual drag."""

        self.clear_drag_placeholders()
        for row in range(self.count()):
            item = self.item(row)
            if (
                item is None
                or item.isHidden()
                or self.itemWidget(item) is not None
                or item.text()
            ):
                continue
            summary = str(item.data(Qt.UserRole + 1) or "").strip()
            if not summary:
                continue
            item.setText(summary)
            self._drag_placeholder_items.append(item)

    def clear_drag_placeholders(self):
        """Clear only texts installed by :meth:`show_drag_placeholders`."""

        for item in self._drag_placeholder_items:
            if item is not None and self.row(item) >= 0:
                item.setText("")
        self._drag_placeholder_items.clear()

    def virtual_row_size_hint(self):
        return QSize(
            0,
            max(
                int(self._virtual_min_row_height),
                int(self._virtual_row_height),
            ),
        )

    def group_row_size_hint(self, presentation=None):
        presentation = presentation if isinstance(presentation, dict) else {}
        header_count = len(presentation.get("headers", []))
        controls_height = (
            0
            if presentation.get("controls_hidden")
            else self._virtual_row_height
        )
        header_height = int(self._virtual_group_header_height)
        return QSize(
            0,
            max(
                header_height,
                int(controls_height + header_count * header_height),
            ),
        )

    def update_virtual_row_height(self, scale):
        effective_scale = max(0.375, min(5.4, float(scale)))
        self._virtual_min_row_height = max(
            1, int(round(42 * effective_scale))
        )
        self._virtual_group_header_height = max(
            1, int(round(32 * effective_scale))
        )
        self._virtual_row_height = max(
            self._virtual_min_row_height,
            int(round(self.BASE_ROW_HEIGHT * effective_scale)),
        )
        size_hint = self.virtual_row_size_hint()
        for row in range(self.count()):
            item = self.item(row)
            if self.itemWidget(item) is None:
                presentation = item.data(Qt.UserRole + 2)
                item.setSizeHint(
                    self.group_row_size_hint(presentation)
                    if isinstance(presentation, dict)
                    else size_hint
                )
        self.schedule_virtual_refresh()

    def virtualization_active(self):
        return self.count() > self.VIRTUALIZATION_THRESHOLD

    def materialized_row_count(self):
        # O(1) when the incremental counter is valid (item count unchanged
        # since last refresh).  Falls back to an O(n) full-list scan when
        # the cache is invalidated — e.g. after items are added/removed or
        # external code (ensure_task_item_widget) materializes widgets
        # without notifying this class.
        total = self.count()
        if self._materialized_count is not None and self._materialized_count_items == total:
            return self._materialized_count
        count = sum(
            1
            for row in range(total)
            if self.itemWidget(self.item(row)) is not None
        )
        self._materialized_count = count
        self._materialized_count_items = total
        return count

    def schedule_virtual_refresh(self, immediate=False):
        if self._virtual_refreshing or self._drag_active:
            return
        if immediate:
            self._virtual_refresh_timer.stop()
            self.refresh_virtual_widgets()
            return
        if self._virtual_refresh_timer.isActive():
            return
        self._virtual_refresh_timer.start(self.VIRTUAL_REFRESH_INTERVAL_MS)

    def _target_materialized_rows(self):
        count = self.count()
        if count <= self.VIRTUALIZATION_THRESHOLD:
            return set(range(count))
        viewport = self.viewport()
        first_index = self.indexAt(QPoint(4, 4))
        last_index = self.indexAt(
            QPoint(4, max(4, viewport.height() - 5))
        )
        first = first_index.row() if first_index.isValid() else max(0, min(count - 1, self.currentRow()))
        if last_index.isValid():
            last = last_index.row()
        else:
            visible_guess = max(1, viewport.height() // self._virtual_row_height)
            last = min(count - 1, first + visible_guess)
        target = set(
            range(
                max(0, first - self.VIRTUAL_BUFFER_ROWS),
                min(count, last + self.VIRTUAL_BUFFER_ROWS + 1),
            )
        )
        current = self.currentRow()
        if 0 <= current < count:
            target.add(current)
        return target

    def refresh_virtual_widgets(self):
        if self._virtual_refreshing or self._drag_active:
            return
        owner = self.window()
        restore = getattr(owner, "restore_row_widget", None)
        if not callable(restore):
            return
        self._virtual_refreshing = True
        started_ns = time.perf_counter_ns()
        try:
            self.setUpdatesEnabled(False)
            target = self._target_materialized_rows()
            focus = QApplication.focusWidget()
            scan = target | self._last_target_materialized
            materialized_delta = 0
            for row in sorted(scan):
                if not (0 <= row < self.count()):
                    continue
                item = self.item(row)
                widget = self.itemWidget(item)
                if widget is None:
                    if row in target:
                        data = item.data(Qt.UserRole)
                        if isinstance(data, dict):
                            restore(item, data)
                            materialized_delta += 1
                    continue
                dialog = getattr(widget, "config_dialog", None)
                try:
                    keeps_dialog = bool(
                        dialog is not None and dialog.isVisible()
                    )
                except RuntimeError:
                    keeps_dialog = False
                picker = getattr(widget, "coordinate_picker", None)
                try:
                    keeps_picker = bool(picker is not None and picker.isVisible())
                except RuntimeError:
                    keeps_picker = False
                keeps_focus = bool(
                    focus is not None
                    and (focus is widget or widget.isAncestorOf(focus))
                )
                if row in target or keeps_dialog or keeps_picker or keeps_focus:
                    continue
                try:
                    data = widget.get_data()
                    summary = widget.drag_summary()
                except RuntimeError:
                    # C++ object already deleted
                    continue
                item.setData(Qt.UserRole, data)
                item.setData(Qt.UserRole + 1, summary)
                item.setSizeHint(
                    self.group_row_size_hint(item.data(Qt.UserRole + 2))
                )
                self.removeItemWidget(item)
                materialized_delta -= 1
                widget.hide()
                widget.deleteLater()
            self._last_target_materialized = target
            # Update the incremental materialized-row counter.
            total = self.count()
            if self._materialized_count is not None and self._materialized_count_items == total:
                self._materialized_count += materialized_delta
            else:
                # Item count changed or cache was never established;
                # invalidate so the next materialized_row_count() call
                # performs a full scan and re-establishes the baseline.
                self._materialized_count = None
                self._materialized_count_items = -1
            metrics = ui_metrics(self)
            if metrics is not None:
                metrics.increment("ui.task_list.virtual_refreshes")
                metrics.set_gauge(
                    "ui.task_list.total_rows", total
                )
                metrics.set_gauge(
                    "ui.task_list.materialized_rows",
                    self.materialized_row_count(),
                )
        finally:
            self.setUpdatesEnabled(True)
            metrics = ui_metrics(self)
            if metrics is not None:
                metrics.observe_since("ui.task_list.virtual_refresh", started_ns)
            self._virtual_refreshing = False
