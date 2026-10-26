# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Workflow-list editing, history and reference maintenance."""

from __future__ import annotations

import logging
import weakref
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer

from .step_clipboard_mixin import StepClipboardMixin
from .step_reorder_reference_mixin import StepReorderReferenceMixin
from .workflow_context import WorkflowContextMixin
from .workflow_editor_shared import (
    _CURRENT_SELECTION,
    DELETE_LOG_CHUNK_CHARS,
    MAX_TASK_CLIPBOARD_BYTES,
    MAX_UNDO_DEPTH,
    _snapshot_hash,
    parse_task_clipboard,
)
from .workflow_group import WorkflowGroupMixin
from .workflow_search import WorkflowSearchMixin
from .workflow_step_edit import WorkflowStepEditMixin
from .workflow_undo import WorkflowUndoMixin

if TYPE_CHECKING:
    from .host_view import HostView

logger = logging.getLogger(__name__)

__all__ = [
    "_snapshot_hash",
    "parse_task_clipboard",
    "MAX_UNDO_DEPTH",
    "MAX_TASK_CLIPBOARD_BYTES",
    "DELETE_LOG_CHUNK_CHARS",
    "_CURRENT_SELECTION",
    "WorkflowEditorController",
]


class WorkflowEditorController(
    WorkflowUndoMixin,
    StepClipboardMixin,
    StepReorderReferenceMixin,
    WorkflowStepEditMixin,
    WorkflowGroupMixin,
    WorkflowSearchMixin,
    WorkflowContextMixin,
):
    """Own mutable editor history while coordinating the task-list view."""

    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)
        self.task_clipboard = None
        self.undo_stack = []
        self.redo_stack = []
        self._last_highlighted_selected = set()
        timer_parent = host if isinstance(host, QObject) else None
        self._selection_highlight_timer = QTimer(timer_parent)
        self._selection_highlight_timer.setSingleShot(True)
        self._selection_highlight_timer.timeout.connect(
            self._apply_selection_highlight
        )
        self._init_task_search(timer_parent)
        self._last_undo_hash = None
        self.restoring_history = False
        self.show_hidden_steps = bool(
            host.settings.value("show_hidden_steps", False, type=bool)
        )
        self.show_step_notes = bool(
            host.settings.value("show_step_notes", False, type=bool)
        )
