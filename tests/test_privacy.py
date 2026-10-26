import json
import os
import tempfile
import threading
import unittest

from fukua_rpa import logging_service, privacy


class PathRedactionTests(unittest.TestCase):
    def test_drive_and_unc_paths_are_redacted(self):
        samples = (
            r"D:\Desktop\project\assets\login.png",
            "E:/data/config.ini",
            r"\\fileserver\share\team\secret.docx",
            '"D:\\Folder With Spaces\\private file.txt"',
        )
        for value in samples:
            with self.subTest(value=value):
                redacted = privacy.plain_log_text(value)
                self.assertIn("<隐藏路径>", redacted)
                self.assertNotIn("secret", redacted.casefold())
                self.assertNotIn("private file", redacted.casefold())

    def test_redact_sensitive_paths_public_helper(self):
        # Review item #11: plugin error/result text can be scrubbed at the host
        # boundary via the public helper without stripping markup/HTML.
        text = 'plugin error: cannot open C:/Users/me/docs/a.png at D:/tmp/b.xlsx'
        redacted = privacy.redact_sensitive_paths(text)
        self.assertNotIn("C:/Users/me", redacted)
        self.assertNotIn("D:/tmp/b.xlsx", redacted)
        self.assertIn("<隐藏路径>", redacted)
        self.assertIn("plugin error:", redacted)

    def test_redact_sensitive_paths_empty(self):
        self.assertEqual(privacy.redact_sensitive_paths(""), "")
        self.assertEqual(privacy.redact_sensitive_paths(None), None)

    def test_urls_and_non_path_text_are_preserved(self):
        samples = (
            "https://example.com/a/b",
            "http://example.com/path",
            "步骤 3 于 12:34:56 完成，比例 3:1",
        )
        for value in samples:
            with self.subTest(value=value):
                self.assertEqual(privacy.plain_log_text(value), value)

    def test_file_url_redacts_its_embedded_local_path(self):
        redacted = privacy.plain_log_text("file:///C:/data/private/x.png")
        self.assertEqual(redacted, "file:///<隐藏路径>")

    def test_path_redaction_preserves_text_after_an_english_comma(self):
        redacted = privacy.plain_log_text(r"文件 D:\private\x.png, loaded")
        self.assertEqual(redacted, "文件 <隐藏路径>, loaded")

    def test_path_redaction_can_be_disabled_for_local_debugging(self):
        value = r"D:\Desktop\project\login.png"
        self.assertEqual(
            privacy.plain_log_text(value, redact_paths=False),
            value,
        )


class CredentialPrivacyTests(unittest.TestCase):
    """Verify credential secret values never enter logs or exports."""

    def setUp(self):
        self.previous_config = dict(logging_service.get_log_config())

    def tearDown(self):
        logging_service.flush_logs()
        logging_service.update_log_config(**self.previous_config)
        logging_service.set_log_base_dir(None)

    def test_secret_text_value_not_in_public_log(self):
        """A secret-text value handed to the private channel never reaches public logs.

        The engine logs only the credential *reference* publicly; the resolved
        secret value travels through ``private_message`` and must never appear in
        the UI callback or the log file while privacy protection is enabled. A
        local path embedded in the public text must still be redacted.
        """
        secret = "my-super-secret-password-12345"
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
                r"输入秘密文本: 登录凭据 (D:\private\cred.png)",
                callback,
                private_message=secret,
            )
            self.assertTrue(completed.wait(1.0))
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                file_text = handle.read()

        joined = "\n".join(received)
        # The public reference name is logged ...
        self.assertIn("输入秘密文本", joined)
        self.assertIn("登录凭据", joined)
        # ... but the secret value never reaches the UI callback or the file.
        self.assertNotIn(secret, joined)
        self.assertNotIn(secret, file_text)
        # The local path in the public message is still redacted.
        self.assertNotIn("D:\\private", joined)
        self.assertNotIn("D:\\private", file_text)
        self.assertIn("<隐藏路径>", joined)

    def test_diagnostic_export_excludes_credentials(self):
        """Diagnostic report must not contain credential secret values.

        A credential store holding a known secret is placed inside the very
        directory that diagnostics inspects; the serialized report must not
        leak the secret value while still surfacing non-sensitive metadata.
        """
        from fukua_rpa.credentials import CredentialStore
        from fukua_rpa.diagnostics import run_runtime_diagnostics

        secret = "DIAGNOSTIC_SECRET_VALUE_12345"
        with tempfile.TemporaryDirectory() as temp_dir:
            store = CredentialStore(
                temp_dir,
                protect=lambda value: value,
                unprotect=lambda value: value,
            )
            store.set("登录凭据", secret)
            # Sanity: the secret is physically present in the inspected directory.
            self.assertEqual(store.get("登录凭据"), secret)

            report = run_runtime_diagnostics(base_dir=temp_dir)

        report_str = json.dumps(report, ensure_ascii=False, default=str)
        self.assertNotIn(secret, report_str)
        self.assertNotIn("DIAGNOSTIC_SECRET", report_str)
        # Non-sensitive metadata is still reported.
        self.assertTrue(report["private_paths_redacted"])
        self.assertEqual(report["format"], "fukuaRPA_runtime_diagnostics")


class PublicMessageSecretValueGapTests(unittest.TestCase):
    """防御纵深：秘密值误入公开 message 通道时须被第二道擦除层擦除（P3-4 修复后）。

    公开 message 通道最终经由 ``plain_log_text`` 处理（见 logging_service 中
    ``safe_message = plain_log_text(..., redact_paths=privacy_protected)``）。
    修复后 ``plain_log_text`` 除路径外还擦除常见秘密形态，因此调用方即使误把含
    高危关键词的秘密值写进公开 message，该值也不会原样进入 UI 回调和日志文件。
    """

    def test_secret_value_in_public_message_is_redacted(self):
        """修复后：秘密值误入公开 message 时被擦除，仅路径仍被脱敏。"""
        secret = "my-super-secret-password-12345"
        message = rf"输入秘密文本: 登录凭据 值为 {secret} (D:\private\cred.png)"

        redacted = privacy.plain_log_text(message)

        # 路径部分仍按现有机制被脱敏。
        self.assertNotIn("D:\\private", redacted)
        self.assertIn("<隐藏路径>", redacted)
        # 修复后：含高危关键词的秘密值本身被第二道擦除层擦除。
        self.assertNotIn(secret, redacted)
        self.assertIn("<隐藏秘密值>", redacted)

    def test_long_unbroken_secret_value_fully_redacted(self):
        """FR-20260809-D6-③: 超长无空格秘密值（PEM 私钥/长 API key）必须整值
        替换——修复前值段 512 上限只替换前缀，剩余明文残留日志。"""
        long_value = "A" * 900  # 超过旧 512 上限
        redacted = privacy.plain_log_text(f"private_key={long_value}")
        self.assertNotIn("AAAA", redacted)
        self.assertIn("<隐藏秘密值>", redacted)

    def test_secret_key_with_empty_value_not_mangled(self):
        """FR-20260809-D6-③: password= 后空值/仅引号不应产生伪替换。"""
        self.assertNotIn("<隐藏秘密值>", privacy.plain_log_text("password="))
        self.assertNotIn(
            "<隐藏秘密值>", privacy.plain_log_text("password=\"\""))
        # 普通文本不受影响。
        self.assertEqual(
            privacy.plain_log_text("比例 0.85 时长 120"), "比例 0.85 时长 120"
        )


class BearerAuthHeaderRedactionTests(unittest.TestCase):
    """FR-20260811-D3-001: Authorization 头的 token 本体必须整体擦除。

    修复前 ``_SECRET_CONTEXT_RE`` 先于 ``_BEARER_RE`` 执行，只把值段第一个词
    "Bearer" 擦掉，随后的 ``Bearer <token>`` 因前缀已消失而无法再被 ``_BEARER_RE``
    命中，导致 ``Authorization: <token明文>`` 残留日志。本类锁定修复后的行为：
    Bearer/Basic 鉴权头、proxy-authorization 与 ``password=Bearer <token>`` 形态
    的 token 本体均不得残留。
    """

    def _redact(self, text: str) -> str:
        return privacy.plain_log_text(text)

    def test_bearer_single_segment_token_fully_redacted(self):
        token = "sk-abc123def456xyz"
        redacted = self._redact(f"Authorization: Bearer {token}")
        self.assertNotIn(token, redacted)
        self.assertIn("<隐藏鉴权头>", redacted)

    def test_bearer_jwt_fully_redacted(self):
        token = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        redacted = self._redact(f"Authorization: Bearer {token}")
        self.assertNotIn("eyJhbGciOi", redacted)
        self.assertIn("<隐藏鉴权头>", redacted)

    def test_jwt_without_bearer_prefix_redacted(self):
        """裸 JWT（无 Bearer 前缀）也应按 JWT 规则脱敏。"""
        token = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        )
        redacted = self._redact(f"value: {token}")
        self.assertNotIn("eyJhbGciOi", redacted)
        self.assertIn("<隐藏令牌>", redacted)

    def test_plain_dotted_text_not_treated_as_jwt(self):
        """FR-20260812 建议-健壮性: 普通点分文本（首段非 JSON）不得被误判为
        JWT 而替换，避免误伤工作流文本。"""
        redacted = self._redact("hello_world.example_text.test_string")
        self.assertIn("hello_world.example_text.test_string", redacted)
        self.assertNotIn("<隐藏令牌>", redacted)

    def test_basic_credentials_fully_redacted(self):
        encoded = "dXNlcjpwYXNz"
        redacted = self._redact(f"authorization: Basic {encoded}")
        self.assertNotIn(encoded, redacted)
        self.assertIn("<隐藏鉴权头>", redacted)

    def test_proxy_authorization_fully_redacted(self):
        token = "tok-abcdefghijklmnop"
        redacted = self._redact(f"proxy-authorization: Bearer {token}")
        self.assertNotIn(token, redacted)
        self.assertIn("<隐藏鉴权头>", redacted)

    def test_bearer_value_inside_key_value_context_redacted(self):
        token = "abcdefghijklmnopqrstuvwx"
        redacted = self._redact(f"password=Bearer {token}")
        self.assertNotIn(token, redacted)
        # 上下文替换保留 key 与分隔符；值段被整体擦除。Bearer 形态先于
        # key=value 规则命中时占位符为 <隐藏鉴权头>，否则为 <隐藏秘密值>。
        self.assertIn("password=", redacted)
        self.assertRegex(redacted, r"<隐藏(鉴权头|秘密值)>")

    def test_plain_workflow_text_untouched(self):
        text = "正常日志: 流程完成 比例 0.85 时长 120"
        self.assertEqual(self._redact(text), text)

    def test_angle_bracket_value_is_not_treated_as_placeholder(self):
        """P2 回归守卫：真实尖括号值不得被占位符白名单误放行。

        走公共 API 直调（不经 HTML 解析），锁定 _context_repl 白名单行为。
        """
        secret = "token=<sk-abc123def456xyz>"
        redacted = privacy.redact_secret_values(secret)
        self.assertNotIn("sk-abc123def456xyz", redacted)
        self.assertIn("<隐藏秘密值>", redacted)

    def test_known_placeholder_survives_context_rewrite(self):
        """占位符经 _SECRET_CONTEXT_RE 二次匹配时原样保留。"""
        redacted = self._redact("token=<隐藏鉴权头>")
        self.assertIn("<隐藏鉴权头>", redacted)

    def test_bearer_without_token_leaves_no_secret(self):
        """Authorization: Bearer 无 token 时值段（Bearer 词）被擦除。"""
        redacted = self._redact("Authorization: Bearer")
        self.assertIn("<隐藏秘密值>", redacted)

    def test_short_basic_token_known_limitation(self):
        """既有限制（非本批引入）：_BEARER_RE 下限 {8,}，<8 字符 token 不按
        鉴权头形态擦除，短 b64 残留（key=authorization 只擦掉值段首词 Basic）。
        该用例锁定现状，避免未来无意识扩大/收缩匹配面。"""
        encoded = "dTpw"  # 4 字符 base64
        redacted = self._redact(f"authorization: Basic {encoded}")
        self.assertIn("<隐藏秘密值>", redacted)  # "Basic" 词被上下文规则擦除
        self.assertIn(encoded, redacted)  # 短 token 残留为已知限制


class LogPrivacyIntegrationTests(unittest.TestCase):
    """Cross-cutting privacy tests for the logging pipeline."""

    def setUp(self):
        self.previous_config = dict(logging_service.get_log_config())

    def tearDown(self):
        logging_service.flush_logs()
        logging_service.update_log_config(**self.previous_config)
        logging_service.set_log_base_dir(None)

    def test_private_msg_never_reaches_ui_callback(self):
        """Messages marked private must not be forwarded to UI callbacks.

        While privacy protection is enabled, ``write_log`` drops the private
        payload at enqueue time and the asynchronous log worker forwards only
        the public portion to the registered UI callback and the log file.
        """
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
                "公开摘要：输入秘密文本 <引用名>",
                callback,
                private_message="PRIVATE_UI_SENTINEL",
            )
            self.assertTrue(completed.wait(1.0))
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                file_text = handle.read()

        joined = "\n".join(received)
        self.assertEqual(len(received), 1)
        self.assertIn("公开摘要", joined)
        self.assertNotIn("PRIVATE_UI_SENTINEL", joined)
        self.assertNotIn("PRIVATE_UI_SENTINEL", file_text)


if __name__ == "__main__":
    unittest.main()
