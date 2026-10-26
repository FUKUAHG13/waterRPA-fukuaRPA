"""Plugin manifest schema, strict validation, and version compatibility.

This is a PURE, framework-free data layer for the fukuaRPA plugin system
(see docs/adr/0036-plugin-system.md and 设计基准「插件」一节; the original
implementation plan sections 5.1-5.7 live in an untracked local document). It
depends only on the Python standard library and the sibling modules created
alongside it. It does NOT import PySide, Qt, or any other fukua_rpa runtime
module.

The manifest is the single source of truth for a plugin's identity, declared
actions, templates, permissions, and per-file integrity hashes.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .constants import PLUGIN_TYPES
from .json_safety import loads_bounded_json

# --------------------------------------------------------------------------
# Supported API / protocol ranges
# --------------------------------------------------------------------------

MANIFEST_FORMAT = "fukuaRPA.plugin"
MANIFEST_SCHEMA_VERSION = 1

MIN_SUPPORTED_API_VERSION = 1
MAX_SUPPORTED_API_VERSION = 1


# --------------------------------------------------------------------------
# String length caps (defense against oversized fields / memory abuse)
# --------------------------------------------------------------------------

MAX_ID_LEN = 128
MAX_NAME_LEN = 128
MAX_AUTHOR_LEN = 128
MAX_VERSION_LEN = 32
MAX_ENTRYPOINT_LEN = 256
MAX_ACTION_ID_LEN = 128
MAX_TEMPLATE_ID_LEN = 128
MAX_DESC_LEN = 512
MAX_PERMISSION_LEN = 64

# FR-a01-002/003: 清单属不可信外部输入，解析必须经 json_safety 有界入口（大小/
# 深度/节点数上限 + 拒重复键/NaN/Infinity）；actions/templates/files 数量与依赖 id
# 长度必须有独立上限，避免第三方清单在 256KB 内放大 UI/注册表工作。
MANIFEST_MAX_BYTES = 256 * 1024
MAX_ACTIONS = 128
MAX_TEMPLATES = 64
MAX_FILES = 512


class PluginError(Exception):
    """Base class for all plugin data-layer errors."""


class PluginManifestError(PluginError):
    """Raised when a plugin manifest fails strict validation."""


_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class PluginPermission(Enum):
    """Allowed WASM/capability categories from design section 5.5.

    The first version intentionally provides NO network capability.
    """

    SCREEN_CAPTURE_REGION = "screen.capture_region"
    VISION_MATCH_TEMPLATE = "vision.match_template"
    INPUT_MOUSE = "input.mouse"
    INPUT_KEYBOARD = "input.keyboard"
    WINDOW_QUERY = "window.query"
    WINDOW_CONTROL = "window.control"
    VARIABLES_READ_DECLARED = "variables.read_declared"
    VARIABLES_WRITE_DECLARED = "variables.write_declared"
    FILES_PLUGIN_PRIVATE_DATA = "files.plugin_private_data"
    FILES_USER_SELECTED = "files.user_selected"
    PROCESS_LAUNCH_CONFIRMED = "process.launch_confirmed"


# NOTE: keep ALL_PERMISSION_VALUES derived from the enum so the two can never
# drift apart.
ALL_PERMISSION_VALUES = frozenset(p.value for p in PluginPermission)


def permission_set_valid(perms: Any) -> bool:
    """Return True only if *perms* is a list of known permission strings."""

    if not isinstance(perms, (list, tuple)):
        return False
    for item in perms:
        if not isinstance(item, str) or len(item) > MAX_PERMISSION_LEN:
            return False
        if item not in ALL_PERMISSION_VALUES:
            return False
    return True


def _normalize_permissions(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        raise PluginManifestError("permissions 必须是数组")
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or len(item) > MAX_PERMISSION_LEN:
            raise PluginManifestError(f"非法的权限声明：{item!r}")
        if item not in ALL_PERMISSION_VALUES:
            raise PluginManifestError(f"未知权限：{item}")
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def is_safe_relative_path(path: Any) -> bool:
    """Reject absolute paths, backslashes, and ``..`` traversal.

    Plugin manifest file paths MUST stay inside the plugin directory.
    """

    if not isinstance(path, str) or not path:
        return False
    if "\\" in path:
        return False
    if path.startswith("/") or path.startswith("\\"):
        return False
    if os.path.isabs(path):
        return False
    normalized = os.path.normpath(path)
    return not (normalized == ".." or normalized.startswith(".." + os.sep))


def _validate_entrypoint_path(entrypoint: str) -> None:
    """Ensure the entrypoint stays inside the plugin directory.

    The entrypoint may be a bare relative path (e.g. ``main.py``) or a
    ``path:function`` reference (e.g. ``src/plugin/action.py:run``). The
    ``:function`` suffix is treated as a non-path identifier, so the *whole*
    string is checked as a filesystem path rather than splitting on ``:`` —
    otherwise a Windows drive letter (``C:...``) would be mistaken for the
    separator and slip through as "relative".
    """
    # Windows drive-letter prefixes (``C:`` / ``C:\`` / ``C:/``) are absolute
    # paths. Match with a regex rather than ``os.path.splitdrive`` so the
    # rejection is deterministic across platforms — ``splitdrive`` is a no-op on
    # POSIX and would otherwise let ``C:evil.py`` slip through there.
    if re.match(r"^[A-Za-z]:", entrypoint):
        raise PluginManifestError(f"entrypoint 路径含驱动器前缀：{entrypoint!r}")
    if not is_safe_relative_path(entrypoint):
        raise PluginManifestError(f"entrypoint 路径非法：{entrypoint!r}")
    # Reject ``..`` components that ``normpath`` would collapse away
    # (e.g. ``src/../../evil.py:run``), which still escape the plugin dir when
    # the loader joins the entrypoint onto the plugin base directory.
    if any(seg == ".." for seg in entrypoint.split("/")):
        raise PluginManifestError(f"entrypoint 路径含非法 '..' 段：{entrypoint!r}")


def compare_versions(a: str, b: str) -> int:
    """Compare two ``X.Y.Z`` versions: -1, 0, or 1."""

    pa = _parse_version(a)
    pb = _parse_version(b)
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


def _parse_version(value: Any) -> tuple[int, int, int]:
    if not isinstance(value, str) or not _VERSION_RE.match(value):
        raise PluginManifestError(f"版本号格式无效：{value!r}")
    major, minor, patch = (int(x) for x in value.split("."))
    return (major, minor, patch)


def api_compatible(
    manifest: PluginManifest,
    app_version: str,
    *,
    min_api_version: int = MIN_SUPPORTED_API_VERSION,
    max_api_version: int = MAX_SUPPORTED_API_VERSION,
) -> bool:
    """True if the manifest's API and minimum app version are acceptable.

    Checks both:
      * ``min_api_version <= manifest.api_version <= max_api_version``
      * ``manifest.min_app_version <= app_version``
    """

    if not (min_api_version <= manifest.api_version <= max_api_version):
        return False
    # DESIGN: APP_VERSION 使用带 "v" 前缀的展示格式（如 "v1.0.50"，见
    # constants.py），而 _parse_version 只接受严格的 "X.Y.Z"。在入口归一化，
    # 去除展示用前缀后再比较，避免所有插件被误判为不兼容。
    normalized = (
        app_version[1:]
        if isinstance(app_version, str) and app_version.startswith("v")
        else app_version
    )
    try:
        return compare_versions(manifest.min_app_version, normalized) <= 0
    except PluginManifestError:
        return False


# --------------------------------------------------------------------------
# Manifest sub-records
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PluginAction:
    """A single invocable action declared by a plugin manifest.

    Attributes:
        id: Stable action identifier, unique within the plugin.
        name: Human-readable display name.
        description: Short description shown in the plugin center.
        permissions: Capability strings the action requires.
        input_schema: JSON-schema-lite object describing accepted config.
        output_schema: JSON-schema-lite object describing the result.
    """

    id: str
    name: str
    description: str
    permissions: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PluginTemplate:
    """A workflow template bundled with a plugin.

    Attributes:
        id: Stable template identifier, unique within the plugin.
        name: Human-readable display name.
        description: Short description shown in the plugin center.
        steps: Ordered workflow step dicts imported on use.
    """

    id: str
    name: str
    description: str
    steps: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class PluginFile:
    """A declared plugin file and its integrity hash.

    Attributes:
        path: Plugin-relative POSIX path; always inside the plugin dir.
        sha256: Lowercase hex SHA-256 digest of the file's bytes.
    """

    path: str
    sha256: str


# --------------------------------------------------------------------------
# Manifest root
# --------------------------------------------------------------------------

_KNOWN_REQUIRED = frozenset(
    {
        "format",
        "manifest_version",
        "id",
        "name",
        "version",
        "author",
        "plugin_type",
        "api_version",
        "min_app_version",
        "entrypoint",
        "permissions",
        "actions",
        "templates",
        "files",
    }
)
# Optional, forward-compatible fields currently understood by the loader.
_KNOWN_OPTIONAL = frozenset({"dependencies"})


@dataclass(frozen=True)
class PluginManifest:
    """Validated, immutable in-memory representation of a plugin manifest.

    Constructed only through :meth:`parse_json` / :meth:`from_dict`, which
    enforce the strict schema (required fields, format/version constants,
    identifier and path safety, per-file hashes). ``extra`` preserves
    underscore-prefixed private developer notes so round-tripping does not
    silently drop them.

    Attributes:
        format: Manifest format identifier; always ``MANIFEST_FORMAT``.
        manifest_version: Schema version; always ``MANIFEST_SCHEMA_VERSION``.
        id: Stable plugin identifier matching the install directory name.
        name: Human-readable plugin name.
        version: Plugin version string in ``X.Y.Z`` form.
        author: Plugin author display string.
        plugin_type: One of the supported ``PLUGIN_TYPES``.
        api_version: Plugin API version checked against the supported range.
        min_app_version: Minimum application version required to run.
        entrypoint: Optional plugin entrypoint path, or None when unused.
        permissions: Plugin-level capability strings.
        actions: Declared invocable actions.
        templates: Bundled workflow templates.
        files: Declared files with SHA-256 integrity hashes.
        dependencies: Identifiers of plugins this plugin depends on.
        extra: Underscore-prefixed private notes preserved on round-trip.
    """

    format: str
    manifest_version: int
    id: str
    name: str
    version: str
    author: str
    plugin_type: str
    api_version: int
    min_app_version: str
    entrypoint: str | None
    permissions: list[str]
    actions: list[PluginAction]
    templates: list[PluginTemplate]
    files: list[PluginFile]
    dependencies: list[str] = field(default_factory=list)
    # Underscore-prefixed private developer notes (e.g. ``_note_p2``) that the
    # loader never consumes. Captured here so the strict validator accepts valid
    # manifests without silently dropping them on re-serialization.
    extra: dict[str, Any] = field(default_factory=dict)

    # -- serialization ----------------------------------------------------

    @classmethod
    def parse_json(cls, text: str | bytes) -> PluginManifest:
        """Parse manifest JSON text with duplicate-key rejection, then validate.

        Args:
            text: Raw JSON bytes or string of the manifest.

        Returns:
            The validated :class:`PluginManifest`.

        Raises:
            PluginManifestError: If the text is not valid JSON or fails schema
                validation.
        """

        try:
            # FR-a01-002: 清单属不可信外部输入，必须走 json_safety 集中有界入口
            # （设计基准 §JSON 解析边界 FR-20260811-D2-001）：大小/深度/节点数上限、
            # 拒绝重复键与 NaN/Infinity。
            data = loads_bounded_json(
                text, max_bytes=MANIFEST_MAX_BYTES, label="插件清单 JSON"
            )
        except ValueError as error:
            raise PluginManifestError(f"清单 JSON 解析失败：{error}") from error
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PluginManifest:
        """Strictly validate a decoded manifest mapping and build the record.

        Rejects unknown top-level fields (except underscore-prefixed private
        notes), enforces required fields, the format/schema-version constants,
        identifier/version/path safety rules, and per-file hash format.

        Args:
            data: Decoded JSON object representing the manifest.

        Returns:
            The validated, immutable :class:`PluginManifest`.

        Raises:
            PluginManifestError: If any field is missing, mistyped, out of
                range, or otherwise violates the schema.
        """
        if not isinstance(data, Mapping):
            raise PluginManifestError("清单必须是 JSON 对象")

        for key in data:
            if key not in _KNOWN_REQUIRED and key not in _KNOWN_OPTIONAL:
                # INVARIANT: underscore-prefixed keys are private developer
                # notes (e.g. _note_p2) the loader never consumes. Accept them
                # so the strict validator does not reject valid-in-practice
                # manifests such as python_action plugins still in the P2 stage.
                if key.startswith("_"):
                    continue
                raise PluginManifestError(f"未知清单字段：{key}")

        for key in _KNOWN_REQUIRED:
            if key not in data:
                raise PluginManifestError(f"缺少必填字段：{key}")

        # format / schema
        fmt = data["format"]
        if fmt != MANIFEST_FORMAT:
            raise PluginManifestError(f"format 必须为 {MANIFEST_FORMAT!r}")
        manifest_version = data["manifest_version"]
        if manifest_version != MANIFEST_SCHEMA_VERSION:
            raise PluginManifestError(
                f"manifest_version 不支持：{manifest_version}"
            )

        plugin_id = _check_string(data["id"], "id", MAX_ID_LEN, _ID_RE)
        if ".." in plugin_id:
            raise PluginManifestError(f"id 不得包含 '..'：{plugin_id!r}")
        name = _check_string(data["name"], "name", MAX_NAME_LEN)
        author = _check_string(data["author"], "author", MAX_AUTHOR_LEN)
        version = _check_string(data["version"], "version", MAX_VERSION_LEN)
        _parse_version(version)  # raises on bad format
        min_app_version = _check_string(
            data["min_app_version"], "min_app_version", MAX_VERSION_LEN
        )
        _parse_version(min_app_version)

        plugin_type = data["plugin_type"]
        if plugin_type not in PLUGIN_TYPES:
            raise PluginManifestError(f"plugin_type 非法：{plugin_type}")

        api_version = data["api_version"]
        if isinstance(api_version, bool) or not isinstance(api_version, int):
            raise PluginManifestError("api_version 必须是整数")
        if api_version < 1:
            raise PluginManifestError("api_version 必须 >= 1")

        entrypoint = data["entrypoint"]
        if entrypoint is not None:
            entrypoint = _check_string(
                entrypoint, "entrypoint", MAX_ENTRYPOINT_LEN
            )
            # S7: the loader resolves the entrypoint relative to the plugin dir,
            # so a traversal/absolute path would let a malicious manifest escape
            # the sandbox. Reject it at parse time.
            _validate_entrypoint_path(entrypoint)

        permissions = _normalize_permissions(data["permissions"])

        actions = _parse_actions(data["actions"])
        templates = _parse_templates(data["templates"])
        files = _parse_files(data["files"])

        dependencies = _parse_dependencies(data.get("dependencies"))

        extra = {key: value for key, value in data.items() if key.startswith("_")}

        return cls(
            format=fmt,
            manifest_version=manifest_version,
            id=plugin_id,
            name=name,
            version=version,
            author=author,
            plugin_type=plugin_type,
            api_version=api_version,
            min_app_version=min_app_version,
            entrypoint=entrypoint,
            permissions=permissions,
            actions=actions,
            templates=templates,
            files=files,
            dependencies=dependencies,
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the manifest back to a JSON-compatible dict.

        Returns:
            A plain dict with the canonical schema fields plus any preserved
            underscore-prefixed private notes from ``extra``.
        """
        result = {
            "format": self.format,
            "manifest_version": self.manifest_version,
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "plugin_type": self.plugin_type,
            "api_version": self.api_version,
            "min_app_version": self.min_app_version,
            "entrypoint": self.entrypoint,
            "permissions": list(self.permissions),
            "actions": [
                {
                    "id": a.id,
                    "name": a.name,
                    "description": a.description,
                    "permissions": list(a.permissions),
                    "input_schema": a.input_schema,
                    "output_schema": a.output_schema,
                }
                for a in self.actions
            ],
            "templates": [
                {
                    "id": t.id,
                    "name": t.name,
                    "description": t.description,
                    "steps": t.steps,
                }
                for t in self.templates
            ],
            "files": [
                {"path": f.path, "sha256": f.sha256} for f in self.files
            ],
            "dependencies": list(self.dependencies),
        }
        # Preserve private developer notes on round-trip.
        result.update(self.extra)
        return result


# --------------------------------------------------------------------------
# Internal field parsers
# --------------------------------------------------------------------------

def _check_string(
    value: Any,
    label: str,
    max_len: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str):
        raise PluginManifestError(f"{label} 必须是字符串")
    if not value:
        raise PluginManifestError(f"{label} 不能为空")
    if len(value) > max_len:
        raise PluginManifestError(f"{label} 长度超过上限 {max_len}")
    if pattern is not None and not pattern.match(value):
        raise PluginManifestError(f"{label} 格式非法：{value!r}")
    return value


def _parse_actions(raw: Any) -> list[PluginAction]:
    if not isinstance(raw, (list, tuple)):
        raise PluginManifestError("actions 必须是数组")
    # FR-a01-003: 数量上限，防止第三方清单在 256KB 内放大 UI/注册表工作。
    if len(raw) > MAX_ACTIONS:
        raise PluginManifestError(f"actions 数量超过上限 {MAX_ACTIONS}")
    actions: list[PluginAction] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise PluginManifestError(f"action[{index}] 必须是对象")
        action_id = _check_string(
            item.get("id"), f"actions[{index}].id", MAX_ACTION_ID_LEN, _ID_RE
        )
        if action_id in seen_ids:
            raise PluginManifestError(f"action id 重复：{action_id}")
        seen_ids.add(action_id)
        name = _check_string(item.get("name", ""), "action.name", MAX_NAME_LEN)
        description = _check_string(
            item.get("description", ""), "action.description", MAX_DESC_LEN
        )
        permissions = _normalize_permissions(item.get("permissions", []))
        input_schema = item.get("input_schema", {})
        output_schema = item.get("output_schema", {})
        if not isinstance(input_schema, Mapping) or not isinstance(
            output_schema, Mapping
        ):
            raise PluginManifestError(
                f"action {action_id} 的 schema 必须是对象"
            )
        actions.append(
            PluginAction(
                id=action_id,
                name=name,
                description=description,
                permissions=permissions,
                input_schema=dict(input_schema),
                output_schema=dict(output_schema),
            )
        )
    return actions


def _parse_templates(raw: Any) -> list[PluginTemplate]:
    if not isinstance(raw, (list, tuple)):
        raise PluginManifestError("templates 必须是数组")
    # FR-a01-003: 数量上限。
    if len(raw) > MAX_TEMPLATES:
        raise PluginManifestError(f"templates 数量超过上限 {MAX_TEMPLATES}")
    templates: list[PluginTemplate] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise PluginManifestError(f"template[{index}] 必须是对象")
        template_id = _check_string(
            item.get("id"), f"templates[{index}].id", MAX_TEMPLATE_ID_LEN, _ID_RE
        )
        if template_id in seen_ids:
            raise PluginManifestError(f"template id 重复：{template_id}")
        seen_ids.add(template_id)
        name = _check_string(
            item.get("name", ""), "template.name", MAX_NAME_LEN
        )
        description = _check_string(
            item.get("description", ""), "template.description", MAX_DESC_LEN
        )
        steps = item.get("steps", [])
        if not isinstance(steps, (list, tuple)):
            raise PluginManifestError(f"template {template_id} 的 steps 必须是数组")
        templates.append(
            PluginTemplate(
                id=template_id,
                name=name,
                description=description,
                steps=list(steps),
            )
        )
    return templates


def _parse_files(raw: Any) -> list[PluginFile]:
    if not isinstance(raw, (list, tuple)):
        raise PluginManifestError("files 必须是数组")
    # FR-a01-003: 数量上限（与 ZIP 侧 max_file_count 一致约束声明资源规模）。
    if len(raw) > MAX_FILES:
        raise PluginManifestError(f"files 数量超过上限 {MAX_FILES}")
    files: list[PluginFile] = []
    seen_paths: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise PluginManifestError(f"files[{index}] 必须是对象")
        path = item.get("path")
        sha256 = item.get("sha256")
        if not isinstance(path, str):
            raise PluginManifestError(f"files[{index}] path 必须是字符串")
        if not is_safe_relative_path(path):
            raise PluginManifestError(f"files[{index}] 路径非法：{path!r}")
        if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
            raise PluginManifestError(f"files[{index}] sha256 非法")
        if path in seen_paths:
            raise PluginManifestError(f"files 路径重复：{path}")
        seen_paths.add(path)
        files.append(PluginFile(path=path, sha256=sha256.lower()))
    return files


def _parse_dependencies(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise PluginManifestError("dependencies 必须是数组")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not _ID_RE.match(item):
            raise PluginManifestError(f"非法的依赖插件 id：{item!r}")
        # FR-a01-003: 依赖 id 复用 MAX_ID_LEN 长度上限。
        if len(item) > MAX_ID_LEN:
            raise PluginManifestError(f"依赖插件 id 过长：{item!r}")
        out.append(item)
    return out
