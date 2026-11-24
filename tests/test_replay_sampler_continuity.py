"""阶段一对照证据：原始点 → 采样保留点 → 保存载荷 → 假时钟实际派发。

覆盖验收矩阵：匀速直线的“停一下再跳”、快慢/弧线/折返/停顿的保留分布，
以及移动合并不得跨越非移动事件边界。所有输入为合成数据，时钟与输入后端均为假实现。
"""

import json
import math
import unittest

from fukua_rpa.actions.input_timeline import (
    normalize_timeline_events,
)
from fukua_rpa.recording_raw import build_raw_playback_task
from fukua_rpa.recording_sampler import DEFAULT_MOVE_WINDOW_SECONDS
from fukua_rpa.validation import TaskListValidator

from .test_input_timeline import _Harness, _run, _timeline_task


def _move_raw(points):
    """合成 move 原始事件：[t, 'move', x, y]。"""
    return [[t, "move", x, y] for t, x, y in points]


def _max_adjacent_gap(events):
    moves = [e for e in events if e.get("kind") == "move"]
    return max((b["t"] - a["t"] for a, b in zip(moves, moves[1:], strict=False)), default=0.0)


def _max_adjacent_displacement(events):
    moves = [e for e in events if e.get("kind") == "move"]
    return max(
        (
            math.hypot(b["x"] - a["x"], b["y"] - a["y"])
            for a, b in zip(moves, moves[1:], strict=False)
        ),
        default=0.0,
    )


class ReplaySamplerContinuityTests(unittest.TestCase):
    def setUp(self):
        self.validator = TaskListValidator(
            {"scale_min": "0.8", "scale_max": "1.2", "scale_step": "0.05"}
        )

    def _build_and_load(self, raw):
        task = build_raw_playback_task(raw)
        self.assertIsNotNone(task)
        # 保存 → 重开（JSON 往返）
        loaded = json.loads(json.dumps(task, ensure_ascii=False))
        self.assertIsNone(self.validator.validate([loaded]))
        return loaded

    def test_straight_line_dispatches_intermediate_before_endpoint(self):
        """24 点 8ms 匀速直线：假时钟必须在终点前派发中间位置。"""
        pts = [(i * 0.008, 100 + i, 200) for i in range(24)]
        loaded = self._build_and_load(_move_raw(pts))
        events = normalize_timeline_events(loaded["input_events"])
        moves = [e for e in events if e["kind"] == "move"]
        # 旧版只留首尾两点（最大相邻间隔 184ms），新版须留中间点
        self.assertGreater(len(moves), 2)
        self.assertLessEqual(
            _max_adjacent_gap(events), DEFAULT_MOVE_WINDOW_SECONDS + 1e-6
        )

        harness = _Harness()
        self.assertEqual(_run(harness, loaded), "success")
        dispatched = [c.args for c in harness.input_backend.move_to.call_args_list]
        # 终点 (123,200) 之前必须派发过中间 x（不是 100 → 123 直接两跳）
        self.assertEqual(dispatched[0], (100, 200))
        self.assertEqual(dispatched[-1], (123, 200))
        self.assertTrue(
            any(100 < x < 123 for x, _y in dispatched),
            f"终点前未派发中间位置：{dispatched}",
        )

    def test_shape_segments_keep_turn_pause_speed(self):
        """快慢相接/弧线/折返/原地停顿：对照点数、最大间隔与最大位移。

        连续移动段的最大相邻间隔受窗口约束；原地停顿是真实存在的长间隔，
        按设计基准必须保留间隔两侧的位置与时刻，不套用窗口上限。
        """
        cases = {
            "快慢相接": [
                (i * 0.008, i * 8 if i < 12 else 96 + (i - 12) * 2, 200)
                for i in range(24)
            ],
            "弧线": [
                (i * 0.008, 100 + i, 100 + int(20 * math.sin(i / 4))) for i in range(20)
            ],
            "折返": [
                (i * 0.008, 100 + (3 * i if i < 8 else 24 - 3 * i), 200)
                for i in range(16)
            ],
        }
        for label, pts in cases.items():
            with self.subTest(label=label):
                loaded = self._build_and_load(_move_raw(pts))
                events = normalize_timeline_events(loaded["input_events"])
                moves = [e for e in events if e["kind"] == "move"]
                # 段首尾必须保留
                self.assertEqual((moves[0]["x"], moves[0]["y"]), (pts[0][1], pts[0][2]))
                self.assertEqual((moves[-1]["x"], moves[-1]["y"]), (pts[-1][1], pts[-1][2]))
                # 对照证据：最大相邻间隔受窗口约束，最大位移有限（无跳变）
                self.assertLessEqual(
                    _max_adjacent_gap(events),
                    DEFAULT_MOVE_WINDOW_SECONDS + 1e-3,
                    f"{label} 最大相邻间隔超限",
                )
                self.assertLess(
                    _max_adjacent_displacement(events),
                    200,
                    f"{label} 出现可疑跳变位移",
                )

    def test_pause_keeps_both_side_positions_and_times(self):
        """原地停顿：保留停顿前后的位置与时刻，不把停顿误判为可合并跳变。"""
        pts = [
            (0.0, 0, 0), (0.04, 40, 0), (0.08, 40, 0),
            (0.16, 40, 0), (0.20, 80, 0), (0.28, 80, 0), (0.32, 120, 0),
        ]
        loaded = self._build_and_load(_move_raw(pts))
        events = normalize_timeline_events(loaded["input_events"])
        moves = [e for e in events if e["kind"] == "move"]
        coords = [(m["t"], m["x"]) for m in moves]
        # 停顿前的到达点与停顿后的离开点都在（两侧位置与时刻保留）
        self.assertIn((0.04, 40), coords)
        self.assertIn((0.20, 80), coords)
        self.assertEqual((moves[-1]["x"], moves[-1]["y"]), (120, 0))

    def test_move_merge_does_not_cross_mouse_key_wheel(self):
        """鼠标按下/松开、按键、滚轮切断移动合并；松开/滚轮落点先定位。"""
        raw = [
            [0.000, "move", 0, 0],
            [0.008, "move", 10, 0],
            [0.016, "mouse", "left", "down", 20, 0],
            [0.024, "move", 30, 0],
            [0.032, "key", "a", "down"],
            [0.040, "move", 40, 0],
            [0.048, "wheel", 120, 50, 0],
            [0.056, "move", 60, 0],
            [0.064, "mouse", "left", "up", 70, 0],
            [0.072, "key", "a", "up"],
        ]
        loaded = self._build_and_load(raw)
        events = normalize_timeline_events(loaded["input_events"])
        # 每个非移动事件两侧的移动各成段：定位 move 仍保留
        kinds = [e["kind"] for e in events]
        self.assertIn("mouse", kinds)
        self.assertIn("key", kinds)
        self.assertIn("wheel", kinds)
        harness = _Harness()
        self.assertEqual(_run(harness, loaded), "success")
        # 派发顺序遵循原始录制列表：mousedown → keydown → mouseup → keyup
        self.assertEqual(harness.calls, [("mdown", "left"), ("down", "a"),
                                         ("mup", "left"), ("up", "a")])
        self.assertEqual(harness._injected_keys, [])
        self.assertEqual(harness._injected_mouse_buttons, set())
        # 松开事件 (70,0) 前必须有一条同刻定位 move
        move_points = [c.args for c in harness.input_backend.move_to.call_args_list]
        self.assertIn((70, 0), move_points)

    def test_dense_drag_save_stays_within_event_budget(self):
        """高频长录制接近上限：保存事件数受 50,000 约束，校验通过。"""
        from fukua_rpa.constants import MAX_TIMELINE_EVENTS

        pts = [(i * 0.008, i % 1000, i % 700) for i in range(2000)]
        loaded = self._build_and_load(_move_raw(pts))
        self.assertLessEqual(len(loaded["input_events"]), MAX_TIMELINE_EVENTS)
        self.assertGreater(len(loaded["input_events"]), 2)


class WheelLandingEditReplayTests(unittest.TestCase):
    """编辑/删除单击后，后续滚轮落点仍在原处（假后端端到端核验）。"""

    def setUp(self):
        self.validator = TaskListValidator(
            {"scale_min": "0.8", "scale_max": "1.2", "scale_step": "0.05"}
        )

    def _base_task(self):
        task = build_raw_playback_task(
            [
                [0.0, "mouse", "left", "down", 100, 100],
                [0.1, "mouse", "left", "up", 100, 100],
                [0.2, "wheel", 120, 100, 100],
            ]
        )
        self.assertIsNotNone(task)
        return task

    def _scroll_position(self, task):
        harness = _Harness()
        self.assertEqual(_run(harness, task), "success")
        moves = [c.args for c in harness.input_backend.move_to.call_args_list]
        self.assertEqual(len(harness.input_backend.scroll.call_args_list), 1)
        # 滚轮落点＝派发滚轮前最后一次 move_to 的坐标
        return moves[-1]

    def test_edit_click_keeps_wheel_at_original_landing(self):
        from fukua_rpa.timeline_editor import edit_click_position, group_events

        task = self._base_task()
        self.assertEqual(self._scroll_position(task), (100, 100))
        click = next(g for g in group_events(task["input_events"]) if g.kind == "click")
        result = edit_click_position(task["input_events"], click.id, 300, 200)
        self.assertTrue(result.ok, result.reason)
        edited = dict(task)
        edited["input_events"] = result.events
        self.assertIsNone(self.validator.validate([edited]))
        # 单击已改到新落点，但滚轮仍落在原处
        self.assertEqual(self._scroll_position(edited), (100, 100))

    def test_delete_click_keeps_wheel_at_original_landing(self):
        from fukua_rpa.timeline_editor import delete_action, group_events

        task = self._base_task()
        click = next(g for g in group_events(task["input_events"]) if g.kind == "click")
        result = delete_action(task["input_events"], click.id)
        self.assertTrue(result.ok, result.reason)
        edited = dict(task)
        edited["input_events"] = result.events
        self.assertIsNone(self.validator.validate([edited]))
        self.assertEqual(self._scroll_position(edited), (100, 100))


class NestedButtonEditReplayTests(unittest.TestCase):
    """按钮重叠与同刻拖拽：编辑入口必须拒绝，回放位移不得被改动。

    SAFETY: 回放派发 mouse down 前先 move_to；若外层按钮仍按住，改内层点击
    的坐标就会在按住期间移动光标，凭空生成原始录制中不存在的拖拽。
    """

    def _moves(self, events):
        harness = _Harness()
        self.assertEqual(_run(harness, _timeline_task(events)), "success")
        return [c.args for c in harness.input_backend.move_to.call_args_list]

    def test_nested_click_edit_is_refused_and_replay_unchanged(self):
        from fukua_rpa.timeline_editor import edit_click_position, group_events

        events = [
            {"t": 0.0, "kind": "mouse", "button": "left", "action": "down", "x": 100, "y": 100},
            {"t": 0.05, "kind": "mouse", "button": "right", "action": "down", "x": 200, "y": 200},
            {"t": 0.10, "kind": "mouse", "button": "right", "action": "up", "x": 200, "y": 200},
            {"t": 0.20, "kind": "mouse", "button": "left", "action": "up", "x": 100, "y": 100},
        ]
        before = self._moves(events)
        self.assertEqual(before, [(100, 100), (200, 200)])
        inner = next(g for g in group_events(events) if g.label.endswith("right"))
        result = edit_click_position(events, inner.id, 300, 300)
        self.assertFalse(result.ok)
        self.assertIn("按住区间重叠", result.reason)
        # 拒绝后事件未被改写，回放位移与录制一致（不会出现 (300, 300)）
        self.assertEqual(self._moves(events), before)

    def test_drag_with_same_time_move_cannot_be_deleted(self):
        from fukua_rpa.timeline_editor import delete_action, group_events

        events = [
            {"t": 0.0, "kind": "mouse", "button": "left", "action": "down", "x": 0, "y": 0},
            {"t": 0.2, "kind": "move", "x": 50, "y": 0},
            {"t": 0.2, "kind": "mouse", "button": "left", "action": "up", "x": 50, "y": 0},
        ]
        group = group_events(events)[0]
        self.assertEqual(group.kind, "drag")
        self.assertFalse(delete_action(events, group.id).ok)
        self.assertEqual(self._moves(events), [(0, 0), (50, 0)])


if __name__ == "__main__":
    unittest.main()
