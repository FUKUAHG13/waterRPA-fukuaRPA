"""只读核对审查记录、测试摘要、原始备份和交付；不替代独立复核。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path, PurePosixPath

import yaml

REVISION = "2026-10-15"
VERIFICATION = {"draft", "partially_verified", "verified", "blocked"}
AUTHORITY = {"working", "canonical", "superseded"}
ENUMS = {
    "delivery_status": {"pending", "complete", "blocked", "local_only"},
    "required_gate_status": {"not_evaluated", "passed", "blocked"},
    "release_readiness": {"not_evaluated", "blocked", "ready"},
    "independent_review_execution_status": {
        "not_started", "pending_init", "running", "completed", "blocked",
        "errored", "interrupted", "shutdown", "not_found",
    },
    "repair_verification_status": {"passed", "changes_required", "blocked"},
    "rework_verification_status": {"passed", "changes_required", "factual_dispute", "blocked", "maintainer_decision_required"},
}
RESULT_ROUTES = {
    "repair_verification_status": {
        "passed": {"repair_verification_passed"}, "changes_required": {"repair_changes_required"},
        "blocked": {"repair_verification_blocked", "maintainer_decision_required"},
    },
    "rework_verification_status": {
        "passed": {"rework_verification_passed"}, "changes_required": {"rework_changes_required"},
        "factual_dispute": {"rework_factual_dispute"}, "blocked": {"rework_verification_blocked"},
        "maintainer_decision_required": {"maintainer_decision_required"},
    },
}
ACTIONS = {"stop", "maintainer_decision", "environment_required", "delivery_closeout"} | {
    f"prompt_{n}" for n in ("2", "3", "4", "4b", "5", "6", "7", "8", "9", "10", "10b")
}
ROUTES = {
    "draft_created": {"prompt_2"}, "independent_review_complete": {"prompt_3"},
    "independent_review_blocked": {"environment_required", "maintainer_decision"},
    "review_corrections_accepted": {"prompt_5"}, "review_disputes_remaining": {"prompt_4"},
    "review_disputes_resolved": {"prompt_5"}, "review_disputes_unresolved": {"prompt_4b", "environment_required"},
    "third_party_review_resolved": {"prompt_5"}, "maintainer_decision_required": {"maintainer_decision"},
    "canonical_report_ready": {"prompt_6"}, "implementation_plan_ready": {"prompt_7", "maintainer_decision"},
    "implementation_waiting_for_verification": {"prompt_8"},
    "repair_verification_passed": {"delivery_closeout"}, "repair_changes_required": {"prompt_9"},
    "repair_verification_blocked": {"environment_required", "maintainer_decision"},
    "rework_submitted": {"prompt_10", "environment_required", "maintainer_decision"},
    "rework_verification_passed": {"delivery_closeout"}, "rework_changes_required": {"prompt_9"},
    "rework_verification_blocked": {"environment_required", "maintainer_decision"},
    "rework_factual_dispute": {"prompt_10b", "environment_required"}, "rework_dispute_resolved": {"prompt_9"},
    "delivery_complete": {"stop"}, "delivery_blocked": {"environment_required"}, "delivery_local_only": {"stop"},
}
PENDING_FIELDS = (
    "transferred_ids", "pending_risk_ids", "pending_decision_ids",
    "pending_suggestion_ids", "pending_evidence_ids", "pending_release_gate_ids",
)
EMPTY_WHEN_PASSED = (
    "unmet_required_gate_ids", "blocking_rework_ids", "changes_required_ids",
    "new_regression_ids", "blocked_ids", "unresolved_dispute_ids",
)
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b")


class InvalidRecord(ValueError):
    """记录不能支持其声明。"""


class UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise InvalidRecord("元数据键必须是唯一字符串")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def require(condition, message):
    if not condition:
        raise InvalidRecord(message)


def read_json(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "JSON 记录有重复键")
            result[key] = value
        return result

    result = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique_pairs)
    require(isinstance(result, dict), "JSON 记录必须为对象")
    return result


def read_report(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    match = re.search(r"^```(?:yaml|json)\s*\n(.*?)^```\s*$", text, re.M | re.S)
    require(match is not None, "缺少文件头 YAML/JSON 元数据")
    metadata = yaml.load(match[1], Loader=UniqueLoader)
    require(isinstance(metadata, dict), "元数据必须为对象")
    return metadata, text


def ledger_ids(path):
    # SAFETY: 只有台账行的首列是登记；正文提到某 ID 不等于该项已承接。
    result = set()
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("|"):
            result.update(ID.findall(line.split("|", 2)[1]))
    return result


def _ids(metadata, field):
    values = metadata.get(field, [])
    require(isinstance(values, list) and all(isinstance(x, str) and ID.fullmatch(x) for x in values),
            f"{field} 必须是展开后的 ID 数组")
    require(len(set(values)) == len(values), f"{field} 有重复 ID")
    return set(values)


def check_report(path, ledger=None):
    return _check_report(path, ledger)


def _check_report(path, ledger=None, *, legacy=False, mapping=None):
    metadata, text = read_report(path)
    revision = str(metadata.get("review_rules_revision", ""))
    if legacy:
        require(date.fromisoformat(revision) < date.fromisoformat(REVISION), "旧证据版本无效")
        if mapping is not None:
            metadata.update(mapping)
    else:
        require(revision == REVISION, "此工具检查 2026-10-15 新记录；历史报告不据此追溯作废")
    for field in ("verification_status", "authority_status"):
        if not legacy and metadata.get("report_schema") == "fukuarpa-review-v3":
            require(field in metadata, f"缺少 {field}")
        if field in metadata:
            require(metadata[field] in (VERIFICATION if field == "verification_status" else AUTHORITY),
                    f"{field} 枚举无效")
    action = metadata.get("next_action")
    require(action in ACTIONS, "next_action 无效")
    require(metadata.get("workflow_schema") == "fukuarpa-review-flow-v2", "workflow_schema 无效")
    status = metadata.get("workflow_status")
    if legacy:
        require(status in {"repair_verification_passed", "rework_verification_passed"} and action == "stop",
                "旧交付依据必须为原有 passed/stop，不能借承接改判")
    else:
        require(status in ROUTES and action in ROUTES[status], "workflow_status 与 next_action 路由不符")
        for field, choices in ENUMS.items():
            if field in metadata:
                require(isinstance(metadata[field], str) and metadata[field] in choices, f"{field} 枚举无效")
        c_arbitration = metadata.get("executing_role") == "C" and metadata.get("workflow_stage") == "rework_arbitration"
        if metadata.get("workflow_stage") == "rework_arbitration" or (
            metadata.get("executing_role") == "C" and status in {"rework_factual_dispute", "rework_dispute_resolved"}
        ):
            require(c_arbitration, "C 裁决角色与阶段不符")
        if c_arbitration:
            # DESIGN: 10B 受阻沿用争议 workflow_status，但 C 必须用自己的裁决结果，不能冒填 B 复验字段。
            outcomes = {
                "action_required": ("rework_dispute_resolved", "prompt_9", "action_required_ids"),
                "no_action_required": ("rework_dispute_resolved", "prompt_9", "no_action_required_ids"),
                "blocked": ("rework_factual_dispute", "environment_required", "blocked_ids"),
                "maintainer_decision_required": ("maintainer_decision_required", "maintainer_decision", "maintainer_decision_ids"),
            }
            outcome = metadata.get("arbitration_status")
            require(isinstance(outcome, str) and outcome in outcomes, "C 裁决结果枚举无效")
            require(not any(field in metadata for field in RESULT_ROUTES), "C 裁决不得冒填 B 复验字段")
            expected_status, expected_action, ids_field = outcomes[outcome]
            require((status, action) == (expected_status, expected_action), "C 裁决结果与路由不符")
            for field in ("action_required_ids", "no_action_required_ids", "blocked_ids", "maintainer_decision_ids"):
                _ids(metadata, field)
            require(_ids(metadata, ids_field), "C 裁决缺少相应结果 ID")
        else:
            for field, routes in RESULT_ROUTES.items():
                if field in metadata:
                    require(status in routes[metadata[field]], f"{field} 与结果路由不符")
                for value, workflows in routes.items():
                    if status in workflows and status != "maintainer_decision_required":
                        require(metadata.get(field) == value, f"{field} 与结果路由不符")
        if status == "repair_changes_required":
            require(_ids(metadata, "blocking_rework_ids") or _ids(metadata, "new_regression_ids"), "返修路由缺少必须返修 ID")
        if status == "rework_changes_required":
            require(_ids(metadata, "changes_required_ids"), "返修路由缺少必须返修 ID")
        if status in {"repair_verification_blocked", "rework_verification_blocked"}:
            require(_ids(metadata, "blocked_ids") or _ids(metadata, "unmet_required_gate_ids"), "阻塞路由缺少阻塞 ID")
        if status == "rework_factual_dispute" and not c_arbitration:
            require(_ids(metadata, "unresolved_dispute_ids"), "争议路由缺少争议 ID")
        if metadata.get("required_gate_status") == "passed":
            require(not _ids(metadata, "unmet_required_gate_ids"), "门禁通过却仍含未满足的阻塞门禁")
    for field in ("workflow_status", "executing_role", "executing_task", "authorization_source"):
        require(isinstance(metadata.get(field), str) and metadata[field].strip(), f"缺少 {field}")
    require(metadata.get("entry_mode") in {"user_handoff", "same_role_continuation", "review_result_return"},
            "entry_mode 无效")
    if metadata["entry_mode"] == "review_result_return":
        require(metadata["executing_role"] == "A" and status.startswith("delivery_"), "复验回传模式仅供原 A 交付收尾")
    target = metadata.get("handoff_file")
    require(isinstance(target, str) and Path(target).is_absolute()
            and Path(target).resolve() == Path(path).resolve(), "handoff_file 未指向本文件")
    tail = [line.strip() for line in text.splitlines() if line.strip()][-3:]
    display = ("提示词 " + action.removeprefix("prompt_").upper()
               if action.startswith("prompt_") else action)
    require(tail == [f"流程状态：{metadata['workflow_status']}", f"下一步使用：{display}",
                     f"下一步交接文件：@file:{target}"], "文件尾三行与元数据不一致")
    pending = set().union(*(_ids(metadata, field) for field in PENDING_FIELDS))
    for field in EMPTY_WHEN_PASSED:
        _ids(metadata, field)
    passed = metadata.get("repair_verification_status") == "passed" or metadata.get("rework_verification_status") == "passed"
    for workflow, result_field in (("repair_verification_passed", "repair_verification_status"),
                                   ("rework_verification_passed", "rework_verification_status")):
        if status == workflow:
            require(metadata.get(result_field) == "passed", "通过路由缺少对应复验结果")
    if passed:
        require(status in {"repair_verification_passed", "rework_verification_passed"}, "复验通过状态与路由不符")
        if not legacy:
            require(action == "delivery_closeout" and metadata.get("delivery_status") == "pending",
                    "复验通过须回交原 A 收尾，不能直接声明交付完成/stop")
        require(metadata.get("required_gate_status") == "passed", "必需门禁未通过")
        require(not any(_ids(metadata, field) for field in EMPTY_WHEN_PASSED), "通过记录仍含阻塞/未通过项")
        require(metadata.get("release_readiness") in {"not_evaluated", "blocked", "ready"}, "缺少发布准备状态")
        require(metadata["release_readiness"] != "ready" or not _ids(metadata, "pending_release_gate_ids"),
                "发布 ready 仍有未完成门禁")
        require(metadata.get("executing_role") == "B"
                and bool(metadata.get("implementation_task"))
                and metadata["implementation_task"] != metadata["executing_task"],
                "通过记录须由独立 B 回交原实施 A")
        if not legacy:
            require(metadata.get("independent_review_execution_status") == "completed",
                    "通过记录缺少独立复核完成声明")
        else:
            require(bool(metadata.get("independent_review_identity_source")), "旧复验缺少身份依据")
    if pending:
        require(ledger is not None, "存在待办时必须提供统一清单")
        missing = pending - ledger_ids(ledger)
        require(not missing, "统一清单漏登：" + ", ".join(sorted(missing)))
    if not legacy and metadata.get("independent_review_execution_status") == "completed":
        require(bool(metadata.get("independent_review_identity_source")), "缺少可核对的独立任务身份来源")
        for parameter, expected, configured in (
            ("model", "gpt-6.1-sol", "model"),
            ("reasoning", "max", "reasoning_effort"),
            ("service_tier", "priority", "service_tier"),
        ):
            support = metadata.get(parameter + "_parameter_support")
            require(support in {"available", "unavailable", "unknown"}, f"缺少 {parameter} 参数可用性记录")
            if support == "available":
                require(metadata.get("independent_review_configured_" + configured) == expected,
                        f"可设置的 {parameter} 未按要求指定")
        actual = metadata.get("independent_review_model")
        require(actual in {"unknown", "gpt-6.1-sol"}, "实际模型缺失或与要求不一致")
    return metadata


def _git(repo, *args, data=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True,
                          check=True, timeout=30).stdout


def verify_backup(record, repo):
    commit = record.get("backup_commit")
    require(isinstance(commit, str) and COMMIT.fullmatch(commit), "备份提交必须为完整 40 位哈希")
    manifest = record.get("manifest")
    require(isinstance(manifest, list) and manifest, "原始清单为空或缺失")
    digest = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode("utf-8")).hexdigest()
    require(record.get("raw_manifest_sha256") == digest, "原始清单总指纹不一致")
    expected = {}
    folded = set()
    for row in manifest:
        require(isinstance(row, dict), "清单条目无效")
        path = row.get("path")
        require(isinstance(path, str) and path and "\\" not in path and ":" not in path
                and not PurePosixPath(path).is_absolute() and ".." not in PurePosixPath(path).parts,
                "清单路径必须为安全仓库相对路径")
        require(path.casefold() not in folded, "清单路径重复或大小写冲突")
        folded.add(path.casefold())
        require(type(row.get("size")) is int and row["size"] >= 0, "清单大小无效")
        require(isinstance(row.get("sha256"), str) and SHA256.fullmatch(row["sha256"]), "清单 SHA-256 无效")
        expected[path] = row
    entries = []
    for raw in _git(repo, "ls-tree", "-r", "-z", commit).split(b"\0"):
        if raw:
            meta, path = raw.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            require(kind == "blob" and mode == "100644", "备份含非普通文件对象")
            entries.append((path.decode("utf-8"), oid))
    require({p for p, _ in entries} == set(expected), "Git 树与原始清单文件集不一致")
    stream = io.BytesIO(_git(repo, "cat-file", "--batch", data="".join(oid + "\n" for _, oid in entries).encode()))
    for path, oid in entries:
        header = stream.readline().decode().split()
        require(len(header) == 3 and header[:2] == [oid, "blob"], "Git blob 响应无效")
        data = stream.read(int(header[2]))
        require(stream.read(1) == b"\n", "Git blob 响应截断")
        row = expected[path]
        require(len(data) == row["size"] and hashlib.sha256(data).hexdigest() == row["sha256"],
                f"原始字节不一致：{path}")
    require(not stream.read(), "Git blob 响应有多余内容")
    return len(entries)


def check_test_log(text, runner, exit_code, expected_exit=0):
    require(exit_code == expected_exit, "实际退出码与预期不一致")
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    require(text.strip(), "缺少实际测试输出")
    if runner == "unittest":
        ran = re.findall(r"^Ran (\d+) tests? in .+$", text, re.M)
        require(ran, "缺少 unittest 执行摘要")
        count = int(ran[-1])
        ok = re.search(r"^OK(?: \(([^\r\n]+)\))?\s*$", text, re.M)
        failed = re.search(r"^FAILED \(([^\n]+)\)\s*$", text, re.M)
        require(count > 0, "unittest 零收集")
        if expected_exit == 0:
            require(ok is not None and failed is None, "unittest 未通过")
            counts = {}
            for item in (ok[1] or "").split(", "):
                if not item:
                    continue
                match = re.fullmatch(r"(skipped|expected failures)=(\d+)", item)
                require(match is not None and match[1] not in counts, "unittest 摘要后缀无效")
                counts[match[1]] = int(match[2])
            require(sum(counts.values()) <= count, "unittest 摘要计数矛盾")
            require(count > counts.get("skipped", 0), "unittest 全部跳过")
        else:
            require(failed is not None and ok is None, "缺少预期失败证据")
    else:
        require(expected_exit != 0 or not re.search(r"SUBFAILED|\d+ subtests? failed", text),
                "存在子测试失败")
        summaries = [line for line in text.splitlines() if re.search(r"\bin \d+(?:\.\d+)?s\b", line)
                     and re.search(r"\d+ (?:passed|failed|errors?|skipped)", line)]
        require(summaries, "缺少 pytest 执行摘要")
        counts = {key: int(value) for value, key in re.findall(
            r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b", summaries[-1])}
        count = sum(counts.get(key, 0) for key in ("passed", "failed", "error", "errors"))
        require(count > 0, "pytest 零有效执行或全部跳过")
        failures = sum(counts.get(key, 0) for key in ("failed", "error", "errors"))
        require((expected_exit == 0 and failures == 0 and counts.get("passed", 0) > 0)
                or (expected_exit != 0 and failures > 0), "pytest 摘要与预期结果不一致")
    return count


def check_delivery(path, repo, ledger):
    metadata = check_report(path, ledger)
    require(metadata.get("delivery_status") == "complete" and metadata.get("next_action") == "stop",
            "仅校验 complete 交付；blocked/local_only 不得冒充 complete")
    require(metadata.get("workflow_status") == "delivery_complete", "交付路由无效")
    base = Path(path).resolve().parent
    review_path = base / metadata["review_report"]
    previous, _ = read_report(review_path)
    if str(previous.get("review_rules_revision", "")) == REVISION:
        require("legacy_review_mapping" not in metadata, "新报告不能使用旧证据映射覆盖身份或对象")
        review = check_report(review_path, ledger)
    else:
        mapping = None
        if "legacy_review_mapping" in metadata:
            mapping_path = base / metadata["legacy_review_mapping"]
            evidence = read_json(mapping_path)
            require(evidence.get("review_report_sha256") == hashlib.sha256(review_path.read_bytes()).hexdigest(),
                    "旧报告原始指纹不符")
            for field in ("identity_evidence", "object_evidence"):
                proof = evidence.get(field)
                require(isinstance(proof, str) and proof.strip(), "旧证据映射缺少身份/对象依据")
                require((mapping_path.parent / proof).is_file()
                        and bool((mapping_path.parent / proof).read_bytes().strip()), "旧证据映射依据缺失或为空")
            mapping = {field: evidence.get(field) for field in ("executing_task", "implementation_task", "reviewed_commit")}
            mapping["independent_review_identity_source"] = str(mapping_path)
        review = _check_report(review_path, ledger, legacy=True, mapping=mapping)
    require(review.get("repair_verification_status") == "passed" or review.get("rework_verification_status") == "passed",
            "原独立复验尚未通过")
    require(review.get("authority_status") != "superseded"
            and review.get("evidence_correction_status") != "pending_targeted_verification",
            "复验依据已被取代或仍待实质补核")
    require(metadata["executing_task"] != review["executing_task"], "B 不得代替 A 完成交付")
    for actor in (metadata["executing_task"], review["executing_task"], review.get("implementation_task")):
        require(isinstance(actor, str) and actor.strip().lower() not in {"", "unknown", "未知"},
                "原 A/B 身份未知，不能完成交付")
    require(metadata.get("executing_task") == review.get("implementation_task"), "交付任务不是原实施 A")
    backup = read_json(base / metadata["backup_record"])
    require(backup.get("checkpoint_kind") == "development" and backup.get("backup_status") == "verified",
            "缺少已核验开发检查点")
    require(backup.get("remote_retrieved_commit") == backup.get("backup_commit") and bool(backup.get("verified_at")),
            "缺少本次远端取回核验记录")
    reviewed = review.get("reviewed_commit")
    require(isinstance(reviewed, str) and COMMIT.fullmatch(reviewed), "缺少复验对象精确提交")
    checkpoint = backup["backup_commit"]
    require(_git(repo, "rev-parse", reviewed + "^{tree}") == _git(repo, "rev-parse", checkpoint + "^{tree}"),
            "完成检查点源码树与复验对象不同")
    return verify_backup(backup, repo)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="kind", required=True)
    report = sub.add_parser("report")
    report.add_argument("path", type=Path)
    report.add_argument("--ledger", type=Path)
    backup = sub.add_parser("backup")
    backup.add_argument("path", type=Path)
    backup.add_argument("--repo", type=Path, required=True)
    log = sub.add_parser("test-log")
    log.add_argument("path", type=Path)
    log.add_argument("--runner", choices=("pytest", "unittest"), required=True)
    log.add_argument("--exit-code", type=int, required=True)
    log.add_argument("--expected-exit", type=int, default=0)
    delivery = sub.add_parser("delivery")
    delivery.add_argument("path", type=Path)
    delivery.add_argument("--repo", type=Path, required=True)
    delivery.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.kind == "report":
            check_report(args.path, args.ledger)
        elif args.kind == "backup":
            verify_backup(read_json(args.path), args.repo)
        elif args.kind == "test-log":
            check_test_log(args.path.read_text(encoding="utf-8", errors="replace"), args.runner,
                           args.exit_code, args.expected_exit)
        else:
            check_delivery(args.path, args.repo, args.ledger)
    except (InvalidRecord, OSError, ValueError, KeyError, TypeError, yaml.YAMLError,
            subprocess.SubprocessError) as exc:
        print(f"校验未通过：{exc}", file=sys.stderr)
        return 1
    print("机械校验通过；身份、授权与证据真实性仍须独立核实。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
