import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QRect
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication

from fukua_rpa.ui.image_crop_dialog import ImageCropCanvas, ImageCropDialog
from tests.qt_test_support import QtCallbackGuardMixin, dispose_widget


class ImageCropCanvasTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_canvas(self, source_width, source_height, width=600, height=400):
        canvas = ImageCropCanvas()
        self.addCleanup(dispose_widget, self.app, canvas)
        canvas.resize(width, height)
        pixmap = QPixmap(source_width, source_height)
        pixmap.fill()
        canvas.set_pixmap(pixmap)
        canvas.show()
        self.app.processEvents()
        return canvas

    def test_full_horizontal_image_maps_to_entire_source(self):
        canvas = self.make_canvas(1000, 200)
        self.assertGreater(canvas.pixmap_rect.top(), 10)

        canvas.set_selection_rect(canvas.pixmap_rect)

        self.assertEqual(canvas.source_crop_rect(), QRect(0, 0, 1000, 200))

    def test_full_vertical_image_maps_to_entire_source(self):
        canvas = self.make_canvas(200, 1000)
        self.assertGreater(canvas.pixmap_rect.left(), 10)

        canvas.set_selection_rect(canvas.pixmap_rect)

        self.assertEqual(canvas.source_crop_rect(), QRect(0, 0, 200, 1000))

    def test_half_selection_ignores_centering_letterbox(self):
        canvas = self.make_canvas(1000, 200)
        display = canvas.pixmap_rect
        canvas.set_selection_rect(
            QRect(display.left(), display.top(), display.width() // 2, display.height())
        )

        self.assertEqual(canvas.source_crop_rect(), QRect(0, 0, 500, 200))

    def test_edge_selection_is_clamped_to_source_bounds(self):
        canvas = self.make_canvas(1000, 200)
        display = canvas.pixmap_rect
        canvas.set_selection_rect(
            QRect(display.right() - 9, display.top(), 20, display.height())
        )

        source = canvas.source_crop_rect()
        self.assertEqual(source.right() + 1, 1000)
        self.assertEqual(source.y(), 0)
        self.assertEqual(source.height(), 200)

    def test_resize_recomputes_geometry_and_clears_stale_selection(self):
        canvas = self.make_canvas(1000, 200)
        old_display = canvas.pixmap_rect
        canvas.set_selection_rect(old_display)

        canvas.resize(800, 500)
        self.app.processEvents()

        self.assertNotEqual(canvas.pixmap_rect, old_display)
        self.assertTrue(canvas.selection_rect.isNull())
        canvas.set_selection_rect(canvas.pixmap_rect)
        self.assertEqual(canvas.source_crop_rect(), QRect(0, 0, 1000, 200))

    def test_dialog_crops_the_selected_source_region(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "sample.png"
            Image.new("RGB", (100, 50), (20, 40, 60)).save(path)
            dialog = ImageCropDialog(str(path))
            self.addCleanup(dispose_widget, self.app, dialog)
            dialog.resize(700, 560)
            dialog.show()
            self.app.processEvents()
            display = dialog._canvas.pixmap_rect
            dialog._canvas.set_selection_rect(
                QRect(
                    display.left() + display.width() // 2,
                    display.top(),
                    display.width() - display.width() // 2,
                    display.height(),
                )
            )

            dialog._do_crop()

            with Image.open(path) as cropped:
                self.assertEqual(cropped.size, (50, 50))
