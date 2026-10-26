# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Until-condition controls for per-step settings."""

from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from ...constants import (
    UNREACHABLE_UNTIL_LIMIT_ACTION,
    UNTIL_CONDITION_LOGICS,
    UNTIL_CONDITION_MODES,
    UNTIL_LIMIT_ACTIONS,
)
from ...task_model import config_bool, until_condition_defaults
from ..components import (
    HelpBtn,
    NoWheelComboBox,
    ResponsiveRow,
    fit_combo_to_contents,
    fit_compact_text_button,
)


def build_until_conditions(dialog, data, layout):
    self = dialog
    until_defaults = until_condition_defaults()
    until_defaults.update(data or {})
    self.until_group = QGroupBox("直到条件成立")
    self.until_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
    until_layout = QVBoxLayout(self.until_group)

    until_top_row = ResponsiveRow()
    self.until_logic_combo = NoWheelComboBox()
    self.until_logic_combo.addItems(UNTIL_CONDITION_LOGICS)
    self.until_logic_combo.setCurrentText(
        str(until_defaults.get("until_logic", "全部满足"))
    )
    fit_combo_to_contents(self.until_logic_combo)
    until_top_row.add_group("条件关系:", self.until_logic_combo)
    self.until_false_jump_edit = QLineEdit(
        str(until_defaults.get("until_false_jump", "1"))
    )
    self.until_false_jump_edit.setFixedWidth(60)
    until_top_row.add_group("未满足跳回第", self.until_false_jump_edit, "步")
    self.until_true_jump_edit = QLineEdit(
        str(until_defaults.get("until_true_jump", "0"))
    )
    self.until_true_jump_edit.setFixedWidth(60)
    self.until_true_jump_edit.setToolTip(
        "填 0 表示满足条件后继续下一步；填具体步号表示直接跳到该步。"
    )
    until_top_row.add_group(
        "满足后跳至",
        self.until_true_jump_edit,
        "步",
        HelpBtn(
            "【条件跳转】\n条件未满足时跳回指定步骤继续循环；满足后填 0 表示执行下一步，填具体步号则直接跳转。"
        ),
    )
    until_layout.addWidget(until_top_row)

    until_limit_row = ResponsiveRow()
    self.until_max_checks_edit = QLineEdit(
        str(until_defaults.get("until_max_checks", "0"))
    )
    self.until_max_checks_edit.setFixedWidth(60)
    self.until_max_checks_edit.setToolTip(
        "填 0 表示不限次数。每次执行到本步骤且条件仍未满足时计数一次。"
    )
    until_limit_row.add_group("最多检查", self.until_max_checks_edit, "次")
    self.until_max_seconds_edit = QLineEdit(
        str(until_defaults.get("until_max_seconds", "0"))
    )
    self.until_max_seconds_edit.setFixedWidth(60)
    self.until_max_seconds_edit.setToolTip(
        "填 0 表示不限时间。从本步骤本轮第一次检查开始计时。"
    )
    until_limit_row.add_group("最多等待", self.until_max_seconds_edit, "秒")
    self.until_on_limit_combo = NoWheelComboBox()
    # 只提供**可达**的两项：「按失败处理」在引擎侧没有任何实现，选了也不会执行。
    # 它仍留在 UNTIL_LIMIT_ACTIONS 里（校验继续接受，旧文件照常加载）。
    selectable = [
        action for action in UNTIL_LIMIT_ACTIONS if action != UNREACHABLE_UNTIL_LIMIT_ACTION
    ]
    self.until_on_limit_combo.addItems(selectable)
    wanted = str(until_defaults.get("until_on_limit", "继续下一步"))
    if wanted not in selectable:
        # FR-20261003-609: 旧方案存的就是这个不可达值。保留为**禁用占位项**，
        # 直接 setCurrentText 在非可编辑下拉上匹配不到会静默落回第一项，用户打开
        # 设置再保存就把磁盘值改掉了，且界面显示与保存值不一致（状态误导）。
        self.until_on_limit_combo.addItem(wanted)
        index = self.until_on_limit_combo.count() - 1
        self.until_on_limit_combo.model().item(index).setEnabled(False)
    self.until_on_limit_combo.setCurrentText(wanted)
    fit_combo_to_contents(self.until_on_limit_combo)
    until_limit_row.add_group(
        "达到上限后:",
        self.until_on_limit_combo,
        HelpBtn(
            "【检查上限】\n次数和时间填 0 表示不限。达到任一已设置的上限后，按右侧选项继续、停止或跳转。"
        ),
    )
    until_layout.addWidget(until_limit_row)

    self.until_condition_widgets = {}
    for cond_idx in range(1, 4):
        row = ResponsiveRow(horizontal_spacing=8, vertical_spacing=7)

        en_chk = QCheckBox(f"条件{cond_idx}")
        en_chk.setChecked(
            config_bool(until_defaults.get(f"until_cond{cond_idx}_en", cond_idx == 1))
        )

        mode_combo = NoWheelComboBox()
        mode_combo.addItems(UNTIL_CONDITION_MODES)
        mode_combo.setCurrentText(
            str(until_defaults.get(f"until_cond{cond_idx}_mode", "图片出现"))
        )
        fit_combo_to_contents(mode_combo)
        row.add_group(en_chk, mode_combo)

        image_edit = QLineEdit(
            str(until_defaults.get(f"until_cond{cond_idx}_image", ""))
        )
        image_edit.setPlaceholderText("图片路径")
        image_edit.setMinimumWidth(170)

        image_btn = QPushButton("图")
        fit_compact_text_button(image_btn)
        image_btn.setToolTip("选择条件要识别或对比的图片。")
        image_btn.clicked.connect(
            lambda _=False, i=cond_idx: self.select_until_condition_image(i)
        )
        row.add_group(image_edit, image_btn)

        region_edit = QLineEdit(
            str(until_defaults.get(f"until_cond{cond_idx}_region", ""))
        )
        region_edit.setPlaceholderText("区域 x,y,w,h，可空")
        region_edit.setMinimumWidth(170)

        region_btn = QPushButton("区")
        fit_compact_text_button(region_btn)
        region_btn.setToolTip("框选本条件只检测的屏幕区域。")
        region_btn.clicked.connect(
            lambda _=False, i=cond_idx: self.start_until_region_pick(i)
        )
        row.add_group(region_edit, region_btn)

        conf_edit = QLineEdit(
            str(until_defaults.get(f"until_cond{cond_idx}_conf", "0.8"))
        )
        conf_edit.setFixedWidth(52)
        conf_edit.setToolTip("图片出现/消失使用的识别相似度，通常 0.7-0.95。")

        diff_edit = QLineEdit(
            str(until_defaults.get(f"until_cond{cond_idx}_diff", "8"))
        )
        diff_edit.setFixedWidth(52)
        diff_edit.setToolTip("区域发生变化的阈值，单位约为百分比；数值越小越敏感。")

        similarity_edit = QLineEdit(
            str(until_defaults.get(f"until_cond{cond_idx}_similarity", "90"))
        )
        similarity_edit.setFixedWidth(52)
        similarity_edit.setToolTip("区域变成指定图片时要求的相似度百分比，通常 85-98。")

        stable_edit = QLineEdit(
            str(until_defaults.get(f"until_cond{cond_idx}_stable_seconds", "0"))
        )
        stable_edit.setFixedWidth(52)
        stable_edit.setToolTip("区域不变持续时间（秒）。填 0 表示差异低于阈值立即成立；>0 则需连续稳定达到该秒数。")

        row.add_group(
            "图",
            conf_edit,
            "变",
            diff_edit,
            "似",
            similarity_edit,
            "稳",
            stable_edit,
            HelpBtn(
                "【条件阈值】\n图：图片出现/消失的识别相似度。\n变：区域变化阈值，越小越敏感。\n似：区域变成指定图片时要求的相似度百分比。\n稳：区域不变持续秒数，0=立即，>0=需连续稳定该时长。"
            ),
        )

        # DESIGN: 子区域变化检测专属行——仅当 mode 为"区域发生变化"时显示。
        # 用户先框选大区域（region），再在其中框选一个子区域（sub_region，相对
        # 偏移），引擎只对子区域做帧间比较，忽略大区域其余持续变化的背景。与下方
        # 覆盖匹配（模板匹配）是不同语义：这里比较的是同一子区域前后两帧的差异。
        sub_region_row = ResponsiveRow(horizontal_spacing=8, vertical_spacing=7)
        sub_region_edit = QLineEdit(
            str(until_defaults.get(f"until_cond{cond_idx}_sub_region", ""))
        )
        sub_region_edit.setPlaceholderText("子区域 x,y,w,h，可空")
        sub_region_edit.setMinimumWidth(170)
        sub_region_edit.setToolTip(
            "只检测大区域内某个子区域的变化，坐标相对于大区域左上角，格式 x,y,w,h。留空则检测整个大区域。"
        )

        sub_region_btn = QPushButton("子区")
        fit_compact_text_button(sub_region_btn)
        sub_region_btn.setToolTip("在大区域内框选只检测变化的子区域。")
        sub_region_btn.clicked.connect(
            lambda _=False, i=cond_idx: self.start_until_sub_region_pick(i)
        )
        sub_region_row.add_group(
            "子区域:",
            sub_region_edit,
            sub_region_btn,
            HelpBtn(
                "【子区域变化】\n大区域背景可能一直在变（动画、视频），但你只关心其中某个元素是否变化。\n先框选大区域，再在这里框选子区域（限制在大区域内），引擎只比较子区域的前后差异。\n坐标相对于大区域左上角；留空则检测整个大区域。"
            ),
        )
        sub_region_row.setVisible(False)

        # DESIGN: 覆盖匹配专属控件——仅当 mode 为"覆盖匹配"时显示。
        overlay_row = ResponsiveRow(horizontal_spacing=8, vertical_spacing=7)
        overlay_image_edit = QLineEdit(
            str(until_defaults.get(f"until_cond{cond_idx}_overlay_image", ""))
        )
        overlay_image_edit.setPlaceholderText("覆盖小图路径")
        overlay_image_edit.setMinimumWidth(170)

        overlay_image_btn = QPushButton("小图")
        fit_compact_text_button(overlay_image_btn)
        overlay_image_btn.setToolTip("选择覆盖匹配用的小模板图。")
        overlay_image_btn.clicked.connect(
            lambda _=False, i=cond_idx: self.select_until_overlay_image(i)
        )
        overlay_row.add_group("小图:", overlay_image_edit, overlay_image_btn)

        overlay_rect_edit = QLineEdit(
            str(until_defaults.get(f"until_cond{cond_idx}_overlay_rect", ""))
        )
        overlay_rect_edit.setPlaceholderText("子区域 x,y,w,h")
        overlay_rect_edit.setMinimumWidth(130)

        overlay_rect_btn = QPushButton("定位")
        fit_compact_text_button(overlay_rect_btn)
        overlay_rect_btn.setToolTip("在大区域截图上框选子区域位置。")
        overlay_rect_btn.clicked.connect(
            lambda _=False, i=cond_idx: self.start_until_overlay_rect_pick(i)
        )
        overlay_row.add_group(overlay_rect_edit, overlay_rect_btn)

        overlay_scale_edit = QLineEdit(
            str(until_defaults.get(f"until_cond{cond_idx}_overlay_scale", "1.0"))
        )
        overlay_scale_edit.setFixedWidth(52)
        overlay_scale_edit.setToolTip("小图缩放比例，0.1~5.0。")

        overlay_threshold_edit = QLineEdit(
            str(until_defaults.get(f"until_cond{cond_idx}_overlay_threshold", "0.8"))
        )
        overlay_threshold_edit.setFixedWidth(52)
        overlay_threshold_edit.setToolTip("覆盖匹配阈值，0.05~1.0，越高越严格。")
        overlay_row.add_group(
            "缩放",
            overlay_scale_edit,
            "阈值",
            overlay_threshold_edit,
            HelpBtn(
                "【覆盖匹配】\n在大区域截图的指定子位置上匹配小模板图。\n小图：要匹配的小模板图片。\n子区域：小图在大区域中的位置(x,y,w,h)。\n缩放：小图缩放比例。\n阈值：匹配分数达到此值则条件满足。"
            ),
        )
        overlay_row.setVisible(False)

        self.until_condition_widgets[cond_idx] = {
            "row": row,
            "enabled": en_chk,
            "mode": mode_combo,
            "image": image_edit,
            "image_btn": image_btn,
            "region": region_edit,
            "region_btn": region_btn,
            "sub_region_row": sub_region_row,
            "sub_region": sub_region_edit,
            "sub_region_btn": sub_region_btn,
            "conf": conf_edit,
            "diff": diff_edit,
            "similarity": similarity_edit,
            "stable": stable_edit,
            "overlay_row": overlay_row,
            "overlay_image": overlay_image_edit,
            "overlay_image_btn": overlay_image_btn,
            "overlay_rect": overlay_rect_edit,
            "overlay_rect_btn": overlay_rect_btn,
            "overlay_scale": overlay_scale_edit,
            "overlay_threshold": overlay_threshold_edit,
        }
        en_chk.toggled.connect(self.update_until_condition_ui)
        mode_combo.currentTextChanged.connect(self.update_until_condition_ui)
        until_layout.addWidget(row)
        until_layout.addWidget(sub_region_row)
        until_layout.addWidget(overlay_row)

    until_note = QLabel(
        "用法：把需要反复执行的步骤放在前面，最后放本步骤。条件未满足时跳回指定步，条件满足时继续下一步或跳到指定步。区域发生变化会在第一次执行到本步骤时自动记录当前区域作为基准。"
    )
    until_note.setWordWrap(True)
    until_note.setProperty("role", "muted")
    until_layout.addWidget(until_note)
    layout.addWidget(self.until_group)
