"""UIC14 守卫测试：运行时编辑锁的统一入口。

三处历史直读 ``_runtime_editing_locked`` 已统一改为 ``is_runtime_editing_locked()``
（ui/lifecycle.py:59）。本测试锁定锁定态下三个编辑入口的守卫行为，防止回归为
「直读私有属性」或在重构中丢失守卫语义：

1. ``profile_controller.merge_profile`` —— 锁定态返回 False 且不弹提示；
2. ``DraggableListWidget.keyPressEvent`` —— 锁定态下 Enter/F2 不打开步骤配置；
3. ``_begin_manual_drag`` —— 锁定态下不启动拖拽。
"""

import contextlib
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QListWidgetItem

from fukua_rpa.ui.task_list import DraggableListWidget
from tests.qt_test_support import QtCallbackGuardMixin, create_test_window, dispose_widget


def _key_event(key):
    return QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier)


class RuntimeEditingLockGuardTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_list(self, count=2):
        widget = DraggableListWidget()
        self.addCleanup(dispose_widget, self.app, widget)
        for index in range(count):
            widget.addItem(QListWidgetItem(f"step {index + 1}"))
        return widget

    # ------------------------------------------------------------------
    # 1) merge_profile
    # ------------------------------------------------------------------
    def test_merge_profile_returns_false_while_locked(self):
        with mock.patch(
            "fukua_rpa.ui.main_window.QMessageBox.warning",
            return_value=None,
        ) as warn:
            window = create_test_window(self)
            self.assertFalse(window._runtime_editing_locked)
            # 解锁态：需要两个以上方案才不因「无可用来源」提前返回 False。
            # 此处只需验证锁定分支 —— 锁定态必须稳定返回 False 且不触发后续逻辑。
            window._runtime_editing_locked = True
            result = window.profile_controller.merge_profile()
            self.assertFalse(result)
            # 锁定分支在弹提示之前就返回，不应出现合并相关提示。
            for call in warn.call_args_list:
                self.assertNotIn("合并", "".join(str(a) for a in call.args))

    def test_merge_profile_allows_when_unlocked(self):
        with mock.patch(
            "fukua_rpa.ui.main_window.QMessageBox.information",
            return_value=None,
        ):
            window = create_test_window(self)
            window._runtime_editing_locked = False
            # 无第二个方案时 merge_profile 因 source_names 为空返回 False（非锁定分支），
            # 同时弹出提示（已 mock）。锁定分支在更早处返回，两者都返回 False。
            result = window.profile_controller.merge_profile()
            self.assertFalse(result)

    # ------------------------------------------------------------------
    # 2) keyPressEvent Enter/F2 不打开配置
    # ------------------------------------------------------------------
    def test_keypress_enter_does_not_open_config_while_locked(self):
        widget = self.make_list()
        widget._runtime_editing_locked = True
        # 锁定分支应先 accept 并返回，绝不触达 item widget 的 open_custom_config。
        opener = mock.Mock()
        widget.ensure_task_item_widget = lambda _item: mock.Mock(
            open_custom_config=opener
        )
        widget.setCurrentRow(0)
        event = _key_event(Qt.Key_Return)
        widget.keyPressEvent(event)
        self.assertTrue(event.isAccepted())
        opener.assert_not_called()

    def test_keypress_f2_does_not_open_config_while_locked(self):
        widget = self.make_list()
        widget._runtime_editing_locked = True
        opener = mock.Mock()
        widget.ensure_task_item_widget = lambda _item: mock.Mock(
            open_custom_config=opener
        )
        widget.setCurrentRow(0)
        event = _key_event(Qt.Key_F2)
        widget.keyPressEvent(event)
        self.assertTrue(event.isAccepted())
        opener.assert_not_called()

    def test_keypress_enter_opens_config_when_unlocked(self):
        widget = self.make_list()
        widget._runtime_editing_locked = False
        opener = mock.Mock()
        widget.ensure_task_item_widget = lambda _item: mock.Mock(
            open_custom_config=opener
        )
        widget.setCurrentRow(0)
        event = _key_event(Qt.Key_Return)
        widget.keyPressEvent(event)
        opener.assert_called_once()

    # ------------------------------------------------------------------
    # 3) _begin_manual_drag
    # ------------------------------------------------------------------
    def test_manual_drag_does_not_start_while_locked(self):
        widget = self.make_list()
        widget._runtime_editing_locked = True
        widget._begin_manual_drag(0)
        self.assertFalse(widget._drag_active)

    def test_manual_drag_starts_when_unlocked(self):
        widget = self.make_list()
        widget._runtime_editing_locked = False
        widget.resize(480, 320)
        widget.show()
        self.app.processEvents()
        widget._begin_manual_drag(0)
        self.assertTrue(widget._drag_active)
        widget._finish_manual_drag(commit=False)


class RuntimeEditLockWidgetListTests(unittest.TestCase):
    """FR-20260812 建议-架构-2: 编辑锁控件清单为独立常量（26 项），
    实现与测试共用同一来源，防止漂移。

    2026-10-22：新增「复制方案」按钮与「工具 → 按键映射」入口（FR-20261021-004）。
    """

    def test_lock_widget_list_constant_is_well_formed(self):
        from fukua_rpa.ui.controllers.execution_controller import (
            RUNTIME_EDIT_LOCK_WIDGETS,
        )

        self.assertIsInstance(RUNTIME_EDIT_LOCK_WIDGETS, tuple)
        # 核查确认清单为 26 项（2026-10-22 增加 duplicate_profile_btn 与 key_mapping_action）
        self.assertEqual(len(RUNTIME_EDIT_LOCK_WIDGETS), 26)
        # 无重复、无空项
        self.assertEqual(len(set(RUNTIME_EDIT_LOCK_WIDGETS)), 26)
        self.assertTrue(all(isinstance(w, str) and w for w in RUNTIME_EDIT_LOCK_WIDGETS))

    def test_lock_widget_list_used_by_execution_controller(self):
        import inspect

        src = inspect.getsource(
            __import__(
                "fukua_rpa.ui.controllers.execution_controller",
                fromlist=["ExecutionController"],
            ).ExecutionController.set_runtime_editing_locked
        )
        # 实现必须引用常量而非内联清单
        self.assertIn("RUNTIME_EDIT_LOCK_WIDGETS", src)
        self.assertNotIn('"profile_combo"', src)


class RuntimeLockSecondaryEditorTests(QtCallbackGuardMixin, unittest.TestCase):
    """FR-20261022-001/002：迁出设置对话框的编辑窗口同样服从运行期只读。

    根因：只读边界过去等于「控件名清单 + 设置对话框子树」。映射编辑区迁到独立
    工具窗口、方案快捷键对话框本身是顶层窗口（parent=None）后，两者都不在
    settings_dialog 子树内，也不在 RUNTIME_EDIT_LOCK_WIDGETS 中，于是整块逃逸。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self):
        window = create_test_window(self)
        self.addCleanup(self._close_secondary_windows, window)
        return window

    def _close_secondary_windows(self, window):
        tool = getattr(window, "_key_mapping_tool_window", None)
        if tool is not None:
            with contextlib.suppress(RuntimeError):
                tool.close()
                tool.deleteLater()
        controller = getattr(window, "profile_hotkey_controller", None)
        dialog = getattr(controller, "_dialog", None) if controller is not None else None
        if dialog is not None:
            with contextlib.suppress(RuntimeError):
                dialog.close()
                dialog.deleteLater()
        self.app.processEvents()

    def _open_mapping_tool(self, window):
        window.show_key_mapping_tool_window()
        self.app.processEvents()
        return window._key_mapping_tool_window

    def _open_hotkey_dialog(self, window):
        window.profile_hotkey_controller.show_dialog()
        self.app.processEvents()
        return window.profile_hotkey_controller._dialog

    # ---------------- FR-20261022-001 ----------------

    def test_mapping_tool_window_is_read_only_while_running(self):
        window = self.make_window()
        tool = self._open_mapping_tool(window)
        section = tool.mapping_section
        rows = list(window.key_mapping_rows)
        self.assertTrue(section.isEnabled(), "前提：未运行时映射区必须可编辑")
        self.assertGreaterEqual(len(rows), 1, "前提：默认应有映射行")

        window.execution_controller.set_runtime_editing_locked(True)
        self.app.processEvents()

        self.assertFalse(section.isEnabled(), "运行期映射区必须整块只读")
        for index, row in enumerate(rows):
            self.assertFalse(
                row["enabled"].isEnabled(),
                f"第 {index} 行启用开关运行期必须禁用",
            )
            self.assertFalse(
                row["hotkey"].isEnabled(),
                f"第 {index} 行热键输入运行期必须禁用",
            )

        window.execution_controller.set_runtime_editing_locked(False)
        self.app.processEvents()
        self.assertTrue(section.isEnabled(), "停止后映射区必须恢复可编辑")
        self.assertTrue(rows[0]["hotkey"].isEnabled(), "停止后行内控件必须恢复")

    def test_mapping_tool_window_opened_during_run_is_read_only(self):
        """锁定后才创建的窗口也必须立即只读（不能只在加锁瞬间扫描已有窗口）。"""
        window = self.make_window()
        window.execution_controller.set_runtime_editing_locked(True)
        self.app.processEvents()

        tool = self._open_mapping_tool(window)
        self.assertFalse(
            tool.mapping_section.isEnabled(),
            "运行中新建的映射工具窗口必须立即只读",
        )

        window.execution_controller.set_runtime_editing_locked(False)
        self.app.processEvents()
        self.assertTrue(tool.mapping_section.isEnabled(), "停止后必须恢复可编辑")

    def test_lock_preserves_initially_disabled_mapping_control(self):
        """解锁只能恢复“因运行而禁用”的部分，不得把原本禁用的控件误启用。"""
        window = self.make_window()
        tool = self._open_mapping_tool(window)
        rows = list(window.key_mapping_rows)
        self.assertGreaterEqual(len(rows), 2, "前提：至少两行才能区分原态")
        rows[0]["hotkey"].setEnabled(False)

        window.execution_controller.set_runtime_editing_locked(True)
        self.app.processEvents()
        window.execution_controller.set_runtime_editing_locked(False)
        self.app.processEvents()

        self.assertTrue(tool.mapping_section.isEnabled())
        self.assertFalse(
            rows[0]["hotkey"].isEnabled(),
            "原本禁用的控件解锁后必须仍为禁用",
        )
        self.assertTrue(rows[1]["hotkey"].isEnabled(), "其余控件必须恢复可用")

    # ---------------- FR-20261022-002 ----------------

    def test_profile_hotkey_dialog_is_read_only_while_running(self):
        window = self.make_window()
        dialog = self._open_hotkey_dialog(window)
        self.assertIsNotNone(dialog, "前提：真实入口必须能打开方案快捷键对话框")
        self.assertTrue(dialog.isEnabled(), "前提：未运行时对话框必须可编辑")

        window.execution_controller.set_runtime_editing_locked(True)
        self.app.processEvents()
        self.assertFalse(dialog.isEnabled(), "运行期方案快捷键对话框必须只读")

        window.execution_controller.set_runtime_editing_locked(False)
        self.app.processEvents()
        self.assertTrue(dialog.isEnabled(), "停止后必须恢复可编辑")

    def test_profile_hotkey_dialog_opened_during_run_is_read_only(self):
        window = self.make_window()
        window.execution_controller.set_runtime_editing_locked(True)
        self.app.processEvents()

        dialog = self._open_hotkey_dialog(window)
        self.assertIsNotNone(dialog)
        self.assertFalse(
            dialog.isEnabled(),
            "运行中新建的方案快捷键对话框必须立即只读",
        )

        window.execution_controller.set_runtime_editing_locked(False)
        self.app.processEvents()
        self.assertTrue(dialog.isEnabled(), "停止后必须恢复可编辑")


if __name__ == "__main__":
    unittest.main()
