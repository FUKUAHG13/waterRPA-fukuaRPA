"""Screenshot-region interaction for the per-step settings dialog."""

from PySide6.QtWidgets import QMessageBox

from ...task_model import format_region_text
from ..region_window_manager import region_window_manager


class CaptureSettingsController:
    def __init__(self, dialog):
        self.dialog = dialog

    def update_ui(self, _=None):
        host = self.dialog
        available = bool(host.screenshot_settings_available)
        custom = (
            available and host.capture_region_mode_combo.currentData() == "custom"
        )
        host.capture_region_mode_combo.setEnabled(available)
        host.capture_region_edit.setEnabled(custom)
        host.capture_region_pick_btn.setEnabled(custom)
        host.capture_region_clear_btn.setEnabled(
            custom and bool(host.capture_region_edit.text().strip())
        )

    def start_region_pick(self):
        host = self.dialog
        if not host.screenshot_settings_available:
            QMessageBox.information(host, "无法框选", "当前步骤不是截图保存步骤。")
            return
        host.capture_region_mode_combo.setCurrentIndex(
            host.capture_region_mode_combo.findData("custom")
        )
        region_window_manager.close(host, "capture_region")
        win = region_window_manager.open(
            host=host,
            key="capture_region",
            purpose="capture",
            on_selected=host.on_capture_region_selected,
        )
        host.capture_region_window = win
        win.destroyed.connect(
            lambda *_args: setattr(host, "capture_region_window", None)
        )

    def on_region_selected(self, rect):
        host = self.dialog
        host.capture_region_edit.setText(format_region_text(rect))
        host.capture_region_mode_combo.setCurrentIndex(
            host.capture_region_mode_combo.findData("custom")
        )
        self.update_ui()

    def clear_region(self):
        self.dialog.capture_region_edit.clear()
        self.update_ui()
