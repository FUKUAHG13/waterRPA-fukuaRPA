"""Generate an offline CycloneDX SBOM and third-party license notices."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import sys
import time
import uuid
from collections import deque
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.hashutil import atomic_write_text  # noqa: E402

RUNTIME_ROOTS = (
    "PySide6",
    "opencv-python-headless",
    "numpy",
    "Pillow",
    "PyAutoGUI",
    "mss",
    "psutil",
    "pyperclip",
    "uiautomation",
    "comtypes",
)
BUILD_ROOTS = ("PyInstaller", "pefile")
LICENSE_PREFIXES = ("license", "licence", "copying", "notice", "authors")
MAX_LICENSE_FILE_BYTES = 2 * 1024 * 1024
MAX_LICENSE_FILES_PER_COMPONENT = 64
MAX_TOTAL_LICENSE_BYTES = 32 * 1024 * 1024


# FR-20260811-D15-5(建议): 单次 CLI 运行期内累积无法解析的依赖声明；
# main 末尾据此 fail-closed，使"依赖声明解析失败"从静默 WARNING 上移为发布门禁。
_UNPARSED_REQUIREMENTS: list[str] = []


def _requirements(distribution) -> list[str]:
    names = []
    for raw in distribution.requires or ():
        try:
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                continue
            names.append(requirement.name)
        except (InvalidRequirement, ValueError):
            # ValueError 覆盖旧 packaging 的 UndefinedEnvironmentName 等
            # marker.evaluate 运行时错误（依赖声明本身不可判定）。
            print(f"[sbom] WARNING: 跳过无法解析的依赖声明: {raw[:120]}", file=sys.stderr)
            _UNPARSED_REQUIREMENTS.append(raw)
            continue
    return sorted(set(names), key=lambda value: canonicalize_name(value))


def dependency_closure() -> tuple[dict[str, dict], dict[str, set[str]]]:
    scopes = {
        canonicalize_name(name): "runtime" for name in RUNTIME_ROOTS
    }
    scopes.update(
        {
            canonicalize_name(name): "build"
            for name in BUILD_ROOTS
            if canonicalize_name(name) not in scopes
        }
    )
    queue_items = deque((name, scope) for name, scope in scopes.items())
    components: dict[str, dict] = {}
    graph: dict[str, set[str]] = {}
    while queue_items:
        requested_name, scope = queue_items.popleft()
        try:
            distribution = metadata.distribution(requested_name)
        except metadata.PackageNotFoundError:
            components[requested_name] = {
                "name": requested_name,
                "version": "not-installed",
                "scope": scope,
                "missing": True,
                "distribution": None,
            }
            graph.setdefault(requested_name, set())
            continue
        canonical = canonicalize_name(distribution.metadata.get("Name") or requested_name)
        previous = components.get(canonical)
        effective_scope = (
            "runtime"
            if scope == "runtime" or (previous and previous.get("scope") == "runtime")
            else "build"
        )
        components[canonical] = {
            "name": distribution.metadata.get("Name") or requested_name,
            "version": distribution.version,
            "scope": effective_scope,
            "missing": False,
            "distribution": distribution,
        }
        dependencies = _requirements(distribution)
        graph[canonical] = {canonicalize_name(name) for name in dependencies}
        for dependency in dependencies:
            child = canonicalize_name(dependency)
            child_scope = scopes.get(child)
            next_scope = "runtime" if effective_scope == "runtime" else "build"
            if child_scope != "runtime" and child_scope != next_scope:
                scopes[child] = next_scope
                queue_items.append((child, next_scope))
            elif child not in components:
                queue_items.append((child, child_scope or next_scope))
    return components, graph


def _project_urls(distribution) -> list[dict]:
    references = []
    seen = set()
    for raw in distribution.metadata.get_all("Project-URL") or ():
        if "," not in raw:
            continue
        label, url = (part.strip() for part in raw.split(",", 1))
        if not url or url in seen:
            continue
        seen.add(url)
        reference_type = "website"
        if "source" in label.lower() or "repository" in label.lower():
            reference_type = "vcs"
        elif "issue" in label.lower() or "bug" in label.lower():
            reference_type = "issue-tracker"
        references.append({"type": reference_type, "url": url})
    home = str(distribution.metadata.get("Home-page") or "").strip()
    if home and home not in seen:
        references.append({"type": "website", "url": home})
    return references


def _license_name(distribution) -> str:
    expression = str(distribution.metadata.get("License-Expression") or "").strip()
    if expression:
        return expression
    declared = str(distribution.metadata.get("License") or "").strip()
    if declared and len(declared) <= 200 and "\n" not in declared:
        return declared
    classifiers = distribution.metadata.get_all("Classifier") or ()
    licenses = [
        item.split(" :: ")[-1]
        for item in classifiers
        if item.startswith("License ::")
    ]
    return ", ".join(licenses) if licenses else "NOASSERTION"


def build_sbom(
    app_version: str,
    build_name: str,
    *,
    build_id: str = "",
    source_sha256: str = "",
) -> tuple[dict, dict[str, dict]]:
    components, graph = dependency_closure()
    rendered = []
    refs = {}
    for canonical, item in sorted(components.items()):
        distribution = item.get("distribution")
        version = str(item.get("version") or "not-installed")
        reference = f"pkg:pypi/{canonical}@{version}"
        refs[canonical] = reference
        component = {
            "type": "library",
            "bom-ref": reference,
            "name": str(item.get("name") or canonical),
            "version": version,
            "scope": "required" if item.get("scope") == "runtime" else "excluded",
            "purl": reference,
            "licenses": [
                {
                    "license": {
                        "name": _license_name(distribution)
                        if distribution is not None
                        else "NOASSERTION"
                    }
                }
            ],
            "properties": [
                {"name": "fukuaRPA.dependency_scope", "value": item.get("scope", "runtime")},
                {"name": "fukuaRPA.installed", "value": str(not item.get("missing")).lower()},
            ],
        }
        if distribution is not None:
            external = _project_urls(distribution)
            if external:
                component["externalReferences"] = external
        rendered.append(component)
    dependency_items = []
    for canonical in sorted(components):
        dependency_items.append(
            {
                "ref": refs[canonical],
                "dependsOn": sorted(
                    refs[child]
                    for child in graph.get(canonical, set())
                    if child in refs
                ),
            }
        )
    identity = "\n".join(
        f"{item['name']}=={item['version']}" for item in rendered
    )
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"fukuaRPA:{build_name}:{identity}")
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(int(os.environ.get("SOURCE_DATE_EPOCH", "1735689600"))),
            ),
            "component": {
                "type": "application",
                "bom-ref": f"fukuaRPA:{app_version}",
                "name": "fukuaRPA",
                "version": app_version.lstrip("v"),
                "licenses": [{"license": {"id": "MIT"}}],
            },
            "properties": [
                {"name": "fukuaRPA.build_name", "value": build_name},
                {"name": "fukuaRPA.network_on_startup", "value": "false"},
                {"name": "fukuaRPA.build_id", "value": build_id},
                {"name": "fukuaRPA.source_sha256", "value": source_sha256},
            ],
        },
        "components": rendered,
        "dependencies": dependency_items,
    }
    return sbom, components


def _license_files(distribution) -> list[Path]:
    selected = []
    for relative in distribution.files or ():
        name = Path(str(relative)).name.lower()
        if not name.startswith(LICENSE_PREFIXES):
            continue
        path = Path(distribution.locate_file(relative))
        try:
            if path.is_file() and path.stat().st_size <= MAX_LICENSE_FILE_BYTES:
                selected.append(path)
        except OSError:
            continue
        if len(selected) >= MAX_LICENSE_FILES_PER_COMPONENT:
            break
    return sorted(set(selected), key=lambda path: str(path).casefold())


def build_notices(components: dict[str, dict]) -> tuple[str, str]:
    notice_lines = [
        "fukuaRPA third-party notices",
        "",
        "This file lists bundled runtime dependencies and build-only tools.",
        "Complete license texts found in the build environment are collected in THIRD_PARTY_LICENSES.txt.",
        "",
    ]
    license_sections = ["fukuaRPA collected third-party license texts", ""]
    seen_license_hashes = set()
    total_license_bytes = 0
    for canonical, item in sorted(components.items()):
        distribution = item.get("distribution")
        license_name = _license_name(distribution) if distribution else "NOASSERTION"
        notice_lines.append(
            f"- {item.get('name', canonical)} {item.get('version', 'unknown')} "
            f"[{item.get('scope', 'runtime')}] - {license_name}"
        )
        if distribution is None:
            continue
        for path in _license_files(distribution):
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            if digest in seen_license_hashes:
                continue
            if total_license_bytes + len(raw) > MAX_TOTAL_LICENSE_BYTES:
                continue
            seen_license_hashes.add(digest)
            total_license_bytes += len(raw)
            text = raw.decode("utf-8", errors="replace")
            license_sections.extend(
                [
                    "=" * 78,
                    f"{item.get('name', canonical)} {item.get('version', 'unknown')} - {path.name}",
                    "=" * 78,
                    text.rstrip(),
                    "",
                ]
            )
    return "\n".join(notice_lines) + "\n", "\n".join(license_sections) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--app-version", required=True)
    parser.add_argument("--build-name", required=True)
    parser.add_argument("--build-info", type=Path)
    args = parser.parse_args()
    release_dir = args.release_dir.resolve()
    if not release_dir.is_dir():
        raise SystemExit(f"Release directory not found: {release_dir}")
    build_id = ""
    source_sha256 = ""
    if args.build_info:
        build_info = json.loads(args.build_info.read_text(encoding="utf-8"))
        if build_info.get("application_version") != args.app_version:
            raise SystemExit("SBOM 版本与构建记录不一致")
        if build_info.get("build_name") != args.build_name:
            raise SystemExit("SBOM 构建名称与构建记录不一致")
        build_id = str(build_info.get("build_id") or "")
        source_sha256 = str(build_info.get("source_sha256") or "")
        if not build_id or not source_sha256:
            raise SystemExit("构建记录缺少 SBOM 所需的统一构建身份")
    sbom, components = build_sbom(
        args.app_version,
        args.build_name,
        build_id=build_id,
        source_sha256=source_sha256,
    )
    notices, licenses = build_notices(components)
    sbom_path = release_dir / "SBOM.cdx.json"
    notices_path = release_dir / "THIRD_PARTY_NOTICES.txt"
    licenses_path = release_dir / "THIRD_PARTY_LICENSES.txt"
    atomic_write_text(sbom_path, json.dumps(sbom, ensure_ascii=False, indent=2) + "\n")
    atomic_write_text(notices_path, notices)
    atomic_write_text(licenses_path, licenses)
    missing = [
        item["name"]
        for item in components.values()
        if item.get("missing")
    ]
    print(
        json.dumps(
            {
                "sbom": str(sbom_path),
                "components": len(components),
                "missing": missing,
                "license_bytes": licenses_path.stat().st_size,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if missing:
        raise SystemExit(f"SBOM dependencies are not installed: {missing}")
    # FR-20260811-D15-5(建议): 声明无法解析的依赖同样阻断——避免生成依赖闭包
    # 不完整的 SBOM 却以成功退出码通过门禁（具体声明见上方 WARNING 输出）。
    if _UNPARSED_REQUIREMENTS:
        raise SystemExit(
            "SBOM 存在无法解析的依赖声明（见上方 WARNING），拒绝生成："
            + "、".join(_UNPARSED_REQUIREMENTS[:5])
            + ("…" if len(_UNPARSED_REQUIREMENTS) > 5 else "")
        )


if __name__ == "__main__":
    main()
