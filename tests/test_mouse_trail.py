"""普通脚本鼠标移动拖影的配置、执行与覆盖层回归测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from fukua_rpa.actions.flow import _mouse_trail_override
from fukua_rpa.commands import (
    SETTING_MOUSE_TRAIL,
    command_name,
    effective_task_settings,
    step_settings_capabilities,
)
from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.engine import RPAEngine
from fukua_rpa.run_config import EngineRunConfig
from fukua_rpa.ui.overlays import MouseTrailOverlay
from fukua_rpa.ui.task_settings_dialog import TaskConfigDialog
from tests.qt_test_support import dispose_widget


class MouseTrailEngineTests(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            self.engine = RPAEngine(defer_backends=True)
        run_id = self.engine.reserve_run()
        self.engine.lifecycle.mark_running(run_id)
        self.run_id = run_id
        self.engine.callback_click_indicator = mock.Mock()

    def tearDown(self):
        self.engine.finish_run(self.run_id)

    def test_instant_move_emits_one_bounded_trail_batch(self):
        self.engine.show_mouse_trail = True
        with mock.patch.object(
            self.engine.input_backend, "position", return_value=(10, 20)
        ), mock.patch.object(self.engine.input_backend, "move_to"):
            result = self.engine._move_mouse_interruptibly(100, 200, 0.0)

        self.assertTrue(result)
        payload = self.engine.callback_click_indicator.call_args.args[0]
        self.assertEqual(payload["kind"], "mouse_trail")
        self.assertEqual(payload["points"], [[10, 20], [100, 200]])
        self.assertTrue(payload["new_path"])

    def test_per_step_off_suppresses_global_trail(self):
        self.engine.show_mouse_trail = True
        with mock.patch.object(
            self.engine.input_backend, "position", return_value=(10, 20)
        ), mock.patch.object(self.engine.input_backend, "move_to"):
            result = self.engine._move_mouse_interruptibly(
                100, 200, 0.0, trail_enabled=False
            )

        self.assertTrue(result)
        self.engine.callback_click_indicator.assert_not_called()

    def test_instant_move_continues_when_trail_start_cannot_be_read(self):
        self.engine.show_mouse_trail = True
        with mock.patch.object(
            self.engine.input_backend,
            "position",
            side_effect=OSError("position unavailable"),
        ), mock.patch.object(self.engine.input_backend, "move_to") as move_to:
            result = self.engine._move_mouse_interruptibly(100, 200, 0.0)

        self.assertTrue(result)
        move_to.assert_called_once_with(100, 200, duration=0)
        self.engine.callback_click_indicator.assert_not_called()

    def test_local_stop_predicate_interrupts_movement(self):
        calls = 0

        def stopped():
            nonlocal calls
            calls += 1
            return calls >= 3

        with mock.patch.object(
            self.engine.input_backend, "position", return_value=(0, 0)
        ), mock.patch.object(self.engine.input_backend, "move_to"):
            result = self.engine._move_mouse_interruptibly(
                100, 100, 0.5, stop_requested=stopped
            )

        self.assertFalse(result)

    def test_global_config_defaults_off_and_parses_on(self):
        config = default_profile_config()
        self.assertFalse(config["mouse_trail"])
        config["mouse_trail"] = True
        parsed = EngineRunConfig.from_mapping(config, 0)
        self.assertTrue(parsed.show_mouse_trail)

    def test_per_step_modes_and_capabilities(self):
        self.assertIsNone(_mouse_trail_override({}))
        self.assertTrue(_mouse_trail_override({"mouse_trail_mode": "on"}))
        self.assertFalse(_mouse_trail_override({"mouse_trail_mode": "off"}))
        for command, value in (
            (1.0, "10,20"),
            (8.0, "10,20"),
            (10.0, "10,20 -> 30,40"),
        ):
            self.assertIn(
                SETTING_MOUSE_TRAIL,
                step_settings_capabilities(command, value),
            )
        effective = effective_task_settings(
            {"type": 5.0, "value": "1", "mouse_trail_mode": "on"}
        )
        self.assertEqual(effective["mouse_trail_mode"], "inherit")


class MouseTrailOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_overlay_reuses_one_bounded_surface(self):
        overlay = MouseTrailOverlay()
        self.addCleanup(overlay.close)
        overlay.add_points([(10, 10), (20, 20)], new_path=True)
        overlay.add_points([(30, 30)], new_path=False)
        self.app.processEvents()

        self.assertTrue(overlay.isVisible())
        self.assertEqual(len(overlay._points), 3)
        self.assertEqual(overlay._points[0][2], overlay._points[-1][2])
        self.assertTrue(overlay.testAttribute(Qt.WA_TransparentForMouseEvents))

        for start in range(0, 96, 16):
            overlay.add_points(
                [(start + offset, start + offset) for offset in range(16)]
            )
        self.assertEqual(len(overlay._points), overlay.MAX_POINTS)


class MouseTrailTaskSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_section_is_advanced_only_and_preserves_saved_override(self):
        with tempfile.TemporaryDirectory() as base_dir:
            simple = TaskConfigDialog(
                None,
                {"mouse_trail_mode": "on"},
                step_type=command_name(1.0),
                step_value="10,20",
                settings_mode="simple",
                base_dir=base_dir,
            )
            advanced = TaskConfigDialog(
                None,
                {"mouse_trail_mode": "on"},
                step_type=command_name(1.0),
                step_value="10,20",
                settings_mode="advanced",
                base_dir=base_dir,
            )
            try:
                simple.show()
                advanced.show()
                self.app.processEvents()
                self.assertTrue(simple.mouse_trail_section.isHidden())
                self.assertFalse(advanced.mouse_trail_section.isHidden())
                self.assertEqual(
                    advanced.mouse_trail_mode_combo.currentData(), "on"
                )
                self.assertEqual(advanced.get_data()["mouse_trail_mode"], "on")
            finally:
                dispose_widget(self.app, simple)
                dispose_widget(self.app, advanced)


if __name__ == "__main__":
    unittest.main()
