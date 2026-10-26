"""Convert low-level hook events into compact, replayable workflow tasks."""

from __future__ import annotations

import heapq
import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .commands import COMMAND_BY_NAME
from .constants import (
    TASK_TYPE_INPUT_TIMELINE,
    TASK_TYPE_LEFT_DRAG,
    TASK_TYPE_RIGHT_DRAG,
    TASK_TYPE_SCROLL,
    TASK_TYPE_WAIT,
)

logger = logging.getLogger(__name__)

# Persisted command codes for recorded click/key events, derived from the
# single source of truth in commands.py COMMAND_SPECS.
TASK_TYPE_LEFT_CLICK = COMMAND_BY_NAME["左键单击"].code
TASK_TYPE_LEFT_DOUBLE_CLICK = COMMAND_BY_NAME["左键双击"].code
TASK_TYPE_RIGHT_CLICK = COMMAND_BY_NAME["右键单击"].code
TASK_TYPE_SYSTEM_KEY = COMMAND_BY_NAME["系统按键"].code

# Polymorphic event payload discriminated by RecordedEvent.kind:
#   "left"/"left_double"/"right" → coordinate pair (x, y)
#   "left_drag"/"right_drag"    → coordinate quad (x1, y1, x2, y2)
#   "scroll"                    → signed step count
#   "key"/"hotkey"              → key name string
type RecordedEventValue = Sequence[float] | int | str


@dataclass(frozen=True)
class RecordedEvent:
    """Normalized timestamped recording event.

    Represents one semantic input action (click, double-click, scroll,
    drag, or key) with its start/end timestamps and payload value.
    The ``kind`` field discriminates the shape of ``value``; see
    ``RecordedEventValue`` for the expected payload types.
    """

    started: float
    kind: str
    value: RecordedEventValue
    ended: float


def _event(value: object) -> RecordedEvent:
    """Coerce a raw event tuple or RecordedEvent into a RecordedEvent."""
    if isinstance(value, RecordedEvent):
        return value
    if len(value) >= 4:  # type: ignore[arg-type]
        started, kind, payload, ended = value[:4]  # type: ignore[index]
    else:
        started, kind, payload = value[:3]  # type: ignore[index,misc]
        ended = started
    started = float(started)
    ended = max(started, float(ended))
    return RecordedEvent(started, str(kind), payload, ended)


def _near(first: object, second: object, tolerance: int = 5) -> bool:
    """Return True if two coordinate pairs are within *tolerance* pixels."""
    try:
        return (
            abs(int(first[0]) - int(second[0])) <= tolerance  # type: ignore[index]
            and abs(int(first[1]) - int(second[1])) <= tolerance  # type: ignore[index]
        )
    except (TypeError, ValueError, IndexError):
        return False


def _scroll_steps(raw_delta: object) -> int:
    """Convert a raw wheel delta into a signed scroll step count."""
    try:
        value = int(raw_delta)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    if value == 0:
        return 0
    return int(math.copysign(max(1, round(abs(value) / 120)), value))


def aggregate_recorded_events(
    events: Iterable[object],
    *,
    double_click_seconds: float = 0.5,
    scroll_merge_seconds: float = 0.25,
) -> list[RecordedEvent]:
    """Merge raw hook events into semantically meaningful units.

    Two nearby left clicks within *double_click_seconds* are collapsed
    into a single ``left_double`` event. Consecutive scroll ticks within
    *scroll_merge_seconds* are summed into one ``scroll`` event with a
    combined step count.

    Args:
        events: Iterable of raw event tuples or RecordedEvent instances.
        double_click_seconds: Maximum gap between clicks to form a double.
        scroll_merge_seconds: Maximum gap between scrolls to merge.

    Returns:
        Time-ordered list of aggregated RecordedEvent instances.
    """
    ordered = sorted((_event(item) for item in events), key=lambda item: item.started)
    result: list[RecordedEvent] = []
    index = 0
    while index < len(ordered):
        current = ordered[index]
        if current.kind == "left" and index + 1 < len(ordered):
            following = ordered[index + 1]
            if (
                following.kind == "left"
                and following.started - current.ended <= double_click_seconds
                and _near(current.value, following.value)
            ):
                result.append(
                    RecordedEvent(
                        current.started,
                        "left_double",
                        following.value,
                        following.ended,
                    )
                )
                index += 2
                continue
        if current.kind == "scroll":
            total = _scroll_steps(current.value)
            ended = current.ended
            cursor = index + 1
            while cursor < len(ordered):
                following = ordered[cursor]
                if (
                    following.kind != "scroll"
                    or following.started - ended > scroll_merge_seconds
                ):
                    break
                total += _scroll_steps(following.value)
                ended = following.ended
                cursor += 1
            if total:
                result.append(RecordedEvent(current.started, "scroll", total, ended))
            index = cursor
            continue
        result.append(current)
        index += 1
    return result


MAX_RECORDED_EVENTS = 50_000


def _raw_event_dicts(raw_events):
    """Normalize raw hook records into timestamped dictionaries.

    Raw records are produced by ``HookThread`` as small lists so the hook
    callback stays cheap; this converts them to the persisted shape.
    Unknown or malformed entries are skipped rather than guessed.
    """
    normalized = []
    for item in raw_events or ():
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        try:
            moment = float(item[0])
        except (TypeError, ValueError):
            continue
        kind = str(item[1]).strip().lower()
        if kind == "key" and len(item) >= 4:
            action = str(item[3]).strip().lower()
            key = str(item[2]).strip().lower()
            if action in ("down", "up", "repeat") and key:
                normalized.append(
                    {"t": moment, "kind": "key", "key": key, "action": action}
                )
        elif kind == "mouse" and len(item) >= 6:
            action = str(item[3]).strip().lower()
            button = str(item[2]).strip().lower()
            if action in ("down", "up") and button:
                normalized.append(
                    {
                        "t": moment,
                        "kind": "mouse",
                        "button": button,
                        "action": action,
                        "x": int(item[4]),
                        "y": int(item[5]),
                    }
                )
        elif kind == "move" and len(item) >= 4:
            try:
                mx = int(item[2])
                my = int(item[3])
            except (TypeError, ValueError):
                continue
            normalized.append({"t": moment, "kind": "move", "x": mx, "y": my})
        elif kind == "wheel":
            try:
                delta = int(item[2])
            except (TypeError, ValueError):
                continue
            normalized.append({"t": moment, "kind": "wheel", "delta": delta})
    normalized.sort(key=lambda event: event["t"])
    return normalized


def _intervals(events, kind, name_field):
    """Return (start, end, name) intervals for key or mouse press events."""
    open_presses = {}
    intervals = []
    for event in events:
        if event["kind"] != kind:
            continue
        name = event[name_field]
        if event["action"] == "down":
            open_presses[name] = event["t"]
        elif event["action"] == "up":
            started = open_presses.pop(name, None)
            if started is not None:
                intervals.append((started, event["t"], name))
    return intervals


def _has_overlap(intervals):
    """Return True when two different presses are held at the same time."""
    active = []
    for started, ended, name in sorted(intervals):
        while active and active[0][0] <= started:
            heapq.heappop(active)
        if any(other != name for _end, other in active):
            return True
        heapq.heappush(active, (ended, name))
    return False


def _drag_has_moves(events):
    """Return True when a recorded drag contains captured move samples.

    设计取舍（维护者 2026-10-20 裁定）：只要拖拽期间采集到了移动事件，
    就必须保留这些采样点及其时间戳并走时间线回放。**不按「路径是否接近
    直线」丢弃**——直线拖拽中的停顿与变速同样是需要还原的操作；既有拖拽
    步骤只有起止点 + 总时长 + 匀速插值，无法表达它们。

    已知边界（不承诺）：采样节流（≥0.02 s 或 ≥3 px）丢弃的中间细节不恢复；
    毫秒级实时性不保证。
    """
    moves = [
        (event["t"], event["x"], event["y"])
        for event in events
        if event["kind"] == "move"
    ]
    if not moves:
        return False
    press = None
    for event in events:
        if event["kind"] != "mouse":
            continue
        if event["action"] == "down":
            press = (event["x"], event["y"], event["t"])
        elif event["action"] == "up" and press is not None:
            if any(press[2] <= moment <= event["t"] for moment, _x, _y in moves):
                return True
            press = None
    return False


def timeline_reason(events, *, include_mouse=False):
    """Return why the raw stream needs a timeline step, or None.

    触发条件：C1 存在自动重复事件；C2 多键重叠；C3 键鼠重叠；
    C5 拖拽期间采集到移动事件。

    返回 None 表示**未命中当前支持的时间线触发条件**，调用方退回旧步骤序列。
    这不等于「录制中不存在时序」：例如鼠标按下到抬起的间隔、两次点击之间的
    空隙同样属于时序，只是当前支持范围未覆盖（见 docs 记录中的未支持清单）。
    """
    if any(
        event["kind"] == "key" and event["action"] == "repeat" for event in events
    ):
        return "C1 自动重复"
    key_intervals = _intervals(events, "key", "key")
    if _has_overlap(key_intervals):
        return "C2 多键重叠"
    if include_mouse:
        if _has_overlap(key_intervals + _intervals(events, "mouse", "button")):
            return "C3 键鼠重叠"
        if _drag_has_moves(events):
            return "C5 拖拽含移动事件"
    return None


def recorded_events_to_timeline(raw_events, *, include_mouse=False):
    """Build a timeline task from the raw hook stream.

    Args:
        raw_events: Raw records captured by ``HookThread.raw_input_events``.
        include_mouse: Whether mouse events take part (batch 3).

    Returns:
        Tuple ``(task, reason)``; ``(None, None)`` when none of the currently
        supported timeline triggers applies (see :func:`timeline_reason`), in
        which case the caller falls back to the legacy step sequence.
    """
    events = _raw_event_dicts(raw_events)
    if not events:
        return None, None
    if not include_mouse:
        events = [event for event in events if event["kind"] == "key"]
        if not events:
            return None, None
    reason = timeline_reason(events, include_mouse=include_mouse)
    if reason is None:
        return None, None
    # INVARIANT: 每个 repeat 之前必须存在同键的 down，否则回放时
    # `_tracked_key_up` 找不到已登记键、不会发送 keyup，按键会残留按下态。
    pressed = set()
    fixed = []
    for event in events:
        if event["kind"] == "key":
            if event["action"] == "down":
                pressed.add(event["key"])
            elif event["action"] == "up":
                pressed.discard(event["key"])
            elif event["action"] == "repeat" and event["key"] not in pressed:
                fixed.append(
                    {
                        "t": event["t"],
                        "kind": "key",
                        "key": event["key"],
                        "action": "down",
                    }
                )
                pressed.add(event["key"])
        fixed.append(event)
    base = fixed[0]["t"]
    payload = []
    for event in fixed:
        item = {name: value for name, value in event.items() if name != "t"}
        item["t"] = round(max(0.0, event["t"] - base), 3)
        payload.append(item)
    duration = payload[-1]["t"] if payload else 0.0
    task = {
        "type": TASK_TYPE_INPUT_TIMELINE,
        "value": f"输入时序：{len(payload)} 个事件 / {duration:.2f} 秒",
        "input_events": payload,
        "recorded_duration": round(duration, 3),
    }
    return task, reason


def build_recording_tasks(
    events,
    raw_events=None,
    *,
    double_click_seconds: float = 0.5,
    scroll_merge_seconds: float = 0.25,
    include_mouse: bool = False,
):
    """Convert a finished recording into steps, choosing the right model.

    A recording that hits a supported timeline trigger (auto-repeat,
    overlapping keys, key-mouse overlap, drag with sampled moves) becomes one
    timeline step. Recordings that hit none of them keep the legacy
    per-action steps; that is a statement about the current trigger set, not
    a claim that such recordings carry no timing at all.

    Args:
        events: Aggregated semantic events from the hook.
        raw_events: Raw hook records (optional; enables timeline detection).
        double_click_seconds: Passed through to the legacy converter.
        scroll_merge_seconds: Passed through to the legacy converter.
        include_mouse: Whether mouse events take part in the timeline.

    Returns:
        List of task dictionaries.
    """
    timeline, reason = recorded_events_to_timeline(
        raw_events, include_mouse=include_mouse
    )
    if timeline is not None:
        logger.info(
            "录制转换：时间线（%s，%d 事件）", reason, len(timeline["input_events"])
        )
        return [timeline]
    logger.debug("录制转换：步骤序列（无自动重复/重叠）")
    return recorded_events_to_tasks(
        events,
        double_click_seconds=double_click_seconds,
        scroll_merge_seconds=scroll_merge_seconds,
    )


def recorded_events_to_tasks(
    events: Iterable[object],
    *,
    wait_threshold: float = 0.15,
    double_click_seconds: float = 0.5,
    scroll_merge_seconds: float = 0.25,
) -> list[dict[str, Any]]:
    """Convert aggregated recording events into replayable workflow tasks.

    Aggregates raw events (double-click and scroll merging), then maps
    each event to its corresponding task dict. Wait steps are inserted
    between events whose inter-event delay exceeds *wait_threshold*.

    Args:
        events: Iterable of raw event tuples or RecordedEvent instances.
        wait_threshold: Minimum delay (seconds) that triggers a wait step.
        double_click_seconds: Maximum gap between clicks to form a double.
        scroll_merge_seconds: Maximum gap between scrolls to merge.

    Returns:
        Ordered list of task dictionaries ready for the workflow editor.
    """
    tasks: list[dict[str, Any]] = []
    previous_end: float | None = None
    aggregated = aggregate_recorded_events(
        events,
        double_click_seconds=double_click_seconds,
        scroll_merge_seconds=scroll_merge_seconds,
    )
    if len(aggregated) > MAX_RECORDED_EVENTS:
        # S5: silently discarding recorded events hides data loss from the user.
        # Report the truncation count so the drop is at least observable.
        dropped = len(aggregated) - MAX_RECORDED_EVENTS
        logger.warning(
            "recorded_events_to_tasks: 录制事件数 %d 超过上限 %d，已截断并丢弃 %d 个事件",
            len(aggregated),
            MAX_RECORDED_EVENTS,
            dropped,
        )
        aggregated = aggregated[:MAX_RECORDED_EVENTS]
    for event in aggregated:
        if previous_end is not None:
            delay = max(0.0, event.started - previous_end)
            if delay > wait_threshold:
                tasks.append({"type": TASK_TYPE_WAIT, "value": f"{delay:.2f}"})
        previous_end = event.ended

        if event.kind == "left":
            if not isinstance(event.value, (list, tuple)) or len(event.value) < 2:
                continue
            tasks.append(
                {"type": TASK_TYPE_LEFT_CLICK, "value": f"{event.value[0]},{event.value[1]}"}
            )
        elif event.kind == "left_double":
            if not isinstance(event.value, (list, tuple)) or len(event.value) < 2:
                continue
            tasks.append(
                {
                    "type": TASK_TYPE_LEFT_DOUBLE_CLICK,
                    "value": f"{event.value[0]},{event.value[1]}",
                }
            )
        elif event.kind == "right":
            if not isinstance(event.value, (list, tuple)) or len(event.value) < 2:
                continue
            tasks.append(
                {"type": TASK_TYPE_RIGHT_CLICK, "value": f"{event.value[0]},{event.value[1]}"}
            )
        elif event.kind in ("left_drag", "right_drag"):
            if not isinstance(event.value, (list, tuple)) or len(event.value) < 4:
                continue
            command = (
                TASK_TYPE_LEFT_DRAG
                if event.kind == "left_drag"
                else TASK_TYPE_RIGHT_DRAG
            )
            tasks.append(
                {
                    "type": command,
                    "value": (
                        f"{event.value[0]},{event.value[1]} -> "
                        f"{event.value[2]},{event.value[3]}"
                    ),
                    "recorded_duration": round(
                        max(0.05, event.ended - event.started), 3
                    ),
                }
            )
        elif event.kind == "scroll":
            # kind == "scroll" guarantees value is int (set by aggregate).
            tasks.append({"type": TASK_TYPE_SCROLL, "value": str(int(event.value))})  # pyright: ignore[reportArgumentType]
        elif event.kind in ("key", "hotkey") and str(event.value).strip():
            # SUP/录制长按：把**可测量**的按下→松开间隔显式写入步骤级 key_hold_duration。
            # 1) 不用等待阈值（wait_threshold）截断按住时长——两者语义不同；
            # 2) 显式写入避免继承用户的全局「按键按住(s)」，让已录制方案的语义不随后续全局设置漂移；
            # 3) 按住期间不再额外生成等待（等待只表示两个动作之间的真实空隙，见上方 delay 口径）。
            hold_seconds = max(0.0, float(event.ended) - float(event.started))
            tasks.append(
                {
                    "type": TASK_TYPE_SYSTEM_KEY,
                    "value": str(event.value).strip(),
                    "key_hold_duration": f"{hold_seconds:.3f}",
                }
            )
    return tasks
