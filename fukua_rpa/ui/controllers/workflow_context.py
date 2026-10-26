# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Context menu, visibility, selection-highlight properties and breakpoints for WorkflowEditorController.

Extracted verbatim from workflow_editor.py; methods call each other (and the
rest of the controller) through ``self``, so behavior is unchanged.
"""

from __future__ import annotations

import copy
import logging
import os
from typing import TYPE_CHECKING

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtGui import QColor, QCursor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QLabel,
    QMenu,
    QStyle,
    QToolTip,
    QWidgetAction,
)

from ...log_policy import LOG_PARAMETERS
from ...paths import get_resource_path
from ...workflow_document import (
    REFERENCE_FIELDS,
    normalize_workflow_tasks,
)
from ...workflow_metadata import (
    STEP_MARKER_COLORS,
    build_group_presentations,
    normalize_editor_metadata,
    normalize_group_path,
    normalize_marker_color,
    repair_discontiguous_group_paths,
)
from ...workflow_summary import task_drag_summary
from ..lifecycle import is_runtime_editing_locked

if TYPE_CHECKING:
    from .host_view import HostView

logger = logging.getLogger(__name__)


class WorkflowContextMixin:
    """Context-menu, visibility and breakpoint glue."""

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("工作流编辑窗口已销毁")
        return host



    def update_indexes(self):
        entries = []
        for i in range(self.host.task_list.count()):
            item = self.host.task_list.item(i)
            widget = self.host.task_list.itemWidget(item)
            entries.append((item, widget))

        normalized = normalize_workflow_tasks(
            [
                widget.get_data() if widget else item.data(Qt.UserRole)
                for item, widget in entries
            ]
        )
        normalized, _split_count = repair_discontiguous_group_paths(normalized)
        reference_keys = {"step_id"}
        for jump_field, target_field, _label in REFERENCE_FIELDS:
            reference_keys.update((jump_field, target_field))
        maximum_index = len(entries)
        for index, ((item, widget), normalized_data) in enumerate(
            zip(entries, normalized, strict=False)
        ):
            if widget:
                widget.custom_data.update(
                    {
                        key: normalized_data.get(key, "")
                        for key in reference_keys
                    }
                )
                widget.custom_data["editor_groups"] = copy.deepcopy(
                    normalized_data.get("editor_groups", [])
                )
            if widget and hasattr(widget, 'set_index'):
                widget.set_index(index + 1, maximum_index)
                final_data = widget.get_data()
            else:
                final_data = normalized_data
                item.setSizeHint(self.host.task_list.virtual_row_size_hint())
            item.setData(Qt.UserRole, final_data)
            item.setData(
                Qt.UserRole + 1,
                (
                    widget.drag_summary()
                    if widget and hasattr(widget, "drag_summary")
                    else task_drag_summary(
                        final_data,
                        index + 1,
                        self.host.current_settings_mode(),
                    )
                ),
            )
            item.setText("")
        self.host.update_selection_highlight(force=True)
        self.refresh_editor_visibility()
        self.host.task_list.schedule_virtual_refresh()
        if getattr(self, "_task_search_query", ""):
            self.refresh_task_search(preserve_current=True)
        mark_dirty = getattr(self.host, "mark_profile_dirty", None)
        if callable(mark_dirty):
            mark_dirty()



    def selected_row_index(self):
        row = self.host.task_list.currentRow()
        return row if row >= 0 else None



    def selected_row_indexes(self):
        rows = self.host.task_list.selected_row_indexes()
        if rows:
            return rows
        current = self.selected_row_index()
        return [] if current is None else [current]



    def refresh_editor_visibility(self):
        """Apply editor-only hiding without removing tasks from runtime snapshots.

        DESIGN: hidden and collapsed rows still participate in numbering,
        export and execution. Only ``step_disabled`` changes run membership.
        """

        task_list = getattr(self.host, "task_list", None)
        if task_list is None:
            return 0
        advanced = self.host.current_settings_mode() == "advanced"
        reveal = advanced and self.show_hidden_steps
        tasks = [self._task_data_at(row) or {} for row in range(task_list.count())]
        presentations = build_group_presentations(tasks)
        hidden_count = 0
        collapsed_group_ids = set()
        for row in range(task_list.count()):
            item = task_list.item(row)
            widget = task_list.itemWidget(item)
            data = tasks[row]
            metadata = normalize_editor_metadata(data or {})
            presentation = presentations[row]
            editor_hidden = metadata["editor_hidden"]
            hidden_count += int(editor_hidden)
            collapsed_group_ids.update(
                header["id"]
                for header in presentation["headers"]
                if header["collapsed"]
            )
            item.setData(Qt.UserRole + 2, presentation)
            item.setHidden(
                (
                    editor_hidden
                    and not reveal
                    and not presentation["collapsed_header_anchor"]
                )
                or bool(presentation["hide_for_collapse"])
            )
            if widget is not None:
                widget.custom_data.update(metadata)
                widget.apply_editor_metadata()
                widget.set_note_display_visible(self.show_step_notes)
                widget.set_hidden_reveal_faded(editor_hidden and reveal)
                widget.set_group_presentation(presentation)
            else:
                canonical = dict(data)
                canonical.update(metadata)
                item.setData(Qt.UserRole, canonical)
                item.setSizeHint(task_list.group_row_size_hint(presentation))
        label = getattr(self.host, "task_count_label", None)
        if label is not None:
            details = []
            if hidden_count:
                details.append(f"隐藏 {hidden_count}")
            if collapsed_group_ids:
                details.append(f"折叠 {len(collapsed_group_ids)} 组")
            suffix = " · " + " · ".join(details) if details else ""
            label.setText(f"{task_list.count()} 步{suffix}")
            label.setToolTip(
                "隐藏只影响主界面显示，运行、导出和步骤编号仍包含这些步骤。"
                if details
                else ""
            )
        current = task_list.currentItem()
        if current is not None and current.isHidden():
            current.setSelected(False)
            for row in range(task_list.count()):
                candidate = task_list.item(row)
                if not candidate.isHidden():
                    task_list.setCurrentItem(candidate)
                    break
        task_list.schedule_virtual_refresh()
        return hidden_count



    def set_show_hidden_steps(self, enabled):
        enabled = bool(enabled)
        changed = enabled != self.show_hidden_steps
        self.show_hidden_steps = enabled
        self.host.settings.setValue("show_hidden_steps", self.show_hidden_steps)
        checkbox = getattr(self.host, "show_hidden_steps_chk", None)
        if checkbox is not None and checkbox.isChecked() != self.show_hidden_steps:
            checkbox.setChecked(self.show_hidden_steps)
        self.refresh_editor_visibility()
        if changed and not self.host.is_switching_profile:
            self.host.log_setting_change(
                "显示隐藏步骤", "开启" if self.show_hidden_steps else "关闭"
            )



    def set_show_step_notes(self, enabled):
        enabled = bool(enabled)
        changed = enabled != self.show_step_notes
        self.show_step_notes = enabled
        self.host.settings.setValue("show_step_notes", self.show_step_notes)
        checkbox = getattr(self.host, "show_step_notes_chk", None)
        if checkbox is not None and checkbox.isChecked() != self.show_step_notes:
            checkbox.setChecked(self.show_step_notes)
        for row in range(self.host.task_list.count()):
            item = self.host.task_list.item(row)
            widget = self.host.task_list.itemWidget(item)
            if widget is not None:
                widget.set_note_display_visible(self.show_step_notes)
        self.host.task_list.schedule_virtual_refresh()
        if changed and not self.host.is_switching_profile:
            self.host.log_setting_change(
                "显示步骤备注", "开启" if self.show_step_notes else "关闭"
            )



    @staticmethod
    def _color_action_icon(color):
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor(color))
        return QIcon(pixmap)



    def build_task_context_menu(self, pos):
        task_list = self.host.task_list
        item = task_list.itemAt(pos)
        clicked_group_id = ""
        if item is None:
            task_list.clearSelection()
            task_list.setCurrentItem(None)
            rows = []
            paste_after_row = None
        else:
            clicked_row = task_list.row(item)
            widget = self.host.ensure_task_item_widget(item)
            if widget is not None:
                local_pos = widget.mapFrom(task_list.viewport(), pos)
                clicked_group_id = widget.group_id_at_position(local_pos)
            if clicked_group_id:
                rows = [
                    row
                    for row in range(task_list.count())
                    if clicked_group_id
                    in {
                        group["id"]
                        for group in normalize_group_path(
                            (self._task_data_at(row) or {}).get(
                                "editor_groups", []
                            )
                        )
                    }
                ]
                task_list.clearSelection()
                task_list.setCurrentItem(item, QItemSelectionModel.NoUpdate)
                for row in rows:
                    task_list.item(row).setSelected(True)
                paste_after_row = rows[-1] if rows else clicked_row
            else:
                if not item.isSelected():
                    task_list.select_item_with_modifiers(item, Qt.NoModifier)
                rows = self.selected_row_indexes()
                paste_after_row = max(rows) if rows else clicked_row
        menu = QMenu(task_list)
        if rows:
            header_text = (
                f"整个分组 · {len(rows)} 个步骤"
                if clicked_group_id
                else f"已选 {len(rows)} 个步骤"
                if len(rows) > 1
                else f"第 {rows[0] + 1} 步"
            )
            header_label = QLabel(header_text)
            header_label.setObjectName("contextMenuHeader")
            header_label.setAlignment(Qt.AlignLeft)
            header_action = QWidgetAction(task_list)
            header_action.setDefaultWidget(header_label)
            header_action.setEnabled(False)
            menu.addAction(header_action)
        locked = is_runtime_editing_locked(self.host)

        self._build_edit_actions(menu, rows, locked, clicked_group_id)
        self._build_group_actions(menu, rows, locked)
        self._build_clipboard_actions(
            menu, rows, locked, paste_after_row, item, clicked_group_id
        )

        if self.host.current_settings_mode() == "advanced":
            menu.addSeparator()
            reveal_action = menu.addAction("显示隐藏步骤")
            reveal_action.setCheckable(True)
            reveal_action.setChecked(self.show_hidden_steps)
            reveal_action.triggered.connect(self.set_show_hidden_steps)

        return menu



    def _build_edit_actions(self, menu, rows, locked, clicked_group_id):
        """Add step-editing entries to the context menu.

        Covers opening the step settings, the always-visible group rename,
        the batch/group settings gear, the note editor, the color-marker
        submenu and (advanced mode only) the hide toggle. Menu order and
        enable/disable conditions must match the original inline layout.
        """
        task_list = self.host.task_list
        if len(rows) == 1:
            open_action = menu.addAction("打开步骤设置")
            open_action.setEnabled(not locked)

            def open_settings():
                row_item = task_list.item(rows[0])
                widget = self.host.ensure_task_item_widget(row_item)
                if widget is not None:
                    widget.open_custom_config()

            open_action.triggered.connect(open_settings)
            if (
                not clicked_group_id
                and self.host.current_settings_mode() == "advanced"
            ):
                start_here_action = menu.addAction("从当前步骤开始执行")
                start_here_action.setEnabled(not locked)
                start_here_action.triggered.connect(
                    lambda _checked=False, row=rows[0]: self.host.execution_controller.start_task_from_step_once(
                        row
                    )
                )
            menu.addSeparator()

        # Top-level, always visible group rename so a group can be renamed
        # without switching to advanced mode (fixes "分组后无法修改组名").
        group_id_for_rename = clicked_group_id or self._selected_group_id()
        if group_id_for_rename:
            rename_top_action = menu.addAction("重命名分组…")
            rename_top_action.setEnabled(not locked)
            rename_top_action.triggered.connect(
                lambda _checked=False, value=group_id_for_rename: self.rename_group_by_id(
                    value
                )
            )

        if rows:
            gear_icon = QIcon(
                get_resource_path(os.path.join("assets", "step_settings.svg"))
            )
            if gear_icon.isNull():
                gear_icon = self.host.style().standardIcon(QStyle.SP_FileDialogDetailedView)
            # When right-clicking a group header, offer group rename (above),
            # not step-level batch settings.  When ALL selected rows happen to
            # belong to the same group even though the click was on a step body,
            # detect that and show a group-scoped gear instead.
            common_gid = self._selected_group_id()
            if not clicked_group_id:
                if common_gid:
                    group_batch_action = menu.addAction(gear_icon, "分组设置…")
                    group_batch_action.setEnabled(not locked)
                    group_batch_action.triggered.connect(self.open_batch_task_settings)
                else:
                    batch_action = menu.addAction(gear_icon, "批量修改步骤设置…")
                    batch_action.setEnabled(not locked)
                    batch_action.triggered.connect(self.open_batch_task_settings)
            note_action = menu.addAction("编辑备注…")
            note_action.setEnabled(not locked)
            note_action.triggered.connect(self.edit_selected_note)

            color_menu = menu.addMenu("颜色标记")
            current_colors = {
                normalize_marker_color(
                    (self._task_data_at(row) or {}).get("marker_color", "")
                )
                for row in rows
            }
            for color_id, label, color in STEP_MARKER_COLORS:
                action = color_menu.addAction(
                    self._color_action_icon(color), label
                )
                action.setCheckable(True)
                action.setChecked(
                    len(current_colors) == 1 and color_id in current_colors
                )
                action.setEnabled(not locked)
                action.triggered.connect(
                    lambda _checked=False, value=color_id: self.set_selected_marker_color(
                        value
                    )
                )

            if self.host.current_settings_mode() == "advanced":
                hidden_values = {
                    normalize_editor_metadata(self._task_data_at(row) or {})[
                        "editor_hidden"
                    ]
                    for row in rows
                }
                hide_text = (
                    "取消隐藏所选步骤"
                    if hidden_values == {True}
                    else "隐藏所选步骤"
                )
                hide_action = menu.addAction(hide_text)
                hide_action.setEnabled(not locked)
                hide_action.triggered.connect(
                    lambda: self.set_selected_hidden(hidden_values != {True})
                )



    def _build_group_actions(self, menu, rows, locked):
        """Add the advanced 步骤分组 submenu (create/rename/ungroup/toggle)."""
        if not rows:
            return
        if self.host.current_settings_mode() != "advanced":
            return
        group_menu = menu.addMenu("步骤分组")
        create_group_action = group_menu.addAction("将所选步骤建立新分组…")
        create_group_action.setEnabled(not locked)
        create_group_action.triggered.connect(
            self.create_group_from_selection
        )
        group_id = self._selected_group_id()
        rename_group_action = group_menu.addAction("重命名所在分组…")
        rename_group_action.setEnabled(bool(group_id) and not locked)
        rename_group_action.triggered.connect(self.rename_selected_group)
        ungroup_action = group_menu.addAction("取消最内层分组")
        ungroup_action.setEnabled(
            not locked
            and any(
                normalize_group_path(
                    (self._task_data_at(row) or {}).get(
                        "editor_groups", []
                    )
                )
                for row in rows
            )
        )
        ungroup_action.triggered.connect(self.ungroup_selected_steps)
        toggle_group_action = group_menu.addAction("折叠或展开所在分组")
        toggle_group_action.setEnabled(bool(group_id) and not locked)
        toggle_group_action.triggered.connect(
            lambda _checked=False, value=group_id: self.toggle_group_collapsed(
                value
            )
        )



    def _build_clipboard_actions(
        self, menu, rows, locked, paste_after_row, item, clicked_group_id
    ):
        """Add copy/cut/delete (when rows are targeted) and the paste entry."""
        if rows:
            menu.addSeparator()
            copy_action = menu.addAction("复制")
            copy_action.triggered.connect(self.copy_selected_row)
            cut_action = menu.addAction("剪切")
            cut_action.setEnabled(not locked)
            cut_action.triggered.connect(self.cut_selected_row)
            delete_action = menu.addAction(
                "删除整个分组" if clicked_group_id else "删除"
            )
            delete_action.setEnabled(not locked)
            delete_action.triggered.connect(
                lambda _checked=False, selected=tuple(rows): self.delete_selected_rows(
                    rows=selected
                )
            )

        paste_action = menu.addAction(
            "粘贴到步骤末尾"
            if item is None
            else "粘贴到整个分组后"
            if clicked_group_id
            else "粘贴到所选步骤后"
        )
        paste_action.setEnabled(not locked and self.has_paste_content())
        paste_action.triggered.connect(
            lambda _checked=False, target=paste_after_row: self.paste_row_after_selected(
                target
            )
        )



    def show_task_context_menu(self, pos):
        menu = self.build_task_context_menu(pos)
        menu.exec(self.host.task_list.viewport().mapToGlobal(pos))



    def toggle_selected_breakpoint(self):
        if self.host.current_settings_mode() != "advanced":
            QToolTip.showText(
                QCursor.pos(), "调试断点仅在高级模式中设置和执行", self.host
            )
            return False
        row = self.host.selected_row_index()
        if row is None:
            QToolTip.showText(QCursor.pos(), "请先选中一个步骤", self.host)
            return False
        item = self.host.task_list.item(row)
        widget = self.host.ensure_task_item_widget(item)
        if not widget or not hasattr(widget, "set_breakpoint"):
            return False
        self.host.push_undo_state()
        enabled = not widget.has_breakpoint()
        widget.set_breakpoint(enabled)
        QToolTip.showText(
            QCursor.pos(),
            f"第 {row + 1} 步断点已{'添加' if enabled else '清除'}",
            self.host.breakpoint_toggle_btn,
        )
        self.host.append_application_log(
            f"第 {row + 1} 步调试断点已{'添加' if enabled else '清除'}。",
            LOG_PARAMETERS,
        )
        return True
