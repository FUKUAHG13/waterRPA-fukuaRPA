import re
import unittest
from unittest import mock

from fukua_rpa.engine import RPAEngine
from fukua_rpa.logging_service import GLOBAL_CONFIG, flush_logs

TIMESTAMP_PATTERN = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\] ")


class LoggingLevelTests(unittest.TestCase):
    def setUp(self):
        self.previous_config = dict(GLOBAL_CONFIG)
        GLOBAL_CONFIG.update({"log_to_file": False, "log_to_ui": True})

    def tearDown(self):
        flush_logs()
        GLOBAL_CONFIG.update(self.previous_config)

    def create_engine(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine(defer_backends=True)
        engine.detect_delay = 0
        engine.settlement_wait = 0
        return engine

    def test_ui_timestamps_start_at_detailed_level(self):
        engine = self.create_engine()
        messages = []
        engine.callback_msg = messages.append

        engine.log_level = 0
        engine.log("simple-event")
        engine.log_level = 1
        engine.log("detailed-event")
        self.assertTrue(flush_logs())

        self.assertEqual(messages[0], "simple-event")
        self.assertRegex(messages[1], TIMESTAMP_PATTERN)
        self.assertTrue(messages[1].endswith("detailed-event"))

    def test_complete_run_emits_safe_parameters_and_phase_timings(self):
        engine = self.create_engine()
        engine.log_level = 2
        engine.load_and_precompute = lambda _tasks: True
        engine.execute_task_once = mock.Mock(return_value="success")
        messages = []

        engine.run_tasks(
            [{"type": 4.0, "value": "sensitive-user-text"}],
            callback_msg=messages.append,
        )
        self.assertTrue(flush_logs())
        joined = "\n".join(messages)

        for section in (
            "完全/运行参数",
            "完全/资源预载",
            "完全/步骤参数",
            "完全/步骤耗时",
            "完全/步骤转移",
            "完全/运行性能报告",
        ):
            self.assertIn(section, joined)
        self.assertIn("all_timing_changes_ms", joined)
        self.assertIn("raw task values, private text, window titles", joined)
        self.assertNotIn("sensitive-user-text", joined)
        self.assertTrue(all(TIMESTAMP_PATTERN.match(message) for message in messages))

    def test_complete_run_can_emit_ordinary_debug_values_after_explicit_opt_out(self):
        engine = self.create_engine()
        engine.log_level = 2
        engine.load_and_precompute = lambda _tasks: True
        engine.execute_task_once = mock.Mock(return_value="success")
        messages = []
        GLOBAL_CONFIG["privacy_protected"] = False

        engine.run_tasks(
            [{"type": 4.0, "value": "ordinary-debug-sentinel"}],
            callback_msg=messages.append,
        )
        self.assertTrue(flush_logs())
        joined = "\n".join(messages)

        self.assertIn("ordinary-debug-sentinel", joined)
        self.assertIn("log privacy protection disabled", joined)
        self.assertIn("credential secret values remain prohibited", joined)
        self.assertIn('"log_privacy_protection": false', joined)
        self.assertIn('"credential_values_logged": false', joined)
        self.assertIn('"secret_text_action_contents_logged": false', joined)


if __name__ == "__main__":
    unittest.main()
