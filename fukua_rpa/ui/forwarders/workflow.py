# pyright: reportAttributeAccessIssue=false
"""Workflow/task-list compatibility forwarders extracted from RPAWindow (batch 2).

All methods delegate to ``self.workflow_editor`` (WorkflowEditorController) or
``self.task_list`` (DraggableListWidget).  No logic, no state.
"""


class WorkflowForwarderMixin:
    """Thin forwarders for WorkflowEditorController + task_list widget (35 methods)."""

    # ------------------------------------------------------------------
    # Properties (workflow_editor)
    # ------------------------------------------------------------------

    @property
    def task_clipboard(self):
        return self.workflow_editor.task_clipboard

    @property
    def undo_stack(self):
        return self.workflow_editor.undo_stack

    @property
    def redo_stack(self):
        return self.workflow_editor.redo_stack

    @property
    def restoring_history(self):
        return self.workflow_editor.restoring_history

    # ------------------------------------------------------------------
    # Selection and indexing
    # ------------------------------------------------------------------

    def update_indexes(self):
        return self.workflow_editor.update_indexes()

    def selected_row_index(self):
        return self.workflow_editor.selected_row_index()

    def selected_row_indexes(self):
        return self.workflow_editor.selected_row_indexes()

    def move_selected_task(self, offset):
        return self.workflow_editor.move_selected_task(offset)

    def handle_tasks_reordered(self, moved_step_ids, previous_rows=None):
        return self.workflow_editor.handle_tasks_reordered(
            moved_step_ids, previous_rows=previous_rows
        )

    # ------------------------------------------------------------------
    # Snapshot / undo
    # ------------------------------------------------------------------

    def snapshot_tasks(self):
        return self.workflow_editor.snapshot_tasks()

    def resolve_task_reference_edits(self, row_widget, updated):
        return self.workflow_editor.resolve_task_reference_edits(row_widget, updated)

    def push_undo_state(self):
        return self.workflow_editor.push_undo_state()

    def push_undo_snapshot(self, tasks):
        return self.workflow_editor.push_undo_snapshot(tasks)

    def apply_task_snapshot_changes(self, tasks, source):
        return self.workflow_editor.apply_task_snapshot_changes(tasks, source)

    def restore_task_snapshot(self, tasks):
        return self.workflow_editor.restore_task_snapshot(tasks)

    def restore_row_widget(self, item, data):
        return self.workflow_editor.restore_row_widget(item, data)

    def ensure_task_item_widget(self, item):
        return self.workflow_editor.ensure_task_item_widget(item)

    def update_selection_highlight(self, force=False):
        return self.workflow_editor.update_selection_highlight(force=force)

    # ------------------------------------------------------------------
    # Row operations
    # ------------------------------------------------------------------

    def add_row(
        self,
        data=None,
        index=None,
        record_undo=True,
        select=True,
        materialize=True,
        update_indexes=True,
    ):
        return self.workflow_editor.add_row(
            data,
            index,
            record_undo,
            select,
            materialize,
            update_indexes,
        )

    def add_rows(self, tasks, *, record_undo=True, select_last=True):
        return self.workflow_editor.add_rows(
            tasks,
            record_undo=record_undo,
            select_last=select_last,
        )

    def del_row(self, row_widget):
        return self.workflow_editor.del_row(row_widget)

    def refresh_task_list_delete_hover(self):
        return self.workflow_editor.refresh_task_list_delete_hover()

    def insert_row_before_selected(self):
        return self.workflow_editor.insert_row_before_selected()

    def copy_selected_row(self):
        return self.workflow_editor.copy_selected_row()

    def cut_selected_row(self):
        return self.workflow_editor.cut_selected_row()

    def paste_row_after_selected(self):
        return self.workflow_editor.paste_row_after_selected()

    def undo_task_change(self):
        return self.workflow_editor.undo_task_change()

    def redo_task_change(self):
        return self.workflow_editor.redo_task_change()

    def log_task_edit(self, step_no, before, after, source):
        return self.workflow_editor.log_task_edit(
            step_no, before, after, source
        )

    def toggle_selected_breakpoint(self):
        return self.workflow_editor.toggle_selected_breakpoint()

    # ------------------------------------------------------------------
    # Task-list widget accessors
    # ------------------------------------------------------------------

    def task_list_row_count(self):
        return self.task_list.count()

    def task_list_item(self, index):
        return self.task_list.item(index)

    def task_list_item_widget(self, item):
        return self.task_list.itemWidget(item)

    def task_list_set_current_item(self, item):
        self.task_list.setCurrentItem(item)

    def task_list_scroll_to_item(self, item):
        self.task_list.scrollToItem(item)

    def task_list_schedule_virtual_refresh(self, immediate=False):
        self.task_list.schedule_virtual_refresh(immediate=immediate)

    def task_list_drag_drop_mode(self):
        return self.task_list.dragDropMode()

    def set_task_list_drag_drop_mode(self, mode):
        self.task_list.setDragDropMode(mode)
