"""Bounded, privacy-filtered detailed logs for scheduled runs."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import threading
from typing import Any

from .io_utils import atomic_write_text
from .logging_service import SCHEDULED_RUN_LOG_SOURCE, ProcessedLogEvent

MAX_SCHEDULE_LOG_FILE_BYTES = 8 * 1024 * 1024
SCHEDULE_LOG_COMPACT_BYTES = 6 * 1024 * 1024
MAX_SCHEDULE_LOG_MESSAGE_CHARS = 32 * 1024
MAX_SCHEDULE_LOG_QUERY_ENTRIES = 5000
MAX_SCHEDULE_LOG_CACHE_ENTRIES = 50000
MAX_SCHEDULE_LOG_DETAIL_CHARS = 1024
MAX_SCHEDULE_LOG_ID_CHARS = 128

_DETAIL_KEYS = frozenset(
    {"event", "log_category", "step_index", "action_id", "duration_ms"}
)


def _normalize_log_id(value: object) -> str:
    text = str(value or "").strip()
    if len(text) <= MAX_SCHEDULE_LOG_ID_CHARS:
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:24]
    prefix_chars = MAX_SCHEDULE_LOG_ID_CHARS - len(digest) - 1
    return f"{text[:prefix_chars]}#{digest}"


class ScheduledTaskLogStore:
    """Append-only JSONL store capped by bytes and queried by schedule/run id.

    Only :class:`ProcessedLogEvent` values are accepted, so the selected
    privacy policy has already been applied by ``logging_service``.  The store
    is independent of the optional global file-log switch because the plan
    window explicitly promises a local execution history.  Capacity is bounded
    and the existing "清除运行历史" command clears this file as well.
    """

    def __init__(
        self,
        path: str,
        *,
        max_bytes: int = MAX_SCHEDULE_LOG_FILE_BYTES,
        compact_bytes: int = SCHEDULE_LOG_COMPACT_BYTES,
    ):
        self.path = os.path.abspath(path)
        self.max_bytes = max(1024, int(max_bytes))
        self.compact_bytes = max(
            512,
            min(int(compact_bytes), self.max_bytes - 1),
        )
        self._lock = threading.RLock()
        self._cache: list[dict[str, Any]] | None = None
        self._revision = 0

    def append_processed(self, event: ProcessedLogEvent) -> bool:
        context = event.context if isinstance(event.context, dict) else {}
        if context.get("log_source") != SCHEDULED_RUN_LOG_SOURCE:
            return False
        schedule_id = _normalize_log_id(context.get("schedule_id"))
        if not schedule_id:
            return False
        run_id = _normalize_log_id(context.get("run_id"))
        structured = event.structured if isinstance(event.structured, dict) else {}
        details = {}
        for key, value in structured.items():
            if key not in _DETAIL_KEYS or not isinstance(
                value, (str, int, float, bool, type(None))
            ):
                continue
            if isinstance(value, float) and not math.isfinite(value):
                continue
            if isinstance(value, str):
                value = value[:MAX_SCHEDULE_LOG_DETAIL_CHARS]
            details[str(key)] = value
        message = str(event.message)
        if len(message) > MAX_SCHEDULE_LOG_MESSAGE_CHARS:
            message = message[:MAX_SCHEDULE_LOG_MESSAGE_CHARS] + "……（已截断）"
        payload = {
            "timestamp": str(event.timestamp),
            "event_time_ns": int(event.event_time_ns),
            "schedule_id": schedule_id,
            "run_id": run_id,
            "message": message,
            "details": details,
        }
        line = (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )
        encoded_size = len(line.encode("utf-8"))
        with self._lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            current_size = self._safe_size()
            if current_size + encoded_size > self.max_bytes:
                self._compact_locked()
            try:
                with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
            except OSError:
                return False
            if self._cache is not None:
                self._cache.append(payload)
                self._cache = self._cache[-MAX_SCHEDULE_LOG_CACHE_ENTRIES:]
            self._revision += 1
        return True

    def entries_for(
        self,
        schedule_id: str,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        target_schedule = _normalize_log_id(schedule_id)
        target_run = None if run_id is None else _normalize_log_id(run_id)
        if not target_schedule:
            return []
        with self._lock:
            entries = self._load_cache_locked()
        result: list[dict[str, Any]] = []
        for item in entries:
            if str(item.get("schedule_id") or "") != target_schedule:
                continue
            if target_run is not None and str(item.get("run_id") or "") != target_run:
                continue
            result.append(item)
        return result[-MAX_SCHEDULE_LOG_QUERY_ENTRIES:]

    def clear(self) -> None:
        with self._lock:
            with contextlib.suppress(OSError):
                os.remove(self.path)
            self._cache = []
            self._revision += 1

    def revision(self) -> int:
        """Return an in-process change counter for low-cost UI polling."""

        with self._lock:
            return self._revision

    def _load_cache_locked(self) -> list[dict[str, Any]]:
        if self._cache is not None:
            return list(self._cache)
        try:
            size = os.path.getsize(self.path)
            if size > self.max_bytes + MAX_SCHEDULE_LOG_MESSAGE_CHARS * 2:
                self._compact_locked()
            with open(self.path, encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            self._cache = []
            return []
        entries: list[dict[str, Any]] = []
        for line in lines:
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
                # RecursionError：超深嵌套损坏行不得中断日志缓存重建（SEC-FR-001 同族）。
                continue
            if isinstance(item, dict):
                entries.append(item)
        self._cache = entries[-MAX_SCHEDULE_LOG_CACHE_ENTRIES:]
        return list(self._cache)

    def _safe_size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def _compact_locked(self) -> None:
        try:
            with open(self.path, "rb") as handle:
                data = handle.read()
        except OSError:
            return
        if len(data) <= self.compact_bytes:
            return
        start = max(0, len(data) - self.compact_bytes)
        if start:
            newline = data.find(b"\n", start)
            start = len(data) if newline < 0 else newline + 1
        retained = data[start:].decode("utf-8", errors="ignore")
        try:
            atomic_write_text(self.path, retained, newline="\n")
        except OSError:
            # Logging is best effort; a failed compaction must not stop a run.
            return
        self._cache = None
