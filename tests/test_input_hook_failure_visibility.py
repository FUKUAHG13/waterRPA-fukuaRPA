# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ctypes
import shutil
import tempfile
import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

from fukua_rpa.hook_backend import win32_hook_backend
from fukua_rpa.ui import input_tools
from fukua_rpa.ui.controllers import HotkeyController
from fukua_rpa.ui.main_window import RPAWindow
from tests.qt_test_support import dispose_widget


class HookInstallFailureVisibilityTests(unittest.TestCase):
    """INP-FR-001：低级钩子安装失败必须可见，不得静默失效（约束 #90）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_mapping_hook_install_failure_reports_reason(self):
        thread = input_tools.KeyMappingHookThread({"F9"})
        failures = []
        thread.failed.connect(failures.append)
        try:
            with mock.patch.object(
                win32_hook_backend, "install_keyboard", return_value=None
            ):
                thread.run()  # 直接调用线程体，避免真实消息循环
            self.assertTrue(thread.install_error)
            self.assertEqual(len(failures), 1)
            self.assertIn("安装失败", failures[0])
            self.assertFalse(thread.is_active)
        finally:
            thread.stop()

    def test_coordinate_pick_hook_install_failure_reports_reason(self):
        thread = input_tools.CoordinatePickThread("coordinate")
        failures = []
        thread.failed.connect(failures.append)
        try:
            with mock.patch.object(
                win32_hook_backend, "install_mouse", return_value=None
            ):
                thread.run()
            self.assertTrue(thread.install_error)
            self.assertEqual(len(failures), 1)
            self.assertIn("安装失败", failures[0])
            self.assertFalse(thread.is_active)
        finally:
            thread.stop()

    def test_coordinate_picker_closes_on_hook_failure(self):
        from shiboken6 import isValid

        from fukua_rpa.ui.input_tools import CoordinatePickerUI

        picks = []
        picker = CoordinatePickerUI("coordinate", callback=picks.append)
        try:
            self.assertTrue(picker.isVisible())
            picker.pick_thread.failed.emit("低级鼠标钩子安装失败")
            self.app.processEvents()
            # WA_DeleteOnClose 会在关闭后销毁 C++ 对象；已销毁即视为不可见。
            self.assertFalse(isValid(picker) and picker.isVisible())
        finally:
            if isValid(picker):
                picker.close()
                picker.pick_thread.stop()
                picker.pick_thread.wait(1500)


class CoordinatePickRightButtonCancelTests(unittest.TestCase):
    """FR-013：右键取消必须成对吞掉 RBUTTONDOWN 与 RBUTTONUP。

    取消会置位 stop_event；若抬起留给 "stop_event → call_next" 分支，目标窗口会
    收到没有配对按下的 WM_RBUTTONUP（DefWindowProc 据此合成 WM_CONTEXTMENU）。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _msll_lparam(x, y):
        struct = input_tools.MSLLHOOKSTRUCT()
        struct.pt.x = x
        struct.pt.y = y
        return ctypes.addressof(struct)

    def _thread(self):
        thread = input_tools.CoordinatePickThread("coordinate")
        self.addCleanup(thread.stop)
        return thread

    def test_right_button_down_and_up_are_both_swallowed(self):
        thread = self._thread()
        with mock.patch.object(
            input_tools.win32_hook_backend, "call_next", return_value=0
        ) as call_next, mock.patch.object(
            input_tools, "physical_to_input_coordinates", side_effect=lambda x, y: (x, y)
        ), mock.patch.object(thread, "cancel") as cancel:
            down = thread.mouse_handler(0, 0x0204, self._msll_lparam(10, 20))
            up = thread.mouse_handler(0, 0x0205, self._msll_lparam(10, 20))

        self.assertEqual(down, 1)
        self.assertEqual(up, 1)
        cancel.assert_called_once()
        call_next.assert_not_called()

    def test_unpaired_right_button_up_is_passed_through(self):
        # 反向：没有先按下过的 WM_RBUTTONUP 不得被本线程吞掉。
        thread = self._thread()
        with mock.patch.object(
            input_tools.win32_hook_backend, "call_next", return_value=0
        ) as call_next, mock.patch.object(
            input_tools, "physical_to_input_coordinates", side_effect=lambda x, y: (x, y)
        ):
            result = thread.mouse_handler(0, 0x0205, self._msll_lparam(10, 20))

        self.assertEqual(result, 0)
        call_next.assert_called_once()


class HotkeyRegistrationFailureVisibilityTests(unittest.TestCase):
    """FR-SCOPE009-002：按键映射热键注册失败必须有界面可见通道。

    基线里 `HotkeyController.register_global_hotkeys` 只把失败写进
    `mapping_registration_failures` + `write_log(...)`，而 `write_log` 未传
    `callback`（`logging_service.py:427-431` 只有在传了 callback 且 `log_to_ui`
    打开时才进界面日志面板），全仓也没有任何 UI 读取该字典 ⇒ 热键静默失效，
    用户以为已生效。

    修复后在设置界面的映射区提供状态驱动的常驻提示位，与失败字典的真实内容
    保持一致：有失败项时显示，无失败项时保持隐藏（注册成功路径不得出现多余提示）。
    """

    MAPPING_HOTKEY = "ctrl+f1"
    MAPPING_ID = 9001

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self):
        """构造真实主窗口，但只在**构造期间**屏蔽注册路径。

        `create_test_window` 会把 `HotkeyController.register_global_hotkeys`
        一直补丁到清理阶段（`addCleanup` 才解除），那样测试里显式调用注册路径
        会打到替身而永远看不到真实结果。这里把补丁限定在构造期间：既避免构造
        过程触碰真实 Win32 热键后端，又让后续显式调用走到真实实现。
        """
        base_dir = tempfile.mkdtemp(prefix="fukua-hotkey-notice-")
        self.addCleanup(shutil.rmtree, base_dir, True)
        with mock.patch.object(
            HotkeyController, "register_global_hotkeys", lambda _self: True
        ), mock.patch.object(
            QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.Ok
        ), mock.patch.object(
            QMessageBox, "information", lambda *args, **kwargs: QMessageBox.Ok
        ):
            window = RPAWindow(base_dir=base_dir)
            # C-1 懒构建 + 2026-10-19 迁移：映射区与失败提示标签已迁入
            # 「工具 → 按键映射」窗口，替代原先的「打开设置对话框」。
            window.show_key_mapping_tool_window()
        self.addCleanup(dispose_widget, self.app, window)
        return window

    def _mappings(self):
        return [
            {
                "id": self.MAPPING_ID,
                "index": 0,
                "hotkey": self.MAPPING_HOTKEY,
                "normalized": self.MAPPING_HOTKEY,
                "modifiers": 0x0002,
                "vk": 0x70,
                "safe_global": True,
            }
        ]

    def _register(self, window, *, fail_mapping):
        """真实调用注册路径，只替换 Win32 后端。"""
        with mock.patch.object(
            window, "active_key_mappings", side_effect=self._mappings
        ), mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32"
        ) as user32, mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.write_log"
        ):
            user32.RegisterHotKey.side_effect = (
                (lambda _hwnd, hotkey_id, _mods, _vk: hotkey_id != self.MAPPING_ID)
                if fail_mapping
                else (lambda *_args: True)
            )
            user32.UnregisterHotKey.return_value = True
            return window.hotkey_controller.register_global_hotkeys()

    def _notice(self, window):
        label = getattr(window, "mapping_hotkey_failure_notice", None)
        self.assertIsInstance(
            label,
            QLabel,
            "按键映射工具窗口缺少映射热键失败提示位：注册失败只能进日志文件",
        )
        return label

    def _notice_shown(self, window):
        label = self._notice(window)
        self.app.processEvents()
        return bool(not label.isHidden() and label.text().strip())

    def test_registration_failure_is_visible_in_settings_ui(self):
        window = self._make_window()
        self._register(window, fail_mapping=True)

        label = self._notice(window)
        self.assertTrue(
            self._notice_shown(window),
            f"映射热键注册失败必须给出界面可见提示，实际文本={label.text()!r}",
        )
        self.assertIn(self.MAPPING_HOTKEY, label.text())

        # 打开按键映射工具窗口后必须真的可见，而不是仅内部状态被置位。
        # 2026-10-19：映射区已从设置对话框迁入该窗口。
        window._key_mapping_tool_window.show()
        self.app.processEvents()
        self.assertTrue(label.isVisible())
        window._key_mapping_tool_window.hide()

    def test_registration_failure_notice_is_actionable(self):
        window = self._make_window()
        self._register(window, fail_mapping=True)
        text = self._notice(window).text()
        self.assertTrue(
            "占用" in text or "冲突" in text,
            f"提示须说明失败原因（组合键被占用/冲突），实际={text!r}",
        )
        self.assertTrue(
            "更换" in text or "关闭" in text,
            f"提示须给出可操作的处置办法，实际={text!r}",
        )

    def test_successful_registration_shows_no_notice(self):
        """正面对照：注册成功路径不得出现多余提示（副作用＝提示过频）。"""
        window = self._make_window()
        registered = self._register(window, fail_mapping=False)

        self.assertTrue(registered)
        self.assertEqual(window.hotkey_controller.mapping_registration_failures, {})
        self.assertFalse(self._notice_shown(window))
        self.assertEqual(self._notice(window).text().strip(), "")

    def test_notice_clears_after_a_later_successful_registration(self):
        """失败记录自愈时提示必须同步消失，避免过期状态误导用户。"""
        window = self._make_window()
        self._register(window, fail_mapping=True)
        self.assertTrue(self._notice_shown(window))

        self._register(window, fail_mapping=False)

        self.assertEqual(window.hotkey_controller.mapping_registration_failures, {})
        self.assertFalse(self._notice_shown(window))
        self.assertEqual(self._notice(window).text().strip(), "")


class MappingNoteCoverageTests(unittest.TestCase):
    """FR-SCOPE009-003：设置界面映射说明必须覆盖全部「忽略映射」的运行态。

    实现侧 `mapping_controller.execute_key_mapping` 经 `input_operation_is_running`
    在**脚本会话或连点器**运行期间都会忽略映射（两者都持输入所有权），而基线说明
    文字只提「脚本正在运行」⇒ 用户在连点器运行期间会误以为映射失效是缺陷。

    文案按文档标准验收（`06_改进路线.md` 阶段 5E：无红绿判据）。这里只补一条轻量
    守护钉住**覆盖范围**与**既有语义**，不约束具体措辞，避免把文案写成脆性断言。
    """

    NOTE_PREFIX = "按下映射热键后"

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self):
        base_dir = tempfile.mkdtemp(prefix="fukua-mapping-note-")
        self.addCleanup(shutil.rmtree, base_dir, True)
        with mock.patch.object(
            HotkeyController, "register_global_hotkeys", lambda _self: True
        ), mock.patch.object(
            QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.Ok
        ), mock.patch.object(
            QMessageBox, "information", lambda *args, **kwargs: QMessageBox.Ok
        ):
            window = RPAWindow(base_dir=base_dir)
            # C-1 懒构建 + 2026-10-19 迁移：映射区与失败提示标签已迁入
            # 「工具 → 按键映射」窗口，替代原先的「打开设置对话框」。
            window.show_key_mapping_tool_window()
        self.addCleanup(dispose_widget, self.app, window)
        return window

    def _mapping_note_text(self, window):
        """按稳定前缀定位映射区说明文字，不依赖控件属性名或具体措辞。"""
        # 2026-10-19：映射说明随映射区迁入按键映射工具窗口。
        for label in window._key_mapping_tool_window.findChildren(QLabel):
            text = label.text() or ""
            if self.NOTE_PREFIX in text:
                return text
        self.fail("按键映射工具窗口缺少说明文字：映射的生效边界无法告知用户")

    def test_note_covers_the_auto_clicker_running_state(self):
        window = self._make_window()
        text = self._mapping_note_text(window)
        self.assertIn(
            "连点器",
            text,
            f"说明文字未覆盖「连点器运行期间同样忽略映射」，实际={text!r}",
        )
        self.assertIn("脚本", text, f"说明文字丢失脚本运行态，实际={text!r}")

    def test_note_keeps_the_existing_guidance(self):
        """必须保留行为：既有说明语义不变（组合键全局生效、裸键需开启映射模式）。"""
        window = self._make_window()
        text = self._mapping_note_text(window)
        self.assertIn("忽略映射", text)
        self.assertIn("Ctrl/Alt/Shift", text)
        self.assertIn("按键映射模式", text)


if __name__ == "__main__":
    unittest.main()
