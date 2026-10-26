# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Step reordering and inbound-reference maintenance for WorkflowEditorController."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from ...workflow_document import (
    REFERENCE_FIELDS,
    clear_references_to_steps,
    references_to_step,
)
from ...workflow_metadata import (
    normalize_group_path,
    repair_discontiguous_group_paths,
)
from ..lifecycle import is_runtime_editing_locked


class StepReorderReferenceMixin:
    """Move a step and keep its group contiguity and inbound jumps consistent."""

    def move_selected_task(self, offset):
        if is_runtime_editing_locked(self.host):
            return False
        current = self.selected_row_index()
        if current is None:
            return False
        target = current + (-1 if int(offset) < 0 else 1)
        if target < 0 or target >= self.host.task_list.count():
            return False
        current_data = self._task_data_at(current) or {}
        moving_step_id = str(current_data.get("step_id", ""))
        # Feature 1(c): when 调整跟随 is OFF, moving a referenced step clears
        # its inbound jumps (legacy behaviour, consistent with delete-off).
        if not self._step_adjust_follow_enabled():
            refs = [
                ref
                for ref in references_to_step(self.host.snapshot_tasks(), moving_step_id)
                if ref.source_id != moving_step_id
            ]
            if refs and not self._confirm_clear_references_on_move(refs):
                return False
        self.host.push_undo_state()
        # SAFETY (FR-20260809-1101): close the moving row's config dialog and
        # detach its widget before takeItem, mirroring the delete path.  A
        # NonModal dialog left open would otherwise bind to the orphaned row
        # after takeItem unbinds item<->widget (see close_and_detach_row_widget).
        self.host.task_list.close_and_detach_row_widget(current)
        item = self.host.task_list.takeItem(current)
        self.host.task_list.insertItem(target, item)
        self.host.task_list.setCurrentItem(item)
        self.handle_tasks_reordered([moving_step_id], previous_rows=[current])
        if not self._step_adjust_follow_enabled() and moving_step_id:
            self._clear_inbound_references([moving_step_id])
        self.host.task_list.scrollToItem(item)
        self.host.task_list.schedule_virtual_refresh(immediate=True)
        return True

    def _confirm_clear_references_on_move(self, references) -> bool:
        preview = "\n".join(
            f"第 {ref.source_index} 步：{ref.label}" for ref in references[:12]
        )
        if len(references) > 12:
            preview += f"\n……另有 {len(references) - 12} 处引用"
        answer = QMessageBox.question(
            self.host,
            "移动被引用步骤",
            "其他步骤仍然跳转到将移动的步骤：\n\n"
            f"{preview}\n\n“调整跟随”已关闭，移动后这些跳转会被清空并改为顺序执行。"
            "是否继续？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return answer == QMessageBox.Yes

    def _clear_inbound_references(self, target_ids):
        """Clear inbound jumps to the given steps without removing them."""
        tasks = self.host.snapshot_tasks()
        repaired = clear_references_to_steps(tasks, target_ids)
        repaired_by_id = {
            str(task.get("step_id", "")): task for task in repaired
        }
        for index in range(self.host.task_list.count()):
            item = self.host.task_list.item(index)
            widget = self.host.task_list.itemWidget(item)
            data = widget.get_data() if widget else item.data(Qt.UserRole)
            if not isinstance(data, dict):
                continue
            step_id = str(data.get("step_id", ""))
            repaired_data = repaired_by_id.get(step_id)
            if repaired_data is None:
                continue
            if widget is not None:
                for jump_field, target_field, _label in REFERENCE_FIELDS:
                    widget.custom_data[jump_field] = repaired_data[jump_field]
                    widget.custom_data[target_field] = repaired_data[target_field]
            else:
                new_data = dict(data)
                for jump_field, target_field, _label in REFERENCE_FIELDS:
                    new_data[jump_field] = repaired_data[jump_field]
                    new_data[target_field] = repaired_data[target_field]
                item.setData(Qt.UserRole, new_data)
        self.host.update_indexes()

    def handle_tasks_reordered(self, moved_step_ids, previous_rows=None):
        """Keep each group contiguous after a partial drag leaves its range."""

        moved = {str(value) for value in moved_step_ids if str(value)}
        tasks = self.host.snapshot_tasks()
        paths = [normalize_group_path(task.get("editor_groups", [])) for task in tasks]
        # Fold a moved step into an adjacent group so dropping an ungrouped
        # step "into" a group joins it instead of duplicating the group name.
        if moved:
            for row in range(len(tasks)):
                if str(tasks[row].get("step_id", "")) not in moved:
                    continue
                inherit = self._group_path_to_inherit(paths, row)
                if not inherit:
                    continue
                if normalize_group_path(inherit) == normalize_group_path(
                    tasks[row].get("editor_groups", [])
                ):
                    continue
                tasks[row]["editor_groups"] = [dict(group) for group in inherit]
                item = self.host.task_list.item(row)
                widget = self.host.task_list.itemWidget(item) if item else None
                if widget is not None:
                    widget.custom_data["editor_groups"] = [
                        dict(group) for group in inherit
                    ]
                    widget.sync_data()
                elif item is not None:
                    data = dict(item.data(Qt.UserRole) or {})
                    data["editor_groups"] = [dict(group) for group in inherit]
                    item.setData(Qt.UserRole, data)
        changed = 0
        for row, task in enumerate(tasks):
            if str(task.get("step_id", "")) not in moved:
                continue
            path = paths[row]
            keep_depth = len(path)
            for depth, _group in enumerate(path):
                prefix = tuple(item["id"] for item in path[: depth + 1])
                positions = []
                for candidate_row, candidate_path in enumerate(paths):
                    candidate_prefix = tuple(
                        item["id"] for item in candidate_path[: depth + 1]
                    )
                    if len(candidate_path) > depth and candidate_prefix == prefix:
                        positions.append(candidate_row)
                if positions and positions != list(
                    range(positions[0], positions[-1] + 1)
                ):
                    keep_depth = depth
                    break
            if keep_depth == len(path):
                continue
            detached = path[:keep_depth]
            tasks[row]["editor_groups"] = detached
            item = self.host.task_list.item(row)
            widget = self.host.task_list.itemWidget(item)
            if widget is not None:
                widget.custom_data["editor_groups"] = detached
                widget.sync_data()
            else:
                data = dict(item.data(Qt.UserRole) or {})
                data["editor_groups"] = detached
                item.setData(Qt.UserRole, data)
            changed += 1

        repaired_tasks, split_count = repair_discontiguous_group_paths(tasks)
        for row, repaired_task in enumerate(repaired_tasks):
            desired_path = normalize_group_path(
                repaired_task.get("editor_groups", [])
            )
            current_data = self._task_data_at(row) or {}
            if normalize_group_path(current_data.get("editor_groups", [])) == desired_path:
                continue
            item = self.host.task_list.item(row)
            widget = self.host.task_list.itemWidget(item)
            if widget is not None:
                widget.custom_data["editor_groups"] = desired_path
                widget.sync_data()
            else:
                data = dict(item.data(Qt.UserRole) or {})
                data["editor_groups"] = desired_path
                item.setData(Qt.UserRole, data)
        self.host.update_indexes()
        moved_rows = [
            row
            for row, task in enumerate(self.host.snapshot_tasks())
            if str(task.get("step_id", "")) in moved
        ]
        if moved_rows:
            old_text = ""
            if previous_rows:
                old_numbers = [int(row) + 1 for row in previous_rows]
                old_text = f"原位置 {self._format_step_numbers(old_numbers)}，"
            new_numbers = [row + 1 for row in moved_rows]
            self.host.append_application_log(
                f"步骤顺序已调整：{old_text}现位于 {self._format_step_numbers(new_numbers)}。"
            )
        if changed:
            self.host.append_application_log(
                f"拖动后有 {changed} 个步骤离开原分组范围，已自动取消其断开的分组层级。"
            )
        if split_count:
            self.host.append_application_log(
                f"拖动后检测到 {split_count} 个不相邻分组片段，已拆分为独立分组。"
            )
        return changed + split_count

    @staticmethod
    def _format_step_numbers(numbers):
        values = sorted({int(value) for value in numbers})
        if not values:
            return "未知位置"
        if len(values) == 1:
            return f"第 {values[0]} 步"
        if values == list(range(values[0], values[-1] + 1)):
            return f"第 {values[0]} 至 {values[-1]} 步"
        return "第 " + "、".join(str(value) for value in values) + " 步"

    def _step_adjust_follow_enabled(self) -> bool:
        chk = getattr(self.host, "step_adjust_follow_chk", None)
        if chk is not None:
            return bool(chk.isChecked())
        cfg = self.host.profiles_data.get(self.host.current_profile_name, {})
        return bool(cfg.get("step_adjust_follow", True))

    def _confirm_remove_references(self, tasks, target_id, follow=None):
        if follow is None:
            follow = self._step_adjust_follow_enabled()
        target_ids = (
            {str(value) for value in target_id}
            if isinstance(target_id, (set, list, tuple))
            else {str(target_id)}
        )
        references = []
        for candidate in target_ids:
            references.extend(
                reference
                for reference in references_to_step(tasks, candidate)
                if reference.source_id not in target_ids
            )
        if not references:
            return True
        preview = "\n".join(
            f"第 {reference.source_index} 步：{reference.label}"
            for reference in references[:12]
        )
        if len(references) > 12:
            preview += f"\n……另有 {len(references) - 12} 处引用"
        if follow:
            message = (
                "其他步骤仍然跳转到将删除的步骤：\n\n"
                f"{preview}\n\n开启“调整跟随”后，这些跳转会自动改指到接替该步骤的"
                "下一个步骤；是否继续？"
            )
        else:
            message = (
                "其他步骤仍然跳转到将删除的步骤：\n\n"
                f"{preview}\n\n删除后，这些跳转会被关闭并改为顺序执行。是否继续？"
            )
        answer = QMessageBox.question(
            self.host,
            "删除被引用步骤",
            message,
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return answer == QMessageBox.Yes



