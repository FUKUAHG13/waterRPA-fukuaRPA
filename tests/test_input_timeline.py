"""录制输入时序（批次 2）：转换、存储往返、回放与停止清理。

批次 2 只处理键盘事件（down / up / repeat）。鼠标与键鼠重叠属批次 3，
因此本文件证明的是「按住期间发生其他事件不打断按键状态」这一模型本身，
用第二个按键作为插入事件；鼠标事件的同型断言由批次 3 补齐。
"""

import json
import threading
import unittest
from unittest import mock

from fukua_rpa.actions.input_timeline import (
    ActionsInputTimelineMixin,
    normalize_timeline_events,
)
from fukua_rpa.constants import TASK_TYPE_INPUT_TIMELINE
from fukua_rpa.recording_model import build_recording_tasks, recorded_events_to_timeline
from fukua_rpa.validation import TaskListValidator


class _Context:
    """Minimal stand-in for StepActionContext."""

    def __init__(self, task):
        self.task = task
        self.value = str(task.get("value", ""))


class _Harness(ActionsInputTimelineMixin):
    """Playback harness: records dispatch order without touching real input."""

    def __init__(self, stop_after_calls=None):
        self._injected_input_lock = threading.Lock()
        self._injected_keys = []
        self._injected_mouse_buttons = set()
        self.calls = []
        self.waits = []
        self.stop = False
        self.log_level = 0
        self.stop_after_calls = stop_after_calls
        self.input_backend = mock.Mock()
        # 虚拟时钟：回放按 time.monotonic 计算间隔，测试必须让它随等待推进，
        # 否则每次等待都会被算成「从起点到现在」的累计值。
        self.clock = 1000.0

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
        self._injected_keys.append(key)
        self._maybe_stop()
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
        self._maybe_stop()
        return True

    def _tracked_mouse_up(self, button):
        self.calls.append(("mup", button))
        self._injected_mouse_buttons.discard(button)
        return True

    def _maybe_stop(self):
        if (
            self.stop_after_calls is not None
            and len(self.calls) >= self.stop_after_calls
        ):
            self.stop = True


def _run(harness, task):
    """Run the handler with a virtual clock so pacing is deterministic."""
    with mock.patch(
        "fukua_rpa.actions.input_timeline.time.monotonic",
        side_effect=lambda: harness.clock,
    ):
        return harness._execute_input_timeline(_Context(task))


def _timeline_task(events):
    return {
        "type": TASK_TYPE_INPUT_TIMELINE,
        "value": "输入时序",
        "input_events": events,
        "recorded_duration": events[-1]["t"] if events else 0.0,
    }


class TimelineConversionTests(unittest.TestCase):
    def test_repeat_produces_timeline(self):
        raw = [
            [100.0, "key", "w", "down"],
            [100.3, "key", "w", "repeat"],
            [100.6, "key", "w", "repeat"],
            [100.9, "key", "w", "up"],
        ]
        tasks = build_recording_tasks([], raw)
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["type"], TASK_TYPE_INPUT_TIMELINE)
        self.assertEqual(len(task["input_events"]), 4)
        self.assertEqual([e["action"] for e in task["input_events"]],
                         ["down", "repeat", "repeat", "up"])
        # 相对时间：首个事件归零，后续保持真实间隔
        self.assertEqual(task["input_events"][0]["t"], 0.0)
        self.assertAlmostEqual(task["input_events"][1]["t"], 0.3, places=3)
        self.assertAlmostEqual(task["recorded_duration"], 0.9, places=3)

    def test_overlapping_keys_produce_timeline_in_real_order(self):
        raw = [
            [10.0, "key", "w", "down"],
            [10.1, "key", "shift", "down"],
            [10.5, "key", "shift", "up"],
            [10.9, "key", "w", "up"],
        ]
        task, reason = recorded_events_to_timeline(raw)
        self.assertIsNotNone(task)
        self.assertIn("C2", reason)
        self.assertEqual(
            [(e["key"], e["action"]) for e in task["input_events"]],
            [("w", "down"), ("shift", "down"), ("shift", "up"), ("w", "up")],
        )

    def test_single_key_hold_keeps_legacy_steps(self):
        """单键长按仍由批次 1 的 key_hold_duration 表达，不产出时间线。"""
        raw = [[5.0, "key", "a", "down"], [5.4, "key", "a", "up"]]
        tasks = build_recording_tasks([[5.0, "hotkey", "a", 5.4]], raw)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["type"], 7.0)
        self.assertEqual(tasks[0]["key_hold_duration"], "0.400")

    def test_isolated_repeat_gets_synthetic_down(self):
        """录制开始时键已按住：首个事件是 repeat，必须补出同刻的 down。"""
        raw = [[20.0, "key", "a", "repeat"], [20.2, "key", "a", "up"]]
        task, _reason = recorded_events_to_timeline(raw)
        self.assertIsNotNone(task)
        self.assertEqual(task["input_events"][0]["action"], "down")
        self.assertEqual(task["input_events"][0]["key"], "a")
        self.assertEqual(task["input_events"][0]["t"], 0.0)
        self.assertEqual(task["input_events"][1]["action"], "repeat")

    def test_key_mouse_overlap_produces_timeline(self):
        raw = [
            [0.0, "key", "w", "down"],
            [0.1, "mouse", "left", "down", 10, 10],
            [0.2, "mouse", "left", "up", 10, 10],
            [0.5, "key", "w", "up"],
        ]
        task, reason = recorded_events_to_timeline(raw, include_mouse=True)
        self.assertIsNotNone(task)
        self.assertIn("C3", reason)
        self.assertEqual(len(task["input_events"]), 4)

    def test_plain_click_still_uses_legacy_step(self):
        """没有采集到移动的点击仍走旧点击步骤。

        这是「未命中当前时间线触发条件」，不等于该点击没有时序：
        按下到抬起的间隔、两次点击之间的空隙都属时序，只是当前支持范围未覆盖。
        """
        raw = [
            [0.0, "mouse", "left", "down", 10, 10],
            [0.1, "mouse", "left", "up", 10, 10],
        ]
        task, reason = recorded_events_to_timeline(raw, include_mouse=True)
        self.assertIsNone(task)
        self.assertIsNone(reason)

    def test_any_drag_with_moves_produces_timeline(self):
        """只要拖拽期间采集到移动就保留时序——不再按「是否弯曲」丢弃。"""
        cases = {
            "匀速直线": [[0.0, "mouse", "left", "down", 0, 0],
                          [0.1, "move", 50, 0],
                          [0.2, "mouse", "left", "up", 100, 0]],
            "先停顿后移动": [[0.0, "mouse", "left", "down", 0, 0],
                              [0.5, "move", 50, 0],
                              [0.6, "mouse", "left", "up", 100, 0]],
            "直线变速": [[0.0, "mouse", "left", "down", 0, 0],
                          [0.1, "move", 10, 0],
                          [0.2, "move", 100, 0],
                          [0.3, "mouse", "left", "up", 100, 0]],
            "越过终点再折返": [[0.0, "mouse", "left", "down", 0, 0],
                                [0.1, "move", 500, 0],
                                [0.2, "move", 300, 0],
                                [0.3, "mouse", "left", "up", 300, 0]],
            "弯曲拖拽": [[0.0, "mouse", "left", "down", 0, 0],
                          [0.1, "move", 50, 80],
                          [0.2, "move", 80, 40],
                          [0.3, "mouse", "left", "up", 100, 0]],
        }
        for label, raw in cases.items():
            with self.subTest(label=label):
                task, reason = recorded_events_to_timeline(raw, include_mouse=True)
                self.assertIsNotNone(task, label)
                self.assertIn("C5", reason, label)
                self.assertEqual(len(task["input_events"]), len(raw), label)

    def test_mouse_events_are_ignored_in_batch_two(self):
        """批次 2 不产出鼠标时间线：纯鼠标录制仍走旧步骤。"""
        raw = [
            [1.0, "mouse", "left", "down", 10, 10],
            [1.2, "mouse", "left", "up", 10, 10],
        ]
        tasks = build_recording_tasks([[1.0, "left", (10, 10), 1.2]], raw)
        self.assertEqual(tasks[0]["type"], 1.0)


class TimelineStorageTests(unittest.TestCase):
    def setUp(self):
        self.validator = TaskListValidator({"scale_min": "0.8", "scale_max": "1.2", "scale_step": "0.05"})

    def test_round_trip_preserves_events_exactly(self):
        raw = [
            [1.0, "key", "w", "down"],
            [1.2, "key", "w", "repeat"],
            [1.5, "key", "w", "up"],
        ]
        task, _reason = recorded_events_to_timeline(raw)
        restored = json.loads(json.dumps(task, ensure_ascii=False))
        self.assertEqual(restored["input_events"], task["input_events"])
        self.assertEqual(restored["recorded_duration"], task["recorded_duration"])

    def test_validation_accepts_recorded_timeline(self):
        raw = [
            [1.0, "key", "w", "down"],
            [1.2, "key", "w", "repeat"],
            [1.5, "key", "w", "up"],
        ]
        task, _reason = recorded_events_to_timeline(raw)
        error = self.validator.validate([task])
        self.assertIsNone(error, error)

    def test_validation_rejects_unreleased_key(self):
        task = _timeline_task([{"t": 0.0, "kind": "key", "key": "w", "action": "down"}])
        error = self.validator.validate([task])
        self.assertIsNotNone(error)
        self.assertIn("未释放", error)

    def test_validation_rejects_repeat_without_press(self):
        task = _timeline_task(
            [
                {"t": 0.0, "kind": "key", "key": "w", "action": "repeat"},
                {"t": 0.2, "kind": "key", "key": "w", "action": "up"},
            ]
        )
        error = self.validator.validate([task])
        self.assertIsNotNone(error)
        self.assertIn("自动重复", error)

    def test_validation_rejects_events_beyond_time_ceiling(self):
        """回放侧会丢弃超过 24 小时的事件，校验侧必须同样拒绝，避免口径不一致。"""
        task = _timeline_task(
            [
                {"t": 0.0, "kind": "key", "key": "w", "action": "down"},
                {"t": 86_401.0, "kind": "key", "key": "w", "action": "up"},
            ]
        )
        error = self.validator.validate([task])
        self.assertIsNotNone(error)
        self.assertIn("86400", error)

    def test_validation_rejects_out_of_order_events(self):
        task = _timeline_task(
            [
                {"t": 0.5, "kind": "key", "key": "w", "action": "down"},
                {"t": 0.1, "kind": "key", "key": "w", "action": "up"},
            ]
        )
        error = self.validator.validate([task])
        self.assertIsNotNone(error)
        self.assertIn("顺序", error)


class TimelinePlaybackTests(unittest.TestCase):
    def test_dispatch_order_and_timing(self):
        task = _timeline_task(
            [
                {"t": 0.0, "kind": "key", "key": "w", "action": "down"},
                {"t": 0.2, "kind": "key", "key": "w", "action": "repeat"},
                {"t": 0.5, "kind": "key", "key": "w", "action": "up"},
            ]
        )
        harness = _Harness()
        result = _run(harness, task)
        self.assertEqual(result, "success")
        self.assertEqual(harness.calls, [("down", "w"), ("up", "w")])
        # 两个间隔由 wait 表达；repeat 不产生额外的 down/up
        self.assertEqual(len(harness.waits), 2)
        self.assertAlmostEqual(harness.waits[0], 0.2, places=2)
        self.assertAlmostEqual(harness.waits[1], 0.3, places=2)
        self.assertEqual(harness._injected_keys, [])
        self.assertEqual(harness.input_backend.key_down.call_count, 1)

    def test_held_key_survives_interleaved_events(self):
        """按住 w 期间派发另一个键的事件，w 必须保持按下（单线程模型成立）。"""
        task = _timeline_task(
            [
                {"t": 0.0, "kind": "key", "key": "w", "action": "down"},
                {"t": 0.1, "kind": "key", "key": "a", "action": "down"},
                {"t": 0.2, "kind": "key", "key": "a", "action": "up"},
                {"t": 0.3, "kind": "key", "key": "w", "action": "up"},
            ]
        )
        harness = _Harness()
        held_after_a_up = []
        original_up = harness._tracked_key_up

        def spy_up(key):
            result = original_up(key)
            held_after_a_up.append(tuple(harness._injected_keys))
            return result

        harness._tracked_key_up = spy_up
        result = _run(harness, task)
        self.assertEqual(result, "success")
        # a 释放后、w 仍按下
        self.assertEqual(held_after_a_up[0], ("w",))
        self.assertEqual(harness._injected_keys, [])

    def test_stop_releases_every_held_key(self):
        task = _timeline_task(
            [
                {"t": 0.0, "kind": "key", "key": "w", "action": "down"},
                {"t": 0.1, "kind": "key", "key": "shift", "action": "down"},
                {"t": 0.5, "kind": "key", "key": "shift", "action": "up"},
                {"t": 0.9, "kind": "key", "key": "w", "action": "up"},
            ]
        )
        harness = _Harness(stop_after_calls=2)
        result = _run(harness, task)
        self.assertEqual(result, "stopped")
        self.assertEqual(harness._injected_keys, [])
        # 逆序释放：先 shift 后 w
        self.assertEqual([c for c in harness.calls if c[0] == "up"],
                         [("up", "shift"), ("up", "w")])

    def test_empty_timeline_is_an_error(self):
        harness = _Harness()
        self.assertEqual(_run(harness, _timeline_task([])), "error")

    def test_mouse_events_dispatch_in_order(self):
        task = _timeline_task(
            [
                {"t": 0.0, "kind": "mouse", "button": "left", "action": "down",
                 "x": 10, "y": 20},
                {"t": 0.1, "kind": "move", "x": 30, "y": 40},
                {"t": 0.2, "kind": "mouse", "button": "left", "action": "up",
                 "x": 30, "y": 40},
            ]
        )
        harness = _Harness()
        self.assertEqual(_run(harness, task), "success")
        self.assertEqual(harness.calls, [("mdown", "left"), ("mup", "left")])
        # 时刻：0 → 0.1 → 0.2，两个间隔各 0.1 秒
        self.assertEqual([round(w, 2) for w in harness.waits], [0.1, 0.1])
        first_move = harness.input_backend.move_to.call_args_list[0]
        self.assertEqual(first_move.args, (10, 20))
        self.assertEqual(harness._injected_mouse_buttons, set())

    def test_wheel_keeps_delta_sign(self):
        task = _timeline_task(
            [
                {"t": 0.0, "kind": "wheel", "delta": -120},
                {"t": 0.1, "kind": "wheel", "delta": 240},
            ]
        )
        harness = _Harness()
        self.assertEqual(_run(harness, task), "success")
        self.assertEqual(
            [call.args[0] for call in harness.input_backend.scroll.call_args_list],
            [-120, 240],
        )

    def test_key_held_across_mouse_click(self):
        """按住 w 期间点击：w 在鼠标事件期间保持按下，抬键后清空。"""
        task = _timeline_task(
            [
                {"t": 0.0, "kind": "key", "key": "w", "action": "down"},
                {"t": 0.1, "kind": "mouse", "button": "left", "action": "down",
                 "x": 5, "y": 5},
                {"t": 0.15, "kind": "mouse", "button": "left", "action": "up",
                 "x": 5, "y": 5},
                {"t": 0.4, "kind": "key", "key": "w", "action": "up"},
            ]
        )
        harness = _Harness()
        held = []
        original_up = harness._tracked_mouse_up

        def spy(button):
            held.append(tuple(harness._injected_keys))
            return original_up(button)

        harness._tracked_mouse_up = spy
        self.assertEqual(_run(harness, task), "success")
        self.assertEqual(held, [("w",)])
        # 时刻：0 → 0.1 → 0.15 → 0.4
        self.assertEqual([round(w, 2) for w in harness.waits], [0.1, 0.05, 0.25])
        self.assertEqual(harness._injected_keys, [])

    def test_stop_releases_held_mouse_button(self):
        task = _timeline_task(
            [
                {"t": 0.0, "kind": "mouse", "button": "left", "action": "down",
                 "x": 1, "y": 1},
                {"t": 0.5, "kind": "mouse", "button": "left", "action": "up",
                 "x": 1, "y": 1},
            ]
        )
        harness = _Harness(stop_after_calls=1)
        self.assertEqual(_run(harness, task), "stopped")
        self.assertEqual(harness._injected_mouse_buttons, set())

    def test_validation_rejects_unreleased_mouse_button(self):
        task = _timeline_task(
            [{"t": 0.0, "kind": "mouse", "button": "left", "action": "down",
              "x": 1, "y": 1}]
        )
        error = TaskListValidator(
            {"scale_min": "0.8", "scale_max": "1.2", "scale_step": "0.05"}
        ).validate([task])
        self.assertIsNotNone(error)
        self.assertIn("鼠标按钮", error)

    def test_validation_rejects_invalid_mouse_point(self):
        task = _timeline_task(
            [
                {"t": 0.0, "kind": "mouse", "button": "left", "action": "down",
                 "x": "abc", "y": 1},
                {"t": 0.1, "kind": "mouse", "button": "left", "action": "up",
                 "x": 1, "y": 1},
            ]
        )
        error = TaskListValidator(
            {"scale_min": "0.8", "scale_max": "1.2", "scale_step": "0.05"}
        ).validate([task])
        self.assertIsNotNone(error)
        self.assertIn("坐标", error)

    def test_end_to_end_capture_convert_save_load_playback(self):
        """采集→转换→保存→加载→校验→回放：键鼠重叠全链路。"""
        raw = [
            [100.0, "key", "w", "down"],
            [100.2, "mouse", "left", "down", 640, 360],
            [100.26, "mouse", "left", "up", 640, 360],
            [100.6, "key", "w", "up"],
        ]
        tasks = build_recording_tasks([], raw, include_mouse=True)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["type"], TASK_TYPE_INPUT_TIMELINE)
        restored = json.loads(json.dumps(tasks, ensure_ascii=False))
        validator = TaskListValidator(
            {"scale_min": "0.8", "scale_max": "1.2", "scale_step": "0.05"}
        )
        self.assertIsNone(validator.validate(restored))
        harness = _Harness()
        self.assertEqual(_run(harness, restored[0]), "success")
        self.assertEqual(
            harness.calls,
            [("down", "w"), ("mdown", "left"), ("mup", "left"), ("up", "w")],
        )
        self.assertEqual(harness._injected_keys, [])
        self.assertEqual(harness._injected_mouse_buttons, set())

    def test_drag_timing_survives_save_load_and_replay(self):
        """五类拖拽：顺序、相对时刻与坐标在保存/加载后回放中一致。"""
        cases = {
            "匀速直线": (
                [[0.0, "mouse", "left", "down", 0, 0],
                 [0.1, "move", 50, 0],
                 [0.2, "mouse", "left", "up", 100, 0]],
                [0.1, 0.1],
                [(0, 0), (50, 0)],
            ),
            "先停顿后移动": (
                [[0.0, "mouse", "left", "down", 0, 0],
                 [0.5, "move", 50, 0],
                 [0.6, "mouse", "left", "up", 100, 0]],
                [0.5, 0.1],
                [(0, 0), (50, 0)],
            ),
            "直线变速": (
                [[0.0, "mouse", "left", "down", 0, 0],
                 [0.1, "move", 10, 0],
                 [0.2, "move", 100, 0],
                 [0.3, "mouse", "left", "up", 100, 0]],
                [0.1, 0.1, 0.1],
                [(0, 0), (10, 0), (100, 0)],
            ),
            "越过终点再折返": (
                [[0.0, "mouse", "left", "down", 0, 0],
                 [0.1, "move", 500, 0],
                 [0.2, "move", 300, 0],
                 [0.3, "mouse", "left", "up", 300, 0]],
                [0.1, 0.1, 0.1],
                [(0, 0), (500, 0), (300, 0)],
            ),
            "弯曲拖拽": (
                [[0.0, "mouse", "left", "down", 0, 0],
                 [0.1, "move", 50, 80],
                 [0.2, "move", 80, 40],
                 [0.3, "mouse", "left", "up", 100, 0]],
                [0.1, 0.1, 0.1],
                [(0, 0), (50, 80), (80, 40)],
            ),
        }
        validator = TaskListValidator(
            {"scale_min": "0.8", "scale_max": "1.2", "scale_step": "0.05"}
        )
        for label, (raw, expected_waits, expected_moves) in cases.items():
            with self.subTest(label=label):
                tasks = build_recording_tasks([], raw, include_mouse=True)
                self.assertEqual(len(tasks), 1, label)
                restored = json.loads(json.dumps(tasks, ensure_ascii=False))
                self.assertIsNone(validator.validate(restored), label)
                harness = _Harness()
                self.assertEqual(_run(harness, restored[0]), "success", label)
                # 顺序：先定位再按下，最后抬起
                self.assertEqual(
                    harness.calls, [("mdown", "left"), ("mup", "left")], label
                )
                # 相对时刻：逐段间隔与录制一致
                self.assertEqual(
                    [round(wait, 2) for wait in harness.waits],
                    expected_waits,
                    label,
                )
                # 采样点坐标按原顺序回放
                self.assertEqual(
                    [call.args for call in harness.input_backend.move_to.call_args_list],
                    expected_moves,
                    label,
                )
                self.assertEqual(harness._injected_mouse_buttons, set(), label)

    def test_curved_drag_end_to_end_replays_move_path(self):
        """纯鼠标弯曲拖拽：转换 → 保存/加载 → 回放，中间轨迹按事件还原。"""
        raw = [
            [10.0, "mouse", "left", "down", 0, 0],
            [10.05, "move", 50, 80],
            [10.10, "move", 80, 40],
            [10.20, "mouse", "left", "up", 100, 0],
        ]
        tasks = build_recording_tasks([], raw, include_mouse=True)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["type"], TASK_TYPE_INPUT_TIMELINE)
        restored = json.loads(json.dumps(tasks, ensure_ascii=False))
        validator = TaskListValidator(
            {"scale_min": "0.8", "scale_max": "1.2", "scale_step": "0.05"}
        )
        self.assertIsNone(validator.validate(restored))
        harness = _Harness()
        self.assertEqual(_run(harness, restored[0]), "success")
        moves = [call.args for call in harness.input_backend.move_to.call_args_list]
        self.assertEqual(moves, [(0, 0), (50, 80), (80, 40)])
        self.assertEqual(harness.calls, [("mdown", "left"), ("mup", "left")])
        self.assertEqual(harness._injected_mouse_buttons, set())

    def test_normalize_drops_invalid_entries(self):
        events = normalize_timeline_events(
            [
                {"t": -1, "kind": "key", "key": "w", "action": "down"},
                {"t": 0.0, "kind": "mystery", "key": "w", "action": "down"},
                {"t": 0.1, "kind": "key", "key": "", "action": "down"},
                {"t": 0.2, "kind": "key", "key": "w", "action": "sideways"},
                {"t": 0.3, "kind": "key", "key": "w", "action": "down"},
            ]
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["t"], 0.3)


if __name__ == "__main__":
    unittest.main()
