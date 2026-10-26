"""Screenshot-specific controls for per-step settings."""

from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

from ..components import (
    HelpBtn,
    NoWheelComboBox,
    ResponsiveRow,
    fit_combo_to_contents,
    fit_text_button,
)


def build_capture_controls(dialog, data):
    self = dialog
    self.capture_region_mode_combo = NoWheelComboBox()
    self.capture_region_mode_combo.addItem("沿用全局识别区域", "global")
    self.capture_region_mode_combo.addItem("整个屏幕", "full")
    self.capture_region_mode_combo.addItem("本步自定义区域", "custom")
    saved_mode = str(data.get("capture_region_mode", "global"))
    mode_index = self.capture_region_mode_combo.findData(saved_mode)
    self.capture_region_mode_combo.setCurrentIndex(mode_index if mode_index >= 0 else 0)
    fit_combo_to_contents(self.capture_region_mode_combo)
    self.capture_region_mode_combo.currentIndexChanged.connect(
        self.update_capture_region_ui
    )

    self.capture_region_edit = QLineEdit(str(data.get("capture_region", "")))
    self.capture_region_edit.setPlaceholderText("区域 x,y,w,h")
    self.capture_region_edit.setMinimumWidth(220)
    self.capture_region_edit.setToolTip(
        "截图步骤专用区域，格式 x,y,w,h；只有选择“本步自定义区域”时生效。"
    )
    self.capture_region_edit.textChanged.connect(self.update_capture_region_ui)

    self.capture_region_pick_btn = QPushButton("框选")
    fit_text_button(self.capture_region_pick_btn, 68)
    self.capture_region_pick_btn.setToolTip("在屏幕上框选本步骤要保存的截图区域。")
    self.capture_region_pick_btn.clicked.connect(self.start_capture_region_pick)

    self.capture_region_clear_btn = QPushButton("清除")
    fit_text_button(self.capture_region_clear_btn, 68)
    self.capture_region_clear_btn.setToolTip("清除本步骤保存的自定义截图区域。")
    self.capture_region_clear_btn.clicked.connect(self.clear_capture_region)

    edit_group = QWidget()
    edit_layout = QHBoxLayout(edit_group)
    edit_layout.setContentsMargins(0, 0, 0, 0)
    edit_layout.setSpacing(10)
    edit_layout.addWidget(self.capture_region_edit)
    edit_layout.addWidget(self.capture_region_pick_btn)
    edit_layout.addWidget(self.capture_region_clear_btn)

    row = ResponsiveRow()
    row.add_group(
        self.capture_region_mode_combo,
        HelpBtn(
            "【截图范围】\n沿用全局识别区域：保存当前全局识别区域；若设置了多个区域，会保存能包住这些区域的最小矩形。\n"
            "整个屏幕：忽略全局识别区域。\n本步自定义区域：只保存这里框选的矩形，不影响其他步骤。"
        ),
    )
    row.add_widget(edit_group)
    return row
