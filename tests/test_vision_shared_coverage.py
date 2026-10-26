"""Targeted coverage tests for fukua_rpa.vision.shared pure-logic paths."""

import threading
import unittest
from unittest import mock

from PIL import Image

from fukua_rpa.vision.shared import (
    _CAPTURE_PROVENANCE,
    VisionSharedMixin,
    capture_returns_physical_pixels,
    match_input_dimensions,
)

# ---------------------------------------------------------------------------
# Minimal concrete host for VisionSharedMixin methods
# ---------------------------------------------------------------------------

class _FakePerformance:
    def __init__(self):
        self.counters = {}

    def increment(self, key, amount=1):
        self.counters[key] = self.counters.get(key, 0) + amount

    def observe_since(self, key, start_ns):
        pass


class _MixinHost(VisionSharedMixin):
    """Bare-minimum object that satisfies VisionSharedMixin attribute access."""

    def __init__(self):
        self.miss_streaks = {}
        self.performance = _FakePerformance()
        self.use_fast_screenshot = False
        self.use_native_core = False
        self.scan_regions = []
        self.scan_region = None
        self._mss_instance = None
        self._mss_disabled_for_run = False
        self._native_capture_disabled_for_run = False
        self._native_capture_failure_logged = False
        self._mss_failure_logged = False
        self.native_core = None
        self.verbose_log_enabled = lambda *a: False
        self.log = lambda *a, **kw: None
        self.check_stop_flag = lambda: False

    def close_screenshot_backend(self):
        pass


# ===========================================================================
# match_input_dimensions (module-level pure function)
# ===========================================================================

class TestMatchInputDimensions(unittest.TestCase):
    """Cover lines 68-83 of shared.py."""

    def test_six_field_match_returns_explicit_dims(self):
        match = (100, 200, 1.0, 0.95, 48.0, 32.0)
        w, h = match_input_dimensions(match, 64, 64)
        self.assertEqual(w, 48.0)
        self.assertEqual(h, 32.0)

    def test_six_field_match_nonfinite_falls_back(self):
        match = (100, 200, 0.5, 0.9, float("nan"), 32.0)
        w, h = match_input_dimensions(match, 80, 40)
        self.assertAlmostEqual(w, 40.0)  # 80 * 0.5
        self.assertAlmostEqual(h, 20.0)  # 40 * 0.5

    def test_six_field_match_zero_dim_falls_back(self):
        match = (0, 0, 2.0, 0.8, 0.0, 10.0)
        w, h = match_input_dimensions(match, 10, 10)
        self.assertAlmostEqual(w, 20.0)  # 10 * 2.0
        self.assertAlmostEqual(h, 20.0)

    def test_four_field_match_uses_scale(self):
        match = (50, 60, 1.5, 0.99)
        w, h = match_input_dimensions(match, 100, 50)
        self.assertAlmostEqual(w, 150.0)
        self.assertAlmostEqual(h, 75.0)

    def test_scale_clamped_to_minimum(self):
        match = (50, 60, 0.001, 0.99)
        w, h = match_input_dimensions(match, 100, 50)
        # scale clamped to 0.01
        self.assertAlmostEqual(w, max(1.0, 100 * 0.01))
        self.assertAlmostEqual(h, max(1.0, 50 * 0.01))

    def test_invalid_match_type_falls_back(self):
        # match[2] not convertible to float
        match = (50, 60, "bad", 0.99)
        w, h = match_input_dimensions(match, 20, 30)
        self.assertAlmostEqual(w, 20.0)  # scale defaults to 1.0
        self.assertAlmostEqual(h, 30.0)

    def test_short_match_index_error(self):
        match = (50,)
        w, h = match_input_dimensions(match, 10, 10)
        self.assertAlmostEqual(w, 10.0)
        self.assertAlmostEqual(h, 10.0)

    def test_none_match_falls_back(self):
        w, h = match_input_dimensions(None, 55, 44)
        self.assertAlmostEqual(w, 55.0)
        self.assertAlmostEqual(h, 44.0)

    def test_minimum_dimension_clamp(self):
        # template_width * scale < 1.0 -> clamped to 1.0
        match = (0, 0, 0.01, 0.5)
        w, h = match_input_dimensions(match, 0.5, 0.5)
        self.assertGreaterEqual(w, 1.0)
        self.assertGreaterEqual(h, 1.0)


# ===========================================================================
# capture_returns_physical_pixels (module-level)
# ===========================================================================

class TestCaptureProvenance(unittest.TestCase):
    """Cover the thread-local provenance flag (line 51)."""

    def test_default_false(self):
        # In a fresh thread the attribute is unset -> False
        result = []
        def _check():
            result.append(capture_returns_physical_pixels())
        t = threading.Thread(target=_check)
        t.start()
        t.join()
        self.assertFalse(result[0])

    def test_true_when_set(self):
        old = getattr(_CAPTURE_PROVENANCE, "physical", None)
        try:
            _CAPTURE_PROVENANCE.physical = True
            self.assertTrue(capture_returns_physical_pixels())
        finally:
            if old is None:
                _CAPTURE_PROVENANCE.physical = False
            else:
                _CAPTURE_PROVENANCE.physical = old


# ===========================================================================
# VisionSharedMixin pure-logic methods
# ===========================================================================

class TestRecognitionKey(unittest.TestCase):
    """Cover lines 93-94."""

    def setUp(self):
        self.host = _MixinHost()

    def test_key_with_step_info(self):
        key = self.host.recognition_key("img.png", {"step": 3})
        self.assertEqual(key[0], 3)
        self.assertTrue(key[1].endswith("img.png"))

    def test_key_without_step_info(self):
        key = self.host.recognition_key("img.png", None)
        self.assertEqual(key[0], 0)

    def test_key_missing_step_field(self):
        key = self.host.recognition_key("img.png", {})
        self.assertEqual(key[0], 0)


class TestMissStreaks(unittest.TestCase):
    """Cover lines 97-99, 102."""

    def setUp(self):
        self.host = _MixinHost()

    def test_record_increments(self):
        info = {"step": 1}
        self.assertEqual(self.host.record_recognition_miss("a.png", info), 1)
        self.assertEqual(self.host.record_recognition_miss("a.png", info), 2)

    def test_reset_clears(self):
        info = {"step": 1}
        self.host.record_recognition_miss("a.png", info)
        self.host.reset_recognition_miss("a.png", info)
        # After reset, next record starts at 1
        self.assertEqual(self.host.record_recognition_miss("a.png", info), 1)

    def test_reset_nonexistent_no_error(self):
        self.host.reset_recognition_miss("x.png", {"step": 99})


class TestAsBool(unittest.TestCase):
    """Cover lines 105-107."""

    def setUp(self):
        self.host = _MixinHost()

    def test_truthy_strings(self):
        for val in ("1", "true", "yes", "on", "True", "YES", "ON"):
            self.assertTrue(self.host.as_bool(val), f"Failed for {val!r}")

    def test_chinese_truthy(self):
        # Chinese truthy values
        self.assertTrue(self.host.as_bool("\u542f\u7528"))
        self.assertTrue(self.host.as_bool("\u662f"))

    def test_falsy_strings(self):
        for val in ("0", "false", "no", "off", "", "random"):
            self.assertFalse(self.host.as_bool(val), f"Failed for {val!r}")

    def test_non_string_truthy(self):
        self.assertTrue(self.host.as_bool(1))
        self.assertTrue(self.host.as_bool([1]))

    def test_non_string_falsy(self):
        self.assertFalse(self.host.as_bool(0))
        self.assertFalse(self.host.as_bool(None))
        self.assertFalse(self.host.as_bool([]))


class TestNormalizedRegions(unittest.TestCase):
    """Cover lines 110-119."""

    def setUp(self):
        self.host = _MixinHost()

    def test_valid_regions(self):
        regions = [(10, 20, 100, 200), (0, 0, 50, 50)]
        result = self.host.normalized_regions(regions)
        self.assertEqual(result, [(10, 20, 100, 200), (0, 0, 50, 50)])

    def test_float_strings_converted(self):
        regions = [("10.5", "20.9", "100.1", "200.7")]
        result = self.host.normalized_regions(regions)
        self.assertEqual(result, [(10, 20, 100, 200)])

    def test_zero_width_filtered(self):
        regions = [(10, 20, 0, 200)]
        result = self.host.normalized_regions(regions)
        self.assertEqual(result, [])

    def test_negative_height_filtered(self):
        regions = [(10, 20, 100, -5)]
        result = self.host.normalized_regions(regions)
        self.assertEqual(result, [])

    def test_invalid_entry_skipped(self):
        regions = [(10, 20, 100, 200), "bad_entry", (5, 5, 10, 10)]
        result = self.host.normalized_regions(regions)
        self.assertEqual(result, [(10, 20, 100, 200), (5, 5, 10, 10)])

    def test_none_input(self):
        result = self.host.normalized_regions(None)
        self.assertEqual(result, [])

    def test_empty_list(self):
        result = self.host.normalized_regions([])
        self.assertEqual(result, [])


class TestRegionBoundingRect(unittest.TestCase):
    """Cover lines 278-282."""

    def setUp(self):
        self.host = _MixinHost()

    def test_single_region(self):
        regions = [(10, 20, 100, 200)]
        self.assertEqual(self.host.region_bounding_rect(regions), (10, 20, 100, 200))

    def test_multiple_regions(self):
        regions = [(0, 0, 50, 50), (100, 100, 50, 50)]
        # left=0, top=0, right=150, bottom=150
        self.assertEqual(self.host.region_bounding_rect(regions), (0, 0, 150, 150))

    def test_negative_origin(self):
        regions = [(-100, -50, 200, 100), (0, 0, 50, 50)]
        # left=-100, top=-50, right=100, bottom=50
        self.assertEqual(self.host.region_bounding_rect(regions), (-100, -50, 200, 100))


class TestClipRegionToBounds(unittest.TestCase):
    """Cover lines 316-324."""

    def setUp(self):
        self.host = _MixinHost()

    def test_region_inside_bounds(self):
        region = (10, 10, 50, 50)
        bounds = (0, 0, 1920, 1080)
        self.assertEqual(self.host.clip_region_to_bounds(region, bounds), (10, 10, 50, 50))

    def test_region_partially_outside(self):
        region = (1900, 1060, 100, 100)
        bounds = (0, 0, 1920, 1080)
        # clipped: left=1900, top=1060, right=1920, bottom=1080
        self.assertEqual(self.host.clip_region_to_bounds(region, bounds), (1900, 1060, 20, 20))

    def test_region_completely_outside(self):
        region = (2000, 2000, 100, 100)
        bounds = (0, 0, 1920, 1080)
        self.assertIsNone(self.host.clip_region_to_bounds(region, bounds))

    def test_region_left_of_bounds(self):
        region = (-200, -200, 100, 100)
        bounds = (0, 0, 1920, 1080)
        self.assertIsNone(self.host.clip_region_to_bounds(region, bounds))

    def test_negative_origin_bounds(self):
        region = (-50, -50, 200, 200)
        bounds = (-100, -100, 300, 300)
        # left=max(-50,-100)=-50, top=-50, right=min(150,200)=150, bottom=150
        self.assertEqual(self.host.clip_region_to_bounds(region, bounds), (-50, -50, 200, 200))

    def test_zero_overlap_returns_none(self):
        region = (100, 100, 50, 50)
        bounds = (200, 200, 100, 100)
        self.assertIsNone(self.host.clip_region_to_bounds(region, bounds))


class TestShouldBatchRegions(unittest.TestCase):
    """Cover lines 327-332."""

    def setUp(self):
        self.host = _MixinHost()

    def test_single_region_false(self):
        self.assertFalse(self.host.should_batch_regions([(0, 0, 100, 100)]))

    def test_empty_false(self):
        self.assertFalse(self.host.should_batch_regions([]))

    def test_overlapping_regions_true(self):
        # Two heavily overlapping regions: bbox area <= 3*total_area
        regions = [(0, 0, 100, 100), (10, 10, 100, 100)]
        # total_area = 10000+10000=20000, bbox=(0,0,110,110)=12100
        # 12100 <= max(60000, 70000) -> True
        self.assertTrue(self.host.should_batch_regions(regions))

    def test_far_apart_regions_false(self):
        # Two tiny regions far apart: bbox area >> total_area
        regions = [(0, 0, 10, 10), (10000, 10000, 10, 10)]
        # total_area=200, bbox=(0,0,10010,10010)=100200100
        # 100200100 <= max(600, 50200) -> False
        self.assertFalse(self.host.should_batch_regions(regions))


class TestEffectiveSearchRegions(unittest.TestCase):
    """Cover lines 335-343."""

    def setUp(self):
        self.host = _MixinHost()

    def test_explicit_regions_preferred(self):
        self.host.scan_regions = [(0, 0, 50, 50)]
        self.host.scan_region = (10, 10, 30, 30)
        result = self.host.effective_search_regions([(5, 5, 100, 100)])
        self.assertEqual(result, [(5, 5, 100, 100)])

    def test_falls_back_to_scan_regions(self):
        self.host.scan_regions = [(0, 0, 50, 50)]
        self.host.scan_region = (10, 10, 30, 30)
        result = self.host.effective_search_regions(None)
        self.assertEqual(result, [(0, 0, 50, 50)])

    def test_falls_back_to_scan_region(self):
        self.host.scan_regions = []
        self.host.scan_region = (10, 10, 30, 30)
        result = self.host.effective_search_regions(None)
        self.assertEqual(result, [(10, 10, 30, 30)])

    def test_no_regions_returns_empty(self):
        self.host.scan_regions = []
        self.host.scan_region = None
        result = self.host.effective_search_regions(None)
        self.assertEqual(result, [])


class TestTargetPositionKey(unittest.TestCase):
    """Cover line 382."""

    def setUp(self):
        self.host = _MixinHost()

    def test_key_structure(self):
        key = self.host.target_position_key("tpl.png", "ck", 0.9, True, False)
        self.assertTrue(key[0].endswith("tpl.png"))
        self.assertEqual(key[1], "ck")
        self.assertAlmostEqual(key[2], 0.9)
        self.assertTrue(key[3])
        self.assertFalse(key[4])

    def test_key_defaults(self):
        key = self.host.target_position_key("a.png", "k", 0.8, False)
        self.assertFalse(key[4])


# ===========================================================================
# iter_search_screenshots (mocked capture)
# ===========================================================================

class TestIterSearchScreenshots(unittest.TestCase):
    """Cover lines 346-377 with mocked capture_screenshot."""

    def setUp(self):
        self.host = _MixinHost()

    def test_no_regions_yields_full_screenshot(self):
        fake_img = Image.new("RGB", (100, 100))
        self.host.capture_screenshot = mock.Mock(return_value=(fake_img, 0, 0))
        results = list(self.host.iter_search_screenshots(None))
        self.assertEqual(len(results), 1)
        self.host.capture_screenshot.assert_called_once_with(None)

    def test_single_region_yields_region_capture(self):
        fake_img = Image.new("RGB", (50, 50))
        self.host.capture_screenshot = mock.Mock(return_value=(fake_img, 10, 20))
        results = list(self.host.iter_search_screenshots([(10, 20, 50, 50)]))
        self.assertEqual(len(results), 1)
        self.host.capture_screenshot.assert_called_once_with((10, 20, 50, 50))

    def test_batch_regions_crops(self):
        # Two overlapping regions that should batch
        big_img = Image.new("RGB", (200, 200))
        self.host.capture_screenshot = mock.Mock(return_value=(big_img, 0, 0))
        regions = [(0, 0, 100, 100), (10, 10, 100, 100)]
        results = list(self.host.iter_search_screenshots(regions))
        self.assertEqual(len(results), 2)
        # Each result is (cropped_image, x, y)
        self.assertEqual(results[0][1], 0)
        self.assertEqual(results[0][2], 0)
        self.assertEqual(results[1][1], 10)
        self.assertEqual(results[1][2], 10)

    def test_stop_flag_halts_iteration(self):
        fake_img = Image.new("RGB", (50, 50))
        self.host.capture_screenshot = mock.Mock(return_value=(fake_img, 0, 0))
        # Stop after first region
        call_count = [0]
        def _stop():
            call_count[0] += 1
            return call_count[0] > 1
        self.host.check_stop_flag = _stop
        # Far-apart regions so no batching
        regions = [(0, 0, 10, 10), (5000, 5000, 10, 10)]
        results = list(self.host.iter_search_screenshots(regions))
        # First region: check_stop_flag returns False (count=1), yields
        # Second region: check_stop_flag returns True (count=2), stops
        self.assertEqual(len(results), 1)

    def test_batch_failure_falls_back_to_individual(self):
        # Force batch path to fail by making capture raise on bbox
        regions = [(0, 0, 100, 100), (10, 10, 100, 100)]
        fake_img = Image.new("RGB", (50, 50))
        call_args = []

        def _capture(region=None):
            call_args.append(region)
            if region == (0, 0, 110, 110):
                raise OSError("capture failed")
            return (fake_img, region[0] if region else 0, region[1] if region else 0)

        self.host.capture_screenshot = _capture
        results = list(self.host.iter_search_screenshots(regions))
        # After batch failure, falls back to individual captures
        self.assertEqual(len(results), 2)

    def test_per_region_capture_failure_after_first_yield_propagates(self):
        # FR-SCOPE002-002 前置验证（结论：设计取舍，不改码）：逐区域路径下第二个
        # 区域的截图失败必须**如实上抛**给消费方，而不是静默结束——静默结束会让
        # 调用方把「截图失败」当成「目标不存在」。
        fake_img = Image.new("RGB", (10, 10))
        regions = [(0, 0, 10, 10), (5000, 5000, 10, 10), (6000, 6000, 10, 10)]

        def _capture(region=None):
            if region == (5000, 5000, 10, 10):
                raise OSError("capture failed")
            return fake_img, region[0], region[1]

        self.host.capture_screenshot = _capture
        generator = self.host.iter_search_screenshots(regions)
        first = next(generator)
        self.assertEqual((first[1], first[2]), (0, 0))
        with self.assertRaises(OSError):
            next(generator)

    def test_batch_crop_failure_after_first_yield_does_not_fall_back(self):
        # INVARIANT（shared.py 的 SAFETY/FR-003）：批处理分支一旦已产出过区域，
        # 就不得再回退到逐区域循环，否则同一区域会被产出两次、消费方重复识别。
        regions = [(0, 0, 100, 100), (10, 10, 100, 100)]

        class _FlakyImage:
            def __init__(self):
                self.crops = 0

            def crop(self, box):
                self.crops += 1
                if self.crops > 1:
                    raise OSError("crop failed")
                return Image.new("RGB", (100, 100))

        self.host.capture_screenshot = mock.Mock(return_value=(_FlakyImage(), 0, 0))
        generator = self.host.iter_search_screenshots(regions)
        next(generator)
        with self.assertRaises(OSError):
            next(generator)
        # 未回退：只发生了一次合并截图（回退会再逐区域截图两次）。
        self.host.capture_screenshot.assert_called_once_with((0, 0, 110, 110))


# ===========================================================================
# capture_screenshot pyautogui fallback path (mocked)
# ===========================================================================

class TestCaptureScreenshotPyautogui(unittest.TestCase):
    """Cover the pyautogui fallback path (lines 262-275)."""

    def setUp(self):
        self.host = _MixinHost()
        self.host.use_fast_screenshot = False
        self.host.use_native_core = False

    @mock.patch("fukua_rpa.vision.shared.pyautogui")
    def test_full_screen_capture(self, mock_pyautogui):
        fake_img = Image.new("RGB", (1920, 1080))
        mock_pyautogui.screenshot.return_value = fake_img
        img, ox, oy = self.host.capture_screenshot(None)
        self.assertEqual(img.size, (1920, 1080))
        self.assertEqual(ox, 0)
        self.assertEqual(oy, 0)
        mock_pyautogui.screenshot.assert_called_once_with(region=None)

    @mock.patch("fukua_rpa.vision.shared.pyautogui")
    def test_region_capture_offset(self, mock_pyautogui):
        fake_img = Image.new("RGB", (100, 100))
        mock_pyautogui.screenshot.return_value = fake_img
        img, ox, oy = self.host.capture_screenshot((50, 60, 100, 100))
        self.assertEqual(ox, 50)
        self.assertEqual(oy, 60)
        mock_pyautogui.screenshot.assert_called_once_with(region=(50, 60, 100, 100))


# ===========================================================================
# screen_bounds (mocked)
# ===========================================================================

class TestScreenBounds(unittest.TestCase):
    """Cover lines 285-313."""

    def setUp(self):
        self.host = _MixinHost()

    @mock.patch("fukua_rpa.vision.shared.HAS_MSS", True)
    def test_mss_path(self):
        fake_mss = mock.Mock()
        fake_mss.monitors = [{"left": -100, "top": 0, "width": 2020, "height": 1080}]
        self.host._mss_instance = fake_mss
        result = self.host.screen_bounds()
        self.assertEqual(result, (-100, 0, 2020, 1080))

    @mock.patch("fukua_rpa.vision.shared.HAS_MSS", True)
    def test_mss_failure_falls_back_to_win32(self):
        fake_mss = mock.Mock()
        type(fake_mss).monitors = mock.PropertyMock(side_effect=Exception("mss error"))
        self.host._mss_instance = fake_mss
        with mock.patch("fukua_rpa.vision.shared.ctypes") as mock_ctypes:
            user32 = mock_ctypes.windll.user32
            user32.GetSystemMetrics.side_effect = lambda idx: {76: 0, 77: 0, 78: 1920, 79: 1080}[idx]
            result = self.host.screen_bounds()
        self.assertEqual(result, (0, 0, 1920, 1080))

    @mock.patch("fukua_rpa.vision.shared.HAS_MSS", False)
    @mock.patch("fukua_rpa.vision.shared.pyautogui")
    def test_no_mss_win32_fallback_to_pyautogui(self, mock_pyautogui):
        mock_pyautogui.size.return_value = (2560, 1440)
        with mock.patch("fukua_rpa.vision.shared.ctypes") as mock_ctypes:
            user32 = mock_ctypes.windll.user32
            user32.GetSystemMetrics.side_effect = Exception("no win32")
            result = self.host.screen_bounds()
        self.assertEqual(result, (0, 0, 2560, 1440))


# ===========================================================================
# create_mss_instance (line 89-90)
# ===========================================================================

class TestCreateMssInstance(unittest.TestCase):
    """Cover lines 89-90."""

    def setUp(self):
        self.host = _MixinHost()

    @mock.patch("fukua_rpa.vision.shared.mss")
    def test_uses_MSS_attr(self, mock_mss_mod):
        factory = mock.Mock()
        mock_mss_mod.MSS = factory
        self.host.create_mss_instance()
        factory.assert_called_once()

    @mock.patch("fukua_rpa.vision.shared.mss")
    def test_uses_mss_func_fallback(self, mock_mss_mod):
        mock_mss_mod.MSS = None
        fallback = mock.Mock()
        mock_mss_mod.mss = fallback
        self.host.create_mss_instance()
        fallback.assert_called_once()


# ===========================================================================
# FR-003: 批量截图裁剪框必须与截图偏移处于同一坐标空间
# ===========================================================================

# 本机真实几何（10_返修复验_02 §12.1）：副屏物理 (-2560,0,2560,1600)、缩放 1.25，
# 即"副屏缩放 ≠ 系统 DPI"的回退态前提。
_FR003_MON0_PHYS = (0, 0, 2560, 1600)
_FR003_MON1_PHYS = (-2560, 0, 2560, 1600)
_FR003_MON1_SCALE = 1.25
_FR003_MON1_VIRT = (
    int(_FR003_MON1_PHYS[0] / _FR003_MON1_SCALE),
    int(_FR003_MON1_PHYS[1] / _FR003_MON1_SCALE),
    int(_FR003_MON1_PHYS[2] / _FR003_MON1_SCALE),
    int(_FR003_MON1_PHYS[3] / _FR003_MON1_SCALE),
)
# 请求区域（输入空间）：副屏上两个相邻区域
_FR003_REGIONS = [(-2048, 100, 200, 200), (-1824, 100, 200, 200)]


def _fr003_physical_map():
    return {
        "monitors": [
            {
                "physical": _FR003_MON0_PHYS,
                "virtual": (0, 0, 2560, 1600),
                "scale_x": 1.0,
                "scale_y": 1.0,
                "scale": 1.0,
            },
            {
                "physical": _FR003_MON1_PHYS,
                "virtual": _FR003_MON1_VIRT,
                "scale_x": _FR003_MON1_SCALE,
                "scale_y": _FR003_MON1_SCALE,
                "scale": _FR003_MON1_SCALE,
            },
        ],
        "vscreen": (_FR003_MON1_VIRT[0], 0, _FR003_MON1_VIRT[2], _FR003_MON1_VIRT[3]),
    }


class TestBatchRegionCropSpace(unittest.TestCase):
    """FR-003：System-DPI 回退 + 副屏缩放 ≠ 系统 DPI 时，批量分支的 crop_box 必须与
    `capture_screenshot` 返回的偏移处于同一（物理）坐标空间。

    等价探针：桩住截图使其返回**物理偏移**且 provenance=True（原生后端在回退态下的
    真实行为），其余（区域装配、should_batch_regions、crop_box 计算、yield）走真实实现。
    实机 RM-001 因本环境无法构造 System-DPI 回退态，保留为发布前门禁。
    """

    def setUp(self):
        import fukua_rpa.coordinates as coordinates

        self._coordinates = coordinates
        self._saved_map = coordinates._get_physical_input_map
        self._saved_state = coordinates._dpi_awareness_state
        # 线程本地属性可能尚未创建（取决于用例执行顺序），必须用 getattr 取值。
        self._saved_provenance = getattr(_CAPTURE_PROVENANCE, "physical", False)
        coordinates._get_physical_input_map = _fr003_physical_map
        coordinates._dpi_awareness_state = False
        self.addCleanup(self._restore_coordinates)

    def _restore_coordinates(self):
        self._coordinates._get_physical_input_map = self._saved_map
        self._coordinates._dpi_awareness_state = self._saved_state
        _CAPTURE_PROVENANCE.physical = self._saved_provenance

    def _host(self):
        """构造探针宿主：图像尺寸取**物理 bbox 的实际大小**（与原生后端一致）。"""

        host = _MixinHost()
        host.scan_regions = list(_FR003_REGIONS)
        bbox = host.region_bounding_rect(_FR003_REGIONS)
        physical_bbox = self._coordinates.input_rect_to_physical_regions(bbox)[0]
        offset = (physical_bbox[0], physical_bbox[1])
        size = (physical_bbox[2], physical_bbox[3])
        image = Image.new("RGB", size, (0, 0, 0))

        def capture(region=None):
            _CAPTURE_PROVENANCE.physical = True
            return image, offset[0], offset[1]

        host.capture_screenshot = capture
        return host, offset, size, image

    def _expected_boxes(self, offset):
        boxes = []
        for region in _FR003_REGIONS:
            px, py, pw, ph = self._coordinates.input_rect_to_physical_regions(region)[0]
            boxes.append(
                (px - offset[0], py - offset[1], px - offset[0] + pw, py - offset[1] + ph)
            )
        return boxes

    def _legacy_boxes(self, offset):
        """修复前的实现：直接用输入空间坐标减物理偏移。"""

        return [
            (
                region[0] - offset[0],
                region[1] - offset[1],
                region[0] - offset[0] + region[2],
                region[1] - offset[1] + region[3],
            )
            for region in _FR003_REGIONS
        ]

    def _run(self, host):
        real_crop = Image.Image.crop
        boxes = []

        def recording_crop(img, box, *args, **kwargs):
            boxes.append(tuple(box))
            return real_crop(img, box, *args, **kwargs)

        with mock.patch.object(Image.Image, "crop", new=recording_crop):
            yielded = list(host.iter_search_screenshots())
        return boxes, yielded

    # ① 修复后通过：裁剪窗口与请求区域（换算到物理空间）一致
    def test_crop_box_matches_requested_region_in_physical_space(self):
        host, offset, _size, _image = self._host()
        boxes, yielded = self._run(host)

        self.assertEqual(boxes, self._expected_boxes(offset))
        # FR-20260930-002: provenance 为物理像素时，yield 的偏移必须与裁剪图像同
        # 空间（物理原点），而不是输入空间区域原点；PMv2 下两者恒等。
        self.assertEqual(
            [(x, y) for _img, x, y in yielded],
            [
                self._coordinates.input_rect_to_physical_regions(region)[0][:2]
                for region in _FR003_REGIONS
            ],
        )

    # ② 越界/负坐标裁剪：修复后裁剪窗口必须完整落在截图内；旧实现必然越界
    def test_crop_box_stays_inside_the_capture(self):
        host, offset, (width, height), _image = self._host()
        boxes, _yielded = self._run(host)

        for box in boxes:
            self.assertGreaterEqual(box[0], 0)
            self.assertGreaterEqual(box[1], 0)
            self.assertLessEqual(box[2], width)
            self.assertLessEqual(box[3], height)
            self.assertGreater(box[2], box[0])
            self.assertGreater(box[3], box[1])

        legacy_out_of_bounds = [
            box
            for box in self._legacy_boxes(offset)
            if box[0] < 0 or box[1] < 0 or box[2] > width or box[3] > height
        ]
        self.assertTrue(
            legacy_out_of_bounds,
            "旧实现的裁剪窗口应越界（PIL 会静默补黑）；否则本用例失去意义",
        )

    # ③ 多屏异倍率：物理尺寸必须体现副屏缩放（不是恒等）
    def test_multi_monitor_scale_is_reflected_in_crop_geometry(self):
        host, _offset, _size, _image = self._host()
        boxes, _yielded = self._run(host)

        # 每个请求区域恰好产生一次裁剪（长度相等由 :693 的 assertEqual 同时保证）；
        # strict=True 让长度不一致时显式失败，而不是被 zip 静默截断成空转断言。
        for region, box in zip(_FR003_REGIONS, boxes, strict=True):
            self.assertLess(box[2] - box[0], region[2])
            self.assertLess(box[3] - box[1], region[3])
        self.assertEqual(
            self._coordinates.input_rect_to_physical_regions(_FR003_REGIONS[0])[0][2],
            boxes[0][2] - boxes[0][0],
        )

    # ④ 黑边防回归：裁剪窗口必须落在图像内容上，而不是越界后的补黑区域
    def test_cropped_tile_lands_on_image_content_not_black_padding(self):
        host, offset, _size, image = self._host()
        expected = self._expected_boxes(offset)
        for box in expected:
            image.putpixel((box[0], box[1]), (255, 0, 0))

        boxes, yielded = self._run(host)
        self.assertEqual(boxes, expected)
        for tile in (item[0] for item in yielded):
            self.assertEqual(
                tile.getpixel((0, 0)),
                (255, 0, 0),
                "裁剪窗口未落在请求区域上（旧实现会偏移到黑边/错误内容）",
            )

    # ⑤ FR-20260930-002 判别性用例：把 yield 偏移喂入识别几何换算，最终输入坐标
    # 必须等于请求区域中心。修复前 yield 返回输入空间原点，换算后偏离区域中心约
    # 410px（旧实现 -1538）；修复后偏差 ≤2px（-1948）。
    def test_batch_hit_coordinate_matches_region_center(self):
        host, _offset, _size, _image = self._host()
        _boxes, yielded = self._run(host)

        for region, (tile, offset_x, offset_y) in zip(
            _FR003_REGIONS, yielded, strict=True
        ):
            # 命中落在裁剪图中心：消费侧按 `res.left + offset` 取物理坐标，
            # 再经 coordinates.physical_to_input_coordinates 虚拟化
            # （等价于 match._recognition_point_to_input）。
            hit_x = tile.width // 2 + offset_x
            hit_y = tile.height // 2 + offset_y
            input_x, input_y = self._coordinates.physical_to_input_coordinates(
                hit_x, hit_y
            )
            expected_center_x = region[0] + region[2] / 2
            expected_center_y = region[1] + region[3] / 2
            self.assertLessEqual(abs(input_x - expected_center_x), 2)
            self.assertLessEqual(abs(input_y - expected_center_y), 2)


if __name__ == "__main__":
    unittest.main()
