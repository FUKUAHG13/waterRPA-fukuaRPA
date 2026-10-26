# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Repeat, branching and execution controls for per-step settings."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QLabel,
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
from .capture import build_capture_controls
from .coordinates import build_coordinate_controls
from .execution_parts import (
    build_auto_clicker_section,
    build_auxiliary_click_section,
    build_click_form_section,
    build_key_hold_section,
    build_mouse_trail_section,
    build_sound_alert_section,
)
from .image import build_image_controls

# 拆分结构：build_execution_section 是调度器，先创建控件再布局。
# 创建阶段把局部行对象存入 self，布局阶段从 self 读取。


def _create_step_and_repeat_widgets(self, data):
    """备注、重复模式、本步骤循环范围控件。"""
    self.step_note_edit = QLineEdit(str(data.get("step_note", "")))
    self.step_note_edit.setMaxLength(2000)
    self.step_note_edit.setPlaceholderText("可选；鼠标悬停步骤时显示")
    self.step_note_edit.setClearButtonEnabled(True)
    self.step_note_edit.setToolTip(
        "备注只帮助整理脚本，不参与执行、判断或日志参数。"
    )

    self.repeat_combo = NoWheelComboBox()
    self.repeat_combo.addItems(["执行一次", "指定次数", "无限重复"])
    fit_combo_to_contents(self.repeat_combo)
    self.repeat_combo.setCurrentText(str(data.get("repeat_mode", "执行一次")))
    self.repeat_combo.currentTextChanged.connect(self.update_repeat_ui)

    self.repeat_count_edit = QLineEdit(str(data.get("repeat_count", "1")))
    self.repeat_count_edit.setFixedWidth(90)

    self.step_loop_start_edit = QLineEdit(str(data.get("step_loop_start", "1")))
    self.step_loop_start_edit.setFixedWidth(70)
    self.step_loop_start_edit.setToolTip(
        "本步骤从第几次脚本循环开始执行；默认 1 表示从第一轮就生效。"
    )
    self.step_loop_end_edit = QLineEdit(str(data.get("step_loop_end", "0")))
    self.step_loop_end_edit.setFixedWidth(70)
    self.step_loop_end_edit.setToolTip(
        "本步骤执行到第几次脚本循环后不再执行；填 0 表示不限。填 5 表示第 1-5 轮执行，第 6 轮开始跳过。"
    )

    step_loop_row = ResponsiveRow()
    step_loop_row.add_group("从第", self.step_loop_start_edit, "次循环开始")
    step_loop_row.add_group(
        "到第",
        self.step_loop_end_edit,
        "次循环后停止",
        HelpBtn(
            "【本步骤循环范围】\n控制当前步骤在脚本第几轮循环中生效。\n起始循环默认 1；停止循环填 0 表示不限。\n被范围跳过时不会触发成功/失败跳转，只会继续执行下一步。"
        ),
    )
    self.step_loop_row = step_loop_row


def _create_sub_builder_and_policy_widgets(self, data):
    """图片/截图/坐标子构建器 + 失败/禁止跳过/上限/状态控件。"""
    image_rows = build_image_controls(self, data)
    capture_region_row = build_capture_controls(self, data)
    coordinate_rows = build_coordinate_controls(self, data)
    image_point_row = image_rows.image_point_row
    image_mask_row = image_rows.image_mask_row
    step_region_row = image_rows.step_region_row
    inverse_match_row = image_rows.inverse_match_row
    image_confirm_click_row = image_rows.image_confirm_click_row
    coord_every_row = coordinate_rows.every_row
    coord_direction_row = coordinate_rows.direction_row
    coord_point_row = coordinate_rows.point_row
    coord_limit_row = coordinate_rows.limit_row
    coord_reset_row = coordinate_rows.reset_row
    coord_advanced_notice = coordinate_rows.advanced_notice
    coord_sequence_box = coordinate_rows.sequence_box
    self.fail_limit_edit = QLineEdit(str(data.get("fail_limit", "1")))
    self.fail_limit_edit.setFixedWidth(90)
    self.fail_limit_edit.setToolTip(
        "例如填 1 表示失败一次就执行下一步；填 3 表示连续失败三次后才放弃本步。优先级低于“禁止跳过”：开启禁止跳过时，会先一直等待本步骤成功或超时。"
    )

    self.no_skip_wait_chk = QCheckBox("禁止跳过：失败后一直等待本步骤")
    self.no_skip_wait_chk.setChecked(config_bool(data.get("no_skip_wait", False)))
    self.no_skip_wait_chk.setToolTip(
        "开启后，本步骤执行失败不会进入下一步，会按全局“识别间隔”反复等待并重试，直到成功或达到单步超时。"
    )

    self.run_max_executions_edit = QLineEdit(str(data.get("run_max_executions", "0")))
    self.run_max_executions_edit.setFixedWidth(90)
    self.run_max_executions_edit.setToolTip(
        "本次启动脚本后，本步骤最多真正执行多少次；填 0 表示不限。达到上限后会跳过本步骤，不触发成功/失败分支；手动停止并重新启动后重新计数。"
    )

    self.step_disabled_chk = QCheckBox("暂时禁用本步骤")
    self.step_disabled_chk.setChecked(config_bool(data.get("step_disabled", False)))
    self.step_disabled_chk.setToolTip(
        "保存后，本步骤会保留在脚本中但暂不执行。"
        "重新开启后即可恢复，不需要删除并重建步骤。"
    )
    self.editor_hidden_chk = QCheckBox("在主界面隐藏本步骤")
    self.editor_hidden_chk.setChecked(
        config_bool(data.get("editor_hidden", False))
    )
    self.editor_hidden_chk.setToolTip(
        "只隐藏主界面中的步骤行，不影响执行、步骤编号、跳转、导出或预览。"
    )
    self.jump_only_chk = QCheckBox("仅被跳转或指定执行时运行")
    self.jump_only_chk.setChecked(config_bool(data.get("jump_only", False)))
    self.jump_only_chk.setToolTip(
        "正常顺序运行到本步时会跳过；成功/失败跳转、跳过若干步落到本步、"
        "指定步骤调用和调试单步执行时仍会运行。"
    )
    self.capture_region_row = capture_region_row
    self.image_point_row = image_point_row
    self.image_mask_row = image_mask_row
    self.step_region_row = step_region_row
    self.inverse_match_row = inverse_match_row
    self.image_confirm_click_row = image_confirm_click_row
    self.coord_every_row = coord_every_row
    self.coord_direction_row = coord_direction_row
    self.coord_point_row = coord_point_row
    self.coord_limit_row = coord_limit_row
    self.coord_reset_row = coord_reset_row
    self.coord_advanced_notice = coord_advanced_notice
    self.coord_sequence_box = coord_sequence_box


def _create_branch_and_click_widgets(self, data):
    """后台点击、成功/失败分支、指定执行、引用字段控件。"""
    self.background_click_chk = QCheckBox("启用")
    self.background_click_chk.setChecked(
        config_bool(data.get("background_click_en", False))
    )
    self.background_click_chk.setToolTip(
        "不移动真实鼠标，只向本步骤绑定的目标窗口发送坐标点击。"
    )
    self.background_click_chk.toggled.connect(self.update_background_click_ui)
    self.background_click_pick_btn = QPushButton("选择窗口")
    fit_text_button(self.background_click_pick_btn, 88)
    self.background_click_pick_btn.clicked.connect(self.start_background_window_pick)
    self.background_click_clear_btn = QPushButton("清除")
    fit_text_button(self.background_click_clear_btn, 68)
    self.background_click_clear_btn.clicked.connect(self.clear_background_window_binding)
    self.background_click_status = QLabel("尚未选择目标窗口")
    self.background_click_status.setProperty("role", "muted")
    background_click_row = ResponsiveRow()
    background_click_row.add_group(
        self.background_click_chk,
        HelpBtn(
            "【后台点击（仅坐标）】\n"
            "只用于左键单击、左键双击和右键单击的直接坐标模式。启用前必须点“选择窗口”，"
            "再亲自单击目标程序窗口中的任意位置。\n"
            "执行时不会移动真实鼠标；坐标步进和自定义点位会按绑定窗口的相对位置发送。"
            "目标失效或不支持后台消息时会明确失败，不会退回前台真实鼠标点击。\n"
            "游戏、浏览器画布、DirectX 或权限更高的窗口可能无效；权限不同时可尝试以管理员身份运行。"
        ),
    )
    background_click_row.add_group(
        self.background_click_pick_btn,
        self.background_click_clear_btn,
        self.background_click_status,
    )
    self.success_skip_edit = QLineEdit(str(data.get("success_skip", "0")))
    self.success_skip_edit.setFixedWidth(90)
    self.success_skip_edit.setToolTip("本步骤成功后跳过后续 N 步。填 0 表示不跳过。")

    self.success_jump_edit = QLineEdit(str(data.get("success_jump", "0")))
    self.success_jump_edit.setFixedWidth(90)
    self.success_jump_edit.setToolTip(
        "本步骤成功后跳至指定步号继续执行。填 0 表示关闭；步号从 1 开始。"
    )

    self.fail_skip_edit = QLineEdit(str(data.get("fail_skip", "0")))
    self.fail_skip_edit.setFixedWidth(90)
    self.fail_skip_edit.setToolTip(
        "达到连续失败次数后，跳过后续 N 步。填 0 表示直接执行下一步。"
    )

    self.fail_jump_edit = QLineEdit(str(data.get("fail_jump", "0")))
    self.fail_jump_edit.setFixedWidth(90)
    self.fail_jump_edit.setToolTip(
        "达到连续失败次数后跳至指定步号继续执行。填 0 表示关闭；步号从 1 开始。"
    )
    self.call_step_edit = QLineEdit(str(data.get("call_step", "0")))
    self.call_step_edit.setFixedWidth(90)
    self.call_step_edit.setToolTip(
        "当前步骤执行结束后，先完整执行指定步骤一次，再返回并按当前步骤的成功/失败结果跳转。填 0 表示关闭。"
    )
    self.reference_fields_dirty = set()
    self.reference_editors = {
        "success_jump": self.success_jump_edit,
        "fail_jump": self.fail_jump_edit,
        "until_false_jump": self.until_false_jump_edit,
        "until_true_jump": self.until_true_jump_edit,
        "call_step": self.call_step_edit,
    }
    for reference_name, editor in self.reference_editors.items():
        editor.textEdited.connect(
            lambda _text, name=reference_name: self.reference_fields_dirty.add(name)
        )

    self.background_click_row = background_click_row


def _layout_step_control_rows(self, inline_row):
    """备注、步骤状态、入口限制、重复、指定执行、循环范围行布局。"""
    control_form = self.control_form
    step_loop_row = self.step_loop_row
    step_status_row = inline_row(
        (self.step_disabled_chk, self.editor_hidden_chk),
        (
            HelpBtn(
                "【暂时禁用本步骤】\n保存后，本步骤仍保留在列表中，但运行、预览、参数检查和断点都会跳过它。\n关闭禁用即可恢复原有设置。"
            ),
        ),
    )
    control_form.addRow("备注:", self.step_note_edit)
    control_form.addRow("步骤状态:", step_status_row)
    self.step_status_row = step_status_row
    jump_only_row = inline_row(
        (self.jump_only_chk,),
        (
            HelpBtn(
                "【仅被跳转时执行】\n正常顺序经过本步骤时自动略过；只有成功/失败分支、"
                "跳过若干步后落到本步、其他步骤的指定执行或调试单步明确进入时才执行。\n"
                "“从第X步开始执行”仍属于顺序入口，因此不会强制执行本步骤。"
            ),
        ),
    )
    control_form.addRow("入口限制:", jump_only_row)
    self.jump_only_row = jump_only_row
    repeat_row = inline_row(
        (self.repeat_combo,),
        ("次数", self.repeat_count_edit),
        (
            HelpBtn(
                "【本步骤重复】\n可执行一次、指定次数或无限重复。指定次数时在右侧填写本步骤连续执行的次数。"
            ),
        ),
    )
    control_form.addRow("本步骤重复:", repeat_row)
    self.repeat_row = repeat_row
    call_step_row = inline_row(
        ("先执行第", self.call_step_edit, "步再返回"),
        (
            HelpBtn(
                "【先执行指定步骤】\n当前步骤结束后，先调用指定步骤一次，再返回当前步骤处理成功/失败分支。\n"
                "被调用步骤的重复、失败重试、超时和急停仍然生效，但它自身的成功/失败跳转以及再次调用会被忽略。"
                "当前步骤的成功或失败结果不会被调用步骤覆盖。填 0 表示关闭。"
            ),
        ),
    )
    control_form.addRow("指定执行:", call_step_row)
    self.call_step_row = call_step_row
    control_form.addRow("循环范围:", step_loop_row)
    self.step_loop_row = step_loop_row


def _layout_image_and_coord_rows(self, inline_row):
    """图片目标表单和坐标目标表单行布局。"""
    image_point_row = self.image_point_row
    image_mask_row = self.image_mask_row
    step_region_row = self.step_region_row
    inverse_match_row = self.inverse_match_row
    image_confirm_click_row = self.image_confirm_click_row
    capture_region_row = self.capture_region_row
    coord_every_row = self.coord_every_row
    coord_direction_row = self.coord_direction_row
    coord_point_row = self.coord_point_row
    coord_limit_row = self.coord_limit_row
    coord_reset_row = self.coord_reset_row
    coord_advanced_notice = self.coord_advanced_notice
    coord_sequence_box = self.coord_sequence_box
    background_click_row = self.background_click_row
    control_form = self.control_form
    point_limit_row = inline_row(
        (self.point_limit_chk,),
        ("次数", self.point_limit_count_edit),
        (
            HelpBtn(
                "【同点点击上限】\n仅对图片点击生效。某个匹配点达到次数上限后会忽略该点，继续尝试其他匹配位置。"
            ),
        ),
    )
    self.image_target_form.addRow("同点点击上限:", point_limit_row)
    self.point_limit_row = point_limit_row
    self.image_target_form.addRow("图片内点击点:", image_point_row)
    self.image_point_row = image_point_row
    self.image_target_form.addRow("忽略局部变化:", image_mask_row)
    self.image_mask_row = image_mask_row
    self.image_target_form.addRow("本步识别区域:", step_region_row)
    self.step_region_row = step_region_row
    self.image_target_form.addRow("反色匹配:", inverse_match_row)
    self.inverse_match_row = inverse_match_row
    self.image_target_form.addRow("确认补点:", image_confirm_click_row)
    self.image_confirm_click_row = image_confirm_click_row
    control_form.addRow("截图范围:", capture_region_row)
    self.capture_region_row = capture_region_row
    self.coordinate_target_form.addRow("坐标步进:", coord_every_row)
    self.coord_every_row = coord_every_row
    self.coordinate_target_form.addRow("步进方向:", coord_direction_row)
    self.coord_direction_row = coord_direction_row
    self.coordinate_target_form.addRow("目标点位:", coord_point_row)
    self.coord_point_row = coord_point_row
    self.coordinate_target_form.addRow("移动上限:", coord_limit_row)
    self.coord_limit_row = coord_limit_row
    self.coordinate_target_form.addRow("重置循环:", coord_reset_row)
    self.coord_reset_row = coord_reset_row
    self.coordinate_target_form.addRow("", coord_advanced_notice)
    self.coord_advanced_notice = coord_advanced_notice
    self.coordinate_target_form.addRow("自定义点位:", coord_sequence_box)
    self.coord_sequence_box = coord_sequence_box
    self.coordinate_target_form.addRow(
        "后台点击（仅坐标）:", background_click_row
    )
    self.background_click_row = background_click_row


def _layout_jitter_and_action_map(self, inline_row, data, jitter_host_layout=None):
    """随机延迟行和 action_row_forms 映射。

    ``jitter_host_layout`` 是随机延迟行的挂载表单。缺省（``None``）时不再挂到
    ``coordinate_target_form``——那条路径对拖拽不可达，见下方注释。
    """
    # 坐标点击 / 拖拽：移动与按住随机延迟
    # DESIGN: 默认留空（""）而非 "0.0"。空值在保存时不写入步骤数据，引擎
    # _parse_jitter_value 收不到值返回 None，从而回退到全局移动/按住随机延迟；
    # 若默认填 "0.0"，会被引擎解析为显式 0.0 覆盖，静默关掉全局抖动。
    # 旧数据中已存在的值（含用户显式设置的 "0.0"）原样保留。
    self.move_jitter_edit = QLineEdit(str(data.get("move_jitter", "")))
    self.move_jitter_edit.setFixedWidth(72)
    self.click_hld_jitter_edit = QLineEdit(
        str(data.get("click_hld_jitter", ""))
    )
    self.click_hld_jitter_edit.setFixedWidth(72)
    jitter_row = inline_row(
        ("移动随机", self.move_jitter_edit, "秒"),
        ("按住随机", self.click_hld_jitter_edit, "秒"),
        (
            HelpBtn(
                "【移动/按住随机延迟】\n仅对本步骤的鼠标移动与按键按住时间附加 "
                "在移动/按住时间基础上随机加减 0~设定值的抖动，让操作更自然、更不易被识别为机器。\n"
                "留空表示沿用全局设置；填写数值（含 0）则只对本步骤生效并覆盖全局，填 0 表示本步骤不附加随机延迟。"
            ),
        ),
    )
    self.jitter_row = jitter_row
    # FR-20261006-007：这一行曾被 ``addRow`` 到 ``coordinate_target_form``，而该
    # form 是 ``coordinate_target_section`` 的内容布局。拖拽的 capabilities 不含
    # ``SETTING_COORDINATE_PATH``，``apply_action_settings_visibility`` 因此对拖拽
    # 执行 ``coordinate_target_section.setVisible(False)``——**祖先被显式隐藏**，
    # 该行即使自身 ``isHidden()`` 为假也永远不会呈现给用户。矩阵第 25 行承诺
    # 拖拽在高级模式可见「移动随机、按住随机」，故改挂到调用方提供的独立容器。
    if jitter_host_layout is not None:
        jitter_host_layout.addRow("随机延迟:", jitter_row)
    self.action_row_forms = {
        self.point_limit_row: self.image_target_form,
        self.image_point_row: self.image_target_form,
        self.image_mask_row: self.image_target_form,
        self.step_region_row: self.image_target_form,
        self.inverse_match_row: self.image_target_form,
        self.image_confirm_click_row: self.image_target_form,
        self.coord_every_row: self.coordinate_target_form,
        self.coord_direction_row: self.coordinate_target_form,
        self.coord_point_row: self.coordinate_target_form,
        self.coord_limit_row: self.coordinate_target_form,
        self.coord_reset_row: self.coordinate_target_form,
        self.coord_advanced_notice: self.coordinate_target_form,
        self.coord_sequence_box: self.coordinate_target_form,
        self.background_click_row: self.coordinate_target_form,
        jitter_row: self.coordinate_target_form,
    }


def _layout_branch_rows_and_finish(self, inline_row, layout):
    """失败处理、运行上限、成功/失败分支行布局并完成折叠区。"""
    control_form = self.control_form
    failure_policy_row = inline_row(
        ("连续失败", self.fail_limit_edit),
        (self.no_skip_wait_chk,),
        (
            HelpBtn(
                "【失败处理】\n连续失败次数决定何时放弃本步骤；开启禁止跳过后，会优先一直重试到成功或单步超时。"
            ),
        ),
    )
    control_form.addRow("失败处理:", failure_policy_row)
    self.failure_policy_row = failure_policy_row
    run_max_row = inline_row(
        (self.run_max_executions_edit, "次后跳过"),
        (
            HelpBtn(
                "【本次运行最多执行】\n只在当前这次启动脚本期间计数，手动停止并重新启动后清零。\n达到上限后，本步骤视为跳过，不触发成功/失败跳转，也不会执行本步骤内的重复次数。填 0 表示不限。"
            ),
        ),
    )
    control_form.addRow("本次运行上限:", run_max_row)
    self.run_max_row = run_max_row
    success_branch_row = inline_row(
        ("跳过", self.success_skip_edit),
        ("跳至", self.success_jump_edit),
        (
            HelpBtn(
                "【成功分支】\n跳至填 0 表示关闭；同一结果中“跳至”优先于“跳过”。"
            ),
        ),
    )
    control_form.addRow("成功分支:", success_branch_row)
    self.success_branch_row = success_branch_row
    failure_branch_row = inline_row(
        ("跳过", self.fail_skip_edit),
        ("跳至", self.fail_jump_edit),
        (
            HelpBtn(
                "【失败分支】\n达到连续失败次数后执行。跳至填 0 表示关闭；“跳至”优先于“跳过”。"
            ),
        ),
    )
    control_form.addRow("失败分支:", failure_branch_row)
    self.failure_branch_row = failure_branch_row

    self.control_note = QLabel("")
    self.control_note.setProperty("role", "muted")
    self.control_note.setWordWrap(True)
    control_form.addRow("", self.control_note)


def build_execution_section(dialog, data, layout, inline_row):
    """构建执行控制/条件分支设置区。

    本函数是调度器，先创建控件再布局，最后构建附属折叠区。
    """
    self = dialog
    self.execution_section = CollapsibleSection(
        "执行控制 / 条件分支", expanded=True
    )
    self.execution_section.setSizePolicy(
        QSizePolicy.Preferred, QSizePolicy.Maximum
    )
    control_form = QFormLayout()
    control_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    control_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    control_form.setHorizontalSpacing(10)
    control_form.setVerticalSpacing(7)
    control_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
    control_form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
    self.control_form = control_form
    # DESIGN: 先挂载父链再填充子控件，确保子方法中创建的控件立即拥有 C++ 父对象，
    # 避免 Python GC 在无父对象期间回收 Qt 控件。
    self.execution_section.set_content_layout(control_form)
    layout.addWidget(self.execution_section)

    # 创建控件
    _create_step_and_repeat_widgets(self, data)
    _create_sub_builder_and_policy_widgets(self, data)
    _create_branch_and_click_widgets(self, data)

    # 布局
    _layout_step_control_rows(self, inline_row)
    _layout_image_and_coord_rows(self, inline_row)
    # FR-20261006-007：随机延迟行需要一个**独立于坐标目标折叠区**的挂载点。
    # 拖拽会隐藏 ``coordinate_target_section``，行挂在其内容布局里就永远看不见。
    jitter_section = CollapsibleSection("随机延迟", expanded=False)
    jitter_section.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
    jitter_form = QFormLayout()
    # UI-CONVENTION: allow layout-metrics reason=沿用本文件其余 form 既定的 10/7 节奏，抽成共享常量需改动十余处既有 form，超出本轮恢复遗漏的范围
    jitter_form.setHorizontalSpacing(10)
    jitter_form.setVerticalSpacing(7)
    # 与其余 form 一致：长行换行而非横向拉伸，否则窗口窄时「随机延迟」一行
    # 会把水平滚动条顶出可视区（docs/UI开发规范.md 的 WrapLongRows 约定）。
    jitter_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    jitter_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
    jitter_form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
    jitter_section.set_content_layout(jitter_form)
    layout.addWidget(jitter_section)
    self.jitter_section = jitter_section
    _layout_jitter_and_action_map(self, inline_row, data, jitter_form)
    _layout_branch_rows_and_finish(self, inline_row, layout)



    # Secondary collapsible sections extracted to execution_parts.py
    build_auxiliary_click_section(self, data, layout)
    build_click_form_section(self, data, layout, inline_row)
    build_mouse_trail_section(self, data, layout, inline_row)
    build_key_hold_section(self, data, layout, inline_row)
    build_auto_clicker_section(self, data, layout, inline_row)
    build_sound_alert_section(self, data, layout, inline_row)
