# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.
"""录制长按采集契约（SUP/批次 1，2026-10-19）。

验证点：按键事件必须带**可测量**的按下→松开时长，而不是 `started == ended`。
1. 按下再抬起 → 事件的 `ended` 被回填为抬起时刻（即时长 > 0）；
2. 抬起发生在按下之前的事件不会被误改；
3. 录制结束时仍未抬起的键，`ended` 在结束点被补齐（时间线截断），
   且只补记录、不触碰真实按键状态。
"""

import ctypes
import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fukua_rpa.ui import input_tools  # noqa: E402

VK_A = 0x41


def _make_handler():
    thread = input_tools.HookThread()
    thread.recording_event.set()
    thread.stop_event.clear()
    return thread


def _key_event(thread, wparam, vk, clock=None):
    struct = input_tools.KBDLLHOOKSTRUCT()
    struct.vkCode = vk
    param = ctypes.byref(struct)
    if clock is not None:
        with mock.patch.object(input_tools.time, "monotonic", clock):
            return thread.keyboard_handler(0, wparam, param)
    return thread.keyboard_handler(0, wparam, param)


def _mouse_event(thread, wparam, x, y, mouse_data=0, clock=None):
    struct = input_tools.MSLLHOOKSTRUCT()
    struct.pt.x = x
    struct.pt.y = y
    struct.mouseData = mouse_data
    param = ctypes.byref(struct)
    if clock is not None:
        with mock.patch.object(input_tools.time, "monotonic", clock):
            return thread.mouse_handler(0, wparam, param)
    return thread.mouse_handler(0, wparam, param)


class RecordingKeyHoldCaptureTests(unittest.TestCase):
    def test_key_up_backfills_ended_with_measurable_duration(self):
        thread = _make_handler()
        ticks = iter([100.0, 100.4, 100.4])
        _key_event(thread, input_tools.WM_KEYDOWN, VK_A, clock=lambda: next(ticks))
        _key_event(thread, input_tools.WM_KEYUP, VK_A, clock=lambda: next(ticks))
        events = list(thread.events)
        self.assertEqual(len(events), 1)
        started, kind, value, ended = events[0]
        self.assertEqual(kind, "hotkey")
        self.assertEqual(value, "a")
        self.assertGreater(ended, started)
        self.assertAlmostEqual(ended - started, 0.4, places=3)

    def test_still_pressed_key_is_truncated_at_recording_end(self):
        """结束录制时未收到抬起的键：ended 在结束点补齐，不改动真实按键。"""
        thread = _make_handler()
        ticks = iter([10.0, 30.0])
        _key_event(thread, input_tools.WM_KEYDOWN, VK_A, clock=lambda: next(ticks))
        thread.truncate_pending_key_events(now=30.0)
        started, _kind, _value, ended = thread.events[0]
        self.assertEqual(started, 10.0)
        self.assertEqual(ended, 30.0)

    def test_raw_stream_records_down_repeat_and_up(self):
        """批次 2：原始流按单键记录 down/repeat/up，修饰键不被折叠。"""
        thread = _make_handler()
        vk_shift = 0x10
        ticks = iter([1.0, 1.2, 1.3, 1.5, 1.8, 1.9])
        _key_event(thread, input_tools.WM_KEYDOWN, VK_A, clock=lambda: next(ticks))
        _key_event(thread, input_tools.WM_KEYDOWN, VK_A, clock=lambda: next(ticks))
        _key_event(thread, input_tools.WM_KEYDOWN, vk_shift, clock=lambda: next(ticks))
        _key_event(thread, input_tools.WM_KEYUP, vk_shift, clock=lambda: next(ticks))
        _key_event(thread, input_tools.WM_KEYUP, VK_A, clock=lambda: next(ticks))
        raw = thread.raw_input_events
        self.assertEqual(
            [(item[1], item[2], item[3]) for item in raw],
            [
                ("key", "a", "down"),
                ("key", "a", "repeat"),
                ("key", "shift", "down"),
                ("key", "shift", "up"),
                ("key", "a", "up"),
            ],
        )
        times = [item[0] for item in raw]
        self.assertEqual(times, sorted(times))

    def test_raw_stream_is_truncated_at_recording_end(self):
        """结束录制时仍未松开的键：原始流补一条 up，且不触碰真实按键。"""
        thread = _make_handler()
        _key_event(thread, input_tools.WM_KEYDOWN, VK_A, clock=lambda: 10.0)
        self.assertEqual(thread._raw_key_down, {VK_A})
        thread.truncate_pending_key_events(now=12.0)
        self.assertEqual(thread._raw_key_down, set())
        self.assertEqual(thread.raw_input_events[-1][1:], ["key", "a", "up"])
        self.assertEqual(thread.raw_input_events[-1][0], 12.0)

    def test_raw_stream_stops_at_ceiling(self):
        """原始流有上限：超限停止追加并置截断标记，不静默丢弃。"""
        thread = _make_handler()
        with mock.patch.object(input_tools, "MAX_RECORDED_EVENTS", 2):
            _key_event(thread, input_tools.WM_KEYDOWN, VK_A, clock=lambda: 1.0)
            _key_event(thread, input_tools.WM_KEYUP, VK_A, clock=lambda: 1.1)
            _key_event(thread, input_tools.WM_KEYDOWN, VK_A, clock=lambda: 1.2)
        self.assertEqual(len(thread.raw_input_events), 2)
        self.assertTrue(thread.raw_events_truncated)

    def test_truncation_closes_held_keys(self):
        """截断发生在按住期间：必须在截断点补 up，否则时间线会被校验拒绝。"""
        thread = _make_handler()
        vk_b = 0x42
        with mock.patch.object(input_tools, "MAX_RECORDED_EVENTS", 2):
            _key_event(thread, input_tools.WM_KEYDOWN, VK_A, clock=lambda: 1.0)
            _key_event(thread, input_tools.WM_KEYDOWN, vk_b, clock=lambda: 1.2)
            # 第三个事件触发截断：此时 a、b 都仍按住
            _key_event(thread, input_tools.WM_KEYDOWN, VK_A, clock=lambda: 1.4)
        self.assertTrue(thread.raw_events_truncated)
        self.assertEqual(thread._raw_key_down, set())
        actions = [(item[2], item[3]) for item in thread.raw_input_events]
        self.assertEqual(actions[-2:], [("a", "up"), ("b", "up")])
        from fukua_rpa.recording_model import recorded_events_to_timeline

        task, _reason = recorded_events_to_timeline(thread.raw_input_events)
        self.assertIsNotNone(task)
        self.assertEqual(task["input_events"][-1]["action"], "up")

    def test_moves_are_sampled_only_while_dragging(self):
        """普通移动不入时间线；按住左键后的移动按「时间或距离」节流采样。"""
        thread = _make_handler()
        # 未按下：移动不记录
        _mouse_event(thread, 0x0200, 100, 100, clock=lambda: 1.0)
        self.assertEqual(thread.raw_input_events, [])
        _mouse_event(thread, 0x0201, 100, 100, clock=lambda: 1.1)
        _mouse_event(thread, 0x0200, 120, 100, clock=lambda: 1.12)
        _mouse_event(thread, 0x0200, 121, 100, clock=lambda: 1.13)
        _mouse_event(thread, 0x0200, 160, 140, clock=lambda: 1.2)
        _mouse_event(thread, 0x0202, 160, 140, clock=lambda: 1.3)
        moves = [item for item in thread.raw_input_events if item[1] == "move"]
        # 121,100 距上一个采样点仅 1px 且时间差 < 0.02s → 被节流
        self.assertEqual(len(moves), 2)

    def test_truncation_closes_held_drag(self):
        """结束录制时仍在拖拽：必须补鼠标 up，否则采样点会被转换端丢弃。"""
        thread = _make_handler()
        _mouse_event(thread, 0x0201, 0, 0, clock=lambda: 1.0)
        _mouse_event(thread, 0x0200, 50, 0, clock=lambda: 1.1)
        self.assertEqual(thread._raw_mouse_down, {"left"})
        thread.truncate_pending_key_events(now=1.2)
        self.assertEqual(thread._raw_mouse_down, set())
        self.assertEqual(thread.raw_input_events[-1][1:4], ["mouse", "left", "up"])
        # 补 up 之后，采样点必须仍能被识别为需要时间线
        from fukua_rpa.recording_model import recorded_events_to_timeline

        task, reason = recorded_events_to_timeline(
            thread.raw_input_events, include_mouse=True
        )
        self.assertIsNotNone(task)
        self.assertIn("C5", reason)
        self.assertEqual(len(task["input_events"]), 3)

    def test_begin_recording_clears_raw_stream(self):
        thread = _make_handler()
        _key_event(thread, input_tools.WM_KEYDOWN, VK_A, clock=lambda: 1.0)
        self.assertTrue(thread.raw_input_events)
        # begin_recording 只在钩子已激活时生效（与运行期状态一致）
        thread.is_active = True
        thread.begin_recording()
        self.assertEqual(thread.raw_input_events, [])
        self.assertFalse(thread.raw_events_truncated)
        self.assertEqual(thread._raw_key_down, set())

    def test_recorded_hold_flows_into_step_parameter(self):
        """采集到的事件经转换后，按住时长进入步骤级 key_hold_duration。"""
        from fukua_rpa.recording_model import recorded_events_to_tasks

        tasks = recorded_events_to_tasks([[100.0, "hotkey", "a", 100.4]])
        self.assertEqual(tasks[0]["type"], 7.0)
        self.assertEqual(tasks[0]["value"], "a")
        self.assertEqual(tasks[0]["key_hold_duration"], "0.400")


if __name__ == "__main__":
    unittest.main()
