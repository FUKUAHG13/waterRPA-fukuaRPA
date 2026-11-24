# ruff: noqa: E402
"""移动采样模型的合成轨迹测试：证明转弯/停顿/变速被保留、冗余点被压掉，
并证明匀速密集移动不再被错误地压缩成首尾两点（原样回放跳动缺陷的红绿对照）。"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fukua_rpa.recording_sampler import (  # noqa: E402
    DEFAULT_MOVE_WINDOW_SECONDS,
    MoveSample,
    sample_moves,
)


def _pts(spec):
    return [MoveSample(t, x, y) for t, x, y in spec]


def _max_gap(out):
    """相邻保留点的最大时间隔（秒）。"""
    return max((b.t - a.t for a, b in zip(out, out[1:], strict=False)), default=0.0)


class MoveSamplerTests(unittest.TestCase):
    def test_window_default_is_8ms(self):
        self.assertAlmostEqual(DEFAULT_MOVE_WINDOW_SECONDS, 0.008)

    def test_straight_line_keeps_replayable_intermediate_points(self):
        # 红绿对照：旧实现按“原始相邻点”判定，24 个 8ms 点只留首尾（间隔 184ms），
        # 回放表现为光标先停再跳。新实现按“保留后间距”判定，必须留下中间点。
        pts = _pts([(i * 0.008, 100 + i, 200) for i in range(24)])
        out = sample_moves(pts)
        self.assertGreater(len(out), 2, "匀速密集移动必须保留可回放的中间点")
        self.assertLessEqual(
            _max_gap(out),
            DEFAULT_MOVE_WINDOW_SECONDS + 1e-9,
            "相邻保留点间隔不得超过采样窗口，否则回放会停顿后跳变",
        )
        self.assertEqual((out[0].x, out[0].y), (100, 200))
        self.assertEqual((out[-1].x, out[-1].y), (123, 200))

    def test_dense_sampling_compresses_to_window_interval(self):
        # 4ms 采样（比窗口密一倍）应压到约 8ms 一个保留点，而不是全部丢弃。
        pts = _pts([(i * 0.004, i, 0) for i in range(25)])  # t=0..0.096
        out = sample_moves(pts)
        self.assertGreater(len(out), 2)
        self.assertLess(len(out), len(pts))
        self.assertLessEqual(_max_gap(out), DEFAULT_MOVE_WINDOW_SECONDS + 1e-9)

    def test_duplicate_coordinates_removed(self):
        pts = _pts([(0.0, 10, 10), (0.004, 10, 10), (0.008, 10, 10), (0.012, 15, 15)])
        out = sample_moves(pts)
        self.assertEqual([p.as_point() for p in out], [(0.0, 10, 10), (0.012, 15, 15)])

    def test_speed_change_is_kept(self):
        pts = []
        x = 0
        for i in range(12):
            x += 8
            pts.append(MoveSample(i * 0.008, x, 200))
        for i in range(12, 24):
            x += 2
            pts.append(MoveSample(i * 0.008, x, 200))
        out = sample_moves(pts)
        # 变速拐点（x≈96）必须保留
        self.assertTrue(any(90 <= p.x <= 100 for p in out))
        self.assertLessEqual(_max_gap(out), DEFAULT_MOVE_WINDOW_SECONDS + 1e-9)

    def test_direction_flip_is_kept(self):
        pts = _pts(
            [(i * 0.008, 100 + i * 3 if i < 8 else 124 - (i - 8) * 3, 200) for i in range(16)]
        )
        out = sample_moves(pts)
        # 折返点（峰值 x≈124）必须保留
        self.assertTrue(any(p.x >= 120 for p in out))
        # 首尾与原始轨迹一致
        self.assertEqual((out[0].t, out[0].x), (pts[0].t, pts[0].x))
        self.assertEqual((out[-1].t, out[-1].x), (pts[-1].t, pts[-1].x))

    def test_pause_before_after_kept(self):
        pts = _pts([(i * 0.008, 100 + i * 2, 200) for i in range(6)])
        pts += _pts([(0.05 + i * 0.008, 110 + i * 2, 200) for i in range(6)])
        out = sample_moves(pts)
        # 停顿前后的两段位置都在（非仅首尾两点）
        self.assertGreaterEqual(len(out), 3)

    def test_pause_time_relationship_preserved(self):
        # 长停顿：到达 x=40 后原地停顿 0.16s 再继续。保留点须含停顿前后位置，
        # 且时刻差反映真实停顿（不是被压成一次跳变）。
        pts = _pts(
            [
                (0.0, 0, 0),
                (0.04, 40, 0),
                (0.08, 40, 0),
                (0.16, 40, 0),
                (0.20, 80, 0),
            ]
        )
        out = sample_moves(pts)
        coords = [(p.t, p.x) for p in out]
        self.assertIn((0.04, 40), coords, "停顿前到达点应保留")
        self.assertIn((0.20, 80), coords, "停顿后移动点应保留")

    def test_empty_and_single(self):
        self.assertEqual(sample_moves([]), [])
        one = [MoveSample(0.0, 5, 5)]
        self.assertEqual(sample_moves(one), one)

    def test_accepts_tuples(self):
        out = sample_moves([(0.0, 1, 1), (0.2, 2, 2)])
        self.assertEqual([p.as_point() for p in out], [(0.0, 1, 1), (0.2, 2, 2)])


if __name__ == "__main__":
    unittest.main()
