# ruff: noqa: E402
"""v1.1.17 修复 2：鼠标轨迹采样点纯模型契约（不依赖 Qt）。"""
import unittest

from fukua_rpa.trajectory_editor import (
    format_trajectory_summary,
    parse_trajectory,
    serialize_trajectory,
    validate_points,
)


class TrajectoryModelTests(unittest.TestCase):
    def test_parse_recorded_trajectory_roundtrip(self):
        raw = "[[0,100,100],[144,130,109]]"
        result = parse_trajectory(raw)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.points, [(0, 100, 100), (144, 130, 109)])
        self.assertEqual(serialize_trajectory(result.points), raw)

    def test_parse_accepts_first_point_nonzero_kept_verbatim(self):
        # 已有合法首点时刻原样往返（不强制归一为 0）。
        raw = "[[37,10,20],[80,30,40]]"
        result = parse_trajectory(raw)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(result.points, [(37, 10, 20), (80, 30, 40)])
        self.assertEqual(serialize_trajectory(result.points), raw)

    def test_edit_second_point(self):
        raw = "[[0,100,100],[144,130,109]]"
        result = parse_trajectory(raw)
        points = list(result.points)
        points[1] = (220, 160, 120)
        self.assertTrue(validate_points(points).ok)
        self.assertEqual(
            serialize_trajectory(points), "[[0,100,100],[220,160,120]]"
        )

    def test_summary_from_actual_data(self):
        points = [(0, 100, 100), (220, 160, 120)]
        summary = format_trajectory_summary(points)
        self.assertIn("2 点", summary)
        self.assertIn("0.220 秒", summary)

    def test_accepts_negative_coords_and_single_zero_point(self):
        self.assertTrue(validate_points([(0, -100, -200)]).ok)
        self.assertTrue(validate_points([(0, -5, 9), (0, -6, 10)]).ok)

    def test_allows_equal_milliseconds_preserving_order(self):
        points = [(0, 1, 1), (0, 2, 2), (0, 3, 3)]
        result = validate_points(points)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(
            serialize_trajectory(points), "[[0,1,1],[0,2,2],[0,3,3]]"
        )

    def test_rejects_time_reversal_with_line_number(self):
        result = validate_points([(0, 1, 1), (5, 2, 2), (3, 3, 3)])
        self.assertFalse(result.ok)
        self.assertIn("3", result.reason)  # 1-based 采样行号

    def test_rejects_out_of_range_coord_and_time(self):
        self.assertFalse(validate_points([(0, 100001, 0)]).ok)
        self.assertFalse(validate_points([(-1, 0, 0)]).ok)

    def test_rejects_non_integer_and_malformed(self):
        self.assertFalse(parse_trajectory("[[0,1.5,2]]").ok)
        self.assertFalse(parse_trajectory("not-json").ok)
        self.assertFalse(parse_trajectory("[]").ok)
        self.assertFalse(parse_trajectory("[[0,1]]").ok)

    def test_rejects_nonmonotonic_from_raw_with_line(self):
        result = parse_trajectory("[[0,1,1],[10,2,2],[9,3,3]]")
        self.assertFalse(result.ok)
        self.assertIn("3", result.reason)

    def test_max_bytes_bound(self):
        huge = "[" + ",".join(["[0,0,0]"] * 200000) + "]"
        result = parse_trajectory(huge)
        self.assertFalse(result.ok)


if __name__ == "__main__":
    unittest.main()
