"""Create and stream-verify the distributable onedir ZIP archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

MAX_ARCHIVE_FILES = 10000
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
# Fixed timestamp for all ZIP entries so identical inputs produce byte-for-byte
# identical archives (reproducible builds), independent of build machine time.
FIXED_ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)
REPARSE_POINT_ATTRIBUTE = 0x400


def hash_stream(handle) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: handle.read(COPY_CHUNK_BYTES), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _safe_member(name: str, build_name: str) -> str:
    if "\\" in name or "\0" in name:
        raise ValueError(f"ZIP 使用了非标准路径分隔符：{name}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"ZIP 包含不安全路径：{name}")
    if path.parts[0] != build_name or any(":" in part for part in path.parts):
        raise ValueError(f"ZIP 文件不在发布根目录内：{name}")
    relative = PurePosixPath(*path.parts[1:])
    if not relative.parts:
        raise ValueError(f"ZIP 文件路径缺少相对名称：{name}")
    # Reject Windows reserved device names (CON, PRN, NUL, COM1-9, LPT1-9)
    for part in relative.parts:
        stem = part.split(".")[0].upper()
        if stem in _WINDOWS_RESERVED:
            raise ValueError(f"Windows reserved device name in archive: {name}")
    relative_name = relative.as_posix()
    if name != f"{build_name}/{relative_name}":
        raise ValueError(f"ZIP 使用了非规范路径：{name}")
    return relative_name


def _is_reparse_point(path: Path) -> bool:
    stat_result = path.lstat()
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & REPARSE_POINT_ATTRIBUTE)


def _manifest_entries(manifest: dict) -> tuple[str, dict[str, tuple[str, int, str]]]:
    if manifest.get("format") != "fukuaRPA_onedir_manifest":
        raise ValueError("外部发布清单格式不正确")
    if int(manifest.get("version", 0)) != 1:
        raise ValueError("外部发布清单版本不受支持")
    build_name = str(manifest.get("build") or "")
    build_path = PurePosixPath(build_name)
    if (
        not build_name
        or build_path.is_absolute()
        or len(build_path.parts) != 1
        or build_name in {".", ".."}
        or ":" in build_name
        or "\\" in build_name
        or "\0" in build_name
    ):
        raise ValueError("外部发布清单的构建名称不安全")
    entries = manifest.get("files", [])
    if not isinstance(entries, list):
        raise ValueError("外部发布清单文件列表格式不正确")
    if len(entries) > MAX_ARCHIVE_FILES:
        raise ValueError("外部发布清单文件数量超过上限")
    expected: dict[str, tuple[str, int, str]] = {}
    total_bytes = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("外部发布清单条目格式不正确")
        raw_relative = str(entry.get("path") or "")
        relative = PurePosixPath(raw_relative)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or any(":" in part for part in relative.parts)
            or "\\" in raw_relative
            or "\0" in raw_relative
            or raw_relative != relative.as_posix()
        ):
            raise ValueError(f"外部发布清单包含不安全路径：{relative}")
        relative_name = relative.as_posix()
        key = relative_name.casefold()
        if key in expected:
            raise ValueError(f"外部发布清单包含重复路径：{relative_name}")
        try:
            size = int(entry.get("size", -1))
        except (TypeError, ValueError) as error:
            raise ValueError(f"外部发布清单文件大小无效：{relative_name}") from error
        checksum = str(entry.get("sha256", "")).lower()
        if size < 0 or len(checksum) != 64 or any(
            character not in "0123456789abcdef" for character in checksum
        ):
            raise ValueError(f"外部发布清单校验信息无效：{relative_name}")
        total_bytes += size
        if total_bytes > MAX_ARCHIVE_BYTES:
            raise ValueError("外部发布清单总大小超过 2 GiB 上限")
        expected[key] = (relative_name, size, checksum)
    if manifest.get("file_count") != len(expected):
        raise ValueError("外部发布清单文件数量字段不一致")
    if manifest.get("total_bytes") != total_bytes:
        raise ValueError("外部发布清单总大小字段不一致")
    return build_name, expected


def build_archive(release_dir: Path, archive_path: Path) -> dict:
    release_dir = release_dir.resolve()
    archive_path = archive_path.resolve()
    if not release_dir.is_dir():
        raise ValueError(f"发布目录不存在：{release_dir}")
    try:
        archive_path.relative_to(release_dir)
    except ValueError:
        pass
    else:
        raise ValueError("便携 ZIP 不能写入待归档的发布目录内部")
    files = [
        path
        for path in sorted(
            release_dir.rglob("*"), key=lambda item: item.as_posix().casefold()
        )
        if path.is_file()
    ]
    total_bytes = sum(path.stat().st_size for path in files)
    if len(files) > MAX_ARCHIVE_FILES:
        raise ValueError(f"发布文件超过 ZIP 上限 {MAX_ARCHIVE_FILES}")
    if total_bytes > MAX_ARCHIVE_BYTES:
        raise ValueError("发布目录超过 2 GiB ZIP 上限")
    relative_keys: set[str] = set()
    for source in files:
        if _is_reparse_point(source):
            raise ValueError(f"发布目录不允许包含链接或重解析点：{source}")
        try:
            relative = source.resolve(strict=True).relative_to(release_dir)
        except ValueError as error:
            raise ValueError(f"发布文件逃逸发布目录：{source}") from error
        key = relative.as_posix().casefold()
        if key in relative_keys:
            raise ValueError(f"发布目录包含大小写冲突路径：{relative}")
        relative_keys.add(key)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{archive_path.name}.", suffix=".tmp", dir=archive_path.parent
    )
    os.close(descriptor)
    try:
        with zipfile.ZipFile(
            temporary_name,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for source in files:
                relative = source.relative_to(release_dir).as_posix()
                member_name = f"{release_dir.name}/{relative}"
                info = zipfile.ZipInfo(member_name, FIXED_ZIP_TIMESTAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 0
                info.external_attr = 0
                with source.open("rb") as input_handle, archive.open(
                    info, "w", force_zip64=True
                ) as output_handle:
                    shutil.copyfileobj(
                        input_handle,
                        output_handle,
                        length=COPY_CHUNK_BYTES,
                    )
        os.replace(temporary_name, archive_path)
    finally:
        if os.path.exists(temporary_name):
            os.remove(temporary_name)
    return {
        "archive": str(archive_path),
        "build": release_dir.name,
        "file_count": len(files),
        "source_bytes": total_bytes,
        "archive_bytes": archive_path.stat().st_size,
    }


def verify_archive(archive_path: Path, manifest_path: Path) -> dict:
    archive_path = archive_path.resolve()
    if not archive_path.is_file():
        raise ValueError(f"便携 ZIP 不存在：{archive_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    build_name, expected = _manifest_entries(manifest)

    seen: set[str] = set()
    expanded_bytes = 0
    with zipfile.ZipFile(archive_path, "r") as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) > MAX_ARCHIVE_FILES:
            raise ValueError("ZIP 文件数量超过上限")
        for member in members:
            # FR-20260811-D15-7(建议): 断言条目时间戳固定，守护可复现构建约定。
            # build_release 的 archive --check 走本函数，verify_dist 因调用时序
            # 早于 archive 创建而不可达，故把断言放在这里（生成后立即校验）。
            if member.date_time != FIXED_ZIP_TIMESTAMP:
                raise ValueError(
                    f"ZIP 条目时间戳不固定（可复现构建约定被破坏）：{member.filename} "
                    f"got {member.date_time} expected {FIXED_ZIP_TIMESTAMP}"
                )
            relative = _safe_member(member.filename, build_name)
            key = relative.casefold()
            if key in seen:
                raise ValueError(f"ZIP 包含重复路径：{relative}")
            seen.add(key)
            expected_item = expected.get(key)
            if expected_item is None:
                raise ValueError(f"ZIP 包含清单外文件：{relative}")
            expected_name, expected_size, expected_hash = expected_item
            if relative != expected_name:
                raise ValueError(f"ZIP 路径大小写与清单不一致：{relative}")
            if member.flag_bits & 0x1:
                raise ValueError(f"ZIP 不允许包含加密成员：{relative}")
            if member.create_system != 0:
                raise ValueError(f"ZIP 成员来源系统不符合 Windows 便携包约定：{relative}")
            if member.file_size != expected_size:
                raise ValueError(f"ZIP 文件大小与清单不一致：{relative}")
            expanded_bytes += member.file_size
            if expanded_bytes > MAX_ARCHIVE_BYTES:
                raise ValueError("ZIP 解压后总大小超过 2 GiB 上限")
            with archive.open(member, "r") as handle:
                checksum, size = hash_stream(handle)
            if size != expected_size or checksum != expected_hash:
                raise ValueError(f"ZIP 文件校验失败：{relative}")
        missing = sorted(set(expected) - seen)
        if missing:
            raise ValueError(f"ZIP 缺少发布文件：{missing[:5]}")
        # SHA-256 验证内容对照清单，testzip 额外验证 ZIP 容器/CRC 完整性，
        # 二者互补而非冗余。
        bad_member = archive.testzip()
        if bad_member:
            raise ValueError(f"ZIP CRC 校验失败：{bad_member}")
    return {
        "archive": str(archive_path),
        "build": build_name,
        "file_count": len(seen),
        "archive_bytes": archive_path.stat().st_size,
        "verified": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    release_dir = args.release_dir.resolve()
    manifest = (
        args.manifest.resolve()
        if args.manifest
        else release_dir.parent / f"{release_dir.name}_manifest.json"
    )
    output = (
        args.output.resolve()
        if args.output
        else release_dir.parent / f"{release_dir.name}_portable.zip"
    )
    report = (
        verify_archive(output, manifest)
        if args.check
        else build_archive(release_dir, output)
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
