"""Coverage tests for fukua_rpa.worker (FailsafeWatchdog + WorkerThread).

Uses mock/controlled threads to avoid real long waits and native dependencies.
All native calls (GetAsyncKeyState, GetForegroundWindow, pyautogui, MessageBeep)
are patched so tests run headless and fast.
"""

import time
import unittest
from unittest import mock

from fukua_rpa.worker import FailsafeWatchdog, WorkerThread


def _make_engine(**overrides):
    """Create a mock engine with sensible defaults for watchdog tests."""
    engine = mock.MagicMock()
    engine.enable_key_stop = overrides.get("enable_key_stop", True)
    engine.enable_tr_stop = overrides.get("enable_tr_stop", False)
    engine.enable_tm_stop = overrides.get("enable_tm_stop", False)
    engine.stop_requested = overrides.get("stop_requested", False)
    engine.log_level = overrides.get("log_level", 2)
    return engine


# ---------------------------------------------------------------------------
# FailsafeWatchdog.__init__ / kill
# ---------------------------------------------------------------------------

class TestWatchdogInit(unittest.TestCase):
    def test_init_sets_attributes(self):
        engine = _make_engine()
        wd = FailsafeWatchdog(engine, {"run_id": 42})
        self.assertIs(wd.engine, engine)
        self.assertEqual(wd.log_context, {"run_id": 42})
        self.assertFalse(wd.stop_event.is_set())
        self.assertTrue(wd.daemon)

    def test_init_default_log_context(self):
        engine = _make_engine()
        wd = FailsafeWatchdog(engine)
        self.assertEqual(wd.log_context, {})

    def test_kill_sets_stop_event(self):
        engine = _make_engine()
        wd = FailsafeWatchdog(engine)
        wd.kill()
        self.assertTrue(wd.stop_event.is_set())


# ---------------------------------------------------------------------------
# FailsafeWatchdog._key_stop_reason
# ---------------------------------------------------------------------------

class TestKeyStopReason(unittest.TestCase):
    @mock.patch("fukua_rpa.worker.GetAsyncKeyState")
    def test_esc_pressed(self, mock_key):
        mock_key.side_effect = lambda vk: 0x8000 if vk == 0x1B else 0
        wd = FailsafeWatchdog(_make_engine())
        reason = wd._key_stop_reason()
        self.assertIn("ESC", reason)

    @mock.patch("fukua_rpa.worker.GetAsyncKeyState")
    def test_middle_mouse_pressed(self, mock_key):
        mock_key.side_effect = lambda vk: 0x8000 if vk == 0x04 else 0
        wd = FailsafeWatchdog(_make_engine())
        reason = wd._key_stop_reason()
        self.assertIn("鼠标中键", reason)

    @mock.patch("fukua_rpa.worker.GetAsyncKeyState")
    def test_no_key_pressed(self, mock_key):
        mock_key.return_value = 0
        wd = FailsafeWatchdog(_make_engine())
        self.assertIsNone(wd._key_stop_reason())


# ---------------------------------------------------------------------------
# FailsafeWatchdog._corner_stop_reason
# ---------------------------------------------------------------------------

class TestCornerStopReason(unittest.TestCase):
    @mock.patch("fukua_rpa.worker.pyautogui")
    def test_in_corner(self, mock_pg):
        mock_pg.position.return_value = (1919, 2)
        mock_pg.size.return_value = (1920, 1080)
        wd = FailsafeWatchdog(_make_engine())
        reason = wd._corner_stop_reason()
        self.assertIn("右上角急停", reason)

    @mock.patch("fukua_rpa.worker.pyautogui")
    def test_not_in_corner(self, mock_pg):
        mock_pg.position.return_value = (500, 500)
        mock_pg.size.return_value = (1920, 1080)
        wd = FailsafeWatchdog(_make_engine())
        self.assertIsNone(wd._corner_stop_reason())


# ---------------------------------------------------------------------------
# FailsafeWatchdog._task_manager_stop_reason
# ---------------------------------------------------------------------------

class TestTaskManagerStopReason(unittest.TestCase):
    @mock.patch("fukua_rpa.worker.get_window_title")
    @mock.patch("ctypes.windll.user32.GetForegroundWindow")
    def test_task_manager_foreground(self, mock_hwnd, mock_title):
        mock_hwnd.return_value = 12345
        mock_title.return_value = "任务管理器"
        wd = FailsafeWatchdog(_make_engine())
        wd._next_task_manager_check = 0.0
        reason = wd._task_manager_stop_reason()
        self.assertIn("任务管理器", reason)

    @mock.patch("fukua_rpa.worker.get_window_title")
    @mock.patch("ctypes.windll.user32.GetForegroundWindow")
    def test_task_manager_english(self, mock_hwnd, mock_title):
        mock_hwnd.return_value = 12345
        mock_title.return_value = "Task Manager"
        wd = FailsafeWatchdog(_make_engine())
        wd._next_task_manager_check = 0.0
        reason = wd._task_manager_stop_reason()
        self.assertIn("任务管理器", reason)

    @mock.patch("fukua_rpa.worker.get_window_title")
    @mock.patch("ctypes.windll.user32.GetForegroundWindow")
    def test_other_window(self, mock_hwnd, mock_title):
        mock_hwnd.return_value = 99
        mock_title.return_value = "Notepad"
        wd = FailsafeWatchdog(_make_engine())
        wd._next_task_manager_check = 0.0
        self.assertIsNone(wd._task_manager_stop_reason())

    @mock.patch("fukua_rpa.worker.get_window_title")
    @mock.patch("ctypes.windll.user32.GetForegroundWindow")
    def test_empty_title(self, mock_hwnd, mock_title):
        mock_hwnd.return_value = 0
        mock_title.return_value = ""
        wd = FailsafeWatchdog(_make_engine())
        wd._next_task_manager_check = 0.0
        self.assertIsNone(wd._task_manager_stop_reason())

    @mock.patch("fukua_rpa.worker.get_window_title")
    @mock.patch("ctypes.windll.user32.GetForegroundWindow")
    def test_throttled_check(self, mock_hwnd, mock_title):
        mock_hwnd.return_value = 12345
        mock_title.return_value = "任务管理器"
        wd = FailsafeWatchdog(_make_engine())
        wd._next_task_manager_check = time.monotonic() + 10.0
        self.assertIsNone(wd._task_manager_stop_reason())


# ---------------------------------------------------------------------------
# FailsafeWatchdog._run_detector
# ---------------------------------------------------------------------------

class TestRunDetector(unittest.TestCase):
    @mock.patch("fukua_rpa.worker.write_log")
    def test_detector_fires(self, mock_log):
        engine = _make_engine()
        wd = FailsafeWatchdog(engine)
        wd.trigger_stop = mock.MagicMock()
        result = wd._run_detector("test", lambda: "some reason")
        self.assertTrue(result)
        wd.trigger_stop.assert_called_once_with("some reason")

    def test_detector_returns_none(self):
        wd = FailsafeWatchdog(_make_engine())
        result = wd._run_detector("test", lambda: None)
        self.assertFalse(result)

    @mock.patch("fukua_rpa.worker.write_log")
    def test_detector_exception_rate_limited(self, mock_wl):
        wd = FailsafeWatchdog(_make_engine())

        def boom():
            raise RuntimeError("fail")

        result = wd._run_detector("det", boom)
        self.assertFalse(result)
        # retry_after should be set
        self.assertIn("det", wd._retry_after)

    @mock.patch("fukua_rpa.worker.write_log")
    def test_detector_retry_not_yet(self, mock_wl):
        wd = FailsafeWatchdog(_make_engine())
        wd._retry_after["det"] = time.monotonic() + 100.0
        result = wd._run_detector("det", lambda: "reason")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# FailsafeWatchdog.trigger_stop
# ---------------------------------------------------------------------------

class TestTriggerStop(unittest.TestCase):
    @mock.patch("ctypes.windll.user32.MessageBeep")
    @mock.patch("fukua_rpa.worker.write_log")
    def test_trigger_stop_calls_engine(self, mock_wl, mock_beep):
        engine = _make_engine(stop_requested=False)
        wd = FailsafeWatchdog(engine)
        wd.trigger_stop("test reason")
        engine.log.assert_called_once()
        engine.stop.assert_called_once()

    @mock.patch("ctypes.windll.user32.MessageBeep")
    @mock.patch("fukua_rpa.worker.write_log")
    def test_trigger_stop_noop_if_already_stopped(self, mock_wl, mock_beep):
        engine = _make_engine(stop_requested=True)
        wd = FailsafeWatchdog(engine)
        wd.trigger_stop("test reason")
        engine.stop.assert_not_called()

    @mock.patch("ctypes.windll.user32.MessageBeep", side_effect=OSError("beep fail"))
    @mock.patch("fukua_rpa.worker.write_log")
    def test_trigger_stop_beep_failure_tolerated(self, mock_wl, mock_beep):
        engine = _make_engine(stop_requested=False)
        wd = FailsafeWatchdog(engine)
        # Should not raise
        wd.trigger_stop("reason")
        engine.stop.assert_called_once()


# ---------------------------------------------------------------------------
# FailsafeWatchdog.run (controlled loop)
# ---------------------------------------------------------------------------

class TestWatchdogRun(unittest.TestCase):
    @mock.patch("fukua_rpa.worker.write_log")
    def test_run_exits_on_stop_event(self, mock_wl):
        engine = _make_engine(enable_key_stop=False, enable_tr_stop=False, enable_tm_stop=False)
        wd = FailsafeWatchdog(engine)
        # Pre-set stop so loop exits immediately
        wd.stop_event.set()
        wd.run()
        # Should complete without hanging

    @mock.patch("fukua_rpa.worker.GetAsyncKeyState")
    @mock.patch("fukua_rpa.worker.write_log")
    @mock.patch("ctypes.windll.user32.MessageBeep")
    def test_run_triggers_on_key(self, mock_beep, mock_wl, mock_key):
        mock_key.side_effect = lambda vk: 0x8000 if vk == 0x1B else 0
        engine = _make_engine(enable_key_stop=True, stop_requested=False)
        wd = FailsafeWatchdog(engine)
        wd.run()
        engine.stop.assert_called_once()


# ---------------------------------------------------------------------------
# WorkerThread
# ---------------------------------------------------------------------------

class TestWorkerThread(unittest.TestCase):
    def _make_worker(self, engine=None, tasks=None, run_id=1):
        if engine is None:
            engine = mock.MagicMock()
            engine.active_run_matches.return_value = True
        if tasks is None:
            tasks = [{"step": 1}]
        with mock.patch.object(WorkerThread, "__init__", lambda self, *a, **kw: None):
            w = WorkerThread.__new__(WorkerThread)
        w.engine = engine
        w.tasks = tasks
        w.run_id = run_id
        w.log_context = {}
        w.watchdog = None
        return w

    @mock.patch("fukua_rpa.worker.FailsafeWatchdog")
    @mock.patch("fukua_rpa.worker.write_log")
    def test_run_success_path(self, mock_wl, mock_wd_cls):
        engine = mock.MagicMock()
        engine.active_run_matches.return_value = True
        w = self._make_worker(engine=engine)
        # Mock signals
        w.log_signal = mock.MagicMock()
        w.status_signal = mock.MagicMock()
        w.click_signal = mock.MagicMock()
        w.debug_signal = mock.MagicMock()
        w.prompt_signal = mock.MagicMock()
        w.error_signal = mock.MagicMock()

        mock_wd_instance = mock.MagicMock()
        mock_wd_cls.return_value = mock_wd_instance

        w.run()

        engine.run_tasks.assert_called_once()
        mock_wd_instance.start.assert_called_once()
        mock_wd_instance.kill.assert_called_once()
        mock_wd_instance.join.assert_called_once()
        engine.finish_run.assert_called_once_with(1)

    @mock.patch("fukua_rpa.worker.safe_exception_report")
    @mock.patch("fukua_rpa.worker.private_error_text")
    @mock.patch("fukua_rpa.worker.safe_error_name")
    @mock.patch("fukua_rpa.worker.FailsafeWatchdog")
    @mock.patch("fukua_rpa.worker.write_log")
    def test_run_exception_path(self, mock_wl, mock_wd_cls, mock_name, mock_priv, mock_report):
        engine = mock.MagicMock()
        engine.active_run_matches.return_value = False
        engine.run_tasks.side_effect = RuntimeError("boom")
        mock_name.return_value = "RuntimeError"
        mock_priv.return_value = "boom"
        mock_report.return_value = "traceback..."

        w = self._make_worker(engine=engine)
        w.log_signal = mock.MagicMock()
        w.status_signal = mock.MagicMock()
        w.click_signal = mock.MagicMock()
        w.debug_signal = mock.MagicMock()
        w.prompt_signal = mock.MagicMock()
        w.error_signal = mock.MagicMock()

        mock_wd_instance = mock.MagicMock()
        mock_wd_cls.return_value = mock_wd_instance

        w.run()

        w.error_signal.emit.assert_called_once_with("RuntimeError")
        engine.finish_run.assert_called_once_with(1, "worker_error", "traceback...")

    @mock.patch("fukua_rpa.worker.GLOBAL_CONFIG", {"log_to_ui": True})
    def test_log_callback_emits_when_enabled(self):
        w = self._make_worker()
        w.log_signal = mock.MagicMock()
        w.log_callback("hello")
        w.log_signal.emit.assert_called_once_with("hello")

    @mock.patch("fukua_rpa.worker.GLOBAL_CONFIG", {"log_to_ui": False})
    def test_log_callback_suppressed_when_disabled(self):
        w = self._make_worker()
        w.log_signal = mock.MagicMock()
        w.log_callback("hello")
        w.log_signal.emit.assert_not_called()

    def test_status_callback(self):
        w = self._make_worker()
        w.status_signal = mock.MagicMock()
        w.status_callback({"step": 2})
        w.status_signal.emit.assert_called_once_with({"step": 2})

    def test_click_callback(self):
        w = self._make_worker()
        w.click_signal = mock.MagicMock()
        w.click_callback({"x": 10, "y": 20})
        w.click_signal.emit.assert_called_once_with({"x": 10, "y": 20})

    def test_debug_callback(self):
        w = self._make_worker()
        w.debug_signal = mock.MagicMock()
        w.debug_callback({"paused": True})
        w.debug_signal.emit.assert_called_once_with({"paused": True})

    def test_prompt_callback(self):
        w = self._make_worker()
        w.prompt_signal = mock.MagicMock()
        req = object()
        w.prompt_callback(req)
        w.prompt_signal.emit.assert_called_once_with(req)


if __name__ == "__main__":
    unittest.main()
