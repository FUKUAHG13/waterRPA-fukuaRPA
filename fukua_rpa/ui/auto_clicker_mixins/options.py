# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""连点器工具窗口的选项交互 Mixin（God Class 拆分）。

从 ``AutoClickerToolWindow`` 提取的高级选项方法组：固定坐标取点、
随机偏移/轨迹开关、自定义速度随机间隔同步与中键一次性提示。
所有控件由主类视图构建持有，本 Mixin 不持有任何状态。
"""

from __future__ import annotations

import contextlib

from PySide6.QtWidgets import QMessageBox

from ...task_model import parse_coordinate_text
from ...usage_notices import automatic_first_use_notice_pending, usage_notice
from ..auto_clicker_core import _SPEED_PRESETS
from ..input_tools import CoordinatePickerUI


class ClickerOptionsMixin:
    """连点器高级选项交互（9 方法）。"""

    def _on_fixed_toggled(self, on: bool):
        self.fixed_x_spin.setVisible(on)
        self.fixed_y_spin.setVisible(on)
        self._fixed_x_label.setVisible(on)
        self._fixed_y_label.setVisible(on)

    def _start_fixed_point_pick(self):
        if self.is_running:
            return
        previous = self._fixed_point_picker
        if previous is not None:
            with contextlib.suppress(RuntimeError):
                previous.close()
        picker = CoordinatePickerUI(
            "point", self._on_fixed_point_picked, parent=self
        )
        self._fixed_point_picker = picker
        picker.destroyed.connect(
            lambda *_args, current=picker: setattr(
                self,
                "_fixed_point_picker",
                None if self._fixed_point_picker is current else self._fixed_point_picker,
            )
        )

    def _on_fixed_point_picked(self, value):
        point = parse_coordinate_text(value)
        if point is None:
            return
        self.chk_fixed_center.setChecked(True)
        self.fixed_x_spin.setValue(int(point[0]))
        self.fixed_y_spin.setValue(int(point[1]))

    def _on_jitter_toggled(self, on: bool):
        self.jitter_controls.setVisible(bool(on))

    def _on_curve_toggled(self, on: bool):
        self.curve_strength_label.setVisible(bool(on))
        self.curve_strength_spin.setVisible(bool(on))

    def _sync_interval_variation_limit(self, value):
        if not hasattr(self, "interval_variation_spin"):
            return
        maximum = max(0.0, float(value))
        self.interval_variation_spin.setMaximum(maximum)

    def _sync_random_interval_ui(self):
        enabled = self.chk_random_interval.isChecked()
        self.random_interval_controls.setVisible(enabled)

    def _on_speed_changed(self, index: int):
        is_custom = index == len(_SPEED_PRESETS) - 1
        self.simple_form.setRowVisible(self.custom_interval_row, is_custom)
        if hasattr(self, "random_interval_row"):
            self.random_interval_row.setVisible(is_custom)
            self.random_interval_group.setVisible(is_custom)
            self._sync_random_interval_ui()

    def _on_button_type_changed(self, _index: int):
        self._update_banner()
        if self.btn_type_combo.currentData() != "middle":
            return
        host = self._host_ref()
        settings = getattr(host, "settings", None)
        notice = usage_notice("auto_clicker_middle_button")
        if settings is None:
            return
        if not automatic_first_use_notice_pending(settings, notice.one_time_key):
            return
        if notice.one_time_key:
            settings.setValue(notice.one_time_key, True)
            settings.sync()
        QMessageBox.information(self, notice.title, notice.body)
