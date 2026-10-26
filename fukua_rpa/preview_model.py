"""Pure coordinate preview planning for clicks, paths, hovers and drags."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .commands import CLICK_COMMANDS, COMMAND_BY_NAME
from .constants import TASK_TYPE_LEFT_DRAG, TASK_TYPE_RIGHT_DRAG
from .task_model import (
    build_coord_step_positions,
    config_bool,
    normalize_coord_step_direction,
    parse_coord_step_manual_points,
    parse_coordinate_sequence,
    parse_coordinate_text,
    parse_float_text,
    serialize_coord_step_manual_points,
    serialize_coordinate_sequence,
    task_is_disabled,
)
from .workflow_metadata import normalize_group_path

# Persisted command code for mouse hover, derived from the single source of
# truth in commands.py COMMAND_SPECS.
TASK_TYPE_HOVER = COMMAND_BY_NAME["鼠标悬停"].code


def _clamped_int(value: Any, default: int) -> int:
    """Parse an int-like value, rejecting non-finite floats.

    This helper remains local because some preview options are integer-like
    text fields. It also protects callers that pass a raw value instead of
    going through the shared task parser.
    """
    try:
        raw = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    if not math.isfinite(raw):
        return default
    try:
        return int(raw)
    except (OverflowError, ValueError):
        return default


@dataclass(frozen=True)
class PreviewTaskRef:
    """Stable reference to one task represented by a preview point."""

    task_index: int
    step_id: str = ""


@dataclass(frozen=True)
class PreviewPointSource:
    """Describe how one preview point maps back to persisted task data."""

    task: PreviewTaskRef
    kind: str
    point_index: int = -1
    group_path: tuple[str, ...] = ()
    editable: bool = False


@dataclass(frozen=True)
class PreviewPlan:
    """Immutable, pure-data result of coordinate preview planning.

    Holds the preview points, their display labels, the connecting line
    segments for canvas rendering, and a flag indicating whether the plan
    was truncated to respect the point limit.
    """

    points: tuple[tuple[float, float], ...]
    labels: tuple[str, ...]
    line_segments: tuple[Mapping[str, Any], ...]
    truncated: bool = False
    point_sources: tuple[PreviewPointSource, ...] = ()


def coordinate_preview_options(task: Mapping[str, Any]) -> dict[str, Any]:
    """Extract coordinate stepping parameters from a raw task dictionary.

    Normalizes step direction, distance, manual points, and limits into
    an options dict consumed by ``build_coord_step_positions``.

    Args:
        task: Raw task dictionary containing coord_step_* fields.

    Returns:
        Normalized options dictionary for preview point generation.
    """
    return {
        "every": max(1, _clamped_int(task.get("coord_step_every", 1), 1)),
        "direction": str(task.get("coord_step_direction", "向下")),
        "distance": parse_float_text(task.get("coord_step_distance", 0), 0.0),
        "dx": parse_float_text(task.get("coord_step_dx", 0), 0.0),
        "dy": parse_float_text(task.get("coord_step_dy", 0), 0.0),
        "point": str(task.get("coord_step_point", "")).strip(),
        "max_steps": max(
            0, _clamped_int(task.get("coord_step_max_steps", 0), 0)
        ),
        "max_distance": max(
            0.0, parse_float_text(task.get("coord_step_max_distance", 0), 0.0)
        ),
        "reset_after": max(
            0, _clamped_int(task.get("coord_step_reset_after", 0), 0)
        ),
        "manual_points": parse_coord_step_manual_points(
            task.get("coord_step_manual_points", "{}")
        ),
    }


def build_preview_line_segments(
    step_groups: Sequence[Mapping[str, Any]],
    internal_segments: Sequence[Mapping[str, Any]] | None = None,
) -> list[Mapping[str, Any]]:
    """Construct line segment descriptors connecting preview point groups.

    Solid segments link representative points of consecutive groups;
    dashed segments connect extra (non-representative) points to their
    neighboring group representatives.

    Args:
        step_groups: List of group dicts, each with a ``"rep"`` point index
            and optional ``"extras"`` list of additional point indices.
        internal_segments: Pre-built segments (e.g. drag start-to-end) that
            are included before group-derived segments.

    Returns:
        List of segment dicts with ``"from"``, ``"to"``, and ``"style"`` keys.
    """
    groups = [group for group in step_groups if group.get("rep") is not None]
    segments: list[Mapping[str, Any]] = list(internal_segments or [])
    for index in range(len(groups) - 1):
        segments.append(
            {"from": groups[index]["rep"], "to": groups[index + 1]["rep"], "style": "solid"}
        )
    for index, group in enumerate(groups):
        extras = group.get("extras", [])
        if not extras:
            continue
        previous = groups[index - 1]["rep"] if index > 0 else group["rep"]
        following = groups[index + 1]["rep"] if index + 1 < len(groups) else group["rep"]
        for extra in extras:
            if previous != extra:
                segments.append({"from": previous, "to": extra, "style": "dash"})
            if following != extra and following != previous:
                segments.append({"from": extra, "to": following, "style": "dash"})
    return segments


_UNGROUPED_SCOPE = ("__preview_ungrouped__",)


def _shared_movement_scope(
    left_path: tuple[str, ...], right_path: tuple[str, ...]
) -> tuple[str, ...] | None:
    """Return the deepest group shared by both segment endpoints.

    DESIGN: all top-level ungrouped steps form one implicit peer group. A
    segment crossing between that implicit group and a real top-level group,
    or between two different top-level groups, is display-only.
    """

    if not left_path and not right_path:
        return _UNGROUPED_SCOPE
    if not left_path or not right_path:
        return None
    shared: list[str] = []
    for left, right in zip(left_path, right_path, strict=False):
        if left != right:
            break
        shared.append(left)
    return tuple(shared) or None


def _annotate_line_movement(
    segments: Sequence[Mapping[str, Any]],
    step_groups: Sequence[Mapping[str, Any]],
    *,
    truncated: bool,
) -> list[Mapping[str, Any]]:
    """Attach immutable movement scopes to rendered preview segments."""

    point_owners: dict[int, Mapping[str, Any]] = {}
    for group in step_groups:
        for point_index in group.get("all", ()):
            point_owners[int(point_index)] = group

    annotated: list[Mapping[str, Any]] = []
    for raw_segment in segments:
        segment = dict(raw_segment)
        try:
            left = point_owners[int(segment["from"])]
            right = point_owners[int(segment["to"])]
        except (KeyError, TypeError, ValueError):
            annotated.append(segment)
            continue

        scope = _shared_movement_scope(
            tuple(left.get("group_path", ())),
            tuple(right.get("group_path", ())),
        )
        if truncated:
            segment["blocked_reason"] = "truncated"
            annotated.append(segment)
            continue
        if scope is None:
            segment["blocked_reason"] = "cross_group"
            annotated.append(segment)
            continue

        if scope == _UNGROUPED_SCOPE:
            included = [
                group
                for group in step_groups
                if not tuple(group.get("group_path", ()))
            ]
        else:
            included = [
                group
                for group in step_groups
                if tuple(group.get("group_path", ()))[: len(scope)] == scope
            ]

        move_indices = tuple(
            dict.fromkeys(
                int(point_index)
                for group in included
                for point_index in group.get("all", ())
            )
        )
        move_task_refs = tuple(
            dict.fromkeys(
                group["task_ref"]
                for group in included
                if isinstance(group.get("task_ref"), PreviewTaskRef)
            )
        )
        if not move_indices or not move_task_refs:
            segment["blocked_reason"] = "not_editable"
            annotated.append(segment)
            continue
        segment["move_indices"] = move_indices
        segment["move_task_refs"] = move_task_refs
        segment["scope_path"] = scope
        annotated.append(segment)
    return annotated


def _resolved_task_index(
    tasks: Sequence[Mapping[str, Any]], task_ref: PreviewTaskRef
) -> int | None:
    if task_ref.step_id:
        for index, task in enumerate(tasks):
            if str(task.get("step_id", "")) == task_ref.step_id:
                return index
        return None
    index = int(task_ref.task_index)
    return index if 0 <= index < len(tasks) else None


def _rounded_coordinate(point: tuple[float, float]) -> tuple[int, int]:
    return int(round(float(point[0]))), int(round(float(point[1])))


def _coordinate_text(point: tuple[float, float]) -> str:
    x, y = _rounded_coordinate(point)
    return f"{x},{y}"


def apply_preview_point_position(
    tasks: Sequence[Mapping[str, Any]],
    source: PreviewPointSource,
    point: tuple[float, float],
) -> list[dict[str, Any]]:
    """Return a task snapshot with one uniquely writable preview point moved."""

    updated = [copy.deepcopy(dict(task)) for task in tasks]
    if not source.editable:
        return updated
    task_index = _resolved_task_index(updated, source.task)
    if task_index is None:
        return updated
    task = updated[task_index]
    coordinate = _rounded_coordinate(point)

    if source.kind == "value":
        task["value"] = _coordinate_text(coordinate)
    elif source.kind == "sequence":
        points = parse_coordinate_sequence(task.get("coord_sequence_points", ""))
        if not 0 <= source.point_index < len(points):
            return updated
        points[source.point_index] = coordinate
        task["coord_sequence_points"] = serialize_coordinate_sequence(points)
    else:
        return updated
    return updated


def _shift_point(point: tuple[float, float], dx: float, dy: float) -> tuple[int, int]:
    return _rounded_coordinate((point[0] + dx, point[1] + dy))


def _translate_task_preview_coordinates(
    task: dict[str, Any], dx: float, dy: float
) -> None:
    try:
        command = float(task.get("type", 0))
    except (TypeError, ValueError):
        return

    coordinate = parse_coordinate_text(task.get("value", ""))
    if command in CLICK_COMMANDS and coordinate:
        if config_bool(task.get("coord_sequence_en", False)):
            points = parse_coordinate_sequence(task.get("coord_sequence_points", ""))
            if points:
                task["coord_sequence_points"] = serialize_coordinate_sequence(
                    [_shift_point(point, dx, dy) for point in points]
                )
            return

        task["value"] = _coordinate_text(_shift_point(coordinate, dx, dy))
        if not config_bool(task.get("coord_step_en", False)):
            return
        # FR-20261008-302: compare through the shared normaliser so a legacy
        # mojibake alias for 「移动到新点位」 keeps translating the target point on
        # drag, exactly like the validated/previewed direction.
        if (
            normalize_coord_step_direction(
                str(task.get("coord_step_direction", ""))
            )
            != "移动到新点位"
        ):
            return
        target = parse_coordinate_text(task.get("coord_step_point", ""))
        if target:
            task["coord_step_point"] = _coordinate_text(
                _shift_point(target, dx, dy)
            )
        manual_points = parse_coord_step_manual_points(
            task.get("coord_step_manual_points", "{}")
        )
        if manual_points:
            task["coord_step_manual_points"] = serialize_coord_step_manual_points(
                {
                    index: _shift_point(point, dx, dy)
                    for index, point in manual_points.items()
                }
            )
        return

    if command == TASK_TYPE_HOVER and coordinate:
        task["value"] = _coordinate_text(_shift_point(coordinate, dx, dy))
        return

    if command not in (TASK_TYPE_LEFT_DRAG, TASK_TYPE_RIGHT_DRAG):
        return
    parts = str(task.get("value", "")).split("->")
    if len(parts) != 2:
        return
    start = parse_coordinate_text(parts[0])
    end = parse_coordinate_text(parts[1])
    if not start or not end:
        return
    task["value"] = (
        f"{_coordinate_text(_shift_point(start, dx, dy))} -> "
        f"{_coordinate_text(_shift_point(end, dx, dy))}"
    )


def translate_preview_task_positions(
    tasks: Sequence[Mapping[str, Any]],
    task_refs: Sequence[PreviewTaskRef],
    dx: float,
    dy: float,
) -> list[dict[str, Any]]:
    """Translate every statically previewed coordinate owned by a scope."""

    updated = [copy.deepcopy(dict(task)) for task in tasks]
    resolved = {
        index
        for task_ref in task_refs
        if (index := _resolved_task_index(updated, task_ref)) is not None
    }
    for index in sorted(resolved):
        _translate_task_preview_coordinates(updated[index], dx, dy)
    return updated


def build_coordinate_preview(
    tasks: Sequence[Mapping[str, Any]], max_points: int = 800
) -> PreviewPlan:
    """Build a pure-data preview plan of all coordinate targets in a workflow.

    Scans enabled tasks for clicks, hovers, and drags; expands coordinate
    sequences and stepping into individual preview points; and computes
    connecting line segments for canvas rendering.

    Args:
        tasks: Ordered sequence of raw task dictionaries.
        max_points: Upper bound on preview points before truncation.

    Returns:
        Immutable ``PreviewPlan`` with points, labels, segments, and a
        truncation flag.
    """
    limit = max(1, int(max_points))
    points: list[tuple[float, float]] = []
    labels: list[str] = []
    point_sources: list[PreviewPointSource] = []
    step_groups: list[dict[str, Any]] = []
    internal_segments: list[dict[str, Any]] = []
    truncated = False

    def add_point(
        point: tuple[float, float],
        label: object,
        source: PreviewPointSource,
    ) -> int | None:
        nonlocal truncated
        if truncated:
            return None
        try:
            x, y = float(point[0]), float(point[1])
        except (IndexError, OverflowError, TypeError, ValueError):
            return None
        if not math.isfinite(x) or not math.isfinite(y):
            return None
        points.append((x, y))
        labels.append(str(label))
        point_sources.append(source)
        point_index = len(points) - 1
        if len(points) >= limit:
            truncated = True
        return point_index

    def add_step_group(
        representative: int | None,
        task_ref: PreviewTaskRef,
        group_path: tuple[str, ...],
        extras: Sequence[int] | None = None,
        all_indices: Sequence[int] | None = None,
    ) -> None:
        if representative is not None:
            step_groups.append(
                {
                    "rep": representative,
                    "extras": list(extras or []),
                    "all": list(all_indices or (representative,)),
                    "task_ref": task_ref,
                    "group_path": group_path,
                }
            )

    def finish() -> PreviewPlan:
        segments = build_preview_line_segments(step_groups, internal_segments)
        segments = _annotate_line_movement(
            segments,
            step_groups,
            truncated=truncated,
        )
        return PreviewPlan(
            tuple(points),
            tuple(labels),
            tuple(segments),
            truncated,
            tuple(point_sources),
        )

    for task_index, task in enumerate(tasks, 1):
        if task_is_disabled(task):
            continue
        task_ref = PreviewTaskRef(
            task_index=task_index - 1,
            step_id=str(task.get("step_id", "")),
        )
        group_path = tuple(
            group["id"]
            for group in normalize_group_path(task.get("editor_groups", []))
        )

        def point_source(
            kind: str,
            *,
            point_index: int = -1,
            editable: bool = False,
            task_ref: PreviewTaskRef = task_ref,
            group_path: tuple[str, ...] = group_path,
        ) -> PreviewPointSource:
            return PreviewPointSource(
                task=task_ref,
                kind=kind,
                point_index=point_index,
                group_path=group_path,
                editable=editable,
            )

        try:
            command = float(task.get("type", 0))
        except (TypeError, ValueError):
            continue
        coordinate = parse_coordinate_text(task.get("value", ""))
        if command in CLICK_COMMANDS and coordinate:
            if config_bool(task.get("coord_sequence_en", False)):
                indices = []
                for sequence_index, point in enumerate(
                    parse_coordinate_sequence(task.get("coord_sequence_points", "")), 1
                ):
                    point_index = add_point(
                        point,
                        f"{task_index}序{sequence_index}",
                        point_source(
                            "sequence",
                            point_index=sequence_index - 1,
                            editable=True,
                        ),
                    )
                    if point_index is not None:
                        indices.append(point_index)
                    if truncated:
                        add_step_group(
                            indices[0] if indices else None,
                            task_ref,
                            group_path,
                            indices[1:],
                            indices,
                        )
                        return finish()
                add_step_group(
                    indices[0] if indices else None,
                    task_ref,
                    group_path,
                    indices[1:],
                    indices,
                )
                continue

            step_points = [coordinate]
            stepping = config_bool(task.get("coord_step_en", False))
            if stepping:
                step_points = build_coord_step_positions(
                    coordinate[0],
                    coordinate[1],
                    coordinate_preview_options(task),
                    max_points=120,
                )
            indices = []
            for point_index, point in enumerate(step_points, 1):
                label = f"{task_index}" if len(step_points) == 1 else f"{task_index}-{point_index}"
                added = add_point(
                    point,
                    label,
                    point_source("derived" if stepping else "value", editable=not stepping),
                )
                if added is not None:
                    indices.append(added)
                if truncated:
                    add_step_group(
                        indices[0] if indices else None,
                        task_ref,
                        group_path,
                        indices[1:],
                        indices,
                    )
                    return finish()
            add_step_group(
                indices[0] if indices else None,
                task_ref,
                group_path,
                indices[1:],
                indices,
            )
            continue

        if command == TASK_TYPE_HOVER and coordinate:
            added = add_point(
                coordinate,
                f"{task_index}悬",
                point_source("derived"),
            )
            add_step_group(added, task_ref, group_path)
            if truncated:
                return finish()
            continue

        if command in (TASK_TYPE_LEFT_DRAG, TASK_TYPE_RIGHT_DRAG):
            parts = str(task.get("value", "")).split("->")
            if len(parts) != 2:
                continue
            start = parse_coordinate_text(parts[0])
            end = parse_coordinate_text(parts[1])
            if not start or not end:
                continue
            prefix = (
                f"{task_index}左拖"
                if command == TASK_TYPE_LEFT_DRAG
                else f"{task_index}右拖"
            )
            start_index = add_point(
                start,
                f"{prefix}起",
                point_source("derived"),
            )
            if truncated:
                add_step_group(start_index, task_ref, group_path)
                return finish()
            end_index = add_point(
                end,
                f"{prefix}终",
                point_source("derived"),
            )
            if start_index is not None and end_index is not None:
                internal_segments.append(
                    {"from": start_index, "to": end_index, "style": "solid"}
                )
            all_indices = [
                index
                for index in (start_index, end_index)
                if index is not None
            ]
            add_step_group(
                start_index,
                task_ref,
                group_path,
                all_indices=all_indices,
            )
            if truncated:
                return finish()
    return finish()
