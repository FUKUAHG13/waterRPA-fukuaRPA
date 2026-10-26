"""Render the project SVG into PNG and multi-resolution Windows ICO assets."""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parents[1]
SVG_PATH = ROOT / "assets" / "fukuaRPA.svg"
PNG_PATH = ROOT / "assets" / "fukuaRPA.png"
ICO_PATH = ROOT / "assets" / "fukuaRPA.ico"
CANVAS_SIZE = 1024
ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]


def main():
    app = QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(str(SVG_PATH))
    if not renderer.isValid():
        raise RuntimeError(f"Invalid SVG: {SVG_PATH}")

    image = QImage(CANVAS_SIZE, CANVAS_SIZE, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    renderer.render(painter)
    painter.end()
    if not image.save(str(PNG_PATH), "PNG"):
        raise RuntimeError(f"Could not write {PNG_PATH}")

    with Image.open(PNG_PATH) as source:
        source.convert("RGBA").save(ICO_PATH, format="ICO", sizes=ICO_SIZES)
    print(f"Wrote {PNG_PATH}")
    print(f"Wrote {ICO_PATH}")
    return app


if __name__ == "__main__":
    main()
