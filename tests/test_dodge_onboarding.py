import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

import fukua_rpa.engine.actions_exec as engine_actions_module
from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.engine import RPAEngine
from fukua_rpa.run_config import EngineRunConfig, RunConfigError
from tests.qt_test_support import QtCallbackGuardMixin, create_test_window


# TODO(L-53): 本模块覆盖偏薄，待补充边界测试：避让坐标为空/越界/起点终点重合、
# 取点器取消或回填非法坐标、避让与延时二次点击/尝试快照组合、双击避让在启用
# 普通避让与未启用时的开关组合，以及避让设置在不同方案间迁移的兼容性。
class DodgeAndOnboardingTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def create_window(self, base_dir):
        return create_test_window(self, base_dir)

    def test_fast_response_tip_is_shown_only_on_first_settings_open(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertTrue(window.fast_response_tip.isHidden())

            window.show_settings_dialog()
            self.app.processEvents()
            self.assertTrue(window.fast_response_tip.isVisible())
            self.assertTrue(
                window.settings.value(
                    "onboarding/fast_response_tip_v1_seen", False, type=bool
                )
            )

            window.close_settings_dialog()
            window.show_settings_dialog()
            self.app.processEvents()
            self.assertTrue(window.fast_response_tip.isHidden())

    def test_dodge_pick_buttons_fill_both_coordinate_pairs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            callbacks = []

            def create_picker(mode, callback):
                self.assertEqual(mode, "point")
                callbacks.append(callback)
                return QWidget()

            with mock.patch(
                "fukua_rpa.ui.controllers.feedback_controller.CoordinatePickerUI",
                side_effect=create_picker,
            ):
                window.dodge_pick1_btn.click()
                callbacks[-1]("-120,345")
                window.dodge_pick2_btn.click()
                callbacks[-1]("800,900")

            self.assertEqual(window.dodge_x1.text(), "-120")
            self.assertEqual(window.dodge_y1.text(), "345")
            self.assertEqual(window.dodge_x2.text(), "800")
            self.assertEqual(window.dodge_y2.text(), "900")

    def test_dodge_click_action_round_trips_and_reaches_engine(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.dodge_click_combo.setCurrentIndex(
                window.dodge_click_combo.findData("right")
            )
            config = window.get_current_ui_config()
            self.assertEqual(config["dodge_click_action"], "right")

            window.dodge_click_combo.setCurrentIndex(0)
            window.apply_ui_config(config)
            self.assertEqual(window.dodge_click_combo.currentData(), "right")

            runtime = EngineRunConfig.from_mapping(config)
            engine = RPAEngine(defer_backends=True)
            runtime.apply_to(engine)
            self.assertEqual(engine.dodge_click_action, "right")

    def test_double_dodge_clicks_only_once_at_the_final_point(self):
        engine = RPAEngine(defer_backends=True)
        engine.enable_dodge = True
        engine.enable_double_dodge = True
        engine.dodge_x1, engine.dodge_y1 = 100, 200
        engine.dodge_x2, engine.dodge_y2 = 300, 400
        engine.dodge_click_action = "right"
        indicators = []
        engine.callback_click_indicator = indicators.append
        mouse = mock.Mock()
        engine.input_backend = mouse

        with mock.patch.object(engine_actions_module.time, "sleep"):
            engine._perform_mouse_click_impl(10, 20, 1, "left")

        self.assertEqual(
            mouse.move_to.call_args_list,
            [
                mock.call(10, 20, duration=0.0),
                mock.call(100, 200, duration=0),
                mock.call(300, 400, duration=0),
            ],
        )
        self.assertEqual(
            mouse.mouse_down.call_args_list,
            [mock.call(button="left"), mock.call(button="right")],
        )
        self.assertEqual(
            mouse.mouse_up.call_args_list,
            [mock.call(button="left"), mock.call(button="right")],
        )
        self.assertEqual(indicators[-1]["x"], 300)
        self.assertEqual(indicators[-1]["y"], 400)
        self.assertEqual(indicators[-1]["text"], "避让后右键单击")
        self.assertEqual(
            engine.performance.snapshot()["counters"]["action.dodge_clicks"],
            1,
        )

    def test_default_dodge_action_keeps_the_existing_move_only_behavior(self):
        engine = RPAEngine(defer_backends=True)
        engine.enable_dodge = True
        engine.dodge_x1, engine.dodge_y1 = 100, 200
        self.assertEqual(engine.dodge_click_action, "none")
        mouse = mock.Mock()
        engine.input_backend = mouse

        with mock.patch.object(engine_actions_module.time, "sleep"):
            engine._perform_mouse_click_impl(10, 20, 1, "left")

        self.assertEqual(
            mouse.mouse_down.call_args_list,
            [mock.call(button="left")],
        )
        self.assertEqual(
            mouse.move_to.call_args_list[-1],
            mock.call(100, 200, duration=0),
        )

    def test_step_background_click_failure_never_uses_the_real_mouse(self):
        engine = RPAEngine(defer_backends=True)
        engine.log_level = -1
        backend = mock.Mock()
        backend.binding_for_coordinate.return_value = {
            "root_hwnd": 700,
            "root_client_x": 120,
            "root_client_y": 80,
            "source_x": 320,
            "source_y": 240,
        }
        backend.background_click.return_value = False
        engine.task_window_backend = mock.Mock(return_value=backend)
        mouse = mock.Mock()
        engine.input_backend = mouse

        result = engine.perform_background_window_click(
            320,
            240,
            1,
            "left",
            {
                "root_hwnd": 700,
                "root_client_x": 120,
                "root_client_y": 80,
                "source_x": 320,
                "source_y": 240,
            },
        )

        self.assertFalse(result)
        backend.background_click.assert_called_once()
        mouse.move_to.assert_not_called()
        mouse.mouse_down.assert_not_called()

    def test_invalid_dodge_click_action_is_rejected(self):
        config = default_profile_config()
        config["dodge_click_action"] = "double"
        with self.assertRaises(RunConfigError):
            EngineRunConfig.from_mapping(config)

    def test_dodge_picker_cancel_or_invalid_value_keeps_existing_fields(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.dodge_x1.setText("111")
            window.dodge_y1.setText("222")
            window.dodge_x2.setText("333")
            window.dodge_y2.setText("444")

            # A cancelled picker (empty callback) or an unparseable coordinate
            # must leave the previously chosen values untouched.
            for value in ("", "   ", "not-a-coordinate"):
                window.on_dodge_point_picked(1, value)
                window.on_dodge_point_picked(2, value)

            self.assertEqual(window.dodge_x1.text(), "111")
            self.assertEqual(window.dodge_y1.text(), "222")
            self.assertEqual(window.dodge_x2.text(), "333")
            self.assertEqual(window.dodge_y2.text(), "444")

    def test_dodge_disabled_performs_no_dodge_move(self):
        engine = RPAEngine(defer_backends=True)
        engine.enable_dodge = False
        engine.dodge_x1, engine.dodge_y1 = 100, 200
        mouse = mock.Mock()
        engine.input_backend = mouse

        with mock.patch.object(engine_actions_module.time, "sleep"):
            engine._perform_mouse_click_impl(10, 20, 1, "left")

        # Only the original click move happens; no dodge move is issued when
        # dodge is not enabled (i.e. no dodge coordinate is configured).
        self.assertEqual(
            mouse.move_to.call_args_list,
            [mock.call(10, 20, duration=0.0)],
        )

    def test_out_of_bounds_dodge_coordinate_is_refused_not_sent_to_mouse(self):
        engine = RPAEngine(defer_backends=True)
        engine.enable_dodge = True
        engine.dodge_x1, engine.dodge_y1 = 2_000_000, 200
        mouse = mock.Mock()
        engine.input_backend = mouse

        with (
            mock.patch.object(engine_actions_module.time, "sleep"),
            self.assertRaisesRegex(ValueError, "超出允许范围"),
        ):
            engine._perform_mouse_click_impl(10, 20, 1, "left")

        # The real mouse only reaches the original click target; the wild dodge
        # coordinate is rejected (fail-closed) and never sent to the backend.
        self.assertEqual(
            mouse.move_to.call_args_list,
            [mock.call(10, 20, duration=0.0)],
        )
        mouse.mouse_down.assert_called_once_with(button="left")

    def test_coincident_double_dodge_points_should_not_repeat_the_same_move(self):
        # Regression test: when the two dodge points coincide the engine must
        # issue the dodge move_to only once, not twice.
        engine = RPAEngine(defer_backends=True)
        engine.enable_dodge = True
        engine.enable_double_dodge = True
        engine.dodge_x1, engine.dodge_y1 = 100, 200
        engine.dodge_x2, engine.dodge_y2 = 100, 200
        engine.dodge_click_action = "none"
        mouse = mock.Mock()
        engine.input_backend = mouse

        with mock.patch.object(engine_actions_module.time, "sleep"):
            engine._perform_mouse_click_impl(10, 20, 1, "left")

        dodge_moves = [
            call
            for call in mouse.move_to.call_args_list
            if call == mock.call(100, 200, duration=0)
        ]
        self.assertEqual(len(dodge_moves), 1)


if __name__ == "__main__":
    unittest.main()
