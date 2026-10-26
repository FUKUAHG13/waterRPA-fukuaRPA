"""Audit x64 PE imports so an onedir release carries non-system dependencies.

FR-20260811-D15-6(建议·措辞更正)：导入分类规则与 verify_release 门禁一致——
- ``bundled``：随 onedir 分发的本地依赖（PyInstaller 收集项）；
- ``system``：命中系统 API 集前缀（SYSTEM_PREFIXES，如 api-ms-win-*/ext-ms-win-*）、
  ALWAYS_SYSTEM_DLLS 白名单，或 Windows 系统目录（System32）实际扫描到的 DLL；
- ``unresolved``：无法归类的导入，提示人工核对（发布门禁对 unresolved 判失败）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pefile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.hashutil import atomic_write_json  # noqa: E402

PE_SUFFIXES = {".exe", ".dll", ".pyd"}
MAX_PE_FILES = 5000
AMD64_MACHINE = 0x8664
SYSTEM_PREFIXES = ("api-ms-win-", "ext-ms-win-")
ALWAYS_SYSTEM_DLLS = {
    "advapi32.dll",
    "bcrypt.dll",
    "cfgmgr32.dll",
    "comctl32.dll",
    "comdlg32.dll",
    "crypt32.dll",
    "dwmapi.dll",
    "gdi32.dll",
    "gdiplus.dll",
    "imm32.dll",
    "kernel32.dll",
    "ncrypt.dll",
    "ntdll.dll",
    "ole32.dll",
    "oleaut32.dll",
    "rpcrt4.dll",
    "secur32.dll",
    "setupapi.dll",
    "shell32.dll",
    "shlwapi.dll",
    "user32.dll",
    "userenv.dll",
    "uxtheme.dll",
    "version.dll",
    "winhttp.dll",
    "winmm.dll",
    "ws2_32.dll",
}
MSVC_RUNTIME_PREFIXES = (
    "concrt",
    "msvcp",
    "ucrtbase",
    "vcomp",
    "vcruntime",
)


def system_dll_names() -> set[str]:
    root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    try:
        return {
            path.name.casefold()
            for path in root.iterdir()
            if path.is_file() and path.suffix.casefold() == ".dll"
        }
    except OSError:
        sys.stderr.write(
            "[audit] WARNING: System32 not readable, system DLL detection disabled\n"
        )
        return set()


def classify_dependency(
    name: str,
    package_index: dict[str, list[str]],
    system_names: set[str],
) -> str:
    """Classify a PE import into bundled / system / unresolved.

    FR-20260809-1305: 判据边界（按序求值，先命中先返回）——
    - ``bundled``：依赖名在打包载荷清单（package_index）内，由 fukuaRPA 自带；
    - ``unresolved``：MSVC 运行库前缀（msvcp*/vcruntime*/concrt* 等），Windows
      通常由系统/VC 运行库提供，不随包分发，故不算 bundled 但也无法静态确认为
      system，归 unresolved 提示人工核对；
    - ``system``：命中系统前缀（SYSTEM_PREFIXES，如 KERNEL32/USER32）或
      ALWAYS_SYSTEM_DLLS 白名单，或扫描出的系统目录 DLL（system_names），
      由操作系统保证存在；
    - 其余一律 ``unresolved``：不在包内、无法确认系统提供。发布门禁对
      unresolved **判失败**（ok=False → SystemExit；onefile 路径直接
      RuntimeError），必须补充打包载荷或白名单规则后重审，不是可放行的告警。
    """
    key = str(name or "").casefold()
    if key in package_index:
        return "bundled"
    if key.startswith(MSVC_RUNTIME_PREFIXES):
        return "unresolved"
    if key.startswith(SYSTEM_PREFIXES) or key in ALWAYS_SYSTEM_DLLS:
        return "system"
    if key in system_names:
        return "system"
    return "unresolved"


def _decode_import_name(raw_name) -> str:
    if not raw_name:
        raise ValueError("PE import descriptor has no DLL name")
    if isinstance(raw_name, bytes):
        return raw_name.decode("ascii", errors="strict")
    return str(raw_name)


def pe_imports(path: Path) -> tuple[int, list[str]]:
    image = pefile.PE(str(path), fast_load=True)
    try:
        directories = [
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
        ]
        image.parse_data_directories(directories=directories)
        names = set()
        for descriptor in getattr(image, "DIRECTORY_ENTRY_IMPORT", ()):
            names.add(_decode_import_name(descriptor.dll))
        for descriptor in getattr(image, "DIRECTORY_ENTRY_DELAY_IMPORT", ()):
            names.add(_decode_import_name(descriptor.dll))
        return int(image.FILE_HEADER.Machine), sorted(names, key=str.casefold)
    finally:
        image.close()


def build_runtime_closure(release_dir: Path) -> dict:
    release_dir = release_dir.resolve()
    if not release_dir.is_dir():
        raise ValueError(f"Release directory not found: {release_dir}")
    all_files = [
        path
        for path in sorted(
            release_dir.rglob("*"), key=lambda item: item.as_posix().casefold()
        )
        if path.is_file()
    ]
    package_index: dict[str, list[str]] = {}
    for path in all_files:
        relative = path.relative_to(release_dir).as_posix()
        package_index.setdefault(path.name.casefold(), []).append(relative)
    pe_paths = [path for path in all_files if path.suffix.casefold() in PE_SUFFIXES]
    if not pe_paths:
        raise ValueError("Release contains no PE executable payloads")
    if len(pe_paths) > MAX_PE_FILES:
        raise ValueError(f"Release contains more than {MAX_PE_FILES} PE files")

    system_names = system_dll_names()
    entries = []
    unresolved_references = []
    wrong_architecture = []
    parse_errors = []
    bundled_names = set()
    system_dependency_names = set()
    runtime_dependencies = set()
    import_reference_count = 0
    for path in pe_paths:
        relative = path.relative_to(release_dir).as_posix()
        try:
            machine, imports = pe_imports(path)
        except (OSError, UnicodeError, ValueError, pefile.PEFormatError) as error:
            parse_errors.append({"path": relative, "error": str(error)[:300]})
            continue
        if machine != AMD64_MACHINE:
            wrong_architecture.append({"path": relative, "machine": machine})
        classified = {"bundled": [], "system": [], "unresolved": []}
        for dependency in imports:
            category = classify_dependency(dependency, package_index, system_names)
            classified[category].append(dependency)
            import_reference_count += 1
            key = dependency.casefold()
            if category == "bundled":
                bundled_names.add(dependency)
                if key.startswith(MSVC_RUNTIME_PREFIXES):
                    runtime_dependencies.add(dependency)
            elif category == "system":
                system_dependency_names.add(dependency)
            else:
                unresolved_references.append(
                    {"importer": relative, "dependency": dependency}
                )
        entries.append(
            {
                "path": relative,
                "machine": f"0x{machine:04X}",
                "imports": imports,
                "bundled": classified["bundled"],
                "system": classified["system"],
                "unresolved": classified["unresolved"],
            }
        )

    ok = not unresolved_references and not wrong_architecture and not parse_errors
    return {
        "format": "fukuaRPA_runtime_closure",
        "format_version": 1,
        "build": release_dir.name,
        "target_machine": "AMD64 (0x8664)",
        "ok": ok,
        "pe_file_count": len(pe_paths),
        "import_reference_count": import_reference_count,
        "bundled_dependency_names": sorted(bundled_names, key=str.casefold),
        "system_dependency_names": sorted(
            system_dependency_names, key=str.casefold
        ),
        "bundled_msvc_runtime_names": sorted(
            runtime_dependencies, key=str.casefold
        ),
        "unresolved_references": unresolved_references,
        "wrong_architecture": wrong_architecture,
        "parse_errors": parse_errors,
        "files": entries,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    release_dir = args.release_dir.resolve()
    output = (
        args.output.resolve()
        if args.output
        else release_dir / "RUNTIME_CLOSURE.json"
    )
    report = build_runtime_closure(release_dir)
    if args.check:
        if not output.is_file():
            raise SystemExit(f"Runtime closure report not found: {output}")
        expected = json.loads(output.read_text(encoding="utf-8"))
        if expected != report:
            raise SystemExit("Runtime closure report no longer matches the release")
    else:
        atomic_write_json(output, report)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "build",
                    "ok",
                    "pe_file_count",
                    "import_reference_count",
                    "bundled_msvc_runtime_names",
                    "unresolved_references",
                    "wrong_architecture",
                    "parse_errors",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not report["ok"]:
        raise SystemExit("Runtime dependency closure audit failed")


if __name__ == "__main__":
    main()
