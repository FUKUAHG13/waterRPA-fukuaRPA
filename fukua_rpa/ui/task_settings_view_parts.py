# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Section builders extracted from ``task_settings_view.build_task_settings_view``.

Each builder creates its own ``CollapsibleSection`` (or section content) and
appends it to the supplied *layout*. Moved verbatim so widget tree + order
are preserved. The coordinating ``build_task_settings_view`` stays in
``task_settings_view.py``.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..task_model import config_bool
from .components import (
    CollapsibleSection,
    HelpBtn,
    ResponsiveRow,
    fit_text_button,
)


def build_image_target_section(self, data, layout, inline_row):
    self.image_settings_note = QLabel(
        "图片识别设置仅对图片点击/图片悬停生效；直接输入坐标时会自动忽略这些参数。"
    )
    self.image_settings_note.setProperty("role", "muted")
    self.image_settings_note.setWordWrap(True)

    self.context_note = QLabel("")
    self.context_note.setProperty("role", "muted")
    self.context_note.setWordWrap(True)
    layout.addWidget(self.context_note)

    self.image_target_section = CollapsibleSection(
        "图片目标",
        expanded=False,
    )
    self.image_target_section.setSizePolicy(
        QSizePolicy.Preferred, QSizePolicy.Maximum
    )
    image_target_layout = QVBoxLayout()
    image_target_layout.setSpacing(8)

    self.enable_chk = QCheckBox("✓ 为当前图片指令启用独立识别参数")
    self.enable_chk.setChecked(config_bool(data.get("custom_en", False)))
    self.enable_chk.setProperty("emphasis", True)
    image_target_layout.addWidget(self.image_settings_note)
    image_target_layout.addWidget(self.enable_chk)

    self.form_widget = QWidget()
    self.form_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
    form = QFormLayout(self.form_widget)
    form.setContentsMargins(0, 8, 0, 8)
    form.setHorizontalSpacing(10)
    form.setVerticalSpacing(6)
    form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
    form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
    form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

    self.conf_edit = QLineEdit(str(data.get("custom_conf", "0.8")))
    self.s_min_edit = QLineEdit(str(data.get("custom_scale_min", "1.0")))
    self.s_max_edit = QLineEdit(str(data.get("custom_scale_max", "1.0")))
    self.s_step_edit = QLineEdit(str(data.get("custom_scale_step", "0.05")))
    for edit in [self.conf_edit, self.s_min_edit, self.s_max_edit, self.s_step_edit]:
        edit.setFixedWidth(72)
    self.gray_chk = QCheckBox("灰度匹配 (取消则严格区分颜色)")
    self.gray_chk.setChecked(config_bool(data.get("custom_gray", True)))

    form.addRow(
        "识别参数:",
        inline_row(
            ("相似度", self.conf_edit),
            ("最小", self.s_min_edit),
            ("最大", self.s_max_edit),
            ("步长", self.s_step_edit),
            (self.gray_chk,),
            (
                HelpBtn(
                    "【独立识别参数】\n只对当前步骤的图片识别生效。直接坐标点击会忽略相似度、缩放和灰度设置。"
                ),
            ),
        ),
    )

    image_target_layout.addWidget(self.form_widget)
    self.image_target_form = QFormLayout()
    self.image_target_form.setContentsMargins(0, 0, 0, 0)
    self.image_target_form.setHorizontalSpacing(10)
    self.image_target_form.setVerticalSpacing(7)
    self.image_target_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    self.image_target_form.setFieldGrowthPolicy(
        QFormLayout.AllNonFixedFieldsGrow
    )
    self.image_target_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
    self.image_target_form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
    image_target_layout.addLayout(self.image_target_form)
    self.image_target_section.set_content_layout(image_target_layout)
    layout.addWidget(self.image_target_section)


def build_coordinate_target_section(self, data, layout):
    # Lazy import avoids a circular reference with task_settings_view at load time.
    from .task_settings_view import _set_container_enabled_except_help

    self.coordinate_target_section = CollapsibleSection(
        "坐标目标",
        expanded=False,
    )
    self.coordinate_target_section.setSizePolicy(
        QSizePolicy.Preferred, QSizePolicy.Maximum
    )
    self.coordinate_target_form = QFormLayout()
    self.coordinate_target_form.setHorizontalSpacing(10)
    self.coordinate_target_form.setVerticalSpacing(7)
    self.coordinate_target_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    self.coordinate_target_form.setFieldGrowthPolicy(
        QFormLayout.AllNonFixedFieldsGrow
    )
    self.coordinate_target_form.setLabelAlignment(
        Qt.AlignLeft | Qt.AlignTop
    )
    self.coordinate_target_form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
    self.coordinate_target_section.set_content_layout(
        self.coordinate_target_form
    )
    layout.addWidget(self.coordinate_target_section)
    self.enable_chk.setEnabled(self.image_settings_available)
    _set_container_enabled_except_help(
        self.form_widget,
        self.image_settings_available and self.enable_chk.isChecked(),
    )
    self.enable_chk.toggled.connect(self.update_image_settings_enabled)


def build_drag_target_section(self, data, layout):
    # --- 拖拽图片定位（起点 + 终点） ---
    self.drag_target_section = CollapsibleSection(
        "拖拽图片定位", expanded=False
    )
    self.drag_target_section.setSizePolicy(
        QSizePolicy.Preferred, QSizePolicy.Maximum
    )
    drag_target_layout = QVBoxLayout()
    drag_target_layout.setSpacing(8)

    # 拖拽起点图片
    self.drag_source_img_edit = QLineEdit(
        str(data.get("drag_source_img", ""))
    )
    self.drag_source_img_edit.setPlaceholderText(
        "拖拽起点图片路径（留空则使用坐标起点）"
    )
    self.drag_source_img_edit.textChanged.connect(
        self._update_drag_source_state
    )
    self.drag_source_img_browse_btn = fit_text_button(
        QPushButton("选择图片"), minimum_width=80
    )
    self.drag_source_img_browse_btn.clicked.connect(
        self.browse_drag_source_img
    )
    self.drag_source_img_clear_btn = QPushButton("清除")
    self.drag_source_img_clear_btn.setFixedWidth(56)
    self.drag_source_img_clear_btn.clicked.connect(self.clear_drag_source_img)
    drag_source_img_row = ResponsiveRow()
    drag_source_img_row.add_group(
        self.drag_source_img_edit,
        self.drag_source_img_browse_btn,
        self.drag_source_img_clear_btn,
    )
    # 拖拽起点图片内的点击位置
    self.drag_source_point_edit = QLineEdit(
        str(data.get("drag_source_point", ""))
    )
    self.drag_source_point_edit.setFixedWidth(120)
    self.drag_source_point_edit.setPlaceholderText("0.5,0.5")
    self.drag_source_point_edit.setToolTip(
        "起点图片内的相对点击位置（0~1），留空默认点击中心"
    )
    self.drag_source_point_select_btn = QPushButton("选点")
    self.drag_source_point_select_btn.setFixedWidth(56)
    self.drag_source_point_select_btn.clicked.connect(
        self.select_drag_source_point
    )
    self.drag_source_point_info = QLabel("")
    self.drag_source_point_info.setProperty("role", "muted")
    drag_source_point_row = ResponsiveRow()
    drag_source_point_row.add_group(
        QLabel("起点位置:"),
        self.drag_source_point_edit,
        self.drag_source_point_select_btn,
        self.drag_source_point_info,
    )

    # 拖拽终点图片
    self.drag_target_img_edit = QLineEdit(
        str(data.get("drag_target_img", ""))
    )
    self.drag_target_img_edit.setPlaceholderText(
        "留空则使用坐标终点；填写图片路径则通过图片识别定位终点"
    )
    # FR-20260809-1003: 与起点（drag_source_img_edit.textChanged）对称，手动输入/
    # 粘贴终点图片路径后立即刷新"图片内偏移"编辑与"选点"按钮状态；否则只有点击
    # "选择图片"按钮路径生效，手动输入后依赖控件保持禁用。
    self.drag_target_img_edit.textChanged.connect(
        self.update_drag_target_ui
    )
    self.drag_target_img_browse_btn = fit_text_button(
        QPushButton("选择图片"), minimum_width=80
    )
    self.drag_target_img_browse_btn.clicked.connect(
        self.browse_drag_target_img
    )
    self.drag_target_img_clear_btn = QPushButton("清除")
    self.drag_target_img_clear_btn.setFixedWidth(56)
    self.drag_target_img_clear_btn.clicked.connect(self.clear_drag_target_img)

    drag_target_img_row = ResponsiveRow()
    drag_target_img_row.add_group(
        self.drag_target_img_edit,
        self.drag_target_img_browse_btn,
        self.drag_target_img_clear_btn,
    )

    self.drag_target_point_edit = QLineEdit(
        str(data.get("drag_target_point", ""))
    )
    self.drag_target_point_edit.setFixedWidth(120)
    self.drag_target_point_edit.setPlaceholderText("0.5,0.5")
    self.drag_target_point_edit.setToolTip(
        "终点图片内的相对偏移（0~1），格式: X比例,Y比例。留空则点击图片中心。"
    )
    self.drag_target_point_select_btn = QPushButton("选点")
    self.drag_target_point_select_btn.setFixedWidth(56)
    self.drag_target_point_select_btn.clicked.connect(
        self.select_drag_target_point
    )
    self.drag_target_point_info = QLabel("")
    self.drag_target_point_info.setProperty("role", "muted")

    drag_target_point_row = ResponsiveRow()
    drag_target_point_row.add_group(
        "图片内偏移:",
        self.drag_target_point_edit,
        self.drag_target_point_select_btn,
        self.drag_target_point_info,
        HelpBtn(
            "【拖拽终点图片】\n填写终点图片路径后，拖拽终点将通过图片识别定位，"
            "而非使用坐标中的终点。\n"
            "图片内偏移可指定点击终点图片内的哪个位置（0~1 比例），留空默认中心。\n"
            "识别使用与起点相同的相似度和缩放设置。"
                ),
    )

    drag_target_layout.addWidget(drag_source_img_row)
    drag_target_layout.addWidget(drag_source_point_row)
    drag_target_layout.addWidget(drag_target_img_row)
    drag_target_layout.addWidget(drag_target_point_row)
    self.drag_target_section.set_content_layout(drag_target_layout)
    layout.addWidget(self.drag_target_section)


def build_plugin_action_section(self, data, layout):
    # ----- 插件动作 (design §5.4) -----
    # Always built; visibility is driven by the step type in
    # apply_action_settings_visibility(). The heavy picker lives in its own
    # dialog (PluginActionSettings) opened from the button below.
    plugin_section = CollapsibleSection("插件动作", expanded=True)
    plugin_section.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
    plugin_layout = QVBoxLayout()
    plugin_layout.setSpacing(8)
    plugin_label = QLabel("尚未配置插件动作。")
    plugin_label.setProperty("role", "muted")
    plugin_label.setWordWrap(True)
    plugin_btn = QPushButton("配置插件动作...")
    plugin_layout.addWidget(plugin_label)
    plugin_layout.addWidget(plugin_btn)
    plugin_section.set_content_layout(plugin_layout)
    layout.addWidget(plugin_section)
    self.plugin_action_section = plugin_section
    self.plugin_action_label = plugin_label
    self.plugin_action_btn = plugin_btn
