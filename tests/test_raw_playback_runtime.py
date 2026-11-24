"""v1.1.16 原样回放逐条步骤：运行期段上下文状态机与跨行清理（E1/短）。

用假后端验证 RecordingTimingContext 的单调时钟、前置间隔累加、跨行按住
与所有段边界的逆序清理；用真实 handler 验证 raw 边/轨迹/滚轮的派发。
"""

import threading
import unittest
from unittest import mock

from fukua_rpa.actions.flow import ActionsFlowMixin
from fukua_rpa.actions.input_timeline import ActionsInputTimelineMixin
from fukua_rpa.actions.keyboard import ActionsKeyboardMixin
from fukua_rpa.actions.mouse import ActionsMouseMixin
from fukua_rpa.commands import COMMAND_BY_NAME
from fukua_rpa.constants import TASK_TYPE_MOUSE_TRAJECTORY, TASK_TYPE_SCROLL
from fukua_rpa.engine.recording_timing import RecordingTimingContext

LEFT = COMMAND_BY_NAME["左键单击"].code
RIGHT = COMMAND_BY_NAME["右键单击"].code
SYSTEM_KEY = COMMAND_BY_NAME["系统按键"].code


class _Ctx:
    def __init__(self, task):
        self.task = task
        self.value = str(task.get("value", ""))


class _Harness(
    ActionsInputTimelineMixin,
    ActionsKeyboardMixin,
    ActionsMouseMixin,
    ActionsFlowMixin,
):
    """Fake-input harness exposing the engine primitives handlers rely on."""

    def __init__(self):
        self._injected_input_lock = threading.RLock()
        self._input_action_lock = threading.RLock()
        self._injected_keys = []
        self._injected_mouse_buttons = set()
        self.calls = []
        self.waits = []
        self.clock = 1000.0
        self.stop = False
        self.log_level = 0
        self.input_backend = mock.Mock()
        self.input_backend.move_to.side_effect = self._on_move
        self.input_backend.scroll.side_effect = self._on_scroll
        self._recording_timing = RecordingTimingContext()

    def _on_move(self, x, y, duration=0):
        self.calls.append(("move", x, y))

    def _on_scroll(self, delta):
        self.calls.append(("scroll", delta))

    def check_stop_flag(self):
        return self.stop

    def _wait_interruptibly(self, seconds, stop_requested=None):
        self.waits.append(round(float(seconds), 3))
        self.clock += float(seconds)
        return not self.stop

    def _tracked_key_down(self, key):
        if self.stop:
            return False
        self.calls.append(("down", key))
        if key not in self._injected_keys:
            self._injected_keys.append(key)
        return True

    def _tracked_key_up(self, key):
        self.calls.append(("up", key))
        if key in self._injected_keys:
            self._injected_keys.remove(key)
        return True

    def _tracked_mouse_down(self, button):
        if self.stop:
            return False
        self.calls.append(("mdown", button))
        self._injected_mouse_buttons.add(button)
        return True

    def _tracked_mouse_up(self, button):
        self.calls.append(("mup", button))
        self._injected_mouse_buttons.discard(button)
        return True

    def _send_key_repeat(self, key):
        self.calls.append(("repeat", key))
        return True

    def log(self, *a, **k):
        pass

    def log_enabled(self, *a, **k):
        return False

    def parse_coordinate(self, value):
        try:
            parts = str(value).split(",")
            return int(parts[0]), int(parts[1])
        except (ValueError, IndexError):
            return None


def _key_task(action, key="a", pre="0", sid="s1"):
    return {
        "type": SYSTEM_KEY,
        "value": key,
        "key_action": action,
        "fidelity": "raw",
        "timing_group_id": sid,
        "pre_delay_ms": pre,
    }


class TimingContextStateMachineTests(unittest.TestCase):
    def test_new_segment_activates(self):
        h = _Harness()
        h._recording_timing.before_dispatch(h, _key_task("down"))
        self.assertTrue(h._recording_timing.has_segment)
        self.assertEqual(h._recording_timing.segment_id, "s1")

    def test_pre_delay_waited_relative_to_now(self):
        h = _Harness()
        timing = h._recording_timing
        timing.before_dispatch(h, _key_task("down", pre="0"))
        self.assertEqual(h.waits, [])
        timing.before_dispatch(h, _key_task("repeat", pre="100"))
        timing.before_dispatch(h, _key_task("up", pre="50"))
        # 前置间隔按"当前时刻"逐行等待，不与动作时长累积
        self.assertEqual(h.waits, [0.1, 0.05])

    def test_no_drift_when_actions_consume_duration(self):
        """回归核心：动作自身耗时（按住/轨迹）不得让后续小空档被吞掉。

        录制轴：click 0-50ms → (150ms 空档) → wheel 200ms → key 300-450ms
        → (150ms 空档) → move 600ms。每行 pre_delay 由转换器按"本行开始 -
        上一行结束"给出；运行时逐行等待 pre_delay，动作耗时由 harness 推进
        虚拟时钟，最终各动作落点时刻必须与录制轴一致。
        """
        h = _Harness()
        timing = h._recording_timing
        timeline = []

        def dispatch(task, action_duration_ms=0):
            timing.before_dispatch(h, task)  # 等待 pre_delay
            timeline.append((round((h.clock - 1000.0) * 1000), task.get("_label")))
            h.clock += action_duration_ms / 1000.0  # 模拟动作自身耗时

        # click 0ms，按住 50ms；pre_delay=0
        dispatch({"type": LEFT, "value": "1,1", "mouse_action": "click",
                  "fidelity": "raw", "timing_group_id": "s1", "pre_delay_ms": "0",
                  "_label": "click"}, 50)
        # wheel 200ms：空档 150ms >= 阈值 → 等待行(value=0.150) + wheel pre_delay=0
        dispatch({"type": 5.0, "value": "0.150", "fidelity": "raw",
                  "timing_group_id": "s1", "pre_delay_ms": "0", "_label": "wait1"}, 150)
        dispatch({"type": TASK_TYPE_SCROLL, "value": "2", "raw_delta": "240",
                  "fidelity": "raw", "timing_group_id": "s1", "pre_delay_ms": "0",
                  "_label": "wheel"})
        # key 300ms：gap=100ms < 阈值 → pre_delay=100；按住 150ms
        dispatch(_key_task("down", pre="100") | {"_label": "key"}, 150)
        # move 600ms：空档 150ms → 等待行 + pre_delay=0
        dispatch({"type": 5.0, "value": "0.150", "fidelity": "raw",
                  "timing_group_id": "s1", "pre_delay_ms": "0", "_label": "wait2"}, 150)
        dispatch({"type": 30.0, "value": "轨迹", "fidelity": "raw",
                  "timing_group_id": "s1", "pre_delay_ms": "0", "_label": "move"})

        self.assertEqual(
            timeline,
            [(0, "click"), (50, "wait1"), (200, "wheel"),
             (300, "key"), (450, "wait2"), (600, "move")],
        )

    def test_cross_row_hold_and_cleanup_on_segment_boundary(self):
        h = _Harness()
        timing = h._recording_timing
        timing.before_dispatch(h, _key_task("down"))
        h._execute_hotkey(_Ctx(_key_task("down")))
        self.assertEqual(timing.pressed, ["a"])
        timing.before_dispatch(h, {"type": 5.0, "value": "1"})
        self.assertFalse(timing.has_segment)
        self.assertIn(("up", "a"), h.calls)
        self.assertEqual(h._injected_keys, [])

    def test_close_segment_releases_reverse_order(self):
        h = _Harness()
        timing = h._recording_timing
        timing.active = True
        timing.segment_id = "s1"
        timing.pressed = ["a", "b"]
        timing.buttons = ["left", "right"]
        timing.close_segment(h, reason="test")
        self.assertEqual(
            [c for c in h.calls if c[0] in ("mup", "up")],
            [("mup", "right"), ("mup", "left"), ("up", "b"), ("up", "a")],
        )

    def test_pause_hook_is_noop(self):
        h = _Harness()
        timing = h._recording_timing
        timing.active = True
        timing.segment_id = "s1"
        # 暂停不额外计入：pre_delay 相对"当前时刻"等待，暂停自然推移当前时刻。
        timing.add_paused_seconds(2.5)
        self.assertTrue(timing.has_segment)
        timing.before_dispatch(h, _key_task("down", pre="100"))
        self.assertEqual(h.waits, [0.1])
        timing.reset()
        timing.add_paused_seconds(3.0)
        self.assertFalse(timing.has_segment)

    def test_loop_reentry_rebuilds_segment(self):
        h = _Harness()
        timing = h._recording_timing
        timing.before_dispatch(h, _key_task("down"))
        h._execute_hotkey(_Ctx(_key_task("down")))
        timing.close_segment(h, reason="loop rollover")
        self.assertFalse(timing.has_segment)
        timing.before_dispatch(h, _key_task("down"))
        self.assertTrue(timing.has_segment)
        self.assertEqual(timing.pressed, [])  # 新段不继承旧按住状态


class RawHandlerDispatchTests(unittest.TestCase):
    def test_raw_scroll_uses_precise_delta_and_landing(self):
        h = _Harness()
        task = {
            "type": TASK_TYPE_SCROLL,
            "value": "2",
            "raw_delta": "200",
            "wheel_x": "55",
            "wheel_y": "-66",
            "fidelity": "raw",
        }
        h._execute_scroll(_Ctx(task))
        self.assertEqual(h.calls, [("move", 55, -66), ("scroll", 200)])

    def test_raw_key_down_repeat_up_sequence(self):
        h = _Harness()
        # 时序改为相对当前时刻等待，before_dispatch 不再直接调用 time.monotonic；
        # 前置等待经 harness 的 _wait_interruptibly 记录，无需 patch 时钟。
        h._recording_timing.before_dispatch(h, _key_task("down"))
        h._execute_hotkey(_Ctx(_key_task("down")))
        h._execute_hotkey(_Ctx(_key_task("repeat")))
        h._execute_hotkey(_Ctx(_key_task("up")))
        self.assertEqual(
            [c for c in h.calls if c[0] in ("down", "repeat", "up")],
            [("down", "a"), ("repeat", "a"), ("up", "a")],
        )
        self.assertEqual(h._injected_keys, [])

    def test_raw_trajectory_dispatch(self):
        h = _Harness()
        task = {
            "type": TASK_TYPE_MOUSE_TRAJECTORY,
            "value": "轨迹",
            "trajectory": "[[0,10,20],[20,30,40],[40,50,60]]",
            "fidelity": "raw",
        }
        with mock.patch(
            "fukua_rpa.actions.input_timeline.time.monotonic",
            side_effect=lambda: h.clock,
        ):
            h._execute_mouse_trajectory(_Ctx(task))
        self.assertEqual(
            [c for c in h.calls if c[0] == "move"],
            [("move", 10, 20), ("move", 30, 40), ("move", 50, 60)],
        )

    def test_raw_click_tap_hold(self):
        h = _Harness()
        task = {
            "type": LEFT,
            "value": "100,100",
            "mouse_action": "click",
            "click_hold_ms": "80",
            "fidelity": "raw",
        }
        h._execute_left_click(_Ctx(task))
        self.assertEqual(
            [c for c in h.calls if c[0] in ("mdown", "mup")],
            [("mdown", "left"), ("mup", "left")],
        )

    def test_raw_mouse_edge_cross_row_hold(self):
        h = _Harness()
        timing = h._recording_timing
        timing.active = True
        timing.segment_id = "s1"
        down = {"type": LEFT, "value": "100,100", "mouse_action": "down", "fidelity": "raw"}
        up = {"type": LEFT, "value": "120,120", "mouse_action": "up", "fidelity": "raw"}
        h._execute_left_click(_Ctx(down))
        self.assertEqual(timing.buttons, ["left"])
        h._execute_left_click(_Ctx(up))
        self.assertEqual(timing.buttons, [])


if __name__ == "__main__":
    unittest.main()
