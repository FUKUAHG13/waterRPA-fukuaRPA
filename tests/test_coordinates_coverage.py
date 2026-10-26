"""Coverage tests for fukua_rpa/coordinates.py uncovered paths.

Targets: _ensure_physical_dpi_awareness, _decode_display_mode,
_physical_mode_for_device, _virtual_screen_rect, _build_physical_input_map,
_get_physical_input_map, physical_to_input_coordinates fallback paths,
input_to_physical_coordinates fallback paths, input_rect_to_physical_regions
edge cases, physical_to_input_rect, system_dpi_physical_monitor_regions,
and the ScreenCoordinateMapper class.
"""

import ctypes
import struct
import unittest
from unittest import mock

from PySide6.QtCore import QPoint, QRect

from fukua_rpa import coordinates
from fukua_rpa.coordinates import (
    _DEVMODE_OFF_DMFIELDS,
    _DEVMODE_OFF_PELSHEIGHT,
    _DEVMODE_OFF_PELSWIDTH,
    _DEVMODE_OFF_POSITION_X,
    _DEVMODE_OFF_POSITION_Y,
    _DEVMODE_SIZE,
    _DM_PELSHEIGHT,
    _DM_PELSWIDTH,
    _DM_POSITION,
)


def _reset_module_state():
    """Reset module-level caches so tests are independent."""
    coordinates._dpi_awareness_state = None
    coordinates._physical_input_map_cache = None


def _make_devmode(width, height, left=0, top=0, include_position=True,
                  include_pels=True):
    """Build a fake DEVMODEW byte buffer with the given display mode."""
    buf = bytearray(_DEVMODE_SIZE)
    fields = 0
    if include_pels:
        fields |= _DM_PELSWIDTH | _DM_PELSHEIGHT
    if include_position:
        fields |= _DM_POSITION
    struct.pack_into("<I", buf, _DEVMODE_OFF_DMFIELDS, fields)
    struct.pack_into("<I", buf, _DEVMODE_OFF_PELSWIDTH, width)
    struct.pack_into("<I", buf, _DEVMODE_OFF_PELSHEIGHT, height)
    struct.pack_into("<i", buf, _DEVMODE_OFF_POSITION_X, left)
    struct.pack_into("<i", buf, _DEVMODE_OFF_POSITION_Y, top)
    return bytes(buf)


class _FakeRect:
    """Mimics the RECT ctypes structure fields."""

    def __init__(self, left, top, right, bottom):
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class _FakeMonitorInfo:
    """Mimics MONITORINFOEXW with rcMonitor and szDevice."""

    def __init__(self, device_name, left, top, right, bottom):
        self.cbSize = 0
        self.rcMonitor = _FakeRect(left, top, right, bottom)
        self.rcWork = _FakeRect(left, top, right, bottom)
        self.dwFlags = 0
        self.szDevice = device_name


class _FakeScreen:
    """Mimics a QScreen for ScreenCoordinateMapper tests."""

    def __init__(self, name, x, y, w, h, dpr=1.0):
        self._name = name
        self._geometry = QRect(x, y, w, h)
        self._dpr = dpr

    def name(self):
        return self._name

    def geometry(self):
        return self._geometry

    def devicePixelRatio(self):
        return self._dpr

    def virtualGeometry(self):
        return self._geometry


# ---------------------------------------------------------------------------
# _ensure_physical_dpi_awareness
# ---------------------------------------------------------------------------


class TestEnsurePhysicalDpiAwareness(unittest.TestCase):
    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    def test_cached_true_returns_immediately(self):
        coordinates._dpi_awareness_state = True
        self.assertTrue(coordinates._ensure_physical_dpi_awareness())

    def test_cached_false_returns_immediately(self):
        coordinates._dpi_awareness_state = False
        self.assertFalse(coordinates._ensure_physical_dpi_awareness())

    def test_already_pmv2_before_call(self):
        """If current_dpi_awareness already returns PMv2, result is True."""
        with mock.patch.object(
            coordinates, "current_dpi_awareness", return_value=2
        ):
            result = coordinates._ensure_physical_dpi_awareness()
        self.assertTrue(result)
        self.assertTrue(coordinates._dpi_awareness_state)

    def test_enable_succeeds(self):
        """enable_per_monitor_v2 returns True -> state is True."""
        call_count = [0]

        def fake_current():
            call_count[0] += 1
            # First call (before): not PMv2; second call (after): PMv2
            return 1 if call_count[0] == 1 else 2

        with mock.patch.object(coordinates, "current_dpi_awareness", side_effect=fake_current), \
             mock.patch.object(coordinates, "enable_per_monitor_v2", return_value=True):
            result = coordinates._ensure_physical_dpi_awareness()
        self.assertTrue(result)

    def test_enable_fails_and_after_not_pmv2(self):
        """enable fails and actual_after != PMv2 -> state is False."""
        with mock.patch.object(coordinates, "current_dpi_awareness", return_value=1), \
             mock.patch.object(coordinates, "enable_per_monitor_v2", return_value=False):
            result = coordinates._ensure_physical_dpi_awareness()
        self.assertFalse(result)
        self.assertFalse(coordinates._dpi_awareness_state)

    def test_enable_fails_but_after_is_pmv2(self):
        """enable returns False but actual_after == PMv2 -> state is True."""
        call_count = [0]

        def fake_current():
            call_count[0] += 1
            return 1 if call_count[0] == 1 else 2

        with mock.patch.object(coordinates, "current_dpi_awareness", side_effect=fake_current), \
             mock.patch.object(coordinates, "enable_per_monitor_v2", return_value=False):
            result = coordinates._ensure_physical_dpi_awareness()
        self.assertTrue(result)

    def test_exception_sets_false(self):
        """Exception during awareness check -> state is False."""
        with mock.patch.object(
            coordinates, "current_dpi_awareness", side_effect=OSError("locked")
        ):
            result = coordinates._ensure_physical_dpi_awareness()
        self.assertFalse(result)
        self.assertFalse(coordinates._dpi_awareness_state)


# ---------------------------------------------------------------------------
# _decode_display_mode
# ---------------------------------------------------------------------------


class TestDecodeDisplayMode(unittest.TestCase):
    def test_none_input(self):
        self.assertIsNone(coordinates._decode_display_mode(None))

    def test_too_short_buffer(self):
        self.assertIsNone(coordinates._decode_display_mode(b"\x00" * 10))

    def test_missing_pels_flags(self):
        buf = _make_devmode(1920, 1080, include_pels=False)
        self.assertIsNone(coordinates._decode_display_mode(buf))

    def test_zero_width(self):
        buf = _make_devmode(0, 1080)
        self.assertIsNone(coordinates._decode_display_mode(buf))

    def test_zero_height(self):
        buf = _make_devmode(1920, 0)
        self.assertIsNone(coordinates._decode_display_mode(buf))

    def test_no_position_flag_defaults_to_origin(self):
        buf = _make_devmode(1920, 1080, left=100, top=200,
                            include_position=False)
        result = coordinates._decode_display_mode(buf)
        self.assertEqual(result, (0, 0, 1920, 1080))

    def test_with_position(self):
        buf = _make_devmode(2560, 1440, left=-1920, top=0)
        result = coordinates._decode_display_mode(buf)
        self.assertEqual(result, (-1920, 0, 2560, 1440))


# ---------------------------------------------------------------------------
# _physical_mode_for_device
# ---------------------------------------------------------------------------


class TestPhysicalModeForDevice(unittest.TestCase):
    def test_success(self):
        """EnumDisplaySettingsExW succeeds and fills the buffer."""
        def fake_enum(device, mode_flag, buf_ptr, flags):
            # Write a valid devmode into the buffer pointer
            devmode = _make_devmode(1920, 1080, left=0, top=0)
            ctypes.memmove(buf_ptr, devmode, _DEVMODE_SIZE)
            return 1

        with mock.patch.object(coordinates.user32, "EnumDisplaySettingsExW",
                               side_effect=fake_enum):
            result = coordinates._physical_mode_for_device("\\\\.\\DISPLAY1")
        self.assertEqual(result, (0, 0, 1920, 1080))

    def test_api_returns_zero(self):
        with mock.patch.object(coordinates.user32, "EnumDisplaySettingsExW",
                               return_value=0):
            result = coordinates._physical_mode_for_device("\\\\.\\DISPLAY1")
        self.assertIsNone(result)

    def test_exception_returns_none(self):
        with mock.patch.object(coordinates.user32, "EnumDisplaySettingsExW",
                               side_effect=OSError("fail")):
            result = coordinates._physical_mode_for_device("\\\\.\\DISPLAY1")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _virtual_screen_rect
# ---------------------------------------------------------------------------


class TestVirtualScreenRect(unittest.TestCase):
    def test_success(self):
        metrics = {76: -1920, 77: 0, 78: 4480, 79: 1440}
        with mock.patch.object(coordinates.user32, "GetSystemMetrics",
                               side_effect=lambda idx: metrics[idx]):
            result = coordinates._virtual_screen_rect()
        self.assertEqual(result, (-1920, 0, 2560, 1440))

    def test_zero_size_returns_none(self):
        metrics = {76: 0, 77: 0, 78: 0, 79: 0}
        with mock.patch.object(coordinates.user32, "GetSystemMetrics",
                               side_effect=lambda idx: metrics[idx]):
            result = coordinates._virtual_screen_rect()
        self.assertIsNone(result)

    def test_exception_returns_none(self):
        with mock.patch.object(coordinates.user32, "GetSystemMetrics",
                               side_effect=OSError("fail")):
            result = coordinates._virtual_screen_rect()
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _build_physical_input_map
# ---------------------------------------------------------------------------


class TestBuildPhysicalInputMap(unittest.TestCase):
    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    def _patch_enum_monitors(self, monitor_infos):
        """Patch EnumDisplayMonitors to invoke callback with fake monitors."""
        def fake_enum(hdc, rect_ptr, callback, data):
            for i, _info in enumerate(monitor_infos):
                # The callback creates its own MONITORINFOEXW and calls
                # GetMonitorInfoW, so we patch that separately.
                callback(i + 1, None, None, 0)
            return 1
        return fake_enum

    def test_successful_build(self):
        """Two monitors build a valid map."""
        infos = [
            _FakeMonitorInfo("\\\\.\\DISPLAY1", 0, 0, 1920, 1080),
            _FakeMonitorInfo("\\\\.\\DISPLAY2", 1920, 0, 3840, 1080),
        ]
        physical_modes = {
            "\\\\.\\DISPLAY1": (0, 0, 1920, 1080),
            "\\\\.\\DISPLAY2": (1920, 0, 1920, 1080),
        }

        def fake_enum(hdc, rect_ptr, callback, data):
            for i in range(len(infos)):
                callback(i + 1, None, None, 0)
            return 1

        def fake_get_info(hmonitor, byref_info):
            # Fill the MONITORINFOEXW structure that was passed by reference
            info = byref_info._obj
            idx = hmonitor - 1
            src = infos[idx]
            info.rcMonitor.left = src.rcMonitor.left
            info.rcMonitor.top = src.rcMonitor.top
            info.rcMonitor.right = src.rcMonitor.right
            info.rcMonitor.bottom = src.rcMonitor.bottom
            info.szDevice = src.szDevice
            return 1

        with mock.patch.object(coordinates.user32, "EnumDisplayMonitors",
                               side_effect=fake_enum), \
             mock.patch.object(coordinates.user32, "GetMonitorInfoW",
                               side_effect=fake_get_info), \
             mock.patch.object(coordinates, "_physical_mode_for_device",
                               side_effect=lambda name: physical_modes.get(name)), \
             mock.patch.object(coordinates, "_virtual_screen_rect",
                               return_value=(0, 0, 3840, 1080)):
            result = coordinates._build_physical_input_map()

        self.assertIsNotNone(result)
        self.assertEqual(len(result["monitors"]), 2)
        self.assertEqual(result["vscreen"], (0, 0, 3840, 1080))
        # First monitor: scale 1.0
        self.assertAlmostEqual(result["monitors"][0]["scale_x"], 1.0)
        # Second monitor: virtual width 1920, physical width 1920 -> 1.0
        self.assertAlmostEqual(result["monitors"][1]["scale_x"], 1.0)

    def test_physical_mode_none_aborts(self):
        """If physical mode is None for any monitor, map is None."""
        def fake_enum(hdc, rect_ptr, callback, data):
            callback(1, None, None, 0)
            return 1

        def fake_get_info(hmonitor, byref_info):
            info = byref_info._obj
            info.rcMonitor.left = 0
            info.rcMonitor.top = 0
            info.rcMonitor.right = 1920
            info.rcMonitor.bottom = 1080
            info.szDevice = "\\\\.\\DISPLAY1"
            return 1

        with mock.patch.object(coordinates.user32, "EnumDisplayMonitors",
                               side_effect=fake_enum), \
             mock.patch.object(coordinates.user32, "GetMonitorInfoW",
                               side_effect=fake_get_info), \
             mock.patch.object(coordinates, "_physical_mode_for_device",
                               return_value=None):
            result = coordinates._build_physical_input_map()
        self.assertIsNone(result)

    def test_enum_exception_returns_none(self):
        with mock.patch.object(coordinates.user32, "EnumDisplayMonitors",
                               side_effect=OSError("enum fail")):
            result = coordinates._build_physical_input_map()
        self.assertIsNone(result)

    def test_zero_dimension_monitor_aborts(self):
        """Monitor with zero virtual dimension aborts build (lines 254-256)."""
        def fake_enum(hdc, rect_ptr, callback, data):
            callback(1, None, None, 0)
            return 1

        def fake_get_info(hmonitor, byref_info):
            info = byref_info._obj
            # Zero-width virtual rect: left == right
            info.rcMonitor.left = 0
            info.rcMonitor.top = 0
            info.rcMonitor.right = 0
            info.rcMonitor.bottom = 1080
            info.szDevice = "\\\\.\\DISPLAY1"
            return 1

        with mock.patch.object(coordinates.user32, "EnumDisplayMonitors",
                               side_effect=fake_enum), \
             mock.patch.object(coordinates.user32, "GetMonitorInfoW",
                               side_effect=fake_get_info), \
             mock.patch.object(coordinates, "_physical_mode_for_device",
                               return_value=(0, 0, 1920, 1080)):
            result = coordinates._build_physical_input_map()
        self.assertIsNone(result)

    def test_get_monitor_info_fails_skips_monitor(self):
        """GetMonitorInfoW returning 0 means the monitor is skipped."""
        def fake_enum(hdc, rect_ptr, callback, data):
            callback(1, None, None, 0)
            return 1

        with mock.patch.object(coordinates.user32, "EnumDisplayMonitors",
                               side_effect=fake_enum), \
             mock.patch.object(coordinates.user32, "GetMonitorInfoW",
                               return_value=0):
            result = coordinates._build_physical_input_map()
        # No monitors collected -> empty list -> returns None
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _get_physical_input_map (cache logic)
# ---------------------------------------------------------------------------


class TestGetPhysicalInputMap(unittest.TestCase):
    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    def test_cache_hit(self):
        """When vscreen matches cached, return cached without rebuild."""
        fake_map = {"monitors": [], "vscreen": (0, 0, 1920, 1080)}
        coordinates._physical_input_map_cache = fake_map
        with mock.patch.object(coordinates, "_virtual_screen_rect",
                               return_value=(0, 0, 1920, 1080)), \
             mock.patch.object(coordinates, "_build_physical_input_map") as build:
            result = coordinates._get_physical_input_map()
        self.assertIs(result, fake_map)
        build.assert_not_called()

    def test_cache_miss_rebuilds(self):
        """When vscreen differs, rebuild."""
        old_map = {"monitors": [], "vscreen": (0, 0, 1920, 1080)}
        new_map = {"monitors": [{"x": 1}], "vscreen": (0, 0, 3840, 1080)}
        coordinates._physical_input_map_cache = old_map
        with mock.patch.object(coordinates, "_virtual_screen_rect",
                               return_value=(0, 0, 3840, 1080)), \
             mock.patch.object(coordinates, "_build_physical_input_map",
                               return_value=new_map):
            result = coordinates._get_physical_input_map()
        self.assertIs(result, new_map)

    def test_rebuild_fails_returns_old_cache(self):
        """When rebuild returns None, fall back to old cache."""
        old_map = {"monitors": [], "vscreen": (0, 0, 1920, 1080)}
        coordinates._physical_input_map_cache = old_map
        with mock.patch.object(coordinates, "_virtual_screen_rect",
                               return_value=(0, 0, 3840, 1080)), \
             mock.patch.object(coordinates, "_build_physical_input_map",
                               return_value=None):
            result = coordinates._get_physical_input_map()
        self.assertIs(result, old_map)

    def test_no_cache_and_rebuild_fails(self):
        """No cache + rebuild fails -> None."""
        coordinates._physical_input_map_cache = None
        with mock.patch.object(coordinates, "_virtual_screen_rect",
                               return_value=(0, 0, 1920, 1080)), \
             mock.patch.object(coordinates, "_build_physical_input_map",
                               return_value=None):
            result = coordinates._get_physical_input_map()
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# physical_to_input_coordinates edge cases
# ---------------------------------------------------------------------------

_TWO_MONITOR_MAP = {
    "vscreen": (0, 0, 3840, 1080),
    "monitors": [
        {
            "physical": (0, 0, 2560, 1440),
            "virtual": (0, 0, 1920, 1080),
            "scale_x": 0.75,
            "scale_y": 0.75,
            "scale": 0.75,
        },
        {
            "physical": (2560, 0, 1920, 1080),
            "virtual": (1920, 0, 1920, 1080),
            "scale_x": 1.0,
            "scale_y": 1.0,
            "scale": 1.0,
        },
    ],
}


class TestPhysicalToInputEdgeCases(unittest.TestCase):
    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    def test_pmv2_identity(self):
        """PMv2 active -> identity transform (line 322)."""
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=True):
            self.assertEqual(
                coordinates.physical_to_input_coordinates(500, 300),
                (500, 300),
            )

    def test_no_mapping_identity(self):
        """No mapping available -> identity (line 326)."""
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=False), \
             mock.patch.object(coordinates, "_get_physical_input_map",
                               return_value=None):
            self.assertEqual(
                coordinates.physical_to_input_coordinates(123, 456),
                (123, 456),
            )

    def test_point_outside_all_monitors_uses_nearest(self):
        """Point outside all physical rects -> nearest monitor (lines 343-351)."""
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=False), \
             mock.patch.object(coordinates, "_get_physical_input_map",
                               return_value=_TWO_MONITOR_MAP):
            # Point far to the right of both monitors
            ix, iy = coordinates.physical_to_input_coordinates(5000, 500)
            # Nearest is monitor 2 (physical 2560..4480); distance from right edge
            # scale_x=1.0, scale_y=1.0 for monitor 2
            # input_x = 1920 + (5000 - 2560) * 1.0 = 4360
            # input_y = 0 + (500 - 0) * 1.0 = 500
            self.assertEqual((ix, iy), (4360, 500))

    def test_empty_monitors_identity(self):
        """Empty monitors list -> identity (line 349-350)."""
        empty_map = {"vscreen": (0, 0, 1920, 1080), "monitors": []}
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=False), \
             mock.patch.object(coordinates, "_get_physical_input_map",
                               return_value=empty_map):
            self.assertEqual(
                coordinates.physical_to_input_coordinates(100, 200),
                (100, 200),
            )

    def test_normal_conversion(self):
        """Point inside first monitor applies scale."""
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=False), \
             mock.patch.object(coordinates, "_get_physical_input_map",
                               return_value=_TWO_MONITOR_MAP):
            # Physical (1280, 720) on monitor 1 (scale 0.75)
            ix, iy = coordinates.physical_to_input_coordinates(1280, 720)
            # input_x = 0 + (1280 - 0) * 0.75 = 960
            # input_y = 0 + (720 - 0) * 0.75 = 540
            self.assertEqual((ix, iy), (960, 540))


# ---------------------------------------------------------------------------
# input_to_physical_coordinates edge cases
# ---------------------------------------------------------------------------


class TestInputToPhysicalEdgeCases(unittest.TestCase):
    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    def test_no_mapping_identity(self):
        """No mapping -> identity (line 384)."""
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=False), \
             mock.patch.object(coordinates, "_get_physical_input_map",
                               return_value=None):
            self.assertEqual(
                coordinates.input_to_physical_coordinates(99, 88),
                (99, 88),
            )

    def test_point_outside_all_virtual_uses_nearest(self):
        """Point outside all virtual rects -> nearest (lines 401-409)."""
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=False), \
             mock.patch.object(coordinates, "_get_physical_input_map",
                               return_value=_TWO_MONITOR_MAP):
            # Point far below both monitors (virtual height 1080)
            px, py = coordinates.input_to_physical_coordinates(960, 2000)
            # Nearest is monitor 1 (virtual 0..1920 x 0..1080)
            # phys_x = 0 + (960 - 0) / 0.75 = 1280
            # phys_y = 0 + (2000 - 0) / 0.75 = 2667 (rounded)
            self.assertEqual((px, py), (1280, 2667))

    def test_empty_monitors_identity(self):
        """Empty monitors -> identity (line 407-408)."""
        empty_map = {"vscreen": (0, 0, 1920, 1080), "monitors": []}
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=False), \
             mock.patch.object(coordinates, "_get_physical_input_map",
                               return_value=empty_map):
            self.assertEqual(
                coordinates.input_to_physical_coordinates(50, 60),
                (50, 60),
            )

    def test_zero_scale_identity(self):
        """Zero scale factor -> identity (line 416)."""
        zero_scale_map = {
            "vscreen": (0, 0, 1920, 1080),
            "monitors": [
                {
                    "physical": (0, 0, 1920, 1080),
                    "virtual": (0, 0, 1920, 1080),
                    "scale_x": 0.0,
                    "scale_y": 0.0,
                    "scale": 0.0,
                },
            ],
        }
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=False), \
             mock.patch.object(coordinates, "_get_physical_input_map",
                               return_value=zero_scale_map):
            self.assertEqual(
                coordinates.input_to_physical_coordinates(100, 200),
                (100, 200),
            )


# ---------------------------------------------------------------------------
# input_rect_to_physical_regions edge cases
# ---------------------------------------------------------------------------


class TestInputRectToPhysicalRegionsEdgeCases(unittest.TestCase):
    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    def test_no_mapping_fallback(self):
        """No mapping -> fallback through input_to_physical_coordinates (lines 452-456)."""
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=False), \
             mock.patch.object(coordinates, "_get_physical_input_map",
                               return_value=None), \
             mock.patch.object(coordinates, "input_to_physical_coordinates",
                               side_effect=lambda x, y: (x * 2, y * 2)):
            regions = coordinates.input_rect_to_physical_regions((10, 20, 100, 50))
        # first = (20, 40), last = (220, 140)
        self.assertEqual(regions, [(20, 40, 200, 100)])

    def test_zero_scale_skips_monitor(self):
        """Monitor with zero scale is skipped (line 476)."""
        zero_map = {
            "vscreen": (0, 0, 1920, 1080),
            "monitors": [
                {
                    "physical": (0, 0, 1920, 1080),
                    "virtual": (0, 0, 1920, 1080),
                    "scale_x": 0.0,
                    "scale_y": 0.0,
                    "scale": 0.0,
                },
            ],
        }
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=False), \
             mock.patch.object(coordinates, "_get_physical_input_map",
                               return_value=zero_map), \
             mock.patch.object(coordinates, "input_to_physical_coordinates",
                               side_effect=lambda x, y: (x, y)):
            regions = coordinates.input_rect_to_physical_regions((10, 10, 100, 100))
        # All monitors skipped -> fallback path (lines 488-492)
        self.assertEqual(regions, [(10, 10, 100, 100)])

    def test_no_intersection_fallback(self):
        """Rect doesn't intersect any monitor -> fallback (lines 488-492)."""
        distant_map = {
            "vscreen": (0, 0, 1920, 1080),
            "monitors": [
                {
                    "physical": (5000, 5000, 1920, 1080),
                    "virtual": (5000, 5000, 1920, 1080),
                    "scale_x": 1.0,
                    "scale_y": 1.0,
                    "scale": 1.0,
                },
            ],
        }
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=False), \
             mock.patch.object(coordinates, "_get_physical_input_map",
                               return_value=distant_map), \
             mock.patch.object(coordinates, "input_to_physical_coordinates",
                               side_effect=lambda x, y: (x + 1, y + 1)):
            regions = coordinates.input_rect_to_physical_regions((0, 0, 100, 100))
        # first=(1,1), last=(102,102) -> (1, 1, 101-1, 101-1) = (1, 1, 100, 100)
        self.assertEqual(regions, [(1, 1, 100, 100)])

    def test_invalid_rect_returns_empty(self):
        self.assertEqual(coordinates.input_rect_to_physical_regions(None), [])
        self.assertEqual(coordinates.input_rect_to_physical_regions((0, 0, 0, 0)), [])
        self.assertEqual(coordinates.input_rect_to_physical_regions((5, 5, -1, 10)), [])


# ---------------------------------------------------------------------------
# physical_to_input_rect edge cases
# ---------------------------------------------------------------------------


class TestPhysicalToInputRect(unittest.TestCase):
    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    def test_none_rect(self):
        """None rect -> None (line 499)."""
        self.assertIsNone(coordinates.physical_to_input_rect(None))

    def test_zero_dimension_rect(self):
        """Zero width/height -> None (line 502)."""
        self.assertIsNone(coordinates.physical_to_input_rect((0, 0, 0, 100)))
        self.assertIsNone(coordinates.physical_to_input_rect((0, 0, 100, 0)))

    def test_valid_rect_pmv2(self):
        """Under PMv2, identity conversion."""
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=True):
            result = coordinates.physical_to_input_rect((10, 20, 300, 400))
        self.assertEqual(result, (10, 20, 300, 400))


# ---------------------------------------------------------------------------
# system_dpi_physical_monitor_regions edge cases
# ---------------------------------------------------------------------------


class TestSystemDpiPhysicalMonitorRegions(unittest.TestCase):
    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    def test_pmv2_returns_empty(self):
        """PMv2 active -> empty list (line 514)."""
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=True):
            self.assertEqual(coordinates.system_dpi_physical_monitor_regions(), [])

    def test_no_mapping_returns_empty(self):
        """No mapping -> empty list (line 517)."""
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=False), \
             mock.patch.object(coordinates, "_get_physical_input_map",
                               return_value=None):
            self.assertEqual(coordinates.system_dpi_physical_monitor_regions(), [])

    def test_returns_physical_regions(self):
        """Valid mapping returns physical rects."""
        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=False), \
             mock.patch.object(coordinates, "_get_physical_input_map",
                               return_value=_TWO_MONITOR_MAP):
            regions = coordinates.system_dpi_physical_monitor_regions()
        self.assertEqual(regions, [(0, 0, 2560, 1440), (2560, 0, 1920, 1080)])


# ---------------------------------------------------------------------------
# ScreenCoordinateMapper
# ---------------------------------------------------------------------------


class TestScreenCoordinateMapperInit(unittest.TestCase):
    """Test ScreenCoordinateMapper.__init__ pairing logic (lines 545-600)."""

    def test_name_matched_pairing(self):
        """Screens paired by name with physical monitors."""
        screens = [
            _FakeScreen("\\\\.\\DISPLAY1", 0, 0, 1920, 1080, dpr=1.0),
            _FakeScreen("\\\\.\\DISPLAY2", 1920, 0, 1920, 1080, dpr=1.5),
        ]
        physical = [
            {"name": "\\\\.\\DISPLAY1", "left": 0, "top": 0,
             "width": 1920, "height": 1080},
            {"name": "\\\\.\\DISPLAY2", "left": 1920, "top": 0,
             "width": 2880, "height": 1620},
        ]
        mapper = coordinates.ScreenCoordinateMapper(
            screens=screens, physical_monitors=physical
        )
        self.assertEqual(len(mapper.entries), 2)
        self.assertEqual(mapper.entries[0]["physical"]["width"], 1920)
        self.assertEqual(mapper.entries[1]["physical"]["width"], 2880)

    def test_size_fallback_pairing(self):
        """When names don't match, pair by closest size (lines 577-589)."""
        screens = [
            _FakeScreen("SCREEN_A", 0, 0, 1920, 1080, dpr=1.0),
        ]
        physical = [
            {"name": "MONITOR_X", "left": 0, "top": 0,
             "width": 1920, "height": 1080},
        ]
        mapper = coordinates.ScreenCoordinateMapper(
            screens=screens, physical_monitors=physical
        )
        self.assertEqual(len(mapper.entries), 1)
        # Should have paired by size fallback
        self.assertEqual(mapper.entries[0]["physical"]["name"], "MONITOR_X")

    def test_no_physical_monitor_synthesizes(self):
        """When no physical monitor available, synthesize from screen (lines 590-599)."""
        screens = [
            _FakeScreen("\\\\.\\DISPLAY1", 0, 0, 1920, 1080, dpr=2.0),
        ]
        mapper = coordinates.ScreenCoordinateMapper(
            screens=screens, physical_monitors=[]
        )
        self.assertEqual(len(mapper.entries), 1)
        phys = mapper.entries[0]["physical"]
        # Synthesized: width = 1920 * 2.0 = 3840, height = 1080 * 2.0 = 2160
        self.assertEqual(phys["width"], 3840)
        self.assertEqual(phys["height"], 2160)
        self.assertEqual(phys["left"], 0)
        self.assertEqual(phys["top"], 0)

    def test_empty_screens_uses_primary_virtual_geometry(self):
        """No screens -> virtual_rect from primaryScreen (lines 554-560)."""
        fake_primary = _FakeScreen("PRIMARY", 0, 0, 2560, 1440)
        with mock.patch("fukua_rpa.coordinates.QApplication") as mock_app:
            mock_app.screens.return_value = []
            mock_app.primaryScreen.return_value = fake_primary
            mapper = coordinates.ScreenCoordinateMapper(
                screens=None, physical_monitors=[]
            )
        self.assertEqual(mapper.virtual_rect, QRect(0, 0, 2560, 1440))

    def test_empty_screens_no_primary(self):
        """No screens and no primary -> QRect(0,0,1,1)."""
        with mock.patch("fukua_rpa.coordinates.QApplication") as mock_app:
            mock_app.screens.return_value = []
            mock_app.primaryScreen.return_value = None
            mapper = coordinates.ScreenCoordinateMapper(
                screens=None, physical_monitors=[]
            )
        self.assertEqual(mapper.virtual_rect, QRect(0, 0, 1, 1))


class TestScreenCoordinateMapperPhysicalMonitors(unittest.TestCase):
    """Test _physical_monitors enumeration (lines 603-640)."""

    def setUp(self):
        _reset_module_state()

    def tearDown(self):
        _reset_module_state()

    def test_enumeration_with_physical_mode(self):
        """Successful enumeration uses physical mode from EnumDisplaySettingsExW."""
        screens = [_FakeScreen("\\\\.\\DISPLAY1", 0, 0, 1920, 1080)]

        def fake_enum(hdc, rect_ptr, callback, data):
            callback(1, None, None, 0)
            return 1

        def fake_get_info(hmonitor, byref_info):
            info = byref_info._obj
            info.rcMonitor.left = 0
            info.rcMonitor.top = 0
            info.rcMonitor.right = 1920
            info.rcMonitor.bottom = 1080
            info.szDevice = "\\\\.\\DISPLAY1"
            return 1

        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=True), \
             mock.patch.object(coordinates.user32, "EnumDisplayMonitors",
                               side_effect=fake_enum), \
             mock.patch.object(coordinates.user32, "GetMonitorInfoW",
                               side_effect=fake_get_info), \
             mock.patch.object(coordinates, "_physical_mode_for_device",
                               return_value=(0, 0, 2560, 1440)):
            mapper = coordinates.ScreenCoordinateMapper(screens=screens)

        self.assertEqual(len(mapper.entries), 1)
        self.assertEqual(mapper.entries[0]["physical"]["width"], 2560)
        self.assertEqual(mapper.entries[0]["physical"]["height"], 1440)

    def test_enumeration_fallback_to_rcmonitor(self):
        """When physical mode is None, use rcMonitor rect (lines 614-620)."""
        screens = [_FakeScreen("\\\\.\\DISPLAY1", 0, 0, 1920, 1080)]

        def fake_enum(hdc, rect_ptr, callback, data):
            callback(1, None, None, 0)
            return 1

        def fake_get_info(hmonitor, byref_info):
            info = byref_info._obj
            info.rcMonitor.left = 0
            info.rcMonitor.top = 0
            info.rcMonitor.right = 1920
            info.rcMonitor.bottom = 1080
            info.szDevice = "\\\\.\\DISPLAY1"
            return 1

        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=True), \
             mock.patch.object(coordinates.user32, "EnumDisplayMonitors",
                               side_effect=fake_enum), \
             mock.patch.object(coordinates.user32, "GetMonitorInfoW",
                               side_effect=fake_get_info), \
             mock.patch.object(coordinates, "_physical_mode_for_device",
                               return_value=None):
            mapper = coordinates.ScreenCoordinateMapper(screens=screens)

        self.assertEqual(mapper.entries[0]["physical"]["width"], 1920)
        self.assertEqual(mapper.entries[0]["physical"]["height"], 1080)

    def test_enum_exception_returns_empty(self):
        """Exception during enumeration -> empty monitors list (lines 637-639)."""
        screens = [_FakeScreen("\\\\.\\DISPLAY1", 0, 0, 1920, 1080)]

        with mock.patch.object(coordinates, "_ensure_physical_dpi_awareness",
                               return_value=True), \
             mock.patch.object(coordinates.user32, "EnumDisplayMonitors",
                               side_effect=OSError("fail")):
            mapper = coordinates.ScreenCoordinateMapper(screens=screens)

        # No physical monitors -> synthesized from screen geometry
        self.assertEqual(len(mapper.entries), 1)
        self.assertEqual(mapper.entries[0]["physical"]["width"], 1920)


class TestScreenCoordinateMapperConversions(unittest.TestCase):
    """Test coordinate conversion methods of ScreenCoordinateMapper."""

    def _make_mapper(self):
        """Create a mapper with two screens at different DPI."""
        screens = [
            _FakeScreen("\\\\.\\DISPLAY1", 0, 0, 1920, 1080, dpr=1.0),
            _FakeScreen("\\\\.\\DISPLAY2", 1920, 0, 1280, 720, dpr=1.5),
        ]
        physical = [
            {"name": "\\\\.\\DISPLAY1", "left": 0, "top": 0,
             "width": 1920, "height": 1080},
            {"name": "\\\\.\\DISPLAY2", "left": 1920, "top": 0,
             "width": 1920, "height": 1080},
        ]
        return coordinates.ScreenCoordinateMapper(
            screens=screens, physical_monitors=physical
        )

    def test_apply_to(self):
        """apply_to sets widget geometry to virtual_rect (line 648)."""
        mapper = self._make_mapper()

        class FakeWidget:
            def __init__(self):
                self.rect = None

            def setGeometry(self, rect):
                self.rect = rect

        widget = FakeWidget()
        mapper.apply_to(widget)
        self.assertEqual(widget.rect, QRect(0, 0, 3200, 1080))

    def test_entry_for_physical_inside(self):
        """Point inside a physical monitor returns that entry (lines 651-657)."""
        mapper = self._make_mapper()
        entry = mapper._entry_for_physical(2000, 500)
        self.assertEqual(entry["physical"]["name"], "\\\\.\\DISPLAY2")

    def test_entry_for_physical_outside_fallback(self):
        """Point outside all monitors falls back to entries[0] (lines 658-664)."""
        mapper = self._make_mapper()
        entry = mapper._entry_for_physical(9999, 9999)
        self.assertEqual(entry["physical"]["name"], "\\\\.\\DISPLAY1")

    def test_entry_for_physical_no_entries(self):
        """No entries -> None."""
        screens = []
        with mock.patch("fukua_rpa.coordinates.QApplication") as mock_app:
            mock_app.screens.return_value = []
            mock_app.primaryScreen.return_value = _FakeScreen("P", 0, 0, 100, 100)
            mapper = coordinates.ScreenCoordinateMapper(
                screens=screens, physical_monitors=[]
            )
        self.assertIsNone(mapper._entry_for_physical(50, 50))

    def test_entry_for_logical_inside(self):
        """Point inside a logical screen returns that entry (lines 667-670)."""
        mapper = self._make_mapper()
        entry = mapper._entry_for_logical(2000, 500)
        self.assertEqual(entry["logical"], QRect(1920, 0, 1280, 720))

    def test_entry_for_logical_outside_fallback(self):
        """Point outside all screens falls back to entries[0] (lines 671-677)."""
        mapper = self._make_mapper()
        entry = mapper._entry_for_logical(-500, -500)
        self.assertEqual(entry["logical"], QRect(0, 0, 1920, 1080))

    def test_physical_to_local_basic(self):
        """physical_to_local converts correctly (lines 693-703)."""
        mapper = self._make_mapper()
        # Physical point (960, 540) on DISPLAY1 (1920x1080 physical, 1920x1080 logical)
        # scale = logical.width / physical.width = 1920/1920 = 1.0
        # global_x = 0 + (960 - 0) * 1.0 = 960
        # global_y = 0 + (540 - 0) * 1.0 = 540
        # local = global - virtual_rect.x() = 960 - 0 = 960
        result = mapper.physical_to_local(960, 540)
        self.assertEqual(result, QPoint(960, 540))

    def test_physical_to_local_second_screen_scaled(self):
        """physical_to_local on second screen with different scale."""
        mapper = self._make_mapper()
        # DISPLAY2: physical (1920, 0, 1920, 1080), logical (1920, 0, 1280, 720)
        # Physical point (2880, 540) = center of DISPLAY2 physical
        # scale_x = logical.width / physical.width = 1280 / 1920 = 2/3
        # global_x = 1920 + (2880 - 1920) * (1280/1920) = 1920 + 960 * 2/3 = 1920 + 640 = 2560
        # scale_y = 720 / 1080 = 2/3
        # global_y = 0 + (540 - 0) * (720/1080) = 540 * 2/3 = 360
        # local = (2560 - 0, 360 - 0) = (2560, 360)
        result = mapper.physical_to_local(2880, 540)
        self.assertEqual(result, QPoint(2560, 360))

    def test_physical_to_local_no_entries(self):
        """No entries -> returns rounded input (line 695)."""
        with mock.patch("fukua_rpa.coordinates.QApplication") as mock_app:
            mock_app.screens.return_value = []
            mock_app.primaryScreen.return_value = _FakeScreen("P", 0, 0, 100, 100)
            mapper = coordinates.ScreenCoordinateMapper(
                screens=[], physical_monitors=[]
            )
        result = mapper.physical_to_local(42.7, 99.3)
        self.assertEqual(result, QPoint(43, 99))

    def test_local_to_physical_basic(self):
        """local_to_physical converts correctly (lines 718-731)."""
        mapper = self._make_mapper()
        # Local point (960, 540) -> global (960, 540) on DISPLAY1
        # physical_x = 0 + (960 - 0) * 1920/1920 = 960
        # physical_y = 0 + (540 - 0) * 1080/1080 = 540
        result = mapper.local_to_physical(QPoint(960, 540))
        self.assertEqual(result, (960, 540))

    def test_local_to_physical_second_screen(self):
        """local_to_physical on second screen with scaling."""
        mapper = self._make_mapper()
        # Local point (2560, 360) -> global (2560, 360) on DISPLAY2
        # DISPLAY2: logical (1920, 0, 1280, 720), physical (1920, 0, 1920, 1080)
        # physical_x = 1920 + (2560 - 1920) * 1920/1280 = 1920 + 640 * 1.5 = 2880
        # physical_y = 0 + (360 - 0) * 1080/720 = 360 * 1.5 = 540
        result = mapper.local_to_physical(QPoint(2560, 360))
        self.assertEqual(result, (2880, 540))

    def test_local_to_physical_no_entries(self):
        """No entries -> returns rounded global coords (line 722)."""
        with mock.patch("fukua_rpa.coordinates.QApplication") as mock_app:
            mock_app.screens.return_value = []
            mock_app.primaryScreen.return_value = _FakeScreen("P", 0, 0, 100, 100)
            mapper = coordinates.ScreenCoordinateMapper(
                screens=[], physical_monitors=[]
            )
        result = mapper.local_to_physical(QPoint(50, 60))
        self.assertEqual(result, (50, 60))

    def test_local_rect_to_physical(self):
        """local_rect_to_physical maps corners and normalizes (lines 745-751)."""
        mapper = self._make_mapper()
        # Rect on DISPLAY1: local (100, 100, 200, 200)
        # topLeft -> (100, 100), bottomRight -> (300, 300)
        # Both on DISPLAY1 with 1:1 scale
        rect = QRect(100, 100, 200, 200)
        result = mapper.local_rect_to_physical(rect)
        self.assertEqual(result, (100, 100, 200, 200))

    def test_local_rect_to_physical_scaled(self):
        """local_rect_to_physical on scaled screen."""
        mapper = self._make_mapper()
        # Rect entirely on DISPLAY2: local (2000, 100, 100, 100)
        # topLeft global = (2000, 100) on DISPLAY2
        # phys_x = 1920 + (2000-1920)*1920/1280 = 1920 + 80*1.5 = 2040
        # phys_y = 0 + (100-0)*1080/720 = 150
        # bottomRight global = (2100, 200)
        # phys_x = 1920 + (2100-1920)*1.5 = 1920 + 270 = 2190
        # phys_y = 0 + 200*1.5 = 300
        rect = QRect(2000, 100, 100, 100)
        result = mapper.local_rect_to_physical(rect)
        self.assertEqual(result, (2040, 150, 150, 150))


# ---------------------------------------------------------------------------
# _configure_dpi_conversion_signatures exception path
# ---------------------------------------------------------------------------


class TestConfigureDpiConversionSignatures(unittest.TestCase):
    def test_exception_logged_not_raised(self):
        """Exception in signature config is caught (lines 158-159)."""
        with mock.patch.object(coordinates, "user32") as mock_user32:
            mock_user32.EnumDisplaySettingsExW = mock.PropertyMock(
                side_effect=AttributeError("no attr")
            )
            # Make setting argtypes raise
            type(mock_user32.EnumDisplaySettingsExW).argtypes = mock.PropertyMock(
                side_effect=AttributeError("locked")
            )
            # Should not raise
            coordinates._configure_dpi_conversion_signatures()


if __name__ == "__main__":
    unittest.main()
