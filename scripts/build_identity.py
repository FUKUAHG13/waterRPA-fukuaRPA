"""Shared, deterministic identity for one frozen fukuaRPA build."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping

BUILD_IDENTITY_VERSION = 1
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def canonical_sha256(value) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def dependency_fingerprint(dependencies: Mapping | None) -> str:
    normalized = {
        str(name): str(version)
        for name, version in sorted((dependencies or {}).items())
    }
    return canonical_sha256(normalized)


def _private_validation_receipt(record: Mapping, *, required: bool):
    receipt = record.get("private_validation_inputs")
    if required and not (
        isinstance(receipt, Mapping)
        and set(receipt) == {"format_version", "sha256", "file_count"}
        and type(receipt.get("format_version")) is int
        and receipt["format_version"] == 1
        and isinstance(receipt.get("sha256"), str)
        and _SHA256_PATTERN.fullmatch(receipt["sha256"])
        and type(receipt.get("file_count")) is int
        and receipt["file_count"] > 0
    ):
        raise ValueError("正式材料缺少有效的私有验证输入回执")
    return receipt


def create_build_identity(
    *,
    application_version: str,
    build_name: str,
    build_mode: str,
    source_sha256: str,
    git_commit: str,
    git_dirty: bool,
    python_version: str,
    python_bits: int,
    dependencies: Mapping | None,
    native_core_sha256: str,
) -> dict:
    source_sha256 = str(source_sha256 or "").lower()
    native_core_sha256 = str(native_core_sha256 or "").lower()
    if not _SHA256_PATTERN.fullmatch(source_sha256):
        raise ValueError("源码指纹不是有效的 SHA-256")
    if not _SHA256_PATTERN.fullmatch(native_core_sha256):
        raise ValueError("原生核心指纹不是有效的 SHA-256")
    if build_mode not in {"development", "formal"}:
        raise ValueError("构建模式无效")
    if not str(application_version or "").strip() or not str(build_name or "").strip():
        raise ValueError("构建版本或名称为空")
    try:
        python_bits = int(python_bits)
    except (TypeError, ValueError) as error:
        raise ValueError("Python 指针位数无效") from error
    if python_bits not in {32, 64}:
        raise ValueError("Python 指针位数必须是 32 或 64")

    fields = {
        "format_version": BUILD_IDENTITY_VERSION,
        "application_version": str(application_version),
        "build_name": str(build_name),
        "build_mode": build_mode,
        "source_sha256": source_sha256,
        "git_commit": str(git_commit or ""),
        "git_dirty": bool(git_dirty),
        "python_version": str(python_version or ""),
        "python_bits": python_bits,
        "dependencies_sha256": dependency_fingerprint(dependencies),
        "native_core_sha256": native_core_sha256,
    }
    return {"build_id": canonical_sha256(fields), "fields": fields}


def identity_from_build_record(record: Mapping) -> dict:
    if not isinstance(record, Mapping):
        raise ValueError("构建记录根节点无效")
    _private_validation_receipt(record, required=record.get("build_mode") == "formal")
    python = record.get("python")
    native = record.get("native_core")
    git = record.get("git")
    if not isinstance(python, Mapping) or not isinstance(native, Mapping):
        raise ValueError("构建记录缺少 Python 或原生核心信息")
    if not isinstance(git, Mapping):
        raise ValueError("构建记录缺少 Git 信息")
    expected = create_build_identity(
        application_version=record.get("application_version", ""),
        build_name=record.get("build_name", ""),
        build_mode=record.get("build_mode", ""),
        source_sha256=record.get("source_sha256", ""),
        git_commit=git.get("commit", ""),
        git_dirty=git.get("dirty", True),
        python_version=python.get("version", ""),
        python_bits=python.get("bits", 0),
        dependencies=record.get("dependencies"),
        native_core_sha256=native.get("sha256", ""),
    )
    if record.get("build_id") != expected["build_id"]:
        raise ValueError("构建记录 build_id 与记录内容不一致")
    if record.get("build_identity") != expected["fields"]:
        raise ValueError("构建记录的身份字段与记录内容不一致")
    return expected


def require_matching_evidence(build_record: Mapping, *evidence: Mapping) -> dict:
    identity = identity_from_build_record(build_record)
    formal = identity["fields"]["build_mode"] == "formal"
    expected_receipt = _private_validation_receipt(build_record, required=formal)
    for item in evidence:
        if not isinstance(item, Mapping):
            raise ValueError("发行证据根节点无效")
        if _private_validation_receipt(item, required=formal) != expected_receipt:
            raise ValueError("发行证据私有验证输入身份不一致")
        if item.get("build_id") != identity["build_id"]:
            raise ValueError("发行证据来自不同构建")
        if item.get("build_identity") != identity["fields"]:
            raise ValueError("发行证据身份字段不一致")
        if item.get("application_version") != identity["fields"]["application_version"]:
            raise ValueError("发行证据版本不一致")
        if item.get("build_name") != identity["fields"]["build_name"]:
            raise ValueError("发行证据构建名称不一致")
        if item.get("build_mode") != identity["fields"]["build_mode"]:
            raise ValueError("发行证据构建模式不一致")
        if item.get("source_sha256") != identity["fields"]["source_sha256"]:
            raise ValueError("发行证据源码指纹不一致")
    return identity
