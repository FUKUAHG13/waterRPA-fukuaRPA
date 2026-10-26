# pyright: reportAttributeAccessIssue=false
"""Shared core symbols for the auto-clicker tool window and its Mixins.

Houses the settings-key constants, speed presets and the runner thread so that
both ``auto_clicker_tool.py`` and the ``auto_clicker_mixins`` package can import
them without a circular dependency (the Mixins must not import the window module).
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from ..actions.shared import StepActionContext
from ..constants import TASK_TYPE_AUTO_CLICKER

_START_HOTKEY_KEY = "auto_clicker/start_hotkey"
_STOP_HOTKEY_KEY = "auto_clicker/stop_hotkey"

# 连点器工具图像匹配的置信度阈值（运行时复用引擎 _execute_auto_clicker）。
_TOOL_CONFIDENCE = 0.8

# 速度预设：(标签, interval_min_s, interval_max_s)  ← 引擎单位为秒
_SPEED_PRESETS = [
    ("极速（间隔 0.010 s）", 0.01, 0.01),
    ("快速（间隔 0.020 s）", 0.02, 0.02),
    ("普通（间隔 0.100 s）", 0.1, 0.1),
    ("慢速（间隔 0.500 s）", 0.5, 0.5),
    ("自定义", 0, 0),
]


class _AutoClickerRunner(QThread):
    """在独立线程中运行引擎的连点器逻辑，避免阻塞界面。"""

    finished = Signal()
    error = Signal(str)

    def __init__(self, engine, task, stop_event):
        super().__init__()
        self.engine = engine
        self.task = task
        self.stop_event = stop_event

    def run(self):
        try:
            context = StepActionContext(
                command=TASK_TYPE_AUTO_CLICKER,
                value="",
                retry=-1,
                step_info={
                    "loop": 0,
                    "step": 0,
                    "cmd": "连点器工具",
                    "attempt": 1,
                    "success_count": 0,
                },
                cache_key="",
                confidence=_TOOL_CONFIDENCE,
                use_gray=True,
                task=self.task,
            )
            self.engine._execute_auto_clicker(
                context, stop_requested=self.stop_event.is_set
            )
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))
        finally:
            self.finished.emit()
