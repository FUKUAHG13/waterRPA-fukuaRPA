"""Write a reproducible source/dependency record into an onedir release."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fukua_rpa.constants import (  # noqa: E402
    APP_VERSION,
    BUILD_NAME,
    SUPPORTED_WINDOWS_TEXT,
)
from fukua_rpa.vision.core import NativeVisionCore  # noqa: E402
from scripts.build_identity import create_build_identity  # noqa: E402
from scripts.hashutil import atomic_write_json, hash_file, hash_source_text  # noqa: E402
from scripts.private_release_inputs import (  # noqa: E402
    is_private_validation_path,
    validation_receipt,
)
from scripts.release_provenance import (  # noqa: E402
    repository_provenance,
    require_formal_release_provenance,
)

SOURCE_ROOTS = (
    Path("fukuaRPA.py"),
    Path("fukuaRPA_onedir.spec"),
    Path("fukuaRPA_onefile.spec"),
    Path("requirements.txt"),
    Path("requirements-build.txt"),
    Path("fukua_rpa"),
    Path("plugins"),
    Path("native_core/fukua_rpa_core.cpp"),
    Path("native_core/build_native_core.ps1"),
    Path("assets/version_info.txt"),
    Path("assets/version_info_onefile.txt"),
    Path("assets/fukuaRPA.ico"),
    Path("assets/fukuaRPA.svg"),
    Path("assets/step_settings.svg"),
    Path("assets/search.svg"),
    # FR-20261003-606: manifest 由两个 spec 通过 manifest= 嵌入 EXE 资源，是决定
    # 产物字节与进程行为的构建输入（UAC 级别 / DPI 感知）。遗漏它会使 build_id
    # 不再唯一标识产物，并让构建期"源码变化即中止"护栏对该文件失效。
    Path("assets/fukuaRPA.manifest"),
    Path("scripts"),
    Path("tests"),
    Path("README.md"),
    Path("ARCHITECTURE.md"),
    Path("ROADMAP.md"),
    Path("CONTEXT.md"),
    Path("AGENTS.md"),
    Path("LICENSE"),
)

DEPENDENCIES = (
    "PySide6",
    "opencv-python-headless",
    "numpy",
    "Pillow",
    "PyAutoGUI",
    "mss",
    "psutil",
    "pyperclip",
    "PyInstaller",
    "uiautomation",
    "comtypes",
    "packaging",
    "pefile",
)


def source_files():
    paths = []
    for item in SOURCE_ROOTS:
        if is_private_validation_path(item.as_posix()):
            continue
        absolute = ROOT / item
        if absolute.is_file():
            paths.append(absolute)
        elif absolute.is_dir():
            paths.extend(
                path
                for path in absolute.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                # SAFETY/FR-005: 排除点开头的本机状态文件/目录（如
                # plugins/.disabled_plugins.json）。它们由运行时生成、被 .gitignore
                # 排除，不是源码；纳入指纹会让同一提交在不同机器上得到不同
                # build_id，并可能在构建窗口内误报"构建期间源码发生变化"而中止
                # 构建（ARCHITECTURE #66/#67）。
                # 已核对：SOURCE_ROOTS 内不存在任何受跟踪的点开头文件，因此本规则
                # 不会漏掉源码。
                and not any(part.startswith(".") for part in path.relative_to(ROOT).parts)
                and path.suffix.lower() not in {".pyc", ".obj", ".lib", ".exp", ".dll"}
                and not is_private_validation_path(path.relative_to(ROOT).as_posix())
            )
    return sorted(set(paths), key=lambda path: path.relative_to(ROOT).as_posix().casefold())


def source_fingerprint():
    aggregate = hashlib.sha256()
    entries = []
    for path in source_files():
        relative = path.relative_to(ROOT).as_posix()
        # FR-SCOPE010-001: 指纹必须描述**提交**而不是**检出**。此前直接对工作树
        # 字节取哈希，于是同一提交在 `core.autocrlf` 下检出为 CRLF 时得到不同的
        # `source_sha256`/`build_id`。改为统一换行为 LF 后再哈希，与 git 对文本
        # 文件的处理一致。注意这里**只**改源码指纹；`hash_file`（io_utils）仍是
        # 原始字节哈希，因为凭据库、完整性清单和插件载荷也复用它。
        checksum, size = hash_source_text(path.read_bytes())
        entries.append({"path": relative, "size": size, "sha256": checksum})
        aggregate.update(f"{checksum}  {relative}\n".encode())
    return aggregate.hexdigest(), entries


def dependency_versions():
    versions = {}
    for distribution in DEPENDENCIES:
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "not-installed"
    return versions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument(
        "--build-mode",
        choices=("development", "formal"),
        default="development",
    )
    parser.add_argument("--expected-source-sha256", default="")
    args = parser.parse_args()
    release_dir = args.release_dir.resolve()
    if not release_dir.is_dir():
        raise SystemExit(f"Release directory not found: {release_dir}")
    source_sha256, sources = source_fingerprint()
    if (
        args.expected_source_sha256
        and source_sha256 != args.expected_source_sha256.lower()
    ):
        raise SystemExit("构建期间源码发生变化，已拒绝生成构建记录。")
    # FR-20260811-D15-001: 构造失败（DLL 缺失/位数不符/导出符号缺失）时 native
    # 从未绑定，后续 native_core 段必须判空，否则 UnboundLocalError 掩盖真实根因。
    native = None
    try:
        native = NativeVisionCore(base_dir=str(ROOT))
        native_health = native.health_snapshot()
    except Exception as exc:
        native_health = {"error": str(exc)}
    dll_path = ROOT / "fukua_rpa_core.dll"
    if not dll_path.is_file():
        raise SystemExit("原生核心不存在，不能生成可验证的构建记录。")
    native_sha256 = hash_file(dll_path)
    packaged_dll = release_dir / "_internal" / "fukua_rpa_core.dll"
    if packaged_dll.is_file() and hash_file(packaged_dll) != native_sha256:
        raise SystemExit("包内原生核心与本次构建的原生核心不一致。")
    provenance = (
        require_formal_release_provenance(APP_VERSION)
        if args.build_mode == "formal"
        else repository_provenance()
    )
    python_record = {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "bits": struct.calcsize("P") * 8,
        "executable": os.path.basename(sys.executable),
    }
    dependencies = dependency_versions()
    identity = create_build_identity(
        application_version=APP_VERSION,
        build_name=BUILD_NAME,
        build_mode=args.build_mode,
        source_sha256=source_sha256,
        git_commit=provenance.get("commit", ""),
        git_dirty=provenance.get("dirty", True),
        python_version=python_record["version"],
        python_bits=python_record["bits"],
        dependencies=dependencies,
        native_core_sha256=native_sha256,
    )
    report = {
        "format": "fukuaRPA_build_record",
        "format_version": 2,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "application_version": APP_VERSION,
        "build_name": BUILD_NAME,
        "build_mode": args.build_mode,
        "private_validation_inputs": validation_receipt(formal=args.build_mode == "formal"),
        "git": provenance,
        "supported_windows": SUPPORTED_WINDOWS_TEXT,
        "target": "Windows x64 onedir portable",
        "unsigned": True,
        "network_on_startup": False,
        "python": python_record,
        "platform": platform.platform(),
        "dependencies": dependencies,
        "native_core": {
            "api_version": native.version if native and native.available else 0,
            "available": bool(native is not None and native.available),
            "capabilities": native.capabilities() if native and native.available else {},
            "abi": native_health.get("abi", {}),
            "sha256": native_sha256,
            "runtime": "MSVC C++17 /O2 /MT",
        },
        "source_sha256": source_sha256,
        "source_file_count": len(sources),
        "sources": sources,
        "build_id": identity["build_id"],
        "build_identity": identity["fields"],
    }
    output = release_dir / "BUILD_INFO.json"
    atomic_write_json(output, report)
    print(
        json.dumps(
            {
                "output": str(output),
                "build_name": BUILD_NAME,
                "source_sha256": source_sha256,
                "source_file_count": len(sources),
                "native_api": report["native_core"]["api_version"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
