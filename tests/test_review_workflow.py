"""审查收尾工具的真实拒绝边界；文件与 Git 对象均由测试自建。"""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.check_review_workflow import (
    InvalidRecord,
    check_delivery,
    check_report,
    check_test_log,
    read_json,
    verify_backup,
)
from scripts.test_impact import plan_impacted_tests


def manifest_record(commit, files):
    manifest = [{"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for path, data in sorted(files.items())]
    digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    return {"backup_commit": commit, "manifest": manifest, "raw_manifest_sha256": digest}


class ReviewFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fukua-review-guard-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.report = self.root / "08_修复复验.md"
        self.ledger = self.root / "ledger.md"
        self.ledger.write_text("| `RM-20261012-368` | 未执行 |\n", encoding="utf-8")
        self.meta = {
            "workflow_schema": "fukuarpa-review-flow-v2",
            "review_rules_revision": "2026-10-15", "report_schema": "fukuarpa-review-v3",
            "verification_status": "draft", "authority_status": "working",
            "workflow_status": "repair_verification_passed", "next_action": "delivery_closeout",
            "executing_role": "B", "executing_task": "independent-review-task-002",
            "implementation_task": "implementation-task-001", "entry_mode": "user_handoff",
            "authorization_source": "测试自有需求", "handoff_file": str(self.report),
            "repair_verification_status": "passed", "required_gate_status": "passed",
            "delivery_status": "pending", "transferred_ids": ["RM-20261012-368"],
            "release_readiness": "not_evaluated",
            "independent_review_execution_status": "completed",
            "independent_review_identity_source": "测试自有独立任务记录",
            "model_parameter_support": "available", "reasoning_parameter_support": "available",
            "service_tier_parameter_support": "unavailable",
            "independent_review_configured_model": "gpt-6.1-sol",
            "independent_review_configured_reasoning_effort": "max",
            "independent_review_model": "unknown",
        }

    def write_report(self, meta=None, path=None):
        meta = self.meta if meta is None else meta
        path = self.report if path is None else path
        action = meta["next_action"]
        display = "提示词 " + action[7:].upper() if action.startswith("prompt_") else action
        text = "# 测试自有记录\n\n```yaml\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
        text += (f"```\n\n流程状态：{meta['workflow_status']}\n下一步使用：{display}\n"
                 f"下一步交接文件：@file:{meta['handoff_file']}\n")
        path.write_text(text, encoding="utf-8")
        return path


class ReviewWorkflowTests(ReviewFixture, unittest.TestCase):
    def arbitration_record(self, result):
        meta = {**self.meta, "workflow_stage": "rework_arbitration", "executing_role": "C",
                "executing_task": "independent-arbitration-task-003", "arbitration_status": result,
                "workflow_status": "rework_dispute_resolved", "next_action": "prompt_9"}
        del meta["repair_verification_status"]
        if result == "blocked":
            meta.update(workflow_status="rework_factual_dispute", next_action="environment_required",
                        required_gate_status="blocked", blocked_ids=["GATE-EXAMPLE-001"])
        elif result == "maintainer_decision_required":
            meta.update(workflow_status="maintainer_decision_required", next_action="maintainer_decision",
                        maintainer_decision_ids=["DEC-EXAMPLE-001"])
        else:
            field = "action_required_ids" if result == "action_required" else "no_action_required_ids"
            meta[field] = ["FR-EXAMPLE-001"]
        return meta

    def test_c_arbitration_routes_use_c_results_including_blocked(self):
        for result in ("blocked", "action_required", "no_action_required", "maintainer_decision_required"):
            with self.subTest(result=result):
                self.write_report(self.arbitration_record(result))
                check_report(self.report, self.ledger)

    def test_c_arbitration_cannot_bypass_result_ids_or_b_requirements(self):
        for result, field in [("blocked", "blocked_ids"), ("action_required", "action_required_ids"),
                              ("no_action_required", "no_action_required_ids"),
                              ("maintainer_decision_required", "maintainer_decision_ids")]:
            with self.subTest(result=result):
                meta = self.arbitration_record(result)
                meta[field] = []
                self.write_report(meta)
                with self.assertRaises(InvalidRecord):
                    check_report(self.report, self.ledger)
        meta = self.arbitration_record("blocked")
        for extra in ({"arbitration_status": "invalid"}, {"rework_verification_status": "factual_dispute"},
                      {"workflow_stage": "rework_verification"}):
            self.write_report({**meta, **extra})
            with self.assertRaises(InvalidRecord):
                check_report(self.report, self.ledger)
        for result in (None, "blocked"):
            meta = {**self.meta, "workflow_status": "rework_factual_dispute", "next_action": "prompt_10b",
                    "unresolved_dispute_ids": ["FR-EXAMPLE-001"]}
            del meta["repair_verification_status"]
            if result is not None:
                meta["rework_verification_status"] = result
            self.write_report(meta)
            with self.assertRaises(InvalidRecord):
                check_report(self.report, self.ledger)

    def test_nonpassing_status_values_and_route_mismatches_are_rejected(self):
        base = {**self.meta, "workflow_status": "repair_changes_required", "next_action": "prompt_9",
                "repair_verification_status": "changes_required", "blocking_rework_ids": ["FR-EXAMPLE-001"]}
        for field in ("delivery_status", "independent_review_execution_status", "required_gate_status",
                      "repair_verification_status", "rework_verification_status"):
            with self.subTest(field=field):
                self.write_report({**base, field: "invalid"})
                with self.assertRaisesRegex(InvalidRecord, "枚举|路由"):
                    check_report(self.report, self.ledger)
        self.write_report({**base, "repair_verification_status": "blocked", "blocking_rework_ids": []})
        with self.assertRaisesRegex(InvalidRecord, "路由|返修"):
            check_report(self.report, self.ledger)
        self.write_report({**base, "blocking_rework_ids": []})
        with self.assertRaisesRegex(InvalidRecord, "返修"):
            check_report(self.report, self.ledger)
        for field, result, status, action, ids in [
            ("rework_verification_status", "changes_required", "rework_changes_required", "prompt_9", {"changes_required_ids": ["FR-EXAMPLE-001"]}),
            ("rework_verification_status", "blocked", "rework_verification_blocked", "environment_required", {"blocked_ids": ["GATE-EXAMPLE-001"]}),
            ("rework_verification_status", "factual_dispute", "rework_factual_dispute", "prompt_10b", {"unresolved_dispute_ids": ["FR-EXAMPLE-001"]}),
        ]:
            with self.subTest(status=status):
                meta = {**self.meta, "workflow_status": status, "next_action": action, field: result, **ids}
                del meta["repair_verification_status"]
                self.write_report(meta)
                check_report(self.report, self.ledger)

    def test_legal_failure_and_blocked_routes_remain_usable(self):
        base = {**self.meta, "required_gate_status": "blocked"}
        for status, result, action, extra in [
            ("repair_changes_required", "changes_required", "prompt_9", {"blocking_rework_ids": ["FR-EXAMPLE-001"]}),
            ("repair_verification_blocked", "blocked", "environment_required", {"blocked_ids": ["GATE-EXAMPLE-001"]}),
        ]:
            with self.subTest(status=status):
                self.write_report({**base, "workflow_status": status, "repair_verification_status": result,
                                   "next_action": action, **extra})
                check_report(self.report, self.ledger)
        self.write_report({**base, "workflow_status": "repair_verification_blocked",
                           "repair_verification_status": "blocked", "next_action": "environment_required"})
        with self.assertRaisesRegex(InvalidRecord, "阻塞"):
            check_report(self.report, self.ledger)

    def test_standard_unittest_expected_failures_and_skips_are_accepted(self):
        for with_skip in (False, True):
            source = ("import unittest\nclass T(unittest.TestCase):\n"
                      " def test_ok(self): self.assertTrue(True)\n"
                      " @unittest.expectedFailure\n def test_expected(self): self.fail('expected')\n")
            if with_skip:
                source += " @unittest.skip('test fixture')\n def test_skipped(self): self.fail('skipped')\n"
            source += "unittest.main()\n"
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", source], capture_output=True,
                                    text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(check_test_log(result.stderr, "unittest", 0), 3 if with_skip else 2)
        with self.assertRaises(InvalidRecord):
            check_test_log("Ran 2 tests in 0.1s\nOK (nonsense=1)", "unittest", 0)

    def test_dev_dependency_changes_keep_conservative_full_selection(self):
        plan = plan_impacted_tests(["requirements-dev.txt"])
        self.assertTrue(plan.full_suite)
        self.assertIn("tests.test_review_workflow", plan.modules)

    def test_passed_review_cannot_omit_independent_execution(self):
        self.meta.pop("independent_review_execution_status")
        self.write_report()
        with self.assertRaisesRegex(InvalidRecord, "独立复核完成"):
            check_report(self.report, self.ledger)

    def test_duplicate_json_backup_keys_are_rejected(self):
        record = self.root / "backup.json"
        record.write_text('{"backup_commit":"first","backup_commit":"second"}', encoding="utf-8")
        with self.assertRaisesRegex(InvalidRecord, "重复键"):
            read_json(record)

    def test_passed_review_returns_to_a_without_being_delivery_complete(self):
        self.write_report()
        self.assertEqual(check_report(self.report, self.ledger)["delivery_status"], "pending")
        self.meta["next_action"] = "stop"
        self.write_report()
        with self.assertRaisesRegex(InvalidRecord, "路由|回交"):
            check_report(self.report, self.ledger)

    def test_invalid_report_enum_and_duplicate_yaml_are_rejected(self):
        self.meta["verification_status"] = "working"
        self.write_report()
        with self.assertRaisesRegex(InvalidRecord, "枚举"):
            check_report(self.report, self.ledger)
        text = self.report.read_text(encoding="utf-8").replace(
            "```yaml\n", "```yaml\nnext_action: stop\n", 1)
        self.report.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(InvalidRecord, "唯一"):
            check_report(self.report, self.ledger)

    def test_footer_cannot_disagree_with_header(self):
        self.write_report()
        self.report.write_text(self.report.read_text(encoding="utf-8").replace(
            "下一步使用：delivery_closeout", "下一步使用：stop"), encoding="utf-8")
        with self.assertRaisesRegex(InvalidRecord, "三行"):
            check_report(self.report, self.ledger)

    def test_unknown_workflow_status_cannot_pass_even_with_matching_footer(self):
        self.meta["workflow_status"] = "anything_done"
        self.write_report()
        with self.assertRaisesRegex(InvalidRecord, "路由"):
            check_report(self.report, self.ledger)

    def test_body_mentions_are_not_ledger_registration(self):
        self.write_report()
        self.ledger.write_text("正文提到 RM-20261012-368，但没有登记行。", encoding="utf-8")
        with self.assertRaisesRegex(InvalidRecord, "漏登"):
            check_report(self.report, self.ledger)

    def test_passed_record_cannot_drop_blocking_gate(self):
        self.meta["unmet_required_gate_ids"] = ["GATE-SOURCE-S11"]
        self.write_report()
        with self.assertRaisesRegex(InvalidRecord, "阻塞"):
            check_report(self.report, self.ledger)

    def test_fallback_preserves_available_model_and_unknown_observation(self):
        self.meta.update({"independent_review_execution_status": "completed",
                          "independent_review_identity_source": "平台任务记录 task-002",
                          "model_parameter_support": "available", "reasoning_parameter_support": "available",
                          "service_tier_parameter_support": "unavailable",
                          "independent_review_configured_model": "gpt-6.1-sol",
                          "independent_review_configured_reasoning_effort": "max",
                          "independent_review_model": "unknown"})
        self.write_report()
        check_report(self.report, self.ledger)
        self.meta["independent_review_configured_model"] = "gpt-5.6-luna"
        self.write_report()
        with self.assertRaisesRegex(InvalidRecord, "model"):
            check_report(self.report, self.ledger)

    def test_test_results_require_nonzero_execution_and_real_summary(self):
        for runner, text in [("pytest", ""), ("pytest", "no tests ran in 0.01s"),
                             ("pytest", "3 skipped in 0.1s"), ("unittest", "Ran 0 tests in 0.0s\nOK"),
                             ("unittest", "Ran 2 tests in 0.0s\nOK (skipped=2)")]:
            with self.subTest(runner=runner, text=text), self.assertRaises(InvalidRecord):
                check_test_log(text, runner, 0)
        self.assertEqual(check_test_log("25 passed, 3 skipped in 2.0s", "pytest", 0), 25)
        self.assertEqual(check_test_log("Ran 28 tests in 2.0s\r\nOK (skipped=3)\r\n", "unittest", 0), 28)

    def test_failure_and_subfailure_cannot_be_hidden_by_exit_zero(self):
        with self.assertRaises(InvalidRecord):
            check_test_log("1 failed, 25 passed in 2.0s", "pytest", 0)
        with self.assertRaises(InvalidRecord):
            check_test_log("SUBFAILED test_a\n25 passed, 1 subtests failed in 2.0s", "pytest", 0)
        self.assertEqual(check_test_log("1 failed, 25 passed in 2.0s", "pytest", 1, 1), 26)

    def test_cli_invalid_input_returns_nonzero(self):
        self.write_report()
        self.meta["verification_status"] = "working"
        self.write_report()
        script = Path(__file__).resolve().parents[1] / "scripts/check_review_workflow.py"
        result = subprocess.run([sys.executable, "-X", "utf8", str(script), "report", str(self.report),
                                 "--ledger", str(self.ledger)], capture_output=True, text=True,
                                encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn("校验未通过", result.stderr)

    def test_real_source_paths_select_this_guard(self):
        for path in ["scripts/check_review_workflow.py", "AGENTS.md", "docs/代码审查基准.md",
                     "docs/代码审查模板.md", "docs/开发备份与恢复规范.md"]:
            with self.subTest(path=path):
                self.assertIn("tests.test_review_workflow", plan_impacted_tests([path]).modules)


class RawBackupTests(ReviewFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "objects.git"
        subprocess.run(["git", "init", "--bare", str(self.repo)], capture_output=True, check=True, timeout=15)

    def git(self, *args, data=None):
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Review guard",
                               "-c", "user.email=review-test@example.invalid", *args], input=data,
                              capture_output=True, check=True, timeout=15).stdout

    def commit(self, data, parent=None):
        oid = self.git("hash-object", "-w", "--stdin", data=data).decode().strip()
        tree = self.git("mktree", data=f"100644 blob {oid}\talpha.txt\n".encode()).decode().strip()
        args = ["commit-tree", tree]
        if parent:
            args += ["-p", parent]
        return self.git(*args, data=b"test-only\n").decode().strip()

    def test_git_commit_presence_cannot_replace_raw_byte_match(self):
        commit = self.commit(b"one\ntwo\n")
        record = manifest_record(commit, {"alpha.txt": b"one\r\ntwo\r\n"})
        with self.assertRaisesRegex(InvalidRecord, "原始字节"):
            verify_backup(record, self.repo)
        record = manifest_record(commit, {"alpha.txt": b"one\ntwo\n"})
        self.assertEqual(verify_backup(record, self.repo), 1)

    def test_raw_binary_backup_and_manifest_integrity(self):
        data = b"\x00\xff\r\n\x01"
        commit = self.commit(data)
        record = manifest_record(commit, {"alpha.txt": data})
        self.assertEqual(verify_backup(record, self.repo), 1)
        record["raw_manifest_sha256"] = "0" * 64
        with self.assertRaisesRegex(InvalidRecord, "总指纹"):
            verify_backup(record, self.repo)

    def test_missing_file_or_duplicate_path_cannot_pass(self):
        commit = self.commit(b"one")
        record = manifest_record(commit, {"other.txt": b"one"})
        with self.assertRaisesRegex(InvalidRecord, "文件集"):
            verify_backup(record, self.repo)
        record = manifest_record(commit, {"../escape": b"one"})
        with self.assertRaisesRegex(InvalidRecord, "安全"):
            verify_backup(record, self.repo)

    def legacy_delivery(self):
        commit = self.commit(b"legacy\r\n")
        record = manifest_record(commit, {"alpha.txt": b"legacy\r\n"})
        record.update(checkpoint_kind="development", backup_status="verified",
                      remote_retrieved_commit=commit, verified_at="2026-10-15")
        (self.root / "backup.json").write_text(json.dumps(record), encoding="utf-8")
        self.meta.update(review_rules_revision="2026-10-09", next_action="stop", reviewed_commit=commit)
        self.write_report()
        delivery = self.root / "11_交付收尾.md"
        meta = {**self.meta, "review_rules_revision": "2026-10-15", "executing_role": "A",
                "executing_task": "implementation-task-001", "workflow_status": "delivery_complete",
                "entry_mode": "review_result_return",
                "delivery_status": "complete", "handoff_file": str(delivery),
                "review_report": self.report.name, "backup_record": "backup.json"}
        del meta["repair_verification_status"]
        self.write_report(meta, delivery)
        return delivery, meta

    def test_complete_legacy_evidence_can_close_without_rewriting_history(self):
        delivery, delivery_meta = self.legacy_delivery()
        original = self.report.read_bytes()
        self.assertEqual(check_delivery(delivery, self.repo, self.ledger), 1)
        self.assertEqual(self.report.read_bytes(), original)
        with self.assertRaisesRegex(InvalidRecord, "历史报告"):
            check_report(self.report, self.ledger)
        for field in ("executing_task", "implementation_task", "reviewed_commit"):
            with self.subTest(field=field):
                meta = {**self.meta, field: "unknown"}
                self.write_report(meta)
                with self.assertRaises(InvalidRecord):
                    check_delivery(delivery, self.repo, self.ledger)
        for extra in ({"authority_status": "superseded"},
                      {"evidence_correction_status": "pending_targeted_verification"}):
            self.write_report({**self.meta, **extra})
            with self.assertRaisesRegex(InvalidRecord, "取代|补核"):
                check_delivery(delivery, self.repo, self.ledger)
        self.write_report({**self.meta, "implementation_task": "unknown"})
        self.write_report({**delivery_meta, "executing_task": "unknown"}, delivery)
        with self.assertRaisesRegex(InvalidRecord, "身份未知"):
            check_delivery(delivery, self.repo, self.ledger)

    def test_explicit_legacy_mapping_requires_bound_report_and_evidence(self):
        delivery, meta = self.legacy_delivery()
        resolved = {key: self.meta[key] for key in ("executing_task", "implementation_task", "reviewed_commit")}
        self.meta.update(executing_task="unknown", implementation_task="unknown", reviewed_commit="旧记录文本")
        self.write_report()
        original = self.report.read_bytes()
        evidence = self.root / "evidence.txt"
        evidence.write_text("测试自有任务与原始对象比对证据；只供合成夹具", encoding="utf-8")
        mapping = {**resolved, "review_report_sha256": hashlib.sha256(original).hexdigest(),
                   "identity_evidence": evidence.name, "object_evidence": evidence.name}
        path = self.root / "legacy_mapping.json"
        path.write_text(json.dumps(mapping), encoding="utf-8")
        meta["legacy_review_mapping"] = path.name
        self.write_report(meta, delivery)
        self.assertEqual(check_delivery(delivery, self.repo, self.ledger), 1)
        self.assertEqual(self.report.read_bytes(), original)
        mapping["review_report_sha256"] = "0" * 64
        path.write_text(json.dumps(mapping), encoding="utf-8")
        with self.assertRaisesRegex(InvalidRecord, "旧报告.*指纹"):
            check_delivery(delivery, self.repo, self.ledger)
        mapping["review_report_sha256"] = hashlib.sha256(original).hexdigest()
        mapping["identity_evidence"] = "missing.txt"
        path.write_text(json.dumps(mapping), encoding="utf-8")
        with self.assertRaises(InvalidRecord):
            check_delivery(delivery, self.repo, self.ledger)

    def test_delivery_requires_same_reviewed_tree_and_original_a(self):
        reviewed = self.commit(b"one\r\n")
        checkpoint = self.commit(b"one\r\n", parent=reviewed)
        record = manifest_record(checkpoint, {"alpha.txt": b"one\r\n"})
        record.update(checkpoint_kind="development", backup_status="verified",
                      remote_retrieved_commit=checkpoint, verified_at="2026-10-15")
        (self.root / "backup.json").write_text(json.dumps(record), encoding="utf-8")
        self.meta["reviewed_commit"] = reviewed
        self.write_report()
        delivery = self.root / "11_交付收尾.md"
        meta = {**self.meta, "executing_role": "A", "executing_task": "implementation-task-001",
                "entry_mode": "review_result_return",
                "workflow_status": "delivery_complete", "next_action": "stop", "delivery_status": "complete",
                "handoff_file": str(delivery), "review_report": self.report.name, "backup_record": "backup.json"}
        del meta["repair_verification_status"]
        self.write_report(meta, delivery)
        self.assertEqual(check_delivery(delivery, self.repo, self.ledger), 1)
        meta["executing_task"] = self.meta["executing_task"]
        self.write_report(meta, delivery)
        with self.assertRaisesRegex(InvalidRecord, "B 不得"):
            check_delivery(delivery, self.repo, self.ledger)
        meta["executing_task"] = "implementation-task-001"
        self.write_report(meta, delivery)
        record["backup_commit"] = self.commit(b"changed")
        record["remote_retrieved_commit"] = record["backup_commit"]
        (self.root / "backup.json").write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(InvalidRecord, "源码树"):
            check_delivery(delivery, self.repo, self.ledger)


if __name__ == "__main__":
    unittest.main()
