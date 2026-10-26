# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Workflow validation, static checks and loop-risk confirmation UI."""

from __future__ import annotations

import logging
import re
import weakref
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
)

from ...constants import TASK_TYPE_SECRET_TEXT
from ...credentials import CredentialStoreError
from ...profile_paths import resolve_task_image_paths
from ...run_config import EngineRunConfig, RunConfigError
from ...workflow_analysis import (
    analyze_loop_risks as analyze_workflow_loop_risks,
)
from ...workflow_analysis import (
    analyze_workflow_structure,
    risk_type_keys,
)

if TYPE_CHECKING:
    from .host_view import HostView


logger = logging.getLogger(__name__)


def _validation():
    """Return the step-validation layer, importing it on first use (ADR 0041).

    启动期不需要该层；失败时记 critical 留痕并原样抛出，不吞异常、不缓存失败，
    因此下一次调用会重新尝试导入。
    """

    try:
        from ... import validation
    except Exception as error:  # noqa: BLE001 - 留痕后原样抛出
        logger.critical("校验层导入失败：%s", error)
        raise
    return validation


class WorkflowReviewController:
    """Keep workflow correctness and risk messaging outside window assembly."""

    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("主窗口已销毁")
        return host

    def validate_global_settings(self, cfg=None):
        cfg = cfg or self.host.get_current_ui_config()
        try:
            EngineRunConfig.from_mapping(cfg)
        except RunConfigError as error:
            return str(error)
        return None

    def validate_tasks(self, tasks, cfg=None):
        validation = _validation()

        host = self.host
        try:
            prepared_tasks = resolve_task_image_paths(tasks, host.base_dir)
        except ValueError as path_error:
            return str(path_error)
        error = validation.validate_task_list(
            prepared_tasks, cfg or host.get_current_ui_config()
        )
        if error:
            return error
        credential_steps = []
        for index, task in enumerate(prepared_tasks):
            try:
                is_secret = float(task.get("type", 0)) == TASK_TYPE_SECRET_TEXT
            except (AttributeError, TypeError, ValueError):
                is_secret = False
            if is_secret:
                credential_steps.append(
                    (index + 1, str(task.get("value", "")).strip())
                )
        if not credential_steps:
            return None
        try:
            available = set(host.credentials.names())
        except CredentialStoreError as store_error:
            return f"凭据库无法读取：{store_error}"
        for step_no, name in credential_steps:
            if name not in available:
                return (
                    f"第 {step_no} 步引用的凭据“{name}”不存在。\n"
                    "请在设置 -> 凭据库中创建同名凭据后再运行。"
                )
        return None

    def validate_tasks_all(self, tasks, cfg=None):
        validation = _validation()

        host = self.host
        try:
            prepared_tasks = resolve_task_image_paths(tasks, host.base_dir)
        except ValueError as path_error:
            return [str(path_error)]
        errors = validation.validate_task_list_all(
            prepared_tasks, cfg or host.get_current_ui_config()
        )
        credential_steps = []
        for index, task in enumerate(prepared_tasks):
            try:
                is_secret = float(task.get("type", 0)) == TASK_TYPE_SECRET_TEXT
            except (AttributeError, TypeError, ValueError):
                is_secret = False
            if is_secret:
                credential_steps.append(
                    (index + 1, str(task.get("value", "")).strip())
                )
        if not credential_steps:
            return errors
        try:
            available = set(host.credentials.names())
        except CredentialStoreError as store_error:
            errors.append(f"凭据库无法读取：{store_error}")
            return errors
        for step_no, name in credential_steps:
            if name not in available:
                errors.append(
                    f"第 {step_no} 步引用的凭据“{name}”不存在。\n"
                    "请在设置 -> 凭据库中创建同名凭据后再运行。"
                )
        return errors

    def script_check_report(self, tasks, cfg):
        syntax_errors = self.validate_tasks_all(tasks, cfg)
        try:
            start_step = max(1, int(float(cfg.get("start_step", 1))))
        except (TypeError, ValueError, OverflowError):
            # REVIEW-20260913-001 FR-002: "inf"/"1e999" 经 float() 通过但 int() 抛
            # OverflowError；此前未捕获会让静态检查报告整体失败。
            start_step = 1
        structure = analyze_workflow_structure(tasks, start_step=start_step)
        risks = self.analyze_loop_risks(tasks, cfg) if not syntax_errors else []
        return {
            "syntax_error": syntax_errors[0] if syntax_errors else "",
            "syntax_errors": syntax_errors,
            "structure": [
                {
                    "severity": issue.severity,
                    "code": issue.code,
                    "message": issue.message,
                    "step": issue.step,
                }
                for issue in structure
            ],
            "loop_risks": list(risks),
        }

    def show_script_check_report(self):
        host = self.host
        cfg = host.get_current_ui_config()
        tasks = cfg.get("tasks", [])
        if not tasks:
            QMessageBox.information(host, "脚本检查", "当前方案没有步骤。")
            return
        report = self.script_check_report(tasks, cfg)
        findings = []
        for syntax_error in report.get("syntax_errors", []):
            match = re.search(r"第\s*(\d+)\s*步", syntax_error)
            findings.append(
                {
                    "severity": "error",
                    "message": syntax_error,
                    "step": int(match.group(1)) if match else None,
                }
            )
        findings.extend(report["structure"])
        for risk in report["loop_risks"]:
            match = re.search(r"第\s*(\d+)\s*步", risk)
            findings.append(
                {
                    "severity": "warning",
                    "message": risk,
                    "step": int(match.group(1)) if match else None,
                }
            )
        if not findings:
            QMessageBox.information(
                host,
                "脚本检查通过",
                f"已检查 {len(tasks)} 个步骤，未发现语法错误、不可到达步骤或明显循环风险。",
            )
            return

        dialog = QDialog(host)
        dialog.setWindowTitle("脚本检查结果")
        dialog.resize(780, 460)
        host.apply_ui_scale_to_widget(dialog)
        layout = QVBoxLayout(dialog)
        error_count = sum(
            item.get("severity") == "error" for item in findings
        )
        summary = QLabel(
            f"共发现 {len(findings)} 项，其中 {error_count} 项会阻止运行。"
            "双击带步号的项目可直接定位。"
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)
        finding_list = QListWidget()
        finding_list.setAlternatingRowColors(True)
        layout.addWidget(finding_list, 1)
        detail = QLabel("")
        detail.setWordWrap(True)
        detail.setProperty("role", "muted")
        layout.addWidget(detail)

        for finding in findings:
            severity = (
                "错误" if finding.get("severity") == "error" else "提示"
            )
            step = finding.get("step")
            location = f"第 {step} 步" if step else "全局"
            item = QListWidgetItem(
                f"[{severity}] [{location}] {finding.get('message', '')}"
            )
            item.setData(Qt.UserRole, step or 0)
            item.setToolTip("双击定位到该步骤" if step else "此项涉及整个脚本")
            finding_list.addItem(item)

        def show_finding_detail(item):
            detail.setText(item.text() if item else "")

        def locate_finding(item=None):
            selected = item or finding_list.currentItem()
            step = int(selected.data(Qt.UserRole) or 0) if selected else 0
            if not 1 <= step <= host.task_list.count():
                return
            target = host.task_list.item(step - 1)
            host.task_list.setCurrentItem(target)
            host.task_list.scrollToItem(target)
            host.raise_()
            host.activateWindow()
            dialog.accept()

        finding_list.currentItemChanged.connect(
            lambda current, _previous: show_finding_detail(current)
        )
        finding_list.itemDoubleClicked.connect(locate_finding)
        if finding_list.count():
            finding_list.setCurrentRow(0)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        locate_button = buttons.addButton(
            "定位到步骤", QDialogButtonBox.ActionRole
        )
        locate_button.clicked.connect(lambda: locate_finding())
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def analyze_loop_risks(self, tasks, cfg):
        return analyze_workflow_loop_risks(
            tasks, cfg, self.host.engine.get_cmd_name
        )

    def _current_profile_id(self):
        model = self.host.profile_model
        name = model.current_name
        profile_id = model.profile_id_of(name)
        return profile_id or name or ""

    def _risk_ack_key(self, profile_id):
        return f"risk_tip_acked_{profile_id}"

    def _acked_risk_types(self, profile_id):
        raw = self.host.settings.value(
            self._risk_ack_key(profile_id), "", type=str
        )
        return {t for t in str(raw).split(",") if t}

    def _mark_risk_types_acked(self, profile_id, types):
        merged = self._acked_risk_types(profile_id) | set(types)
        self.host.settings.setValue(
            self._risk_ack_key(profile_id), ",".join(sorted(merged))
        )
        self.host.settings.sync()

    def confirm_loop_risks(self, tasks, cfg, *, profile_id=None):
        """Prompt for loop risks, deduplicating per *profile_id*.

        Args:
            tasks: Tasks about to run.
            cfg: Their configuration.
            profile_id: Profile the acknowledgement belongs to. Callers that run
                something other than the currently edited profile — the
                multi-profile hotkey path — must pass it, otherwise the
                acknowledgement is recorded against the wrong profile.
        """

        # Persistent setting: user can disable risk tips entirely.
        if not self.host.settings.value("risk_tip_enabled", True, type=bool):
            return True

        risks = self.analyze_loop_risks(tasks, cfg)
        if not risks:
            return True

        # Per-profile, persistent dedupe: only prompt when at least one risk
        # *type* has not been acknowledged for this profile. Acknowledged
        # types never re-prompt, even after restart or parameter edits; a
        # genuinely new risk type re-prompts (design request).
        current_types = risk_type_keys(risks)
        # 显式传入优先；缺省才回落到当前编辑方案。
        profile_id = profile_id if profile_id is not None else self._current_profile_id()
        if current_types <= self._acked_risk_types(profile_id):
            return True

        preview = "\n".join(risks[:8])
        if len(risks) > 8:
            preview += f"\n……另有 {len(risks) - 8} 条风险未显示。"
        # 反馈项：风险摘要下方常显永久关闭入口，继续运行/取消运行的行为不变。
        close_hint = (
            "如需永久关闭此提示：设置 → 高级模式 → 运行范围与流程 → "
            "关闭“运行前风险提示”。"
        )

        message = QMessageBox(self.host)
        message.setIcon(QMessageBox.Warning)
        message.setWindowTitle("可能存在无限循环/等待")
        message.setText("检测到当前方案可能长时间停在某一步或循环执行。")
        message.setInformativeText(f"{preview}\n\n{close_hint}")
        message.setDetailedText("\n".join(risks))
        message.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        message.button(QMessageBox.Ok).setText("我知道了，继续运行")
        message.button(QMessageBox.Cancel).setText("取消运行")
        result = message.exec()
        if result == QMessageBox.Ok:
            # Record every type seen this run so only genuinely new types
            # prompt on subsequent runs.
            self._mark_risk_types_acked(profile_id, current_types)
        return result == QMessageBox.Ok
