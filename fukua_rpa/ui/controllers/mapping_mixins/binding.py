# pyright: reportAttributeAccessIssue=false
"""目标窗口绑定与取点 Mixin（God Class 拆分）。

从 ``MappingController`` 搬移的坐标/窗口取点、picker 生命周期、
绑定 UI 刷新与窗口检查器方法组。``pickers``/``inspectors`` 由主控制器
``__init__`` 创建，经实例属性自然可达；Mixin 自身不定义状态。
"""

from __future__ import annotations

import contextlib

import pyperclip
from PySide6.QtCore import Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
)

from ....mapping_backend import WindowBindingError
from ....task_model import parse_coordinate_text
from ....window_diagnostics import format_window_inspection
from ...components import enable_work_window_controls
from ...input_tools import CoordinatePickerUI


class MappingBindingMixin:
    """坐标/窗口取点与目标窗口绑定（11 方法）。"""

    def start_mapping_coordinate_pick(self, map_idx):
        if map_idx < 0 or map_idx >= len(self.host.key_mapping_rows):
            return
        row_data = self.host.key_mapping_rows[map_idx]
        self.host.store_mapping_picker(
            row_data,
            "coordinate",
            CoordinatePickerUI(
                "point",
                lambda value, r=row_data: self.host.on_mapping_coordinate_picked_by_row(r, value),
            ),
        )

    def start_mapping_coordinate_pick_by_row(self, row_data):
        self.host.start_mapping_coordinate_pick(self.host.mapping_row_index(row_data))

    def on_mapping_coordinate_picked(self, map_idx, value):
        if map_idx < 0 or map_idx >= len(self.host.key_mapping_rows):
            return
        row = self.host.key_mapping_rows[map_idx]
        row["coord"].setText(value)
        row["enabled"].setChecked(True)
        self.host.refresh_hotkey_backend()
        self.host.mark_profile_dirty()

    def on_mapping_coordinate_picked_by_row(self, row_data, value):
        self.host.on_mapping_coordinate_picked(self.host.mapping_row_index(row_data), value)

    def store_mapping_picker(self, row_data, purpose, picker):
        key = (id(row_data), str(purpose))
        previous = self.pickers.pop(key, None)
        if previous is not None:
            with contextlib.suppress(RuntimeError):
                previous.close()
        self.pickers[key] = picker
        picker.destroyed.connect(
            lambda *_args, k=key: self.pickers.pop(k, None)
        )
        return picker

    def clear_mapping_window_binding(self, row_data):
        row_data["window_binding"] = {}
        self.host.update_mapping_window_binding_ui(row_data)
        self.host.mark_profile_dirty()

    def update_mapping_window_binding_ui(self, row_data):
        button = row_data.get("bind_window")
        if not button:
            return
        status = row_data.get("binding_status")
        inspect_button = row_data.get("inspect_btn")
        binding = row_data.get("window_binding", {})
        background_mode = self.host.current_mapping_click_mode() == "后台窗口点击(实验)"
        button.setEnabled(background_mode)
        if inspect_button is not None:
            inspect_button.setEnabled(bool(background_mode and binding))
        if status is not None:
            status.setEnabled(background_mode)
        if binding:
            title = str(binding.get("root_title", "")).strip()
            class_name = str(binding.get("root_class", "")).strip()
            target_text = title or class_name or "目标窗口"
            button.setProperty("bound", True)
            button.setToolTip(
                f"已绑定：{target_text}\n点击后可重新手动选择目标程序窗口"
                if background_mode
                else "切换到“后台窗口点击(实验)”后才可重新选择目标窗口"
            )
            if status is not None:
                status.setText(f"已绑定：{target_text}")
                status.setToolTip(f"已绑定：{target_text}")
                status.setProperty("role", "statusGood")
        else:
            button.setProperty("bound", False)
            button.setToolTip(
                "先填写或取点，再点此按钮并亲自单击目标程序窗口"
                if background_mode
                else "仅“后台窗口点击(实验)”模式需要并允许选择目标窗口"
            )
            if status is not None:
                status.setText("未绑定目标窗口")
                status.setToolTip("后台窗口点击前需要绑定目标窗口")
                status.setProperty("role", "muted")
        button.style().unpolish(button)
        button.style().polish(button)
        if status is not None:
            status.style().unpolish(status)
            status.style().polish(status)

    def show_mapping_window_inspector(self, row_data):
        binding = dict(row_data.get("window_binding", {}))
        if not binding:
            QMessageBox.information(
                self.host.settings_interaction_parent(),
                "尚未绑定",
                "请先切换到后台窗口点击，并使用“选择窗口”绑定目标程序。",
            )
            return

        dialog = QDialog(None)
        dialog.setAttribute(Qt.WA_QuitOnClose, False)
        enable_work_window_controls(dialog)
        dialog.setWindowModality(Qt.NonModal)
        row_index = self.host.mapping_row_index(row_data)
        dialog.setWindowTitle(f"映射{row_index + 1} 窗口检查")
        dialog.setMinimumSize(720, 520)
        dialog.resize(860, 640)
        self.host.apply_ui_scale_to_widget(dialog)
        layout = QVBoxLayout(dialog)
        summary = QLabel()
        summary.setProperty("role", "sectionTitle")
        summary.setWordWrap(True)
        layout.addWidget(summary)
        report_view = QTextEdit()
        report_view.setReadOnly(True)
        layout.addWidget(report_view, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        refresh_button = buttons.addButton("刷新", QDialogButtonBox.ActionRole)
        copy_button = buttons.addButton("复制报告", QDialogButtonBox.ActionRole)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(dialog.close)
        buttons.button(QDialogButtonBox.Close).clicked.connect(dialog.close)
        layout.addWidget(buttons)

        def refresh_report():
            current_binding = dict(row_data.get("window_binding", {}))
            report = self.host.ensure_mapping_backend().inspect_binding(current_binding)
            text = format_window_inspection(report)
            report_view.setPlainText(text)
            compatibility = report.get("compatibility", {}).get(
                "classification", "目标窗口已失效"
            )
            summary.setText(f"当前判断：{compatibility}")
            dialog.setProperty("inspectionText", text)

        def copy_report():
            pyperclip.copy(str(dialog.property("inspectionText") or ""))
            QToolTip.showText(QCursor.pos(), "检查报告已复制", copy_button)

        refresh_button.clicked.connect(refresh_report)
        copy_button.clicked.connect(copy_report)
        refresh_report()
        self.inspectors.add(dialog)
        dialog.destroyed.connect(
            lambda *_args, d=dialog: self.inspectors.discard(d)
        )
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def refresh_mapping_binding_controls(self, *_):
        for row in getattr(self.host, "key_mapping_rows", []):
            self.host.update_mapping_window_binding_ui(row)

    def bind_mapping_window_by_row(self, row_data, show_message=True):
        message_parent = self.host.settings_interaction_parent()
        if self.host.current_mapping_click_mode() != "后台窗口点击(实验)":
            if show_message:
                QMessageBox.information(
                    message_parent,
                    "无需选择窗口",
                    "“选择窗口”只用于后台窗口点击。请先把映射点击方式切换为“后台窗口点击(实验)”。",
                )
            return False
        coord = parse_coordinate_text(row_data.get("coord").text() if row_data.get("coord") else "")
        if not coord:
            if show_message:
                QMessageBox.warning(
                    message_parent, "无法绑定窗口", "请先填写坐标，或点击“取点”选择目标位置。"
                )
            return False
        picker = CoordinatePickerUI(
            "window",
            lambda value, r=row_data: self.host.on_mapping_window_picked_by_row(r, value),
        )
        self.host.store_mapping_picker(row_data, "window", picker)
        return True

    def on_mapping_window_picked_by_row(self, row_data, selection_value):
        if self.host.mapping_row_index(row_data) < 0:
            return False
        message_parent = self.host.settings_interaction_parent()
        if self.host.current_mapping_click_mode() != "后台窗口点击(实验)":
            return False
        coord = parse_coordinate_text(
            row_data.get("coord").text() if row_data.get("coord") else ""
        )
        selection = parse_coordinate_text(selection_value)
        if not coord or not selection:
            QMessageBox.warning(message_parent, "无法绑定窗口", "坐标无效，请重新取点。")
            return False
        try:
            binding = self.host.ensure_mapping_backend().create_binding_for_window_at_point(
                selection[0], selection[1], coord[0], coord[1]
            )
        except WindowBindingError as error:
            QMessageBox.warning(message_parent, "无法绑定窗口", str(error))
            self.host.restore_settings_dialog_focus()
            return False
        row_data["window_binding"] = binding
        self.host.update_mapping_window_binding_ui(row_data)
        target_text = binding.get("root_title") or binding.get("root_class") or "目标窗口"
        QToolTip.showText(QCursor.pos(), f"已绑定：{target_text}", row_data.get("bind_window"))
        self.host.append_application_log(
            "<font color='green'>按键映射已绑定到用户选择的目标窗口；后台点击将只发送给该目标。</font>"
        )
        self.host.restore_settings_dialog_focus()
        return True
