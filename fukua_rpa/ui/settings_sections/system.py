"""System, logging and credential sections for global settings."""

from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ...config_store import normalize_profile_backup_count
from ...constants import DEFAULT_PROFILE_BACKUP_COUNT, MAX_PROFILE_BACKUP_COUNT
from ...usage_notices import first_use_notices_enabled
from ..components import (
    CollapsibleSection,
    HelpBtn,
    NoWheelComboBox,
    NoWheelSpinBox,
    ResponsiveRow,
    fit_combo_to_contents,
    fit_text_button,
)

# 拆分结构：build_system_sections 是调度器，按逻辑区段调用以下私有子方法。
# 每个子方法只负责一组相关控件的创建和挂载，不改变公开接口或运行时行为。


def _build_hotkey_and_failsafe_controls(self, system_settings_row):
    """启动/停止热键与急停方式（热键编辑框由宿主态创建，C-1）。"""
    self.start_key_btn = fit_text_button(QPushButton("录入"), 68)
    self.start_key_btn.clicked.connect(self.capture_start_hotkey)

    self.stop_key_btn = fit_text_button(QPushButton("录入"), 68)
    self.stop_key_btn.clicked.connect(self.capture_stop_hotkey)
    hotkey_help = "【启动/停止热键】\n可以直接点“录入”后按下想用的按键或组合键。\n为避免打字时误启动脚本，启动/停止不接受裸字母、裸数字、空格、回车这类容易误触的单键；需要用 Ctrl/Alt/Shift/Win 组合，或使用 F1-F12 等功能键。\n裸字母/数字建议只用于下方“按键映射模式”。"
    system_settings_row.add_group(
        "启动热键:", self.hotkey_start_edit, self.start_key_btn, HelpBtn(hotkey_help)
    )
    system_settings_row.add_group(
        "停止热键:", self.hotkey_stop_edit, self.stop_key_btn, HelpBtn(hotkey_help)
    )

    self.tm_failsafe = QCheckBox("任务管理器急停")
    self.tm_failsafe.setChecked(True)
    self.tr_failsafe = QCheckBox("右上角急停")
    self.tr_failsafe.setChecked(True)
    self.key_failsafe = QCheckBox("ESC/中键急停")
    self.key_failsafe.setChecked(True)
    system_settings_row.add_group(
        self.tm_failsafe,
        self.tr_failsafe,
        self.key_failsafe,
        HelpBtn(
            "【急停方式】\n可同时启用多种急停方式。任务管理器急停用于窗口失去响应时辅助停止；右上角和 ESC/中键用于运行期间快速中止。"
        ),
    )


def _build_profile_hotkey_controls(self, system_settings_row):
    """Global profile launch slots; intentionally not stored in one profile."""

    self.profile_hotkey_mode_chk = QCheckBox("多方案快捷启动")
    self.profile_hotkey_mode_chk.setChecked(
        self.profile_hotkey_controller.enabled()
    )
    self.profile_hotkey_mode_chk.toggled.connect(
        self.profile_hotkey_controller.set_enabled
    )
    configure_btn = fit_text_button(QPushButton("配置方案快捷键"), 118)
    configure_btn.setProperty("variant", "tonal")
    configure_btn.clicked.connect(self.profile_hotkey_controller.show_dialog)
    self.profile_hotkey_status_label = QLabel("")
    self.profile_hotkey_status_label.setProperty("role", "muted")
    group = system_settings_row.add_group(
        self.profile_hotkey_mode_chk,
        configure_btn,
        self.profile_hotkey_status_label,
        HelpBtn(
            "【多方案快捷启动】\n"
            "在同一个主程序中，为多个方案分别绑定系统级启动热键和可选停止热键。"
            "触发后直接运行对应方案的快照，不切换当前编辑页面，也不会多开程序或同时写配置。\n"
            "同一时间只允许一个脚本或连点器运行；已有任务运行时，其他启动热键会被拦截。"
        ),
    )
    self.profile_hotkey_group = group
    self.advanced_setting_widgets.append(group)
    self.profile_hotkey_controller.refresh_status()


def _build_log_controls(self, system_settings_row):
    """日志级别、内容菜单、文件/界面输出（控件由宿主态创建，C-1；此处只入列）。"""
    log_output_group = system_settings_row.add_group(
        "日志级别:",
        self.log_level_combo,
        self.log_content_btn,
        self.log_file_chk,
        self.log_ui_chk,
        HelpBtn(
            "【日志输出】\n"
            "简易：只保留运行进度、步骤结果、分支、停止原因和错误，适合日常使用。\n"
            "详细：在简易内容上增加本地时间、目标/动作细节、等待信息和性能摘要，适合排查流程。\n"
            "完全：再记录运行与步骤参数、识别/截图/匹配/点击等阶段耗时、缓存与回退统计，主要用于软件调试。\n"
            "内容：逐项勾选后自动切换为自定义；菜单底部可恢复任一预设。错误、急停和严重警告始终输出。\n"
            "写入文件与界面显示可以分别关闭；完全日志信息量和界面刷新开销最大。"
        ),
    )
    self.log_output_group = log_output_group
    self.advanced_setting_widgets.append(log_output_group)


def _build_privacy_controls(self, system_settings_row):
    """日志隐私保护（控件由宿主态创建，C-1；此处只入列）。"""
    log_privacy_group = system_settings_row.add_group(
        self.log_privacy_chk,
        self.log_privacy_status_label,
        HelpBtn(
            "【日志隐私保护】\n"
            "默认开启：日志隐藏普通输入文字、变量值、窗口条件、完整路径和错误详情。\n"
            "关闭后，详细/完全日志可显示这些普通调试信息，适合仅在自己的设备上排查问题；"
            "分享日志前请重新开启。\n"
            "凭据值和“输入秘密文本”的原文始终不会记录；诊断包与崩溃报告也始终保持脱敏。"
        ),
    )
    self.advanced_setting_widgets.append(log_privacy_group)




def _build_display_and_indicator_controls(self, display_row, workflow_row):
    """窗口行为、运行状态提示、点击提示与风险提示。"""
    # C-1：mini_chk / top_chk 由宿主态创建（底栏使用），此处不再创建。
    self.run_status_chk = QCheckBox("运行状态提示")
    self.run_status_chk.setChecked(True)
    self.run_status_pos_combo = NoWheelComboBox()
    self.run_status_pos_combo.addItems(["右上角", "右下角"])
    fit_combo_to_contents(self.run_status_pos_combo)
    run_status_group = display_row.add_group(
        self.run_status_chk,
        "提示位置:",
        self.run_status_pos_combo,
        HelpBtn(
            "【运行状态提示】\n脚本运行时在屏幕角落显示“脚本正在执行中”、当前循环、当前步骤和已运行时间。"
        ),
    )
    self.run_status_group = run_status_group
    self.advanced_setting_widgets.append(run_status_group)
    self.click_indicator_chk = QCheckBox("点击位置提示")
    self.click_indicator_chk.setChecked(True)
    self.click_indicator_chk.setToolTip(
        "脚本执行点击、拖拽或悬停时，在屏幕上短暂标出实际位置。"
    )
    display_row.add_group(
        self.click_indicator_chk,
        HelpBtn(
            "【点击位置提示】\n开启后，脚本每次点击、拖拽结束或鼠标悬停时，会在屏幕上短暂显示一个定位标记，方便确认刚刚操作了哪里。\n关闭后不显示定位标记，脚本执行逻辑不变。"
        ),
    )
    self.mouse_trail_chk = QCheckBox("显示鼠标移动拖影")
    self.mouse_trail_chk.setChecked(False)
    mouse_trail_group = display_row.add_group(
        self.mouse_trail_chk,
        HelpBtn(
            "【鼠标移动拖影】\n开启后，普通脚本造成的前台鼠标移动会留下短暂的蓝色半透明拖影，"
            "便于观察移动方向；只改变视觉提示，不改变鼠标路径、速度或点击位置。\n"
            "适用于坐标/图片点击、拖拽和鼠标悬停；后台窗口点击不会移动真实鼠标，因此不显示。"
        ),
    )
    self.advanced_setting_widgets.append(mouse_trail_group)
    self.risk_tip_chk = QCheckBox("运行前风险提示")
    self.risk_tip_chk.setChecked(
        self.settings.value("risk_tip_enabled", True, type=bool)
    )
    self.risk_tip_chk.setToolTip(
        "开启后，运行含循环/等待风险的方案时会弹出确认提示；"
        "确认过的风险类型按方案记住（重启后仍然有效），不再重复提示，"
        "出现新的风险类型时会再次提示。关闭后不再提示。"
    )
    risk_tip_group = workflow_row.add_group(
        self.risk_tip_chk,
        HelpBtn(
            "【运行前风险提示】\n"
            "开启后，运行含无限循环或长期等待风险的方案时会弹出确认提示。\n"
            "确认过的风险类型会按当前方案记住（重启软件后仍然有效），"
            "同一类型不再重复提示；检测到新的风险类型时会再次提示。\n"
            "关闭后不显示风险提示，脚本直接运行。"
        ),
    )
    self.advanced_setting_widgets.append(risk_tip_group)
    self.first_use_notices_chk = QCheckBox("首次使用提示")
    self.first_use_notices_chk.setChecked(
        first_use_notices_enabled(self.settings)
    )
    self.first_use_notices_chk.setToolTip(
        "控制“首次使用”类自动提示（首次运行学习急停、透明图片、连点器中键、"
        "日志性能等）。关闭后这些提示不再自动弹出，但错误提示和操作确认不受影响；"
        "“使用注意”窗口随时可以手动查阅。"
    )
    first_use_group = display_row.add_group(
        self.first_use_notices_chk,
        HelpBtn(
            "【首次使用提示】\n"
            "开启后，某些功能第一次被用到时会弹出一次说明（例如首次运行学习急停、"
            "选择透明图片、连点器使用中键、切换到详细日志）。\n"
            "关闭后这些说明不再自动弹出；错误提示、删除/覆盖确认和完整迁移包的隐私确认"
            "仍然会出现。已经自动看过的提示不会因为关闭再开启而重复弹出，"
            "未真正看过的提示会保留到下次对应入口。\n"
            "任何说明都可以通过设置中的“使用注意”按钮随时手动查阅。"
        ),
    )
    self.advanced_setting_widgets.append(first_use_group)


def _build_execution_controls(self, workflow_row, diagnostic_row):
    """起始步、循环范围、限时停止、资源监测。"""
    self.start_step_edit = QLineEdit("1")
    self.start_step_edit.setFixedWidth(60)
    self.start_step_group = workflow_row.add_group(
        "从第",
        self.start_step_edit,
        "步开始",
        HelpBtn(
            "【从第X步开始执行】\n默认 1。启动脚本后每轮循环都从这里开始；成功/失败跳至仍按列表中的实际步号计算。"
        ),
    )
    self.advanced_setting_widgets.append(self.start_step_group)

    self.loop_start_round_edit = QLineEdit("1")
    self.loop_start_round_edit.setFixedWidth(60)
    self.loop_start_round_edit.setValidator(
        QIntValidator(0, 1000000, self.loop_start_round_edit)
    )
    self.loop_end_round_edit = QLineEdit("0")
    self.loop_end_round_edit.setFixedWidth(60)
    self.loop_end_round_edit.setValidator(
        QIntValidator(0, 1000000, self.loop_end_round_edit)
    )
    self.loop_range_group = workflow_row.add_group(
        "脚本从第",
        self.loop_start_round_edit,
        "次循环开始",
        "到第",
        self.loop_end_round_edit,
        "次循环停止",
        HelpBtn(
            "【全局循环范围】\n从第几次循环开始真正执行步骤；前面的循环轮次会直接跳过。\n停止循环填 0 表示不限；填 5 表示执行到第 5 次循环后结束。\n这个设置和“从第X步开始”不同：它控制第几轮循环生效。"
        ),
    )
    self.advanced_setting_widgets.append(self.loop_range_group)

    self.time_limit_stop_mode_combo = NoWheelComboBox()
    self.time_limit_stop_mode_combo.addItem("本轮结束后停止", "finish_loop")
    self.time_limit_stop_mode_combo.addItem("尽快停止当前步骤", "immediate")
    fit_combo_to_contents(self.time_limit_stop_mode_combo)
    time_limit_stop_group = workflow_row.add_group(
        "限时停止:",
        self.time_limit_stop_mode_combo,
        HelpBtn(
            "【限时停止方式】\n只在循环模式为“指定时间”时生效。\n"
            "本轮结束后停止（默认）：时间到后允许当前脚本循环执行完，再停止；"
            "如果本轮包含无限重复、禁止跳过或无上限条件循环，实际停止时间可能明显晚于设定时间。\n"
            "尽快停止当前步骤：时间到后在下一次安全取消检查时停止当前动作，通常为几十毫秒，"
            "但不承诺操作系统层面的瞬间强制结束。\n手动停止和急停始终尽快生效，不受此项影响。"
        ),
    )
    self.advanced_setting_widgets.append(time_limit_stop_group)

    self.cpu_refresh_combo = NoWheelComboBox()
    self.cpu_refresh_combo.addItem("0.5 秒", 500)
    self.cpu_refresh_combo.addItem("1 秒", 1000)
    self.cpu_refresh_combo.addItem("2 秒", 2000)
    self.cpu_refresh_combo.addItem("3 秒", 3000)
    self.cpu_refresh_combo.addItem("5 秒", 5000)
    self.cpu_refresh_combo.addItem("关闭", 0)
    self.cpu_refresh_combo.setCurrentIndex(self.cpu_refresh_combo.findData(2000))
    fit_combo_to_contents(self.cpu_refresh_combo)
    self.cpu_refresh_combo.currentIndexChanged.connect(
        self.window_performance_controller.apply_resource_monitor_interval
    )
    cpu_refresh_group = diagnostic_row.add_group(
        "资源监测:",
        self.cpu_refresh_combo,
        HelpBtn(
            "【资源监测刷新】\n控制主界面右下角 CPU 信息的刷新频率。"
            "采样使用非阻塞接口，不会等待一个完整统计周期。\n"
            "默认每 2 秒刷新；选择“关闭”会停止监测定时器。\n"
            "窗口移动或缩放期间会自动暂停采样，结束后恢复。\n"
            "启用独立 Runner 后，脚本运行期间会自动采样 Runner 子进程，"
            "仍保留单核口径。"
        ),
    )
    self.advanced_setting_widgets.append(cpu_refresh_group)


def _build_runner_and_appearance_controls(self, diagnostic_row, display_row):
    """独立 Runner、界面倍率、主题、步骤显示选项。"""
    self.isolated_runner_chk = QCheckBox("启用独立 Runner（实验）")
    self.isolated_runner_chk.setChecked(False)
    isolated_runner_group = diagnostic_row.add_group(
        self.isolated_runner_chk,
        HelpBtn(
            "【独立 Runner（实验）】\n"
            "默认关闭，软件沿用已验证的进程内执行线程。开启后，每次运行脚本会启动一个独立子进程，"
            "界面与执行器互相隔离；普通急停无响应时，主程序可强制结束子进程。\n"
            "日志、运行状态、点击提示、调试暂停、脚本弹窗和本次缩放记忆会通过带版本号的本机 IPC 传递。\n"
            "实验模式会增加每次启动脚本的准备时间，也可能遇到杀毒软件拦截或个别 Windows 环境兼容问题；"
            "遇到异常请关闭此项并使用默认模式。启动失败会明确提示，不会静默切换后端。"
        ),
    )
    self.isolated_runner_group = isolated_runner_group
    self.advanced_setting_widgets.append(isolated_runner_group)
    # C-1：ui_scale_edit / theme_mode_combo 由宿主态创建，此处只入列。
    display_row.add_group(
        "界面倍率(%):",
        self.ui_scale_edit,
        HelpBtn(
            "【界面倍率】\n用于适配不同分辨率或缩放习惯的显示屏。\n100 表示原始大小；例如 125 会把字体和多数按钮/输入框放大到 125%。\n倍率过大时窗口内容需要更多空间，可以手动拉大窗口。"
        ),
    )
    display_row.add_group(
        "界面主题:",
        self.theme_mode_combo,
    )
    self.show_step_notes_chk = QCheckBox("显示步骤备注")
    self.show_step_notes_chk.setChecked(self.workflow_editor.show_step_notes)
    self.show_step_notes_chk.setToolTip(
        "开启后，每个步骤的值栏分成左右两部分，右侧显示该步骤的备注。"
    )
    self.show_step_notes_chk.toggled.connect(
        self.workflow_editor.set_show_step_notes
    )
    display_row.add_group(
        self.show_step_notes_chk,
        HelpBtn(
            "【显示步骤备注】\n开启后，主界面每个步骤的值栏会一分为二：左侧保留原值，右侧显示备注。\n"
            "关闭只隐藏备注栏，不会删除已填写的备注。"
        ),
    )
    self.show_hidden_steps_chk = QCheckBox("显示隐藏步骤")
    self.show_hidden_steps_chk.setChecked(self.workflow_editor.show_hidden_steps)
    self.show_hidden_steps_chk.setToolTip(
        "临时显示被隐藏的步骤，并将整行淡化；只在高级模式中可用。"
    )
    self.show_hidden_steps_chk.toggled.connect(
        self.workflow_editor.set_show_hidden_steps
    )
    show_hidden_group = display_row.add_group(
        self.show_hidden_steps_chk,
        HelpBtn(
            "【显示隐藏步骤】\n开启后，主界面临时显示被隐藏的步骤，并将文字、图标、输入框和边框整体淡化。\n"
            "隐藏状态不会影响执行、编号、跳转、导出或预览。"
        ),
    )
    self.advanced_setting_widgets.append(show_hidden_group)



def _build_sound_and_follow_controls(self, workflow_row, display_row):
    """调整跟随与声音提示全局默认。"""
    self.step_adjust_follow_chk = QCheckBox("调整跟随")
    self.step_adjust_follow_chk.setChecked(True)
    self.step_adjust_follow_chk.setToolTip(
        "默认开启：删除/新增/移动某一步后，指向它的其他跳转会保持有效（按身份跟随）；"
        "关闭后，这些跳转会被清空并改为顺序执行。"
    )
    adjust_follow_group = workflow_row.add_group(
        self.step_adjust_follow_chk,
        HelpBtn(
            "【调整跟随】\n开启后，删除、新增或移动一个步骤时，指向它的跳转"
            "（成功/失败/循环/直到等）会自动保持有效、流程结构不被打断。\n"
            "关闭后，这些跳转会被清空并退回顺序执行（与旧版一致）。"
        ),
    )
    self.advanced_setting_widgets.append(adjust_follow_group)

    # 声音提示：全局默认音频 + 播放时长，均为高级模式专属。
    # 每步还可在“声音提示”步骤的小齿轮中单独覆盖（优先级更高）。
    self.sound_alert_audio_path_edit = QLineEdit("")
    self.sound_alert_audio_path_edit.setPlaceholderText("留空则使用系统提示音")
    self.sound_alert_audio_path_edit.setMinimumWidth(280)
    sound_browse_btn = fit_text_button(QPushButton("浏览..."), 84)

    def _browse_sound_alert_audio():
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "选择声音提示音频",
            "",
            "音频文件 (*.wav *.mp3 *.ogg *.flac *.m4a);;所有文件 (*.*)",
        )
        if chosen:
            self.sound_alert_audio_path_edit.setText(chosen)

    sound_browse_btn.clicked.connect(_browse_sound_alert_audio)
    sound_alert_group = display_row.add_group(
        "声音提示音频:",
        self.sound_alert_audio_path_edit,
        sound_browse_btn,
        HelpBtn(
            "【声音提示音频（全局默认）】\n“声音提示”步骤默认播放系统提示音。在此填入"
            "本地音频文件（.wav 直接播放；.mp3 等用系统解码播放），作为所有声音提示步骤"
            "的默认音频；留空或文件无效时回退到系统蜂鸣。\n"
            "每个“声音提示”步骤还可在其小齿轮中单独指定音频，单独指定的优先级高于此处。"
        ),
    )
    self.advanced_setting_widgets.append(sound_alert_group)

    self.sound_alert_play_seconds_edit = QLineEdit("30")
    self.sound_alert_play_seconds_edit.setFixedWidth(72)
    self.sound_alert_play_seconds_edit.setValidator(
        QIntValidator(0, 600, self.sound_alert_play_seconds_edit)
    )
    sound_seconds_group = display_row.add_group(
        "播放时长(秒):",
        self.sound_alert_play_seconds_edit,
        HelpBtn(
            "【声音提示播放时长（全局默认）】\n声音提示音频的播放时长上限（秒）。"
            "音频短于该值时完整播放，长于此值时自动截断到该长度；填 0 表示不截断。\n"
            "默认 30 秒。每个“声音提示”步骤还可在其小齿轮中单独设置，单独设置的优先级高于此处。"
        ),
    )
    self.advanced_setting_widgets.append(sound_seconds_group)




def _build_backup_section(self, settings_content_layout):
    """方案备份折叠区。"""
    self.profile_backup_section = CollapsibleSection("方案备份", expanded=False)
    backup_layout = QVBoxLayout()
    backup_row = ResponsiveRow()
    self.profile_backup_count_spin = NoWheelSpinBox()
    self.profile_backup_count_spin.setRange(0, MAX_PROFILE_BACKUP_COUNT)
    self.profile_backup_count_spin.setValue(
        normalize_profile_backup_count(
            self.settings.value(
                "profile_backup_count", DEFAULT_PROFILE_BACKUP_COUNT
            )
        )
    )
    self.profile_backup_count_spin.setToolTip(
        "总保留数量包含当前最新副本。0 表示完全关闭并删除备份；"
        "1 表示只保留最新恢复副本；N 表示最新副本加 N-1 份历史记录。"
    )
    backup_apply_btn = fit_text_button(QPushButton("应用数量"), 84)
    backup_apply_btn.setProperty("variant", "tonal")
    backup_apply_btn.clicked.connect(self.apply_profile_backup_count)
    backup_export_btn = fit_text_button(QPushButton("导出备份"), 84)
    backup_export_btn.setToolTip(
        "导出当前恢复副本和历史 JSON；不会复制模板图片。"
        "跨电脑迁移可运行方案请使用上方“完整迁移包”。"
    )
    backup_export_btn.clicked.connect(self.export_profile_backups)
    backup_restore_btn = fit_text_button(QPushButton("恢复备份"), 84)
    backup_restore_btn.clicked.connect(self.restore_profile_backup)
    backup_clear_btn = fit_text_button(QPushButton("清除备份"), 84)
    backup_clear_btn.setProperty("variant", "dangerGhost")
    backup_clear_btn.clicked.connect(self.clear_profile_backups)
    backup_row.add_group(
        "自动保留:",
        self.profile_backup_count_spin,
        "份",
        backup_apply_btn,
        HelpBtn(
            "【方案自动备份】\n默认保留 3 份：当前最新恢复副本加 2 份历史记录。\n"
            "设为 0 会完全关闭并清理备份；设为 1 只保留最新恢复副本；"
            "最多可保留 50 份。该设置只影响自动备份，不影响普通导出或完整迁移包。\n"
            "备份包含方案文字、设置和路径，但不包含凭据秘密值，也不复制模板图片。"
        ),
    )
    backup_row.add_group(
        backup_export_btn,
        backup_restore_btn,
        backup_clear_btn,
    )
    backup_layout.addWidget(backup_row)
    self.profile_backup_status_label = QLabel("")
    self.profile_backup_status_label.setWordWrap(True)
    self.profile_backup_status_label.setProperty("role", "muted")
    backup_layout.addWidget(self.profile_backup_status_label)
    self.profile_backup_section.set_content_layout(backup_layout)
    settings_content_layout.addWidget(self.profile_backup_section)
    self.advanced_setting_widgets.append(self.profile_backup_section)
    self.refresh_profile_backup_status()


def _build_credential_section(self, settings_content_layout):
    """凭据库折叠区。"""
    self.credential_section = CollapsibleSection("凭据库", expanded=False)
    credential_section = self.credential_section
    credential_layout = QVBoxLayout()
    credential_row = ResponsiveRow()
    self.credential_combo = NoWheelComboBox()
    self.credential_combo.setMinimumWidth(220)
    credential_row.add_group("凭据名称:", self.credential_combo)
    credential_set_btn = QPushButton("新增 / 修改")
    credential_set_btn.setProperty("variant", "tonal")
    credential_set_btn.clicked.connect(self.settings_controller.edit_credential)
    credential_delete_btn = QPushButton("删除")
    credential_delete_btn.setProperty("variant", "dangerGhost")
    credential_delete_btn.clicked.connect(self.settings_controller.delete_credential)
    credential_row.add_group(
        credential_set_btn,
        credential_delete_btn,
        HelpBtn(
            "【凭据库】\n用于“输入秘密文本”步骤。秘密内容由 Windows DPAPI 按当前用户加密，"
            "不会写入脚本方案、任何导出文件或日志。\n脚本中只保存凭据名称；换电脑后需要在新电脑重新创建同名凭据。"
        ),
    )
    credential_layout.addWidget(credential_row)
    credential_note = QLabel(
        "凭据只在当前 Windows 用户下可解密；程序不会显示或导出现有秘密内容。"
    )
    credential_note.setWordWrap(True)
    credential_note.setProperty("role", "muted")
    credential_layout.addWidget(credential_note)
    credential_section.set_content_layout(credential_layout)
    settings_content_layout.addWidget(credential_section)
    self.advanced_setting_widgets.append(credential_section)
    self.settings_controller.refresh_credential_names()


def _create_settings_box(settings_content_layout, title, row_role, *, expanded=True):
    """Create one responsive top-level settings category."""

    section = CollapsibleSection(title, expanded=expanded)
    section_layout = QVBoxLayout()
    row = ResponsiveRow()
    row.setProperty("rowRole", row_role)
    section_layout.addWidget(row)
    section.set_content_layout(section_layout)
    settings_content_layout.addWidget(section)
    return section, row




def build_system_sections(host, settings_content_layout):
    """构建系统、日志与凭据设置区。

    `self = host` 仅为沿用原主窗口构建代码的书写习惯，控件实际挂载到主窗口 host。
    本函数是调度器，按逻辑区段调用私有子方法完成构建。
    """
    self = host
    # DESIGN: 先挂载四个父级分类再填充控件，既避免无父 Qt 对象被回收，
    # 也防止高级模式把运行、日志和界面选项堆进同一个超长区域。
    self.system_settings_section, system_settings_row = _create_settings_box(
        settings_content_layout,
        "系统与快捷键",
        "systemSettings",
    )
    self.system_settings_row = system_settings_row
    self.display_settings_section, display_row = _create_settings_box(
        settings_content_layout,
        "界面与提示",
        "displaySettings",
    )
    self.display_settings_row = display_row
    self.workflow_settings_section, workflow_row = _create_settings_box(
        settings_content_layout,
        "运行范围与流程",
        "workflowSettings",
        expanded=False,
    )
    self.workflow_settings_row = workflow_row
    self.diagnostic_settings_section, diagnostic_row = _create_settings_box(
        settings_content_layout,
        "日志与诊断",
        "diagnosticSettings",
        expanded=False,
    )
    self.diagnostic_settings_row = diagnostic_row

    _build_hotkey_and_failsafe_controls(self, system_settings_row)
    _build_profile_hotkey_controls(self, system_settings_row)
    _build_display_and_indicator_controls(self, display_row, workflow_row)
    _build_execution_controls(self, workflow_row, diagnostic_row)
    _build_log_controls(self, diagnostic_row)
    _build_privacy_controls(self, diagnostic_row)
    _build_runner_and_appearance_controls(self, diagnostic_row, display_row)
    _build_sound_and_follow_controls(self, workflow_row, display_row)

    # 两个分类只承载高级设置；简易模式隐藏整个外框，避免留下空白区域。
    self.advanced_setting_widgets.extend(
        (self.workflow_settings_section, self.diagnostic_settings_section)
    )

    _build_backup_section(self, settings_content_layout)
    _build_credential_section(self, settings_content_layout)
