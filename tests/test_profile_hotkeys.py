"""多方案快捷启动的持久化、稳定身份和运行互斥测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from fukua_rpa.profile_hotkeys import (
    MAX_PROFILE_HOTKEY_SLOTS,
    normalize_profile_hotkey_slots,
    serialize_profile_hotkey_slots,
)
from fukua_rpa.ui.controllers.profile_hotkey_controller import (
    ProfileHotkeyController,
)
from fukua_rpa.ui.profile_hotkey_dialog import ProfileHotkeyDialog


class ProfileHotkeyModelTests(unittest.TestCase):
    def test_untrusted_slots_are_bounded_and_canonicalized(self):
        raw = [
            {
                "slot_id": "same",
                "enabled": "true",
                "profile_id": f"profile-{index}",
                "start_hotkey": "ctrl+alt+1",
                "stop_hotkey": "F7",
            }
            for index in range(MAX_PROFILE_HOTKEY_SLOTS + 5)
        ]
        slots = normalize_profile_hotkey_slots(json.dumps(raw))

        self.assertEqual(len(slots), MAX_PROFILE_HOTKEY_SLOTS)
        self.assertEqual(len({slot["slot_id"] for slot in slots}), len(slots))
        self.assertEqual(slots[0]["start_hotkey"], "Ctrl+Alt+1")
        self.assertTrue(slots[0]["enabled"])
        self.assertEqual(
            normalize_profile_hotkey_slots(serialize_profile_hotkey_slots(slots)),
            slots,
        )

    def test_invalid_or_oversized_settings_are_rejected(self):
        self.assertEqual(normalize_profile_hotkey_slots("not-json"), [])
        self.assertEqual(
            normalize_profile_hotkey_slots("[" + " " * (64 * 1024) + "]"),
            [],
        )

    def test_hostile_json_shapes_rejected(self):
        """FR-20260809-0503: 深层嵌套/重复键/NaN/Infinity 必须被拒绝（loads_
        bounded_json，ARCHITECTURE #61）——原生 json.loads 对深层嵌套 64KB 内
        即抛 RecursionError 且静默接受重复键/NaN。"""
        deep = "[" * 6000 + "]" * 6000
        self.assertEqual(normalize_profile_hotkey_slots(deep), [])
        self.assertEqual(
            normalize_profile_hotkey_slots('{"slot_id": "a", "slot_id": "b"}'), []
        )
        self.assertEqual(normalize_profile_hotkey_slots('[{"x": NaN}]'), [])
        self.assertEqual(normalize_profile_hotkey_slots('[{"x": Infinity}]'), [])


class _ProfileModelStub:
    def __init__(self):
        self.name = "原方案"
        self.config = {
            "profile_id": "profile-a",
            "tasks": [{"type": 1.0, "value": "1,2"}],
        }

    def find_by_id(self, profile_id):
        return self.name if profile_id == "profile-a" else None

    @property
    def profiles(self):
        return {self.name: self.config}

    def get_config_by_id(self, profile_id):
        return dict(self.config) if profile_id == "profile-a" else None


class _ProfileHotkeyHost:
    def __init__(self, settings):
        self.settings = settings
        self.profile_model = _ProfileModelStub()
        self.hotkey_controller = mock.Mock()
        self.run_controller = mock.Mock(is_active=False)
        self.runtime_ready = True
        self.execution_controller = mock.Mock()
        self.execution_controller.prepare_and_launch_profile.return_value = True
        self.workflow_review_controller = mock.Mock()
        self.workflow_review_controller.validate_tasks.return_value = ""
        self.workflow_review_controller.confirm_loop_risks.return_value = True
        self.logs = []
        self.stop_calls = 0

    @staticmethod
    def auto_clicker_is_running():
        return False

    def append_application_log(self, message):
        self.logs.append(message)

    def stop_task(self):
        self.stop_calls += 1

    @staticmethod
    def show_start_tooltip(_message):
        return None

    @staticmethod
    def show_critical_message(_title, _message):
        return None


class ProfileHotkeyControllerTests(unittest.TestCase):
    def test_binding_survives_profile_rename_and_launches_detached_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                os.path.join(temp_dir, "config.ini"), QSettings.IniFormat
            )
            host = _ProfileHotkeyHost(settings)
            controller = ProfileHotkeyController(host)
            host.profile_hotkey_controller = controller
            controller.set_enabled(True)
            controller.save_slots(
                [
                    {
                        "slot_id": "slot-a",
                        "enabled": True,
                        "profile_id": "profile-a",
                        "start_hotkey": "Ctrl+Alt+1",
                        "stop_hotkey": "Ctrl+Alt+2",
                    }
                ]
            )

            host.profile_model.name = "重命名后方案"
            bindings = controller.active_bindings()
            self.assertEqual(bindings[0]["profile_name"], "重命名后方案")
            self.assertTrue(controller.start_profile("profile-a", "slot-a"))
            host.execution_controller.prepare_and_launch_profile.assert_called_once()
            args = host.execution_controller.prepare_and_launch_profile.call_args.args
            self.assertEqual(args[:3], ("profile-a", "profile_hotkey", "slot-a"))
            self.assertEqual(controller.active_profile_id, "profile-a")

            host.run_controller.is_active = True
            self.assertTrue(controller.stop_profile("profile-a"))
            self.assertEqual(host.stop_calls, 1)

    def test_running_clicker_blocks_profile_hotkey_launch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                os.path.join(temp_dir, "config.ini"), QSettings.IniFormat
            )
            host = _ProfileHotkeyHost(settings)
            host.auto_clicker_is_running = lambda: True
            controller = ProfileHotkeyController(host)
            host.profile_hotkey_controller = controller
            controller.set_enabled(True)

            self.assertFalse(controller.start_profile("profile-a", "slot-a"))
            host.execution_controller.prepare_and_launch_profile.assert_not_called()

    def test_invalid_manual_settings_are_not_reported_as_usable_bindings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                os.path.join(temp_dir, "config.ini"), QSettings.IniFormat
            )
            host = _ProfileHotkeyHost(settings)
            controller = ProfileHotkeyController(host)
            host.profile_hotkey_controller = controller
            controller.set_enabled(True)
            controller.save_slots(
                [
                    {
                        "slot_id": "slot-a",
                        "enabled": True,
                        "profile_id": "profile-a",
                        "start_hotkey": "A",
                        "stop_hotkey": "",
                    }
                ]
            )

            self.assertEqual(controller.active_bindings(), [])


class ProfileHotkeyDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dialog_is_an_independent_resizable_work_window(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                os.path.join(temp_dir, "config.ini"), QSettings.IniFormat
            )
            host = _ProfileHotkeyHost(settings)
            dialog = ProfileHotkeyDialog(host, [])
            self.addCleanup(dialog.deleteLater)

            self.assertIsNone(dialog.parentWidget())
            self.assertTrue(
                bool(dialog.windowFlags() & Qt.WindowMinimizeButtonHint)
            )
            self.assertTrue(
                bool(dialog.windowFlags() & Qt.WindowMaximizeButtonHint)
            )

    def test_slot_actions_fit_text_and_enabled_checkbox_is_centered(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(
                os.path.join(temp_dir, "config.ini"), QSettings.IniFormat
            )
            host = _ProfileHotkeyHost(settings)
            dialog = ProfileHotkeyDialog(
                host,
                [
                    {
                        "enabled": True,
                        "profile_id": "profile-a",
                        "start_hotkey": "F6",
                        "stop_hotkey": "F7",
                    }
                ],
            )
            self.addCleanup(dialog.deleteLater)
            dialog.show()
            self.app.processEvents()

            wrapper = dialog.table.cellWidget(0, 0)
            checkbox = dialog._enabled_checks[0]
            self.assertLessEqual(
                abs(checkbox.geometry().center().x() - wrapper.rect().center().x()),
                1,
            )
            for column in (3, 5, 6):
                button = dialog.table.cellWidget(0, column)
                self.assertGreaterEqual(
                    dialog.table.columnWidth(column),
                    button.sizeHint().width() + 16,
                )


if __name__ == "__main__":
    unittest.main()
