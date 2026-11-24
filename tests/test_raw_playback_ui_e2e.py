"""v1.1.16 原样回放逐条生成：离屏主窗口端到端验收（E2/中）。

同一合成录制输入走真实 RecorderUI.on_recorded 回调与主列表 add_rows 事务：
- 工作流传出真实、可编辑、独立的普通步骤（不再是单条 29.0）；
- 保存后 JSON 往返保留新字段；
- 一次撤销完整移除新增区间，重做恢复。

v1.1.17：截图写入测试临时目录，不再覆盖已归档的证据。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from fukua_rpa.commands import COMMAND_BY_NAME
from fukua_rpa.constants import TASK_TYPE_MOUSE_TRAJECTORY, TASK_TYPE_SCROLL
from tests.qt_test_support import create_test_window, dispose_widget

LEFT = COMMAND_BY_NAME["左键单击"].code
SYSTEM_KEY = COMMAND_BY_NAME["系统按键"].code

RAW_EVENTS = [
    [0.0, "move", 100, 100],
    [0.05, "mouse", "left", "down", 100, 100],
    [0.12, "mouse", "left", "up", 100, 100],
    [0.20, "wheel", 240, 200, 200],
    [0.30, "key", "a", "down"],
    [0.45, "key", "a", "up"],
    [0.60, "move", 300, 300],
]


class _FakeHook(QObject):
    finished_signal = Signal(object)
    ready_signal = Signal(bool, str)
    control_signal = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.raw_input_events = list(RAW_EVENTS)
        self.raw_events_truncated = False
        self.raw_unsupported = False

    def set_capture_mode(self, _mode):
        pass

    def start(self):
        pass

    def stop(self):
        pass

    def wait(self, _ms):
        return True

    def isRunning(self):
        return False


class RawPlaybackUIE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = QApplication.instance() or QApplication([])
        cls.app = app

    def test_raw_recording_creates_editable_steps_group_and_survives_roundtrip(self):
        with tempfile.TemporaryDirectory() as shot_dir_raw:
            shot_dir = Path(shot_dir_raw)
            window = create_test_window(self)
            try:
                from fukua_rpa.ui.controllers.recording_controller import RecordingController
                from fukua_rpa.ui.input_tools import RecorderUI

                before = window.snapshot_tasks()
                with mock.patch.object(RecorderUI, "show", lambda _self: None):
                    window.grab().save(str(shot_dir / "证据_主界面录制前.png"))
                controller = RecordingController(window)
                # 真实路径：RecordingController.start 把 _add_tasks 作为 tasks_callback，
                # 由它负责建组。这里直接复用同一 callback，不启动真实录制浮层。
                recorder = RecorderUI(
                    main_window=window,
                    tasks_callback=controller._add_tasks,
                    mode="raw",
                    hook_factory=_FakeHook,
                )
                try:
                    # 真实 on_recorded 入口：从 hook 取 raw 事件 → 转换 → add_rows 事务。
                    recorder.on_recorded([])
                finally:
                    dispose_widget(self.app, recorder)

                window.grab().save(str(shot_dir / "证据_主界面录制后.png"))
                after = window.snapshot_tasks()
                added = after[len(before):]
                self.assertGreater(len(added), 1, "原样回放应生成多条真实步骤")
                types = {t["type"] for t in added}
                self.assertNotIn(29.0, types, "不得再是单条输入时序")
                self.assertTrue(all(t.get("fidelity") == "raw" for t in added))
                # 动作类型覆盖：点击、滚轮、轨迹至少各一
                self.assertIn(LEFT, types)
                self.assertIn(TASK_TYPE_SCROLL, types)
                self.assertIn(TASK_TYPE_MOUSE_TRAJECTORY, types)
                # 同一录制组，默认展开
                group_ids = {g["id"] for t in added for g in t.get("editor_groups", [])}
                self.assertEqual(len(group_ids), 1)
                self.assertFalse(added[0]["editor_groups"][0].get("collapsed", False))

                # 保存 JSON 往返保留新字段
                serialized = json.dumps(
                    {"tasks": window.snapshot_tasks()}, ensure_ascii=False
                )
                self.assertIn("timing_group_id", serialized)
                self.assertIn("trajectory", serialized)
                parsed = json.loads(serialized)
                self.assertEqual(parsed["tasks"][len(before):], added)

                # 撤销整批移除、重做恢复
                window.workflow_editor.undo_task_change()
                self.assertEqual(len(window.snapshot_tasks()), len(before))
                window.workflow_editor.redo_task_change()
                restored = window.snapshot_tasks()
                self.assertEqual(len(restored), len(after))
                self.assertEqual(restored[len(before):], added)
            finally:
                dispose_widget(self.app, window)


if __name__ == "__main__":
    unittest.main()
