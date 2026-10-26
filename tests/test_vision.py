import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image

from fukua_rpa.vision.core import (
    NativeVisionCore,
    build_scale_values,
    template_detail_status,
    template_image_detail_status,
)


def _create_test_image(width, height, color=(128, 64, 200), mode="RGB"):
    """Create a temporary image file with noise-like content for testing."""
    img = Image.new(mode, (width, height), color)
    # Add some variation so stddev checks pass
    pixels = img.load()
    for x in range(width):
        for y in range(height):
            if mode == "RGB":
                pixels[x, y] = (
                    (x * 37 + y * 13) % 256,
                    (x * 53 + y * 7) % 256,
                    (x * 11 + y * 41) % 256,
                )
            else:
                pixels[x, y] = (x * 37 + y * 13) % 256
    return img


class FindTemplateScaleBoundaryTests(unittest.TestCase):
    """find_template returns None when unavailable; handles region boundaries."""

    def test_unavailable_core_returns_none(self):
        core = NativeVisionCore.__new__(NativeVisionCore)
        core.available = False
        core.dll = None
        core.load_error = "test: no DLL"
        core.last_result_code = 0
        core._match_buffer = None
        core._match_buffer_size = 0

        result = core.find_template(
            "dummy.png", [(0, 0, 100, 100)],
            0.5, 1.5, 0.1, True, 0.8,
        )
        self.assertIsNone(result)

    def test_no_dll_returns_none(self):
        core = NativeVisionCore.__new__(NativeVisionCore)
        core.available = True
        core.dll = None
        core.load_error = ""
        core.last_result_code = 0
        core._match_buffer = None
        core._match_buffer_size = 0

        result = core.find_template(
            "dummy.png", [(0, 0, 100, 100)],
            0.5, 1.5, 0.1, True, 0.8,
        )
        self.assertIsNone(result)

    def test_invalid_regions_filtered_out(self):
        """Regions with zero or negative dimensions are discarded."""
        core = NativeVisionCore.__new__(NativeVisionCore)
        core.available = False
        core.dll = None
        core.load_error = ""
        core.last_result_code = 0
        core._match_buffer = None
        core._match_buffer_size = 0

        # All regions invalid -> still returns None (unavailable), but the
        # cleaning logic should not crash.
        result = core.find_template(
            "dummy.png",
            [(0, 0, 0, 0), (10, 10, -5, 100), (0, 0, 50, -1)],
            0.5, 1.5, 0.1, True, 0.8,
        )
        self.assertIsNone(result)

    def test_explicit_scale_only_without_preferred_returns_empty(self):
        """explicit_scale_only with no preferred scales returns [] immediately."""
        core = NativeVisionCore.__new__(NativeVisionCore)
        core.available = True
        core.dll = mock.Mock()
        core.has_explicit_scale_only = True
        core.has_preferred_scale_list = True
        core.has_extended_search = True
        core.load_error = ""
        core.last_result_code = 0
        core._match_buffer = None
        core._match_buffer_size = 0

        result = core.find_template(
            "dummy.png", [(0, 0, 800, 600)],
            0.5, 1.5, 0.1, True, 0.8,
            explicit_scale_only=True,
            preferred_scales=None,
        )
        self.assertEqual(result, [])

    def test_explicit_scale_only_unsupported_returns_none(self):
        """explicit_scale_only when DLL lacks support returns None."""
        core = NativeVisionCore.__new__(NativeVisionCore)
        core.available = True
        core.dll = mock.Mock()
        core.has_explicit_scale_only = False
        core.has_preferred_scale_list = True
        core.has_extended_search = True
        core.load_error = ""
        core.last_result_code = 0
        core._match_buffer = None
        core._match_buffer_size = 0

        result = core.find_template(
            "dummy.png", [(0, 0, 800, 600)],
            0.5, 1.5, 0.1, True, 0.8,
            explicit_scale_only=True,
            preferred_scales=[1.0],
        )
        self.assertIsNone(result)

    def test_mocked_dll_returns_match_coordinates(self):
        """Simulate a successful DLL match returning coordinates and score."""
        core = NativeVisionCore.__new__(NativeVisionCore)
        core.available = True
        core.has_explicit_scale_only = False
        core.has_preferred_scale_list = False
        core.has_extended_search = False
        core.load_error = ""
        core.last_result_code = 0
        core._match_buffer = None
        core._match_buffer_size = 0

        from fukua_rpa.vision.core import NativeMatch

        mock_dll = mock.Mock()

        def fake_find_template(
            path_ptr, rect_ptr, region_count,
            min_scale, max_scale, scale_step,
            use_gray, threshold, find_all,
            out_array, max_matches, out_count_ptr,
            error_buf, error_buf_len,
        ):
            # Simulate one match at (150, 200) scale=1.0 score=0.95
            out_array[0] = NativeMatch(
                x=150.0, y=200.0, scale=1.0, score=0.95, radius=25.0
            )
            out_count_ptr._obj.value = 1
            return 1

        mock_dll.wrpa_find_template = fake_find_template
        core.dll = mock_dll

        result = core.find_template(
            "template.png", [(0, 0, 800, 600)],
            0.8, 1.2, 0.1, True, 0.8,
        )
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        x, y, scale, score, radius = result[0]
        self.assertAlmostEqual(x, 150.0)
        self.assertAlmostEqual(y, 200.0)
        self.assertAlmostEqual(scale, 1.0)
        self.assertAlmostEqual(score, 0.95)

    def test_mocked_dll_negative_code_returns_none(self):
        """DLL returning a negative result code signals failure -> None."""
        core = NativeVisionCore.__new__(NativeVisionCore)
        core.available = True
        core.has_explicit_scale_only = False
        core.has_preferred_scale_list = False
        core.has_extended_search = False
        core.load_error = ""
        core.last_result_code = 0
        core._match_buffer = None
        core._match_buffer_size = 0

        mock_dll = mock.Mock()

        def fake_find_template(*args):
            return -1

        mock_dll.wrpa_find_template = fake_find_template
        core.dll = mock_dll

        result = core.find_template(
            "template.png", [(0, 0, 800, 600)],
            0.8, 1.2, 0.1, True, 0.8,
        )
        self.assertIsNone(result)
        self.assertEqual(core.last_result_code, -1)


class TemplateSafetyTests(unittest.TestCase):
    """Template path validation rejects empty and traversal paths."""

    def test_empty_path_rejected(self):
        valid, reason = template_detail_status("")
        self.assertFalse(valid)
        self.assertIn("无法读取", reason)

    def test_none_path_rejected(self):
        valid, reason = template_detail_status(None)
        self.assertFalse(valid)

    def test_path_traversal_rejected(self):
        """A ../ path that does not resolve to a real file is rejected."""
        traversal_path = os.path.join(
            tempfile.gettempdir(), "..", "..", "nonexistent_template.png"
        )
        valid, reason = template_detail_status(traversal_path)
        self.assertFalse(valid)
        self.assertIn("无法读取", reason)

    def test_nonexistent_file_rejected(self):
        valid, reason = template_detail_status("/no/such/file_anywhere.png")
        self.assertFalse(valid)
        self.assertIn("无法读取", reason)

    def test_directory_path_rejected(self):
        valid, reason = template_detail_status(tempfile.gettempdir())
        self.assertFalse(valid)

    def test_valid_template_accepted(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img = _create_test_image(32, 32)
            img.save(f, format="PNG")
            path = f.name
        try:
            valid, reason = template_detail_status(path)
            self.assertTrue(valid, f"Expected valid but got: {reason}")
        finally:
            os.unlink(path)


class TemplateDetailValidationTests(unittest.TestCase):
    """template_image_detail_status rejects tiny and flat templates."""

    def test_too_small_template_rejected(self):
        img = Image.new("RGB", (2, 2), (100, 100, 100))
        valid, reason = template_image_detail_status(img)
        self.assertFalse(valid)
        self.assertIn("3×3", reason)

    def test_flat_color_template_rejected(self):
        img = Image.new("RGB", (64, 64), (128, 128, 128))
        valid, reason = template_image_detail_status(img)
        self.assertFalse(valid)
        self.assertIn("纯色", reason)

    def test_fully_transparent_template_rejected(self):
        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        valid, reason = template_image_detail_status(img)
        self.assertFalse(valid)
        self.assertIn("透明", reason)

    def test_valid_textured_template_accepted(self):
        img = _create_test_image(48, 48)
        valid, reason = template_image_detail_status(img)
        self.assertTrue(valid, f"Expected valid but got: {reason}")


class ConfidenceThresholdTests(unittest.TestCase):
    """Confidence threshold is passed correctly to the native search."""

    def test_threshold_forwarded_to_dll(self):
        """The threshold parameter reaches the DLL call unchanged."""
        core = NativeVisionCore.__new__(NativeVisionCore)
        core.available = True
        core.has_explicit_scale_only = False
        core.has_preferred_scale_list = False
        core.has_extended_search = False
        core.load_error = ""
        core.last_result_code = 0
        core._match_buffer = None
        core._match_buffer_size = 0

        captured_args = {}
        mock_dll = mock.Mock()

        def fake_find_template(
            path_ptr, rect_ptr, region_count,
            min_scale, max_scale, scale_step,
            use_gray, threshold, find_all,
            out_array, max_matches, out_count_ptr,
            error_buf, error_buf_len,
        ):
            captured_args["threshold"] = threshold
            captured_args["min_scale"] = min_scale
            captured_args["max_scale"] = max_scale
            out_count_ptr._obj.value = 0
            return 0

        mock_dll.wrpa_find_template = fake_find_template
        core.dll = mock_dll

        core.find_template(
            "template.png", [(0, 0, 800, 600)],
            0.7, 1.3, 0.05, True, 0.92,
        )
        self.assertAlmostEqual(captured_args["threshold"], 0.92)
        self.assertAlmostEqual(captured_args["min_scale"], 0.7)
        self.assertAlmostEqual(captured_args["max_scale"], 1.3)

    def test_low_confidence_match_below_threshold_still_returned_by_dll(self):
        """The DLL decides filtering; Python passes results through faithfully."""
        from fukua_rpa.vision.core import NativeMatch

        core = NativeVisionCore.__new__(NativeVisionCore)
        core.available = True
        core.has_explicit_scale_only = False
        core.has_preferred_scale_list = False
        core.has_extended_search = False
        core.load_error = ""
        core.last_result_code = 0
        core._match_buffer = None
        core._match_buffer_size = 0

        mock_dll = mock.Mock()

        def fake_find_template(
            path_ptr, rect_ptr, region_count,
            min_scale, max_scale, scale_step,
            use_gray, threshold, find_all,
            out_array, max_matches, out_count_ptr,
            error_buf, error_buf_len,
        ):
            # DLL returns a match with score below threshold
            # (DLL is responsible for filtering, but may return borderline hits)
            out_array[0] = NativeMatch(
                x=50.0, y=60.0, scale=1.0, score=0.75, radius=10.0
            )
            out_count_ptr._obj.value = 1
            return 1

        mock_dll.wrpa_find_template = fake_find_template
        core.dll = mock_dll

        result = core.find_template(
            "template.png", [(0, 0, 800, 600)],
            0.8, 1.2, 0.1, True, 0.9,
        )
        # Python layer passes through DLL results faithfully
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0][3], 0.75)


class BuildScaleValuesTests(unittest.TestCase):
    """build_scale_values validates parameters and generates correct ranges."""

    def test_basic_range(self):
        values = build_scale_values(0.8, 1.2, 0.2)
        # Should include 0.8 and 1.2 but skip ~1.0
        for v in values:
            self.assertFalse(0.99 < v < 1.01)
        self.assertTrue(any(abs(v - 0.8) < 0.01 for v in values))
        self.assertTrue(any(abs(v - 1.2) < 0.01 for v in values))

    def test_invalid_min_scale_raises(self):
        with self.assertRaises(ValueError):
            build_scale_values(0, 1.5, 0.1)

    def test_max_less_than_min_raises(self):
        with self.assertRaises(ValueError):
            build_scale_values(1.5, 0.5, 0.1)

    def test_zero_step_raises(self):
        with self.assertRaises(ValueError):
            build_scale_values(0.5, 1.5, 0)

    def test_non_finite_raises(self):
        with self.assertRaises(ValueError):
            build_scale_values(float("nan"), 1.5, 0.1)

    def test_too_many_scales_raises(self):
        with self.assertRaises(ValueError):
            build_scale_values(0.01, 10.0, 0.001, max_count=10)


if __name__ == "__main__":
    unittest.main()
