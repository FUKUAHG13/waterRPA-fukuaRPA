"""Bounded JSON parsing and iterative structure validation."""

from __future__ import annotations

import json
import math
import os
from typing import Any, cast

from .constants import MAX_JSON_NESTING_DEPTH, MAX_JSON_NODES

DEFAULT_MAX_DEPTH = MAX_JSON_NESTING_DEPTH
DEFAULT_MAX_NODES = MAX_JSON_NODES


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复字段：{key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError(f"JSON 包含无效数字：{value}")


def validate_json_structure(
    value: Any,
    *,
    label: str = "JSON",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> None:
    """Validate that a value is JSON-serializable within depth and node bounds.

    Uses an explicit stack instead of recursion so that deeply nested
    structures cannot trigger a RecursionError in the validator itself.

    Args:
        value: The Python object to validate (dict, list, str, int, etc.).
        label: Human-readable label used in error messages.
        max_depth: Maximum allowed nesting depth.
        max_nodes: Maximum allowed total node count.

    Raises:
        ValueError: If the structure exceeds depth/node limits, contains
            circular references, non-string dict keys, NaN/Infinity floats,
            or types that cannot be serialized to JSON.
    """

    depth_limit = max(1, int(max_depth))
    node_limit = max(1, int(max_nodes))
    active_containers: set[int] = set()
    stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
    nodes = 0
    while stack:
        current, depth, leaving = stack.pop()
        if leaving:
            active_containers.discard(id(current))
            continue
        nodes += 1
        if nodes > node_limit:
            raise ValueError(f"{label}节点数量超过上限 {node_limit}")
        if depth > depth_limit:
            raise ValueError(f"{label}嵌套层级超过上限 {depth_limit}")
        if isinstance(current, dict):
            mapping = cast("dict[Any, Any]", current)
            if any(not isinstance(key, str) for key in mapping):
                raise ValueError(f"{label}包含非文本字段名")
            identity = id(mapping)
            if identity in active_containers:
                raise ValueError(f"{label}包含循环引用")
            active_containers.add(identity)
            stack.append((mapping, depth, True))
            stack.extend(
                (child, depth + 1, False) for child in mapping.values()
            )
        elif isinstance(current, (list, tuple)):
            sequence: list[Any] | tuple[Any, ...] = cast(
                "list[Any] | tuple[Any, ...]", current
            )
            identity = id(sequence)
            if identity in active_containers:
                raise ValueError(f"{label}包含循环引用")
            active_containers.add(identity)
            stack.append((sequence, depth, True))
            stack.extend((child, depth + 1, False) for child in sequence)
        elif current is None or isinstance(current, (str, bool, int)):
            continue
        elif isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError(f"{label}包含 NaN 或无穷大")
        else:
            raise ValueError(
                f"{label}包含无法序列化的类型：{type(current).__name__}"
            )


def loads_bounded_json(
    value: Any,
    *,
    max_bytes: int,
    label: str = "JSON",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> Any:
    """Parse JSON only after enforcing byte size and duplicate-key limits.

    Accepts raw bytes, a string, or an already-parsed Python object. Bytes and
    strings are decoded/parsed with duplicate-key rejection and constant
    rejection (NaN, Infinity). Already-parsed objects are validated in-place
    via :func:`validate_json_structure`.

    Args:
        value: Raw bytes, a JSON string, or a pre-parsed Python object.
        max_bytes: Maximum allowed payload size in bytes.
        label: Human-readable label used in error messages.
        max_depth: Maximum allowed nesting depth.
        max_nodes: Maximum allowed total node count.

    Returns:
        The parsed (or validated) Python object.

    Raises:
        ValueError: If the payload exceeds the byte limit, is not valid UTF-8,
            contains duplicate keys, has invalid syntax, or fails structural
            validation.
    """

    byte_limit = max(1, int(max_bytes))
    if isinstance(value, bytes):
        raw = value
        if len(raw) > byte_limit:
            raise ValueError(f"{label}大小超过上限 {byte_limit} 字节")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError(f"{label}不是有效的 UTF-8 文本") from error
    elif isinstance(value, str):
        if len(value) > byte_limit:
            raise ValueError(f"{label}大小超过上限 {byte_limit} 字节")
        raw = value.encode("utf-8")
        if len(raw) > byte_limit:
            raise ValueError(f"{label}大小超过上限 {byte_limit} 字节")
        text = value.lstrip("\ufeff")
    else:
        validate_json_structure(
            value,
            label=label,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
        return value

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except RecursionError as error:
        raise ValueError(f"{label}嵌套层级过深") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"{label}格式无效（第 {error.lineno} 行）") from error
    validate_json_structure(
        parsed,
        label=label,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    return parsed


def load_bounded_json_file(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    label: str = "JSON 文件",
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> Any:
    """Read and parse a JSON file with size and structural bounds.

    Checks the file size before reading to avoid loading excessively large
    payloads into memory, then delegates to :func:`loads_bounded_json` for
    parsing and structural validation.

    Args:
        path: Path to the JSON file.
        max_bytes: Maximum allowed file size in bytes.
        label: Human-readable label used in error messages.
        max_depth: Maximum allowed nesting depth.
        max_nodes: Maximum allowed total node count.

    Returns:
        The parsed Python object.

    Raises:
        ValueError: If the file exceeds the byte limit or fails JSON parsing
            and structural validation.
        OSError: If the file cannot be opened or read.
    """
    byte_limit = max(1, int(max_bytes))
    if os.path.getsize(path) > byte_limit:
        raise ValueError(f"{label}大小超过上限 {byte_limit} 字节")
    with open(path, "rb") as handle:
        raw = handle.read(byte_limit + 1)
    return loads_bounded_json(
        raw,
        max_bytes=byte_limit,
        label=label,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
