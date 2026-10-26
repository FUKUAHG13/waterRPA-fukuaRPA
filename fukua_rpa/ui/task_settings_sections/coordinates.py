"""Coordinate-step and custom-point controls for per-step settings."""

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...task_model import config_bool
from ..components import (
    HelpBtn,
    NoWheelComboBox,
    ResponsiveRow,
    fit_combo_to_contents,
    fit_compact_text_button,
    fit_text_button,
)


@dataclass(frozen=True)
class CoordinateControlRows:
    every_row: ResponsiveRow
    direction_row: ResponsiveRow
    point_row: ResponsiveRow
    limit_row: ResponsiveRow
    reset_row: ResponsiveRow
    advanced_notice: QLabel
    sequence_box: QWidget


def build_coordinate_controls(dialog, data):
    self = dialog
    self.coord_step_chk = QCheckBox("坐标点击启用步进偏移")
    self.coord_step_chk.setChecked(
        config_bool(data.get("coord_step_en", False))
    )
    self.coord_step_chk.setEnabled(self.coordinate_step_available)
    self.coord_step_chk.setToolTip(
        "仅对直接输入坐标的点击步骤生效；图片识别点击会自动忽略。"
    )
    self.coord_step_chk.toggled.connect(self.update_coord_step_ui)

    self.coord_step_every_edit = QLineEdit(str(data.get("coord_step_every", "1")))
    self.coord_step_every_edit.setFixedWidth(70)
    self.coord_step_every_edit.setToolTip(
        "每执行本坐标点击步骤多少次后，移动到下一个点击位置。"
    )

    self.coord_step_direction_combo = NoWheelComboBox()
    self.coord_step_direction_combo.addItems(self.COORD_STEP_DIRECTIONS)
    fit_combo_to_contents(self.coord_step_direction_combo)
    self.coord_step_direction_combo.setCurrentText(
        str(data.get("coord_step_direction", "向下"))
    )
    self.coord_step_direction_combo.currentTextChanged.connect(
        self.update_coord_step_ui
    )

    self.coord_step_distance_edit = QLineEdit(str(data.get("coord_step_distance", "0")))
    self.coord_step_distance_edit.setFixedWidth(70)
    self.coord_step_dx_edit = QLineEdit(str(data.get("coord_step_dx", "0")))
    self.coord_step_dx_edit.setFixedWidth(70)
    self.coord_step_dy_edit = QLineEdit(str(data.get("coord_step_dy", "0")))
    self.coord_step_dy_edit.setFixedWidth(70)
    self.coord_step_point_edit = QLineEdit(str(data.get("coord_step_point", "")))
    self.coord_step_point_edit.setPlaceholderText("例如 960,540")

    self.coord_step_max_steps_edit = QLineEdit(
        str(data.get("coord_step_max_steps", "0"))
    )
    self.coord_step_max_steps_edit.setFixedWidth(70)
    self.coord_step_max_steps_edit.setToolTip(
        "普通方向：最多偏移多少次后不再移动，填 0 表示不限次数。"
        "移动到新点位：表示整条路径的点位总数（包含起点和目标点）；填 0 表示只使用起点和目标点。"
    )
    self.coord_step_max_steps_edit.textChanged.connect(self.on_coord_step_count_changed)
    self.coord_step_max_distance_edit = QLineEdit(
        str(data.get("coord_step_max_distance", "0"))
    )
    self.coord_step_max_distance_edit.setFixedWidth(70)
    self.coord_step_max_distance_edit.setToolTip(
        "普通方向累计偏移距离达到多少像素后不再移动；填 0 表示不限距离。"
        "移动到新点位时此项不参与计算。"
    )
    self.coord_step_stop_chk = QCheckBox("达到移动上限后停止脚本")
    self.coord_step_stop_chk.setChecked(config_bool(data.get("coord_step_stop", False)))
    self.coord_step_reset_after_edit = QLineEdit(
        str(data.get("coord_step_reset_after", "0"))
    )
    self.coord_step_reset_after_edit.setFixedWidth(70)
    self.coord_step_reset_after_edit.setToolTip(
        "本坐标步进成功点击多少次后自动回到起点并重新开始移动；填 0 表示不自动重置。重置触发时优先于“达到移动上限后停止脚本”。左键双击按一次本步骤点击动作计数。"
    )

    coord_every_row = ResponsiveRow()
    coord_every_row.add_group(
        self.coord_step_chk,
        HelpBtn(
            "【坐标步进】\n只对直接输入坐标的点击步骤生效。按设定频率依次移动点击位置，图片识别点击会自动忽略。"
        ),
    )
    coord_every_row.add_group("每", self.coord_step_every_edit, "次后移动")

    coord_direction_row = ResponsiveRow()
    coord_direction_row.add_group(self.coord_step_direction_combo)
    coord_direction_row.add_group("距离:", self.coord_step_distance_edit)
    coord_direction_row.add_group(
        "dx:", self.coord_step_dx_edit, "dy:", self.coord_step_dy_edit
    )

    coord_point_row = ResponsiveRow()
    self.coord_step_point_edit.setMinimumWidth(220)
    self.coord_step_pick_btn = QPushButton("取")
    fit_compact_text_button(self.coord_step_pick_btn)
    self.coord_step_pick_btn.setToolTip(
        "点击后直接进入取点状态，左键选取目标点位，右键取消。"
    )
    self.coord_step_pick_btn.clicked.connect(self.start_coord_step_point_pick)
    self.coord_step_preview_btn = QPushButton("预览")
    fit_text_button(self.coord_step_preview_btn, 68)
    self.coord_step_preview_btn.setToolTip(
        "在屏幕上临时显示本步骤会点击的点位；预览不会执行点击。"
    )
    self.coord_step_preview_btn.clicked.connect(self.show_coord_step_preview)
    coord_point_row.add_group(
        self.coord_step_point_edit,
        self.coord_step_pick_btn,
        self.coord_step_preview_btn,
    )

    self.coord_step_manual_info = QLabel("")
    self.coord_step_manual_info.setProperty("role", "statusInfo")
    self.coord_step_clear_manual_btn = QPushButton("清除手动修正点")
    fit_text_button(self.coord_step_clear_manual_btn, 132)
    self.coord_step_clear_manual_btn.setToolTip(
        "清除本步骤预览中拖动中间点产生的手动修正坐标。"
    )
    self.coord_step_clear_manual_btn.clicked.connect(
        self.clear_coord_step_manual_points
    )
    self.coord_step_manual_help = HelpBtn(
        "【手动修正点】\n预览中单独拖动中间点会保存手动修正。"
        "修改路径点位数时会自动清除，避免旧坐标错位。"
    )
    coord_point_row.add_group(
        self.coord_step_manual_info,
        self.coord_step_clear_manual_btn,
        self.coord_step_manual_help,
    )

    coord_limit_row = ResponsiveRow()
    self.coord_step_max_steps_label = QLabel("最多偏移次数:")
    self.coord_step_max_distance_label = QLabel("最远偏移距离:")
    coord_limit_row.add_group(
        self.coord_step_max_steps_label, self.coord_step_max_steps_edit
    )
    self.coord_step_distance_limit_group = coord_limit_row.add_group(
        self.coord_step_max_distance_label, self.coord_step_max_distance_edit
    )
    coord_limit_row.add_group(
        self.coord_step_stop_chk,
        HelpBtn(
            "【路径或移动上限】\n"
            "向上、向下、向左、向右或自定义偏移时：次数表示最多偏移几次，"
            "距离表示离起点最远多少像素；两项填 0 都表示不限。\n"
            "移动到新点位时：只使用路径点位数，将起点到目标点均分；"
            "点位数包含首尾，填 0 表示只点起点和目标点，最大距离会自动忽略。"
        ),
    )

    coord_reset_row = ResponsiveRow()
    coord_reset_row.add_group(
        self.coord_step_reset_after_edit,
        "次点击后回到起点",
        HelpBtn(
            "【重置循环】\n填 0 表示不自动重置；填具体次数后，路径完成相应点击次数会重新从起点开始。"
        ),
    )

    coord_advanced_notice = QLabel(
        "当前步骤已启用高级坐标步进；简易模式只显示自定义点位，切换到高级模式可修改步进路径。"
    )
    coord_advanced_notice.setWordWrap(True)
    coord_advanced_notice.setProperty("role", "statusWarn")

    self.coord_sequence_chk = QCheckBox("启用自定义点位")
    self.coord_sequence_chk.setChecked(
        config_bool(data.get("coord_sequence_en", False))
    )
    self.coord_sequence_chk.setEnabled(self.coordinate_step_available)
    self.coord_sequence_chk.setToolTip(
        "仅对直接输入坐标的点击步骤生效。开启后会按列表中的点位依次点击，坐标步进会自动忽略。"
    )
    self.coord_sequence_chk.toggled.connect(self.update_coord_sequence_ui)
    self.coord_sequence_text = QTextEdit(str(data.get("coord_sequence_points", "")))
    self.coord_sequence_text.setFixedHeight(58)
    self.coord_sequence_text.setPlaceholderText("例如：100,100; 200,180; 350,260")
    self.coord_sequence_text.setToolTip(
        "多个点用分号或换行分隔。执行时每次运行到本步骤会取下一个点。"
    )
    self.coord_sequence_end_combo = NoWheelComboBox()
    self.coord_sequence_end_combo.addItems(
        ["点完后跳过本步", "点完后停在最后一个", "点完后循环"]
    )
    fit_combo_to_contents(self.coord_sequence_end_combo)
    self.coord_sequence_end_combo.setCurrentText(
        str(data.get("coord_sequence_end_action", "点完后跳过本步"))
    )
    self.coord_sequence_batch_combo = NoWheelComboBox()
    self.coord_sequence_batch_combo.addItem("点击 1 个", "one")
    self.coord_sequence_batch_combo.addItem("点击指定数量", "count")
    self.coord_sequence_batch_combo.addItem("点完全部剩余点", "all")
    batch_mode = str(data.get("coord_sequence_batch_mode", "one"))
    batch_index = self.coord_sequence_batch_combo.findData(batch_mode)
    self.coord_sequence_batch_combo.setCurrentIndex(
        batch_index if batch_index >= 0 else 0
    )
    fit_combo_to_contents(self.coord_sequence_batch_combo)
    self.coord_sequence_batch_combo.currentIndexChanged.connect(
        self.update_coord_sequence_ui
    )
    self.coord_sequence_batch_count_edit = QLineEdit(
        str(data.get("coord_sequence_batch_count", "1"))
    )
    self.coord_sequence_batch_count_edit.setFixedWidth(70)
    self.coord_sequence_batch_count_edit.setToolTip(
        "选择“点击指定数量”时，每次进入本步骤最多连续点击多少个尚未执行的点。"
    )
    self.coord_sequence_pick_btn = QPushButton("连续取点")
    fit_text_button(self.coord_sequence_pick_btn, 88)
    self.coord_sequence_pick_btn.setToolTip(
        "打开全屏取点层，左键连续添加点位，右键或 Esc 完成。"
    )
    self.coord_sequence_pick_btn.clicked.connect(self.start_coord_sequence_pick)
    self.coord_sequence_preview_btn = QPushButton("预览")
    fit_text_button(self.coord_sequence_preview_btn, 68)
    self.coord_sequence_preview_btn.clicked.connect(self.preview_coord_sequence)
    self.coord_sequence_clear_btn = QPushButton("清空")
    fit_text_button(self.coord_sequence_clear_btn, 68)
    self.coord_sequence_clear_btn.clicked.connect(
        lambda: self.coord_sequence_text.clear()
    )

    coord_sequence_top = ResponsiveRow()
    coord_sequence_top.add_group(
        self.coord_sequence_chk,
        HelpBtn(
            "【自定义点位】\n只对直接坐标点击生效。每次执行到本步骤时按列表顺序取下一个坐标，坐标步进会自动忽略。"
        ),
    )
    coord_sequence_controls = ResponsiveRow()
    coord_sequence_controls.add_group(
        "每次进入本步骤:",
        self.coord_sequence_batch_combo,
        "数量:",
        self.coord_sequence_batch_count_edit,
        HelpBtn(
            "【每次进入本步骤】\n控制本次到达该步骤时连续消费几个自定义点。"
            "通用的“本步骤重复”在启用自定义点位时会暂时忽略，避免两个次数相乘。\n"
            "同一次进入中的相邻点仍按全局步间隔等待；完成所选批次后只进行一次成功/失败分支。"
        ),
    )
    coord_sequence_controls.add_group(
        "全部点完以后:", self.coord_sequence_end_combo
    )
    coord_sequence_controls.add_group(
        self.coord_sequence_pick_btn,
        self.coord_sequence_preview_btn,
        self.coord_sequence_clear_btn,
    )

    self.coord_sequence_details = QWidget()
    coord_sequence_details_layout = QVBoxLayout(self.coord_sequence_details)
    coord_sequence_details_layout.setContentsMargins(0, 0, 0, 0)
    coord_sequence_details_layout.setSpacing(4)
    coord_sequence_details_layout.addWidget(coord_sequence_controls)
    coord_sequence_details_layout.addWidget(self.coord_sequence_text)

    coord_sequence_box = QWidget()
    self.coord_sequence_box = coord_sequence_box
    coord_sequence_box_layout = QVBoxLayout(coord_sequence_box)
    coord_sequence_box_layout.setContentsMargins(0, 0, 0, 0)
    coord_sequence_box_layout.setSpacing(4)
    coord_sequence_box_layout.addWidget(coord_sequence_top)
    coord_sequence_box_layout.addWidget(self.coord_sequence_details)
    return CoordinateControlRows(
        every_row=coord_every_row,
        direction_row=coord_direction_row,
        point_row=coord_point_row,
        limit_row=coord_limit_row,
        reset_row=coord_reset_row,
        advanced_notice=coord_advanced_notice,
        sequence_box=coord_sequence_box,
    )
