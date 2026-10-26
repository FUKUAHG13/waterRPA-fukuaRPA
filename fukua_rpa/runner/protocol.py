"""Versioned, bounded JSON IPC for the optional isolated Runner.

The protocol rejects unknown versions/capability gaps instead of guessing, so
the UI never reports a backend different from the one actually running.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Mapping
from typing import Any

from fukua_rpa.json_safety import loads_bounded_json, validate_json_structure

RUNNER_PROTOCOL_NAME = "fukuaRPA.runner"
# INVARIANT: bump this only with an intentional compatibility decision. Both
# peers must reject a mismatch; permissive decoding can corrupt run semantics.
RUNNER_PROTOCOL_VERSION = 3
MAX_RUNNER_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_RUNNER_CAPABILITIES = 64
RUNNER_HEARTBEAT_INTERVAL_SECONDS = 1.0
RUNNER_HEARTBEAT_STALE_SECONDS = 5.0
RUNNER_READY_TIMEOUT_SECONDS = 45.0

RUNNER_CAPABILITIES = frozenset(
    {
        "control.stop",
        "control.pause",
        "control.continue",
        "control.step_over",
        "control.prompt_response",
        "event.log",
        "event.status",
        "event.click",
        "event.debug",
        "event.prompt",
        "state.input",
        "state.runtime",
        "health.heartbeat",
        "native.report",
        "native.services.v1",
    }
)
REQUIRED_RUNNER_CAPABILITIES = RUNNER_CAPABILITIES


class RunnerProtocolError(ValueError):
    """Raised when an IPC peer sends an incompatible or malformed message."""


def normalize_runner_capabilities(values: object) -> tuple[str, ...]:
    """Validate and normalize a capability list into a sorted tuple.

    Args:
        values: Raw capability collection from an IPC message.

    Returns:
        A sorted tuple of unique, trimmed capability strings.

    Raises:
        RunnerProtocolError: If the list is malformed, too large, or
            contains non-string or empty entries.
    """
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise RunnerProtocolError("Runner 能力列表格式无效")
    if len(values) > MAX_RUNNER_CAPABILITIES:
        raise RunnerProtocolError("Runner 能力数量超过上限")
    normalized = set()
    for value in values:
        if not isinstance(value, str):
            raise RunnerProtocolError("Runner 能力名称必须是字符串")
        capability = value.strip()
        if not capability or len(capability) > 96:
            raise RunnerProtocolError("Runner 能力名称无效")
        normalized.add(capability)
    return tuple(sorted(normalized))


def make_runner_hello_payload(
    process_id: int, capabilities: object | None = None
) -> dict[str, object]:
    """Build the hello handshake payload sent by the child Runner on connect.

    Args:
        process_id: PID of the child Runner process.
        capabilities: Capability list; defaults to RUNNER_CAPABILITIES.

    Returns:
        A dict with role, pid, ppid, and normalized capabilities.

    Raises:
        RunnerProtocolError: If the PID is invalid.
    """
    if isinstance(process_id, bool):
        raise RunnerProtocolError("Runner PID 无效")
    try:
        pid = int(process_id)
    except (TypeError, ValueError) as error:
        raise RunnerProtocolError("Runner PID 无效") from error
    if pid <= 0:
        raise RunnerProtocolError("Runner PID 无效")
    return {
        "role": "runner",
        "pid": pid,
        "ppid": os.getppid(),
        "capabilities": list(
            normalize_runner_capabilities(
                RUNNER_CAPABILITIES if capabilities is None else capabilities
            )
        ),
    }


def _optional_parent_pid(payload: Mapping[str, object]) -> int:
    """Extract and validate the optional ppid field from a hello payload."""
    value = payload.get("ppid")
    if value is None:
        return 0
    if isinstance(value, bool):
        raise RunnerProtocolError("独立 Runner 父 PID 无效")
    try:
        parent_pid = int(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError) as error:
        raise RunnerProtocolError("独立 Runner 父 PID 无效") from error
    if parent_pid <= 0:
        raise RunnerProtocolError("独立 Runner 父 PID 无效")
    return parent_pid


def validate_runner_hello(
    message: object,
    *,
    expected_pid: int | None = None,
    required_capabilities: object = REQUIRED_RUNNER_CAPABILITIES,
) -> dict[str, object]:
    """Validate a hello message from the child Runner and extract identity.

    Args:
        message: Raw decoded IPC message.
        expected_pid: If set, the hello PID or ppid must match this value.
        required_capabilities: Capabilities the child must advertise.

    Returns:
        A validated dict with role, pid, ppid, and capabilities.

    Raises:
        RunnerProtocolError: If the message is malformed, the PID does not
            match, or required capabilities are missing.
    """
    validated = validate_runner_message(message)
    if validated["kind"] != "hello":
        raise RunnerProtocolError("独立 Runner 未发送 hello 握手")
    payload = validated["payload"]
    if payload.get("role") != "runner":
        raise RunnerProtocolError("独立 Runner 角色无效")
    if isinstance(payload.get("pid"), bool):
        raise RunnerProtocolError("独立 Runner PID 无效")
    try:
        pid = int(payload.get("pid"))
    except (TypeError, ValueError) as error:
        raise RunnerProtocolError("独立 Runner PID 无效") from error
    if pid <= 0:
        raise RunnerProtocolError("独立 Runner PID 无效")
    parent_pid = _optional_parent_pid(payload)
    if expected_pid is not None:
        expected = int(expected_pid)
        if pid != expected and parent_pid != expected:
            raise RunnerProtocolError(
                f"独立 Runner PID 不匹配：握手 {pid}，进程 {expected}"
            )
    capabilities = normalize_runner_capabilities(payload.get("capabilities"))
    required = set(normalize_runner_capabilities(required_capabilities))
    missing = sorted(required.difference(capabilities))
    if missing:
        raise RunnerProtocolError(
            "独立 Runner 缺少必需能力：" + "、".join(missing)
        )
    return {
        "role": "runner",
        "pid": pid,
        "ppid": parent_pid,
        "capabilities": capabilities,
    }


def _runner_summary_int(value: object, label: str, *, minimum: int = 0) -> int:
    """Coerce a payload field to int, rejecting bools and out-of-range values."""
    if isinstance(value, bool):
        raise RunnerProtocolError(f"Runner {label} 无效")
    try:
        normalized = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise RunnerProtocolError(f"Runner {label} 无效") from error
    if normalized < minimum:
        raise RunnerProtocolError(f"Runner {label} 无效")
    return normalized


def _runner_summary_bool(value: object, label: str) -> bool:
    """Validate that a payload field is strictly a bool."""
    if not isinstance(value, bool):
        raise RunnerProtocolError(f"Runner {label} 无效")
    return value


def _normalize_service_descriptor(value: object) -> dict[str, object]:
    """Validate and whitelist a single native-service descriptor from IPC."""
    if not isinstance(value, Mapping):
        raise RunnerProtocolError("Runner 服务描述格式无效")
    service = str(value.get("service") or "")
    if service not in {"screenshot", "vision", "input", "hook"}:
        raise RunnerProtocolError("Runner 服务名称无效")
    backend = str(value.get("backend") or "")
    if (
        not backend
        or len(backend) > 64
        or any(marker in backend for marker in ("/", "\\", ":"))
    ):
        raise RunnerProtocolError("Runner 服务后端名称无效")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, (list, tuple)) or len(capabilities) > 64:
        raise RunnerProtocolError("Runner 服务能力列表无效")
    clean_capabilities = []
    for item in capabilities:
        if not isinstance(item, str) or not item or len(item) > 96:
            raise RunnerProtocolError("Runner 服务能力名称无效")
        clean_capabilities.append(item)
    cancellation = str(value.get("cancellation") or "")
    if cancellation not in {"none", "bounded_call", "cooperative", "process"}:
        raise RunnerProtocolError("Runner 服务取消模式无效")
    clean = {
        "service": service,
        "contract_version": _runner_summary_int(
            value.get("contract_version"), "服务契约版本", minimum=1
        ),
        "backend": backend,
        "requested": _runner_summary_bool(value.get("requested"), "服务请求状态"),
        "available": _runner_summary_bool(value.get("available"), "服务可用状态"),
        "native": _runner_summary_bool(value.get("native"), "服务原生状态"),
        "capabilities": sorted(set(clean_capabilities)),
        "cancellation": cancellation,
        "timeout_ms": _runner_summary_int(
            value.get("timeout_ms"), "服务超时", minimum=0
        ),
    }
    fallback = value.get("fallback")
    if fallback is not None:
        if not isinstance(fallback, Mapping):
            raise RunnerProtocolError("Runner 服务回退描述无效")
        fallback_name = str(fallback.get("name") or "")
        if (
            not fallback_name
            or len(fallback_name) > 64
            or any(marker in fallback_name for marker in ("/", "\\", ":"))
        ):
            raise RunnerProtocolError("Runner 服务回退名称无效")
        clean["fallback"] = {
            "name": fallback_name,
            "available": _runner_summary_bool(
                fallback.get("available"), "服务回退状态"
            ),
        }
    return clean


def normalize_service_catalog(value: object) -> dict[str, object]:
    """Validate the full service catalog ensuring all four services are present.

    Args:
        value: Raw catalog mapping from the runtime summary payload.

    Returns:
        A normalized dict with catalog_version and sorted services list.

    Raises:
        RunnerProtocolError: If the catalog is malformed, has wrong version,
            contains duplicates, or is missing required services.
    """
    if not isinstance(value, Mapping):
        raise RunnerProtocolError("Runner 服务目录格式无效")
    version = _runner_summary_int(
        value.get("catalog_version"), "服务目录版本", minimum=1
    )
    if version != 1:
        raise RunnerProtocolError(f"Runner 服务目录版本不兼容：{version}")
    services = value.get("services")
    if not isinstance(services, (list, tuple)) or len(services) > 16:
        raise RunnerProtocolError("Runner 服务目录内容无效")
    clean_services = [_normalize_service_descriptor(item) for item in services]
    names = [item["service"] for item in clean_services]
    if len(names) != len(set(names)):
        raise RunnerProtocolError("Runner 服务目录包含重复服务")
    expected = {"screenshot", "vision", "input", "hook"}
    if set(names) != expected:
        raise RunnerProtocolError("Runner 服务目录缺少必需服务")
    return {
        "catalog_version": 1,
        "services": sorted(clean_services, key=lambda item: str(item["service"])),
    }


def normalize_runner_runtime_summary(value: object) -> dict[str, object]:
    """Whitelist the path-free runtime fields allowed into diagnostics."""

    if not isinstance(value, Mapping):
        raise RunnerProtocolError("Runner 运行摘要格式无效")
    python_version = value.get("python_version")
    if not isinstance(python_version, (list, tuple)) or len(python_version) != 3:
        raise RunnerProtocolError("Runner Python 版本摘要无效")
    clean_python = [
        _runner_summary_int(item, "Python 版本", minimum=0)
        for item in python_version
    ]
    opencv = value.get("opencv")
    native = value.get("native")
    services = value.get("services")
    if not isinstance(opencv, Mapping) or not isinstance(native, Mapping):
        raise RunnerProtocolError("Runner 后端摘要无效")
    fallback = native.get("fallback")
    if not isinstance(fallback, Mapping):
        raise RunnerProtocolError("Runner 原生回退摘要无效")
    service = str(native.get("service") or "vision")
    fallback_name = str(fallback.get("name") or "opencv")
    if service != "vision" or fallback_name != "opencv":
        raise RunnerProtocolError("Runner 原生服务名称无效")
    return {
        "pid": _runner_summary_int(value.get("pid"), "运行摘要 PID", minimum=1),
        "python_version": clean_python,
        "pointer_bits": _runner_summary_int(
            value.get("pointer_bits"), "指针位数", minimum=1
        ),
        "frozen": _runner_summary_bool(value.get("frozen"), "冻结状态"),
        "opencv": {
            "available": _runner_summary_bool(
                opencv.get("available"), "OpenCV 状态"
            ),
            "threads": _runner_summary_int(
                opencv.get("threads"), "OpenCV 线程数", minimum=0
            ),
        },
        "native": {
            "service": "vision",
            "contract_version": _runner_summary_int(
                native.get("contract_version"), "原生服务契约版本", minimum=1
            ),
            "requested": _runner_summary_bool(
                native.get("requested"), "原生请求状态"
            ),
            "available": _runner_summary_bool(
                native.get("available"), "原生可用状态"
            ),
            "api_version": _runner_summary_int(
                native.get("api_version"), "原生 API 版本", minimum=0
            ),
            "capability_mask": _runner_summary_int(
                native.get("capability_mask"), "原生能力掩码", minimum=0
            ),
            "abi_compatible": _runner_summary_bool(
                native.get("abi_compatible"), "原生 ABI 状态"
            ),
            "cancellable_search": _runner_summary_bool(
                native.get("cancellable_search"), "原生取消状态"
            ),
            "fallback": {
                "name": "opencv",
                "available": _runner_summary_bool(
                    fallback.get("available"), "原生回退状态"
                ),
            },
        },
        "services": normalize_service_catalog(services),
    }


def make_runner_message(kind: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Construct a protocol-conformant Runner IPC message envelope.

    Args:
        kind: Message type identifier (e.g. "hello", "heartbeat", "stop").
        payload: Optional message body; defaults to an empty dict.

    Returns:
        A dict with protocol, version, kind, and payload fields.

    Raises:
        RunnerProtocolError: If kind is empty or the structure fails
            JSON-safety validation.
    """
    message_kind = str(kind or "").strip()
    if not message_kind:
        raise RunnerProtocolError("Runner 消息类型不能为空")
    message = {
        "protocol": RUNNER_PROTOCOL_NAME,
        "version": RUNNER_PROTOCOL_VERSION,
        "kind": message_kind,
        "payload": dict(payload or {}),
    }
    try:
        validate_json_structure(message, label="Runner 消息")
    except ValueError as error:
        raise RunnerProtocolError(str(error)) from error
    return message


def encode_runner_message(message: Mapping[str, Any]) -> bytes:
    """Validate and serialize a Runner message to compact UTF-8 JSON bytes.

    Args:
        message: A message dict conforming to the Runner protocol.

    Returns:
        UTF-8 encoded JSON bytes within the size limit.

    Raises:
        RunnerProtocolError: If the message is invalid or exceeds
            MAX_RUNNER_MESSAGE_BYTES.
    """
    validated = validate_runner_message(message)
    try:
        raw = json.dumps(
            validated,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RunnerProtocolError(f"Runner 消息无法序列化：{error}") from error
    if len(raw) > MAX_RUNNER_MESSAGE_BYTES:
        raise RunnerProtocolError(
            f"Runner 消息超过 {MAX_RUNNER_MESSAGE_BYTES} 字节上限"
        )
    return raw


def decode_runner_message(raw: bytes) -> dict[str, Any]:
    """Decode and validate raw bytes into a Runner protocol message.

    Args:
        raw: UTF-8 JSON bytes received from the IPC channel.

    Returns:
        A validated message dict.

    Raises:
        RunnerProtocolError: If the bytes exceed the size limit, are not
            valid JSON, or fail structural validation.
    """
    try:
        message = loads_bounded_json(
            raw,
            max_bytes=MAX_RUNNER_MESSAGE_BYTES,
            label="Runner 消息",
        )
    except ValueError as error:
        raise RunnerProtocolError(str(error)) from error
    return validate_runner_message(message)


def validate_runner_message(message: object) -> dict[str, Any]:
    """Validate the protocol envelope of a decoded Runner message.

    Checks protocol name, version, kind, and payload structure.

    Args:
        message: A decoded JSON object (typically a dict).

    Returns:
        A normalized message dict with trimmed kind and dict payload.

    Raises:
        RunnerProtocolError: If any envelope field is missing or invalid.
    """
    if not isinstance(message, Mapping):
        raise RunnerProtocolError("Runner 消息必须是对象")
    if message.get("protocol") != RUNNER_PROTOCOL_NAME:
        raise RunnerProtocolError("Runner 协议标识不匹配")
    if message.get("version") != RUNNER_PROTOCOL_VERSION:
        raise RunnerProtocolError(
            "Runner 协议版本不兼容："
            f"收到 {message.get('version')}，需要 {RUNNER_PROTOCOL_VERSION}"
        )
    kind = message.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise RunnerProtocolError("Runner 消息类型无效")
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        raise RunnerProtocolError("Runner 消息内容必须是对象")
    normalized = {
        "protocol": RUNNER_PROTOCOL_NAME,
        "version": RUNNER_PROTOCOL_VERSION,
        "kind": kind.strip(),
        "payload": dict(payload),
    }
    try:
        validate_json_structure(normalized, label="Runner 消息")
    except ValueError as error:
        raise RunnerProtocolError(str(error)) from error
    return normalized


class RunnerConnection:
    """Serialize writes while allowing one independent reader thread.

    Wraps a multiprocessing Connection with protocol encoding/decoding and
    a send lock so that heartbeat and control threads can write concurrently.
    """

    def __init__(self, connection: object) -> None:
        self.connection = connection
        self._send_lock = threading.Lock()

    def send(self, kind: str, payload: Mapping[str, Any] | None = None) -> None:
        """Encode and send a protocol message over the connection.

        Args:
            kind: Message type identifier.
            payload: Optional message body.

        Raises:
            RunnerProtocolError: If encoding fails.
            OSError: If the underlying connection is broken.
        """
        raw = encode_runner_message(make_runner_message(kind, payload))
        with self._send_lock:
            self.connection.send_bytes(raw)  # type: ignore[attr-defined]

    def receive(self) -> dict[str, Any]:
        """Receive and decode the next protocol message from the connection.

        Returns:
            A validated message dict.

        Raises:
            EOFError: If the connection is closed.
            RunnerProtocolError: If the message is malformed.
        """
        return decode_runner_message(self.connection.recv_bytes())  # type: ignore[attr-defined]

    def poll(self, timeout: float = 0.0) -> bool:
        """Check whether data is available on the connection.

        FR-20260930-014: a closed peer is a **normal** terminal condition, not an
        error. Letting ``BrokenPipeError`` escape reached the main loop's blanket
        ``except Exception``, which pushed the raw English OSError text to the
        user via ``_emit_local_error``. A dead child must instead look like
        "nothing more to read" so the loop leaves through its own orderly path.

        Args:
            timeout: Maximum seconds to wait; 0 for non-blocking.

        Returns:
            True if a message can be received without blocking.
        """
        try:
            return bool(self.connection.poll(max(0.0, float(timeout))))  # type: ignore[attr-defined]
        except (BrokenPipeError, EOFError, OSError):
            return False

    def close(self) -> None:
        """Close the underlying connection, ignoring already-closed errors."""
        with contextlib.suppress(OSError, EOFError):
            self.connection.close()  # type: ignore[attr-defined]
