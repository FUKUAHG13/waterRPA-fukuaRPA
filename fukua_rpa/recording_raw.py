"""Raw ("replay as recorded") conversion.

Two conversions share the same preprocessing entry (:func:`prepare_raw_events`):

* :func:`build_raw_playback_task` (legacy v1.1.15 path) packs everything into a
  single 29.0 timeline step, kept for old profiles and the legacy path.
* :func:`build_raw_playback_steps` (v1.1.16) turns the same recording into a
  list of real, individually editable normal steps (scroll / key / click /
  trajectory / wait / key-mouse down-repeat-up edges) that preserve movement,
  speed, gaps and key-mouse overlap.

The raw stream is timestamped on the unified relative clock established at
recording start (``t = now - started``). This module never re-bases time: the
delay between pressing F8 and the first event is real and must be preserved.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from .commands import COMMAND_BY_NAME
from .constants import (
    MAX_TASKS_PER_PROFILE,
    MAX_TIMELINE_COORDINATE,
    MAX_TIMELINE_EVENT_SECONDS,
    MAX_TIMELINE_EVENTS,
    TASK_TYPE_INPUT_TIMELINE,
    TASK_TYPE_MOUSE_TRAJECTORY,
    TASK_TYPE_SCROLL,
    TASK_TYPE_WAIT,
)
from .recording_sampler import MoveSample, sample_moves

# 点击/系统按键 code 与 recording_model 同源（从命令注册表派生，避免重复字面量）。
TASK_TYPE_LEFT_CLICK = COMMAND_BY_NAME["左键单击"].code
TASK_TYPE_RIGHT_CLICK = COMMAND_BY_NAME["右键单击"].code
TASK_TYPE_MIDDLE_CLICK = 3.0
TASK_TYPE_SYSTEM_KEY = COMMAND_BY_NAME["系统按键"].code

# 一段连续动作前的空档达到该阈值（毫秒）时，用独立的「等待」行展示；
# 更小的正空档保存在动作行的 pre_delay_ms。与设计基准的既有 150ms 口径一致。
_WAIT_STEP_THRESHOLD_MS = 150


def _raw_point(value: object, value2: object) -> tuple[int, int] | None:
    try:
        x, y = int(value), int(value2)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if abs(x) > MAX_TIMELINE_COORDINATE or abs(y) > MAX_TIMELINE_COORDINATE:
        return None
    return x, y


def prepare_raw_events(
    raw_events: Iterable[object],
    *,
    sampler: Callable[..., list[MoveSample]] | None = None,
) -> list[dict[str, Any]]:
    """Normalize a raw hook stream into a stable, preprocessed event list.

    This is the single preprocessing entry shared by both raw conversions. It
    keeps capture order for equal timestamps, reduces only *consecutive* moves,
    and injects a same-time ``move`` before a mouse ``up`` or ``wheel`` carrying
    a landing point so playback positions the cursor before that event.

    Unlike the legacy payload, wheel events here retain their optional ``x``/
    ``y`` landing point (the v1.1.16 step conversion needs it for the scroll
    landing). The legacy 29.0 builder strips it when constructing ``input_events``.

    Args:
        raw_events: Raw hook records on the unified relative clock.
        sampler: Injectable move reducer (tests may supply a passthrough).

    Returns:
        Ordered list of normalized event dicts (each has ``t`` and ``kind``).
    """
    reduce = sampler or sample_moves
    events: list[dict[str, Any]] = []
    for item in raw_events or ():
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            moment = float(item[0])
        except (TypeError, ValueError):
            continue
        kind = str(item[1]).strip().lower()
        normalized: dict[str, Any] | None = None
        if kind == "move" and len(item) >= 4:
            try:
                x, y = int(item[2]), int(item[3])
            except (TypeError, ValueError):
                continue
            normalized = {"t": moment, "kind": "move", "x": x, "y": y}
        elif kind == "wheel" and len(item) >= 3:
            try:
                delta = int(item[2])
            except (TypeError, ValueError):
                continue
            if delta == 0:
                continue
            normalized = {"t": moment, "kind": "wheel", "delta": delta}
            # M7: extended shape [t, wheel, delta, x, y] carries the landing point.
            if len(item) >= 5:
                point = _raw_point(item[3], item[4])
                if point is not None:
                    normalized["x"], normalized["y"] = point
        elif kind == "mouse" and len(item) >= 6:
            action = str(item[3]).strip().lower()
            button = str(item[2]).strip().lower()
            if action not in ("down", "up") or not button:
                continue
            point = _raw_point(item[4], item[5])
            if point is None:
                continue
            normalized = {
                "t": moment,
                "kind": "mouse",
                "button": button,
                "action": action,
                "x": point[0],
                "y": point[1],
            }
        elif kind == "key" and len(item) >= 4:
            action = str(item[3]).strip().lower()
            key = str(item[2]).strip().lower()
            if action not in ("down", "up", "repeat") or not key:
                continue
            normalized = {"t": moment, "kind": "key", "key": key, "action": action}
        if normalized is not None:
            events.append(normalized)

    if not events:
        return []
    # Stable sort preserves capture order for equal timestamps.
    events.sort(key=lambda ev: ev["t"])

    payload: list[dict[str, Any]] = []
    i = 0
    last_position: tuple[int, int] | None = None
    while i < len(events):
        event = events[i]
        if event["kind"] == "move":
            segment: list[MoveSample] = []
            while i < len(events) and events[i]["kind"] == "move":
                segment.append(
                    MoveSample(events[i]["t"], events[i]["x"], events[i]["y"])
                )
                i += 1
            for sample in reduce(segment):
                payload.append(
                    {"t": round(sample.t, 3), "kind": "move", "x": sample.x, "y": sample.y}
                )
                last_position = (sample.x, sample.y)
            continue

        landing: tuple[int, int] | None = None
        if (event["kind"] == "mouse" and event["action"] == "up") or (
            event["kind"] == "wheel" and "x" in event and "y" in event
        ):
            landing = (event["x"], event["y"])
        if landing is not None and landing != last_position:
            payload.append(
                {"t": round(event["t"], 3), "kind": "move", "x": landing[0], "y": landing[1]}
            )
            last_position = landing

        item = {name: value for name, value in event.items() if name != "t"}
        item["t"] = round(event["t"], 3)
        payload.append(item)
        if event["kind"] == "mouse":
            last_position = (event["x"], event["y"])
        i += 1
    return payload


def build_raw_playback_task(
    raw_events: Iterable[object],
    *,
    sampler: Callable[..., list[MoveSample]] | None = None,
) -> dict[str, Any] | None:
    """Build one input-timeline step that replays a recording verbatim.

    Legacy v1.1.15 conversion, retained for old profiles and the legacy path.
    Delegates preprocessing to :func:`prepare_raw_events` so both conversions
    share one source of truth; strips wheel landing points from ``input_events``
    exactly as before.

    Args:
        raw_events: Raw hook records on the unified relative clock.
        sampler: Injectable move reducer (tests may supply a passthrough).

    Returns:
        A single 29.0 task dict, or ``None`` for an empty/invalid recording.
    """
    payload = prepare_raw_events(raw_events, sampler=sampler)
    if not payload:
        return None
    events: list[dict[str, Any]] = []
    for item in payload:
        event = dict(item)
        if event.get("kind") == "wheel":
            event.pop("x", None)
            event.pop("y", None)
        events.append(event)
    # DESIGN: 事件时刻已经是「相对录制起点」，最后一个事件的时刻即总时长，
    # 包含 F8 到首个事件的起始等待。与回放器 log 的 events[-1]["t"] 口径一致。
    duration = round(max(0.0, events[-1]["t"]), 3)
    return {
        "type": TASK_TYPE_INPUT_TIMELINE,
        "value": f"原样回放：{len(events)} 个事件 / {duration:.2f} 秒",
        "input_events": events,
        "recorded_duration": duration,
        "playback_mode": "raw",
        "tool_only": True,
    }


def _ms(event: dict[str, Any]) -> int:
    """Event time in integer milliseconds (source times are relative seconds)."""
    return int(round(float(event.get("t", 0.0)) * 1000))


def _valid_coord(x: object, y: object) -> bool:
    try:
        return (
            abs(int(x)) <= MAX_TIMELINE_COORDINATE
            and abs(int(y)) <= MAX_TIMELINE_COORDINATE
        )
    except (TypeError, ValueError):
        return False


def build_raw_playback_steps(
    raw_events: Iterable[object],
    *,
    sampler: Callable[..., list[MoveSample]] | None = None,
    segment_id: str | None = None,
    max_steps: int = MAX_TASKS_PER_PROFILE,
) -> list[dict[str, Any]] | None:
    """Turn a raw recording into a list of real, editable normal steps.

    Each step owns its own parameters: scroll delta/landing, key name/edge,
    click landing/hold, sampled trajectory, explicit wait or pre-delay. Steps
    in the same recording share ``timing_group_id`` and carry ``fidelity=raw``
    so the runtime uses one monotonic clock and original-speed dispatch.

    A ``down``/``up`` pair with no interleaving input collapses into a single
    tap step (key ``tap`` / click ``click``) carrying the measured hold time.
    Overlaps and unmergeable edges are emitted as separate down/repeat/up steps
    so their pairing edges remain individually editable.

    Returns ``None`` when the recording is empty, exceeds event/time/coordinate
    limits, contains values that cannot be fully expressed, or would exceed the
    profile step budget. The caller must not submit anything in that case.
    """
    payload = prepare_raw_events(raw_events, sampler=sampler)
    if not payload:
        return None
    # Full validation up front: reject anything that cannot be faithfully kept.
    if len(payload) > MAX_TIMELINE_EVENTS:
        return None
    last_t = max(float(e.get("t", 0.0)) for e in payload)
    if last_t < 0 or last_t > MAX_TIMELINE_EVENT_SECONDS:
        return None
    for event in payload:
        kind = event.get("kind")
        if kind == "move" and not _valid_coord(event.get("x"), event.get("y")):
            return None
        if kind == "wheel":
            try:
                if int(event["delta"]) == 0:
                    return None
            except (KeyError, TypeError, ValueError):
                return None
            if "x" in event and not _valid_coord(event.get("x"), event.get("y")):
                return None
        if kind == "mouse" and not _valid_coord(event.get("x"), event.get("y")):
            return None

    sid = segment_id or ("seg_" + uuid.uuid4().hex)

    def base(task_type: float, **fields: object) -> dict[str, Any]:
        task: dict[str, Any] = {
            "type": task_type,
            "fidelity": "raw",
            "timing_group_id": sid,
            "tool_only": True,
        }
        task.update(fields)
        return task

    steps: list[dict[str, Any]] = []
    prev_end_ms = 0

    def emit_action(row: dict[str, Any], start_ms: int, end_ms: int) -> None:
        """Append one action row, resolving the gap since the previous row.

        A gap >= threshold becomes a dedicated wait row whose value is the whole
        gap; the action then has ``pre_delay_ms=0``. A smaller positive gap is
        carried by the action row's ``pre_delay_ms``. ``prev_end_ms`` advances to
        this row's end so the next gap is measured from it.
        """
        nonlocal prev_end_ms
        gap = max(0, start_ms - prev_end_ms)
        if gap >= _WAIT_STEP_THRESHOLD_MS:
            steps.append(base(TASK_TYPE_WAIT, value=f"{gap / 1000:.3f}", pre_delay_ms="0"))
            row["pre_delay_ms"] = "0"
        else:
            row["pre_delay_ms"] = str(gap)
        steps.append(row)
        prev_end_ms = end_ms

    # 跨行按住状态：未合并的 down 之后若有移动，轨迹行需携带 hold_button，
    # 按钮直到对应 up 行才松开（方案要求拖拽期间的轨迹保持按下）。
    held_buttons: list[str] = []
    i = 0
    while i < len(payload):
        event = payload[i]

        # --- 连续 move → 一个轨迹行（30.0）---
        if event.get("kind") == "move":
            points: list[list[int]] = []
            seg_start = _ms(event)
            seg_end = seg_start
            while i < len(payload) and payload[i].get("kind") == "move":
                m = _ms(payload[i])
                points.append([m - seg_start, int(payload[i]["x"]), int(payload[i]["y"])])
                seg_end = m
                i += 1
            row = base(
                TASK_TYPE_MOUSE_TRAJECTORY,
                value=(
                    f"鼠标轨迹：{len(points)} 点 / "
                    f"{(seg_end - seg_start) / 1000:.3f} 秒"
                ),
                trajectory=json.dumps(points, ensure_ascii=False),
                hold_button=(held_buttons[-1] if held_buttons else ""),
            )
            emit_action(row, seg_start, seg_end)
            continue

        # --- 滚轮 → 6.0 ---
        if event.get("kind") == "wheel":
            now = _ms(event)
            delta = int(event["delta"])
            fields: dict[str, Any] = {"value": _scroll_cells(delta), "raw_delta": str(delta)}
            if "x" in event and "y" in event:
                fields["wheel_x"] = str(int(event["x"]))
                fields["wheel_y"] = str(int(event["y"]))
            emit_action(base(TASK_TYPE_SCROLL, **fields), now, now)
            i += 1
            continue

        # --- 按键 down/repeat/up → 7.0（含严格相邻配对合并）---
        if event.get("kind") == "key":
            now = _ms(event)
            action = str(event.get("action", ""))
            key = str(event.get("key", ""))
            if action == "down":
                merged = _try_merge_key_tap(payload, i)
                if merged is not None:
                    end_ms, hold_ms = merged
                    emit_action(
                        base(
                            TASK_TYPE_SYSTEM_KEY,
                            value=key,
                            key_action="tap",
                            key_hold_duration=f"{hold_ms / 1000:.3f}",
                        ),
                        now,
                        end_ms,
                    )
                    i += 2  # 跳过紧邻的 up
                    continue
                emit_action(base(TASK_TYPE_SYSTEM_KEY, value=key, key_action="down"), now, now)
                i += 1
                continue
            if action == "repeat":
                emit_action(base(TASK_TYPE_SYSTEM_KEY, value=key, key_action="repeat"), now, now)
                i += 1
                continue
            # up
            emit_action(base(TASK_TYPE_SYSTEM_KEY, value=key, key_action="up"), now, now)
            i += 1
            continue

        # --- 鼠标 down/up → 1.0/3.0（含严格相邻配对合并）---
        if event.get("kind") == "mouse":
            now = _ms(event)
            action = str(event.get("action", ""))
            button = str(event.get("button", ""))
            command = _click_command(button)
            if command is None:
                # 未映射按钮（侧键等）已在采集端拦截；此处保守拒绝。
                return None
            coord = f"{int(event['x'])},{int(event['y'])}"
            if action == "down":
                merged = _try_merge_click(payload, i)
                if merged is not None:
                    end_ms, hold_ms = merged
                    emit_action(
                        base(
                            command,
                            value=coord,
                            mouse_action="click",
                            click_hold_ms=str(hold_ms),
                        ),
                        now,
                        end_ms,
                    )
                    i += 2  # 跳过紧邻的 up
                    continue
                emit_action(base(command, value=coord, mouse_action="down"), now, now)
                held_buttons.append(button)
                i += 1
                continue
            # up
            emit_action(base(command, value=coord, mouse_action="up"), now, now)
            if button in held_buttons:
                held_buttons.remove(button)
            i += 1
            continue

        # 未知事件：保守拒绝，避免静默丢弃。
        return None

    if not steps:
        return None
    if len(steps) > max_steps:
        return None
    return steps


def _scroll_cells(delta: int) -> str:
    """Grid count for display/default dispatch (precise delta is in raw_delta)."""
    cells = max(1, round(abs(delta) / 120))
    return str(cells if delta > 0 else -cells)


def _click_command(button: str) -> float | None:
    return {
        "left": TASK_TYPE_LEFT_CLICK,
        "right": TASK_TYPE_RIGHT_CLICK,
        "middle": TASK_TYPE_MIDDLE_CLICK,
    }.get(button)


def _try_merge_key_tap(
    payload: list[dict[str, Any]], index: int
) -> tuple[int, int] | None:
    """Merge a key ``down`` with a strictly adjacent same-key ``up``.

    Only adjacent edges merge: any intervening event (move, other key, mouse or
    wheel) means the press/release participates in an overlap and must stay as
    separate editable down/up rows. Returns ``(end_ms, hold_ms)`` on merge.
    """
    j = index + 1
    if j < len(payload):
        nxt = payload[j]
        cur = payload[index]
        if (
            nxt.get("kind") == "key"
            and nxt.get("action") == "up"
            and nxt.get("key") == cur.get("key")
        ):
            end_ms = _ms(nxt)
            return end_ms, max(0, end_ms - _ms(cur))
    return None


def _try_merge_click(
    payload: list[dict[str, Any]], index: int
) -> tuple[int, int] | None:
    """Merge a mouse ``down`` with a strictly adjacent same-button ``up`` at the
    same landing point. Returns ``(end_ms, hold_ms)`` on merge."""
    j = index + 1
    if j < len(payload):
        nxt = payload[j]
        cur = payload[index]
        if (
            nxt.get("kind") == "mouse"
            and nxt.get("action") == "up"
            and nxt.get("button") == cur.get("button")
            and nxt.get("x") == cur.get("x")
            and nxt.get("y") == cur.get("y")
        ):
            end_ms = _ms(nxt)
            return end_ms, max(0, end_ms - _ms(cur))
    return None
