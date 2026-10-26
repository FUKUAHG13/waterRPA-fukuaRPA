# pyright: reportAttributeAccessIssue=false
"""热键录入 Mixin（God Class 拆分）。

从 ``MappingController`` 搬移的启动/停止/映射热键录入方法组，
基于 ``KeyCaptureDialog`` 完成录入并经宿主回写控件。Mixin 自身
不定义状态。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog

from ....win32_api import hotkey_display_text
from ...input_tools import KeyCaptureDialog
from ...lifecycle import schedule_widget_callback


class MappingHotkeyCaptureMixin:
    """启动/停止/映射热键录入（5 方法）。"""

    def capture_hotkey_text(self, title):
        parent = self.host.settings_interaction_parent()
        dialog = KeyCaptureDialog(parent, title)
        accepted = dialog.exec() == QDialog.Accepted
        captured = hotkey_display_text(dialog.captured_text) if accepted else ""
        if parent is getattr(self.host, "settings_dialog", None):
            self.host.restore_settings_dialog_focus()
            schedule_widget_callback(
                self.host,
                0,
                lambda owner: owner.restore_settings_dialog_focus(),
            )
        return captured

    def capture_start_hotkey(self):
        captured = self.host.capture_hotkey_text("录入启动热键")
        if captured:
            self.host.hotkey_start_edit.setText(captured)
            self.host.update_hotkeys()
            self.host.hotkey_start_edit.setFocus(Qt.OtherFocusReason)
            self.host.mark_profile_dirty()

    def capture_stop_hotkey(self):
        captured = self.host.capture_hotkey_text("录入停止热键")
        if captured:
            self.host.hotkey_stop_edit.setText(captured)
            self.host.update_hotkeys()
            self.host.hotkey_stop_edit.setFocus(Qt.OtherFocusReason)
            self.host.mark_profile_dirty()

    def capture_mapping_hotkey(self, map_idx):
        if map_idx < 0 or map_idx >= len(self.host.key_mapping_rows):
            return
        captured = self.host.capture_hotkey_text(f"录入映射{map_idx + 1}热键")
        if captured:
            row = self.host.key_mapping_rows[map_idx]
            row["hotkey"].setText(captured)
            row["enabled"].setChecked(True)
            self.host.refresh_hotkey_backend()
            self.host.restore_settings_dialog_focus()
            row["hotkey"].setFocus(Qt.OtherFocusReason)
            self.host.mark_profile_dirty()

    def capture_mapping_hotkey_by_row(self, row_data):
        self.host.capture_mapping_hotkey(self.host.mapping_row_index(row_data))
