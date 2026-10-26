"""Build complete fukuaRPA releases; onedir is the safe default."""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sys
from pathlib import Path

# Advisory file locking for the site-packages cv2.pyd mutation below. Windows
# only — on other platforms the helper degrades to "no lock" (the project is
# Windows-only, so this is moot in practice).
try:  # pragma: no cover - platform dependent
    import msvcrt  # type: ignore

    _MSVCRT_AVAILABLE = True
except ImportError:  # pragma: no cover - non-Windows dev environments
    msvcrt = None  # type: ignore[assignment]
    _MSVCRT_AVAILABLE = False

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fukua_rpa.constants import (  # noqa: E402
    APP_VERSION,
    BUILD_NAME,
    ONEFILE_BUILD_NAME,
)
from scripts.create_build_record import source_fingerprint  # noqa: E402
from scripts.hashutil import hash_file  # noqa: E402
from scripts.lib.run_helpers import run as _run_helper  # noqa: E402
from scripts.private_release_inputs import (  # noqa: E402
    assert_public_source_boundary,
    assert_validation_inputs_unchanged,
    bind_validation_inputs,
)
from scripts.release_provenance import require_formal_release_provenance  # noqa: E402


def run(command, timeout=600, env=None):
    # 统一到 scripts/lib/run_helpers.run（§3.5(c)）；保留构建侧进程树清理语义。
    _run_helper(command, timeout=timeout, env=env, tree_kill=True)


def build_pyinstaller_environment(base_environment=None):
    """Return a deterministic environment for PyInstaller dependency scans.

    SAFETY: IDEs and agent hosts often prepend their own Poppler, image codec or
    language-runtime directories to PATH.  PyInstaller treats PATH as a DLL
    search source, so an unrelated ``icuuc.dll`` can shadow the Windows ICU ABI
    required by Qt and produce a package that only fails after freezing.  The
    freezer needs the selected Python runtime and Windows system directories;
    it must not inherit arbitrary host-tool DLL directories or Python/Qt import
    injection variables.
    """

    environment = dict(
        os.environ if base_environment is None else base_environment
    )
    system_root = Path(
        environment.get("SystemRoot")
        or environment.get("SYSTEMROOT")
        or environment.get("WINDIR")
        or r"C:\Windows"
    ).resolve()
    prefixes = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()]
    candidates = [
        Path(sys.executable).resolve().parent,
        *(prefix / "Scripts" for prefix in prefixes),
        *prefixes,
        *(prefix / "DLLs" for prefix in prefixes),
        system_root / "System32",
        system_root,
        system_root / "System32" / "Wbem",
        system_root / "System32" / "WindowsPowerShell" / "v1.0",
        system_root / "System32" / "OpenSSH",
    ]
    trusted_paths = []
    seen = set()
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        normalized = os.path.normcase(str(candidate))
        if normalized in seen:
            continue
        seen.add(normalized)
        trusted_paths.append(str(candidate))

    removed_keys = {
        "path",
        "pythonhome",
        "pythonpath",
        "pythonuserbase",
        "qml2_import_path",
        "qml_import_path",
        "pyside_designer_plugins",
        "qt_plugin_path",
        "qt_qpa_platform_plugin_path",
    }
    for key in list(environment):
        if key.casefold() in removed_keys:
            environment.pop(key, None)
    environment["PATH"] = os.pathsep.join(trusted_paths)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def run_pyinstaller(spec_name: str) -> None:
    """Freeze one spec without inheriting DLL paths from the host application."""

    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            spec_name,
        ],
        timeout=1800,
        env=build_pyinstaller_environment(),
    )


def write_checksums(paths, output):
    command = [
        sys.executable,
        "scripts/create_release_checksums.py",
        *[str(path) for path in paths],
        "--output",
        str(output),
    ]
    run(command)
    run([*command, "--check"])


def copy_project_license(release_dir: Path) -> Path:
    source = ROOT / "LICENSE"
    if not source.is_file():
        raise RuntimeError(f"Project license not found: {source}")
    destination = release_dir / "LICENSE"
    shutil.copy2(source, destination)
    return destination


def build_native_core():
    run(
        [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "native_core/build_native_core.ps1",
        ]
    )


def _backup_pip_cv2(pip_pyd: Path) -> Path | None:
    """Back up the original pip ``cv2.pyd`` before it is overwritten.

    Returns the backup path on success, or ``None`` if a backup could not be
    taken (the caller then proceeds with the overwrite but logs the risk). The
    backup is named ``<cv2.pyd>.bak`` next to the original and is only created
    once, so repeated builds do not stack backups (review item #9).

    FR-20260812-D15-002: the backup must track the *current* pip build. If a
    previous backup exists but its content differs from the live ``cv2.pyd``
    (e.g. the developer upgraded OpenCV since the last build), the backup is
    refreshed; otherwise ``atexit`` restoration would silently roll the
    developer's cv2 back to the stale pre-upgrade version.
    """

    backup = pip_pyd.with_name(pip_pyd.name + ".bak")
    try:
        if backup.exists() and hash_file(pip_pyd) == hash_file(backup):
            # 已备份且与当前 pip 版本一致：不重复覆盖。
            return backup
        shutil.copy2(pip_pyd, backup)
        return backup
    except OSError as exc:
        print(f"fukuaRPA: WARNING - 无法备份 pip cv2.pyd：{exc}")
        return None


def restore_pip_opencv() -> bool:
    """Restore the original pip ``cv2.pyd`` from its ``.bak`` backup.

    Returns True when a backup was found and restored. Use this to undo the
    side effect of :func:`install_minimal_opencv` (e.g. when a build is aborted
    or the minimal build misbehaves). No-op when no backup exists.
    """

    import sysconfig

    _ext_suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".pyd"
    _cv2_pyd_name = f"cv2{_ext_suffix}"
    try:
        import importlib.util

        spec = importlib.util.find_spec("cv2")
        if spec is None or spec.origin is None:
            return False
        cv2_dir = Path(spec.origin).parent
    except Exception:
        return False
    pip_pyd = cv2_dir / _cv2_pyd_name
    if not pip_pyd.is_file():
        pip_pyd = cv2_dir / "cv2.pyd"
    backup = pip_pyd.with_name(pip_pyd.name + ".bak")
    if not backup.is_file():
        return False
    try:
        shutil.copy2(backup, pip_pyd)
        print(f"fukuaRPA: restored original pip cv2.pyd from {backup}")
        return True
    except OSError as exc:
        print(f"fukuaRPA: WARNING - 无法恢复 pip cv2.pyd：{exc}")
        return False


def _acquire_cv2_build_lock(cv2_dir: Path, timeout: float = 120.0):
    """Advisory-exclusive lock serializing the site-packages cv2.pyd mutation.

    Two concurrent builds would otherwise race on the same ``cv2.pyd`` (backup
    + replace + atexit restore). We take a non-blocking advisory lock on a lock
    file inside the cv2 directory so the second build waits for the first to
    leave its critical section instead of corrupting the shared file.

    Returns the open file descriptor on success, or ``None`` when advisory
    locking is unavailable (non-Windows) or the wait timed out. The caller must
    ``os.close`` the descriptor to release the lock.
    """
    import time

    if not _MSVCRT_AVAILABLE or msvcrt is None:
        return None
    lock_path = cv2_dir / "cv2.pyd.fukua-buildlock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                print(
                    "fukuaRPA: WARNING - 等待 cv2.pyd 构建锁超时，"
                    "可能存在另一个并发构建；跳过精简 cv2 替换以避免竞争。"
                )
                return None
            time.sleep(0.25)


def install_minimal_opencv():
    """Replace pip cv2.pyd with custom minimal build if available.

    Side effect (review item #9): this mutates the user's installed ``cv2``
    package in site-packages. The original ``cv2.pyd`` is backed up to
    ``cv2.pyd.bak`` next to it first, so :func:`restore_pip_opencv` can undo the
    change. This is an intentional ADR-0022 build-size optimization, not a bug;
    the backup exists purely to make the mutation recoverable.

    Concurrency (S10): the backup + replace is wrapped in an advisory build
    lock (see :func:`_acquire_cv2_build_lock`) so two builds running on the
    same machine cannot race on the shared ``cv2.pyd``.

    Hard-kill boundary: restoration happens via ``atexit``. If the build
    process is forcibly killed (SIGKILL / Task Manager "End process") before
    Python unwinds, ``atexit`` never runs and site-packages ``cv2.pyd`` may
    remain the minimal build. FR-20260811-D15-004（措辞更正）：重跑构建**不能**
    恢复——哈希相等分支会跳过替换且不注册 atexit 还原（``restore_pip_opencv``
    仅在构建真正执行替换后才挂载）。此时只能手动调用 :func:`restore_pip_opencv`
    恢复；do not ship or import the dev environment's ``cv2`` in that state.
    """

    import sysconfig

    _ext_suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".pyd"
    _cv2_pyd_name = f"cv2{_ext_suffix}"
    custom = ROOT / "opencv_build" / "lib" / "python3" / _cv2_pyd_name
    if not custom.is_file():
        return
    import importlib.util

    spec = importlib.util.find_spec("cv2")
    if spec is None or spec.origin is None:
        return
    cv2_dir = Path(spec.origin).parent
    pip_pyd = cv2_dir / _cv2_pyd_name
    if not pip_pyd.is_file():
        pip_pyd = cv2_dir / "cv2.pyd"
    if pip_pyd.is_file() and hash_file(pip_pyd) != hash_file(custom):
        # S10: serialize the backup + replace against concurrent builds so the
        # shared site-packages file is never written by two processes at once.
        lock_fd = _acquire_cv2_build_lock(cv2_dir)
        if lock_fd is None and _MSVCRT_AVAILABLE:
            # Timed out waiting for another build's lock; skip to avoid racing.
            return
        try:
            backup = _backup_pip_cv2(pip_pyd)
            try:
                shutil.copy2(custom, pip_pyd)
                # F4 (review item #9): 构建期间临时替换了开发机的 cv2；无论后续构建
                # 步骤成功还是异常退出，都在进程结束时还原 site-packages，避免开发
                # 环境被静默改写且无法恢复。
                import atexit

                atexit.register(restore_pip_opencv)
                size_mb = custom.stat().st_size / 1048576
                note = f" (original backed up to {backup.name})" if backup else ""
                print(
                    f"fukuaRPA: replaced pip cv2.pyd with minimal build "
                    f"({size_mb:.1f} MB){note}"
                )
            except PermissionError:
                print(
                    "fukuaRPA: WARNING - pip cv2.pyd is locked by another process, "
                    "using original (close other Python processes and retry for smaller build)"
                )
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            # FR-20260812 建议-打包-3: 释放锁后删除锁文件本身，避免每次构建
            # 在 site-packages 残留 cv2.pyd.fukua-buildlock 空文件。
            with contextlib.suppress(OSError):
                os.remove(cv2_dir / "cv2.pyd.fukua-buildlock")


def verify_source_scope(formal_release=False):
    if formal_release:
        run([sys.executable, "scripts/verify_release.py"], timeout=1800)
        return
    run([sys.executable, "scripts/run_impacted_tests.py"], timeout=3600)


def validate_build_mode(*, formal_release: bool, skip_quality: bool) -> None:
    if formal_release and skip_quality:
        raise ValueError("正式发布不能与 --skip-quality 同时使用。")


def build_onedir(
    skip_quality=False,
    formal_release=False,
    expected_source_sha256="",
):
    install_minimal_opencv()
    run_pyinstaller("fukuaRPA_onedir.spec")
    release_dir = ROOT / "dist" / BUILD_NAME
    copy_project_license(release_dir)
    closure_path = release_dir / "RUNTIME_CLOSURE.json"
    run(
        [
            sys.executable,
            "scripts/audit_runtime_closure.py",
            str(release_dir),
            "--output",
            str(closure_path),
        ]
    )
    run(
        [
            sys.executable,
            "scripts/audit_runtime_closure.py",
            str(release_dir),
            "--output",
            str(closure_path),
            "--check",
        ]
    )
    build_mode = "formal" if formal_release else "development"
    run(
        [
            sys.executable,
            "scripts/create_build_record.py",
            str(release_dir),
            "--build-mode",
            build_mode,
            "--expected-source-sha256",
            expected_source_sha256,
        ]
    )
    run(
        [
            sys.executable,
            "scripts/create_sbom.py",
            str(release_dir),
            "--app-version",
            APP_VERSION,
            "--build-name",
            BUILD_NAME,
            "--build-info",
            str(release_dir / "BUILD_INFO.json"),
        ]
    )
    run([sys.executable, "scripts/create_release_readme.py", str(release_dir)])
    source_gate = "skipped" if skip_quality else "passed"
    package_gate = "skipped" if skip_quality else "pending"
    quality_command = [
        sys.executable,
        "scripts/create_quality_report.py",
        str(release_dir),
        "--mode",
        build_mode,
        "--source-gate",
        source_gate,
        "--package-gate",
        package_gate,
        "--build-name",
        BUILD_NAME,
        "--expected-source-sha256",
        expected_source_sha256,
    ]
    run(quality_command)
    run(
        [
            sys.executable,
            "scripts/create_release_info.py",
            str(release_dir),
            "--build-name",
            BUILD_NAME,
        ]
    )
    run([sys.executable, "scripts/create_payload_hashes.py", str(release_dir)])
    run(
        [
            sys.executable,
            "scripts/create_payload_hashes.py",
            str(release_dir),
            "--check",
        ]
    )
    if not skip_quality:
        run(
            [
                sys.executable,
                "scripts/verify_release.py",
                "--skip-source",
                "--dist",
                str(release_dir),
                "--allow-pending-quality",
            ]
        )
        quality_command[quality_command.index("--package-gate") + 1] = "passed"
        run(quality_command)
        # QUALITY_REPORT and its hash are finalized only after the frozen
        # package gate has returned successfully.
        run(
            [
                sys.executable,
                "scripts/create_release_info.py",
                str(release_dir),
                "--build-name",
                BUILD_NAME,
            ]
        )
        run([sys.executable, "scripts/create_payload_hashes.py", str(release_dir)])
        run(
            [
                sys.executable,
                "scripts/create_payload_hashes.py",
                str(release_dir),
                "--check",
            ]
        )

    manifest_path = ROOT / "dist" / f"{BUILD_NAME}_manifest.json"
    manifest_command = [
        sys.executable,
        "scripts/create_release_manifest.py",
        str(release_dir),
        "--output",
        str(manifest_path),
    ]
    run(manifest_command)
    run([*manifest_command, "--check"])

    archive_path = ROOT / "dist" / f"{BUILD_NAME}_portable.zip"
    archive_command = [
        sys.executable,
        "scripts/create_release_archive.py",
        str(release_dir),
        "--manifest",
        str(manifest_path),
        "--output",
        str(archive_path),
    ]
    run(archive_command)
    run([*archive_command, "--check"])

    checksums_path = ROOT / "dist" / f"{BUILD_NAME}_SHA256SUMS.txt"
    write_checksums([archive_path, manifest_path], checksums_path)
    print(f"Onedir release ready: {release_dir}")
    return [archive_path, manifest_path]


def build_onefile(
    skip_quality=False,
    formal_release=False,
    expected_source_sha256="",
):
    install_minimal_opencv()
    run_pyinstaller("fukuaRPA_onefile.spec")
    executable = ROOT / "dist" / f"{ONEFILE_BUILD_NAME}.exe"
    if not executable.is_file():
        raise RuntimeError(f"Onefile build was not created: {executable}")
    if not skip_quality:
        run(
            [
                sys.executable,
                "scripts/verify_release.py",
                "--skip-source",
                "--onefile",
                str(executable),
            ]
        )
    quality_report = ROOT / "dist" / f"{ONEFILE_BUILD_NAME}_QUALITY_REPORT.json"
    run(
        [
            sys.executable,
            "scripts/create_quality_report.py",
            str(ROOT / "dist"),
            "--mode",
            "formal" if formal_release else "development",
            "--source-gate",
            "skipped" if skip_quality else "passed",
            "--package-gate",
            "skipped" if skip_quality else "passed",
            "--artifact",
            str(executable),
            "--build-name",
            ONEFILE_BUILD_NAME,
            "--expected-source-sha256",
            expected_source_sha256,
            "--output",
            str(quality_report),
        ]
    )
    checksums_path = ROOT / "dist" / f"{ONEFILE_BUILD_NAME}_SHA256SUMS.txt"
    write_checksums([executable, quality_report], checksums_path)
    print(f"Onefile release ready: {executable}")
    return [executable, quality_report]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--format",
        choices=("all", "onedir", "onefile"),
        default="onedir",
        help="Build complete onedir by default; onefile requires an explicit format.",
    )
    parser.add_argument(
        "--skip-quality",
        action="store_true",
        help=(
            "UNSAFE: skip source checks and frozen-package smokes only for a "
            "temporary local packaging experiment."
        ),
    )
    parser.add_argument(
        "--formal-release",
        action="store_true",
        help=(
            "Run the complete release gate and force a native rebuild. Use only "
            "when the user explicitly approves a formal/stable release."
        ),
    )
    args = parser.parse_args()

    if args.formal_release and args.format in ("onefile", "all"):
        print("错误：--formal-release 不支持 onefile 格式（ADR 0016：onefile 使用替代验证路径）")
        return 1

    try:
        validate_build_mode(
            formal_release=args.formal_release,
            skip_quality=args.skip_quality,
        )
        if args.formal_release:
            require_formal_release_provenance(APP_VERSION)
            assert_public_source_boundary()
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))

    private_inputs = bind_validation_inputs() if args.formal_release else None

    # Normal builds skip git-cleanliness check (only --formal-release enforces it).
    # This is intentional: local test builds should not require a clean tree.
    # A stale DLL can be ABI-compatible while containing older matching logic.
    # Rebuilding for every package is cheap compared with shipping mixed evidence.
    build_native_core()
    if not args.skip_quality:
        verify_source_scope(formal_release=args.formal_release)
    if args.formal_release:
        # Native rebuilding and quality tooling must not have changed tracked
        # source or binaries after the initial provenance check.
        require_formal_release_provenance(APP_VERSION)
    expected_source_sha256, _sources = source_fingerprint()

    artifacts = []
    if args.format in ("all", "onedir"):
        artifacts.extend(
            build_onedir(
                skip_quality=args.skip_quality,
                formal_release=args.formal_release,
                expected_source_sha256=expected_source_sha256,
            )
        )
    if args.format in ("all", "onefile"):
        artifacts.extend(
            build_onefile(
                skip_quality=args.skip_quality,
                formal_release=args.formal_release,
                expected_source_sha256=expected_source_sha256,
            )
        )
    if args.format == "all":
        combined = ROOT / "dist" / f"{BUILD_NAME}_all_SHA256SUMS.txt"
        write_checksums(artifacts, combined)
        print(f"Combined checksums: {combined}")
    if private_inputs is not None:
        assert_validation_inputs_unchanged(private_inputs)


if __name__ == "__main__":
    sys.exit(main() or 0)
