import logging
import unittest
from unittest import mock

import fukua_rpa.recording_model as recording_model
from fukua_rpa.recording_model import recorded_events_to_tasks


class RecordingModelTests(unittest.TestCase):
    def test_same_point_clicks_within_threshold_become_double_click(self):
        tasks = recorded_events_to_tasks(
            [
                (1.0, "left", (100, 200), 1.04),
                (1.22, "left", (102, 199), 1.26),
            ]
        )
        self.assertEqual(tasks, [{"type": 2.0, "value": "102,199"}])

    def test_distant_or_slow_clicks_remain_independent(self):
        tasks = recorded_events_to_tasks(
            [
                (1.0, "left", (10, 10), 1.02),
                (1.7, "left", (10, 10), 1.72),
            ]
        )
        self.assertEqual(tasks[0], {"type": 1.0, "value": "10,10"})
        self.assertEqual(tasks[1], {"type": 5.0, "value": "0.68"})
        self.assertEqual(tasks[2], {"type": 1.0, "value": "10,10"})

    def test_modifier_combination_stays_one_hotkey_task(self):
        tasks = recorded_events_to_tasks([(1.0, "hotkey", "ctrl+shift+a", 1.0)])
        # 长按语义（2026-10-19）：按键步骤显式写入实测按住时长，避免继承全局设置。
        self.assertEqual(
            tasks,
            [{"type": 7.0, "value": "ctrl+shift+a", "key_hold_duration": "0.000"}],
        )

    def test_wheel_events_merge_and_convert_native_delta_to_notches(self):
        tasks = recorded_events_to_tasks(
            [
                (1.0, "scroll", 120, 1.0),
                (1.1, "scroll", 120, 1.1),
                (1.2, "scroll", -120, 1.2),
            ]
        )
        self.assertEqual(tasks, [{"type": 6.0, "value": "1"}])

    def test_drag_preserves_duration_and_wait_uses_previous_end(self):
        tasks = recorded_events_to_tasks(
            [
                (1.0, "left_drag", (1, 2, 30, 40), 1.8),
                (2.0, "hotkey", "ctrl+c", 2.0),
            ]
        )
        self.assertEqual(tasks[0]["type"], 10.0)
        self.assertEqual(tasks[0]["recorded_duration"], 0.8)
        self.assertEqual(tasks[1], {"type": 5.0, "value": "0.20"})
        self.assertEqual(
            tasks[2],
            {"type": 7.0, "value": "ctrl+c", "key_hold_duration": "0.000"},
        )

    def test_excess_recorded_events_are_truncated_with_warning(self):
        # S5: exceeding the cap must truncate AND report how many were dropped
        # (previously the drop was silent).
        events = [
            (float(i), "left", (i, i), float(i) + 0.01) for i in range(10)
        ]
        with (
            mock.patch.object(recording_model, "MAX_RECORDED_EVENTS", 3),
            self.assertLogs(level=logging.WARNING) as captured,
        ):
            # High wait_threshold avoids inserting wait steps, so the task
            # count reflects the truncated aggregated-event count directly.
            tasks = recorded_events_to_tasks(events, wait_threshold=999)
        self.assertLessEqual(len(tasks), 3)
        self.assertTrue(
            any("超过上限" in message or "截断" in message for message in captured.output)
        )

    def test_measured_hold_duration_is_written_without_threshold(self):
        """按住时长按实测写入，等待阈值不得兼作按住阈值。"""
        tasks = recorded_events_to_tasks([(1.0, "hotkey", "w", 1.05)])
        self.assertEqual(tasks[0]["type"], 7.0)
        self.assertEqual(tasks[0]["value"], "w")
        # 0.05 s 小于默认 wait_threshold(0.15)，仍必须按实测保留，不被视作瞬时。
        self.assertEqual(tasks[0]["key_hold_duration"], "0.050")
        self.assertEqual(len(tasks), 1)

    def test_hold_then_gap_then_click_keeps_both_hold_and_wait(self):
        """按住 1.0 s、松开后隔 0.3 s 点击 → 按住 + 等待 + 点击，不重复计算。"""
        tasks = recorded_events_to_tasks(
            [
                (1.0, "hotkey", "w", 2.0),
                (2.3, "left", (10, 20), 2.3),
            ]
        )
        self.assertEqual(tasks[0]["type"], 7.0)
        self.assertEqual(tasks[0]["key_hold_duration"], "1.000")
        self.assertEqual(tasks[1], {"type": 5.0, "value": "0.30"})
        self.assertEqual(tasks[2]["type"], 1.0)

    def test_recorded_hold_is_parsed_by_the_key_action(self):
        """录得的按住时长必须能被「系统按键」执行器原样解析（不依赖全局设置）。"""
        from fukua_rpa.actions.flow import _parse_optional_float

        # 按下在 1.0、抬起在 2.6 → 实测按住 1.6 秒
        task = recorded_events_to_tasks([(1.0, "hotkey", "w", 2.6)])[0]
        self.assertEqual(task["key_hold_duration"], "1.600")
        parsed = _parse_optional_float(task.get("key_hold_duration"), 3600)
        self.assertEqual(parsed, 1.6)
        # 未显式写入时解析结果为 None，由执行器回退全局「按键按住(s)」。
        self.assertIsNone(_parse_optional_float(None, 3600))

class RawPlaybackConversionTests(unittest.TestCase):
    """原样回放转换：无条件单步、空录制、同刻定位、模式隔离。"""

    def setUp(self):
        from fukua_rpa.recording_raw import build_raw_playback_task

        self.build = build_raw_playback_task

    def test_single_plain_click_produces_one_timeline_step(self):
        raw = [
            [0.5, "mouse", "left", "down", 100, 200],
            [0.55, "mouse", "left", "up", 100, 200],
        ]
        task = self.build(raw)
        self.assertIsNotNone(task)
        self.assertEqual(task["type"], 29.0)
        self.assertEqual(task["playback_mode"], "raw")
        self.assertEqual(len(task["input_events"]), 2)

    def test_free_move_only_produces_one_step(self):
        raw = [[0.1, "move", 10, 10], [0.11, "move", 20, 20], [0.12, "move", 30, 30]]
        task = self.build(raw)
        self.assertIsNotNone(task)
        self.assertEqual(task["type"], 29.0)
        self.assertLessEqual(len(task["input_events"]), 3)

    def test_single_key_produces_one_step(self):
        raw = [[0.2, "key", "a", "down"], [0.4, "key", "a", "up"]]
        task = self.build(raw)
        self.assertIsNotNone(task)
        self.assertEqual([e["action"] for e in task["input_events"]], ["down", "up"])

    def test_empty_recording_returns_none(self):
        self.assertIsNone(self.build([]))
        # 全非法事件同样视为空
        self.assertIsNone(self.build([[0.0, "bogus"], ["bad", "move", 1, 2]]))

    def test_initial_wait_is_preserved(self):
        raw = [[1.5, "key", "a", "down"], [1.6, "key", "a", "up"]]
        task = self.build(raw)
        self.assertAlmostEqual(task["input_events"][0]["t"], 1.5)
        self.assertAlmostEqual(task["recorded_duration"], 1.6)

    def test_up_injects_same_timestamp_move_when_landing_differs(self):
        # 松开落点与最后一个采样点不同 → 必须在其前插入同刻 move
        raw = [
            [0.0, "mouse", "left", "down", 10, 10],
            [0.1, "move", 50, 50],
            [0.2, "mouse", "left", "up", 80, 80],
        ]
        task = self.build(raw)
        events = task["input_events"]
        up_idx = next(i for i, e in enumerate(events) if e["kind"] == "mouse" and e["action"] == "up")
        prev = events[up_idx - 1]
        self.assertEqual(prev["kind"], "move")
        self.assertEqual((prev["x"], prev["y"]), (80, 80))
        self.assertAlmostEqual(prev["t"], events[up_idx]["t"])

    def test_wheel_uses_its_own_landing_point(self):
        raw = [
            [0.0, "move", 10, 10],
            [0.1, "wheel", 120, 300, 400],
        ]
        task = self.build(raw)
        events = task["input_events"]
        wheel_idx = next(i for i, e in enumerate(events) if e["kind"] == "wheel")
        self.assertEqual(events[wheel_idx]["delta"], 120)
        self.assertNotIn("x", events[wheel_idx])
        # wheel 前有同刻 move 到 (300,400)
        prev = events[wheel_idx - 1]
        self.assertEqual((prev["x"], prev["y"]), (300, 400))

    def test_legacy_wheel_shape_without_point_still_accepted(self):
        raw = [[0.0, "wheel", -120]]
        task = self.build(raw)
        self.assertEqual(task["input_events"][0]["delta"], -120)

    def test_equal_timestamps_keep_capture_order(self):
        raw = [
            [0.5, "key", "ctrl", "down"],
            [0.5, "key", "v", "down"],
            [0.5, "key", "v", "up"],
            [0.5, "key", "ctrl", "up"],
        ]
        events = self.build(raw)["input_events"]
        self.assertEqual(
            [(e["key"], e["action"]) for e in events],
            [("ctrl", "down"), ("v", "down"), ("v", "up"), ("ctrl", "up")],
        )

    def test_normal_mode_converter_unchanged(self):
        # 普通模式：单键长按仍走既有步骤（不构成 29.0 触发条件），不因新增原样模式改变。
        raw = [[0.0, "key", "a", "down"], [0.5, "key", "a", "up"]]
        from fukua_rpa.recording_model import recorded_events_to_timeline

        timeline, reason = recorded_events_to_timeline(raw, include_mouse=True)
        self.assertIsNone(timeline)
        self.assertIsNone(reason)


class MixedRecordingSegmentationTests(unittest.TestCase):
    """普通录制分段：普通步骤 → 必要时序片段 → 普通步骤。"""

    def setUp(self):
        from fukua_rpa.recording_model import build_recording_tasks_detailed

        self.build = build_recording_tasks_detailed

    def test_pure_normal_matches_legacy_item_by_item(self):
        """纯普通录制：分段路径与旧版逐项一致（正常对照）。"""
        events = [(0.5, "left", (10, 20), 0.55), (0.9, "hotkey", "a", 0.95)]
        raw = [
            [0.5, "mouse", "left", "down", 10, 20],
            [0.55, "mouse", "left", "up", 10, 20],
            [0.9, "key", "a", "down"],
            [0.95, "key", "a", "up"],
        ]
        tasks, info = self.build(events, raw, include_mouse=True)
        self.assertEqual(info["timeline_segments"], 0)
        self.assertEqual(tasks, recorded_events_to_tasks(events))

    def test_auto_repeat_middle_is_one_timeline_fragment(self):
        events = [
            (0.1, "scroll", 120, 0.1),
            (0.5, "hotkey", "f", 0.58),
            (1.0, "hotkey", "a", 1.4),
            (2.0, "hotkey", "d", 2.08),
        ]
        raw = [
            [0.1, "wheel", 120],
            [0.5, "key", "f", "down"], [0.58, "key", "f", "up"],
            [1.0, "key", "a", "down"],
            [1.1, "key", "a", "repeat"], [1.2, "key", "a", "repeat"],
            [1.4, "key", "a", "up"],
            [2.0, "key", "d", "down"], [2.08, "key", "d", "up"],
        ]
        tasks, info = self.build(events, raw, include_mouse=True)
        self.assertEqual(info["timeline_segments"], 1)
        self.assertIn("C1", info["reason"])
        timeline = [t for t in tasks if t["type"] == 29.0]
        self.assertEqual(len(timeline), 1)
        # 前后普通动作各自保留，repeat 不生成孤立行
        values = [t.get("value") for t in tasks]
        self.assertIn("f", values)
        self.assertIn("d", values)
        # 前置滚轮只输出一次（不因分段重复生成）
        self.assertEqual(values.count("1"), 1)
        # 每个 raw 动作恰好消费一次：片段事件在 1.0~1.4
        frag = timeline[0]["input_events"]
        self.assertEqual([e["action"] for e in frag], ["down", "repeat", "repeat", "up"])

    def test_drag_with_moves_middle_keeps_samples(self):
        events = [
            (0.1, "scroll", 120, 0.1),
            (0.5, "left_drag", (10, 10, 60, 40), 0.9),
            (1.2, "hotkey", "d", 1.28),
        ]
        raw = [
            [0.1, "wheel", 120],
            [0.5, "mouse", "left", "down", 10, 10],
            [0.6, "move", 30, 20], [0.7, "move", 60, 40],
            [0.9, "mouse", "left", "up", 60, 40],
            [1.2, "key", "d", "down"], [1.28, "key", "d", "up"],
        ]
        tasks, info = self.build(events, raw, include_mouse=True)
        self.assertEqual(info["timeline_segments"], 1)
        self.assertIn("C5", info["reason"])
        frag = [t for t in tasks if t["type"] == 29.0][0]["input_events"]
        self.assertEqual([e["kind"] for e in frag].count("move"), 2)
        self.assertEqual([t.get("value") for t in tasks].count("d"), 1)

    def test_key_mouse_overlap_middle(self):
        events = [
            (0.1, "hotkey", "f", 0.18),
            (0.65, "left", (5, 5), 0.75),
            (1.1, "hotkey", "d", 1.18),
        ]
        raw = [
            [0.1, "key", "f", "down"], [0.18, "key", "f", "up"],
            [0.6, "key", "ctrl", "down"],
            [0.65, "mouse", "left", "down", 5, 5],
            [0.75, "mouse", "left", "up", 5, 5],
            [0.8, "key", "ctrl", "up"],
            [1.1, "key", "d", "down"], [1.18, "key", "d", "up"],
        ]
        tasks, info = self.build(events, raw, include_mouse=True)
        self.assertEqual(info["timeline_segments"], 1)
        self.assertIn("C3", info["reason"])
        values = [t.get("value") for t in tasks]
        self.assertIn("f", values)
        self.assertIn("d", values)

    def test_full_coverage_falls_back_to_single_timeline(self):
        """无法安全拆分（保真区间覆盖全程）时回退整段时序。"""
        raw = [
            [0.2, "key", "ctrl", "down"], [0.25, "key", "v", "down"],
            [0.3, "key", "v", "up"], [0.4, "key", "ctrl", "up"],
        ]
        tasks, info = self.build([(0.25, "hotkey", "ctrl+v", 0.3)], raw, include_mouse=True)
        self.assertEqual(info["timeline_segments"], 1)
        self.assertEqual(len(tasks), 1)
        self.assertIn("覆盖全程", info["reason"])

    def test_cross_segment_gap_inserts_single_wait_with_absolute_time(self):
        """跨段空档按绝对时间计一次显式等待（E1）。"""
        events = [
            (0.0, "hotkey", "f", 0.1),
            (1.0, "hotkey", "a", 1.4),
            (2.0, "hotkey", "d", 2.1),
        ]
        raw = [
            [0.0, "key", "f", "down"], [0.1, "key", "f", "up"],
            [1.0, "key", "a", "down"], [1.1, "key", "a", "repeat"], [1.4, "key", "a", "up"],
            [2.0, "key", "d", "down"], [2.1, "key", "d", "up"],
        ]
        tasks, info = self.build(events, raw, include_mouse=True, wait_threshold=0.15)
        # F(0.1 end) → 片段(1.0 start)：0.9 空档；片段(1.4 end) → D(2.0)：0.6 空档
        # 跨段等待按毫秒精度写入三位小数，不再被段内 150ms 阈值过滤。
        waits = [t["value"] for t in tasks if t["type"] == 5.0]
        self.assertEqual(waits, ["0.900", "0.600"])

    def test_small_cross_segment_gap_still_inserts_wait(self):
        """100ms 跨段空档仍生成等待（原 E2 断言语义反转）。"""
        events = [
            (0.0, "hotkey", "f", 0.1),
            (0.2, "hotkey", "a", 0.6),
            (0.7, "hotkey", "d", 0.8),
        ]
        raw = [
            [0.0, "key", "f", "down"], [0.1, "key", "f", "up"],
            [0.2, "key", "a", "down"], [0.3, "key", "a", "repeat"], [0.6, "key", "a", "up"],
            [0.7, "key", "d", "down"], [0.8, "key", "d", "up"],
        ]
        tasks, _info = self.build(events, raw, include_mouse=True, wait_threshold=0.15)
        waits = [t["value"] for t in tasks if t["type"] == 5.0]
        self.assertEqual(waits, ["0.100", "0.100"])

    def test_millisecond_cross_segment_gap_boundary(self):
        """名义 1ms 的空档不被浮点误差吞掉；0.6ms 不生成。

        f → a 的 0.9 空档恒存在，只需核对 a → d 这一段是否生成。
        """
        for start, first, tail in ((1.401, "0.900", "0.001"), (1.4006, "0.900", None)):
            with self.subTest(start=start):
                events = [
                    (0.0, "hotkey", "f", 0.1),
                    (1.0, "hotkey", "a", 1.4),
                    (start, "hotkey", "d", start + 0.08),
                ]
                raw = [
                    [0.0, "key", "f", "down"], [0.1, "key", "f", "up"],
                    [1.0, "key", "a", "down"], [1.1, "key", "a", "repeat"], [1.4, "key", "a", "up"],
                    [start, "key", "d", "down"], [start + 0.08, "key", "d", "up"],
                ]
                tasks, _info = self.build(events, raw, include_mouse=True)
                waits = [t["value"] for t in tasks if t["type"] == 5.0]
                self.assertEqual(waits, [first] if tail is None else [first, tail])

    def test_three_cross_segment_gaps_each_inserted_once(self):
        """三处切口各生成一次等待，值精确且不重复。"""
        events = [
            (0.0, "hotkey", "f", 0.1),
            (0.5, "hotkey", "a", 0.8),
            (1.0, "hotkey", "d", 1.1),
            (1.5, "hotkey", "b", 1.8),
        ]
        raw = [
            [0.0, "key", "f", "down"], [0.1, "key", "f", "up"],
            [0.5, "key", "a", "down"], [0.6, "key", "a", "repeat"], [0.8, "key", "a", "up"],
            [1.0, "key", "d", "down"], [1.1, "key", "d", "up"],
            [1.5, "key", "b", "down"], [1.6, "key", "b", "repeat"], [1.8, "key", "b", "up"],
        ]
        tasks, _info = self.build(events, raw, include_mouse=True)
        waits = [t["value"] for t in tasks if t["type"] == 5.0]
        self.assertEqual(waits, ["0.400", "0.200", "0.400"])

    def test_in_segment_small_gap_still_uses_legacy_threshold(self):
        """段内 100ms 空档仍沿用 150ms 阈值，不生成等待（G2 回归）。"""
        events = [(0.0, "hotkey", "f", 0.05), (0.15, "hotkey", "d", 0.2)]
        raw = [
            [0.0, "key", "f", "down"], [0.05, "key", "f", "up"],
            [0.15, "key", "d", "down"], [0.2, "key", "d", "up"],
        ]
        tasks, info = self.build(events, raw, include_mouse=True)
        self.assertEqual(info["timeline_segments"], 0)
        self.assertEqual([t for t in tasks if t["type"] == 5.0], [])

    def test_cross_boundary_hold_is_not_dropped(self):
        """跨切口的按住区间被扩展进保真片段，不会丢事件。"""
        events = [
            (0.1, "hotkey", "f", 0.18),
            (0.5, "hotkey", "a", 0.9),
            (1.0, "hotkey", "d", 1.08),
        ]
        # shift 从 0.4 按住到 1.05，跨越 a 的自动重复区间切口
        raw = [
            [0.1, "key", "f", "down"], [0.18, "key", "f", "up"],
            [0.4, "key", "shift", "down"],
            [0.5, "key", "a", "down"], [0.6, "key", "a", "repeat"], [0.9, "key", "a", "up"],
            [1.0, "key", "d", "down"],
            [1.05, "key", "shift", "up"],
            [1.08, "key", "d", "up"],
        ]
        tasks, info = self.build(events, raw, include_mouse=True)
        frag = [t for t in tasks if t["type"] == 29.0][0]["input_events"]
        keys = [(e["key"], e["action"]) for e in frag]
        self.assertIn(("shift", "down"), keys)
        self.assertIn(("shift", "up"), keys)

    def test_touching_boundary_press_is_fully_included(self):
        """同刻相接的按压也必须完整进入片段（B 复验 P2 返修回归）。

        片段切片用闭区间 [start, end]：若某按压的 down 恰落在保真区间 end
        （或 up 恰落在区间 start），严格相交判定会漏掉它，生成只有一端的
        29.0，预运行校验报“按键残留，请重新录制”。相接也须扩展为完整
        down→up，保证每个按键成对。
        """
        cases = {
            # g.down 恰落在 a 区间 end=0.9，g.up 在区间外 1.0
            "down_at_range_end": [
                [0.9, "key", "g", "down"], [1.0, "key", "g", "up"],
            ],
            # g.down 在区间外 0.4，g.up 恰落在 a 区间 start=0.5
            "up_at_range_start": [
                [0.4, "key", "g", "down"], [0.5, "key", "g", "up"],
            ],
        }
        for name, touch in cases.items():
            with self.subTest(case=name):
                raw = [
                    [0.1, "key", "f", "down"], [0.18, "key", "f", "up"],
                    [0.5, "key", "a", "down"], [0.6, "key", "a", "repeat"],
                    [0.9, "key", "a", "up"],
                    *touch,
                    [1.2, "key", "d", "down"], [1.28, "key", "d", "up"],
                ]
                events = [
                    (0.1, "hotkey", "f", 0.18),
                    (0.5, "hotkey", "a", 0.9),
                    (1.2, "hotkey", "d", 1.28),
                ]
                tasks, _info = self.build(events, raw, include_mouse=True)
                frag = [t for t in tasks if t["type"] == 29.0][0]["input_events"]
                g_events = [
                    e["action"] for e in frag if isinstance(e, dict) and e.get("key") == "g"
                ]
                # 同刻相接的 g 必须 down/up 成对，不能只有一端
                self.assertEqual(sorted(g_events), ["down", "up"])
                # 片段不能以未配对的 down 或 up 结尾
                key_events = [
                    (e["key"], e["action"])
                    for e in frag if isinstance(e, dict) and e.get("kind") == "key"
                ]
                for key in {k for k, _a in key_events}:
                    actions = [a for k, a in key_events if k == key]
                    self.assertEqual(actions.count("down"), actions.count("up"))

    def test_empty_and_invalid_do_not_raise(self):
        self.assertEqual(self.build([], [], include_mouse=True)[0], [])
        tasks, info = self.build([(0.0, "hotkey", "f", 0.1)], [], include_mouse=True)
        self.assertEqual(info["timeline_segments"], 0)
        self.assertTrue(tasks)

    def test_truncation_flag_is_reported(self):
        events = [(0.5, "left", (1, 1), 0.55)]
        raw = [[0.5, "mouse", "left", "down", 1, 1], [0.55, "mouse", "left", "up", 1, 1]]
        _tasks, info = self.build(events, raw, include_mouse=True, truncated=True)
        self.assertTrue(info["truncated"])


if __name__ == "__main__":
    unittest.main()
