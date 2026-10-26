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

if __name__ == "__main__":
    unittest.main()
