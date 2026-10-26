# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Diagnostic report collection and asynchronous payload verification."""

from __future__ import annotations

import os
import time
import weakref
from dataclasses import asdict
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from ...config_store import atomic_write_json
from ...constants import APP_VERSION, BUILD_NAME
from ...diagnostics import run_runtime_diagnostics
from ...integrity import PAYLOAD_MANIFEST_NAME
from ..integrity_worker import PayloadVerificationThread

if TYPE_CHECKING:
    from .host_view import HostView


class _DiagnosticReportThread(QThread):
    """Runs the slow run_runtime_diagnostics probe off the UI thread.

    Mirrors PayloadVerificationThread: the heavy filesystem/native checks run
    in run() and the result is delivered back to the UI thread via a signal so
    the export dialog does not freeze.
    """

    report_ready = Signal(dict)
    failed = Signal(str)

    def __init__(self, base_dir, parent=None):
        super().__init__(parent)
        self.base_dir = base_dir

    def run(self):
        try:
            self.report_ready.emit(run_runtime_diagnostics(self.base_dir))
        except Exception as error:
            self.failed.emit(str(error))


class DiagnosticsController:
    def __init__(self, host: HostView):
        self._host_ref = weakref.ref(host)
        self.worker = None
        self.report_worker = None

    @property
    def host(self) -> HostView:
        host = self._host_ref()
        if host is None:
            raise RuntimeError("诊断窗口已销毁")
        return host

    def _collect_ui_report_portion(self):
        """Collect the UI/engine-derived fields of the diagnostic report.

        These values read live Qt/engine state and therefore must be gathered
        on the UI thread. The slow filesystem/native probe
        (run_runtime_diagnostics) is collected separately so the export can run
        it off the UI thread.
        """
        lifecycle = asdict(self.host.engine.lifecycle.snapshot())
        lifecycle_state = lifecycle.get("state", "idle")
        lifecycle["state"] = getattr(lifecycle_state, "value", str(lifecycle_state))
        lifecycle["last_error_present"] = bool(
            lifecycle.pop("last_error", "")
        )
        screens = []
        for screen in QApplication.screens():
            geometry = screen.geometry()
            screens.append(
                {
                    "name": screen.name(),
                    "geometry": [
                        geometry.x(), geometry.y(), geometry.width(), geometry.height()
                    ],
                    "device_pixel_ratio": screen.devicePixelRatio(),
                    "logical_dpi": round(screen.logicalDotsPerInch(), 2),
                }
            )
        current_config = self.host.get_current_ui_config()
        log_policy = self.host.current_log_policy()
        return {
            "application": {
                "version": APP_VERSION,
                "build_name": BUILD_NAME,
                "ui_scale_percent": round(self.host.ui_scale * 100.0, 1),
                "log_level": self.host.log_level_combo.currentText(),
                "log_mode": log_policy.mode,
                "log_categories": list(log_policy.enabled_categories),
                "task_count": len(current_config.get("tasks", [])),
                "mapping_count": len(current_config.get("key_mappings", [])),
            },
            "screens": screens,
            "lifecycle": lifecycle,
            "performance": {
                "engine_last_run": dict(self.host.engine.last_performance_report),
                "engine_current": self.host.engine.performance_snapshot(),
                "ui": self.host.ui_performance.snapshot(),
            },
            "runtime_trace": {
                "last_run": dict(self.host.engine.last_run_trace),
                "current": self.host.engine.run_trace.snapshot(),
            },
            "runner": self.host.run_controller.diagnostic_snapshot(),
            "privacy": {
                "script_content_included": False,
                "private_paths_included": False,
                "credential_values_included": False,
                "window_titles_included": False,
            },
        }

    def collect_diagnostic_report(self):
        # NOTE: The production export path (export_diagnostic_report below)
        # already runs run_runtime_diagnostics asynchronously in a background
        # QThread. This synchronous variant is kept for programmatic/test use.
        report = run_runtime_diagnostics(self.host.base_dir)
        report.update(self._collect_ui_report_portion())
        return report

    def export_diagnostic_report(self):
        default_name = f"{BUILD_NAME}_diagnostics_{time.strftime('%Y%m%d_%H%M%S')}.json"
        path, _selected_filter = QFileDialog.getSaveFileName(
            self.host.settings_interaction_parent(),
            "导出离线诊断",
            os.path.join(self.host.base_dir, default_name),
            "JSON 文件 (*.json)",
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        # Gather the fast UI/engine snapshot on the UI thread; the slow
        # runtime diagnostics (filesystem/native probes) run in a background
        # QThread below, mirroring PayloadVerificationThread, and the file is
        # written back on the UI thread once the report is ready.
        try:
            ui_portion = self._collect_ui_report_portion()
        except Exception as error:
            QMessageBox.warning(
                self.host.settings_interaction_parent(), "导出失败", str(error)
            )
            return
        worker = getattr(self, "report_worker", None)
        if worker is not None and worker.isRunning():
            return
        # INVARIANT: the background thread must NOT be parented to the host
        # window. A QThread parented to a widget is force-waited by that widget's
        # destructor, which would freeze application shutdown while the slow
        # diagnostics probe is still running. Running unparented lets the window
        # close immediately; the thread self-deletes via finished -> deleteLater.
        worker = _DiagnosticReportThread(self.host.base_dir)
        self.report_worker = worker
        worker.report_ready.connect(
            lambda runtime, target=path, ui=ui_portion: self._finish_diagnostic_export(
                target, runtime, ui
            )
        )
        worker.failed.connect(self._on_diagnostic_export_failed)
        worker.finished.connect(self._on_diagnostic_worker_finished)
        worker.start()

    def _finish_diagnostic_export(self, path, runtime_report, ui_portion):
        """Merge the background runtime report with the UI snapshot and write it."""
        try:
            host = self.host
        except RuntimeError:
            # Host window was closed while the background probe was running; the
            # report is discarded rather than touching a destroyed widget.
            return
        try:
            report = dict(runtime_report)
            report.update(ui_portion)
            atomic_write_json(path, report)
            QMessageBox.information(
                host.settings_interaction_parent(),
                "导出完成",
                "诊断报告已保存。报告不会联网，也不包含脚本步骤、图片路径或按键映射坐标。",
            )
        except Exception as error:
            QMessageBox.warning(
                host.settings_interaction_parent(), "导出失败", str(error)
            )

    def _on_diagnostic_export_failed(self, message):
        try:
            host = self.host
        except RuntimeError:
            return
        QMessageBox.warning(
            host.settings_interaction_parent(), "导出失败", str(message)
        )

    def _on_diagnostic_worker_finished(self):
        worker = getattr(self, "report_worker", None)
        self.report_worker = None
        if worker is not None:
            worker.deleteLater()

    def verify_installed_payload(self):
        manifest_path = os.path.join(self.host.base_dir, PAYLOAD_MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            QMessageBox.information(
                self.host.settings_interaction_parent(),
                "没有发布清单",
                "当前目录不是带完整性清单的正式发布包，无法执行程序文件校验。",
            )
            return
        worker = getattr(self, "worker", None)
        if worker and worker.isRunning():
            return
        self.host.integrity_btn.setEnabled(False)
        self.host.integrity_btn.setText("校验中…")
        # INVARIANT: run unparented so closing the window cannot block on this
        # thread (see export_diagnostic_report). The weakref-guarded wrappers
        # drop callbacks if the host is already gone, and finished -> deleteLater
        # guarantees cleanup even if the host closed first.
        worker = PayloadVerificationThread(self.host.base_dir)
        self.worker = worker
        worker.report_ready.connect(self._safe_integrity_report)
        worker.finished.connect(self._safe_integrity_finished)
        worker.start()

    def on_integrity_worker_finished(self):
        worker = self.worker
        self.worker = None
        self.host.integrity_btn.setEnabled(True)
        self.host.integrity_btn.setText("校验程序")
        if worker:
            worker.deleteLater()

    def _safe_integrity_report(self, report):
        # FR-20260812-D10-002: try 块必须包裹真实宿主访问；此前 try 内仅
        # pass，宿主销毁后 self.host 抛出的 RuntimeError 会从槽内逃逸。
        try:
            self.on_integrity_report(report)
        except RuntimeError:
            return

    def _safe_integrity_finished(self):
        try:
            self.on_integrity_worker_finished()
        except RuntimeError:
            return

    def on_integrity_report(self, report):
        if report.get("cancelled"):
            return
        missing = list(report.get("missing", []))
        mismatched = list(report.get("mismatched", []))
        suspicious = list(report.get("suspicious_unexpected", []))
        unexpected = list(report.get("unexpected", []))
        details = []
        if missing:
            details.append("缺失文件：\n" + "\n".join(missing))
        if mismatched:
            details.append("内容不一致：\n" + "\n".join(mismatched))
        if suspicious:
            details.append("清单外可执行文件：\n" + "\n".join(suspicious))
        if unexpected:
            details.append("其他清单外文件（通常是运行配置/日志）：\n" + "\n".join(unexpected))
        message = QMessageBox(self.host.settings_interaction_parent())
        message.setWindowTitle("程序文件校验")
        message.setStandardButtons(QMessageBox.Ok)
        if report.get("ok"):
            message.setIcon(QMessageBox.Information)
            message.setText(
                f"已核对 {report.get('checked', 0)} 个发布文件，没有发现缺失或损坏。"
            )
            message.setInformativeText(
                "内部哈希用于发现意外损坏；当前版本没有作者数字签名，不能据此证明发布者身份。"
            )
        else:
            message.setIcon(QMessageBox.Warning)
            message.setText("程序文件校验未通过。")
            message.setInformativeText(
                str(report.get("error") or "发现文件缺失、内容变化或额外可执行文件。")
            )
        if details:
            message.setDetailedText("\n\n".join(details))
        message.exec()
