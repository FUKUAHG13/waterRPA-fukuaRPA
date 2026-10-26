import ast
import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

from fukua_rpa.constants import (
    APP_VERSION,
    BUILD_NAME,
    ONEFILE_BUILD_NAME,
    PROJECT_RELEASES_URL,
)
from scripts import create_build_record
from scripts.audit_runtime_closure import build_runtime_closure
from scripts.build_identity import (
    create_build_identity,
    identity_from_build_record,
    require_matching_evidence,
)
from scripts.build_release import (
    build_pyinstaller_environment,
    copy_project_license,
    run_pyinstaller,
    validate_build_mode,
    verify_source_scope,
)
from scripts.create_build_record import dependency_versions, source_fingerprint
from scripts.hashutil import hash_file as hashutil_hash_file

try:
    # FR-SCOPE010-001: 修复后新增的 LF 规范化源码哈希。守卫导入是为了让
    # 「函数缺失」在修复前表现为干净的断言失败，而不是整个测试模块的收集错误。
    from scripts.hashutil import hash_source_text
except ImportError:  # pragma: no cover - 仅修复前走到
    hash_source_text = None
from scripts.create_quality_report import build_quality_report
from scripts.create_release_archive import (
    FIXED_ZIP_TIMESTAMP,
    build_archive,
    verify_archive,
)
from scripts.create_release_checksums import build_checksums, verify_checksums
from scripts.create_release_info import (
    authenticode_status,
    build_release_info,
    hash_file,
)
from scripts.create_release_manifest import build_manifest, verify_manifest
from scripts.create_release_readme import release_readme_text
from scripts.create_sbom import build_sbom
from scripts.release_provenance import (
    repository_provenance,
    require_formal_release_provenance,
    version_tag_matches,
)
from scripts.runtime_pruning import discover_uiautomation_x64_dll_basenames
from scripts.smoke_startup import build_startup_smoke_environment
from scripts.verify_release import verify_dist, verify_plugin_tests

ROOT = Path(__file__).resolve().parents[1]

# ADV-20260920-001：本机状态探针只允许写入**测试自有的唯一隔离目录**，不得再直接
# 写到仓库 `plugins/` 根下（此前 `.fingerprint_probe_state.json` 位于仓库内且未被
# `.gitignore` 覆盖，残留可能被误提交）。目录名点开头，因此不会进入源码指纹，也不会
# 被 git 跟踪。
_PROBE_DIR_PREFIX = ".probe-"


def _make_probe_dir(base: Path) -> Path:
    """Create a unique, test-owned isolated directory under *base*.

    Raises:
        OSError: If the directory cannot be created. Callers must degrade to an
            explicit skip that records the reason — never a silent pass.
    """

    path = Path(base) / f"{_PROBE_DIR_PREFIX}{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path


def _probe_dirs(base: Path) -> set[str]:
    """Names of probe directories currently present under *base*."""

    return {
        path.name
        for path in Path(base).iterdir()
        if path.name.startswith(_PROBE_DIR_PREFIX)
    }


def _remove_probe_dir(path: Path, base: Path) -> bool:
    """Remove *path* only when it is provably a probe directory owned by *base*.

    SAFETY: 清理只允许作用于"父目录恰为 *base*、名字以 ``.probe-`` 开头且带唯一后缀"
    的目录，避免误删未知用户数据（例如 ``plugins/tools`` 共享工具链目录）。

    Returns:
        True if the path was removed (or did not exist); False if it was refused
        because it is not provably owned by *base*.
    """

    path = Path(path)
    base = Path(base)
    owned = (
        path.parent == base
        and path.name.startswith(_PROBE_DIR_PREFIX)
        and len(path.name) > len(_PROBE_DIR_PREFIX)
    )
    if not owned:
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


def make_build_record(*, native_sha256: str, source_sha256: str | None = None) -> dict:
    if source_sha256 is None:
        source_sha256, _entries = source_fingerprint()
    provenance = repository_provenance()
    dependencies = dependency_versions()
    python_record = {
        "version": platform.python_version(),
        "bits": struct.calcsize("P") * 8,
    }
    identity = create_build_identity(
        application_version=APP_VERSION,
        build_name=BUILD_NAME,
        build_mode="development",
        source_sha256=source_sha256,
        git_commit=provenance.get("commit", ""),
        git_dirty=provenance.get("dirty", True),
        python_version=python_record["version"],
        python_bits=python_record["bits"],
        dependencies=dependencies,
        native_core_sha256=native_sha256,
    )
    return {
        "format": "fukuaRPA_build_record",
        "format_version": 2,
        "application_version": APP_VERSION,
        "build_name": BUILD_NAME,
        "build_mode": "development",
        "git": provenance,
        "python": python_record,
        "dependencies": dependencies,
        "native_core": {"sha256": native_sha256},
        "source_sha256": source_sha256,
        "build_id": identity["build_id"],
        "build_identity": identity["fields"],
    }


def matching_quality_record(build_record: dict, build_info_sha256: str) -> dict:
    return {
        "format": "fukuaRPA_quality_report",
        "application_version": build_record["application_version"],
        "build_name": build_record["build_name"],
        "build_mode": build_record["build_mode"],
        "source_sha256": build_record["source_sha256"],
        "build_id": build_record["build_id"],
        "build_identity": build_record["build_identity"],
        "build_record": {
            "path": "BUILD_INFO.json",
            "sha256": build_info_sha256,
        },
    }


class ReleaseToolingTests(unittest.TestCase):
    def test_private_ci_uses_impacted_tests_and_manual_test_build(self):
        workflow = (ROOT / ".github" / "workflows" / "windows-ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("- fukuaRPA-testing", workflow)
        self.assertIn("python scripts/run_impacted_tests.py --tests-only", workflow)
        self.assertIn("if: github.event_name == 'workflow_dispatch'", workflow)
        self.assertNotIn("run: python -m pytest -q", workflow)

    def test_formal_release_runs_every_plugin_test_in_strict_mode(self):
        plugin_dirs = (Path("plugins/one/tests"), Path("plugins/two/tests"))
        with mock.patch(
            "scripts.verify_release.discover_plugin_test_dirs",
            return_value=plugin_dirs,
        ), mock.patch("scripts.verify_release.run_plugin_tests") as run_plugins:
            verify_plugin_tests({"QT_QPA_PLATFORM": "offscreen"})

        run_plugins.assert_called_once_with(
            plugin_dirs,
            {"QT_QPA_PLATFORM": "offscreen"},
            strict=True,
        )

    def test_formal_release_rejects_a_missing_plugin_test_tree(self):
        with mock.patch(
            "scripts.verify_release.discover_plugin_test_dirs", return_value=()
        ), self.assertRaisesRegex(RuntimeError, "插件测试目录"):
            verify_plugin_tests({})

    def test_source_fingerprint_is_deterministic_and_covers_runtime_core(self):
        first_hash, first_entries = source_fingerprint()
        second_hash, second_entries = source_fingerprint()
        paths = {entry["path"] for entry in first_entries}
        self.assertEqual(first_hash, second_hash)
        self.assertEqual(first_entries, second_entries)
        self.assertEqual(len(first_hash), 64)
        self.assertIn("fukua_rpa/engine/core.py", paths)
        self.assertIn("native_core/fukua_rpa_core.cpp", paths)
        self.assertIn("fukuaRPA_onedir.spec", paths)
        self.assertIn("fukuaRPA_onefile.spec", paths)
        self.assertIn("assets/version_info_onefile.txt", paths)
        self.assertIn("tests/test_release_tooling.py", paths)
        # 维护者不公开docs；其完整性由独立的私有验证输入指纹保护。
        self.assertFalse(any(path.startswith("docs/") for path in paths))
        self.assertNotIn("scripts/run_startup_baseline.py", paths)
        self.assertNotIn("tests/test_crash_guard.py", paths)
        self.assertNotIn("tests/test_pyright_plugin_baseline.py", paths)
        self.assertIn("scripts/private_release_inputs.py", paths)
        self.assertIn("AGENTS.md", paths)
        self.assertIn("LICENSE", paths)
        self.assertIn("assets/fukuaRPA.svg", paths)
        self.assertIn("assets/search.svg", paths)
        # FR-20261003-606: manifest 由 spec 嵌入 EXE，必须纳入源码指纹，
        # 否则修改它不改变 source_sha256/build_id，构建期中止护栏也失效。
        self.assertIn("assets/fukuaRPA.manifest", paths)
        self.assertFalse(any(path.startswith(".scratch/") for path in paths))
        self.assertFalse(any(path.startswith("docs/adr/") for path in paths))

    def test_nightly_artifact_check_does_not_use_always_false_pattern(self):
        """FR-20261003-607: nightly 产物核验不得使用恒假通配符。

        产物名为 ``fukuaRPA_<version>.exe``（BUILD_NAME），原来的
        ``dist/fukuaRPA_*/fukuaRPA.exe`` 永不匹配，使该 step 恒定失败。
        """
        workflow = (ROOT / ".github" / "workflows" / "windows-ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('dist/fukuaRPA_*/fukuaRPA.exe"', workflow)
        self.assertIn("fukuaRPA_*.exe", workflow)

    def test_source_fingerprint_excludes_local_state_files(self):
        # FR-005：plugins/.disabled_plugins.json 由运行时生成、被 .gitignore 排除，
        # 纳入指纹会让同一提交在不同机器上得到不同 build_id，并可能在构建窗口内
        # 误报"构建期间源码发生变化"而中止构建（ARCHITECTURE #66/#67）。
        paths = {entry["path"] for entry in source_fingerprint()[1]}
        self.assertNotIn("plugins/.disabled_plugins.json", paths)
        self.assertFalse(
            any(part.startswith(".") for path in paths for part in path.split("/"))
        )

    def test_new_local_state_file_does_not_change_the_fingerprint(self):
        # 直接验证：新增一个本机状态文件（点开头）不改变指纹。
        # ADV-20260920-001：探针只写入测试自有的唯一隔离目录，清理只作用于该精确目录；
        # 无法安全创建/写入时显式降级为 skip 并记录原因（不得静默通过）。
        base = ROOT / "plugins"
        before_hash, _before = source_fingerprint()
        try:
            probe_dir = _make_probe_dir(base)
        except OSError as error:
            self.skipTest(
                f"无法创建测试自有的隔离目录（显式降级，不静默通过）：{error}"
            )
        try:
            (probe_dir / "state.json").write_text(
                '{"disabled": [], "reviewed": []}', encoding="utf-8"
            )
            after_hash, after_entries = source_fingerprint()
        except OSError as error:
            self.skipTest(
                f"无法在测试自有的隔离目录中安全写入探针（显式降级，不静默通过）：{error}"
            )
        finally:
            self.assertTrue(
                _remove_probe_dir(probe_dir, base),
                "清理必须作用于本测试创建的精确隔离目录",
            )
        self.assertEqual(before_hash, after_hash)
        self.assertFalse(
            any(entry["path"].startswith("plugins/.") for entry in after_entries),
            "点开头的本机状态文件不得进入源码指纹",
        )
        self.assertFalse(probe_dir.exists(), "隔离目录必须被清理，不得留下残留")

    def test_probe_directories_are_not_committable(self):
        # ADV-20260920-001：探针残留不得成为可提交项（`.gitignore` 必须覆盖
        # plugins/.probe-*/，否则被中断的测试运行留下的目录会被误提交）。
        result = subprocess.run(
            ["git", "check-ignore", "-q", f"plugins/{_PROBE_DIR_PREFIX}probe/state.json"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode == 128:
            self.skipTest("当前目录不是 git 工作树，无法校验忽略规则")
        self.assertEqual(
            result.returncode, 0, "plugins/.probe-*/ 必须被 .gitignore 覆盖"
        )

    def test_probe_directories_are_unique_and_isolated(self):
        base = ROOT / "plugins"
        first = _make_probe_dir(base)
        second = _make_probe_dir(base)
        try:
            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, base)
            self.assertEqual(second.parent, base)
            self.assertTrue(first.name.startswith(_PROBE_DIR_PREFIX))
            self.assertTrue(second.name.startswith(_PROBE_DIR_PREFIX))
        finally:
            self.assertTrue(_remove_probe_dir(first, base))
            self.assertTrue(_remove_probe_dir(second, base))
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_probe_cleanup_refuses_paths_it_does_not_own(self):
        # SAFETY：清理不得作用于未知用户数据。
        base = ROOT / "plugins"
        foreign = base / "tools"
        self.assertFalse(_remove_probe_dir(foreign, base))
        self.assertTrue(foreign.is_dir(), "非本测试创建的目录绝不能被删除")
        with tempfile.TemporaryDirectory() as temp_dir:
            # 名字带前缀但父目录不是 base ⇒ 拒绝。
            decoy = Path(temp_dir) / f"{_PROBE_DIR_PREFIX}decoy"
            decoy.mkdir()
            self.assertFalse(_remove_probe_dir(decoy, base))
            self.assertTrue(decoy.is_dir())
            # 父目录正确但缺少前缀 ⇒ 拒绝。
            plain = Path(temp_dir) / "not_a_probe"
            plain.mkdir()
            self.assertFalse(_remove_probe_dir(plain, Path(temp_dir)))
            self.assertTrue(plain.is_dir())

    def test_probe_dir_creation_failure_is_not_silently_ignored(self):
        # 构造"隔离目录不可写"场景：创建必须显式失败，由调用方降级/跳过并记录原因，
        # 不得静默返回一个不可用路径而让断言变成空转。
        base = ROOT / "plugins"
        with mock.patch.object(
            Path, "mkdir", side_effect=PermissionError("模拟不可写")
        ), self.assertRaises(OSError):
            _make_probe_dir(base)

    def test_probe_cycle_leaves_no_residue_in_plugins(self):
        # 判据：本测试自有的探针生命周期结束后，plugins/ 不得比开始前多出任何
        # .probe-* 条目。
        # DESIGN: 这里只衡量**本测试自身的增量**，因此不受其他并发进程遗留的同名目录
        # 影响——那些残留不是本测试产生的，也不属于本测试应当清理的用户数据；若改用
        # "plugins/ 下必须为空"的全局断言，任何并发或被中断的外部测试运行都会让本用例
        # 误报失败（实测：外部运行被终止时 finally 不会执行，会留下同名目录）。
        base = ROOT / "plugins"
        before = _probe_dirs(base)
        try:
            probe_dir = _make_probe_dir(base)
        except OSError as error:
            self.skipTest(
                f"无法创建测试自有的隔离目录（显式降级，不静默通过）：{error}"
            )
        try:
            (probe_dir / "state.json").write_text(
                '{"disabled": [], "reviewed": []}', encoding="utf-8"
            )
        finally:
            self.assertTrue(
                _remove_probe_dir(probe_dir, base),
                "清理必须作用于本测试创建的精确隔离目录",
            )
        self.assertFalse(probe_dir.exists(), "本测试的隔离目录必须被删除")
        self.assertEqual(
            _probe_dirs(base) - before,
            set(),
            "本测试的探针必须被清理，不得在 plugins/ 下新增残留",
        )

    def test_build_identity_rejects_mixed_or_tampered_evidence(self):
        record = make_build_record(native_sha256="1" * 64)
        identity = identity_from_build_record(record)
        evidence = matching_quality_record(record, "2" * 64)
        self.assertEqual(
            require_matching_evidence(record, evidence)["build_id"],
            identity["build_id"],
        )

        mixed = dict(evidence, build_id="3" * 64)
        with self.assertRaisesRegex(ValueError, "不同构建"):
            require_matching_evidence(record, mixed)

        tampered = json.loads(json.dumps(record))
        tampered["native_core"]["sha256"] = "4" * 64
        with self.assertRaisesRegex(ValueError, "build_id"):
            identity_from_build_record(tampered)

    def test_quality_report_rejects_build_record_from_other_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            release_dir = Path(temp_dir)
            record = make_build_record(
                native_sha256=hash_file(ROOT / "fukua_rpa_core.dll"),
                source_sha256="5" * 64,
            )
            (release_dir / "BUILD_INFO.json").write_text(
                json.dumps(record), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "当前源码"):
                build_quality_report(
                    release_dir,
                    mode="development",
                    source_gate="passed",
                    package_gate="passed",
                )

    def test_version_identity_is_consistent_in_spec_and_windows_resource(self):
        spec = (ROOT / "fukuaRPA_onedir.spec").read_text(encoding="utf-8")
        onefile_spec = (ROOT / "fukuaRPA_onefile.spec").read_text(
            encoding="utf-8"
        )
        version_info = (ROOT / "assets" / "version_info.txt").read_text(
            encoding="utf-8"
        )
        onefile_version_info = (
            ROOT / "assets" / "version_info_onefile.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("from fukua_rpa.constants import BUILD_NAME", spec)
        self.assertIn('collect_dynamic_libs("uiautomation")', spec)
        # The uiautomation X64 helper DLL must still be collected, but via the
        # dynamic discovery helper rather than a hardcoded VC runtime label.
        self.assertIn("discover_uiautomation_x64_dll_basenames", spec)
        self.assertIn('"pyautogui"', spec)
        self.assertIn("apply_runtime_pruning(a)", spec)
        self.assertIn("name=BUILD_NAME", spec)
        self.assertIn(
            "from fukua_rpa.constants import ONEFILE_BUILD_NAME", onefile_spec
        )
        self.assertIn("a.binaries", onefile_spec)
        self.assertIn("a.datas", onefile_spec)
        self.assertIn("name=ONEFILE_BUILD_NAME", onefile_spec)
        self.assertIn('"pyautogui"', onefile_spec)
        self.assertIn("apply_runtime_pruning(a)", onefile_spec)
        self.assertIn('(\"LICENSE\", \".\")', onefile_spec)
        self.assertNotIn("COLLECT(", onefile_spec)
        # Confirm the dynamic discovery actually resolves the installed X64 DLL.
        uia_dll_basenames = discover_uiautomation_x64_dll_basenames()
        self.assertTrue(
            any(name.lower().endswith("x64.dll") for name in uia_dll_basenames),
            "uiautomation X64 helper DLL not discovered from installed package",
        )
        self.assertIn("discover_uiautomation_x64_dll_basenames", onefile_spec)
        self.assertIn(f"{APP_VERSION.removeprefix('v')}.0", version_info)
        version_tuple = ", ".join(APP_VERSION.removeprefix("v").split("."))
        self.assertIn(f"filevers=({version_tuple}, 0)", version_info)
        self.assertIn(f"prodvers=({version_tuple}, 0)", version_info)
        self.assertIn(f"filevers=({version_tuple}, 0)", onefile_version_info)
        self.assertIn(f"prodvers=({version_tuple}, 0)", onefile_version_info)
        self.assertIn(f"{BUILD_NAME}.exe", version_info)
        self.assertIn(f"{ONEFILE_BUILD_NAME}.exe", onefile_version_info)
        self.assertRegex(APP_VERSION, r"^v\d+\.\d+\.\d+$")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(PROJECT_RELEASES_URL, readme)
        self.assertNotIn("waterRPA-FUKUA", readme)

    def test_release_builder_defaults_to_complete_onedir(self):
        script = (ROOT / "scripts" / "build_release.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('choices=("all", "onedir", "onefile")', script)
        self.assertIn('default="onedir"', script)
        self.assertIn("build_onedir", script)
        self.assertIn("build_onefile", script)
        self.assertNotIn("lite.spec", script)
        self.assertIn('"--formal-release"', script)
        self.assertIn("run_impacted_tests.py", script)
        self.assertIn("scripts/verify_release.py", script)
        self.assertIn("create_quality_report.py", script)
        self.assertIn("require_formal_release_provenance", script)
        self.assertIn("build_native_core()", script)
        self.assertNotIn("native_sources_changed(changed_paths)", script)
        self.assertIn("ONEFILE_BUILD_NAME}_QUALITY_REPORT.json", script)
        self.assertIn("[executable, quality_report]", script)

    def test_formal_release_cannot_skip_quality(self):
        with self.assertRaisesRegex(ValueError, "不能"):
            validate_build_mode(formal_release=True, skip_quality=True)
        validate_build_mode(formal_release=True, skip_quality=False)
        validate_build_mode(formal_release=False, skip_quality=True)

    def test_formal_release_requires_clean_version_tagged_commit(self):
        valid = {
            "available": True,
            "commit": "a" * 40,
            "dirty": False,
            "exact_tags": [f"fukuaRPA-{APP_VERSION}"],
        }
        with mock.patch(
            "scripts.release_provenance.repository_provenance",
            return_value=valid,
        ):
            report = require_formal_release_provenance(APP_VERSION)
        self.assertEqual(report["version_tags"], [f"fukuaRPA-{APP_VERSION}"])
        self.assertTrue(version_tag_matches(APP_VERSION, APP_VERSION))

        dirty = dict(valid, dirty=True)
        with mock.patch(
            "scripts.release_provenance.repository_provenance",
            return_value=dirty,
        ), self.assertRaisesRegex(RuntimeError, "干净"):
            require_formal_release_provenance(APP_VERSION)

    def test_release_builder_separates_test_package_and_formal_source_gates(self):
        with mock.patch("scripts.build_release.run") as run:
            verify_source_scope(formal_release=False)
            run.assert_called_once_with(
                [sys.executable, "scripts/run_impacted_tests.py"], timeout=3600
            )
        with mock.patch("scripts.build_release.run") as run:
            verify_source_scope(formal_release=True)
            run.assert_called_once_with(
                [sys.executable, "scripts/verify_release.py"], timeout=1800
            )

    def test_startup_smoke_does_not_spawn_a_detached_crash_watchdog(self):
        environment = build_startup_smoke_environment(
            {"PRESERVED": "yes", "FUKUA_CRASH_WATCHDOG": "0"}
        )
        self.assertEqual(environment["PRESERVED"], "yes")
        self.assertEqual(environment["FUKUA_CRASH_WATCHDOG"], "1")

    def test_pyinstaller_environment_rejects_host_runtime_path_injection(self):
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        environment = build_pyinstaller_environment(
            {
                "PATH": os.pathsep.join(
                    [r"C:\host-agent\poppler\bin", r"C:\host-agent\libheif\bin"]
                ),
                "PRESERVED": "yes",
                "PYTHONHOME": r"C:\host-agent\python",
                "PYTHONPATH": r"C:\host-agent\packages",
                "QT_PLUGIN_PATH": r"C:\host-agent\qt-plugins",
                "QML2_IMPORT_PATH": r"C:\host-agent\qml",
                "SystemRoot": system_root,
            }
        )
        path_entries = {
            os.path.normcase(path)
            for path in environment["PATH"].split(os.pathsep)
            if path
        }
        self.assertIn(
            os.path.normcase(str(Path(sys.executable).resolve().parent)),
            path_entries,
        )
        self.assertIn(
            os.path.normcase(str(Path(system_root).resolve() / "System32")),
            path_entries,
        )
        self.assertFalse(any("host-agent" in path for path in path_entries))
        self.assertEqual(environment["PRESERVED"], "yes")
        self.assertEqual(environment["PYTHONNOUSERSITE"], "1")
        for key in (
            "PYTHONHOME",
            "PYTHONPATH",
            "QT_PLUGIN_PATH",
            "QML2_IMPORT_PATH",
        ):
            self.assertNotIn(key, environment)

    def test_pyinstaller_command_uses_controlled_environment(self):
        sentinel = {"PATH": "trusted"}
        with mock.patch(
            "scripts.build_release.build_pyinstaller_environment",
            return_value=sentinel,
        ), mock.patch("scripts.build_release.run") as run:
            run_pyinstaller("fukuaRPA_onedir.spec")
        run.assert_called_once_with(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--clean",
                "fukuaRPA_onedir.spec",
            ],
            timeout=1800,
            env=sentinel,
        )

    def test_project_license_preserves_upstream_notice_and_is_copyable(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", license_text)
        self.assertIn("Copyright (c) 2026 unfiled0", license_text)
        self.assertIn("Copyright (c) 2026 FUKUAHG13", license_text)
        with tempfile.TemporaryDirectory() as temp_dir:
            release_dir = Path(temp_dir)
            copied = copy_project_license(release_dir)
            self.assertEqual(copied.read_text(encoding="utf-8"), license_text)

    def test_native_build_enforces_static_runtime_and_x64_pe(self):
        build_script = (ROOT / "native_core" / "build_native_core.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("/MT", build_script)
        self.assertIn("/W4", build_script)
        self.assertIn("dumpbin /nologo /dependents", build_script)
        self.assertIn("VCRUNTIME|MSVCP|UCRTBASE|api-ms-win-crt", build_script)
        self.assertIn("8664 machine", build_script)

    def test_smoke_modules_declare_64bit_foreground_restype(self):
        """FR-20260812-D4-001: 冒烟模块必须显式声明 GetForegroundWindow 的
        64 位 restype，避免 HWND>2^31 时句柄截断导致前台比对失效。"""
        native_src = (ROOT / "fukua_rpa" / "native_smoke.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "user32.GetForegroundWindow.restype = wintypes.HWND", native_src
        )
        uia_src = (ROOT / "fukua_rpa" / "uia_smoke.py").read_text(
            encoding="utf-8"
        )
        # 子进程 _configure_win32 与父进程 run_uia_smoke 两处都需配置
        # （restype 不跨进程继承）。
        self.assertEqual(
            uia_src.count("GetForegroundWindow.restype = wintypes.HWND"), 2
        )

    def test_build_record_can_target_an_empty_release_directory(self):
        # The script performs only local reads/writes; the actual executable is
        # verified later by verify_release.py after PyInstaller has run.
        with tempfile.TemporaryDirectory() as temp_dir:
            release_dir = Path(temp_dir) / BUILD_NAME
            release_dir.mkdir()
            command = [
                sys.executable,
                str(ROOT / "scripts" / "create_build_record.py"),
                str(release_dir),
            ]
            subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
            self.assertTrue((release_dir / "BUILD_INFO.json").is_file())

    def test_development_quality_report_records_gates_and_git_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            release_dir = Path(temp_dir)
            native_sha256 = hash_file(ROOT / "fukua_rpa_core.dll")
            build_record = make_build_record(native_sha256=native_sha256)
            (release_dir / "BUILD_INFO.json").write_text(
                json.dumps(build_record), encoding="utf-8"
            )
            report = build_quality_report(
                release_dir,
                mode="development",
                source_gate="passed",
                package_gate="passed",
            )

        self.assertEqual(report["format"], "fukuaRPA_quality_report")
        self.assertEqual(report["source_sha256"], build_record["source_sha256"])
        self.assertEqual(report["build_id"], build_record["build_id"])
        self.assertEqual(report["gates"]["source"]["status"], "passed")
        self.assertEqual(
            report["gates"]["frozen_package"]["status"], "passed"
        )
        self.assertGreater(report["tests"]["declared_test_method_count"], 0)
        self.assertIn("dirty", report["git"])

    def test_onefile_quality_report_hashes_the_exact_executable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            release_dir = Path(temp_dir)
            executable = release_dir / "portable.exe"
            executable.write_bytes(b"onefile-payload")
            expected_hash = hash_file(executable)
            report = build_quality_report(
                release_dir,
                mode="development",
                source_gate="passed",
                package_gate="passed",
                artifact=executable,
            )

        self.assertEqual(report["artifact"]["name"], "portable.exe")
        self.assertEqual(report["artifact"]["sha256"], expected_hash)

    def test_sbom_contains_new_runtime_and_build_scopes(self):
        sbom, components = build_sbom(APP_VERSION, BUILD_NAME)
        self.assertEqual(sbom["bomFormat"], "CycloneDX")
        self.assertEqual(
            sbom["metadata"]["component"]["licenses"],
            [{"license": {"id": "MIT"}}],
        )
        self.assertIn("uiautomation", components)
        self.assertIn("comtypes", components)
        self.assertEqual(components["uiautomation"]["scope"], "runtime")
        self.assertEqual(components["pyinstaller"]["scope"], "build")
        self.assertEqual(components["pefile"]["scope"], "build")
        self.assertFalse(
            any(item.get("missing") for item in components.values())
        )

    def test_authenticode_status_does_not_publish_local_path(self):
        local_path = Path(r"D:\private\workspace\dist\fukuaRPA.exe")
        powershell_result = mock.Mock(
            stdout=json.dumps(
                {
                    "status": "NotSigned",
                    "message": f"The file {local_path} is not digitally signed.",
                    "subject": "",
                    "thumbprint": "",
                }
            )
        )
        with mock.patch(
            "scripts.create_release_info.subprocess.run",
            return_value=powershell_result,
        ):
            result = authenticode_status(local_path)

        self.assertEqual(result["status"], "NotSigned")
        self.assertEqual(result["message"], "No Authenticode signature is present.")
        self.assertNotIn(str(local_path), json.dumps(result))

    def test_authenticode_failure_does_not_publish_exception_path(self):
        local_path = Path(r"D:\private\workspace\dist\fukuaRPA.exe")
        with mock.patch(
            "scripts.create_release_info.subprocess.run",
            side_effect=OSError(f"cannot inspect {local_path}"),
        ):
            result = authenticode_status(local_path)

        self.assertEqual(result["status"], "Unavailable")
        self.assertEqual(result["message"], "Authenticode status could not be queried.")
        self.assertNotIn(str(local_path), json.dumps(result))

    def test_release_info_hashes_executable_build_record_and_sbom(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            release = Path(temp_dir)
            executable = release / f"{BUILD_NAME}.exe"
            executable.write_bytes(b"portable-executable")
            native_core = release / "_internal" / "fukua_rpa_core.dll"
            uia_basename = next(
                iter(discover_uiautomation_x64_dll_basenames()),
                "UIAutomationClient_VC140_X64.dll",
            )
            uia_helper = (
                release
                / "_internal"
                / "uiautomation"
                / "bin"
                / uia_basename
            )
            native_core.parent.mkdir(parents=True)
            uia_helper.parent.mkdir(parents=True)
            native_core.write_bytes(b"native-core")
            uia_helper.write_bytes(b"uia-helper")
            build_record = make_build_record(
                native_sha256=hash_file(native_core),
                source_sha256="a" * 64,
            )
            build_record["target"] = "Windows x64 onedir portable"
            build_info = release / "BUILD_INFO.json"
            build_info.write_text(json.dumps(build_record), encoding="utf-8")
            (release / "SBOM.cdx.json").write_text(
                json.dumps(
                    {
                        "metadata": {
                            "component": {"version": APP_VERSION.lstrip("v")},
                            "properties": [
                                {
                                    "name": "fukuaRPA.build_name",
                                    "value": BUILD_NAME,
                                },
                                {
                                    "name": "fukuaRPA.build_id",
                                    "value": build_record["build_id"],
                                },
                                {
                                    "name": "fukuaRPA.source_sha256",
                                    "value": build_record["source_sha256"],
                                },
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            runtime_closure = release / "RUNTIME_CLOSURE.json"
            runtime_closure.write_text('{"ok": true}\n', encoding="utf-8")
            quality_report = release / "QUALITY_REPORT.json"
            quality_report.write_text(
                json.dumps(
                    matching_quality_record(build_record, hash_file(build_info))
                ),
                encoding="utf-8",
            )
            with mock.patch(
                "scripts.create_release_info.authenticode_status",
                return_value={"status": "NotSigned"},
            ):
                report = build_release_info(release, BUILD_NAME)
            self.assertEqual(report["executable"]["sha256"], hash_file(executable))
            self.assertEqual(report["executable"]["authenticode"]["status"], "NotSigned")
            self.assertFalse(report["network_on_startup"])
            self.assertFalse(report["automatic_update_check"])
            self.assertEqual(report["update_url"], PROJECT_RELEASES_URL)
            self.assertEqual(
                report["runtime_closure"]["sha256"], hash_file(runtime_closure)
            )
            self.assertEqual(
                report["quality_report"]["sha256"], hash_file(quality_report)
            )
            self.assertEqual(report["build_id"], build_record["build_id"])
            payloads = {item["role"]: item for item in report["code_payloads"]}
            self.assertEqual(
                set(payloads),
                {"main_executable", "native_core", "uia_bitmap_helper"},
            )
            self.assertEqual(payloads["native_core"]["sha256"], hash_file(native_core))
            self.assertEqual(
                payloads["uia_bitmap_helper"]["sha256"], hash_file(uia_helper)
            )

    def test_manifest_check_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            release = Path(temp_dir) / BUILD_NAME
            release.mkdir()
            payload = release / "payload.bin"
            payload.write_bytes(b"original")
            manifest_path = Path(temp_dir) / "manifest.json"
            manifest = build_manifest(release)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(
                verify_manifest(release, manifest_path)["directory_sha256"],
                manifest["directory_sha256"],
            )
            payload.write_bytes(b"tampered")
            with self.assertRaises(RuntimeError):
                verify_manifest(release, manifest_path)

    def test_portable_archive_roundtrip_and_extra_member_rejection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            release = root / BUILD_NAME
            release.mkdir()
            (release / f"{BUILD_NAME}.exe").write_bytes(b"exe")
            internal = release / "_internal"
            internal.mkdir()
            (internal / "payload.bin").write_bytes(b"payload")
            manifest_path = root / f"{BUILD_NAME}_manifest.json"
            manifest_path.write_text(
                json.dumps(build_manifest(release)), encoding="utf-8"
            )
            archive_path = root / f"{BUILD_NAME}_portable.zip"
            build_report = build_archive(release, archive_path)
            verify_report = verify_archive(archive_path, manifest_path)
            self.assertEqual(build_report["file_count"], 2)
            self.assertTrue(verify_report["verified"])
            with zipfile.ZipFile(archive_path, "a") as archive:
                # 固定时间戳：聚焦"清单外文件"行为，不被时间戳断言（D15-7）干扰。
                archive.writestr(
                    zipfile.ZipInfo(
                        f"{BUILD_NAME}/unexpected.bin", FIXED_ZIP_TIMESTAMP
                    ),
                    b"extra",
                )
            with self.assertRaisesRegex(ValueError, "清单外文件"):
                verify_archive(archive_path, manifest_path)

    def test_portable_archive_rejects_noncanonical_member_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            release = root / BUILD_NAME
            release.mkdir()
            payload = release / "payload.bin"
            payload.write_bytes(b"payload")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(
                json.dumps(build_manifest(release)), encoding="utf-8"
            )
            archive_path = root / "noncanonical.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                # 固定时间戳：聚焦"非规范路径"行为，不被时间戳断言（D15-7）干扰。
                archive.writestr(
                    zipfile.ZipInfo(f"{BUILD_NAME}/./payload.bin", FIXED_ZIP_TIMESTAMP),
                    b"payload",
                )
            with self.assertRaisesRegex(ValueError, "非规范路径"):
                verify_archive(archive_path, manifest_path)

    def test_external_checksums_detect_tampering_and_reject_extra_lines(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            archive = root / "portable.zip"
            manifest = root / "manifest.json"
            archive.write_bytes(b"archive")
            manifest.write_bytes(b"manifest")
            paths = [archive, manifest]
            checksums = build_checksums(paths)
            self.assertTrue(verify_checksums(paths, checksums)["verified"])
            archive.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "校验失败"):
                verify_checksums(paths, checksums)
            with self.assertRaisesRegex(ValueError, "文件列表"):
                verify_checksums(paths, checksums + f"{'0' * 64}  extra.bin\n")

    def test_release_readme_explains_portable_runtime_and_trust_boundary(self):
        text = release_readme_text()
        self.assertIn(BUILD_NAME, text)
        self.assertIn("完整解压", text)
        self.assertIn("本身始终不联网", text)
        self.assertIn("不访问互联网或局域网", text)
        self.assertIn("网页访问由浏览器完成", text)
        self.assertIn("不能单独证明发布者身份", text)
        self.assertIn("LICENSE", text)

    def test_runtime_closure_accepts_x64_static_native_core(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            release = Path(temp_dir) / BUILD_NAME
            release.mkdir()
            shutil.copy2(ROOT / "fukua_rpa_core.dll", release / "fukua_rpa_core.dll")
            report = build_runtime_closure(release)
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["pe_file_count"], 1)
            self.assertEqual(report["unresolved_references"], [])
            self.assertEqual(report["wrong_architecture"], [])


class SourceFingerprintLineEndingTests(unittest.TestCase):
    """FR-SCOPE010-001: 源码指纹必须与检出换行无关。

    同一提交在 `core.autocrlf` 下可能以 CRLF 检出，而 git blob 存的是 LF。
    此前直接哈希工作树字节，于是 `source_sha256`/`build_id` 随检出方式变化
    （实测 `native_core/build_native_core.ps1` 工作树 75 个 CRLF、HEAD blob 0 个）。

    以下用例锁定修复后的契约，同时锁定不得被破坏的不变量：指纹仍须覆盖全部在
    范围文件、内容变化仍须被检出，且共享的 `io_utils.hash_file` 必须保持原始
    字节语义（它同时支撑凭据库、完整性清单与插件载荷）。
    """

    LF_TREE = {
        "pkg/mod.py": b"def f():\n    return 1\n",
        "pkg/readme.txt": b"line1\nline2\nline3\n",
    }
    CRLF_TREE = {
        "pkg/mod.py": b"def f():\r\n    return 1\r\n",
        "pkg/readme.txt": b"line1\r\nline2\r\nline3\r\n",
    }

    def _write_tree(self, root: Path, contents: dict) -> None:
        for relative, data in contents.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

    def _fingerprint_of(self, root: Path):
        """Run the production fingerprint against a synthetic tree."""
        with (
            mock.patch.object(create_build_record, "ROOT", root),
            mock.patch.object(create_build_record, "SOURCE_ROOTS", (Path("pkg"),)),
        ):
            return source_fingerprint()

    def _reference_fingerprint(self):
        """LF-normalized fingerprint computed independently of the production path."""
        aggregate = hashlib.sha256()
        entries = []
        for path in create_build_record.source_files():
            relative = path.relative_to(ROOT).as_posix()
            data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            checksum = hashlib.sha256(data).hexdigest()
            entries.append({"path": relative, "size": len(data), "sha256": checksum})
            aggregate.update(f"{checksum}  {relative}\n".encode())
        return aggregate.hexdigest(), entries

    def test_checkout_line_endings_do_not_change_the_fingerprint(self):
        tmp = Path(tempfile.mkdtemp(prefix="fr-scope010-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        lf_dir = tmp / "lf"
        crlf_dir = tmp / "crlf"
        self._write_tree(lf_dir, self.LF_TREE)
        self._write_tree(crlf_dir, self.CRLF_TREE)
        lf_hash, lf_entries = self._fingerprint_of(lf_dir)
        crlf_hash, crlf_entries = self._fingerprint_of(crlf_dir)
        self.assertEqual(lf_hash, crlf_hash)
        # sources 条目（含 size）也必须与检出无关，否则构建记录仍不可复现。
        self.assertEqual(lf_entries, crlf_entries)

    def test_scope_and_content_sensitivity_are_preserved(self):
        tmp = Path(tempfile.mkdtemp(prefix="fr-scope010-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        base_dir = tmp / "base"
        changed_dir = tmp / "changed"
        self._write_tree(base_dir, self.LF_TREE)
        self._write_tree(
            changed_dir, {**self.LF_TREE, "pkg/mod.py": b"def f():\n    return 2\n"}
        )
        base_hash, base_entries = self._fingerprint_of(base_dir)
        changed_hash, _ = self._fingerprint_of(changed_dir)
        self.assertEqual(
            sorted(entry["path"] for entry in base_entries), sorted(self.LF_TREE)
        )
        self.assertNotEqual(base_hash, changed_hash)

    def test_production_fingerprint_equals_an_independent_lf_reference(self):
        # 用测试侧独立实现的 LF 规范化指纹对照生产路径，确认修复确实是 LF
        # 规范化，而不是别的等价改写。
        self.assertEqual(source_fingerprint(), self._reference_fingerprint())

    def test_shared_hash_file_keeps_raw_byte_semantics(self):
        tmp = Path(tempfile.mkdtemp(prefix="fr-scope010-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        crlf = tmp / "crlf.txt"
        lf = tmp / "lf.txt"
        crlf.write_bytes(b"a\r\nb\r\n")
        lf.write_bytes(b"a\nb\n")
        self.assertNotEqual(hashutil_hash_file(crlf), hashutil_hash_file(lf))

    def test_hash_source_text_normalizes_and_reports_normalized_size(self):
        if hash_source_text is None:
            self.fail("FR-SCOPE010-001: 缺少 LF 规范化源码哈希 hash_source_text")
        digest, size = hash_source_text(b"a\r\nb\r\nc\r")
        self.assertEqual(digest, hashlib.sha256(b"a\nb\nc\n").hexdigest())
        self.assertEqual(size, len(b"a\nb\nc\n"))


class TestReleaseTreeExcludesSensitiveFiles(unittest.TestCase):
    """验证发布验证流程能检测并拒绝包含敏感文件的发布树。

    FR-20261005-009: 本类原先自带一份 SENSITIVE_PATTERNS 常量副本，与生产
    ``verify_release.verify_dist`` 的模式表**完全不同**（``credentials.json``
    vs 生产 ``credentials.dat``、含生产没有的 ``*.fkrplugin``、把
    ``crash_reports/`` 目录混入文件模式），而配套用例只断言那份副本自身非空，
    从不校验生产表——生产门禁因此处于零守护状态，且两份清单必然持续漂移。
    现改为直接对**生产行为**建断言，测试与生产不再有第二份真源。
    """

    def test_verify_release_rejects_debug_log_family(self):
        """FR-20261005-004: 主日志与轮转副本必须被拒绝。

        ``paths.current_log_path()`` 返回 ``rpa_debug_log.txt``，轮转副本为
        ``<path>.N``，结构化日志为 ``rpa_debug_log.jsonl`` 及其轮转副本。
        原生产模式表只有 ``rpa_debug_log*.jsonl``，这 7 个文件全部漏检
        （实测 7/7），使兜底门禁与其声明目标不一致。
        """
        log_family = (
            "rpa_debug_log.txt",
            "rpa_debug_log.txt.1",
            "rpa_debug_log.txt.2",
            "rpa_debug_log.txt.3",
            "rpa_debug_log.jsonl",
            "rpa_debug_log.jsonl.1",
            "rpa_debug_log.jsonl.2",
        )
        for name in log_family:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                dist_dir = Path(tmp)
                (dist_dir / f"{BUILD_NAME}.exe").write_bytes(b"fake-exe")
                (dist_dir / name).write_text("log line", encoding="utf-8")
                with self.assertRaises(RuntimeError) as ctx:
                    verify_dist(dist_dir)
                self.assertIn(name, str(ctx.exception))

    def test_verify_release_accepts_clean_tree(self):
        """FR-20261005-009 反向验收：正常产物不得被新增模式误拒。

        只验证敏感文件门禁这一段：``verify_dist`` 在该检查之后还会要求
        ``BUILD_INFO.json`` 等发布物料，因此这里用一个足以越过敏感检查、
        但未必能通过后续全部检查的最小目录，并断言异常原因**不是**
        "敏感/本机状态文件"。
        """
        with tempfile.TemporaryDirectory() as tmp:
            dist_dir = Path(tmp)
            (dist_dir / f"{BUILD_NAME}.exe").write_bytes(b"fake-exe")
            (dist_dir / "fukua_rpa").mkdir()
            (dist_dir / "fukua_rpa" / "__init__.py").write_text(
                "", encoding="utf-8"
            )
            (dist_dir / "assets").mkdir()
            (dist_dir / "assets" / "fukuaRPA.ico").write_bytes(b"ico")
            try:
                verify_dist(dist_dir)
            except RuntimeError as exc:
                # 干净树不得因敏感文件被拒；后续发布物料缺失属预期。
                self.assertNotIn("敏感/本机状态文件", str(exc))
            else:
                # 若整条流水线恰好都通过，也属合格（新增模式未过度拦截）。
                pass

    def test_verify_release_would_reject_sensitive_files(self):
        """FR-20260809-1303: verify_dist 必须拒绝含敏感/本机状态文件的发布树。

        直接调用生产 verify_dist（原测试自实现 os.walk+fnmatch 扫描，只验证了
        测试自身的模式逻辑，不守护 verify_dist 的实际敏感扫描）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            dist_dir = Path(tmp)
            (dist_dir / f"{BUILD_NAME}.exe").write_bytes(b"fake-exe")
            (dist_dir / "config.ini").write_text("[app]\n", encoding="utf-8")
            (dist_dir / "nested").mkdir()
            (dist_dir / "nested" / "crash-report-001.json").write_text(
                "{}", encoding="utf-8"
            )
            with self.assertRaises(RuntimeError) as ctx:
                verify_dist(dist_dir)
            message = str(ctx.exception)
            self.assertIn("敏感/本机状态文件", message)
            self.assertIn("config.ini", message)
            self.assertIn("crash-report-001.json", message)

    def test_verify_release_rejects_portable_layout_state_files(self):
        """FR-20261003-608: 便携布局运行后生成的本机状态文件必须被拒绝。

        原 sensitive_patterns 仅覆盖 config.ini/*.log/*.bak 等，漏检
        profiles_backup.json、profiles_history/、imported_assets/ 与
        crash_reports/session_state.json 等便携布局残留。
        """
        with tempfile.TemporaryDirectory() as tmp:
            dist_dir = Path(tmp)
            (dist_dir / f"{BUILD_NAME}.exe").write_bytes(b"fake-exe")
            (dist_dir / "profiles_backup.json").write_text("{}", encoding="utf-8")
            (dist_dir / "profiles_history").mkdir()
            (dist_dir / "imported_assets").mkdir()
            (dist_dir / "crash_reports").mkdir()
            (dist_dir / "crash_reports" / "session_state.json").write_text(
                "{}", encoding="utf-8"
            )
            (dist_dir / "portable.flag").write_text("", encoding="utf-8")
            with self.assertRaises(RuntimeError) as ctx:
                verify_dist(dist_dir)
            message = str(ctx.exception)
            self.assertIn("敏感/本机状态文件", message)
            self.assertIn("profiles_backup.json", message)
            self.assertIn("session_state.json", message)
            self.assertIn("profiles_history", message)

    def test_smoke_startup_cli_runs_directly(self):
        """FR-20260809-1304: scripts/smoke_startup.py 必须以 CLI 方式直接运行
        （verify_release 以子进程方式调用），--help 退出码 0 即证明 import 路径
        不抛 ModuleNotFoundError。"""
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "smoke_startup.py"), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        self.assertIn("usage", proc.stdout)

    def test_verify_release_native_smokes_include_native_capture(self):
        """FR-20260809-1302 防回归：smoke_native_capture.py 必须在正式发布
        门禁的 run 列表与 required 清单中各出现一次（与 TEST_MATRIX 声明
        '修改截图 ABI 时必须运行' 对齐）；若再次漂移出门禁即失败。"""
        source = (ROOT / "scripts" / "verify_release.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            source.count("smoke_native_capture.py"),
            2,
            "smoke_native_capture.py 应同时出现在 run 冒烟列表与 required 清单（各 1 处）",
        )

    def test_native_capture_smoke_runs_without_offscreen_platform(self):
        """C1-FUP-02 防回归：原生采集冒烟必须拿到**真实桌面**环境。

        它对自己的探针窗口调用 `GetClientRect`，在 `QT_QPA_PLATFORM=offscreen`
        下必然失败（没有原生窗口句柄）。门禁按检查类型分发环境：其余检查用
        `env_ui`（offscreen），仅该冒烟用 `env_desktop`（弹出该项）。
        若有人把环境改回一刀切 offscreen，本用例失败。
        """
        import ast

        source = (ROOT / "scripts" / "verify_release.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        func = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "verify_source"
        )

        capture_envs = []
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run"):
                continue
            scripts = [
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and child.value.endswith("smoke_native_capture.py")
            ]
            if scripts:
                capture_envs.append(getattr(node.args[1], "id", None) if len(node.args) > 1 else None)

        self.assertEqual(
            capture_envs,
            ["env_desktop"],
            "原生采集冒烟必须且只能用 env_desktop（真实桌面平台）",
        )
        self.assertIn(
            'env_desktop.pop("QT_QPA_PLATFORM", None)',
            source,
            "env_desktop 必须显式移除 QT_QPA_PLATFORM，而不是继承 offscreen",
        )
        self.assertIn(
            'env_ui["QT_QPA_PLATFORM"] = "offscreen"',
            source,
            "其余检查仍须使用 offscreen，避免无人值守时弹窗",
        )


class ReleaseLintTargetConsistencyTests(unittest.TestCase):
    """FR-20260930-018 防回归：三处 ruff 目标清单的口径一致性。

    ``scripts/verify_release.py`` 的 S11 第一遍注释声称「目标清单由
    test_release_lint_targets 锁定」，但该用例此前并不存在——独立审查
    REVIEW-20260930-002 的 FR-20260930-018 据此发现本地提交门禁
    （``.pre-commit-config.yaml`` 的 ``ruff check .``）与 CI / 正式发布口径不一致，
    恒红后被绕过。本类补齐该锁定，并约束：

    * 正式发布 S11 第一遍的目标清单不得漂移；
    * 本地提交门禁与正式发布口径一致；
    * CI 的源码子集被提交门禁覆盖；
    * 提交门禁不得整体纳入 ``plugins``——``plugins/*/tests`` 存在 7 项位于
      CI / 发布门禁范围之外的既有 ruff 违规，纳入会使钩子恒红。
    """

    #: 正式发布 S11 第一遍的权威目标清单（与 verify_release.py 同步维护）。
    RELEASE_TARGETS = [
        "fukuaRPA.py",
        "fukua_rpa",
        "tests",
        "scripts",
        "plugins/tools",
    ]

    @staticmethod
    def _release_ruff_targets(source):
        """从 verify_release.py 提取首个非 ``--select`` 的 ruff check 目标清单。"""
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.List):
                continue
            literals = [
                element.value
                for element in first.elts
                if isinstance(element, ast.Constant)
                and isinstance(element.value, str)
            ]
            if (
                "ruff" in literals
                and "check" in literals
                and "--select" not in literals
            ):
                return literals[literals.index("check") + 1 :]
        raise AssertionError("verify_release.py 中未找到不带 --select 的 ruff check 调用")

    @staticmethod
    def _precommit_ruff_args(source):
        """从 .pre-commit-config.yaml 的 ruff 钩子解析 ``args`` **原值**。

        FR-20261003-501: 与 ``_precommit_ruff_targets`` 分开——目标清单需要剔除
        钩子 ``entry`` 已提供的 ``check`` 子命令才能比对，而"命令是否可执行"必须
        看**原值**。此前只有一个函数、且无条件剥掉首个 ``check``，导致 ``args``
        里多写一个 ``check`` 也能通过全部测试（钩子实际恒红）。
        """
        lines = source.splitlines()
        for index, line in enumerate(lines):
            if line.strip() != "- id: ruff":
                continue
            for follow in lines[index + 1 :]:
                stripped = follow.strip()
                if stripped.startswith("- "):
                    break
                if not stripped.startswith("args:"):
                    continue
                return [
                    str(item)
                    for item in ast.literal_eval(stripped[len("args:") :].strip())
                ]
        raise AssertionError(".pre-commit-config.yaml 中未找到 ruff 钩子的 args")

    @classmethod
    def _precommit_ruff_targets(cls, source):
        """从 .pre-commit-config.yaml 的 ruff 钩子解析 ``args`` 目标清单。"""
        args = cls._precommit_ruff_args(source)
        if args and args[0] == "check":
            args = args[1:]
        return args

    def test_precommit_ruff_hook_command_is_executable(self):
        """FR-20261003-501：提交门禁**实际组装出的命令**必须可执行。

        钩子 ``entry`` 由外部仓库提供（``ruff-pre-commit`` 的
        ``ruff check --force-exclude``），本仓库只负责 ``args``。修复前 ``args``
        首元素重复写入 ``"check"``，pre-commit 组装出
        ``ruff check --force-exclude check <目标清单>``，``check`` 被当作不存在的
        路径 → ``E902`` → 钩子每次提交恒红、实际被绕过。

        本用例**不解析配置文本**，而是按 pre-commit 的组装方式
        （``entry`` + ``args`` + 文件）真实执行一次 ruff，断言退出码为 0；这样
        "目标清单正确但命令拼接错误"也会被拦下。
        """
        args = self._precommit_ruff_args(
            (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        )
        self.assertNotEqual(
            args[:1],
            ["check"],
            "钩子 entry 已含 `check`，args 不得重复；否则组装命令会因 E902 恒失败",
        )

        ruff_cmd = [sys.executable, "-m", "ruff"]
        probe = subprocess.run(
            ruff_cmd + ["--version"], capture_output=True, text=True, encoding="utf-8"
        )
        if probe.returncode != 0:
            self.skipTest("本环境 ruff 不可用，跳过命令拼接门禁")

        # 与 ruff-pre-commit 官方 `id: ruff` 的 entry 保持一致。
        hook_entry = ["check", "--force-exclude"]
        probe_file = "tests/test_release_tooling.py"
        command = ruff_cmd + hook_entry + args + [probe_file]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(
            result.returncode,
            0,
            "提交门禁命令不可执行："
            + " ".join(command)
            + "\n"
            + result.stdout
            + result.stderr,
        )

    @staticmethod
    def _ci_ruff_targets(source):
        """从 windows-ci.yml 的 ``run:`` 行解析 ruff check 目标清单。"""
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped.startswith("run:") or "ruff check" not in stripped:
                continue
            command = stripped[len("run:") :].split()
            return command[command.index("check") + 1 :]
        raise AssertionError("windows-ci.yml 中未找到 ruff check 步骤")

    def test_release_lint_targets(self):
        """正式发布 S11 第一遍的目标清单与权威清单一致。"""
        release = self._release_ruff_targets(
            (ROOT / "scripts" / "verify_release.py").read_text(encoding="utf-8")
        )
        self.assertEqual(release, self.RELEASE_TARGETS)

    def test_precommit_ruff_targets_match_release(self):
        """本地提交门禁与正式发布口径一致，且不整体纳入 plugins。"""
        precommit = self._precommit_ruff_targets(
            (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            precommit,
            self.RELEASE_TARGETS,
            "本地提交门禁的 ruff 目标清单必须与正式发布 S11 第一遍一致",
        )
        self.assertNotIn(
            "plugins",
            precommit,
            "不得整体纳入 plugins：plugins/*/tests 有 7 项门禁范围外的既有违规",
        )
        self.assertIn("plugins/tools", precommit)

    def test_ci_ruff_targets_are_subset_of_precommit(self):
        """CI 的源码子集必须被本地提交门禁覆盖。"""
        ci = self._ci_ruff_targets(
            (ROOT / ".github" / "workflows" / "windows-ci.yml").read_text(
                encoding="utf-8"
            )
        )
        precommit = self._precommit_ruff_targets(
            (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        )
        self.assertTrue(
            set(ci) <= set(precommit),
            f"CI 目标未被提交门禁覆盖：{sorted(set(ci) - set(precommit))}",
        )


class PyrightBaselineGateTests(unittest.TestCase):
    """ADV-20261006-019：FR-015 棘轮必须同时接入 CI 与 pre-commit。

    棘轮此前只有手工运行一条路径，CI 与提交前都不校验，"基线与实际不一致"只能
    等有人想起来跑才发现。这些用例锁定两条接入路径都存在，且**只校验既有基线**：
    不得出现 ``--write`` 或 ``--force``，否则门禁会变成"自动改写批准上限"。
    """

    HOOK_ID = "pyright-plugin-baseline"

    def test_precommit_registers_the_baseline_hook(self):
        source = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        self.assertIn(self.HOOK_ID, source, "pre-commit 未登记 FR-015 棘轮钩子")

    def test_precommit_hook_never_writes_or_forces(self):
        source = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        block = source.split(f"- id: {self.HOOK_ID}", 1)[1]
        entry_line = next(
            line for line in block.splitlines() if line.strip().startswith("entry:")
        )
        self.assertIn("scripts/pyright_plugin_baseline.py", entry_line)
        self.assertNotIn("--write", entry_line)
        self.assertNotIn("--force", entry_line)

    def test_hook_triggers_on_the_governed_files(self):
        source = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        block = source.split(f"- id: {self.HOOK_ID}", 1)[1]
        files_line = next(
            line for line in block.splitlines() if line.strip().startswith("files:")
        )
        # 该行是正则，含反斜杠转义，因此比对时去掉转义符。
        normalized = files_line.replace("\\", "")
        self.assertIn("pyright_plugin_baseline.json", normalized)
        self.assertIn("plugin_", normalized)

    def test_ci_runs_the_baseline_gate(self):
        ci = (ROOT / ".github" / "workflows" / "windows-ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "scripts/pyright_plugin_baseline.py", ci, "CI 未运行 FR-015 棘轮"
        )

    def test_ci_never_writes_or_forces(self):
        ci = (ROOT / ".github" / "workflows" / "windows-ci.yml").read_text(
            encoding="utf-8"
        )
        for line in ci.splitlines():
            if "scripts/pyright_plugin_baseline.py" in line:
                self.assertNotIn("--write", line)
                self.assertNotIn("--force", line)

    def test_ci_treats_missing_pyright_as_skipped_not_passed(self):
        """pyright 缺失时必须告警跳过，不得静默当成通过。"""
        ci = (ROOT / ".github" / "workflows" / "windows-ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("LASTEXITCODE -eq 2", ci)
        self.assertIn("::warning::", ci)


if __name__ == "__main__":
    unittest.main()
