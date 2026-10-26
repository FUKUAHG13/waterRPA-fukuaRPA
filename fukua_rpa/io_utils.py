"""统一 IO 工具：原子写入与文件哈希。

io_utils.hash_file 是文件哈希的唯一规范实现；config_store、scheduled_task_store、integrity、scripts/hashutil、plugins/tools 等均已去重统一到此。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from typing import Any


def atomic_write_json(path: str | os.PathLike[str], data: Any, *, indent: int | None = 2, ensure_ascii: bool = False, restrict_acl: bool = False, newline: str | None = None) -> None:
    """Atomically write data as JSON to the specified path.

    Uses mkstemp + write + fsync + os.replace to guarantee that readers
    never see a partially-written file.

    When *restrict_acl* is true, the target file receives owner-only
    permissions (0o600) after the atomic replace as defense-in-depth for
    credential files; DPAPI user-binding remains the primary protection.

    When *newline* is ``""``, no newline translation is applied, so the file
    bytes equal the serialized text on every platform. Callers that must
    compare a projected size against the on-disk size (credentials,
    FR-20261011-010) rely on this; the default ``None`` keeps the platform's
    text-mode translation for every other caller.

    Args:
        path: Destination file path. Parent directories are created if absent.
        data: JSON-serializable data to write.
        indent: JSON indentation level (default 2).
        ensure_ascii: Whether to escape non-ASCII characters.
        restrict_acl: If true, chmod the result to 0o600 (best-effort on
            Windows where DPAPI already provides user-level isolation).
        newline: Newline translation passed to ``open``; ``""`` disables it.

    Raises:
        OSError: If the write, fsync, or atomic replace fails.
    """
    path = os.fspath(path)
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if restrict_acl:
            # 权限收敛为纵深防御；Windows 上 DPAPI 已做用户绑定兜底，
            # 单平台 chmod 失败不应阻断写入。
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def atomic_write_text(path: str | os.PathLike[str], text: str, *, encoding: str = "utf-8", newline: str | None = None) -> None:
    """Atomically write plain text to a file.

    Follows the same mkstemp + write + fsync + os.replace strategy as
    :func:`atomic_write_json`, ensuring readers never observe a partial file.

    Args:
        path: Destination file path. Parent directories are created if absent.
        text: The text content to write.
        encoding: File encoding (default UTF-8).
        newline: Newline translation mode passed to :func:`open`.

    Raises:
        OSError: If the write, fsync, or atomic replace fails.
    """
    path = os.fspath(path)
    dir_name = os.path.dirname(path) or "."
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def hash_file(
    path: str | os.PathLike[str],
    *,
    chunk_size: int = 1024 * 1024,
    algorithm: str = "sha256",
    cancelled: Callable[[], bool] | None = None,
) -> str:
    """Compute the hash digest of a file using chunked reads.

    This is the single canonical file-hashing implementation for the project;
    config_store, scheduled_task_store, integrity, scripts/hashutil, and
    plugins/tools all delegate here.

    Args:
        path: Path to the file to hash.
        chunk_size: Read buffer size in bytes (default 1 MiB).
        algorithm: Any algorithm accepted by :func:`hashlib.new` (default
            ``"sha256"``).
        cancelled: Optional callback polled after each chunk; if it returns a
            truthy value the operation is aborted.

    Returns:
        The lowercase hex digest of the file content.

    Raises:
        RuntimeError: If *cancelled* returns true mid-read.
        OSError: If the file cannot be opened or read.
    """
    h = hashlib.new(algorithm)
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
            if cancelled is not None and cancelled():
                raise RuntimeError("hashing cancelled")
    return h.hexdigest()
