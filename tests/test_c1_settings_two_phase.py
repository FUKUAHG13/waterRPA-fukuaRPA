"""C-1 两段式设置迁移的专项回归（不含「先 ensure 对话框」的统一夹具）。

覆盖 B 复核指出的缺口：
- D1：首开必须显示**方案值**，capture 不得把默认值写回方案；
- D2：对话框缺席时高级模式不得降级；
- D3：预置对话框被收养时仍需完成连接/绑定/应用，且只做一次；
- V-6：构建失败时不留下半成品，重试可恢复；
- B-C1-19：无对话框时 capture 不得清空 `key_mappings`。
"""

from __future__ import annotations

import unittest
from unittest import mock

from PySide6.QtWidgets import QApplication

from tests.qt_test_support import create_test_window


class TwoPhaseSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        return create_test_window(self, ensure_dialog=False)

    def test_capture_without_dialog_preserves_key_mappings(self):
        window = self._window()
        seeded = [
            {"enabled": True, "hotkey": "F6", "coord": "10,20", "action": "左键单击"},
            {"enabled": False, "hotkey": "F7", "coord": "30,40", "action": "左键单击"},
        ]
        cfg = dict(window.get_default_config_dict())
        cfg.update({"key_mappings": [dict(item) for item in seeded], "key_mapping_count": 2})
        window.apply_ui_config(cfg)

        captured = window.get_current_ui_config()

        self.assertEqual(
            [m.get("hotkey") for m in captured.get("key_mappings") or []],
            ["F6", "F7"],
            "对话框缺席时 capture 必须沿用映射行内容，而不是写成空列表",
        )
        self.assertEqual(
            window.get_key_mappings_config(),
            [dict(item, window_binding={}) for item in seeded],
            "对话框缺席时映射序列化必须与已应用的配置一致",
        )

    def test_apply_without_dialog_keeps_host_owned_state(self):
        window = self._window()
        cfg = dict(window.get_default_config_dict())
        cfg.update(
            {
                "hotkey_start": "Ctrl+Alt+9",
                "mini": True,
                "mapping_mode_enabled": True,
            }
        )
        window.apply_ui_config(cfg)

        self.assertEqual(window.hotkey_start_edit.text(), "Ctrl+Alt+9")
        self.assertTrue(window.mini_chk.isChecked())
        self.assertTrue(window.mapping_mode_chk.isChecked())

    def test_first_open_shows_profile_values_not_defaults(self):
        window = self._window()
        profile_name = window.current_profile_name
        profile = dict(window.profiles_data[profile_name])
        profile.update({"conf": "0.5", "playback_speed": "1.5"})
        window.profiles_data[profile_name] = profile

        self.assertIsNone(getattr(window, "settings_dialog", None))
        window.show_settings_dialog()

        self.assertEqual(window.conf_edit.text(), "0.5")
        self.assertEqual(window.playback_speed.text(), "1.5")
        self.assertEqual(window.get_current_ui_config().get("conf"), "0.5")

    def test_advanced_mode_not_downgraded_before_dialog(self):
        window = self._window()
        window.settings.setValue("settings_view_mode", "advanced")
        window.settings.sync()

        self.assertEqual(window.current_settings_mode(), "advanced")
        tasks = [{"type": 5.0, "value": "1", "debug_breakpoint": True}]
        kept = window.tasks_for_current_settings_mode(tasks)
        self.assertTrue(kept and kept[0].get("debug_breakpoint"))

    def test_adopted_dialog_is_prepared_once(self):
        window = self._window()
        from fukua_rpa.ui.settings_view import build_global_settings_dialog

        build_global_settings_dialog(window)
        adopted = window.settings_dialog

        with mock.patch.object(
            window.log_controller, "bind_setting_logs_dialog", autospec=True
        ) as bind_mock:
            first = window._ensure_settings_dialog()
            second = window._ensure_settings_dialog()

        self.assertIs(first, adopted)
        self.assertIs(second, adopted)
        self.assertEqual(bind_mock.call_count, 1, "收养后只允许绑定一次")

    def test_build_failure_leaves_no_partial_dialog_and_recovers(self):
        window = self._window()
        with (
            mock.patch(
                "fukua_rpa.ui.settings_view.build_global_settings_dialog",
                side_effect=RuntimeError("boom"),
            ),
            self.assertRaises(RuntimeError),
        ):
            window._ensure_settings_dialog()
        self.assertIsNone(getattr(window, "settings_dialog", None))
        self.assertFalse(getattr(window, "_settings_dialog_ready", False))

        dialog = window._ensure_settings_dialog()
        self.assertIsNotNone(dialog)
        self.assertTrue(getattr(window, "_settings_dialog_ready", False))

    def test_prepare_failure_then_retry_emits_single_audit(self):
        """准备阶段失败（apply 抛异常）后重试，审计绑定必须仍然只有一条。

        FR-20261022-201 起首开改走 ``apply_settings_controls_config``（只填设置
        控件、不重建步骤列表），因此这里打桩点同步改为该入口；断言与重试语义不变。
        """
        window = self._window()
        original_apply = window.apply_settings_controls_config
        state = {"failed": False}

        def flaky_apply(config):
            if not state["failed"]:
                state["failed"] = True
                raise RuntimeError("boom")
            return original_apply(config)

        window.apply_settings_controls_config = flaky_apply
        with self.assertRaises(RuntimeError):
            window._ensure_settings_dialog()
        # apply 抛异常发生在构建之后：对话框对象可能存在，但**准备未完成**。
        self.assertFalse(getattr(window, "_settings_dialog_ready", False))

        window.apply_settings_controls_config = original_apply
        window._ensure_settings_dialog()

        with mock.patch.object(
            window, "log_setting_change", wraps=window.log_setting_change
        ) as audit:
            window.conf_edit.editingFinished.emit()
        self.assertEqual(
            audit.call_count,
            1,
            "准备失败后重试不得重复绑定审计日志",
        )


class FirstOpenKeepsStepListTests(unittest.TestCase):
    """FR-20261022-201：首开设置只填设置控件，不得重建步骤列表或销毁步骤对话框。

    首开路径曾复用 ``apply_ui_config``（“载入方案”语义），因此会把 ``task_list``
    重置为已保存版本；这四条用例分别锁定新增、编辑、已打开的步骤对话框与
    只读保护状态四种现场。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self):
        return create_test_window(self, ensure_dialog=False)

    @staticmethod
    def _row_data(window, index=0):
        item = window.task_list.item(index)
        row = window.task_list.itemWidget(item)
        return row.get_data() if row is not None else item.data(0x0100)

    def test_first_open_keeps_unsaved_added_step(self):
        window = self._window()
        self.assertIsNone(getattr(window, "settings_dialog", None))

        window.add_row({"type": 5.0, "value": "1.5"})
        self.app.processEvents()
        self.assertEqual(window.task_list.count(), 1)

        window.show_settings_dialog()
        self.app.processEvents()

        self.assertEqual(
            window.task_list.count(),
            1,
            "首开设置不得丢弃尚未自动保存的新增步骤",
        )
        self.assertEqual(self._row_data(window).get("value"), "1.5")

    def test_first_open_keeps_unsaved_edited_value(self):
        window = self._window()
        name = window.current_profile_name
        window.add_row({"type": 5.0, "value": "1.5"})
        self.app.processEvents()
        window.persist_profiles_state(force=True)
        self.app.processEvents()
        self.assertEqual(
            [task.get("value") for task in window.profiles_data[name]["tasks"]],
            ["1.5"],
        )

        item = window.task_list.item(0)
        row = window.task_list.itemWidget(item)
        edited = row.get_data()
        edited["value"] = "9.9"
        row.set_data(edited)
        self.app.processEvents()

        window.show_settings_dialog()
        self.app.processEvents()

        self.assertEqual(
            self._row_data(window).get("value"),
            "9.9",
            "首开设置不得把界面上的未提交编辑回退为已保存值",
        )

    def test_first_open_keeps_open_task_config_dialog(self):
        window = self._window()
        from fukua_rpa.ui.task_settings_dialog import TaskConfigDialog

        window.add_row({"type": 5.0, "value": "1"})
        self.app.processEvents()
        dialog = TaskConfigDialog(
            None, {"type": 5.0, "value": "1"}, base_dir=window.base_dir
        )
        dialog.show()
        window.task_config_dialogs.add(dialog)
        self.app.processEvents()

        window.show_settings_dialog()
        self.app.processEvents()

        try:
            still_visible = dialog.isVisible()
        except RuntimeError as error:  # pragma: no cover - 失败路径
            self.fail(f"首开设置销毁了已打开的步骤设置对话框：{error}")
        self.assertTrue(still_visible, "首开设置不得关闭用户的步骤设置对话框")

    def test_first_open_keeps_rows_when_persistence_blocked(self):
        window = self._window()
        window._profiles_persistence_blocked = True
        window.add_row({"type": 5.0, "value": "2"})
        self.app.processEvents()
        self.assertEqual(window.task_list.count(), 1)

        window.show_settings_dialog()
        self.app.processEvents()

        self.assertEqual(
            window.task_list.count(),
            1,
            "只读保护状态下首开设置同样不得丢弃界面上的步骤",
        )


if __name__ == "__main__":
    unittest.main()
