"""连点器启停热键放开裸单键的回归测试。

需求：连点器「启动/停止快捷键」允许裸单键（A/T/数字/空格/Enter），
继续支持 Ctrl/Alt/Shift/Win 组合键；A+C 这类多普通键组合仍被拒绝。
主程序脚本热键、方案热键、按键映射的裸键禁令不受影响。
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox

from fukua_rpa.ui.auto_clicker_core import _START_HOTKEY_KEY, _STOP_HOTKEY_KEY
from fukua_rpa.ui.auto_clicker_mixins.hotkey import HotkeyMixin
from fukua_rpa.ui.controllers.hotkey_controller import HotkeyController
from fukua_rpa.win32_api import (
    HOTKEY_ID_AUTO_CLICKER_START,
    HOTKEY_ID_AUTO_CLICKER_STOP,
    MOD_NOREPEAT,
    is_safe_global_hotkey,
    parse_hotkey_text,
)


class _Harness(HotkeyMixin):
    """最小宿主：提供热键控件、配置与后端刷新桩，直接复用被测 Mixin。"""

    def __init__(self, settings=None, host=None):
        self.start_key_edit = QLineEdit("F6")
        self.stop_key_edit = QLineEdit("F7")
        self.banner_label = QLineEdit()  # 仅需 setText
        self.btn_type_combo = SimpleNamespace(
            currentText=lambda: "左键"
        )
        self._settings = settings
        self._host = host
        self._hotkeys_active = True
        self._registered_calls = []

    def _host_ref(self):
        return self._host

    def _adjust_size(self):
        return None

    @property
    def settings(self):
        return self._settings

    def _register_shortcuts(self):
        self._registered_calls.append(True)


class _Settings:
    """内存版 QSettings 替身，仅实现 value/setValue/sync。"""

    def __init__(self, initial=None):
        self._data = dict(initial or {})
        self.synced = 0

    def value(self, key, default=None):
        return self._data.get(key, default)

    def setValue(self, key, value):
        self._data[key] = value

    def sync(self):
        self.synced += 1


class HotkeyMixinBareKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_load_hotkey_accepts_bare_single_keys(self):
        cases = {
            _START_HOTKEY_KEY: ("A", "A"),
            _STOP_HOTKEY_KEY: ("5", "5"),
        }
        settings = _Settings({key: stored for key, (stored, _) in cases.items()})
        for key, (stored, expected) in cases.items():
            with self.subTest(key=key, stored=stored):
                val = _Harness._load_hotkey(settings, key, "F6")
                self.assertEqual(val, expected)

    def test_load_hotkey_accepts_space_enter_and_combos(self):
        for text, expected in (
            ("Space", "Space"),
            ("Enter", "Enter"),
            ("ctrl+a", "Ctrl+A"),
            ("Ctrl+Alt+Shift+Win+PrintScreen", "Ctrl+Alt+Shift+Win+PrintScreen"),
        ):
            with self.subTest(text=text):
                settings = _Settings({_START_HOTKEY_KEY: text})
                self.assertEqual(
                    _Harness._load_hotkey(settings, _START_HOTKEY_KEY, "F6"),
                    expected,
                )

    def test_load_hotkey_falls_back_for_multi_plain_key(self):
        # A+C 这类多普通键无法解析，必须回退默认，绝不进入热键状态。
        settings = _Settings({_START_HOTKEY_KEY: "A+C"})
        self.assertEqual(
            _Harness._load_hotkey(settings, _START_HOTKEY_KEY, "F6"), "F6"
        )

    def test_hotkey_bindings_accept_bare_keys_and_toggle(self):
        harness = _Harness()
        for start, stop in (("A", "A"), ("A", "T"), ("A", "Ctrl+S")):
            with self.subTest(start=start, stop=stop):
                harness.start_key_edit.setText(start)
                harness.stop_key_edit.setText(stop)
                bindings = harness.hotkey_bindings()
                self.assertEqual(bindings["start_hotkey"], parse_hotkey_text(start)["display"])
                self.assertEqual(bindings["stop_hotkey"], parse_hotkey_text(stop)["display"])
                self.assertEqual(bindings["toggle"], start == stop or start.lower() == stop.lower())

    def test_hotkey_bindings_toggle_uses_one_signature(self):
        harness = _Harness()
        harness.start_key_edit.setText("a")
        harness.stop_key_edit.setText("A")  # 大小写归一后同键
        bindings = harness.hotkey_bindings()
        self.assertTrue(bindings["toggle"])
        self.assertEqual(bindings["start_hotkey"], "A")
        self.assertEqual(bindings["stop_hotkey"], "A")

    def test_hotkey_bindings_reject_multi_plain_key(self):
        harness = _Harness()
        harness.start_key_edit.setText("A+C")
        harness.stop_key_edit.setText("F7")
        bindings = harness.hotkey_bindings()
        self.assertEqual(bindings["start_hotkey"], "")
        self.assertFalse(bindings["toggle"])

    def _run_capture(self, harness, result_text):
        """执行录入流程：用真 QDialog.Accepted 值，屏蔽模态弹窗与对话框构造。"""
        from PySide6.QtWidgets import QDialog as RealQDialog

        dlg = SimpleNamespace(
            result_text=result_text,
            exec=lambda: RealQDialog.Accepted,
        )
        with mock.patch(
            "fukua_rpa.ui.input_tools.KeyCaptureDialog", return_value=dlg
        ), mock.patch.object(QMessageBox, "warning") as warn:
            harness._capture_hotkey(harness.start_key_edit)
        return warn

    def test_capture_bare_key_persists_and_updates_edit(self):
        settings = _Settings()
        host = SimpleNamespace(settings=settings, hotkey_controller=None)
        harness = _Harness(settings=settings, host=host)

        warn = self._run_capture(harness, "a")

        warn.assert_not_called()
        self.assertEqual(harness.start_key_edit.text(), "A")
        self.assertEqual(settings.value(_START_HOTKEY_KEY), "A")
        self.assertEqual(settings.synced, 1)

    def test_capture_multi_plain_key_is_rejected_without_write(self):
        settings = _Settings()
        host = SimpleNamespace(settings=settings, hotkey_controller=None)
        harness = _Harness(settings=settings, host=host)

        warn = self._run_capture(harness, "a+c")

        warn.assert_called_once()
        self.assertEqual(harness.start_key_edit.text(), "F6")
        self.assertIsNone(settings.value(_START_HOTKEY_KEY))


class HotkeyControllerBareClickerTests(unittest.TestCase):
    """直接构造控制器，仅替换 host 暴露的必要接口。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _controller(clicker_bindings=None, profiles=None, mappings=None):
        from PySide6.QtWidgets import QMainWindow

        window = QMainWindow()
        controller = HotkeyController(window)
        controller.hotkey_hwnd = lambda: 12345

        host = window
        host.active_key_mappings = lambda: mappings or []
        host.mapping_mode_chk = SimpleNamespace(isChecked=lambda: False)
        host.hotkey_start_edit = QLineEdit("F9")
        host.hotkey_stop_edit = QLineEdit("F10")
        host._bulk_mapping_update = False
        controller._profile_bindings = lambda: profiles or []
        controller._auto_clicker_bindings = lambda: clicker_bindings or {}
        return controller, window

    def test_bare_clicker_hotkey_is_registered(self):
        controller, window = self._controller(
            clicker_bindings={"start_hotkey": "A", "stop_hotkey": "F7", "toggle": False}
        )
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32.RegisterHotKey",
            return_value=1,
        ) as reg, mock.patch.object(
            HotkeyController, "unregister_global_hotkeys", lambda _s: None
        ):
            used = {(6, 121), (2, 65)}  # F9 / F10 起始签名占位，避免冲突
            ok = controller._register_optional_hotkeys(12345, used)

        self.assertTrue(ok)
        self.assertIn(HOTKEY_ID_AUTO_CLICKER_START, controller.auto_clicker_ids)
        # 至少有一次注册使用 VK_A(0x41) 且 modifiers 仅为 MOD_NOREPEAT
        self.assertTrue(
            any(
                call.args[2] == MOD_NOREPEAT and call.args[3] == 0x41
                for call in reg.call_args_list
            ),
            reg.call_args_list,
        )
        window.close()

    def test_bare_clicker_toggle_registers_once(self):
        controller, window = self._controller(
            clicker_bindings={"start_hotkey": "t", "stop_hotkey": "T", "toggle": True}
        )
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32.RegisterHotKey",
            return_value=1,
        ) as reg, mock.patch.object(
            HotkeyController, "unregister_global_hotkeys", lambda _s: None
        ):
            ok = controller._register_optional_hotkeys(12345, set())

        self.assertTrue(ok)
        clicker_registrations = [
            c for c in reg.call_args_list if c.args[1] in (
                HOTKEY_ID_AUTO_CLICKER_START, HOTKEY_ID_AUTO_CLICKER_STOP
            )
        ]
        self.assertEqual(len(clicker_registrations), 1)
        self.assertEqual(controller.auto_clicker_ids.get(HOTKEY_ID_AUTO_CLICKER_START), "toggle")
        window.close()

    def test_bare_clicker_registration_failure_is_propagated(self):
        controller, window = self._controller(
            clicker_bindings={"start_hotkey": "A", "stop_hotkey": "F7", "toggle": False}
        )
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32.RegisterHotKey",
            return_value=0,
        ), mock.patch.object(
            HotkeyController, "unregister_global_hotkeys", lambda _s: None
        ):
            ok = controller._register_optional_hotkeys(12345, set())
        self.assertFalse(ok)
        window.close()

    def test_multi_plain_key_clicker_is_skipped(self):
        controller, window = self._controller(
            clicker_bindings={"start_hotkey": "A+C", "stop_hotkey": "F7", "toggle": False}
        )
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32.RegisterHotKey",
            return_value=1,
        ):
            ok = controller._register_optional_hotkeys(12345, set())
        self.assertTrue(ok)
        self.assertNotIn(HOTKEY_ID_AUTO_CLICKER_START, controller.auto_clicker_ids)
        window.close()

    def test_profile_bare_hotkey_still_rejected(self):
        # 方案热键仍须拒绝裸键，确认未误伤。
        controller, window = self._controller(
            profiles=[{
                "profile_name": "P",
                "profile_id": "p1",
                "slot_id": "s1",
                "start_hotkey": "A",
                "stop_hotkey": "F10",
            }]
        )
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32.RegisterHotKey",
            return_value=1,
        ) as reg, mock.patch.object(
            HotkeyController, "unregister_global_hotkeys", lambda _s: None
        ):
            ok = controller._register_optional_hotkeys(12345, set())
        self.assertTrue(ok)
        self.assertFalse(controller.profile_start_ids)
        self.assertFalse(
            any(
                c.args[3] == 0x41 and c.args[2] == MOD_NOREPEAT
                for c in reg.call_args_list
            ),
            "方案热键不应因连点器放开裸键而被注册",
        )
        window.close()

    def test_main_control_hotkey_bare_still_rejected(self):
        controller, window = self._controller()
        with mock.patch.object(QMessageBox, "warning") as warn:
            fallback = controller.validate_control_hotkey("A", "F9", "启动热键")
        self.assertEqual(fallback["display"], "F9")
        warn.assert_called_once()
        window.close()

    def test_key_mapping_bare_key_still_requires_mapping_mode(self):
        """按键映射的裸键禁令不受连点器放开影响：safe_global 仍为 False。

        mapping_controller.py:115 用 is_safe_global_hotkey 决定 safe_global，
        未开启映射模式时 register_global_hotkeys 会跳过裸键映射。本例固化该判据
        不被连点器改动误伤。
        """
        for bare in ("A", "5", "Space", "Enter"):
            with self.subTest(bare=bare):
                parsed = parse_hotkey_text(bare)
                self.assertTrue(parsed["bare"])
                self.assertFalse(
                    is_safe_global_hotkey(parsed),
                    "裸键在按键映射路径仍必须被判为非全局安全键",
                )

    def test_capture_conflict_detection_still_blocks(self):
        """录入已被占用的键仍被冲突检测拦下，不写入配置。"""
        from PySide6.QtWidgets import QDialog as RealQDialog

        settings = _Settings()
        occupied = mock.Mock()
        occupied.reserved_hotkey_signatures.return_value = {(0, 0x41): "脚本启动热键"}
        host = SimpleNamespace(settings=settings, hotkey_controller=occupied)
        harness = _Harness(settings=settings, host=host)

        dlg = SimpleNamespace(
            result_text="a", exec=lambda: RealQDialog.Accepted
        )
        with mock.patch(
            "fukua_rpa.ui.input_tools.KeyCaptureDialog", return_value=dlg
        ), mock.patch.object(QMessageBox, "warning") as warn:
            harness._capture_hotkey(harness.start_key_edit)

        warn.assert_called_once()
        self.assertIn("脚本启动热键", warn.call_args.args[2])
        self.assertEqual(harness.start_key_edit.text(), "F6")
        self.assertIsNone(settings.value(_START_HOTKEY_KEY))


if __name__ == "__main__":
    unittest.main()
