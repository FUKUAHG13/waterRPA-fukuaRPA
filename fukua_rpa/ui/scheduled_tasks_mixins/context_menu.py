# pyright: reportAttributeAccessIssue=false
"""Context-menu Mixin for ScheduledTasksWindow (God Class split phase 1).

Groups the table right-click menu and its follow-up actions. Delegates to
``self._service`` / ``self._controller`` and calls sibling methods provided by
the window or other Mixins (resolved via MRO). No state of its own.
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from ._timefmt import _fmt_local


class ContextMenuMixin:
    """Table context menu + delete-confirm + future-runs preview (3 methods)."""

    def _handle_run_choice(self, task) -> None:
        """FR-20260812 建议-体验-4: "立即运行"分支——目标方案缺失（空
        profile_id）时明确提示，不再静默无反应。抽成独立方法便于测试。
        """
        if task.profile_id:
            self._on_try_run(task.profile_id)
        else:
            QMessageBox.information(
                self,
                "无法运行",
                "该计划未关联有效方案（profile_id 为空），无法立即运行。\n"
                "请在编辑计划时重新选择目标方案。",
            )

    def _on_table_context_menu(self, position) -> None:
        table = self._table()
        index = table.indexAt(position)
        if not index.isValid():
            return
        selection = table.selectionModel()
        if not selection.isRowSelected(index.row(), QModelIndex()):
            table.clearSelection()
            table.selectRow(index.row())
        sid = self._controller.model().schedule_id_at(index)
        if not sid:
            return
        row = self._find_row_by_id(sid)
        if row is None:
            return
        task = row["task"]
        selected_ids = self._selected_schedule_ids() or [sid]
        menu = QMenu(self)
        act_log = menu.addAction("查看日志")
        act_run = menu.addAction("立即运行")
        act_edit = menu.addAction("编辑")
        act_dup = menu.addAction("复制计划")
        menu.addSeparator()
        act_enable = menu.addAction("启用所选")
        act_disable = menu.addAction("停用所选")
        act_export = menu.addAction("导出所选")
        more = menu.addMenu("更多")
        act_copy_id = more.addAction("复制计划 ID")
        act_future = more.addAction("查看未来五次")
        menu.addSeparator()
        act_del = menu.addAction(
            "删除所选计划" if len(selected_ids) > 1 else "删除计划"
        )

        choice = menu.exec(table.viewport().mapToGlobal(position))
        if choice is None:
            return
        # DESIGN (UIC15): 运行中编辑锁 —— 复制/删除/启停会改写计划数据，锁定态下
        # 统一拦截；查看/运行/导出/复制 ID/未来预览为只读操作，不受锁约束。
        if choice in (act_edit, act_dup, act_enable, act_disable, act_del) \
                and self._deny_while_locked():
            return
        if choice is act_log:
            self._open_log_window(sid)
        elif choice is act_run:
            self._handle_run_choice(task)
        elif choice is act_edit:
            self._open_editor(sid)
        elif choice is act_dup:
            self._service.duplicate_task(sid)
        elif choice is act_enable:
            # FR-20260812 建议-架构-5: 多选批量启用用批量方法（一次落盘+单次通知）。
            self._service.set_enabled_many(selected_ids, True)
        elif choice is act_disable:
            # FR-20260812 建议-架构-5: 多选批量停用用批量方法（一次落盘+单次通知）。
            self._service.set_enabled_many(selected_ids, False)
        elif choice is act_export:
            self._on_export()
        elif choice is act_del:
            self._confirm_delete_many(selected_ids)
        elif choice is act_copy_id:
            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(sid)
        elif choice is act_future:
            self._show_future(task)

    def _confirm_delete_many(self, schedule_ids: list[str]) -> None:
        rows = [self._find_row_by_id(sid) for sid in schedule_ids]
        tasks = [row["task"] for row in rows if row is not None]
        if not tasks:
            return
        if len(tasks) == 1:
            prompt = f"确定删除计划“{tasks[0].name}”吗？此操作不可撤销。"
        else:
            prompt = f"确定删除所选的 {len(tasks)} 个计划吗？此操作不可撤销。"
        answer = QMessageBox.question(
            self,
            "删除计划",
            prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            # FR-20260812 建议-架构-5: 批量删除用批量方法（一次落盘+单次通知）。
            self._service.remove_many([task.schedule_id for task in tasks])

    def _show_future(self, task) -> None:
        runs = self._service.future_runs(task, count=5)
        if not runs:
            text = "未来无可计算的触发时间（请检查触发规则）。"
        else:
            text = "\n".join(f"{i + 1}. {_fmt_local(r)}" for i, r in enumerate(runs))
        box = QMessageBox(self)
        box.setWindowTitle(f"未来五次 · {task.name}")
        box.setText(text)
        box.exec()
