import os
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.engine import RPAEngine
from fukua_rpa.logging_service import (
    SCHEDULED_RUN_LOG_SOURCE,
    register_processed_log_sink,
    unregister_processed_log_sink,
)
from fukua_rpa.runner.backend import (
    RUNNER_MODE_ISOLATED,
    RunnerBackendError,
    create_runner_session,
)
from fukua_rpa.runner.ipc import create_runner_pipe_name, validate_runner_pipe_name
from fukua_rpa.runner.isolated import (
    IsolatedRunnerThread,
    RemoteScriptPromptRequest,
)
from fukua_rpa.runner.job import RunnerJobGuard
from fukua_rpa.runner.protocol import (
    MAX_RUNNER_MESSAGE_BYTES,
    RUNNER_CAPABILITIES,
    RUNNER_PROTOCOL_VERSION,
    RunnerConnection,
    RunnerProtocolError,
    decode_runner_message,
    encode_runner_message,
    make_runner_hello_payload,
    make_runner_message,
    normalize_runner_runtime_summary,
    validate_runner_hello,
)
from fukua_rpa.scheduled_task_logs import ScheduledTaskLogStore
from fukua_rpa.ui.controllers.run_controller import RunController
from fukua_rpa.vision.scale_memory import ScaleMemoryPolicy, ScaleMemoryStore
from tests.qt_test_support import QtCallbackGuardMixin


class RunnerIsolationTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for_thread(self, thread, *, condition=None, timeout=20.0):
        from PySide6.QtCore import QEventLoop, QTimer

        loop = QEventLoop()
        timer = QTimer()
        timer.setInterval(10)

        def check():
            if not thread.isRunning() or condition is not None and condition():
                loop.quit()

        timer.timeout.connect(check)
        QTimer.singleShot(int(timeout * 1000), loop.quit)
        timer.start()
        loop.exec()
        timer.stop()
        self.app.processEvents()
        if condition is not None:
            return bool(condition())
        return not thread.isRunning()

    def test_protocol_is_versioned_json_and_rejects_mismatch(self):
        raw = encode_runner_message(
            make_runner_message("status", {"data": {"step": 2}})
        )
        decoded = decode_runner_message(raw)
        self.assertEqual(decoded["version"], RUNNER_PROTOCOL_VERSION)
        self.assertEqual(decoded["payload"]["data"]["step"], 2)

        incompatible = raw.replace(
            f'"version":{RUNNER_PROTOCOL_VERSION}'.encode(),
            b'"version":999',
        )
        with self.assertRaises(RunnerProtocolError):
            decode_runner_message(incompatible)

    def test_mouse_trail_event_round_trips_over_runner_click_channel(self):
        thread = IsolatedRunnerThread(
            [],
            default_profile_config(),
            base_dir=os.getcwd(),
            profile_name="mouse-trail-ipc-test",
            logging_options={},
            scale_memory_state={},
        )
        received = []
        thread.click_signal.connect(received.append)
        payload = {
            "kind": "mouse_trail",
            "points": [[10, 20], [30, 40]],
            "new_path": True,
        }

        encoded = encode_runner_message(
            make_runner_message("click", {"data": payload})
        )
        thread._handle_message(decode_runner_message(encoded))

        self.assertEqual(received, [payload])

    def test_runner_ipc_uses_only_a_valid_local_named_pipe(self):
        pipe_name = create_runner_pipe_name(
            process_id=4321,
            token="a" * 32,
        )
        self.assertEqual(validate_runner_pipe_name(pipe_name), pipe_name)
        self.assertTrue(pipe_name.startswith(r"\\.\pipe\fukuaRPA-runner-"))
        self.assertNotIn("127.0.0.1", pipe_name)
        with self.assertRaises(ValueError):
            validate_runner_pipe_name(r"\\.\pipe\foreign-runner")

    def test_runner_pipe_name_rejects_invalid_tokens_and_pids(self):
        # Token must be exactly 32 lowercase hex characters.
        for bad_token in ("xyz", "a" * 31, "a" * 33, "G" * 32):
            with self.subTest(token=bad_token), self.assertRaisesRegex(ValueError, "随机标识无效"):
                create_runner_pipe_name(process_id=1, token=bad_token)
        # A valid 32-hex token is accepted and round-trips through validation.
        good = create_runner_pipe_name(process_id=1, token="0123456789abcdef" * 2)
        self.assertEqual(validate_runner_pipe_name(good), good)
        # An omitted or empty token auto-generates a valid random pipe name.
        for auto_token in (None, ""):
            with self.subTest(auto_token=auto_token):
                generated = create_runner_pipe_name(
                    process_id=1, token=auto_token
                )
                self.assertEqual(
                    validate_runner_pipe_name(generated), generated
                )
        # Non-positive PIDs are rejected as well.
        for bad_pid in (0, -5):
            with self.subTest(pid=bad_pid), self.assertRaisesRegex(ValueError, "PID 无效"):
                create_runner_pipe_name(
                    process_id=bad_pid, token="a" * 32
                )

    def test_runner_messages_over_the_size_limit_are_rejected(self):
        # Decode path: oversized raw bytes are refused before any parsing.
        oversized = b"x" * (MAX_RUNNER_MESSAGE_BYTES + 1)
        with self.assertRaisesRegex(RunnerProtocolError, "超过上限"):
            decode_runner_message(oversized)

        # Encode path: a payload whose UTF-8 encoding exceeds the limit is
        # refused instead of being written to the pipe.
        message = make_runner_message(
            "status", {"blob": "x" * MAX_RUNNER_MESSAGE_BYTES}
        )
        with self.assertRaisesRegex(RunnerProtocolError, "字节上限"):
            encode_runner_message(message)

    def test_runner_messages_reject_non_json_serializable_payloads(self):
        with self.assertRaisesRegex(RunnerProtocolError, "无法序列化的类型"):
            make_runner_message("status", {"fn": lambda: 1})
        with self.assertRaisesRegex(RunnerProtocolError, "无法序列化的类型"):
            make_runner_message("status", {"items": {1, 2, 3}})
        # A directly-built message carrying an unserializable value is also
        # refused by the encode path's structural validation.
        with self.assertRaisesRegex(RunnerProtocolError, "无法序列化的类型"):
            encode_runner_message(
                {
                    "protocol": "fukuaRPA.runner",
                    "version": RUNNER_PROTOCOL_VERSION,
                    "kind": "status",
                    "payload": {"fn": lambda: 1},
                }
            )

    @unittest.skipUnless(os.name == "nt", "Job Object 只支持 Windows")
    def test_runner_job_guard_kills_attached_process_when_closed(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        guard = RunnerJobGuard()
        try:
            self.assertTrue(guard.create(), guard.error)
            self.assertTrue(guard.attach(process), guard.error)
            self.assertTrue(guard.snapshot()["kill_on_parent_exit"])
            guard.close()
            process.wait(timeout=3.0)
            self.assertIsNotNone(process.returncode)
        finally:
            guard.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3.0)

    def test_runner_start_fails_closed_when_job_guard_is_unavailable(self):
        thread = IsolatedRunnerThread(
            [],
            default_profile_config(),
            base_dir=os.getcwd(),
            profile_name="runner-job-failure",
            logging_options={},
            scale_memory_state={},
        )
        process = mock.Mock()
        process.pid = 4321
        process.wait.return_value = 1
        guard = mock.Mock()
        guard.create.return_value = False
        guard.error = "Job Object unavailable"
        with (
            mock.patch("fukua_rpa.runner.isolated.subprocess.Popen", return_value=process),
            mock.patch("fukua_rpa.runner.isolated.RunnerJobGuard", return_value=guard),
            self.assertRaisesRegex(RuntimeError, "Job Object unavailable"),
        ):
            thread._launch_process(
                SimpleNamespace(
                    address=create_runner_pipe_name(
                        process_id=4321,
                        token="b" * 32,
                    )
                ),
                "00" * 32,
            )
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=1.0)
        guard.close.assert_called_once_with()
        self.assertIsNone(thread._job_guard)

    def test_runner_hello_binds_pid_and_required_capabilities(self):
        hello = make_runner_message(
            "hello",
            make_runner_hello_payload(4321, RUNNER_CAPABILITIES),
        )

        validated = validate_runner_hello(hello, expected_pid=4321)

        self.assertEqual(validated["pid"], 4321)
        self.assertEqual(set(validated["capabilities"]), RUNNER_CAPABILITIES)
        with self.assertRaisesRegex(RunnerProtocolError, "PID 不匹配"):
            validate_runner_hello(hello, expected_pid=9876)

        incomplete = make_runner_message(
            "hello",
            make_runner_hello_payload(4321, {"control.stop"}),
        )
        with self.assertRaisesRegex(RunnerProtocolError, "缺少必需能力"):
            validate_runner_hello(incomplete, expected_pid=4321)

    def test_runner_hello_accepts_managed_launcher_parent_pid(self):
        hello = make_runner_message(
            "hello",
            make_runner_hello_payload(4321, RUNNER_CAPABILITIES),
        )
        hello["payload"]["ppid"] = 1234

        validated = validate_runner_hello(hello, expected_pid=1234)

        self.assertEqual(validated["pid"], 4321)
        self.assertEqual(validated["ppid"], 1234)
        hello["payload"].pop("ppid")
        with self.assertRaisesRegex(RunnerProtocolError, "PID 不匹配"):
            validate_runner_hello(hello, expected_pid=1234)

    def test_runner_messages_accept_managed_launcher_parent_pid(self):
        thread = IsolatedRunnerThread(
            [],
            default_profile_config(),
            base_dir=os.getcwd(),
            profile_name="launcher-pid-test",
            logging_options={},
            scale_memory_state={},
        )
        process = mock.Mock()
        process.pid = 1234
        process.poll.return_value = None
        thread._process = process
        thread._hello_info = {"pid": 4321}

        self.assertEqual(
            thread._message_pid({"pid": 4321, "ppid": 1234}, "heartbeat"),
            4321,
        )
        self.assertEqual(thread.process_id, 4321)
        with self.assertRaisesRegex(RunnerProtocolError, "PID 不匹配"):
            thread._message_pid({"pid": 4321, "ppid": 9999}, "heartbeat")

    def test_runner_messages_reject_boolean_process_ids(self):
        thread = IsolatedRunnerThread(
            [],
            default_profile_config(),
            base_dir=os.getcwd(),
            profile_name="boolean-pid-test",
            logging_options={},
            scale_memory_state={},
        )
        process = mock.Mock()
        process.pid = 1
        thread._process = process

        with self.assertRaisesRegex(RunnerProtocolError, "PID 无效"):
            thread._message_pid({"pid": True}, "heartbeat")
        with self.assertRaisesRegex(RunnerProtocolError, "父 PID 无效"):
            thread._message_pid({"pid": 4321, "ppid": True}, "heartbeat")

    def test_send_holds_channel_lock_through_transport_write(self):
        thread = IsolatedRunnerThread(
            [],
            default_profile_config(),
            base_dir=os.getcwd(),
            profile_name="send-lock-test",
            logging_options={},
            scale_memory_state={},
        )

        class InspectingChannel:
            lock_was_held = False

            def send(inner_self, _kind, _payload):
                inner_self.lock_was_held = thread._channel_lock.locked()

        channel = InspectingChannel()
        thread._channel = channel

        self.assertTrue(thread._send("stop"))
        self.assertTrue(channel.lock_was_held)

    def test_input_backend_does_not_advertise_unimplemented_release_tracking(self):
        engine = RPAEngine(defer_backends=True)
        capabilities = engine.input_backend.service_snapshot()["capabilities"]
        self.assertNotIn("tracked_release", capabilities)

    def test_runner_runtime_summary_drops_unapproved_private_fields(self):
        runtime = normalize_runner_runtime_summary(
            {
                "pid": 4321,
                "python_version": [3, 14, 0],
                "pointer_bits": 64,
                "frozen": False,
                "dll_path": "D:/private/fukua_rpa_core.dll",
                "opencv": {"available": True, "threads": 2},
                "native": {
                    "service": "vision",
                    "contract_version": 1,
                    "requested": True,
                    "available": True,
                    "api_version": 11500,
                    "capability_mask": 123,
                    "abi_compatible": True,
                    "cancellable_search": True,
                    "last_error": "private details",
                    "fallback": {"name": "opencv", "available": True},
                },
                "services": {
                    "catalog_version": 1,
                    "services": [
                        {
                            "service": "vision",
                            "contract_version": 1,
                            "backend": "native.dll",
                            "requested": True,
                            "available": True,
                            "native": True,
                            "capabilities": ["find.template"],
                            "cancellation": "cooperative",
                            "timeout_ms": 0,
                            "fallback": {"name": "opencv", "available": True},
                            "private_path": "D:/private/core.dll",
                        },
                        {
                            "service": "screenshot",
                            "contract_version": 1,
                            "backend": "native.dll",
                            "requested": True,
                            "available": True,
                            "native": True,
                            "capabilities": ["capture.rgb"],
                            "cancellation": "bounded_call",
                            "timeout_ms": 0,
                            "fallback": {
                                "name": "mss_pyautogui",
                                "available": True,
                            },
                        },
                        {
                            "service": "input",
                            "contract_version": 1,
                            "backend": "python.pyautogui",
                            "requested": True,
                            "available": True,
                            "native": False,
                            "capabilities": ["tracked_release"],
                            "cancellation": "cooperative",
                            "timeout_ms": 0,
                        },
                        {
                            "service": "hook",
                            "contract_version": 1,
                            "backend": "win32.ctypes",
                            "requested": False,
                            "available": True,
                            "native": True,
                            "capabilities": ["keyboard.low_level"],
                            "cancellation": "cooperative",
                            "timeout_ms": 0,
                        },
                    ],
                },
            }
        )

        self.assertNotIn("dll_path", runtime)
        self.assertNotIn("last_error", runtime["native"])
        self.assertEqual(runtime["native"]["fallback"]["name"], "opencv")
        self.assertEqual(runtime["services"]["catalog_version"], 1)
        self.assertNotIn(
            "private_path",
            next(
                item
                for item in runtime["services"]["services"]
                if item["service"] == "vision"
            ),
        )

    def test_runner_heartbeat_is_pid_checked_and_reported(self):
        thread = IsolatedRunnerThread(
            [],
            default_profile_config(),
            base_dir=os.getcwd(),
            profile_name="heartbeat-test",
            logging_options={},
            scale_memory_state={},
        )
        process = mock.Mock()
        process.pid = 4321
        thread._process = process
        health = []
        thread.health_signal.connect(health.append)

        thread._handle_message(
            make_runner_message(
                "heartbeat",
                {
                    "pid": 4321,
                    "sequence": 7,
                    "phase": "running",
                    "engine_running": True,
                    "stop_requested": False,
                },
            )
        )

        self.assertEqual(health[-1]["state"], "healthy")
        self.assertEqual(health[-1]["sequence"], 7)
        with self.assertRaisesRegex(RunnerProtocolError, "PID 不匹配"):
            thread._handle_message(
                make_runner_message(
                    "heartbeat",
                    {"pid": 9999, "sequence": 8, "phase": "running"},
                )
            )

    def test_stale_heartbeat_warns_without_terminating_the_process(self):
        thread = IsolatedRunnerThread(
            [],
            default_profile_config(),
            base_dir=os.getcwd(),
            profile_name="heartbeat-stale-test",
            logging_options={},
            scale_memory_state={},
        )
        process = mock.Mock()
        process.pid = 4321
        process.poll.return_value = None
        thread._process = process
        thread._last_heartbeat_at = time.monotonic() - 60
        health = []
        logs = []
        thread.health_signal.connect(health.append)
        thread.log_signal.connect(logs.append)

        thread._check_heartbeat_health(process)

        self.assertEqual(health[-1]["state"], "unresponsive")
        self.assertIn("不会被自动终止", logs[-1])
        process.kill.assert_not_called()
        process.terminate.assert_not_called()

    def test_remote_prompt_relays_only_the_first_answer(self):
        answers = []
        request = RemoteScriptPromptRequest(
            "prompt-1",
            "hello",
            lambda prompt_id, accepted: answers.append((prompt_id, accepted)),
        )

        request.acknowledge()
        request.cancel()

        self.assertTrue(request.done.is_set())
        self.assertTrue(request.acknowledged)
        self.assertFalse(request.cancelled)
        self.assertEqual(answers, [("prompt-1", True)])

    def test_unknown_backend_name_is_rejected_instead_of_silent_fallback(self):
        engine = RPAEngine(defer_backends=True)
        with self.assertRaises(RunnerBackendError):
            create_runner_session("future-unknown", engine, [])
        self.assertFalse(engine.is_running)

    def test_run_controller_reports_the_process_carrying_the_workload(self):
        controller = RunController(RPAEngine(defer_backends=True))
        worker = mock.Mock()
        worker.isRunning.return_value = True
        worker.process_id = 54321
        controller.worker = worker
        controller.backend_mode = RUNNER_MODE_ISOLATED

        self.assertEqual(
            controller.resource_process_target(),
            (54321, "独立 Runner"),
        )

        worker.isRunning.return_value = False
        process_id, label = controller.resource_process_target()
        self.assertEqual(process_id, os.getpid())
        self.assertEqual(label, "本程序")

    def test_run_controller_ignores_late_state_from_an_old_runner(self):
        engine = RPAEngine(defer_backends=True)
        controller = RunController(engine)
        current = object()
        old = object()
        controller.worker = current

        controller._apply_runtime_state(
            old, {"performance_report": {"source": "old"}}
        )
        self.assertEqual(engine.last_performance_report, {})

        controller._apply_runtime_state(
            current, {"performance_report": {"source": "current"}}
        )
        self.assertEqual(
            engine.last_performance_report, {"source": "current"}
        )

    def test_scale_memory_round_trips_between_processes(self):
        policy = ScaleMemoryPolicy(enabled=True)
        source = ScaleMemoryStore()
        source.record(
            ("C:/template.png", True),
            "template.png",
            (0.8, 1.0, 1.2),
            1.0,
            0.95,
            policy,
            generation=(10, 20),
        )
        restored = ScaleMemoryStore()

        self.assertTrue(restored.import_state(source.export_state()))
        summaries = restored.summaries(policy)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].observed_count, 1)
        self.assertEqual(summaries[0].preferred_scales, (1.0,))

    @unittest.skipUnless(os.name == "nt", "独立 Runner 目前只支持 Windows")
    def test_isolated_runner_debug_prompt_and_stop_channels(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            errors = []
            debug_events = []
            prompts = []
            runtime_states = []
            runner_infos = []
            health_events = []
            tasks = [
                {
                    "type": 12.0,
                    "value": "runner prompt",
                    "debug_breakpoint": True,
                },
                {"type": 5.0, "value": "30"},
            ]
            thread = IsolatedRunnerThread(
                tasks,
                default_profile_config(),
                base_dir=temp_dir,
                profile_name="runner-test",
                logging_options={
                    "log_to_ui": False,
                    "log_to_file": False,
                    "privacy_protected": True,
                },
                scale_memory_state={},
            )
            thread.error_signal.connect(errors.append)
            thread.runtime_state_signal.connect(runtime_states.append)
            thread.runner_info_signal.connect(runner_infos.append)
            thread.health_signal.connect(health_events.append)

            def handle_debug(data):
                debug_events.append(data.get("state"))
                if data.get("state") == "paused":
                    thread.continue_run()

            def handle_prompt(request):
                prompts.append(request.text)
                request.acknowledge()

            thread.debug_signal.connect(handle_debug)
            thread.prompt_signal.connect(handle_prompt)
            thread.start()
            self.addCleanup(
                lambda: thread.request_stop() if thread.isRunning() else None
            )
            self.addCleanup(lambda: thread.wait(3000))

            reached_prompt = self.wait_for_thread(
                thread,
                condition=lambda: bool(prompts),
            )
            self.assertTrue(reached_prompt)
            self.assertIsInstance(thread.process_id, int)
            stopped_at = time.monotonic()
            self.assertTrue(thread.request_stop())
            self.wait_for_thread(thread, timeout=5.0)
            thread.wait(1000)
            self.app.processEvents()

            self.assertFalse(thread.isRunning())
            self.assertIsNone(thread.process_id)
            self.assertLess(time.monotonic() - stopped_at, 2.5)
            self.assertEqual(errors, [])
            self.assertEqual(prompts, ["runner prompt"])
            self.assertEqual(debug_events[:2], ["paused", "resumed"])
            self.assertTrue(runtime_states)
            self.assertTrue(runner_infos)
            runtime = runner_infos[-1]["runtime"]
            self.assertEqual(runtime["pid"], runner_infos[-1]["pid"])
            self.assertIn("native", runtime)
            self.assertIn("capability_mask", runtime["native"])
            self.assertEqual(runtime["services"]["catalog_version"], 1)
            self.assertEqual(
                {item["service"] for item in runtime["services"]["services"]},
                {"screenshot", "vision", "input", "hook"},
            )
            self.assertEqual(thread.last_exit_report["outcome"], "user_stopped")
            self.assertTrue(
                thread.last_exit_report["job_guard"]["kill_on_parent_exit"]
            )
            self.assertTrue(
                any(event.get("state") == "healthy" for event in health_events)
            )
            self.assertEqual(health_events[-1]["state"], "stopped")

    @unittest.skipUnless(os.name == "nt", "独立 Runner 目前只支持 Windows")
    def test_isolated_runner_relays_scheduled_logs_when_ui_log_is_disabled(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ScheduledTaskLogStore(
                os.path.join(temp_dir, "scheduled_task_logs.jsonl")
            )
            sink = store.append_processed
            register_processed_log_sink(sink)
            self.addCleanup(lambda: unregister_processed_log_sink(sink))
            thread = IsolatedRunnerThread(
                [{"type": 5.0, "value": "0"}],
                default_profile_config(),
                base_dir=temp_dir,
                profile_name="scheduled-log-ipc-test",
                logging_options={
                    "log_to_ui": False,
                    "log_to_file": False,
                    "privacy_protected": True,
                },
                log_context={
                    "log_source": SCHEDULED_RUN_LOG_SOURCE,
                    "schedule_id": "schedule-ipc",
                    "run_id": "run-ipc",
                },
                scale_memory_state={},
            )
            visible_logs = []
            errors = []
            thread.log_signal.connect(visible_logs.append)
            thread.error_signal.connect(errors.append)
            thread.start()
            self.addCleanup(
                lambda: thread.request_stop() if thread.isRunning() else None
            )
            self.addCleanup(lambda: thread.wait(3000))

            self.assertTrue(self.wait_for_thread(thread, timeout=15.0))
            thread.wait(1000)
            self.app.processEvents()

            self.assertEqual(errors, [])
            self.assertEqual(visible_logs, [])
            entries = store.entries_for("schedule-ipc", "run-ipc")
            self.assertTrue(entries)
            self.assertTrue(any(row.get("message") for row in entries))

    @unittest.skipUnless(os.name == "nt", "独立 Runner 目前只支持 Windows")
    def test_isolated_runner_crash_recovers_ui_session_without_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            errors = []
            runner_infos = []
            health_events = []
            thread = IsolatedRunnerThread(
                [{"type": 5.0, "value": "30"}],
                default_profile_config(),
                base_dir=temp_dir,
                profile_name="runner-crash-test",
                logging_options={
                    "log_to_ui": False,
                    "log_to_file": False,
                    "privacy_protected": True,
                },
                scale_memory_state={},
            )
            thread.error_signal.connect(errors.append)
            thread.runner_info_signal.connect(runner_infos.append)
            thread.health_signal.connect(health_events.append)
            release = mock.Mock(wraps=thread._release_tracked_inputs)
            thread._release_tracked_inputs = release
            thread.start()
            self.addCleanup(
                lambda: thread.request_stop() if thread.isRunning() else None
            )
            self.addCleanup(lambda: thread.wait(3000))

            self.assertTrue(
                self.wait_for_thread(
                    thread,
                    condition=lambda: bool(runner_infos),
                    timeout=10.0,
                )
            )
            first_pid = runner_infos[0]["pid"]
            process = thread._process
            self.assertIsNotNone(process)
            process.kill()
            self.wait_for_thread(thread, timeout=5.0)
            thread.wait(1000)
            self.app.processEvents()

            self.assertFalse(thread.isRunning())
            self.assertEqual(thread.last_exit_report["outcome"], "crashed")
            self.assertTrue(any("意外退出" in message for message in errors))
            self.assertEqual(health_events[-1]["state"], "stopped")
            self.assertTrue(release.called)
            self.assertTrue(
                all(info.get("pid") == first_pid for info in runner_infos)
            )

    @unittest.skipUnless(os.name == "nt", "独立 Runner 目前只支持 Windows")
    def test_emergency_stop_releases_tracked_inputs_like_normal_path(self):
        """FR-20260812-D1-101: 急停路径（_force_stop_process）与正常收尾路径
        必须释放同一批跟踪输入（等义性断言），确保停止后无残留按键/鼠标。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            thread = IsolatedRunnerThread(
                [{"type": 5.0, "value": "30"}],
                default_profile_config(),
                base_dir=temp_dir,
                profile_name="runner-emergency-test",
                logging_options={
                    "log_to_ui": False,
                    "log_to_file": False,
                    "privacy_protected": True,
                },
                scale_memory_state={},
            )
            # 模拟子进程已上报按下状态：Ctrl + 左键
            thread._tracked_keys = ["ctrl"]
            thread._tracked_buttons = ["left"]
            release_spy = mock.Mock(wraps=thread._release_tracked_inputs)
            thread._release_tracked_inputs = release_spy
            thread._process = mock.Mock()
            thread._process.poll.return_value = None

            thread._force_stop_process()

            self.assertTrue(release_spy.called)
            self.assertEqual(thread._tracked_keys, [])
            self.assertEqual(thread._tracked_buttons, [])


class _ClosedPipeConnection:
    """模拟对端已关闭的 multiprocessing 连接：poll 抛 BrokenPipeError。"""

    def poll(self, _timeout=None):
        raise BrokenPipeError(109, "管道已结束。")

    def close(self):
        pass


class _OpenPipeConnection:
    """模拟对端仍连通：poll 返回可用性，且记录收到的超时参数。"""

    def __init__(self, available):
        self._available = available
        self.seen_timeout = None

    def poll(self, timeout=None):
        self.seen_timeout = timeout
        return self._available

    def close(self):
        pass


class RunnerConnectionPollTests(unittest.TestCase):
    """FR-20260930-014：poll 必须把「对端已关闭」收敛为 False，不抛异常。"""

    def test_poll_returns_false_when_peer_closed(self):
        """对端关闭时 poll 返回 False，而不是把 BrokenPipeError 冒泡给调用方。

        修复前：异常逃逸到 IsolatedRunnerThread 主循环的 `except Exception`，
        经 `_emit_local_error(str(error))` 把英文内部错误原文显示给用户。
        """
        connection = RunnerConnection(_ClosedPipeConnection())
        self.assertFalse(connection.poll(0.0))

    def test_poll_reports_availability(self):
        """正常连通时 poll 仍如实回报可用性（不得恒为 False 导致忙等）。"""
        available = _OpenPipeConnection(True)
        self.assertTrue(RunnerConnection(available).poll(0.5))
        self.assertAlmostEqual(available.seen_timeout, 0.5)

        unavailable = _OpenPipeConnection(False)
        self.assertFalse(RunnerConnection(unavailable).poll(0.0))
        self.assertAlmostEqual(unavailable.seen_timeout, 0.0)

    def test_poll_clamps_negative_timeout(self):
        """负超时被钳为 0（非阻塞），不把非法值透传给底层连接。"""
        connection = _OpenPipeConnection(False)
        RunnerConnection(connection).poll(-1.0)
        self.assertAlmostEqual(connection.seen_timeout, 0.0)


class ForcedStopOutcomeTests(unittest.TestCase):
    """FR-20260930-015：`forced_stop` 终局必须可达（ARCHITECTURE.md #73 五分类）。

    修复前：`_forced_stop` 只在 `_force_stop_process` 内、且被
    `not _stop_requested.is_set()` 门控，而两处强杀调用点的前置都蕴含
    `_stop_requested` 已置位，故该分类恒不可达；收尾阶段（`finally`）的终止
    也不会置位该标志。
    """

    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_dir.cleanup)
        self.thread = IsolatedRunnerThread(
            [{"type": 5.0, "value": "30"}],
            default_profile_config(),
            base_dir=self._tmp_dir.name,
            profile_name="runner-forced-stop-test",
            logging_options={
                "log_to_ui": False,
                "log_to_file": False,
                "privacy_protected": True,
            },
            scale_memory_state={},
        )
        self.addCleanup(
            lambda: self.thread.request_stop()
            if self.thread.isRunning()
            else None
        )

    @staticmethod
    def _live_process():
        process = mock.Mock()
        process.poll.return_value = None
        return process

    def test_exit_report_ladder_maps_flags_to_outcomes(self):
        """`_build_exit_report` 的五分类阶梯（含 forced_stop 的优先级）。"""
        process = self._live_process()
        self.thread._received_started = True

        self.thread._forced_stop = True
        self.thread._stop_requested.set()
        self.assertEqual(
            self.thread._build_exit_report(process)["outcome"], "forced_stop"
        )

        self.thread._forced_stop = False
        self.assertEqual(
            self.thread._build_exit_report(process)["outcome"], "user_stopped"
        )

        self.thread._stop_requested.clear()
        self.thread._received_finished = True
        self.thread._child_outcome = "finished"
        process.poll.return_value = 0
        self.assertEqual(
            self.thread._build_exit_report(process)["outcome"], "finished"
        )

        self.thread._received_finished = False
        process.poll.return_value = 1
        self.assertEqual(
            self.thread._build_exit_report(process)["outcome"], "crashed"
        )

    def test_finally_termination_without_stop_request_is_forced_stop(self):
        """未请求停止却终止子进程 → forced_stop（收尾阶段的归类）。

        修复前：收尾阶段只 `terminate()` 而不置 `_forced_stop`，终局落入
        `crashed`；`forced_stop` 分支在任何可达路径上都不产生。
        """
        process = self._live_process()
        self.thread._received_started = True

        self.thread._terminate_live_process(process)

        self.assertTrue(self.thread._forced_stop)
        self.assertTrue(process.terminate.called)
        self.assertEqual(
            self.thread._build_exit_report(process)["outcome"], "forced_stop"
        )

    def test_finally_termination_after_stop_request_is_user_stopped(self):
        """已请求停止后终止子进程仍归为 user_stopped（保留既有语义）。"""
        process = self._live_process()
        self.thread._received_started = True
        # 模拟「已请求停止」状态：request_stop 在未启动的线程上会提前返回。
        self.thread._stop_requested.set()

        self.thread._terminate_live_process(process)

        self.assertFalse(self.thread._forced_stop)
        self.assertEqual(
            self.thread._build_exit_report(process)["outcome"], "user_stopped"
        )

    def test_startup_phase_force_stop_uses_startup_specific_message(self):
        """FR-20261008-905：启动期急停强杀不得输出运行期强停文案。

        启动期路径（``_accept_connection`` 的停止检查）在子进程**尚未连接、
        从未上报任何输入状态**时就会终止子进程；运行期文案的"未在急停时限内
        退出"与"已强制结束并释放其记录的输入状态"两处均与事实不符。运行期
        默认路径的既有文案与终局分类保持不变。
        """
        messages: list[str] = []
        self.thread.log_signal.connect(messages.append)
        process = self._live_process()
        self.thread._received_started = True

        # 启动期路径（reason="startup"）：应输出启动期专用文案。
        self.thread._terminate_live_process(process, reason="startup")
        self.assertTrue(
            any("启动期间" in m for m in messages),
            f"启动期路径应输出启动期专用文案，实际：{messages!r}",
        )
        self.assertFalse(
            any("未在急停时限内退出" in m for m in messages),
            "启动期文案不得声称『未在急停时限内退出/释放输入』",
        )

        # 运行期默认路径：既有文案保持不变。
        messages.clear()
        self.thread._terminate_live_process(self._live_process())
        self.assertTrue(any("未在急停时限内退出" in m for m in messages))


    def test_finally_termination_when_child_dies_during_probe_stays_crashed(self):
        """探测窗口内子进程自行退出（外部强杀/崩溃）→ 保持 crashed。

        回归防护：外部强杀会让管道先断开，而 `process.poll()` 在收尾瞬间仍可能
        返回 None；若仅凭这一点判定，真实崩溃会被误报为 forced_stop
        （REVIEW-20260930-002 实施期实测到该回归）。
        """
        state = {"alive": True}
        process = mock.Mock()
        process.poll.side_effect = lambda: None if state["alive"] else 1

        def _reaped(timeout=None):
            # 探测窗口内被回收：模拟外部强杀后的进程退出。
            state["alive"] = False

        process.wait.side_effect = _reaped
        self.thread._received_started = True

        self.thread._terminate_live_process(process)

        self.assertFalse(self.thread._forced_stop)
        self.assertEqual(
            self.thread._build_exit_report(process)["outcome"], "crashed"
        )

    def test_finally_termination_after_observed_exit_stays_crashed(self):
        """父进程已观察到子进程退出时，收尾终止不得改写为 forced_stop。"""
        process = self._live_process()
        self.thread._received_started = True
        self.thread._observed_child_exit = True

        self.thread._terminate_live_process(process)

        self.assertFalse(self.thread._forced_stop)
        process.poll.return_value = 1
        self.assertEqual(
            self.thread._build_exit_report(process)["outcome"], "crashed"
        )

    def test_startup_phase_termination_stays_startup_failed(self):
        """FR-20261003-201：启动期终止存活子进程必须归为 startup_failed。

        启动期失败（ready 超时、握手超时、能力不匹配、连接/accept 失败）同样会
        走到 `finally` 的终止分支。修复前置位条件不含 `_received_started`，使
        `_build_exit_report` 既有的 `startup_failed` 分支（`not _received_started`）
        被 `_forced_stop` 遮蔽，UI 把「启动失败」误报为「强制停止」。
        """
        process = self._live_process()
        self.thread._received_started = False

        self.thread._terminate_live_process(process)

        self.assertFalse(self.thread._forced_stop)
        self.assertTrue(process.terminate.called)
        report = self.thread._build_exit_report(process)
        self.assertEqual(report["outcome"], "startup_failed")
        self.assertFalse(report["forced"])
        self.assertFalse(report["started"])

    def test_outcome_matrix_over_started_stop_and_observed_exit(self):
        """FR-20261003-201：收尾终止归类的三轴不变量矩阵。

        覆盖 `_received_started` × `_stop_requested` × `_observed_child_exit`
        的全部组合，同时守护两个方向：
        * 启动期终止 → `startup_failed`（不得被 `forced_stop` 遮蔽）；
        * 运行期未请求停止的终止 → `forced_stop`（`FR-20260930-015` 恢复的可达性
          不得被本次修复撤回）。

        期望值取自 `_build_exit_report` 既有阶梯（本次修复不改动阶梯顺序）：
        已请求停止恒为 `user_stopped`；未收到 `started` 时按 `startup_failed`
        归类（即使子进程已退出，阶梯也先命中 `not _received_started`）。
        """
        cases = [
            # (received_started, stop_requested, observed_exit, expected_outcome)
            (False, False, False, "startup_failed"),
            (False, True, False, "user_stopped"),
            (False, False, True, "startup_failed"),
            (False, True, True, "user_stopped"),
            (True, False, False, "forced_stop"),
            (True, True, False, "user_stopped"),
            (True, False, True, "crashed"),
            (True, True, True, "user_stopped"),
        ]
        for started, stop_requested, observed, expected in cases:
            with self.subTest(
                started=started,
                stop_requested=stop_requested,
                observed=observed,
            ):
                process = self._live_process()
                # 子用例共用同一 thread 实例，必须显式复位三轴状态，否则前一
                # 组合的置位会污染后一组合（红检阶段实测到该泄漏）。
                self.thread._received_started = started
                self.thread._observed_child_exit = observed
                self.thread._forced_stop = False
                if stop_requested:
                    self.thread._stop_requested.set()
                else:
                    self.thread._stop_requested.clear()
                if observed:
                    process.poll.return_value = 1

                self.thread._terminate_live_process(process)
                report = self.thread._build_exit_report(process)
                self.assertEqual(report["outcome"], expected)
                self.assertEqual(report["forced"], expected == "forced_stop")


if __name__ == "__main__":
    unittest.main()


class WorkerConstructionFailureReleasesRunSlotTests(unittest.TestCase):
    """FR-RISK-20261005-301: 构造 WorkerThread 失败必须释放已 reserve 的运行槽位。

    ``InProcessRunnerSession.__init__`` 先 ``reserve_run()`` 再构造线程；若构造
    抛异常，会话对象不会返回给调用方，``run_controller`` 的 ``except`` 分支因
    ``session is None`` 无法调用 ``start_failed``。修复前 run 槽位永久占用，用户
    再也无法启动任何脚本（违反 ARCHITECTURE.md #2）。
    """

    def test_worker_construction_failure_releases_the_reserved_run(self):
        from fukua_rpa.runner import backend as backend_module
        from fukua_rpa.runner.backend import InProcessRunnerSession

        engine = mock.Mock()
        engine.reserve_run.return_value = "run-1"
        with (
            mock.patch.object(
                backend_module, "WorkerThread", side_effect=RuntimeError("boom")
            ),
            self.assertRaises(RuntimeError),
        ):
            InProcessRunnerSession(engine, [])
        engine.finish_run.assert_called_once()
        args = engine.finish_run.call_args[0]
        self.assertEqual(args[0], "run-1")
        self.assertEqual(args[1], "start_failed")

    def test_reserve_run_failure_does_not_call_finish_run(self):
        from fukua_rpa.runner.backend import InProcessRunnerSession, RunnerBackendError

        engine = mock.Mock()
        engine.reserve_run.return_value = None
        with self.assertRaises(RunnerBackendError):
            InProcessRunnerSession(engine, [])
        engine.finish_run.assert_not_called()

    def test_successful_construction_does_not_release_the_slot(self):
        from fukua_rpa.runner import backend as backend_module
        from fukua_rpa.runner.backend import InProcessRunnerSession

        engine = mock.Mock()
        engine.reserve_run.return_value = "run-2"
        with mock.patch.object(backend_module, "WorkerThread") as fake_worker:
            session = InProcessRunnerSession(engine, [])
        self.assertEqual(session.run_id, "run-2")
        engine.finish_run.assert_not_called()
        fake_worker.assert_called_once()


if __name__ == "__main__":
    unittest.main()
