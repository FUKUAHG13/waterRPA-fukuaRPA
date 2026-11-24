# ruff: noqa: E402
"""v1.1.17 修复 2：编辑后的轨迹经真实派发核对坐标/时刻与后续间隔起点。"""
import tempfile
import unittest
from unittest import mock

from fukua_rpa.constants import TASK_TYPE_MOUSE_TRAJECTORY
from fukua_rpa.trajectory_editor import (
    parse_trajectory,
    serialize_trajectory,
)
from fukua_rpa.ui.task_settings_dialog import TaskConfigDialog
from tests.test_raw_playback_runtime import _Ctx, _Harness


class TrajectoryReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _edited_raw(self):
        """经真实设置对话框把第二点改为 [220,160,120] 后序列化。"""
        with tempfile.TemporaryDirectory() as base:
            raw = "[[0,100,100],[144,130,109]]"
            dlg = TaskConfigDialog(
                None,
                {"trajectory": raw, "value": "旧摘要"},
                base_dir=base,
                step_type="鼠标轨迹",
            )
            try:
                rows = dlg.trajectory_controller.rows
                rows[1][0].setText("220")
                rows[1][1].setText("160")
                rows[1][2].setText("120")
                self.assertEqual(dlg.trajectory_controller.sync_from_rows(), "")
                return serialize_trajectory(dlg.trajectory_controller.points)
            finally:
                dlg.close()

    def test_edited_trajectory_replays_second_point_at_edited_time(self):
        raw = self._edited_raw()
        self.assertEqual(parse_trajectory(raw).points, [(0, 100, 100), (220, 160, 120)])
        task = {
            "type": TASK_TYPE_MOUSE_TRAJECTORY,
            "value": "鼠标轨迹：2 点 / 0.220 秒",
            "trajectory": raw,
            "fidelity": "raw",
        }
        h = _Harness()
        start_clock = h.clock
        with mock.patch(
            "fukua_rpa.actions.input_timeline.time.monotonic",
            side_effect=lambda: h.clock,
        ):
            h._execute_mouse_trajectory(_Ctx(task))
        moves = [c for c in h.calls if c[0] == "move"]
        self.assertEqual(moves, [("move", 100, 100), ("move", 160, 120)])
        # 第二点在编辑后的 220ms 延迟派发；时钟推进到轨迹结束。
        self.assertEqual(h.waits, [0.22])
        self.assertAlmostEqual(h.clock - start_clock, 0.22, places=3)

    def test_next_step_delay_starts_after_edited_trajectory_end(self):
        """编辑末点时刻改变轨迹时长后，后续步骤按原 pre_delay 从结束点起算一次。"""
        raw = self._edited_raw()  # 末点 220ms
        task = {
            "type": TASK_TYPE_MOUSE_TRAJECTORY,
            "value": "轨迹",
            "trajectory": raw,
            "fidelity": "raw",
            "timing_group_id": "seg-1",
            "pre_delay_ms": "0",
        }
        h = _Harness()
        timing = h._recording_timing
        with mock.patch(
            "fukua_rpa.actions.input_timeline.time.monotonic",
            side_effect=lambda: h.clock,
        ):
            timing.before_dispatch(h, task)  # 轨迹 pre_delay=0
            h._execute_mouse_trajectory(_Ctx(task))  # 推进 0.22s
            trajectory_end_clock = h.clock
            # 紧随的动作保留原前置间隔 50ms。
            follow = {
                "type": 1.0, "value": "1,1", "fidelity": "raw",
                "timing_group_id": "seg-1", "pre_delay_ms": "50",
            }
            timing.before_dispatch(h, follow)
        # 轨迹结束时刻 = 起始 + 0.22s；后续只等待一次 50ms，不重复计轨迹耗时。
        self.assertAlmostEqual(trajectory_end_clock - 1000.0, 0.22, places=3)
        self.assertAlmostEqual(h.clock - trajectory_end_clock, 0.05, places=3)
        self.assertEqual(h.waits[-1], 0.05)

    def test_edited_trajectory_survives_json_roundtrip_then_replays(self):
        # 模拟保存方案→重开：轨迹随 JSON 往返后坐标/时刻不变，派发仍正确。
        import json

        raw = self._edited_raw()
        profile = {"tasks": [{"type": TASK_TYPE_MOUSE_TRAJECTORY, "trajectory": raw}]}
        restored = json.loads(json.dumps(profile, ensure_ascii=False))["tasks"][0]
        self.assertEqual(parse_trajectory(restored["trajectory"]).points,
                         [(0, 100, 100), (220, 160, 120)])
        task = dict(restored, value="轨迹", fidelity="raw")
        h = _Harness()
        with mock.patch(
            "fukua_rpa.actions.input_timeline.time.monotonic",
            side_effect=lambda: h.clock,
        ):
            h._execute_mouse_trajectory(_Ctx(task))
        self.assertEqual(
            [c for c in h.calls if c[0] == "move"],
            [("move", 100, 100), ("move", 160, 120)],
        )

    def test_summary_without_trajectory_is_not_executed(self):
        # 摘要写着点数，但 trajectory 为空时回放报错，不按摘要执行。
        h = _Harness()
        task = {"type": TASK_TYPE_MOUSE_TRAJECTORY, "value": "鼠标轨迹：2 点", "trajectory": ""}
        with mock.patch(
            "fukua_rpa.actions.input_timeline.time.monotonic",
            side_effect=lambda: h.clock,
        ):
            result = h._execute_mouse_trajectory(_Ctx(task))
        self.assertEqual(result, "error")
        self.assertEqual(h.calls, [])


if __name__ == "__main__":
    unittest.main()
