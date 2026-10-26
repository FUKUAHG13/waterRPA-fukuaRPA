"""OPEN-RC-20261015-PERF 守卫测试：benchmark_util 的核心类别亲和推导与读回自检。

锁定三条不变量：

1. 目标类别由机器自报 ``EfficiencyClass`` 的**最大值**推导，掩码 = 该类全部 LPI 的按位或
   （**不得**退化为硬编码常量）；
2. 读回自检在任何不一致时**显式失败**，绝不静默退化到未绑定运行；
3. ``all_flags`` 只作原始字节记录，不参与任何判定。

另含一条真实环境集成用例：绑定后读回的掩码必须等于推导掩码（用后恢复原掩码，
避免污染测试进程）。
"""

from __future__ import annotations

import ctypes
import unittest
from ctypes import wintypes

from scripts.benchmark_util import (
    apply_benchmark_affinity,
    derive_target_core_class,
    read_cpu_set_map,
    verify_affinity_readback,
)


def _row(lpi, efficiency_class):
    return {
        "record": lpi,
        "size_field": 32,
        "lpi": lpi,
        "core_index": lpi,
        "efficiency_class": efficiency_class,
        "all_flags": 0,
        "scheduling_class": 0,
    }


class DeriveTargetCoreClassTests(unittest.TestCase):
    def test_picks_highest_efficiency_class(self):
        rows = [_row(0, 0), _row(1, 1), _row(2, 1), _row(3, 0)]
        target, mask, lpis = derive_target_core_class(rows)
        self.assertEqual(target, 1)
        self.assertEqual(lpis, [1, 2])
        self.assertEqual(mask, 0b110)

    def test_single_class_covers_all_lpis(self):
        rows = [_row(lpi, 0) for lpi in range(4)]
        target, mask, lpis = derive_target_core_class(rows)
        self.assertEqual(target, 0)
        self.assertEqual(lpis, [0, 1, 2, 3])
        self.assertEqual(mask, 0b1111)

    def test_mask_is_derived_not_hardcoded(self):
        # 一组刻意非 0xFFFF 的类别归属：掩码必须随输入变化。
        rows = [_row(3, 5), _row(7, 5), _row(9, 2)]
        target, mask, lpis = derive_target_core_class(rows)
        self.assertEqual(target, 5)
        self.assertEqual(lpis, [3, 7])
        self.assertEqual(mask, (1 << 3) | (1 << 7))
        self.assertNotEqual(mask, 0xFFFF)

    def test_lpi_above_31_is_representable(self):
        rows = [_row(40, 1), _row(41, 1)]
        _, mask, _ = derive_target_core_class(rows)
        self.assertEqual(mask, (1 << 40) | (1 << 41))

    def test_empty_raises(self):
        with self.assertRaises(RuntimeError):
            derive_target_core_class([])


class VerifyAffinityReadbackTests(unittest.TestCase):
    def test_match_returns_usable_count(self):
        self.assertEqual(verify_affinity_readback(0b1011, 0b1011, 3), 3)

    def test_mask_mismatch_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            verify_affinity_readback(0b1111, 0b0111, 3)
        self.assertIn("self-check failed", str(ctx.exception))

    def test_count_mismatch_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            verify_affinity_readback(0b1111, 0b1111, 3)
        self.assertIn("target-class LPs", str(ctx.exception))


class CpuSetMapTests(unittest.TestCase):
    def test_read_cpu_set_map_is_self_consistent(self):
        rows = read_cpu_set_map()
        self.assertTrue(rows, "GetSystemCpuSetInformation returned no rows")
        lpis = [row["lpi"] for row in rows]
        self.assertEqual(len(lpis), len(set(lpis)), "duplicate logical processor indices")
        for row in rows:
            self.assertGreater(row["size_field"], 0)
            self.assertIn("efficiency_class", row)
            self.assertIn("all_flags", row)
        target, mask, target_lpis = derive_target_core_class(rows)
        self.assertEqual(bin(mask).count("1"), len(target_lpis))
        self.assertEqual(
            sorted(row["lpi"] for row in rows if row["efficiency_class"] == target),
            sorted(target_lpis),
        )


class ApplyBenchmarkAffinityTests(unittest.TestCase):
    """真实环境集成用例：绑定 → 读回 → 用后恢复原掩码。"""

    def _kernel32(self):
        kernel32 = ctypes.windll.kernel32
        kernel32.GetProcessAffinityMask.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.GetProcessAffinityMask.restype = wintypes.BOOL
        kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
        kernel32.SetProcessAffinityMask.restype = wintypes.BOOL
        return kernel32

    def test_binding_self_checks_and_restores(self):
        kernel32 = self._kernel32()
        handle = kernel32.GetCurrentProcess()
        original = ctypes.c_size_t()
        system = ctypes.c_size_t()
        self.assertTrue(
            kernel32.GetProcessAffinityMask(
                handle, ctypes.byref(original), ctypes.byref(system)
            )
        )
        try:
            env = apply_benchmark_affinity(load_sample_seconds=0.1)
            self.assertTrue(env["affinity_verified"])
            target, mask, lpis = derive_target_core_class(read_cpu_set_map())
            self.assertEqual(env["cpu_core_class"], target)
            self.assertEqual(int(env["affinity_mask_hex"], 16), mask)
            self.assertEqual(env["usable_lp_count"], len(lpis))
            self.assertEqual(env["usable_lp_count"], bin(mask).count("1"))
            self.assertLessEqual(env["usable_lp_count"], env["usable_lp_count_before"])
            actual = ctypes.c_size_t()
            self.assertTrue(
                kernel32.GetProcessAffinityMask(
                    handle, ctypes.byref(actual), ctypes.byref(system)
                )
            )
            self.assertEqual(actual.value, mask)
        finally:
            kernel32.SetProcessAffinityMask(handle, ctypes.c_size_t(original.value))


if __name__ == "__main__":
    unittest.main()
