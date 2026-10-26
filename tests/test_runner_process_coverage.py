"""Targeted coverage tests for fukua_rpa.runner.process.

Covers: _PromptRegistry, _RunnerHealthState, _heartbeat_loop,
_normalize_region_list, _json_safe, _json_safe_run_trace,
_native_runtime_summary, _runner_runtime_summary, _configure_child_logging,
_control_loop, run_isolated_runner, and main.
"""

from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fukua_rpa.runner.process import (
    _configure_child_logging,
    _control_loop,
    _heartbeat_loop,
    _json_safe,
    _native_runtime_summary,
    _normalize_region_list,
    _PromptRegistry,
    _runner_runtime_summary,
    _RunnerHealthState,
    main,
    run_isolated_runner,
)

try:
    # FR-SCOPE004-001: 修复后新增的运行轨迹净化入口。用守卫导入而非直接导入，
    # 是为了让「入口缺失」在修复前表现为干净的断言失败，而不是整个测试模块的
    # 收集错误（否则红侧无法给出可归因的失败证据）。
    from fukua_rpa.runner.process import _json_safe_run_trace
except ImportError:  # pragma: no cover - 仅修复前走到
    _json_safe_run_trace = None

from fukua_rpa.runtime_trace import DEFAULT_TRACE_LIMIT, RunTrace

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeChannel:
    """Lightweight stand-in for RunnerConnection used in unit tests."""

    def __init__(self):
        self.sent: list[tuple[str, object]] = []
        self._receive_queue: list[dict] = []
        self._poll_results: list[bool] = []
        self.closed = False
        self.send_error: Exception | None = None

    def send(self, kind: str, payload=None):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((kind, payload))

    def receive(self):
        if self._receive_queue:
            return self._receive_queue.pop(0)
        raise EOFError("no more messages")

    def poll(self, timeout=0.0):
        if self._poll_results:
            return self._poll_results.pop(0)
        return bool(self._receive_queue)

    def close(self):
        self.closed = True


class FakeRequest:
    """Mimics a script prompt request with acknowledge/cancel."""

    def __init__(self, text="confirm?"):
        self.text = text
        self.acknowledged = False
        self.cancelled = False

    def acknowledge(self):
        self.acknowledged = True

    def cancel(self):
        self.cancelled = True


class FakeEngine:
    """Minimal engine mock for process-level tests."""

    def __init__(self):
        self.is_running = False
        self.stop_requested = False
        self.stopped = False
        self.native_core = None
        self.use_native_core = False
        self.opencv_available = True
        self.opencv_threads = 4
        self.debug_session = mock.MagicMock()
        self.scale_memory_store = mock.MagicMock()
        self.lifecycle = mock.MagicMock()
        self.lifecycle.snapshot.return_value = mock.MagicMock(
            last_outcome="finished", last_error=""
        )
        self.last_performance_report = {}
        self.last_run_trace = {}
        self.run_log_context = {}
        self.callback_input_state = None

    def stop(self):
        self.stopped = True
        self.stop_requested = True

    def release_injected_inputs(self):
        pass

    def reserve_run(self):
        return 1

    def apply_scan_region(self, **kwargs):
        pass

    def run_tasks(self, *args, **kwargs):
        pass


# ---------------------------------------------------------------------------
# _PromptRegistry
# ---------------------------------------------------------------------------


class TestPromptRegistry(unittest.TestCase):
    def test_publish_sends_prompt_and_registers(self):
        channel = FakeChannel()
        registry = _PromptRegistry(channel)
        request = FakeRequest("hello?")
        registry.publish(request)
        self.assertEqual(len(channel.sent), 1)
        kind, payload = channel.sent[0]
        self.assertEqual(kind, "prompt")
        self.assertIn("id", payload)
        self.assertEqual(payload["text"], "hello?")

    def test_resolve_accepted(self):
        channel = FakeChannel()
        registry = _PromptRegistry(channel)
        request = FakeRequest()
        registry.publish(request)
        prompt_id = channel.sent[0][1]["id"]
        registry.resolve(prompt_id, accepted=True)
        self.assertTrue(request.acknowledged)
        self.assertFalse(request.cancelled)

    def test_resolve_rejected(self):
        channel = FakeChannel()
        registry = _PromptRegistry(channel)
        request = FakeRequest()
        registry.publish(request)
        prompt_id = channel.sent[0][1]["id"]
        registry.resolve(prompt_id, accepted=False)
        self.assertTrue(request.cancelled)
        self.assertFalse(request.acknowledged)

    def test_resolve_unknown_id_is_noop(self):
        channel = FakeChannel()
        registry = _PromptRegistry(channel)
        sent_before = len(channel.sent)
        # Should not raise
        registry.resolve("nonexistent-id", accepted=True)
        # FR-20260809-1405: 负向测试补断言——未知 ID 不产生副作用。
        self.assertEqual(registry._requests, {})
        self.assertEqual(len(channel.sent), sent_before)

    def test_cancel_all(self):
        channel = FakeChannel()
        registry = _PromptRegistry(channel)
        r1 = FakeRequest("a")
        r2 = FakeRequest("b")
        registry.publish(r1)
        registry.publish(r2)
        registry.cancel_all()
        self.assertTrue(r1.cancelled)
        self.assertTrue(r2.cancelled)

    def test_cancel_all_suppresses_exception(self):
        channel = FakeChannel()
        registry = _PromptRegistry(channel)

        class BadRequest:
            text = "x"

            def cancel(self):
                raise RuntimeError("boom")

        bad = BadRequest()
        registry.publish(bad)
        self.assertEqual(len(registry._requests), 1)  # 前置：已登记
        # Should not raise
        registry.cancel_all()
        # FR-20260809-1405: 负向测试补断言——异常被吞但清理完成。
        self.assertEqual(registry._requests, {})


# ---------------------------------------------------------------------------
# _RunnerHealthState
# ---------------------------------------------------------------------------


class TestRunnerHealthState(unittest.TestCase):
    def test_initial_snapshot(self):
        state = _RunnerHealthState()
        snap = state.snapshot()
        self.assertEqual(snap["phase"], "connected")
        self.assertFalse(snap["engine_running"])
        self.assertFalse(snap["stop_requested"])
        self.assertEqual(snap["pid"], os.getpid())

    def test_update_phase_and_engine(self):
        state = _RunnerHealthState()
        engine = FakeEngine()
        engine.is_running = True
        state.update(phase="running", engine=engine)
        snap = state.snapshot()
        self.assertEqual(snap["phase"], "running")
        self.assertTrue(snap["engine_running"])

    def test_update_partial(self):
        state = _RunnerHealthState()
        state.update(phase="waiting_start")
        snap = state.snapshot()
        self.assertEqual(snap["phase"], "waiting_start")
        self.assertFalse(snap["engine_running"])

    def test_stop_engine_calls_engine_stop(self):
        state = _RunnerHealthState()
        engine = FakeEngine()
        state.update(engine=engine)
        state.stop_engine()
        self.assertTrue(engine.stopped)

    def test_stop_engine_no_engine(self):
        state = _RunnerHealthState()
        # Should not raise
        state.stop_engine()
        # FR-20260809-1405: 负向测试补断言——无引擎时快照保持可用且不报告运行。
        snap = state.snapshot()
        self.assertFalse(snap["engine_running"])
        self.assertEqual(snap["phase"], "connected")

    def test_stop_engine_suppresses_exception(self):
        state = _RunnerHealthState()
        engine = mock.MagicMock()
        engine.stop.side_effect = RuntimeError("fail")
        engine.is_running = False
        engine.stop_requested = True
        state.update(engine=engine)
        # Should not raise
        state.stop_engine()
        # FR-20260809-1405: 负向测试补断言——异常被吞但 stop 确实被请求。
        engine.stop.assert_called_once_with()
        snap = state.snapshot()
        self.assertTrue(snap["stop_requested"])


# ---------------------------------------------------------------------------
# _heartbeat_loop
# ---------------------------------------------------------------------------


class TestHeartbeatLoop(unittest.TestCase):
    def test_sends_heartbeat_until_stop(self):
        channel = FakeChannel()
        state = _RunnerHealthState()
        stop_event = threading.Event()

        # Let it send 2 heartbeats then stop
        call_count = [0]
        original_send = channel.send

        def counting_send(kind, payload=None):
            original_send(kind, payload)
            call_count[0] += 1
            if call_count[0] >= 2:
                stop_event.set()

        channel.send = counting_send
        _heartbeat_loop(channel, state, stop_event)
        self.assertTrue(stop_event.is_set())
        self.assertGreaterEqual(call_count[0], 2)
        # Verify heartbeat payloads
        heartbeats = [(k, p) for k, p in channel.sent if k == "heartbeat"]
        self.assertGreaterEqual(len(heartbeats), 2)
        self.assertEqual(heartbeats[0][1]["sequence"], 1)
        self.assertEqual(heartbeats[1][1]["sequence"], 2)

    def test_ipc_failure_stops_engine(self):
        channel = FakeChannel()
        channel.send_error = OSError("pipe broken")
        state = _RunnerHealthState()
        engine = FakeEngine()
        state.update(engine=engine)
        stop_event = threading.Event()
        _heartbeat_loop(channel, state, stop_event)
        self.assertTrue(stop_event.is_set())
        self.assertTrue(engine.stopped)

    def test_immediate_stop_event(self):
        channel = FakeChannel()
        state = _RunnerHealthState()
        stop_event = threading.Event()
        stop_event.set()
        _heartbeat_loop(channel, state, stop_event)
        # No heartbeats sent
        self.assertEqual(len(channel.sent), 0)


# ---------------------------------------------------------------------------
# _normalize_region_list
# ---------------------------------------------------------------------------


class TestNormalizeRegionList(unittest.TestCase):
    def test_valid_regions(self):
        result = _normalize_region_list([(10, 20, 100, 200), [0, 0, 50, 50]])
        self.assertEqual(result, [(10, 20, 100, 200), (0, 0, 50, 50)])

    def test_float_values_truncated(self):
        result = _normalize_region_list([(1.9, 2.1, 10.5, 20.7)])
        self.assertEqual(result, [(1, 2, 10, 20)])

    def test_invalid_entries_skipped(self):
        result = _normalize_region_list(["bad", (1, 2), None, (0, 0, 10, 10)])
        self.assertEqual(result, [(0, 0, 10, 10)])

    def test_zero_or_negative_dimensions_skipped(self):
        result = _normalize_region_list([(0, 0, 0, 10), (0, 0, 10, -1), (0, 0, 5, 5)])
        self.assertEqual(result, [(0, 0, 5, 5)])

    def test_none_input(self):
        result = _normalize_region_list(None)
        self.assertEqual(result, [])

    def test_empty_list(self):
        result = _normalize_region_list([])
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# _json_safe
# ---------------------------------------------------------------------------


class TestJsonSafe(unittest.TestCase):
    def test_primitives_passthrough(self):
        self.assertIsNone(_json_safe(None))
        self.assertEqual(_json_safe("hello"), "hello")
        self.assertEqual(_json_safe(True), True)
        self.assertEqual(_json_safe(42), 42)

    def test_finite_float(self):
        self.assertEqual(_json_safe(3.14), 3.14)

    def test_non_finite_float_stringified(self):
        self.assertEqual(_json_safe(float("inf")), "inf")
        self.assertEqual(_json_safe(float("nan")), "nan")

    def test_mapping(self):
        result = _json_safe({"a": 1, "b": [2, 3]})
        self.assertEqual(result, {"a": 1, "b": [2, 3]})

    def test_list_tuple_set(self):
        self.assertEqual(_json_safe([1, 2]), [1, 2])
        self.assertEqual(_json_safe((1, 2)), [1, 2])
        self.assertEqual(sorted(_json_safe({1, 2})), [1, 2])

    def test_depth_limit(self):
        # Build nesting deeper than _JSON_SAFE_MAX_DEPTH (24)
        value = "leaf"
        for _ in range(30):
            value = [value]
        result = _json_safe(value)
        # Walk down to find the depth marker
        current = result
        depth = 0
        while isinstance(current, list) and current:
            current = current[0]
            depth += 1
        self.assertIn("嵌套过深", str(current))

    def test_node_count_limit(self):
        # Create a list exceeding 4096 nodes
        big = list(range(5000))
        result = _json_safe(big)
        # Should be truncated at some point
        self.assertIn("truncated", str(result[-1]) if isinstance(result, list) else str(result))

    def test_unknown_type_repr(self):
        class Custom:
            def __repr__(self):
                return "Custom()"

        result = _json_safe(Custom())
        self.assertEqual(result, "Custom()")

    def test_repr_truncated_at_limit(self):
        class LongRepr:
            def __repr__(self):
                return "x" * 2000

        result = _json_safe(LongRepr())
        self.assertEqual(len(result), 1000)


# ---------------------------------------------------------------------------
# _json_safe_run_trace (FR-SCOPE004-001)
# ---------------------------------------------------------------------------


def _marker_count(value) -> int:
    """Count truncation markers inside a sanitized subtree."""
    if isinstance(value, str):
        return 1 if value in ("...(truncated)", "<嵌套过深>") else 0
    if isinstance(value, dict):
        return sum(_marker_count(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return sum(_marker_count(item) for item in value)
    return 0


def _node_count(value) -> int:
    total = 1
    if isinstance(value, dict):
        for item in value.values():
            total += _node_count(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            total += _node_count(item)
    return total


def _build_trace(count: int) -> dict:
    """Build a realistic RunTrace snapshot holding `count` recorded events."""
    trace = RunTrace(max_events=DEFAULT_TRACE_LIMIT)
    trace.reset(task_count=1)
    for index in range(count):
        trace.record(
            "step_start",
            loop=1,
            step=index,
            command="左键单击",
            status="ok",
            duration_ms=12.5,
            attempt=1,
            next_step=index + 1,
            entry_reason="sequential",
        )
    return trace.snapshot()


class TestJsonSafeRunTrace(unittest.TestCase):
    """FR-SCOPE004-001: IPC 净化截断必须给出显式、可聚合的丢失计数。

    共享的 4096 节点预算耗尽后，`_json_safe` 把值换成可见哨兵却**保留容器
    长度**，因此只看 `len(events)` 会以为事件全都在。以下用例锁定修复后的
    契约，同时锁定不得被破坏的不变量（有界性、无误报、语义不混同）。
    """

    def setUp(self):
        if _json_safe_run_trace is None:
            self.fail(
                "FR-SCOPE004-001: 运行轨迹净化缺少显式截断计数入口 _json_safe_run_trace"
            )

    def test_truncated_trace_reports_explicit_dropped_count(self):
        sanitized = _json_safe_run_trace(_build_trace(2000))
        events = sanitized["events"]
        # 容器长度被保留 —— 这正是「不可聚合」的根源。
        self.assertEqual(len(events), 2000)
        damaged = sum(1 for item in events if _marker_count(item) > 0)
        self.assertGreater(damaged, 0)
        self.assertEqual(sanitized["ipc_dropped_events"], damaged)

    def test_payload_remains_bounded(self):
        small = _node_count(_json_safe_run_trace(_build_trace(1000)))
        large = _node_count(_json_safe_run_trace(_build_trace(2000)))
        # 有界性不得被移除：节点数不得随输入无界增长。
        self.assertLessEqual(large, small * 2)

    def test_untruncated_trace_is_unchanged(self):
        sanitized = _json_safe_run_trace(_build_trace(5))
        self.assertEqual(_marker_count(sanitized), 0)
        self.assertEqual(len(sanitized["events"]), 5)
        # 无截断时不得新增字段，普通运行的载荷形态保持不变。
        self.assertNotIn("ipc_dropped_events", sanitized)

    def test_trace_own_dropped_events_semantics_unchanged(self):
        sanitized = _json_safe_run_trace(_build_trace(2000))
        # 轨迹自身的 dropped_events 只统计「从未记录」的事件，与 IPC 层丢失
        # 是两件事，不得被混同或改写。
        self.assertEqual(sanitized["dropped_events"], 0)
        self.assertGreater(sanitized["ipc_dropped_events"], 0)

    def test_non_mapping_input_is_passed_through(self):
        # 载荷形态不满足预期时不得抛出，也不得伪造计数。
        self.assertIsNone(_json_safe_run_trace(None))
        self.assertEqual(_json_safe_run_trace([1, 2]), [1, 2])


# ---------------------------------------------------------------------------
# _native_runtime_summary
# ---------------------------------------------------------------------------


class TestNativeRuntimeSummary(unittest.TestCase):
    def test_no_native_core(self):
        engine = FakeEngine()
        engine.native_core = None
        engine.use_native_core = True
        engine.opencv_available = True
        result = _native_runtime_summary(engine)
        self.assertEqual(result["service"], "vision")
        self.assertFalse(result["available"])
        self.assertTrue(result["requested"])
        self.assertEqual(result["fallback"]["name"], "opencv")
        self.assertTrue(result["fallback"]["available"])

    def test_with_native_core(self):
        engine = FakeEngine()
        core = mock.MagicMock()
        core.service_snapshot.return_value = {"service": "vision", "available": True}
        engine.native_core = core
        engine.use_native_core = True
        engine.opencv_available = False
        result = _native_runtime_summary(engine)
        core.service_snapshot.assert_called_once_with(
            requested=True, fallback_available=False
        )
        self.assertEqual(result["available"], True)


# ---------------------------------------------------------------------------
# _runner_runtime_summary
# ---------------------------------------------------------------------------


class TestRunnerRuntimeSummary(unittest.TestCase):
    @mock.patch("fukua_rpa.runner.process.build_runtime_service_catalog")
    def test_summary_structure(self, mock_catalog):
        mock_catalog.return_value = {"catalog_version": 1, "services": []}
        engine = FakeEngine()
        result = _runner_runtime_summary(engine)
        self.assertEqual(result["pid"], os.getpid())
        self.assertEqual(len(result["python_version"]), 3)
        self.assertIn(result["pointer_bits"], (32, 64))
        self.assertIsInstance(result["frozen"], bool)
        self.assertTrue(result["opencv"]["available"])
        self.assertEqual(result["opencv"]["threads"], 4)
        self.assertIn("native", result)
        self.assertEqual(result["services"], {"catalog_version": 1, "services": []})


# ---------------------------------------------------------------------------
# _configure_child_logging
# ---------------------------------------------------------------------------


class TestConfigureChildLogging(unittest.TestCase):
    @mock.patch("fukua_rpa.runner.process.set_log_base_dir")
    @mock.patch("fukua_rpa.runner.process.update_log_config")
    def test_with_mapping_payload(self, mock_update, mock_set_dir):
        payload = {"log_to_file": True, "log_to_ui": False, "privacy_protected": False}
        _configure_child_logging(payload, "/tmp/logs")
        mock_update.assert_called_once_with(
            log_to_file=True, log_to_ui=False, privacy_protected=False
        )
        mock_set_dir.assert_called_once_with("/tmp/logs")

    @mock.patch("fukua_rpa.runner.process.set_log_base_dir")
    @mock.patch("fukua_rpa.runner.process.update_log_config")
    def test_with_non_mapping_payload(self, mock_update, mock_set_dir):
        _configure_child_logging("not-a-dict", "/base")
        mock_update.assert_called_once_with(
            log_to_file=False, log_to_ui=True, privacy_protected=True
        )
        mock_set_dir.assert_called_once_with("/base")


# ---------------------------------------------------------------------------
# _control_loop
# ---------------------------------------------------------------------------


class TestControlLoop(unittest.TestCase):
    def _make_channel_with_messages(self, messages):
        channel = FakeChannel()
        channel._receive_queue = list(messages)
        channel._poll_results = [True] * len(messages) + [False] * 100
        return channel

    def test_stop_command(self):
        engine = FakeEngine()
        channel = self._make_channel_with_messages(
            [{"kind": "stop", "payload": {}}]
        )
        prompts = _PromptRegistry(FakeChannel())
        stop_event = threading.Event()
        # After stop, the loop should continue polling; set stop_event via side effect
        original_stop = engine.stop

        def stop_and_set():
            original_stop()
            stop_event.set()

        engine.stop = stop_and_set
        _control_loop(channel, engine, prompts, stop_event)
        self.assertTrue(engine.stopped)

    def test_shutdown_command_sets_stop_event(self):
        engine = FakeEngine()
        channel = self._make_channel_with_messages(
            [{"kind": "shutdown", "payload": {}}]
        )
        prompts = _PromptRegistry(FakeChannel())
        stop_event = threading.Event()
        _control_loop(channel, engine, prompts, stop_event)
        self.assertTrue(stop_event.is_set())
        self.assertTrue(engine.stopped)

    def test_pause_command(self):
        engine = FakeEngine()
        channel = self._make_channel_with_messages(
            [{"kind": "pause", "payload": {}}]
        )
        prompts = _PromptRegistry(FakeChannel())
        stop_event = threading.Event()
        pause_called = []
        original_pause = engine.debug_session.request_pause

        def pause_and_stop():
            original_pause()
            pause_called.append(True)
            stop_event.set()

        engine.debug_session.request_pause = pause_and_stop
        _control_loop(channel, engine, prompts, stop_event)
        self.assertTrue(pause_called)

    def test_continue_command(self):
        engine = FakeEngine()
        channel = self._make_channel_with_messages(
            [{"kind": "continue", "payload": {}}]
        )
        prompts = _PromptRegistry(FakeChannel())
        stop_event = threading.Event()

        def continue_and_stop():
            stop_event.set()

        engine.debug_session.continue_run = continue_and_stop
        _control_loop(channel, engine, prompts, stop_event)
        self.assertTrue(stop_event.is_set())

    def test_step_over_command(self):
        engine = FakeEngine()
        channel = self._make_channel_with_messages(
            [{"kind": "step_over", "payload": {}}]
        )
        prompts = _PromptRegistry(FakeChannel())
        stop_event = threading.Event()

        def step_and_stop():
            stop_event.set()

        engine.debug_session.step_over = step_and_stop
        _control_loop(channel, engine, prompts, stop_event)
        self.assertTrue(stop_event.is_set())

    def test_prompt_response_command(self):
        engine = FakeEngine()
        prompt_channel = FakeChannel()
        prompts = _PromptRegistry(prompt_channel)
        request = FakeRequest()
        prompts.publish(request)
        prompt_id = prompt_channel.sent[0][1]["id"]

        channel = self._make_channel_with_messages(
            [{"kind": "prompt_response", "payload": {"id": prompt_id, "accepted": True}}]
        )
        stop_event = threading.Event()

        # Stop after processing
        original_resolve = prompts.resolve

        def resolve_and_stop(pid, accepted):
            original_resolve(pid, accepted)
            stop_event.set()

        prompts.resolve = resolve_and_stop
        _control_loop(channel, engine, prompts, stop_event)
        self.assertTrue(request.acknowledged)

    def test_unknown_command_raises_protocol_error_and_stops(self):
        engine = FakeEngine()
        channel = self._make_channel_with_messages(
            [{"kind": "bogus_command", "payload": {}}]
        )
        prompts = _PromptRegistry(FakeChannel())
        stop_event = threading.Event()
        _control_loop(channel, engine, prompts, stop_event)
        # The protocol error triggers the except branch
        self.assertTrue(stop_event.is_set())
        self.assertTrue(engine.stopped)
        # An error message should have been sent
        error_msgs = [(k, p) for k, p in channel.sent if k == "error"]
        self.assertGreaterEqual(len(error_msgs), 1)

    def test_eof_error_stops_loop(self):
        engine = FakeEngine()
        channel = FakeChannel()
        channel._poll_results = [True]
        # receive will raise EOFError because queue is empty
        prompts = _PromptRegistry(FakeChannel())
        stop_event = threading.Event()
        _control_loop(channel, engine, prompts, stop_event)
        self.assertTrue(stop_event.is_set())
        self.assertTrue(engine.stopped)


# ---------------------------------------------------------------------------
# run_isolated_runner
# ---------------------------------------------------------------------------


def _make_valid_pipe_name():
    return r"\\.\pipe\fukuaRPA-runner-1234-" + "a" * 32


def _make_valid_auth_hex():
    return "ab" * 16  # 32 hex chars


class TestRunIsolatedRunner(unittest.TestCase):
    def test_invalid_auth_empty(self):
        result = run_isolated_runner(_make_valid_pipe_name(), "")
        self.assertEqual(result, 2)

    def test_invalid_auth_odd_length(self):
        result = run_isolated_runner(_make_valid_pipe_name(), "abc")
        self.assertEqual(result, 2)

    def test_invalid_auth_non_hex(self):
        result = run_isolated_runner(_make_valid_pipe_name(), "zz" * 16)
        self.assertEqual(result, 2)

    @mock.patch("fukua_rpa.runner.process.Client")
    def test_start_timeout(self, mock_client_cls):
        """If no start message arrives before deadline, return 1."""
        mock_conn = mock.MagicMock()
        mock_client_cls.return_value = mock_conn

        with mock.patch("fukua_rpa.runner.process._START_TIMEOUT_SECONDS", 0.01), mock.patch(
            "fukua_rpa.runner.process.RunnerConnection"
        ) as mock_rc_cls:
            mock_channel = FakeChannel()
            # poll always returns False -> timeout path
            mock_channel._poll_results = []
            mock_channel._receive_queue = []
            # Override poll to always return False
            mock_channel.poll = lambda timeout=0: False
            mock_rc_cls.return_value = mock_channel
            result = run_isolated_runner(
                _make_valid_pipe_name(), _make_valid_auth_hex()
            )
        self.assertEqual(result, 1)
        # Should have sent hello then error
        kinds = [k for k, _ in mock_channel.sent]
        self.assertIn("hello", kinds)
        self.assertIn("error", kinds)

    @mock.patch("fukua_rpa.runner.process.Client")
    def test_eof_during_start_poll_returns_zero(self, mock_client_cls):
        """EOFError during start poll returns 0 (parent closed)."""
        mock_conn = mock.MagicMock()
        mock_client_cls.return_value = mock_conn

        with mock.patch(
            "fukua_rpa.runner.process.RunnerConnection"
        ) as mock_rc_cls:
            mock_channel = FakeChannel()
            # First poll returns True, then receive raises EOFError
            mock_channel.poll = lambda timeout=0: True
            mock_channel.receive = mock.MagicMock(side_effect=EOFError)
            mock_rc_cls.return_value = mock_channel
            result = run_isolated_runner(
                _make_valid_pipe_name(), _make_valid_auth_hex()
            )
        self.assertEqual(result, 0)

    @mock.patch("fukua_rpa.runner.process.Client")
    def test_first_message_not_start_raises(self, mock_client_cls):
        """If first message is not 'start', protocol error -> return 1."""
        mock_conn = mock.MagicMock()
        mock_client_cls.return_value = mock_conn

        with mock.patch(
            "fukua_rpa.runner.process.RunnerConnection"
        ) as mock_rc_cls:
            mock_channel = FakeChannel()
            mock_channel.poll = lambda timeout=0: True
            mock_channel.receive = lambda: {"kind": "bogus", "payload": {}}
            mock_rc_cls.return_value = mock_channel
            result = run_isolated_runner(
                _make_valid_pipe_name(), _make_valid_auth_hex()
            )
        self.assertEqual(result, 1)

    @mock.patch("fukua_rpa.runner.process.Client")
    def test_invalid_start_payload_format(self, mock_client_cls):
        """tasks not a list or config not a mapping -> return 1."""
        mock_conn = mock.MagicMock()
        mock_client_cls.return_value = mock_conn

        with mock.patch(
            "fukua_rpa.runner.process.RunnerConnection"
        ) as mock_rc_cls:
            mock_channel = FakeChannel()
            mock_channel.poll = lambda timeout=0: True
            mock_channel.receive = lambda: {
                "kind": "start",
                "payload": {"tasks": "not-a-list", "config": {}},
            }
            mock_rc_cls.return_value = mock_channel
            result = run_isolated_runner(
                _make_valid_pipe_name(), _make_valid_auth_hex()
            )
        self.assertEqual(result, 1)

    @mock.patch("fukua_rpa.runner.process.unregister_processed_log_sink")
    @mock.patch("fukua_rpa.runner.process.register_processed_log_sink")
    @mock.patch("fukua_rpa.runner.process.flush_logs")
    @mock.patch("fukua_rpa.runner.process.write_log")
    @mock.patch("fukua_rpa.runner.process.FailsafeWatchdog")
    @mock.patch("fukua_rpa.runner.process.RPAEngine")
    @mock.patch("fukua_rpa.runner.process.RunRequest")
    @mock.patch("fukua_rpa.runner.process._configure_child_logging")
    @mock.patch("fukua_rpa.runner.process.build_runtime_service_catalog")
    @mock.patch("fukua_rpa.runner.process.Client")
    def test_full_successful_run(
        self,
        mock_client_cls,
        mock_catalog,
        mock_log_cfg,
        mock_run_request_cls,
        mock_engine_cls,
        mock_watchdog_cls,
        mock_write_log,
        mock_flush,
        mock_reg_sink,
        mock_unreg_sink,
    ):
        """Full happy-path: start -> run -> finished -> return 0."""
        mock_catalog.return_value = {"catalog_version": 1, "services": []}
        mock_conn = mock.MagicMock()
        mock_client_cls.return_value = mock_conn

        engine = FakeEngine()
        mock_engine_cls.return_value = engine

        mock_request = mock.MagicMock()
        mock_request.mutable_tasks.return_value = []
        mock_request.config = mock.MagicMock()
        mock_run_request_cls.create.return_value = mock_request

        mock_watchdog = mock.MagicMock()
        mock_watchdog_cls.return_value = mock_watchdog

        with mock.patch(
            "fukua_rpa.runner.process.RunnerConnection"
        ) as mock_rc_cls:
            mock_channel = FakeChannel()
            poll_count = [0]

            def fake_poll(timeout=0):
                poll_count[0] += 1
                return poll_count[0] == 1

            mock_channel.poll = fake_poll
            mock_channel.receive = lambda: {
                "kind": "start",
                "payload": {
                    "tasks": [{"name": "task1"}],
                    "config": {"scan_regions": [[0, 0, 100, 100]]},
                    "base_dir": "/tmp",
                    "profile_name": "test",
                    "logging": {"log_to_file": False},
                    "log_context": {"key": "value"},
                    "scale_memory": {},
                },
            }
            mock_rc_cls.return_value = mock_channel
            result = run_isolated_runner(
                _make_valid_pipe_name(), _make_valid_auth_hex()
            )

        self.assertEqual(result, 0)
        kinds = [k for k, _ in mock_channel.sent]
        self.assertIn("hello", kinds)
        self.assertIn("started", kinds)
        self.assertIn("runtime_state", kinds)
        self.assertIn("finished", kinds)
        # Engine cleanup
        self.assertTrue(engine.stopped)
        mock_watchdog.kill.assert_called_once()
        self.assertTrue(mock_channel.closed)

    @mock.patch("fukua_rpa.runner.process.unregister_processed_log_sink")
    @mock.patch("fukua_rpa.runner.process.register_processed_log_sink")
    @mock.patch("fukua_rpa.runner.process.flush_logs")
    @mock.patch("fukua_rpa.runner.process.write_log")
    @mock.patch("fukua_rpa.runner.process.FailsafeWatchdog")
    @mock.patch("fukua_rpa.runner.process.RPAEngine")
    @mock.patch("fukua_rpa.runner.process.RunRequest")
    @mock.patch("fukua_rpa.runner.process._configure_child_logging")
    @mock.patch("fukua_rpa.runner.process.build_runtime_service_catalog")
    @mock.patch("fukua_rpa.runner.process.Client")
    def test_runtime_state_trace_reports_ipc_dropped_events(
        self,
        mock_client_cls,
        mock_catalog,
        mock_log_cfg,
        mock_run_request_cls,
        mock_engine_cls,
        mock_watchdog_cls,
        mock_write_log,
        mock_flush,
        mock_reg_sink,
        mock_unreg_sink,
    ):
        """FR-20261008-504：生产发送路径必须走 ``_json_safe_run_trace``。

        该函数是 FR-SCOPE004-001 为「显式、可聚合的丢失计数」新增的入口，但生产
        发送 ``runtime_state`` 时用的仍是通用 ``_json_safe``，于是
        ``ipc_dropped_events`` 在生产运行轨迹里永不出现——只有测试直接调用它才能
        看到，可见性修复落空。
        """
        mock_catalog.return_value = {"catalog_version": 1, "services": []}
        mock_client_cls.return_value = mock.MagicMock()

        engine = FakeEngine()
        engine.last_run_trace = _build_trace(2000)
        mock_engine_cls.return_value = engine

        mock_request = mock.MagicMock()
        mock_request.mutable_tasks.return_value = []
        mock_request.config = mock.MagicMock()
        mock_run_request_cls.create.return_value = mock_request
        mock_watchdog_cls.return_value = mock.MagicMock()

        with mock.patch(
            "fukua_rpa.runner.process.RunnerConnection"
        ) as mock_rc_cls:
            mock_channel = FakeChannel()
            poll_count = [0]

            def fake_poll(timeout=0):
                poll_count[0] += 1
                return poll_count[0] == 1

            mock_channel.poll = fake_poll
            mock_channel.receive = lambda: {
                "kind": "start",
                "payload": {
                    "tasks": [{"name": "task1"}],
                    "config": {},
                    "base_dir": "/tmp",
                    "profile_name": "test",
                    "logging": {"log_to_file": False},
                    "log_context": {},
                    "scale_memory": {},
                },
            }
            mock_rc_cls.return_value = mock_channel
            result = run_isolated_runner(
                _make_valid_pipe_name(), _make_valid_auth_hex()
            )

        self.assertEqual(result, 0)
        state_payloads = [
            payload
            for kind, payload in mock_channel.sent
            if kind == "runtime_state" and isinstance(payload, dict)
        ]
        self.assertTrue(state_payloads, "未发送 runtime_state")
        trace = state_payloads[-1]["run_trace"]
        self.assertIn(
            "ipc_dropped_events",
            trace,
            "生产运行轨迹必须带显式丢失计数（FR-SCOPE004-001）",
        )
        self.assertGreater(trace["ipc_dropped_events"], 0)

    @mock.patch("fukua_rpa.runner.process.unregister_processed_log_sink")
    @mock.patch("fukua_rpa.runner.process.register_processed_log_sink")
    @mock.patch("fukua_rpa.runner.process.flush_logs")
    @mock.patch("fukua_rpa.runner.process.write_log")
    @mock.patch("fukua_rpa.runner.process.FailsafeWatchdog")
    @mock.patch("fukua_rpa.runner.process.RPAEngine")
    @mock.patch("fukua_rpa.runner.process.RunRequest")
    @mock.patch("fukua_rpa.runner.process._configure_child_logging")
    @mock.patch("fukua_rpa.runner.process.build_runtime_service_catalog")
    @mock.patch("fukua_rpa.runner.process.Client")
    def test_engine_busy_returns_error(
        self,
        mock_client_cls,
        mock_catalog,
        mock_log_cfg,
        mock_run_request_cls,
        mock_engine_cls,
        mock_watchdog_cls,
        mock_write_log,
        mock_flush,
        mock_reg_sink,
        mock_unreg_sink,
    ):
        """reserve_run returns None -> RuntimeError -> return 1."""
        mock_catalog.return_value = {"catalog_version": 1, "services": []}
        mock_conn = mock.MagicMock()
        mock_client_cls.return_value = mock_conn

        engine = FakeEngine()
        engine.reserve_run = lambda: None
        mock_engine_cls.return_value = engine

        mock_request = mock.MagicMock()
        mock_request.config = mock.MagicMock()
        mock_run_request_cls.create.return_value = mock_request

        with mock.patch(
            "fukua_rpa.runner.process.RunnerConnection"
        ) as mock_rc_cls:
            mock_channel = FakeChannel()
            mock_channel.poll = lambda timeout=0: True
            mock_channel.receive = lambda: {
                "kind": "start",
                "payload": {
                    "tasks": [{"name": "t"}],
                    "config": {},
                    "base_dir": "/tmp",
                },
            }
            mock_rc_cls.return_value = mock_channel
            result = run_isolated_runner(
                _make_valid_pipe_name(), _make_valid_auth_hex()
            )

        self.assertEqual(result, 1)
        kinds = [k for k, _ in mock_channel.sent]
        self.assertIn("error", kinds)

    @mock.patch("fukua_rpa.runner.process.unregister_processed_log_sink")
    @mock.patch("fukua_rpa.runner.process.register_processed_log_sink")
    @mock.patch("fukua_rpa.runner.process.flush_logs")
    @mock.patch("fukua_rpa.runner.process.write_log")
    @mock.patch("fukua_rpa.runner.process.FailsafeWatchdog")
    @mock.patch("fukua_rpa.runner.process.RPAEngine")
    @mock.patch("fukua_rpa.runner.process.RunRequest")
    @mock.patch("fukua_rpa.runner.process._configure_child_logging")
    @mock.patch("fukua_rpa.runner.process.build_runtime_service_catalog")
    @mock.patch("fukua_rpa.runner.process.Client")
    def test_single_scan_region_fallback(
        self,
        mock_client_cls,
        mock_catalog,
        mock_log_cfg,
        mock_run_request_cls,
        mock_engine_cls,
        mock_watchdog_cls,
        mock_write_log,
        mock_flush,
        mock_reg_sink,
        mock_unreg_sink,
    ):
        """When scan_regions is empty, falls back to single scan_region."""
        mock_catalog.return_value = {"catalog_version": 1, "services": []}
        mock_conn = mock.MagicMock()
        mock_client_cls.return_value = mock_conn

        engine = FakeEngine()
        applied_regions = {}

        def capture_region(**kwargs):
            applied_regions.update(kwargs)

        engine.apply_scan_region = capture_region
        mock_engine_cls.return_value = engine

        mock_request = mock.MagicMock()
        mock_request.mutable_tasks.return_value = []
        mock_request.config = mock.MagicMock()
        mock_run_request_cls.create.return_value = mock_request

        mock_watchdog = mock.MagicMock()
        mock_watchdog_cls.return_value = mock_watchdog

        with mock.patch(
            "fukua_rpa.runner.process.RunnerConnection"
        ) as mock_rc_cls:
            mock_channel = FakeChannel()
            poll_count = [0]

            def fake_poll(timeout=0):
                poll_count[0] += 1
                return poll_count[0] == 1

            mock_channel.poll = fake_poll
            mock_channel.receive = lambda: {
                "kind": "start",
                "payload": {
                    "tasks": [{"name": "t"}],
                    "config": {"scan_region": [10, 20, 300, 400]},
                    "base_dir": "/tmp",
                },
            }
            mock_rc_cls.return_value = mock_channel
            result = run_isolated_runner(
                _make_valid_pipe_name(), _make_valid_auth_hex()
            )

        self.assertEqual(result, 0)
        self.assertEqual(applied_regions.get("scan_region"), (10, 20, 300, 400))

    @mock.patch("fukua_rpa.runner.process.Client")
    def test_control_stop_set_during_start_poll(self, mock_client_cls):
        """If control_stop is set before start arrives, return 0."""
        mock_conn = mock.MagicMock()
        mock_client_cls.return_value = mock_conn

        with mock.patch(
            "fukua_rpa.runner.process.RunnerConnection"
        ) as mock_rc_cls:
            mock_channel = FakeChannel()
            # Simulate: heartbeat thread sets stop before start arrives
            # We patch threading.Event to return a pre-set event for control_stop
            mock_channel.poll = lambda timeout=0: False
            mock_rc_cls.return_value = mock_channel

            # Patch _heartbeat_loop to immediately set the stop event
            def fake_heartbeat(channel, health_state, stop_event):
                stop_event.set()

            with mock.patch(
                "fukua_rpa.runner.process._heartbeat_loop", fake_heartbeat
            ):
                result = run_isolated_runner(
                    _make_valid_pipe_name(), _make_valid_auth_hex()
                )
        self.assertEqual(result, 0)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def test_missing_auth_env_returns_1(self):
        # FR-20260923-003：**不得**改回 mock.patch.dict(os.environ, {}, clear=True/False)。
        # 它在退出时执行 os.environ.clear() + in_dict.update(original)，把宿主环境的**全部**
        # 变量重新写回；Windows 上单个变量超过 32767 字符时 update 抛
        # ``ValueError: the environment variable is longer than 32767 characters``，
        # 还原中途中断，PATH 等键永久丢失，导致其后每个派生子进程的用例级联失败
        # （实测：宿主注入 330,449 字符变量时完整套件 14 failed + 2 errors）。
        # 这里改为替换 os.environ 对象本身：不复制、不重放宿主环境，因此不受环境块长度影响。
        # 断言强度不变：FUKUARPA_RUNNER_AUTH 仍必须不在环境内（替换为全新空 dict）。
        with mock.patch.object(os, "environ", {}):
            os.environ.pop("FUKUARPA_RUNNER_AUTH", None)
            result = main(["--runner-pipe", _make_valid_pipe_name()])
        self.assertEqual(result, 1)

    @mock.patch("fukua_rpa.runner.process.run_isolated_runner")
    def test_valid_args_delegates(self, mock_run):
        mock_run.return_value = 0
        auth = "ab" * 16
        pipe = _make_valid_pipe_name()
        # FR-20260923-003：同 test_missing_auth_env_returns_1 —— 不得用
        # mock.patch.dict 操作真实 os.environ（长环境块下退出时会抛 ValueError 并
        # 遗留缺失 PATH 的环境，级联拖垮其后派生子进程的用例）。
        with mock.patch.object(os, "environ", {"FUKUARPA_RUNNER_AUTH": auth}):
            result = main(["--runner-pipe", pipe])
            # SAFETY: auth 必须在 main 内被立即移除，不得留给孙进程继承。
            # 断言放在 with 内部才真正检验 main 的 pop；放在还原后只是空转。
            self.assertNotIn("FUKUARPA_RUNNER_AUTH", os.environ)
        self.assertEqual(result, 0)
        mock_run.assert_called_once_with(pipe, auth)

    def test_missing_pipe_arg_exits(self):
        auth = "cd" * 16
        # FR-20260923-003：同上，避免 mock.patch.dict 在长环境块下破坏 os.environ。
        with mock.patch.object(
            os, "environ", {"FUKUARPA_RUNNER_AUTH": auth}
        ), self.assertRaises(SystemExit):
            main([])


if __name__ == "__main__":
    unittest.main()
