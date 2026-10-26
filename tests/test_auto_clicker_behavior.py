"""连点器随机分布、参数显隐和执行层级回归测试。"""

from __future__ import annotations

import os
import random
import statistics
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from fukua_rpa.actions.shared import StepActionContext
from fukua_rpa.constants import TASK_TYPE_AUTO_CLICKER
from fukua_rpa.engine import RPAEngine
from fukua_rpa.random_distribution import (
    concentration_alpha,
    sample_centered_unit,
    sample_interval_seconds,
    sample_pointer_offset,
)
from fukua_rpa.ui.auto_clicker_tool import AutoClickerToolWindow
from fukua_rpa.ui.theme import apply_widget_geometry_scale

try:
    # FR-SCOPE009-001：修复后界面与执行器共用的连击次数上限。
    # 用守卫导入而非直接导入，是为了让「常量缺失」在红侧表现为干净的断言失败，
    # 而不是整个测试模块的导入错误（否则红侧无法给出可归因的失败证据）。
    from fukua_rpa.constants import AUTO_CLICKER_COUNT_MAX
except ImportError:  # pragma: no cover - 仅修复前走到
    AUTO_CLICKER_COUNT_MAX = None


class RandomDistributionTests(unittest.TestCase):
    def test_presets_share_one_monotonic_concentration_curve(self):
        self.assertEqual(concentration_alpha(0), 1.0)
        self.assertLess(concentration_alpha(0), concentration_alpha(50))
        self.assertLess(concentration_alpha(50), concentration_alpha(80))

        uniform_rng = random.Random(20260801)
        concentrated_rng = random.Random(20260801)
        uniform = [
            abs(sample_centered_unit(0, rng=uniform_rng)) for _ in range(4000)
        ]
        concentrated = [
            abs(sample_centered_unit(80, rng=concentrated_rng))
            for _ in range(4000)
        ]
        self.assertLess(statistics.mean(concentrated), statistics.mean(uniform))

    def test_interval_stays_inside_base_plus_or_minus_range(self):
        rng = random.Random(7)
        values = [
            sample_interval_seconds(0.2, 0.08, 50, rng=rng)
            for _ in range(1000)
        ]
        self.assertGreaterEqual(min(values), 0.12)
        self.assertLessEqual(max(values), 0.28)

    def test_rectangle_and_ellipse_obey_their_declared_bounds(self):
        rectangle_rng = random.Random(11)
        ellipse_rng = random.Random(13)
        for _ in range(1000):
            x, y = sample_pointer_offset(
                12, 5, "rectangle", 0, rng=rectangle_rng
            )
            self.assertLessEqual(abs(x), 12)
            self.assertLessEqual(abs(y), 5)

            x, y = sample_pointer_offset(
                12, 5, "ellipse", 50, rng=ellipse_rng
            )
            self.assertLessEqual((x / 12) ** 2 + (y / 5) ** 2, 1.0000001)


class AutoClickerWindowBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = AutoClickerToolWindow(None, None)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def test_dependent_controls_have_correct_initial_visibility(self):
        label = self.window.simple_form.labelForField(
            self.window.custom_interval_row
        )
        self.assertTrue(self.window.custom_interval_row.isHidden())
        self.assertTrue(label.isHidden())
        self.assertTrue(self.window.fixed_x_spin.isHidden())
        self.assertTrue(self.window._fixed_x_label.isHidden())
        self.assertFalse(self.window.fixed_pick_btn.isHidden())
        self.assertTrue(self.window.jitter_controls.isHidden())
        self.assertTrue(self.window.curve_strength_spin.isHidden())
        self.assertEqual(self.window.chk_jitter.text(), "随机坐标偏移")
        self.assertEqual(self.window.chk_curve.text(), "随机移动轨迹")
        self.assertTrue(self.window.interval_base_spin.suffix().strip() == "s")
        self.assertTrue(self.window.random_interval_group.isHidden())
        self.assertEqual(
            self.window.position_movement_group.title(), "点击位置与移动"
        )

    def test_collapsed_window_is_resizable_and_keeps_readable_width(self):
        self.window.show()
        self.app.processEvents()
        self.assertGreaterEqual(self.window.minimumWidth(), 614)
        self.assertGreaterEqual(self.window.advanced_toggle.minimumWidth(), 104)
        longest_hotkey = "Ctrl+Alt+Shift+Win+PrintScreen"
        for edit in (self.window.start_key_edit, self.window.stop_key_edit):
            required_width = edit.fontMetrics().horizontalAdvance(
                longest_hotkey + "  "
            )
            self.assertGreaterEqual(edit.minimumWidth(), required_width)
        self.assertGreater(self.window.maximumWidth(), 10_000)
        self.assertIsNone(self.window.parentWidget())
        self.assertEqual(self.window.hold_min.value(), 10)
        self.assertEqual(self.window.hold_max.value(), 10)
        self.assertEqual(self.window.jitter_concentration.custom_spin.suffix(), "")
        self.assertTrue(
            bool(self.window.windowFlags() & Qt.WindowMinimizeButtonHint)
        )
        self.assertTrue(
            bool(self.window.windowFlags() & Qt.WindowMaximizeButtonHint)
        )

        self.window.resize(820, self.window.height())
        self.window._toggle_advanced()
        self.window._toggle_advanced()
        self.app.processEvents()
        self.assertGreaterEqual(self.window.width(), 820)

    def test_hotkey_width_constraint_scales_with_screen_density(self):
        edit = self.window.start_key_edit
        base_width = edit.minimumWidth()
        for density in (1.5, 2.0):
            apply_widget_geometry_scale(
                [edit],
                ui_scale=1.0,
                density_ratio=density,
            )
            self.assertGreaterEqual(
                edit.minimumWidth(),
                round(base_width * density),
            )

    def test_middle_button_notice_is_shown_only_on_first_selection(self):
        class Host:
            def __init__(self, path):
                self.settings = QSettings(path, QSettings.IniFormat)
                self.run_controller = mock.Mock(is_active=False)
                self.hotkey_controller = mock.Mock()
                self._close_state_saved = False

            def hide_running_status_overlay(self, _activity=None):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            host = Host(os.path.join(temp_dir, "config.ini"))
            window = AutoClickerToolWindow(host, None)
            self.addCleanup(window.deleteLater)
            with mock.patch.object(QMessageBox, "information") as information:
                window.btn_type_combo.setCurrentIndex(1)
                window.btn_type_combo.setCurrentIndex(2)
                window.btn_type_combo.setCurrentIndex(1)
            information.assert_called_once()
            self.assertTrue(
                host.settings.value(
                    "onboarding/auto_clicker_middle_button_v1_seen",
                    False,
                    type=bool,
                )
            )

    def test_same_start_and_stop_hotkey_is_kept_instead_of_split(self):
        """反馈项：同键启停必须保留，不得在加载时被改写拆开。"""

        class Host:
            def __init__(self, path):
                self.settings = QSettings(path, QSettings.IniFormat)
                self.settings.setValue("auto_clicker/start_hotkey", "F6")
                self.settings.setValue("auto_clicker/stop_hotkey", "F6")
                self.run_controller = mock.Mock(is_active=False)
                self.hotkey_controller = mock.Mock()
                self._close_state_saved = False

            def hide_running_status_overlay(self, _activity=None):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            host = Host(os.path.join(temp_dir, "config.ini"))
            window = AutoClickerToolWindow(host, None)
            self.addCleanup(window.deleteLater)

            self.assertEqual(window.start_key_edit.text(), "F6")
            self.assertEqual(window.stop_key_edit.text(), "F6")
            self.assertEqual(
                host.settings.value("auto_clicker/stop_hotkey"),
                "F6",
                "同键设置不得在窗口加载时被自动改写",
            )
            window._hotkeys_active = True
            self.assertTrue(window.hotkey_bindings().get("toggle"))
            window._update_banner()
            self.assertIn("启动/停止", window.banner_label.text())

    def test_custom_speed_uses_seconds_and_collects_both_concentrations(self):
        self.window.speed_combo.setCurrentIndex(
            self.window.speed_combo.count() - 1
        )
        self.assertFalse(self.window.random_interval_group.isHidden())
        self.window.interval_base_spin.setValue(0.25)
        self.window.chk_random_interval.setChecked(True)
        self.window.interval_variation_spin.setValue(0.05)
        self.window.interval_concentration.combo.setCurrentIndex(2)
        self.window.chk_jitter.setChecked(True)
        self.window.jitter_x_spin.setValue(20)
        self.window.jitter_y_spin.setValue(8)
        self.window.jitter_shape_combo.setCurrentIndex(1)
        self.window.jitter_concentration.combo.setCurrentIndex(1)

        task = self.window._collect_task()

        self.assertEqual(task["ac_interval_base"], "0.25")
        self.assertEqual(task["ac_interval_variation"], "0.05")
        self.assertEqual(task["ac_interval_concentration"], "80")
        self.assertEqual(task["ac_jitter_shape"], "ellipse")
        self.assertEqual(task["ac_jitter_concentration"], "50")
        self.assertEqual(task["ac_jitter_h"], "20")
        self.assertEqual(task["ac_jitter_v"], "8")

    def test_point_pick_enables_fixed_center_and_fills_coordinates(self):
        self.window._on_fixed_point_picked("321,654")
        self.assertTrue(self.window.chk_fixed_center.isChecked())
        self.assertEqual(self.window.fixed_x_spin.value(), 321)
        self.assertEqual(self.window.fixed_y_spin.value(), 654)


class AutoClickerExecutionTests(unittest.TestCase):
    def setUp(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            self.engine = RPAEngine(defer_backends=True)
        run_id = self.engine.reserve_run()
        self.engine.lifecycle.mark_running(run_id)
        self.run_id = run_id
        self.engine.move_duration = 0.0

    def tearDown(self):
        self.engine.finish_run(self.run_id)

    @staticmethod
    def _context(task):
        return StepActionContext(
            command=TASK_TYPE_AUTO_CLICKER,
            value="",
            retry=-1,
            step_info={"step": 1, "loop": 1, "cmd": "连点器"},
            cache_key="",
            confidence=0.8,
            use_gray=True,
            task=task,
        )

    def test_three_level_ranges_are_executed_instead_of_ignored(self):
        task = {
            "ac_button": "left",
            "ac_center_mode": "current",
            "ac_big_loop_min": "2",
            "ac_big_loop_max": "2",
            "ac_sub_loop_min": "2",
            "ac_sub_loop_max": "2",
            "ac_click_count_min": "3",
            "ac_click_count_max": "3",
            "ac_interval_base": "0",
            "ac_interval_random_en": False,
            "ac_press_duration_min": "0",
            "ac_press_duration_max": "0",
            "ac_sub_interval_min": "0",
            "ac_sub_interval_max": "0",
            "ac_big_interval_min": "0",
            "ac_big_interval_max": "0",
        }
        with mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ) as click, mock.patch.object(
            self.engine, "_wait_interruptibly", return_value=True
        ):
            result = self.engine._execute_auto_clicker(self._context(task))

        self.assertEqual(result, "success")
        self.assertEqual(click.call_count, 12)

    def test_click_interval_applies_between_single_click_groups(self):
        task = {
            "ac_button": "left",
            "ac_center_mode": "current",
            "ac_big_loop_min": "3",
            "ac_big_loop_max": "3",
            "ac_sub_loop_min": "1",
            "ac_sub_loop_max": "1",
            "ac_click_count_min": "1",
            "ac_click_count_max": "1",
            "ac_interval_base": "0.5",
            "ac_interval_random_en": False,
            "ac_press_duration_min": "0",
            "ac_press_duration_max": "0",
            "ac_sub_interval_min": "0",
            "ac_sub_interval_max": "0",
            "ac_big_interval_min": "0",
            "ac_big_interval_max": "0",
        }
        waits = []

        def record_wait(seconds, **_kwargs):
            waits.append(seconds)
            return True

        with mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ) as click, mock.patch.object(
            self.engine, "_wait_interruptibly", side_effect=record_wait
        ):
            result = self.engine._execute_auto_clicker(self._context(task))

        self.assertEqual(result, "success")
        self.assertEqual(click.call_count, 3)
        self.assertEqual(waits.count(0.5), 2)

    def test_click_interval_and_loop_pause_are_additive(self):
        task = {
            "ac_button": "left",
            "ac_center_mode": "current",
            "ac_big_loop_min": "1",
            "ac_big_loop_max": "1",
            "ac_sub_loop_min": "2",
            "ac_sub_loop_max": "2",
            "ac_click_count_min": "2",
            "ac_click_count_max": "2",
            "ac_interval_base": "0.5",
            "ac_interval_random_en": False,
            "ac_press_duration_min": "0",
            "ac_press_duration_max": "0",
            "ac_sub_interval_min": "0.2",
            "ac_sub_interval_max": "0.2",
            "ac_big_interval_min": "0",
            "ac_big_interval_max": "0",
        }
        waits = []

        def record_wait(seconds, **_kwargs):
            waits.append(seconds)
            return True

        with mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ) as click, mock.patch.object(
            self.engine, "_wait_interruptibly", side_effect=record_wait
        ):
            result = self.engine._execute_auto_clicker(self._context(task))

        self.assertEqual(result, "success")
        self.assertEqual(click.call_count, 4)
        self.assertEqual(waits.count(0.5), 3)
        self.assertEqual(waits.count(0.2), 1)

    def test_shape_offset_and_curve_reach_the_click_primitive(self):
        task = {
            "ac_button": "left",
            "ac_center_mode": "fixed",
            "ac_fixed_x": "100",
            "ac_fixed_y": "200",
            "ac_big_loop_min": "1",
            "ac_big_loop_max": "1",
            "ac_sub_loop_min": "1",
            "ac_sub_loop_max": "1",
            "ac_click_count_min": "1",
            "ac_click_count_max": "1",
            "ac_interval_base": "0",
            "ac_interval_random_en": False,
            "ac_press_duration_min": "0",
            "ac_press_duration_max": "0",
            "ac_jitter_h": "20",
            "ac_jitter_v": "8",
            "ac_jitter_shape": "ellipse",
            "ac_jitter_concentration": "50",
            "ac_move_curve_strength": "0.7",
        }
        with mock.patch(
            "fukua_rpa.actions.flow.sample_pointer_offset",
            return_value=(3.0, -2.0),
        ) as sample_offset, mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ) as click:
            result = self.engine._execute_auto_clicker(self._context(task))

        self.assertEqual(result, "success")
        sample_offset.assert_called_once_with(20.0, 8.0, "ellipse", 50)
        self.assertEqual(click.call_args.args[:2], (103.0, 198.0))
        self.assertEqual(click.call_args.kwargs["curve_strength"], 0.7)

    def test_local_stop_prevents_a_no_move_click_from_slipping_through(self):
        with mock.patch.object(self.engine, "_tracked_mouse_down") as mouse_down:
            result = self.engine._auto_clicker_click(
                10,
                20,
                "left",
                0.0,
                0.0,
                stop_requested=lambda: True,
                move_first=False,
            )

        self.assertFalse(result)
        mouse_down.assert_not_called()


class AutoClickerCountRangeAlignmentTests(unittest.TestCase):
    """FR-SCOPE009-001：界面可设范围必须与执行器钳制范围取自同一上限。

    基线里界面写死 `setRange(1, 9_999_999)`，而执行器（`actions/flow.py` 的
    `_number_pair`）经 `_parse_optional_float` 把超限值静默钳到 1 000 000：
    用户填 9 999 999 会被截断且界面毫无提示。修复后两者共用
    `fukua_rpa.constants.AUTO_CLICKER_COUNT_MAX`。

    上限的判定不导入修复后新增的常量，而是**行为测量**：真实跑一次连点器并
    捕获 `_sample_count` 传给 `random.randint` 的边界。这样红侧失败是「界面范围
    ≠ 执行器范围」的断言失败，而不是「常量不存在」的导入错误。
    """

    #: 远超界面与执行器两者的输入，用于逼出执行器的真实上限。
    OVER_LIMIT = 99_999_999
    IN_RANGE = 5

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _measure_executor_cap(self):
        """返回 `(cap, captured)`：超限输入下执行器实际生效的采样上界。"""
        task = {
            "ac_button": "left",
            "ac_center_mode": "current",
            "ac_interval_base": "0",
            "ac_interval_random_en": False,
            "ac_press_duration_min": "0",
            "ac_press_duration_max": "0",
            "ac_sub_interval_min": "0",
            "ac_sub_interval_max": "0",
            "ac_big_interval_min": "0",
            "ac_big_interval_max": "0",
        }
        over = str(self.OVER_LIMIT)
        for key in (
            "ac_click_count_min",
            "ac_click_count_max",
            "ac_sub_loop_min",
            "ac_sub_loop_max",
            "ac_big_loop_min",
            "ac_big_loop_max",
        ):
            task[key] = over

        engine = RPAEngine(defer_backends=True)
        run_id = engine.reserve_run()
        engine.lifecycle.mark_running(run_id)
        engine.move_duration = 0.0
        captured: list[tuple[int, int]] = []

        def fake_randint(low, high):
            # 只记录边界、恒定返回 1，避免真的点 1 000 000 次。
            captured.append((low, high))
            return 1

        try:
            with mock.patch.object(
                engine, "_auto_clicker_click", return_value=True
            ), mock.patch.object(
                engine, "_wait_interruptibly", return_value=True
            ), mock.patch(
                "fukua_rpa.actions.flow.random.randint", side_effect=fake_randint
            ):
                result = engine._execute_auto_clicker(
                    self._context(task)
                )
        finally:
            engine.finish_run(run_id)

        self.assertEqual(result, "success")
        self.assertEqual(len(captured), 3, f"三层参数应各采样一次：{captured}")
        highs = {high for _low, high in captured}
        self.assertEqual(len(highs), 1, f"三层上限应一致：{captured}")
        return highs.pop(), captured

    @staticmethod
    def _context(task):
        return StepActionContext(
            command=TASK_TYPE_AUTO_CLICKER,
            value="",
            retry=-1,
            step_info={"step": 1, "loop": 1, "cmd": "连点器"},
            cache_key="",
            confidence=0.8,
            use_gray=True,
            task=task,
        )

    def _make_window(self):
        window = AutoClickerToolWindow(parent=None, engine=mock.MagicMock())
        self.addCleanup(window.deleteLater)
        return window

    def test_ui_count_range_matches_the_executor_clamp(self):
        """界面「连击」上下限的可设范围必须等于执行器实际上限。"""
        cap, captured = self._measure_executor_cap()
        window = self._make_window()

        for label, spin in (
            ("连击下限", window.click_count_min),
            ("连击上限", window.click_count_max),
        ):
            with self.subTest(control=label):
                self.assertEqual(
                    spin.maximum(),
                    cap,
                    f"{label}控件可设上限 {spin.maximum()} 与执行器上限 {cap} "
                    f"不一致：用户填写的超限值会被静默截断（采样边界 {captured}）",
                )

    def test_shared_count_limit_constant_matches_the_executor_clamp(self):
        """单一来源常量必须与执行器实际钳制上限一致，且界面取自该常量。"""
        cap, _captured = self._measure_executor_cap()
        self.assertIsNotNone(
            AUTO_CLICKER_COUNT_MAX,
            "缺少界面与执行器共用的连击次数上限常量，两者会再次脱节",
        )
        self.assertEqual(AUTO_CLICKER_COUNT_MAX, cap)

    def test_in_range_count_is_not_clamped(self):
        """不变量：范围内的合法取值不得被改动。"""
        task = {
            "ac_button": "left",
            "ac_center_mode": "current",
            "ac_click_count_min": str(self.IN_RANGE),
            "ac_click_count_max": str(self.IN_RANGE),
            "ac_sub_loop_min": "1",
            "ac_sub_loop_max": "1",
            "ac_big_loop_min": "1",
            "ac_big_loop_max": "1",
            "ac_interval_base": "0",
            "ac_interval_random_en": False,
            "ac_press_duration_min": "0",
            "ac_press_duration_max": "0",
            "ac_sub_interval_min": "0",
            "ac_sub_interval_max": "0",
            "ac_big_interval_min": "0",
            "ac_big_interval_max": "0",
        }
        engine = RPAEngine(defer_backends=True)
        run_id = engine.reserve_run()
        engine.lifecycle.mark_running(run_id)
        engine.move_duration = 0.0
        captured: list[tuple[int, int]] = []

        def fake_randint(low, high):
            captured.append((low, high))
            return 1

        try:
            with mock.patch.object(
                engine, "_auto_clicker_click", return_value=True
            ), mock.patch.object(
                engine, "_wait_interruptibly", return_value=True
            ), mock.patch(
                "fukua_rpa.actions.flow.random.randint", side_effect=fake_randint
            ):
                result = engine._execute_auto_clicker(self._context(task))
        finally:
            engine.finish_run(run_id)

        self.assertEqual(result, "success")
        self.assertIn((self.IN_RANGE, self.IN_RANGE), captured)


if __name__ == "__main__":
    unittest.main()
