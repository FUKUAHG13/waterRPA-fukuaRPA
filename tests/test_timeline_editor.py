"""内部事件编辑纯模型测试：动作分组、四项首版操作与编辑后校验。

全部使用合成事件，不依赖真实输入或 Qt。
"""

import unittest

from fukua_rpa.constants import TASK_TYPE_INPUT_TIMELINE
from fukua_rpa.timeline_editor import (
    adjust_wait,
    build_display_rows,
    delete_action,
    edit_click_position,
    find_wait_gaps,
    group_events,
    summarize_edited_task,
    validate_edited_timeline,
)


def _click(t=0.0, x=10, y=20, button="left", with_move=False):
    """合成单击；with_move=True 时按真实录制格式在松开前插入同刻定位 move。"""
    events = [
        {"t": t, "kind": "mouse", "button": button, "action": "down", "x": x, "y": y},
    ]
    up_t = round(t + 0.1, 3)
    if with_move:
        events.append({"t": up_t, "kind": "move", "x": x, "y": y})
    events.append(
        {"t": up_t, "kind": "mouse", "button": button, "action": "up", "x": x, "y": y}
    )
    return events


class TimelineGroupingTests(unittest.TestCase):
    def test_groups_by_kind_and_button(self):
        events = [
            {"t": 0.0, "kind": "key", "key": "a", "action": "down"},
            {"t": 0.05, "kind": "key", "key": "b", "action": "down"},
            {"t": 0.1, "kind": "key", "key": "a", "action": "repeat"},
            {"t": 0.2, "kind": "key", "key": "b", "action": "up"},
            {"t": 0.3, "kind": "key", "key": "a", "action": "up"},
            {"t": 0.4, "kind": "wheel", "delta": 120},
        ]
        groups = group_events(events)
        keys = [g for g in groups if g.kind == "key"]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0].label, "按键 a（重复 1）")
        self.assertEqual(keys[0].t_end, 0.3)
        self.assertEqual(keys[1].label, "按键 b")
        # 键鼠/按键重叠各自独立分组
        self.assertEqual([g.kind for g in groups].count("wheel"), 1)

    def test_positioning_point_association_requires_evidence(self):
        # 同刻 + 紧邻 + 坐标一致 → 确认关联
        events = _click(t=0.5, x=30, y=40, with_move=True)  # move 在 up 前，下标 1
        groups = group_events(events)
        click = next(g for g in groups if g.kind == "click")
        self.assertTrue(click.editable_click)
        self.assertEqual(click.positioning_indices, (1,))
        # 同刻但坐标不一致 → 仅线索，不可确认
        events[1]["x"] = 99
        groups = group_events(events)
        click = next(g for g in groups if g.kind == "click")
        self.assertFalse(click.editable_click)
        self.assertIn("坐标与落点不一致", click.positional_hint)

    def test_drag_and_orphan_are_preview_only(self):
        events = [
            {"t": 0.0, "kind": "mouse", "button": "left", "action": "down", "x": 0, "y": 0},
            {"t": 0.1, "kind": "move", "x": 50, "y": 0},
            {"t": 0.2, "kind": "mouse", "button": "left", "action": "up", "x": 50, "y": 0},
            {"t": 0.3, "kind": "mouse", "button": "right", "action": "up", "x": 50, "y": 0},
        ]
        groups = group_events(events)
        self.assertEqual(groups[0].kind, "drag")
        self.assertFalse(groups[0].editable_click)
        # 拖拽属仅预览动作，不得进入删除入口
        self.assertFalse(groups[0].editable_delete)
        self.assertFalse(delete_action(events, groups[0].id).ok)
        self.assertTrue(any(g.kind == "unpaired" for g in groups))

    def test_nested_button_click_inside_hold_is_preview_only(self):
        # 左键按住期间点右键：内层点击改点会在按住期间移动光标（生成原录制
        # 没有的拖拽），删除也会改动嵌套关系，故只作预览。
        events = [
            {"t": 0.0, "kind": "mouse", "button": "left", "action": "down", "x": 100, "y": 100},
            {"t": 0.05, "kind": "mouse", "button": "right", "action": "down", "x": 200, "y": 200},
            {"t": 0.10, "kind": "mouse", "button": "right", "action": "up", "x": 200, "y": 200},
            {"t": 0.20, "kind": "mouse", "button": "left", "action": "up", "x": 100, "y": 100},
        ]
        groups = group_events(events)
        self.assertEqual(len(groups), 2)
        outer, inner = groups
        self.assertIn("多按钮同时按下", outer.reason)
        self.assertFalse(outer.editable_click)
        self.assertFalse(outer.editable_delete)
        # 内层右键：仍是单击语义，但不可改点、不可删除
        self.assertEqual(inner.kind, "click")
        self.assertIn("按住区间重叠", inner.reason)
        self.assertFalse(inner.editable_click)
        self.assertFalse(inner.editable_delete)
        self.assertFalse(edit_click_position(events, inner.id, 300, 300).ok)
        self.assertIn("按住区间重叠", delete_action(events, inner.id).reason)

    def test_drag_ending_with_same_time_move_is_preview_only(self):
        # 最后一个 move 与松开同刻、且按下/松开落点不同：按住期间确有位移，
        # 属拖拽，不得被当作可删除单击。
        events = [
            {"t": 0.0, "kind": "mouse", "button": "left", "action": "down", "x": 0, "y": 0},
            {"t": 0.2, "kind": "move", "x": 50, "y": 0},
            {"t": 0.2, "kind": "mouse", "button": "left", "action": "up", "x": 50, "y": 0},
        ]
        groups = group_events(events)
        self.assertEqual(groups[0].kind, "drag")
        self.assertIn("拖拽", groups[0].reason)
        self.assertFalse(groups[0].editable_click)
        self.assertFalse(groups[0].editable_delete)
        self.assertFalse(delete_action(events, groups[0].id).ok)

    def test_paired_releases_are_not_listed_as_orphans(self):
        # 完整动作已消费配对的松开，不应再额外出现「孤立的…松开」幻影行。
        events = [
            {"t": 0.0, "kind": "mouse", "button": "left", "action": "down", "x": 10, "y": 20},
            {"t": 0.1, "kind": "mouse", "button": "left", "action": "up", "x": 10, "y": 20},
            {"t": 0.3, "kind": "key", "key": "a", "action": "down"},
            {"t": 0.4, "kind": "key", "key": "a", "action": "up"},
        ]
        groups = group_events(events)
        self.assertEqual([g.kind for g in groups], ["click", "key"])
        self.assertFalse(any(g.kind == "unpaired" for g in groups))

    def test_true_orphan_release_is_still_listed(self):
        # 没有对应按下的松开仍是真实孤立事件，必须继续展示。
        events = [
            {"t": 0.0, "kind": "mouse", "button": "left", "action": "up", "x": 10, "y": 20},
            {"t": 0.1, "kind": "key", "key": "b", "action": "up"},
        ]
        groups = group_events(events)
        self.assertEqual([g.kind for g in groups], ["unpaired", "unpaired"])

    def test_key_group_consumes_repeats(self):
        # 自动重复属于同一按键动作（计入「重复 N」），不得再单列孤立行。
        events = [
            {"t": 0.0, "kind": "key", "key": "a", "action": "down"},
            {"t": 0.3, "kind": "key", "key": "a", "action": "repeat"},
            {"t": 0.4, "kind": "key", "key": "a", "action": "repeat"},
            {"t": 0.5, "kind": "key", "key": "a", "action": "up"},
        ]
        groups = group_events(events)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].kind, "key")
        self.assertEqual(groups[0].indices, (0, 1, 2, 3))
        self.assertIn("重复 2", groups[0].label)
        self.assertFalse(any(g.kind == "unpaired" for g in groups))

    def test_isolated_repeat_without_down_is_still_listed(self):
        # 没有对应按下的 repeat 仍是真实孤立事件，必须继续展示并给出原因。
        events = [
            {"t": 0.0, "kind": "key", "key": "c", "action": "repeat"},
            {"t": 0.1, "kind": "key", "key": "c", "action": "repeat"},
        ]
        groups = group_events(events)
        self.assertEqual([g.kind for g in groups], ["unpaired", "unpaired"])
        self.assertEqual(groups[0].reason, "自动重复缺少对应按下")


class TimelineEditTests(unittest.TestCase):
    def test_move_click_position_syncs_down_up_and_positioning(self):
        events = _click(t=0.5, x=10, y=20, with_move=True)
        groups = group_events(events)
        click_id = next(g.id for g in groups if g.kind == "click")
        result = edit_click_position(events, click_id, 300, 400)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(
            [(e.get("x"), e.get("y")) for e in result.events],
            [(300, 400), (300, 400), (300, 400)],
        )
        # 原列表未被修改（编辑在副本上进行）
        self.assertEqual(events[0]["x"], 10)

    def test_move_click_rejects_drag_and_unclear_positioning(self):
        drag = [
            {"t": 0.0, "kind": "mouse", "button": "left", "action": "down", "x": 0, "y": 0},
            {"t": 0.1, "kind": "move", "x": 50, "y": 0},
            {"t": 0.2, "kind": "mouse", "button": "left", "action": "up", "x": 50, "y": 0},
        ]
        gid = next(g.id for g in group_events(drag) if g.kind == "drag")
        self.assertFalse(edit_click_position(drag, gid, 1, 1).ok)

        unclear = _click(t=0.5, x=10, y=20, with_move=True)
        unclear[1]["x"] = 99  # 定位 move 在 up 前，坐标与落点不一致
        gid = next(g.id for g in group_events(unclear) if g.kind == "click")
        self.assertFalse(edit_click_position(unclear, gid, 1, 1).ok)

    def test_adjust_leading_wait_shifts_all_events(self):
        events = [
            {"t": 0.8, "kind": "key", "key": "a", "action": "down"},
            {"t": 0.9, "kind": "key", "key": "a", "action": "up"},
        ]
        result = adjust_wait(events, boundary=0, delta_seconds=-0.5)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual([e["t"] for e in result.events], [0.3, 0.4])
        # 后续动作内部时长保持 0.1
        self.assertAlmostEqual(result.events[1]["t"] - result.events[0]["t"], 0.1)

    def test_adjust_middle_wait_only_shifts_later_events(self):
        events = _click(t=0.0, x=10, y=20) + _click(t=1.0, x=30, y=40)
        # 间隔边界为第二个单击的按下（下标 2）
        gaps = find_wait_gaps(events)
        self.assertTrue(gaps[2].editable)
        result = adjust_wait(events, boundary=2, delta_seconds=-0.5)
        self.assertTrue(result.ok, result.reason)
        # 第一个单击时刻不变
        self.assertEqual([e["t"] for e in result.events[:2]], [0.0, 0.1])
        self.assertEqual([e["t"] for e in result.events[2:]], [0.5, 0.6])

    def test_adjust_wait_rejects_held_interval(self):
        events = [
            {"t": 0.0, "kind": "key", "key": "a", "action": "down"},
            {"t": 0.5, "kind": "key", "key": "b", "action": "down"},
            {"t": 0.6, "kind": "key", "key": "b", "action": "up"},
            {"t": 1.0, "kind": "key", "key": "a", "action": "up"},
        ]
        # a 在 0～1.0 之间一直按住，下标 1/2/3 的间隔均不可调
        self.assertFalse(find_wait_gaps(events)[1].editable)
        result = adjust_wait(events, boundary=1, delta_seconds=-0.1)
        self.assertFalse(result.ok)
        self.assertIn("按住", result.reason)

    def test_adjust_wait_rejects_negative_result(self):
        events = [
            {"t": 0.2, "kind": "key", "key": "a", "action": "down"},
            {"t": 0.4, "kind": "key", "key": "a", "action": "up"},
        ]
        self.assertFalse(adjust_wait(events, boundary=0, delta_seconds=-0.5).ok)

    def test_delete_complete_click_and_key(self):
        events = _click(t=0.0, x=10, y=20, with_move=True) + [
            {"t": 0.5, "kind": "key", "key": "a", "action": "down"},
            {"t": 0.6, "kind": "key", "key": "a", "action": "repeat"},
            {"t": 0.8, "kind": "key", "key": "a", "action": "up"},
            {"t": 1.0, "kind": "mouse", "button": "right", "action": "down", "x": 5, "y": 5},
            {"t": 1.1, "kind": "mouse", "button": "right", "action": "up", "x": 5, "y": 5},
        ]
        groups = group_events(events)
        key_id = next(g.id for g in groups if g.kind == "key")
        result = delete_action(events, key_id)
        self.assertTrue(result.ok, result.reason)
        # 按下、重复、松开一次性删除；其它同时/前后事件保留且时刻不变
        self.assertEqual(len(result.events), 5)
        self.assertEqual([e["t"] for e in result.events], [0.0, 0.1, 0.1, 1.0, 1.1])
        self.assertFalse(any(e.get("key") == "a" for e in result.events))

        # 删单击时确认关联的定位点一起删
        groups = group_events(events)
        click_id = next(g.id for g in groups if g.kind == "click")
        r2 = delete_action(events, click_id)
        self.assertTrue(r2.ok, r2.reason)
        self.assertFalse(any(e.get("kind") == "move" for e in r2.events))

    def test_delete_keeps_time_by_default(self):
        # 删除后默认保留后续事件时刻，不压缩空白
        events = _click(t=0.0, x=1, y=1) + [
            {"t": 5.0, "kind": "key", "key": "b", "action": "down"},
            {"t": 5.2, "kind": "key", "key": "b", "action": "up"},
        ]
        gid = next(g.id for g in group_events(events) if g.kind == "click")
        result = delete_action(events, gid)
        self.assertEqual([e["t"] for e in result.events], [5.0, 5.2])

    def test_edit_click_preserves_following_wheel_landing(self):
        # 单击后原地滚轮：滚轮未存落点，落点由上一个定位事件决定。
        # 改单击位置后必须在滚轮前插入同刻 move，保留滚轮原落点 (100,100)。
        events = _click(t=0.0, x=100, y=100) + [
            {"t": 0.2, "kind": "wheel", "delta": 120},
        ]
        gid = next(g.id for g in group_events(events) if g.kind == "click")
        result = edit_click_position(events, gid, 300, 200)
        self.assertTrue(result.ok, result.reason)
        wheel = next(e for e in result.events if e["kind"] == "wheel")
        guard = [
            e
            for e in result.events
            if e["kind"] == "move" and e["t"] == wheel["t"] and (e["x"], e["y"]) == (100, 100)
        ]
        self.assertEqual(len(guard), 1, "滚轮前须有同刻 move 保留原落点")
        # 单击本身已改到新落点
        down = next(e for e in result.events if e["kind"] == "mouse" and e["action"] == "down")
        self.assertEqual((down["x"], down["y"]), (300, 200))
        # 回放语义：定位先于滚轮，滚轮仍在 (100,100)
        self.assertLessEqual(
            result.events.index(guard[0]), result.events.index(wheel)
        )

    def test_delete_click_preserves_following_wheel_landing(self):
        events = _click(t=0.0, x=100, y=100) + [
            {"t": 0.2, "kind": "wheel", "delta": 120},
        ]
        gid = next(g.id for g in group_events(events) if g.kind == "click")
        result = delete_action(events, gid)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(
            result.events,
            [
                {"t": 0.2, "kind": "move", "x": 100, "y": 100},
                {"t": 0.2, "kind": "wheel", "delta": 120},
            ],
        )

    def test_edit_rejects_wheel_inside_click_hold(self):
        # 滚轮夹在按下与松开之间：改落点会把单击变成拖拽，无法安全保留，应拒绝。
        events = [
            {"t": 0.0, "kind": "mouse", "button": "left", "action": "down", "x": 10, "y": 10},
            {"t": 0.05, "kind": "wheel", "delta": 120},
            {"t": 0.1, "kind": "mouse", "button": "left", "action": "up", "x": 10, "y": 10},
        ]
        groups = group_events(events)
        click = next(g for g in groups if g.kind == "click")
        self.assertTrue(click.editable_click)
        result = edit_click_position(events, click.id, 300, 300)
        self.assertFalse(result.ok)
        self.assertIn("按住期间", result.reason)


class TimelineEditValidationTests(unittest.TestCase):
    def test_rejects_orphan_up_repeated_down_cross_boundary(self):
        cases = {
            "孤立松开": [
                {"t": 0.0, "kind": "key", "key": "a", "action": "up"},
            ],
            "重复按下": [
                {"t": 0.0, "kind": "key", "key": "a", "action": "down"},
                {"t": 0.1, "kind": "key", "key": "a", "action": "down"},
                {"t": 0.2, "kind": "key", "key": "a", "action": "up"},
            ],
            "未释放": [
                {"t": 0.0, "kind": "key", "key": "a", "action": "down"},
            ],
            "乱序": [
                {"t": 0.2, "kind": "key", "key": "a", "action": "down"},
                {"t": 0.1, "kind": "key", "key": "a", "action": "up"},
            ],
        }
        for label, events in cases.items():
            with self.subTest(label=label):
                self.assertTrue(validate_edited_timeline(events), label)

    def test_accepts_valid_timeline(self):
        events = _click(t=0.0, x=10, y=20, with_move=True) + [
            {"t": 0.5, "kind": "key", "key": "a", "action": "down"},
            {"t": 0.6, "kind": "key", "key": "a", "action": "up"},
            {"t": 0.7, "kind": "wheel", "delta": -120},
        ]
        self.assertEqual(validate_edited_timeline(events), [])

    def test_summary_matches_recording_convention(self):
        events = _click(t=0.0, x=1, y=1)
        summary, duration = summarize_edited_task(events)
        self.assertEqual(duration, 0.1)
        self.assertIn("2 个事件", summary)
        self.assertIn("0.10 秒", summary)

    def test_old_timeline_contract_still_accepted_by_task_validator(self):
        # 编辑后的数据仍满足现有 29.0 步骤契约
        from fukua_rpa.validation import TaskListValidator

        events = _click(t=0.0, x=1, y=1)
        summary, duration = summarize_edited_task(events)
        task = {
            "type": TASK_TYPE_INPUT_TIMELINE,
            "value": summary,
            "input_events": events,
            "recorded_duration": duration,
            "playback_mode": "raw",
        }
        validator = TaskListValidator(
            {"scale_min": "0.8", "scale_max": "1.2", "scale_step": "0.05"}
        )
        self.assertIsNone(validator.validate([task]))

    def test_sanitization_clears_edited_payload(self):
        """编辑后脱敏仍清空事件载荷、时长与模式标注并停用步骤。"""
        from fukua_rpa.workflow_security import sanitize_profile_for_sharing

        events = _click(t=0.0, x=1, y=1)
        summary, duration = summarize_edited_task(events)
        task = {
            "type": TASK_TYPE_INPUT_TIMELINE,
            "value": summary,
            "input_events": events,
            "recorded_duration": duration,
            "playback_mode": "raw",
        }
        result = sanitize_profile_for_sharing({"tasks": [task]})
        step = result.profile["tasks"][0]
        self.assertEqual(step["input_events"], [])
        self.assertEqual(step["recorded_duration"], 0)
        self.assertFalse(step.get("playback_mode"))
        self.assertTrue(step.get("step_disabled"))

    def test_legacy_non_raw_timeline_untouched_by_editor_entry(self):
        """窄语义兼容别名仍只认 raw；通用编辑入口对所有来源一致开放。"""
        from fukua_rpa.ui.controllers.workflow_timeline_edit import (
            is_editable_timeline_task,
            is_raw_timeline_task,
        )

        legacy = {
            "type": TASK_TYPE_INPUT_TIMELINE,
            "input_events": _click(t=0.0, x=1, y=1),
            # 无 playback_mode 的旧 29.0
        }
        # 兼容别名保持旧窄语义
        self.assertFalse(is_raw_timeline_task(legacy))
        legacy["playback_mode"] = "triggered"
        self.assertFalse(is_raw_timeline_task(legacy))
        legacy["playback_mode"] = "raw"
        self.assertTrue(is_raw_timeline_task(legacy))
        # 阶段一：统一的编辑资格只看 29.0 + 合法事件，不依赖来源标注
        for mode in (None, "", "triggered", "raw"):
            task = {
                "type": TASK_TYPE_INPUT_TIMELINE,
                "input_events": _click(t=0.0, x=1, y=1),
            }
            if mode is not None:
                task["playback_mode"] = mode
            self.assertTrue(is_editable_timeline_task(task), mode)

    def test_unified_entry_status_for_empty_and_invalid_timeline(self):
        """空/非法时序不可编辑；非法结构可查看并给出原因。"""
        from fukua_rpa.ui.controllers.workflow_timeline_edit import (
            timeline_edit_status,
        )

        editable, viewable, _label, _reason = timeline_edit_status(
            {"type": TASK_TYPE_INPUT_TIMELINE, "input_events": []}
        )
        self.assertFalse(editable)
        self.assertFalse(viewable)
        editable, viewable, _label, reason = timeline_edit_status(
            {"type": TASK_TYPE_INPUT_TIMELINE, "input_events": [{"t": -1, "kind": "wheel", "delta": 1}]}
        )
        self.assertFalse(editable)
        self.assertTrue(viewable)
        self.assertTrue(reason)

    def test_source_label_distinguishes_raw_from_normal_timeline(self):
        """摘要按真实来源显示，普通录制时序不再被误标为原样回放。"""
        from fukua_rpa.timeline_editor import (
            summarize_edited_task,
            timeline_source_label,
        )

        clicks = _click(t=0.0, x=1, y=1)
        self.assertEqual(timeline_source_label({"playback_mode": "raw"}), "原样回放")
        self.assertEqual(timeline_source_label({"playback_mode": ""}), "输入时序")
        self.assertEqual(timeline_source_label({}), "输入时序")
        self.assertIn("输入时序", summarize_edited_task(clicks, "输入时序")[0])
        self.assertIn("原样回放", summarize_edited_task(clicks, "原样回放")[0])


class RowActionMappingTests(unittest.TestCase):
    """模型能力到行尾胶囊的映射（v1.1.10 四列行视图）。"""

    def test_capability_maps_to_chips(self):
        from fukua_rpa.ui.timeline_editor_dialog import row_action_chips

        click = row_action_chips(("detail", "edit_pos", "delete"))
        self.assertEqual(
            tuple(action for action, _label in click), ("detail", "edit_pos", "delete")
        )
        move = row_action_chips(("detail", "preview", "expand"))
        self.assertEqual(tuple(action for action, _label in move), ("detail", "expand"))
        wait = row_action_chips(("detail", "edit_wait"))
        self.assertEqual(tuple(action for action, _label in wait), ("detail", "edit_wait"))
        for chips in (click, move, wait):
            self.assertTrue(chips)
            self.assertEqual(chips[0][0], "detail")


class SingleWheelDoesNotSkipNextRowTests(unittest.TestCase):
    """单条滚轮后不得跳过下一步（返修前 wheel 分支双重 i+=1）。"""

    def _labels(self, events):
        groups = group_events(events)
        rows = build_display_rows(events)
        return [g.kind for g in groups], [row.label for row in rows]

    def test_wheel_then_key_keeps_following_key(self):
        events = [
            {"t": 0.10, "kind": "wheel", "delta": -120},
            {"t": 0.20, "kind": "key", "key": "b", "action": "down"},
            {"t": 0.30, "kind": "key", "key": "b", "action": "up"},
        ]
        kinds, labels = self._labels(events)
        self.assertEqual(kinds.count("wheel"), 1)
        self.assertEqual(len(rows := build_display_rows(events)), len(kinds),
                         f"group={kinds} 但显示行={[r.label for r in rows]}，滚轮后漏行")
        self.assertTrue(any("按键 b" in label for label in labels), labels)
        self.assertEqual(len(events), 3, "列表投影不得改动原始事件")

    def test_wheel_then_move_keeps_following_move(self):
        events = [
            {"t": 0.10, "kind": "wheel", "delta": 120},
            {"t": 0.20, "kind": "move", "x": 100, "y": 100},
            {"t": 0.30, "kind": "move", "x": 110, "y": 100},
        ]
        kinds, labels = self._labels(events)
        # 两条连续 move 按设计折叠为一行；关键是滚轮后的 move 没有整段丢失。
        self.assertTrue(any("移动" in label for label in labels),
                        f"wheel→move 漏行：{kinds} vs {labels}")
        self.assertIn("滚轮", labels)

    def test_single_wheel_between_two_keys_keeps_both(self):
        events = [
            {"t": 0.10, "kind": "key", "key": "a", "action": "down"},
            {"t": 0.15, "kind": "key", "key": "a", "action": "up"},
            {"t": 0.27, "kind": "wheel", "delta": -120},
            {"t": 0.30, "kind": "key", "key": "b", "action": "down"},
            {"t": 0.37, "kind": "key", "key": "b", "action": "up"},
        ]
        kinds, labels = self._labels(events)
        self.assertEqual(len(kinds), 3)
        self.assertEqual(len(labels), 3, f"应有按键a/滚轮/按键b三行，实际 {labels}")
        self.assertIn("按键 a", labels[0])
        self.assertIn("滚轮", labels[1])
        self.assertIn("按键 b", labels[2])

    def test_multi_wheel_still_folds_into_one_row(self):
        # 回归：多条同向滚轮仍折叠为一行，展开子行正常（未被单条修复破坏）。
        events = [
            {"t": 0.10, "kind": "wheel", "delta": -120},
            {"t": 0.20, "kind": "wheel", "delta": -120},
            {"t": 0.30, "kind": "wheel", "delta": -120},
        ]
        rows = build_display_rows(events)
        self.assertEqual(len(rows), 1, "同向连续滚轮应折叠为一行")
        expanded = build_display_rows(events, expanded_ids=frozenset(r.id for r in rows))
        self.assertGreater(len(expanded), 1, "展开后应可见逐条滚轮")


if __name__ == "__main__":
    unittest.main()
