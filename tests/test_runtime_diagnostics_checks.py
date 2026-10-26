"""FR-20261008-102 回归守护：运行自检的检查项名称必须唯一且结构完整。

缺陷
----
``run_runtime_diagnostics`` 在原生核心构造成功后**立刻**追加 ``native_dll_api``，
随后才调用 ``native.performance_stats()`` / ``capabilities()`` /
``abi_snapshot()``。任一步抛异常（故障 DLL、不兼容 DLL）时，``except`` **再次**
追加同名 ``native_dll_api``，于是报告里出现两条同名且互相矛盾的行，而
``native_performance_api`` / ``native_capabilities`` / ``native_abi`` /
``native_build_flags`` 全部缺失——整体 ``ok`` 虽仍为 ``False``，但报告结构失真、
无法逐项对账（自检报告会被 ``--self-test-file`` 消费）。

本文件在缺陷对象上因"同名条目重复且后续检查缺失"而失败，在修复对象上通过。
"""

from __future__ import annotations

import unittest
from unittest import mock

from fukua_rpa import diagnostics

_EXPECTED_NATIVE_CHECKS = (
    "native_dll_api",
    "native_performance_api",
    "native_capabilities",
    "native_abi",
    "native_build_flags",
)


class _FaultyNative:
    """构造成功，但第一次能力查询就抛异常的替身（模拟故障 DLL）。"""

    available = True
    version = 11500

    @staticmethod
    def performance_stats():
        raise RuntimeError("faulty dll")

    @staticmethod
    def capabilities():
        return {}

    @staticmethod
    def abi_snapshot():
        return {}

    @staticmethod
    def health_snapshot():
        return {}


class RuntimeDiagnosticsStructureTests(unittest.TestCase):
    def test_faulty_native_calls_keep_check_names_unique_and_complete(self):
        with mock.patch.object(
            diagnostics, "NativeVisionCore", lambda **kwargs: _FaultyNative()
        ):
            report = diagnostics.run_runtime_diagnostics()

        names = [check["name"] for check in report["checks"]]
        self.assertEqual(len(names), len(set(names)), f"检查项名称重复: {names}")
        for expected in _EXPECTED_NATIVE_CHECKS:
            self.assertIn(expected, names, f"缺少检查项 {expected}")
        self.assertFalse(report["ok"])

    def test_healthy_native_calls_still_produce_each_check_once(self):
        healthy = mock.MagicMock()
        healthy.available = True
        healthy.version = 11500
        healthy.performance_stats.return_value = {"captures": 1, "cache_entries": 1}
        healthy.capabilities.return_value = dict.fromkeys(
            (
                "gdi_capture",
                "multi_scale",
                "grayscale",
                "color",
                "abi_metadata",
                "bounded_job_pool",
                "preferred_scale_fallback",
                "preferred_scale_list",
                "explicit_scale_only",
                "low_res_scene_fingerprint",
                "dxgi_scene_change",
                "cancellable_search",
                "opencv_color_semantics",
                "raw_rgb_capture",
            ),
            True,
        )
        healthy.abi_snapshot.return_value = {
            "compatible": True,
            "pointer_bits": 64,
            "struct_sizes": {},
            "build_flags": {
                "x64": True,
                "static_crt": True,
                "cpp17": True,
                "windows10_target": True,
                "msvc": True,
                "mask": 0,
            },
        }
        healthy.health_snapshot.return_value = {}

        with mock.patch.object(
            diagnostics, "NativeVisionCore", lambda **kwargs: healthy
        ):
            report = diagnostics.run_runtime_diagnostics()

        names = [check["name"] for check in report["checks"]]
        self.assertEqual(len(names), len(set(names)), f"检查项名称重复: {names}")
        for expected in _EXPECTED_NATIVE_CHECKS:
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
