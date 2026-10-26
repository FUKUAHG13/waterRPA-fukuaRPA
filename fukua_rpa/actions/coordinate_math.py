"""坐标几何纯计算助手（A-1 Step 1 提取自 actions/mouse.py）。

本模块仅含无副作用的数学函数，不依赖 Qt、引擎状态或 Mixin 属性。
从 mouse.py 提取以消除重复并降低巨型模块认知负载。
"""

from __future__ import annotations

import math
import random


def parse_drag_coordinates(val: str) -> tuple[int, int, int, int]:
    """Parse 'x1,y1 -> x2,y2' text into four integer coordinates.

    Args:
        val: Drag coordinate string in the format "x1,y1 -> x2,y2".

    Returns:
        Tuple (x1, y1, x2, y2).

    Raises:
        ValueError: If the format is invalid or coordinates are not integers.
    """
    parts = val.split("->")
    if len(parts) != 2:
        raise ValueError(f"拖拽坐标格式异常（应为 'x1,y1 -> x2,y2'）: {val}")
    p1 = parts[0].split(",")
    p2 = parts[1].split(",")
    if len(p1) != 2 or len(p2) != 2:
        raise ValueError(f"拖拽坐标分量不足: {val}")
    x1, y1 = int(p1[0].strip()), int(p1[1].strip())
    x2, y2 = int(p2[0].strip()), int(p2[1].strip())
    return x1, y1, x2, y2


def bezier_control_point(
    start_x: float,
    start_y: float,
    target_x: float,
    target_y: float,
    curve_strength: float,
    steps: int,
) -> tuple[float, float] | None:
    """Compute a randomized quadratic Bezier control point.

    The control point is placed at the midpoint between start and target,
    offset perpendicular to the line by a random amount proportional to
    distance and curve_strength.

    Args:
        start_x: Start X coordinate.
        start_y: Start Y coordinate.
        target_x: Target X coordinate.
        target_y: Target Y coordinate.
        curve_strength: Curve intensity (0 = straight line).
        steps: Number of interpolation steps.

    Returns:
        (ctrl_x, ctrl_y) if curve is applicable, else None (straight line).
    """
    if curve_strength <= 0 or steps <= 2:
        return None
    mid_x = (start_x + target_x) / 2.0
    mid_y = (start_y + target_y) / 2.0
    dx = target_x - start_x
    dy = target_y - start_y
    dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
    # 垂直于起终点方向的单位向量
    perp_x, perp_y = -dy / dist, dx / dist
    # 随机偏移量（正负随机），幅度与距离和强度成正比
    offset = random.uniform(-1.0, 1.0) * curve_strength * dist * 0.2
    ctrl_x = mid_x + perp_x * offset
    ctrl_y = mid_y + perp_y * offset
    return ctrl_x, ctrl_y


def verification_search_region(
    center_x: float,
    center_y: float,
    template_width: float,
    template_height: float,
    scale: float,
) -> tuple[int, int, int, int]:
    """Compute the search region for auxiliary image verification.

    Builds a bounding box around the expected match location with margin
    proportional to the scaled template size.

    Args:
        center_x: Expected match center X.
        center_y: Expected match center Y.
        template_width: Original template width in pixels.
        template_height: Original template height in pixels.
        scale: Scale factor applied during matching.

    Returns:
        (x, y, width, height) region tuple for screenshot capture.
    """
    scale = max(0.01, scale)
    matched_width = max(1.0, float(template_width) * scale)
    matched_height = max(1.0, float(template_height) * scale)
    margin = max(12.0, max(matched_width, matched_height) * 0.35)
    return (
        int(math.floor(center_x - matched_width / 2.0 - margin)),
        int(math.floor(center_y - matched_height / 2.0 - margin)),
        int(math.ceil(matched_width + margin * 2.0)),
        int(math.ceil(matched_height + margin * 2.0)),
    )
