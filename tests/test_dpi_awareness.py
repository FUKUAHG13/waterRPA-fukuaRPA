"""Regression tests for the mixed-DPI process and packaging contract."""

from __future__ import annotations

import ctypes
import os
import struct
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from fukua_rpa import coordinates, dpi_awareness
from fukua_rpa.ui import input_tools, theme
from fukua_rpa.win32_api import MSLLHOOKSTRUCT, WM_LBUTTONDOWN

ROOT = Path(__file__).resolve().parents[1]


class TestDpiBootstrap(unittest.TestCase):
    def test_existing_per_monitor_context_is_success_not_failure(self):
        with (
            mock.patch.object(
                dpi_awareness,
                "current_dpi_awareness",
                return_value=dpi_awareness.DPI_AWARENESS_PER_MONITOR_AWARE,
            ),
            mock.patch.object(dpi_awareness, "_request_per_monitor_v2") as request,
        ):
            self.assertTrue(dpi_awareness.enable_per_monitor_v2())
            request.assert_not_called()

    def test_false_raw_request_still_accepts_context_activated_elsewhere(self):
        with (
            mock.patch.object(
                dpi_awareness,
                "current_dpi_awareness",
                side_effect=[1, dpi_awareness.DPI_AWARENESS_PER_MONITOR_AWARE],
            ),
            mock.patch.object(
                dpi_awareness, "_request_per_monitor_v2", return_value=False
            ),
        ):
            self.assertTrue(dpi_awareness.enable_per_monitor_v2())

    def test_entry_point_enables_pmv2_before_importing_qt(self):
        source = (ROOT / "fukuaRPA.py").read_text(encoding="utf-8")
        enable_at = source.index("_PER_MONITOR_DPI_READY = enable_per_monitor_v2()")
        qt_at = source.index("from PySide6.QtCore import")
        self.assertLess(enable_at, qt_at)
        self.assertNotIn("SetProcessDpiAwareness(1)", source)
        self.assertNotIn("SetProcessDPIAware()", source)

    def test_frozen_manifest_declares_per_monitor_v2(self):
        manifest = ROOT / "assets" / "fukuaRPA.manifest"
        root = ET.parse(manifest).getroot()
        values = {
            element.tag.rsplit("}", 1)[-1]: (element.text or "").strip()
            for element in root.iter()
        }
        self.assertEqual(values.get("dpiAwareness"), "PerMonitorV2,PerMonitor")
        self.assertEqual(values.get("dpiAware"), "true/pm")
        for spec_name in ("fukuaRPA_onedir.spec", "fukuaRPA_onefile.spec"):
            spec_source = (ROOT / spec_name).read_text(encoding="utf-8")
            self.assertIn('manifest="assets/fukuaRPA.manifest"', spec_source)

    def test_theme_installs_per_screen_font_refresh_without_coordinate_rescale(self):
        source = (ROOT / "fukua_rpa" / "ui" / "theme.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("handle.screenChanged.connect", source)
        self.assertIn(
            'handle.property("_fukua_screen_signal_connected")', source
        )
        self.assertNotIn(
            'widget.property("_fukua_screen_signal_connected")', source
        )
        self.assertIn("fresh_font.fromString(old_font.toString())", source)
        self.assertIn("fresh_font.setResolveMask(resolve_mask)", source)
        self.assertIn("apply_layout_density_scale(widget, density)", source)
        self.assertIn("apply_widget_geometry_scale(children, ui_scale, density)", source)
        self.assertNotIn("devicePixelRatio() *", source)
        self.assertTrue(callable(theme.install_per_screen_font_refresh))

    def test_visual_density_is_relative_to_launch_screen_and_bounded(self):
        self.assertAlmostEqual(theme.visual_density_ratio(96, 144), 2 / 3)
        self.assertEqual(theme.visual_density_ratio(144, 144), 1.0)
        self.assertEqual(theme.visual_density_ratio(96, 0), 1.0)
        self.assertEqual(theme.visual_density_ratio(float("nan"), 144), 1.0)
        self.assertEqual(theme.visual_density_ratio(96, 384), 0.5)
        self.assertEqual(theme.visual_density_ratio(384, 96), 3.0)

    def test_stylesheet_scales_geometry_without_double_scaling_fonts(self):
        stylesheet = theme.build_modern_stylesheet(
            1.0,
            "light",
            metric_scale=0.5,
        )
        self.assertIn("min-height: 14px;", stylesheet)
        self.assertIn("font-size: 12pt;", stylesheet)

    def test_layout_and_fixed_geometry_round_trip_from_base_values(self):
        application = QApplication.instance() or QApplication([])
        self.assertIsNotNone(application)
        root = QDialog()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 10, 8, 6)
        layout.setSpacing(8)
        button = QPushButton("测试", root)
        button.setFixedSize(100, 40)
        layout.addWidget(button)

        theme.apply_layout_density_scale(root, 0.5)
        theme.apply_widget_geometry_scale((button,), 1.0, 0.5)
        margins = layout.contentsMargins()
        self.assertEqual(
            (margins.left(), margins.top(), margins.right(), margins.bottom()),
            (6, 5, 4, 3),
        )
        self.assertEqual(layout.spacing(), 4)
        self.assertEqual((button.minimumWidth(), button.maximumWidth()), (50, 50))
        self.assertEqual((button.minimumHeight(), button.maximumHeight()), (20, 20))

        theme.apply_layout_density_scale(root, 0.5)
        theme.apply_widget_geometry_scale((button,), 1.0, 0.5)
        self.assertEqual(layout.spacing(), 4)
        self.assertEqual(button.minimumWidth(), 50)

        theme.apply_layout_density_scale(root, 1.0)
        theme.apply_widget_geometry_scale((button,), 1.0, 1.0)
        margins = layout.contentsMargins()
        self.assertEqual(
            (margins.left(), margins.top(), margins.right(), margins.bottom()),
            (12, 10, 8, 6),
        )
        self.assertEqual(layout.spacing(), 8)
        self.assertEqual((button.minimumWidth(), button.maximumWidth()), (100, 100))
        self.assertEqual((button.minimumHeight(), button.maximumHeight()), (40, 40))

        theme.apply_widget_geometry_scale((button,), 1.5, 2 / 3)
        self.assertEqual((button.minimumWidth(), button.maximumWidth()), (100, 100))
        self.assertEqual((button.minimumHeight(), button.maximumHeight()), (40, 40))
        theme.apply_widget_geometry_scale((button,), 1.5, 1.0)
        self.assertEqual((button.minimumWidth(), button.maximumWidth()), (150, 150))
        self.assertEqual((button.minimumHeight(), button.maximumHeight()), (60, 60))
        root.deleteLater()

    def test_layout_scaling_accepts_widget_layout_attribute_alias(self):
        application = QApplication.instance() or QApplication([])
        self.assertIsNotNone(application)
        root = QDialog()
        aliased_layout = QVBoxLayout(root)
        aliased_layout.setContentsMargins(12, 9, 6, 3)
        root.layout = aliased_layout
        theme.apply_layout_density_scale(root, 2 / 3)
        margins = aliased_layout.contentsMargins()
        self.assertEqual(
            (margins.left(), margins.top(), margins.right(), margins.bottom()),
            (8, 6, 4, 2),
        )
        root.deleteLater()

    def test_detached_base_capture_prevents_secondary_double_scaling(self):
        application = QApplication.instance() or QApplication([])
        self.assertIsNotNone(application)
        button = QPushButton("")
        button.setFixedSize(30, 30)

        # Dynamic controls are primed while detached, before a low-DPI
        # top-level stylesheet can replace their effective constraints.
        theme.apply_widget_geometry_scale((button,), 1.0, 1.0)
        button.setFixedSize(20, 20)
        theme.apply_widget_geometry_scale((button,), 1.0, 2 / 3)

        self.assertEqual((button.minimumWidth(), button.maximumWidth()), (20, 20))
        self.assertEqual((button.minimumHeight(), button.maximumHeight()), (20, 20))
        self.assertEqual(button.property("_ui_base_min_w"), 30)
        button.deleteLater()

    def test_first_show_on_secondary_captures_geometry_before_local_style(self):
        application = QApplication.instance() or QApplication([])
        self.assertIsNotNone(application)
        theme.apply_modern_theme(application, 1.0, "light")
        root = QDialog()
        button = QPushButton("", root)
        button.setProperty("iconOnly", True)
        button.setFixedSize(30, 30)
        manager = theme.install_per_screen_font_refresh(application)

        with mock.patch.object(
            theme,
            "window_visual_density",
            return_value=2 / 3,
        ):
            manager.refresh_widget(root)

        self.assertEqual(button.property("_ui_base_min_w"), 30)
        self.assertEqual(button.property("_ui_base_max_w"), 30)
        root.deleteLater()

    def test_inline_stylesheet_round_trips_without_density_accumulation(self):
        application = QApplication.instance() or QApplication([])
        self.assertIsNotNone(application)
        root = QDialog()
        label = QLabel("状态", root)
        label.setStyleSheet("font-size: 12px; padding: 14px;")
        manager = theme.install_per_screen_font_refresh(application)

        with mock.patch.object(
            theme,
            "window_visual_density",
            return_value=2 / 3,
        ):
            manager.refresh_widget(root)
            self.assertIn("font-size: 8px", label.styleSheet())
            self.assertIn("padding: 9px", label.styleSheet())
            manager.refresh_widget(root)
            self.assertIn("font-size: 8px", label.styleSheet())

        with mock.patch.object(
            theme,
            "window_visual_density",
            return_value=1.0,
        ):
            manager.refresh_widget(root)
        self.assertIn("font-size: 12px", label.styleSheet())
        self.assertIn("padding: 14px", label.styleSheet())
        root.deleteLater()

    def test_global_theme_refresh_does_not_cache_half_built_hidden_window(self):
        application = QApplication.instance() or QApplication([])
        manager = theme.install_per_screen_font_refresh(application)
        root = QDialog()
        root.setMinimumSize(320, 240)
        manager.refresh_all()
        self.assertFalse(bool(root.property("_ui_scale_base_saved")))
        root.deleteLater()


class TestPhysicalDisplayMode(unittest.TestCase):
    def test_decoder_uses_real_devmode_offsets(self):
        raw = bytearray(coordinates._DEVMODE_SIZE)
        fields = (
            coordinates._DM_POSITION
            | coordinates._DM_PELSWIDTH
            | coordinates._DM_PELSHEIGHT
        )
        struct.pack_into("<I", raw, coordinates._DEVMODE_OFF_DMFIELDS, fields)
        struct.pack_into("<i", raw, coordinates._DEVMODE_OFF_POSITION_X, -1920)
        struct.pack_into("<i", raw, coordinates._DEVMODE_OFF_POSITION_Y, 120)
        struct.pack_into("<I", raw, coordinates._DEVMODE_OFF_PELSWIDTH, 1920)
        struct.pack_into("<I", raw, coordinates._DEVMODE_OFF_PELSHEIGHT, 1080)

        self.assertEqual(
            coordinates._decode_display_mode(raw),
            (-1920, 120, 1920, 1080),
        )


class TestHookCoordinateBoundary(unittest.TestCase):
    @staticmethod
    def _mouse_message(x=-960, y=540):
        message = MSLLHOOKSTRUCT()
        message.pt.x = x
        message.pt.y = y
        return message

    def test_coordinate_picker_converts_hook_physical_point_once(self):
        thread = input_tools.CoordinatePickThread("point")
        message = self._mouse_message()
        with (
            mock.patch.object(
                input_tools,
                "physical_to_input_coordinates",
                return_value=(-1440, 810),
            ) as convert,
            mock.patch.object(
                input_tools.win32_hook_backend, "call_next", return_value=0
            ),
        ):
            thread.mouse_handler(
                0,
                WM_LBUTTONDOWN,
                ctypes.addressof(message),
            )
        self.assertEqual(thread.down_pos, (-1440, 810))
        convert.assert_called_with(-960, 540)

    def test_recorder_stores_input_space_coordinates(self):
        thread = input_tools.HookThread()
        thread.recording_event.set()
        message = self._mouse_message()
        with (
            mock.patch.object(
                input_tools,
                "physical_to_input_coordinates",
                return_value=(-1440, 810),
            ),
            mock.patch.object(
                input_tools.win32_hook_backend, "call_next", return_value=0
            ),
        ):
            thread.mouse_handler(
                0,
                WM_LBUTTONDOWN,
                ctypes.addressof(message),
            )
        self.assertEqual(thread.l_down_pos, (-1440, 810))


if __name__ == "__main__":
    unittest.main()
