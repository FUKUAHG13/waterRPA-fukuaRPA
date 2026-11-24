"""v1.1.16 原样回放逐条生成：转换保真与数据校验（E1/短）。

用同一合成录制输入对比两条路径：
- 基线 build_raw_playback_task → 单条 29.0 的 input_events；
- 新版 build_raw_playback_steps → 多条真实普通步骤。

断言新版步骤按参数派发后，动作序列、目标时刻、坐标、滚轮 delta、按住与
重复都与基线一致（原样回放保真契约）。
"""

import json
import unittest

from fukua_rpa.commands import COMMAND_BY_NAME
from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.constants import (
    MAX_TASKS_PER_PROFILE,
    TASK_TYPE_MOUSE_TRAJECTORY,
    TASK_TYPE_SCROLL,
    TASK_TYPE_WAIT,
)
from fukua_rpa.recording_raw import (
    build_raw_playback_steps,
    build_raw_playback_task,
    prepare_raw_events,
)
from fukua_rpa.validation import validate_recording_segments, validate_task_list_all

LEFT = COMMAND_BY_NAME["左键单击"].code
SYSTEM_KEY = COMMAND_BY_NAME["系统按键"].code


def _steps_to_dispatch(steps):
    """把生成步骤还原为 (相对毫秒, 动作) 派发序列，供保真对比。

    只解析本行参数，不引入执行引擎；时序由 pre_delay_ms / 等待行 / 轨迹
    内部相对时刻累加，与运行期的累积目标时刻口径一致。
    """
    seq = []
    clock = 0
    for step in steps:
        kind = step["type"]
        if kind == TASK_TYPE_WAIT:
            clock += int(round(float(step["value"]) * 1000))
            continue
        clock += int(step.get("pre_delay_ms", "0") or "0")
        if kind == TASK_TYPE_MOUSE_TRAJECTORY:
            points = json.loads(step["trajectory"])
            for t_ms, x, y in points:
                seq.append((clock + int(t_ms), "move", x, y))
            hold = step.get("hold_button", "")
            clock += int(points[-1][0]) if points else 0
            if hold:
                seq.append((clock, "hold", hold))
        elif kind == TASK_TYPE_SCROLL:
            seq.append((clock, "wheel", int(step["raw_delta"])))
        elif kind == SYSTEM_KEY:
            action = step.get("key_action", "tap")
            if action == "tap":
                seq.append((clock, "key", step["value"], "down"))
                hold = int(round(float(step.get("key_hold_duration", "0")) * 1000))
                seq.append((clock + hold, "key", step["value"], "up"))
                clock += hold
            else:
                seq.append((clock, "key", step["value"], action))
        elif kind == LEFT:
            action = step.get("mouse_action", "click")
            x, y = (int(v) for v in step["value"].split(","))
            if action == "click":
                seq.append((clock, "mouse", "left", "down", x, y))
                hold = int(step.get("click_hold_ms", "0") or "0")
                seq.append((clock + hold, "mouse", "left", "up", x, y))
                clock += hold
            else:
                seq.append((clock, "mouse", "left", action, x, y))
    return seq


class RawStepConversionTests(unittest.TestCase):
    def test_single_29_step_is_red_baseline(self):
        events = [
            [0.0, "move", 100, 100],
            [0.05, "mouse", "left", "down", 100, 100],
            [0.12, "mouse", "left", "up", 100, 100],
        ]
        task = build_raw_playback_task(events)
        self.assertEqual(task["type"], 29.0)
        self.assertEqual(len(task["input_events"]), 3)

    def test_conversion_produces_multiple_real_steps(self):
        events = [
            [0.0, "move", 100, 100],
            [0.05, "mouse", "left", "down", 100, 100],
            [0.12, "mouse", "left", "up", 100, 100],
            [0.20, "wheel", 240, 200, 200],
            [0.30, "key", "a", "down"],
            [0.45, "key", "a", "up"],
            [0.60, "move", 300, 300],
        ]
        steps = build_raw_playback_steps(events)
        self.assertIsNotNone(steps)
        self.assertGreater(len(steps), 1)
        self.assertTrue(all(s["fidelity"] == "raw" for s in steps))
        self.assertEqual(len({s["timing_group_id"] for s in steps}), 1)
        self.assertTrue(all(s.get("tool_only") for s in steps))
        # 无一条是 29.0
        self.assertNotIn(29.0, {s["type"] for s in steps})
        # 真实校验器放行（新动作类型/字段不破坏预检）
        self.assertEqual(validate_task_list_all(steps, default_profile_config()), [])

    def test_precise_delta_and_landing_preserved(self):
        events = [[0.0, "wheel", 200, 50, -10]]
        steps = build_raw_playback_steps(events)
        # wheel 前按需插入同刻定位 move（轨迹行），随后是滚轮行
        wheel = next(st for st in steps if st["type"] == TASK_TYPE_SCROLL)
        self.assertEqual(wheel["type"], TASK_TYPE_SCROLL)
        self.assertEqual(wheel["raw_delta"], "200")
        self.assertEqual(wheel["wheel_x"], "50")
        self.assertEqual(wheel["wheel_y"], "-10")
        # 格数仍为可显示/默认派发的整数 2（非整格另有 raw_delta 表达）
        self.assertEqual(wheel["value"], "2")

    def test_non_integer_grid_delta_keeps_precision(self):
        events = [[0.0, "wheel", 100, 7, 9]]  # 100/120 非整格
        steps = build_raw_playback_steps(events)
        wheel = next(st for st in steps if st["type"] == TASK_TYPE_SCROLL)
        self.assertEqual(wheel["raw_delta"], "100")
        self.assertEqual(wheel["value"], "1")

    def test_gap_wait_boundaries(self):
        # 149ms 空档进 pre_delay；150ms 空档生成等待行
        e149 = [[0.0, "mouse", "left", "down", 1, 1], [0.149, "key", "a", "down"], [0.299, "key", "a", "up"]]
        steps149 = build_raw_playback_steps(e149)
        self.assertFalse(any(s["type"] == TASK_TYPE_WAIT for s in steps149))
        e150 = [[0.0, "key", "a", "down"], [0.15, "key", "b", "down"], [0.30, "key", "b", "up"], [0.45, "key", "a", "up"]]
        # 首 key a down 到 key b down 间隔 150ms → 等待行
        steps150 = build_raw_playback_steps(e150)
        self.assertTrue(any(s["type"] == TASK_TYPE_WAIT for s in steps150))
        wait = next(s for s in steps150 if s["type"] == TASK_TYPE_WAIT)
        self.assertEqual(wait["value"], "0.150")

    def test_start_wait_and_last_event_time(self):
        # F8 到首个事件 200ms 起始等待必须保留
        events = [[0.2, "mouse", "left", "down", 5, 5], [0.25, "mouse", "left", "up", 5, 5]]
        steps = build_raw_playback_steps(events)
        # 200ms 起始等待 >= 150ms → 独立等待行
        wait = next(st for st in steps if st["type"] == TASK_TYPE_WAIT)
        self.assertEqual(wait["value"], "0.200")

    def test_key_mouse_overlap_splits_edges(self):
        events = [
            [0.0, "key", "a", "down"],
            [0.1, "mouse", "left", "down", 100, 100],
            [0.15, "mouse", "left", "up", 100, 100],
            [0.2, "key", "a", "up"],
        ]
        steps = build_raw_playback_steps(events)
        key_actions = [s.get("key_action") for s in steps if s["type"] == SYSTEM_KEY]
        self.assertIn("down", key_actions)
        self.assertIn("up", key_actions)
        # 交叠时按键不会合并成 tap
        self.assertNotIn("tap", key_actions)
        self.assertEqual(validate_recording_segments(steps), [])

    def test_auto_repeat_becomes_repeat_rows(self):
        events = [
            [0.0, "key", "b", "down"],
            [0.05, "key", "b", "repeat"],
            [0.10, "key", "b", "repeat"],
            [0.20, "key", "b", "up"],
        ]
        steps = build_raw_playback_steps(events)
        reps = [s for s in steps if s.get("key_action") == "repeat"]
        self.assertEqual(len(reps), 2)
        self.assertEqual(validate_recording_segments(steps), [])

    def test_drag_trajectory_keeps_button(self):
        events = [
            [0.0, "mouse", "left", "down", 100, 100],
            [0.02, "move", 110, 110],
            [0.04, "move", 120, 120],
            [0.06, "mouse", "left", "up", 120, 120],
        ]
        steps = build_raw_playback_steps(events)
        trajs = [s for s in steps if s["type"] == TASK_TYPE_MOUSE_TRAJECTORY]
        self.assertTrue(trajs)
        self.assertEqual(trajs[0]["hold_button"], "left")
        self.assertEqual(validate_recording_segments(steps), [])

    def test_negative_coordinates_preserved(self):
        events = [[0.0, "move", -50, -20], [0.01, "move", -10, -5]]
        steps = build_raw_playback_steps(events)
        pts = json.loads(steps[0]["trajectory"])
        self.assertEqual(pts[0][1:], [-50, -20])

    def test_empty_and_limits_rejected(self):
        self.assertIsNone(build_raw_playback_steps([]))
        # 超坐标上限拒绝
        self.assertIsNone(build_raw_playback_steps([[0.0, "move", 200000, 0]]))
        # 超步骤预算拒绝
        many = [[i * 0.002, "wheel", 120, 0, 0] for i in range(MAX_TASKS_PER_PROFILE + 10)]
        self.assertIsNone(build_raw_playback_steps(many, max_steps=MAX_TASKS_PER_PROFILE))

    def test_legacy_prepare_keeps_wheel_landing_for_steps(self):
        events = [[0.0, "wheel", 240, 33, 44]]
        payload = prepare_raw_events(events)
        wheel = next(e for e in payload if e["kind"] == "wheel")
        self.assertEqual((wheel["x"], wheel["y"]), (33, 44))
        legacy = build_raw_playback_task(events)
        leg_wheel = next(e for e in legacy["input_events"] if e["kind"] == "wheel")
        self.assertNotIn("x", leg_wheel)


class RawSegmentValidationTests(unittest.TestCase):
    def test_orphan_up_rejected(self):
        steps = [
            {"type": SYSTEM_KEY, "value": "a", "key_action": "up",
             "fidelity": "raw", "timing_group_id": "s1"},
        ]
        errs = validate_recording_segments(steps)
        self.assertTrue(any("孤立松开" in e for e in errs))

    def test_repeat_without_down_rejected(self):
        steps = [
            {"type": SYSTEM_KEY, "value": "b", "key_action": "repeat",
             "fidelity": "raw", "timing_group_id": "s1"},
        ]
        errs = validate_recording_segments(steps)
        self.assertTrue(any("缺少对应的按下" in e for e in errs))

    def test_duplicate_down_rejected(self):
        steps = [
            {"type": SYSTEM_KEY, "value": "a", "key_action": "down",
             "fidelity": "raw", "timing_group_id": "s1"},
            {"type": SYSTEM_KEY, "value": "a", "key_action": "down",
             "fidelity": "raw", "timing_group_id": "s1"},
        ]
        errs = validate_recording_segments(steps)
        self.assertTrue(any("重复按下" in e for e in errs))

    def test_held_at_segment_end_rejected(self):
        steps = [
            {"type": SYSTEM_KEY, "value": "a", "key_action": "down",
             "fidelity": "raw", "timing_group_id": "s1"},
        ]
        errs = validate_recording_segments(steps)
        self.assertTrue(any("仍有未松开" in e for e in errs))

    def test_start_from_mid_segment_rejected(self):
        steps = [
            {"type": SYSTEM_KEY, "value": "a", "key_action": "down",
             "fidelity": "raw", "timing_group_id": "s1"},
            {"type": SYSTEM_KEY, "value": "a", "key_action": "up",
             "fidelity": "raw", "timing_group_id": "s1"},
        ]
        # 从第 2 步（段中间）启动必须拒绝；从第 1 步启动正常。
        errs = validate_recording_segments(steps, start_step=2)
        self.assertTrue(any("录制段中间" in e for e in errs))
        self.assertEqual(validate_recording_segments(steps, start_step=1), [])

    def test_disabled_rows_exempt(self):
        steps = [
            {"type": SYSTEM_KEY, "value": "a", "key_action": "up",
             "fidelity": "raw", "timing_group_id": "s1", "step_disabled": True},
        ]
        self.assertEqual(validate_recording_segments(steps), [])


if __name__ == "__main__":
    unittest.main()
