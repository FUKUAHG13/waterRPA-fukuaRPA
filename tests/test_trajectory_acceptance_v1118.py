# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.
"""v1.1.18 轨迹编辑开发验收（端到端，不依赖内存字段捷径）。

真实链路：
1. 主窗口步骤行的设置小齿轮修改某采样点的时间与坐标；
2. 经 ``get_current_ui_config`` + ``atomic_write_json`` 保存到临时方案；
3. 关闭窗口后由新窗口经 ``load_bounded_json_file`` / ``migrate_profile_config``
   / ``apply_ui_config`` 重开，``snapshot_tasks`` 取回轨迹；
4. 从正常运行入口 ``RPAEngine._dispatch_step``（与 ``run_tasks`` 共用核心，
   经 ``ACTION_HANDLERS`` 注册表派发）以假时钟、假输入后端执行。

断言编辑后的坐标、第二点派发时刻、下一步前置间隔只计一次且从轨迹结束后起算。
``test_trajectory_replay_v1117.py`` 的内存 JSON 往返仅作辅助证据。
"""
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from fukua_rpa.actions.flow import ACTION_HANDLERS  # noqa: E402
from fukua_rpa.config_schema import migrate_profile_config  # noqa: E402
from fukua_rpa.constants import (  # noqa: E402
    MAX_PROFILE_STORE_JSON_BYTES,
    TASK_TYPE_MOUSE_TRAJECTORY,
    TASK_TYPE_WAIT,
)
from fukua_rpa.engine.core import RPAEngine  # noqa: E402
from fukua_rpa.io_utils import atomic_write_json  # noqa: E402
from fukua_rpa.json_safety import load_bounded_json_file  # noqa: E402
from fukua_rpa.profile_structure import validate_profile_config_data  # noqa: E402
from tests.qt_test_support import (  # noqa: E402
    QtCallbackGuardMixin,
    create_test_window,
    dispose_widget,
)

RAW_TRAJECTORY = "[[0,100,100],[144,130,109]]"
EDITED_MS = 260
EDITED_X = 205
EDITED_Y = 145
EDITED_POINTS = [[0, 100, 100], [EDITED_MS, EDITED_X, EDITED_Y]]
FOLLOW_PRE_DELAY_MS = 60
FOLLOW_WAIT_SECONDS = "0.020"


class TrajectoryEditAcceptanceTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _add_rows(self, window):
        window.add_row(
            {
                "type": TASK_TYPE_MOUSE_TRAJECTORY,
                "value": "鼠标轨迹：2 点 / 0.144 秒",
                "trajectory": RAW_TRAJECTORY,
                "fidelity": "raw",
                "timing_group_id": "seg-acceptance",
                "pre_delay_ms": "0",
            },
            record_undo=False,
            select=False,
        )
        window.add_row(
            {
                "type": TASK_TYPE_WAIT,
                "value": FOLLOW_WAIT_SECONDS,
                "fidelity": "raw",
                "timing_group_id": "seg-acceptance",
                "pre_delay_ms": str(FOLLOW_PRE_DELAY_MS),
            },
            record_undo=False,
            select=False,
        )
        window.task_list.schedule_virtual_refresh(immediate=True)
        self.app.processEvents()

    def _edit_second_point_with_gear(self, window):
        """真实步骤行小齿轮：把第二采样点改为指定时刻与坐标。"""
        item = window.task_list.item(0)
        row_widget = window.ensure_task_item_widget(item)
        row_widget.open_custom_config()
        dialog = row_widget.config_dialog
        self.assertIsNotNone(dialog)
        self.assertTrue(dialog.is_mouse_trajectory())
        rows = dialog.trajectory_controller.rows
        self.assertGreaterEqual(len(rows), 2)
        rows[1][0].setText(str(EDITED_MS))
        rows[1][1].setText(str(EDITED_X))
        rows[1][2].setText(str(EDITED_Y))
        dialog.accept()
        self.assertEqual(dialog.trajectory_controller.status_label.text(), "")
        row_widget.close_config_dialog()
        self.app.processEvents()
        # 齿轮确实把编辑写回该步骤的可保存数据（不是只改对话框草稿）。
        self.assertEqual(
            json.loads(row_widget.custom_data["trajectory"]), EDITED_POINTS
        )

    def _save_close_reopen(self, window, base_dir, scheme_path):
        """程序入口保存方案 → 关闭窗口 → 新窗口经真实加载入口重开。"""
        config = window.get_current_ui_config()
        self.assertIsNone(validate_profile_config_data(config, "方案"))
        atomic_write_json(scheme_path, config)
        # 落盘内容确为编辑后的轨迹（保存入口没有回退到旧值）。
        with open(scheme_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(
            json.loads(saved["tasks"][0]["trajectory"]), EDITED_POINTS
        )
        dispose_widget(self.app, window)

        reopened = create_test_window(self, base_dir)
        data = load_bounded_json_file(
            scheme_path,
            max_bytes=MAX_PROFILE_STORE_JSON_BYTES,
            max_nodes=MAX_PROFILE_STORE_JSON_BYTES,
            label="导入方案",
        )
        migrated = migrate_profile_config(data).value
        self.assertTrue(reopened.apply_ui_config(migrated))
        self.app.processEvents()
        return reopened

    def _dispatch_from_run_entry(self, tasks):
        """正常运行入口派发：RPAEngine._dispatch_step → ACTION_HANDLERS。"""
        origin = 500.0
        clock = {"t": origin}

        def monotonic():
            return clock["t"]

        def fake_sleep(seconds):
            clock["t"] += float(seconds)

        engine = RPAEngine(defer_backends=True)
        engine.log_level = -1
        engine.settlement_wait = 0.0
        engine.detect_delay = 0.0
        moves = []
        engine.input_backend = mock.Mock()

        def move_to(x, y, duration=0):
            moves.append((round(clock["t"] - origin, 3), int(x), int(y)))

        engine.input_backend.move_to.side_effect = move_to
        waits = []
        original_wait = engine._wait_interruptibly

        def spy_wait(seconds, *args, **kwargs):
            waits.append((round(clock["t"] - origin, 3), round(float(seconds), 3)))
            return original_wait(seconds, *args, **kwargs)

        handled = []
        trajectory_handler = ACTION_HANDLERS[TASK_TYPE_MOUSE_TRAJECTORY]

        def spy_handler(engine_self, context):
            handled.append(str((context.task or {}).get("trajectory", "")))
            return trajectory_handler(engine_self, context)

        marks = []
        with mock.patch("fukua_rpa.engine.timing.time.monotonic", monotonic), \
                mock.patch("fukua_rpa.engine.timing.time.sleep", fake_sleep), \
                mock.patch("fukua_rpa.actions.input_timeline.time.monotonic", monotonic), \
                mock.patch.object(engine, "_wait_interruptibly", spy_wait), \
                mock.patch.dict(
                    ACTION_HANDLERS, {TASK_TYPE_MOUSE_TRAJECTORY: spy_handler}
                ):
            for index, task in enumerate(tasks):
                engine._dispatch_step(tasks, task, index, 1)
                marks.append(round(clock["t"] - origin, 3))
        return moves, waits, marks, handled

    def test_edited_trajectory_survives_program_save_reopen_and_run_entry(self):
        with tempfile.TemporaryDirectory() as base_dir:
            scheme_path = os.path.join(base_dir, "trajectory_acceptance.json")
            window = create_test_window(self, base_dir)
            self._add_rows(window)
            self._edit_second_point_with_gear(window)
            reopened = self._save_close_reopen(window, base_dir, scheme_path)
            try:
                tasks = reopened.snapshot_tasks()
                self.assertEqual(len(tasks), 2, tasks)
                trajectory_task = tasks[0]
                follow_task = tasks[1]
                # 重开后的轨迹来自程序入口保存的编辑值，摘要同源于数据。
                self.assertEqual(
                    json.loads(trajectory_task["trajectory"]), EDITED_POINTS
                )
                self.assertIn("0.260 秒", trajectory_task["value"])
                self.assertEqual(follow_task["type"], TASK_TYPE_WAIT)
                self.assertEqual(
                    follow_task["pre_delay_ms"], str(FOLLOW_PRE_DELAY_MS)
                )

                moves, waits, marks, handled = self._dispatch_from_run_entry(tasks)
                # ① 真实 ACTION_HANDLERS 收到编辑后的轨迹原文。
                self.assertEqual(len(handled), 1)
                self.assertEqual(json.loads(handled[0]), EDITED_POINTS)
                self.assertEqual(handled[0], trajectory_task["trajectory"])
                # ② 坐标与第二点派发时刻均为编辑后的值。
                self.assertEqual(
                    moves, [(0.0, 100, 100), (EDITED_MS / 1000.0, EDITED_X, EDITED_Y)]
                )
                # 轨迹结束时刻等于编辑后末点时刻。
                self.assertAlmostEqual(marks[0], EDITED_MS / 1000.0, places=3)
                # ③ 下一步前置间隔只等待一次，且恰好从轨迹结束后开始。
                self.assertEqual(
                    waits,
                    [
                        # 轨迹内部：从首点到编辑后第二点的行内等待。
                        (0.0, EDITED_MS / 1000.0),
                        # 下一步前置间隔：起点等于轨迹结束时刻，只出现一次。
                        (EDITED_MS / 1000.0, FOLLOW_PRE_DELAY_MS / 1000.0),
                        # 下一步自身的等待行（原速回放）。
                        (
                            (EDITED_MS + FOLLOW_PRE_DELAY_MS) / 1000.0,
                            float(FOLLOW_WAIT_SECONDS),
                        ),
                    ],
                )
                self.assertEqual(
                    waits.count((EDITED_MS / 1000.0, FOLLOW_PRE_DELAY_MS / 1000.0)),
                    1,
                )
                self.assertAlmostEqual(
                    marks[1],
                    (EDITED_MS + FOLLOW_PRE_DELAY_MS) / 1000.0
                    + float(FOLLOW_WAIT_SECONDS),
                    places=3,
                )
            finally:
                dispose_widget(self.app, reopened)


if __name__ == "__main__":
    unittest.main()
