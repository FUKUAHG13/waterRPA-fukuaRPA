# pyright: reportAttributeAccessIssue=false
"""Non-destructive search and navigation for workflow steps."""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer
from PySide6.QtWidgets import QAbstractItemView

from ...workflow_summary import task_drag_summary


class WorkflowSearchMixin:
    """Find visible workflow rows without filtering or changing task data."""

    def _init_task_search(self, timer_parent=None):
        self._task_search_query = ""
        self._task_search_matches = []
        self._task_search_position = -1
        self._task_search_hidden_count = 0
        self._task_search_timer = QTimer(timer_parent)
        self._task_search_timer.setSingleShot(True)
        self._task_search_timer.setInterval(120)
        self._task_search_timer.timeout.connect(self.refresh_task_search)

    def show_task_search(self):
        host = self.host
        host.task_search_widget.show()
        host.task_search_spacer.hide()
        host.task_search_edit.setFocus(Qt.ShortcutFocusReason)
        host.task_search_edit.selectAll()
        self.refresh_task_search()

    def close_task_search(self):
        self._task_search_timer.stop()
        self._task_search_query = ""
        self._task_search_matches = []
        self._task_search_position = -1
        self._task_search_hidden_count = 0
        self.host.task_search_edit.blockSignals(True)
        self.host.task_search_edit.clear()
        self.host.task_search_edit.blockSignals(False)
        self._task_search_timer.stop()
        self.host.task_search_status.clear()
        self.host.task_search_widget.hide()
        self.host.task_search_spacer.show()
        self.host.task_search_btn.setFocus(Qt.OtherFocusReason)

    def queue_task_search(self, text):
        self._task_search_query = str(text or "")
        self._task_search_timer.start()

    def _task_search_text(self, row):
        data = self._task_data_at(row) or {}
        summary = task_drag_summary(
            data,
            row + 1,
            self.host.current_settings_mode(),
        )
        if self.show_step_notes:
            summary = f"{summary} {data.get('step_note', '')}"
        return summary.casefold()

    def refresh_task_search(self, *, preserve_current=True):
        query = str(self.host.task_search_edit.text() or "").strip().casefold()
        self._task_search_query = query
        if not query:
            self._task_search_matches = []
            self._task_search_position = -1
            self._task_search_hidden_count = 0
            self.host.task_search_status.clear()
            return

        task_list = self.host.task_list
        visible_matches = []
        hidden_count = 0
        for row in range(task_list.count()):
            if query not in self._task_search_text(row):
                continue
            item = task_list.item(row)
            if item.isHidden():
                hidden_count += 1
            else:
                visible_matches.append(row)

        current_row = task_list.currentRow()
        self._task_search_matches = visible_matches
        self._task_search_hidden_count = hidden_count
        if not visible_matches:
            self._task_search_position = -1
            self._update_task_search_status()
            return
        if preserve_current and current_row in visible_matches:
            self._task_search_position = visible_matches.index(current_row)
        else:
            self._task_search_position = 0
            self._select_task_search_match()
        self._update_task_search_status()

    def find_task_match(self, direction=1):
        if not self._task_search_query:
            self.show_task_search()
            return False
        self.refresh_task_search(preserve_current=True)
        if not self._task_search_matches:
            return False
        step = 1 if int(direction) >= 0 else -1
        self._task_search_position = (
            self._task_search_position + step
        ) % len(self._task_search_matches)
        self._select_task_search_match()
        self._update_task_search_status()
        return True

    def _select_task_search_match(self):
        if not (
            0 <= self._task_search_position < len(self._task_search_matches)
        ):
            return False
        row = self._task_search_matches[self._task_search_position]
        task_list = self.host.task_list
        item = task_list.item(row)
        task_list.clearSelection()
        task_list.setCurrentItem(
            item,
            QItemSelectionModel.ClearAndSelect,
        )
        task_list.scrollToItem(item, QAbstractItemView.PositionAtCenter)
        task_list.schedule_virtual_refresh(immediate=True)
        self.host.ensure_task_item_widget(item)
        self.host.update_selection_highlight(force=True)
        return True

    def _update_task_search_status(self):
        visible_count = len(self._task_search_matches)
        hidden_suffix = (
            f"，另有 {self._task_search_hidden_count} 个已隐藏"
            if self._task_search_hidden_count
            else ""
        )
        if visible_count:
            current = max(0, self._task_search_position) + 1
            text = f"{current}/{visible_count}{hidden_suffix}"
        elif self._task_search_hidden_count:
            text = f"无可见结果，{self._task_search_hidden_count} 个已隐藏"
        else:
            text = "无结果"
        self.host.task_search_status.setText(text)
