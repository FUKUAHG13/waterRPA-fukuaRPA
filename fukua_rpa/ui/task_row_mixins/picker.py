# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""TaskRow 的坐标/窗口/UIA 取点与按键捕获 Mixin（God Class 拆分）。

从 ``TaskRow`` 提取的屏幕取点、窗口与 UIA 控件选取、按键录入方法组。
窗口构造、控件与草稿数据仍归主类所有，本 Mixin 不持有任何状态；
取点结果统一经 ``commit_external_edit`` 提交为单次历史与审计事件。
"""

import logging

from PySide6.QtWidgets import QDialog, QMessageBox

from ...task_model import parse_coordinate_text
from ..input_tools import CoordinatePickerUI, KeyCaptureDialog

logger = logging.getLogger(__name__)


class TaskRowPickerMixin:
    """屏幕坐标/窗口/UIA 控件取点与按键捕获（12 方法）。"""

    def is_coordinate_pickable(self, text=None):
        if text is None:
            text = self.type_combo.currentText()
        return text in [
            "左键单击",
            "左键双击",
            "右键单击",
            "左键拖拽",
            "右键拖拽",
            "鼠标悬停",
        ]

    def parse_direct_coordinate(self, val):
        try:
            parts = str(val).strip().split(",")
            if len(parts) == 2:
                int(parts[0].strip())
                int(parts[1].strip())
                return True
        except Exception as exc:
            logger.debug("检查坐标有效性失败: %s", exc, exc_info=True)
            pass
        return False

    def direct_coordinate_tuple(self):
        return parse_coordinate_text(self.value_input.text())

    def is_direct_coordinate_value(self, text=None):
        if text is None:
            text = self.type_combo.currentText()
        if text not in ["左键单击", "左键双击", "右键单击", "鼠标悬停"]:
            return False
        return self.parse_direct_coordinate(self.value_input.text())

    def start_coordinate_pick(self):
        text = self.type_combo.currentText()
        if not self.is_coordinate_pickable(text):
            return
        mode = "drag" if "拖拽" in text else "point"
        picker = CoordinatePickerUI(mode, self.on_coordinate_picked)
        self._track_picker(picker)

    def handle_pick_button(self):
        text = self.type_combo.currentText()
        if text == "系统按键":
            self.start_key_capture()
        elif text in ("等待窗口", "激活窗口", "关闭窗口"):
            picker = CoordinatePickerUI("window", self.on_window_picked)
            self._track_picker(picker)
        elif text in ("点击窗口控件", "设置控件文本", "读取控件文本"):
            picker = CoordinatePickerUI("window", self.on_uia_control_picked)
            self._track_picker(picker)
        else:
            self.start_coordinate_pick()

    def _track_picker(self, picker):
        """Store picker reference and connect destroyed signal for cleanup."""
        self.coordinate_picker = picker
        picker.destroyed.connect(
            lambda *args, p=picker: self._on_picker_destroyed(p)
        )

    def _on_picker_destroyed(self, picker):
        """Clear the picker reference only if it is the current one."""
        if getattr(self, "coordinate_picker", None) is picker:
            self.coordinate_picker = None

    def on_window_picked(self, value):
        self.coordinate_picker = None
        point = parse_coordinate_text(value)
        if not point:
            return
        try:
            from ...window_actions import window_title_at_point

            title = window_title_at_point(*point)
            self.commit_external_edit(
                "窗口选取", lambda: self.value_input.setText(title)
            )
        except Exception as error:
            QMessageBox.warning(self, "窗口选取失败", str(error))

    def on_uia_control_picked(self, value):
        self.coordinate_picker = None
        point = parse_coordinate_text(value)
        if not point:
            return
        backend = None
        try:
            from ...mapping_backend import WindowMappingBackend

            backend = WindowMappingBackend()
            # create_binding uses Win32 WindowFromPoint + EnumChildWindows (fast).
            # UIA operations run in a separate worker thread with 2.5s timeout.
            # Typical latency: <50ms. No background thread needed for Win32 path.
            binding = backend.create_binding(*point)
            title = str(binding.get("root_title") or "目标窗口").strip()
            control_class = str(binding.get("target_class") or "控件").strip()

            def apply_binding():
                self.custom_data["uia_binding"] = binding
                if self.type_combo.currentText() == "点击窗口控件":
                    self.value_input.setText(f"{title} / {control_class}")
                self.value_input.setToolTip(
                    f"已绑定：{title}\n控件类型：{control_class}\n点击“控”可重新选择"
                )

            self.commit_external_edit("UIA 控件选取", apply_binding)
        except Exception as error:
            QMessageBox.warning(self, "控件选取失败", str(error))
        finally:
            if backend is not None:
                backend.close()

    def start_key_capture(self):
        dialog = KeyCaptureDialog(self, "录入系统按键")
        if dialog.exec() == QDialog.Accepted and dialog.captured_text:
            self.commit_external_edit(
                "按键录入",
                lambda: self.value_input.setText(dialog.captured_text),
            )

    def on_coordinate_picked(self, value):
        self.coordinate_picker = None
        self.commit_external_edit(
            "坐标选取", lambda: self.value_input.setText(value)
        )
