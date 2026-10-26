"""Verify native RGB capture dimensions and channel order on a solid window."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import contextlib  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from fukua_rpa.vision.core import NativeVisionCore  # noqa: E402


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def physical_client_rect(widget):
    user32 = ctypes.windll.user32
    hwnd = ctypes.c_void_p(int(widget.winId()))
    rect = _Rect()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise OSError("GetClientRect failed")
    origin = _Point(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise OSError("ClientToScreen failed")
    return (
        int(origin.x),
        int(origin.y),
        int(rect.right - rect.left),
        int(rect.bottom - rect.top),
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-channel-error", type=int, default=2)
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise RuntimeError("原生截图烟雾测试只支持 Windows")
    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

    expected = (17, 83, 201)
    app = QApplication.instance() or QApplication([])
    probe = QWidget()
    probe.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
    probe.setStyleSheet(
        f"background-color: rgb({expected[0]}, {expected[1]}, {expected[2]});"
    )
    probe.setGeometry(160, 160, 160, 96)
    probe.show()
    probe.raise_()
    app.processEvents()
    QTest.qWait(180)
    app.processEvents()

    x, y, width, height = physical_client_rect(probe)
    margin = 12
    region = (x + margin, y + margin, width - margin * 2, height - margin * 2)
    core = NativeVisionCore(base_dir=ROOT)
    if not core.available or not core.has_raw_rgb_capture:
        raise RuntimeError(core.load_error or "原生 RGB 截图接口不可用")
    captured = core.capture_rgb(region)
    probe.close()
    if captured is None:
        raise RuntimeError(core.last_scene_error or "原生 RGB 截图失败")
    captured_width, captured_height, pixels = captured
    if (captured_width, captured_height) != (region[2], region[3]):
        raise RuntimeError("原生 RGB 截图尺寸不一致")

    maximum_error = 0
    for index in range(0, len(pixels), 3):
        maximum_error = max(
            maximum_error,
            abs(pixels[index] - expected[0]),
            abs(pixels[index + 1] - expected[1]),
            abs(pixels[index + 2] - expected[2]),
        )
    if maximum_error > max(0, int(args.max_channel_error)):
        raise RuntimeError(
            f"原生 RGB 截图颜色/通道不一致：最大误差 {maximum_error}"
        )
    print(
        "Native RGB capture smoke passed: "
        f"{captured_width}x{captured_height}, max channel error={maximum_error}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
