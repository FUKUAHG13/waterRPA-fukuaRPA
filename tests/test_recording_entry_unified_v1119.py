# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.
"""v1.1.19：录制入口统一原样回放——record_btn 直达浮层、无模式窗口。"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fukua_rpa.ui.controllers import recording_controller as rc_mod  # noqa: E402
from fukua_rpa.ui.input_tools import RecorderUI  # noqa: E402
from tests.qt_test_support import QtCallbackGuardMixin, create_test_window  # noqa: E402

RAW_EVENTS = [
    [0.0, "move", 100, 100],
    [0.05, "mouse", "left", "down", 100, 100],
    [0.12, "mouse", "left", "up", 100, 100],
    [0.20, "wheel", 240, 200, 200],
]


class _FakeHook(QObject):
    finished_signal = Signal(object)
    ready_signal = Signal(bool, str)
    control_signal = Signal(str, object)

    def __init__(self, raw=None, *, truncated=False, unsupported=False):
        super().__init__()
        self.raw_input_events = list(raw if raw is not None else RAW_EVENTS)
        self.raw_events_truncated = truncated
        self.raw_unsupported = unsupported

    def set_capture_mode(self, _mode):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def wait(self, _ms):
        return True

    def isRunning(self):
        return False


class _StubRecorder:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.destroyed = _FakeSignal()

    def show(self):
        pass

    def close(self):
        pass


class _FakeSignal:
    def connect(self, _cb):
        pass


class RecordingEntryUnifiedTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_record_btn_opens_raw_overlay_without_mode_dialog(self):
        """真实 record_btn 点击：直接进入原样浮层，无模式选择窗口。"""
        window = create_test_window(self, tempfile.mkdtemp())
        self.addCleanup(window.close)
        self.app.processEvents()
        with mock.patch.object(rc_mod, "RecorderUI", _StubRecorder), \
             mock.patch.object(window, "showMinimized"), \
             mock.patch.object(window, "showNormal"):
            QTest.mouseClick(window.record_btn, Qt.LeftButton)
            self.app.processEvents()
            controller = window.recording_controller
            # 浮层由真实点击创建，且收到 mode="raw"（无模式选择窗口）
            recorder = controller.recorder
            self.assertIsInstance(recorder, _StubRecorder)
            self.assertEqual(recorder.kwargs["mode"], "raw")

    def test_no_mode_chooser_symbol_remains(self):
        self.assertFalse(hasattr(rc_mod, "choose_recording_mode"))

    def test_recorder_normalizes_legacy_steps_mode_to_raw(self):
        rec = RecorderUI(mode="steps", hook_factory=_FakeHook)
        self.addCleanup(rec.close)
        self.assertEqual(rec.mode, "raw")

    def test_fake_hook_full_chain_creates_raw_steps(self):
        """假 Hook 发信号走完整链，生成可编辑的原速步骤组。"""
        window = create_test_window(self, tempfile.mkdtemp())
        self.addCleanup(window.close)
        controller = rc_mod.RecordingController(window)
        before = window.snapshot_tasks()
        rec = RecorderUI(
            tasks_callback=controller._add_tasks,
            log_callback=window.append_log,
            restore_callback=window.showNormal,
            mode="raw",
            hook_factory=lambda: _FakeHook(),
        )
        try:
            rec.hook_thread = _FakeHook()
            rec.on_recorded([])
        finally:
            rec.close()
        after = window.snapshot_tasks()
        added = after[len(before):]
        self.assertGreater(len(added), 1)
        self.assertTrue(all(t.get("fidelity") == "raw" for t in added))
        group_ids = {g["id"] for t in added for g in t.get("editor_groups", [])}
        self.assertEqual(len(group_ids), 1)

    def test_truncated_and_unsupported_emit_no_steps(self):
        from tests.qt_test_support import dispose_widget
        for kwargs in (
            {"raw": RAW_EVENTS, "truncated": True},
            {"raw": RAW_EVENTS, "unsupported": True},
        ):
            with self.subTest(**kwargs):
                window = create_test_window(self, tempfile.mkdtemp())
                rec = RecorderUI(
                    main_window=window, mode="raw",
                    hook_factory=lambda k=kwargs: _FakeHook(**k),
                )
                rec.hook_thread = _FakeHook(**kwargs)
                rec.on_recorded([])
                self.assertEqual(len(window.snapshot_tasks()), 0)
                dispose_widget(self.app, rec)
                dispose_widget(self.app, window)


if __name__ == "__main__":
    unittest.main()
