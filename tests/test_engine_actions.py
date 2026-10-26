import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fukua_rpa.engine import RPAEngine
from fukua_rpa.engine.actions_exec import ActionExecutionMixin, StepActionContext


def action_context(command, value, task=None):
    return StepActionContext(
        command=float(command),
        value=value,
        retry=1,
        step_info={"step": 1, "loop": 1, "cmd": "test"},
        cache_key="test",
        confidence=0.8,
        use_gray=True,
        task=task or {},
    )


class HotkeyValidationTests(unittest.TestCase):
    """_execute_hotkey rejects empty keys and pure '+' separators."""

    def setUp(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            self.engine = RPAEngine(defer_backends=True)
        self.engine.callback_msg = mock.Mock()
        self.engine.log_level = 2

    def test_empty_string_returns_error(self):
        result = self.engine._execute_hotkey(action_context(7.0, ""))
        self.assertEqual(result, "error")

    def test_pure_plus_returns_error(self):
        result = self.engine._execute_hotkey(action_context(7.0, "+"))
        self.assertEqual(result, "error")

    def test_spaced_plus_returns_error(self):
        result = self.engine._execute_hotkey(action_context(7.0, " + "))
        self.assertEqual(result, "error")

    def test_multiple_plus_only_returns_error(self):
        result = self.engine._execute_hotkey(action_context(7.0, "+++"))
        self.assertEqual(result, "error")

    def test_valid_hotkey_does_not_return_error(self):
        run_id = self.engine.reserve_run()
        self.engine.lifecycle.mark_running(run_id)
        try:
            # 单元测试边界：defer_backends 只推迟视觉后端，input_backend 仍是
            # 真实 PyAutoGUIInputBackend；不 mock 输入出口时 ctrl+a 会经
            # pyautogui.keyDown 发出真实按键。按本文件既有惯例 mock 后端方法，
            # 另以 pyautogui 守卫捕获任何绕过 input_backend 的真实调用。
            with mock.patch.object(
                self.engine.input_backend, "key_down"
            ) as key_down, mock.patch.object(
                self.engine.input_backend, "key_up"
            ) as key_up, mock.patch(
                "fukua_rpa.input_backend.pyautogui"
            ) as pyautogui_guard:
                result = self.engine._execute_hotkey(action_context(7.0, "ctrl+a"))
            self.assertNotEqual(result, "error")
            self.assertEqual(
                [call.args[0] for call in key_down.call_args_list], ["ctrl", "a"]
            )
            self.assertEqual(
                [call.args[0] for call in key_up.call_args_list], ["a", "ctrl"]
            )
            self.assertEqual(self.engine._injected_keys, [])
            pyautogui_guard.keyDown.assert_not_called()
            pyautogui_guard.keyUp.assert_not_called()
        finally:
            self.engine.finish_run(run_id)


class JitterDelayTests(unittest.TestCase):
    """_jitter_delay computes bounded random offsets."""

    def test_zero_jitter_returns_zero(self):
        self.assertEqual(ActionExecutionMixin._jitter_delay(0), 0.0)

    def test_none_jitter_returns_zero(self):
        self.assertEqual(ActionExecutionMixin._jitter_delay(None), 0.0)

    def test_negative_jitter_returns_zero(self):
        self.assertEqual(ActionExecutionMixin._jitter_delay(-1.0), 0.0)
        self.assertEqual(ActionExecutionMixin._jitter_delay(-100), 0.0)

    def test_positive_jitter_within_range(self):
        for _ in range(200):
            value = ActionExecutionMixin._jitter_delay(0.5)
            self.assertGreaterEqual(value, -0.5)
            self.assertLessEqual(value, 0.5)

    def test_invalid_string_returns_zero(self):
        self.assertEqual(ActionExecutionMixin._jitter_delay("abc"), 0.0)

    def test_large_jitter_within_range(self):
        for _ in range(100):
            value = ActionExecutionMixin._jitter_delay(3.0)
            self.assertGreaterEqual(value, -3.0)
            self.assertLessEqual(value, 3.0)


class ClickSpatialJitterTests(unittest.TestCase):
    """click_spatial_jitter offsets click coordinates within bounds."""

    def setUp(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            self.engine = RPAEngine(defer_backends=True)
        self.engine.callback_msg = mock.Mock()
        self.engine.log_level = 2
        run_id = self.engine.reserve_run()
        self.engine.lifecycle.mark_running(run_id)
        self.run_id = run_id

    def tearDown(self):
        self.engine.finish_run(self.run_id)

    def test_zero_jitter_no_offset(self):
        self.engine.click_spatial_jitter = 0
        self.engine.move_duration = 0.0
        self.engine.click_hold = 0.01
        self.engine.enable_dodge = False
        self.engine.move_duration_jitter = 0.0
        self.engine.click_hold_jitter = 0.0

        positions = []
        with mock.patch.object(
            self.engine.input_backend, "move_to",
            side_effect=lambda x, y, **kw: positions.append((x, y)),
        ), mock.patch.object(
            self.engine.input_backend, "mouse_down",
        ), mock.patch.object(
            self.engine.input_backend, "mouse_up",
        ), mock.patch.object(
            self.engine, "report_click_indicator",
        ):
            self.engine._perform_mouse_click_impl(100, 200, 1, "left")

        self.assertIn((100, 200), positions)

    def test_jitter_3_offsets_within_bounds(self):
        self.engine.click_spatial_jitter = 3
        self.engine.move_duration = 0.0
        self.engine.click_hold = 0.01
        self.engine.enable_dodge = False
        self.engine.move_duration_jitter = 0.0
        self.engine.click_hold_jitter = 0.0

        for _ in range(100):
            positions = []
            with mock.patch.object(
                self.engine.input_backend, "move_to",
                side_effect=lambda x, y, positions=positions, **kw: positions.append(
                    (x, y)
                ),
            ), mock.patch.object(
                self.engine.input_backend, "mouse_down",
            ), mock.patch.object(
                self.engine.input_backend, "mouse_up",
            ), mock.patch.object(
                self.engine, "report_click_indicator",
            ):
                self.engine._perform_mouse_click_impl(100, 200, 1, "left")

            self.assertTrue(len(positions) >= 1)
            click_x, click_y = positions[0]
            self.assertGreaterEqual(click_x, 97)
            self.assertLessEqual(click_x, 103)
            self.assertGreaterEqual(click_y, 197)
            self.assertLessEqual(click_y, 203)

    def test_jitter_produces_varied_offsets(self):
        self.engine.click_spatial_jitter = 3
        self.engine.move_duration = 0.0
        self.engine.click_hold = 0.01
        self.engine.enable_dodge = False
        self.engine.move_duration_jitter = 0.0
        self.engine.click_hold_jitter = 0.0

        observed_positions = set()
        for _ in range(50):
            positions = []
            with mock.patch.object(
                self.engine.input_backend, "move_to",
                side_effect=lambda x, y, positions=positions, **kw: positions.append(
                    (x, y)
                ),
            ), mock.patch.object(
                self.engine.input_backend, "mouse_down",
            ), mock.patch.object(
                self.engine.input_backend, "mouse_up",
            ), mock.patch.object(
                self.engine, "report_click_indicator",
            ):
                self.engine._perform_mouse_click_impl(100, 200, 1, "left")
            if positions:
                observed_positions.add(positions[0])

        self.assertGreater(len(observed_positions), 1)


class CurveMoveTests(unittest.TestCase):
    """move_curve_strength controls linear vs bezier interpolation."""

    def setUp(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            self.engine = RPAEngine(defer_backends=True)
        self.engine.callback_msg = mock.Mock()
        self.engine.log_level = 2
        run_id = self.engine.reserve_run()
        self.engine.lifecycle.mark_running(run_id)
        self.run_id = run_id

    def tearDown(self):
        self.engine.finish_run(self.run_id)

    def test_strength_zero_linear_interpolation(self):
        self.engine.move_curve_strength = 0.0
        positions = []
        with mock.patch.object(
            self.engine.input_backend, "position", return_value=(0, 0),
        ), mock.patch.object(
            self.engine.input_backend, "move_to",
            side_effect=lambda x, y, **kw: positions.append((x, y)),
        ):
            result = self.engine._move_mouse_interruptibly(100, 100, duration=0.1)

        self.assertTrue(result)
        self.assertTrue(len(positions) >= 2)
        # Linear: all points should lie on the line y=x
        for px, py in positions:
            self.assertAlmostEqual(px, py, delta=1)

    def test_strength_positive_produces_curve(self):
        self.engine.move_curve_strength = 50.0
        positions = []
        with mock.patch.object(
            self.engine.input_backend, "position", return_value=(0, 0),
        ), mock.patch.object(
            self.engine.input_backend, "move_to",
            side_effect=lambda x, y, **kw: positions.append((x, y)),
        ):
            result = self.engine._move_mouse_interruptibly(200, 0, duration=0.2)

        self.assertTrue(result)
        self.assertTrue(len(positions) >= 3)
        # With a curve, at least one intermediate point should deviate from
        # the straight line y=0 (start=(0,0), end=(200,0)).
        mid_points = positions[1:-1]
        deviations = [abs(py) for _px, py in mid_points]
        # The curve may deviate in either direction; check that not all are zero
        # (statistically near-impossible with strength=50 over 200px).
        self.assertTrue(any(d > 0 for d in deviations))

    def test_final_point_reaches_target(self):
        self.engine.move_curve_strength = 30.0
        positions = []
        with mock.patch.object(
            self.engine.input_backend, "position", return_value=(10, 20),
        ), mock.patch.object(
            self.engine.input_backend, "move_to",
            side_effect=lambda x, y, **kw: positions.append((x, y)),
        ):
            result = self.engine._move_mouse_interruptibly(300, 400, duration=0.15)

        self.assertTrue(result)
        self.assertTrue(len(positions) >= 1)
        final_x, final_y = positions[-1]
        self.assertEqual(final_x, 300)
        self.assertEqual(final_y, 400)


class ScrollWheelDeltaTests(unittest.TestCase):
    """scroll action multiplies by WHEEL_DELTA (120)."""

    def setUp(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            self.engine = RPAEngine(defer_backends=True)
        self.engine.callback_msg = mock.Mock()
        self.engine.log_level = 2
        run_id = self.engine.reserve_run()
        self.engine.lifecycle.mark_running(run_id)
        self.run_id = run_id

    def tearDown(self):
        self.engine.finish_run(self.run_id)

    def test_scroll_multiplied_by_120(self):
        with mock.patch.object(
            self.engine.input_backend, "scroll",
        ) as scroll_mock:
            result = self.engine._execute_scroll(action_context(6.0, "3"))
        self.assertEqual(result, "success")
        scroll_mock.assert_called_once_with(3 * 120)

    def test_scroll_negative_multiplied_by_120(self):
        with mock.patch.object(
            self.engine.input_backend, "scroll",
        ) as scroll_mock:
            result = self.engine._execute_scroll(action_context(6.0, "-5"))
        self.assertEqual(result, "success")
        scroll_mock.assert_called_once_with(-5 * 120)

    def test_scroll_clamped_to_bounds(self):
        with mock.patch.object(
            self.engine.input_backend, "scroll",
        ) as scroll_mock:
            result = self.engine._execute_scroll(action_context(6.0, "99999"))
        self.assertEqual(result, "success")
        scroll_mock.assert_called_once_with(10000 * 120)

    def test_scroll_invalid_value_returns_error(self):
        result = self.engine._execute_scroll(action_context(6.0, "abc"))
        self.assertEqual(result, "error")

    def test_scroll_infinity_returns_error(self):
        result = self.engine._execute_scroll(action_context(6.0, "inf"))
        self.assertEqual(result, "error")


class ReleaseInjectedInputsTests(unittest.TestCase):
    """release_injected_inputs clears tracked state on success, retains on failure."""

    def setUp(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            self.engine = RPAEngine(defer_backends=True)
        self.engine.callback_msg = mock.Mock()
        self.engine.log_level = 2
        run_id = self.engine.reserve_run()
        self.engine.lifecycle.mark_running(run_id)
        self.run_id = run_id

    def tearDown(self):
        self.engine.finish_run(self.run_id)

    def test_successful_release_clears_all(self):
        self.engine._injected_keys.append("ctrl")
        self.engine._injected_keys.append("a")
        self.engine._injected_mouse_buttons.add("left")

        self.engine.release_injected_inputs()

        self.assertEqual(self.engine._injected_keys, [])
        self.assertEqual(self.engine._injected_mouse_buttons, set())

    def test_failed_key_release_retains_key(self):
        self.engine._injected_keys.append("ctrl")
        self.engine._injected_keys.append("shift")

        with mock.patch.object(
            self.engine.input_backend, "key_up",
            side_effect=RuntimeError("device busy"),
        ):
            self.engine.release_injected_inputs()

        # Both keys should remain because every release failed
        self.assertIn("ctrl", self.engine._injected_keys)
        self.assertIn("shift", self.engine._injected_keys)

    def test_partial_failure_retains_only_failed_keys(self):
        self.engine._injected_keys.append("ctrl")
        self.engine._injected_keys.append("a")

        call_count = [0]

        def selective_failure(key):
            call_count[0] += 1
            if key == "a":
                raise RuntimeError("stuck key")

        with mock.patch.object(
            self.engine.input_backend, "key_up",
            side_effect=selective_failure,
        ):
            self.engine.release_injected_inputs()

        # "ctrl" released successfully (processed first in reversed order: a, ctrl)
        # "a" failed, so it stays
        self.assertNotIn("ctrl", self.engine._injected_keys)
        self.assertIn("a", self.engine._injected_keys)

    def test_failed_mouse_release_retains_button(self):
        self.engine._injected_mouse_buttons.add("right")

        with mock.patch.object(
            self.engine.input_backend, "mouse_up",
            side_effect=RuntimeError("button stuck"),
        ):
            self.engine.release_injected_inputs()

        self.assertIn("right", self.engine._injected_mouse_buttons)

    def test_retry_after_failure_succeeds(self):
        self.engine._injected_keys.append("ctrl")
        self.engine._injected_mouse_buttons.add("left")

        with mock.patch.object(
            self.engine.input_backend, "key_up",
            side_effect=RuntimeError("busy"),
        ), mock.patch.object(
            self.engine.input_backend, "mouse_up",
            side_effect=RuntimeError("busy"),
        ):
            self.engine.release_injected_inputs()

        # Still tracked after failure
        self.assertEqual(self.engine._injected_keys, ["ctrl"])
        self.assertEqual(self.engine._injected_mouse_buttons, {"left"})

        # Retry succeeds
        self.engine.release_injected_inputs()
        self.assertEqual(self.engine._injected_keys, [])
        self.assertEqual(self.engine._injected_mouse_buttons, set())


if __name__ == "__main__":
    unittest.main()
