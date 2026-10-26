"""Offline payload hashing and defensive verification for onedir releases."""

from __future__ import annotations

import hashlib
import json
import string
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from .io_utils import atomic_write_text
from .io_utils import hash_file as _io_hash_file
from .json_safety import load_bounded_json_file

PAYLOAD_MANIFEST_NAME = "PAYLOAD_HASHES.json"
MAX_PAYLOAD_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_PAYLOAD_FILES = 10000
SUSPICIOUS_EXTRA_SUFFIXES = {
    ".bat",
    ".chm",
    ".cmd",
    ".com",
    ".cpl",
    ".dll",
    ".exe",
    ".hta",
    ".jar",
    ".js",
    ".jse",
    ".lnk",
    ".msi",
    ".msp",
    ".pif",
    ".ps1",
    ".psd1",
    ".psm1",
    ".pyd",
    ".py",
    ".pyc",
    ".pyw",
    ".scr",
    ".sys",
    ".vbe",
    ".vbs",
    ".wsf",
    ".wsh",
    ".zip",
}


def hash_file(path: Path, cancelled: Callable[[], bool] | None = None) -> str:
    """计算文件 SHA-256；cancelled 回调返回真值时中止（保持 InterruptedError 语义）。

    委托到统一的 io_utils.hash_file 实现，保留 (path, cancelled=None) 签名与取消
    行为（verify_payload 依赖捕获 InterruptedError 返回 cancelled=True）。
    """
    try:
        return _io_hash_file(path, cancelled=cancelled)
    except RuntimeError as exc:
        if str(exc) == "hashing cancelled":
            raise InterruptedError("校验已取消") from exc
        raise


def _safe_relative_path(value: str) -> PurePosixPath:
    relative = PurePosixPath(str(value or ""))
    if (
        not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or any(":" in part for part in relative.parts)
    ):
        raise ValueError(f"清单包含不安全路径：{value}")
    return relative


def build_payload_manifest(
    root: Path, cancelled: Callable[[], bool] | None = None
) -> dict:
    root = Path(root).resolve()
    files = []
    aggregate = hashlib.sha256()
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if cancelled and cancelled():
            raise InterruptedError("构建已取消")
        if not path.is_file() or path.name == PAYLOAD_MANIFEST_NAME:
            continue
        relative = path.relative_to(root).as_posix()
        checksum = hash_file(path, cancelled)
        size = path.stat().st_size
        files.append({"path": relative, "size": size, "sha256": checksum})
        total_bytes += size
        aggregate.update(f"{checksum}  {relative}\n".encode())
        if len(files) > MAX_PAYLOAD_FILES:
            raise ValueError(f"发布文件超过上限 {MAX_PAYLOAD_FILES}")
    return {
        "format": "fukuaRPA_payload_hashes",
        "format_version": 1,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "file_count": len(files),
        "total_bytes": total_bytes,
        "directory_sha256": aggregate.hexdigest(),
        "self_protected": False,
        "purpose": "Detect accidental corruption; Authenticode is required to establish publisher identity.",
        "files": files,
    }


def atomic_write_manifest(path: Path, report: dict[str, object]) -> None:
    """Serialize *report* as pretty-printed JSON and write it atomically.

    Args:
        path: Destination file path for the manifest.
        report: Manifest dict to serialize.
    """
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, text, newline="\n")


def verify_payload(
    root: Path,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, object]:
    """Verify payload integrity against the embedded hash manifest.

    Checks every file listed in PAYLOAD_HASHES.json for presence and hash
    match, then scans for unexpected files (flagging suspicious executable
    extensions separately).

    Args:
        root: Payload directory containing the manifest.
        cancelled: Optional callback polled between files; return True to abort.
        progress: Optional callback invoked as ``progress(checked, total)``
            after each manifest entry is verified.

    Returns:
        Result dict with keys: ok, cancelled, error, checked, missing,
        mismatched, unexpected, suspicious_unexpected, and counts.
    """
    root = Path(root).resolve()
    manifest_path = root / PAYLOAD_MANIFEST_NAME
    if not manifest_path.is_file():
        return {
            "ok": False,
            "cancelled": False,
            "error": f"没有找到 {PAYLOAD_MANIFEST_NAME}",
            "checked": 0,
            "missing": [],
            "mismatched": [],
            "unexpected": [],
            "suspicious_unexpected": [],
        }
    if manifest_path.stat().st_size > MAX_PAYLOAD_MANIFEST_BYTES:
        return {
            "ok": False,
            "cancelled": False,
            "error": "校验清单体积异常。",
            "checked": 0,
            "missing": [],
            "mismatched": [],
            "unexpected": [],
            "suspicious_unexpected": [],
        }
    try:
        manifest = load_bounded_json_file(
            manifest_path,
            max_bytes=MAX_PAYLOAD_MANIFEST_BYTES,
            label="发布校验清单",
            max_depth=8,
            max_nodes=MAX_PAYLOAD_FILES * 8 + 32,
        )
        if not isinstance(manifest, dict):
            raise ValueError("清单根结构必须是对象")
        entries = manifest.get("files", [])
        if manifest.get("format") != "fukuaRPA_payload_hashes":
            raise ValueError("清单格式不正确")
        if not isinstance(entries, list) or len(entries) > MAX_PAYLOAD_FILES:
            raise ValueError("清单文件数量异常")
        expected_paths = set()
        missing = []
        mismatched = []
        checked = 0
        for entry in entries:
            if cancelled and cancelled():
                raise InterruptedError("校验已取消")
            if not isinstance(entry, dict):
                raise ValueError("清单文件条目格式不正确")
            relative = _safe_relative_path(entry.get("path", ""))
            key = relative.as_posix().casefold()
            if key in expected_paths:
                raise ValueError(f"清单包含重复路径：{relative.as_posix()}")
            expected_paths.add(key)
            target = root.joinpath(*relative.parts).resolve()
            try:
                target.relative_to(root)
            except ValueError as error:
                raise ValueError(f"清单路径越过程序目录：{relative}") from error
            if not target.is_file():
                missing.append(relative.as_posix())
            else:
                expected_size = int(entry.get("size", -1))
                expected_hash = str(entry.get("sha256", "")).lower()
                if expected_size < 0:
                    raise ValueError(f"清单文件大小不正确：{relative.as_posix()}")
                if len(expected_hash) != 64 or any(
                    character not in string.hexdigits for character in expected_hash
                ):
                    raise ValueError(f"清单哈希格式不正确：{relative.as_posix()}")
                actual_hash = hash_file(target, cancelled)
                if target.stat().st_size != expected_size or actual_hash != expected_hash:
                    mismatched.append(relative.as_posix())
            checked += 1
            if progress:
                progress(checked, len(entries))
        unexpected = []
        suspicious = []
        unexpected_count = 0
        suspicious_count = 0
        scanned_files = 0
        for path in root.rglob("*"):
            if not path.is_file() or path.name == PAYLOAD_MANIFEST_NAME:
                continue
            scanned_files += 1
            if scanned_files > MAX_PAYLOAD_FILES:
                raise ValueError(f"程序目录文件数量超过上限 {MAX_PAYLOAD_FILES}")
            relative = path.relative_to(root).as_posix()
            if relative.casefold() in expected_paths:
                continue
            unexpected_count += 1
            if len(unexpected) < 100:
                unexpected.append(relative)
            if path.suffix.lower() in SUSPICIOUS_EXTRA_SUFFIXES:
                suspicious_count += 1
                if len(suspicious) < 100:
                    suspicious.append(relative)
        return {
            "ok": not missing and not mismatched and not suspicious,
            "cancelled": False,
            "error": "",
            "checked": checked,
            "expected": len(entries),
            "missing": missing[:100],
            "mismatched": mismatched[:100],
            "unexpected": unexpected,
            "unexpected_count": unexpected_count,
            "suspicious_unexpected": suspicious,
            "suspicious_unexpected_count": suspicious_count,
            "directory_sha256": manifest.get("directory_sha256", ""),
            "manifest_self_protected": bool(manifest.get("self_protected", False)),
        }
    except InterruptedError:
        return {
            "ok": False,
            "cancelled": True,
            "error": "校验已取消。",
            "checked": 0,
            "missing": [],
            "mismatched": [],
            "unexpected": [],
            "suspicious_unexpected": [],
        }
    except (OSError, ValueError, TypeError) as error:
        return {
            "ok": False,
            "cancelled": False,
            "error": str(error),
            "checked": 0,
            "missing": [],
            "mismatched": [],
            "unexpected": [],
            "suspicious_unexpected": [],
        }
