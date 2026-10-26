"""SCR03 守卫测试：benchmark_util.percentile 的 nearest-rank 边界行为。

锁定两条不变量，防止未来"稳健分位"改造只改实现而不同步门禁阈值：

1. n=10 时 P95 退化为 max（数学可证：ceil(10*0.95)-1 = 9 = 末位）；
2. 样本量增大后 P95 脱离 max（n=20 时 index=18，P95 < max）。

同时锁定空样本抛 ValueError 与 P50 中位语义。
"""

import unittest

from scripts.benchmark_util import percentile


class PercentileBoundaryTests(unittest.TestCase):
    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            percentile([], 95)

    def test_p50_is_median(self):
        self.assertEqual(percentile([1, 2, 3, 4, 5], 50), 3)
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2)  # ceil(4*0.5)-1 = 1

    def test_n10_p95_equals_max(self):
        # SCR03：nearest-rank 在 n=10 时 P95 退化为 max（有意保守取舍）。
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(percentile(values, 95), 10)
        self.assertEqual(percentile(values, 95), max(values))

    def test_larger_sample_p95_detaches_from_max(self):
        # n=20：ceil(20*0.95)-1 = 18，P95 = 第 19 小值 < max。
        values = list(range(1, 21))
        self.assertEqual(percentile(values, 95), 19)
        self.assertLess(percentile(values, 95), max(values))

    def test_p100_always_max(self):
        self.assertEqual(percentile([3, 1, 2], 100), 3)


if __name__ == "__main__":
    unittest.main()
