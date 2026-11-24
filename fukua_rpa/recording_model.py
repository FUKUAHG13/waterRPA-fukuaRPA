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

# 跨段等待的最小可表示空档（秒，1ms）。DESIGN: 跨段空档必须逐毫秒复原录制节奏；
# wait_threshold 只属于普通段内部的既有规则，不得用于过滤跨段空档。
MIN_CROSS_SEGMENT_GAP = 0.001

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
    task = _timeline_task_from_events(events, value_prefix="输入时序")
    return task, reason


def _timeline_task_from_events(
    events: list[dict], *, value_prefix: str = "输入时序"
) -> dict[str, Any]:
    """从已规范化的 raw 事件列表构造 29.0 任务（时间归一为段内相对时间）。

    供整段录制与分段后的时序片段共用，保证两者的 repeat 修复、时间精度与
    载荷结构完全一致。传入事件的 ``t`` 是绝对时刻；生成时减去段内首事件时刻。
    """
    # INVARIANT: 每个 repeat 之前必须存在同键的 down，否则回放时
    # `_tracked_key_up` 找不到已登记键、不会发送 keyup，按键会残留按下态。
    pressed: set[str] = set()
    fixed: list[dict] = []
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
    return {
        "type": TASK_TYPE_INPUT_TIMELINE,
        "value": f"{value_prefix}：{len(payload)} 个事件 / {duration:.2f} 秒",
        "input_events": payload,
        "recorded_duration": round(duration, 3),
    }


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
    tasks, info = build_recording_tasks_detailed(
        events,
        raw_events,
        double_click_seconds=double_click_seconds,
        scroll_merge_seconds=scroll_merge_seconds,
        include_mouse=include_mouse,
    )
    if info["timeline_segments"]:
        logger.info(
            "录制转换：%s，%d 个输入时序片段",
            info["reason"],
            info["timeline_segments"],
        )
    else:
        logger.debug("录制转换：步骤序列（无自动重复/重叠）")
    return tasks


# --- 普通录制分段（阶段二） ------------------------------------------------
#
# 目标：一次混合录制只在“确需保真”的区间生成输入时序片段，其余动作仍是普通步骤。
# 语义事件（events）与 raw 事件（raw_events）在 steps 模式共用同一绝对时钟，
# 因此用绝对时刻把两者对齐；时序片段的时间归一只在片段内部进行。


@dataclass(frozen=True)
class FidelityRange:
    """一个需要保真回放的输入区间（绝对时间，半开区间近似）。"""

    start: float
    end: float
    reason: str


@dataclass(frozen=True)
class SegmentPlan:
    """分段计划：一个片段要么是普通语义事件，要么是 raw 时序片段。"""

    kind: str  # "normal" | "timeline"
    start: float  # 绝对开始时刻（跨段等待用）
    end: float  # 绝对结束时刻
    events: tuple[Any, ...]  # normal: 原始语义事件；timeline: 规范化 raw dict
    reason: str = ""


@dataclass(frozen=True)
class SegmentationResult:
    segments: tuple[SegmentPlan, ...]
    reason: str
    truncated: bool = False


def _press_intervals(events: list[dict], kind: str, name_field: str):
    """按具体设备+键/按钮配对，返回 (start, end, name, kind) 按压区间。"""
    open_presses: dict[str, float] = {}
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
                intervals.append((started, event["t"], name, kind))
    return intervals


def _intervals_overlap(first, second) -> bool:
    """严格相交：一方松开时刻等于另一方按下时刻不算重叠。

    口径与 :func:`_has_overlap` 一致（该函数的 active 堆在 ``end <= started`` 时
    先弹出），保证无保真触发时不会误判，纯普通录制输出与旧版逐项一致。
    """
    return first[0] < second[1] and second[0] < first[1]


def _drag_intervals(events: list[dict]):
    """返回带移动采样的鼠标按压区间（C5），身份按具体按钮区分。"""
    moves = [
        (event["t"], event["x"], event["y"])
        for event in events
        if event["kind"] == "move"
    ]
    if not moves:
        return []
    result = []
    for start, end, name, kind in _press_intervals(events, "mouse", "button"):
        if kind != "mouse":
            continue
        if any(start <= moment <= end for moment, _x, _y in moves):
            result.append((start, end, name, kind))
    return result


def _fidelity_ranges(events: list[dict], include_mouse: bool) -> list[FidelityRange]:
    """圈出需要保留时序的完整输入区间，返回按时间排序的合并区间。

    触发口径与 :func:`timeline_reason` 一致：C1 自动重复、C2 多键重叠、
    C3 键鼠重叠、C5 拖拽含移动采样。区间扩展为“按下到松开”的完整区间，
    重叠或相接的区间合并，切口只落在相关键鼠均已松开的边界。
    """
    key_intervals = _press_intervals(events, "key", "key")
    mouse_intervals = _press_intervals(events, "mouse", "button") if include_mouse else []
    marks: list[tuple[float, float, str]] = []

    # C1：自动重复——把该键的完整按下区间纳入
    repeated_keys = {
        event["key"]
        for event in events
        if event["kind"] == "key" and event["action"] == "repeat"
    }
    for start, end, name, _kind in key_intervals:
        if name in repeated_keys:
            marks.append((start, end, "C1 自动重复"))

    if _has_overlap([(s, e, n) for s, e, n, _k in key_intervals]):
        # C2：多键重叠——所有参与重叠的键区间合并标记
        for interval in key_intervals:
            if any(
                other[2] != interval[2] and _intervals_overlap(interval, other)
                for other in key_intervals
            ):
                marks.append((interval[0], interval[1], "C2 多键重叠"))

    if include_mouse:
        # C3：键鼠重叠
        for key_iv in key_intervals:
            for mouse_iv in mouse_intervals:
                if _intervals_overlap(key_iv, mouse_iv):
                    marks.append((key_iv[0], key_iv[1], "C3 键鼠重叠"))
                    marks.append((mouse_iv[0], mouse_iv[1], "C3 键鼠重叠"))
        # C5：拖拽含移动采样
        for start, end, _name, _kind in _drag_intervals(events):
            marks.append((start, end, "C5 拖拽含移动事件"))

    if not marks:
        return []

    # 合并重叠/相接区间，切口落在全部相关输入松开的边界。
    marks.sort(key=lambda item: (item[0], item[1]))
    merged: list[list[Any]] = [[marks[0][0], marks[0][1], {marks[0][2]}]]
    for start, end, reason in marks[1:]:
        current = merged[-1]
        if start <= current[1]:
            current[1] = max(current[1], end)
            current[2].add(reason)
        else:
            merged.append([start, end, {reason}])

    # INVARIANT: 切口不得切开一个完整的按下→松开区间。若某个无关按键/按钮恰好
    # 跨越切口，它的 down 会落在前段、up 落在时序片段而两者都不完整——普通段不会
    # 生成它、片段切片也不含两端，最终该动作被静默丢弃。这里把区间扩展到覆盖所有
    # 与它相交的按压，再重新合并，直到切口只落在全部相关输入均已松开的边界。
    holds = key_intervals + mouse_intervals
    changed = True
    while changed:
        changed = False
        expanded: list[list[Any]] = []
        for start, end, reasons in merged:
            new_start, new_end = start, end
            for h_start, h_end, _name, _kind in holds:
                # 端点相接也必须纳入：片段切片用闭区间 [start, end]，若某按压的
                # down 恰落在区间 end（或 up 恰落在区间 start），相接不算相交会
                # 让该按压只有一端进入片段，生成以未配对 down/up 结尾的 29.0，
                # 预运行校验会拒绝并让用户重录。相接即按“部分落在区间内”扩展
                # 为完整 down→up，保证切口只落在所有相关输入均松开的边界。
                if (
                    h_start <= new_end
                    and new_start <= h_end
                    and (h_start < new_start or h_end > new_end)
                ):
                    new_start = min(new_start, h_start)
                    new_end = max(new_end, h_end)
                    changed = True
            expanded.append([new_start, new_end, reasons])
        merged = expanded
        if changed:
            merged.sort(key=lambda item: (item[0], item[1]))
            remerged: list[list[Any]] = []
            for start, end, reasons in merged:
                if remerged and start <= remerged[-1][1]:
                    remerged[-1][1] = max(remerged[-1][1], end)
                    remerged[-1][2] = remerged[-1][2] | reasons
                else:
                    remerged.append([start, end, set(reasons)])
            merged = remerged
    return [
        FidelityRange(start, end, "、".join(sorted(reasons)))
        for start, end, reasons in merged
    ]


def _semantic_window(event: object) -> tuple[float, float]:
    """返回语义事件的 (started, ended)，兼容 tuple 与 RecordedEvent。"""
    if isinstance(event, RecordedEvent):
        return event.started, event.ended
    if isinstance(event, dict):
        return float(event.get("t", 0.0)), float(event.get("ended", event.get("t", 0.0)))
    started, _kind, _value = event[0], event[1], event[2]
    ended = event[3] if len(event) >= 4 else started
    return float(started), float(ended)


def _ranges_cover(
    start: float, end: float, ranges: list[FidelityRange]
) -> FidelityRange | None:
    """语义动作是否与某个保真区间相交（含边界）。"""
    for item in ranges:
        if start <= item.end and item.start <= end:
            return item
    return None


def plan_recording_segments(
    events,
    raw_events,
    *,
    include_mouse: bool = True,
    truncated: bool = False,
) -> SegmentationResult:
    """把一次普通录制切成“普通段 → 时序片段 → 普通段”。

    纯普通录制返回单个 normal 段，输出与旧版逐项一致；无法安全拆分（区间覆盖
    全程或边界无法确认）时回退为单个 timeline 段。跨段等待在装配阶段按绝对时刻
    计算，不在此处使用段内相对时间。
    """
    raw = _raw_event_dicts(raw_events)
    semantic = list(events or ())
    if not raw:
        return SegmentationResult(
            (SegmentPlan("normal", 0.0, 0.0, tuple(semantic)),) if semantic else (),
            "空录制",
        )

    ranges = _fidelity_ranges(raw, include_mouse=include_mouse)
    if not ranges:
        return SegmentationResult(
            (SegmentPlan("normal", 0.0, 0.0, tuple(semantic)),),
            "无保真触发",
            truncated,
        )

    first = ranges[0]
    semantic_start = min((_semantic_window(e)[0] for e in semantic), default=raw[0]["t"])
    semantic_end = max((_semantic_window(e)[1] for e in semantic), default=raw[-1]["t"])
    if len(ranges) == 1 and first.start <= semantic_start and first.end >= semantic_end:
        # 保真区间覆盖整次录制：回退为整段时序，与既有行为一致。
        return SegmentationResult(
            (SegmentPlan("timeline", first.start, first.end, tuple(raw), first.reason),),
            f"保真区间覆盖全程（{first.reason}）",
        )

    segments: list[SegmentPlan] = []
    cursor = 0
    normal_buffer: list[Any] = []
    normal_start: float | None = None
    normal_end = 0.0

    def flush_normal():
        nonlocal normal_buffer, normal_start, normal_end
        if normal_buffer:
            segments.append(
                SegmentPlan(
                    "normal",
                    normal_start if normal_start is not None else 0.0,
                    normal_end,
                    tuple(normal_buffer),
                )
            )
        normal_buffer = []
        normal_start = None

    for item in ranges:
        # 收集落在当前保真区间之前的普通语义事件（保留原始对象，不二次聚合）。
        while cursor < len(semantic):
            event = semantic[cursor]
            start, end = _semantic_window(event)
            if start >= item.end:
                break
            if _ranges_cover(start, end, ranges):
                cursor += 1
                continue
            normal_buffer.append(event)
            normal_start = start if normal_start is None else min(normal_start, start)
            normal_end = max(normal_end, end)
            cursor += 1
        flush_normal()
        # 时序片段只消费落在该区间内的 raw 事件；同刻事件按原列表顺序保留。
        slice_events = [
            event for event in raw if item.start <= event["t"] <= item.end
        ]
        if slice_events:
            segments.append(
                SegmentPlan(
                    "timeline",
                    slice_events[0]["t"],
                    slice_events[-1]["t"],
                    tuple(slice_events),
                    item.reason,
                )
            )
        # 跨过本次录制里已被区间消费的语义事件。
        while cursor < len(semantic) and _semantic_window(semantic[cursor])[1] <= item.end:
            cursor += 1

    # 区间之后的普通事件
    while cursor < len(semantic):
        event = semantic[cursor]
        start, end = _semantic_window(event)
        if not _ranges_cover(start, end, ranges):
            normal_buffer.append(event)
            normal_start = start if normal_start is None else min(normal_start, start)
            normal_end = max(normal_end, end)
        cursor += 1
    flush_normal()

    reasons = "、".join(item.reason for item in ranges)
    return SegmentationResult(tuple(segments), f"分段（{reasons}）", truncated)


def build_recording_tasks_detailed(
    events,
    raw_events=None,
    *,
    double_click_seconds: float = 0.5,
    scroll_merge_seconds: float = 0.25,
    wait_threshold: float = 0.15,
    include_mouse: bool = False,
    truncated: bool = False,
):
    """分段版录制转换，返回 ``(tasks, info)``。

    ``info`` 含片段数量、保真原因、是否截断，供完成提示使用。纯普通录制保持
    旧版输出；无法安全拆分时回退整段时序并说明原因。

    跨段空档按毫秒精度复原：正空档 >= 1ms 计一次显式等待（值三位小数），
    不足 1ms 不生成；该口径与段内的 ``wait_threshold`` 无关。
    """
    raw = list(raw_events or ())
    plan = plan_recording_segments(
        events, raw, include_mouse=include_mouse, truncated=truncated
    )
    tasks: list[dict[str, Any]] = []
    timeline_segments = 0

    if len(plan.segments) == 1 and plan.segments[0].kind == "normal":
        tasks = recorded_events_to_tasks(
            plan.segments[0].events,
            wait_threshold=wait_threshold,
            double_click_seconds=double_click_seconds,
            scroll_merge_seconds=scroll_merge_seconds,
        )
    elif len(plan.segments) == 1 and plan.segments[0].kind == "timeline":
        tasks = [
            _timeline_task_from_events(
                list(plan.segments[0].events), value_prefix="输入时序"
            )
        ]
        timeline_segments = 1
    else:
        # 多段：段内按各自规则生成；跨段空档用**绝对时刻**计算一次显式等待。
        previous_end: float | None = None
        for segment in plan.segments:
            if segment.kind == "normal":
                built = recorded_events_to_tasks(
                    segment.events,
                    wait_threshold=wait_threshold,
                    double_click_seconds=double_click_seconds,
                    scroll_merge_seconds=scroll_merge_seconds,
                )
                seg_start, seg_end = segment.start, segment.end
            else:
                built = [
                    _timeline_task_from_events(
                        list(segment.events), value_prefix="输入时序"
                    )
                ]
                seg_start, seg_end = segment.start, segment.end
                timeline_segments += 1
            if not built:
                continue
            if previous_end is not None and seg_start > previous_end:
                gap = seg_start - previous_end
                # DESIGN: 逐毫秒复原跨段节奏。1e-9 只抵消二进制浮点差
                # （2.001 - 2.0 == 0.0009999999999998899），不改变毫秒量化口径；
                # max() 保证量化后不会写 "0.000" 的零等待步骤。
                if gap + 1e-9 >= MIN_CROSS_SEGMENT_GAP:
                    value = max(MIN_CROSS_SEGMENT_GAP, round(gap, 3))
                    tasks.append({"type": TASK_TYPE_WAIT, "value": f"{value:.3f}"})
            tasks.extend(built)
            previous_end = seg_end

    info = {
        "timeline_segments": timeline_segments,
        "reason": plan.reason,
        "truncated": bool(getattr(plan, "truncated", False)),
        "segment_count": len(plan.segments),
    }
    return tasks, info


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

    The two-decimal wait format and the ``wait_threshold`` filter are the
    existing in-segment contract; cross-segment waits use millisecond
    precision and are built in :func:`build_recording_tasks_detailed`.

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
