"""Shared hashing utilities for build/release scripts.

Delegates to fukua_rpa.io_utils for the core atomic-write and hashing
primitives; keeps thin wrappers to preserve the scripts' existing interface
(atomic_write_json appends a trailing newline).
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fukua_rpa.io_utils import (  # noqa: E402
    atomic_write_text,
    hash_file,
)

__all__ = [
    "atomic_write_json",
    "atomic_write_text",
    "hash_file",
    "hash_source_text",
]


def atomic_write_json(path, data) -> None:
    """Atomically write JSON data to a file (with trailing newline)."""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def hash_source_text(data: bytes) -> tuple[str, int]:
    """Hash source bytes with newlines normalized to LF; return (sha256, size).

    FR-SCOPE010-001: the source fingerprint must identify the **commit**, not
    the **checkout**. Git stores blobs with LF, but `core.autocrlf` may hand the
    working tree CRLF, so hashing raw working-tree bytes made one commit yield
    different `source_sha256`/`build_id` depending on how it was checked out
    (measured: `native_core/build_native_core.ps1` had 75 CRLF in the working
    tree and 0 in the HEAD blob). Normalizing first makes the fingerprint
    newline-insensitive, matching how git treats text files.

    The returned size describes the **same normalized bytes** as the digest, so
    a recorded `(sha256, size)` pair stays internally consistent and verifiable
    from any checkout.

    Scope: only the source fingerprint normalizes. `fukua_rpa.io_utils.hash_file`
    deliberately stays a raw byte hash — it also backs credential stores,
    integrity manifests and plugin payloads, where rewriting bytes before
    hashing would be a correctness bug. Binary assets are normalized too; the
    transformation is deterministic and git checks such files out byte-identical
    everywhere, so the "same commit ⇒ same fingerprint" property still holds.
    """
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest(), len(normalized)

