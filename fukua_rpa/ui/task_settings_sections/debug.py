# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Breakpoint controls for per-step settings."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QFormLayout, QLabel, QLineEdit, QSizePolicy

from ...task_model import config_bool
from ..components import (
    CollapsibleSection,
    HelpBtn,
    NoWheelComboBox,
    ResponsiveRow,
    fit_combo_to_contents,
)


def build_debug_section(dialog, data, layout, inline_row):
    self = dialog
    self.debug_breakpoint_chk = QCheckBox("运行到本步骤时暂停")
    self.debug_breakpoint_chk.setChecked(
        config_bool(data.get("debug_breakpoint", False))
    )
    self.debug_breakpoint_chk.setToolTip(
        "调试断点只在完整脚本运行时生效；暂停发生在本步骤执行前。"
        "可选择继续运行到下一个断点，或只执行当前步骤后再次暂停。"
    )
    self.debug_breakpoint_chk.toggled.connect(self.update_debug_breakpoint_ui)
    self.debug_condition_edit = QLineEdit(str(data.get("debug_condition", "") or ""))
    self.debug_condition_edit.setMinimumWidth(240)
    self.debug_condition_edit.setPlaceholderText("例如：loop >= 5")
    self.debug_condition_edit.setToolTip(
        "条件为真时才命中断点。使用与“判断表达式”相同的安全语法；"
        "本步骤执行前求值，因此只能读取此前已经设置的变量。"
    )

    self.debug_preset_combo = NoWheelComboBox()
    self.debug_preset_combo.addItems(
        [
            "每次经过都暂停",
            "从第 N 次循环开始",
            "本步骤执行 N 次后",
            "上一步成功时",
            "上一步失败时",
            "高级表达式",
        ]
    )
    fit_combo_to_contents(self.debug_preset_combo)
    debug_preset, debug_value = self.infer_debug_preset(
        self.debug_condition_edit.text()
    )
    self.debug_preset_value_edit = QLineEdit(debug_value or "1")
    self.debug_preset_value_edit.setFixedWidth(70)
    self.debug_preset_value_edit.setToolTip("填写大于等于 1 的整数。")
    self._debug_custom_expression = (
        self.debug_condition_edit.text().strip() if debug_preset == "高级表达式" else ""
    )
    self._debug_preset_mode = debug_preset
    self.debug_preset_combo.setCurrentText(debug_preset)
    self.debug_preset_combo.currentTextChanged.connect(self.on_debug_preset_changed)
    self.debug_preset_value_edit.textChanged.connect(
        self.sync_debug_condition_from_preset
    )
    self.debug_condition_edit.textEdited.connect(self.remember_debug_custom_expression)
    self.debug_section = CollapsibleSection(
        "高级调试",
        expanded=self.debug_breakpoint_chk.isChecked(),
    )
    self.debug_section.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
    debug_form = QFormLayout()
    debug_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    debug_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    debug_form.setHorizontalSpacing(10)
    debug_form.setVerticalSpacing(7)
    debug_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignTop)
    debug_form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
    debug_form.addRow(
        "步骤前暂停:",
        inline_row(
            (self.debug_breakpoint_chk,),
            (
                HelpBtn(
                    "【调试断点】\n完整运行脚本时，会在执行本步骤前暂停。暂停后可继续运行或单步越过。\n直接使用主界面的“只执行当前步骤”不会在断点处停住。"
                ),
            ),
        ),
    )
    debug_preset_row = ResponsiveRow()
    debug_preset_row.add_group(self.debug_preset_combo)
    self.debug_preset_value_group = debug_preset_row.add_group(
        "N:", self.debug_preset_value_edit
    )
    debug_preset_row.add_group(
        HelpBtn(
            "【暂停条件】\n“每次经过”最适合普通排查。\n“从第 N 次循环开始”会从第 N 轮起在每次经过本步骤时暂停。\n“本步骤执行 N 次后”会在已执行 N 次后的每次再次执行前暂停。\n上一执行步骤的成功或失败也可作为条件；脚本刚启动时尚无上一步，失败状态按默认值处理。"
        )
    )
    debug_form.addRow("暂停条件:", debug_preset_row)
    self.debug_expression_label = QLabel("高级表达式:")
    self.debug_expression_row = inline_row(
        (self.debug_condition_edit,),
        (
            HelpBtn(
                "【高级断点表达式】\n只有条件结果为真时才暂停，例如 loop >= 5 或 counter > 10。\n使用安全表达式语法，不会执行 Python 代码。变量必须在到达本步骤前已经设置；若运行时无法求值，为避免漏过问题会暂停并显示原因。"
            ),
        ),
    )
    debug_form.addRow(self.debug_expression_label, self.debug_expression_row)
    debug_note = QLabel(
        "调试设置不会改变步骤执行结果。没有排查脚本问题时，可保持关闭。"
    )
    debug_note.setProperty("role", "muted")
    debug_note.setWordWrap(True)
    debug_form.addRow("", debug_note)
    self.debug_section.set_content_layout(debug_form)
    layout.addWidget(self.debug_section)
    # FR-SCOPE009-004: 模式应用属于协调层职责。此处曾调用
    # `self.apply_settings_mode(self.settings_mode)`，让一个区块构建函数反过来
    # 驱动整窗模式应用（docs/UI开发规范.md「架构边界」第 1、3 条），且当时视图
    # 尚未构建完毕——依赖的控件还不存在。改由 TaskConfigDialog 在
    # `build_task_settings_view` 返回之后统一调用一次。
