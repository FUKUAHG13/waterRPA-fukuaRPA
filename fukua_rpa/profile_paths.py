"""Portable references for assets managed inside the fukuaRPA data folder."""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping, Sequence
from typing import Any

from .task_images import iter_task_image_refs

MANAGED_DATA_PREFIX = "fukua-data://"


def managed_path_reference(path: str, base_dir: str) -> str:
    """Encode paths under *base_dir* while leaving external paths absolute.

    Paths inside the managed data folder are stored as portable
    ``fukua-data://`` relative references so a profile keeps working when the
    data folder moves; paths outside are returned as absolute paths.

    Args:
        path: Filesystem path to encode.
        base_dir: Root of the managed data folder.

    Returns:
        A ``fukua-data://`` reference for paths inside *base_dir*, otherwise
        the absolute form of *path*.

    Raises:
        ValueError: If the relative form escapes the data folder.
    """

    absolute = os.path.abspath(path)
    root = os.path.abspath(base_dir)
    try:
        inside = os.path.commonpath((absolute, root)) == root
    except ValueError:
        inside = False
    if not inside:
        return absolute
    relative = os.path.relpath(absolute, root).replace("\\", "/")
    if relative in ("", ".") or relative.startswith("../"):
        raise ValueError("托管资源路径无效")
    return MANAGED_DATA_PREFIX + relative


def resolve_profile_path(value: object, base_dir: str) -> str:
    """Resolve a stored path value to a concrete filesystem path.

    ``fukua-data://`` references are joined onto *base_dir* and validated to
    stay inside it; any other value is returned unchanged (stripped) so
    external absolute paths pass through untouched.

    Args:
        value: Stored path string, possibly a ``fukua-data://`` reference.
        base_dir: Root of the managed data folder.

    Returns:
        The resolved absolute path for managed references, or the original
        text for external paths.

    Raises:
        ValueError: If a managed reference contains unsafe segments or escapes
            the data folder.
    """
    text = str(value or "").strip()
    if not text.startswith(MANAGED_DATA_PREFIX):
        return text
    relative = text[len(MANAGED_DATA_PREFIX) :].replace("\\", "/")
    parts = relative.split("/")
    if not relative or any(part in ("", ".", "..") for part in parts):
        raise ValueError("托管资源引用包含不安全路径")
    root = os.path.abspath(base_dir)
    resolved = os.path.abspath(os.path.join(root, *parts))
    try:
        if os.path.commonpath((resolved, root)) != root:
            raise ValueError("托管资源引用越过数据目录")
    except ValueError as error:
        raise ValueError("托管资源引用越过数据目录") from error
    return resolved


def resolve_task_image_paths(
    tasks: Sequence[Mapping[str, Any]] | None, base_dir: str
) -> list[Any]:
    """Detach tasks and resolve only fields whose command semantics are images.

    Args:
        tasks: Task dicts to copy and resolve; ``None`` yields an empty list.
        base_dir: Root of the managed data folder.

    Returns:
        A deep-copied list of tasks with image fields resolved through
        ``resolve_profile_path``; non-dict entries are preserved as-is.
    """
    prepared = copy.deepcopy(list(tasks or []))
    for task in prepared:
        if not isinstance(task, dict):
            continue
        for _kind, container, key, path in iter_task_image_refs(task):
            container[key] = resolve_profile_path(path, base_dir)
    return prepared
