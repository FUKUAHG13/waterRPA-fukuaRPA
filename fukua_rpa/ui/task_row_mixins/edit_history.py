# pyright: reportAttributeAccessIssue=false
"""TaskRow 的快照/撤销/外部编辑提交 Mixin（God Class 拆分）。

从 ``TaskRow`` 提取的编辑历史方法组：主窗口快照获取与撤销提交、
编辑审计日志、值/类型输入的历史边界，以及取点等外部编辑的统一提交。
快照与历史游标状态仍由主类 ``__init__`` 初始化，本 Mixin 不持有任何状态。
"""


class TaskRowEditHistoryMixin:
    """快照、撤销提交与外部编辑审计（8 方法）。"""

    def task_snapshot(self):
        main_window = self.window()
        if hasattr(main_window, "snapshot_tasks"):
            return main_window.snapshot_tasks()
        return None

    def commit_task_snapshot(self, snapshot):
        main_window = self.window()
        if snapshot is not None and hasattr(main_window, "push_undo_snapshot"):
            main_window.push_undo_snapshot(snapshot)
            if hasattr(main_window, "mark_profile_dirty"):
                main_window.mark_profile_dirty()

    def log_committed_task_edit(self, snapshot, source):
        if not isinstance(snapshot, list):
            return
        step_no = self.current_step_index()
        index = step_no - 1
        if not 0 <= index < len(snapshot):
            return
        main_window = self.window()
        if hasattr(main_window, "log_task_edit"):
            main_window.log_task_edit(
                step_no,
                snapshot[index],
                self.get_data(),
                source,
            )

    def begin_value_history(self):
        if self._value_edit_snapshot is None:
            self._value_edit_snapshot = self.task_snapshot()
            self._value_edit_original = self.value_input.text()

    def begin_type_history(self):
        if self._type_edit_snapshot is None:
            self._type_edit_snapshot = self.task_snapshot()
            self._type_edit_original = self.type_combo.currentText()

    def finish_value_history(self):
        snapshot = self._value_edit_snapshot
        changed = self.value_input.text() != self._value_edit_original
        self._value_edit_snapshot = None
        if changed:
            self.commit_task_snapshot(snapshot)
            self.log_committed_task_edit(snapshot, "步骤内容")

    def finish_type_history(self, _index=None):
        snapshot = self._type_edit_snapshot
        changed = self.type_combo.currentText() != self._type_edit_original
        self._type_edit_snapshot = None
        if changed:
            self.commit_task_snapshot(snapshot)
            self.log_committed_task_edit(snapshot, "指令类型")

    def commit_external_edit(self, source, mutation):
        """Commit one picker-driven edit as a single history and audit event."""

        snapshot = self.task_snapshot()
        step_no = self.current_step_index()
        index = step_no - 1 if isinstance(step_no, int) else -1
        before = (
            snapshot[index]
            if isinstance(snapshot, list) and 0 <= index < len(snapshot)
            else self.get_data()
        )
        mutation()
        self.sync_data()
        after = self.get_data()
        if before == after:
            return False
        self.commit_task_snapshot(snapshot)
        self.log_committed_task_edit(snapshot, source)
        # FR-20260812-D11-003（残留路径）: 外部编辑不先关对话框时，对话框持有
        # 的陈旧草稿可能在后续 accepted 时覆盖此行新数据。mutation 已改变行数据，
        # 这里主动刷新打开中的设置对话框上下文，使对话框显示与行数据同步。
        dialog = getattr(self, "config_dialog", None)
        if dialog is not None:
            try:
                if getattr(dialog, "isVisible", lambda: False)():
                    controller = getattr(self, "config_dialog_controller", None)
                    if controller is not None and hasattr(controller, "refresh_context"):
                        controller.refresh_context()
            except RuntimeError:
                self.config_dialog = None
        return True
