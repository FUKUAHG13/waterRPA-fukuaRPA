import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fukua_rpa.commands import COMMAND_SPECS
from fukua_rpa.credentials import (
    CREDENTIAL_FORMAT,
    CREDENTIAL_VERSION,
    CredentialStore,
    CredentialStoreError,
)
from fukua_rpa.engine import RPAEngine
from fukua_rpa.engine.actions_exec import StepActionContext, registered_action_codes
from fukua_rpa.logging_service import GLOBAL_CONFIG, flush_logs
from fukua_rpa.text_input import _utf16_units


def context(command, value="", task=None):
    return StepActionContext(
        command=float(command),
        value=value,
        retry=1,
        step_info={"step": 1, "loop": 1},
        cache_key="test",
        confidence=0.8,
        use_gray=True,
        task=task or {},
    )


class CredentialStoreTests(unittest.TestCase):
    def test_store_persists_only_protected_text(self):
        def protect(value):
            return "encrypted:" + value[::-1]

        def unprotect(value):
            return value.removeprefix("encrypted:")[::-1]

        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(directory, protect=protect, unprotect=unprotect)
            store.set("account", "plain-secret")

            with open(store.path, encoding="utf-8") as handle:
                raw = handle.read()
            self.assertNotIn("plain-secret", raw)
            self.assertEqual(store.get("account"), "plain-secret")
            self.assertEqual(store.names(), ["account"])

            payload = json.loads(raw)
            self.assertEqual(payload["credentials"]["account"], "encrypted:terces-nialp")

    def test_malformed_root_and_future_version_are_rejected_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(
                directory,
                protect=lambda value: value,
                unprotect=lambda value: value,
            )
            with open(store.path, "w", encoding="utf-8") as handle:
                json.dump([], handle)
            with self.assertRaisesRegex(CredentialStoreError, "根结构"):
                store.names()

            with open(store.path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "format": CREDENTIAL_FORMAT,
                        "version": CREDENTIAL_VERSION + 1,
                        "credentials": {},
                    },
                    handle,
                )
            with self.assertRaisesRegex(CredentialStoreError, "高于本程序支持"):
                store.names()

    def test_duplicate_or_non_text_credential_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(
                directory,
                protect=lambda value: value,
                unprotect=lambda value: value,
            )
            with open(store.path, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"format":"fukuaRPA_dpapi_credentials","version":1,'
                    '"credentials":{"same":"one","same":"two"}}'
                )
            with self.assertRaisesRegex(CredentialStoreError, "重复字段"):
                store.names()

            with open(store.path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "format": CREDENTIAL_FORMAT,
                        "version": CREDENTIAL_VERSION,
                        "credentials": {"account": 123},
                    },
                    handle,
                )
            with self.assertRaisesRegex(CredentialStoreError, "必须是文本"):
                store.names()

    def test_normalized_duplicate_names_are_rejected_not_silently_dropped(self):
        """FR-20260811-D2-002: strip 归一化后同名的两条凭据必须显式报错，
        禁止 dict 推导后者静默覆盖前者（凭据丢失）。"""
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(
                directory,
                protect=lambda value: value,
                unprotect=lambda value: value,
            )
            with open(store.path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "format": CREDENTIAL_FORMAT,
                        "version": CREDENTIAL_VERSION,
                        "credentials": {"key": "one", " key ": "two"},
                    },
                    handle,
                )
            with self.assertRaisesRegex(CredentialStoreError, "归一化后同名"):
                store.names()

    def test_set_overwrites_same_normalized_name_is_explicit_update(self):
        """同名更新（set 覆盖）是用户主动行为，必须保留。"""
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(
                directory,
                protect=lambda value: value,
                unprotect=lambda value: value,
            )
            store.set("key", "first")
            store.set("key", "second")
            self.assertEqual(store.get("key"), "second")

    def test_oversized_credential_file_is_rejected_before_json_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialStore(
                directory,
                protect=lambda value: value,
                unprotect=lambda value: value,
            )
            with open(store.path, "w", encoding="utf-8") as handle:
                handle.write('{"credentials":"' + "x" * 64 + '"}')

            with mock.patch(
                "fukua_rpa.credentials.MAX_CREDENTIAL_FILE_BYTES", 32
            ), self.assertRaisesRegex(CredentialStoreError, "大小超过上限"):
                store.names()

    def test_unicode_units_keep_supplementary_characters_as_surrogates(self):
        self.assertEqual(_utf16_units("A中"), [65, 0x4E2D])
        self.assertEqual(len(_utf16_units("😀")), 2)


class NewActionTests(unittest.TestCase):
    def setUp(self):
        self.previous_log_config = dict(GLOBAL_CONFIG)
        GLOBAL_CONFIG.update(
            {
                "log_to_file": False,
                "log_to_ui": True,
                "privacy_protected": True,
            }
        )
        self.engine = RPAEngine(defer_backends=True)
        self.engine.callback_msg = mock.Mock()
        self.engine.log_level = 2

    def tearDown(self):
        flush_logs()
        GLOBAL_CONFIG.update(self.previous_log_config)

    def test_every_registered_command_has_an_action_handler(self):
        self.assertEqual(registered_action_codes(), {spec.code for spec in COMMAND_SPECS})

    @mock.patch("fukua_rpa.actions.window.launch_application", return_value=1234)
    def test_launch_application_action(self, launch):
        result = self.engine._execute_launch_application(context(18, "demo.exe"))
        self.assertEqual(result, "success")
        launch.assert_called_once_with("demo.exe")

    def test_uia_read_stores_a_runtime_variable(self):
        backend = SimpleNamespace(
            uia_control_action=mock.Mock(
                return_value={"success": True, "method": "UIA Value", "value": "result"}
            )
        )
        self.engine._task_window_backend = backend
        self.engine.reset_expression_runtime()

        result = self.engine._execute_uia_read_value(
            context(25, "answer", {"uia_binding": {"root_hwnd": 1}})
        )

        self.assertEqual(result, "success")
        self.assertEqual(self.engine.runtime_variables["answer"], "result")

    def test_uia_unknown_side_effect_stops_instead_of_retrying(self):
        backend = SimpleNamespace(
            uia_control_action=mock.Mock(
                return_value={
                    "success": False,
                    "timed_out": True,
                    "outcome_unknown": True,
                    "error": "动作结果未知",
                }
            )
        )
        self.engine._task_window_backend = backend

        with mock.patch.object(self.engine, "stop") as stop:
            click_result = self.engine._execute_uia_click(
                context(23, "", {"uia_binding": {"root_hwnd": 1}})
            )
        self.assertEqual(click_result, "stopped")
        stop.assert_called_once_with()

        with mock.patch.object(self.engine, "stop") as stop:
            set_result = self.engine._execute_uia_set_value(
                context(24, "new value", {"uia_binding": {"root_hwnd": 1}})
            )
        self.assertEqual(set_result, "stopped")
        stop.assert_called_once_with()

    @mock.patch("fukua_rpa.actions.text.send_unicode_text")
    def test_regular_text_log_never_contains_plain_text(self, send):
        send.return_value = True
        self.engine._execute_text(context(4, "private words"))
        self.assertTrue(flush_logs())
        send.assert_called_once()
        self.assertEqual(send.call_args.args, ("private words",))
        self.assertTrue(callable(send.call_args.kwargs["stop_requested"]))
        logged = " ".join(str(call.args[0]) for call in self.engine.callback_msg.call_args_list)
        self.assertNotIn("private words", logged)

    @mock.patch("fukua_rpa.actions.text.send_unicode_text")
    def test_regular_text_can_enter_log_after_explicit_privacy_opt_out(self, send):
        send.return_value = True
        GLOBAL_CONFIG["privacy_protected"] = False

        self.engine._execute_text(context(4, "ordinary debug words"))
        self.assertTrue(flush_logs())

        logged = " ".join(
            str(call.args[0]) for call in self.engine.callback_msg.call_args_list
        )
        self.assertIn("ordinary debug words", logged)

    @mock.patch("fukua_rpa.actions.text.send_unicode_text")
    def test_secret_text_resolves_name_without_logging_secret(self, send):
        send.return_value = True
        GLOBAL_CONFIG["privacy_protected"] = False
        self.engine._credential_store = SimpleNamespace(get=lambda name: "top-secret")
        self.engine._execute_secret_text(context(22, "account"))
        self.assertTrue(flush_logs())
        send.assert_called_once()
        self.assertEqual(send.call_args.args, ("top-secret",))
        self.assertTrue(callable(send.call_args.kwargs["stop_requested"]))
        logged = " ".join(str(call.args[0]) for call in self.engine.callback_msg.call_args_list)
        self.assertNotIn("top-secret", logged)

    @mock.patch("fukua_rpa.actions.text.send_unicode_text")
    def test_secret_input_exception_never_logs_secret_even_when_unprotected(self, send):
        GLOBAL_CONFIG["privacy_protected"] = False
        self.engine._credential_store = SimpleNamespace(get=lambda _name: "top-secret")
        send.side_effect = RuntimeError("transport failed near top-secret")

        result = self.engine._execute_secret_text(context(22, "account"))
        self.assertTrue(flush_logs())

        self.assertEqual(result, "error")
        logged = " ".join(
            str(call.args[0]) for call in self.engine.callback_msg.call_args_list
        )
        self.assertIn("RuntimeError", logged)
        self.assertNotIn("top-secret", logged)

    def test_background_click_outcome_unknown_stops_instead_of_retrying(self):
        """P1 regression: outcome_unknown from background UIA click must stop the run."""
        backend = SimpleNamespace(
            binding_for_coordinate=mock.Mock(return_value=(10, 20)),
            background_click=mock.Mock(return_value=False),
            last_background_detail={"outcome_unknown": True, "success": False},
            last_background_method="UI Automation 超时（未重复点击）",
        )
        self.engine._task_window_backend = backend

        with mock.patch.object(self.engine, "stop") as stop:
            result = self.engine.perform_background_window_click(
                100, 200, 1, "left", {"root_hwnd": 1}
            )

        self.assertFalse(result)
        stop.assert_called_once_with()

    def test_background_click_normal_failure_does_not_stop(self):
        """Normal background click failure (no outcome_unknown) must NOT stop the run."""
        backend = SimpleNamespace(
            binding_for_coordinate=mock.Mock(return_value=(10, 20)),
            background_click=mock.Mock(return_value=False),
            last_background_detail={"success": False},
            last_background_method="PostMessage",
        )
        self.engine._task_window_backend = backend

        with mock.patch.object(self.engine, "stop") as stop:
            result = self.engine.perform_background_window_click(
                100, 200, 1, "left", {"root_hwnd": 1}
            )

        self.assertFalse(result)
        stop.assert_not_called()

    def test_plugin_action_success_returns_success_status(self):
        """P2 regression: plugin action must return 'success' so last_step_success is True."""
        from fukua_rpa.plugin_actions import execute_plugin_action

        mock_response = SimpleNamespace(ok=True, result={}, error=None)
        mock_client = SimpleNamespace(call=mock.Mock(return_value=mock_response))

        ctx = context(27, "", {"plugin_id": "test", "plugin_action_id": "act"})
        with mock.patch(
            "fukua_rpa.plugin_actions.get_plugin_host_client",
            return_value=mock_client,
        ):
            status = execute_plugin_action(self.engine, ctx)

        self.assertEqual(status, "success")
        # Verify engine would set last_step_success = True
        self.assertIn(status, ("success", "condition_true"))


if __name__ == "__main__":
    unittest.main()
