"""Validated, recoverable profile persistence independent from the main window."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

import contextlib  # noqa: E402

from PySide6.QtCore import QSettings  # noqa: E402

from .config_schema import (  # noqa: E402
    UnsupportedProfileVersion,
    default_profile_config,
    migrate_profiles,
)
from .constants import (  # noqa: E402
    APP_VERSION,
    DEFAULT_PROFILE_BACKUP_COUNT,
    LEGACY_PROFILE_BACKUP_FORMAT,
    MAX_PROFILE_BACKUP_COUNT,
    MAX_PROFILE_NAME_LENGTH,
    MAX_PROFILE_STORE_FILE_BYTES,
    MAX_PROFILE_STORE_JSON_BYTES,
    MAX_PROFILES,
    PROFILE_BACKUP_FORMAT,
    PROFILE_BACKUP_VERSION,
    PROFILE_COMMIT_FORMAT,
    PROFILE_COMMIT_VERSION,
    PROFILE_HISTORY_LIMIT,
    PROFILE_HISTORY_MIN_INTERVAL_SECONDS,
)
from .io_utils import atomic_write_json  # noqa: F401, E402
from .json_safety import (  # noqa: E402
    load_bounded_json_file,
    loads_bounded_json,
)
from .profile_structure import validate_profile_config_data  # noqa: E402


@dataclass(frozen=True)
class ProfilesLoadResult:
    """Outcome of loading profiles at startup, including recovery details.

    Attributes:
        profiles: Loaded (and possibly migrated) profile mapping.
        current_profile: Name of the profile selected after loading.
        recovery_message: Human-readable note shown when recovery occurred.
        migrated: Whether any profile required schema migration.
        source: Where the data came from (settings/backup/history/etc.).
        persistence_blocked: True when saving must be disabled to avoid
            overwriting data written by an incompatible program version.
    """

    profiles: dict[str, dict[str, Any]]
    current_profile: str
    recovery_message: str = ""
    migrated: bool = False
    source: str = "settings"
    persistence_blocked: bool = False


@dataclass(frozen=True)
class _ProfilesRecord:
    """Internal validated profile payload with generation and signature."""

    profiles: dict[str, dict[str, Any]]
    current_profile: str
    generation: int
    signature: str
    migrated: bool = False
    #: 共享映射归一化**之前**的签名。同代次冲突比较必须用它：归一化会改变内容，
    #: 用归一化后的签名比较会把正常升级误判成“主提交写入未完成”（FR-20261021-002）。
    comparison_signature: str = ""


#: 按键映射相关字段：2026-10-21 维护者裁定（1b）——映射**不随方案走**，
#: 所有方案共用同一套映射。做法是「镜像」：以当前方案为权威写入全局键，
#: 再覆盖到所有方案，因此方案切换不会改变映射内容，导出/校验路径也不需要改结构。
MAPPING_FIELDS = (
    "key_mappings",
    "key_mapping_count",
    "mapping_mode_enabled",
    "mapping_click_mode",
)
GLOBAL_MAPPING_KEY = "global_mapping_json"


def _mapping_payload(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the mapping-related subset of a profile config."""
    return {field: config[field] for field in MAPPING_FIELDS if field in config}


def _read_global_mapping_payload(settings: QSettings) -> dict[str, Any]:
    """Return the stored shared mapping subset, or ``{}`` when absent/unreadable.

    SAFETY: 全局键存放在用户可写的配置里，解析必须有界；内容是否可用由调用方
    结合具体方案做结构校验（见 `_mapping_payload_applies`）。
    """
    if settings is None:
        return {}
    raw = settings.value(GLOBAL_MAPPING_KEY, "")
    if not raw:
        return {}
    try:
        loaded = loads_bounded_json(
            raw,
            max_bytes=MAX_PROFILE_STORE_JSON_BYTES,
            max_nodes=MAX_PROFILE_STORE_JSON_BYTES,
            label="全局按键映射",
        )
    except (TypeError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {field: loaded[field] for field in MAPPING_FIELDS if field in loaded}


def _mapping_payload_applies(payload: Mapping[str, Any], base: Any) -> bool:
    """Return True when applying *payload* to *base* keeps the config valid.

    SAFETY: 一次被拒绝的保存不应改变共享状态；同样，损坏或被篡改的全局键也不能
    覆盖所有方案。这里复用保存路径的同一套结构校验（FR-20261021-003）。
    """
    if not payload:
        return False
    probe = dict(base) if isinstance(base, Mapping) else {}
    probe.update(payload)
    return validate_profile_config_data(probe, "全局按键映射") is None


def load_global_mappings(settings: QSettings) -> dict[str, Any]:
    """Return the shared key-mapping set stored outside profiles.

    Returns an empty dict when the key is missing, unreadable or structurally
    invalid; callers then fall back to whatever the profile already carries.
    """
    payload = _read_global_mapping_payload(settings)
    if not payload:
        return {}
    if not _mapping_payload_applies(payload, default_profile_config()):
        logger.warning("全局按键映射结构无效，已忽略并回退方案自带映射")
        return {}
    return payload


def _apply_global_mappings(
    settings: QSettings, record: _ProfilesRecord
) -> _ProfilesRecord:
    """Overwrite every profile's mapping fields with the shared global set.

    Seeds the global key from the active profile when it is missing, so the
    first run after upgrade keeps whatever the user already had instead of
    resetting to defaults.
    """
    payload = _read_global_mapping_payload(settings)
    base = record.profiles.get(record.current_profile) or {}
    if payload and not _mapping_payload_applies(payload, base):
        # 损坏/无效的全局键不得扩散到所有方案：忽略它，沿用方案自带映射。
        logger.warning("全局按键映射结构无效，已忽略并沿用方案自带映射")
        payload = {}
    if not payload:
        payload = _mapping_payload(base)
        if not payload:
            return record
        # 只读替身（测试用 FakeSettings）可能没有 setValue；生产 QSettings 一定有。
        setter = getattr(settings, "setValue", None)
        if callable(setter):
            setter(GLOBAL_MAPPING_KEY, json.dumps(payload, ensure_ascii=False))
    changed = False
    for profile in record.profiles.values():
        if not isinstance(profile, dict):
            continue
        if any(profile.get(field) != value for field, value in payload.items()):
            changed = True
        profile.update(payload)
    if not changed:
        # 常见情形：所有方案已是同一套映射。此时不必重算签名——签名要对整份
        # 方案载荷做哈希，重复计算会拖慢启动（startup.workspace_visible_ms）。
        return record
    signature = profiles_signature(record.profiles, record.current_profile)[1]
    return _ProfilesRecord(
        record.profiles,
        record.current_profile,
        record.generation,
        signature,
        record.migrated,
        record.comparison_signature or record.signature,
    )


UNSUPPORTED_PROFILE_PREFIX = "方案版本不受支持："


def _unsupported_profile_message(error: object) -> str:
    """Format an unsupported-version error into a prefixed user message."""
    return f"{UNSUPPORTED_PROFILE_PREFIX}{error}"


def _is_unsupported_profile_message(message: object) -> bool:
    """Return True if *message* carries the unsupported-version prefix."""
    return str(message or "").startswith(UNSUPPORTED_PROFILE_PREFIX)


def validate_profiles_payload(profiles: Any) -> str | None:
    """Validate the full profiles mapping for persistence.

    Checks structure, name constraints, per-profile schema, and aggregate
    serialized size.

    Args:
        profiles: The profiles mapping to validate (expected dict of dicts).

    Returns:
        A human-readable error string, or None when the payload is valid.
    """
    if not isinstance(profiles, dict) or not profiles:
        return "方案集合为空或格式错误"
    if len(profiles) > MAX_PROFILES:
        return f"方案数量 {len(profiles)} 超过上限 {MAX_PROFILES}"
    aggregate_size = 2
    for name, config in profiles.items():
        if not isinstance(name, str) or not name.strip():
            return "存在空名称或非文本名称的方案"
        if name != name.strip():
            return f"方案名称“{name}”两端不能包含空格"
        if len(name) > MAX_PROFILE_NAME_LENGTH:
            return f"方案名称“{name[:40]}…”过长，最多允许 {MAX_PROFILE_NAME_LENGTH} 个字符"
        error = validate_profile_config_data(config, f"方案“{name}”")
        if error:
            return error
        # The full JSON serialization here is intentional: it produces the exact
        # byte count that will be written to disk, so the aggregate size limit
        # stays accurate. Validation is not a hot path, so the cost is acceptable.
        # Note: Multiple json.dumps calls on the same payload are intentional.
        # This is a cold path (save/migrate), not a hot loop. The redundancy
        # ensures each validation stage operates on its own canonical form.
        try:
            aggregate_size += len(name.encode("utf-8")) + len(
                json.dumps(config, ensure_ascii=False).encode("utf-8")
            ) + 4
        except (RecursionError, TypeError, ValueError) as error:
            return f"方案“{name}”包含无法保存的数据：{error}"
        if aggregate_size > MAX_PROFILE_STORE_JSON_BYTES:
            return (
                "全部方案序列化后的总大小超过上限 "
                f"{MAX_PROFILE_STORE_JSON_BYTES / 1024 / 1024:.0f} MB"
            )
    return None


def preserve_corrupt_config(config_path: str) -> str:
    """Copy a corrupt config file to a timestamped .bak for forensic recovery.

    Args:
        config_path: Absolute path to the corrupt configuration file.

    Returns:
        The backup path, or "" if the source does not exist.
    """
    if not config_path or not os.path.isfile(config_path):
        return ""
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    target = f"{config_path}.corrupt_{timestamp}.bak"
    shutil.copy2(config_path, target)
    return target


def profiles_signature(
    profiles: Mapping[str, Any], current_profile: str
) -> tuple[str, str]:
    """Compute the canonical payload text and SHA-256 integrity signature.

    The signature is derived from a sort-keyed canonical JSON representation so
    that dictionary insertion order does not affect the checksum, while the
    returned payload text preserves user-controlled profile order.

    Args:
        profiles: Profile mapping to sign.
        current_profile: Name of the active profile (included in the hash).

    Returns:
        Tuple of (payload_text, hex_signature).
    """
    # The stored text keeps insertion order because profile order is user
    # controlled. A separate canonical representation makes the checksum
    # independent from dictionary ordering.
    payload_text = json.dumps(profiles, ensure_ascii=False)
    canonical_text = json.dumps(
        profiles,
        ensure_ascii=False,
        sort_keys=True,
    )
    signature = hashlib.sha256(
        f"{current_profile}\n{canonical_text}".encode()
    ).hexdigest()
    return payload_text, signature


def _commit_signature(
    profiles: Mapping[str, Any], current_profile: str, generation: int
) -> str:
    """Compute the commit signature binding generation to the data signature."""
    _payload_text, data_signature = profiles_signature(profiles, current_profile)
    return hashlib.sha256(
        f"{int(generation)}\n{current_profile}\n{data_signature}".encode()
    ).hexdigest()


def _validated_generation(value: Any) -> int:
    """Coerce a raw generation value to a non-negative int.

    Raises:
        ValueError: If the value is not an integer or is negative.
    """
    try:
        generation = int(value or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("配置代次无效") from error
    if generation < 0:
        raise ValueError("配置代次不能为负数")
    return generation


def _record_from_payload(
    payload: Any,
    *,
    expected_format: str,
    maximum_version: int,
    label: str,
) -> _ProfilesRecord:
    """Validate, signature-check and migrate a stored envelope into a record.

    Args:
        payload: Decoded JSON envelope (must be a dict with matching format).
        expected_format: Required value of the envelope's ``format`` field.
        maximum_version: Highest envelope version this build can read.
        label: Human-readable store name used in error messages.

    Returns:
        A validated, migrated _ProfilesRecord.

    Raises:
        ValueError: If the envelope is malformed or a signature mismatches.
        UnsupportedProfileVersion: If the version exceeds *maximum_version*.
    """
    if not isinstance(payload, dict) or payload.get("format") != expected_format:
        raise ValueError(f"{label}格式无效")
    version = int(payload.get("version", 0) or 0)
    if version > maximum_version:
        raise UnsupportedProfileVersion(
            f"{label}版本 {version} 高于本程序支持的版本 {maximum_version}"
        )
    if version < 0:
        raise ValueError(f"{label}版本无效")
    profiles = payload.get("profiles")
    current_profile = str(payload.get("current_profile", "") or "")
    error = validate_profiles_payload(profiles)
    if error:
        raise ValueError(error)
    if not isinstance(profiles, dict):
        raise ValueError(f"{label}格式无效")
    expected_signature = str(payload.get("signature", "") or "")
    actual_signature = profiles_signature(profiles, current_profile)[1]
    if expected_signature and actual_signature != expected_signature:
        raise ValueError(f"{label}完整性签名不匹配")
    generation = _validated_generation(payload.get("generation", 0))
    expected_commit = str(payload.get("commit_signature", "") or "")
    if version >= maximum_version and not expected_commit:
        raise ValueError(f"{label}缺少提交签名")
    if expected_commit and expected_commit != _commit_signature(
        profiles, current_profile, generation
    ):
        raise ValueError(f"{label}提交签名不匹配")
    assert isinstance(profiles, dict)
    migrated_profiles, migrated = migrate_profiles(profiles)
    if current_profile not in migrated_profiles:
        current_profile = next(iter(migrated_profiles))
    runtime_signature = profiles_signature(
        migrated_profiles, current_profile
    )[1]
    return _ProfilesRecord(
        migrated_profiles,
        current_profile,
        generation,
        runtime_signature,
        migrated,
        runtime_signature,
    )


def _load_profiles_backup_record(
    backup_path: str,
) -> tuple[_ProfilesRecord | None, str]:
    """Load and validate a backup file into a record.

    Returns:
        Tuple of (record, error). On success error is ""; on failure the
        record is None and error describes the problem.
    """
    if not os.path.isfile(backup_path):
        return None, "没有找到自动备份"
    try:
        payload = load_bounded_json_file(
            backup_path,
            max_bytes=MAX_PROFILE_STORE_FILE_BYTES,
            # FR-20261011-006 / DOC-SCOPE005-001: 备份信封包含整个方案集合，导出侧
            # 只按聚合字节校验；节点上限若沿用默认 2M 会拒绝本版本能导出的集合。
            # JSON 每个节点序列化后至少占 1 字节（json_safety 只统计容器与值），
            # 故以字节上限作节点上限，保证"能导出即可导入"。
            max_nodes=MAX_PROFILE_STORE_FILE_BYTES,
            label="自动备份",
        )
        accepted_formats = {PROFILE_BACKUP_FORMAT, LEGACY_PROFILE_BACKUP_FORMAT}
        payload_format = str(payload.get("format")) if isinstance(payload, dict) else ""
        if isinstance(payload, dict) and payload_format in accepted_formats:
            record = _record_from_payload(
                payload,
                expected_format=payload_format,
                maximum_version=PROFILE_BACKUP_VERSION,
                label="自动备份",
            )
            return record, ""

        # Very early backups stored the profile mapping directly.
        error = validate_profiles_payload(payload)
        if error:
            return None, error
        profiles, migrated = migrate_profiles(payload)
        current_profile = next(iter(profiles))
        legacy_signature = profiles_signature(profiles, current_profile)[1]
        return _ProfilesRecord(
            profiles,
            current_profile,
            0,
            legacy_signature,
            migrated,
            legacy_signature,
        ), ""
    except UnsupportedProfileVersion as error:
        return None, _unsupported_profile_message(error)
    except (OSError, TypeError, ValueError) as error:
        return None, str(error)


def load_profiles_backup(
    backup_path: str,
) -> tuple[dict[str, dict[str, Any]] | None, str | None, str]:
    """Load and validate the atomic backup file.

    Returns:
        Tuple of (profiles_dict, current_profile_name, error_string).
        On success error is ""; on failure profiles and current are None.
    """
    record, error = _load_profiles_backup_record(backup_path)
    if record is None:
        return None, None, error
    return record.profiles, record.current_profile, ""


def profiles_backup_payload(
    profiles: Mapping[str, Any], current_profile: str, generation: int = 0
) -> dict[str, Any]:
    """Build the signed backup envelope dict for atomic persistence.

    The payload includes format/version metadata, a data integrity signature,
    and a commit signature binding the generation counter to the data.

    Args:
        profiles: Profile mapping to persist.
        current_profile: Name of the active profile.
        generation: Monotonically increasing write generation counter.

    Returns:
        JSON-serializable envelope dict ready for atomic write.
    """
    profiles, _changed = migrate_profiles(profiles)
    generation = _validated_generation(generation)
    _payload_text, signature = profiles_signature(profiles, current_profile)
    return {
        "format": PROFILE_BACKUP_FORMAT,
        "version": PROFILE_BACKUP_VERSION,
        "app_version": APP_VERSION,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "generation": generation,
        "current_profile": current_profile,
        "signature": signature,
        "commit_signature": _commit_signature(
            profiles, current_profile, generation
        ),
        "profiles": profiles,
    }


def _settings_commit_payload(
    profiles: Mapping[str, Any], current_profile: str, generation: int
) -> dict[str, Any]:
    """Build the QSettings commit envelope (backup payload minus timestamps and app_version)."""
    payload = profiles_backup_payload(profiles, current_profile, generation)
    payload["format"] = PROFILE_COMMIT_FORMAT
    payload["version"] = PROFILE_COMMIT_VERSION
    payload.pop("saved_at", None)
    payload.pop("app_version", None)
    return payload


def _load_settings_record(settings: QSettings) -> tuple[_ProfilesRecord | None, str]:
    """Load profiles and normalize the shared (global) key-mapping set."""
    record, error = _load_settings_record_raw(settings)
    if record is not None:
        record = _apply_global_mappings(settings, record)
    return record, error


def _load_settings_record_raw(settings: QSettings) -> tuple[_ProfilesRecord | None, str]:
    """Load the profile record stored in QSettings (commit or legacy format).

    Returns:
        Tuple of (record, error). On success error is ""; on failure the
        record is None and error describes the problem.

    Raises:
        UnsupportedProfileVersion: If the stored envelope is from a newer,
            incompatible program version.
    """
    if settings.contains("profiles_commit_json"):
        raw_commit = settings.value("profiles_commit_json")
        try:
            payload = loads_bounded_json(
                raw_commit,
                max_bytes=MAX_PROFILE_STORE_FILE_BYTES,
                # FR-20261011-006 / DOC-SCOPE005-001: 见 _load_backup_record 说明。
                max_nodes=MAX_PROFILE_STORE_FILE_BYTES,
                label="主配置提交",
            )
            return _record_from_payload(
                payload,
                expected_format=PROFILE_COMMIT_FORMAT,
                maximum_version=PROFILE_COMMIT_VERSION,
                label="主配置提交",
            ), ""
        except UnsupportedProfileVersion:
            raise
        except (TypeError, ValueError) as error:
            return None, str(error)

    if not settings.contains("profiles_json"):
        return None, "没有找到主配置"
    try:
        saved_profiles = settings.value("profiles_json", "{}")
        profiles = loads_bounded_json(
            saved_profiles,
            max_bytes=MAX_PROFILE_STORE_JSON_BYTES,
            # FR-20261011-006 / DOC-SCOPE005-001: 集合正文的节点上限与字节上限同口径。
            max_nodes=MAX_PROFILE_STORE_JSON_BYTES,
            label="主配置",
        )
        error = validate_profiles_payload(profiles)
        if error:
            raise ValueError(error)
        current_profile = str(settings.value("current_profile", "默认方案"))
        stored_signature = str(settings.value("profiles_signature", "") or "")
        actual_signature = profiles_signature(profiles, current_profile)[1]
        if stored_signature and stored_signature != actual_signature:
            raise ValueError("主配置完整性签名不匹配，可能是上次写入未完成")
        generation = _validated_generation(settings.value("profiles_generation", 0))
        profiles, migrated = migrate_profiles(profiles)
        if current_profile not in profiles:
            current_profile = next(iter(profiles))
        stored_signature = profiles_signature(profiles, current_profile)[1]
        return _ProfilesRecord(
            profiles,
            current_profile,
            generation,
            stored_signature,
            migrated,
            stored_signature,
        ), ""
    except UnsupportedProfileVersion:
        raise
    except (TypeError, ValueError) as error:
        return None, str(error)


def _history_dir_for(backup_path: str) -> str:
    """Return the history directory path sibling to the backup file."""
    return os.path.join(os.path.dirname(os.path.abspath(backup_path)), "profiles_history")


def normalize_profile_backup_count(
    value: Any, default: int = DEFAULT_PROFILE_BACKUP_COUNT
) -> int:
    """Return the user-facing total backup count, including the latest copy.

    DESIGN: 0 means no automatic backup at all; 1 means only the current atomic
    recovery copy. The program must not retain an undisclosed extra copy.

    Args:
        value: Raw user-supplied backup count (coerced via int()).
        default: Fallback when *value* cannot be parsed.

    Returns:
        Clamped integer in [0, MAX_PROFILE_BACKUP_COUNT].
    """

    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        count = int(default)
    return max(0, min(MAX_PROFILE_BACKUP_COUNT, count))


def _safe_mtime(path: str) -> float:
    """Return the file mtime, or -1.0 if it vanished (TOCTOU-tolerant sort key)."""

    try:
        return os.path.getmtime(path)
    except OSError:
        return -1.0


def list_profile_history(backup_path: str) -> list[str]:
    """List historical backup file paths, newest first (by mtime).

    Args:
        backup_path: Path to the current atomic backup file; the history
            directory is resolved as a sibling.

    Returns:
        Absolute paths of history JSON files sorted by mtime descending.
    """
    history_dir = _history_dir_for(backup_path)
    if not os.path.isdir(history_dir):
        return []
    paths = [
        os.path.join(history_dir, name)
        for name in os.listdir(history_dir)
        if name.startswith("profiles_") and name.endswith(".json")
    ]
    return sorted(paths, key=_safe_mtime, reverse=True)


def list_profile_backups(backup_path: str) -> list[str]:
    """List the current atomic copy followed by history, newest first.

    Args:
        backup_path: Path to the current atomic backup file.

    Returns:
        Absolute paths starting with the live backup (if it exists),
        followed by history entries in mtime-descending order.
    """

    result = []
    if os.path.isfile(backup_path):
        result.append(os.path.abspath(backup_path))
    result.extend(list_profile_history(backup_path))
    return result


def prune_profile_backups(backup_path: str, backup_count: Any) -> list[str]:
    """Apply the total retention count and optionally remove every backup.

    Args:
        backup_path: Path to the current atomic backup file.
        backup_count: User-configured total retention count (normalized
            internally); 0 removes all backups.

    Returns:
        Absolute paths of files that were deleted.
    """

    backup_count = normalize_profile_backup_count(backup_count)
    removed = []
    if backup_count == 0 and os.path.isfile(backup_path):
        try:
            os.remove(backup_path)
            removed.append(os.path.abspath(backup_path))
        except FileNotFoundError:
            pass
        except OSError as exc:
            # FR-101-002: 清理属尽力而为；删除失败（文件被占用/权限）不得让已成功的
            # 主配置保存误报失败。count=0 时残留副本不会被读取（_try_load_history 跳过）。
            logger.warning("删除自动备份失败（backup_count=0）：%s（%s）", backup_path, exc)

    keep_history = max(0, backup_count - 1)
    history = list_profile_history(backup_path)
    for old_path in history[keep_history:]:
        try:
            os.remove(old_path)
            removed.append(os.path.abspath(old_path))
        except FileNotFoundError:
            pass
        except OSError as exc:
            # FR-101-002: 历史备份清理失败同样不阻断保存结果（尽力而为）。
            logger.warning("删除历史备份失败：%s（%s）", old_path, exc)

    history_dir = _history_dir_for(backup_path)
    try:
        if os.path.isdir(history_dir) and not os.listdir(history_dir):
            os.rmdir(history_dir)
    except OSError:
        pass
    return removed


def _copy_backup_to_history(
    backup_path: str,
    history_dir: str,
    profiles: Mapping[str, Any],
    current: str | None,
) -> str:
    """Copy a valid backup file into the history directory atomically.

    Returns the new history path, or "" when the target already exists or the
    copy fails. Best-effort: errors are swallowed so a failed archive never
    blocks the caller's main persistence path.
    """

    stamp = time.strftime("%Y%m%d_%H%M%S")
    suffix = profiles_signature(profiles, current or "")[1][:10]
    target = os.path.join(history_dir, f"profiles_{stamp}_{suffix}.json")
    if os.path.exists(target):
        return ""
    temp_target = f"{target}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        shutil.copy2(backup_path, temp_target)
        os.replace(temp_target, target)
    except OSError:
        return ""
    finally:
        if os.path.exists(temp_target):
            with contextlib.suppress(OSError):
                os.remove(temp_target)
    return target


def archive_existing_backup(
    backup_path: str,
    *,
    limit: int = PROFILE_HISTORY_LIMIT,
    min_interval: float = PROFILE_HISTORY_MIN_INTERVAL_SECONDS,
) -> str:
    """Copy the previous valid backup into a bounded history directory.

    Args:
        backup_path: Path to the current atomic backup file.
        limit: Maximum number of history entries to retain.
        min_interval: Minimum seconds between regular archives; suppressed
            within this window (but a safety copy is still made).

    Returns:
        Absolute path of the newest history entry, or "" if nothing archived.
    """

    limit = max(0, int(limit))
    if limit == 0:
        prune_profile_backups(backup_path, 1)
        return ""
    if not os.path.isfile(backup_path):
        return ""
    profiles, current, error = load_profiles_backup(backup_path)
    if error or not profiles:
        return ""
    history_dir = _history_dir_for(backup_path)
    os.makedirs(history_dir, exist_ok=True)
    existing = list_profile_history(backup_path)
    if existing and min_interval > 0:
        try:
            if time.time() - os.path.getmtime(existing[0]) < min_interval:
                # Throttle hit: a regular archive is suppressed, but the caller
                # is about to overwrite backup_path. Copy the current backup
                # into history anyway (best-effort) so no recent copy is lost.
                _copy_backup_to_history(backup_path, history_dir, profiles, current)
                for old_path in list_profile_history(backup_path)[limit:]:
                    with contextlib.suppress(OSError):
                        os.remove(old_path)
                return ""
        except OSError:
            pass
    _copy_backup_to_history(backup_path, history_dir, profiles, current)
    for old_path in list_profile_history(backup_path)[limit:]:
        with contextlib.suppress(OSError):
            os.remove(old_path)
    history = list_profile_history(backup_path)
    return history[0] if history else ""


def load_latest_profile_history(
    backup_path: str,
) -> tuple[dict[str, dict[str, Any]] | None, str | None, str, str]:
    """Try each history backup newest-first until one loads successfully.

    Args:
        backup_path: Path to the current atomic backup file; the history
            directory is resolved as a sibling.

    Returns:
        Tuple of (profiles, current_profile, source_path, error).
        On success error is ""; on failure profiles/current/path are None/None/"".
    """
    errors = []
    for history_path in list_profile_history(backup_path):
        profiles, current, error = load_profiles_backup(history_path)
        if profiles is not None:
            return profiles, current, history_path, ""
        errors.append(f"{os.path.basename(history_path)}: {error}")
    return None, None, "", "；".join(errors) if errors else "没有找到历史备份"


def _try_load_main(
    settings: QSettings, config_path: str
) -> tuple[_ProfilesRecord | None, str]:
    """Attempt to load profiles from the main configuration.

    Returns (record, error).  Raises UnsupportedProfileVersion when the
    stored format was written by an incompatible program version.
    """
    main_record, main_error = _load_settings_record(settings)
    return main_record, main_error


def _try_load_backup(backup_path: str) -> tuple[_ProfilesRecord | None, str]:
    """Attempt to load profiles from the current atomic backup file.

    Returns (record, error).
    """
    return _load_profiles_backup_record(backup_path)


def _comparison_signature(record: _ProfilesRecord) -> str:
    """Return the signature used to compare two records of the same generation.

    DESIGN: 共享映射归一化会改变记录内容，但**不**代表上次写入残缺。比较必须用
    归一化前的签名，否则正常升级会被误判为“主提交只完成原子备份”
    （FR-20261021-002）。真实残缺的主提交签名不同，仍会被识别。
    """
    return record.comparison_signature or record.signature


def _try_load_history(
    history_dir: str, count: Any
) -> tuple[dict[str, dict[str, Any]] | None, str | None, str, str]:
    """Attempt to load profiles from historical backup files.

    *count* is the user-configured backup count; when backups are disabled
    (count normalises to 0) the search is skipped.

    Returns (profiles, current, path, error).
    """
    if count is not None and normalize_profile_backup_count(count) <= 0:
        return None, None, "", "自动备份已关闭"
    return load_latest_profile_history(history_dir)


def _build_default_profiles() -> dict[str, dict[str, Any]]:
    """Build the default empty profiles dictionary."""
    return {}


def load_profiles_state(
    settings: QSettings,
    config_path: str,
    backup_path: str,
    backup_count: Any = None,
) -> ProfilesLoadResult:
    """Load, migrate and recover profiles without depending on any Qt widget.

    Orchestration: try main -> backup -> history -> default.

    Args:
        settings: QSettings instance holding the primary profile store.
        config_path: Filesystem path to the legacy config file (for corrupt
            copy preservation).
        backup_path: Path to the atomic backup file.
        backup_count: User-configured backup retention count; None means
            backups are assumed enabled.

    Returns:
        A ProfilesLoadResult with loaded profiles and recovery metadata.
    """

    current_hint = str(settings.value("current_profile", "默认方案"))

    # --- Stage 1: main configuration ---
    try:
        main_record, main_error = _try_load_main(settings, config_path)
    except UnsupportedProfileVersion as error:
        message = (
            "当前方案由不兼容的程序版本写入，本版本已拒绝加载并禁止自动保存，"
            "原配置和自动备份不会被覆盖。\n\n"
            f"具体原因：{error}\n\n"
            "请改用创建该方案的版本，或使用明确提供方案转换功能的新版本。"
        )
        return ProfilesLoadResult(
            {},
            current_hint,
            message,
            source="unsupported",
            persistence_blocked=True,
        )

    # --- Stage 2: current backup ---
    backups_enabled = (
        backup_count is None or normalize_profile_backup_count(backup_count) > 0
    )
    if backups_enabled:
        backup_record, backup_error = _try_load_backup(backup_path)
    else:
        backup_record, backup_error = None, "自动备份已关闭"

    if main_record is not None:
        if _is_unsupported_profile_message(backup_error):
            return ProfilesLoadResult(
                main_record.profiles,
                main_record.current_profile,
                (
                    "检测到自动备份由不兼容的程序版本写入。当前主配置可以只读加载，"
                    "但本版本已禁止自动保存，以免覆盖更新版本的备份。\n\n"
                    f"自动备份问题：{backup_error}"
                ),
                migrated=main_record.migrated,
                source="unsupported_backup",
                persistence_blocked=True,
            )
        if backup_record is not None:
            backup_is_newer = backup_record.generation > main_record.generation
            same_generation_conflict = (
                backup_record.generation > 0
                and backup_record.generation == main_record.generation
                and _comparison_signature(backup_record)
                != _comparison_signature(main_record)
            )
            if backup_is_newer or same_generation_conflict:
                return ProfilesLoadResult(
                    backup_record.profiles,
                    backup_record.current_profile,
                    "检测到上次配置写入只完成了原子备份，程序已采用较新的备份内容。",
                    migrated=backup_record.migrated,
                    source="pending_backup",
                )
        return ProfilesLoadResult(
            main_record.profiles,
            main_record.current_profile,
            migrated=main_record.migrated,
        )

    # --- Main config failed; attempt recovery ---
    main_missing = main_error == "没有找到主配置"
    if _is_unsupported_profile_message(backup_error):
        message = (
            "自动备份由不兼容的程序版本写入，本版本已拒绝加载并禁止自动保存，"
            "原配置和自动备份不会被覆盖。\n\n"
            f"主配置问题：{main_error or '格式错误'}\n"
            f"自动备份问题：{backup_error}"
        )
        return ProfilesLoadResult(
            {},
            current_hint,
            message,
            source="unsupported",
            persistence_blocked=True,
        )

    if main_missing and backup_record is None:
        if not backups_enabled:
            return ProfilesLoadResult({}, current_hint, source="empty")
        # --- Stage 3: history backups ---
        history_profiles, history_current, history_path, _history_error = (
            _try_load_history(backup_path, backup_count)
        )
        if history_profiles is None:
            return ProfilesLoadResult({}, current_hint, source="empty")
        current = (
            history_current
            if history_current in history_profiles
            else next(iter(history_profiles))
        )
        return ProfilesLoadResult(
            history_profiles,
            str(current or ""),
            "主配置和当前自动备份不存在，程序已从历史备份恢复方案。\n\n"
            f"历史备份：{history_path}",
            source="history",
        )

    try:
        corrupt_copy = "" if main_missing else preserve_corrupt_config(config_path)
    except Exception as exc:
        logger.warning("保存损坏配置副本失败: %s", exc, exc_info=True)
        corrupt_copy = ""

    if backup_record is not None:
        profiles = backup_record.profiles
        current = backup_record.current_profile
        message = (
            (
                "主配置不存在，程序已从自动备份恢复方案。\n\n"
                if main_missing
                else "主配置无法读取，程序已从自动备份恢复方案。\n\n"
            )
            + f"原配置问题：{main_error or '格式错误'}"
        )
        source = "backup"
    else:
        # --- Stage 3: history backups ---
        profiles, history_current, history_path, history_error = (
            _try_load_history(backup_path, backup_count)
        )
        if profiles is not None:
            current = history_current if history_current in profiles else next(iter(profiles))
            message = (
                "主配置和当前自动备份都无法读取，程序已从历史备份恢复方案。\n\n"
                f"主配置问题：{main_error or '格式错误'}\n"
                f"当前备份问题：{backup_error}\n历史备份：{history_path}"
            )
            source = "history"
        else:
            # --- Stage 4: default ---
            profiles = _build_default_profiles()
            current = current_hint
            message = (
                "主配置、当前自动备份和历史备份都无法读取，程序已创建默认方案。\n\n"
                f"主配置问题：{main_error or '格式错误'}\n"
                f"当前备份问题：{backup_error}\n历史备份问题：{history_error}"
            )
            source = "default"
    if corrupt_copy:
        message += f"\n\n原配置副本已保留在：\n{corrupt_copy}"
    return ProfilesLoadResult(profiles, str(current or ""), message, source=source)


def persist_profiles(
    settings: QSettings,
    backup_path: str,
    profiles: Mapping[str, Any],
    current_profile: str,
    *,
    backup_count: Any = DEFAULT_PROFILE_BACKUP_COUNT,
    persistence_blocked: bool = False,
) -> str:
    """Validate and atomically persist profiles to settings plus backup.

    Concurrency contract: this function performs a read-modify-write cycle
    (it loads the current settings/backup records, derives the next generation
    and then writes) and is NOT internally locked. It must be called serially
    on the main thread. The project upholds this via single-instance startup,
    main-thread-only persistence, reentrancy guards at the call sites and the
    atomic-write helper, so an additional lock is unnecessary here.

    Args:
        settings: QSettings instance for the primary profile store.
        backup_path: Path to the atomic backup file.
        profiles: Profile mapping to persist.
        current_profile: Name of the active profile.
        backup_count: Total backup retention count (normalized internally).
        persistence_blocked: When True, skip all writes silently (defense
            against overwriting incompatible-version data).

    Returns:
        The hex signature of the committed payload, or "" if blocked.

    Raises:
        ValueError: If profiles fail validation or current_profile is missing.
        UnsupportedProfileVersion: If existing storage was written by an
            incompatible program version.
        OSError: If QSettings write or post-commit verification fails.
    """
    assert threading.current_thread() is threading.main_thread(), \
        "persist_profiles must be called on the main thread"
    if persistence_blocked:
        # SAFETY: defense-in-depth read-only protection. Callers are expected
        # to check persistence_blocked themselves and pass the flag through;
        # this backstop silently skips the write so an incompatible/invalid
        # config is never overwritten by a stray call path.
        return ""
    profiles, _changed = migrate_profiles(profiles)
    # 映射全局化：以当前方案为权威（UI 刚写入的就是它），先在校验范围内覆盖所有方案，
    # **校验通过后**才写共享键——被拒绝的保存不得改变任何持久状态（FR-20261021-003）。
    shared_mapping_payload: dict[str, Any] = {}
    if isinstance(profiles, dict):
        source = profiles.get(current_profile)
        if isinstance(source, dict):
            shared_mapping_payload = _mapping_payload(source)
            if shared_mapping_payload:
                for profile in profiles.values():
                    if isinstance(profile, dict):
                        profile.update(shared_mapping_payload)
    error = validate_profiles_payload(profiles)
    if error:
        raise ValueError(error)
    if current_profile not in profiles:
        raise ValueError(f"当前方案不存在：{current_profile}")
    if shared_mapping_payload:
        settings.setValue(
            GLOBAL_MAPPING_KEY, json.dumps(shared_mapping_payload, ensure_ascii=False)
        )
    payload_text, signature = profiles_signature(profiles, current_profile)
    try:
        main_record, main_error = _load_settings_record(settings)
    except UnsupportedProfileVersion as error:
        raise UnsupportedProfileVersion(
            f"主配置由不兼容的程序版本写入，已拒绝覆盖：{error}"
        ) from error
    backup_count = normalize_profile_backup_count(backup_count)
    existing_record = None
    if backup_count > 0:
        existing_record, existing_error = _load_profiles_backup_record(backup_path)
        if _is_unsupported_profile_message(existing_error):
            raise UnsupportedProfileVersion(
                "自动备份由不兼容的程序版本写入，已拒绝覆盖："
                f"{existing_error.removeprefix(UNSUPPORTED_PROFILE_PREFIX)}"
            )
    if main_record is None and _is_unsupported_profile_message(main_error):
        raise UnsupportedProfileVersion(main_error)
    existing_signature = existing_record.signature if existing_record else ""
    if existing_signature and existing_signature != signature:
        archive_existing_backup(backup_path, limit=max(0, backup_count - 1))
    generation = max(
        main_record.generation if main_record else 0,
        existing_record.generation if existing_record else 0,
    ) + 1
    if backup_count > 0:
        # INVARIANT: atomically write the signed recovery file before committing
        # QSettings. A newer backup generation is how startup detects a main
        # settings write that was interrupted halfway through. See ADR 0001.
        atomic_write_json(
            backup_path,
            profiles_backup_payload(profiles, current_profile, generation),
        )
        prune_profile_backups(backup_path, backup_count)
    settings.setValue("profiles_json", payload_text)
    settings.setValue("current_profile", current_profile)
    settings.setValue("profiles_signature", signature)
    settings.setValue("profiles_generation", generation)
    settings.setValue(
        "profiles_commit_json",
        json.dumps(
            _settings_commit_payload(profiles, current_profile, generation),
            ensure_ascii=False,
        ),
    )
    settings.sync()
    if settings.status() != QSettings.Status.NoError:  # type: ignore[attr-defined]
        raise OSError(f"QSettings 写入失败，状态码 {settings.status()}")
    committed, commit_error = _load_settings_record(settings)
    if (
        committed is None
        or committed.generation != generation
        or committed.signature != signature
    ):
        raise OSError(f"QSettings 提交校验失败：{commit_error or '内容不一致'}")
    if backup_count == 0:
        prune_profile_backups(backup_path, 0)
    return signature
