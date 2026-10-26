import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QInputDialog,
    QPlainTextEdit,
)

from fukua_rpa import logging_service
from fukua_rpa.constants import (
    TASK_TYPE_LAUNCH_APP,
    TASK_TYPE_SECRET_TEXT,
    TASK_TYPE_UIA_CLICK,
    TASK_TYPE_WAIT_WINDOW,
)
from fukua_rpa.log_policy import LOG_MODE_COMPLETE, LOG_PARAMETERS
from fukua_rpa.ui.controllers.log_controller import LogController
from fukua_rpa.ui.controllers.settings_controller import SettingsController
from fukua_rpa.ui.controllers.workflow_editor import WorkflowEditorController
from tests.qt_test_support import QtCallbackGuardMixin, create_test_window, dispose_widget


class _UiPerformanceStub:
    def observe_since(self, *_args, **_kwargs):
        return None

    def increment(self, *_args, **_kwargs):
        return None


class _LogHost:
    def __init__(self):
        self.log_text = QPlainTextEdit()
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItem("完全", LOG_MODE_COMPLETE)
        self.ui_performance = _UiPerformanceStub()
        self.is_switching_profile = False


class _SettingsStub:
    @staticmethod
    def value(_key, default=None, **_kwargs):
        return default


class _WorkflowHost(_LogHost):
    def __init__(self):
        super().__init__()
        self.settings = _SettingsStub()
        self.log_controller = LogController(self)

    def append_application_log(
        self, message, category=None, *, critical=False, private_msg=None
    ):
        return self.log_controller.append_application(
            message,
            category,
            critical=critical,
            private_message=private_msg,
        )


class _SettingsAuditHost:
    def __init__(self):
        self.current_profile_name = "当前方案"
        self.is_switching_profile = False
        self.events = []

    def append_application_log(
        self, message, category=None, *, critical=False, private_msg=None
    ):
        self.events.append((message, category, private_msg))
        return True


class ApplicationAuditLogTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.previous_config = dict(logging_service.GLOBAL_CONFIG)

    def tearDown(self):
        logging_service.flush_logs()
        logging_service.GLOBAL_CONFIG.update(self.previous_config)
        logging_service.set_log_base_dir(None)

    def create_window(self, base_dir):
        window = create_test_window(self, base_dir)
        window.log_level_combo.setCurrentIndex(
            window.log_level_combo.findData(LOG_MODE_COMPLETE)
        )
        return window

    @staticmethod
    def invoke_external_selector(row, kind, value, *, accepted=True):
        coordinate = "120,240" if accepted else ""
        if kind == "window":
            with mock.patch(
                "fukua_rpa.window_actions.window_title_at_point",
                return_value=value,
            ) as pick:
                row.on_window_picked(coordinate)
            if accepted:
                pick.assert_called_once_with(120, 240)
            else:
                pick.assert_not_called()
            return None
        if kind == "uia":
            binding = {
                "root_title": value,
                "target_class": "Button",
                "root_hwnd": 123,
            }
            backend = mock.Mock()
            backend.create_binding.return_value = binding
            with mock.patch(
                "fukua_rpa.mapping_backend.WindowMappingBackend",
                return_value=backend,
            ) as backend_type:
                row.on_uia_control_picked(coordinate)
            if accepted:
                backend_type.assert_called_once_with()
                backend.create_binding.assert_called_once_with(120, 240)
                backend.close.assert_called_once_with()
                return binding
            backend_type.assert_not_called()
            return None
        if kind == "key":
            result = QDialog.Accepted if accepted else QDialog.Rejected

            class FakeCaptureDialog:
                captured_text = value if accepted else ""

                def __init__(self, _parent, _title):
                    pass

                def exec(self):
                    return result

            with mock.patch(
                "fukua_rpa.ui.task_row_mixins.picker.KeyCaptureDialog", FakeCaptureDialog
            ):
                row.start_key_capture()
            return None
        if kind == "directory":
            selected = value if accepted else ""
            with mock.patch.object(
                QFileDialog, "getExistingDirectory", return_value=selected
            ):
                row.select_file()
            return None
        selected = (value, "selected") if accepted else ("", "")
        with mock.patch.object(
            QFileDialog, "getOpenFileName", return_value=selected
        ), mock.patch(
            "fukua_rpa.ui.task_row.show_transparent_template_warning_once"
        ):
            row.select_file()
        return None

    @staticmethod
    def reconstruct_delete_snapshot(text, action="删除"):
        marker = f"{action}步骤完整快照第 "
        chunks = {}
        expected_total = None
        for line in str(text).splitlines():
            if marker not in line:
                continue
            remainder = line.split(marker, 1)[1]
            header, payload = remainder.split(" 段：", 1)
            chunk_index, chunk_total = (
                int(value) for value in header.split("/", 1)
            )
            expected_total = chunk_total
            chunks[chunk_index] = payload
        if not expected_total or sorted(chunks) != list(
            range(1, expected_total + 1)
        ):
            raise AssertionError("删除快照分段不完整")
        return json.loads("".join(chunks[index] for index in sorted(chunks)))

    def test_application_event_reaches_file_when_ui_log_is_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            logging_service.set_log_base_dir(temp_dir)
            logging_service.GLOBAL_CONFIG.update(
                {
                    "log_to_file": True,
                    "log_to_ui": False,
                    "privacy_protected": True,
                }
            )
            host = _LogHost()
            self.addCleanup(dispose_widget, self.app, host.log_text)
            controller = LogController(host)

            self.assertTrue(
                controller.append_application(
                    "步骤编辑摘要",
                    private_message="PRIVATE_AUDIT_SENTINEL",
                )
            )
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                text = handle.read()

            self.assertEqual(host.log_text.toPlainText(), "")
            self.assertIn("步骤编辑摘要", text)
            self.assertNotIn("PRIVATE_AUDIT_SENTINEL", text)

    def test_old_secret_text_is_never_revealed_after_action_type_changes(self):
        logging_service.GLOBAL_CONFIG.update(
            {
                "log_to_file": False,
                "log_to_ui": True,
                "privacy_protected": False,
            }
        )
        host = _WorkflowHost()
        self.addCleanup(dispose_widget, self.app, host.log_text)
        editor = WorkflowEditorController(host)

        secret = "SECRET_TEXT_MUST_NOT_APPEAR"
        editor.log_task_edit(
            1,
            {"type": TASK_TYPE_SECRET_TEXT, "value": secret},
            {"type": 1.0, "value": "100,200"},
            "指令类型",
        )

        text = host.log_text.toPlainText()
        self.assertIn("秘密文本引用已隐藏", text)
        self.assertNotIn(secret, text)

    def test_settings_session_reports_changed_keys_without_public_values(self):
        host = _SettingsAuditHost()
        controller = SettingsController(host)
        snapshots = iter(
            (
                {"hotkey_start": "F8", "key_mappings": [{"x": 10, "y": 20}]},
                {"hotkey_start": "Ctrl+F8", "key_mappings": [{"x": 30, "y": 40}]},
            )
        )
        controller._capture_settings_audit_snapshot = lambda: next(snapshots)

        controller.on_dialog_visibility_changed(True)
        controller.on_dialog_visibility_changed(False)

        self.assertEqual(len(host.events), 1)
        public, _category, private = host.events[0]
        self.assertIn("hotkey_start", public)
        self.assertIn("key_mappings", public)
        self.assertNotIn("Ctrl+F8", public)
        self.assertNotIn('"x": 30', public)
        self.assertIn("Ctrl+F8", private)
        self.assertIn('"x": 30', private)

    def test_external_selector_commit_is_atomic_private_and_cancel_safe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            logging_service.set_log_base_dir(temp_dir)
            logging_service.GLOBAL_CONFIG.update(
                {
                    "log_to_file": True,
                    "log_to_ui": True,
                    "privacy_protected": True,
                }
            )
            window.add_row(
                {"type": 1.0, "value": "10,20"},
                record_undo=False,
            )
            row = window.ensure_task_item_widget(window.task_list.item(0))
            window.undo_stack.clear()
            window.redo_stack.clear()
            window.log_text.clear()

            row.on_coordinate_picked("321,654")

            public_text = window.log_text.toPlainText()
            audit_marker = "第 1 步已保存坐标选取修改"
            self.assertEqual(len(window.undo_stack), 1)
            self.assertEqual(public_text.count(audit_marker), 1)
            self.assertIn("变更字段：步骤内容", public_text)
            self.assertNotIn("321,654", public_text)
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                protected_file_text = handle.read()
            self.assertEqual(protected_file_text.count(audit_marker), 1)
            self.assertNotIn("321,654", protected_file_text)

            logging_service.GLOBAL_CONFIG["privacy_protected"] = False
            window.undo_stack.clear()
            window.log_text.clear()
            row.on_coordinate_picked("987,456")

            private_text = window.log_text.toPlainText()
            self.assertEqual(len(window.undo_stack), 1)
            self.assertEqual(private_text.count(audit_marker), 1)
            self.assertIn("987,456", private_text)
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                private_file_text = handle.read()
            self.assertIn("987,456", private_file_text)

            window.undo_stack.clear()
            window.log_text.clear()
            row.on_coordinate_picked("987,456")
            self.assertEqual(window.undo_stack, [])
            self.assertNotIn(audit_marker, window.log_text.toPlainText())

    def _assert_selector_audit(
        self,
        kind,
        task_type,
        old_name,
        value_name,
        source,
        privacy_protected,
        temp_dir,
        *,
        path_based,
    ):
        """Arrange/act/assert one selector's private audit transaction.

        Covers the three operations from the original combined test: the initial
        commit, the same-value no-op (commit but no audit log), and the cancel
        (no commit, no audit log), under a single privacy mode.
        """
        if path_based:
            old_value = os.path.join(temp_dir, old_name)
            value = os.path.join(temp_dir, value_name)
        else:
            old_value = old_name
            value = value_name

        window = self.create_window(temp_dir)
        advanced = window.settings_mode_combo.findData("advanced")
        window.settings_mode_combo.setCurrentIndex(advanced)
        window.apply_settings_mode()
        logging_service.set_log_base_dir(temp_dir)
        logging_service.GLOBAL_CONFIG.update(
            {
                "log_to_file": True,
                "log_to_ui": True,
                "privacy_protected": privacy_protected,
            }
        )
        window.add_row(
            {"type": task_type, "value": old_value},
            record_undo=False,
            select=False,
        )
        row = window.ensure_task_item_widget(window.task_list.item(0))
        window.undo_stack.clear()
        window.redo_stack.clear()
        window.log_text.clear()
        try:
            # --- operation 1: the initial commit is one atomic audit entry ---
            with mock.patch.object(
                row, "commit_external_edit", wraps=row.commit_external_edit
            ) as commit, mock.patch.object(
                window, "append_application_log", wraps=window.append_application_log
            ) as append_log:
                binding = self.invoke_external_selector(row, kind, value)

            audit_marker = "第 1 步已保存" + source + "修改"
            sentinel = (
                os.path.basename(value)
                if kind in {"launch", "directory", "image"}
                else value
            )
            commit.assert_called_once()
            self.assertEqual(len(window.undo_stack), 1)
            self.assertEqual(append_log.call_count, 1)
            public_message = append_log.call_args.args[0]
            self.assertEqual(append_log.call_args.args[1], LOG_PARAMETERS)
            self.assertIn(audit_marker, public_message)
            self.assertIn("变更字段：", public_message)
            self.assertNotIn(value, public_message)
            self.assertNotIn(sentinel, public_message)
            private_message = append_log.call_args.kwargs["private_msg"]
            self.assertIn(sentinel, private_message)
            if kind == "uia":
                self.assertEqual(row.get_data()["uia_binding"], binding)

            ui_text = window.log_text.toPlainText()
            self.assertEqual(ui_text.count(audit_marker), 1)
            if privacy_protected:
                self.assertNotIn(sentinel, ui_text)
            else:
                self.assertIn(sentinel, ui_text)
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                file_text = handle.read()
            self.assertEqual(file_text.count(audit_marker), 1)
            if privacy_protected:
                self.assertNotIn(sentinel, file_text)
            else:
                self.assertIn(sentinel, file_text)

            # --- operation 2: committing the same value is a no-op for the log ---
            window.undo_stack.clear()
            window.log_text.clear()
            with mock.patch.object(
                row, "commit_external_edit", wraps=row.commit_external_edit
            ) as same_commit, mock.patch.object(
                window, "append_application_log", wraps=window.append_application_log
            ) as same_log:
                self.invoke_external_selector(row, kind, value)
            same_commit.assert_called_once()
            same_log.assert_not_called()
            self.assertEqual(window.undo_stack, [])

            # --- operation 3: cancelling never commits nor logs ---
            with mock.patch.object(
                row, "commit_external_edit", wraps=row.commit_external_edit
            ) as cancel_commit, mock.patch.object(
                window, "append_application_log", wraps=window.append_application_log
            ) as cancel_log:
                self.invoke_external_selector(row, kind, value, accepted=False)
            cancel_commit.assert_not_called()
            cancel_log.assert_not_called()
            self.assertEqual(window.undo_stack, [])
        finally:
            self.assertTrue(logging_service.flush_logs())
            logging_service.GLOBAL_CONFIG["log_to_file"] = False
            dispose_widget(self.app, window)

    def _run_selector_audit(
        self, kind, task_type, old_name, value_name, source, *, path_based
    ):
        for privacy_protected in (True, False):
            with (
                self.subTest(privacy_protected=privacy_protected),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                self._assert_selector_audit(
                    kind,
                    task_type,
                    old_name,
                    value_name,
                    source,
                    privacy_protected,
                    temp_dir,
                    path_based=path_based,
                )

    def test_window_selector_audit_transaction(self):
        self._run_selector_audit(
            "window",
            TASK_TYPE_WAIT_WINDOW,
            "旧窗口",
            "WINDOW_USER_TOKEN",
            "窗口选取",
            path_based=False,
        )

    def test_uia_selector_audit_transaction(self):
        self._run_selector_audit(
            "uia",
            TASK_TYPE_UIA_CLICK,
            "旧控件",
            "UIA_USER_TOKEN",
            "UIA 控件选取",
            path_based=False,
        )

    def test_key_selector_audit_transaction(self):
        self._run_selector_audit(
            "key",
            7.0,
            "F8",
            "CTRL_SHIFT_K_TOKEN",
            "按键录入",
            path_based=False,
        )

    def test_launch_selector_audit_transaction(self):
        self._run_selector_audit(
            "launch",
            TASK_TYPE_LAUNCH_APP,
            "old.exe",
            "LAUNCH_USER_TOKEN.exe",
            "程序文件选择",
            path_based=True,
        )

    def test_directory_selector_audit_transaction(self):
        self._run_selector_audit(
            "directory",
            9.0,
            "old-dir",
            "DIRECTORY_USER_TOKEN",
            "目录选择",
            path_based=True,
        )

    def test_image_selector_audit_transaction(self):
        self._run_selector_audit(
            "image",
            1.0,
            "old.png",
            "IMAGE_USER_TOKEN.png",
            "图片选择",
            path_based=True,
        )

    def test_secret_text_stays_hidden_through_public_key_capture_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            advanced = window.settings_mode_combo.findData("advanced")
            window.settings_mode_combo.setCurrentIndex(advanced)
            window.apply_settings_mode()
            logging_service.set_log_base_dir(temp_dir)
            logging_service.GLOBAL_CONFIG.update(
                {
                    "log_to_file": True,
                    "log_to_ui": True,
                    "privacy_protected": False,
                }
            )
            old_secret = "OLD_SECRET_TEXT_TOKEN"
            new_secret = "NEW_SECRET_TEXT_TOKEN"
            window.add_row(
                {"type": TASK_TYPE_SECRET_TEXT, "value": old_secret},
                record_undo=False,
            )
            row = window.ensure_task_item_widget(window.task_list.item(0))
            window.undo_stack.clear()
            window.log_text.clear()

            self.invoke_external_selector(row, "key", new_secret)

            self.assertEqual(len(window.undo_stack), 1)
            ui_text = window.log_text.toPlainText()
            self.assertIn("秘密文本引用已隐藏", ui_text)
            self.assertNotIn(old_secret, ui_text)
            self.assertNotIn(new_secret, ui_text)
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                file_text = handle.read()
            self.assertIn("秘密文本引用已隐藏", file_text)
            self.assertNotIn(old_secret, file_text)
            self.assertNotIn(new_secret, file_text)

    def test_delete_logs_original_steps_and_complete_snapshot_without_secret(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            logging_service.GLOBAL_CONFIG.update(
                {
                    "log_to_file": False,
                    "log_to_ui": True,
                    "privacy_protected": False,
                }
            )
            secret = "DELETE_SECRET_SENTINEL"
            window.restore_task_snapshot(
                [
                    {"type": 1.0, "value": "123,456"},
                    {"type": TASK_TYPE_SECRET_TEXT, "value": secret},
                ]
            )
            first = window.task_list.item(0)
            second = window.task_list.item(1)
            window.task_list.select_item_with_modifiers(first, Qt.NoModifier)
            window.task_list.select_item_with_modifiers(
                second, Qt.ControlModifier
            )
            window.log_text.clear()

            self.assertTrue(window.workflow_editor.delete_selected_rows())

            text = window.log_text.toPlainText()
            self.assertIn("已删除原第 1 至 2 步", text)
            self.assertIn("左键单击", text)
            self.assertIn("输入秘密文本", text)
            self.assertIn("123,456", text)
            self.assertIn("秘密文本引用已隐藏", text)
            self.assertNotIn(secret, text)

    def test_large_delete_snapshot_is_chunked_without_losing_tail_steps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            logging_service.set_log_base_dir(temp_dir)
            logging_service.GLOBAL_CONFIG.update(
                {
                    "log_to_file": True,
                    "log_to_ui": True,
                    "privacy_protected": False,
                }
            )
            secret = "CHUNKED_DELETE_SECRET"
            tasks = [
                {
                    "type": 1.0,
                    "value": f"DELETE_VALUE_{index:03d}",
                    "step_note": "N" * 2000 + f"_TAIL_{index:03d}",
                }
                for index in range(120)
            ]
            tasks[60] = {
                "type": TASK_TYPE_SECRET_TEXT,
                "value": secret,
                "step_note": "秘密步骤的说明仍可记录",
            }
            html_like_value = "<b>DELETE_MARKER</b><img src=x>TAIL"
            html_like_note = "<i>NOTE_MARKER</i>"
            tasks[118] = {
                "type": 1.0,
                "value": html_like_value,
                "step_note": html_like_note,
            }
            window.log_text.clear()

            window.workflow_editor._log_deleted_tasks(
                "删除", list(range(len(tasks))), tasks
            )
            self.assertTrue(logging_service.flush_logs())

            ui_text = window.log_text.toPlainText()
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                file_text = handle.read()
            for text in (ui_text, file_text):
                self.assertIn("DELETE_VALUE_000", text)
                self.assertIn("DELETE_VALUE_119", text)
                self.assertIn("_TAIL_119", text)
                self.assertIn("完整快照第 1/", text)
                self.assertNotIn("日志内容过长", text)
                self.assertNotIn("已省略", text)
                self.assertNotIn(secret, text)
                reconstructed = self.reconstruct_delete_snapshot(text)
                self.assertEqual(len(reconstructed), len(tasks))
                self.assertEqual(
                    reconstructed[118]["参数"]["value"], html_like_value
                )
                self.assertEqual(
                    reconstructed[118]["参数"]["step_note"], html_like_note
                )
                self.assertEqual(
                    reconstructed[60]["参数"]["value"],
                    "<秘密文本引用已隐藏>",
                )

    def test_large_delete_snapshot_stays_private_when_protection_is_enabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            logging_service.set_log_base_dir(temp_dir)
            logging_service.GLOBAL_CONFIG.update(
                {
                    "log_to_file": True,
                    "log_to_ui": True,
                    "privacy_protected": True,
                }
            )
            sentinel = "PROTECTED_DELETE_VALUE"
            tasks = [
                {
                    "type": 1.0,
                    "value": f"{sentinel}_{index:03d}",
                    "step_note": "P" * 2000,
                }
                for index in range(40)
            ]
            window.log_text.clear()

            window.workflow_editor._log_deleted_tasks(
                "删除", list(range(len(tasks))), tasks
            )
            self.assertTrue(logging_service.flush_logs())

            ui_text = window.log_text.toPlainText()
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                file_text = handle.read()
            for text in (ui_text, file_text):
                self.assertIn("参数快照第 1/", text)
                self.assertNotIn(sentinel, text)

    def test_group_names_only_enter_private_ui_and_file_logs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            logging_service.set_log_base_dir(temp_dir)
            logging_service.GLOBAL_CONFIG.update(
                {
                    "log_to_file": True,
                    "log_to_ui": True,
                    "privacy_protected": True,
                }
            )
            advanced = window.settings_mode_combo.findData("advanced")
            window.settings_mode_combo.setCurrentIndex(advanced)
            window.apply_settings_mode()
            for index in range(2):
                window.add_row(
                    {"type": 1.0, "value": f"{index},{index}"},
                    record_undo=False,
                    select=False,
                )
            window.task_list.select_item_with_modifiers(
                window.task_list.item(0), Qt.NoModifier
            )
            window.task_list.select_item_with_modifiers(
                window.task_list.item(1), Qt.ControlModifier
            )
            window.log_text.clear()
            created_name = "GROUP_TOKEN Z:\\private\\group.txt\n<b>HTML&VALUE</b>"
            with mock.patch.object(
                QInputDialog, "getText", return_value=(created_name, True)
            ), mock.patch.object(
                window,
                "append_application_log",
                wraps=window.append_application_log,
            ) as append_log:
                self.assertTrue(window.workflow_editor.create_group_from_selection())

            self.assertTrue(
                any(
                    call.kwargs.get("private_msg")
                    and created_name in call.kwargs["private_msg"]
                    for call in append_log.call_args_list
                )
            )
            for call in append_log.call_args_list:
                self.assertNotIn("GROUP_TOKEN", str(call.args[0]))
                self.assertNotIn("Z:\\private", str(call.args[0]))
                self.assertNotIn("HTML&VALUE", str(call.args[0]))

            protected_ui = window.log_text.toPlainText()
            self.assertNotIn("GROUP_TOKEN", protected_ui)
            self.assertNotIn("Z:\\private", protected_ui)
            self.assertNotIn("HTML&VALUE", protected_ui)
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                protected_file = handle.read()
            self.assertNotIn("GROUP_TOKEN", protected_file)
            self.assertNotIn("Z:\\private", protected_file)
            self.assertNotIn("HTML&VALUE", protected_file)

            logging_service.GLOBAL_CONFIG["privacy_protected"] = False
            window.log_text.clear()
            renamed_name = "RENAME_TOKEN Z:\\private\\renamed.txt\n<i>HTML&NEXT</i>"
            with mock.patch.object(
                QInputDialog, "getText", return_value=(renamed_name, True)
            ), mock.patch.object(
                window,
                "append_application_log",
                wraps=window.append_application_log,
            ) as append_log:
                self.assertTrue(window.workflow_editor.rename_selected_group())

            for call in append_log.call_args_list:
                self.assertNotIn("RENAME_TOKEN", str(call.args[0]))
                self.assertNotIn("Z:\\private", str(call.args[0]))
                self.assertNotIn("HTML&NEXT", str(call.args[0]))
            self.assertTrue(
                any(
                    call.kwargs.get("private_msg")
                    and renamed_name in call.kwargs["private_msg"]
                    for call in append_log.call_args_list
                )
            )
            private_ui = window.log_text.toPlainText()
            self.assertIn("RENAME_TOKEN", private_ui)
            self.assertIn("Z:\\private\\renamed.txt", private_ui)
            self.assertIn("HTML&NEXT", private_ui)
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                private_file = handle.read()
            self.assertIn("RENAME_TOKEN", private_file)
            self.assertIn("Z:\\private\\renamed.txt", private_file)
            self.assertIn("HTML&NEXT", private_file)

    def test_external_edit_refreshes_open_config_dialog(self):
        """FR-20260812-D11-003（残留路径）: 外部编辑（commit_external_edit）
        修改行数据后必须刷新打开中的设置对话框上下文，防止陈旧草稿在后续
        accepted 时覆盖此行新数据。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row(
                {"type": 1.0, "value": "10,20"},
                record_undo=False,
            )
            row = window.ensure_task_item_widget(window.task_list.item(0))
            window.undo_stack.clear()

            # 模拟打开中的设置对话框
            fake_dialog = mock.Mock()
            fake_dialog.isVisible.return_value = True
            row.config_dialog = fake_dialog
            row.config_dialog_controller = mock.Mock()

            row.commit_external_edit(
                "图片选择", lambda: row.value_input.setText("C:/img/a.png")
            )
            # 外部编辑后应刷新对话框上下文（sync_data 链路可能触发多次）
            self.assertGreaterEqual(
                row.config_dialog_controller.refresh_context.call_count, 1
            )
            self.assertEqual(len(window.undo_stack), 1)


if __name__ == "__main__":
    unittest.main()
