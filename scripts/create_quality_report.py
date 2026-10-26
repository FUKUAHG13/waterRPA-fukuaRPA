"""Create machine-readable evidence for one release build."""

from __future__ import annotations

import argparse
import ast
import json
import platform
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fukua_rpa.constants import APP_VERSION, BUILD_NAME  # noqa: E402
from scripts.build_identity import (  # noqa: E402
    create_build_identity,
    identity_from_build_record,
)
from scripts.hashutil import atomic_write_json, hash_file  # noqa: E402
from scripts.private_release_inputs import validation_receipt  # noqa: E402
from scripts.release_provenance import (  # noqa: E402
    repository_provenance,
    require_formal_release_provenance,
)


def declared_test_inventory(root: Path = ROOT) -> tuple[int, int]:
    file_count = 0
    method_count = 0
    for path in sorted((root / "tests").glob("test_*.py")):
        file_count += 1
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                method_count += 1
    return file_count, method_count


def build_quality_report(
    release_dir: Path,
    *,
    mode: str,
    source_gate: str,
    package_gate: str,
    artifact: Path | None = None,
    build_name: str | None = None,
    expected_source_sha256: str = "",
) -> dict:
    release_dir = Path(release_dir).resolve()
    provenance = require_formal_release_provenance(APP_VERSION) if mode == "formal" else repository_provenance()
    private_inputs = validation_receipt(formal=mode == "formal")
    test_files, declared_tests = declared_test_inventory()
    baseline = ROOT / "docs" / "PERFORMANCE_BASELINE.json"
    build_info_path = release_dir / "BUILD_INFO.json"
    build_info = (
        json.loads(build_info_path.read_text(encoding="utf-8"))
        if build_info_path.is_file()
        else {}
    )
    from scripts.create_build_record import (
        dependency_versions,
        source_fingerprint,
    )

    current_source_sha256, _sources = source_fingerprint()
    if (
        expected_source_sha256
        and current_source_sha256 != expected_source_sha256.lower()
    ):
        raise ValueError("构建期间源码发生变化，不能生成质量报告")
    if build_info:
        if mode == "formal" and build_info.get("private_validation_inputs") != private_inputs:
            raise ValueError("构建记录与质量报告的私有验证输入身份不一致")
        identity = identity_from_build_record(build_info)
        fields = identity["fields"]
        if fields["application_version"] != APP_VERSION:
            raise ValueError("构建记录版本与当前源码版本不一致")
        if fields["build_mode"] != mode:
            raise ValueError("构建记录与质量报告的构建模式不一致")
        if build_name and fields["build_name"] != build_name:
            raise ValueError("构建记录与质量报告的构建名称不一致")
        if fields["source_sha256"] != current_source_sha256:
            raise ValueError("构建记录不属于当前源码快照")
        if fields["git_commit"] != str(provenance.get("commit", "")) or fields[
            "git_dirty"
        ] != bool(provenance.get("dirty", True)):
            raise ValueError("构建记录与当前 Git 状态不一致")
        effective_build_name = fields["build_name"]
        source_sha256 = fields["source_sha256"]
    else:
        native_path = ROOT / "fukua_rpa_core.dll"
        if not native_path.is_file():
            raise ValueError("原生核心不存在，不能生成可验证的质量报告")
        effective_build_name = str(
            build_name or (Path(artifact).stem if artifact is not None else BUILD_NAME)
        )
        source_sha256 = current_source_sha256
        identity = create_build_identity(
            application_version=APP_VERSION,
            build_name=effective_build_name,
            build_mode=mode,
            source_sha256=source_sha256,
            git_commit=provenance.get("commit", ""),
            git_dirty=provenance.get("dirty", True),
            python_version=platform.python_version(),
            python_bits=struct.calcsize("P") * 8,
            dependencies=dependency_versions(),
            native_core_sha256=hash_file(native_path),
        )
    artifact_record = {}
    if artifact is not None:
        artifact = Path(artifact).resolve()
        if not artifact.is_file():
            raise ValueError(f"构建产物不存在：{artifact.name}")
        artifact_record = {
            "name": artifact.name,
            "size": artifact.stat().st_size,
            "sha256": hash_file(artifact),
        }
    return {
        "format": "fukuaRPA_quality_report",
        "format_version": 1,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "application_version": APP_VERSION,
        "build_name": effective_build_name,
        "build_mode": mode,
        "private_validation_inputs": private_inputs,
        "git": provenance,
        "source_sha256": source_sha256,
        "build_id": identity["build_id"],
        "build_identity": identity["fields"],
        "build_record": (
            {"path": build_info_path.name, "sha256": hash_file(build_info_path)}
            if build_info_path.is_file()
            else {}
        ),
        "artifact": artifact_record,
        "gates": {
            "source": {
                "status": source_gate,
                "scope": "full" if mode == "formal" else "impact-selected",
            },
            "frozen_package": {"status": package_gate},
            "performance": {
                "status": source_gate,
                "scope": "full benchmarks and soak" if mode == "formal" else "impact-selected short benchmarks",
                "baseline_sha256": hash_file(baseline) if baseline.is_file() else "",
            },
        },
        "tests": {
            "declared_test_file_count": test_files,
            "declared_test_method_count": declared_tests,
            "result": source_gate,
        },
        "release_claims": {
            "dirty": bool(provenance.get("dirty", True)),
            "formal_release": mode == "formal",
            "quality_skipped": source_gate == "skipped" or package_gate == "skipped",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--mode", choices=("development", "formal"), required=True)
    parser.add_argument(
        "--source-gate", choices=("passed", "skipped"), required=True
    )
    parser.add_argument(
        "--package-gate",
        choices=("pending", "passed", "skipped"),
        required=True,
    )
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--build-name")
    parser.add_argument("--expected-source-sha256", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_quality_report(
        args.release_dir,
        mode=args.mode,
        source_gate=args.source_gate,
        package_gate=args.package_gate,
        artifact=args.artifact,
        build_name=args.build_name,
        expected_source_sha256=args.expected_source_sha256,
    )
    output = (
        args.output.resolve()
        if args.output
        else args.release_dir.resolve() / "QUALITY_REPORT.json"
    )
    atomic_write_json(output, report)
    print(json.dumps({"output": str(output), **report["gates"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
