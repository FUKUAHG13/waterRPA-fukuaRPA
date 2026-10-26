# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Image and recognition-region interactions for per-step settings.

DESIGN: 区域选择窗口的创建/销毁由 RegionWindowManager 统一收口
（见 region_window_manager.py）；控制器仍负责"何时发起、连接哪个回调"，
其生命周期与控制器操作绑定而非与主窗口绑定。宿主属性（step_region_window 等）
仅作为便于统一关闭的引用，后续由 P5b-3 封装为 HostView 契约。
"""

from PySide6.QtWidgets import QDialog, QMessageBox

from ...task_model import format_region_text, parse_float_text
from ...vision.core import (
    apply_template_mask_regions,
    normalize_template_mask_regions,
    template_image_detail_status,
)
from ..image_click_point_dialog import ImageClickPointDialog
from ..image_mask_dialog import ImageMaskDialog
from ..region_window_manager import region_window_manager
from ..task_settings_view import _set_container_enabled_except_help


class ImageSettingsController:
    def __init__(self, dialog):
        self.dialog = dialog

    def update_settings_enabled(self):
        host = self.dialog
        _set_container_enabled_except_help(
            host.form_widget,
            host.image_settings_available and host.enable_chk.isChecked(),
        )

    def update_template_mask_ui(self):
        host = self.dialog
        enabled = bool(host.image_settings_available and host.image_path)
        regions = normalize_template_mask_regions(host.template_mask_regions)
        host.template_mask_edit_btn.setEnabled(enabled)
        host.template_mask_clear_btn.setEnabled(enabled and bool(regions))
        host.template_mask_info.setText(
            f"已忽略 {len(regions)} 个区域" if regions else "未设置忽略区域"
        )

    def edit_template_mask_regions(self):
        host = self.dialog
        if not host.image_path:
            QMessageBox.information(host, "无法编辑", "请先在主步骤中选择有效的图片路径。")
            return
        try:
            dialog = ImageMaskDialog(
                host.image_path,
                host.template_mask_regions,
                parent=host,
            )
        except Exception as exc:
            QMessageBox.warning(host, "图片打开失败", str(exc))
            return
        if dialog.exec() != QDialog.Accepted:
            return
        regions = dialog.selected_regions()
        if regions:
            try:
                from PIL import Image

                with Image.open(host.image_path) as source:
                    masked = apply_template_mask_regions(source, regions)
                    valid, reason = template_image_detail_status(
                        masked,
                        host.gray_chk.isChecked(),
                    )
                if not valid:
                    QMessageBox.warning(
                        host,
                        "剩余识别内容不足",
                        f"当前忽略区域无法保存：{reason}",
                    )
                    return
            except Exception as exc:
                QMessageBox.warning(host, "蒙版检查失败", str(exc))
                return
        host.template_mask_regions = list(regions)
        self.update_template_mask_ui()

    def clear_template_mask_regions(self):
        host = self.dialog
        host.template_mask_regions = []
        self.update_template_mask_ui()

    def update_point_limit_ui(self, _=None):
        host = self.dialog
        host.point_limit_count_edit.setEnabled(
            host.point_limit_available and host.point_limit_chk.isChecked()
        )

    def update_click_point_ui(self, _=None):
        host = self.dialog
        enabled = host.image_click_point_available
        checked = enabled and host.image_click_point_chk.isChecked()
        host.image_click_point_chk.setEnabled(enabled)
        host.image_click_point_select_btn.setEnabled(enabled)
        host.image_click_point_preview_btn.setEnabled(checked)
        if not enabled:
            host.image_click_point_info.setText("请先选择图片路径")
            return
        rx = parse_float_text(host.image_click_point_rx, 0.5)
        ry = parse_float_text(host.image_click_point_ry, 0.5)
        host.image_click_point_info.setText(
            f"X {rx:.3f}, Y {ry:.3f}" if checked else "默认点击图片中心"
        )

    def update_step_region_ui(self, _=None):
        host = self.dialog
        enabled = host.image_settings_available
        checked = enabled and host.step_region_chk.isChecked()
        host.step_region_chk.setEnabled(enabled)
        host.step_region_edit.setEnabled(checked)
        host.step_region_pick_btn.setEnabled(enabled)
        host.step_region_clear_btn.setEnabled(
            bool(host.step_region_edit.text().strip())
        )

    def start_step_region_pick(self):
        host = self.dialog
        if not host.image_settings_available:
            QMessageBox.information(host, "无法框选", "当前步骤不是图片识别步骤。")
            return
        region_window_manager.close(host, "step_region")
        # RegionWindow 生命周期统一由 RegionWindowManager 收口（见模块 docstring）。
        win = region_window_manager.open(
            host=host,
            key="step_region",
            purpose="recognition",
            on_selected=host.on_step_region_selected,
        )
        host.step_region_window = win
        win.destroyed.connect(
            lambda *_args: setattr(host, "step_region_window", None)
        )

    def on_step_region_selected(self, rect):
        host = self.dialog
        host.step_region_edit.setText(format_region_text(rect))
        host.step_region_chk.setChecked(True)
        self.update_step_region_ui()

    def clear_step_region(self):
        host = self.dialog
        host.step_region_edit.clear()
        host.step_region_chk.setChecked(False)
        self.update_step_region_ui()

    def select_click_point(self):
        host = self.dialog
        if not host.image_click_point_available:
            QMessageBox.information(
                host,
                "无法选择",
                "当前步骤不是可用的图片点击步骤。请先选择左键/右键点击指令，并填写存在的图片路径。",
            )
            return
        # If the image is deleted externally between path resolution and
        # dialog construction, the exception is caught and reported to the user.
        try:
            dialog = ImageClickPointDialog(
                host.image_path,
                parse_float_text(host.image_click_point_rx, 0.5),
                parse_float_text(host.image_click_point_ry, 0.5),
                selectable=True,
                parent=host,
            )
        except Exception as exc:
            QMessageBox.warning(host, "图片打开失败", str(exc))
            return
        if dialog.exec() == QDialog.Accepted:
            rx, ry = dialog.selected_ratio()
            host.image_click_point_rx = f"{rx:.6f}"
            host.image_click_point_ry = f"{ry:.6f}"
            host.image_click_point_chk.setChecked(True)
            self.update_click_point_ui()

    def preview_click_point(self):
        host = self.dialog
        if not (
            host.image_click_point_available and host.image_click_point_chk.isChecked()
        ):
            return
        try:
            dialog = ImageClickPointDialog(
                host.image_path,
                parse_float_text(host.image_click_point_rx, 0.5),
                parse_float_text(host.image_click_point_ry, 0.5),
                selectable=False,
                parent=host,
            )
        except Exception as exc:
            QMessageBox.warning(host, "图片打开失败", str(exc))
            return
        dialog.exec()

    def crop_template_image(self):
        host = self.dialog
        if not host.image_path:
            QMessageBox.information(
                host, "无法裁剪", "请先在主步骤中选择有效的图片路径。"
            )
            return
        import os

        if not os.path.isfile(host.image_path):
            QMessageBox.warning(host, "文件不存在", f"图片文件不存在：{host.image_path}")
            return
        from ..image_crop_dialog import ImageCropDialog

        try:
            dialog = ImageCropDialog(host.image_path, parent=host)
        except Exception as exc:
            QMessageBox.warning(host, "图片打开失败", str(exc))
            return
        if dialog.exec() == QDialog.Accepted:
            # Refresh template mask info (dimensions may have changed)
            host.template_mask_regions = []
            host.update_template_mask_ui()
            QMessageBox.information(
                host, "裁剪完成", "图片已裁剪并覆盖原文件。忽略区域已重置。"
            )
