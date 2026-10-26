"""FR-20261023-STARTUP-B2-001：真实完成回调的导入失败与状态恢复。"""

import gc
import os
import threading
import unittest
import weakref
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QPushButton, QWidget
from shiboken6 import delete

from fukua_rpa.ui.controllers import feedback_controller as module
from tests.qt_test_support import dispose_widget
from tests.test_startup_lazy_runtime_modules import hide_module, restore_module

HIT = {
    "found": True, "error": "", "center_x": 40, "center_y": 50,
    "matched_width": 20, "matched_height": 30, "click_x": 42, "click_y": 53,
    "score": 0.96, "scale": 1.1, "elapsed_ms": 2.5,
}
TARGET = "fukua_rpa.ui.overlays"


class _Host(QWidget):
    def __init__(self):
        super().__init__()
        self.runtime_ready = True
        self.run_controller = SimpleNamespace(is_active=False)
        self.start_btn = QPushButton(self)
        self.single_step_btn = QPushButton(self)
        self.base_dir = os.getcwd()
        # 只替代识别引擎，不产生截图或输入；入口、配置、线程和完成信号均走真实实现。
        self.engine = mock.Mock()
        self.engine.recognize_task_once.return_value = dict(HIT)

    def get_current_ui_config(self):
        return {}


class _Row(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.recognition_test_btn = QPushButton(self)

    def get_data(self):
        return {"type": 1.0, "value": "10,20"}


class RecognitionCompletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = _Host()
        self.row = _Row(self.host)
        self.controller = module.VisualFeedbackController(self.host)
        self.previous = QWidget()
        self.previous.show()
        self.controller.recognition_test_overlay = self.previous
        self.addCleanup(dispose_widget, self.app, self.host)
        self.addCleanup(dispose_widget, self.app, self.previous)
        self.addCleanup(self.controller.close_all)

    def active_state(self):
        self.controller.recognition_test_active = True
        self.controller.recognition_test_thread = object()
        self.controller.recognition_test_cancel = threading.Event()
        self.controller.recognition_test_row = weakref.ref(self.row)
        self.row.recognition_test_btn.setEnabled(False)
        self.host.start_btn.setEnabled(False)
        self.host.single_step_btn.setEnabled(False)

    def assert_cleared(self, *, check_row=True):
        self.assertFalse(self.controller.recognition_test_active)
        for name in ("thread", "cancel", "row"):
            self.assertIsNone(getattr(self.controller, "recognition_test_" + name))
        if check_row:
            self.assertTrue(self.row.recognition_test_btn.isEnabled())
        enabled = self.host.runtime_ready and not self.host.run_controller.is_active
        self.assertEqual(self.host.start_btn.isEnabled(), enabled)
        self.assertEqual(self.host.single_step_btn.isEnabled(), enabled)

    def fail_hit_import(self):
        state = hide_module(TARGET, "fukua_rpa.ui")
        try:
            with (mock.patch.object(module, "logger") as logger,
                  mock.patch.object(module.QMessageBox, "warning") as warning,
                  mock.patch.object(module.QMessageBox, "information") as information,
                  mock.patch.object(self.previous, "close", wraps=self.previous.close) as close):
                with self.assertRaises(ImportError):
                    self.controller.on_task_image_recognition_finished(dict(HIT))
                logger.critical.assert_called_once()
                warning.assert_not_called()
                information.assert_not_called()
                close.assert_not_called()
        finally:
            restore_module(state, TARGET)
        self.assertIs(self.controller.recognition_test_overlay, self.previous)
        self.assertTrue(self.previous.isVisible())

    def test_import_failure_cleans_state_and_preserves_button_conditions(self):
        for ready in (False, True):
            for running in (False, True):
                with self.subTest(runtime_ready=ready, running=running):
                    self.host.runtime_ready = ready
                    self.host.run_controller.is_active = running
                    self.active_state()
                    self.fail_hit_import()
                    self.assert_cleared()

    def test_error_and_miss_do_not_import_overlays(self):
        for result in ({"error": "识别失败", "error_type": "ValueError", "found": True},
                       {"found": False, "elapsed_ms": 3.0}, None):
            with self.subTest(result=result):
                self.active_state()
                state = hide_module(TARGET, "fukua_rpa.ui")
                try:
                    with (mock.patch.object(module, "logger") as logger,
                          mock.patch.object(module, "write_log") as write_log,
                          mock.patch.object(module.QMessageBox, "warning") as warning,
                          mock.patch.object(module.QMessageBox, "information") as information):
                        self.controller.on_task_image_recognition_finished(result)
                        logger.critical.assert_not_called()
                        if result and result.get("error"):
                            warning.assert_called_once_with(self.host, "识别测试失败", "识别失败")
                            information.assert_not_called()
                            write_log.assert_called_once()
                        else:
                            warning.assert_not_called()
                            information.assert_called_once()
                            self.assertEqual(information.call_args.args[1], "未识别到目标")
                            write_log.assert_not_called()
                finally:
                    restore_module(state, TARGET)
                self.assert_cleared()
                self.assertIs(self.controller.recognition_test_overlay, self.previous)
                self.assertTrue(self.previous.isVisible())

    def test_normal_hit_preserves_overlay_values_and_replacement(self):
        self.active_state()
        self.controller.on_task_image_recognition_finished(dict(HIT))
        self.assert_cleared()
        self.assertFalse(self.previous.isVisible())
        overlay = self.controller.recognition_test_overlay
        self.assertIsNot(overlay, self.previous)
        self.assertTrue(overlay.isVisible())
        for key in ("center_x", "center_y", "matched_width", "matched_height", "click_x", "click_y", "score", "scale"):
            self.assertEqual(getattr(overlay, key), HIT[key])

    def test_restored_module_allows_same_controller_real_entry_and_completion(self):
        self.active_state()
        self.fail_hit_import()
        self.assert_cleared()
        self.controller.test_task_image_recognition(self.row)
        thread = self.controller.recognition_test_thread
        self.assertIsInstance(thread, threading.Thread)
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "识别替身必须有界结束")
        self.app.processEvents()
        self.host.engine.recognize_task_once.assert_called_once()
        self.assert_cleared()
        self.assertIsNot(self.controller.recognition_test_overlay, self.previous)
        self.assertTrue(self.controller.recognition_test_overlay.isVisible())

    def test_absent_expired_and_destroyed_row_do_not_block_cleanup(self):
        for kind in ("absent", "expired", "destroyed"):
            with self.subTest(kind=kind):
                self.active_state()
                if kind == "absent":
                    self.controller.recognition_test_row = None
                elif kind == "expired":
                    temporary = _Row(None)
                    self.controller.recognition_test_row = weakref.ref(temporary)
                    delete(temporary)
                    del temporary
                    gc.collect()
                    self.assertIsNone(self.controller.recognition_test_row())
                else:
                    temporary = _Row(self.host)
                    self.controller.recognition_test_row = weakref.ref(temporary)
                    delete(temporary)
                self.fail_hit_import()
                self.assert_cleared(check_row=False)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
