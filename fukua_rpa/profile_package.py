"""Validated profile-package import/export independent from Qt dialogs.

Complete migration and redacted sharing are intentionally different products:
the former preserves runnable text/assets, the latter removes private content.
Secret VALUES never enter a package (they live in the per-user DPAPI store).
Credential names are workflow references rather than secret values. Complete
migration and normal batch exports preserve those names so the recipient can
recreate the same credentials locally; redacted sharing still removes them.
See ADR 0024 and ``docs/设计基准.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .config_schema import migrate_profile_config
from .constants import (
    APP_VERSION,
    FULL_PACKAGE_FORMAT,
    LEGACY_FULL_PACKAGE_FORMAT,
    MAX_PACKAGE_FILE_BYTES,
    MAX_PACKAGE_FILES,
    MAX_PACKAGE_MANIFEST_BYTES,
    MAX_PACKAGE_TOTAL_BYTES,
    MAX_PROFILE_JSON_BYTES,
    MAX_PROFILE_STORE_JSON_BYTES,
    MAX_PROFILES,
    PROFILE_COLLECTION_FORMAT,
    PROFILE_COLLECTION_PACKAGE_FORMAT,
    SUPPORTED_WINDOWS_TEXT,
)
from .json_safety import load_bounded_json_file, loads_bounded_json
from .profile_paths import managed_path_reference, resolve_profile_path
from .profile_structure import validate_profile_config_data
from .task_images import iter_task_image_paths, iter_task_image_refs
from .workflow_security import sanitize_profile_for_sharing
from .zip_member_count import (
    CentralDirectoryUnreadable,
    PackageTooLarge,
    Zip64RecordUnreadable,
    bounded_precheck,
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp"}
PACKAGE_VERSION = 4
MIN_VERIFIABLE_PACKAGE_VERSION = 3


def _dump_package_json(data: Any) -> bytes:
    """统一序列化方案包 JSON 条目（供 ZIP writestr 使用）。

    集中 ensure_ascii=False、indent=2 设置，避免散落的 json.dumps 参数不一致。
    json_safety 目前未提供有界的 dumps，故仍走 json.dumps，但统一经由本 helper。

    SAFETY/FR-009：``allow_nan=False`` 必须保留。导入侧 ``loads_bounded_json``
    会拒绝 NaN/Infinity 字面量；导出侧若允许写出它们，就会生成"导出成功但本
    版本无法导入"的残缺包（ARCHITECTURE #48）。json.dumps 的默认值
    ``allow_nan=True`` 曾使该不对称成立。
    """
    return json.dumps(
        data, ensure_ascii=False, indent=2, allow_nan=False
    ).encode("utf-8")


@dataclass(frozen=True)
class PackageExportResult:
    """Result of exporting a single-profile full or redacted package.

    Attributes:
        path: Absolute path of the written ZIP file.
        asset_count: Number of image assets packed into the archive.
        missing_images: Image paths that could not be found (empty on success).
        privacy_protected: True when the package was redacted for sharing.
        redacted_step_count: Number of steps modified during redaction.
    """

    path: str
    asset_count: int
    missing_images: tuple[str, ...]
    privacy_protected: bool = True
    redacted_step_count: int = 0


@dataclass(frozen=True)
class PackageImportResult:
    """Result of importing a single-profile full package.

    Attributes:
        profile: The migrated profile dict with image paths re-pointed to the
            extracted asset directory.
        suggested_name: Suggested display name taken from the manifest.
        package_dir: Directory holding the extracted package assets.
        privacy_protected: True when the source package was redacted.
    """

    profile: dict[str, Any]
    suggested_name: str
    package_dir: str
    privacy_protected: bool = False


@dataclass(frozen=True)
class CollectionPackageExportResult:
    """Result of exporting a multi-profile collection package.

    Attributes:
        path: Absolute path of the written ZIP file.
        profile_count: Number of profiles packed.
        asset_count: Number of deduplicated image assets packed.
        privacy_protected: True when the package was redacted for sharing.
        redacted_step_count: Total steps modified during redaction.
    """

    path: str
    profile_count: int
    asset_count: int
    privacy_protected: bool = False
    redacted_step_count: int = 0


@dataclass(frozen=True)
class CollectionPackageImportResult:
    """Result of importing a multi-profile collection package.

    Attributes:
        profiles: Ordered ``(name, profile)`` tuples with image paths re-pointed
            to the extracted asset directory.
        package_dir: Directory holding the extracted package assets.
        privacy_protected: True when the source package was redacted.
    """

    profiles: tuple[tuple[str, dict[str, Any]], ...]
    package_dir: str
    privacy_protected: bool = False


class MissingPackageAssetsError(ValueError):
    """Raised when image assets referenced by a profile cannot be packed.

    Attributes:
        paths: Tuple of the missing image path strings.
    """

    def __init__(self, paths: Iterable[object]) -> None:
        """Initialize with the collection of missing image paths.

        Args:
            paths: Missing image paths; each is coerced to ``str``.
        """
        self.paths = tuple(str(path) for path in paths)
        super().__init__(f"有 {len(self.paths)} 个图片路径不存在，未生成全量包。")


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bytes_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_asset_to_archive(archive, source_path, relative_path, total_written):
    digest = hashlib.sha256()
    size = 0
    try:
        source = open(source_path, "rb")  # noqa: SIM115
    except OSError as error:
        raise MissingPackageAssetsError([source_path]) from error
    with source, archive.open(relative_path, "w") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_PACKAGE_FILE_BYTES:
                raise ValueError(
                    f"全量包资产过大：{source_path}（单文件上限 "
                    f"{MAX_PACKAGE_FILE_BYTES / 1024 / 1024:.0f} MB）"
                )
            total_written += len(chunk)
            if total_written > MAX_PACKAGE_TOTAL_BYTES:
                raise ValueError(
                    "全量包内容总大小超过上限 "
                    f"{MAX_PACKAGE_TOTAL_BYTES / 1024 / 1024:.0f} MB"
                )
            digest.update(chunk)
            output.write(chunk)
    return {"sha256": digest.hexdigest(), "size": size}, total_written


def asset_export_name(path: str) -> str:
    """Derive a stable, safe archive member name for an image asset.

    The basename is prefixed with the first 12 hex chars of the absolute path's
    SHA-1 so distinct source paths almost never collide, and any Windows-unsafe
    character is replaced. A genuine collision is still detected later by the
    casefold check in the exporter, which raises instead of overwriting.

    Args:
        path: Absolute path of the source image.

    Returns:
        A sanitized ``<digest>_<basename>`` string for use under ``assets/``.
    """
    absolute = os.path.abspath(path)
    # SHA-1 first 12 hex chars (48 bits) as dedup prefix. Collision is
    # detected at line ~287 (casefold check raises ValueError), so even
    # in the astronomically unlikely event of a collision, it fails loudly.
    digest = hashlib.sha1(absolute.encode("utf-8", errors="ignore")).hexdigest()[:12]
    base = os.path.basename(absolute)
    safe_base = "".join(character if character not in '<>:"/\\|?*' else "_" for character in base)
    return f"{digest}_{safe_base.strip() or 'image'}"


def rewrite_profile_image_paths(
    config: Mapping[str, Any], mapper: Callable[[str], str | None]
) -> dict[str, Any]:
    """Return a migrated profile with every image field rewritten via *mapper*.

    Each image reference discovered by ``iter_task_image_refs`` is passed to
    *mapper*; a non-empty result replaces the stored value, while ``None``
    leaves the original text untouched. Used both to point exports at packed
    asset names and to point imports at extracted asset paths.

    Args:
        config: Raw profile mapping to rewrite.
        mapper: Callback mapping an image path to its replacement, or ``None``
            to keep the original.

    Returns:
        A new, migrated profile dict with rewritten image fields.
    """
    data = migrate_profile_config(config).value

    def rewrite(path):
        text = str(path or "").strip()
        if not text:
            return path
        mapped = mapper(text)
        return mapped if mapped else path

    for task in data.get("tasks", []):
        for kind, container, key, path in iter_task_image_refs(task):
            # ``value`` is rewritten from its stripped form (historical
            # behavior); condition and multi-image fields pass the raw stored
            # value so an unmapped path keeps its original text verbatim.
            source = path if kind == "value" else container.get(key, "")
            container[key] = rewrite(source)
    return data


def collect_profile_image_paths(config: Mapping[str, Any]) -> list[str]:
    """Return the deduplicated image paths referenced across all tasks.

    Paths are compared case-insensitively by their absolute, normalized form so
    the same file referenced twice is packed once, while the first-seen original
    spelling is preserved in the result order.

    Args:
        config: Profile mapping whose tasks are scanned for image references.

    Returns:
        An order-preserving list of unique image path strings.
    """
    paths: list[str] = []
    for task in config.get("tasks", []):
        paths.extend(iter_task_image_paths(task))
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        normalized = os.path.normcase(os.path.abspath(path))
        if normalized not in seen:
            seen.add(normalized)
            result.append(path)
    return result


def build_profile_collection_payload(
    profiles: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Build an ordered, versioned JSON payload for several profiles."""

    if not profiles:
        raise ValueError("至少需要选择一个方案")
    if len(profiles) > MAX_PROFILES:
        raise ValueError(f"批量方案数量超过上限 {MAX_PROFILES}")
    entries = []
    seen = set()
    for raw_name, config in profiles.items():
        name = str(raw_name or "").strip()
        folded = name.casefold()
        if not name:
            raise ValueError("批量方案中包含空名称")
        if folded in seen:
            raise ValueError(f"批量方案包含重复名称：{name}")
        seen.add(folded)
        entries.append(
            {"name": name, "profile": migrate_profile_config(config).value}
        )
    return {
        "format": PROFILE_COLLECTION_FORMAT,
        "version": 1,
        "profiles": entries,
    }


def parse_profile_collection_payload(data: object) -> list[tuple[str, dict[str, Any]]] | None:
    """Return ordered collection entries, or None for a normal single profile."""

    if not isinstance(data, Mapping) or data.get("format") != PROFILE_COLLECTION_FORMAT:
        return None
    try:
        version = int(data.get("version", 0) or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("方案集合版本无效") from error
    if version != 1:
        raise ValueError(f"不支持的方案集合版本：{version}")
    raw_entries = data.get("profiles")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("方案集合中没有可导入的方案")
    if len(raw_entries) > MAX_PROFILES:
        raise ValueError(f"方案集合数量超过上限 {MAX_PROFILES}")
    result = []
    seen = set()
    for index, entry in enumerate(raw_entries, 1):
        if not isinstance(entry, Mapping):
            raise ValueError(f"方案集合第 {index} 项格式错误")
        name = str(entry.get("name", "") or "").strip()
        profile = entry.get("profile")
        if not name:
            raise ValueError(f"方案集合第 {index} 项缺少名称")
        if name.casefold() in seen:
            raise ValueError(f"方案集合包含重复名称：{name}")
        if not isinstance(profile, Mapping):
            raise ValueError(f"方案“{name}”正文格式错误")
        seen.add(name.casefold())
        result.append((name, migrate_profile_config(profile).value))
    return result


def export_full_package(
    config: Mapping[str, Any],
    profile_name: str,
    destination: str,
    *,
    privacy_protected: bool = True,
    base_dir: str | None = None,
) -> PackageExportResult:
    """Export either a complete migration ZIP or a redacted sharing ZIP.

    DESIGN: ``privacy_protected=False`` deliberately keeps ordinary workflow
    text and template images so the package remains runnable on another PC.
    The UI presents that mode as “完整迁移包” and warns on first use.
    """
    destination = os.path.abspath(destination)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    if privacy_protected:
        privacy_result = sanitize_profile_for_sharing(config)
        export_config = privacy_result.profile
        redacted_step_count = len(privacy_result.redacted_steps)
    else:
        # DESIGN: a credential name is a portable workflow reference, not the
        # DPAPI-protected value. Keeping it lets another PC recreate the same
        # binding without editing every secret-text step.
        export_config = migrate_profile_config(config).value
        redacted_step_count = 0
    base_root = os.path.abspath(base_dir or os.getcwd())
    asset_map: dict[str, tuple[str, str]] = {}
    missing: list[str] = []
    folded_asset_names: set[str] = set()
    for image_path in collect_profile_image_paths(export_config):
        resolved_path = resolve_profile_path(image_path, base_root)
        if not os.path.isfile(resolved_path):
            missing.append(image_path)
            continue
        absolute = os.path.abspath(resolved_path)
        # FR-008: 同一物理文件被托管引用（fukua-data://...）与旧版遗留绝对路径
        # 同时引用时，两者在收集阶段是不同字符串（abspath 无法解析托管前缀），
        # 去重键不同因而都保留下来；解析后却指向同一文件。此处复用集合包
        # （export_collection_package）已有的同文件守卫，保证两入口行为一致：
        # 同一文件只打包一次，而不是抛出误导性的"资产名称冲突"。
        normalized = os.path.normcase(absolute)
        if normalized in asset_map:
            continue
        relative = f"assets/{asset_export_name(absolute)}"
        if relative.casefold() in folded_asset_names:
            raise ValueError(f"全量包资产名称冲突：{relative}")
        folded_asset_names.add(relative.casefold())
        asset_map[normalized] = (absolute, relative)

    if missing:
        raise MissingPackageAssetsError(missing)
    if len(asset_map) + 2 > MAX_PACKAGE_FILES:
        raise ValueError(f"全量包文件数量超过上限 {MAX_PACKAGE_FILES}")

    def packaged_path(source):
        resolved = os.path.normcase(
            os.path.abspath(resolve_profile_path(source, base_root))
        )
        entry = asset_map.get(resolved)
        return entry[1] if entry else None

    packaged = rewrite_profile_image_paths(export_config, packaged_path)
    # FR-009: 导入侧会在落盘前调用 validate_profile_config_data（见
    # import_full_package）；导出侧此前没有对应校验，可能生成"导出成功但本版本
    # 无法导入"的残缺包（ARCHITECTURE #48）。此处对齐，且校验发生在创建临时 ZIP
    # 之前，失败时不触碰目标文件。
    structural_error = validate_profile_config_data(packaged, label="全量包方案")
    if structural_error:
        raise ValueError(structural_error)
    profile_bytes = _dump_package_json(packaged)
    if len(profile_bytes) > MAX_PROFILE_JSON_BYTES:
        raise ValueError(
            "方案正文大小超过上限 "
            f"{MAX_PROFILE_JSON_BYTES / 1024 / 1024:.0f} MB"
        )
    descriptor, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(destination)}.", suffix=".tmp", dir=os.path.dirname(destination)
    )
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("profile.json", profile_bytes)
            total_written = len(profile_bytes)
            asset_metadata: dict[str, dict[str, Any]] = {}
            for absolute, relative in asset_map.values():
                metadata, total_written = _write_asset_to_archive(
                    archive,
                    absolute,
                    relative,
                    total_written,
                )
                asset_metadata[relative] = metadata
            manifest = {
                "format": FULL_PACKAGE_FORMAT,
                "version": PACKAGE_VERSION,
                "app_version": APP_VERSION,
                "supported_windows": SUPPORTED_WINDOWS_TEXT,
                "profile_name": (
                    "脱敏方案" if privacy_protected else str(profile_name)
                ),
                "privacy_protected": bool(privacy_protected),
                # SAFETY: credential secret values live outside profile data
                # and must never be added to package assets or manifests.
                "credentials_included": False,
                "redacted_step_count": redacted_step_count,
                "asset_count": len(asset_map),
                "missing_images": missing,
                "assets": asset_metadata,
                "profile": {
                    "sha256": _bytes_sha256(profile_bytes),
                    "size": len(profile_bytes),
                },
            }
            manifest_bytes = _dump_package_json(manifest)
            if len(manifest_bytes) > MAX_PACKAGE_MANIFEST_BYTES:
                raise ValueError(
                    "全量包清单大小超过上限 "
                    f"{MAX_PACKAGE_MANIFEST_BYTES / 1024 / 1024:.0f} MB"
                )
            if total_written + len(manifest_bytes) > MAX_PACKAGE_TOTAL_BYTES:
                raise ValueError(
                    "全量包内容总大小超过上限 "
                    f"{MAX_PACKAGE_TOTAL_BYTES / 1024 / 1024:.0f} MB"
                )
            archive.writestr("manifest.json", manifest_bytes)
        os.replace(temp_path, destination)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return PackageExportResult(
        destination,
        len(asset_map),
        tuple(missing),
        bool(privacy_protected),
        redacted_step_count,
    )


def export_collection_package(
    profiles: Mapping[str, Mapping[str, Any]],
    destination: str,
    *,
    privacy_protected: bool = False,
    base_dir: str | None = None,
) -> CollectionPackageExportResult:
    """Export ordered profiles and their deduplicated image assets to one ZIP."""

    payload = build_profile_collection_payload(profiles)
    prepared_entries = []
    redacted_step_count = 0
    for index, entry in enumerate(payload["profiles"], 1):
        if privacy_protected:
            privacy_result = sanitize_profile_for_sharing(entry["profile"])
            profile = privacy_result.profile
            name = f"脱敏方案 {index}"
            redacted_step_count += len(privacy_result.redacted_steps)
        else:
            # Normal batch migration preserves credential names but never has
            # access to the DPAPI secret values stored outside profile data.
            profile = migrate_profile_config(entry["profile"]).value
            name = entry["name"]
        prepared_entries.append({"name": name, "profile": profile})

    destination = os.path.abspath(destination)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    base_root = os.path.abspath(base_dir or os.getcwd())
    asset_map: dict[str, tuple[str, str]] = {}
    missing: list[str] = []
    folded_asset_names: set[str] = set()
    for entry in prepared_entries:
        for image_path in collect_profile_image_paths(entry["profile"]):
            resolved_path = resolve_profile_path(image_path, base_root)
            if not os.path.isfile(resolved_path):
                if image_path not in missing:
                    missing.append(image_path)
                continue
            absolute = os.path.abspath(resolved_path)
            normalized = os.path.normcase(absolute)
            if normalized in asset_map:
                continue
            relative = f"assets/{asset_export_name(absolute)}"
            if relative.casefold() in folded_asset_names:
                raise ValueError(f"全量包资产名称冲突：{relative}")
            folded_asset_names.add(relative.casefold())
            asset_map[normalized] = (absolute, relative)
    if missing:
        raise MissingPackageAssetsError(missing)
    if len(asset_map) + 2 > MAX_PACKAGE_FILES:
        raise ValueError(f"方案集合文件数量超过上限 {MAX_PACKAGE_FILES}")

    def packaged_path(source):
        resolved = os.path.normcase(
            os.path.abspath(resolve_profile_path(source, base_root))
        )
        entry = asset_map.get(resolved)
        return entry[1] if entry else None

    packaged_profiles = []
    for entry in prepared_entries:
        rewritten = rewrite_profile_image_paths(entry["profile"], packaged_path)
        # FR-009: 与全量包导出对齐——导出前做与导入侧（import_collection_package）
        # 相同的结构校验，避免生成"导出成功但本版本无法导入"的残缺包
        # （ARCHITECTURE #48）。校验发生在创建临时 ZIP 之前，失败不触碰目标文件。
        structural_error = validate_profile_config_data(
            rewritten, label=f"方案集合包中的方案“{entry['name']}”"
        )
        if structural_error:
            raise ValueError(structural_error)
        packaged_profiles.append({"name": entry["name"], "profile": rewritten})

    packaged_payload = {
        "format": PROFILE_COLLECTION_FORMAT,
        "version": 1,
        "profiles": packaged_profiles,
    }
    profiles_bytes = _dump_package_json(packaged_payload)
    if len(profiles_bytes) > MAX_PROFILE_STORE_JSON_BYTES:
        raise ValueError(
            "方案集合正文大小超过上限 "
            f"{MAX_PROFILE_STORE_JSON_BYTES / 1024 / 1024:.0f} MB"
        )

    descriptor, temp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(destination)}.",
        suffix=".tmp",
        dir=os.path.dirname(destination),
    )
    os.close(descriptor)
    try:
        with zipfile.ZipFile(
            temp_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("profiles.json", profiles_bytes)
            total_written = len(profiles_bytes)
            asset_metadata: dict[str, dict[str, Any]] = {}
            for absolute, relative in asset_map.values():
                metadata, total_written = _write_asset_to_archive(
                    archive, absolute, relative, total_written
                )
                asset_metadata[relative] = metadata
            manifest = {
                "format": PROFILE_COLLECTION_PACKAGE_FORMAT,
                "version": 1,
                "app_version": APP_VERSION,
                "supported_windows": SUPPORTED_WINDOWS_TEXT,
                "profile_count": len(prepared_entries),
                "privacy_protected": bool(privacy_protected),
                "credentials_included": False,
                "redacted_step_count": redacted_step_count,
                "asset_count": len(asset_map),
                "assets": asset_metadata,
                "profiles": {
                    "sha256": _bytes_sha256(profiles_bytes),
                    "size": len(profiles_bytes),
                },
            }
            manifest_bytes = _dump_package_json(manifest)
            if len(manifest_bytes) > MAX_PACKAGE_MANIFEST_BYTES:
                raise ValueError("方案集合清单大小超过安全上限")
            if total_written + len(manifest_bytes) > MAX_PACKAGE_TOTAL_BYTES:
                raise ValueError("方案集合内容总大小超过安全上限")
            archive.writestr("manifest.json", manifest_bytes)
        os.replace(temp_path, destination)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return CollectionPackageExportResult(
        destination,
        len(prepared_entries),
        len(asset_map),
        bool(privacy_protected),
        redacted_step_count,
    )


# ZIP central-directory pre-check. FR-SCOPE005-002: the member-count limit must
# be a precondition of reading the central directory, not a follow-up to it.
# The bounded pre-check itself lives in ``zip_member_count`` so the profile and
# the plugin importers share one implementation (FR-20261005-602, A2).
def _reject_oversized_package(zip_path: str) -> None:
    """Reject a package that exceeds ``MAX_PACKAGE_FILES`` before it is opened.

    FR-SCOPE005-002: called before the ZIP directory is read so the limit bounds
    the materialization itself. The post-open check in
    :func:`_safe_extract_package` is kept as the authoritative guard for packages
    whose declared count could not be read at all (no usable EOCD record).

    FR-20261005-501: a package whose EOCD declares ZIP64 (``0xFFFF``) but whose
    ZIP64 records cannot be read is rejected here rather than being allowed to
    fall back to the post-open check — otherwise a crafted EOCD bypasses the
    pre-read and the directory is materialized anyway.

    A2 (维护者裁定2026-10-07): the maintainer rejected both "trust the declared
    count" and "adding a ``size_cd`` check is sufficient proof". This now calls
    :func:`zip_member_count.bounded_precheck`, which *proves* the bound from the
    declared count, the directory size, a bounded record walk, and a count
    cross-check. The cheap declared-count read is kept only as the explicit
    fast path so the error message can name the declared figure.
    """
    try:
        with open(zip_path, "rb") as handle:
            bounded_precheck(handle, MAX_PACKAGE_FILES)
    except PackageTooLarge as error:
        detail = f"（声明 {error.declared}）" if error.declared else ""
        raise ValueError(f"压缩包文件数量超过上限 {MAX_PACKAGE_FILES}{detail}") from error
    except (Zip64RecordUnreadable, CentralDirectoryUnreadable):
        raise
    except OSError:
        return


def _safe_member_name(raw_name: str) -> str:
    name = str(raw_name).replace("\\", "/")
    parts = name.split("/")
    reserved_names = {"CON", "PRN", "AUX", "NUL"}
    reserved_names.update(f"COM{index}" for index in range(1, 10))
    reserved_names.update(f"LPT{index}" for index in range(1, 10))
    unsafe_windows_name = any(
        ":" in part
        or part.rstrip(" .") != part
        or part.split(".", 1)[0].upper() in reserved_names
        for part in parts
    )
    if (
        not name
        or name.startswith("/")
        or any(part in ("", ".", "..") for part in parts)
        or unsafe_windows_name
    ):
        raise ValueError(f"压缩包包含不安全路径：{raw_name}")
    return name


def _safe_extract_package(
    zip_path: str, target_dir: str, *, payload_names: set[str]
) -> None:
    target = os.path.abspath(target_dir)
    os.makedirs(target, exist_ok=True)
    # FR-SCOPE005-002: 上限必须在读取中央目录**之前**生效，否则 ZipFile() 会先
    # 把每个成员物化成 ZipInfo，上限只约束不了物化本身。
    _reject_oversized_package(zip_path)
    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()
        if len(members) > MAX_PACKAGE_FILES:
            raise ValueError(f"压缩包文件数量超过上限 {MAX_PACKAGE_FILES}")
        declared_total = sum(max(0, int(member.file_size)) for member in members if not member.is_dir())
        if declared_total > MAX_PACKAGE_TOTAL_BYTES:
            raise ValueError(
                f"压缩包解压后预计超过 {MAX_PACKAGE_TOTAL_BYTES / 1024 / 1024:.0f} MB 上限"
            )
        seen: set[str] = set()
        actual_total = 0
        for member in members:
            if member.is_dir():
                directory_name = member.filename.rstrip("/\\")
                if directory_name:
                    safe_directory = _safe_member_name(directory_name)
                    if not (
                        safe_directory == "assets"
                        or safe_directory.startswith("assets/")
                    ):
                        raise ValueError(
                            f"压缩包包含不允许的目录：{member.filename}"
                        )
                continue
            name = _safe_member_name(member.filename)
            allowed_root_files = {"manifest.json", *payload_names}
            if name not in allowed_root_files and not name.startswith("assets/"):
                raise ValueError(f"压缩包包含不允许的额外文件：{member.filename}")
            folded = name.casefold()
            if folded in seen:
                raise ValueError(f"压缩包包含重复路径：{member.filename}")
            seen.add(folded)
            if member.file_size > MAX_PACKAGE_FILE_BYTES:
                raise ValueError(
                    f"压缩包中的文件过大：{member.filename}（单文件上限 "
                    f"{MAX_PACKAGE_FILE_BYTES / 1024 / 1024:.0f} MB）"
                )
            unix_mode = (int(member.external_attr) >> 16) & 0o170000
            if unix_mode == 0o120000:
                raise ValueError(f"压缩包不允许符号链接：{member.filename}")
            destination = os.path.abspath(os.path.join(target, *name.split("/")))
            if not (destination == target or destination.startswith(target + os.sep)):
                raise ValueError(f"压缩包路径越界：{member.filename}")
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with archive.open(member) as source, open(destination, "wb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    actual_total += len(chunk)
                    if actual_total > MAX_PACKAGE_TOTAL_BYTES:
                        raise ValueError("压缩包实际解压大小超过安全上限")
                    output.write(chunk)


def safe_extract_full_package(zip_path: str, target_dir: str) -> None:
    """Securely extract a single-profile package archive into *target_dir*.

    Delegates to ``_safe_extract_package`` allowing only ``profile.json``,
    ``manifest.json`` and files under ``assets/``, after validating member
    names, sizes, symlink rejection and path containment.

    Args:
        zip_path: Path of the package ZIP to extract.
        target_dir: Directory to extract into (created if missing).

    Raises:
        ValueError: If the archive violates any safety or layout constraint.
    """
    _safe_extract_package(
        zip_path, target_dir, payload_names={"profile.json"}
    )


def _verify_assets(package_dir: str, manifest: Mapping[str, Any]) -> set[str]:
    metadata = manifest.get("assets", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("全量包资产清单格式错误")
    declared: set[str] = set()
    folded_declared: set[str] = set()
    for relative, expected in metadata.items():
        if not isinstance(expected, Mapping):
            raise ValueError(f"全量包资产记录格式错误：{relative}")
        name = _safe_member_name(str(relative))
        if not name.startswith("assets/"):
            raise ValueError(f"全量包资产路径无效：{relative}")
        if name.casefold() in folded_declared:
            raise ValueError(f"全量包资产清单包含重复路径：{relative}")
        declared.add(name)
        folded_declared.add(name.casefold())
        path = os.path.abspath(os.path.join(package_dir, *name.split("/")))
        if not os.path.isfile(path):
            raise ValueError(f"全量包缺少资产：{relative}")
        expected_size = int(expected.get("size", -1))
        if expected_size != os.path.getsize(path):
            raise ValueError(f"全量包资产大小校验失败：{relative}")
        expected_hash = str(expected.get("sha256", "")).lower()
        if (
            len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
            or _file_sha256(path).lower() != expected_hash
        ):
            raise ValueError(f"全量包资产完整性校验失败：{relative}")
    assets_root = os.path.join(package_dir, "assets")
    actual: set[str] = set()
    if os.path.isdir(assets_root):
        for root, _directories, files in os.walk(assets_root):
            for filename in files:
                path = os.path.join(root, filename)
                relative = os.path.relpath(path, package_dir).replace("\\", "/")
                actual.add(_safe_member_name(relative))
    if {name.casefold() for name in actual} != folded_declared:
        extras = sorted(actual - declared)
        missing = sorted(declared - actual)
        detail = []
        if extras:
            detail.append("未声明资产：" + "、".join(extras[:5]))
        if missing:
            detail.append("缺失资产：" + "、".join(missing[:5]))
        raise ValueError("全量包资产清单与实际文件不一致（" + "；".join(detail) + "）")
    try:
        asset_count = int(manifest.get("asset_count", -1))
    except (TypeError, ValueError) as error:
        raise ValueError("全量包资产数量无效") from error
    if asset_count != len(declared):
        raise ValueError("全量包资产数量与清单不一致")
    return declared


def _verify_profile_payload(
    profile_path: str, manifest: Mapping[str, Any], version: int
) -> bytes:
    if os.path.getsize(profile_path) > MAX_PROFILE_JSON_BYTES:
        raise ValueError(
            "全量包方案正文超过上限 "
            f"{MAX_PROFILE_JSON_BYTES / 1024 / 1024:.0f} MB"
        )
    with open(profile_path, "rb") as handle:
        profile_bytes = handle.read()
    if version < 4:
        return profile_bytes
    metadata = manifest.get("profile")
    if not isinstance(metadata, Mapping):
        raise ValueError("全量包缺少方案正文校验信息")
    try:
        expected_size = int(metadata.get("size", -1))
    except (TypeError, ValueError) as error:
        raise ValueError("全量包方案正文大小无效") from error
    expected_hash = str(metadata.get("sha256", "") or "").lower()
    if expected_size != len(profile_bytes):
        raise ValueError("全量包方案正文大小校验失败")
    if (
        len(expected_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_hash)
        or _bytes_sha256(profile_bytes) != expected_hash
    ):
        raise ValueError("全量包方案正文完整性校验失败")
    return profile_bytes


def import_full_package(zip_path: str, base_dir: str) -> PackageImportResult:
    """Atomically extract, verify and import a single-profile package.

    The archive is extracted to a staging directory, validated against its
    manifest (format, version, asset hashes/sizes and profile payload hash for
    verifiable versions), and only moved into ``imported_assets`` on success.
    Image references are re-pointed to portable ``fukua-data://`` paths under
    *base_dir*. On any failure the staging directory is removed.

    Args:
        zip_path: Path of the package ZIP to import.
        base_dir: Root of the managed data folder for asset references.

    Returns:
        A ``PackageImportResult`` with the migrated profile and package dir.

    Raises:
        ValueError: If the package is malformed, fails verification, or
            references undeclared/unused assets.
    """
    base_name = os.path.splitext(os.path.basename(zip_path))[0]
    import_root = os.path.join(os.path.abspath(base_dir), "imported_assets")
    os.makedirs(import_root, exist_ok=True)
    package_dir = os.path.join(import_root, base_name)
    counter = 1
    while os.path.exists(package_dir):
        if counter > 10000:
            raise ValueError(
                "imported_assets 中可用目录名过多，请清理 imported_assets 后重试"
            )
        package_dir = os.path.join(import_root, f"{base_name}_{counter}")
        counter += 1
    staging = f"{package_dir}.__extracting"
    if os.path.exists(staging):
        shutil.rmtree(staging)
    try:
        safe_extract_full_package(zip_path, staging)
        profile_path = os.path.join(staging, "profile.json")
        manifest_path = os.path.join(staging, "manifest.json")
        if not os.path.isfile(profile_path):
            raise ValueError("全量包缺少 profile.json")
        if not os.path.isfile(manifest_path):
            raise ValueError("全量包缺少 manifest.json")
        manifest = load_bounded_json_file(
            manifest_path,
            max_bytes=MAX_PACKAGE_MANIFEST_BYTES,
            label="全量包清单",
            max_depth=16,
            max_nodes=MAX_PACKAGE_FILES * 8,
        )
        accepted = {FULL_PACKAGE_FORMAT, LEGACY_FULL_PACKAGE_FORMAT}
        if not isinstance(manifest, dict) or manifest.get("format") not in accepted:
            raise ValueError("无法识别的全量包格式")
        try:
            version = int(manifest.get("version", 0) or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("全量包版本无效") from error
        if version <= 0:
            raise ValueError("全量包缺少有效版本")
        if version > PACKAGE_VERSION:
            raise ValueError(
                f"全量包版本 {version} 高于本程序支持的版本 {PACKAGE_VERSION}"
            )
        if manifest.get("credentials_included") not in (False, None):
            raise ValueError("全量包声明包含凭据，本程序拒绝导入")
        declared_assets = set()
        if version >= MIN_VERIFIABLE_PACKAGE_VERSION:
            declared_assets = _verify_assets(staging, manifest)
        profile_bytes = _verify_profile_payload(profile_path, manifest, version)
        data = loads_bounded_json(
            profile_bytes,
            max_bytes=MAX_PROFILE_JSON_BYTES,
            label="全量包方案正文",
        )
        referenced_assets: set[str] = set()

        def import_mapper(source):
            text = str(source or "").strip().replace("\\", "/")
            if not text.startswith("assets/"):
                return None
            safe_name = _safe_member_name(text)
            if version >= MIN_VERIFIABLE_PACKAGE_VERSION and safe_name not in declared_assets:
                raise ValueError(f"全量包引用了未声明的图片：{text}")
            referenced_assets.add(safe_name)
            candidate = os.path.abspath(os.path.join(package_dir, *safe_name.split("/")))
            assets_root = os.path.abspath(os.path.join(package_dir, "assets"))
            if not candidate.startswith(assets_root + os.sep):
                raise ValueError(f"全量包引用了无效图片路径：{text}")
            staging_candidate = os.path.abspath(os.path.join(staging, *safe_name.split("/")))
            if not os.path.isfile(staging_candidate):
                raise ValueError(f"全量包引用了不存在的图片：{text}")
            return managed_path_reference(candidate, base_dir)

        profile = rewrite_profile_image_paths(data, import_mapper)
        if (
            version >= MIN_VERIFIABLE_PACKAGE_VERSION
            and referenced_assets != declared_assets
        ):
            unused = sorted(declared_assets - referenced_assets)
            raise ValueError(
                "全量包包含未被方案引用的资产：" + "、".join(unused[:5])
            )
        structural_error = validate_profile_config_data(profile, label="全量包方案")
        if structural_error:
            raise ValueError(structural_error)
        os.replace(staging, package_dir)
        suggested = str(manifest.get("profile_name", "")).strip() or base_name
        return PackageImportResult(
            profile,
            suggested,
            package_dir,
            manifest.get("privacy_protected") is True,
        )
    except Exception:
        if os.path.exists(staging):
            shutil.rmtree(staging, ignore_errors=True)
        raise


def inspect_package_format(zip_path: str) -> str:
    """Read only the bounded manifest needed to dispatch a package importer."""

    # FR-SCOPE005-002: 这是导入流程最先打开包的地方，上限必须在此之前生效。
    _reject_oversized_package(zip_path)
    with zipfile.ZipFile(zip_path, "r") as archive:
        members = archive.infolist()
        # FR-20261008-202: the pre-read limit returns "unvalidated" (no usable
        # EOCD record) for an archive whose EOCD is not at the file end, and this
        # dispatch stage is the *first* place a package is opened. The design
        # (docs/设计基准.md「导出侧的结构校验」) delegates that "unvalidated" case
        # to a post-open check, but only ``_safe_extract_package`` had one, so a
        # crafted package could materialize its whole central directory here
        # before any limit applied. Keep the same authoritative post-open guard
        # as ``_safe_extract_package`` so the bound holds on every open path.
        if len(members) > MAX_PACKAGE_FILES:
            raise ValueError(f"压缩包文件数量超过上限 {MAX_PACKAGE_FILES}")
        matches = [
            member
            for member in members
            if member.filename.replace("\\", "/").casefold()
            == "manifest.json"
        ]
        if len(matches) != 1:
            raise ValueError("压缩包必须且只能包含一个 manifest.json")
        member = matches[0]
        if member.file_size > MAX_PACKAGE_MANIFEST_BYTES:
            raise ValueError("压缩包清单大小超过安全上限")
        with archive.open(member) as source:
            raw = source.read(MAX_PACKAGE_MANIFEST_BYTES + 1)
        manifest = loads_bounded_json(
            raw,
            max_bytes=MAX_PACKAGE_MANIFEST_BYTES,
            label="压缩包清单",
            max_depth=16,
            max_nodes=MAX_PACKAGE_FILES * 8,
        )
    if not isinstance(manifest, Mapping):
        raise ValueError("压缩包清单格式错误")
    return str(manifest.get("format", "") or "")


def import_collection_package(
    zip_path: str, base_dir: str
) -> CollectionPackageImportResult:
    """Import an ordered multi-profile package as one atomic asset directory."""

    base_name = os.path.splitext(os.path.basename(zip_path))[0]
    import_root = os.path.join(os.path.abspath(base_dir), "imported_assets")
    os.makedirs(import_root, exist_ok=True)
    package_dir = os.path.join(import_root, base_name)
    counter = 1
    while os.path.exists(package_dir):
        if counter > 10000:
            raise ValueError(
                "imported_assets 中可用目录名过多，请清理 imported_assets 后重试"
            )
        package_dir = os.path.join(import_root, f"{base_name}_{counter}")
        counter += 1
    staging = f"{package_dir}.__extracting"
    if os.path.exists(staging):
        shutil.rmtree(staging)
    try:
        _safe_extract_package(
            zip_path, staging, payload_names={"profiles.json"}
        )
        payload_path = os.path.join(staging, "profiles.json")
        manifest_path = os.path.join(staging, "manifest.json")
        if not os.path.isfile(payload_path):
            raise ValueError("方案集合包缺少 profiles.json")
        if not os.path.isfile(manifest_path):
            raise ValueError("方案集合包缺少 manifest.json")
        manifest = load_bounded_json_file(
            manifest_path,
            max_bytes=MAX_PACKAGE_MANIFEST_BYTES,
            label="方案集合包清单",
            max_depth=16,
            max_nodes=MAX_PACKAGE_FILES * 8,
        )
        if (
            not isinstance(manifest, Mapping)
            or manifest.get("format") != PROFILE_COLLECTION_PACKAGE_FORMAT
        ):
            raise ValueError("无法识别的方案集合包格式")
        try:
            version = int(manifest.get("version", 0) or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("方案集合包版本无效") from error
        if version != 1:
            raise ValueError(f"不支持的方案集合包版本：{version}")
        if manifest.get("credentials_included") not in (False, None):
            raise ValueError("方案集合包声明包含凭据，本程序拒绝导入")
        declared_assets = _verify_assets(staging, manifest)

        if os.path.getsize(payload_path) > MAX_PROFILE_STORE_JSON_BYTES:
            raise ValueError("方案集合正文大小超过安全上限")
        with open(payload_path, "rb") as handle:
            payload_bytes = handle.read(MAX_PROFILE_STORE_JSON_BYTES + 1)
        payload_metadata = manifest.get("profiles")
        if not isinstance(payload_metadata, Mapping):
            raise ValueError("方案集合包缺少正文校验信息")
        try:
            expected_size = int(payload_metadata.get("size", -1))
        except (TypeError, ValueError) as error:
            raise ValueError("方案集合正文大小无效") from error
        expected_hash = str(payload_metadata.get("sha256", "") or "").lower()
        if expected_size != len(payload_bytes):
            raise ValueError("方案集合正文大小校验失败")
        if (
            len(expected_hash) != 64
            or any(c not in "0123456789abcdef" for c in expected_hash)
            or _bytes_sha256(payload_bytes) != expected_hash
        ):
            raise ValueError("方案集合正文完整性校验失败")
        payload = loads_bounded_json(
            payload_bytes,
            max_bytes=MAX_PROFILE_STORE_JSON_BYTES,
            # FR-20261011-006 / DOC-SCOPE005-001: 集合包正文的节点上限必须与导出侧
            # 口径一致（导出只校验聚合字节 + 逐方案节点），否则会出现"导出成功但本
            # 版本无法导入"的集合包。节点数 ≤ 字节数，故以字节上限作节点上限。
            max_nodes=MAX_PROFILE_STORE_JSON_BYTES,
            label="方案集合正文",
        )
        entries = parse_profile_collection_payload(payload)
        if entries is None:
            raise ValueError("方案集合正文格式错误")
        if int(manifest.get("profile_count", -1)) != len(entries):
            raise ValueError("方案集合数量与清单不一致")

        referenced_assets: set[str] = set()

        def import_mapper(source):
            text = str(source or "").strip().replace("\\", "/")
            if not text.startswith("assets/"):
                return None
            safe_name = _safe_member_name(text)
            if safe_name not in declared_assets:
                raise ValueError(f"方案集合引用了未声明的图片：{text}")
            referenced_assets.add(safe_name)
            candidate = os.path.abspath(
                os.path.join(package_dir, *safe_name.split("/"))
            )
            assets_root = os.path.abspath(os.path.join(package_dir, "assets"))
            if not candidate.startswith(assets_root + os.sep):
                raise ValueError(f"方案集合引用了无效图片路径：{text}")
            staging_candidate = os.path.abspath(
                os.path.join(staging, *safe_name.split("/"))
            )
            if not os.path.isfile(staging_candidate):
                raise ValueError(f"方案集合引用了不存在的图片：{text}")
            return managed_path_reference(candidate, base_dir)

        imported_entries = tuple(
            (name, rewrite_profile_image_paths(profile, import_mapper))
            for name, profile in entries
        )
        if referenced_assets != declared_assets:
            unused = sorted(declared_assets - referenced_assets)
            raise ValueError(
                "方案集合包包含未被方案引用的资产："
                + "、".join(unused[:5])
            )
        for name, profile in imported_entries:
            structural_error = validate_profile_config_data(
                profile, label=f"方案集合包中的方案“{name}”"
            )
            if structural_error:
                raise ValueError(structural_error)
        os.replace(staging, package_dir)
        return CollectionPackageImportResult(
            imported_entries,
            package_dir,
            manifest.get("privacy_protected") is True,
        )
    except Exception:
        if os.path.exists(staging):
            shutil.rmtree(staging, ignore_errors=True)
        raise


def discard_imported_package(package_dir: str, base_dir: str) -> bool:
    """Remove an unaccepted import only when it is inside imported_assets."""

    import_root = os.path.realpath(
        os.path.join(os.path.abspath(base_dir), "imported_assets")
    )
    target = os.path.realpath(str(package_dir or ""))
    try:
        inside = os.path.commonpath((import_root, target)) == import_root
    except ValueError:
        inside = False
    if not inside or target == import_root or not os.path.isdir(target):
        return False
    shutil.rmtree(target)
    return True
