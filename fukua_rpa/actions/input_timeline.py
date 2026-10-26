# pyright: reportAttributeAccessIssue=false
"""Recorded input timeline playback (batch 2: keyboard only).

The recorded timeline is a list of timestamped input events. Playback
dispatches them in order on a **single thread**: a held key stays pressed
because its ``up`` event has not been dispatched yet, so overlapping keys
and key-while-other-action do not require concurrency.
"""

import logging
import time

from fukua_rpa.constants import (
    MAX_TIMELINE_EVENT_SECONDS,
    MAX_TIMELINE_EVENTS,
    TASK_TYPE_INPUT_TIMELINE,
)
from fukua_rpa.log_policy import LOG_ACTION

from .shared import action_handler

logger = logging.getLogger(__name__)

_KEY_ACTIONS = frozenset({"down", "up", "repeat"})
_MOUSE_ACTIONS = frozenset({"down", "up"})
# 坐标绝对上限：物理桌面坐标远小于此值，超出说明数据损坏而非用户意图。
_MAX_COORDINATE = 100_000


def _point(item):
    """Return (x, y) as bounded ints, or None when the coordinates are invalid."""
    try:
        x = int(item.get("x"))
        y = int(item.get("y"))
    except (TypeError, ValueError):
        return None
    if abs(x) > _MAX_COORDINATE or abs(y) > _MAX_COORDINATE:
        return None
    return x, y


def normalize_timeline_events(raw_events: object) -> list[dict]:
    """Return a validated, time-sorted copy of a recorded event list.

    Invalid entries are dropped rather than guessed: a wrong timestamp or
    an unknown action would silently change playback order, which is the
    exact behaviour this feature exists to preserve.

    Args:
        raw_events: Value stored under ``input_events`` on the step.

    Returns:
        List of normalized event dictionaries sorted by ``t``.
    """
    if not isinstance(raw_events, (list, tuple)):
        return []
    events: list[dict] = []
    dropped = 0
    for item in raw_events[:MAX_TIMELINE_EVENTS]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        try:
            moment = float(item.get("t", 0.0))
        except (TypeError, ValueError):
            dropped += 1
            continue
        if moment < 0 or moment > MAX_TIMELINE_EVENT_SECONDS:
            dropped += 1
            continue
        kind = str(item.get("kind", "")).strip().lower()
        if kind == "key":
            action = str(item.get("action", "")).strip().lower()
            key = str(item.get("key", "")).strip().lower()
            if action not in _KEY_ACTIONS or not key:
                dropped += 1
                continue
            events.append({"t": moment, "kind": "key", "key": key, "action": action})
        elif kind == "mouse":
            action = str(item.get("action", "")).strip().lower()
            button = str(item.get("button", "")).strip().lower()
            if action not in _MOUSE_ACTIONS or not button:
                dropped += 1
                continue
            point = _point(item)
            if point is None:
                dropped += 1
                continue
            events.append(
                {
                    "t": moment,
                    "kind": "mouse",
                    "button": button,
                    "action": action,
                    "x": point[0],
                    "y": point[1],
                }
            )
        elif kind == "move":
            point = _point(item)
            if point is None:
                dropped += 1
                continue
            events.append({"t": moment, "kind": "move", "x": point[0], "y": point[1]})
        elif kind == "wheel":
            try:
                delta = int(item.get("delta", 0))
            except (TypeError, ValueError):
                dropped += 1
                continue
            if delta == 0:
                dropped += 1
                continue
            events.append({"t": moment, "kind": "wheel", "delta": delta})
        else:
            dropped += 1
            continue
    events.sort(key=lambda event: event["t"])
    if dropped:
        # 不静默：被丢弃的事件会改变时序，必须可观察（运行日志，隐私规则内只记条数）。
        logger.warning(
            "输入时序回放：%d 个事件格式无效或不支持，已跳过（有效 %d 个）",
            dropped,
            len(events),
        )
    return events


class ActionsInputTimelineMixin:
    """Action primitives for recorded input timelines."""

    def _send_key_repeat(self, key):
        """Send one auto-repeat keydown without touching tracked-key state.

        The key is already tracked as pressed by the preceding ``down``
        event; re-registering it would make ``_tracked_key_up`` release it
        too early (or report a phantom second press).

        Args:
            key: Key name to repeat.

        Returns:
            True if dispatched; False if a stop was requested.
        """
        normalized = str(key or "").strip().lower()
        if not normalized:
            return False
        with self._injected_input_lock:
            if self.check_stop_flag():
                return False
            self.input_backend.key_down(normalized)
        return True

    @action_handler(TASK_TYPE_INPUT_TIMELINE)
    def _execute_input_timeline(self, context):
        """Replay a recorded input timeline.

        Args:
            context: StepActionContext whose task carries ``input_events``.

        Returns:
            Outcome string: "success", "stopped", or "error".
        """
        task = context.task or {}
        events = normalize_timeline_events(task.get("input_events"))
        if not events:
            if self.log_level >= 1:
                self.log("    -> 输入时序步骤没有可回放的事件", LOG_ACTION)
            return "error"
        if self.log_level >= 1:
            duration = events[-1]["t"]
            self.log(
                f"    -> 回放输入时序：{len(events)} 个事件 / {duration:.2f} 秒",
                LOG_ACTION,
            )
        pressed: list[str] = []
        buttons: list[str] = []
        started = time.monotonic()
        try:
            for event in events:
                remaining = event["t"] - (time.monotonic() - started)
                if remaining > 0 and not self._wait_interruptibly(remaining):
                    return "stopped"
                if self.check_stop_flag():
                    return "stopped"
                kind = event["kind"]
                if kind == "key":
                    action = event["action"]
                    key = event["key"]
                    if action == "down":
                        if not self._tracked_key_down(key):
                            return "stopped"
                        pressed.append(key)
                    elif action == "up":
                        self._tracked_key_up(key)
                        if key in pressed:
                            pressed.remove(key)
                    else:  # repeat
                        # Defensive: an isolated repeat (no preceding down in
                        # this step) must still result in a real key press,
                        # otherwise the repeat is a no-op and the key would
                        # never release.
                        if key not in self._injected_keys:
                            if not self._tracked_key_down(key):
                                return "stopped"
                            pressed.append(key)
                        if not self._send_key_repeat(key):
                            return "stopped"
                elif kind == "move":
                    self.input_backend.move_to(event["x"], event["y"], duration=0)
                elif kind == "wheel":
                    self.input_backend.scroll(event["delta"])
                else:  # mouse
                    button = event["button"]
                    if event["action"] == "down":
                        # 先定位再按下：按下发生在光标当前位置，顺序颠倒会让
                        # 按下点落在上一个事件的坐标上。
                        self.input_backend.move_to(event["x"], event["y"], duration=0)
                        if not self._tracked_mouse_down(button):
                            return "stopped"
                        buttons.append(button)
                    else:
                        self._tracked_mouse_up(button)
                        if button in buttons:
                            buttons.remove(button)
            return "stopped" if self.check_stop_flag() else "success"
        finally:
            # DESIGN: 时间线回放必须自己收尾——中途 stop、异常或提前返回时，
            # 已按下的键仍处于物理按下状态，必须逆序释放；run 级
            # release_injected_inputs 只是强杀兜底，不能代替这里。
            for button in reversed(buttons):
                try:
                    self._tracked_mouse_up(button)
                except Exception:  # noqa: BLE001
                    logger.debug("输入时序清理鼠标按钮失败: %s", button, exc_info=True)
            for key in reversed(pressed):
                try:
                    self._tracked_key_up(key)
                except Exception:  # noqa: BLE001
                    logger.debug("输入时序清理按键失败: %s", key, exc_info=True)
