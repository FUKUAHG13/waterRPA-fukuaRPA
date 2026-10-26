"""FR-b01-004 返修：MISSED_ASK 弹窗接线（_on_scheduled_missed_ask）的测试。

覆盖：主窗口可见且用户选择"补跑/跳过"时 resolve_missed_ask 被正确调用；
主窗口不可见（构造期/退出期）时不弹窗、按跳过处理。

REVIEW-20260928-001 REG-20260929-001 返修说明：`_on_scheduled_missed_ask`
自 FR-20260928-003 起经 `lifecycle.schedule_widget_callback` 接线（宿主必须是
QObject），因此宿主从纯 Python `_FakeHost` 升级为真实 QWidget（offscreen），
"立即执行延迟回调"改为真实事件循环冲刷（0ms 定时器）。三条断言语义
（可见性分流、Yes/No/跳过记账）逐条保留，未删除、未放宽。
"""

import os
import unittest
from types import MethodType
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget  # noqa: E402

from fukua_rpa.ui.main_window import RPAWindow  # noqa: E402


class _WidgetHost(QWidget):
    """真实 QWidget 宿主：满足 schedule_widget_callback 的 QObject 要求。

    可见性分流由真实 isVisible() 承担：show() 后可见；不 show 即不可见。
    """

    def __init__(self, visible: bool):
        super().__init__()
        self.scheduled_service = mock.Mock()
        self.scheduled_service.resolve_missed_ask = mock.Mock(return_value=True)
        if visible:
            self.show()


class MissedAskPromptTests(unittest.TestCase):
    """FR-b01-004: _on_scheduled_missed_ask 弹窗接线与跳过兜底。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _invoke(self, host, question_return=None, expect_question_called=True):
        bound = MethodType(RPAWindow._on_scheduled_missed_ask, host)
        with mock.patch.object(
            QMessageBox, "question", return_value=question_return
        ) as question:
            bound("sched-1", "测试任务")
            # schedule_widget_callback 以 0ms QTimer 接线延迟回调：
            # 冲刷事件循环使其到点执行（与生产事件循环语义一致）。
            self.app.processEvents()
        if expect_question_called:
            question.assert_called_once()
        return question

    def test_visible_window_yes_runs_catchup(self):
        host = _WidgetHost(visible=True)
        try:
            self._invoke(host, question_return=QMessageBox.Yes)
            host.scheduled_service.resolve_missed_ask.assert_called_once_with(
                "sched-1", True
            )
        finally:
            host.deleteLater()
            self.app.processEvents()

    def test_visible_window_no_skips(self):
        host = _WidgetHost(visible=True)
        try:
            self._invoke(host, question_return=QMessageBox.No)
            host.scheduled_service.resolve_missed_ask.assert_called_once_with(
                "sched-1", False
            )
        finally:
            host.deleteLater()
            self.app.processEvents()

    def test_invisible_window_skips_without_dialog(self):
        host = _WidgetHost(visible=False)
        try:
            question = self._invoke(
                host, question_return=None, expect_question_called=False
            )
            question.assert_not_called()
            host.scheduled_service.resolve_missed_ask.assert_called_once_with(
                "sched-1", False
            )
        finally:
            host.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
