"""Read a ZIP archive's declared member count without opening the archive.

Both package importers (``profile_package`` for profile packages and
``plugin_package`` for plugin packages) must enforce their member-count limit
**before** ``zipfile.ZipFile`` materializes the central directory: the stdlib
constructor turns every entry into a ``ZipInfo`` first, so a package declaring a
huge number of members allocates them all before any caller can apply a limit
(design §导出侧的结构校验 / DOC-SCOPE005-001, ARCHITECTURE.md #14).

This module is the single implementation of that pre-read so the two importers
cannot drift apart.

Contract
--------
``declared_member_count`` returns the count declared by the end-of-central-
directory (EOCD) record, or ``None`` when there is no usable EOCD record at all
(short file, or the signature is payload bytes). When the EOCD declares ZIP64
(``count == 0xFFFF``) but the ZIP64 records cannot be read, it raises
:class:`Zip64RecordUnreadable`: such a file is malformed or fabricated, and the
caller must reject it instead of falling back to materializing the directory.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = [
    "CentralDirectoryUnreadable",
    "PackageTooLarge",
    "Zip64RecordUnreadable",
    "bounded_precheck",
    "declared_member_count",
    "read_declared_member_count",
]

ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
ZIP64_EOCD_LOCATOR_SIGNATURE = b"PK\x06\x07"
ZIP_EOCD_STRUCT_SIZE = 22
ZIP_EOCD_MAX_COMMENT_BYTES = 0xFFFF
ZIP64_EOCD_LOCATOR_SIZE = 20
ZIP64_EOCD_MIN_SIZE = 56
ZIP64_ENTRY_SENTINEL = 0xFFFF

# Central-directory geometry used by the bounded pre-check.
CD_SIGNATURE = b"PK\x01\x02"
CD_HEADER_SIZE = 46
#: Smallest possible central-directory record: the 46-byte fixed header plus a
#: one-byte name and zero extra/comment fields. Used to derive an upper bound on
#: the member count from the declared directory size alone.
CD_MIN_RECORD_SIZE = CD_HEADER_SIZE + 2
#: Hard ceiling on how much of the central directory the pre-check will read.
#: Scanning stops as soon as the member budget is exceeded, so this only bounds
#: pathological inputs (a directory padded with huge names/comments) rather than
#: normal packages.
CD_SCAN_BYTE_BUDGET = 8 * 1024 * 1024

ZIP64_UNREADABLE_MESSAGE = (
    "压缩包声明 ZIP64 但 ZIP64 目录记录不可读取，已拒绝（避免先物化中央目录）"
)

CD_UNREADABLE_MESSAGE = "压缩包中央目录结构无法校验，已拒绝（避免先物化中央目录）"


class Zip64RecordUnreadable(ValueError):
    """The EOCD declares ZIP64 but the ZIP64 records cannot be read."""


class CentralDirectoryUnreadable(ValueError):
    """The central directory is truncated, inconsistent, or self-contradicting."""


class PackageTooLarge(ValueError):
    """The archive provably exceeds ``max_members`` members."""

    def __init__(self, message: str, *, declared: int | None) -> None:
        super().__init__(message)
        self.declared = declared


def declared_member_count(zip_path: str | os.PathLike[str]) -> int | None:
    """Return the EOCD-declared member count, or ``None`` when there is no EOCD.

    Raises:
        Zip64RecordUnreadable: If the EOCD declares ZIP64 but the ZIP64 records
            cannot be located or validated.
        OSError: If the file cannot be opened or read.
    """
    with open(zip_path, "rb") as handle:
        return read_declared_member_count(handle)


def read_declared_member_count(handle: Any) -> int | None:
    """Return the EOCD-declared member count read from an open binary *handle*.

    ``None`` means "no usable EOCD record"; see the module docstring.
    """
    geometry = _read_eocd_geometry(handle)
    return None if geometry is None else geometry[0]


def _read_eocd_geometry(handle: Any) -> tuple[int, int, int] | None:
    """Return ``(member_count, cd_offset, cd_size)`` from the trailing records.

    ``None`` means "no usable EOCD record". ``member_count`` is the ZIP64 count
    when the archive declares ZIP64. Raises :class:`Zip64RecordUnreadable` when
    ZIP64 is declared but its records cannot be validated.
    """
    handle.seek(0, os.SEEK_END)
    file_size = handle.tell()
    window = min(file_size, ZIP_EOCD_STRUCT_SIZE + ZIP_EOCD_MAX_COMMENT_BYTES)
    if window < ZIP_EOCD_STRUCT_SIZE:
        return None
    handle.seek(file_size - window)
    tail = handle.read(window)
    index = tail.rfind(ZIP_EOCD_SIGNATURE)
    if index < 0:
        return None
    record = tail[index:]
    if len(record) < ZIP_EOCD_STRUCT_SIZE:
        return None
    # The record must end the file (followed only by its own comment). Otherwise
    # the signature we found is payload bytes, not the directory record.
    comment_length = int.from_bytes(record[20:22], "little")
    if index + ZIP_EOCD_STRUCT_SIZE + comment_length != len(tail):
        return None
    total = int.from_bytes(record[10:12], "little")
    cd_size = int.from_bytes(record[12:16], "little")
    cd_offset = int.from_bytes(record[16:20], "little")
    if total != ZIP64_ENTRY_SENTINEL:
        # FR-20261008-201: ``cd_offset`` is relative to the *archive* start, not
        # to the file. ``zipfile`` recovers the real position with
        # ``concat = eocd_location - size_cd - offset_cd`` (then
        # ``start_dir = offset_cd + concat``); this module must do the same or a
        # legal archive carrying a prepended stub (self-extracting style) or a
        # ``cat a.zip b.zip`` concatenation is rejected as unreadable even though
        # the stdlib opens it fine. The EOCD record's absolute location minus the
        # declared directory size is exactly that corrected base, and it equals
        # ``cd_offset`` for an ordinary archive (no stub, optional own comment),
        # so the normal path is unchanged.
        eocd_location = (file_size - window) + index
        corrected = eocd_location - cd_size
        if corrected >= 0:
            cd_offset = corrected
        return total, cd_offset, cd_size
    # ZIP64: the locator sits immediately before the EOCD record and carries the
    # absolute offset of the ZIP64 EOCD record, whose entry count is 8 bytes wide
    # at +32. A count of 0xFFFF is the ZIP64 sentinel, so under any reading the
    # package declares at least 0xFFFF (65535) members. If the ZIP64 records
    # cannot be read the file is malformed or fabricated; fail closed so the
    # caller's limit still bounds the materialization instead of letting a
    # crafted EOCD bypass the pre-read.
    locator_index = index - ZIP64_EOCD_LOCATOR_SIZE
    if locator_index < 0:
        raise Zip64RecordUnreadable(ZIP64_UNREADABLE_MESSAGE)
    if tail[locator_index : locator_index + 4] != ZIP64_EOCD_LOCATOR_SIGNATURE:
        raise Zip64RecordUnreadable(ZIP64_UNREADABLE_MESSAGE)
    zip64_offset = int.from_bytes(tail[locator_index + 8 : locator_index + 16], "little")
    if zip64_offset + ZIP64_EOCD_MIN_SIZE > file_size:
        raise Zip64RecordUnreadable(ZIP64_UNREADABLE_MESSAGE)
    handle.seek(zip64_offset)
    zip64 = handle.read(ZIP64_EOCD_MIN_SIZE)
    if len(zip64) < ZIP64_EOCD_MIN_SIZE or zip64[0:4] != ZIP64_EOCD_SIGNATURE:
        raise Zip64RecordUnreadable(ZIP64_UNREADABLE_MESSAGE)
    zip64_total = int.from_bytes(zip64[32:40], "little")
    # ZIP64 stores the 8-byte-wide directory size/offset at +40/+48. Only the
    # first 8 bytes were read, so recover the full values with two more reads
    # rather than assuming the 32-bit EOCD copies are valid.
    zip64_size = _read_zip64_field(handle, zip64_offset + 40)
    zip64_offset_cd = _read_zip64_field(handle, zip64_offset + 48)
    if cd_size == 0xFFFFFFFF:
        cd_size = zip64_size
    if cd_offset == 0xFFFFFFFF:
        cd_offset = zip64_offset_cd
    return zip64_total, cd_offset, cd_size


def _read_zip64_field(handle: Any, offset: int) -> int:
    handle.seek(offset)
    raw = handle.read(8)
    if len(raw) < 8:
        raise Zip64RecordUnreadable(ZIP64_UNREADABLE_MESSAGE)
    return int.from_bytes(raw, "little")


def bounded_precheck(handle: Any, max_members: int) -> int | None:
    """Bound a package's member count *without* materializing the directory.

    Returns the verified member count, or ``None`` when the archive has no usable
    EOCD record and therefore cannot be pre-checked (the caller must keep its
    post-open check).

    A2 (维护者裁定 2026-10-07): the maintainer rejected "only trust the declared
    count" and rejected "adding a ``size_cd`` check is sufficient proof". This
    routine therefore proves the bound instead of asserting it, in three steps:

    1. **Declared count.** Reject immediately when the EOCD/ZIP64 count exceeds
       ``max_members``. This is sound: the count is what the archive claims, and
       no honest at-limit package exceeds it.
    2. **Directory geometry.** ``cd_offset``/``cd_size`` must lie inside the file.
       A directory that runs past the end is truncated, and ``ZipFile`` would
       only surface that *after* materializing what it could parse.
    3. **Bounded directory walk + cross-check.** Walk the records from
       ``cd_offset``, reading one header at a time, stopping the moment the count
       exceeds ``max_members`` (reject). Then require the walked count to equal
       the declared count. A mismatch means the declaration is falsified or the
       archive is malformed; both are rejected rather than trusted.

    DESIGN: the record walk — not the declared count and not ``cd_size`` — is
    what actually catches a falsified declaration. ``cd_size`` looks like the
    cheap fix ("each record needs ≥ ``CD_MIN_RECORD_SIZE``, so a large
    ``cd_size`` proves too many members"), but that is an **upper** bound on the
    member count, so rejecting when it exceeds the limit also rejects an honest
    package sitting exactly at the limit: 5000 members with 7-character names
    occupy 265000 bytes, and 265000 // 48 = 5520 > 5000. Measured before this
    comment was written (see ``evidence/batch5a2_probe.py``
    ``A2-normal_at_limit``). ``cd_size`` is therefore used only to bound the walk
    extent, never as a rejection criterion on its own. This is precisely the
    "补 size_cd 检查不等于充分证明" case the maintainer ruled on.

    Raises:
        PackageTooLarge: The archive provably exceeds ``max_members``.
        CentralDirectoryUnreadable: The directory is truncated, self-contradicting,
            or too large to verify within :data:`CD_SCAN_BYTE_BUDGET`.
        Zip64RecordUnreadable: ZIP64 was declared but cannot be validated.
    """
    geometry = _read_eocd_geometry(handle)
    if geometry is None:
        return None
    declared, cd_offset, cd_size = geometry

    if declared > max_members:
        raise PackageTooLarge(
            f"压缩包声明文件数量 {declared} 超过上限 {max_members}", declared=declared
        )

    # Step 2: the directory must lie inside the file.
    handle.seek(0, os.SEEK_END)
    file_size = handle.tell()
    if cd_offset > file_size or cd_offset + cd_size > file_size:
        raise CentralDirectoryUnreadable(CD_UNREADABLE_MESSAGE)

    counted, bytes_read = _scan_directory_records(handle, cd_offset, cd_size, max_members)
    if counted > max_members:
        raise PackageTooLarge(
            f"压缩包实际包含超过 {max_members} 个成员", declared=declared
        )
    if counted != declared:
        raise CentralDirectoryUnreadable(CD_UNREADABLE_MESSAGE)
    # Defence in depth: the walk reads one header at a time and stops as soon as
    # the member budget is exceeded, so its cost is bounded by ``max_members``
    # regardless of how large ``cd_size`` claims to be. This budget exists so
    # that removing that early exit cannot silently remove the bound.
    if bytes_read >= CD_SCAN_BYTE_BUDGET:
        raise CentralDirectoryUnreadable(CD_UNREADABLE_MESSAGE)
    return counted


def _scan_directory_records(
    handle: Any, cd_offset: int, cd_size: int, max_members: int
) -> tuple[int, int]:
    """Count central-directory records, reading at most one header at a time.

    Returns ``(count, bytes_read)``. ``count`` may exceed ``max_members`` by at
    most one, which is all the caller needs to reject. Raises
    :class:`CentralDirectoryUnreadable` when the walk runs past the declared
    directory extent or hits the byte budget.
    """
    handle.seek(cd_offset)
    position = 0
    counted = 0
    while position < cd_size:
        if counted > max_members:
            break
        header = handle.read(CD_HEADER_SIZE)
        if len(header) < CD_HEADER_SIZE or header[0:4] != CD_SIGNATURE:
            raise CentralDirectoryUnreadable(CD_UNREADABLE_MESSAGE)
        name_length = int.from_bytes(header[28:30], "little")
        extra_length = int.from_bytes(header[30:32], "little")
        comment_length = int.from_bytes(header[32:34], "little")
        record_size = CD_HEADER_SIZE + name_length + extra_length + comment_length
        if position + record_size > cd_size:
            raise CentralDirectoryUnreadable(CD_UNREADABLE_MESSAGE)
        # The variable-length tail is skipped rather than read: its content
        # cannot change the member count, and not reading it is what keeps this
        # pre-check bounded.
        handle.seek(record_size - CD_HEADER_SIZE, os.SEEK_CUR)
        position += record_size
        counted += 1
        if position >= CD_SCAN_BYTE_BUDGET:
            return counted, position
    return counted, position
