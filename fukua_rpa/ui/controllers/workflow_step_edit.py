# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Step/row creation, editing, deletion, selection and clipboard for WorkflowEditorController.

Extracted verbatim from workflow_editor.py; methods call each other (and the
rest of the controller) through ``self``, so behavior is unchanged.
"""

from __future__ import annotations

import copy
import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QApplication,
    QInputDialog,
    QListWidgetItem,
    QPushButton,
    QToolTip,
)

from ...workflow_document import (
    REFERENCE_FIELDS,
    normalize_workflow_tasks,
    remove_tasks_and_follow_references,
)
from ...workflow_metadata import (
    MAX_STEP_NOTE_LENGTH,
    normalize_editor_metadata,
    normalize_marker_color,
    normalize_step_note,
)
from ...workflow_summary import task_drag_summary
from ..accessibility import apply_accessible_metadata
from ..lifecycle import is_runtime_editing_locked
from ..task_row import TaskRow

logger = logging.getLogger(__name__)


class WorkflowStepEditMixin:
    """Step/row editing glue."""

    def _task_data_at(self, row):
        item = self.host.task_list.item(int(row))
        if item is None:
            return None
        widget = self.host.task_list.itemWidget(item)
        data = widget.get_data() if widget else item.data(Qt.UserRole)
        return data if isinstance(data, dict) else None


    def _apply_task_updates(self, rows, updates, source):
        if is_runtime_editing_locked(self.host):
            return 0
        rows = sorted({int(row) for row in rows if 0 <= int(row) < self.host.task_list.count()})
        if not rows:
            return 0
        before = self.host.snapshot_tasks()
        changed_rows = []
        prepared = dict(updates)
        if "step_note" in prepared:
            prepared["step_note"] = normalize_step_note(prepared["step_note"])
        if "marker_color" in prepared:
            prepared["marker_color"] = normalize_marker_color(
                prepared["marker_color"]
            )
        if "editor_hidden" in prepared:
            prepared["editor_hidden"] = bool(prepared["editor_hidden"])
        for row in rows:
            old_data = self._task_data_at(row)
            if old_data is None:
                continue
            new_data = dict(old_data)
            new_data.update(copy.deepcopy(prepared))
            if new_data == old_data:
                continue
            item = self.host.task_list.item(row)
            widget = self.host.task_list.itemWidget(item)
            if widget is not None:
                widget.custom_data.update(copy.deepcopy(prepared))
                widget.update_markers()
                widget.sync_data()
            else:
                item.setData(Qt.UserRole, new_data)
            changed_rows.append(row)
        if not changed_rows:
            return 0
        self.host.push_undo_snapshot(before)
        self.host.update_indexes()
        after = self.host.snapshot_tasks()
        for row in changed_rows:
            self.log_task_edit(row + 1, before[row], after[row], source)
        self.refresh_editor_visibility()
        return len(changed_rows)


    def apply_task_snapshot_changes(self, tasks, source):
        """Apply per-row task replacements as one undo and audit transaction."""

        if is_runtime_editing_locked(self.host):
            return 0
        before = self.host.snapshot_tasks()
        if not isinstance(tasks, (list, tuple)) or len(tasks) != len(before):
            return 0
        after = normalize_workflow_tasks(tasks)
        before_ids = [str(task.get("step_id", "")) for task in before]
        after_ids = [str(task.get("step_id", "")) for task in after]
        # SAFETY: preview callbacks resolve stable IDs against a fresh snapshot.
        # Refuse stale results instead of applying coordinates to reordered rows.
        if before_ids != after_ids:
            return 0
        changed_rows = [
            row
            for row, (old, new) in enumerate(zip(before, after, strict=True))
            if old != new
        ]
        if not changed_rows:
            return 0

        self.host.push_undo_snapshot(before)
        task_list = self.host.task_list
        task_list.setUpdatesEnabled(False)
        try:
            for row in changed_rows:
                item = task_list.item(row)
                if item is None:
                    continue
                replacement = copy.deepcopy(after[row])
                widget = task_list.itemWidget(item)
                if widget is not None:
                    widget.set_data(replacement)
                    widget.sync_data()
                else:
                    item.setData(Qt.UserRole, replacement)
                    item.setData(
                        Qt.UserRole + 1,
                        task_drag_summary(
                            replacement,
                            row + 1,
                            self.host.current_settings_mode(),
                        ),
                    )
                    item.setText("")
        finally:
            task_list.setUpdatesEnabled(True)

        self.host.update_indexes()
        committed = self.host.snapshot_tasks()
        for row in changed_rows:
            self.log_task_edit(row + 1, before[row], committed[row], source)
        return len(changed_rows)


    def set_selected_marker_color(self, color_id):
        return self._apply_task_updates(
            self.selected_row_indexes(),
            {"marker_color": color_id},
            "颜色标记",
        )


    def set_selected_hidden(self, hidden):
        if self.host.current_settings_mode() != "advanced":
            return 0
        return self._apply_task_updates(
            self.selected_row_indexes(),
            {"editor_hidden": bool(hidden)},
            "编辑器隐藏状态",
        )


    def edit_selected_note(self):
        rows = self.selected_row_indexes()
        if not rows:
            return 0
        notes = {
            normalize_step_note((self._task_data_at(row) or {}).get("step_note", ""))
            for row in rows
        }
        initial = notes.pop() if len(notes) == 1 else ""
        note, accepted = QInputDialog.getMultiLineText(
            self.host,
            "步骤备注",
            (
                "备注（将应用到全部选中步骤）："
                if len(rows) > 1
                else "备注："
            ),
            initial,
        )
        if not accepted:
            return 0
        note = normalize_step_note(note)
        if len(str(note)) > MAX_STEP_NOTE_LENGTH:
            note = note[:MAX_STEP_NOTE_LENGTH]
        return self._apply_task_updates(rows, {"step_note": note}, "备注")


    def _restore_structured_tasks(
        self,
        before,
        after,
        selected_ids,
        message,
        *,
        private_message=None,
        audit_source="",
    ):
        if before == after:
            return False
        self.host.push_undo_state()
        self.host.restore_task_snapshot(after)
        selected_ids = {str(value) for value in selected_ids}
        first = True
        for row in range(self.host.task_list.count()):
            data = self._task_data_at(row) or {}
            if str(data.get("step_id", "")) not in selected_ids:
                continue
            self.host.task_list.select_item_with_modifiers(
                self.host.task_list.item(row),
                Qt.NoModifier if first else Qt.ControlModifier,
            )
            first = False
        self.host.append_application_log(message, private_msg=private_message)
        if audit_source:
            before_by_id = {
                str(task.get("step_id", "")): (index, task)
                for index, task in enumerate(before)
            }
            for index, task in enumerate(after):
                step_id = str(task.get("step_id", ""))
                previous = before_by_id.get(step_id)
                if previous is None or previous[1] == task:
                    continue
                self.log_task_edit(index + 1, previous[1], task, audit_source)
        return True


    def add_row(
        self,
        data=None,
        index=None,
        record_undo=True,
        select=True,
        materialize=True,
        update_indexes=True,
    ):
        if is_runtime_editing_locked(self.host):
            return None
        if record_undo:
            self.host.push_undo_state()
        materialize = bool(materialize or data is None)
        item = QListWidgetItem()
        if index is None:
            self.host.task_list.addItem(item)
        else:
            self.host.task_list.insertItem(max(0, min(index, self.host.task_list.count())), item)
        inserted_row = self.host.task_list.row(item)
        if materialize:
            self.host.restore_row_widget(item, data or {})
        else:
            task_data = copy.deepcopy(data)
            item.setSizeHint(self.host.task_list.virtual_row_size_hint())
            item.setData(Qt.UserRole, task_data)
            item.setData(
                Qt.UserRole + 1,
                task_drag_summary(
                    task_data,
                    self.host.task_list.row(item) + 1,
                    self.host.current_settings_mode(),
                ),
            )
            item.setText("")
        if select:
            self.host.task_list.setCurrentItem(item)
        if update_indexes:
            self.host.update_indexes()
        if select:
            self.host.task_list.schedule_virtual_refresh(immediate=True)
        if record_undo:
            self.host.append_application_log(f"已新增第 {inserted_row + 1} 步。")
        return item


    def add_rows(self, tasks, *, record_undo=True, select_last=True):
        """Append a task batch with one history snapshot and one index refresh."""

        if is_runtime_editing_locked(self.host):
            return 0

        prepared = [
            copy.deepcopy(task)
            for task in tasks
            if isinstance(task, dict)
        ]
        if not prepared:
            return 0
        if record_undo:
            self.host.push_undo_state()

        task_list = self.host.task_list
        task_list.setUpdatesEnabled(False)
        try:
            for task in prepared:
                self.add_row(
                    task,
                    record_undo=False,
                    select=False,
                    materialize=False,
                    update_indexes=False,
                )
            self.host.update_indexes()
            if select_last:
                task_list.setCurrentItem(task_list.item(task_list.count() - 1))
        finally:
            task_list.setUpdatesEnabled(True)
            task_list.viewport().update()
        task_list.schedule_virtual_refresh(immediate=True)
        if record_undo:
            first = task_list.count() - len(prepared) + 1
            last = task_list.count()
            self.host.append_application_log(
                f"已批量新增 {len(prepared)} 个步骤（第 {first} 至 {last} 步）。"
            )
        return len(prepared)


    def _remove_task_at(self, row, *, copy_to_clipboard=False):
        if is_runtime_editing_locked(self.host):
            return False
        if not 0 <= int(row) < self.host.task_list.count():
            return False
        tasks = self.host.snapshot_tasks()
        target_data = tasks[int(row)]
        target_id = str(target_data.get("step_id", ""))
        if not self._confirm_remove_references(
            tasks, target_id, follow=self._step_adjust_follow_enabled()
        ):
            return False
        if copy_to_clipboard:
            self._store_task_clipboard(target_data)
        self.host.push_undo_state()
        repaired = remove_tasks_and_follow_references(
            tasks, [target_id], follow=self._step_adjust_follow_enabled()
        )
        repaired_by_id = {
            str(task.get("step_id", "")): task for task in repaired
        }
        item = self.host.task_list.item(int(row))
        row_widget = self.host.task_list.itemWidget(item)
        if row_widget is not None and hasattr(row_widget, "close_config_dialog"):
            row_widget.close_config_dialog()
        if row_widget is not None:
            self.host.task_list.removeItemWidget(item)
        self.host.task_list.takeItem(int(row))
        if row_widget is not None:
            row_widget.hide()
            row_widget.deleteLater()
        for index in range(self.host.task_list.count()):
            item = self.host.task_list.item(index)
            widget = self.host.task_list.itemWidget(item)
            current_data = (
                widget.get_data() if widget else item.data(Qt.UserRole)
            )
            if not isinstance(current_data, dict):
                continue
            step_id = str(current_data.get("step_id", ""))
            repaired_data = repaired_by_id.get(step_id)
            if not repaired_data:
                continue
            if widget:
                for jump_field, target_field, _label in REFERENCE_FIELDS:
                    widget.custom_data[jump_field] = repaired_data[jump_field]
                    widget.custom_data[target_field] = repaired_data[target_field]
            else:
                item.setData(Qt.UserRole, repaired_data)
        self.host.update_indexes()
        QToolTip.hideText()
        self.host.task_list.viewport().update()
        self.host.delete_hover_timer.start(25)
        action = "剪切" if copy_to_clipboard else "删除"
        self._log_deleted_tasks(action, [int(row)], [target_data])
        return True


    def del_row(self, row_widget):
        for row in range(self.host.task_list.count()):
            item = self.host.task_list.item(row)
            if self.host.task_list.itemWidget(item) is row_widget:
                return self._remove_task_at(row)
        return False


    def refresh_task_list_delete_hover(self):
        for button in self.host.task_list.findChildren(QPushButton):
            refresh = getattr(button, "set_synthetic_hover", None)
            if callable(refresh):
                refresh(False)
        target = QApplication.widgetAt(QCursor.pos())
        if target is None or not self.host.task_list.isAncestorOf(target):
            return
        refresh = getattr(target, "set_synthetic_hover", None)
        if callable(refresh) and target.property("variant") == "dangerGhost":
            refresh(True)


    def insert_row_before_selected(self):
        if is_runtime_editing_locked(self.host):
            return
        row = self.host.selected_row_index()
        self.host.add_row(index=(row if row is not None else self.host.task_list.count()))


    def delete_selected_rows(self, *, copy_to_clipboard=False, rows=None):
        if is_runtime_editing_locked(self.host):
            return False
        rows = self.selected_row_indexes() if rows is None else sorted(
            {
                int(row)
                for row in rows
                if 0 <= int(row) < self.host.task_list.count()
            }
        )
        if not rows:
            return False
        tasks = self.host.snapshot_tasks()
        selected_tasks = [tasks[row] for row in rows]
        target_ids = {str(task.get("step_id", "")) for task in selected_tasks}
        if not self._confirm_remove_references(
            tasks, target_ids, follow=self._step_adjust_follow_enabled()
        ):
            return False
        if copy_to_clipboard:
            self._store_task_clipboard(
                selected_tasks[0] if len(selected_tasks) == 1 else selected_tasks
            )
        self.host.push_undo_state()
        # FR-20261006-013: 关闭被删行上仍打开的取点器。它的回调绑定在被删控件上，
        # 不关闭的话用户点击会写进已脱离列表的控件——界面无反应、坐标静默丢失。
        # 必须在 restore_task_snapshot 之前按**行下标**关闭：那之后行已重排。
        # 行->控件的查找由 RPAWindow.close_coordinate_pickers 承担，本mixin 不再
        # 新增方法（其方法数已触及tests/test_workflow_editor_split.py 的预算上限）。
        self.host.close_coordinate_pickers(rows)
        repaired = remove_tasks_and_follow_references(
            tasks, target_ids, follow=self._step_adjust_follow_enabled()
        )
        first_row = rows[0]
        self.host.restore_task_snapshot(repaired)
        if self.host.task_list.count():
            self.host.task_list.setCurrentRow(
                min(first_row, self.host.task_list.count() - 1)
            )
        action = "剪切" if copy_to_clipboard else "删除"
        self._log_deleted_tasks(action, rows, selected_tasks)
        return True


    def restore_row_widget(self, item, data):
        existing = self.host.task_list.itemWidget(item)
        if existing is not None:
            return existing
        row_widget = TaskRow(
            delete_callback=self.host.del_row,
            settings_mode=self.host.current_settings_mode(),
        )
        if data:
            row_widget.set_data(data)
        # Capture code-defined base sizes while the row is still detached.  A
        # row created inside a low-DPI window already inherits that window's
        # density stylesheet; capturing after setItemWidget() would therefore
        # shrink fixed controls (for example the 30 px gear button) twice.
        self.host.apply_ui_scale_to_widget(row_widget)
        item.setSizeHint(row_widget.sizeHint())
        self.host.task_list.setItemWidget(item, row_widget)
        # FR-20261008-803: step rows are created dynamically, so the window-level
        # apply_accessible_metadata calls (run once at construction) never reached
        # their icon-only buttons — the gear and delete buttons have no text and
        # exposed an empty accessible name to screen readers. Apply it per row.
        apply_accessible_metadata(
            row_widget,
            root_name=f"步骤 {self.host.task_list.row(item) + 1}",
        )
        row_widget.set_parent_item(item)
        row_widget.set_note_display_visible(self.show_step_notes)
        metadata = normalize_editor_metadata(row_widget.get_data())
        row_widget.set_hidden_reveal_faded(
            metadata["editor_hidden"]
            and self.host.current_settings_mode() == "advanced"
            and self.show_hidden_steps
        )
        row_widget.set_group_presentation(item.data(Qt.UserRole + 2))
        row_widget.set_selected(item.isSelected())
        item.setData(Qt.UserRole, row_widget.get_data())
        item.setData(Qt.UserRole + 1, row_widget.drag_summary())
        item.setText("")
        row_index = self.host.task_list.row(item)
        if row_index >= 0:
            row_widget.set_index(
                row_index + 1,
                self.host.task_list.count(),
            )
            if self.host.debug_paused_step == row_index + 1:
                row_widget.set_debug_paused(True)
        self.host.apply_ui_scale_to_widget(row_widget)
        item.setSizeHint(row_widget.sizeHint())
        row_widget.set_runtime_editing_enabled(
            not is_runtime_editing_locked(self.host)
        )
        return row_widget


    def ensure_task_item_widget(self, item):
        if item is None:
            return None
        widget = self.host.task_list.itemWidget(item)
        if widget is not None:
            return widget
        data = item.data(Qt.UserRole)
        if not isinstance(data, dict):
            return None
        return self.host.restore_row_widget(item, data)


    def update_selection_highlight(self, force=False):
        """Schedule a coalesced, diff-based selection repaint.

        Only rows whose selected state actually changed are repainted, and
        rapid selection bursts (rubber-band drags) collapse into one paint
        per frame instead of one per changed row.
        """

        if force:
            self._last_highlighted_selected = set()
        if not self._selection_highlight_timer.isActive():
            self._selection_highlight_timer.start(0)


    def _apply_selection_highlight(self):
        task_list = self.host.task_list
        current = set(task_list.selected_row_indexes())
        previous = self._last_highlighted_selected
        if current == previous:
            return
        # Only touch rows whose selected state actually changed; re-applying
        # set_selected to unchanged rows re-runs style unpolish/polish on every
        # rubber-band frame and pegs the CPU during box-select. (#4)
        for row in (current - previous) | (previous - current):
            if 0 <= row < task_list.count():
                item = task_list.item(row)
                widget = task_list.itemWidget(item)
                if widget is not None and hasattr(widget, "set_selected"):
                    widget.set_selected(item.isSelected())
        self._last_highlighted_selected = current
