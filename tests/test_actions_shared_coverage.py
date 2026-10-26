"""Targeted coverage tests for fukua_rpa/actions/shared.py uncovered paths."""

import threading
import unittest
from unittest import mock

from fukua_rpa.actions.shared import (
    ACTION_HANDLERS,
    ActionsSharedMixin,
    _parse_jitter_value,
    _parse_optional_float,
    action_handler,
    activate_window_for_click,
    root_window_at_point,
    uipi_blocks_input,
)

# ---------------------------------------------------------------------------
# _parse_jitter_value (lines 102-111)
# ---------------------------------------------------------------------------


class TestParseJitterValue(unittest.TestCase):
    def test_valid_float(self):
        self.assertAlmostEqual(_parse_jitter_value("1.5"), 1.5)

    def test_clamp_negative_to_zero(self):
        self.assertAlmostEqual(_parse_jitter_value("-3"), 0.0)

    def test_clamp_above_ten(self):
        self.assertAlmostEqual(_parse_jitter_value("99"), 10.0)

    def test_invalid_string_returns_none(self):
        self.assertIsNone(_parse_jitter_value("abc"))

    def test_none_returns_none(self):
        self.assertIsNone(_parse_jitter_value(None))

    def test_boundary_zero(self):
        self.assertAlmostEqual(_parse_jitter_value("0"), 0.0)

    def test_boundary_ten(self):
        self.assertAlmostEqual(_parse_jitter_value("10"), 10.0)


# ---------------------------------------------------------------------------
# _parse_optional_float (lines 120-133)
# ---------------------------------------------------------------------------


class TestParseOptionalFloat(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(_parse_optional_float(None, 5.0))

    def test_valid_value(self):
        self.assertAlmostEqual(_parse_optional_float("2.5", 5.0), 2.5)

    def test_clamp_negative(self):
        self.assertAlmostEqual(_parse_optional_float("-1", 5.0), 0.0)

    def test_clamp_above_maximum(self):
        self.assertAlmostEqual(_parse_optional_float("99", 5.0), 5.0)

    def test_invalid_string_returns_none(self):
        self.assertIsNone(_parse_optional_float("xyz", 5.0))

    def test_non_finite_returns_none(self):
        self.assertIsNone(_parse_optional_float("inf", 5.0))
        self.assertIsNone(_parse_optional_float("nan", 5.0))

    def test_type_error_returns_none(self):
        self.assertIsNone(_parse_optional_float(object(), 5.0))


# ---------------------------------------------------------------------------
# root_window_at_point (lines 172, 175-177)
# ---------------------------------------------------------------------------


class TestRootWindowAtPoint(unittest.TestCase):
    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_no_window_returns_zero(self, mock_user32):
        mock_user32.WindowFromPoint.return_value = 0
        self.assertEqual(root_window_at_point(100, 200), 0)

    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_returns_root_ancestor(self, mock_user32):
        mock_user32.WindowFromPoint.return_value = 12345
        mock_user32.GetAncestor.return_value = 99999
        self.assertEqual(root_window_at_point(10, 20), 99999)

    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_ancestor_zero_falls_back_to_hwnd(self, mock_user32):
        mock_user32.WindowFromPoint.return_value = 12345
        mock_user32.GetAncestor.return_value = 0
        self.assertEqual(root_window_at_point(10, 20), 12345)

    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_exception_returns_zero(self, mock_user32):
        mock_user32.WindowFromPoint.side_effect = OSError("fail")
        self.assertEqual(root_window_at_point(10, 20), 0)


# ---------------------------------------------------------------------------
# uipi_blocks_input (lines 200, 204, 208, 211-213)
# ---------------------------------------------------------------------------


class TestUipiBlocksInput(unittest.TestCase):
    @mock.patch("fukua_rpa.actions.shared.process_integrity")
    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_null_hwnd_returns_false(self, mock_user32, mock_pi):
        blocked, pid = uipi_blocks_input(0)
        self.assertFalse(blocked)
        self.assertEqual(pid, 0)

    @mock.patch("fukua_rpa.actions.shared.process_integrity")
    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_null_pid_returns_false(self, mock_user32, mock_pi):
        # GetWindowThreadProcessId leaves pid at 0
        def fake_get_thread_proc(hwnd, pid_ref):
            pass  # pid stays 0

        mock_user32.GetWindowThreadProcessId.side_effect = fake_get_thread_proc
        blocked, pid = uipi_blocks_input(1234)
        self.assertFalse(blocked)
        self.assertEqual(pid, 0)

    @mock.patch("fukua_rpa.actions.shared.os.getpid", return_value=100)
    @mock.patch("fukua_rpa.actions.shared.process_integrity")
    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_unavailable_integrity_returns_false_with_pid(
        self, mock_user32, mock_pi, mock_getpid
    ):

        def fake_get_thread_proc(hwnd, pid_ref):
            pid_ref._obj.value = 456

        mock_user32.GetWindowThreadProcessId.side_effect = fake_get_thread_proc
        mock_pi.return_value = {"available": False}
        blocked, pid = uipi_blocks_input(1234)
        self.assertFalse(blocked)
        self.assertEqual(pid, 456)

    @mock.patch("fukua_rpa.actions.shared.os.getpid", return_value=100)
    @mock.patch("fukua_rpa.actions.shared.process_integrity")
    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_higher_integrity_blocked(self, mock_user32, mock_pi, mock_getpid):

        def fake_get_thread_proc(hwnd, pid_ref):
            pid_ref._obj.value = 456

        mock_user32.GetWindowThreadProcessId.side_effect = fake_get_thread_proc
        mock_pi.side_effect = [
            {"available": True, "rank": 5},  # target
            {"available": True, "rank": 3},  # current
        ]
        blocked, pid = uipi_blocks_input(1234)
        self.assertTrue(blocked)
        self.assertEqual(pid, 456)

    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_exception_returns_false_zero(self, mock_user32):
        mock_user32.GetWindowThreadProcessId.side_effect = OSError("boom")
        blocked, pid = uipi_blocks_input(1234)
        self.assertFalse(blocked)
        self.assertEqual(pid, 0)


# ---------------------------------------------------------------------------
# activate_window_for_click (lines 228-248)
# ---------------------------------------------------------------------------


class TestActivateWindowForClick(unittest.TestCase):
    @mock.patch("fukua_rpa.actions.shared.kernel32")
    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_null_hwnd_returns_false(self, mock_user32, mock_kernel32):
        self.assertFalse(activate_window_for_click(0))

    @mock.patch("fukua_rpa.actions.shared.kernel32")
    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_already_foreground_returns_true(self, mock_user32, mock_kernel32):
        mock_user32.GetForegroundWindow.return_value = 777
        self.assertTrue(activate_window_for_click(777))

    @mock.patch("fukua_rpa.actions.shared.kernel32")
    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_activates_with_thread_attach(self, mock_user32, mock_kernel32):
        mock_user32.GetForegroundWindow.return_value = 111
        mock_user32.GetWindowThreadProcessId.return_value = 222
        mock_kernel32.GetCurrentThreadId.return_value = 333
        mock_user32.AttachThreadInput.return_value = True
        mock_user32.SetForegroundWindow.return_value = True

        result = activate_window_for_click(777)
        self.assertTrue(result)
        mock_user32.ShowWindow.assert_called_once()
        # Attach then detach
        self.assertEqual(mock_user32.AttachThreadInput.call_count, 2)

    @mock.patch("fukua_rpa.actions.shared.kernel32")
    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_same_thread_no_attach(self, mock_user32, mock_kernel32):
        mock_user32.GetForegroundWindow.return_value = 111
        mock_user32.GetWindowThreadProcessId.return_value = 333
        mock_kernel32.GetCurrentThreadId.return_value = 333
        mock_user32.SetForegroundWindow.return_value = False

        result = activate_window_for_click(777)
        self.assertFalse(result)
        mock_user32.AttachThreadInput.assert_not_called()

    @mock.patch("fukua_rpa.actions.shared.kernel32")
    @mock.patch("fukua_rpa.actions.shared.user32")
    def test_exception_returns_false(self, mock_user32, mock_kernel32):
        mock_user32.GetForegroundWindow.side_effect = OSError("fail")
        self.assertFalse(activate_window_for_click(777))


# ---------------------------------------------------------------------------
# action_handler duplicate registration (line 266)
# ---------------------------------------------------------------------------


class TestActionHandlerDuplicate(unittest.TestCase):
    def test_duplicate_registration_raises(self):
        # Use a unique code to avoid polluting the global table
        code = 99999.0
        # Ensure clean state
        ACTION_HANDLERS.pop(code, None)

        @action_handler(code)
        def handler_a():
            pass

        with self.assertRaises(RuntimeError):
            @action_handler(code)
            def handler_b():
                pass

        # Cleanup
        ACTION_HANDLERS.pop(code, None)


# ---------------------------------------------------------------------------
# ActionsSharedMixin._bounded_action_duration (lines 303-305, 307)
# ---------------------------------------------------------------------------


class TestBoundedActionDuration(unittest.TestCase):
    def test_parse_failure_returns_default(self):
        result = ActionsSharedMixin._bounded_action_duration("abc", default=2.0)
        self.assertAlmostEqual(result, 2.0)

    def test_none_returns_default(self):
        result = ActionsSharedMixin._bounded_action_duration(None, default=1.0)
        self.assertAlmostEqual(result, 1.0)

    def test_negative_raises_value_error(self):
        with self.assertRaises(ValueError):
            ActionsSharedMixin._bounded_action_duration("-1")

    def test_above_maximum_raises_value_error(self):
        with self.assertRaises(ValueError):
            ActionsSharedMixin._bounded_action_duration("999999")

    def test_non_finite_raises_value_error(self):
        with self.assertRaises(ValueError):
            ActionsSharedMixin._bounded_action_duration("inf")

    def test_valid_value(self):
        result = ActionsSharedMixin._bounded_action_duration("5.5")
        self.assertAlmostEqual(result, 5.5)

    def test_custom_maximum(self):
        with self.assertRaises(ValueError):
            ActionsSharedMixin._bounded_action_duration("11", maximum=10.0)
        result = ActionsSharedMixin._bounded_action_duration("10", maximum=10.0)
        self.assertAlmostEqual(result, 10.0)


# ---------------------------------------------------------------------------
# ActionsSharedMixin._safe_input_coordinate (line 325)
# ---------------------------------------------------------------------------


class TestSafeInputCoordinate(unittest.TestCase):
    def test_valid_coordinate(self):
        self.assertEqual(ActionsSharedMixin._safe_input_coordinate(100.4), 100)

    def test_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            ActionsSharedMixin._safe_input_coordinate(2_000_000)

    def test_non_finite_raises(self):
        with self.assertRaises(ValueError):
            ActionsSharedMixin._safe_input_coordinate(float("inf"))

    def test_negative_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            ActionsSharedMixin._safe_input_coordinate(-2_000_000)


# ---------------------------------------------------------------------------
# Helper: minimal engine stub for tracked input methods
# ---------------------------------------------------------------------------


class _EngineStub(ActionsSharedMixin):
    """Minimal stub providing the attributes ActionsSharedMixin methods need."""

    def __init__(self):
        self._injected_input_lock = threading.Lock()
        self._injected_mouse_buttons = set()
        self._injected_keys = []
        self._stop = False
        self.input_backend = mock.MagicMock()
        self.callback_input_state = None

    def check_stop_flag(self):
        return self._stop


# ---------------------------------------------------------------------------
# _tracked_mouse_down (lines 340, 344-351)
# ---------------------------------------------------------------------------


class TestTrackedMouseDown(unittest.TestCase):
    def test_stop_flag_returns_false(self):
        engine = _EngineStub()
        engine._stop = True
        self.assertFalse(engine._tracked_mouse_down("left"))
        engine.input_backend.mouse_down.assert_not_called()

    def test_success_tracks_button(self):
        engine = _EngineStub()
        self.assertTrue(engine._tracked_mouse_down("left"))
        self.assertIn("left", engine._injected_mouse_buttons)
        engine.input_backend.mouse_down.assert_called_once_with(button="left")

    def test_exception_cleans_up_and_reraises(self):
        engine = _EngineStub()
        engine.input_backend.mouse_down.side_effect = OSError("fail")
        with self.assertRaises(OSError):
            engine._tracked_mouse_down("right")
        # mouse_up was attempted as cleanup
        engine.input_backend.mouse_up.assert_called_once_with(button="right")
        self.assertNotIn("right", engine._injected_mouse_buttons)

    def test_exception_in_cleanup_reraises_original(self):
        engine = _EngineStub()
        engine.input_backend.mouse_down.side_effect = OSError("down fail")
        engine.input_backend.mouse_up.side_effect = OSError("up fail")
        with self.assertRaises(OSError):
            engine._tracked_mouse_down("middle")
        # Button stays tracked for run-level cleanup
        self.assertIn("middle", engine._injected_mouse_buttons)

    def test_notify_happens_before_physical_mouse_down(self):
        # RUN02 方案A（与 _tracked_key_down 对称）：先上报后按下。上报必须发生在
        # mouse_down 之前，使强杀窗口内宿主已记录该按钮（宿主已知但物理未按 →
        # 强杀时多发一次 mouseUp，无副作用）。
        engine = _EngineStub()
        order = []
        engine.callback_input_state = lambda state: order.append(
            ("notify", list(state["mouse_buttons"]))
        )
        engine.input_backend.mouse_down.side_effect = (
            lambda button: order.append(("mouse_down", button))
        )
        self.assertTrue(engine._tracked_mouse_down("left"))
        self.assertEqual(
            order,
            [("notify", ["left"]), ("mouse_down", "left")],
        )

    def test_mouse_down_failure_rolls_back_reported_state(self):
        # RUN02 方案A：mouse_down 失败后必须再次上报撤销该按钮，避免宿主
        # 「以为按住、实际未按」的错位（强杀时误发 mouseUp 属于无害过度释放，
        # 但状态必须与 _injected_mouse_buttons 一致）。
        engine = _EngineStub()
        reported = []
        engine.callback_input_state = lambda state: reported.append(
            list(state["mouse_buttons"])
        )
        engine.input_backend.mouse_down.side_effect = OSError("fail")
        with self.assertRaises(OSError):
            engine._tracked_mouse_down("right")
        # 第一次上报含该按钮（按下前），第二次上报（回滚）不含该按钮。
        self.assertEqual(reported, [["right"], []])
        self.assertNotIn("right", engine._injected_mouse_buttons)


# ---------------------------------------------------------------------------
# _tracked_mouse_up (lines 367, 370-373)
# ---------------------------------------------------------------------------


class TestTrackedMouseUp(unittest.TestCase):
    def test_untracked_button_returns_false(self):
        engine = _EngineStub()
        self.assertFalse(engine._tracked_mouse_up("left"))

    def test_success_releases_and_discards(self):
        engine = _EngineStub()
        engine._injected_mouse_buttons.add("left")
        self.assertTrue(engine._tracked_mouse_up("left"))
        self.assertNotIn("left", engine._injected_mouse_buttons)
        engine.input_backend.mouse_up.assert_called_once_with(button="left")

    def test_exception_reraises_and_keeps_tracked(self):
        engine = _EngineStub()
        engine._injected_mouse_buttons.add("right")
        engine.input_backend.mouse_up.side_effect = OSError("fail")
        with self.assertRaises(OSError):
            engine._tracked_mouse_up("right")
        self.assertIn("right", engine._injected_mouse_buttons)


# ---------------------------------------------------------------------------
# _tracked_key_down (lines 393, 396, 401-409)
# ---------------------------------------------------------------------------


class TestTrackedKeyDown(unittest.TestCase):
    def test_empty_key_raises_value_error(self):
        engine = _EngineStub()
        with self.assertRaises(ValueError):
            engine._tracked_key_down("")
        with self.assertRaises(ValueError):
            engine._tracked_key_down("   ")

    def test_stop_flag_returns_false(self):
        engine = _EngineStub()
        engine._stop = True
        self.assertFalse(engine._tracked_key_down("a"))

    def test_success_tracks_key(self):
        engine = _EngineStub()
        self.assertTrue(engine._tracked_key_down("A"))
        self.assertIn("a", engine._injected_keys)
        engine.input_backend.key_down.assert_called_once_with("a")

    def test_exception_cleans_up_and_reraises(self):
        engine = _EngineStub()
        engine.input_backend.key_down.side_effect = OSError("fail")
        with self.assertRaises(OSError):
            engine._tracked_key_down("b")
        engine.input_backend.key_up.assert_called_once_with("b")
        self.assertNotIn("b", engine._injected_keys)

    def test_exception_in_cleanup_keeps_tracked(self):
        engine = _EngineStub()
        engine.input_backend.key_down.side_effect = OSError("down")
        engine.input_backend.key_up.side_effect = OSError("up")
        with self.assertRaises(OSError):
            engine._tracked_key_down("c")
        self.assertIn("c", engine._injected_keys)

    def test_notify_happens_before_physical_key_down(self):
        # RUN02 方案A：先上报后按下。上报必须发生在 key_down 之前，使强杀窗口
        # 内宿主已记录该键（宿主已知但物理未按 → 强杀时多发一次 keyUp，无副作用）。
        engine = _EngineStub()
        order = []
        engine.callback_input_state = lambda state: order.append(
            ("notify", list(state["keys"]))
        )
        engine.input_backend.key_down.side_effect = lambda key: order.append(
            ("key_down", key)
        )
        self.assertTrue(engine._tracked_key_down("a"))
        self.assertEqual(
            order,
            [("notify", ["a"]), ("key_down", "a")],
        )

    def test_key_down_failure_rolls_back_reported_state(self):
        # RUN02 方案A：key_down 失败后必须再次上报撤销该键，避免宿主
        # 「以为按住、实际未按」的错位（强杀时误发 keyUp 属于无害过度释放，
        # 但状态必须与 _injected_keys 一致）。
        engine = _EngineStub()
        reported = []
        engine.callback_input_state = lambda state: reported.append(
            list(state["keys"])
        )
        engine.input_backend.key_down.side_effect = OSError("fail")
        with self.assertRaises(OSError):
            engine._tracked_key_down("b")
        # 第一次上报含该键（按下前），第二次上报（回滚）不含该键。
        self.assertEqual(reported, [["b"], []])
        self.assertNotIn("b", engine._injected_keys)


# ---------------------------------------------------------------------------
# _tracked_key_up (lines 425, 428-429)
# ---------------------------------------------------------------------------


class TestTrackedKeyUp(unittest.TestCase):
    def test_untracked_key_returns_false(self):
        engine = _EngineStub()
        self.assertFalse(engine._tracked_key_up("x"))

    def test_success_releases(self):
        engine = _EngineStub()
        engine._injected_keys.append("x")
        self.assertTrue(engine._tracked_key_up("x"))
        self.assertNotIn("x", engine._injected_keys)
        engine.input_backend.key_up.assert_called_once_with("x")

    def test_exception_reraises_and_keeps_tracked(self):
        engine = _EngineStub()
        engine._injected_keys.append("y")
        engine.input_backend.key_up.side_effect = OSError("fail")
        with self.assertRaises(OSError):
            engine._tracked_key_up("y")
        self.assertIn("y", engine._injected_keys)


# ---------------------------------------------------------------------------
# _notify_injected_input_state (lines 442-450)
# ---------------------------------------------------------------------------


class TestNotifyInjectedInputState(unittest.TestCase):
    def test_no_callback_is_noop(self):
        engine = _EngineStub()
        engine.callback_input_state = None
        # Should not raise
        engine._notify_injected_input_state()

    def test_callback_receives_state(self):
        engine = _EngineStub()
        received = []
        engine.callback_input_state = lambda state: received.append(state)
        engine._injected_mouse_buttons.add("left")
        engine._injected_keys.append("ctrl")
        engine._notify_injected_input_state()
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["mouse_buttons"], ["left"])
        self.assertEqual(received[0]["keys"], ["ctrl"])

    def test_callback_exception_is_swallowed(self):
        engine = _EngineStub()

        def bad_callback(state):
            raise RuntimeError("callback boom")

        engine.callback_input_state = bad_callback
        # Should not raise
        engine._notify_injected_input_state()


# ---------------------------------------------------------------------------
# release_injected_inputs (lines 442-450 partial, 456-478)
# ---------------------------------------------------------------------------


class TestReleaseInjectedInputs(unittest.TestCase):
    def test_releases_all_tracked(self):
        engine = _EngineStub()
        engine._injected_keys = ["ctrl", "a"]
        engine._injected_mouse_buttons = {"left", "right"}
        engine.release_injected_inputs()
        self.assertEqual(engine._injected_keys, [])
        self.assertEqual(engine._injected_mouse_buttons, set())
        # Keys released in reverse order
        key_calls = [c[0][0] for c in engine.input_backend.key_up.call_args_list]
        self.assertEqual(key_calls, ["a", "ctrl"])

    def test_key_release_failure_continues(self):
        engine = _EngineStub()
        engine._injected_keys = ["a", "b"]
        # release_injected_inputs iterates reversed: "b" first, then "a"
        engine.input_backend.key_up.side_effect = [OSError("fail"), None]
        engine.release_injected_inputs()
        # "b" failed so stays; "a" succeeded so removed
        self.assertEqual(engine._injected_keys, ["b"])

    def test_mouse_release_failure_continues(self):
        engine = _EngineStub()
        engine._injected_mouse_buttons = {"left", "right"}
        engine.input_backend.mouse_up.side_effect = [OSError("fail"), None]
        engine.release_injected_inputs()
        # One button failed, one succeeded
        self.assertEqual(len(engine._injected_mouse_buttons), 1)


if __name__ == "__main__":
    unittest.main()
