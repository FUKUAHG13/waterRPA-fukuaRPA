"""公开源码与私有门禁资料必须分别受保护，私有输入缺失不能成为跳过理由。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import create_build_record
from scripts.private_release_inputs import (
    BOUND_HASH_ENV,
    BOUND_ROOT_ENV,
    REFACTOR_BASELINE_PATH,
    REQUIRED_INPUTS,
    assert_public_source_boundary,
    assert_validation_inputs_unchanged,
    bind_validation_inputs,
    capture_validation_inputs,
    is_private_validation_path,
    load_refactor_baseline,
)


def seed_inputs(root: Path) -> None:
    for name in REQUIRED_INPUTS:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("private-input:" + name).encode())


class PrivateReleaseInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = mock.patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop(BOUND_ROOT_ENV, None)
        os.environ.pop(BOUND_HASH_ENV, None)

    def _evidence_fixture(self, mode="formal"):
        from scripts.build_identity import create_build_identity

        fields = {
            "application_version": "v1.1.0", "build_name": "fixture", "build_mode": mode,
            "source_sha256": "a" * 64,
        }
        receipt = {"format_version": 1, "sha256": "b" * 64, "file_count": 2}
        identity = create_build_identity(
            **fields, git_commit="c" * 40, git_dirty=False, python_version="3.14.3",
            python_bits=64, dependencies={}, native_core_sha256="d" * 64,
        )
        evidence = {**fields, "build_id": identity["build_id"],
                    "build_identity": identity["fields"], "private_validation_inputs": receipt}
        record = {
            **evidence, "git": {"commit": "c" * 40, "dirty": False},
            "python": {"version": "3.14.3", "bits": 64}, "dependencies": {},
            "native_core": {"sha256": "d" * 64},
        }
        return record, evidence

    def test_exact_boundary_keeps_public_coverage_and_helpers(self):
        for name in (
            "docs/设计基准.md", "DOCS/BUILD_FROM_SOURCE.md", "docs\\UI开发规范.md",
            "scripts/run_startup_baseline.py", "tests/test_crash_guard.py",
            "tests/test_pyright_plugin_baseline.py",
        ):
            with self.subTest(path=name):
                self.assertTrue(is_private_validation_path(name))
        for name in (
            "fukua_rpa/crash_guard.py", "tests/test_crash_guard_coverage.py",
            "scripts/private_release_inputs.py", "plugins/office-docs/README.md",
            "mydocs/README.md",
        ):
            with self.subTest(path=name):
                self.assertFalse(is_private_validation_path(name))

    def test_formal_inputs_missing_fail_explicitly(self):
        with self.assertRaisesRegex(RuntimeError, "不能跳过正式门禁"):
            capture_validation_inputs(self.root, required=True)

    def test_historical_baseline_cannot_be_missing_or_replaced_even_before_binding(self):
        with self.assertRaisesRegex(RuntimeError, "缺少固定历史重构基线"):
            load_refactor_baseline(self.root)
        seed_inputs(self.root)
        # 即使其他资料均齐备、可以产生新的资料指纹，也不能换掉原始历史文件。
        bind_validation_inputs(self.root)
        with self.assertRaisesRegex(RuntimeError, "历史重构基线身份不符"):
            load_refactor_baseline(self.root)
        self.assertTrue((self.root / REFACTOR_BASELINE_PATH).is_file())

    def test_receipt_does_not_disclose_content_names_or_machine_paths(self):
        seed_inputs(self.root)
        receipt = capture_validation_inputs(self.root, required=True).receipt()
        self.assertEqual(set(receipt), {"format_version", "sha256", "file_count"})
        serialized = json.dumps(receipt)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("private-input", serialized)
        self.assertNotIn("docs/", serialized)
        self.assertEqual(receipt["file_count"], len(REQUIRED_INPUTS))

    def test_identity_is_stable_but_tracks_actual_bytes_and_membership(self):
        seed_inputs(self.root)
        first = capture_validation_inputs(self.root, required=True)
        self.assertEqual(first, capture_validation_inputs(self.root, required=True))
        (self.root / "docs/PERFORMANCE_BASELINE.json").write_bytes(b"changed-baseline")
        second = capture_validation_inputs(self.root, required=True)
        self.assertNotEqual(first.sha256, second.sha256)
        (self.root / "docs/new-private.md").write_bytes(b"new")
        self.assertNotEqual(second.sha256, capture_validation_inputs(self.root).sha256)

    def test_bound_inputs_cannot_be_rebound_after_modification(self):
        seed_inputs(self.root)
        first = bind_validation_inputs(self.root)
        (self.root / "tests/test_crash_guard.py").write_bytes(b"changed-test")
        with self.assertRaisesRegex(RuntimeError, "发生变化"):
            bind_validation_inputs(self.root)
        with self.assertRaisesRegex(RuntimeError, "发生变化"):
            assert_validation_inputs_unchanged(first, self.root)

    def test_bound_root_cannot_silently_switch_to_an_equivalent_copy(self):
        seed_inputs(self.root)
        bind_validation_inputs(self.root)
        other = self.root / "other"
        seed_inputs(other)
        with self.assertRaisesRegex(RuntimeError, "目录发生变化"):
            bind_validation_inputs(other)

    def test_missing_file_after_binding_is_rejected(self):
        seed_inputs(self.root)
        first = bind_validation_inputs(self.root)
        (self.root / "docs/PERFORMANCE_BASELINE.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "缺少维护者私有验证输入"):
            assert_validation_inputs_unchanged(first, self.root)

    def test_public_source_and_private_inputs_have_independent_fingerprints(self):
        seed_inputs(self.root)
        public = self.root / "scripts/public_helper.py"
        public.write_bytes(b"answer = 42\n")
        roots = (Path("docs"), Path("scripts"), Path("tests"))
        with (
            mock.patch.object(create_build_record, "ROOT", self.root),
            mock.patch.object(create_build_record, "SOURCE_ROOTS", roots),
        ):
            source_before, entries = create_build_record.source_fingerprint()
            self.assertEqual([item["path"] for item in entries], ["scripts/public_helper.py"])
            inputs_before = capture_validation_inputs(self.root, required=True)
            (self.root / "docs/PERFORMANCE_BASELINE.json").write_bytes(b"different")
            self.assertEqual(source_before, create_build_record.source_fingerprint()[0])
            inputs_after = capture_validation_inputs(self.root, required=True)
            self.assertNotEqual(inputs_before.sha256, inputs_after.sha256)
            public.write_bytes(b"answer = 43\n")
            self.assertNotEqual(source_before, create_build_record.source_fingerprint()[0])
            self.assertEqual(inputs_after, capture_validation_inputs(self.root, required=True))

    def test_formal_build_cannot_rebuild_native_before_inputs_are_checked(self):
        from scripts import build_release

        with (
            mock.patch.object(sys, "argv", ["build_release.py", "--formal-release"]),
            mock.patch.object(build_release, "require_formal_release_provenance"),
            mock.patch.object(build_release, "assert_public_source_boundary"),
            mock.patch.object(build_release, "bind_validation_inputs", side_effect=RuntimeError("missing")),
            mock.patch.object(build_release, "build_native_core") as build,
        ):
            with self.assertRaisesRegex(RuntimeError, "missing"):
                build_release.main()
            build.assert_not_called()

    def test_formal_quality_rejects_a_different_private_validation_identity(self):
        from scripts import create_quality_report

        release = self.root / "release"
        release.mkdir()
        (release / "BUILD_INFO.json").write_text(
            json.dumps({"private_validation_inputs": {"sha256": "old"}}), encoding="utf-8"
        )
        with (
            mock.patch.object(create_quality_report, "require_formal_release_provenance", return_value={}),
            mock.patch.object(create_quality_report, "validation_receipt", return_value={"sha256": "new"}),
            mock.patch.object(create_build_record, "source_fingerprint", return_value=("a" * 64, [])),
            self.assertRaisesRegex(ValueError, "私有验证输入身份不一致"),
        ):
            create_quality_report.build_quality_report(
                release, mode="formal", source_gate="passed", package_gate="passed"
            )

    def test_partial_binding_is_rejected(self):
        seed_inputs(self.root)
        os.environ[BOUND_HASH_ENV] = "f" * 64
        with self.assertRaisesRegex(RuntimeError, "拒绝继续正式构建"):
            bind_validation_inputs(self.root)

    def test_offline_evidence_cannot_mix_validation_receipts_even_with_same_build_id(self):
        from scripts import build_identity

        fields = {
            "application_version": "v1.1.0", "build_name": "fukuaRPA_v1.1.0",
            "build_mode": "formal", "source_sha256": "source",
        }
        identity = {"build_id": "same-build", "fields": fields}
        receipt = {"format_version": 1, "sha256": "a" * 64, "file_count": 56}
        record = {"private_validation_inputs": receipt}
        evidence = {**fields, "build_id": "same-build", "build_identity": fields,
                    "private_validation_inputs": receipt}
        with mock.patch.object(build_identity, "identity_from_build_record", return_value=identity):
            self.assertEqual(build_identity.require_matching_evidence(record, evidence), identity)
            for bad in (None, {**receipt, "sha256": "b" * 64}):
                with self.subTest(receipt=bad), self.assertRaisesRegex(ValueError, "私有验证输入"):
                    build_identity.require_matching_evidence(
                        record, {**evidence, "private_validation_inputs": bad}
                    )

    def test_formal_receipts_cannot_all_be_missing_null_or_equally_invalid(self):
        from scripts.build_identity import require_matching_evidence

        record, evidence = self._evidence_fixture()
        expected = require_matching_evidence(record, evidence)
        valid = record["private_validation_inputs"]
        cases = [
            ("missing", None), ("null", None),
            ("wrong-version", {**valid, "format_version": 2}),
            ("boolean-version", {**valid, "format_version": True}),
            ("invalid-hash", {**valid, "sha256": "z" * 64}),
            ("short-hash", {**valid, "sha256": "a"}),
            ("zero-count", {**valid, "file_count": 0}),
            ("boolean-count", {**valid, "file_count": True}),
            ("string-count", {**valid, "file_count": "2"}),
            ("private-fields", {**valid, "paths": ["private-local-file"]}),
        ]
        for name, receipt in cases:
            left, right = dict(record), dict(evidence)
            if name == "missing":
                left.pop("private_validation_inputs")
                right.pop("private_validation_inputs")
            else:
                left["private_validation_inputs"] = right["private_validation_inputs"] = receipt
            with self.subTest(case=name), self.assertRaisesRegex(ValueError, "私有验证输入回执"):
                require_matching_evidence(left, right)
        with self.assertRaisesRegex(ValueError, "身份不一致"):
            require_matching_evidence(record, {**evidence, "private_validation_inputs": {**valid, "sha256": "e" * 64}})
        self.assertEqual(require_matching_evidence(record, evidence), expected)
        legacy_record, legacy_evidence = self._evidence_fixture("development")
        legacy_record.pop("private_validation_inputs")
        legacy_evidence.pop("private_validation_inputs")
        require_matching_evidence(legacy_record, legacy_evidence)

    def test_formal_receipt_is_required_by_real_offline_release_info_command(self):
        root = Path(__file__).resolve().parents[1]
        release = self.root / "release"
        release.mkdir()
        record, evidence = self._evidence_fixture()
        record.pop("private_validation_inputs")
        evidence.pop("private_validation_inputs")
        for name, payload in (("BUILD_INFO.json", record), ("QUALITY_REPORT.json", evidence),
                              ("SBOM.cdx.json", {}), ("RUNTIME_CLOSURE.json", {})):
            (release / name).write_text(json.dumps(payload), encoding="utf-8")
        (release / "fixture.exe").write_bytes(b"offline-test-fixture-not-executable")
        command = [sys.executable, "-B", str(root / "scripts/create_release_info.py"),
                   str(release), "--build-name", "fixture"]
        environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            command, cwd=root, env=environment, capture_output=True, encoding="utf-8", errors="replace"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("私有验证输入回执", result.stderr)
        self.assertFalse((release / "RELEASE_INFO.json").exists())

    def test_actual_index_rejects_private_inputs_even_if_gitignore_hides_them(self):
        subprocess.run(["git", "init", str(self.root)], capture_output=True, check=True)
        (self.root / ".gitignore").write_text("/docs/\n", encoding="utf-8")
        private = self.root / "docs/PERFORMANCE_BASELINE.json"
        private.parent.mkdir()
        private.write_bytes(b"private")
        subprocess.run(
            ["git", "add", "-f", "--", "docs/PERFORMANCE_BASELINE.json"],
            cwd=self.root, capture_output=True, check=True,
        )
        with self.assertRaisesRegex(RuntimeError, "禁止公开的文件"):
            assert_public_source_boundary(self.root)

    def test_tracked_public_plugin_bundle_is_not_mistaken_for_a_build_product(self):
        subprocess.run(["git", "init", str(self.root)], capture_output=True, check=True)
        plugin = self.root / "plugins/example/example.fkrplugin"
        plugin.parent.mkdir(parents=True)
        plugin.write_bytes(b"deterministic-bundle-fixture")
        subprocess.run(
            ["git", "add", "--", "plugins/example/example.fkrplugin"],
            cwd=self.root, capture_output=True, check=True,
        )
        assert_public_source_boundary(self.root)

    def test_missing_private_inputs_cannot_fall_back_to_reduced_formal_checks(self):
        from scripts import verify_release

        with (
            mock.patch.object(verify_release, "bind_validation_inputs", side_effect=RuntimeError("missing")),
            mock.patch.object(verify_release, "run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "missing"):
                verify_release.verify_source()
            run.assert_not_called()

    def test_ignored_private_rules_require_real_files_and_do_not_exempt_other_ignored_paths(self):
        from scripts import test_impact

        subprocess.run(["git", "init", str(self.root)], capture_output=True, check=True)
        (self.root / ".gitignore").write_text("/docs/\n/notes/\n", encoding="utf-8")
        private = self.root / "docs/present.md"
        private.parent.mkdir()
        private.write_text("private validation input", encoding="utf-8")
        other = self.root / "notes/local.md"
        other.parent.mkdir()
        other.write_text("ignored local state", encoding="utf-8")
        patterns = ("docs/present.md", "docs/missing.md", "notes/local.md")
        rules = (test_impact.ImpactRule(patterns, ("packaging",), "private input guard"),)
        with (
            mock.patch.object(test_impact, "ROOT", self.root),
            mock.patch.object(test_impact, "RULES", rules),
        ):
            self.assertEqual(test_impact.stale_rule_patterns(), ("docs/missing.md", "notes/local.md"))
            private.unlink()
            self.assertEqual(test_impact.stale_rule_patterns(), patterns)

    def test_public_ci_does_not_build_packages_without_private_validation_inputs(self):
        import yaml

        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/windows-ci.yml").read_text(encoding="utf-8"))
        jobs = workflow["jobs"]
        self.assertIn("不替代正式门禁", jobs["source-quality"]["name"])
        for name in ("onedir-build", "nightly-onedir-build"):
            self.assertEqual(jobs[name]["needs"], "source-quality")
            self.assertIn("needs.source-quality.outputs.private-inputs == 'true'", jobs[name]["if"])
        steps = {step["name"]: step for step in jobs["source-quality"]["steps"]}
        for name in ("运行受影响源码测试", "UI 新增代码规范门禁"):
            self.assertEqual(steps[name]["if"], "steps.private-inputs.outputs.available == 'true'")


if __name__ == "__main__":
    unittest.main()
