"""FR-20260811-D7-001: 场景签名区域输入空间→物理像素换算与混合 DPI 回退。

锁定三分支：PMv2 恒等映射（区域原样）、混合 DPI 多物理区域（返回 None 回退）、
多输入区域各自单映射；以及 capture_scene_signatures/begin_scene_monitor 的回退行为。
"""
from __future__ import annotations

import unittest
from unittest import mock

from fukua_rpa.vision.scene import VisionSceneMixin


class _Perf:
    def increment(self, *args, **kwargs):
        pass

    def observe_since(self, *args, **kwargs):
        pass


class _Native:
    available = True
    has_scene_fingerprint = True
    has_dxgi_scene_change = True
    dxgi_scene_usable = True

    def capture_scene_signature(self, region):
        return 8, 8, b"\x00" * 64

    def poll_desktop_change(self, regions, reset_baseline=False):
        return True


class _Host(VisionSceneMixin):
    def __init__(self, regions, native):
        self._regions = regions
        self.native_core = native
        self.performance = _Perf()
        self.use_native_core = True
        self._native_disabled_for_run = False
        self.scene_signature_cache = {}

    def effective_search_regions(self, search_regions=None):
        return list(self._regions)

    def screen_bounds(self):
        return (0, 0, 1920, 1080)

    def check_stop_flag(self):
        return False

    def iter_search_screenshots(self, search_regions=None):
        return iter(())


class NativePhysicalRegionsTests(unittest.TestCase):
    def _host(self, regions):
        return _Host(regions, _Native())

    def _patch_mapper(self, side_effect):
        return mock.patch(
            "fukua_rpa.coordinates.input_rect_to_physical_regions",
            side_effect=side_effect,
        )

    def test_pmv2_identity_mapping(self):
        host = self._host([(10, 20, 100, 50)])
        with self._patch_mapper(lambda rect: [tuple(int(v) for v in rect)]):
            self.assertEqual(
                host._native_physical_regions(None), [(10, 20, 100, 50)]
            )

    def test_mixed_dpi_multi_region_returns_none(self):
        host = self._host([(10, 20, 100, 50)])
        with self._patch_mapper(
            lambda rect: [(10, 20, 100, 50), (1010, 20, 100, 50)]
        ):
            self.assertIsNone(host._native_physical_regions(None))

    def test_empty_search_regions_uses_screen_bounds(self):
        host = self._host([])
        captured = {}

        def mapper(rect):
            captured["rect"] = tuple(int(v) for v in rect)
            return [tuple(int(v) for v in rect)]

        with self._patch_mapper(mapper):
            result = host._native_physical_regions(None)
        self.assertEqual(captured["rect"], (0, 0, 1920, 1080))
        self.assertEqual(result, [(0, 0, 1920, 1080)])

    def test_capture_physical_regions_passed_to_native(self):
        host = self._host([(10, 20, 100, 50)])
        with self._patch_mapper(lambda rect: [tuple(int(v) for v in rect)]), mock.patch.object(
            host.native_core, "capture_scene_signature",
            return_value=(8, 8, b"\x00" * 64),
        ) as cap:
            sigs = host.capture_scene_signatures(None)
        self.assertEqual(len(sigs), 1)
        cap.assert_called_once_with((10, 20, 100, 50))

    def test_capture_mixed_dpi_falls_back_to_screenshot_path(self):
        host = self._host([(10, 20, 100, 50)])
        with self._patch_mapper(
            lambda rect: [(10, 20, 100, 50), (1010, 20, 100, 50)]
        ), mock.patch.object(host, "iter_search_screenshots") as fallback:
            fallback.return_value = iter(())
            host.capture_scene_signatures(None)
            fallback.assert_called_once()

    def test_begin_monitor_mixed_dpi_falls_back_to_fingerprint(self):
        host = self._host([(10, 20, 100, 50)])
        with self._patch_mapper(
            lambda rect: [(10, 20, 100, 50), (1010, 20, 100, 50)]
        ), mock.patch.object(
            _Native, "poll_desktop_change", wraps=_Native.poll_desktop_change
        ) as poll:
            result = host.begin_scene_monitor({"key": "k", "search_regions": None})
        self.assertEqual(result["mode"], "fingerprint")
        poll.assert_not_called()

    def test_begin_monitor_mapper_exception_falls_back(self):
        host = self._host([(10, 20, 100, 50)])

        def boom(rect):
            raise RuntimeError("mapper exploded")

        with self._patch_mapper(boom), mock.patch.object(
            _Native, "poll_desktop_change", wraps=_Native.poll_desktop_change
        ) as poll:
            result = host.begin_scene_monitor({"key": "k", "search_regions": None})
        self.assertEqual(result["mode"], "fingerprint")
        poll.assert_not_called()


if __name__ == "__main__":
    unittest.main()
