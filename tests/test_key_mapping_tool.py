# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.
"""按键映射迁移到「工具」入口的验收用例（2026-10-19）。

覆盖维护者裁定与计划 §7 的验收清单：
- 设置页不再有映射编辑控件，但保留「打开按键映射工具」链接；
- 支持链接（平台/作者B站/GitHub）仍留在设置页；
- 工具窗口收揽映射区与「按键映射模式」开关（映射全局共享，不再显示方案名）；
- 关闭只隐藏、重开不重复注册/连接、切换方案刷新方案名；
- 运行中触发映射仍被忽略并写日志（输入互斥不改变）。
"""

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from fukua_rpa.ui.controllers import HotkeyController  # noqa: E402
from fukua_rpa.ui.main_window import RPAWindow  # noqa: E402


def _labels(window):
    return [item.text() for item in window.findChildren(QLabel)]


class KeyMappingToolMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with mock.patch.object(
            HotkeyController, "register_global_hotkeys", lambda self: True
        ):
            self.window = RPAWindow(base_dir=tempfile.mkdtemp(), defer_runtime=True)
        self.addCleanup(self._dispose)

    def _dispose(self):
        window = getattr(self, "window", None)
        tool = getattr(window, "_key_mapping_tool_window", None) if window else None
        if tool is not None:
            tool.close()
        if window is not None:
            window.close()
            window.deleteLater()
        self.app.processEvents()

    def _open_settings(self):
        self.window.show_settings_dialog()
        self.app.processEvents()
        return self.window.settings_dialog

    def _open_tool(self):
        self.window.show_key_mapping_tool_window()
        self.app.processEvents()
        return self.window._key_mapping_tool_window

    # ---------------- 设置页 ----------------

    def test_settings_page_has_no_mapping_entry_and_keeps_support_links(self):
        """设置页不得保留任何按键映射入口（维护者 2026-10-20 裁定 A）。"""
        dialog = self._open_settings()
        texts = [item.text() for item in dialog.findChildren(QLabel)]
        buttons = [item.text() for item in dialog.findChildren(QPushButton)]
        self.assertFalse(
            any("按键映射" in t for t in buttons),
            "设置页不应再出现按键映射入口，映射只能从「工具 → 按键映射」进入",
        )
        self.assertTrue(any("支持 Windows 10 / 11 x64" in t for t in texts))
        self.assertTrue(any("作者B站主页" in t for t in buttons))
        self.assertTrue(any("GitHub下载页" in t for t in buttons))

    def test_settings_page_has_no_mapping_editing_controls(self):
        dialog = self._open_settings()
        # 映射编辑控件（裸键开关、点击方式组合、添加映射按钮）必须只在工具窗口出现。
        for text in ("+ 添加映射", "按键映射模式"):
            self.assertFalse(
                any(text in item.text() for item in dialog.findChildren(QPushButton))
                or any(text in item.text() for item in dialog.findChildren(QLabel)),
                f"设置页不应保留映射编辑控件：{text}",
            )
        # 映射行容器不应挂在设置对话框内。
        self.assertIsNot(self.window.mapping_rows_layout.parent(), dialog)

    # ---------------- 工具窗口 ----------------

    def test_tool_window_collects_mapping_section_and_profile_name(self):
        tool = self._open_tool()
        # 折叠区与开关必须落在工具窗口层级内（经 ResponsiveRow 等中间容器，
        # 故以 window() 判定而不是直接 parent()）。
        self.assertIs(tool.mapping_section.window(), tool)
        self.assertIs(self.window.mapping_mode_chk.window(), tool)
        self.assertIs(self.window.mapping_click_mode_combo.window(), tool)
        self.assertIsNot(self.window.mapping_rows_layout.parentWidget().window(), self._open_settings().window())
        # 2026-10-21：映射改为全局共享，窗口不再显示「当前方案」。
        self.assertFalse(any("当前方案" in t for t in _labels(tool)))

    def test_tool_window_created_once_and_reopen_is_silent(self):
        first = self._open_tool()
        first.close()  # 只隐藏
        self.assertFalse(first.isVisible())
        self.assertIs(self.window._key_mapping_tool_window, first)
        second = self._open_tool()
        self.assertIs(second, first)
        # 重开不得重复连接信号：切换点击方式只会触发一次热键后端刷新。
        combo = self.window.mapping_click_mode_combo
        with mock.patch.object(self.window, "refresh_mapping_binding_controls") as refresh:
            combo.setCurrentText("点击后返回原位")
            self.app.processEvents()
        self.assertEqual(refresh.call_count, 1)
        self.assertIsNotNone(second.mapping_section)

    def test_profile_switch_does_not_change_window_label(self):
        """映射不再随方案变化，切换后窗口不再展示方案名（也不再闪回旧映射）。"""
        tool = self._open_tool()
        self.window.current_profile_name = "迁移验证方案"
        self.window.refresh_key_mapping_tool_profile()
        self.assertFalse(any("当前方案" in t for t in _labels(tool)))

    def test_close_only_hides_and_keeps_mapping_enabled_state(self):
        tool = self._open_tool()
        tool.close()
        self.assertFalse(tool.isVisible())
        # 关闭窗口不得改变裸键开关与热键后端状态。
        self.assertFalse(self.window.mapping_mode_chk.isChecked())

    def test_mapping_controls_work_inside_tool_window(self):
        """映射编辑能力随窗口工作：添加映射、启停行、点选目标行均不因迁移失效。"""
        tool = self._open_tool()
        before = len(self.window.key_mapping_rows)
        self.window.add_key_mapping_row(refresh=False)
        self.app.processEvents()
        self.assertEqual(len(self.window.key_mapping_rows), before + 1)
        new_row = self.window.key_mapping_rows[-1]
        # 新行控件必须落在工具窗口层级内。
        self.assertIs(new_row["hotkey"].window(), tool)
        # 行的启用勾选仍可切换（每行启用规则不变）。
        new_row["enabled"].setChecked(False)
        self.assertFalse(new_row["enabled"].isChecked())
        # 热键冲突/注册语义由既有 hotkey_controller 用例覆盖，不在此重复依赖真实窗口句柄。

    def test_runtime_lock_disables_mapping_entry_and_profile_duplicate(self):
        """FR-20261021-004：运行期只读必须覆盖新增入口（复制方案 / 映射编辑）。"""
        window = self.window
        window.add_key_mapping_row(refresh=False)
        self.app.processEvents()
        row = window.key_mapping_rows[-1]
        controller = window.execution_controller
        controller.set_runtime_editing_locked(True)
        try:
            self.assertFalse(window.duplicate_profile_btn.isEnabled())
            self.assertFalse(window.key_mapping_action.isEnabled())
            self.assertFalse(row["enabled"].isEnabled())
            # 运行中即使绕过菜单直接调用，也不得复制方案
            self.assertFalse(window.profile_controller.duplicate_current_profile())
        finally:
            controller.set_runtime_editing_locked(False)
        self.assertTrue(window.duplicate_profile_btn.isEnabled())
        self.assertTrue(window.key_mapping_action.isEnabled())
        self.assertTrue(row["enabled"].isEnabled())


if __name__ == "__main__":
    unittest.main()
