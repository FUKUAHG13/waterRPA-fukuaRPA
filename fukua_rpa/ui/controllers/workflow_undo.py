# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Undo/redo history and task-snapshot management for WorkflowEditorController.

Extracted verbatim from workflow_editor.py; methods call each other (and the
rest of the controller) through ``self``, so behavior is unchanged.
"""

from __future__ import annotations

import copy
import logging

from PySide6.QtCore import Qt

from ...workflow_document import (
    apply_numeric_reference_edits,
    normalize_workflow_tasks,
)
from ..lifecycle import is_runtime_editing_locked
from .workflow_editor_shared import (
    MAX_UNDO_DEPTH,
    _snapshot_hash,
)

logger = logging.getLogger(__name__)


class WorkflowUndoMixin:
    """Undo/redo history and snapshot glue."""

    def snapshot_tasks(self):
        tasks = []
        for i in range(self.host.task_list.count()):
            item = self.host.task_list.item(i)
            widget = self.host.task_list.itemWidget(item)
            if widget:
                tasks.append(widget.get_data())
            else:
                tasks.append(item.data(Qt.UserRole))
        normalized = normalize_workflow_tasks(tasks)
        return copy.deepcopy(normalized)



    def resolve_task_reference_edits(self, row_widget, updated):
        tasks = self.host.snapshot_tasks()
        row_index = None
        for index in range(self.host.task_list.count()):
            item = self.host.task_list.item(index)
            if self.host.task_list.itemWidget(item) is row_widget:
                row_index = index
                break
        if row_index is None:
            return updated
        current_id = str(tasks[row_index].get("step_id", ""))
        edited = dict(updated)
        edited["step_id"] = current_id
        tasks[row_index] = edited
        return apply_numeric_reference_edits(edited, tasks)



    def push_undo_state(self):
        if self.restoring_history or self.host.is_switching_profile:
            return
        self.undo_stack.append(self.host.snapshot_tasks())
        if len(self.undo_stack) > MAX_UNDO_DEPTH:
            self.undo_stack.pop(0)
        self.redo_stack.clear()



    def push_undo_snapshot(self, tasks):
        if self.restoring_history or self.host.is_switching_profile or tasks is None:
            return
        snapshot = copy.deepcopy(tasks)
        current_hash = _snapshot_hash(snapshot)
        # A new edit always invalidates the redo stack, even when the resulting
        # state matches the last pushed snapshot (e.g. right after undo/redo).
        # Clear it before the dedup early-return so a later redo cannot jump to a
        # state that is no longer reachable from the current one.
        self.redo_stack.clear()
        if self._last_undo_hash is not None and current_hash == self._last_undo_hash:
            return
        if not self.undo_stack or self.undo_stack[-1] != snapshot:
            self.undo_stack.append(snapshot)
            if len(self.undo_stack) > MAX_UNDO_DEPTH:
                self.undo_stack.pop(0)
        self._last_undo_hash = current_hash



    def restore_task_snapshot(self, tasks):
        self.restoring_history = True
        try:
            self.host.close_all_task_config_dialogs()
            self.host.task_list.clear()
            for data in tasks:
                self.host.add_row(
                    data,
                    record_undo=False,
                    select=False,
                    materialize=False,
                    update_indexes=False,
                )
            self.host.update_indexes()
            self.host.task_list.schedule_virtual_refresh(immediate=True)
        finally:
            self.restoring_history = False



    def undo_task_change(self):
        if is_runtime_editing_locked(self.host):
            return
        if not self.undo_stack:
            return
        self.redo_stack.append(self.host.snapshot_tasks())
        tasks = self.undo_stack.pop()
        self.host.restore_task_snapshot(tasks)
        # Clear the dedup baseline: the restored state is a new starting point,
        # so the next edit's before-hash must not be suppressed by dedup.
        self._last_undo_hash = None
        self.host.append_application_log("已撤销上一次步骤修改。")



    def redo_task_change(self):
        if is_runtime_editing_locked(self.host):
            return
        if not self.redo_stack:
            return
        self.undo_stack.append(self.host.snapshot_tasks())
        tasks = self.redo_stack.pop()
        self.host.restore_task_snapshot(tasks)
        self._last_undo_hash = None
        self.host.append_application_log("已重做上一次步骤修改。")

