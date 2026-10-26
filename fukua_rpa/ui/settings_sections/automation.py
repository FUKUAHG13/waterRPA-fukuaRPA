"""Dodge, speed and multi-target sections for global settings."""

from PySide6.QtWidgets import QCheckBox, QLineEdit, QPushButton, QVBoxLayout

from ..components import (
    CollapsibleSection,
    HelpBtn,
    NoWheelComboBox,
    ResponsiveRow,
    fit_combo_to_contents,
    fit_text_button,
)

# 拆分结构：build_automation_sections 是调度器，按逻辑区段调用以下私有子方法。


def _build_dodge_section(self, settings_content_layout):
    """避让设置折叠区。"""
    # DESIGN: 先挂载父链再填充子控件，确保 add_group 中的控件立即拥有 C++ 父对象。
    self.dodge_section = CollapsibleSection("避让设置", expanded=False)
    g_dodge = self.dodge_section
    dodge_rows = QVBoxLayout()
    dodge_settings_row = ResponsiveRow()
    dodge_rows.addWidget(dodge_settings_row)
    g_dodge.set_content_layout(dodge_rows)
    settings_content_layout.addWidget(g_dodge)
    self.advanced_setting_widgets.append(g_dodge)

    self.dodge_x1 = QLineEdit("100")
    self.dodge_x1.setFixedWidth(70)
    self.dodge_y1 = QLineEdit("100")
    self.dodge_y1.setFixedWidth(70)
    self.dodge_pick1_btn = fit_text_button(QPushButton("取点"), 64)
    self.dodge_pick1_btn.setProperty("variant", "tonal")
    self.dodge_pick1_btn.setToolTip("左键单击屏幕位置，直接填入避让坐标1；右键取消。")
    self.dodge_pick1_btn.clicked.connect(lambda: self.start_dodge_point_pick(1))
    dodge_settings_row.add_group(
        "坐标1 X:",
        self.dodge_x1,
        "Y:",
        self.dodge_y1,
        self.dodge_pick1_btn,
    )
    self.dodge_x2 = QLineEdit("200")
    self.dodge_x2.setFixedWidth(70)
    self.dodge_y2 = QLineEdit("100")
    self.dodge_y2.setFixedWidth(70)
    self.dodge_pick2_btn = fit_text_button(QPushButton("取点"), 64)
    self.dodge_pick2_btn.setProperty("variant", "tonal")
    self.dodge_pick2_btn.setToolTip("左键单击屏幕位置，直接填入避让坐标2；右键取消。")
    self.dodge_pick2_btn.clicked.connect(lambda: self.start_dodge_point_pick(2))
    dodge_settings_row.add_group(
        "坐标2 X:",
        self.dodge_x2,
        "Y:",
        self.dodge_y2,
        self.dodge_pick2_btn,
    )
    self.dodge_chk = QCheckBox("启用避让")
    self.double_dodge_chk = QCheckBox("二段避让")
    dodge_settings_row.add_group(self.dodge_chk, self.double_dodge_chk)
    self.dbl_wait = QLineEdit("0.015")
    self.dbl_wait.setFixedWidth(70)
    dodge_settings_row.add_group(
        "二段间隔:", self.dbl_wait, HelpBtn("【二段避让间隔】\n间隔时间，单位：秒")
    )
    self.dodge_click_combo = NoWheelComboBox()
    self.dodge_click_combo.addItem("仅移动（默认）", "none")
    self.dodge_click_combo.addItem("最终点左键单击", "left")
    self.dodge_click_combo.addItem("最终点右键单击", "right")
    fit_combo_to_contents(self.dodge_click_combo)
    dodge_settings_row.add_group(
        "避让后操作:",
        self.dodge_click_combo,
        HelpBtn(
            "【避让后操作】\n默认只移动鼠标。选择单击后，程序会在完成避让移动后，"
            "在最终避让点额外点击一次。\n启用二段避让时只点击坐标2，否则点击坐标1。"
            "请把避让点放在点击不会产生副作用的安全位置。"
        ),
    )


def _build_speed_basic_controls(self, speed_settings_row):
    """移动/按住耗时与随机延迟。"""
    self.move_spd = QLineEdit("0.0")
    self.move_spd.setFixedWidth(70)
    move_group = speed_settings_row.add_group(
        "移动(s):",
        self.move_spd,
        HelpBtn(
            "【移动耗时】\n鼠标指针从当前位置移动到目标位置所用的时间，单位为秒；填 0 表示立即移动到目标位置。"
        ),
    )
    self.move_speed_group = move_group
    self.click_hld = QLineEdit("0.04")
    self.click_hld.setFixedWidth(70)
    hold_group = speed_settings_row.add_group(
        "按住(s):",
        self.click_hld,
        HelpBtn(
            "【按住时长】\n每次点击时，从鼠标按键按下到松开之间保持的时间，单位为秒。"
        ),
    )
    self.click_hold_group = hold_group
    self.advanced_setting_widgets.extend((move_group, hold_group))
    self.move_jitter = QLineEdit("0.0")
    self.move_jitter.setFixedWidth(70)
    move_jitter_group = speed_settings_row.add_group(
        "移动随机(s):",
        self.move_jitter,
        HelpBtn(
            "【移动随机延迟】\n在“移动耗时”基础上，额外随机增加 0~该秒数的延迟，"
            "让每次移动耗时略有波动；填 0 表示不额外随机。"
        ),
    )
    self.click_hld_jitter = QLineEdit("0.0")
    self.click_hld_jitter.setFixedWidth(70)
    hold_jitter_group = speed_settings_row.add_group(
        "按住随机(s):",
        self.click_hld_jitter,
        HelpBtn(
            "【按住随机延迟】\n在“按住时长”基础上，额外随机增加 0~该秒数的延迟；"
            "填 0 表示不额外随机。"
        ),
    )
    self.advanced_setting_widgets.extend((move_jitter_group, hold_jitter_group))


def _build_speed_advanced_controls(self, speed_settings_row):
    """点击偏移、移动曲线、按键按住时长。"""
    self.click_spatial_jitter = QLineEdit("0")
    self.click_spatial_jitter.setFixedWidth(70)
    spatial_jitter_group = speed_settings_row.add_group(
        "点击偏移(±px):",
        self.click_spatial_jitter,
        HelpBtn(
            "【点击偏移】\n每次点击在目标点周围随机偏移的像素范围，0 为精确点击。"
            "例如填 3 表示每次点击坐标在目标 ±3 像素内随机浮动，模拟人类不会每次点同一个像素。"
        ),
    )
    self.move_curve_strength = QLineEdit("0")
    self.move_curve_strength.setFixedWidth(70)
    curve_strength_group = speed_settings_row.add_group(
        "移动曲线:",
        self.move_curve_strength,
        HelpBtn(
            "【移动曲线强度】\n鼠标移动路径的弯曲程度，0 为直线，越大越像人手自然移动。"
            "取值范围为 0～1。"
            "使用二次贝塞尔曲线，控制点为起终点中点加随机垂直偏移。"
            "仅在移动耗时 > 0 时生效。"
        ),
    )
    self.key_hold_duration = QLineEdit("0")
    self.key_hold_duration.setFixedWidth(70)
    key_hold_group = speed_settings_row.add_group(
        "按键按住(s):",
        self.key_hold_duration,
        HelpBtn(
            "【系统按键按住时长】\n“系统按键”指令触发时，组合键按下后保持按住该秒数再松开；"
            "0 表示按下即松开（原有行为）。"
        ),
    )
    self.advanced_setting_widgets.extend(
        (spatial_jitter_group, curve_strength_group, key_hold_group)
    )




def _build_speed_timing_controls(self, speed_settings_row):
    """步间隔、单步超时、识别间隔与自适应降频。"""
    self.settle = QLineEdit("0.5")
    self.settle.setFixedWidth(70)
    speed_settings_row.add_group(
        "间隔(s):",
        self.settle,
        HelpBtn(
            "【步间隔】\n每一步执行完毕后等待多久再进入下一步；0 表示立刻执行下一步。\n优先级低于“等待”指令：如果当前步骤是等待，或下一步实际要执行的是等待，程序会自动屏蔽等待指令前后的全局间隔。\n例如：第1步点击、第2步等待3秒、第3步点击，则第1步后不会额外加间隔，第2步后也不会额外加间隔，确保第1步后准确等待3秒再执行第3步。"
        ),
    )
    self.timeout = QLineEdit("0.0")
    self.timeout.setFixedWidth(70)
    timeout_group = speed_settings_row.add_group(
        "单步超时(s):",
        self.timeout,
        HelpBtn(
            "【单步超时】\n0 表示不设置等待上限。\n未开启“超时急停”时：达到超时会把本步骤视为失败，再按小齿轮里的失败跳过/跳至规则处理。\n开启“超时急停”时：达到超时会立即停止整个脚本和后续循环。"
        ),
    )
    self.timeout_stop_chk = QCheckBox("超时急停")
    self.timeout_stop_chk.setToolTip(
        "开启后，任意步骤达到单步超时都会立即停止全部循环，不再执行后续步骤。"
    )
    timeout_stop_group = speed_settings_row.add_group(
        self.timeout_stop_chk,
        HelpBtn(
            "【超时急停】\n开启后，任意步骤达到单步超时都会立即停止全部循环，不再执行后续步骤。"
        ),
    )
    self.advanced_setting_widgets.extend((timeout_group, timeout_stop_group))
    self.detect_delay = QLineEdit("0.1")
    self.detect_delay.setFixedWidth(70)
    speed_settings_row.add_group(
        "识别间隔(s):",
        self.detect_delay,
        HelpBtn(
            "【识别间隔】\n每次开始图片识别或重试前先等待这么多秒，用于降低 CPU 占用；填 0 表示不额外等待。\n"
            "它与“步间隔”是叠加关系：普通步骤结束后先等待步间隔，进入图片步骤后再等待识别间隔，然后才开始找图。\n"
            "只有相邻步骤是“等待”指令时，步间隔才会按等待指令的规则被屏蔽；识别间隔不会因此自动取消。"
        ),
    )
    self.adaptive_backoff_chk = QCheckBox("自适应降频")
    self.adaptive_backoff_chk.setChecked(True)
    self.adaptive_backoff_chk.setToolTip(
        "连续找不到同一目标时自动逐步放慢重试；找到目标后自动恢复。"
    )
    speed_settings_row.add_group(
        self.adaptive_backoff_chk,
        HelpBtn(
            "【自适应降频】\n开启后，如果某张图连续多次找不到，程序会在识别间隔之外额外等待一点时间，避免 CPU 一直满速空转。\n找到目标后会自动清零恢复速度。"
        ),
    )
    self.input_backend_combo = NoWheelComboBox()
    self.input_backend_combo.addItems(["默认", "SendInput"])
    fit_combo_to_contents(self.input_backend_combo)
    # DESIGN: 分组先建在速度区，settings_view 构建完“系统与快捷键”后再换父
    # （反馈 20261010-3：输入行为参数归系统区）。
    self.input_backend_group = speed_settings_row.add_group(
        "输入方式:",
        self.input_backend_combo,
        HelpBtn(
            "【输入方式】\n默认：使用常规输入注入方式，兼容绝大多数标准 Windows 程序。\n"
            "SendInput：改用 Windows SendInput 注入鼠标与键盘事件，用于少数对输入方式敏感的普通程序。\n"
            "两种方式都不能保证被游戏、反作弊程序或权限更高的程序接受；"
            "调用失败会明确报错，不会在运行中自动换回另一种方式。"
        ),
    )
    self.keyboard_scan_code_chk = QCheckBox("键盘使用扫描码")
    self.keyboard_scan_code_chk.setToolTip(
        "仅在使用 SendInput 时生效：以扫描码方式发送按键，并正确处理方向键等扩展键。"
        "个别程序只识别扫描码输入时可用。"
    )
    self.key_scan_group = speed_settings_row.add_group(
        self.keyboard_scan_code_chk,
        HelpBtn(
            "【键盘使用扫描码】\n开启后，按键改用扫描码（KEYEVENTF_SCANCODE）发送，"
            "方向键、编辑键等会带上扩展键标记；关闭时按普通虚拟键方式发送。\n"
            "仅在“输入方式”选择 SendInput 时生效；默认关闭。"
        ),
    )
    self.mouse_move_settle = QLineEdit("0")
    # UI-CONVENTION: allow fixed-geometry reason=与同区数值输入框一致的窄列宽，避免响应式行被拉伸
    self.mouse_move_settle.setFixedWidth(70)
    move_settle_group = speed_settings_row.add_group(
        "移动后等待(s):",
        self.mouse_move_settle,
        HelpBtn(
            "【移动后等待】\n点击前把鼠标移动到目标后，额外等待这么久再按下按键。\n"
            "0 表示不等待；它独立于“移动耗时”和“间隔”，用于个别程序需要一点时间确认鼠标到位的情况。"
        ),
    )
    self.advanced_setting_widgets.extend(
        (self.input_backend_group, self.key_scan_group, move_settle_group)
    )


def _build_speed_behavior_controls(self, speed_settings_row):
    """窗口激活、画面唤醒、播放倍速。"""
    self.activate_before_click_chk = QCheckBox("点击前激活目标窗口")
    self.activate_before_click_chk.setChecked(False)
    self.activate_before_click_chk.setToolTip(
        "真实点击前尽力把目标窗口激活到前台，避免部分应用忽略发给非前台窗口的点击。"
    )
    # DESIGN: 分组先建在速度区，settings_view 构建完“系统与快捷键”后再换父
    # （反馈 20261010-3：输入行为参数归系统区）。
    self.activate_before_click_group = speed_settings_row.add_group(
        self.activate_before_click_chk,
        HelpBtn(
            "【点击前激活目标窗口】\n开启后，每次真实鼠标点击前会尽力把目标窗口激活到前台，"
            "避免部分应用忽略发给非前台窗口的点击。\n"
            "默认关闭：某些流程需要目标窗口保持非前台（例如同时操作多个窗口），强制激活会破坏这些场景。"
        ),
    )
    self.advanced_setting_widgets.append(self.activate_before_click_group)
    self.scene_wake_chk = QCheckBox("画面变化唤醒")
    self.scene_wake_chk.setChecked(True)
    self.scene_wake_sensitivity_combo = NoWheelComboBox()
    self.scene_wake_sensitivity_combo.addItem("稳妥", "conservative")
    self.scene_wake_sensitivity_combo.addItem("均衡（推荐）", "balanced")
    self.scene_wake_sensitivity_combo.addItem("灵敏", "sensitive")
    fit_combo_to_contents(self.scene_wake_sensitivity_combo)
    scene_wake_group = speed_settings_row.add_group(
        self.scene_wake_chk,
        self.scene_wake_sensitivity_combo,
        HelpBtn(
            "【画面变化唤醒】\n仅在“自适应降频”产生的额外等待期间生效。"
            "程序优先使用 DLL 的 DXGI 变化通知，不复制整张屏幕；环境不支持时自动回退到低分辨率分块指纹。"
            "发现变化后，会优先探测常用倍率和少量轮换倍率，"
            "随后仍保留原本的完整范围识别，因此不会因为开启本功能而漏掉其他倍率。\n"
            "“灵敏”响应更积极，也更容易被动画或状态提示唤醒；不适用于“全部匹配”和同点点击上限。"
        ),
    )
    self.advanced_setting_widgets.append(scene_wake_group)
    self.playback_speed = QLineEdit("1.0")
    self.playback_speed.setFixedWidth(70)
    speed_settings_row.add_group(
        "播放倍速:",
        self.playback_speed,
        HelpBtn("【播放倍速】 用于缩放录制脚本中的等待时间，> 1 为加速，< 1 为减速"),
    )


def _build_multi_target_section(self, settings_content_layout):
    """多目标点击折叠区。"""
    # 4. 多目标点击
    # DESIGN: 先挂载父链再填充子控件。
    self.multi_target_section = CollapsibleSection("多目标点击")
    g_multi = self.multi_target_section
    multi_settings_row = ResponsiveRow()
    multi_layout = QVBoxLayout()
    multi_layout.addWidget(multi_settings_row)
    g_multi.set_content_layout(multi_layout)
    settings_content_layout.addWidget(g_multi)
    self.advanced_setting_widgets.append(g_multi)

    self.multi_mode_combo = NoWheelComboBox()
    self.multi_mode_combo.addItems(["快速一个", "全部匹配"])
    fit_combo_to_contents(self.multi_mode_combo)
    self.multi_mode_combo.currentTextChanged.connect(self.update_multi_target_ui)
    multi_settings_row.add_group(
        "目标模式:",
        self.multi_mode_combo,
        HelpBtn(
            "【目标模式】\n快速一个：走最快路径，找到第一个达到相似度阈值的位置后立即点击；不保证它是全屏及全部缩放结果中相似度最高的位置，适合普通单目标脚本。\n全部匹配：执行更完整的峰值搜索，一次找出所有超过相似度阈值的目标并逐个点击，CPU压力更高。"
        ),
    )
    self.multi_order_combo = NoWheelComboBox()
    self.multi_order_combo.addItems(
        ["从上到下", "从左到右", "从右到左", "距离鼠标最近优先", "随机顺序"]
    )
    fit_combo_to_contents(self.multi_order_combo)
    multi_settings_row.add_group(
        "点击顺序:",
        self.multi_order_combo,
        HelpBtn(
            "【点击顺序】\n仅在目标模式为“全部匹配”时生效。程序会先识别本轮截图中的全部目标，再按此顺序点击。"
        ),
    )
    self.multi_order_b_combo = NoWheelComboBox()
    self.multi_order_b_combo.addItems(
        ["不启用", "从上到下", "从左到右", "从右到左", "从下到上"]
    )
    fit_combo_to_contents(self.multi_order_b_combo)
    multi_settings_row.add_group(
        "第二排序:",
        self.multi_order_b_combo,
        HelpBtn(
            "【第二排序条件】\n仅在“点击顺序”无法分出先后时使用（例如主顺序为“从上到下”，"
            "同一行的目标再按此处选择的方向决定先后）。主顺序为“随机顺序”时本项不生效。"
        ),
    )
    self.multi_collect_first_edit = QLineEdit("0")
    self.multi_collect_last_edit = QLineEdit("0")
    self.multi_collect_max_edit = QLineEdit("0")
    self.multi_collect_targets_edit = QLineEdit("0")
    # UI-CONVENTION: allow fixed-geometry reason=多目标数值输入框统一 70px 窄列宽，与速度控制区一致
    self.multi_collect_first_edit.setFixedWidth(70)
    self.multi_collect_last_edit.setFixedWidth(70)
    # UI-CONVENTION: allow fixed-geometry reason=多目标数值输入框统一 70px 窄列宽，与速度控制区一致
    self.multi_collect_max_edit.setFixedWidth(70)
    self.multi_collect_targets_edit.setFixedWidth(70)
    multi_settings_row.add_group(
        "首次命中缓冲(s):",
        self.multi_collect_first_edit,
        HelpBtn(
            "【首次命中缓冲】\n“全部匹配”下，从首次发现目标开始至少再收集这么久，"
            "给稍后才出现的目标留出时间。0 表示关闭该条件。旧方案默认 0，升级后不会变慢。"
        ),
    )
    multi_settings_row.add_group(
        "末位缓冲(s):",
        self.multi_collect_last_edit,
        HelpBtn(
            "【末位缓冲】\n从最后一次发现“新目标”开始，连续这么久没有新目标出现就结束收集。"
            "同一个目标反复被识别不会重置计时。0 表示关闭该条件。\n"
            "两个缓冲同时启用时按“较晚满足”的那个结束；相邻目标出现间隔超过末位缓冲时，"
            "仍可能被分成两批。"
        ),
    )
    multi_settings_row.add_group(
        "最长收集时间(s):",
        self.multi_collect_max_edit,
        HelpBtn(
            "【最长收集时间】\n0 = 不设上限：由“单步超时”控制结束；单步超时也为 0 时完全不设限"
            "（停止热键仍可中断）。\n大于 0 时为硬上限：从首次发现目标开始计算，防止画面持续变化"
            "导致久等；到达上限仍收不齐按原有超时处理，不会把未收齐当成成功。"
        ),
    )
    multi_settings_row.add_group(
        "最多点击目标数:",
        self.multi_collect_targets_edit,
        HelpBtn(
            "【本步最多点击目标数】\n0 = 点击收集到的全部目标；1 = 只点击排序后的第一个；"
            "N = 最多点击排序后的前 N 个。按“目标数”计数：双击一个目标仍算一个目标。"
        ),
    )
    self.multi_collect_partial_chk = QCheckBox("目标不足时点击现有目标并完成")
    self.multi_collect_partial_chk.setChecked(True)
    # “目标数 > 0”才使该项有意义，输入变化时同步启用态。
    self.multi_collect_targets_edit.textChanged.connect(
        self.update_multi_target_ui
    )
    self.multi_order_combo.currentTextChanged.connect(self.update_multi_target_ui)
    multi_settings_row.add_group(
        self.multi_collect_partial_chk,
        HelpBtn(
            "【目标不足时点击现有目标并完成】\n仅在“最多点击目标数”大于 0 时有效。\n"
            "开启：收集到的目标少于设定数量时，点击现有的这些目标即完成本步。\n"
            "关闭：继续等待，直到达到设定数量或超时（最长收集时间为 0 且单步超时也为 0 时"
            "会一直等待，停止热键可中断）；不会少点后当作成功。"
        ),
    )


def build_automation_sections(host, settings_content_layout):
    """构建避让、速度与多目标设置区。

    `self = host` 仅为沿用原主窗口构建代码的书写习惯，控件实际挂载到主窗口 host。
    本函数是调度器，按逻辑区段调用私有子方法完成构建。
    """
    self = host

    _build_dodge_section(self, settings_content_layout)

    # 3. 速度控制
    # DESIGN: 先挂载父链再填充子控件，确保子方法中创建的控件立即拥有 C++ 父对象，
    # 避免 Python GC 在无父对象期间回收 Qt 控件。
    g2 = CollapsibleSection("速度控制")
    speed_rows = QVBoxLayout()
    speed_settings_row = ResponsiveRow()
    speed_rows.addWidget(speed_settings_row)
    g2.set_content_layout(speed_rows)
    settings_content_layout.addWidget(g2)

    _build_speed_basic_controls(self, speed_settings_row)
    _build_speed_advanced_controls(self, speed_settings_row)
    _build_speed_timing_controls(self, speed_settings_row)
    _build_speed_behavior_controls(self, speed_settings_row)

    _build_multi_target_section(self, settings_content_layout)
