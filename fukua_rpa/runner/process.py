"""Child-process entry point for the experimental isolated Runner."""

from __future__ import annotations

import argparse
import logging
import math
import os
import struct
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from multiprocessing.connection import Client

logger = logging.getLogger(__name__)

import contextlib  # noqa: E402

from fukua_rpa.engine import RPAEngine  # noqa: E402
from fukua_rpa.log_policy import LOG_RUN  # noqa: E402
from fukua_rpa.logging_service import (  # noqa: E402
    ProcessedLogEvent,
    flush_logs,
    register_processed_log_sink,
    set_log_base_dir,
    unregister_processed_log_sink,
    update_log_config,
    write_log,
)
from fukua_rpa.native_services import build_runtime_service_catalog  # noqa: E402
from fukua_rpa.privacy import private_error_text, safe_error_name  # noqa: E402
from fukua_rpa.run_config import RunRequest  # noqa: E402
from fukua_rpa.worker import FailsafeWatchdog  # noqa: E402

from .ipc import RUNNER_IPC_FAMILY, validate_runner_pipe_name  # noqa: E402
from .protocol import (  # noqa: E402
    RUNNER_CAPABILITIES,
    RUNNER_HEARTBEAT_INTERVAL_SECONDS,
    RunnerConnection,
    RunnerProtocolError,
    make_runner_hello_payload,
)


class _PromptRegistry:
    """Track outstanding user-prompt requests and relay responses via IPC."""

    def __init__(self, channel: RunnerConnection) -> None:
        self.channel = channel
        self._lock = threading.Lock()
        self._requests: dict[str, object] = {}

    def publish(self, request: object) -> None:
        """Send a prompt request to the parent and register it for resolution."""
        prompt_id = uuid.uuid4().hex
        with self._lock:
            self._requests[prompt_id] = request
        self.channel.send(
            "prompt",
            {"id": prompt_id, "text": str(getattr(request, "text", ""))},
        )

    def resolve(self, prompt_id: object, accepted: bool) -> None:
        """Resolve a pending prompt by ID, acknowledging or cancelling it."""
        with self._lock:
            request = self._requests.pop(str(prompt_id), None)
        if request is None:
            return
        if accepted:
            request.acknowledge()  # type: ignore[attr-defined]
        else:
            request.cancel()  # type: ignore[attr-defined]

    def cancel_all(self) -> None:
        """Cancel all outstanding prompt requests during shutdown."""
        with self._lock:
            requests = list(self._requests.values())
            self._requests.clear()
        for request in requests:
            try:
                request.cancel()  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning("取消请求失败: %s", exc, exc_info=True)


class _RunnerHealthState:
    """Share a small content-free lifecycle snapshot with the heartbeat thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phase: str = "connected"
        self._engine: object | None = None

    def update(self, *, phase: str | None = None, engine: object | None = None) -> None:
        """Update the tracked phase and/or engine reference under lock."""
        with self._lock:
            if phase is not None:
                self._phase = str(phase)
            if engine is not None:
                self._engine = engine

    def snapshot(self) -> dict[str, object]:
        """Return a serializable health snapshot for the next heartbeat."""
        with self._lock:
            phase = self._phase
            engine = self._engine
        return {
            "pid": os.getpid(),
            "phase": phase,
            "engine_running": bool(engine is not None and engine.is_running),  # type: ignore[attr-defined]
            "stop_requested": bool(
                engine is not None and engine.stop_requested  # type: ignore[attr-defined]
            ),
        }

    def stop_engine(self) -> None:
        """Request an engine stop from the heartbeat thread on IPC failure."""
        with self._lock:
            engine = self._engine
        if engine is not None:
            try:
                engine.stop()  # type: ignore[attr-defined]
            except Exception as exc:
                logger.warning("停止引擎失败: %s", exc, exc_info=True)


_START_TIMEOUT_SECONDS = 30.0


def _heartbeat_loop(
    channel: RunnerConnection,
    health_state: _RunnerHealthState,
    stop_event: threading.Event,
) -> None:
    """Periodically send heartbeat messages until stopped or IPC fails."""
    sequence = 0
    while not stop_event.is_set():
        sequence += 1
        payload = health_state.snapshot()
        payload["sequence"] = sequence
        payload["runner_monotonic"] = round(time.monotonic(), 6)
        payload["ppid"] = os.getppid()
        try:
            channel.send("heartbeat", payload)
        except (EOFError, OSError, RunnerProtocolError):
            stop_event.set()
            health_state.stop_engine()
            return
        if stop_event.wait(RUNNER_HEARTBEAT_INTERVAL_SECONDS):
            return


def _normalize_region_list(
    regions: object,
) -> list[tuple[int, int, int, int]]:
    """Coerce a raw region list into validated (x, y, w, h) integer tuples."""
    normalized: list[tuple[int, int, int, int]] = []
    for region in regions or []:  # type: ignore[union-attr]
        try:
            x, y, width, height = [int(float(value)) for value in region]  # type: ignore[union-attr]
        except (TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            normalized.append((x, y, width, height))
    return normalized


_JSON_SAFE_MAX_NODES = 4096
_JSON_SAFE_MAX_DEPTH = 24
_JSON_SAFE_REPR_LIMIT = 1000


def _json_safe(value: object, *, depth: int = 0, _count: list[int] | None = None) -> object:
    """Recursively sanitize a value for safe JSON serialization over IPC.

    Enforces node-count and depth limits to prevent unbounded payloads.
    """
    if _count is None:
        _count = [0]
    _count[0] += 1
    if _count[0] > _JSON_SAFE_MAX_NODES:
        return "...(truncated)"
    if depth > _JSON_SAFE_MAX_DEPTH:
        return "<嵌套过深>"
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item, depth=depth + 1, _count=_count)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item, depth=depth + 1, _count=_count) for item in value]
    return repr(value)[:_JSON_SAFE_REPR_LIMIT]


#: IPC 净化把值替换为可见哨兵时使用的标记，二者都表示"此处被丢弃"。
_JSON_SAFE_TRUNCATED = "...(truncated)"
_JSON_SAFE_TOO_DEEP = "<嵌套过深>"


def _count_sanitized_markers(value: object) -> int:
    """Count truncation markers inside an already-sanitized subtree."""
    if isinstance(value, str):
        return 1 if value in (_JSON_SAFE_TRUNCATED, _JSON_SAFE_TOO_DEEP) else 0
    if isinstance(value, Mapping):
        return sum(_count_sanitized_markers(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return sum(_count_sanitized_markers(item) for item in value)
    return 0


def _json_safe_run_trace(trace: object) -> object:
    """Sanitize a RunTrace snapshot and report how much the IPC budget dropped.

    FR-SCOPE004-001: the shared 4096-node budget replaces values with a visible
    sentinel but **keeps the container length**, so ``len(events)`` still looks
    complete and a consumer cannot tell that events were lost. This wrapper adds
    an explicit, aggregable count so that loss is visible instead of silent.

    ``dropped_events`` is the trace's own counter ("never recorded") and is left
    untouched: IPC-level loss and never-recorded events are different things and
    must not be conflated. The extra field is only added when something was
    actually dropped, so an ordinary payload keeps its previous shape.
    """
    if not isinstance(trace, Mapping):
        # Unexpected payload shape: pass through without fabricating a count.
        return _json_safe(trace)
    sanitized = _json_safe(dict(trace))
    if not isinstance(sanitized, dict):
        return sanitized
    # Count **damaged entries**, not markers: one event may contain several
    # replaced values but is still a single lost record. Anything damaged outside
    # ``events`` is rare; fold it in so the count never under-reports.
    events = sanitized.get("events")
    if isinstance(events, list):
        damaged = sum(1 for item in events if _count_sanitized_markers(item))
        outside = _count_sanitized_markers(
            {k: v for k, v in sanitized.items() if k != "events"}
        )
        damaged += outside
    else:
        damaged = _count_sanitized_markers(sanitized)
    if damaged:
        sanitized["ipc_dropped_events"] = damaged
    return sanitized


def _native_runtime_summary(engine: object) -> dict[str, object]:
    """Build a native-core service snapshot for the runtime report."""
    core = getattr(engine, "native_core", None)
    if core is None:
        return {
            "service": "vision",
            "contract_version": 1,
            "requested": bool(getattr(engine, "use_native_core", False)),
            "available": False,
            "api_version": 0,
            "capability_mask": 0,
            "abi_compatible": False,
            "cancellable_search": False,
            "fallback": {
                "name": "opencv",
                "available": bool(getattr(engine, "opencv_available", False)),
            },
        }
    return core.service_snapshot(
        requested=bool(getattr(engine, "use_native_core", False)),
        fallback_available=bool(getattr(engine, "opencv_available", False)),
    )


def _runner_runtime_summary(engine: object) -> dict[str, object]:
    """Collect a full runtime summary including Python, OpenCV, and services."""
    version = sys.version_info
    return {
        "pid": os.getpid(),
        "python_version": [version.major, version.minor, version.micro],
        "pointer_bits": struct.calcsize("P") * 8,
        "frozen": bool(getattr(sys, "frozen", False)),
        "opencv": {
            "available": bool(getattr(engine, "opencv_available", False)),
            "threads": int(getattr(engine, "opencv_threads", 0) or 0),
        },
        "native": _native_runtime_summary(engine),
        "services": build_runtime_service_catalog(engine),
    }


def _configure_child_logging(payload: object, base_dir: str) -> None:
    """Apply logging options received from the parent to the child process."""
    options = payload if isinstance(payload, Mapping) else {}
    update_log_config(
        log_to_file=bool(options.get("log_to_file", False)),
        log_to_ui=bool(options.get("log_to_ui", True)),
        privacy_protected=bool(options.get("privacy_protected", True)),
    )
    set_log_base_dir(base_dir)


def _control_loop(
    channel: RunnerConnection,
    engine: object,
    prompts: _PromptRegistry,
    stop_event: threading.Event,
) -> None:
    """Listen for parent commands (stop, pause, step) and dispatch them."""
    try:
        while not stop_event.is_set():
            if not channel.poll(0.05):
                continue
            message = channel.receive()
            kind = message["kind"]
            payload = message["payload"]
            if kind in ("stop", "shutdown"):
                engine.stop()  # type: ignore[attr-defined]
                prompts.cancel_all()
                if kind == "shutdown":
                    stop_event.set()
            elif kind == "pause":
                engine.debug_session.request_pause()  # type: ignore[attr-defined]
            elif kind == "continue":
                engine.debug_session.continue_run()  # type: ignore[attr-defined]
            elif kind == "step_over":
                engine.debug_session.step_over()  # type: ignore[attr-defined]
            elif kind == "prompt_response":
                prompts.resolve(payload.get("id"), bool(payload.get("accepted")))
            else:
                raise RunnerProtocolError(f"独立 Runner 收到未知命令：{kind}")
    except (EOFError, OSError, RunnerProtocolError) as exc:
        with contextlib.suppress(Exception):
            channel.send("error", {"message": str(exc)})
        engine.stop()  # type: ignore[attr-defined]
        prompts.cancel_all()
        stop_event.set()


def run_isolated_runner(pipe_name: str, auth_hex: str) -> int:
    """Connect to the parent via named pipe and execute the full Runner lifecycle.

    This is the main entry point for the child process. It performs the IPC
    handshake, receives the start command, runs tasks, and reports results
    back over the protocol channel.

    Args:
        pipe_name: Validated named-pipe address to connect to.
        auth_hex: Hex-encoded authentication key for the connection.

    Returns:
        Process exit code: 0 on success, 1 on runtime error, 2 on bad args.
    """
    auth_str = str(auth_hex).strip()
    if (
        not auth_str
        or len(auth_str) % 2 != 0
        or not all(c in "0123456789abcdefABCDEF" for c in auth_str)
    ):
        print(
            f"[runner] auth_hex 格式无效: 长度={len(auth_str)}",
            file=sys.stderr,
        )
        return 2
    connection = Client(
        validate_runner_pipe_name(pipe_name),
        family=RUNNER_IPC_FAMILY,
        authkey=bytes.fromhex(auth_str),
    )
    channel = RunnerConnection(connection)
    engine = None
    watchdog = None
    control_thread = None
    heartbeat_thread = None
    processed_log_sink = None
    run_log_context: dict[str, object] = {}
    control_stop = threading.Event()
    health_state = _RunnerHealthState()
    prompts = _PromptRegistry(channel)
    try:
        channel.send(
            "hello",
            make_runner_hello_payload(os.getpid(), RUNNER_CAPABILITIES),
        )
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(channel, health_state, control_stop),
            daemon=True,
            name="fukuaRPA-runner-heartbeat",
        )
        heartbeat_thread.start()
        health_state.update(phase="waiting_start")
        # Poll for the start command with a timeout so we don't hang forever
        # if the parent process becomes unresponsive after connecting.
        start_deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        start_message = None
        while True:
            if control_stop.is_set():
                return 0
            if time.monotonic() > start_deadline:
                # Parent may be hung; report and exit safely.
                try:
                    channel.send("error", {"message": "start timeout"})
                except Exception as exc:
                    logger.warning("发送启动超时错误失败: %s", exc, exc_info=True)
                return 1
            try:
                if channel.poll(0.5):
                    start_message = channel.receive()
                    break
            except EOFError:
                return 0
        if start_message["kind"] != "start":
            raise RunnerProtocolError("独立 Runner 首条命令必须是 start")
        health_state.update(phase="initializing")
        payload = start_message["payload"]
        tasks = payload.get("tasks")
        config = payload.get("config")
        if not isinstance(tasks, list) or not isinstance(config, Mapping):
            raise RunnerProtocolError("独立 Runner 启动参数格式无效")
        base_dir = os.path.abspath(str(payload.get("base_dir") or os.getcwd()))
        profile_name = str(payload.get("profile_name") or "当前方案")
        _configure_child_logging(payload.get("logging"), base_dir)
        raw_log_context = payload.get("log_context")
        if isinstance(raw_log_context, Mapping):
            run_log_context = {
                str(key): value
                for key, value in raw_log_context.items()
                if isinstance(value, (str, int, float, bool, type(None)))
            }

        def relay_log(event: ProcessedLogEvent) -> None:
            channel.send(
                "log",
                {
                    "processed": True,
                    "text": str(event.message),
                    "timestamp": str(event.timestamp),
                    "event_time_ns": int(event.event_time_ns),
                    "context": _json_safe(event.context),
                    "structured": _json_safe(event.structured),
                },
            )

        processed_log_sink = relay_log
        register_processed_log_sink(processed_log_sink)

        request = RunRequest.create(tasks, config, profile_name)
        engine = RPAEngine(base_dir=base_dir, defer_backends=False)
        engine.run_log_context = dict(run_log_context)
        health_state.update(engine=engine)
        request.config.apply_to(engine)
        regions = _normalize_region_list(config.get("scan_regions"))
        if regions:
            engine.apply_scan_region(scan_regions=regions)
        else:
            single = _normalize_region_list([config.get("scan_region")])
            engine.apply_scan_region(scan_region=single[0] if single else None)
        engine.scale_memory_store.import_state(payload.get("scale_memory"))
        engine.callback_input_state = lambda data: channel.send(  # pyright: ignore[reportAttributeAccessIssue]
            "input_state", {"data": _json_safe(data)}
        )

        run_id = engine.reserve_run()
        if run_id is None:
            raise RuntimeError("独立 Runner 执行引擎正忙")
        control_thread = threading.Thread(
            target=_control_loop,
            args=(channel, engine, prompts, control_stop),
            daemon=True,
            name="fukuaRPA-runner-control",
        )
        control_thread.start()
        watchdog = FailsafeWatchdog(engine, run_log_context)
        watchdog.start()
        health_state.update(phase="running")
        channel.send(
            "started",
            {
                "run_id": int(run_id),
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "runtime": _runner_runtime_summary(engine),
            },
        )

        engine.run_tasks(
            request.mutable_tasks(),
            lambda _message: None,
            lambda data: channel.send("status", {"data": _json_safe(data)}),
            lambda data: channel.send("click", {"data": _json_safe(data)}),
            lambda data: channel.send("debug", {"data": _json_safe(data)}),
            prompts.publish,
            run_id,
        )
        flush_logs(timeout=2.0)
        health_state.update(phase="finalizing")
        lifecycle = engine.lifecycle.snapshot()
        channel.send(
            "runtime_state",
            {
                "scale_memory": engine.scale_memory_store.export_state(),
                "performance_report": _json_safe(
                    getattr(engine, "last_performance_report", {}) or {}
                ),
                "run_trace": _json_safe_run_trace(
                    getattr(engine, "last_run_trace", {}) or {}
                ),
                "runner": _runner_runtime_summary(engine),
            },
        )
        channel.send(
            "finished",
            {
                "outcome": str(lifecycle.last_outcome or "finished"),
                "error": str(lifecycle.last_error or ""),
            },
        )
        return 0
    except Exception as error:
        health_state.update(phase="failed")
        try:
            write_log(
                f"独立 Runner 异常: {safe_error_name(error)}",
                private_message=f"独立 Runner 异常: {private_error_text(error)}",
                context=run_log_context,
                         category=LOG_RUN,
                         critical=True,
                     )
            flush_logs(timeout=1.0)
            channel.send("error", {"message": safe_error_name(error)})
        except Exception as exc:
            logger.warning("发送 Runner 错误报告失败: %s", exc, exc_info=True)
        return 1
    finally:
        with contextlib.suppress(Exception):
            health_state.update(phase="stopping")
        control_stop.set()
        with contextlib.suppress(Exception):
            prompts.cancel_all()
        if engine is not None:
            try:
                engine.stop()
                engine.release_injected_inputs()
            except Exception as exc:
                logger.warning("Runner 清理引擎失败: %s", exc, exc_info=True)
        if watchdog is not None:
            with contextlib.suppress(Exception):
                watchdog.kill()
                watchdog.join(timeout=1.0)
        if control_thread is not None and control_thread is not threading.current_thread():
            with contextlib.suppress(Exception):
                control_thread.join(timeout=0.5)
        if heartbeat_thread is not None:
            with contextlib.suppress(Exception):
                heartbeat_thread.join(timeout=0.5)
        # Cleanup after the authoritative "finished" message must never turn a
        # completed automation into exit code 1. The sink is best-effort and
        # all substantive records were already flushed before that message.
        with contextlib.suppress(Exception):
            flush_logs(timeout=1.0)
        if processed_log_sink is not None:
            with contextlib.suppress(Exception):
                unregister_processed_log_sink(processed_log_sink)
        with contextlib.suppress(Exception):
            channel.close()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the isolated Runner child process.

    Reads the auth key from the FUKUARPA_RUNNER_AUTH environment variable
    (deleted immediately to prevent grandchild inheritance) and the pipe
    address from --runner-pipe, then delegates to run_isolated_runner.

    Args:
        argv: Command-line arguments; defaults to sys.argv[1:].

    Returns:
        Process exit code.
    """
    # SAFETY: auth key arrives via env var (same-user visibility only), not
    # command-line args.  Delete immediately so grandchild processes cannot
    # inherit the secret.
    auth_hex = os.environ.pop("FUKUARPA_RUNNER_AUTH", "")
    if not auth_hex:
        return 1
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--runner-pipe", required=True)
    args = parser.parse_args(argv)
    return run_isolated_runner(args.runner_pipe, auth_hex)


if __name__ == "__main__":
    raise SystemExit(main())
