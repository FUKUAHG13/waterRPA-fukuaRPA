"""鼠标轨迹采样点的纯模型：解析、校验、序列化与摘要（不依赖 Qt）。

供「鼠标轨迹」步骤的小齿轮设置使用，与回放侧
``actions/input_timeline._execute_mouse_trajectory`` 共用同一取值域常量。
写回、撤销与运行锁由对话框/控制器负责；本模块只返回新数据或带行号的拒绝
原因。

INVARIANT: 轨迹数据唯一持久形式是 ``trajectory`` 字段的
``[[相对毫秒, X, Y], ...]`` JSON 字符串；主行的点数/秒数摘要只由该数据生成，
不可作为执行来源（v1.1.17 修复 2）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fukua_rpa.constants import (
    MAX_TIMELINE_COORDINATE,
    MAX_TIMELINE_EVENT_SECONDS,
    MAX_TIMELINE_EVENTS,
)

# 与回放侧 actions/input_timeline.MAX_TRAJECTORY_JSON_BYTES 同值。
MAX_TRAJECTORY_JSON_BYTES = 4 * 1024 * 1024
_MAX_TIME_MS = int(MAX_TIMELINE_EVENT_SECONDS * 1000)


@dataclass
class TrajectoryResult:
    ok: bool
    points: list[tuple[int, int, int]] | None = None
    reason: str = ""
    line: int = 0  # 1-based 采样行号，定位到具体采样点


def validate_points(points: list[tuple[int, int, int]]) -> TrajectoryResult:
    """校验采样点序列；时间非负且非递减，坐标为整数且在上限内。"""
    if not isinstance(points, list) or not points:
        return TrajectoryResult(False, reason="轨迹至少需要一个采样点")
    if len(points) > MAX_TIMELINE_EVENTS:
        return TrajectoryResult(False, reason=f"采样点超过上限 {MAX_TIMELINE_EVENTS}")
    previous_ms = -1
    for index, point in enumerate(points, start=1):
        if not isinstance(point, (list, tuple)) or len(point) != 3:
            return TrajectoryResult(False, reason=f"第 {index} 行不是 [毫秒,X,Y]", line=index)
        try:
            t_ms, x, y = int(point[0]), int(point[1]), int(point[2])
        except (TypeError, ValueError):
            return TrajectoryResult(False, reason=f"第 {index} 行必须为整数", line=index)
        if isinstance(point[0], float) or isinstance(point[1], float) or isinstance(point[2], float):
            return TrajectoryResult(False, reason=f"第 {index} 行必须为整数", line=index)
        if t_ms < 0 or t_ms > _MAX_TIME_MS:
            return TrajectoryResult(False, reason=f"第 {index} 行时刻越界", line=index)
        if t_ms < previous_ms:
            return TrajectoryResult(False, reason=f"第 {index} 行时刻早于上一行", line=index)
        if abs(x) > MAX_TIMELINE_COORDINATE or abs(y) > MAX_TIMELINE_COORDINATE:
            return TrajectoryResult(False, reason=f"第 {index} 行坐标越界", line=index)
        previous_ms = t_ms
    return TrajectoryResult(True, points=list(points))


def parse_trajectory(raw: str) -> TrajectoryResult:
    """从 trajectory JSON 字符串解析并校验；首点时刻原样保留，不归一。"""
    text = str(raw or "").strip()
    if not text:
        return TrajectoryResult(False, reason="轨迹为空")
    if len(text.encode("utf-8")) > MAX_TRAJECTORY_JSON_BYTES:
        return TrajectoryResult(False, reason="轨迹数据超过字节上限")
    try:
        parsed: Any = json.loads(text)
    except (ValueError, TypeError):
        return TrajectoryResult(False, reason="轨迹不是合法 JSON")
    if not isinstance(parsed, list) or not parsed:
        return TrajectoryResult(False, reason="轨迹至少需要一个采样点")
    points: list[tuple[int, int, int]] = []
    for index, item in enumerate(parsed, start=1):
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            return TrajectoryResult(False, reason=f"第 {index} 行不是 [毫秒,X,Y]", line=index)
        if any(isinstance(v, float) for v in item[:3]):
            return TrajectoryResult(False, reason=f"第 {index} 行必须为整数", line=index)
        points.append((item[0], item[1], item[2]))  # type: ignore[arg-type]
    return validate_points(points)


def serialize_trajectory(points: list[tuple[int, int, int]]) -> str:
    """序列化为回放使用的紧凑 JSON 数组，保留行顺序。

    DESIGN: 用紧凑 separators 输出，与方案示例 ``[[0,100,100],[144,130,109]]``
    一致；回放侧用 json 解析，空格不影响语义。编辑保存后字节格式统一，但
    时刻/坐标值与行顺序原样保留。
    """
    return json.dumps(
        [[int(t), int(x), int(y)] for t, x, y in points],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def format_trajectory_summary(points: list[tuple[int, int, int]]) -> str:
    """从实际数据生成只读摘要：点数 + 首末时刻跨度（秒，三位小数）。"""
    if not points:
        return "鼠标轨迹：0 点 / 0.000 秒"
    duration_ms = max(0, int(points[-1][0]) - int(points[0][0]))
    return f"鼠标轨迹：{len(points)} 点 / {duration_ms / 1000:.3f} 秒"
