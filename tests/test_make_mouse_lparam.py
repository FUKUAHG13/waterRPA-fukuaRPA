"""Tests for make_mouse_lparam coordinate saturation (P3-3 fix).

Note: this environment's ctypes build raises on ``int(c_longlong(...))``, so
the test reads ``.value`` (portable and what callers effectively rely on when
passing the LPARAM to Win32 APIs).
"""

import unittest

from fukua_rpa.win32_api import make_mouse_lparam


def _as_int(lparam):
    return int(lparam.value)


class TestMakeMouseLparamSaturation(unittest.TestCase):
    def test_normal_coordinates_unchanged(self):
        self.assertEqual(_as_int(make_mouse_lparam(100, 200)), 0xC80064)

    def test_oversize_saturated_to_signed_16bit(self):
        # x=40000, y=40000 would wrap to a negative signed coordinate via the
        # raw & 0xFFFF mask; P3-3 clamps to [-32768, 32767] first.
        val = _as_int(make_mouse_lparam(40000, 40000))
        x_part = val & 0xFFFF
        y_part = (val >> 16) & 0xFFFF
        self.assertEqual((x_part, y_part), (32767, 32767))

    def test_negative_coordinates_preserved(self):
        val = _as_int(make_mouse_lparam(-5, -5))
        x_part = val & 0xFFFF
        y_part = (val >> 16) & 0xFFFF
        # -5 as signed 16-bit two's complement.
        self.assertEqual((x_part, y_part), (0xFFFB, 0xFFFB))


if __name__ == "__main__":
    unittest.main()
