# pyright: reportAttributeAccessIssue=false
"""TaskRow 的配置对话框转发、模板捕获转发与能力查询 Mixin（God Class 拆分）。

从 ``TaskRow`` 提取的方法组：步骤配置对话框与模板捕获控制器的薄转发，
以及按当前指令类型解析的步骤设置能力查询。控制器实例仍由主类
``__init__`` 创建，本 Mixin 不持有任何状态。
"""

import os

from ...commands import (
    SETTING_CAPTURE_REGION,
    SETTING_COORDINATE_PATH,
    SETTING_IMAGE_CLICK_POINT,
    SETTING_IMAGE_POINT_LIMIT,
    SETTING_IMAGE_RECOGNITION,
    step_settings_capabilities,
)
from ...profile_paths import resolve_profile_path


class TaskRowConfigCapabilityMixin:
    """配置对话框/模板捕获转发与步骤设置能力查询（18 方法）。"""

    def open_custom_config(self):
        return self.config_dialog_controller.open()

    def apply_custom_config(self, dialog):
        return self.config_dialog_controller.apply(dialog)

    def refresh_config_dialog_context(self):
        return self.config_dialog_controller.refresh_context()

    def on_config_base_coordinate_changed(self, value):
        return self.config_dialog_controller.on_base_coordinate_changed(value)

    def clear_custom_config_dialog(self, dialog, result=None):
        return self.config_dialog_controller.clear(dialog, result)

    def close_config_dialog(self):
        return self.config_dialog_controller.close()

    def start_template_capture(self):
        return self.template_capture_controller.start()

    def on_template_region_selected(self, rect):
        return self.template_capture_controller.on_region_selected(rect)

    def save_template_capture(self, rect):
        return self.template_capture_controller.save(rect)

    def image_settings_available(self):
        return SETTING_IMAGE_RECOGNITION in self.settings_capabilities()

    def point_limit_available(self):
        return SETTING_IMAGE_POINT_LIMIT in self.settings_capabilities()

    def image_click_point_available(self):
        if SETTING_IMAGE_CLICK_POINT not in self.settings_capabilities():
            return False
        return os.path.isfile(self.resolved_image_path())

    def image_test_available(self):
        return (
            self.type_combo.currentText()
            in ("左键单击", "左键双击", "右键单击", "鼠标悬停")
            and not self.is_direct_coordinate_value()
            and os.path.isfile(self.resolved_image_path())
        )

    def resolved_image_path(self):
        main_window = self.window()
        base_dir = getattr(main_window, "base_dir", os.getcwd())
        try:
            return resolve_profile_path(self.value_input.text(), base_dir)
        except ValueError:
            return ""

    def test_image_recognition(self):
        main_window = self.window()
        if hasattr(main_window, "test_task_image_recognition"):
            main_window.test_task_image_recognition(self)

    def coordinate_step_available(self):
        return SETTING_COORDINATE_PATH in self.settings_capabilities()

    def screenshot_settings_available(self):
        return SETTING_CAPTURE_REGION in self.settings_capabilities()

    def settings_capabilities(self):
        return step_settings_capabilities(
            self.type_combo.currentText(), self.value_input.text()
        )
