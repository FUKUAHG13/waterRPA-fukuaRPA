"""Write hashes and real Authenticode status for an onedir release."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fukua_rpa.constants import PROJECT_RELEASES_URL  # noqa: E402
from scripts.build_identity import require_matching_evidence  # noqa: E402
from scripts.hashutil import atomic_write_json, hash_file  # noqa: E402
from scripts.runtime_pruning import find_uiautomation_x64_dll  # noqa: E402

_AUTHENTICODE_MESSAGES = {
    "Valid": "Authenticode signature is valid.",
    "NotSigned": "No Authenticode signature is present.",
    "HashMismatch": "Authenticode signature does not match the current file.",
    "NotTrusted": "Authenticode signature is not trusted on this machine.",
    "NotSupportedFileFormat": "This file format does not support Authenticode.",
    "Incompatible": "Authenticode signature is incompatible with this file.",
    "UnknownError": "Authenticode status could not be determined.",
}


def _safe_authenticode_message(status: str) -> str:
    """Return stable release metadata without PowerShell's local file path."""
    return _AUTHENTICODE_MESSAGES.get(
        status,
        "Authenticode status was reported by Windows.",
    )


def authenticode_status(executable: Path) -> dict:
    script = (
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
        "$p=[Environment]::GetEnvironmentVariable('FUKUA_RELEASE_EXE');"
        "$s=Get-AuthenticodeSignature -LiteralPath $p;"
        "[pscustomobject]@{status=[string]$s.Status;"
        "message=[string]$s.StatusMessage;"
        "subject=[string]$s.SignerCertificate.Subject;"
        "thumbprint=[string]$s.SignerCertificate.Thumbprint}|ConvertTo-Json -Compress"
    )
    # FR-20260811-D15-1: 用绝对路径调用 powershell，避免发布机 PATH 被劫持时
    # 执行到被替换的伪 powershell.exe（签名查询属于发布元数据，不应依赖 PATH）。
    # INVARIANT: 变量名写成规范大写 `SYSTEMROOT`。Windows 的 `os.environ` 键不区分
    # 大小写（`nt.environ` 统一转大写），因此与历史写法 `SystemRoot` 的查表结果
    # 逐字节相同；本脚本只在 Windows 上运行（需要 System32\WindowsPowerShell）。
    powershell = os.path.join(
        os.environ.get("SYSTEMROOT", r"C:\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    if not os.path.isfile(powershell):
        # 极少数系统 PowerShell 路径缺失时的显式回退；正常发行路径应命中上方绝对路径。
        powershell = "powershell"
    try:
        environment = os.environ.copy()
        environment["FUKUA_RELEASE_EXE"] = str(executable)
        completed = subprocess.run(
            [powershell, "-NoProfile", "-Command", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=environment,
        )
        result = json.loads(completed.stdout.strip())
        status = str(result.get("status") or "Unknown")
        return {
            "status": status,
            # SAFETY: StatusMessage embeds the absolute build path for unsigned
            # files. Release metadata records only a stable, path-free summary.
            "message": _safe_authenticode_message(status),
            "signer_subject": str(result.get("subject") or ""),
            "signer_thumbprint": str(result.get("thumbprint") or ""),
        }
    except (
        # FR-20260811-D15-003: TimeoutExpired 继承 SubprocessError，不是
        # CalledProcessError/OSError 子类；不捕获会被穿透中断整条发布链。
        # 签名状态属元数据，超时/失败一律降级为 Unavailable，不阻断发布。
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return {
            "status": "Unavailable",
            # Exceptions may contain the executable's absolute path as well.
            "message": "Authenticode status could not be queried.",
            "signer_subject": "",
            "signer_thumbprint": "",
        }


def build_release_info(release_dir: Path, build_name: str) -> dict:
    executable = release_dir / f"{build_name}.exe"
    build_info = release_dir / "BUILD_INFO.json"
    sbom = release_dir / "SBOM.cdx.json"
    runtime_closure = release_dir / "RUNTIME_CLOSURE.json"
    quality_report = release_dir / "QUALITY_REPORT.json"
    for path in (executable, build_info, sbom, runtime_closure, quality_report):
        if not path.is_file():
            raise RuntimeError(f"Required release file not found: {path}")
    build_data = json.loads(build_info.read_text(encoding="utf-8"))
    quality_data = json.loads(quality_report.read_text(encoding="utf-8"))
    try:
        identity = require_matching_evidence(build_data, quality_data)
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    if identity["fields"]["build_name"] != build_name:
        raise RuntimeError("构建记录与发行名称不一致")
    quality_build_record = quality_data.get("build_record", {})
    if quality_build_record.get("sha256") != hash_file(build_info):
        raise RuntimeError("质量报告引用的构建记录已发生变化")
    sbom_data = json.loads(sbom.read_text(encoding="utf-8"))
    sbom_metadata = sbom_data.get("metadata", {})
    sbom_component = sbom_metadata.get("component", {})
    sbom_properties = {
        str(item.get("name")): str(item.get("value"))
        for item in sbom_metadata.get("properties", [])
        if isinstance(item, dict)
    }
    if sbom_component.get("version") != str(
        build_data.get("application_version", "")
    ).lstrip("v"):
        raise RuntimeError("SBOM 与构建记录的版本不一致")
    if sbom_properties.get("fukuaRPA.build_name") != build_name:
        raise RuntimeError("SBOM 与构建记录的构建名称不一致")
    if sbom_properties.get("fukuaRPA.build_id") != identity["build_id"]:
        raise RuntimeError("SBOM 与构建记录的 build_id 不一致")
    if sbom_properties.get("fukuaRPA.source_sha256") != identity["fields"][
        "source_sha256"
    ]:
        raise RuntimeError("SBOM 与构建记录的源码指纹不一致")
    # The uiautomation X64 helper DLL ships under <release>/_internal/
    # uiautomation/bin/. Its exact filename embeds a VC runtime label
    # (e.g. "VC140") that can change between releases, so discover it
    # dynamically rather than hardcoding the full name.
    uia_helper_path = find_uiautomation_x64_dll(
        release_dir / "_internal" / "uiautomation"
    )
    if uia_helper_path is None:
        raise RuntimeError(
            "Required code payload not found: uiautomation X64 helper DLL under "
            f"{release_dir / '_internal' / 'uiautomation'}"
        )
    code_paths = (
        ("main_executable", executable),
        ("native_core", release_dir / "_internal" / "fukua_rpa_core.dll"),
        ("uia_bitmap_helper", uia_helper_path),
    )
    code_payloads = []
    for role, path in code_paths:
        if not path.is_file():
            raise RuntimeError(f"Required code payload not found: {path}")
        code_payloads.append(
            {
                "role": role,
                "path": path.relative_to(release_dir).as_posix(),
                "size": path.stat().st_size,
                "sha256": hash_file(path),
                "authenticode": authenticode_status(path),
            }
        )
    executable_record = code_payloads[0]
    packaged_native_sha256 = next(
        item["sha256"] for item in code_payloads if item["role"] == "native_core"
    )
    if packaged_native_sha256 != build_data.get("native_core", {}).get("sha256"):
        raise RuntimeError("包内原生核心与构建记录不一致")
    return {
        "format": "fukuaRPA_release_info",
        "format_version": 1,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "application_version": build_data.get("application_version", ""),
        "build_name": build_name,
        "build_mode": build_data.get("build_mode", ""),
        "source_sha256": build_data.get("source_sha256", ""),
        "build_id": identity["build_id"],
        "build_identity": identity["fields"],
        "private_validation_inputs": build_data.get("private_validation_inputs"),
        "target": build_data.get("target", "Windows x64 onedir portable"),
        "supported_windows": build_data.get("supported_windows", ""),
        "network_on_startup": False,
        "automatic_update_check": False,
        "update_channel": "manual GitHub releases",
        "update_url": PROJECT_RELEASES_URL,
        "executable": {
            "path": executable.name,
            "size": executable.stat().st_size,
            "sha256": executable_record["sha256"],
            "authenticode": executable_record["authenticode"],
        },
        "code_payloads": code_payloads,
        "build_record": {
            "path": build_info.name,
            "sha256": hash_file(build_info),
            "source_sha256": build_data.get("source_sha256", ""),
        },
        "sbom": {"path": sbom.name, "sha256": hash_file(sbom)},
        "runtime_closure": {
            "path": runtime_closure.name,
            "sha256": hash_file(runtime_closure),
        },
        "quality_report": {
            "path": quality_report.name,
            "sha256": hash_file(quality_report),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--build-name", required=True)
    args = parser.parse_args()
    release_dir = args.release_dir.resolve()
    report = build_release_info(release_dir, args.build_name)
    output = release_dir / "RELEASE_INFO.json"
    atomic_write_json(output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
