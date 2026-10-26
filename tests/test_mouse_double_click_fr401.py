"""FR-401-001 回归测试：前台双击补发的落点坐标必须直接使用输入坐标。

System-DPI 回退 + 混合 DPI 下，WindowFromPoint/GetWindowRect 按虚拟化空间解释，
若先经 input_to_physical_coordinates 反算成物理像素再传入，落点窗口选择与客户区
坐标会错位（副屏 150% 偏移 1.5 倍）。修复后补发坐标与输入坐标直传一致。
"""

import unittest
from unittest import mock

from fukua_rpa.engine import RPAEngine


class DoubleClickCoordinatePassThroughTests(unittest.TestCase):
    """FR-401-001: 前台双击补发 DBLCLK 使用输入坐标直传，不再反算物理像素。"""

    def setUp(self):
        # B-20261024-B2-OBS-001：单元测试边界守卫。defer_backends 不含输入后端，
        # 不替换 input_backend 时 _perform_mouse_click_impl 会经真实
        # PyAutoGUIInputBackend.moveTo 移动真实光标；守卫另捕获任何绕过
        # input_backend 的意外 pyautogui 调用。
        self.pyautogui_guard = self.enterContext(
            mock.patch("fukua_rpa.input_backend.pyautogui")
        )

    def tearDown(self):
        self.pyautogui_guard.moveTo.assert_not_called()
        self.pyautogui_guard.mouseDown.assert_not_called()
        self.pyautogui_guard.mouseUp.assert_not_called()
        self.pyautogui_guard.keyDown.assert_not_called()
        self.pyautogui_guard.keyUp.assert_not_called()
        self.pyautogui_guard.scroll.assert_not_called()

    def _engine(self):
        engine = RPAEngine(defer_backends=True)
        # 与 test_dodge_onboarding 既有惯例一致：整体替换输入后端，
        # 双击回归只关心补发坐标，不发出任何真实输入。
        engine.input_backend = mock.Mock()
        engine._press_mouse_button = mock.Mock(return_value=True)
        engine.show_click_indicator = False
        engine.enable_dodge = False
        return engine

    def test_double_click_posts_input_coordinates_directly(self):
        engine = self._engine()
        with mock.patch(
            "fukua_rpa.win32_api.post_double_click_to_window_at_point"
        ) as post:
            engine._perform_mouse_click_impl(10, 20, 2, "left")
            post.assert_called_once_with(10, 20, "left")

    def test_single_click_does_not_post_double_click(self):
        engine = self._engine()
        with mock.patch(
            "fukua_rpa.win32_api.post_double_click_to_window_at_point"
        ) as post:
            engine._perform_mouse_click_impl(10, 20, 1, "left")
            post.assert_not_called()

    def test_right_double_click_passes_button_through(self):
        engine = self._engine()
        with mock.patch(
            "fukua_rpa.win32_api.post_double_click_to_window_at_point"
        ) as post:
            engine._perform_mouse_click_impl(-30, 45, 2, "right")
            post.assert_called_once_with(-30, 45, "right")


if __name__ == "__main__":
    unittest.main()
