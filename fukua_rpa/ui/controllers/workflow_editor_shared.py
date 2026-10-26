# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Shared helpers, constants and logger for the WorkflowEditorController mixins.

Extracted verbatim from workflow_editor.py; imported by the split mixins and
re-exported by the workflow_editor facade for backward compatibility.
"""

from __future__ import annotations

import json
import logging

from ...constants import LEGACY_TASK_CLIPBOARD_KEY, TASK_CLIPBOARD_KEY
from ...json_safety import loads_bounded_json

logger = logging.getLogger(__name__)


MAX_UNDO_DEPTH = 50


MAX_TASK_CLIPBOARD_BYTES = 1024 * 1024


DELETE_LOG_CHUNK_CHARS = 48 * 1024


_CURRENT_SELECTION = object()


def _snapshot_hash(tasks):
    """Compute a fast hash of a task list for undo dedup comparison."""
    import hashlib

    return hashlib.md5(
        json.dumps(tasks, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()



def parse_task_clipboard(text):
    clip = loads_bounded_json(
        str(text or ""),
        max_bytes=MAX_TASK_CLIPBOARD_BYTES,
        label="剪贴板步骤",
        max_depth=16,
        max_nodes=10_000,
    )
    if not isinstance(clip, dict):
        return None
    data = clip.get(TASK_CLIPBOARD_KEY, clip.get(LEGACY_TASK_CLIPBOARD_KEY))
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
        return data
    return None

