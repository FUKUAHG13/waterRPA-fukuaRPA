# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Qt worker for responsive offline release verification."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..integrity import verify_payload


class PayloadVerificationThread(QThread):
    report_ready = Signal(dict)

    def __init__(self, root: str, parent=None):
        super().__init__(parent)
        self.root = Path(root)

    def run(self):
        try:
            report = verify_payload(
                self.root, cancelled=lambda: self.isInterruptionRequested()
            )
        except Exception as exc:
            report = {"ok": False, "error": str(exc), "checks": []}
        self.report_ready.emit(report)
