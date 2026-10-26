# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Grouping and batch task-setting operations for WorkflowEditorController.

Extracted verbatim from workflow_editor.py; methods call each other (and the
rest of the controller) through ``self``, so behavior is unchanged.
"""

from __future__ import annotations

import copy
import json
import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QDialog,
    QInputDialog,
    QToolTip,
)

from ...bulk_task_settings import (
    combined_batch_capabilities,
    common_batch_capabilities,
    filter_bulk_updates,
    mixed_bulk_fields,
)
from ...constants import TASK_TYPE_SECRET_TEXT
from ...log_policy import LOG_PARAMETERS
from ...workflow_document import (
    REFERENCE_FIELDS,
    normalize_workflow_tasks,
)
from ...workflow_metadata import (
    MAX_GROUP_DEPTH,
    new_editor_group,
    normalize_group_path,
    repair_discontiguous_group_paths,
)
from ..lazy_dialogs import load_task_config_dialog
from ..lifecycle import is_runtime_editing_locked
from ..task_settings_binding import dialog_visible_setting_keys

logger = logging.getLogger(__name__)


class WorkflowGroupMixin:
    """Grouping and batch glue."""

    def create_group_from_selection(self):
        if self.host.current_settings_mode() != "advanced":
            return False
        rows = self.selected_row_indexes()
        if not rows:
            return False
        if rows != list(range(rows[0], rows[-1] + 1)):
            QToolTip.showText(
                QCursor.pos(),
                "分组只支持一段连续步骤，请重新选择相邻步骤。",
                self.host,
            )
            return False
        before = self.host.snapshot_tasks()
        paths = [
            normalize_group_path(before[row].get("editor_groups", []))
            for row in rows
        ]
        same_path = all(path == paths[0] for path in paths)
        max_depth = max(len(path) for path in paths)
        if same_path:
            if len(paths[0]) >= MAX_GROUP_DEPTH:
                QToolTip.showText(
                    QCursor.pos(), "分组最多支持三级。", self.host
                )
                return False
        else:
            # Auto outer group (issue #7): wrap the selection, including any
            # nested groups, in a new top-level group, demoting existing groups
            # by one level. Still capped at MAX_GROUP_DEPTH (no infinite nesting).
            if max_depth + 1 > MAX_GROUP_DEPTH:
                QToolTip.showText(
                    QCursor.pos(),
                    "所选步骤已达到三级分组，无法再向外建立新分组。",
                    self.host,
                )
                return False
        prompt_title = "新建步骤分组"
        prompt_label = (
            f"分组名称（第 {max_depth + 1} 级）："
            if same_path
            else "外层分组名称（将包裹所选步骤及其原有分组）："
        )
        name, accepted = QInputDialog.getText(
            self.host, prompt_title, prompt_label
        )
        if not accepted or not str(name).strip():
            return False
        group = new_editor_group(name)
        after = copy.deepcopy(before)
        for row, path in zip(rows, paths, strict=False):
            if same_path:
                after[row]["editor_groups"] = [*path, copy.deepcopy(group)]
            else:
                after[row]["editor_groups"] = [copy.deepcopy(group), *path]
        selected_ids = [before[row].get("step_id", "") for row in rows]
        return self._restore_structured_tasks(
            before,
            after,
            selected_ids,
            f"已将第 {rows[0] + 1} 至 {rows[-1] + 1} 步建立为分组。",
            private_message=(
                f"已将第 {rows[0] + 1} 至 {rows[-1] + 1} 步建立为"
                f"分组“{group['name']}”。"
            ),
            audit_source="步骤分组",
        )



    def _group_name(self, tasks, group_id):
        for task in tasks:
            for group in normalize_group_path(task.get("editor_groups", [])):
                if group["id"] == group_id:
                    return group.get("name", "")
        return ""



    def add_selection_to_group(self, group_id):
        """Add the current selection into an existing group (issue #5).

        Strict rule: the union of the selection and the target group must stay
        a single contiguous block. Otherwise the whole operation is rejected
        with a hint about which steps are not adjacent.
        """

        if self.host.current_settings_mode() != "advanced":
            return False
        group_id = str(group_id or "")
        if not group_id:
            return False
        rows = self.selected_row_indexes()
        if not rows:
            return False
        before = self.host.snapshot_tasks()
        group_span = [
            row
            for row in range(len(before))
            if group_id
            in {
                g["id"]
                for g in normalize_group_path(
                    before[row].get("editor_groups", [])
                )
            }
        ]
        if not group_span:
            return False
        union = sorted(set(rows) | set(group_span))
        if union != list(range(union[0], union[-1] + 1)):
            non_adjacent = [
                row + 1
                for row in rows
                if row < group_span[0] or row > group_span[-1]
            ]
            QToolTip.showText(
                QCursor.pos(),
                "所选步骤必须与分组“%s”相邻才能加入；以下步骤不相邻：第 %s 步。"
                % (
                    self._group_name(before, group_id),
                    "、".join(str(value) for value in non_adjacent),
                ),
                self.host,
            )
            return False
        target_path = []
        for row in group_span:
            path = normalize_group_path(before[row].get("editor_groups", []))
            for index, group in enumerate(path):
                if group["id"] == group_id:
                    target_path = path[: index + 1]
                    break
            if target_path:
                break
        after = copy.deepcopy(before)
        changed = False
        for row in rows:
            path = normalize_group_path(after[row].get("editor_groups", []))
            if group_id in {g["id"] for g in path}:
                continue
            new_path = [*target_path, *path]
            if len(new_path) > MAX_GROUP_DEPTH:
                QToolTip.showText(
                    QCursor.pos(),
                    "加入后分组将超过三级，无法加入。",
                    self.host,
                )
                return False
            after[row]["editor_groups"] = new_path
            changed = True
        if not changed:
            return False
        selected_ids = [before[row].get("step_id", "") for row in rows]
        return self._restore_structured_tasks(
            before,
            after,
            selected_ids,
            f"已将所选步骤加入分组“{self._group_name(before, group_id)}”。",
            audit_source="加入分组",
        )



    def _selected_group_id(self):
        rows = self.selected_row_indexes()
        if not rows:
            return ""
        paths = [
            normalize_group_path((self._task_data_at(row) or {}).get("editor_groups", []))
            for row in rows
        ]
        if not paths or not paths[0]:
            return ""
        group_id = paths[0][-1]["id"]
        return group_id if all(any(group["id"] == group_id for group in path) for path in paths) else ""



    def rename_group_by_id(self, group_id):
        """Prompt for a new name and rename the group everywhere it occurs."""

        group_id = str(group_id or "")
        if not group_id:
            return False
        before = self.host.snapshot_tasks()
        current_name = ""
        for task in before:
            for group in normalize_group_path(task.get("editor_groups", [])):
                if group["id"] == group_id:
                    current_name = group["name"]
                    break
            if current_name:
                break
        name, accepted = QInputDialog.getText(
            self.host, "重命名步骤分组", "新的分组名称：", text=current_name
        )
        if not accepted or not str(name).strip():
            return False
        name = str(name).replace("\x00", "").strip()[:100]
        after = copy.deepcopy(before)
        selected_ids = []
        for task in after:
            path = normalize_group_path(task.get("editor_groups", []))
            changed = False
            for group in path:
                if group["id"] == group_id:
                    group["name"] = name
                    changed = True
            if changed:
                task["editor_groups"] = path
                selected_ids.append(task.get("step_id", ""))
        return self._restore_structured_tasks(
            before,
            after,
            selected_ids,
            "步骤分组已重命名。",
            private_message=f"步骤分组已从“{current_name}”重命名为“{name}”。",
            audit_source="分组重命名",
        )



    def rename_selected_group(self):
        return self.rename_group_by_id(self._selected_group_id())



    def ungroup_selected_steps(self):
        if self.host.current_settings_mode() != "advanced":
            return False
        rows = self.selected_row_indexes()
        before = self.host.snapshot_tasks()
        after = copy.deepcopy(before)
        changed = False
        for row in rows:
            path = normalize_group_path(after[row].get("editor_groups", []))
            if path:
                after[row]["editor_groups"] = path[:-1]
                changed = True
        if not changed:
            return False
        after, split_count = repair_discontiguous_group_paths(after)
        selected_ids = [before[row].get("step_id", "") for row in rows]
        suffix = (
            f"；为保持分组连续，另拆分了 {split_count} 个断开片段"
            if split_count
            else ""
        )
        return self._restore_structured_tasks(
            before,
            after,
            selected_ids,
            f"已取消 {len(rows)} 个步骤的最内层分组{suffix}。",
            audit_source="取消分组",
        )



    def toggle_group_collapsed(self, group_id):
        group_id = str(group_id or "")
        if not group_id:
            return False
        before = self.host.snapshot_tasks()
        current = None
        for task in before:
            for group in normalize_group_path(task.get("editor_groups", [])):
                if group["id"] == group_id:
                    current = bool(group["collapsed"])
                    break
            if current is not None:
                break
        if current is None:
            return False
        after = copy.deepcopy(before)
        changed_rows = []
        for index, task in enumerate(after):
            path = normalize_group_path(task.get("editor_groups", []))
            changed = False
            for group in path:
                if group["id"] == group_id:
                    group["collapsed"] = not current
                    changed = True
            if changed:
                task["editor_groups"] = path
                changed_rows.append(index)
        # Apply in place: mutate each row's editor_groups without clearing and
        # rebuilding the whole list, then let refresh_editor_visibility recompute
        # hide/show and group-header widgets (it updates rows, it does not
        # recreate them). This keeps collapse cheap for large workflows.
        self.host.push_undo_state()
        task_list = self.host.task_list
        for row in range(task_list.count()):
            item = task_list.item(row)
            widget = task_list.itemWidget(item)
            normalized_path = normalize_group_path(
                after[row].get("editor_groups", [])
            )
            if widget is not None:
                widget.custom_data["editor_groups"] = normalized_path
                widget.sync_data()
            else:
                canonical = dict(item.data(Qt.UserRole) or {})
                canonical["editor_groups"] = normalized_path
                item.setData(Qt.UserRole, canonical)
        self.refresh_editor_visibility()
        # Mirror _restore_structured_tasks' selection restore: keep the group's
        # first member selected after a lightweight in-place collapse so callers
        # that rely on the current selection (e.g. rename_selected_group) keep
        # working. The original full-rebuild path did the same via selected_ids.
        if changed_rows:
            anchor_item = task_list.item(changed_rows[0])
            if anchor_item is not None:
                task_list.select_item_with_modifiers(anchor_item, Qt.NoModifier)
        self.host.append_application_log(
            "步骤分组已折叠。" if not current else "步骤分组已展开。"
        )
        # Mirror _restore_structured_tasks' audit trail (minus the full rebuild)
        # so the collapse/expand is recorded against the changed steps.
        before_by_id = {
            str(task.get("step_id", "")): (index, task)
            for index, task in enumerate(before)
        }
        for index, task in enumerate(after):
            step_id = str(task.get("step_id", ""))
            previous = before_by_id.get(step_id)
            if previous is None or previous[1] == task:
                continue
            self.log_task_edit(index + 1, previous[1], task, "分组折叠状态")
        return True



    def _group_path_to_inherit(self, paths, row):
        """Return the group path a moved step at ``row`` should join.

        A step dropped between (or adjacent to) steps that belong to the same
        group should fold into that existing group rather than staying outside
        and forcing ``repair_discontiguous_group_paths`` to split the group
        into two same-named segments.
        """

        prev_path = paths[row - 1] if row - 1 >= 0 else None
        next_path = paths[row + 1] if row + 1 < len(paths) else None
        grouped = [path for path in (prev_path, next_path) if path]
        if not grouped:
            return None
        if len(grouped) == 1:
            return list(grouped[0])
        # Both neighbours are grouped: only fold into their shared prefix.
        common = []
        for left, right in zip(grouped[0], grouped[1], strict=False):
            if left.get("id") == right.get("id") and left.get("name") == right.get(
                "name"
            ):
                common.append(left)
            else:
                break
        return common if common else None



    def log_task_edit(self, step_no, before, after, source):
        before = before if isinstance(before, dict) else {}
        after = after if isinstance(after, dict) else {}
        changed = sorted(
            key
            for key in set(before) | set(after)
            if before.get(key) != after.get(key)
        )
        if not changed:
            return False
        labels = {
            "type": "指令类型",
            "value": "步骤内容",
            "step_disabled": "暂时禁用",
            "step_note": "备注",
            "marker_color": "颜色标记",
            "editor_hidden": "编辑器隐藏",
            "editor_groups": "步骤分组",
            "uia_binding": "UIA 控件绑定",
            "repeat_mode": "本步骤重复",
            "repeat_count": "重复次数",
            "debug_breakpoint": "步骤前暂停",
            "coord_sequence_points": "自定义点位",
            "coord_step_point": "坐标步进目标点",
            "coord_step_manual_points": "坐标步进手动修正",
        }
        field_names = "、".join(labels.get(key, key) for key in changed)
        public_message = (
            f"第 {int(step_no)} 步已保存{source}修改；变更字段：{field_names}。"
        )
        try:
            before_type = float(before.get("type", 0))
        except (TypeError, ValueError):
            before_type = 0
        try:
            after_type = float(after.get("type", before_type))
        except (TypeError, ValueError):
            after_type = before_type
        secret_value = TASK_TYPE_SECRET_TEXT in (before_type, after_type)
        private_parts = []
        for key in changed:
            if key == "value" and secret_value:
                detail = "<秘密文本引用已隐藏>"
            else:
                old_text = json.dumps(before.get(key), ensure_ascii=False)
                new_text = json.dumps(after.get(key), ensure_ascii=False)
                detail = f"{old_text[:160]} -> {new_text[:160]}"
            private_parts.append(f"{labels.get(key, key)}: {detail}")
        private_message = public_message + " " + "；".join(private_parts)
        return self.host.append_application_log(
            public_message,
            LOG_PARAMETERS,
            private_msg=private_message,
        )



    def open_batch_task_settings(self):
        rows = self.selected_row_indexes()
        if not rows or is_runtime_editing_locked(self.host):
            return False
        # 与单行小齿轮一致：对话框按需导入，失败只记日志并中止本次操作，
        # 不修改方案快照（ADR 0040）。
        try:
            TaskConfigDialog = load_task_config_dialog()
        except Exception as error:  # noqa: BLE001 - 导入失败要如实记录并中止
            logger.critical("步骤设置对话框模块导入失败：%s", error)
            return False
        before = self.host.snapshot_tasks()
        selected_tasks = [before[row] for row in rows]
        advanced = self.host.current_settings_mode() == "advanced"
        if advanced:
            setting_capabilities, control_capabilities = (
                combined_batch_capabilities(selected_tasks)
            )
        else:
            setting_capabilities, control_capabilities = common_batch_capabilities(
                selected_tasks
            )
        baseline = copy.deepcopy(selected_tasks[0])
        dialog = TaskConfigDialog(
            self.host,
            baseline,
            image_settings_available=True,
            point_limit_available=True,
            coordinate_step_available=True,
            base_coordinate=None,
            image_path="",
            image_click_point_available=True,
            base_coordinate_changed=None,
            step_index=None,
            step_type="",
            base_dir=getattr(self.host, "base_dir", None),
            settings_mode="advanced" if advanced else "simple",
            screenshot_settings_available=True,
            step_value="",
            settings_capabilities_override=setting_capabilities,
            control_capabilities_override=control_capabilities,
            batch_mode=True,
            batch_count=len(rows),
        )
        if hasattr(self.host, "apply_ui_scale_to_widget"):
            self.host.apply_ui_scale_to_widget(dialog)
        initial = dialog.get_data()
        mixed = mixed_bulk_fields(selected_tasks, tuple(initial))
        # FR-20261006-008: "统一未改字段" may only reach fields the user can
        # currently see and edit. ``initial`` covers *every* field the dialog
        # collects, including ones hidden in simple mode — bulk-applying those
        # overwrites values the user never saw with the first selected step's
        # values. Narrowing happens here, before the count is reported, so the
        # note tells the truth about what will actually be written.
        visible_keys = dialog_visible_setting_keys(dialog, tuple(initial))
        hidden_mixed = mixed - visible_keys
        mixed = mixed & visible_keys
        if mixed or hidden_mixed:
            note = dialog.context_note.text()
            if mixed:
                note += f" 当前选择中有 {len(mixed)} 个设置项的原值不同。"
            if hidden_mixed:
                note += (
                    f" 另有 {len(hidden_mixed)} 个设置项在当前模式下不可见，"
                    "勾选统一后仍保持各步骤原值。"
                )
            dialog.context_note.setText(note)
        try:
            if dialog.exec() != QDialog.Accepted:
                return False
            final = dialog.get_data()
            # FR-20260809-1002: 单步清空"点击偏移/移动曲线/按键按住时长"会移除键使
            # 引擎回退全局；批量路径必须语义一致——被清空的字段（initial 有、final
            # 无）以哨兵标记，应用时从目标步骤移除键，否则步骤级覆盖旧值残留。
            _BULK_CLEAR_SENTINEL = object()
            changed_fields = {
                field for field in final if final.get(field) != initial.get(field)
            }
            cleared_fields = {
                field
                for field in initial
                if field not in final
                and initial[field] not in (None, "", False)
            }
            changed_fields.update(cleared_fields)
            if dialog.batch_apply_mixed_chk is not None and dialog.batch_apply_mixed_chk.isChecked():
                changed_fields.update(mixed)
            updates: dict[str, object] = {}
            for field in changed_fields:
                if field in final:
                    updates[field] = copy.deepcopy(final[field])
                else:
                    updates[field] = _BULK_CLEAR_SENTINEL
            if not updates:
                QToolTip.showText(QCursor.pos(), "没有需要批量应用的修改。", self.host)
                return False

            after = copy.deepcopy(before)
            applied_steps = 0
            for row in rows:
                applicable = filter_bulk_updates(after[row], updates)
                if not applicable:
                    continue
                updated = copy.deepcopy(after[row])
                for field, value in applicable.items():
                    if value is _BULK_CLEAR_SENTINEL:
                        updated.pop(field, None)
                    else:
                        updated[field] = copy.deepcopy(value)
                for jump_field, target_field, _label in REFERENCE_FIELDS:
                    if jump_field in applicable:
                        updated[target_field] = ""
                after[row] = updated
                applied_steps += 1
            after = normalize_workflow_tasks(after)
            if not applied_steps or after == before:
                QToolTip.showText(
                    QCursor.pos(), "所改项目不适用于当前选中的步骤。", self.host
                )
                return False
            selected_ids = [before[row].get("step_id", "") for row in rows]
            return self._restore_structured_tasks(
                before,
                after,
                selected_ids,
                f"已批量修改 {applied_steps} 个步骤，共应用 {len(updates)} 个设置项。",
                audit_source="批量设置",
            )
        finally:
            dialog.deleteLater()
