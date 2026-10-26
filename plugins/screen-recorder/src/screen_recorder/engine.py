"""录制动作编排（不依赖宿主，便于复用与测试）。

``dispatch`` 接收已构造好的后端与动作参数，返回纯数据字典；
未知动作或参数缺失会抛 ``ValueError``/``RuntimeError``，由 ``action.execute_action``
转成失败响应，保证宿主/运行不会因单个动作崩溃。
"""

from __future__ import annotations

import os
import time
from typing import Any

from .models import STATE_PAUSED, STATE_RECORDING, RecorderResult


def _resolve_region(payload: dict[str, Any]) -> dict[str, int] | None:
    region = payload.get("region")
    if not region:
        return None
    if not isinstance(region, dict):
        raise ValueError("region 必须是 {x, y, width, height} 对象")
    for key in ("x", "y", "width", "height"):
        if key not in region:
            raise ValueError(f"region 缺少字段 {key!r}")
    return {
        "x": int(region["x"]),
        "y": int(region["y"]),
        "width": int(region["width"]),
        "height": int(region["height"]),
    }


def _resolve_output_path(payload: dict[str, Any]) -> str:
    """解析 start 的输出路径。

    优先级：显式 ``output_path`` > ``output_dir`` + 自动文件名 >
    都没有则报错（避免误写到未知位置）。
    """
    output_path = payload.get("output_path")
    if output_path:
        return os.path.abspath(output_path)
    output_dir = payload.get("output_dir")
    if output_dir:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        fmt = (payload.get("format") or "mp4").lower()
        return os.path.abspath(os.path.join(output_dir, f"fukua_rec_{stamp}.{fmt}"))
    raise ValueError("recorder_start 需要 output_path 或 output_dir")


def dispatch(backend: Any, action_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if action_id == "recorder_start":
        output_path = _resolve_output_path(payload)
        fps = int(payload.get("fps", 15))
        if fps <= 0:
            raise ValueError("fps 必须为正整数")
        region = _resolve_region(payload)
        audio = bool(payload.get("audio", False))
        status = backend.start(output_path, fps, region, audio)
        return RecorderResult(
            action="recorder_start",
            recording=(status.state == STATE_RECORDING),
            output_path=status.output_path,
        ).to_dict()

    if action_id == "recorder_pause":
        status = backend.pause()
        return RecorderResult(
            action="recorder_pause",
            recording=(status.state == STATE_RECORDING),
            paused=(status.state == STATE_PAUSED),
            output_path=status.output_path,
        ).to_dict()

    if action_id == "recorder_resume":
        status = backend.resume()
        return RecorderResult(
            action="recorder_resume",
            recording=(status.state == STATE_RECORDING),
            paused=False,
            output_path=status.output_path,
        ).to_dict()

    if action_id == "recorder_stop":
        status = backend.stop()
        return RecorderResult(
            action="recorder_stop",
            recording=False,
            output_path=status.output_path,
            duration_ms=status.duration_ms,
            frames=status.frames,
            file_bytes=status.file_bytes,
        ).to_dict()

    if action_id == "recorder_snapshot":
        path = payload.get("path")
        if not path:
            output_dir = payload.get("output_dir")
            if output_dir:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                path = os.path.abspath(os.path.join(output_dir, f"fukua_shot_{stamp}.png"))
            else:
                raise ValueError("recorder_snapshot 需要 path 或 output_dir")
        region = _resolve_region(payload)
        saved = backend.snapshot(os.path.abspath(path), region)
        return RecorderResult(action="recorder_snapshot", path=saved).to_dict()

    raise ValueError(f"未知动作：{action_id}")
