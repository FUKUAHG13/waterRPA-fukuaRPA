# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Secondary collapsible-section builders for the per-step execution control.

These functions were extracted verbatim from
``execution.py::build_execution_section`` so that the entry point stays a
thin orchestrator. Behavior is unchanged: each builder creates its own
``CollapsibleSection`` and appends it to the supplied *layout*.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
)

from ...task_model import config_bool
from ..components import (
    CollapsibleSection,
    HelpBtn,
    NoWheelComboBox,
    ResponsiveRow,
    fit_combo_to_contents,
    fit_text_button,
)


def build_auxiliary_click_section(self, data, layout):
    self.auxiliary_click_section = CollapsibleSection("附属点击", expanded=False)
    self.auxiliary_click_section.setSizePolicy(
        QSizePolicy.Preferred, QSizePolicy.Maximum
    )
    auxiliary_form = QFormLayout()
    auxiliary_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    auxiliary_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    auxiliary_form.setHorizontalSpacing(10)
    auxiliary_form.setVerticalSpacing(7)
    auxiliary_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
    self.delayed_second_click_chk = QCheckBox("启用延时二次点击")
    self.delayed_second_click_chk.setChecked(
        config_bool(data.get("delayed_second_click_en", False))
    )
    self.delayed_second_click_delay_edit = QLineEdit(
        str(data.get("delayed_second_click_delay", "0.5"))
    )
    self.delayed_second_click_delay_edit.setFixedWidth(72)
    self.delayed_second_click_chk.toggled.connect(self.update_auxiliary_click_ui)
    delayed_row = ResponsiveRow()
    delayed_row.add_group(
        self.delayed_second_click_chk,
        "延时",
        self.delayed_second_click_delay_edit,
        "秒",
        HelpBtn(
            "【延时二次点击】\n本步骤每次成功点击后，主流程立即继续；到达延时时间后，在当时的点击位置追加一次同按键单击。\n"
            "附属点击不参与循环、重复次数和条件分支。即使原步骤是双击，附属动作也只追加一次单击；停止脚本会取消尚未执行的附属点击。"
        ),
    )
    auxiliary_form.addRow("二次点击:", delayed_row)
    self.delayed_second_click_row = delayed_row
    self.auxiliary_click_section.set_content_layout(auxiliary_form)
    layout.addWidget(self.auxiliary_click_section)


def build_click_form_section(self, data, layout, inline_row):
    # 本步点击形态按字段能力显隐：点击偏移/返回仅用于点击，移动曲线
    # 同时用于点击与拖拽。控件始终保留原始草稿，隐藏不会清空用户数据。
    self.click_form_section = CollapsibleSection("本步点击形态", expanded=False)
    self.click_form_section.setSizePolicy(
        QSizePolicy.Preferred, QSizePolicy.Maximum
    )
    click_form = QFormLayout()
    click_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    click_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    click_form.setHorizontalSpacing(10)
    click_form.setVerticalSpacing(7)
    click_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
    self.click_spatial_jitter_edit = QLineEdit(
        str(data.get("click_spatial_jitter", ""))
    )
    self.click_spatial_jitter_edit.setFixedWidth(72)
    self.move_curve_strength_edit = QLineEdit(
        str(data.get("move_curve_strength", ""))
    )
    self.move_curve_strength_edit.setFixedWidth(72)
    self.click_return_chk = QCheckBox("点击后返回原位")
    self.click_return_chk.setChecked(config_bool(data.get("click_return", False)))
    click_motion_row = ResponsiveRow(horizontal_spacing=10, vertical_spacing=7)
    self.click_spatial_group = click_motion_row.add_group(
        "点击偏移",
        self.click_spatial_jitter_edit,
        "像素",
        HelpBtn(
            "【点击偏移】\n本步骤每次点击在目标周围随机偏移 0～设定像素；"
            "留空时沿用全局设置。只对单击和双击生效。"
        ),
    )
    self.move_curve_group = click_motion_row.add_group(
        "移动曲线",
        self.move_curve_strength_edit,
        "强度",
        HelpBtn(
            "【移动曲线】\n本步骤鼠标移动路径的弯曲程度，0 为直线、1 为大幅弯曲；"
            "留空时沿用全局设置。适用于点击和拖拽。"
        ),
    )
    self.click_return_group = click_motion_row.add_group(
        self.click_return_chk,
        HelpBtn(
            "【点击后返回】\n点击完成后将鼠标移回点击前的位置。"
            "只对单击和双击生效。"
        ),
    )
    click_form.addRow("点击形态:", click_motion_row)
    self.click_form_section.set_content_layout(click_form)
    layout.addWidget(self.click_form_section)


def build_mouse_trail_section(self, data, layout, inline_row):
    """Build the advanced-only visual trail override for pointer actions."""

    self.mouse_trail_section = CollapsibleSection(
        "本步鼠标移动拖影", expanded=False
    )
    self.mouse_trail_section.setSizePolicy(
        QSizePolicy.Preferred, QSizePolicy.Maximum
    )
    trail_form = QFormLayout()
    trail_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    trail_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    trail_form.setHorizontalSpacing(10)
    trail_form.setVerticalSpacing(7)
    trail_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    self.mouse_trail_mode_combo = NoWheelComboBox()
    self.mouse_trail_mode_combo.addItem("沿用全局设置", "inherit")
    self.mouse_trail_mode_combo.addItem("本步关闭", "off")
    self.mouse_trail_mode_combo.addItem("本步开启", "on")
    mode = str(data.get("mouse_trail_mode", "inherit") or "inherit").lower()
    index = self.mouse_trail_mode_combo.findData(mode)
    self.mouse_trail_mode_combo.setCurrentIndex(index if index >= 0 else 0)
    fit_combo_to_contents(self.mouse_trail_mode_combo)
    trail_form.addRow(
        "显示方式:",
        inline_row(
            (self.mouse_trail_mode_combo,),
            (
                HelpBtn(
                    "【本步鼠标移动拖影】\n只改变视觉提示，不会改变鼠标实际路径、"
                    "速度或点击位置。适用于本步的前台点击、拖拽或悬停；后台窗口点击"
                    "不移动真实鼠标，因此不会显示拖影。"
                ),
            ),
        ),
    )
    self.mouse_trail_section.set_content_layout(trail_form)
    layout.addWidget(self.mouse_trail_section)


def build_key_hold_section(self, data, layout, inline_row):
    # 本步系统按键：按住时长（仅系统按键）
    # DESIGN: 默认留空（""），保存时未填写则不写入步骤数据，引擎回退全局
    # key_hold_duration；填 0 表示按下即松开（原有行为）。
    self.key_hold_section = CollapsibleSection("本步系统按键", expanded=False)
    self.key_hold_section.setSizePolicy(
        QSizePolicy.Preferred, QSizePolicy.Maximum
    )
    key_hold_form = QFormLayout()
    key_hold_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    key_hold_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    key_hold_form.setHorizontalSpacing(10)
    key_hold_form.setVerticalSpacing(7)
    key_hold_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
    self.key_hold_duration_edit = QLineEdit(
        str(data.get("key_hold_duration", ""))
    )
    self.key_hold_duration_edit.setFixedWidth(72)
    key_hold_row = inline_row(
        ("按住时长", self.key_hold_duration_edit, "秒"),
        (
            HelpBtn(
                "【本步系统按键按住时长】\n组合键按下后保持按住设定秒数再松开，"
                "留空沿用全局设置；填 0 表示按下即松开（原有行为）。"
            ),
        ),
    )
    key_hold_form.addRow("按住时长:", key_hold_row)
    self.key_hold_section.set_content_layout(key_hold_form)
    layout.addWidget(self.key_hold_section)


def build_auto_clicker_section(self, data, layout, inline_row):
    # 连点器（原生指令 28.0）专属设置：左/中/右键重复点击，支持随机点击间隔、
    # 按下抬起间隔、以当前鼠标或固定坐标为中心 + H/V 抖动、点击次数限制，以及
    # 大循环/小循环嵌套。仅在该指令下可见（见 task_settings_dialog 可见性逻辑）。
    self.auto_clicker_section = CollapsibleSection("连点器参数", expanded=False)
    self.auto_clicker_section.setSizePolicy(
        QSizePolicy.Preferred, QSizePolicy.Maximum
    )
    ac_form = QFormLayout()
    ac_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    ac_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    ac_form.setHorizontalSpacing(10)
    ac_form.setVerticalSpacing(7)
    ac_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)

    self.ac_button_combo = NoWheelComboBox()
    self.ac_button_combo.addItem("左键", "left")
    self.ac_button_combo.addItem("中键", "middle")
    self.ac_button_combo.addItem("右键", "right")
    fit_combo_to_contents(self.ac_button_combo)
    _ac_btn = str(data.get("ac_button", "left"))
    self.ac_button_combo.setCurrentIndex(
        max(0, self.ac_button_combo.findData(_ac_btn))
    )

    self.ac_click_count_edit = QLineEdit(str(data.get("ac_click_count", "1")))
    self.ac_click_count_edit.setFixedWidth(72)
    self.ac_big_loop_count_edit = QLineEdit(
        str(data.get("ac_big_loop_count", "1"))
    )
    self.ac_big_loop_count_edit.setFixedWidth(72)
    ac_loop_row = inline_row(
        ("小循环次数", self.ac_click_count_edit),
        ("大循环次数", self.ac_big_loop_count_edit),
        (
            HelpBtn(
                "【循环次数】\n小循环=单次点击序列的点击次数；大循环=把整段小循环"
                "重复的次数。两者填 0 均表示“无限循环直到停止”。"
            ),
        ),
    )
    ac_form.addRow("循环次数:", ac_loop_row)

    self.ac_interval_min_edit = QLineEdit(str(data.get("ac_interval_min", "0.1")))
    self.ac_interval_min_edit.setFixedWidth(72)
    self.ac_interval_max_edit = QLineEdit(str(data.get("ac_interval_max", "0.3")))
    self.ac_interval_max_edit.setFixedWidth(72)
    self.ac_press_duration_edit = QLineEdit(str(data.get("ac_press_duration", "0")))
    self.ac_press_duration_edit.setFixedWidth(72)
    self.ac_big_loop_interval_edit = QLineEdit(
        str(data.get("ac_big_loop_interval", "0"))
    )
    self.ac_big_loop_interval_edit.setFixedWidth(72)
    ac_timing_row = inline_row(
        ("点击间隔(s)", self.ac_interval_min_edit, "~", self.ac_interval_max_edit),
        ("按下时长(s)", self.ac_press_duration_edit),
        ("大循环间隔(s)", self.ac_big_loop_interval_edit),
        (
            HelpBtn(
                "【时间与间隔】\n点击间隔：每次点击之间随机等待 最小~最大 秒；"
                "按下时长：鼠标按下的保持时间（按下到抬起之间的间隔）；"
                "大循环间隔：每一轮大循环之间的等待秒数。"
            ),
        ),
    )
    ac_form.addRow("时间参数:", ac_timing_row)

    self.ac_center_mode_combo = NoWheelComboBox()
    self.ac_center_mode_combo.addItem("以当前鼠标为中心", "current")
    self.ac_center_mode_combo.addItem("固定坐标", "fixed")
    fit_combo_to_contents(self.ac_center_mode_combo)
    _ac_center = str(data.get("ac_center_mode", "current"))
    self.ac_center_mode_combo.setCurrentIndex(
        max(0, self.ac_center_mode_combo.findData(_ac_center))
    )
    ac_button_row = inline_row(
        ("按键", self.ac_button_combo),
        (self.ac_center_mode_combo,),
        (
            HelpBtn(
                "【按键 / 中心】\n按键：左/中/右键；中心：以当前鼠标位置为中心"
                "（每次点击在鼠标当前位置附近抖动，点击后归位），或以固定屏幕坐标"
                "为中心。"
            ),
        ),
    )
    ac_form.addRow("按键/中心:", ac_button_row)

    self.ac_fixed_x_edit = QLineEdit(str(data.get("ac_fixed_x", "0")))
    self.ac_fixed_x_edit.setFixedWidth(72)
    self.ac_fixed_y_edit = QLineEdit(str(data.get("ac_fixed_y", "0")))
    self.ac_fixed_y_edit.setFixedWidth(72)
    ac_fixed_row = inline_row(
        ("坐标 X", self.ac_fixed_x_edit),
        ("Y", self.ac_fixed_y_edit),
        (
            HelpBtn(
                "【固定坐标】\n中心模式为“固定坐标”时使用的点击基准坐标（屏幕像素）。"
            ),
        ),
    )
    self.ac_fixed_row = ac_fixed_row
    ac_form.addRow("固定坐标:", ac_fixed_row)

    self.ac_jitter_h_edit = QLineEdit(str(data.get("ac_jitter_h", "0")))
    self.ac_jitter_h_edit.setFixedWidth(72)
    self.ac_jitter_v_edit = QLineEdit(str(data.get("ac_jitter_v", "0")))
    self.ac_jitter_v_edit.setFixedWidth(72)
    ac_jitter_row = inline_row(
        ("横向抖动(px)", self.ac_jitter_h_edit),
        ("纵向抖动(px)", self.ac_jitter_v_edit),
        (
            HelpBtn(
                "【坐标抖动】\n每次点击在基准坐标周围随机偏移 ±设定像素，模拟人类"
                "不会每次点同一个像素。填 0 表示精确点击。"
            ),
        ),
    )
    ac_form.addRow("坐标抖动:", ac_jitter_row)

    self.auto_clicker_section.set_content_layout(ac_form)
    layout.addWidget(self.auto_clicker_section)

    def _sync_ac_fixed_visibility():
        fixed = self.ac_center_mode_combo.currentData() == "fixed"
        ac_form.setRowVisible(self.ac_fixed_row, fixed)

    self.ac_center_mode_combo.currentIndexChanged.connect(
        _sync_ac_fixed_visibility
    )
    _sync_ac_fixed_visibility()


def build_sound_alert_section(self, data, layout, inline_row):
    # 声音提示（14.0）专属小齿轮设置：每步可单独指定音频文件与播放时长，
    # 优先级高于全局设置。仅高级模式可见（见 task_settings_dialog 可见性逻辑）。
    self.sound_alert_section = CollapsibleSection("声音提示（每步覆盖）", expanded=False)
    self.sound_alert_section.setSizePolicy(
        QSizePolicy.Preferred, QSizePolicy.Maximum
    )
    sa_form = QFormLayout()
    sa_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    sa_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    sa_form.setHorizontalSpacing(10)
    sa_form.setVerticalSpacing(7)
    sa_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)

    self.sound_alert_audio_path_edit = QLineEdit(
        str(data.get("sound_alert_audio_path", ""))
    )
    self.sound_alert_audio_path_edit.setPlaceholderText(
        "留空则使用全局默认音频"
    )
    self.sound_alert_audio_path_edit.setMinimumWidth(260)
    sa_browse_btn = fit_text_button(QPushButton("浏览..."), 84)

    def _browse_sound_alert_audio():
        from PySide6.QtWidgets import QFileDialog

        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "选择声音提示音频",
            "",
            "音频文件 (*.wav *.mp3 *.ogg *.flac *.m4a);;所有文件 (*.*)",
        )
        if chosen:
            self.sound_alert_audio_path_edit.setText(chosen)

    sa_browse_btn.clicked.connect(_browse_sound_alert_audio)
    sa_audio_row = inline_row(
        ("音频文件:", self.sound_alert_audio_path_edit, sa_browse_btn),
        (
            HelpBtn(
                "【每步音频（覆盖全局）】\n此处指定的音频仅作用于这一个“声音提示”步骤，"
                "优先级高于全局默认音频。留空则沿用全局默认；文件无效时回退系统蜂鸣。"
            ),
        ),
    )
    sa_form.addRow("每步音频:", sa_audio_row)
    self.sound_alert_audio_row = sa_audio_row

    self.sound_alert_play_seconds_edit = QLineEdit(
        str(data.get("sound_alert_play_seconds", "30"))
    )
    self.sound_alert_play_seconds_edit.setFixedWidth(72)
    self.sound_alert_play_seconds_edit.setValidator(
        QIntValidator(0, 600, self.sound_alert_play_seconds_edit)
    )
    sa_seconds_row = inline_row(
        ("播放时长(秒)", self.sound_alert_play_seconds_edit),
        (
            HelpBtn(
                "【每步播放时长（覆盖全局）】\n音频短于该值时完整播放，长于此值时自动"
                "截断到该长度；填 0 表示不截断。默认 30 秒，优先级高于全局默认。\n"
                "仅高级模式可见。"
            ),
        ),
    )
    sa_form.addRow("播放时长:", sa_seconds_row)
    self.sound_alert_seconds_row = sa_seconds_row

    self.sound_alert_section.set_content_layout(sa_form)
    layout.addWidget(self.sound_alert_section)
