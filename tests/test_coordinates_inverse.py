"""Tests for the input<->physical coordinate inverse helpers (P1-2 fix).

The inverse helpers only do real work under the System DPI Aware fallback
path; under Per-Monitor V2 they are identity. These tests force the fallback
path with a mocked monitor map so the inverse math itself is exercised
without needing a real multi-DPI machine.
"""

import unittest
from unittest import mock

from fukua_rpa import coordinates

# Two monitors exercise the negative-coordinate boundary that used to treat a
# virtual (left, top, right, bottom) tuple as (left, top, width, height) and
# accidentally select the primary display for points on the secondary display.
_FAKE_MAP = {
    "vscreen": (-2880, 0, 2560, 1620),
    "monitors": [
        {
            "physical": (0, 0, 2560, 1600),
            "virtual": (0, 0, 2560, 1600),
            "scale_x": 1.0,
            "scale_y": 1.0,
        },
        {
            "physical": (-1920, 0, 1920, 1080),
            "virtual": (-2880, 0, 2880, 1620),
            "scale_x": 1.5,
            "scale_y": 1.5,
        },
    ],
}


def _patch_fallback(test_case):
    """Force the System DPI Aware fallback and inject the fake monitor map."""
    return mock.patch.multiple(
        coordinates,
        _ensure_physical_dpi_awareness=lambda: False,
        _get_physical_input_map=lambda: _FAKE_MAP,
    )


class TestInputToPhysicalInverse(unittest.TestCase):
    def test_point_round_trip(self):
        with _patch_fallback(self):
            phys_x, phys_y = coordinates.input_to_physical_coordinates(-1440, 810)
            self.assertEqual((phys_x, phys_y), (-960, 540))

    def test_inverse_of_forward(self):
        with _patch_fallback(self):
            px, py = -960, 540
            ix, iy = coordinates.physical_to_input_coordinates(px, py)
            self.assertEqual((ix, iy), (-1440, 810))
            back_x, back_y = coordinates.input_to_physical_coordinates(ix, iy)
            self.assertEqual((back_x, back_y), (px, py))

    def test_rect_round_trip(self):
        with _patch_fallback(self):
            rect = coordinates.input_to_physical_rect((-1800, 300, 600, 300))
            self.assertEqual(rect, (-1200, 200, 400, 200))

    def test_cross_monitor_rect_is_split_before_native_capture(self):
        with _patch_fallback(self):
            regions = coordinates.input_rect_to_physical_regions(
                (-300, 300, 600, 300)
            )
        self.assertEqual(
            regions,
            [
                (0, 300, 300, 300),
                (-200, 200, 200, 200),
            ],
        )

    def test_physical_rect_converts_to_input_space(self):
        with _patch_fallback(self):
            converted = coordinates.physical_to_input_rect(
                (-1200, 200, 400, 200)
            )
        self.assertEqual(converted, (-1800, 300, 600, 300))

    def test_system_dpi_fullscreen_native_search_uses_per_monitor_regions(self):
        with _patch_fallback(self):
            regions = coordinates.system_dpi_physical_monitor_regions()
        self.assertEqual(
            regions,
            [(0, 0, 2560, 1600), (-1920, 0, 1920, 1080)],
        )

    def test_rect_invalid_returns_none(self):
        with _patch_fallback(self):
            self.assertIsNone(coordinates.input_to_physical_rect((0, 0, 0, 0)))
            self.assertIsNone(coordinates.input_to_physical_rect(None))

    def test_identity_when_pmv2_active(self):
        # PMv2 active => _ensure_physical_dpi_awareness returns non-False => identity.
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness", lambda: True):
            self.assertEqual(
                coordinates.input_to_physical_coordinates(123, 456), (123, 456)
            )
            self.assertEqual(
                coordinates.input_to_physical_rect((10, 20, 30, 40)), (10, 20, 30, 40)
            )


if __name__ == "__main__":
    unittest.main()
