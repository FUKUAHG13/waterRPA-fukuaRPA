"""屏幕录制插件 —— 数据模型。

定义录制状态与动作返回结构。坐标 ``region`` 使用屏幕像素
``{"x", "y", "width", "height"}``（Windows 桌面坐标，左上角为原点）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 录制状态机取值。
STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_PAUSED = "paused"


@dataclass
class RecorderStatus:
    """当前录制会话的状态快照。"""

    state: str = STATE_IDLE
    output_path: str | None = None
    fps: int = 15
    region: dict[str, int] | None = None
    started_at_ns: int = 0
    frames: int = 0
    # 最近一次停止时记录的时长（毫秒）与字节数。
    duration_ms: int = 0
    file_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "output_path": self.output_path,
            "fps": self.fps,
            "region": dict(self.region) if self.region else None,
            "started_at_ns": self.started_at_ns,
            "frames": self.frames,
            "duration_ms": self.duration_ms,
            "file_bytes": self.file_bytes,
        }


@dataclass
class RecorderResult:
    """一次录制动作的结果。"""

    action: str
    recording: bool = False
    paused: bool = False
    output_path: str | None = None
    path: str | None = None  # 截图动作返回的单帧路径
    duration_ms: int = 0
    frames: int = 0
    file_bytes: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "recording": self.recording,
            "paused": self.paused,
            "output_path": self.output_path,
            "path": self.path,
            "duration_ms": self.duration_ms,
            "frames": self.frames,
            "file_bytes": self.file_bytes,
        }
