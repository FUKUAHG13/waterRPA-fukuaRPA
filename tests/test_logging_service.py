import os
import queue
import tempfile
import threading
import unittest
from unittest import mock

from fukua_rpa import logging_service


class LoggingServiceTests(unittest.TestCase):
    def setUp(self):
        self.previous_config = dict(logging_service.get_log_config())

    def tearDown(self):
        logging_service.flush_logs()
        logging_service.update_log_config(**self.previous_config)
        logging_service.set_log_base_dir(None)

    def test_callback_failure_does_not_block_following_log_items(self):
        completed = threading.Event()
        logging_service.update_log_config(log_to_file=False, log_to_ui=True)

        def broken(_message):
            raise RuntimeError("callback failed")

        logging_service.write_log("first", broken)
        logging_service.write_log("second", lambda _message: completed.set())
        self.assertTrue(completed.wait(1.0))
        self.assertTrue(logging_service.flush_logs())

    def test_bounded_queue_drops_without_blocking_and_reports_count(self):
        local_queue = queue.Queue(maxsize=1)
        local_queue.put_nowait(object())
        record = logging_service.LogRecord(
            message="overflow",
            private_message=None,
            callback=None,
            event_time_ns=0,
        )
        before = logging_service.log_queue_stats()["dropped_total"]

        started = logging_service.time.monotonic()
        queued = logging_service._enqueue_log_record(record, local_queue)
        elapsed = logging_service.time.monotonic() - started
        dropped_notice = logging_service._take_drop_notice(force=True)

        self.assertFalse(queued)
        self.assertLess(elapsed, 0.05)
        self.assertGreaterEqual(dropped_notice, 1)
        self.assertEqual(
            logging_service.log_queue_stats()["dropped_total"], before + 1
        )

    def test_file_logging_rotates_and_flushes(self):
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            logging_service, "MAX_LOG_BYTES", 160
        ):
            logging_service.set_log_base_dir(temp_dir)
            logging_service.update_log_config(log_to_file=True, log_to_ui=False)
            for index in range(30):
                logging_service.write_log(f"line-{index}-" + "x" * 40)
            self.assertTrue(logging_service.flush_logs())
            path = os.path.join(temp_dir, "rpa_debug_log.txt")
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(os.path.isfile(path + ".1"))
            self.assertLessEqual(
                len([name for name in os.listdir(temp_dir) if name.startswith("rpa_debug_log.txt.")]),
                logging_service.LOG_BACKUP_COUNT,
            )

    def test_event_time_is_shared_by_file_and_timestamped_ui(self):
        event_time_ns = 1_700_000_000_123_000_000
        expected_time = logging_service.format_local_timestamp(event_time_ns)
        received = []
        completed = threading.Event()

        with tempfile.TemporaryDirectory() as temp_dir:
            logging_service.set_log_base_dir(temp_dir)
            logging_service.update_log_config(log_to_file=True, log_to_ui=True)

            def callback(message):
                received.append(message)
                completed.set()

            logging_service.write_log(
                "timed-event",
                callback,
                ui_timestamp=True,
                event_time_ns=event_time_ns,
            )
            self.assertTrue(completed.wait(1.0))
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                text = handle.read()

        self.assertEqual(received, [f"[{expected_time}] timed-event"])
        self.assertIn(f"[{expected_time}] timed-event", text)

    def test_registered_private_path_is_redacted_from_file_and_ui(self):
        received = []
        completed = threading.Event()
        with tempfile.TemporaryDirectory() as temp_dir:
            logging_service.set_log_base_dir(temp_dir)
            logging_service.update_log_config(log_to_file=True, log_to_ui=True)

            def callback(message):
                received.append(message)
                completed.set()

            logging_service.write_log(
                f"failed near {temp_dir}\\private.png", callback
            )
            self.assertTrue(completed.wait(1.0))
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                file_text = handle.read()

        self.assertNotIn(temp_dir, "\n".join(received))
        self.assertNotIn(temp_dir, file_text)
        self.assertIn("<本机目录>", file_text)

    def test_privacy_opt_out_uses_private_message_without_disabling_plain_text_safety(self):
        received = []
        completed = threading.Event()
        with tempfile.TemporaryDirectory() as temp_dir:
            logging_service.set_log_base_dir(temp_dir)
            logging_service.update_log_config(
                log_to_file=True,
                log_to_ui=True,
                privacy_protected=False,
            )

            def callback(message):
                received.append(message)
                completed.set()

            private_message = (
                f"raw path {temp_dir} <b>ordinary debug value</b>\x1b[31m"
            )
            logging_service.write_log(
                "protected summary",
                callback,
                private_message=private_message,
            )
            self.assertTrue(completed.wait(1.0))
            self.assertTrue(logging_service.flush_logs())
            with open(
                os.path.join(temp_dir, "rpa_debug_log.txt"), encoding="utf-8"
            ) as handle:
                file_text = handle.read()

        joined = "\n".join(received)
        self.assertIn(temp_dir, joined)
        self.assertIn(temp_dir, file_text)
        self.assertIn("ordinary debug value", joined)
        self.assertNotIn("<b>", joined)
        self.assertNotIn("\x1b", joined)
        self.assertIn("\\x1b", joined)
        self.assertNotIn("protected summary", joined)

    def test_privacy_state_is_captured_when_record_is_enqueued(self):
        completed = threading.Event()
        received = []
        logging_service.update_log_config(
            log_to_file=False,
            log_to_ui=True,
            privacy_protected=True,
        )
        logging_service.write_log(
            "protected summary",
            lambda message: (received.append(message), completed.set()),
            private_message="PRIVATE_ENQUEUE_SENTINEL",
        )
        logging_service.update_log_config(privacy_protected=False)

        self.assertTrue(completed.wait(1.0))
        self.assertTrue(logging_service.flush_logs())
        self.assertEqual(received, ["protected summary"])

    def test_protected_queue_record_does_not_retain_private_copy(self):
        captured = []
        logging_service.update_log_config(privacy_protected=True)
        with mock.patch.object(
            logging_service,
            "_enqueue_log_record",
            side_effect=lambda record: captured.append(record) or True,
        ):
            logging_service.write_log(
                "protected summary",
                private_message="PRIVATE_QUEUE_SENTINEL",
            )

        self.assertEqual(len(captured), 1)
        self.assertIsNone(captured[0].private_message)

    def test_crash_log_omits_exception_value_and_source_line(self):
        secret = "PRIVATE_CRASH_SENTINEL"
        captured = []
        logging_service.update_log_config(privacy_protected=False)
        try:
            raise RuntimeError(secret)
        except RuntimeError as error:
            exception_type = type(error)
            traceback_object = error.__traceback__
            with mock.patch.object(
                logging_service, "_append_direct", captured.append
            ), mock.patch.object(logging_service.sys, "__excepthook__"):
                logging_service.global_exception_handler(
                    exception_type, error, traceback_object
                )

        report = "\n".join(captured)
        self.assertIn("RuntimeError", report)
        self.assertNotIn(secret, report)

    def test_markup_controls_and_oversized_messages_remain_bounded_plain_text(self):
        received = []
        completed = threading.Event()
        logging_service.update_log_config(log_to_file=False, log_to_ui=True)
        payload = (
            "<b>普通文字</b><img src='not-a-file'><script>脚本字样</script>"
            "\x1b[31m\u202e" + "x" * 70_000
        )

        def callback(message):
            received.append(message)
            completed.set()

        logging_service.write_log(payload, callback)
        self.assertTrue(completed.wait(1.0))
        self.assertTrue(logging_service.flush_logs())

        text = received[0]
        self.assertNotIn("<b>", text)
        self.assertNotIn("<script>", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\u202e", text)
        self.assertIn("\\x1b", text)
        self.assertIn("日志内容过长", text)
        self.assertLess(len(text), 66_000)


if __name__ == "__main__":
    unittest.main()
