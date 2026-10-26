"""Stable task identity and branch-reference operations for workflow editing.

Displayed step numbers are editable views. Reorder and grouping preserve each
step ID; cloning creates new IDs and remaps references inside the copied batch.
See ADR 0023.
"""

from __future__ import annotations

import bisect
import copy
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .workflow_metadata import (
    MAX_GROUP_DEPTH,
    isolate_editor_group_identities_for_insert,
    normalize_group_path,
    repair_discontiguous_group_paths,
)

# INVARIANT: each visible numeric jump has a stable-ID companion. Runtime/UI
# code must update both through this module instead of treating row numbers as
# durable identities.
REFERENCE_FIELDS = (
    ("success_jump", "success_target_id", "成功后跳至"),
    ("fail_jump", "fail_target_id", "失败后跳至"),
    ("call_step", "call_target_id", "先执行指定步骤"),
    ("until_false_jump", "until_false_target_id", "未满足跳回"),
    ("until_true_jump", "until_true_target_id", "满足后跳至"),
)
_STEP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _reference_applies(task: Mapping[str, Any], jump_field: str) -> bool:
    if not jump_field.startswith("until_"):
        return True
    try:
        return float(task.get("type", 0)) == 15.0
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class TaskReference:
    source_index: int
    source_id: str
    field: str
    label: str


@dataclass(frozen=True)
class WorkflowMergeResult:
    tasks: list[dict[str, Any]]
    appended_count: int
    remapped_references: int
    cleared_references: int


def new_step_id() -> str:
    """Generate a fresh unique step identifier (UUID4 hex)."""
    return uuid.uuid4().hex


def valid_step_id(value: Any) -> bool:
    """Return True if *value* is a well-formed step ID (8-64 alphanumeric/dash/underscore chars)."""
    return bool(_STEP_ID_RE.fullmatch(str(value or "").strip()))


def _jump_number(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_workflow_tasks(
    tasks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return detached tasks with unique IDs and synchronized display jumps.

    Existing valid target IDs win over stale numeric fields; numbers are then
    regenerated for display. This is intentional after a reorder.
    """

    normalized = [copy.deepcopy(dict(task)) for task in tasks]
    seen: set[str] = set()
    for task in normalized:
        step_id = str(task.get("step_id", "")).strip()
        if not valid_step_id(step_id) or step_id in seen:
            step_id = new_step_id()
        task["step_id"] = step_id
        seen.add(step_id)

    index_by_id = {
        str(task["step_id"]): index for index, task in enumerate(normalized)
    }
    for task in normalized:
        for jump_field, target_field, _label in REFERENCE_FIELDS:
            if not _reference_applies(task, jump_field):
                task[target_field] = ""
                continue
            target_id = str(task.get(target_field, "") or "").strip()
            if target_id in index_by_id:
                task[target_field] = target_id
                task[jump_field] = str(index_by_id[target_id] + 1)
                continue
            if target_id:
                # FR-20260811-D5-001: target_id 非空但悬空（跨方案粘贴/导入，
                # 源方案的 step_id 在当前列表不存在）——不得按旧数字分支号改绑到
                # 无关步骤（会把跳转静默重定向到错误步骤）。置空并标记需用户修正。
                task[target_field] = ""
                task[jump_field] = "0"
                continue
            # target_id 为空（旧数据只有数字分支字段）：按数字回退绑定，保持
            # 对历史方案的兼容。
            jump = _jump_number(task.get(jump_field, 0))
            if 1 <= jump <= len(normalized):
                target_id = str(normalized[jump - 1]["step_id"])
                task[target_field] = target_id
                task[jump_field] = str(jump)
            else:
                task[target_field] = ""
                task[jump_field] = "0"
    return normalized


def apply_numeric_reference_edits(
    task: Mapping[str, Any], ordered_tasks: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Resolve branch numbers explicitly edited in a task dialog to stable IDs."""

    ordered = normalize_workflow_tasks(ordered_tasks)
    result = copy.deepcopy(dict(task))
    for jump_field, target_field, _label in REFERENCE_FIELDS:
        if not _reference_applies(result, jump_field):
            result[target_field] = ""
            continue
        jump = _jump_number(result.get(jump_field, 0))
        if 1 <= jump <= len(ordered):
            result[target_field] = str(ordered[jump - 1]["step_id"])
            result[jump_field] = str(jump)
        else:
            result[target_field] = ""
            result[jump_field] = "0"
    return result


def clone_task_for_insert(task: Mapping[str, Any]) -> dict[str, Any]:
    """Clone one task into a fresh group namespace and rebase self-jumps."""

    cloned = copy.deepcopy(dict(task))
    old_id = str(cloned.get("step_id", "") or "")
    cloned["step_id"] = new_step_id()
    for jump_field, target_field, _label in REFERENCE_FIELDS:
        if not _reference_applies(cloned, jump_field):
            cloned[target_field] = ""
            continue
        if old_id and str(cloned.get(target_field, "") or "") == old_id:
            cloned[target_field] = cloned["step_id"]
    return isolate_editor_group_identities_for_insert([cloned])[0]


def clone_tasks_for_insert(
    tasks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Clone one freshly isolated batch and rebase its internal references.

    INVARIANT: references within the copied batch follow the copies, while
    references outside the batch retain their original destination.
    """

    source = isolate_editor_group_identities_for_insert(tasks)
    id_map = {
        str(task.get("step_id", "")): new_step_id()
        for task in source
        if valid_step_id(task.get("step_id", ""))
    }
    cloned: list[dict[str, Any]] = []
    for task in source:
        old_id = str(task.get("step_id", ""))
        copied = copy.deepcopy(task)
        copied["step_id"] = id_map.get(old_id) or new_step_id()
        for jump_field, target_field, _label in REFERENCE_FIELDS:
            if not _reference_applies(copied, jump_field):
                copied[target_field] = ""
                continue
            target_id = str(task.get(target_field, "") or "")
            if target_id in id_map:
                copied[target_field] = id_map[target_id]
        cloned.append(copied)
    return cloned


def attach_cloned_tasks_to_insertion_context(
    cloned_tasks: Sequence[Mapping[str, Any]],
    destination_tasks: Sequence[Mapping[str, Any]],
    insert_at: int,
) -> list[dict[str, Any]]:
    """Keep a surrounding contiguous group intact when inserting cloned tasks."""

    prepared = [copy.deepcopy(dict(task)) for task in cloned_tasks]
    if not prepared:
        return []
    destination = [dict(task) for task in destination_tasks]
    boundary = max(0, min(int(insert_at), len(destination)))
    if boundary <= 0 or boundary >= len(destination):
        return prepared

    before_path = normalize_group_path(
        destination[boundary - 1].get("editor_groups", [])
    )
    after_path = normalize_group_path(
        destination[boundary].get("editor_groups", [])
    )
    shared_path: list[dict[str, Any]] = []
    for before_group, after_group in zip(before_path, after_path, strict=False):
        if before_group["id"] != after_group["id"]:
            break
        shared_path.append(copy.deepcopy(before_group))
    if not shared_path:
        return prepared

    if len(prepared) == 1:
        prepared[0]["editor_groups"] = shared_path
        return prepared

    remaining_depth = max(0, MAX_GROUP_DEPTH - len(shared_path))
    for task in prepared:
        source_path = normalize_group_path(task.get("editor_groups", []))
        task["editor_groups"] = [
            *copy.deepcopy(shared_path),
            *copy.deepcopy(source_path[:remaining_depth]),
        ]
    return prepared


def merge_workflow_tasks(
    destination_tasks: Sequence[Mapping[str, Any]],
    source_tasks: Sequence[Mapping[str, Any]],
    *,
    preserve_internal_references: bool = True,
) -> WorkflowMergeResult:
    """Append a freshly isolated workflow and safely rebase absolute jumps."""

    destination, _split_count = repair_discontiguous_group_paths(
        normalize_workflow_tasks(destination_tasks)
    )
    destination_group_ids = {
        group["id"]
        for task in destination
        for group in normalize_group_path(task.get("editor_groups", []))
    }
    source = isolate_editor_group_identities_for_insert(
        normalize_workflow_tasks(source_tasks),
        reserved_ids=destination_group_ids,
    )
    used_ids = {str(task["step_id"]) for task in destination}
    id_map: dict[str, str] = {}
    for task in source:
        old_id = str(task["step_id"])
        for _ in range(1000):
            new_id = new_step_id()
            if new_id not in used_ids:
                break
        else:
            raise ValueError("无法生成唯一的步骤 ID（已尝试 1000 次）")
        id_map[old_id] = new_id
        used_ids.add(new_id)

    appended: list[dict[str, Any]] = []
    remapped = 0
    cleared = 0
    for source_task in source:
        copied = copy.deepcopy(source_task)
        copied["step_id"] = id_map[str(source_task["step_id"])]
        for jump_field, target_field, _label in REFERENCE_FIELDS:
            if not _reference_applies(copied, jump_field):
                # FR-20261005-102: 合并方案**不得**清零与当前动作无关的 until_*
                # 草稿——用户可能先在"直到条件成立"里填好跳转，再把该步改成别的
                # 动作（草稿随之不可见但仍在数据里）。清零 target_field 即可：
                # 它在非 until 步骤上没有意义，留着只会让后续切回 until 时指向错行。
                # jump_field 是用户填的原值，必须原样保留。
                copied[target_field] = ""
                continue
            target_id = str(source_task.get(target_field, "") or "")
            had_reference = bool(target_id) or _jump_number(
                source_task.get(jump_field, 0)
            ) > 0
            if preserve_internal_references and target_id in id_map:
                copied[target_field] = id_map[target_id]
                if had_reference:
                    remapped += 1
            else:
                copied[target_field] = ""
                copied[jump_field] = "0"
                if had_reference:
                    cleared += 1
        appended.append(copied)

    merged, _split_count = repair_discontiguous_group_paths(
        normalize_workflow_tasks([*destination, *appended])
    )
    return WorkflowMergeResult(
        tasks=merged,
        appended_count=len(appended),
        remapped_references=remapped,
        cleared_references=cleared,
    )


def references_to_step(
    tasks: Sequence[Mapping[str, Any]], target_id: str
) -> list[TaskReference]:
    """Find all branch references that point at the step identified by *target_id*.

    Args:
        tasks: Ordered task list to scan.
        target_id: The step_id to search for among reference targets.

    Returns:
        List of TaskReference descriptors (source index is 1-based for display).
    """
    target_id = str(target_id or "").strip()
    references: list[TaskReference] = []
    if not target_id:
        return references
    for index, task in enumerate(tasks):
        source_id = str(task.get("step_id", "") or "")
        for jump_field, target_field, label in REFERENCE_FIELDS:
            if not _reference_applies(task, jump_field):
                continue
            if str(task.get(target_field, "") or "") == target_id:
                references.append(
                    TaskReference(index + 1, source_id, target_field, label)
                )
    return references


def remove_task_and_clear_references(
    tasks: Sequence[Mapping[str, Any]], target_id: str
) -> list[dict[str, Any]]:
    """Remove a task and turn references to it into normal fall-through branches."""

    target_id = str(target_id or "").strip()
    remaining = [
        copy.deepcopy(dict(task))
        for task in tasks
        if str(task.get("step_id", "") or "") != target_id
    ]
    for task in remaining:
        for jump_field, target_field, _label in REFERENCE_FIELDS:
            if not _reference_applies(task, jump_field):
                task[target_field] = ""
                continue
            if str(task.get(target_field, "") or "") == target_id:
                task[target_field] = ""
                task[jump_field] = "0"
    return normalize_workflow_tasks(remaining)


def remove_tasks_and_clear_references(
    tasks: Sequence[Mapping[str, Any]], target_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """Remove several tasks and clear every surviving reference to them."""

    targets = {str(value or "").strip() for value in target_ids}
    targets.discard("")
    remaining = [
        copy.deepcopy(dict(task))
        for task in tasks
        if str(task.get("step_id", "") or "") not in targets
    ]
    for task in remaining:
        for jump_field, target_field, _label in REFERENCE_FIELDS:
            if not _reference_applies(task, jump_field):
                task[target_field] = ""
                continue
            if str(task.get(target_field, "") or "") in targets:
                task[target_field] = ""
                task[jump_field] = "0"
    return normalize_workflow_tasks(remaining)


def clear_references_to_steps(
    tasks: Sequence[Mapping[str, Any]], target_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """Clear every reference that points at the given steps, without removing them.

    Used when the "调整跟随" (adjust-follow) toggle is OFF and a step is moved:
    inbound jumps to the moved step are dropped to normal fall-through, matching
    the legacy delete behaviour. References are keyed by step_id, so moving a
    step never invalidates them by itself — this is the explicit opt-out path.
    """

    targets = {str(value or "").strip() for value in target_ids}
    targets.discard("")
    if not targets:
        return normalize_workflow_tasks(list(tasks))
    result = [copy.deepcopy(dict(task)) for task in tasks]
    for task in result:
        for jump_field, target_field, _label in REFERENCE_FIELDS:
            if not _reference_applies(task, jump_field):
                continue
            if str(task.get(target_field, "") or "") in targets:
                task[target_field] = ""
                task[jump_field] = "0"
    return normalize_workflow_tasks(result)


def _compute_follow_successors(
    tasks: Sequence[Mapping[str, Any]], removed_ids: set[str]
) -> dict[str, str]:
    """Map each removed step_id to the surviving step_id that follows it.

    "Follow" redirects a reference that pointed at a deleted step to the next
    surviving step in original order, so the branch keeps targeting the position
    the deleted step occupied. A removed last step has no successor and maps to
    "" (the caller then falls back to clearing the reference).
    """

    ordered = [
        (str(task.get("step_id", "") or ""), index)
        for index, task in enumerate(tasks)
    ]
    # FR-20261008-301: the successor lookup used to call an O(n) ``original_index``
    # from inside two nested loops, so deleting k steps out of n cost ~O(k·n²)
    # (measured: 260s for the 5000-step product limit). Precompute each step's
    # *first* occurrence index once, then binary-search the first surviving step
    # after the removed one. Sorting surviving steps by their first-occurrence
    # index is equivalent to the previous list-order scan (a step that appears
    # later in the list can never carry a smaller first-occurrence index than one
    # already passed, because its own first occurrence would have come first),
    # and the result is unchanged for unique and duplicated step IDs alike.
    first_index: dict[str, int] = {}
    for sid, index in ordered:
        first_index.setdefault(sid, index)
    surviving = [
        (first_index[sid], sid) for sid, _ in ordered if sid not in removed_ids
    ]
    surviving.sort(key=lambda pair: pair[0])
    keys = [index for index, _ in surviving]

    follow: dict[str, str] = {}
    for removed_id in removed_ids:
        removed_index = first_index.get(removed_id, -1)
        position = bisect.bisect_right(keys, removed_index)
        follow[removed_id] = (
            surviving[position][1] if position < len(surviving) else ""
        )
    return follow


def remove_tasks_and_follow_references(
    tasks: Sequence[Mapping[str, Any]],
    target_ids: Sequence[str],
    follow: bool = True,
) -> list[dict[str, Any]]:
    """Remove tasks and either follow or clear their inbound references.

    When ``follow`` is True (default), a reference that pointed at a deleted
    step is redirected to that step's successor (see ``_compute_follow_successors``)
    so the branch keeps targeting the same position. When False, every inbound
    reference is cleared to a normal fall-through, matching the legacy behavior.
    """

    targets = {str(value or "").strip() for value in target_ids}
    targets.discard("")
    if not targets:
        return normalize_workflow_tasks(list(tasks))
    if not follow:
        return remove_tasks_and_clear_references(tasks, target_ids)

    follow_map = _compute_follow_successors(list(tasks), targets)
    remaining = [
        copy.deepcopy(dict(task))
        for task in tasks
        if str(task.get("step_id", "") or "") not in targets
    ]
    for task in remaining:
        for jump_field, target_field, _label in REFERENCE_FIELDS:
            if not _reference_applies(task, jump_field):
                task[target_field] = ""
                continue
            target_id = str(task.get(target_field, "") or "")
            if target_id in targets:
                successor = follow_map.get(target_id, "")
                if successor:
                    task[target_field] = successor
                    task[jump_field] = ""
                else:
                    task[target_field] = ""
                    task[jump_field] = "0"
    return normalize_workflow_tasks(remaining)


def materialize_runtime_references(
    tasks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Produce a runtime snapshot whose numeric jumps match stable references."""

    return normalize_workflow_tasks(tasks)
