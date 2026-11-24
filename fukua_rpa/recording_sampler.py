"""Sampled mouse-move reduction for the raw "replay as recorded" mode.

This is a pure, clock-free model: it only sees already-timestamped move points
and returns a reduced set that keeps the shape of the gesture (start, end,
turns, pauses and speed changes) while dropping redundant samples. The hook
callback never calls this module; reduction runs once at conversion time.

INVARIANT: only *consecutive* move points ever enter the same reduction pass.
Any non-move event (button down/up, wheel, key) is a hard boundary, because a
press/release or wheel tick may need a same-timestamp move injected next to it
and must not be separated from its neighbouring path by a merged point.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# DESIGN: 0.008 s 是采样窗口起点。低级鼠标钩子在 Windows 上以 ~125 Hz（8 ms）
# 报告移动；小于该间隔的点多为同一物理运动的重复采样，合并它们不会丢失可见的
# 转弯或停顿。该值可用合成轨迹校准（见 tests/test_recording_sampler.py）。
DEFAULT_MOVE_WINDOW_SECONDS = 0.008

# 连续同坐标点视为冗余；保留段首尾。
Point = tuple[float, int, int]  # (t, x, y)


@dataclass(frozen=True)
class MoveSample:
    """A single timestamped cursor position (timestamps are relative seconds)."""

    t: float
    x: int
    y: int

    @classmethod
    def from_point(cls, point: Point) -> MoveSample:
        return cls(float(point[0]), int(point[1]), int(point[2]))

    def as_point(self) -> Point:
        return (self.t, self.x, self.y)


def _same_point(a: MoveSample, b: MoveSample) -> bool:
    return a.x == b.x and a.y == b.y


def _direction_and_speed(first: MoveSample, second: MoveSample):
    dx = second.x - first.x
    dy = second.y - first.y
    dt = second.t - first.t
    if dt <= 0:
        return None  # 无法判断速度；视为需要保留
    return (dx, dy, dx / dt, dy / dt)


def _significant_change(prev: MoveSample, cur: MoveSample, nxt: MoveSample) -> bool:
    """Return True when *cur* is a turn / pause / speed change worth keeping.

    We keep a middle point when the motion direction flips on either axis, or
    when the per-axis speed changes by a large factor. This preserves corners,
    stop-then-go and fast-then-slow gestures even under a short time window.
    """
    before = _direction_and_speed(prev, cur)
    after = _direction_and_speed(cur, nxt)
    if before is None or after is None:
        return True
    dx0, dy0, vx0, vy0 = before
    dx1, dy1, vx1, vy1 = after
    # 方向翻转（含停顿后反向）
    if dx0 * dx1 < 0 or dy0 * dy1 < 0:
        return True
    # 从移动变停顿，或从停顿变移动
    if (dx0 == 0 and dy0 == 0) != (dx1 == 0 and dy1 == 0):
        return True
    # 速度显著变化（4 倍）：保留先快后慢 / 先慢后快的拐点
    for v0, v1 in ((vx0, vx1), (vy0, vy1)):
        a0, a1 = abs(v0), abs(v1)
        if a0 == 0 or a1 == 0:
            continue
        if a1 >= a0 * 4 or a0 >= a1 * 4:
            return True
    return False


def reduce_move_segment(
    points: Sequence[MoveSample],
    window_seconds: float = DEFAULT_MOVE_WINDOW_SECONDS,
) -> list[MoveSample]:
    """Reduce one consecutive move segment to its shape-defining points.

    Steps: (1) drop consecutive duplicate coordinates; (2) within each
    moving stretch, keep the next original point whenever skipping it would
    leave a gap larger than the window measured from the **last kept point**;
    always keep turns, pauses and speed changes, plus the segment endpoints.
    """
    if not points:
        return []
    if len(points) <= 2:
        return list(points)

    # (1) 去冗余同坐标（保留每段同坐标游程的首点）。连续同坐标表示停顿，
    # 停顿的时间关系由游程末点到下一个不同坐标点之间的时间隔承载，无需保留
    # 游程中的每个重复点。
    deduped: list[MoveSample] = [points[0]]
    for point in points[1:]:
        if not _same_point(deduped[-1], point):
            deduped.append(point)
    if len(deduped) <= 2:
        return deduped

    # (2) 按“保留后的间距”决定能否删点。
    # DESIGN: 旧实现拿 cur 与原始相邻点 deduped[i-1] 比时间差；密集匀速段里
    # 该差恒为 8ms，于是整段被判为可合并，只剩首尾（24 点 → 2 点，间隔 184ms），
    # 回放时表现为光标先停住再跳到终点。正确判据是：若删掉 cur，下一个有机会
    # 保留的原始点至少是 nxt，last_kept 到 nxt 的间隔若超过窗口，就会形成可见的
    # 停顿+跳变，因此必须保留 cur。这样密集匀速段按窗口均匀留点，稀疏段自然
    # 保留两侧位置与时刻。加 1e-9 容差消除 i*0.008 的浮点累积误差。
    window = max(0.0, float(window_seconds)) + 1e-9
    kept: list[MoveSample] = [deduped[0]]
    for i in range(1, len(deduped) - 1):
        cur = deduped[i]
        nxt = deduped[i + 1]
        last_kept = kept[-1]
        gap_without_cur = nxt.t - last_kept.t
        if gap_without_cur > window or _significant_change(
            deduped[i - 1], cur, nxt
        ):
            kept.append(cur)
    kept.append(deduped[-1])
    return kept


def sample_moves(
    points: Sequence[MoveSample],
    window_seconds: float = DEFAULT_MOVE_WINDOW_SECONDS,
) -> list[MoveSample]:
    """Public entry: reduce a sequence of move points.

    Accepts MoveSample objects or (t, x, y) tuples. There is no cross-event
    context here by design — callers must pass one contiguous move segment at
    a time (see the INVARIANT note at the top of this module).
    """
    normalized = [
        p if isinstance(p, MoveSample) else MoveSample.from_point(p) for p in points
    ]
    return reduce_move_segment(normalized, window_seconds=window_seconds)
