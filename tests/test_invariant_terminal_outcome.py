"""Regression tests for invariant #73: terminal five-state outcome.

The Isolated Runner can report an explicit terminal outcome via
health_signal: finished / user_stopped / forced_stop / startup_failed /
crashed.  RunController must record that AUTHORITATIVE outcome in the run
result ``meta["outcome"]`` and must NOT fall back to the binary
success/failed heuristic when a runner outcome is present.

Only the outcome-preference decision lives in
``RunController._on_finished``; it is exercised directly (the engine/worker is
never started).  Qt is required only because RunController is a QObject, so
the whole module is skipped when PySide6 is unavailable.
"""

import unittest

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    raise unittest.SkipTest("PySide6 unavailable") from None


from fukua_rpa.ui.controllers.run_controller import RunController


class _RunEndedCapture:
    def __init__(self):
        self.meta = None

    def __call__(self, meta):
        self.meta = meta


class TerminalOutcomeInvariantTests(unittest.TestCase):
    """RunController must prefer the runner's explicit outcome over the
    binary success/failed heuristic."""

    @classmethod
    def setUpClass(cls):
        # DESIGN (全量套件崩溃修复): 不能在模块级创建 QCoreApplication 单例 ——
        # pytest 收集时导入本模块，若立即创建 QCoreApplication，后续需要
        # QApplication（GUI 版）的测试（如 test_advanced_image_actions）会因
        # 「已有 core app 单例」而进程级崩溃（exit 127；PySide6 中
        # QApplication.instance() 会返回该 core 实例且为 truthy，导致
        # `QApplication.instance() or QApplication([])` 拿不到 GUI app）。
        # 正确做法是**与全部 GUI 测试一致地创建 QApplication**（QApplication 是
        # QCoreApplication 子类，RunController 的 QTimer.singleShot 完全兼容），
        # 惰性创建并优先复用既有实例，避免提前创建任何 Qt 单例。
        cls._app = QApplication.instance() or QApplication([])

    def _run_and_capture(self, runner_outcome, run_failed=False):
        ctrl = RunController(engine=None)
        capture = _RunEndedCapture()
        ctrl.run_ended.connect(capture)
        ctrl._run_meta = {"profile_name": "demo"}
        ctrl._run_failed = run_failed
        ctrl._runner_outcome = runner_outcome
        # _on_finished only proceeds when the finished worker is the live one.
        dummy = object()
        ctrl.worker = dummy
        ctrl._on_finished(dummy)
        return ctrl, capture.meta

    def test_forced_stop_is_authoritative_over_failed_heuristic(self):
        ctrl, meta = self._run_and_capture("forced_stop", run_failed=True)
        self.assertEqual(meta["outcome"], "forced_stop")

    def test_startup_failed_is_authoritative(self):
        ctrl, meta = self._run_and_capture("startup_failed", run_failed=True)
        self.assertEqual(meta["outcome"], "startup_failed")

    def test_crashed_is_authoritative(self):
        ctrl, meta = self._run_and_capture("crashed", run_failed=False)
        self.assertEqual(meta["outcome"], "crashed")

    def test_finished_is_authoritative_even_when_run_failed_flag_set(self):
        ctrl, meta = self._run_and_capture("finished", run_failed=True)
        self.assertEqual(meta["outcome"], "finished")

    def test_user_stopped_is_authoritative(self):
        ctrl, meta = self._run_and_capture("user_stopped", run_failed=True)
        self.assertEqual(meta["outcome"], "user_stopped")

    def test_fallback_to_failed_when_runner_reports_nothing(self):
        ctrl, meta = self._run_and_capture(None, run_failed=True)
        self.assertEqual(meta["outcome"], "failed")

    def test_fallback_to_success_when_runner_reports_nothing(self):
        ctrl, meta = self._run_and_capture("", run_failed=False)
        self.assertEqual(meta["outcome"], "success")

    def test_runner_meta_is_preserved_in_result(self):
        ctrl, meta = self._run_and_capture("forced_stop")
        self.assertEqual(meta.get("profile_name"), "demo")


if __name__ == "__main__":
    unittest.main()
