"""Non-blocking file/UI logging and the process-level exception hook."""

import os
import queue
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from .log_policy import StructuredJsonFormatter
from .paths import get_log_path
from .privacy import (
    plain_log_text,
    register_private_path_root,
    safe_exception_report,
)
from .timing_constants import LOG_FLUSH_INTERVAL_S

# INVARIANT: GLOBAL_CONFIG has a fixed set of keys (bounded). Under the
# CPython GIL, dict.update() and the dict() snapshot are atomic for single-key
# writes/reads. No explicit lock needed unless migrating to free-threaded
# Python (3.13+).
GLOBAL_CONFIG = {
    "log_to_file": False,
    "log_to_ui": True,
    "privacy_protected": True,
    # Optional structured JSON Lines output (design §7 / v1.0.62 structured
    # logging). When enabled, the worker writes one JSON object per record to
    # rpa_debug_log.jsonl via StructuredJsonFormatter. It never changes the
    # plain-text file or the UI panel output.
    "log_to_jsonl": False,
    # A5: the active LogPolicy, published by whichever layer owns the setting
    # (the settings UI). Producers that run outside the UI read it from here so
    # that "file output" and "UI output" apply one identical filtering rule
    # instead of two that can drift.
    "active_log_policy": None,
}


def publish_log_policy(policy) -> None:
    """Publish *policy* as the active filtering rule for non-UI producers."""
    GLOBAL_CONFIG["active_log_policy"] = policy


def active_log_policy():
    """Return the published active policy, or ``None`` when none is published.

    ``None`` means "no policy known here" — callers must then allow the message
    rather than silently dropping it.
    """
    policy = GLOBAL_CONFIG.get("active_log_policy")
    return policy if hasattr(policy, "allows") else None


def get_log_config() -> dict:
    """Return a consistent snapshot of the current config (atomic under GIL)."""
    return dict(GLOBAL_CONFIG)


def update_log_config(**changes) -> None:
    """Update GLOBAL_CONFIG in place. Thread-safe for individual key writes under GIL."""
    GLOBAL_CONFIG.update(changes)


LOG_QUEUE_MAX_RECORDS = 4096
MAX_LOG_MESSAGE_CHARS = 64 * 1024
LOG_QUEUE = queue.Queue(maxsize=LOG_QUEUE_MAX_RECORDS)


def _development_log_base_dir() -> str | None:
    if getattr(sys, "frozen", False):
        return None
    return os.path.join(
        tempfile.gettempdir(),
        "fukuaRPA-development",
        f"process-{os.getpid()}",
    )


_DEFAULT_LOG_BASE_DIR = _development_log_base_dir()
_LOG_BASE_DIR = _DEFAULT_LOG_BASE_DIR
_LOG_WRITE_LOCK = threading.Lock()
_LOG_DROP_LOCK = threading.Lock()
_LOG_SINK_LOCK = threading.Lock()
_LOG_SINKS: set = set()
_LOG_DROPPED_TOTAL = 0
_LOG_DROPPED_PENDING = 0
_LOG_LAST_DROP_NOTICE = 0.0
_LOG_SINK_FAILURES = 0
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
SCHEDULED_RUN_LOG_SOURCE = "scheduled_run"


# Optional process-wide diagnostic context retained for public compatibility.
# Runner attribution MUST use build_run_log_context and pass the result
# explicitly: a process-global schedule_id would incorrectly label concurrent
# UI, hotkey and settings logs as part of the active plan.
_LOG_CONTEXT: dict = {}


def set_log_context(**kwargs) -> None:
    """Attach structured-context tags that subsequent log lines will carry."""
    _LOG_CONTEXT.update({str(k): v for k, v in kwargs.items()})


def clear_log_context() -> None:
    """Remove all structured-context tags."""
    _LOG_CONTEXT.clear()


# retained (P7-1): no in-repo callers; public introspection API surface.
def current_log_context() -> dict:
    """Return a snapshot of the current structured-context tags."""
    return dict(_LOG_CONTEXT)


def build_run_log_context(run_meta: Mapping | None) -> dict:
    """Build explicit attribution for logs produced by one scheduled run.

    Profile hotkeys also carry a ``schedule_id`` compatibility field in their
    run metadata, so the source discriminator is mandatory.  Returning an empty
    mapping for every non-scheduled run prevents unrelated UI/application logs
    from being swept into a plan merely because another run is active.
    """

    if not isinstance(run_meta, Mapping):
        return {}
    if str(run_meta.get("source") or "") != "scheduled":
        return {}
    schedule_id = str(run_meta.get("schedule_id") or "").strip()
    run_id = str(run_meta.get("run_id") or "").strip()
    if not schedule_id or not run_id:
        return {}
    return {
        "log_source": SCHEDULED_RUN_LOG_SOURCE,
        "schedule_id": schedule_id,
        "run_id": run_id,
    }


@dataclass(frozen=True)
class LogRecord:
    message: str
    private_message: str | None
    callback: object
    event_time_ns: int
    ui_timestamp: bool = False
    privacy_protected: bool = True
    # Design §7: structured tracing context (e.g. schedule_id / plugin_id).
    # A plain dict is safe here because each record gets its own copy at
    # enqueue time (see write_log), so later mutations never retroactively
    # change an already-queued record.
    context: dict = field(default_factory=dict)
    # Machine-readable extra fields (e.g. step_index / action_id / duration_ms)
    # emitted by the engine execution path. Only consumed by the optional
    # structured JSONL output; never affects the plain-text file or UI panel.
    structured: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessedLogEvent:
    """Privacy-filtered event delivered to optional local log sinks."""

    message: str
    timestamp: str
    event_time_ns: int
    context: dict = field(default_factory=dict)
    structured: dict = field(default_factory=dict)


def register_processed_log_sink(callback) -> None:
    """Register a bounded local consumer for already-sanitized log events."""

    if not callable(callback):
        raise TypeError("日志接收器必须可调用")
    with _LOG_SINK_LOCK:
        _LOG_SINKS.add(callback)


def unregister_processed_log_sink(callback) -> None:
    """Remove a previously registered processed-log consumer."""

    with _LOG_SINK_LOCK:
        _LOG_SINKS.discard(callback)


def _deliver_processed_log_event(event: ProcessedLogEvent) -> None:
    global _LOG_SINK_FAILURES
    with _LOG_SINK_LOCK:
        sinks = tuple(_LOG_SINKS)
    for sink in sinks:
        try:
            sink(event)
        except Exception:
            # Optional sinks must never break the main asynchronous logger.
            with _LOG_DROP_LOCK:
                _LOG_SINK_FAILURES += 1


def _bounded_log_text(value) -> str:
    text = str(value)
    if len(text) <= MAX_LOG_MESSAGE_CHARS:
        return text
    suffix = "\n……日志内容过长，已在入队前截断"
    return text[: MAX_LOG_MESSAGE_CHARS - len(suffix)] + suffix


def _record_log_drop() -> None:
    global _LOG_DROPPED_TOTAL, _LOG_DROPPED_PENDING
    with _LOG_DROP_LOCK:
        _LOG_DROPPED_TOTAL += 1
        _LOG_DROPPED_PENDING += 1


def _take_drop_notice(*, force=False) -> int:
    global _LOG_DROPPED_PENDING, _LOG_LAST_DROP_NOTICE
    now = time.monotonic()
    with _LOG_DROP_LOCK:
        if not _LOG_DROPPED_PENDING:
            return 0
        if not force and now - _LOG_LAST_DROP_NOTICE < 1.0:
            return 0
        dropped = _LOG_DROPPED_PENDING
        _LOG_DROPPED_PENDING = 0
        _LOG_LAST_DROP_NOTICE = now
        return dropped


def log_queue_stats() -> dict[str, int]:
    with _LOG_DROP_LOCK:
        dropped_total = _LOG_DROPPED_TOTAL
        dropped_pending = _LOG_DROPPED_PENDING
        sink_failures = _LOG_SINK_FAILURES
    return {
        "capacity": LOG_QUEUE_MAX_RECORDS,
        "queued": LOG_QUEUE.qsize(),
        "unfinished": int(LOG_QUEUE.unfinished_tasks),
        "dropped_total": dropped_total,
        "dropped_pending_notice": dropped_pending,
        "sink_failures": sink_failures,
    }


def _enqueue_log_record(record, target_queue=None) -> bool:
    destination = LOG_QUEUE if target_queue is None else target_queue
    try:
        destination.put_nowait(record)
        return True
    except queue.Full:
        _record_log_drop()
        return False


def format_local_timestamp(event_time_ns=None) -> str:
    """Format the local wall-clock time captured when an event occurred."""
    timestamp_ns = time.time_ns() if event_time_ns is None else int(event_time_ns)
    seconds = timestamp_ns // 1_000_000_000
    milliseconds = (timestamp_ns // 1_000_000) % 1000
    return f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(seconds))}.{milliseconds:03d}"


def relay_processed_log_event(event: ProcessedLogEvent) -> None:
    """Deliver a child-Runner event to local sinks without duplicating UI/files.

    The child has already selected its privacy-safe text, but the parent applies
    its current privacy setting once more so enabling protection mid-run cannot
    expose a path that was sent moments earlier.  This relay intentionally does
    not enqueue another file/UI record; its sole purpose is preserving explicit
    structured attribution across the local Runner IPC boundary.
    """

    if not isinstance(event, ProcessedLogEvent):
        raise TypeError("转发日志必须是 ProcessedLogEvent")
    try:
        event_time_ns = int(event.event_time_ns)
    except (TypeError, ValueError, OverflowError):
        event_time_ns = time.time_ns()
    privacy_protected = bool(get_log_config().get("privacy_protected", True))
    safe_message = plain_log_text(
        _bounded_log_text(event.message),
        redact_paths=privacy_protected,
    )
    timestamp = str(event.timestamp or "").strip() or format_local_timestamp(
        event_time_ns
    )
    _deliver_processed_log_event(
        ProcessedLogEvent(
            message=safe_message,
            timestamp=timestamp,
            event_time_ns=event_time_ns,
            context=dict(event.context) if isinstance(event.context, dict) else {},
            structured=(
                dict(event.structured) if isinstance(event.structured, dict) else {}
            ),
        )
    )


def set_log_base_dir(base_dir) -> None:
    global _LOG_BASE_DIR
    _LOG_BASE_DIR = (
        os.path.abspath(base_dir) if base_dir else _DEFAULT_LOG_BASE_DIR
    )
    register_private_path_root(_LOG_BASE_DIR)


def current_log_path() -> str:
    return get_log_path(_LOG_BASE_DIR)


def current_structured_log_path() -> str:
    """Return the path of the optional structured JSON Lines log file."""
    text_path = get_log_path(_LOG_BASE_DIR)
    root, _ext = os.path.splitext(text_path)
    return root + ".jsonl"


# Single shared formatter instance: it is stateless and thread-safe for format().
_JSON_FORMATTER = StructuredJsonFormatter()


def _rotate_log(path) -> None:
    # Log rotation is a non-critical path: partial completion (e.g. some
    # backups shifted but the final replace failing) is acceptable and any
    # OSError is swallowed by the caller. Losing one rotation is preferred
    # over letting logging I/O break the running workflow.
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < MAX_LOG_BYTES:
            return
        for index in range(LOG_BACKUP_COUNT, 0, -1):
            source = f"{path}.{index}"
            if index == LOG_BACKUP_COUNT and os.path.exists(source):
                os.remove(source)
            elif os.path.exists(source):
                os.replace(source, f"{path}.{index + 1}")
        os.replace(path, f"{path}.1")
    except OSError:
        pass


_WRITE_ERRORS = 0
_LAST_FLUSH_ERRORS = 0


def _append_direct(formatted) -> None:
    path = current_log_path()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with _LOG_WRITE_LOCK:
            _rotate_log(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(formatted + "\n")
    except OSError:
        # 写盘失败必须可见：否则 flush_logs 只看队列长度会误报「已落盘」。
        global _WRITE_ERRORS
        _WRITE_ERRORS += 1


def _append_jsonl(record, safe_message) -> None:
    """Append one structured JSON object for ``record`` to the JSONL file.

    The record's structured ``extra`` fields (e.g. step_index / action_id /
    duration_ms) and tracing context are attached to a standard LogRecord so
    ``StructuredJsonFormatter`` can read them via ``getattr``. Failures are
    contained exactly like the plain-text writer: structured logging must never
    break the running workflow.
    """
    import logging as _logging

    path = current_structured_log_path()
    fields = {}
    if isinstance(record.context, dict):
        fields.update(record.context)
    if isinstance(getattr(record, "structured", None), dict):
        fields.update(record.structured)
    try:
        log_record = _logging.LogRecord(
            name="fukua_rpa",
            level=_logging.INFO,
            pathname=__file__,
            lineno=0,
            msg=safe_message,
            args=None,
            exc_info=None,
        )
        log_record.created = record.event_time_ns / 1_000_000_000
        for key, value in fields.items():
            # Never clobber standard LogRecord attributes (e.g. "message").
            if not hasattr(log_record, key):
                setattr(log_record, key, value)
        formatted = _JSON_FORMATTER.format(log_record)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with _LOG_WRITE_LOCK:
            _rotate_log(path)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(formatted + "\n")
    except OSError:
        # JSONL 与文本日志共用同一份「写失败可见」契约：只吞异常会让
        # flush_logs 在 JSONL 写失败时仍报 True（Codex 诊断 §4 实测）。
        global _WRITE_ERRORS
        _WRITE_ERRORS += 1
    except (ValueError, TypeError):
        pass


def _log_worker() -> None:
    while True:
        try:
            item = LOG_QUEUE.get()
            if item is None:
                return
            record = item
            cfg = get_log_config()
            dropped = _take_drop_notice(force=LOG_QUEUE.empty())
            if dropped:
                notice = f"日志积压超过容量，已丢弃 {dropped} 条日志；脚本执行未被阻塞。"
                notice_timestamp = format_local_timestamp()
                if cfg["log_to_file"]:
                    _append_direct(f"[{notice_timestamp}] {notice}")
                if record.callback and cfg["log_to_ui"]:
                    record.callback(
                        f"[{notice_timestamp}] {notice}"
                        if record.ui_timestamp
                        else notice
                    )
            timestamp = format_local_timestamp(record.event_time_ns)
            # SAFETY: disabling protection must never reveal an older protected
            # item; re-enabling it immediately protects anything still queued.
            privacy_protected = record.privacy_protected or bool(
                cfg.get("privacy_protected", True)
            )
            selected_message = (
                record.message
                if privacy_protected or record.private_message is None
                else record.private_message
            )
            safe_message = plain_log_text(
                selected_message,
                redact_paths=privacy_protected,
            )
            formatted = f"[{timestamp}] {safe_message}"
            _deliver_processed_log_event(
                ProcessedLogEvent(
                    message=safe_message,
                    timestamp=timestamp,
                    event_time_ns=record.event_time_ns,
                    context=dict(record.context),
                    structured=dict(record.structured),
                )
            )
            if cfg["log_to_file"]:
                _append_direct(formatted)
            if cfg.get("log_to_jsonl", False):
                _append_jsonl(record, safe_message)
            if record.callback and cfg["log_to_ui"]:
                display_message = safe_message
                if record.ui_timestamp:
                    display_message = f"[{timestamp}] {display_message}"
                record.callback(display_message)
        except Exception:
            # Logging must never crash the automation or recursively log itself.
            _record_log_drop()
        finally:
            LOG_QUEUE.task_done()


LOG_THREAD = threading.Thread(target=_log_worker, daemon=True, name="fukuaRPA-log")
LOG_THREAD.start()


def write_log(
    message,
    callback=None,
    *,
    private_message=None,
    ui_timestamp=False,
    event_time_ns=None,
    context=None,
    structured=None,
    category=None,
    critical=False,
) -> bool:
    """Queue one log record, applying the category policy **before** enqueue.

    A5 (maintainer ruling 2026-10-07): category and severity are recorded
    separately. ``category`` selects the user's filter; ``critical`` marks a real
    error, emergency stop or severe warning and always bypasses that filter.

    ``category=None`` (the default) keeps the pre-A5 behaviour of enqueueing
    unconditionally, so migrating call sites incrementally can never drop a
    message that has not been classified yet.
    """
    privacy_protected = bool(get_log_config().get("privacy_protected", True))
    bounded = _bounded_log_text(message)
    if category is not None and not critical:
        # INVARIANT: the filter runs here, before the record is queued, so a
        # disabled category costs nothing downstream (ARCHITECTURE #41).
        policy = active_log_policy()
        if policy is not None and not policy.allows(category, message=bounded):
            return False
    try:
        timestamp_ns = (
            time.time_ns() if event_time_ns is None else int(event_time_ns)
        )
    except (TypeError, ValueError, OverflowError):
        timestamp_ns = time.time_ns()
    # Merge the process-wide tracing context with any per-call context. A copy
    # is taken so an already-queued record is never mutated later.
    merged_context = dict(_LOG_CONTEXT)
    if isinstance(context, dict):
        merged_context.update(context)
    # Defensive copy: an already-queued record must never be mutated later.
    structured_fields = dict(structured) if isinstance(structured, dict) else {}
    return _enqueue_log_record(
        LogRecord(
            message=bounded,
            private_message=(
                # SAFETY: do not even retain the private variant in memory while
                # protection is enabled. A later opt-out cannot reveal old data.
                None
                if privacy_protected or private_message is None
                else _bounded_log_text(private_message)
            ),
            callback=callback,
            event_time_ns=timestamp_ns,
            ui_timestamp=bool(ui_timestamp),
            privacy_protected=privacy_protected,
            context=merged_context,
            structured=structured_fields,
        )
    )


def flush_logs(timeout=2.0) -> bool:
    """Drain the queue and report whether the channel looked healthy.

    契约（2026-10-21 与诊断对齐）：
    - 返回 False 表示「队列未在超时内排空」**或**「自上次 flush 以来观察到写失败」；
    - 返回 True **不**代表某条具体记录已落盘：通道关闭（`log_to_file=False`）时
      本来就没有写入请求，也会返回 True；
    - 它不提供持久化回执，也不保证物理刷盘。需要「某条关键记录确已写入」的调用方
      不能依赖本函数，只能做尽力而为并如实记录不确定性。
    """
    # 写入是异步的：错误通常发生在 flush 调用**之前**，用「调用前快照」比不出来。
    # 改为与上一次 flush 时的计数比较，即「自上次 flush 以来是否有写失败」。
    global _LAST_FLUSH_ERRORS
    deadline = time.monotonic() + max(0.0, float(timeout))
    while LOG_QUEUE.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(LOG_FLUSH_INTERVAL_S)
    errors_since_last = _WRITE_ERRORS != _LAST_FLUSH_ERRORS
    _LAST_FLUSH_ERRORS = _WRITE_ERRORS
    return LOG_QUEUE.unfinished_tasks == 0 and not errors_since_last


def global_exception_handler(exception_type, value, traceback_object) -> None:
    details = safe_exception_report(exception_type, traceback_object)
    message = f"!!! 严重崩溃 !!!\n{details}"
    formatted = f"[{format_local_timestamp()}] {message}"
    _append_direct(formatted)
    # 仅向 stderr 输出已脱敏的安全报告（safe_exception_report 已剥离凭据/路径等
    # 敏感片段）；不再回退到 sys.__excepthook__，避免完整 traceback 泄露到控制台。
    sys.stderr.write(formatted + "\n")


def threading_exception_handler(args) -> None:
    global_exception_handler(args.exc_type, args.exc_value, args.exc_traceback)


def install_global_exception_handler() -> None:
    sys.excepthook = global_exception_handler
    threading.excepthook = threading_exception_handler


install_global_exception_handler()
