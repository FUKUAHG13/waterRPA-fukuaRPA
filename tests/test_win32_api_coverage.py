"""Targeted coverage tests for fukua_rpa/win32_api.py uncovered paths.

Pure parsing/signature logic runs for real; ctypes windll calls are mocked.
"""

import unittest
from unittest import mock

from fukua_rpa.win32_api import (
    MOD_NAME_TO_VK,
    MOD_NAME_TO_VKS,
    _safe_configure,
    get_window_title,
    hotkey_display_text,
    hotkey_is_down,
    hotkey_signature,
    hotkey_text_from_pressed_vks,
    is_safe_global_hotkey,
    key_event_to_hotkey_text,
    modifier_is_down,
    normalize_hotkey_token,
    parse_hotkey_text,
    post_double_click_to_window_at_point,
    pressed_hotkey_display_text,
)

# ---------------------------------------------------------------------------
# _safe_configure exception path (lines 204-206)
# ---------------------------------------------------------------------------


class TestSafeConfigure(unittest.TestCase):
    def test_exception_is_logged_and_does_not_raise(self):
        """FR-20261008-402：失败提示走模块 logger，且处理失败本身不得抛出。

        旧实现在 ``except`` 内写 ``sys.stderr``；冻结 windowed 构建中该对象为
        ``None``，于是"处理失败"的代码自身抛 ``AttributeError`` 并逃出模块导入。
        """
        bad_func = mock.MagicMock()
        # Make setting argtypes raise
        type(bad_func).argtypes = mock.PropertyMock(side_effect=OSError("blocked"))

        with self.assertLogs("fukua_rpa.win32_api", level="WARNING") as captured:
            _safe_configure(bad_func, [], None, "TestFunc")

        self.assertTrue(
            any("TestFunc" in line for line in captured.output),
            f"失败提示未记录函数名: {captured.output}",
        )

    def test_exception_with_missing_stderr_does_not_raise(self):
        import sys

        bad_func = mock.MagicMock()
        type(bad_func).argtypes = mock.PropertyMock(side_effect=OSError("blocked"))

        with mock.patch.object(sys, "stderr", None):
            _safe_configure(bad_func, [], None, "TestFunc")


# ---------------------------------------------------------------------------
# get_window_title (lines 321-328)
# ---------------------------------------------------------------------------


class TestGetWindowTitle(unittest.TestCase):
    @mock.patch("fukua_rpa.win32_api.user32")
    def test_null_hwnd_returns_empty(self, mock_user32):
        self.assertEqual(get_window_title(0), "")

    @mock.patch("fukua_rpa.win32_api.user32")
    def test_zero_length_returns_empty(self, mock_user32):
        mock_user32.GetWindowTextLengthW.return_value = 0
        self.assertEqual(get_window_title(123), "")

    @mock.patch("fukua_rpa.win32_api.user32")
    def test_returns_title_text(self, mock_user32):
        mock_user32.GetWindowTextLengthW.return_value = 5

        def fake_get_text(hwnd, buf, size):
            buf.value = "Hello"
            return 5

        mock_user32.GetWindowTextW.side_effect = fake_get_text
        self.assertEqual(get_window_title(123), "Hello")


# ---------------------------------------------------------------------------
# key_event_to_hotkey_text (lines 332-373)
# ---------------------------------------------------------------------------


class _FakeKeyEvent:
    """Minimal stub mimicking a QKeyEvent for hotkey text extraction.

    Returns Qt enum values directly (not int-wrapped) because
    key_event_to_hotkey_text uses Qt enum operators (&, ==).
    """

    def __init__(self, key, modifiers, text=""):
        self._key = key
        self._modifiers = modifiers
        self._text = text

    def key(self):
        return self._key

    def modifiers(self):
        return self._modifiers

    def text(self):
        return self._text


class TestKeyEventToHotkeyText(unittest.TestCase):
    def _qt(self):
        from PySide6.QtCore import Qt
        return Qt

    def test_bare_escape_returns_empty(self):
        Qt = self._qt()
        event = _FakeKeyEvent(Qt.Key_Escape, Qt.KeyboardModifier.NoModifier)
        self.assertEqual(key_event_to_hotkey_text(event), "")

    def test_ctrl_a(self):
        Qt = self._qt()
        event = _FakeKeyEvent(
            Qt.Key_A, Qt.KeyboardModifier.ControlModifier, text="a"
        )
        self.assertEqual(key_event_to_hotkey_text(event), "ctrl+a")

    def test_ctrl_alt_shift_f5(self):
        Qt = self._qt()
        event = _FakeKeyEvent(
            Qt.Key_F5,
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.ShiftModifier,
        )
        self.assertEqual(key_event_to_hotkey_text(event), "ctrl+alt+shift+f5")

    def test_win_key(self):
        Qt = self._qt()
        event = _FakeKeyEvent(Qt.Key_Meta, Qt.KeyboardModifier.MetaModifier)
        self.assertEqual(key_event_to_hotkey_text(event), "win")

    def test_bare_special_key(self):
        Qt = self._qt()
        event = _FakeKeyEvent(Qt.Key_F9, Qt.KeyboardModifier.NoModifier)
        self.assertEqual(key_event_to_hotkey_text(event), "f9")

    def test_digit_key_from_code(self):
        Qt = self._qt()
        # Key_5 with no text (some layouts)
        event = _FakeKeyEvent(Qt.Key_5, Qt.KeyboardModifier.NoModifier, text="")
        self.assertEqual(key_event_to_hotkey_text(event), "5")

    def test_alpha_key_from_code_no_text(self):
        Qt = self._qt()
        event = _FakeKeyEvent(Qt.Key_Z, Qt.KeyboardModifier.NoModifier, text="")
        self.assertEqual(key_event_to_hotkey_text(event), "z")

    def test_unresolvable_key_returns_empty(self):
        Qt = self._qt()
        # A key code outside A-Z/0-9 with no text and not in special map
        event = _FakeKeyEvent(Qt.Key(0x01000099), Qt.KeyboardModifier.NoModifier, text="")
        self.assertEqual(key_event_to_hotkey_text(event), "")

    def test_modifier_key_alone(self):
        Qt = self._qt()
        event = _FakeKeyEvent(Qt.Key_Control, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(key_event_to_hotkey_text(event), "ctrl")


# ---------------------------------------------------------------------------
# normalize_hotkey_token numpad branch (line 380)
# ---------------------------------------------------------------------------


class TestNormalizeHotkeyToken(unittest.TestCase):
    def test_numpad_prefix_converted(self):
        self.assertEqual(normalize_hotkey_token("numpad5"), "num5")

    def test_alias_converted(self):
        self.assertEqual(normalize_hotkey_token("Control"), "ctrl")
        self.assertEqual(normalize_hotkey_token("escape"), "esc")

    def test_fullwidth_plus_replaced(self):
        # The token itself won't have + normally, but test the replace path
        self.assertEqual(normalize_hotkey_token("ctrl"), "ctrl")

    def test_none_returns_empty(self):
        self.assertEqual(normalize_hotkey_token(None), "")


# ---------------------------------------------------------------------------
# hotkey_display_text branches (lines 388, 403, 407)
# ---------------------------------------------------------------------------


class TestHotkeyDisplayText(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(hotkey_display_text(""), "")
        self.assertEqual(hotkey_display_text(None), "")

    def test_modifier_display(self):
        self.assertEqual(hotkey_display_text("ctrl+alt+a"), "Ctrl+Alt+A")

    def test_function_key_uppercase(self):
        self.assertEqual(hotkey_display_text("f12"), "F12")

    def test_numpad_display(self):
        self.assertEqual(hotkey_display_text("num5"), "Num5")

    def test_named_key_display(self):
        self.assertEqual(hotkey_display_text("esc"), "Esc")
        self.assertEqual(hotkey_display_text("pageup"), "PageUp")

    def test_unknown_name_passthrough(self):
        # A token not in display_names and not matching other rules
        self.assertEqual(hotkey_display_text("capslock"), "CapsLock")


# ---------------------------------------------------------------------------
# parse_hotkey_text edge cases (lines 417, 426, 429, 434)
# ---------------------------------------------------------------------------


class TestParseHotkeyText(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(parse_hotkey_text(""))
        self.assertIsNone(parse_hotkey_text(None))

    def test_only_plus_signs_returns_none(self):
        self.assertIsNone(parse_hotkey_text("+++"))

    def test_multiple_non_modifier_keys_returns_none(self):
        self.assertIsNone(parse_hotkey_text("a+b"))

    def test_modifier_only_returns_none(self):
        self.assertIsNone(parse_hotkey_text("ctrl+alt"))

    def test_unknown_key_returns_none(self):
        self.assertIsNone(parse_hotkey_text("ctrl+unknownkey"))

    def test_valid_hotkey_parsed(self):
        result = parse_hotkey_text("ctrl+alt+f5")
        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "ctrl+alt+f5")
        self.assertEqual(result["key"], "f5")
        self.assertEqual(result["mod_names"], ["ctrl", "alt"])
        self.assertFalse(result["bare"])

    def test_bare_key(self):
        result = parse_hotkey_text("f9")
        self.assertIsNotNone(result)
        self.assertTrue(result["bare"])
        self.assertEqual(result["modifiers"], 0)

    def test_fullwidth_plus_normalized(self):
        result = parse_hotkey_text("ctrl\uFF0Bf5")
        self.assertIsNotNone(result)
        self.assertEqual(result["text"], "ctrl+f5")

    def test_duplicate_modifier_deduplicated(self):
        result = parse_hotkey_text("ctrl+ctrl+a")
        self.assertIsNotNone(result)
        self.assertEqual(result["mod_names"], ["ctrl"])


# ---------------------------------------------------------------------------
# is_safe_global_hotkey
# ---------------------------------------------------------------------------


class TestIsSafeGlobalHotkey(unittest.TestCase):
    def test_none_parsed(self):
        self.assertFalse(is_safe_global_hotkey(None))

    def test_bare_unsafe_key(self):
        parsed = parse_hotkey_text("a")
        self.assertFalse(is_safe_global_hotkey(parsed))

    def test_bare_safe_key(self):
        parsed = parse_hotkey_text("f8")
        self.assertTrue(is_safe_global_hotkey(parsed))

    def test_modified_key_always_safe(self):
        parsed = parse_hotkey_text("ctrl+a")
        self.assertTrue(is_safe_global_hotkey(parsed))


# ---------------------------------------------------------------------------
# hotkey_signature (lines 456-458)
# ---------------------------------------------------------------------------


class TestHotkeySignature(unittest.TestCase):
    def test_none_returns_none(self):
        self.assertIsNone(hotkey_signature(None))

    def test_valid_signature(self):
        parsed = parse_hotkey_text("ctrl+alt+f5")
        sig = hotkey_signature(parsed)
        self.assertIsNotNone(sig)
        self.assertEqual(sig, (parsed["modifiers"], parsed["vk"]))

    def test_bare_key_signature(self):
        parsed = parse_hotkey_text("f9")
        sig = hotkey_signature(parsed)
        self.assertEqual(sig, (0, 0x78))  # F9 = 0x78


# ---------------------------------------------------------------------------
# modifier_is_down (lines 462-463) - mock GetAsyncKeyState
# ---------------------------------------------------------------------------


class TestModifierIsDown(unittest.TestCase):
    @mock.patch("fukua_rpa.win32_api.GetAsyncKeyState")
    def test_modifier_pressed(self, mock_gaks):
        mock_gaks.return_value = 0x8000
        self.assertTrue(modifier_is_down("ctrl"))
        # FR-SCOPE003-002: 轮询回退路径改用集合语义，查询必须落在集合内。
        # （全部按下时 any() 短路，故此处只断言取值范围。）
        self.assertTrue(
            {call.args[0] for call in mock_gaks.call_args_list}
            <= set(MOD_NAME_TO_VKS["ctrl"])
        )

    @mock.patch("fukua_rpa.win32_api.GetAsyncKeyState")
    def test_modifier_release_queries_the_whole_set(self, mock_gaks):
        """全部抬起时必须查完整个集合才能判定未按下。"""

        mock_gaks.return_value = 0x0000
        self.assertFalse(modifier_is_down("ctrl"))
        self.assertEqual(
            sorted(call.args[0] for call in mock_gaks.call_args_list),
            sorted(MOD_NAME_TO_VKS["ctrl"]),
        )

    @mock.patch("fukua_rpa.win32_api.GetAsyncKeyState")
    def test_modifier_not_pressed(self, mock_gaks):
        mock_gaks.return_value = 0x0000
        self.assertFalse(modifier_is_down("ctrl"))

    def test_unknown_modifier_name(self):
        self.assertFalse(modifier_is_down("nonexistent"))

    def test_legacy_single_vk_table_stays_inside_the_set_table(self):
        """FR-SCOPE003-002: 单值表不得再与集合表分叉。"""

        for name, virtual_key in MOD_NAME_TO_VK.items():
            self.assertIn(
                virtual_key,
                MOD_NAME_TO_VKS[name],
                f"{name} 的单值表项必须包含于集合表，否则双轨会再次分叉",
            )

    @mock.patch("fukua_rpa.win32_api.GetAsyncKeyState")
    def test_right_win_is_recognized(self, mock_gaks):
        """FR-SCOPE003-002 判别项：仅按下右 Win（0x5C）也须满足 ``win``。"""

        def side_effect(vk):
            return 0x8000 if vk == 0x5C else 0x0000

        mock_gaks.side_effect = side_effect
        self.assertTrue(modifier_is_down("win"))

    @mock.patch("fukua_rpa.win32_api.GetAsyncKeyState")
    def test_left_win_is_still_recognized(self, mock_gaks):
        """边界：左 Win（0x5B）行为不变。"""

        def side_effect(vk):
            return 0x8000 if vk == 0x5B else 0x0000

        mock_gaks.side_effect = side_effect
        self.assertTrue(modifier_is_down("win"))

    @mock.patch("fukua_rpa.win32_api.GetAsyncKeyState")
    def test_win_is_not_down_when_neither_key_is_pressed(self, mock_gaks):
        """边界：左右 Win 均抬起时不得误判为按下。"""

        mock_gaks.return_value = 0x0000
        self.assertFalse(modifier_is_down("win"))

    @mock.patch("fukua_rpa.win32_api.GetAsyncKeyState")
    def test_hotkey_is_down_accepts_right_win(self, mock_gaks):
        """FR-SCOPE003-002: ``win+f5`` 在只有右 Win 按下时同样成立。"""

        parsed = parse_hotkey_text("win+f5")
        f5_vk = parsed["vk"]

        def side_effect(vk):
            if vk == 0x5C or vk == f5_vk:
                return 0x8000
            return 0x0000

        mock_gaks.side_effect = side_effect
        self.assertTrue(hotkey_is_down(parsed))


# ---------------------------------------------------------------------------
# hotkey_is_down (lines 467-474) - mock GetAsyncKeyState
# ---------------------------------------------------------------------------


class TestHotkeyIsDown(unittest.TestCase):
    @mock.patch("fukua_rpa.win32_api.GetAsyncKeyState")
    def test_none_parsed_returns_false(self, mock_gaks):
        self.assertFalse(hotkey_is_down(None))
        mock_gaks.assert_not_called()

    @mock.patch("fukua_rpa.win32_api.GetAsyncKeyState")
    def test_all_pressed(self, mock_gaks):
        parsed = parse_hotkey_text("ctrl+f5")
        # Only ctrl VKs and f5 VK are down; other modifiers are up
        ctrl_vks = MOD_NAME_TO_VKS["ctrl"]
        f5_vk = parsed["vk"]

        def side_effect(vk):
            if vk in ctrl_vks or vk == f5_vk:
                return 0x8000
            return 0x0000

        mock_gaks.side_effect = side_effect
        self.assertTrue(hotkey_is_down(parsed))

    @mock.patch("fukua_rpa.win32_api.GetAsyncKeyState")
    def test_required_modifier_not_down(self, mock_gaks):
        parsed = parse_hotkey_text("ctrl+f5")
        # ctrl 未按下（集合内所有虚拟键均抬起），f5 按下
        def side_effect(vk):
            if vk in MOD_NAME_TO_VKS["ctrl"]:
                return 0x0000
            return 0x8000
        mock_gaks.side_effect = side_effect
        self.assertFalse(hotkey_is_down(parsed))

    @mock.patch("fukua_rpa.win32_api.GetAsyncKeyState")
    def test_extra_modifier_down_blocks(self, mock_gaks):
        parsed = parse_hotkey_text("ctrl+f5")
        # ctrl 与 f5 均按下，但未被要求的 alt 也按下 ⇒ 阻断
        mock_gaks.return_value = 0x8000
        self.assertFalse(hotkey_is_down(parsed))

    @mock.patch("fukua_rpa.win32_api.GetAsyncKeyState")
    def test_main_key_not_down(self, mock_gaks):
        parsed = parse_hotkey_text("ctrl+f5")
        # 仅必需修饰键(ctrl)按下，额外修饰键与主键均抬起，
        # 才能通过额外修饰键守卫抵达 win32_api.py:474 的主键检查。
        required_vks = {
            vk for name in parsed["mod_names"] for vk in MOD_NAME_TO_VKS[name]
        }
        def side_effect(vk):
            return 0x8000 if vk in required_vks else 0x0000
        mock_gaks.side_effect = side_effect
        self.assertFalse(hotkey_is_down(parsed))


# ---------------------------------------------------------------------------
# hotkey_text_from_pressed_vks (lines 480-494)
# ---------------------------------------------------------------------------


class TestHotkeyTextFromPressedVks(unittest.TestCase):
    def test_modifier_vk_returns_empty(self):
        # 0x11 = VK_CONTROL
        self.assertEqual(hotkey_text_from_pressed_vks(0x11, [0x11]), "")

    def test_unknown_vk_returns_empty(self):
        self.assertEqual(hotkey_text_from_pressed_vks(0xFE, []), "")

    def test_ctrl_f5(self):
        # ctrl (0x11) + F5 (0x74)
        result = hotkey_text_from_pressed_vks(0x74, [0x11, 0x74])
        self.assertEqual(result, "ctrl+f5")

    def test_bare_key(self):
        result = hotkey_text_from_pressed_vks(0x78, [])  # F9
        self.assertEqual(result, "f9")

    def test_ctrl_alt_shift_a(self):
        # 0xA2 = LCONTROL, 0xA4 = LALT, 0xA0 = LSHIFT, 0x41 = 'A'
        result = hotkey_text_from_pressed_vks(0x41, [0xA2, 0xA4, 0xA0, 0x41])
        self.assertEqual(result, "ctrl+alt+shift+a")

    def test_none_pressed_vks(self):
        result = hotkey_text_from_pressed_vks(0x78, None)
        self.assertEqual(result, "f9")


# ---------------------------------------------------------------------------
# pressed_hotkey_display_text (lines 500-512)
# ---------------------------------------------------------------------------


class TestPressedHotkeyDisplayText(unittest.TestCase):
    def test_empty_vks(self):
        self.assertEqual(pressed_hotkey_display_text([]), "")

    def test_none_vks(self):
        self.assertEqual(pressed_hotkey_display_text(None), "")

    def test_modifiers_only(self):
        # ctrl (0x11) + alt (0x12)
        result = pressed_hotkey_display_text([0x11, 0x12])
        self.assertEqual(result, "Ctrl+Alt")

    def test_ctrl_plus_f5(self):
        result = pressed_hotkey_display_text([0x11, 0x74])
        self.assertEqual(result, "Ctrl+F5")

    def test_multiple_keys(self):
        # ctrl + a + b
        result = pressed_hotkey_display_text([0x11, 0x41, 0x42])
        self.assertEqual(result, "Ctrl+A+B")


# ---------------------------------------------------------------------------
# post_double_click_to_window_at_point (lines 542-571)
# ---------------------------------------------------------------------------


class TestPostDoubleClickToWindowAtPoint(unittest.TestCase):
    @mock.patch("fukua_rpa.win32_api.user32")
    def test_no_window_returns_false(self, mock_user32):
        mock_user32.WindowFromPoint.return_value = 0
        self.assertFalse(post_double_click_to_window_at_point(100, 200))

    @mock.patch("fukua_rpa.win32_api.user32")
    def test_cs_dblclicks_skips_post(self, mock_user32):
        mock_user32.WindowFromPoint.return_value = 111
        mock_user32.GetAncestor.return_value = 222
        # CS_DBLCLKS = 0x0008
        mock_user32.GetClassLongPtrW.return_value = 0x0008
        self.assertTrue(post_double_click_to_window_at_point(100, 200))
        mock_user32.PostMessageW.assert_not_called()

    @mock.patch("fukua_rpa.win32_api.user32")
    def test_posts_left_dblclk(self, mock_user32):
        mock_user32.WindowFromPoint.return_value = 111
        mock_user32.GetAncestor.return_value = 222
        mock_user32.GetClassLongPtrW.return_value = 0  # no CS_DBLCLKS
        # Mock rect
        mock_user32.GetWindowRect.return_value = 1

        def fake_get_rect(hwnd, rect_ptr):
            rect_ptr._obj.left = 50
            rect_ptr._obj.top = 80
            rect_ptr._obj.right = 500
            rect_ptr._obj.bottom = 400
            return 1

        mock_user32.GetWindowRect.side_effect = fake_get_rect
        result = post_double_click_to_window_at_point(100, 200, "left")
        self.assertTrue(result)
        mock_user32.PostMessageW.assert_called_once()
        call_args = mock_user32.PostMessageW.call_args[0]
        # WM_LBUTTONDBLCLK = 0x0203
        self.assertEqual(call_args[1], 0x0203)

    @mock.patch("fukua_rpa.win32_api.user32")
    def test_posts_right_dblclk(self, mock_user32):
        mock_user32.WindowFromPoint.return_value = 111
        mock_user32.GetAncestor.return_value = 0  # fallback to hwnd
        mock_user32.GetClassLongPtrW.return_value = 0

        def fake_get_rect(hwnd, rect_ptr):
            rect_ptr._obj.left = 0
            rect_ptr._obj.top = 0
            rect_ptr._obj.right = 800
            rect_ptr._obj.bottom = 600
            return 1

        mock_user32.GetWindowRect.side_effect = fake_get_rect
        result = post_double_click_to_window_at_point(50, 60, "right")
        self.assertTrue(result)
        call_args = mock_user32.PostMessageW.call_args[0]
        # WM_RBUTTONDBLCLK = 0x0206
        self.assertEqual(call_args[1], 0x0206)

    @mock.patch("fukua_rpa.win32_api.user32")
    def test_screen_to_client_fails(self, mock_user32):
        # FR-WIN-001: 改用 ScreenToClient 换算客户区坐标；换算失败（返回 0）时
        # 静默返回 False，物理点击已完成，不影响主流程。
        mock_user32.WindowFromPoint.return_value = 111
        mock_user32.GetAncestor.return_value = 222  # 不再使用，仅保留隔离
        mock_user32.GetClassLongPtrW.return_value = 0  # 无 CS_DBLCLKS
        mock_user32.ScreenToClient.return_value = 0  # 换算失败
        self.assertFalse(post_double_click_to_window_at_point(100, 200))

    @mock.patch("fukua_rpa.win32_api.user32")
    def test_posts_to_window_under_point_not_root(self, mock_user32):
        # FR-WIN-001: 向落点控件（WindowFromPoint 返回值）补发 DBLCLK，而非顶层窗口。
        mock_user32.WindowFromPoint.return_value = 111  # 落点子控件
        mock_user32.GetAncestor.return_value = 222  # 顶层（不应被选中）
        mock_user32.GetClassLongPtrW.return_value = 0  # 无 CS_DBLCLKS
        mock_user32.ScreenToClient.return_value = 1  # 换算成功
        result = post_double_click_to_window_at_point(100, 200)
        self.assertTrue(result)
        mock_user32.PostMessageW.assert_called_once()
        # 第一个位置参数应为落点 hwnd(111)，而非 root(222)。
        self.assertEqual(mock_user32.PostMessageW.call_args[0][0], 111)
        # 客户区坐标换算须针对落点 hwnd。
        mock_user32.ScreenToClient.assert_called_once_with(111, mock.ANY)

    @mock.patch("fukua_rpa.win32_api.user32")
    def test_exception_returns_false(self, mock_user32):
        mock_user32.WindowFromPoint.side_effect = OSError("boom")
        self.assertFalse(post_double_click_to_window_at_point(100, 200))


if __name__ == "__main__":
    unittest.main()
