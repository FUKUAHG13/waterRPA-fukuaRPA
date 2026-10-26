"""Bounded, security-checked plugin package installation and removal.

Implements the installation pre-checks from design section 5.3 BEFORE any file
is extracted: ZIP path traversal / absolute paths / symlinks / reserved device
names, single-file and total size caps, zip-bomb (compression ratio) guard,
duplicate-key JSON rejection, required-field/string validation, per-file SHA-256
verification, and the same-plugin-id upgrade/downgrade rule.

Extraction happens into a temp dir first; only after ALL checks pass is the temp
dir moved atomically (``os.replace``) into the final location. Any failure leaves
no half-installed state: the temp dir is always cleaned.

Design section 5.6 additionally forbids overwriting a plugin that is currently
running. Callers supply that run state through the ``active_run_resolver``
callable so this module stays free of registry/runtime imports.

Stdlib only. Imports the sibling ``plugin_manifest`` module for validation types.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass

from .plugin_manifest import (
    PluginError,
    PluginManifest,
    PluginManifestError,
    compare_versions,
    is_safe_relative_path,
)
from .zip_member_count import (
    CentralDirectoryUnreadable,
    PackageTooLarge,
    Zip64RecordUnreadable,
    bounded_precheck,
)

logger = logging.getLogger(__name__)


class PluginPackageError(PluginError):
    """Raised when a plugin package fails a security or integrity check."""


# Windows reserved device names (case-insensitive, with or without extension).
_RESERVED_DEVICES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *[f"COM{i}" for i in range(1, 10)],
    *[f"LPT{i}" for i in range(1, 10)],
}

MANIFEST_NAME = "manifest.json"


@dataclass
class PackageLimits:
    """Configurable, safe-by-default resource ceilings for a plugin package."""

    max_single_file_bytes: int = 16 * 1024 * 1024          # 16 MiB
    max_file_count: int = 2000
    max_total_unpacked_bytes: int = 256 * 1024 * 1024       # 256 MiB
    max_compression_ratio: float = 100.0                    # uncompressed/compressed
    max_manifest_bytes: int = 256 * 1024                    # 256 KiB


def _is_reserved_device_name(name: str) -> bool:
    stem = name.split(".")[0].upper() if name else ""
    return stem in _RESERVED_DEVICES


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    checker = getattr(info, "is_symlink", None)
    if callable(checker) and checker():
        return True
    # Unix mode bits live in the high 16 bits of external_attr.
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def _check_member_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise PluginPackageError("非法的 ZIP 成员名")
    if name.startswith(("/", "\\")) or os.path.isabs(name):
        raise PluginPackageError(f"绝对路径成员：{name}")
    # FR-20260812-D4-002: 拒绝含冒号成员名——Windows 上 "C:evil" 经
    # os.path.join 会丢弃临时目录前缀写出越界，":evil" 还可能是 NTFS ADS。
    # （realpath 守卫能事后删除越界文件，但应先拒绝，避免先写后删。）
    if ":" in name:
        raise PluginPackageError(f"含冒号的成员名被拒绝：{name!r}")
    # FR-SCOPE007-003: Windows 会剥掉文件名尾随的点与空格，故 "manifest.json."
    # 与 "manifest.json " 在磁盘上都落成规范的 manifest.json，覆盖真清单并绕过
    # 步骤 4 的身份校验（步骤 2 的清单定位是精确匹配，不会把它们当作清单豁免）。
    # 规范化口径与 profile_package._safe_member_name 的 part.rstrip(" .") 一致。
    if any(part.rstrip(" .") != part for part in name.split("/")):
        raise PluginPackageError(f"含尾随点或空格的成员名被拒绝：{name!r}")
    normalized = os.path.normpath(name)
    if normalized == ".." or normalized.startswith(".." + os.sep):
        raise PluginPackageError(f"路径穿越成员：{name}")
    # 拒绝 normpath 后等价于 MANIFEST_NAME 但原始名不同的成员（如 "./manifest.json"），
    # 否则解压时后者会覆盖前者，使步骤 4 读到被替换的清单内容，绕过身份校验。
    # 比较须大小写归一（casefold）：Windows 文件系统大小写不敏感，"Manifest.json"
    # 等变体会覆盖规范的 manifest.json，故同样视为等价路径拒绝。仅当成员名与
    # MANIFEST_NAME 完全相等时才放行（即规范的 manifest.json 本身）。
    if normalized.casefold() == MANIFEST_NAME.casefold() and name != MANIFEST_NAME:
        raise PluginPackageError(f"等价路径成员被拒绝：{name!r}")
    for part in name.split("/"):
        if _is_reserved_device_name(part):
            raise PluginPackageError(f"保留设备名成员：{name}")


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(path: str, max_bytes: int) -> str:
    if os.path.getsize(path) > max_bytes:
        raise PluginPackageError(f"文件过大（上限 {max_bytes} 字节）：{path}")
    with open(path, "rb") as handle:
        raw = handle.read(max_bytes + 1)
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PluginPackageError(f"文件不是有效的 UTF-8：{path}") from error


def _extract_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, temp_dir: str) -> None:
    target = os.path.join(temp_dir, info.filename)
    if info.is_dir():
        os.makedirs(target, exist_ok=True)
        return
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    data = zf.read(info)
    with open(target, "wb") as handle:
        handle.write(data)
    # Post-extraction realpath containment: guard against OS-level path
    # resolution (e.g. junctions) escaping the temp directory.
    real_target = os.path.realpath(target)
    real_base = os.path.realpath(temp_dir)
    if not real_target.startswith(real_base + os.sep):
        os.remove(target)
        raise PluginPackageError(f"路径穿越检测：{info.filename}")


def _validate_and_extract(
    path: str, temp_dir: str, limits: PackageLimits
) -> PluginManifest:
    # FR-20261005-602: enforce the member-count limit BEFORE ``zipfile.ZipFile``
    # materializes the central directory. The stdlib constructor turns every
    # entry into a ``ZipInfo`` first, so a package declaring a huge number of
    # members allocates them all before the check below can run (design
    # DOC-SCOPE005-001 / ARCHITECTURE.md #14). Uses the same bounded pre-check as
    # the profile importer so the two cannot drift apart.
    #
    # A2 (维护者裁定 2026-10-07): the maintainer rejected both "trust the declared
    # count" and "adding a size_cd check is sufficient proof", so this proves the
    # bound from the declared count, the directory size, a bounded record walk and
    # a count cross-check.
    try:
        with open(path, "rb") as handle:
            bounded_precheck(handle, limits.max_file_count)
    except PackageTooLarge as error:
        detail = f"（声明 {error.declared}）" if error.declared else ""
        raise PluginPackageError(
            f"文件数量超过上限 {limits.max_file_count}{detail}"
        ) from error
    except (Zip64RecordUnreadable, CentralDirectoryUnreadable) as error:
        raise PluginPackageError(str(error)) from error
    except OSError:
        pass

    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()

        # 1) structural / security scan of member names + symlinks
        for info in infos:
            _check_member_name(info.filename)
            if _is_symlink(info):
                raise PluginPackageError(f"插件包包含符号链接：{info.filename}")

        if len(infos) > limits.max_file_count:
            raise PluginPackageError(
                f"文件数量超过上限 {limits.max_file_count}"
            )

        # 2) locate + size the manifest before extracting anything
        manifest_info = next(
            (i for i in infos if i.filename == MANIFEST_NAME), None
        )
        if manifest_info is None:
            raise PluginPackageError("插件包缺少 manifest.json")
        if manifest_info.file_size > limits.max_manifest_bytes:
            raise PluginPackageError("manifest.json 超过大小上限")

        total_unpacked = 0
        for info in infos:
            if info.is_dir():
                continue
            if info.file_size > limits.max_single_file_bytes:
                raise PluginPackageError(f"单文件过大：{info.filename}")
            total_unpacked += info.file_size
            if total_unpacked > limits.max_total_unpacked_bytes:
                raise PluginPackageError("解压总大小超过上限")
            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > limits.max_compression_ratio:
                    raise PluginPackageError(
                        f"压缩比异常（疑似 zip 炸弹）：{info.filename}"
                    )
            elif info.compress_size == 0 and info.file_size > 0:
                raise PluginPackageError(f"压缩数据异常：{info.filename}")

        # 3) extract (symlinks already rejected)
        for info in infos:
            _extract_member(zf, info, temp_dir)

    # 4) parse + strict-validate the manifest (rejects duplicate keys, etc.)
    manifest = PluginManifest.parse_json(
        _read_text(os.path.join(temp_dir, MANIFEST_NAME), limits.max_manifest_bytes)
    )

    # 5) verify every declared file against its SHA-256
    _verify_declared_files(manifest, temp_dir)
    # 6) reject any file on disk that the manifest does not declare
    _reject_undeclared_files(manifest, temp_dir)
    return manifest


def _verify_declared_files(manifest: PluginManifest, temp_dir: str) -> None:
    base = os.path.abspath(temp_dir)
    seen: set[str] = set()
    for entry in manifest.files:
        rel = entry.path
        if rel in seen:
            raise PluginPackageError(f"重复声明文件：{rel}")
        seen.add(rel)
        if not is_safe_relative_path(rel):
            raise PluginPackageError(f"非法声明文件路径：{rel}")
        abspath = os.path.abspath(os.path.join(temp_dir, rel))
        if abspath != base and not abspath.startswith(base + os.sep):
            raise PluginPackageError(f"声明文件越界：{rel}")
        if not os.path.isfile(abspath):
            raise PluginPackageError(f"声明文件缺失：{rel}")
        if _sha256_file(abspath) != entry.sha256:
            raise PluginPackageError(f"文件哈希不匹配：{rel}")


def _reject_undeclared_files(manifest: PluginManifest, temp_dir: str) -> None:
    """Reject any extracted file that is not declared in ``manifest.files``.

    Closes the manifest-outside-code gap: an attacker could ship executable
    files (e.g. an extra ``.py``/``.dll``) that are never referenced by the
    manifest and therefore escape :func:`_verify_declared_files`. Every shipped
    file other than the manifest itself must be declared so its presence and
    hash are accounted for.
    """
    declared = {entry.path for entry in manifest.files}
    base = os.path.abspath(temp_dir)
    for root, _dirs, files in os.walk(base):
        for name in files:
            abspath = os.path.abspath(os.path.join(root, name))
            rel = os.path.relpath(abspath, base).replace(os.sep, "/")
            if rel == MANIFEST_NAME:
                continue
            if rel not in declared:
                raise PluginPackageError(f"清单外文件（拒绝安装）：{rel}")


def _check_upgrade_rule(
    manifest: PluginManifest, install_base_dir: str, allow_downgrade: bool
) -> None:
    existing = os.path.join(install_base_dir, manifest.id, MANIFEST_NAME)
    if not os.path.isfile(existing):
        return
    try:
        old = PluginManifest.parse_json(_read_text(existing, 256 * 1024))
    except (PluginError, OSError) as exc:
        # ADV-SCOPE007-001: 已安装清单不可读时新旧版本关系不可判定，降级规则无从
        # 执行。原行为直接上抛 PluginManifestError，用户看到的是「插件清单 JSON
        # 格式无效（第 N 行）」这类技术性解析错误，既装不上、也不知道可以先卸载，
        # 陷入死角。
        # DESIGN: 清单损坏的安装本身已不可用——注册表把它标为 CORRUPT、插件宿主
        # 拒绝运行（见 PluginStatus.CORRUPT 的处理）——用户唯一能走的修复路径就是
        # 重新安装同一插件。因此这里放行覆盖，而不是把不可判定的版本关系变成拒绝
        # 安装的理由。
        # SAFETY: 放行不是静默——记录一条可见警告，说明本次覆盖建立在无法判定版本
        # 的基础上。活跃运行守卫（_check_active_run_rule）不受影响，仍在覆盖前执行，
        # 运行中的插件不会被替换。
        logger.warning(
            "已安装插件 %s 的清单无法解析，跳过降级检查并允许覆盖安装：%s",
            manifest.id,
            exc,
        )
        return
    cmp = compare_versions(manifest.version, old.version)
    if cmp < 0 and not allow_downgrade:
        raise PluginPackageError(
            f"禁止降级：已安装 {old.version}，目标 {manifest.version}"
        )


def _check_active_run_rule(
    manifest: PluginManifest,
    install_base_dir: str,
    active_run_resolver: Callable[[str], int] | None,
) -> None:
    """Refuse to overwrite an install that is currently executing.

    SAFETY: 设计 §5.6「运行中的插件不能被覆盖，更新在运行结束后生效」。
    ``_commit_install`` replaces the whole plugin directory with ``os.replace``;
    doing that while a run has the plugin loaded would swap the files out from
    under an in-flight execution.

    DESIGN: only *active-run* references block the overwrite. A plugin merely
    referenced by a saved profile stays upgradable — unlike uninstall, which
    also refuses on profile references because removal would silently delete
    steps. Overwriting keeps the plugin id and its actions, so profile
    references survive the upgrade.

    Args:
        manifest: Validated manifest of the package being installed.
        install_base_dir: Root directory that plugin subdirectories live in.
        active_run_resolver: Optional callable returning the number of
            in-progress runs holding ``manifest.id``. When None the caller has
            no run state to consult and the rule is not enforced.

    Raises:
        PluginPackageError: If an existing install of the same id is currently
            referenced by at least one active run.
    """

    if active_run_resolver is None:
        return
    # 仅覆盖安装才受本规则约束：目标目录不存在时没有「运行中的旧文件」可被替换。
    if not os.path.isdir(os.path.join(install_base_dir, manifest.id)):
        return
    count = int(active_run_resolver(manifest.id) or 0)
    if count > 0:
        raise PluginPackageError(
            f"插件正在运行，无法覆盖更新：{manifest.id}（{count} 个进行中的运行）。"
            "请等待运行结束后重试。"
        )


def _commit_install(manifest: PluginManifest, temp_dir: str, install_base_dir: str) -> None:
    final = os.path.join(install_base_dir, manifest.id)
    if os.path.exists(final):
        backup = final + ".bkp"
        if os.path.exists(backup):
            shutil.rmtree(backup, ignore_errors=True)
        os.replace(final, backup)
        try:
            os.replace(temp_dir, final)
        except Exception:
            os.replace(backup, final)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    else:
        os.replace(temp_dir, final)


def install_from_file(
    path: str,
    install_base_dir: str,
    *,
    limits: PackageLimits | None = None,
    allow_downgrade: bool = False,
    active_run_resolver: Callable[[str], int] | None = None,
) -> PluginManifest:
    """Validate and install a ``.fkrplugin``/ZIP package.

    The package is extracted into a temporary directory first, where every
    security and integrity check runs (member-name safety, size and zip-bomb
    caps, manifest schema, per-file SHA-256, and rejection of undeclared
    files). Only after all checks pass and the upgrade/downgrade and
    active-run rules are satisfied is the temp directory moved atomically into
    place; an existing install is backed up and restored if the final move
    fails. On ANY failure the temporary extraction directory is removed and no
    partial install remains.

    An existing install whose own manifest cannot be read (corrupt JSON,
    oversized, undecodable, or unreadable) does not block the overwrite: the
    version relationship is then undefined, so only the downgrade rule is
    skipped and a warning is logged. See :func:`_check_upgrade_rule`.

    Args:
        path: Filesystem path to the plugin package.
        install_base_dir: Root directory that plugin subdirectories install
            into.
        limits: Optional resource ceilings; defaults to :class:`PackageLimits`.
        allow_downgrade: When False (default), installing a version older than
            the currently installed one is rejected.
        active_run_resolver: Optional callable returning how many in-progress
            runs hold the plugin id. When supplied, overwriting an existing
            install is refused while that count is non-zero (design §5.6). The
            registry-agnostic callable keeps this module stdlib-only; see
            :func:`_check_active_run_rule`.

    Returns:
        The validated :class:`PluginManifest` of the installed plugin.

    Raises:
        PluginPackageError: If the package is missing or fails any security,
            integrity, size, upgrade-rule, or active-run check.
        PluginManifestError: If the embedded manifest is structurally invalid.
    """

    limits = limits or PackageLimits()
    if not os.path.isfile(path):
        raise PluginPackageError(f"插件包不存在：{path}")

    install_base_dir = os.path.abspath(install_base_dir)
    os.makedirs(install_base_dir, exist_ok=True)

    temp_dir = tempfile.mkdtemp(prefix="fkrplugin_", dir=install_base_dir)
    try:
        manifest = _validate_and_extract(path, temp_dir, limits)
        _check_upgrade_rule(manifest, install_base_dir, allow_downgrade)
        _check_active_run_rule(manifest, install_base_dir, active_run_resolver)
        _commit_install(manifest, temp_dir, install_base_dir)
        return manifest
    except PluginManifestError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def uninstall(plugin_id: str, install_base_dir: str) -> None:
    """Remove an installed plugin directory.

    Args:
        plugin_id: Identifier of the plugin to remove; must not contain path
            separators or ``..``.
        install_base_dir: Root directory that plugin subdirectories live in.

    Raises:
        PluginPackageError: If the id is malformed or the plugin is not
            currently installed.
    """

    if not plugin_id or "/" in plugin_id or "\\" in plugin_id or ".." in plugin_id:
        raise PluginPackageError(f"非法插件 id：{plugin_id}")
    target = os.path.join(os.path.abspath(install_base_dir), plugin_id)
    if not os.path.isdir(target):
        raise PluginPackageError(f"插件未安装：{plugin_id}")
    shutil.rmtree(target, ignore_errors=False)


def load_installed_manifest(plugin_id: str, install_base_dir: str) -> PluginManifest:
    """Load and validate an installed plugin's manifest.

    Args:
        plugin_id: Identifier of the installed plugin; must not be empty or
            contain path separators or ``..``.
        install_base_dir: Root directory that plugin subdirectories live in.

    Returns:
        The parsed and validated :class:`PluginManifest`.

    Raises:
        PluginManifestError: If the id is empty, the plugin is not installed,
            or the manifest fails schema validation.
        PluginPackageError: If the id contains illegal path components.
    """

    if not plugin_id:
        raise PluginManifestError("插件 id 为空")
    if "/" in plugin_id or "\\" in plugin_id or ".." in plugin_id:
        raise PluginPackageError(f"非法 plugin_id：{plugin_id!r}")
    target = os.path.join(os.path.abspath(install_base_dir), plugin_id, MANIFEST_NAME)
    if not os.path.isfile(target):
        raise PluginManifestError(f"插件未安装或清单缺失：{plugin_id}")
    return PluginManifest.parse_json(_read_text(target, 256 * 1024))
