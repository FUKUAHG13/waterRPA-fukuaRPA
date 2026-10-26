"""Window binding for coordinate-only per-step background clicks."""

from PySide6.QtWidgets import QMessageBox

from ...commands import SETTING_BACKGROUND_CLICK
from ...mapping_backend import WindowBindingError, WindowMappingBackend
from ...task_model import parse_coordinate_text
from ..input_tools import CoordinatePickerUI
from ..lifecycle import safe_close


class BackgroundClickSettingsController:
    def __init__(self, dialog):
        self.dialog = dialog
        self.backend = None

    def _coordinate(self):
        return parse_coordinate_text(self.dialog.step_value)

    def _binding_matches_coordinate(self):
        host = self.dialog
        coordinate = self._coordinate()
        binding = host.background_click_binding
        if coordinate is None or not binding:
            return False
        try:
            return (
                int(binding.get("source_x")) == int(coordinate[0])
                and int(binding.get("source_y")) == int(coordinate[1])
            )
        except (TypeError, ValueError):
            return False

    def update_ui(self, _=None):
        host = self.dialog
        relevant = SETTING_BACKGROUND_CLICK in host.settings_capabilities()
        binding = host.background_click_binding
        binding_matches = self._binding_matches_coordinate()
        host.background_click_chk.setEnabled(relevant)
        host.background_click_pick_btn.setEnabled(relevant)
        host.background_click_clear_btn.setEnabled(bool(binding))

        if not relevant:
            text = "仅直接坐标点击可用"
            role = "muted"
        elif not binding:
            text = "尚未选择目标窗口"
            role = "muted"
        elif not binding_matches:
            text = "坐标已变化，请重新选择窗口"
            role = "statusWarn"
        else:
            target = (
                str(binding.get("root_title") or "").strip()
                or str(binding.get("root_class") or "").strip()
                or "目标窗口"
            )
            text = f"已绑定：{target}"
            role = "statusGood"
        host.background_click_status.setText(text)
        host.background_click_status.setToolTip(text)
        host.background_click_status.setProperty("role", role)
        host.background_click_status.style().unpolish(host.background_click_status)
        host.background_click_status.style().polish(host.background_click_status)

    def start_window_pick(self):
        host = self.dialog
        coordinate = self._coordinate()
        if coordinate is None:
            QMessageBox.information(
                host,
                "无法选择窗口",
                "后台点击只用于直接坐标的左键、双击或右键点击。请先在主步骤中填写或选取坐标。",
            )
            return
        previous = getattr(host, "background_window_picker", None)
        safe_close(previous)
        picker = CoordinatePickerUI("window", host.on_background_window_selected)
        host.background_window_picker = picker
        picker.destroyed.connect(
            lambda *_args: setattr(host, "background_window_picker", None)
        )

    def on_window_selected(self, value):
        host = self.dialog
        selection = parse_coordinate_text(value)
        coordinate = self._coordinate()
        if selection is None or coordinate is None:
            QMessageBox.warning(host, "无法选择窗口", "选取坐标无效，请重新选择。")
            return
        try:
            if self.backend is None:
                self.backend = WindowMappingBackend()
            # create_binding_for_window_at_point uses Win32 WindowFromPoint +
            # EnumChildWindows (fast, <50ms). UIA operations already run in a
            # separate worker thread with 2.5s timeout. No background thread needed.
            binding = self.backend.create_binding_for_window_at_point(
                selection[0], selection[1], coordinate[0], coordinate[1]
            )
        except WindowBindingError as error:
            QMessageBox.warning(host, "无法选择窗口", str(error))
            return
        binding = dict(binding)
        binding["source_x"] = int(coordinate[0])
        binding["source_y"] = int(coordinate[1])
        host.background_click_binding = binding
        host.background_click_chk.setChecked(True)
        self.update_ui()

    def clear_binding(self):
        host = self.dialog
        host.background_click_binding = {}
        host.background_click_chk.setChecked(False)
        self.update_ui()

    def close(self):
        host = self.dialog
        picker = getattr(host, "background_window_picker", None)
        safe_close(picker)
        host.background_window_picker = None
        backend = self.backend
        self.backend = None
        if backend is not None:
            backend.close()
