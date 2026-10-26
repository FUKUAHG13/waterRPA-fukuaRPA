"""Versioned, size-bounded, framed JSON IPC for the future plugin host.

Design section 5.7: code plugins run in an isolated host process, never in the
main UI. IPC reuses the Runner protocol's ideas (version, message size limit,
bounded JSON) but uses its own message types so the two protocols never mix.

This module is DATA-ONLY. It defines the structured message types and a
length-prefixed frame/deframe pair suitable for a named-pipe transport later.
It contains NO socket, pipe, or threading code.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

PLUGIN_PROTOCOL_NAME = "fukuaRPA.plugin"
PLUGIN_PROTOCOL_VERSION = 1

# Upper bound on a single framed message (request/response/stop) in bytes.
MESSAGE_SIZE_LIMIT = 4 * 1024 * 1024  # 4 MiB
LENGTH_PREFIX_BYTES = 4

_KNOWN_KINDS = ("request", "response", "stop")


class PluginProtocolError(ValueError):
    """Raised on malformed, oversized, or version-incompatible messages."""


def _reject_duplicate_keys(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PluginProtocolError(f"消息包含重复字段：{key}")
        result[key] = value
    return result


def _reject_non_finite(token: str) -> Any:
    """``parse_constant`` hook: reject NaN/Infinity in inbound frames.

    FR-004: ``frame_message`` already refuses to *encode* NaN/Infinity
    (``allow_nan=False``), but ``deframe_message`` used a bare ``json.loads``,
    which accepts them — so the two directions of the same protocol disagreed.
    ARCHITECTURE.md #61 requires rejecting NaN/Infinity at parse time.
    """

    raise PluginProtocolError(f"消息包含非有限数值：{token}")


# --------------------------------------------------------------------------
# Structured message payloads
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PluginRequest:
    """Typed payload of a ``request`` envelope invoking a plugin action.

    Attributes:
        run_id: Identifier of the workflow run issuing the request.
        step_id: Identifier of the step within the run.
        plugin_id: Target plugin that owns the action.
        action_id: Action to invoke within the plugin.
        deadline_ns: Absolute monotonic deadline in nanoseconds by which the
            host should complete the action.
        payload: Action-specific arguments supplied by the step.
    """

    run_id: str
    step_id: str
    plugin_id: str
    action_id: str
    deadline_ns: int
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class PluginResponse:
    """Typed payload of a ``response`` envelope returning an action outcome.

    Attributes:
        run_id: Identifier of the workflow run being answered.
        step_id: Identifier of the step being answered.
        ok: True if the action succeeded, False if it failed.
        result: Optional result payload on success.
        error: Optional error detail on failure.
    """

    run_id: str
    step_id: str
    ok: bool
    result: Any
    error: Any


@dataclass(frozen=True)
class PluginStop:
    """Typed payload of a ``stop`` envelope requesting step cancellation.

    Attributes:
        run_id: Identifier of the workflow run to stop a step in.
        step_id: Identifier of the step to cancel.
    """

    run_id: str
    step_id: str


# --------------------------------------------------------------------------
# Envelope constructors
# --------------------------------------------------------------------------

def make_request(
    run_id: str,
    step_id: str,
    plugin_id: str,
    action_id: str,
    deadline_ns: int,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a ``request`` envelope invoking a plugin action.

    Args:
        run_id: Identifier of the workflow run issuing the request.
        step_id: Identifier of the step within the run.
        plugin_id: Target plugin that owns the action.
        action_id: Action to invoke within the plugin.
        deadline_ns: Absolute monotonic deadline in nanoseconds by which the
            host should complete the action.
        payload: Optional action-specific arguments; copied into the envelope.

    Returns:
        A protocol/version-tagged envelope dict ready for validation and
        framing.
    """
    payload = dict(payload or {})
    return _make_envelope(
        "request",
        {
            "run_id": run_id,
            "step_id": step_id,
            "plugin_id": plugin_id,
            "action_id": action_id,
            "deadline_ns": deadline_ns,
            "payload": payload,
        },
    )


def make_response(
    run_id: str,
    step_id: str,
    ok: bool,
    result: Any = None,
    error: Any = None,
) -> dict[str, Any]:
    """Build a ``response`` envelope returning an action's outcome.

    Args:
        run_id: Identifier of the workflow run being answered.
        step_id: Identifier of the step being answered.
        ok: True if the action succeeded, False if it failed.
        result: Optional result payload on success.
        error: Optional error detail on failure.

    Returns:
        A protocol/version-tagged response envelope dict.
    """
    return _make_envelope(
        "response",
        {
            "run_id": run_id,
            "step_id": step_id,
            "ok": ok,
            "result": result,
            "error": error,
        },
    )


def make_stop(run_id: str, step_id: str) -> dict[str, Any]:
    """Build a ``stop`` envelope requesting cancellation of a step.

    Args:
        run_id: Identifier of the workflow run to stop a step in.
        step_id: Identifier of the step to cancel.

    Returns:
        A protocol/version-tagged stop envelope dict.
    """
    return _make_envelope(
        "stop", {"run_id": run_id, "step_id": step_id}
    )


def _make_envelope(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol": PLUGIN_PROTOCOL_NAME,
        "version": PLUGIN_PROTOCOL_VERSION,
        "kind": kind,
        "payload": payload,
    }


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PluginProtocolError(f"{label} 必须是非空字符串")
    return value


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PluginProtocolError(f"{label} 必须是整数")
    return value


def validate_plugin_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an envelope dict and return a normalized copy.

    Checks the protocol identifier and version, requires a known ``kind``, and
    type-checks every payload field for that kind (request, response, or stop).
    The returned envelope contains only the canonical fields with coerced
    types, so downstream code never sees unexpected or mistyped data.

    Args:
        message: Raw envelope mapping, typically decoded from a frame.

    Returns:
        A normalized envelope dict with validated ``protocol``, ``version``,
        ``kind``, and ``payload`` fields.

    Raises:
        PluginProtocolError: If the message is not a mapping, has a mismatched
            protocol/version, an unknown kind, or any missing/mistyped payload
            field.
    """

    if not isinstance(message, Mapping):
        raise PluginProtocolError("消息必须是对象")
    if message.get("protocol") != PLUGIN_PROTOCOL_NAME:
        raise PluginProtocolError("协议标识不匹配")
    if message.get("version") != PLUGIN_PROTOCOL_VERSION:
        raise PluginProtocolError(
            f"协议版本不兼容：收到 {message.get('version')}，"
            f"需要 {PLUGIN_PROTOCOL_VERSION}"
        )
    kind = message.get("kind")
    if kind not in _KNOWN_KINDS:
        raise PluginProtocolError(f"消息类型无效：{kind!r}")
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        raise PluginProtocolError("消息内容必须是对象")

    if kind == "request":
        # FR-009: 修复前写作 dict(payload.get("payload") or {})，对 list/str/int
        # 等不可转 dict 的真值会泄漏裸 TypeError / ValueError，违反本模块
        # "失败一律抛 PluginProtocolError" 的异常契约，也不满足 #86 对可分类
        # 错误处理的要求。改为显式 Mapping 判断。
        # 注意：这会从严拒绝 pair-list（如 [("a", 1)]）——原写法经 dict() 接受，
        # 属契约收紧，由 tests/test_plugin_protocol_contract.py 锁定。
        inner_payload = payload.get("payload")
        if inner_payload is None:
            inner_payload = {}
        elif not isinstance(inner_payload, Mapping):
            raise PluginProtocolError("payload.payload 必须是对象")
        else:
            inner_payload = dict(inner_payload)
        normalized_payload = {
            "run_id": _require_str(payload.get("run_id"), "run_id"),
            "step_id": _require_str(payload.get("step_id"), "step_id"),
            "plugin_id": _require_str(payload.get("plugin_id"), "plugin_id"),
            "action_id": _require_str(payload.get("action_id"), "action_id"),
            "deadline_ns": _require_int(payload.get("deadline_ns"), "deadline_ns"),
            "payload": inner_payload,
        }
        if normalized_payload["deadline_ns"] < 0:
            raise PluginProtocolError("deadline_ns 不能为负")
    elif kind == "response":
        ok = payload.get("ok")
        if not isinstance(ok, bool):
            raise PluginProtocolError("ok 必须是布尔值")
        normalized_payload = {
            "run_id": _require_str(payload.get("run_id"), "run_id"),
            "step_id": _require_str(payload.get("step_id"), "step_id"),
            "ok": ok,
            "result": payload.get("result"),
            "error": payload.get("error"),
        }
    else:  # stop
        normalized_payload = {
            "run_id": _require_str(payload.get("run_id"), "run_id"),
            "step_id": _require_str(payload.get("step_id"), "step_id"),
        }

    return {
        "protocol": PLUGIN_PROTOCOL_NAME,
        "version": PLUGIN_PROTOCOL_VERSION,
        "kind": kind,
        "payload": normalized_payload,
    }


# --------------------------------------------------------------------------
# Framing (length-prefixed JSON), ready for a named-pipe transport
# --------------------------------------------------------------------------

def frame_message(message: Mapping[str, Any]) -> bytes:
    """Validate and encode a message as ``<4-byte BE length><JSON>``.

    The message is validated first, then serialized as compact UTF-8 JSON
    (no NaN/Infinity) and prefixed with its big-endian byte length so a
    stream transport can delimit it.

    Args:
        message: Envelope mapping to frame.

    Returns:
        The length-prefixed wire bytes.

    Raises:
        PluginProtocolError: If validation fails, the message is not
            JSON-serializable, or the encoded form exceeds
            ``MESSAGE_SIZE_LIMIT``.
    """

    validated = validate_plugin_message(message)
    try:
        raw = json.dumps(
            validated,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise PluginProtocolError(f"消息无法序列化：{error}") from error
    if len(raw) > MESSAGE_SIZE_LIMIT:
        raise PluginProtocolError(f"消息超过 {MESSAGE_SIZE_LIMIT} 字节上限")
    return len(raw).to_bytes(LENGTH_PREFIX_BYTES, "big") + raw


def deframe_message(data: bytes | bytearray) -> dict[str, Any]:
    """Parse the first length-prefixed message from *data*.

    Reads the 4-byte big-endian length prefix, bounds-checks it against
    ``MESSAGE_SIZE_LIMIT``, slices out exactly that many bytes, and decodes
    them as JSON. Duplicate object keys and non-finite numbers (NaN/Infinity)
    are rejected during decoding, and the resulting envelope is validated
    before being returned.

    Args:
        data: Raw bytes received from the transport, starting at a frame
            boundary.

    Returns:
        The validated, normalized envelope dict.

    Raises:
        PluginProtocolError: On truncation, a declared length over the size
            limit, malformed JSON, duplicate keys, non-finite numbers, or
            envelope validation failure.
    """

    if not isinstance(data, (bytes, bytearray)):
        raise PluginProtocolError("消息数据必须是字节")
    data = bytes(data)
    if len(data) < LENGTH_PREFIX_BYTES:
        raise PluginProtocolError("消息数据过短")
    length = int.from_bytes(data[:LENGTH_PREFIX_BYTES], "big")
    if length > MESSAGE_SIZE_LIMIT:
        raise PluginProtocolError("声明的消息长度超过上限")
    if len(data) < LENGTH_PREFIX_BYTES + length:
        raise PluginProtocolError("消息数据不完整")
    raw = data[LENGTH_PREFIX_BYTES : LENGTH_PREFIX_BYTES + length]
    try:
        message = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except json.JSONDecodeError as error:
        raise PluginProtocolError(f"消息 JSON 解析失败：{error}") from error
    return validate_plugin_message(message)


# --------------------------------------------------------------------------
# Typed accessors for parsed payloads
# --------------------------------------------------------------------------

def parse_request(envelope: Mapping[str, Any]) -> PluginRequest:
    """Validate an envelope and return it as a typed :class:`PluginRequest`.

    Args:
        envelope: Raw request envelope mapping.

    Returns:
        A frozen :class:`PluginRequest` with the validated payload fields.

    Raises:
        PluginProtocolError: If validation fails or the envelope is not a
            ``request`` message.
    """
    validated = validate_plugin_message(envelope)
    if validated["kind"] != "request":
        raise PluginProtocolError("不是 request 消息")
    p = validated["payload"]
    return PluginRequest(
        run_id=p["run_id"],
        step_id=p["step_id"],
        plugin_id=p["plugin_id"],
        action_id=p["action_id"],
        deadline_ns=p["deadline_ns"],
        payload=p["payload"],
    )


def parse_response(envelope: Mapping[str, Any]) -> PluginResponse:
    """Validate an envelope and return it as a typed :class:`PluginResponse`.

    Args:
        envelope: Raw response envelope mapping.

    Returns:
        A frozen :class:`PluginResponse` with the validated outcome fields.

    Raises:
        PluginProtocolError: If validation fails or the envelope is not a
            ``response`` message.
    """
    validated = validate_plugin_message(envelope)
    if validated["kind"] != "response":
        raise PluginProtocolError("不是 response 消息")
    p = validated["payload"]
    return PluginResponse(
        run_id=p["run_id"],
        step_id=p["step_id"],
        ok=p["ok"],
        result=p["result"],
        error=p["error"],
    )


def parse_stop(envelope: Mapping[str, Any]) -> PluginStop:
    """Validate an envelope and return it as a typed :class:`PluginStop`.

    Args:
        envelope: Raw stop envelope mapping.

    Returns:
        A frozen :class:`PluginStop` identifying the run and step to cancel.

    Raises:
        PluginProtocolError: If validation fails or the envelope is not a
            ``stop`` message.
    """
    validated = validate_plugin_message(envelope)
    if validated["kind"] != "stop":
        raise PluginProtocolError("不是 stop 消息")
    p = validated["payload"]
    return PluginStop(run_id=p["run_id"], step_id=p["step_id"])
