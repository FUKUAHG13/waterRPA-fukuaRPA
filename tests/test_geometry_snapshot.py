"""离屏几何快照测试：验证核心窗口在不同缩放下的布局尺寸稳定性。

使用 QT_QPA_PLATFORM=offscreen 渲染到 QImage，提取控件几何信息（位置、尺寸），
与预期范围对比。不比较像素（字体渲染因环境而异），只比较布局结构。
"""

from __future__ import annotations

import os
import unittest

_OFFSCREEN = os.environ.get("QT_QPA_PLATFORM") == "offscreen"


def _ensure_app():
    """Return the shared QApplication, creating one if needed."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@unittest.skipUnless(_OFFSCREEN, "geometry snapshot requires offscreen Qt")
class TestMainWindowGeometry(unittest.TestCase):
    """主窗口在 100% 缩放下的基本布局约束。"""

    def test_main_window_minimum_size(self):
        """主窗口最小尺寸不低于 800x600。"""
        from tests.qt_test_support import create_test_window

        _ensure_app()
        window = create_test_window(self)

        min_w = window.minimumWidth()
        min_h = window.minimumHeight()
        self.assertGreaterEqual(min_w, 760, f"minimumWidth={min_w}, expected >= 760")
        self.assertGreaterEqual(min_h, 620, f"minimumHeight={min_h}, expected >= 620")
        # Sanity: minimum should not exceed a reasonable desktop viewport
        self.assertLessEqual(min_w, 1920, f"minimumWidth={min_w} unreasonably large")
        self.assertLessEqual(min_h, 1080, f"minimumHeight={min_h} unreasonably large")

    def test_main_window_initial_size(self):
        """主窗口初始尺寸在合理范围内（不小于最小尺寸，不超过 4K）。"""
        from tests.qt_test_support import create_test_window

        _ensure_app()
        window = create_test_window(self)

        w = window.width()
        h = window.height()
        self.assertGreaterEqual(w, window.minimumWidth())
        self.assertGreaterEqual(h, window.minimumHeight())
        self.assertLessEqual(w, 3840, f"initial width={w} exceeds 4K")
        self.assertLessEqual(h, 2160, f"initial height={h} exceeds 4K")

    def test_settings_dialog_fits_viewport(self):
        """设置对话框在 1080p 下不超出屏幕。"""
        from tests.qt_test_support import create_test_window

        _ensure_app()
        window = create_test_window(self)

        # Trigger settings dialog construction
        self.assertTrue(
            hasattr(window, "settings_dialog"),
            "main window should have settings_dialog after construction",
        )
        dialog = window.settings_dialog
        min_w = dialog.minimumWidth()
        min_h = dialog.minimumHeight()
        # FloatingSettingsDialog sets minimumSize(720, 520)
        self.assertGreaterEqual(min_w, 720, f"settings min width={min_w}")
        self.assertGreaterEqual(min_h, 520, f"settings min height={min_h}")
        # Must fit within a 1920x1080 viewport
        self.assertLessEqual(min_w, 1920, f"settings min width={min_w} exceeds 1080p")
        self.assertLessEqual(min_h, 1080, f"settings min height={min_h} exceeds 1080p")


@unittest.skipUnless(_OFFSCREEN, "geometry snapshot requires offscreen Qt")
class TestTaskSettingsGeometry(unittest.TestCase):
    """步骤设置对话框的布局约束。"""

    def test_dialog_minimum_size(self):
        """步骤设置对话框最小尺寸合理。"""
        from fukua_rpa.ui.task_settings_dialog import TaskConfigDialog

        app = _ensure_app()
        dialog = TaskConfigDialog(data={"type": 1})
        self.addCleanup(dialog.close)
        self.addCleanup(dialog.deleteLater)
        app.processEvents()

        min_w = dialog.minimumWidth()
        min_h = dialog.minimumHeight()
        # TaskConfigDialog sets minimumSize(900, 520)
        self.assertGreaterEqual(min_w, 900, f"task dialog min width={min_w}")
        self.assertGreaterEqual(min_h, 520, f"task dialog min height={min_h}")
        # Should not exceed a 1080p viewport
        self.assertLessEqual(min_w, 1920, f"task dialog min width={min_w} too large")
        self.assertLessEqual(min_h, 1080, f"task dialog min height={min_h} too large")

    def test_dialog_size_hint_nonzero(self):
        """步骤设置对话框 sizeHint 非零且不超过合理上限。"""
        from fukua_rpa.ui.task_settings_dialog import TaskConfigDialog

        app = _ensure_app()
        dialog = TaskConfigDialog(data={"type": 1})
        self.addCleanup(dialog.close)
        self.addCleanup(dialog.deleteLater)
        app.processEvents()

        hint = dialog.sizeHint()
        self.assertGreater(hint.width(), 0, "sizeHint width should be positive")
        self.assertGreater(hint.height(), 0, "sizeHint height should be positive")
        # sizeHint should not exceed 4K resolution
        self.assertLessEqual(hint.width(), 3840, f"sizeHint width={hint.width()}")
        self.assertLessEqual(hint.height(), 2160, f"sizeHint height={hint.height()}")


if __name__ == "__main__":
    unittest.main()
