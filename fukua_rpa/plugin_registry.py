"""In-memory plugin registry for the plugin center (design section 5.6).

Tracks the status of every installed plugin: ENABLED, DISABLED, INCOMPATIBLE
(api/version), CORRUPT (unreadable/invalid manifest), or DEPENDENCY_MISSING.

The registry is a PURE logic layer: it accepts ``install_base_dir`` and
``app_version`` as parameters, never scans profiles itself, and learns about
references (profiles/steps that use a plugin) only through an injected resolver
or a set of referenced plugin ids. Uninstall refuses when a plugin is still
referenced, per the design rule that referenced steps must never be silently
deleted.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

from .io_utils import atomic_write_json, hash_file
from .json_safety import loads_bounded_json
from .plugin_manifest import (
    PluginAction,
    PluginError,
    PluginManifest,
    api_compatible,
)
from .plugin_package import load_installed_manifest
from .plugin_package import uninstall as _uninstall_package

# DESIGN: 插件禁用采用本机持久语义（维护者裁定）。状态落在安装根目录的点开头文件，
# 不写入任何插件子目录，因此不会随插件包或方案导出。点开头同时让 scan() 自然跳过。
_DISABLED_STATE_NAME = ".disabled_plugins.json"
# SAFETY: 状态文件体积极小（仅插件 id 列表），设上界防止被篡改为超大 JSON。
_MAX_DISABLED_STATE_BYTES = 256 * 1024


class PluginRegistryError(PluginError):
    """Raised on illegal registry operations (unknown id, etc.)."""


# FR-014 / DEC-20260920-002: infrastructure directories that live inside the
# install base and are not plugins. In a source checkout ``install_base_dir`` is
# the repository's ``plugins/``, which also holds the shared build toolchain.
#
# The match is an **exact directory name** and requires that the directory carry
# no ``manifest.json``. A leftover plugin root without a manifest is still
# registered as CORRUPT so the user can see and remove it; a manifest always wins
# over the name, because a manifest is the plugin's explicit declaration.
INFRASTRUCTURE_DIR_NAMES = frozenset({"tools"})


def _string_id_set(value: Any) -> set[str]:
    """Return *value* as a set of string ids, tolerating a malformed state file.

    The state file is machine-local and hand-editable, so ``disabled``/``reviewed``
    may hold anything. Two shapes are actively dangerous: a bare string iterates
    into characters (``"abc"`` -> three one-character ids), and a number or ``None``
    raises ``TypeError`` during startup. Anything not list-like is treated as empty:
    fail closed, never crash.
    """
    # ``cast`` on the container, not an annotation: iterating an ``Any`` yields
    # ``Unknown`` elements, and an annotated alias would not suppress that (the
    # checker keeps the inferred type). Casting the container to a parameterised
    # ``Iterable[Any]`` states "an iterable of arbitrary JSON values" and keeps the
    # per-item ``str`` filter below verifiable.
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    elements = cast(Iterable[Any], value)
    return {item for item in elements if isinstance(item, str)}


class PluginStatus(Enum):
    """Possible lifecycle states of an installed plugin.

    A plugin's status is recomputed on every query from its corruption flag,
    API compatibility, dependency graph, and the user-controlled disabled set.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"
    INCOMPATIBLE = "incompatible"
    CORRUPT = "corrupt"
    DEPENDENCY_MISSING = "dependency_missing"


@dataclass
class _Record:
    plugin_id: str
    manifest: PluginManifest | None = None
    corrupt: bool = False
    corrupt_reason: str | None = None
    dependency_missing: bool = False


@dataclass
class PluginStatusRecord:
    """Display-ready snapshot of a plugin's status and metadata.

    Corrupt plugins carry only ``plugin_id``, ``status``, and
    ``corrupt_reason``; healthy plugins additionally populate the manifest-
    derived fields for rendering in the plugin center UI.
    """

    plugin_id: str
    status: PluginStatus
    name: str = ""
    version: str = ""
    api_version: int | None = None
    plugin_type: str = ""
    author: str = ""
    action_count: int = 0
    template_count: int = 0
    permission_count: int = 0
    permissions: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    corrupt_reason: str | None = None


def _safe_plugin_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "/" in value
        or "\\" in value
        or ".." in value
    ):
        raise PluginRegistryError(f"非法插件 id：{value}")
    return value


class PluginRegistry:
    """In-memory registry tracking installed plugins and their status.

    Acts as a pure logic layer: it scans ``install_base_dir`` for plugin
    directories, classifies each one (ENABLED, DISABLED, INCOMPATIBLE, CORRUPT,
    or DEPENDENCY_MISSING), and gates enable/disable/uninstall operations on
    those states. Reference information is supplied externally via a resolver
    callable or a set of referenced ids; the registry never scans profiles
    itself.
    """

    def __init__(self, install_base_dir: str, app_version: str) -> None:
        """Initialize the registry and perform an initial scan.

        Args:
            install_base_dir: Root directory containing plugin subdirectories.
            app_version: Current application version used for compatibility
                checks against each plugin's declared API version.
        """
        self.install_base_dir = os.path.abspath(install_base_dir)
        self.app_version = app_version
        self._records: dict[str, _Record] = {}
        self._disabled: set[str] = set()
        # DESIGN: 用户已处理过的插件集合。既不在 _disabled 也不在 _reviewed 的插件
        # 视为新安装，默认保持禁用，直到用户检查后手动启用（维护者裁定）。
        self._reviewed: set[str] = set()
        # INVARIANT: active-run reference counts prevent uninstall while a run
        # is in progress. acquire_for_run/release_after_run are called on the
        # main thread (GIL-protected); no additional locking required.
        self._active_run_refs: dict[str, int] = {}
        # FR-SCOPE007-001: 扫描失败（目录不可访问等）不得让状态初始化把存量插件
        # 永久变为禁用。失败时 _records 为空，若照常走"无状态文件"分支就会把空集
        # 写入 reviewed 并落盘——下次启动读到空 reviewed，全部存量插件会被当成
        # 新安装而默认禁用，且不会自愈。因此失败时完全跳过状态初始化。
        self._state_loaded = False
        # 目录不可访问时仍以空注册表启动（不让一次扫描失败阻断应用启动）；
        # 状态引导由 scan() 内部在成功时完成，失败则留待下次成功扫描。
        with contextlib.suppress(OSError):
            self.scan()

    # -- persisted disabled state -----------------------------------------

    @property
    def _state_path(self) -> str:
        """Path of the machine-local plugin state file."""
        return os.path.join(self.install_base_dir, _DISABLED_STATE_NAME)

    def _load_disabled_state(self) -> None:
        """Restore the disabled/reviewed sets from the state file.

        DESIGN: 无状态文件（首次运行）时，把当前已安装的插件视为"已处理过"，
        避免升级后把存量插件一并变为禁用而打断既有自动化；此后新出现的插件才
        按"新安装默认禁用"处理。损坏或超限的状态文件按空状态处理（失败关闭，
        绝不让坏数据把插件误判为可用）。
        """
        path = self._state_path
        if not os.path.isfile(path):
            self._reviewed = set(self._records)
            self._save_disabled_state()
            return
        try:
            with open(path, "rb") as handle:
                raw = handle.read(_MAX_DISABLED_STATE_BYTES + 1)
        except OSError:  # 不可读时降级为内存态，不阻断启动
            return
        try:
            data = loads_bounded_json(
                raw,
                max_bytes=_MAX_DISABLED_STATE_BYTES,
                label="插件禁用状态",
            )
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        # ``cast`` rather than an annotated alias: assigning to an ``Any``-annotated
        # name still lets the checker keep the inferred (unparameterised ``dict``)
        # type, which is itself reported as partially unknown. ``cast`` states the
        # intent explicitly — this boundary deliberately accepts any JSON shape.
        payload = cast(Any, data)
        self._disabled = _string_id_set(payload.get("disabled"))
        self._reviewed = _string_id_set(payload.get("reviewed"))

    def _save_disabled_state(self) -> None:
        """Atomically persist the disabled/reviewed sets.

        SAFETY: 状态只含插件 id，不含任何凭据或方案内容，故无需 restrict_acl；
        原子写入保证读者不会看到半写文件。
        """
        data = {
            "disabled": sorted(self._disabled),
            "reviewed": sorted(self._reviewed),
        }
        with contextlib.suppress(OSError):  # 只读安装目录时降级为内存态
            atomic_write_json(self._state_path, data)

    # -- scanning ---------------------------------------------------------

    def scan(self) -> None:
        """Reload every installed plugin's status from disk.

        Rebuilds the internal record table from scratch by walking the install
        base directory. Temporary/backup directories left behind by a crashed
        install (``fkrplugin_*``, ``*.bkp``, etc.) are skipped so they are never
        registered as ghost plugins. Each surviving directory has its manifest
        loaded, its declared files cross-checked, and its inter-plugin
        dependencies resolved; any failure marks that plugin CORRUPT rather
        than aborting the whole scan. The new table is built locally and
        assigned in a single step so worker threads never observe a partially
        constructed registry.
        """

        # INVARIANT: 构建到局部字典后单次赋值，避免 Worker 线程读到空/半建字典
        new_records: dict = {}
        if not os.path.isdir(self.install_base_dir):
            self._records = new_records
            if not self._state_loaded:
                self._state_loaded = True
                self._load_disabled_state()
            return

        installed: set[str] = set()
        for entry in os.scandir(self.install_base_dir):
            if not entry.is_dir():
                continue
            # 跳过已知临时/备份目录（install_from_file 崩溃后残留的
            # fkrplugin_ 临时目录与 .bkp 备份不应被注册为幽灵插件）
            name = entry.name
            if name.startswith((".", "fkrplugin_", "tmp_", "__")):
                continue
            if name.endswith((".bkp", ".tmp", ".bak")):
                continue
            plugin_id = entry.name
            # FR-014: 命中精确名单**且**没有清单的基础设施目录才跳过。子串不算——
            # "tools_extra" 仍须登记为 CORRUPT（见 test_scan_skip_uses_exact_name）。
            if plugin_id in INFRASTRUCTURE_DIR_NAMES and not os.path.isfile(
                os.path.join(entry.path, "manifest.json")
            ):
                continue
            try:
                manifest = load_installed_manifest(plugin_id, self.install_base_dir)
                if manifest.id != plugin_id:
                    new_records[plugin_id] = _Record(
                        plugin_id=plugin_id,
                        corrupt=True,
                        corrupt_reason=(
                            f"目录名与清单 ID 不一致：目录 {plugin_id}，"
                            f"清单 {manifest.id}"
                        ),
                    )
                else:
                    new_records[plugin_id] = _Record(
                        plugin_id=plugin_id, manifest=manifest
                    )
                    self._verify_installed_files(plugin_id, manifest, new_records)
            except PluginError as error:
                new_records[plugin_id] = _Record(
                    plugin_id=plugin_id,
                    corrupt=True,
                    corrupt_reason=str(error),
                )
            installed.add(plugin_id)

        corrupt_set = {
            pid for pid, rec in new_records.items() if rec.corrupt
        }

        for record in new_records.values():
            if record.corrupt or record.manifest is None:
                continue
            missing = [
                dep
                for dep in record.manifest.dependencies
                if dep not in installed or dep in corrupt_set
            ]
            record.dependency_missing = bool(missing)

        self._records = new_records  # GIL 下单次赋值原子
        # FR-SCOPE007-001: 状态引导只在**扫描成功**后进行。失败扫描会留下空的
        # _records，若此时仍走"无状态文件"分支就会把空 reviewed 写盘，使全部
        # 存量插件在下次启动被当成新安装而永久默认禁用。放在这里而不是 __init__，
        # 是为了让"首次扫描失败、后续扫描成功"的实例仍能完成引导。
        if not self._state_loaded:
            self._state_loaded = True
            self._load_disabled_state()

    def _verify_installed_files(
        self, plugin_id: str, manifest: PluginManifest, records: dict | None = None
    ) -> None:
        """Cross-check the installed directory against the manifest file list.

        The install path already rejects undeclared files, so this is defense in
        depth: if a declared file is missing (tampering or partial install) the
        plugin is marked corrupt rather than loaded with a gap.

        S8: in addition to existence, each declared file's SHA-256 is recomputed
        and compared against the manifest digest, so post-install tampering
        (e.g. an overwritten ``.py``) is detected instead of silently executed.
        """
        target = records if records is not None else self._records
        base = os.path.join(self.install_base_dir, plugin_id)
        for entry in manifest.files:
            full = os.path.join(base, entry.path)
            if not os.path.isfile(full):
                rec = target.get(plugin_id)
                if rec is not None:
                    rec.corrupt = True
                    rec.corrupt_reason = f"缺少清单声明文件：{entry.path}"
                return
            try:
                actual = hash_file(full)
            except OSError:
                rec = target.get(plugin_id)
                if rec is not None:
                    rec.corrupt = True
                    rec.corrupt_reason = f"清单声明文件无法读取：{entry.path}"
                return
            if actual != entry.sha256:
                rec = target.get(plugin_id)
                if rec is not None:
                    rec.corrupt = True
                    rec.corrupt_reason = f"清单声明文件哈希不匹配：{entry.path}"
                return

    # -- status -----------------------------------------------------------

    def _status(self, record: _Record) -> PluginStatus:
        if record.corrupt or record.manifest is None:
            return PluginStatus.CORRUPT
        if not api_compatible(record.manifest, self.app_version):
            return PluginStatus.INCOMPATIBLE
        if record.dependency_missing:
            return PluginStatus.DEPENDENCY_MISSING
        if record.plugin_id in self._disabled:
            return PluginStatus.DISABLED
        # DESIGN: 新安装插件默认禁用，用户检查后手动启用（维护者裁定）。既未被显式
        # 禁用、也未进入 _reviewed 的插件即为新安装，须保持禁用直到用户启用一次。
        if record.plugin_id not in self._reviewed:
            return PluginStatus.DISABLED
        return PluginStatus.ENABLED

    def status(self, plugin_id: str) -> PluginStatus:
        """Return the current status of an installed plugin.

        Args:
            plugin_id: Identifier of the plugin to query.

        Returns:
            The plugin's resolved :class:`PluginStatus`.

        Raises:
            PluginRegistryError: If the id is malformed or the plugin is not
                present in the registry.
        """
        _safe_plugin_id(plugin_id)
        record = self._records.get(plugin_id)
        if record is None:
            raise PluginRegistryError(f"未知插件：{plugin_id}")
        return self._status(record)

    # -- enable / disable ------------------------------------------------

    def enable(self, plugin_id: str) -> None:
        """Enable a previously disabled plugin.

        Args:
            plugin_id: Identifier of the plugin to enable.

        Raises:
            PluginRegistryError: If the plugin is unknown, corrupt,
                incompatible with the current app version, or has missing
                dependencies.
        """
        _safe_plugin_id(plugin_id)
        record = self._records.get(plugin_id)
        if record is None:
            raise PluginRegistryError(f"未知插件：{plugin_id}")
        if record.corrupt or record.manifest is None:
            raise PluginRegistryError(f"插件已损坏，无法启用：{plugin_id}")
        if not api_compatible(record.manifest, self.app_version):
            raise PluginRegistryError(f"插件不兼容，无法启用：{plugin_id}")
        if record.dependency_missing:
            raise PluginRegistryError(f"插件依赖缺失，无法启用：{plugin_id}")
        self._disabled.discard(plugin_id)
        # 启用即代表用户已完成检查，之后该插件不再回落到"新安装默认禁用"。
        self._reviewed.add(plugin_id)
        self._save_disabled_state()

    def disable(self, plugin_id: str) -> None:
        """Mark a plugin as disabled.

        Args:
            plugin_id: Identifier of the plugin to disable.

        Raises:
            PluginRegistryError: If the plugin is not present in the registry.
        """
        _safe_plugin_id(plugin_id)
        if plugin_id not in self._records:
            raise PluginRegistryError(f"未知插件：{plugin_id}")
        self._disabled.add(plugin_id)
        self._reviewed.add(plugin_id)
        self._save_disabled_state()

    def is_enabled(self, plugin_id: str) -> bool:
        """Return whether the plugin currently resolves to ENABLED.

        Args:
            plugin_id: Identifier of the plugin to check.

        Returns:
            True only if :meth:`status` reports ``PluginStatus.ENABLED``.
        """
        return self.status(plugin_id) == PluginStatus.ENABLED

    # -- references / uninstall ------------------------------------------

    def acquire_for_run(self, plugin_ids: list[str] | tuple[str, ...]) -> None:
        """Increment active-run reference counts to block uninstall.

        Called at the start of a workflow run for every plugin it uses. While a
        plugin's active-run count is non-zero, :meth:`uninstall` refuses to
        remove it so a running workflow never loses its plugin mid-execution.

        Args:
            plugin_ids: Plugins referenced by the starting run.
        """
        for pid in plugin_ids:
            self._active_run_refs[pid] = self._active_run_refs.get(pid, 0) + 1

    def release_after_run(self, plugin_ids: list[str] | tuple[str, ...]) -> None:
        """Decrement active-run reference counts after a run finishes.

        Mirrors :meth:`acquire_for_run`; counts that drop to zero are removed
        from the table entirely.

        Args:
            plugin_ids: Plugins referenced by the finished run.
        """
        for pid in plugin_ids:
            count = self._active_run_refs.get(pid, 0)
            if count <= 1:
                self._active_run_refs.pop(pid, None)
            else:
                self._active_run_refs[pid] = count - 1

    def active_run_count(self, plugin_id: str) -> int:
        """Return how many in-progress runs currently hold this plugin.

        DESIGN: unlike :meth:`reference_count`, profile/step references are
        deliberately NOT counted — only references acquired through
        :meth:`acquire_for_run`. Design §5.6 forbids *overwriting* a plugin
        that is running, but a plugin merely referenced by a saved profile
        stays upgradable (the id and its actions survive an overwrite, so no
        step is silently invalidated).

        Args:
            plugin_id: Identifier of the plugin to count active runs for.

        Returns:
            Number of active runs holding ``plugin_id``; 0 when none.

        Raises:
            PluginRegistryError: If the id is malformed.
        """

        _safe_plugin_id(plugin_id)
        return self._active_run_refs.get(plugin_id, 0)

    def reference_count(
        self,
        plugin_id: str,
        resolver: Callable[[str], int] | None = None,
        referenced_ids: Iterable[str] | None = None,
    ) -> int:
        """Count how many profiles/steps reference this plugin.

        The registry never scans profiles itself; callers supply reference
        information through exactly one of ``resolver`` or ``referenced_ids``.
        Active-run references acquired via :meth:`acquire_for_run` are always
        added on top, so an in-progress run keeps the count non-zero.

        Args:
            plugin_id: Identifier of the plugin to count references for.
            resolver: Optional callable taking a plugin id and returning the
                number of references.
            referenced_ids: Optional iterable of referenced plugin ids; each
                occurrence of ``plugin_id`` counts as one reference.

        Returns:
            Total reference count, including any active-run references.
        """

        _safe_plugin_id(plugin_id)
        count = 0
        if resolver is not None:
            count += int(resolver(plugin_id) or 0)
        elif referenced_ids is not None:
            count += sum(1 for ref in referenced_ids if ref == plugin_id)
        # 活跃运行引用：运行期间阻止卸载。
        # INVARIANT: 与 active_run_count 共用同一份 _active_run_refs 读取路径，
        # 使「卸载守卫」与「覆盖安装守卫」的活跃运行口径不会各自漂移。
        count += self.active_run_count(plugin_id)
        return count

    def uninstall(
        self,
        plugin_id: str,
        resolver: Callable[[str], int] | None = None,
        referenced_ids: Iterable[str] | None = None,
    ) -> tuple[bool, int]:
        """Uninstall a plugin unless it is still referenced.

        Per the design rule that referenced steps must never be silently
        deleted, removal is refused while any profile, step, or active run
        still references the plugin.

        Args:
            plugin_id: Identifier of the plugin to remove.
            resolver: Optional reference resolver; see :meth:`reference_count`.
            referenced_ids: Optional referenced-id iterable; see
                :meth:`reference_count`.

        Returns:
            A ``(ok, referenced_count)`` tuple. ``ok`` is False and the plugin
            is left in place when ``referenced_count > 0``.

        Raises:
            PluginRegistryError: If the plugin is not present in the registry.
        """

        _safe_plugin_id(plugin_id)
        if plugin_id not in self._records:
            raise PluginRegistryError(f"未知插件：{plugin_id}")
        count = self.reference_count(
            plugin_id, resolver=resolver, referenced_ids=referenced_ids
        )
        if count > 0:
            return (False, count)
        _uninstall_package(plugin_id, self.install_base_dir)
        self._records.pop(plugin_id, None)
        # DESIGN: 卸载即彻底清理本机持久状态，避免重装后被旧记录意外禁用。
        self._disabled.discard(plugin_id)
        self._reviewed.discard(plugin_id)
        self._save_disabled_state()
        return (True, 0)

    # -- lookup -----------------------------------------------------------

    def get_manifest(self, plugin_id: str) -> PluginManifest | None:
        """Return the validated manifest for a plugin.

        Args:
            plugin_id: Identifier of the plugin to look up.

        Returns:
            The plugin's :class:`PluginManifest`, or None if the plugin is
            unknown or corrupt.
        """
        record = self._records.get(plugin_id)
        if record is None or record.corrupt:
            return None
        return record.manifest

    def get_action(
        self, plugin_id: str, action_id: str
    ) -> PluginAction | None:
        """Return a single action descriptor from a compatible plugin.

        Args:
            plugin_id: Identifier of the plugin owning the action.
            action_id: Identifier of the action within that plugin.

        Returns:
            The matching :class:`PluginAction`, or None if the plugin is
            unknown, corrupt, incompatible with the current app version, or
            has no action with the given id.
        """
        record = self._records.get(plugin_id)
        if record is None or record.corrupt or record.manifest is None:
            return None
        if not api_compatible(record.manifest, self.app_version):
            return None
        for action in record.manifest.actions:
            if action.id == action_id:
                return action
        return None

    # -- listing ----------------------------------------------------------

    def list_plugins(self) -> list[PluginStatusRecord]:
        """Build a display-ready snapshot of every registered plugin.

        Corrupt plugins are reported with only their id, status, and the
        corruption reason; healthy plugins additionally carry manifest metadata
        (name, version, author, action/template/permission counts, and
        dependencies) for rendering in the plugin center.

        Returns:
            A list of :class:`PluginStatusRecord` snapshots, one per plugin.
        """
        records: list[PluginStatusRecord] = []
        for plugin_id, record in self._records.items():
            status = self._status(record)
            if record.corrupt or record.manifest is None:
                records.append(
                    PluginStatusRecord(
                        plugin_id=plugin_id,
                        status=status,
                        corrupt_reason=record.corrupt_reason,
                    )
                )
                continue
            manifest = record.manifest
            records.append(
                PluginStatusRecord(
                    plugin_id=plugin_id,
                    status=status,
                    name=manifest.name,
                    version=manifest.version,
                    api_version=manifest.api_version,
                    plugin_type=manifest.plugin_type,
                    author=manifest.author,
                    action_count=len(manifest.actions),
                    template_count=len(manifest.templates),
                    permission_count=len(manifest.permissions),
                    permissions=list(manifest.permissions),
                    dependencies=list(manifest.dependencies),
                )
            )
        return records
