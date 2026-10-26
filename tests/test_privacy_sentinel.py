"""隐私哨兵回归测试：秘密值不得进入日志或崩溃报告。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import threading
import unittest
from unittest import mock

from fukua_rpa import crash_guard, logging_service


class SecretValueInPublicLogTests(unittest.TestCase):
    """公开消息误带常见秘密形态时，第二道擦除层仍须生效。"""

    def setUp(self):
        self.previous_config = dict(logging_service.get_log_config())

    def tearDown(self):
        logging_service.flush_logs()
        logging_service.update_log_config(**self.previous_config)
        logging_service.set_log_base_dir(None)

    def test_secret_value_in_message_is_redacted(self):
        secret = "secret123"
        received = []
        completed = threading.Event()

        with tempfile.TemporaryDirectory() as temp_dir:
            logging_service.set_log_base_dir(temp_dir)
            logging_service.update_log_config(
                log_to_file=True, log_to_ui=True, privacy_protected=True
            )

            def callback(message):
                received.append(message)
                completed.set()

            logging_service.write_log(
                f"用户密码是 {secret}",
                callback,
            )
            self.assertTrue(completed.wait(2.0))
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                file_text = handle.read()

        joined = "\n".join(received)
        self.assertNotIn(secret, joined)
        self.assertNotIn(secret, file_text)
        self.assertIn("<隐藏秘密值>", joined)


class PrivateMessageProtectedTests(unittest.TestCase):
    """Test 2: private_message must not appear when privacy protection is on."""

    def setUp(self):
        self.previous_config = dict(logging_service.get_log_config())

    def tearDown(self):
        logging_service.flush_logs()
        logging_service.update_log_config(**self.previous_config)
        logging_service.set_log_base_dir(None)

    def test_private_message_not_output_when_protected(self):
        secret = "secret123"
        received = []
        completed = threading.Event()

        with tempfile.TemporaryDirectory() as temp_dir:
            logging_service.set_log_base_dir(temp_dir)
            logging_service.update_log_config(
                log_to_file=True, log_to_ui=True, privacy_protected=True
            )

            def callback(message):
                received.append(message)
                completed.set()

            logging_service.write_log(
                "安全摘要",
                callback,
                private_message=secret,
            )
            self.assertTrue(completed.wait(2.0))
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                file_text = handle.read()

        joined = "\n".join(received)
        # Hard invariant: private_message is discarded at enqueue time when
        # privacy_protected=True; it must never reach UI or file output.
        self.assertNotIn(secret, joined)
        self.assertNotIn(secret, file_text)
        # The public message is still delivered.
        self.assertIn("安全摘要", joined)


class PrivateMessageUnprotectedTests(unittest.TestCase):
    """Test 3: private_message is visible when privacy protection is off."""

    def setUp(self):
        self.previous_config = dict(logging_service.get_log_config())

    def tearDown(self):
        logging_service.flush_logs()
        logging_service.update_log_config(**self.previous_config)
        logging_service.set_log_base_dir(None)

    def test_private_message_output_when_unprotected(self):
        private_text = "详细调试信息 visible-debug"
        received = []
        completed = threading.Event()

        with tempfile.TemporaryDirectory() as temp_dir:
            logging_service.set_log_base_dir(temp_dir)
            logging_service.update_log_config(
                log_to_file=True, log_to_ui=True, privacy_protected=False
            )

            def callback(message):
                received.append(message)
                completed.set()

            logging_service.write_log(
                "公开摘要",
                callback,
                private_message=private_text,
            )
            self.assertTrue(completed.wait(2.0))
            self.assertTrue(logging_service.flush_logs())

        joined = "\n".join(received)
        # When protection is off, the private_message variant is selected.
        self.assertIn(private_text, joined)
        # The public message should NOT be the one delivered.
        self.assertNotIn("公开摘要", joined)


class CrashGuardDetailPathRedactionTests(unittest.TestCase):
    """Test 4: crash_guard._safe_detail redacts local paths."""

    def test_path_is_redacted_in_safe_detail(self):
        result = crash_guard._safe_detail(r"C:\Users\secret\file.txt")
        # Hard invariant: absolute paths are replaced by <隐藏路径>.
        self.assertNotIn("secret", result)
        self.assertNotIn(r"C:\Users", result)
        self.assertIn("<隐藏路径>", result)

    def test_unc_path_is_redacted(self):
        result = crash_guard._safe_detail(r"\\server\share\secret_doc.xlsx")
        self.assertNotIn("secret_doc", result)
        self.assertIn("<隐藏路径>", result)


class CrashGuardDetailNonPathTests(unittest.TestCase):
    """崩溃详情中的非路径秘密也必须被擦除。"""

    def test_non_path_value_is_redacted(self):
        result = crash_guard._safe_detail("password=abc123")
        self.assertNotIn("abc123", result)
        self.assertIn("<隐藏秘密值>", result)


class CredentialValueNotInLogTests(unittest.TestCase):
    """Test 6: _execute_secret_text logs only the length, never the content."""

    def test_secret_text_log_contains_length_not_value(self):
        from fukua_rpa.engine import RPAEngine
        from fukua_rpa.engine.actions_exec import StepActionContext

        secret_value = "super-secret-credential-xyz"
        log_messages = []

        engine = RPAEngine.__new__(RPAEngine)
        engine._credential_store = None
        engine.base_dir = None
        engine.log_level = 1
        engine._log_policy = None
        engine.performance = mock.MagicMock()
        engine.callback_msg = None

        # Mock credential_store to return our known secret.
        mock_store = mock.MagicMock()
        mock_store.get.return_value = secret_value
        engine._credential_store = mock_store

        # Capture log calls.
        def capture_log(msg, category=None, *, critical=False, private_msg=None):
            log_messages.append(msg)
            if private_msg:
                log_messages.append(private_msg)
            return True

        engine.log = capture_log

        # Mock _send_private_text to capture what it receives and log.
        def mock_send_private_text(value, label, *, allow_unprotected_log=False):
            # Replicate the real logging behaviour of _send_private_text:
            # it logs "    -> {label}（{len(text)} 个字符）" publicly.
            text = str(value)
            public_msg = f"    -> {label}（{len(text)} 个字符）"
            log_messages.append(public_msg)
            if allow_unprotected_log:
                from fukua_rpa.privacy import private_debug_value
                log_messages.append(f"    -> {label}：{private_debug_value(text)}")
            return "success"

        engine._send_private_text = mock_send_private_text

        # Build a minimal context for TASK_TYPE_SECRET_TEXT.
        from fukua_rpa.engine.actions_exec import TASK_TYPE_SECRET_TEXT

        ctx = StepActionContext(
            command=float(TASK_TYPE_SECRET_TEXT),
            value="我的凭据",
            retry=1,
            step_info={"step": 1, "loop": 1},
            cache_key="test",
            confidence=0.8,
            use_gray=True,
            task={},
        )

        result = engine._execute_secret_text(ctx)

        self.assertEqual(result, "success")
        # The secret value must NOT appear in any log message.
        all_output = "\n".join(log_messages)
        self.assertNotIn(secret_value, all_output)
        # The length-based public message IS present.
        self.assertIn(f"{len(secret_value)} 个字符", all_output)
        # credential_store.get was called with the credential name.
        mock_store.get.assert_called_once_with("我的凭据")


if __name__ == "__main__":
    unittest.main()
