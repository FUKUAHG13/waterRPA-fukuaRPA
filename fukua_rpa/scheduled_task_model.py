"""Pure, framework-free data model for scheduled tasks.

This module intentionally imports nothing from ``fukua_rpa`` or PySide so it can
be unit-tested without the application runtime and reused by both the
app-level scheduled-task service and the (offline) test suite.

Time model
----------
* User-facing trigger rules describe LOCAL wall-clock time (e.g. ``"09:00"``).
* For persistence and ordering we keep UTC-aware timestamps.
* DST / time-zone jumps are handled by a :class:`ScheduledTaskClock` so the
  model itself stays deterministic and testable. The default clock uses the
  operating system's local time (``datetime.now`` + ``time.mktime``), which is
  DST-correct on Windows. Tests inject a fixed-zone clock.

All "next run" computations return a NAIVE local datetime (the wall-clock time
the user configured). The service converts to UTC for storage/ordering via the
clock, which applies the platform DST rules.
"""

from __future__ import annotations

import calendar
import math
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from enum import StrEnum
from typing import Any

__all__ = [
    "TriggerKind",
    "ScheduleStatus",
    "ScheduledTaskClock",
    "FixedZoneClock",
    "ScheduledTrigger",
    "ScheduledTask",
    "ScheduledRunHistory",
    "new_schedule_id",
    "next_due_local",
    "compute_future_runs",
    "status_for",
    "CONFLICT_SKIP",
    "CONFLICT_CATCHUP_ONCE",
    "MISSED_SKIP",
    "MISSED_CATCHUP_ONCE",
    "MISSED_ASK",
    "SCHEDULE_SCHEMA_VERSION",
]


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class TriggerKind(StrEnum):
    """Kinds of schedule triggers supported by the planner.

    Values are persisted as their string form, so they double as the JSON
    serialization of ``ScheduledTrigger.kind``.
    """

    SINGLE = "single"
    INTERVAL = "interval"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    STARTUP = "startup"


class ScheduleStatus(StrEnum):
    """Display/lifecycle status of a scheduled task.

    Computed from static facts by :func:`status_for` and refined with runtime
    state (RUNNING / PENDING_CATCHUP / LAST_RUN_FAILED) by the service.
    """

    DISABLED = "disabled"
    WAITING = "waiting"
    READY = "ready"
    RUNNING = "running"
    PENDING_CATCHUP = "pending_catchup"
    PROFILE_MISSING = "profile_missing"
    INVALID_CONFIG = "invalid_config"
    PLUGIN_OR_CAPABILITY_MISSING = "plugin_or_capability_missing"
    LAST_RUN_FAILED = "last_run_failed"


# Conflict / missed policies (design §4.3 / §11)
CONFLICT_SKIP = "skip"
CONFLICT_CATCHUP_ONCE = "catchup_once"
MISSED_SKIP = "skip"
MISSED_CATCHUP_ONCE = "catchup_once"
MISSED_ASK = "ask"

SCHEDULE_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Clock abstraction (DST handled by the OS in the default implementation)
# ---------------------------------------------------------------------------

class ScheduledTaskClock:
    """Local-time semantics used by all "next run" computations.

    The default implementation delegates to the operating system's local time,
    which applies the platform DST rules correctly on Windows. Tests substitute
    :class:`FixedZoneClock` to make time computations deterministic.
    """

    def now_local(self) -> datetime:
        """Return the current wall-clock time as a naive local datetime."""
        return datetime.now()

    def to_utc(self, naive_local: datetime) -> datetime:
        """Convert a naive local datetime to an aware UTC datetime.

        Args:
            naive_local: Wall-clock time without tzinfo.

        Returns:
            Timezone-aware UTC datetime corresponding to the local instant.
        """
        epoch = time.mktime(naive_local.timetuple())
        return datetime.fromtimestamp(epoch, tz=UTC)

    def from_utc(self, utc: datetime) -> datetime:
        """Convert an aware UTC datetime to a naive local wall-clock datetime.

        Args:
            utc: Timezone-aware UTC datetime.

        Returns:
            Naive local datetime suitable for comparison with trigger rules.
        """
        return datetime.fromtimestamp(utc.timestamp())


class FixedZoneClock(ScheduledTaskClock):
    """Clock backed by a fixed IANA zone, for deterministic tests."""

    def __init__(self, tz: tzinfo) -> None:
        """Bind the clock to a fixed timezone.

        Args:
            tz: Timezone used to interpret and produce local wall-clock times.
        """
        self.tz = tz

    def now_local(self) -> datetime:
        """Return the current wall-clock time in the fixed zone (naive)."""
        return datetime.now(self.tz).replace(tzinfo=None)

    def to_utc(self, naive_local: datetime) -> datetime:
        """Interpret a naive datetime in the fixed zone and convert to UTC."""
        aware = naive_local.replace(tzinfo=self.tz)
        return aware.astimezone(UTC)

    def from_utc(self, utc: datetime) -> datetime:
        """Convert an aware UTC datetime to a naive datetime in the fixed zone."""
        return utc.astimezone(self.tz).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _parse_local_time(s: str | None):
    if not s:
        return None
    parts = [int(x) for x in str(s).split(":")]
    while len(parts) < 3:
        parts.append(0)
    h, m, sec = parts[0], parts[1], parts[2]
    return datetime.min.time().replace(hour=h, minute=m, second=sec)


def _format_local_time(t) -> str:
    return f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}"


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    s2 = str(s).replace("Z", "+00:00")
    return datetime.fromisoformat(s2).astimezone(UTC)


def _strict_int(value: Any) -> int:
    """Parse an integer, accepting only exact integers.

    An integer-valued float is accepted because it is valid JSON and round-trips
    through :func:`float`; ``bool`` is rejected even though it subclasses ``int``,
    and a string is never coerced (``int("15")`` would hide a malformed payload).
    """
    if isinstance(value, bool):
        raise ValueError(f"必须是整数，收到 bool：{value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ValueError(f"必须是整数，收到 {type(value).__name__}：{value!r}")


def _optional_duration_ms(value: Any) -> int | None:
    """Best-effort coercion of a persisted ``duration_ms`` to ``int | None``.

    FR-20261011-008: history files are read from disk and may be corrupted or
    hand-edited, so a non-numeric ``duration_ms`` must degrade to ``None``
    rather than propagate. Leaving a string here made the scheduled-task log
    window raise ``TypeError`` on ``duration_ms / 1000.0`` during its refresh
    timer. ``bool`` is rejected because it is not a duration.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def _strict_int_list(value: Any) -> frozenset[int]:
    """Parse a collection of weekday indexes.

    A bare string must be rejected outright: iterating ``"12"`` yields characters,
    so accepting it would register Monday and Tuesday instead of rejecting the
    payload. ``None``/absent means "no weekday constraint".
    """
    if value is None:
        return frozenset()
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(
            f"weekdays 必须是整数列表，收到 {type(value).__name__}：{value!r}"
        )
    days = frozenset(_strict_int(item) for item in value)
    # 0=Mon .. 6=Sun. Out-of-range indexes would silently never match, so the
    # weekly task would simply never fire — reject instead.
    out_of_range = sorted(day for day in days if not 0 <= day <= 6)
    if out_of_range:
        raise ValueError(f"weekdays 必须在 0~6 之间，越界值：{out_of_range!r}")
    return days


def _strict_day_of_month(value: Any) -> int:
    """Parse a calendar day, rejecting values no month can have.

    The monthly arithmetic clamps a too-large day to the month's last day, so
    accepting ``32`` would quietly schedule the 28th/29th/30th/31st instead of
    reporting the typo. ``0`` is rejected for the same reason.
    """
    day = _strict_int(value)
    if not 1 <= day <= 31:
        raise ValueError(f"day_of_month 必须在 1~31 之间，收到：{day!r}")
    return day


def _strict_bool(value: Any) -> bool:
    """FR-20260812-D2-002: 严格布尔解析。

    仅接受 Python 布尔、以及字符串 "true"/"false"（大小写不敏感）；其余任何
    值（含 "0"/"yes"/""/数字）一律视为停用，避免 bool("false")==True 把
    手工编辑的非标准数据误判为启用。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return False


def _iso_naive(dt: datetime | None) -> str | None:
    return None if dt is None else dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_naive(s: str | None) -> datetime | None:
    return None if not s else datetime.fromisoformat(str(s))


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------

@dataclass
class ScheduledTrigger:
    """Recurrence rule describing when a scheduled task fires.

    All wall-clock fields are LOCAL naive time; the interval anchor is the only
    UTC-aware field (it must be absolute to survive DST changes). Which fields
    are meaningful depends on ``kind``; irrelevant fields stay at their defaults.

    Attributes:
        kind: Trigger category (single/interval/daily/weekly/monthly/startup).
        local_time: Time of day for daily/weekly/monthly triggers.
        weekdays: Set of weekdays for weekly triggers (0=Mon .. 6=Sun).
        day_of_month: Calendar day for monthly triggers (clamped to month end).
        interval_seconds: Period length for interval triggers.
        anchor_at_utc: Aware UTC anchor the interval sequence is computed from.
        once_at_local: Naive local fire time for single triggers.
        startup_delay_seconds: Delay after app start for startup triggers.
    """

    kind: TriggerKind
    local_time: datetime.time | None = None  # daily / weekly / monthly
    weekdays: frozenset[int] = frozenset()      # weekly: 0=Mon .. 6=Sun
    day_of_month: int | None = None          # monthly
    interval_seconds: int | None = None      # interval
    anchor_at_utc: datetime | None = None    # interval anchor (aware UTC)
    once_at_local: datetime | None = None    # single (naive local)
    startup_delay_seconds: float = 0.0          # startup

    # ----- next occurrence (naive local) -----
    def next_run_local(self, now_local: datetime, clock: ScheduledTaskClock) -> datetime | None:
        """Compute the next fire time strictly after ``now_local``.

        Args:
            now_local: Reference wall-clock time (naive local).
            clock: Clock used for interval anchor conversion.

        Returns:
            Naive local datetime of the next occurrence, or None when the
            trigger is exhausted (past single), fired externally (startup),
            or missing required fields.
        """
        if self.kind == TriggerKind.SINGLE:
            if self.once_at_local is None:
                return None
            return self.once_at_local if self.once_at_local > now_local else None
        if self.kind == TriggerKind.STARTUP:
            return None  # fired by the service at startup, not via calendar
        if self.kind == TriggerKind.INTERVAL:
            return self._next_interval(now_local, clock)
        if self.kind == TriggerKind.DAILY:
            if self.local_time is None:
                return None
            cand = datetime.combine(now_local.date(), self.local_time)
            if cand <= now_local:
                cand += timedelta(days=1)
            return cand
        if self.kind == TriggerKind.WEEKLY:
            return self._next_weekly(now_local)
        if self.kind == TriggerKind.MONTHLY:
            return self._next_monthly(now_local)
        return None

    def _next_interval(self, now_local: datetime, clock: ScheduledTaskClock) -> datetime | None:
        """Next interval tick after ``now_local`` measured from the UTC anchor."""
        if not self.interval_seconds or self.interval_seconds <= 0 or self.anchor_at_utc is None:
            return None
        anchor_local = clock.from_utc(self.anchor_at_utc)
        delta = (now_local - anchor_local).total_seconds()
        if delta <= 0:
            return anchor_local
        steps = int(delta // self.interval_seconds) + 1
        return anchor_local + timedelta(seconds=steps * self.interval_seconds)

    def _next_weekly(self, now_local: datetime) -> datetime | None:
        """Earliest upcoming weekday/time-of-day combination after ``now_local``."""
        if not self.weekdays or self.local_time is None:
            return None
        best: datetime | None = None
        for wd in self.weekdays:
            days_ahead = (wd - now_local.weekday()) % 7
            cand_date = now_local.date() + timedelta(days=days_ahead)
            cand = datetime.combine(cand_date, self.local_time)
            if cand <= now_local:
                cand = datetime.combine(cand_date + timedelta(days=7), self.local_time)
            if best is None or cand < best:
                best = cand
        return best

    def _next_monthly(self, now_local: datetime) -> datetime | None:
        """Next monthly occurrence, clamping day_of_month to short months."""
        if self.day_of_month is None or self.local_time is None:
            return None
        y, m = now_local.year, now_local.month
        for _ in range(13):
            last = calendar.monthrange(y, m)[1]
            d = min(self.day_of_month, last)
            cand = datetime.combine(date(y, m, d), self.local_time)
            if cand > now_local:
                return cand
            m += 1
            if m > 12:
                m = 1
                y += 1
        return None

    # ----- serialization -----
    def to_dict(self) -> dict[str, Any]:
        """Serialize the trigger to a JSON-compatible dict (only set fields)."""
        d: dict[str, Any] = {"kind": self.kind.value}
        if self.local_time is not None:
            d["local_time"] = _format_local_time(self.local_time)
        if self.weekdays:
            d["weekdays"] = sorted(self.weekdays)
        if self.day_of_month is not None:
            d["day_of_month"] = self.day_of_month
        if self.interval_seconds is not None:
            d["interval_seconds"] = self.interval_seconds
        if self.anchor_at_utc is not None:
            d["anchor_at_utc"] = _iso_utc(self.anchor_at_utc)
        if self.once_at_local is not None:
            d["once_at_local"] = _iso_naive(self.once_at_local)
        if self.startup_delay_seconds:
            d["startup_delay_seconds"] = self.startup_delay_seconds
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ScheduledTrigger:
        """Reconstruct a trigger from its dict form.

        Args:
            d: Mapping previously produced by :meth:`to_dict`.

        Returns:
            A new ScheduledTrigger. INTERVAL triggers missing an anchor default
            it to "now" so the sequence starts from load time (see INVARIANT).

        Raises:
            ValueError: If ``kind`` is not a recognized TriggerKind value.
        """
        kind = TriggerKind(d["kind"])
        anchor = _parse_utc(d.get("anchor_at_utc"))
        # INVARIANT: INTERVAL triggers require an anchor to compute the next
        # occurrence. If deserialization yields None (e.g. legacy data or a
        # task created without an explicit anchor), default to now so the
        # interval sequence starts from the moment of loading.
        if kind == TriggerKind.INTERVAL and anchor is None:
            anchor = datetime.now(UTC)
        return cls(
            kind=kind,
            local_time=_parse_local_time(d.get("local_time")),
            weekdays=_strict_int_list(d.get("weekdays")),
            day_of_month=(
                None
                if d.get("day_of_month") is None
                else _strict_day_of_month(d.get("day_of_month"))
            ),
            interval_seconds=d.get("interval_seconds"),
            anchor_at_utc=anchor,
            once_at_local=_parse_naive(d.get("once_at_local")),
            startup_delay_seconds=float(d.get("startup_delay_seconds", 0.0) or 0.0),
        )


# ---------------------------------------------------------------------------
# Scheduled task definition
# ---------------------------------------------------------------------------

@dataclass
class ScheduledTask:
    """Persisted definition of a single scheduled task.

    Combines identity, the target profile snapshot, the recurrence trigger, an
    optional active window (UTC), and the conflict/missed policies applied by
    the service. ``profile_name_snapshot`` is a display-only copy that survives
    profile renames/deletions.

    Attributes:
        schedule_id: Stable unique identifier (UUID string).
        name: User-facing task name.
        profile_id: Target profile id, or None/empty when unassigned.
        profile_name_snapshot: Profile name captured at edit time for display.
        enabled: Whether the service considers this task for scheduling.
        trigger: Recurrence rule.
        active_from: Inclusive start of the active window (aware UTC).
        active_until: Inclusive end of the active window (aware UTC).
        conflict_policy: What to do when a trigger fires during a running task.
        missed_policy: What to do for triggers missed while the app was closed.
        max_runtime_seconds: Optional per-run watchdog limit.
        created_at_utc: Creation timestamp (aware UTC).
        updated_at_utc: Last modification timestamp (aware UTC).
    """

    schedule_id: str
    name: str
    profile_id: str | None
    profile_name_snapshot: str
    enabled: bool
    trigger: ScheduledTrigger
    active_from: datetime | None = None   # UTC aware
    active_until: datetime | None = None  # UTC aware
    conflict_policy: str = CONFLICT_SKIP
    missed_policy: str = MISSED_SKIP
    max_runtime_seconds: int | None = None
    created_at_utc: datetime | None = None
    updated_at_utc: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the task to a JSON-compatible dict with schema_version."""
        return {
            "schema_version": SCHEDULE_SCHEMA_VERSION,
            "schedule_id": self.schedule_id,
            "name": self.name,
            "profile_id": self.profile_id,
            "profile_name_snapshot": self.profile_name_snapshot,
            "enabled": self.enabled,
            "trigger": self.trigger.to_dict(),
            "active_from": _iso_utc(self.active_from),
            "active_until": _iso_utc(self.active_until),
            "conflict_policy": self.conflict_policy,
            "missed_policy": self.missed_policy,
            "max_runtime_seconds": self.max_runtime_seconds,
            "created_at_utc": _iso_utc(self.created_at_utc),
            "updated_at_utc": _iso_utc(self.updated_at_utc),
        }

    @staticmethod
    def _reject_negative_duration(name: str, value: Any) -> Any:
        """Return *value*, or raise if it is a negative duration.

        A negative interval / max-runtime / startup delay is finite, so the store's
        NaN check lets it through, yet it is meaningless and makes the scheduler
        either fire immediately or never fire. Enforced here as well so the
        invariant does not depend on which entry point built the object.

        Raises:
            ValueError: If *value* is a negative number.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value
        if value < 0:
            raise ValueError(f"{name} 不能为负数：{value!r}")
        return value

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ScheduledTask:
        """Reconstruct a task from its dict form.

        Args:
            d: Mapping previously produced by :meth:`to_dict`.

        Returns:
            A new ScheduledTask with defaults applied for absent optional keys.

        Raises:
            KeyError: If a required key (schedule_id/name/trigger) is missing.
            ValueError: If a duration field is negative.
        """
        trigger = ScheduledTrigger.from_dict(d["trigger"])
        cls._reject_negative_duration(
            "trigger.interval_seconds", trigger.interval_seconds
        )
        cls._reject_negative_duration(
            "trigger.startup_delay_seconds", trigger.startup_delay_seconds
        )
        return cls(
            schedule_id=d["schedule_id"],
            name=d["name"],
            profile_id=d.get("profile_id"),
            profile_name_snapshot=d.get("profile_name_snapshot", ""),
            # FR-20260812-D2-002: 严格布尔解析——bool("false")==True 会把
            # 任何非空字符串当启用。写路径恒序列化布尔，仅手工编辑/外部数据
            # 会触发；这里仅接受 True/False/"true"/"false"，其余视为停用。
            enabled=_strict_bool(d.get("enabled", False)),
            trigger=trigger,
            active_from=_parse_utc(d.get("active_from")),
            active_until=_parse_utc(d.get("active_until")),
            conflict_policy=d.get("conflict_policy", CONFLICT_SKIP),
            missed_policy=d.get("missed_policy", MISSED_SKIP),
            max_runtime_seconds=cls._reject_negative_duration(
                "max_runtime_seconds", d.get("max_runtime_seconds")
            ),
            created_at_utc=_parse_utc(d.get("created_at_utc")),
            updated_at_utc=_parse_utc(d.get("updated_at_utc")),
        )


# ---------------------------------------------------------------------------
# Run history (bounded; references only, no script/log body)
# ---------------------------------------------------------------------------

@dataclass
class ScheduledRunHistory:
    """Bounded, reference-only record of a single scheduled run.

    Per design §4.7 this stores only identifiers, timing, and a result summary;
    it never embeds the script, variable values, or log bodies. ``reason`` and
    ``application_log_reference`` point to where full details live elsewhere.

    Attributes:
        history_id: Unique identifier for this history entry.
        schedule_id: Owning scheduled task id.
        run_id: Execution run id (empty for skipped/missed placeholders).
        profile_id: Target profile id at run time.
        due_at_utc: Trigger time the run was scheduled for (aware UTC).
        started_at_utc: Actual start time (aware UTC), if it ran.
        ended_at_utc: Completion time (aware UTC), if it finished.
        duration_ms: Wall-clock run duration in milliseconds.
        outcome: Terminal result tag (success/skipped/watchdog_timeout/...).
        reason: Machine-readable reason for skip/miss outcomes.
        application_log_reference: Pointer to the full application log.
    """

    history_id: str
    schedule_id: str
    run_id: str
    profile_id: str | None
    due_at_utc: datetime | None
    started_at_utc: datetime | None = None
    ended_at_utc: datetime | None = None
    duration_ms: int | None = None
    outcome: str = ""
    reason: str = ""
    application_log_reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize the history record to a JSON-compatible dict."""
        return {
            "history_id": self.history_id,
            "schedule_id": self.schedule_id,
            "run_id": self.run_id,
            "profile_id": self.profile_id,
            "due_at_utc": _iso_utc(self.due_at_utc),
            "started_at_utc": _iso_utc(self.started_at_utc),
            "ended_at_utc": _iso_utc(self.ended_at_utc),
            "duration_ms": self.duration_ms,
            "outcome": self.outcome,
            "reason": self.reason,
            "application_log_reference": self.application_log_reference,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ScheduledRunHistory:
        """Reconstruct a history record from its dict form.

        Args:
            d: Mapping previously produced by :meth:`to_dict`.

        Raises:
            KeyError: If a required key (history_id/schedule_id/run_id) is missing.
        """
        return cls(
            history_id=d["history_id"],
            schedule_id=d["schedule_id"],
            run_id=d["run_id"],
            profile_id=d.get("profile_id"),
            due_at_utc=_parse_utc(d.get("due_at_utc")),
            started_at_utc=_parse_utc(d.get("started_at_utc")),
            ended_at_utc=_parse_utc(d.get("ended_at_utc")),
            duration_ms=_optional_duration_ms(d.get("duration_ms")),
            outcome=d.get("outcome", ""),
            reason=d.get("reason", ""),
            application_log_reference=d.get("application_log_reference", ""),
        )


# ---------------------------------------------------------------------------
# Free functions
# ---------------------------------------------------------------------------

def new_schedule_id() -> str:
    """Generate a fresh, globally unique schedule identifier (UUID4 string)."""
    return str(uuid.uuid4())


def next_due_local(
    task: ScheduledTask, now_local: datetime, clock: ScheduledTaskClock
) -> datetime | None:
    """Compute the next wall-clock run time for an enabled task.

    Applies the active-from / active-until window. STARTUP triggers return
    None here (the service fires them at startup). Disabled tasks return None.

    Args:
        task: Scheduled task definition to evaluate.
        now_local: Reference wall-clock time (naive local).
        clock: Clock used for trigger and window conversions.

    Returns:
        Naive local datetime of the next due trigger, or None when the task is
        disabled, exhausted, outside its active window, or startup-triggered.
    """
    if not task.enabled:
        return None
    n = task.trigger.next_run_local(now_local, clock)
    if n is None:
        return None
    utc = clock.to_utc(n)
    if task.active_from is not None and utc < task.active_from:
        return None
    if task.active_until is not None and utc > task.active_until:
        return None
    return n


def compute_future_runs(
    trigger: ScheduledTrigger,
    now_local: datetime,
    clock: ScheduledTaskClock,
    count: int = 5,
) -> list[datetime]:
    """Preview up to ``count`` future occurrences of a trigger.

    Pure preview — never executes anything. Used by the editor to show the
    user when a newly configured trigger will next fire.

    Args:
        trigger: Recurrence rule to project forward.
        now_local: Reference wall-clock time (naive local).
        clock: Clock used for trigger conversions.
        count: Maximum number of occurrences to return.

    Returns:
        List of naive local datetimes, in ascending order, possibly shorter
        than ``count`` when the trigger is exhausted.
    """
    runs: list[datetime] = []
    cursor = now_local
    for _ in range(count):
        n = trigger.next_run_local(cursor, clock)
        if n is None:
            break
        runs.append(n)
        cursor = n + timedelta(seconds=1)
    return runs


def status_for(
    task: ScheduledTask,
    profile_exists: bool,
    config_valid: bool,
    plugin_ok: bool,
    running: bool = False,
) -> ScheduleStatus:
    """Derive the base status from static facts about a task.

    The service refines this with runtime state (RUNNING / PENDING_CATCHUP /
    LAST_RUN_FAILED); this function only encodes the precedence of static
    health conditions.

    Args:
        task: Scheduled task definition to evaluate.
        profile_exists: Whether the target profile still exists.
        config_valid: Whether the profile configuration passes validation.
        plugin_ok: Whether required plugins/capabilities are available.
        running: Whether the task is currently executing.

    Returns:
        The highest-precedence applicable ScheduleStatus.
    """
    if not task.enabled:
        return ScheduleStatus.DISABLED
    if not profile_exists:
        return ScheduleStatus.PROFILE_MISSING
    if not config_valid:
        return ScheduleStatus.INVALID_CONFIG
    if not plugin_ok:
        return ScheduleStatus.PLUGIN_OR_CAPABILITY_MISSING
    if running:
        return ScheduleStatus.RUNNING
    return ScheduleStatus.WAITING
