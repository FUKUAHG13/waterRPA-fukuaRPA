# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Copy / cut / paste and delete-snapshot clipboard for WorkflowEditorController."""

from __future__ import annotations

import copy
import json
import logging

import pyperclip

from ...commands import command_name
from ...constants import TASK_CLIPBOARD_KEY, TASK_TYPE_SECRET_TEXT
from ...log_policy import LOG_ACTION, LOG_PARAMETERS
from ...workflow_document import (
    attach_cloned_tasks_to_insertion_context,
    clone_tasks_for_insert,
)
from ..lifecycle import is_runtime_editing_locked
from .workflow_editor_shared import (
    _CURRENT_SELECTION,
    DELETE_LOG_CHUNK_CHARS,
    parse_task_clipboard,
)

logger = logging.getLogger(__name__)


class StepClipboardMixin:
    """Row clipboard plus copy/cut/paste and delete audit snapshot.

    Owns every method that reads or writes the ``task_clipboard`` state. That
    attribute itself is initialized by ``WorkflowEditorController.__init__``,
    like all other mixin state in this package — mixins here declare behaviour,
    never class-level defaults. Shared helpers (``_task_data_at`` etc.) stay on
    ``WorkflowStepEditMixin`` so callers keep reaching them through ``self``
    unchanged.
    """

    def _store_task_clipboard(self, data):
        self.task_clipboard = copy.deepcopy(data)
        try:
            pyperclip.copy(
                json.dumps(
                    {TASK_CLIPBOARD_KEY: self.task_clipboard},
                    ensure_ascii=False,
                )
            )
        except Exception as exc:
            self.host.append_application_log(
                f"<font color='orange'>剪贴板同步失败: {exc}</font>"
            )

    def has_paste_content(self):
        if self.task_clipboard is not None:
            return True
        try:
            return parse_task_clipboard(pyperclip.paste()) is not None
        except Exception as exc:
            logger.debug("检查剪贴板失败: %s", exc, exc_info=True)
            return False

    def copy_selected_row(self):
        rows = self.selected_row_indexes()
        if not rows:
            return
        tasks = [self._task_data_at(row) for row in rows]
        tasks = [task for task in tasks if isinstance(task, dict)]
        if not tasks:
            return
        self._store_task_clipboard(tasks[0] if len(tasks) == 1 else tasks)
        detail = (
            f"第 {rows[0] + 1} 步"
            if len(rows) == 1
            else f"{len(rows)} 个步骤"
        )
        self.host.append_application_log(f"<font color='gray'>已复制{detail}。</font>")

    @staticmethod
    def _task_command_name(task):
        try:
            return command_name(float((task or {}).get("type", 0)))
        except (TypeError, ValueError, OverflowError):
            return "未知指令"

    @staticmethod
    def _task_for_private_delete_log(task):
        payload = copy.deepcopy(task if isinstance(task, dict) else {})
        try:
            secret = float(payload.get("type", 0)) == TASK_TYPE_SECRET_TEXT
        except (TypeError, ValueError, OverflowError):
            secret = False
        if secret:
            payload["value"] = "<秘密文本引用已隐藏>"
        return payload

    def _log_deleted_tasks(self, action, rows, tasks):
        numbered_rows = [int(row) + 1 for row in rows]
        counts = {}
        for task in tasks:
            name = self._task_command_name(task)
            counts[name] = counts.get(name, 0) + 1
        command_summary = "、".join(
            f"{name} {count} 个" if count > 1 else name
            for name, count in counts.items()
        )
        location = "原" + self._format_step_numbers(numbered_rows)
        self.host.append_application_log(
            f"已{action}{location}（{command_summary}）。"
        )
        # The jump-repair note is noisy in simple mode; only surface it in
        # detailed/complete logs (LOG_ACTION is excluded from simple mode).
        self.host.append_application_log("相关绝对跳转已安全修复。", LOG_ACTION)
        snapshots = [
            {
                "原步号": row + 1,
                "指令": self._task_command_name(task),
                "参数": self._task_for_private_delete_log(task),
            }
            for row, task in zip(rows, tasks, strict=False)
        ]
        serialized = json.dumps(
            snapshots, ensure_ascii=False, separators=(",", ":")
        )
        # Keep the JSON reconstructable after legacy HTML-like log cleanup.
        serialized = serialized.replace("<", "\\u003c").replace(">", "\\u003e")
        chunks = [
            serialized[offset : offset + DELETE_LOG_CHUNK_CHARS]
            for offset in range(0, max(1, len(serialized)), DELETE_LOG_CHUNK_CHARS)
        ]
        total_chunks = len(chunks)
        for chunk_index, chunk in enumerate(chunks, 1):
            public_detail = (
                f"{action}步骤参数快照第 {chunk_index}/{total_chunks} 段："
                f"{location}（{command_summary}）；具体参数受日志隐私保护。"
            )
            self.host.append_application_log(
                public_detail,
                LOG_PARAMETERS,
                private_msg=(
                    f"{action}步骤完整快照第 {chunk_index}/{total_chunks} 段："
                    + chunk
                ),
            )

    def cut_selected_row(self):
        if is_runtime_editing_locked(self.host):
            return False
        rows = self.selected_row_indexes()
        if len(rows) <= 1:
            row = rows[0] if rows else None
            if row is None:
                return False
            return self._remove_task_at(row, copy_to_clipboard=True)
        return self.delete_selected_rows(copy_to_clipboard=True)

    def paste_row_after_selected(self, after_row=_CURRENT_SELECTION):
        if is_runtime_editing_locked(self.host):
            return
        data = self.task_clipboard
        if data is None:
            try:
                data = parse_task_clipboard(pyperclip.paste())
            except Exception as exc:
                logger.debug("读取剪贴板失败: %s", exc, exc_info=True)
                data = None
        if not isinstance(data, (dict, list)):
            return
        row = (
            self.host.selected_row_index()
            if after_row is _CURRENT_SELECTION
            else after_row
        )
        if row is not None:
            try:
                row = int(row)
            except (TypeError, ValueError, OverflowError):
                row = None
            if row is not None and not 0 <= row < self.host.task_list.count():
                row = None
        insert_at = self.host.task_list.count() if row is None else row + 1
        source = [data] if isinstance(data, dict) else data
        cloned = clone_tasks_for_insert(source)
        cloned = attach_cloned_tasks_to_insertion_context(
            cloned,
            self.host.snapshot_tasks(),
            insert_at,
        )
        if not cloned:
            return
        self.host.push_undo_state()
        for offset, task in enumerate(cloned):
            self.host.add_row(
                task,
                index=insert_at + offset,
                record_undo=False,
                select=False,
                materialize=False,
                update_indexes=False,
            )
        self.host.update_indexes()
        last = self.host.task_list.item(insert_at + len(cloned) - 1)
        self.host.task_list.setCurrentItem(last)
        self.host.task_list.scrollToItem(last)
        self.host.task_list.schedule_virtual_refresh(immediate=True)
        suffix = (
            f"第 {insert_at + 1} 步"
            if len(cloned) == 1
            else f"第 {insert_at + 1} 至 {insert_at + len(cloned)} 步"
        )
        self.host.append_application_log(f"已粘贴为{suffix}。")



