# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Construction of the global settings window.

The builder owns layout assembly only. Runtime behavior remains in dedicated
controllers or the main-window coordinator while that migration is in progress.
"""

import weakref

from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..constants import (
    PRODUCT_NAME,
)
from ..usage_notices import usage_notice
from .accessibility import apply_accessible_metadata
from .components import (
    FloatingSettingsDialog,
    HelpBtn,
    NoWheelComboBox,
    ResponsiveRow,
    fit_combo_to_contents,
    fit_text_button,
)
from .settings_defaults_actions import (
    restore_factory_settings,
    set_current_settings_as_default,
)
from .settings_sections import (
    build_automation_sections,
    build_recognition_sections,
    build_support_links,
    build_system_sections,
)
from .usage_notices_dialog import show_usage_notices


def build_global_settings_dialog(host):
    """Build controls on *host* without importing the concrete main window."""

    self = host
    self.settings_dialog = FloatingSettingsDialog(
        self.settings, "settings_dialog_geometry", f"{PRODUCT_NAME} 设置", (1300, 780)
    )
    self.settings_dialog.performance_owner_ref = weakref.ref(self)
    settings_outer = QVBoxLayout(self.settings_dialog)
    settings_outer.setContentsMargins(12, 12, 12, 12)
    settings_outer.setSpacing(8)

    self.settings_actions = QFrame()
    self.settings_actions.setObjectName("commandBar")
    settings_action_bar = QVBoxLayout(self.settings_actions)
    settings_action_bar.setContentsMargins(8, 6, 8, 6)
    settings_action_bar.setSpacing(0)
    self.settings_action_row = ResponsiveRow(
        horizontal_spacing=6,
        vertical_spacing=6,
    )
    export_help_text = (
        "【普通导出】生成 JSON，只保存步骤和设置，不复制模板图片。适合同一台电脑备份；"
        "换电脑后，原图片路径可能失效。\n\n"
        "【完整迁移包】生成 ZIP，保留脚本文字、设置和模板图片，适合在自己的多台电脑间迁移。"
        "其中可能含私人文字、窗口信息和本机路径；凭据秘密值始终不会导出。\n\n"
        "【脱敏分享包】生成 ZIP，移除文字、路径、窗口绑定和模板图片，并停用受影响步骤。"
        "适合发给他人排查或参考结构，不适合直接运行。"
    )
    self.export_json_btn = fit_text_button(QPushButton("普通导出"), 84)
    self.export_json_btn.setProperty("variant", "primary")
    _export_parts = export_help_text.split("\n\n")
    self.export_json_btn.setToolTip(_export_parts[0] if len(_export_parts) > 0 else export_help_text)
    self.export_json_btn.clicked.connect(self.workflow_transfer_controller.save)
    self.settings_action_row.add_widget(self.export_json_btn)
    self.export_portable_btn = fit_text_button(QPushButton("完整迁移包"), 96)
    self.export_portable_btn.setProperty("variant", "tonal")
    self.export_portable_btn.setToolTip(_export_parts[1] if len(_export_parts) > 1 else export_help_text)
    self.export_portable_btn.clicked.connect(
        self.workflow_transfer_controller.save_portable_package
    )
    self.settings_action_row.add_widget(self.export_portable_btn)
    self.export_sanitized_btn = fit_text_button(QPushButton("脱敏分享包"), 96)
    self.export_sanitized_btn.setProperty("variant", "ghost")
    self.export_sanitized_btn.setToolTip(_export_parts[2] if len(_export_parts) > 2 else export_help_text)
    self.export_sanitized_btn.clicked.connect(
        self.workflow_transfer_controller.save_sanitized_package
    )
    self.settings_action_row.add_widget(self.export_sanitized_btn)
    self.export_help_btn = HelpBtn(export_help_text)
    self.settings_action_row.add_widget(self.export_help_btn)
    self.batch_export_btn = fit_text_button(QPushButton("批量导出"), 76)
    self.batch_export_btn.setProperty("variant", "ghost")
    self.batch_export_btn.setToolTip(
        "勾选多个方案并按当前顺序导出；可选择普通集合、完整迁移集合包或脱敏集合包。"
    )
    self.batch_export_btn.clicked.connect(
        self.workflow_transfer_controller.save_batch
    )
    self.settings_action_row.add_widget(self.batch_export_btn)
    self.advanced_setting_widgets.append(self.batch_export_btn)
    load_btn = fit_text_button(QPushButton("导入方案"), 76)
    load_btn.setProperty("variant", "ghost")
    load_btn.clicked.connect(self.workflow_transfer_controller.load)
    self.settings_action_row.add_widget(load_btn)
    open_dir_btn = fit_text_button(QPushButton("打开配置目录"), 104)
    self.open_config_dir_btn = open_dir_btn
    open_dir_btn.setProperty("variant", "ghost")
    open_dir_btn.clicked.connect(self.settings_controller.open_config_dir)
    self.settings_action_row.add_widget(open_dir_btn)
    self.advanced_setting_widgets.append(open_dir_btn)
    diagnostic_btn = fit_text_button(QPushButton("导出诊断"), 76)
    self.diagnostic_export_btn = diagnostic_btn
    diagnostic_btn.setProperty("variant", "ghost")
    diagnostic_btn.setToolTip(
        "离线导出系统依赖、最近一次运行耗时和界面刷新统计；不会联网，也不导出脚本内容。"
    )
    diagnostic_btn.clicked.connect(self.diagnostics_controller.export_diagnostic_report)
    self.settings_action_row.add_widget(diagnostic_btn)
    self.advanced_setting_widgets.append(diagnostic_btn)
    self.integrity_btn = fit_text_button(QPushButton("校验程序"), 76)
    self.integrity_btn.setProperty("variant", "ghost")
    self.integrity_btn.setToolTip(
        "后台核对发布包文件是否缺失或损坏；此功能不联网，也不能代替作者数字签名。"
    )
    self.integrity_btn.clicked.connect(self.diagnostics_controller.verify_installed_payload)
    self.settings_action_row.add_widget(self.integrity_btn)
    self.usage_notices_btn = fit_text_button(QPushButton("使用注意"), 76)
    self.usage_notices_btn.setProperty("variant", "ghost")
    self.usage_notices_btn.clicked.connect(
        lambda: show_usage_notices(self.settings_dialog, self.settings)
    )
    self.settings_action_row.add_widget(self.usage_notices_btn)
    settings_action_bar.addWidget(self.settings_action_row)
    settings_outer.addWidget(self.settings_actions)

    settings_mode_row = QHBoxLayout()
    settings_mode_row.setContentsMargins(2, 0, 2, 0)
    settings_mode_row.addWidget(QLabel("设置视图:"))
    self.settings_mode_combo = NoWheelComboBox()
    self.settings_mode_combo.addItem("简易模式（推荐）", "simple")
    self.settings_mode_combo.addItem("高级模式", "advanced")
    saved_settings_mode = str(
        self.settings.value("settings_view_mode", "simple") or "simple"
    )
    mode_index = self.settings_mode_combo.findData(saved_settings_mode)
    self.settings_mode_combo.setCurrentIndex(max(0, mode_index))
    fit_combo_to_contents(self.settings_mode_combo)
    self.settings_mode_combo.currentIndexChanged.connect(self.apply_settings_mode)
    settings_mode_row.addWidget(self.settings_mode_combo)
    self.advanced_settings_notice = QLabel("")
    self.advanced_settings_notice.setProperty("role", "statusWarn")
    settings_mode_row.addWidget(self.advanced_settings_notice)
    settings_mode_row.addStretch()
    # 2026-10-21 维护者要求：默认设置的两个入口固定在「设置视图」行最右，
    # 简易/高级模式都可见（它们操作的是全局设置项，与视图层级无关）。
    self.set_default_settings_btn = QPushButton("将当前设置设定为默认设置")
    self.set_default_settings_btn.setProperty("variant", "ghost")
    self.set_default_settings_btn.setToolTip(
        "把当前方案的各项设置保存为默认设置，以后新建方案沿用这些设置。"
    )
    self.set_default_settings_btn.clicked.connect(
        lambda: set_current_settings_as_default(self)
    )
    settings_mode_row.addWidget(self.set_default_settings_btn)
    settings_mode_row.addWidget(
        HelpBtn(
            "【将当前设置设定为默认设置】\n"
            "把当前方案的各项设置保存为模板，之后新建方案会沿用这些设置。\n"
            "步骤列表、方案名、按键映射不进入模板。\n\n"
            "【恢复默认设置】\n"
            "把当前方案的各项设置恢复为软件初始默认值；\n"
            "步骤列表、方案名、按键映射不会被清除。"
        )
    )
    self.restore_default_settings_btn = QPushButton("恢复默认设置")
    self.restore_default_settings_btn.setProperty("variant", "ghost")
    self.restore_default_settings_btn.setToolTip(
        "把当前方案的各项设置恢复为软件初始默认值（步骤与方案名不受影响）。"
    )
    self.restore_default_settings_btn.clicked.connect(
        lambda: restore_factory_settings(self)
    )
    settings_mode_row.addWidget(self.restore_default_settings_btn)
    settings_outer.addLayout(settings_mode_row)

    self.fast_response_tip = QFrame()
    self.fast_response_tip.setObjectName("onboardingNotice")
    fast_response_layout = QHBoxLayout(self.fast_response_tip)
    fast_response_layout.setContentsMargins(12, 8, 8, 8)
    fast_response_layout.setSpacing(10)
    fast_response_text = QLabel(
        "快速反应提示：" + usage_notice("fast_response").body
    )
    fast_response_text.setWordWrap(True)
    fast_response_layout.addWidget(fast_response_text, 1)
    fast_response_dismiss = fit_text_button(QPushButton("知道了"), 68)
    fast_response_dismiss.setProperty("variant", "ghost")
    fast_response_dismiss.clicked.connect(self.dismiss_fast_response_tip)
    fast_response_layout.addWidget(fast_response_dismiss)
    self.fast_response_tip.hide()
    settings_outer.addWidget(self.fast_response_tip)

    self.settings_scroll = QScrollArea()
    self.settings_scroll.setWidgetResizable(True)
    settings_body = QWidget()
    settings_body.setObjectName("settingsBody")
    settings_content_layout = QVBoxLayout(settings_body)
    settings_content_layout.setContentsMargins(2, 2, 2, 2)
    settings_content_layout.setSpacing(4)
    self.settings_scroll.setWidget(settings_body)
    settings_outer.addWidget(self.settings_scroll)

    settings_close_btns = QDialogButtonBox(QDialogButtonBox.Close)
    settings_close_btns.button(QDialogButtonBox.Close).setText("关闭")
    # 仅连 rejected：Close 按钮点击与 Esc 均走 reject() 信号，避免双连接冗余触发。
    settings_close_btns.rejected.connect(self.close_settings_dialog)
    settings_outer.addWidget(settings_close_btns)

    build_recognition_sections(self, settings_content_layout)
    build_automation_sections(self, settings_content_layout)
    build_system_sections(self, settings_content_layout)
    # DESIGN: 三个输入行为参数属于“系统与快捷键”（反馈 20261010-3），
    # 但控件创建顺序在速度区；两区都构建完成后统一换父即可，绑定不受影响。
    for _group_name in (
        "input_backend_group",
        "key_scan_group",
        "activate_before_click_group",
    ):
        _group = getattr(self, _group_name, None)
        if _group is not None:
            self.system_settings_row.add_widget(_group)
    # 2026-10-19 迁移：映射编辑区移到「工具 → 按键映射」；设置页只留入口链接，
    # 支持链接（平台/作者/下载页）仍留在设置页，不随映射区搬走。
    # 2026-10-20 维护者裁定（A）：设置页不再保留任何按键映射入口，
    # 映射只能从「工具 → 按键映射」进入。
    build_support_links(self, settings_content_layout)
    # DESIGN: 末尾 stretch 吸收多余高度。缺它时 QVBoxLayout 会把剩余空间分摊给
    # 各折叠区（Preferred 策略），窗口越高区内容越空——表现为「纵向间距随窗口
    # 大小变化」。stretch 让各区保持自然高度，多余空间留在页面底部。
    settings_content_layout.addStretch(1)
    apply_accessible_metadata(self.settings_dialog, root_name="fukuaRPA 全局设置")

    # ================= 任务列表与日志分屏 =================
    return self.settings_dialog
