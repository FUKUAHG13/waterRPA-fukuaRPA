"""Editor-only workflow metadata shared by models and Qt views.

Notes, colors, hiding, folding and grouping never change execution membership
or step identity. See ADR 0034 and ``docs/设计基准.md``.
"""

from __future__ import annotations

import copy
import hashlib
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .task_model import config_bool

MAX_STEP_NOTE_LENGTH = 2000
# DESIGN: three levels are a deliberate UI comprehension limit, not a parser
# accident. Raising it requires layout, selection and import/export review.
MAX_GROUP_DEPTH = 3
MAX_GROUP_NAME_LENGTH = 100
_MAX_GROUP_ID_LENGTH = 80
_GROUP_ID_DIGEST_LENGTH = 12
_CANONICAL_REMAP_NAMESPACE = "canonical-v1"
_ISOLATION_REMAP_PURPOSE = "insert-isolation-v1"

STEP_MARKER_COLORS = (
    ("", "无标记", "#FFFFFF"),
    ("red", "红色", "#EF4444"),
    ("orange", "橙色", "#F59E0B"),
    ("yellow", "黄色", "#EAB308"),
    ("green", "绿色", "#22C55E"),
    ("blue", "蓝色", "#3B82F6"),
    ("purple", "紫色", "#A855F7"),
    ("gray", "灰色", "#98A2B3"),
)

_MARKER_COLOR_IDS = frozenset(item[0] for item in STEP_MARKER_COLORS)


@dataclass(frozen=True)
class EditorGroupSpan:
    """Immutable descriptor of one contiguous group occurrence in flat order.

    Represents a single visual group header spanning rows [start, end] at a
    given nesting depth. Used by presentation builders and collapse logic.

    Attributes:
        group_id: Stable identity of the group.
        name: Display name shown in the group header.
        depth: Nesting level (1-based; 1 = top-level group).
        start: Index of the first task row in this span.
        end: Index of the last task row in this span (inclusive).
        collapsed: Whether the group is visually collapsed in the editor.
    """

    group_id: str
    name: str
    depth: int
    start: int
    end: int
    collapsed: bool

    @property
    def count(self) -> int:
        """Return the number of task rows contained in this span."""
        return self.end - self.start + 1


@dataclass(frozen=True)
class _EditorGroupOwner:
    depth: int
    parent_identity: tuple[tuple[str, str, bool], ...]
    name: str
    collapsed: bool
    start: int
    end: int


def new_editor_group_id() -> str:
    """Generate a fresh, globally unique editor group identifier.

    Returns:
        A string of the form ``"group_<uuid4-hex>"``.
    """
    return "group_" + uuid.uuid4().hex


def new_editor_group_isolation_namespace() -> str:
    """Generate a random namespace token for insert-isolation remapping.

    Each clone/paste batch receives its own namespace so that remapped group
    IDs never collide with the source or with other concurrent batches.

    Returns:
        A UUID4 hex string used as the namespace component.
    """
    return uuid.uuid4().hex


def _normalize_group_id(value: Any) -> str:
    group_id = str(value or "").strip()
    if len(group_id) <= _MAX_GROUP_ID_LENGTH:
        return group_id
    digest = hashlib.sha256(group_id.encode("utf-8")).hexdigest()[
        :_GROUP_ID_DIGEST_LENGTH
    ]
    prefix_length = _MAX_GROUP_ID_LENGTH - _GROUP_ID_DIGEST_LENGTH - 1
    return f"{group_id[:prefix_length]}_{digest}"


def _normalize_group_path_fields(value: Any) -> list[dict[str, Any]]:
    """Normalize bounded fields without dropping repeated group identities."""

    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for raw in value[:MAX_GROUP_DEPTH]:
        if not isinstance(raw, Mapping):
            continue
        group_id = _normalize_group_id(raw.get("id", ""))
        if not group_id:
            continue
        name = str(raw.get("name", "") or "").replace("\x00", "").strip()
        result.append(
            {
                "id": group_id,
                "name": (name or "未命名分组")[:MAX_GROUP_NAME_LENGTH],
                "collapsed": config_bool(raw.get("collapsed", False)),
            }
        )
    return result


def _deduplicated_path_group_id(group, depth, parents, occupied_ids) -> str:
    parent_identity = tuple(
        (parent["id"], parent["name"], bool(parent["collapsed"]))
        for parent in parents
    )
    for attempt in range(1000):
        semantic_identity = (
            group["id"],
            depth,
            parent_identity,
            group["name"],
            bool(group["collapsed"]),
            attempt,
        )
        digest = hashlib.sha256(repr(semantic_identity).encode("utf-8")).hexdigest()
        candidate = f"group_normalized_{digest[:32]}"
        if candidate not in occupied_ids:
            return candidate
    raise ValueError("无法生成唯一的分组 ID（已尝试 1000 次）")


def normalize_group_path(value: Any) -> list[dict[str, Any]]:
    """Normalize a raw group path into at most three layers with unique IDs.

    Parses the raw value, strips invalid entries, deduplicates repeated
    group IDs within the path, and enforces MAX_GROUP_DEPTH.

    Args:
        value: Raw ``editor_groups`` field from a task dict (any type
            accepted; non-list values yield an empty path).

    Returns:
        List of group dicts (each with ``id``, ``name``, ``collapsed``)
        with globally unique IDs within the path, length <= MAX_GROUP_DEPTH.
    """

    parsed = _normalize_group_path_fields(value)
    occupied_ids = {group["id"] for group in parsed}
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for depth, parsed_group in enumerate(parsed):
        group = copy.deepcopy(parsed_group)
        if group["id"] in seen:
            group["id"] = _deduplicated_path_group_id(
                group,
                depth,
                result,
                occupied_ids,
            )
            occupied_ids.add(group["id"])
        result.append(group)
        seen.add(group["id"])
    return result


def new_editor_group(name: Any) -> dict[str, Any]:
    """Create a new editor group dict with a unique ID and sanitized name.

    Args:
        name: Desired display name; NUL characters are stripped, whitespace
            trimmed, and the result truncated to MAX_GROUP_NAME_LENGTH.
            Falls back to "未命名分组" when empty.

    Returns:
        Dict with ``id``, ``name`` and ``collapsed`` (initially False).
    """
    normalized_name = str(name or "").replace("\x00", "").strip()
    return {
        "id": new_editor_group_id(),
        "name": (normalized_name or "未命名分组")[:MAX_GROUP_NAME_LENGTH],
        "collapsed": False,
    }


def _group_prefix_identity(path, depth) -> tuple[tuple[str, str, bool], ...]:
    return tuple(
        (group["id"], group["name"], bool(group["collapsed"]))
        for group in path[: depth + 1]
    )


def _new_unique_group_id(used_ids) -> str:
    group_id = new_editor_group_id()
    while group_id in used_ids:
        group_id = new_editor_group_id()
    used_ids.add(group_id)
    return group_id


def _materialize_reserved_group_ids(reserved_ids: Iterable[str]) -> frozenset[str]:
    materialized: set[str] = set()
    for value in reserved_ids:
        group_id = str(value)
        if group_id:
            materialized.add(group_id)
    return frozenset(materialized)


def _group_owner(path, depth, start, end) -> _EditorGroupOwner:
    group = path[depth]
    return _EditorGroupOwner(
        depth=depth,
        parent_identity=_group_prefix_identity(path, depth - 1),
        name=group["name"],
        collapsed=bool(group["collapsed"]),
        start=start,
        end=end,
    )


def _semantic_group_keys(
    paths, owners_by_id
) -> tuple[dict[str, tuple[int, tuple[Any, ...], str, bool, int, int]], list[tuple[str, _EditorGroupOwner]]]:
    semantic_keys = {}
    ordered_owners = sorted(
        owners_by_id.items(),
        key=lambda item: (item[1].start, item[1].depth, item[1].end),
    )
    for group_id, owner in ordered_owners:
        parent_key = ()
        if owner.depth:
            parent_id = paths[owner.start][owner.depth - 1]["id"]
            parent_key = semantic_keys[parent_id]
        semantic_keys[group_id] = (
            owner.depth,
            parent_key,
            owner.name,
            owner.collapsed,
            owner.start,
            owner.end,
        )
    return semantic_keys, ordered_owners


def _apply_deterministic_group_remap(
    paths,
    owners_by_id,
    *,
    namespace,
    reserved_ids: frozenset[str],
) -> None:
    semantic_keys, ordered_owners = _semantic_group_keys(paths, owners_by_id)
    assigned: set[str] = set()
    replacements: dict[str, str] = {}
    for old_id, _owner in ordered_owners:
        attempt = 0
        while True:
            identity = (
                "fukua-rpa-editor-group-v1",
                str(namespace),
                semantic_keys[old_id],
                attempt,
            )
            digest = hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()
            candidate = f"group_{digest}"
            if candidate not in reserved_ids and candidate not in assigned:
                break
            attempt += 1
        replacements[old_id] = candidate
        assigned.add(candidate)

    for path in paths:
        for group in path:
            group["id"] = replacements[group["id"]]


def _split_conflicting_group_ids(
    paths: list,
    row: int,
    end: int,
    depth: int,
    used_group_ids: set[str],
) -> None:
    """Assign fresh unique IDs to a span whose group ID conflicts with another owner.

    INVARIANT: every node at *depth* and below within [row, end] gets a new ID
    so the conflicting span becomes a distinct group.
    """
    replacements: dict[tuple[int, str], str] = {}
    for segment_row in range(row, end + 1):
        segment_path = paths[segment_row]
        for suffix_depth in range(depth, len(segment_path)):
            old_id = segment_path[suffix_depth]["id"]
            replacement_key = (suffix_depth, old_id)
            replacement = replacements.get(replacement_key)
            if replacement is None:
                replacement = _new_unique_group_id(used_group_ids)
                replacements[replacement_key] = replacement
            segment_path[suffix_depth]["id"] = replacement


def rebuild_editor_group_identities(
    tasks: Sequence[Mapping[str, Any]],
    *,
    remap_all: bool = False,
    reserved_ids: Iterable[str] = (),
    namespace: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Canonicalize segments and optionally remap them deterministically.

    INVARIANT: one group identity represents one contiguous span under one
    parent. Reusing it for a separated span would visually merge unrelated
    steps and make later insertion ambiguous.
    """

    reserved_group_ids = _materialize_reserved_group_ids(reserved_ids)
    repaired = [copy.deepcopy(dict(task)) for task in tasks]
    paths = [
        _normalize_group_path_fields(task.get("editor_groups", []))
        for task in tasks
    ]
    used_group_ids = {
        group["id"] for path in paths for group in path
    } | set(reserved_group_ids)
    split_count = 0

    owners_by_id: dict[str, _EditorGroupOwner] = {}
    for row, path in enumerate(paths):
        for depth in range(len(path)):
            prefix = _group_prefix_identity(path, depth)
            if row:
                previous = paths[row - 1]
                if (
                    len(previous) > depth
                    and _group_prefix_identity(previous, depth) == prefix
                ):
                    continue

            end = row
            while end + 1 < len(paths):
                candidate = paths[end + 1]
                if (
                    len(candidate) <= depth
                    or _group_prefix_identity(candidate, depth) != prefix
                ):
                    break
                end += 1

            group_id = path[depth]["id"]
            owner = _group_owner(path, depth, row, end)
            if group_id in owners_by_id and owners_by_id[group_id] != owner:
                _split_conflicting_group_ids(paths, row, end, depth, used_group_ids)
                group_id = paths[row][depth]["id"]
                split_count += 1
            owners_by_id[group_id] = owner

    if remap_all:
        _apply_deterministic_group_remap(
            paths,
            owners_by_id,
            namespace=(
                _CANONICAL_REMAP_NAMESPACE if namespace is None else namespace
            ),
            reserved_ids=reserved_group_ids,
        )

    for row, path in enumerate(paths):
        repaired[row]["editor_groups"] = path
    return repaired, split_count


def repair_discontiguous_group_paths(
    tasks: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Split non-adjacent or metadata-conflicting group segments.

    Convenience wrapper around :func:`rebuild_editor_group_identities`
    without deterministic remapping. Use when loading or pasting tasks
    that may contain duplicated group IDs spanning non-contiguous rows.

    Args:
        tasks: Ordered sequence of task dicts to repair.

    Returns:
        Tuple of (repaired task dicts, number of splits performed).
    """

    return rebuild_editor_group_identities(tasks)


def remap_editor_group_identities(
    tasks: Sequence[Mapping[str, Any]],
    *,
    reserved_ids: Iterable[str] = (),
    namespace: str | None = None,
) -> list[dict[str, Any]]:
    """Deterministically remap all group IDs to canonical hash-based identities.

    Idempotent: applying this function twice with the same namespace yields
    identical results. Use for import/merge scenarios where group IDs from
    different sources must not collide.

    Args:
        tasks: Ordered sequence of task dicts whose group IDs to remap.
        reserved_ids: Group IDs that must not be produced by the remap.
        namespace: Remap namespace; defaults to the canonical namespace.

    Returns:
        New task dicts with remapped ``editor_groups`` paths.
    """

    remapped, _split_count = rebuild_editor_group_identities(
        tasks,
        remap_all=True,
        reserved_ids=reserved_ids,
        namespace=namespace,
    )
    return remapped


def isolate_editor_group_identities_for_insert(
    tasks: Sequence[Mapping[str, Any]],
    *,
    reserved_ids: Iterable[str] = (),
    namespace: str | None = None,
) -> list[dict[str, Any]]:
    """Remap group IDs into a fresh isolation namespace for clone/paste insertion.

    Each invocation generates a unique namespace (unless one is supplied),
    ensuring that inserted clones never share group identities with the
    source rows or with other concurrent paste batches.

    Args:
        tasks: Ordered sequence of cloned task dicts to isolate.
        reserved_ids: Group IDs already in use that must not be produced.
        namespace: Explicit isolation namespace; a random UUID4 hex is
            generated when None.

    Returns:
        New task dicts with isolated ``editor_groups`` paths.
    """

    operation_namespace = (
        new_editor_group_isolation_namespace()
        if namespace is None
        else str(namespace)
    )
    remapped, _split_count = rebuild_editor_group_identities(
        tasks,
        remap_all=True,
        reserved_ids=reserved_ids,
        namespace=f"{_ISOLATION_REMAP_PURPOSE}:{operation_namespace}",
    )
    return remapped


def editor_group_spans(
    tasks: list[Mapping[str, Any]],
    paths: list[list[dict[str, Any]]] | None = None,
) -> list[EditorGroupSpan]:
    """Return every contiguous group occurrence in flat workflow order.

    ``paths`` may supply already-normalized group paths (one per task) so a
    caller that also needs them does not normalize each task a second time.
    """

    if paths is None:
        paths = [normalize_group_path(task.get("editor_groups", [])) for task in tasks]
    spans: list[EditorGroupSpan] = []
    for row, path in enumerate(paths):
        for depth, group in enumerate(path):
            identity = tuple(item["id"] for item in path[: depth + 1])
            if row:
                previous = paths[row - 1]
                previous_identity = tuple(
                    item["id"] for item in previous[: depth + 1]
                )
                if len(previous) > depth and previous_identity == identity:
                    continue
            end = row
            while end + 1 < len(paths):
                candidate = paths[end + 1]
                candidate_identity = tuple(
                    item["id"] for item in candidate[: depth + 1]
                )
                if len(candidate) <= depth or candidate_identity != identity:
                    break
                end += 1
            spans.append(
                EditorGroupSpan(
                    group_id=group["id"],
                    name=group["name"],
                    depth=depth + 1,
                    start=row,
                    end=end,
                    collapsed=bool(group["collapsed"]),
                )
            )
    return spans


def build_group_presentations(
    tasks: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Precompute per-row group presentation metadata for the step list view.

    Uses a scan-line over group spans so each row is processed in O(depth)
    rather than O(spans). The result drives header rendering, indentation,
    collapse hiding and connector lines in the Qt step list.

    Args:
        tasks: Full ordered task list (only ``editor_groups`` is read).

    Returns:
        One dict per task row with keys: ``headers``, ``depth``,
        ``group_start``, ``group_end``, ``hide_for_collapse``,
        ``controls_hidden`` and ``collapsed_header_anchor``.
    """
    # M-30: 预计算一次归一化分组路径，editor_group_spans 与下方逻辑共用，
    # 避免对每个步骤重复调用 normalize_group_path（原先为 2N 次）。
    paths = [normalize_group_path(task.get("editor_groups", [])) for task in tasks]
    spans = editor_group_spans(tasks, paths)
    # L-38: 用扫描线预计算每行包含的分组，避免对每行线性扫描全部 spans。
    # 任一行的活跃分组数量不超过 MAX_GROUP_DEPTH（每个深度至多一个），
    # 因此活跃集合始终很小，整体复杂度约为 O(步骤数 × 分组深度)。
    start_events: dict[int, list[int]] = {}
    end_events: dict[int, list[int]] = {}
    for index, span in enumerate(spans):
        start_events.setdefault(span.start, []).append(index)
        end_events.setdefault(span.end + 1, []).append(index)
    active: set[int] = set()
    result: list[dict[str, Any]] = []
    for row, _task in enumerate(tasks):
        for index in end_events.get(row, ()):
            active.discard(index)
        for index in start_events.get(row, ()):
            active.add(index)
        containing = [spans[index] for index in active]
        collapsed = [span for span in containing if span.collapsed]
        headers = [span for span in containing if span.start == row]
        if collapsed:
            first_collapsed_depth = min(span.depth for span in collapsed)
            headers = [
                span for span in headers if span.depth <= first_collapsed_depth
            ]
        path = paths[row]
        result.append(
            {
                "headers": [
                    {
                        "id": span.group_id,
                        "name": span.name,
                        "depth": span.depth,
                        "count": span.count,
                        "collapsed": span.collapsed,
                    }
                    for span in sorted(headers, key=lambda item: item.depth)
                ],
                "depth": len(path),
                "group_start": any(span.start == row for span in containing),
                "group_end": any(span.end == row for span in containing),
                "hide_for_collapse": any(span.start != row for span in collapsed),
                "controls_hidden": any(span.start == row for span in collapsed),
                "collapsed_header_anchor": any(
                    span.start == row for span in collapsed
                ),
            }
        )
    return result


def normalize_step_note(value: Any) -> str:
    """Sanitize and bound a step note for safe storage and display.

    Strips embedded NUL characters, trims leading/trailing whitespace,
    and truncates to MAX_STEP_NOTE_LENGTH.

    Args:
        value: Raw note value from task data (any type accepted).

    Returns:
        Cleaned note string within the maximum length bound.
    """

    return str(value or "").replace("\x00", "").strip()[:MAX_STEP_NOTE_LENGTH]


def normalize_marker_color(value: Any) -> str:
    """Validate and normalize a step marker color identifier.

    Args:
        value: Raw color value from task data (any type accepted).

    Returns:
        The lowercase color ID if it is one of STEP_MARKER_COLORS,
        otherwise an empty string (meaning "no marker").
    """
    value = str(value or "").strip().lower()
    return value if value in _MARKER_COLOR_IDS else ""


def normalize_editor_metadata(task: Mapping[str, Any]) -> dict[str, Any]:
    """Extract and canonicalize all editor metadata fields from a task dict.

    Normalizes step_note, marker_color, editor_hidden, and editor_groups
    into their bounded, validated forms suitable for persistence or display.

    Args:
        task: Raw task dictionary potentially containing editor metadata.

    Returns:
        Dict with keys ``step_note``, ``marker_color``, ``editor_hidden``,
        and ``editor_groups`` in their canonical representations.
    """

    return {
        "step_note": normalize_step_note(task.get("step_note", "")),
        "marker_color": normalize_marker_color(task.get("marker_color", "")),
        "editor_hidden": config_bool(task.get("editor_hidden", False)),
        "editor_groups": normalize_group_path(task.get("editor_groups", [])),
    }
