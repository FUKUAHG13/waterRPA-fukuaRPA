# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Advanced ordered image-to-action mapping section."""

from PySide6.QtWidgets import QSizePolicy, QVBoxLayout

from ..components import CollapsibleSection, HelpBtn, ResponsiveRow
from ..multi_image_action_editor import MultiImageActionEditor


def build_multi_image_section(dialog, data, layout):
    self = dialog
    self.multi_image_section = CollapsibleSection("多图动作规则", expanded=True)
    self.multi_image_section.setSizePolicy(
        QSizePolicy.Preferred, QSizePolicy.Maximum
    )
    section_layout = QVBoxLayout()
    section_layout.setSpacing(8)
    help_row = ResponsiveRow()
    help_row.add_group(
        HelpBtn(
            "【多图动作映射】\n按表格从上到下依次找图，第一张匹配成功的图片会执行本行绑定动作，随后结束本步骤。\n"
            "每行可单独填写识别区域和相似度；留空时沿用本步骤或全局设置。匹配点点击/悬停不需要动作参数。"
        )
    )
    section_layout.addWidget(help_row)
    self.multi_image_action_editor = MultiImageActionEditor(
        data.get("multi_image_actions", []),
        settings=self.dialog_settings,
        parent=self,
    )
    section_layout.addWidget(self.multi_image_action_editor)
    self.multi_image_section.set_content_layout(section_layout)
    layout.addWidget(self.multi_image_section)
