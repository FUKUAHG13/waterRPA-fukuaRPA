"""Image-specific controls for per-step settings."""

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from ...task_model import config_bool
from ..components import (
    HelpBtn,
    ResponsiveRow,
    fit_text_button,
)


@dataclass(frozen=True)
class ImageControlRows:
    image_point_row: ResponsiveRow
    image_mask_row: ResponsiveRow
    step_region_row: ResponsiveRow
    inverse_match_row: ResponsiveRow
    image_confirm_click_row: ResponsiveRow


def build_image_controls(dialog, data):
    self = dialog
    self.template_mask_regions = list(data.get("template_mask_regions", []))
    self.template_mask_edit_btn = QPushButton("编辑忽略区域")
    fit_text_button(self.template_mask_edit_btn, 112)
    self.template_mask_edit_btn.clicked.connect(self.edit_template_mask_regions)
    self.template_mask_clear_btn = QPushButton("清除")
    fit_text_button(self.template_mask_clear_btn, 68)
    self.template_mask_clear_btn.clicked.connect(self.clear_template_mask_regions)
    self.crop_image_btn = QPushButton("裁剪图片")
    fit_text_button(self.crop_image_btn, 90)
    self.crop_image_btn.setToolTip("在模板图片上框选区域进行裁剪，覆盖原文件。")
    self.crop_image_btn.clicked.connect(self.crop_template_image)
    self.template_mask_info = QLabel("")
    self.template_mask_info.setProperty("role", "muted")
    image_mask_row = ResponsiveRow()
    image_mask_row.add_group(
        self.template_mask_edit_btn,
        self.template_mask_clear_btn,
        self.crop_image_btn,
        HelpBtn(
            "【忽略图片局部变化】\n在完整模板图片中框出会变化、无需参与识别的一个或多个区域。"
            "软件仍会寻找和点击整张图片，因此目标整体移动后也可以识别。\n"
            "被忽略区域越大，剩余特征越少，误识别风险越高；此功能会自动使用 OpenCV Alpha 掩膜路径。\n\n"
            "【裁剪图片】\n直接在模板图片上框选区域完成裁剪，无需外部工具。裁剪后覆盖原文件。"
        ),
    )
    image_mask_row.add_group(self.template_mask_info)

    self.inverse_match_chk = QCheckBox("允许反色模板匹配")
    self.inverse_match_chk.setChecked(
        config_bool(data.get("inverse_match_en", False))
    )
    self.inverse_match_chk.setToolTip(
        "同时接受明暗反转但轮廓一致的目标。启用后本步骤使用 OpenCV 完整匹配路径，可能比默认原生识别稍慢。"
    )
    inverse_match_row = ResponsiveRow()
    inverse_match_row.add_group(
        self.inverse_match_chk,
        HelpBtn(
            "【反色模板匹配】\n除普通匹配外，也会检查颜色明暗完全相反但结构一致的图像。\n"
            "适合按钮在浅色/深色状态间反转的场景；只在确有需要时开启，以免增加匹配耗时或误识别范围。"
        ),
    )

    self.image_confirm_click_chk = QCheckBox("识别确认补点")
    self.image_confirm_click_chk.setChecked(
        config_bool(data.get("image_confirm_click_en", False))
    )
    self.image_confirm_click_delay_edit = QLineEdit(
        str(data.get("image_confirm_click_delay", "0.5"))
    )
    self.image_confirm_click_delay_edit.setFixedWidth(72)
    self.image_confirm_click_chk.toggled.connect(self.update_auxiliary_click_ui)
    image_confirm_click_row = ResponsiveRow()
    image_confirm_click_row.add_group(
        self.image_confirm_click_chk,
        "延时",
        self.image_confirm_click_delay_edit,
        "秒",
        HelpBtn(
            "【识别确认补点】\n本次图片点击完成后，延时再次检查原命中位置；只有同一图片仍停留在附近时，才在原点击位置追加一次同按键单击。\n"
            "它不参与本步骤的重复、成功/失败分支或脚本循环；停止脚本会取消尚未执行的补点。"
        ),
    )

    self.point_limit_chk = QCheckBox("图片点击同一点位达到上限后忽略此点位")
    self.point_limit_chk.setChecked(
        config_bool(data.get("point_limit_en", False))
    )
    self.point_limit_chk.setEnabled(self.point_limit_available)
    self.point_limit_chk.setToolTip(
        "仅对图片点击生效。填坐标时自动忽略；达到上限后会尝试点击下一个匹配点位。"
    )
    self.point_limit_chk.toggled.connect(self.update_point_limit_ui)

    self.point_limit_count_edit = QLineEdit(str(data.get("point_limit_count", "0")))
    self.point_limit_count_edit.setFixedWidth(90)
    self.point_limit_count_edit.setToolTip(
        "填 0 表示不限制；例如填 1 表示同一个识别点位只点击一次。"
    )

    self.image_click_point_chk = QCheckBox("命中图片后点击图片内指定位置")
    self.image_click_point_chk.setMinimumWidth(245)
    self.image_click_point_chk.setChecked(
        config_bool(data.get("image_click_point_en", False))
    )
    self.image_click_point_chk.setEnabled(self.image_click_point_available)
    self.image_click_point_chk.setToolTip(
        "仅对图片路径的左键单击、左键双击、右键单击生效；直接坐标点击会自动忽略。"
    )
    self.image_click_point_chk.toggled.connect(self.update_image_click_point_ui)
    self.image_click_point_rx = str(data.get("image_click_point_rx", "0.5"))
    self.image_click_point_ry = str(data.get("image_click_point_ry", "0.5"))

    image_point_row = ResponsiveRow()
    image_point_row.add_group(
        self.image_click_point_chk,
        HelpBtn(
            "【图片内点击点】\n只对图片路径的左键、双击和右键点击生效。启用后可在放大的模板图片上选择实际点击的相对位置。"
        ),
    )
    self.image_click_point_select_btn = QPushButton("选择")
    fit_text_button(self.image_click_point_select_btn, 68)
    self.image_click_point_select_btn.setToolTip(
        "打开放大的模板图片，左键点击要实际点击的位置。"
    )
    self.image_click_point_select_btn.clicked.connect(self.select_image_click_point)
    self.image_click_point_preview_btn = QPushButton("预览")
    fit_text_button(self.image_click_point_preview_btn, 68)
    self.image_click_point_preview_btn.setToolTip("预览当前保存的图片内点击位置。")
    self.image_click_point_preview_btn.clicked.connect(self.preview_image_click_point)
    image_point_row.add_group(
        self.image_click_point_select_btn, self.image_click_point_preview_btn
    )
    self.image_click_point_info = QLabel("")
    self.image_click_point_info.setProperty("role", "muted")
    image_point_row.add_group(self.image_click_point_info)

    self.step_region_chk = QCheckBox("启用本步识别区域")
    self.step_region_chk.setChecked(
        config_bool(data.get("step_region_en", False))
    )
    self.step_region_chk.setEnabled(self.image_settings_available)
    self.step_region_chk.setToolTip(
        "仅对当前步骤的图片点击/图片悬停生效；开启后本步骤只会在该区域内找图，优先级高于全局识别区域。直接坐标点击会自动忽略。"
    )
    self.step_region_chk.toggled.connect(self.update_step_region_ui)
    self.step_region_edit = QLineEdit(str(data.get("step_region", "")))
    self.step_region_edit.setPlaceholderText("区域 x,y,w,h")
    self.step_region_edit.setToolTip(
        "本步骤专用识别区域，格式 x,y,w,h。为空或关闭时使用全局识别区域。"
    )
    self.step_region_pick_btn = QPushButton("框选")
    fit_text_button(self.step_region_pick_btn, 68)
    self.step_region_pick_btn.setToolTip("框选当前步骤专用识别区域。")
    self.step_region_pick_btn.clicked.connect(self.start_step_region_pick)
    self.step_region_clear_btn = QPushButton("清除")
    fit_text_button(self.step_region_clear_btn, 68)
    self.step_region_clear_btn.clicked.connect(self.clear_step_region)
    self.step_region_edit.textChanged.connect(self.update_step_region_ui)
    step_region_edit_group = QWidget()
    step_region_edit_layout = QHBoxLayout(step_region_edit_group)
    step_region_edit_layout.setContentsMargins(0, 0, 0, 0)
    step_region_edit_layout.setSpacing(10)
    self.step_region_edit.setMinimumWidth(220)
    step_region_edit_layout.addWidget(self.step_region_edit)
    step_region_edit_layout.addWidget(self.step_region_pick_btn)
    step_region_edit_layout.addWidget(self.step_region_clear_btn)
    step_region_row = ResponsiveRow()
    step_region_row.add_group(
        self.step_region_chk,
        HelpBtn(
            "【本步识别区域】\n只限制当前这一步的图片识别范围，优先级高于全局识别区域。\n适合屏幕上有多个相似图片，但本步骤只允许点击其中一个区域的场景。\n直接输入坐标时自动忽略。"
        ),
    )
    step_region_row.add_widget(step_region_edit_group)
    return ImageControlRows(
        image_point_row=image_point_row,
        image_mask_row=image_mask_row,
        step_region_row=step_region_row,
        inverse_match_row=inverse_match_row,
        image_confirm_click_row=image_confirm_click_row,
    )
