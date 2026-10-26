# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false

"""Main fukuaRPA composition root and required Qt/Win32 event entry points.

Business behavior belongs in ``ui/controllers`` or pure model modules. Keep
this class focused on object wiring, widget ownership and thin compatibility
forwarders; do not rebuild a second copy of controller state here.
"""

# RPAWindow slim: batch 1, removed 15 forwarding methods (dead code, zero callers)
# RPAWindow slim: batch 2, removed 21 forwarding methods (1-2 callers migrated to direct controller access)
# RPAWindow slim: batch 3, removed 16 forwarding methods (workflow_transfer_controller + workflow_review_controller)
# RPAWindow slim: batch 6, removed 39 forwarding methods (feedback/plugin/region/misc → FeedbackMiscForwarderMixin)

import contextlib
import logging
import os
import time
import weakref
import webbrowser
from ctypes import wintypes

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..constants import (
    APP_VERSION,
    PRODUCT_NAME,
)
from ..credentials import CredentialStore
from ..engine import RPAEngine
from ..log_policy import (
    DEFAULT_CUSTOM_LOG_CATEGORIES,
    LOG_CATEGORY_SPECS,
    LOG_MODE_COMPLETE,
    LOG_MODE_CUSTOM,
    LOG_MODE_DETAILED,
    LOG_MODE_LABELS,
    LOG_MODE_SIMPLE,
)
from ..logging_service import (
    set_log_base_dir,
)
from ..paths import get_base_dir, get_log_path, get_resource_path, runtime_path_info
from ..performance import PerformanceMetrics
from ..profile_model import ProfileCollection
from ..session import ApplicationSession
from ..usage_notices import (
    FIRST_RUN_CHOICE_DONE_KEY,
    automatic_first_use_notice_pending,
    set_first_use_notices_enabled,
    usage_notice,
)
from ..win32_api import (
    WM_ENTERSIZEMOVE,
    WM_EXITSIZEMOVE,
    WM_HOTKEY,
    WM_MOVING,
    WM_SIZING,
    hotkey_signature,
    parse_hotkey_text,
    user32,  # noqa: F401  (re-exported for tests)
)
from .accessibility import configure_main_window_accessibility
from .components import (
    HelpBtn,
    NoWheelComboBox,
    PersistentActionMenu,
    ReorderableComboBox,
    fit_combo_to_contents,
    fit_text_button,
)
from .controllers import (
    DiagnosticsController,
    ExecutionController,
    HotkeyController,
    LogController,
    MappingController,
    ProfileController,
    ProfileHotkeyController,
    RecordingController,
    RunController,
    RuntimeInitController,
    SettingsController,
    ShutdownController,
    VisualFeedbackController,
    WindowPerformanceController,
    WorkflowEditorController,
    WorkflowReviewController,
    WorkflowTransferController,
)
from .forwarders import (
    ExecutionForwarderMixin,
    FeedbackMiscForwarderMixin,
    MappingForwarderMixin,
    ProfileLogForwarderMixin,
    SettingsForwarderMixin,
    WorkflowForwarderMixin,
)
from .input_tools import CoordinatePickerUI  # noqa: F401  (patched by tests as main_window.CoordinatePickerUI)
from .lifecycle import schedule_widget_callback
from .task_list import DraggableListWidget
from .theme import apply_modern_theme

logger = logging.getLogger(__name__)
_LOG_MAX_BLOCK_COUNT = 500
_SPLITTER_TOP_HEIGHT = 480
_SPLITTER_BOTTOM_HEIGHT = 230


class RPAWindow(
    ExecutionForwarderMixin,
    FeedbackMiscForwarderMixin,
    MappingForwarderMixin,
    ProfileLogForwarderMixin,
    SettingsForwarderMixin,
    WorkflowForwarderMixin,
    QMainWindow,
):
    HOTKEY_POLL_INTERVAL_MS = 100

    def __init__(self, base_dir=None, defer_runtime=False):
        super().__init__()
        self._init_base_attributes()
        self._init_window_geometry(base_dir)
        self._init_controllers(defer_runtime)
        self._init_state()
        self._build_central_widget()
        self._build_workspace()
        self._build_status_bar()
        self._start_timers()
        self._finalize_init()

    def _init_base_attributes(self):
        """Initialize performance metrics, live-interaction state, and apply base theme."""
        self.ui_performance = PerformanceMetrics()
        self._live_window_interaction = False
        self._native_window_interaction = False
        self._live_window_interaction_kind = ""
        self._live_window_interaction_started_ns = 0
        self._cpu_timer_was_active = False
        self._autosave_timer_was_active = False
        self._live_interaction_idle_timer = QTimer(self)
        self._live_interaction_idle_timer.setSingleShot(True)
        self._live_interaction_idle_timer.timeout.connect(
            self.end_live_window_interaction
        )
        application = QApplication.instance()
        current_theme_scale = (
            float(application.property("_fukua_ui_scale") or 1.0)
            if application is not None
            else 1.0
        )
        current_theme_mode = (
            str(application.property("_fukua_theme_mode") or "light")
            if application is not None
            else "light"
        )
        # Known one-frame flicker: if a previous theme scale was applied and
        # differs from 1.0, re-applying here causes a brief visual jump.
        # This runs before show(), so impact is minimal.
        if application is not None and (
            not application.property("_fukua_modern_theme")
            or abs(current_theme_scale - 1.0) > 0.001
        ):
            apply_modern_theme(application, 1.0, current_theme_mode)

    def _init_window_geometry(self, base_dir):
        """Set up base directory, credentials, session guard, and window dimensions."""
        self.base_dir = os.path.abspath(base_dir or get_base_dir())
        self.credentials = CredentialStore(self.base_dir)
        set_log_base_dir(self.base_dir)
        self.session_guard = ApplicationSession(self.base_dir).start()
        # FR-20260812 建议-架构-4: previous_unclean 此前无生产消费方（仅测试
        # 读取）。若上次会话异常退出，在窗口显示后弹一次性提示，帮助用户了解
        # 启动状态；崩溃详情由看门狗报告承载，此处仅提示不展开细节。
        if getattr(self.session_guard, "previous_unclean", False):
            from PySide6.QtWidgets import QMessageBox as _QMB

            # FR-20260928-003: 延迟回调必须随宿主销毁取消（UI 开发规范生命
            # 周期第 3 条）；裸 singleShot 在窗口快速关闭后会以失效 QWidget
            # 调用 QMessageBox。
            schedule_widget_callback(
                self,
                800,
                lambda owner: _QMB.information(
                    owner,
                    "上次会话未正常退出",
                    "检测到上次 fukuaRPA 会话未正常退出。"
                    "如果遇到了崩溃，可在程序的 crash_reports 目录查看崩溃报告；"
                    "如只是上次直接关闭或系统重启，可以忽略本提示。",
                ),
            )
        self.setWindowTitle(f"{PRODUCT_NAME} {APP_VERSION}")
        self.resize(1300, 840)
        self.setMinimumSize(760, 620)

    def _init_controllers(self, defer_runtime):
        """Instantiate the RPA engine and all UI controllers."""
        self.defer_runtime = bool(defer_runtime)
        self.delete_hover_timer = QTimer(self)
        self.delete_hover_timer.setSingleShot(True)
        self.delete_hover_timer.timeout.connect(
            self.refresh_task_list_delete_hover
        )
        self.engine = RPAEngine(
            base_dir=self.base_dir, defer_backends=self.defer_runtime
        )
        self.runtime_init_controller = RuntimeInitController(
            self, ready=not self.defer_runtime
        )
        self.run_controller = RunController(self.engine, self)
        self.run_controller.completed.connect(self.on_finish)
        self.run_controller.failed.connect(self.on_worker_error)

        self.config_path = os.path.join(self.base_dir, "config.ini")
        self.profile_backup_path = os.path.join(self.base_dir, "profiles_backup.json")
        # 视图偏好（窗口几何、提示标志）持久化，非领域状态——符合 ARCHITECTURE.md :359
        self.settings = QSettings(self.config_path, QSettings.IniFormat)
        self.log_controller = LogController(self)
        self.diagnostics_controller = DiagnosticsController(self)
        self.window_performance_controller = WindowPerformanceController(self)
        self.feedback_controller = VisualFeedbackController(self)
        self.execution_controller = ExecutionController(self)
        self.hotkey_controller = HotkeyController(self)
        self.mapping_controller = MappingController(self)
        self.profile_controller = ProfileController(self)
        self.profile_hotkey_controller = ProfileHotkeyController(self)
        self.recording_controller = RecordingController(self)
        self.settings_controller = SettingsController(self)
        self.shutdown_controller = ShutdownController(self)
        self.workflow_editor = WorkflowEditorController(self)
        self.workflow_review_controller = WorkflowReviewController(self)
        self.workflow_transfer_controller = WorkflowTransferController(self)
        self._profiles_save_signature = ""
        self._profiles_save_in_progress = False
        self._profiles_persistence_blocked = False
        self._profiles_block_warning_logged = False
        # DOC-901-001: 会话守卫机制在后台保留（仍写入/清除标记文件）。"上次会话
        # 未正常退出"提示并非禁用——见 _init_window_geometry（FR-20260812 建议-
        # 架构-4）：窗口显示后 800ms 以 QMessageBox 提示一次，崩溃详情由看门狗
        # 报告承载。旧注释称"UX prompt is disabled"与实现矛盾，已改写。
        self._config_recovery_message = ""
        self.ui_scale = 1.0

    def _init_state(self):
        """Restore window geometry and initialize profile/model state flags."""
        geometry = self.settings.value("window_geometry")
        if geometry:
            self.restoreGeometry(geometry)

        self.profile_model = ProfileCollection()
        self.is_switching_profile = True

        self.advanced_setting_widgets = []
        self.dodge_pickers = {}
        self.region_win = None
        self._close_after_worker = False
        self._close_state_saved = False
        self.task_config_dialogs = set()
        self._runtime_editing_locked = False

    def _build_central_widget(self):
        """Build the central widget, profile bar, and command bar."""
        central = QWidget()
        central.setObjectName("appRoot")
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(12, 10, 12, 10)
        main_layout.setSpacing(8)
        self._main_layout = main_layout

        # ================= 顶部方案管理 =================
        profile_bar = QFrame()
        profile_bar.setObjectName("profileBar")
        self.profile_bar = profile_bar
        profile_layout = QHBoxLayout(profile_bar)
        profile_layout.setContentsMargins(10, 6, 8, 6)
        profile_layout.setSpacing(6)

        brand_label = QLabel(PRODUCT_NAME)
        brand_label.setProperty("role", "title")
        profile_layout.addWidget(brand_label)
        profile_layout.addSpacing(10)
        profile_caption = QLabel("方案")
        profile_caption.setProperty("role", "muted")
        profile_layout.addWidget(profile_caption)
        self.profile_combo = ReorderableComboBox()
        self.profile_combo.setMinimumWidth(170)
        self.profile_combo.setToolTip("展开后可直接拖动方案调整顺序")
        self.profile_combo.currentTextChanged.connect(self.profile_controller.on_profile_changed)
        self.profile_combo.orderChanged.connect(self.on_profile_order_changed)
        profile_layout.addWidget(self.profile_combo)

        new_prof_btn = QPushButton("新建")
        new_prof_btn.setProperty("variant", "tonal")
        new_prof_btn.clicked.connect(self.profile_controller.create_new_profile)
        profile_layout.addWidget(new_prof_btn)
        self.new_profile_btn = new_prof_btn

        # 2026-10-21 维护者要求：方案支持复制（连步骤一起复制）。
        dup_prof_btn = QPushButton("复制")
        dup_prof_btn.setProperty("variant", "ghost")
        dup_prof_btn.setToolTip("把当前方案（含步骤列表）复制成新方案")
        dup_prof_btn.clicked.connect(self.profile_controller.duplicate_current_profile)
        profile_layout.addWidget(dup_prof_btn)
        self.duplicate_profile_btn = dup_prof_btn

        rename_prof_btn = QPushButton("重命名")
        rename_prof_btn.setProperty("variant", "ghost")
        rename_prof_btn.clicked.connect(self.profile_controller.rename_current_profile)
        profile_layout.addWidget(rename_prof_btn)
        self.rename_profile_btn = rename_prof_btn

        del_prof_btn = QPushButton("删除")
        del_prof_btn.setProperty("variant", "dangerGhost")
        del_prof_btn.clicked.connect(self.profile_controller.delete_current_profile)
        profile_layout.addWidget(del_prof_btn)
        self.delete_profile_btn = del_prof_btn

        merge_prof_btn = QPushButton("合并")
        merge_prof_btn.setProperty("variant", "ghost")
        merge_prof_btn.setToolTip("把另一个方案的步骤追加到当前方案末尾")
        merge_prof_btn.clicked.connect(self.profile_controller.merge_profile)
        profile_layout.addWidget(merge_prof_btn)
        self.merge_profile_btn = merge_prof_btn

        profile_layout.addStretch()

        settings_btn = QPushButton("设置")
        settings_btn.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        settings_btn.setProperty("variant", "ghost")
        settings_btn.setToolTip("打开全局设置、方案导入导出和配置目录")
        settings_btn.clicked.connect(self.show_settings_dialog)
        profile_layout.addWidget(settings_btn)
        self.settings_btn = settings_btn

        main_layout.addWidget(profile_bar)

        command_bar = QFrame()
        command_bar.setObjectName("commandBar")
        self.command_bar = command_bar
        top_bar = QHBoxLayout(command_bar)
        top_bar.setContentsMargins(8, 6, 8, 6)
        top_bar.setSpacing(6)
        add_btn = QPushButton("新增步骤")
        add_btn.setProperty("variant", "primary")
        add_btn.setIcon(self.style().standardIcon(QStyle.SP_FileDialogNewFolder))
        add_btn.clicked.connect(lambda: self.add_row())
        top_bar.addWidget(add_btn)
        self.add_step_btn = add_btn

        insert_btn = QPushButton("插入步骤")
        insert_btn.setProperty("variant", "ghost")
        insert_btn.setToolTip("在当前选中步骤前插入一条新指令")
        insert_btn.clicked.connect(self.insert_row_before_selected)
        top_bar.addWidget(insert_btn)
        self.insert_step_btn = insert_btn

        redo_btn = QPushButton("重做")
        redo_btn.setIcon(self.style().standardIcon(QStyle.SP_ArrowForward))
        redo_btn.setProperty("variant", "ghost")
        redo_btn.setToolTip("恢复刚撤销的步骤列表操作 (Ctrl+Y)")
        redo_btn.clicked.connect(self.redo_task_change)
        top_bar.addWidget(redo_btn)
        self.redo_btn = redo_btn

        record_btn = QPushButton("● 操作录制")
        record_btn.setProperty("variant", "tonal")
        record_btn.clicked.connect(self.recording_controller.start)
        top_bar.addWidget(record_btn)
        self.record_btn = record_btn

        self.region_btn = QPushButton("识别区域")
        self.region_btn.setIcon(self.style().standardIcon(QStyle.SP_DesktopIcon))
        self.region_btn.setProperty("variant", "tonal")
        self.region_btn.clicked.connect(self.open_region_selector)
        top_bar.addWidget(self.region_btn)

        self.region_help_btn = HelpBtn("【设定识别区域】\n如CPU占用较高，务必使用此功能。\n左键拖拽框选一个或多个区域，右键完成。\n多区域适合目标分散在几个相距较远的小区域，且这些区域总面积只占屏幕很小一部分的情况。\n如果几个区域相隔很近，通常框成一个较大的单区域更快、更稳定。")
        self.region_help_btn.setProperty("helpFor", "recognitionRegion")
        top_bar.addWidget(self.region_help_btn)

        self.preview_points_btn = QPushButton("预览坐标点")
        self.preview_points_btn.setProperty("variant", "ghost")
        self.preview_points_btn.setToolTip("在屏幕上预览当前脚本中所有直接坐标点击点；仅统计坐标点击，不识别图片。")
        self.preview_points_btn.clicked.connect(self.show_all_coordinate_click_preview)
        top_bar.addWidget(self.preview_points_btn)

        self.task_search_btn = QPushButton("")
        search_icon = QIcon(
            get_resource_path(os.path.join("assets", "search.svg"))
        )
        if search_icon.isNull():
            search_icon = self.style().standardIcon(QStyle.SP_FileDialogContentsView)
        self.task_search_btn.setIcon(search_icon)
        self.task_search_btn.setProperty("iconOnly", True)
        self.task_search_btn.setProperty("variant", "ghost")
        self.task_search_btn.setAccessibleName("查找步骤")
        self.task_search_btn.setToolTip("查找步骤类型或当前显示的文字 (Ctrl+F)")
        self.task_search_btn.setShortcut("Ctrl+F")
        self.task_search_btn.clicked.connect(self.workflow_editor.show_task_search)
        top_bar.addWidget(self.task_search_btn)

        script_check_btn = QPushButton("")
        script_check_btn.setIcon(
            self.style().standardIcon(QStyle.SP_DialogApplyButton)
        )
        script_check_btn.setFixedWidth(34)
        script_check_btn.setProperty("iconOnly", True)
        script_check_btn.setProperty("variant", "ghost")
        script_check_btn.setToolTip("检查步骤语法、不可到达步骤和潜在循环，不执行脚本")
        script_check_btn.clicked.connect(self.workflow_review_controller.show_script_check_report)
        top_bar.addWidget(script_check_btn)
        self.script_check_btn = script_check_btn
        self.breakpoint_toggle_btn = QPushButton("")
        self.breakpoint_toggle_btn.setIcon(
            self.style().standardIcon(QStyle.SP_DialogNoButton)
        )
        self.breakpoint_toggle_btn.setFixedWidth(34)
        self.breakpoint_toggle_btn.setProperty("iconOnly", True)
        self.breakpoint_toggle_btn.setProperty("variant", "ghost")
        self.breakpoint_toggle_btn.setToolTip(
            "为当前选中步骤添加或清除调试断点 (F8)"
        )
        self.breakpoint_toggle_btn.clicked.connect(self.toggle_selected_breakpoint)
        top_bar.addWidget(self.breakpoint_toggle_btn)
        self.single_step_btn = QPushButton("")
        self.single_step_btn.setIcon(
            self.style().standardIcon(QStyle.SP_MediaSkipForward)
        )
        self.single_step_btn.setFixedWidth(34)
        self.single_step_btn.setProperty("iconOnly", True)
        self.single_step_btn.setProperty("variant", "ghost")
        self.single_step_btn.setToolTip("只执行当前选中步骤一次，不执行循环和跳转")
        self.single_step_btn.clicked.connect(self.run_selected_step_once)
        top_bar.addWidget(self.single_step_btn)
        self.advanced_setting_widgets.extend(
            (
                self.merge_profile_btn,
                self.insert_step_btn,
                self.redo_btn,
                self.single_step_btn,
            )
        )
        top_bar.addStretch()
        main_layout.addWidget(command_bar)

    def _build_host_owned_settings_controls(self):
        """Create settings controls that the startup path reads, before the dialog exists.

        C-1：设置对话框改为首次打开时构建，但下列 13 个控件被启动链直接使用
        （日志级别/内容/隐私、映射开关与布局、热键编辑框、界面倍率/主题、
        最小化/置顶），因此改为宿主态创建；`settings_sections/*` 只把它们加入布局。
        属性与信号连接逐项复刻自原创建点，不改变运行语义。
        """
        # ---- M-1：底栏最小化/置顶 ----
        self.mini_chk = QCheckBox("启动时最小化")
        self.top_chk = QCheckBox("窗口置顶")
        self.top_chk.stateChanged.connect(
            self.window_performance_controller.toggle_top_window
        )

        # ---- M-2：热键编辑框、界面倍率、主题 ----
        self.hotkey_start_edit = QLineEdit("F9")
        # UI-CONVENTION: allow UI003 reason=既有代码由 settings_sections 搬移至此，本轮未改动该尺寸语义。
        self.hotkey_start_edit.setMinimumWidth(120)
        self.hotkey_start_edit.setToolTip(
            "可输入 F9、Ctrl+F9、Ctrl+Alt+S 等；启动/停止不允许使用裸字母或裸数字。"
        )
        self.hotkey_start_edit.editingFinished.connect(self.update_hotkeys)
        self.hotkey_stop_edit = QLineEdit("F10")
        # UI-CONVENTION: allow UI003 reason=既有代码由 settings_sections 搬移至此，本轮未改动该尺寸/间距语义，仅位置变化。
        self.hotkey_stop_edit.setMinimumWidth(120)
        self.hotkey_stop_edit.setToolTip(
            "可输入 F10、Ctrl+F10、Ctrl+Alt+Q 等；停止热键建议使用不易误触的组合键。"
        )
        self.hotkey_stop_edit.editingFinished.connect(self.update_hotkeys)
        self.ui_scale_edit = QLineEdit("100")
        # UI-CONVENTION: allow UI003 reason=既有代码由 settings_sections 搬移至此，本轮未改动该尺寸/间距语义，仅位置变化。
        self.ui_scale_edit.setFixedWidth(60)
        self.ui_scale_edit.setToolTip(
            "调整主界面、设置窗口和小齿轮窗口的字体与控件尺寸，建议范围 75-180。"
        )
        self.theme_mode_combo = NoWheelComboBox()
        self.theme_mode_combo.addItem("浅色", "light")
        self.theme_mode_combo.addItem("深色", "dark")
        fit_combo_to_contents(self.theme_mode_combo)
        self.theme_mode_combo.currentIndexChanged.connect(
            self.window_performance_controller.apply_theme_mode
        )

        # ---- M-4：日志级别/内容/隐私 ----
        self.log_level_combo = NoWheelComboBox()
        for mode in (
            LOG_MODE_SIMPLE,
            LOG_MODE_DETAILED,
            LOG_MODE_COMPLETE,
            LOG_MODE_CUSTOM,
        ):
            self.log_level_combo.addItem(LOG_MODE_LABELS[mode], mode)
        fit_combo_to_contents(self.log_level_combo)
        self.log_content_menu = PersistentActionMenu(self)
        self.log_content_menu.setToolTipsVisible(True)
        self.log_critical_action = self.log_content_menu.addAction(
            "错误、急停和严重警告（始终输出）"
        )
        self.log_critical_action.setCheckable(True)
        self.log_critical_action.setChecked(True)
        self.log_critical_action.setEnabled(False)
        self.log_category_actions = {}
        for spec in LOG_CATEGORY_SPECS:
            action = self.log_content_menu.addAction(spec.label)
            action.setCheckable(True)
            action.setToolTip(spec.description)
            action.toggled.connect(
                lambda _checked, key=spec.key: self.log_controller.on_category_toggled(key)
            )
            self.log_category_actions[spec.key] = action
        self.log_content_menu.addSeparator()
        for mode in (LOG_MODE_SIMPLE, LOG_MODE_DETAILED, LOG_MODE_COMPLETE):
            action = self.log_content_menu.addAction(
                f"恢复为{LOG_MODE_LABELS[mode]}预设"
            )
            action.triggered.connect(
                lambda _checked=False, selected=mode: self.log_controller.select_preset(
                    selected
                )
            )
        self.log_content_btn = fit_text_button(QPushButton("内容..."), 78)
        self.log_content_btn.setMenu(self.log_content_menu)
        self.log_level_combo.currentIndexChanged.connect(
            self.log_controller.on_mode_selected
        )
        self.log_controller.apply_controls(
            LOG_MODE_SIMPLE, DEFAULT_CUSTOM_LOG_CATEGORIES
        )
        self.log_file_chk = QCheckBox("写入文件日志")
        self.log_ui_chk = QCheckBox("界面日志")
        self.log_ui_chk.setChecked(True)
        self.log_privacy_chk = QCheckBox("日志隐私保护")
        self.log_privacy_chk.setChecked(
            self.settings.value("log_privacy_protected", True, type=bool)
        )
        self.log_privacy_status_label = QLabel("隐私保护已关闭")
        self.log_privacy_status_label.setProperty("role", "statusWarn")
        self.log_privacy_status_label.setVisible(
            not self.log_privacy_chk.isChecked()
        )

        # ---- M-4：按键映射（开关、点击方式、行容器与布局）----
        self.mapping_mode_chk = QCheckBox("启用按键映射模式")
        self.mapping_mode_chk.setChecked(False)
        self.mapping_mode_chk.setToolTip(
            "开启后，映射里的裸字母、裸数字、空格等单键才会被软件接管。"
            "关闭后这些裸键不会生效，也不会影响正常打字。"
        )
        self.mapping_mode_chk.stateChanged.connect(self.refresh_hotkey_backend)
        self.mapping_click_mode_combo = NoWheelComboBox()
        self.mapping_click_mode_combo.addItems(
            ["真实鼠标点击", "点击后返回原位", "后台窗口点击(实验)"]
        )
        fit_combo_to_contents(self.mapping_click_mode_combo)
        self.mapping_click_mode_combo.currentTextChanged.connect(
            self.refresh_mapping_binding_controls
        )
        self.key_mapping_rows = []
        self.mapping_rows_layout = QVBoxLayout()
        # UI-CONVENTION: allow UI004 reason=既有代码由 settings_sections 搬移至此，本轮未改动该尺寸/间距语义，仅位置变化。
        self.mapping_rows_layout.setSpacing(7)

    def _ensure_settings_dialog(self):
        """Build the settings dialog on first use (C-1: deferred from startup).

        Gate key is the dialog's existence (not a separate flag): a pre-existing
        dialog (tests or future code) is adopted rather than rebuilt.

        INVARIANT: 首次构建后必须用**已保存的方案配置**填充控件，不能用
        `get_current_ui_config()`（那是 capture，会把刚建好的默认值当成用户配置，
        经自动保存落盘并影响运行行为）。
        """
        dialog = getattr(self, "settings_dialog", None)
        if dialog is None:
            # O-1：`settings_view` 的导入随之延迟到首次打开。
            from .settings_view import build_global_settings_dialog

            build_global_settings_dialog(self)
            dialog = self.settings_dialog
        if getattr(self, "_settings_dialog_ready", False):
            return dialog
        # 顺序即不变量（B 复验条件 1）：**先应用配置**（可能抛异常），后连接/绑定。
        # 若先绑定再应用，失败时 ready 不置位、重试会重复连接，
        # 一次编辑触发两条审计日志。
        # 打开前把**已保存的当前方案**完整推入对话框控件（含尾部宿主态动作）。
        # FR-20261022-201：这里只能应用“设置控件”，不得复用 apply_ui_config 的
        # 载入方案语义——后者会 close_all_task_config_dialogs() + task_list.clear()
        # 并按已保存 tasks 重建，从而丢弃首开前尚未自动保存的步骤新增/编辑。
        if not self.apply_settings_controls_config(
            self._settings_dialog_source_config()
        ):
            detail = getattr(self, "_last_config_apply_error", "") or "未知原因"
            QMessageBox.warning(
                self,
                "设置窗口未就绪",
                "设置窗口的控件初始化失败，本次不会打开。\n\n"
                f"原因：{detail}\n\n可以稍后重试；当前步骤列表未被改动。",
            )
            return None
        # 简易/高级模式在启动时因对话框缺失无法应用到对话框控件，构建后补一次。
        self.apply_settings_mode()
        if not getattr(self, "_settings_visibility_connected", False):
            # B 复验建议 1：连接用显式幂等位，避免准备失败重试时重复连接。
            # 不用 `Qt.UniqueConnection`：本仓库既有守护对 connect 的返回类型敏感。
            dialog.visibilityChanged.connect(
                self.settings_controller.on_dialog_visibility_changed
            )
            self._settings_visibility_connected = True
        # 审计日志的对话框阶段绑定（宿主态绑定已在 _finalize_init 完成）。
        self.log_controller.bind_setting_logs_dialog()
        self._settings_dialog_ready = True
        return dialog

    def _settings_dialog_source_config(self):
        """返回用于填充对话框控件的配置：默认值叠加当前方案，**不做 capture**。"""
        from fukua_rpa.config_schema import default_profile_config

        config = default_profile_config()
        stored = self.profiles_data.get(self.current_profile_name) or {}
        config.update(stored)
        return config

    def _build_workspace(self):
        """Build the settings dialog, splitter, task panel, run controls, and log view."""
        main_layout = self._main_layout
        # ================= 设置窗口 =================
        # C-1：设置对话框改为**首次打开时构建**（`_ensure_settings_dialog`）。
        # 构造期不再构建它；但其中 13 个控件被启动链直接读取（日志级别/隐私、
        # 映射开关与布局、热键编辑框、界面倍率/主题、最小化/置顶），
        # 这些控件改由 `_build_host_owned_settings_controls` 在宿主态创建，
        # 对话框区只负责把它们加入布局（与 loop_combo 同模式）。
        self._build_host_owned_settings_controls()

        self.splitter = QSplitter(Qt.Vertical)

        task_panel = QFrame()
        task_panel.setObjectName("workspacePanel")
        task_panel_layout = QVBoxLayout(task_panel)
        task_panel_layout.setContentsMargins(0, 0, 0, 0)
        task_panel_layout.setSpacing(0)
        task_header = QHBoxLayout()
        task_header.setContentsMargins(12, 8, 10, 7)
        task_title = QLabel("脚本步骤")
        task_title.setProperty("role", "sectionTitle")
        task_header.addWidget(task_title)

        self.task_search_widget = QWidget()
        task_search_layout = QHBoxLayout(self.task_search_widget)
        task_search_layout.setContentsMargins(0, 0, 0, 0)
        self.task_search_edit = QLineEdit()
        self.task_search_edit.setPlaceholderText("输入步骤类型或显示文字")
        self.task_search_edit.setClearButtonEnabled(True)
        self.task_search_edit.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed
        )
        self.task_search_edit.textChanged.connect(
            self.workflow_editor.queue_task_search
        )
        self.task_search_edit.returnPressed.connect(
            lambda: self.workflow_editor.find_task_match(1)
        )
        task_search_layout.addWidget(self.task_search_edit, 1)
        self.task_search_status = QLabel("")
        self.task_search_status.setProperty("role", "muted")
        task_search_layout.addWidget(self.task_search_status)
        self.task_search_prev_btn = QPushButton("")
        self.task_search_prev_btn.setIcon(
            self.style().standardIcon(QStyle.SP_ArrowBack)
        )
        self.task_search_prev_btn.setProperty("iconOnly", True)
        self.task_search_prev_btn.setToolTip("上一个匹配项 (Shift+F3)")
        self.task_search_prev_btn.setShortcut("Shift+F3")
        self.task_search_prev_btn.clicked.connect(
            lambda: self.workflow_editor.find_task_match(-1)
        )
        task_search_layout.addWidget(self.task_search_prev_btn)
        self.task_search_next_btn = QPushButton("")
        self.task_search_next_btn.setIcon(
            self.style().standardIcon(QStyle.SP_ArrowForward)
        )
        self.task_search_next_btn.setProperty("iconOnly", True)
        self.task_search_next_btn.setToolTip("下一个匹配项 (F3)")
        self.task_search_next_btn.setShortcut("F3")
        self.task_search_next_btn.clicked.connect(
            lambda: self.workflow_editor.find_task_match(1)
        )
        task_search_layout.addWidget(self.task_search_next_btn)
        self.task_search_close_btn = QPushButton("")
        self.task_search_close_btn.setIcon(
            self.style().standardIcon(QStyle.SP_DialogCloseButton)
        )
        self.task_search_close_btn.setProperty("iconOnly", True)
        self.task_search_close_btn.setToolTip("关闭查找")
        self.task_search_close_btn.clicked.connect(
            self.workflow_editor.close_task_search
        )
        task_search_layout.addWidget(self.task_search_close_btn)
        self.task_search_widget.hide()
        task_header.addWidget(self.task_search_widget, 1)

        self.task_search_spacer = QWidget()
        self.task_search_spacer.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Preferred
        )
        task_header.addWidget(self.task_search_spacer, 1)
        self.task_count_label = QLabel("0 步")
        self.task_count_label.setProperty("role", "muted")
        task_header.addWidget(self.task_count_label)
        task_panel_layout.addLayout(task_header)

        self.task_list = DraggableListWidget()
        self.task_list.setObjectName("taskList")
        self.task_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.task_list.customContextMenuRequested.connect(
            self.workflow_editor.show_task_context_menu
        )
        self.task_list.itemSelectionChanged.connect(self.update_selection_highlight)
        task_panel_layout.addWidget(self.task_list, 1)
        self.splitter.addWidget(task_panel)

        bottom_widget = QFrame()
        bottom_widget.setObjectName("workspacePanel")
        bottom_vbox = QVBoxLayout(bottom_widget)
        bottom_vbox.setContentsMargins(10, 8, 10, 10)
        bottom_vbox.setSpacing(7)

        # 底部运行控制区
        bot_layout = QHBoxLayout()
        self.run_controls_layout = bot_layout
        bot_layout.setContentsMargins(0, 0, 0, 0)
        loop_caption = QLabel("循环")
        loop_caption.setProperty("role", "muted")
        bot_layout.addWidget(loop_caption)
        self.loop_combo = NoWheelComboBox()
        self.loop_combo.addItems(["单次", "无限", "指定次数", "指定时间(时)", "指定时间(分)", "指定时间(秒)"])
        self.loop_combo.currentTextChanged.connect(self.update_loop_ui)
        bot_layout.addWidget(self.loop_combo)

        self.loop_val_edit = QLineEdit("10")
        self.loop_val_edit.setFixedWidth(50)
        bot_layout.addWidget(self.loop_val_edit)
        self.update_loop_ui(self.loop_combo.currentText())

        bot_layout.addStretch()
        bot_layout.addWidget(self.mini_chk)
        bot_layout.addWidget(self.top_chk)
        bot_layout.addSpacing(10)

        self.start_btn = QPushButton("启动")
        self.start_btn.clicked.connect(self.start_task)
        self.start_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.start_btn.setProperty("variant", "success")
        self.start_btn.setMinimumSize(112, 32)
        bot_layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.clicked.connect(self.stop_task)
        self.stop_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaStop))
        self.stop_btn.setProperty("variant", "danger")
        self.stop_btn.setMinimumSize(112, 32)
        self.stop_btn.setEnabled(False)
        bot_layout.addWidget(self.stop_btn)

        self.debug_toolbar_widget = QWidget()
        self.debug_toolbar_widget.setObjectName("debugToolbar")
        debug_layout = QHBoxLayout(self.debug_toolbar_widget)
        debug_layout.setContentsMargins(0, 0, 0, 0)
        debug_layout.setSpacing(6)
        debug_caption = QLabel("调试")
        self.debug_layout = debug_layout
        self.debug_caption = debug_caption
        debug_caption.setProperty("role", "muted")
        debug_layout.addStretch()
        debug_layout.addWidget(debug_caption)

        self.debug_pause_btn = QPushButton("")
        self.debug_pause_btn.setIcon(
            self.style().standardIcon(QStyle.SP_MediaPause)
        )
        self.debug_pause_btn.setFixedSize(34, 32)
        self.debug_pause_btn.setProperty("iconOnly", True)
        self.debug_pause_btn.setProperty("variant", "ghost")
        self.debug_pause_btn.setToolTip("完成当前步骤后，在下一个步骤执行前暂停")
        self.debug_pause_btn.clicked.connect(self.pause_debug_run)
        self.debug_pause_btn.setEnabled(False)
        debug_layout.addWidget(self.debug_pause_btn)

        self.debug_continue_btn = QPushButton("")
        self.debug_continue_btn.setIcon(
            self.style().standardIcon(QStyle.SP_MediaPlay)
        )
        self.debug_continue_btn.setFixedSize(34, 32)
        self.debug_continue_btn.setProperty("iconOnly", True)
        self.debug_continue_btn.setProperty("variant", "tonal")
        self.debug_continue_btn.setToolTip("从暂停位置继续运行到下一个断点")
        self.debug_continue_btn.clicked.connect(self.continue_debug_run)
        self.debug_continue_btn.setEnabled(False)
        debug_layout.addWidget(self.debug_continue_btn)

        self.debug_next_btn = QPushButton("")
        self.debug_next_btn.setIcon(
            self.style().standardIcon(QStyle.SP_MediaSkipForward)
        )
        self.debug_next_btn.setFixedSize(34, 32)
        self.debug_next_btn.setProperty("iconOnly", True)
        self.debug_next_btn.setProperty("variant", "tonal")
        self.debug_next_btn.setToolTip("执行当前暂停步骤，并在下一个实际步骤前再次暂停")
        self.debug_next_btn.clicked.connect(self.step_over_debug_run)
        self.debug_next_btn.setEnabled(False)
        debug_layout.addWidget(self.debug_next_btn)

        self.debug_variables_btn = QPushButton("")
        self.debug_variables_btn.setIcon(
            self.style().standardIcon(QStyle.SP_FileDialogInfoView)
        )
        self.debug_variables_btn.setFixedSize(34, 32)
        self.debug_variables_btn.setProperty("iconOnly", True)
        self.debug_variables_btn.setProperty("variant", "ghost")
        self.debug_variables_btn.setToolTip("查看当前暂停位置的运行变量")
        self.debug_variables_btn.clicked.connect(self.show_debug_variables)
        self.debug_variables_btn.setEnabled(False)
        debug_layout.addWidget(self.debug_variables_btn)

        bottom_vbox.addLayout(bot_layout)
        bottom_vbox.addWidget(self.debug_toolbar_widget)

        log_header = QHBoxLayout()
        log_title = QLabel("运行日志")
        log_title.setProperty("role", "sectionTitle")
        log_header.addWidget(log_title)
        log_header.addStretch()
        bottom_vbox.addLayout(log_header)

        self.log_text = QPlainTextEdit()
        self.log_text.setObjectName("logView")
        self.log_text.setReadOnly(True)
        self.log_text.document().setMaximumBlockCount(_LOG_MAX_BLOCK_COUNT)
        self.log_text.setContextMenuPolicy(Qt.CustomContextMenu)
        self.log_text.customContextMenuRequested.connect(
            self._on_log_context_menu
        )
        bottom_vbox.addWidget(self.log_text)

        self.splitter.addWidget(bottom_widget)
        self.splitter.setSizes([_SPLITTER_TOP_HEIGHT, _SPLITTER_BOTTOM_HEIGHT])
        main_layout.addWidget(self.splitter)

    def _build_status_bar(self):
        """Build the bottom status bar with log path, region, and CPU labels."""
        main_layout = self._main_layout
        # 状态栏
        status_bar = QFrame()
        status_bar.setObjectName("statusBar")
        self.status_bar = status_bar
        self.status_layout = QHBoxLayout(status_bar)
        self.status_layout.setContentsMargins(10, 5, 10, 5)
        self.status_layout.setSpacing(12)
        log_path = get_log_path(self.base_dir)
        self.log_path_label = QLabel(f"日志: {os.path.basename(log_path)}")
        self.log_path_label.setProperty("role", "muted")
        self.log_path_label.setToolTip(log_path)
        self.status_layout.addWidget(self.log_path_label)
        self.region_label = QLabel("范围: 全屏")
        self.region_label.setProperty("role", "statusGood")
        self.status_layout.addWidget(self.region_label)
        self.profile_save_status_label = QLabel("")
        self.profile_save_status_label.setProperty("role", "statusError")
        self.profile_save_status_label.hide()
        self.status_layout.addWidget(self.profile_save_status_label)
        self.status_layout.addStretch()
        self.cpu_label = QLabel("本程序 CPU: --%")
        self.cpu_label.setProperty("role", "statusInfo")
        self.cpu_label.setWordWrap(False)
        self.cpu_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.cpu_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.cpu_label.setToolTip(
            "程序 CPU 保留单核口径：占满一个逻辑核心约为 100%，"
            "使用多个核心时可能超过 100%。"
        )
        self.status_layout.addWidget(self.cpu_label)
        main_layout.addWidget(status_bar)

    def _start_timers(self):
        """Create and start the CPU monitoring and hotkey polling timers."""
        # 初始化定时器与全局配置
        self.cpu_timer = QTimer(self)
        self.cpu_timer.timeout.connect(self.update_cpu_info)
        self.cpu_timer.start(self.current_cpu_interval())

        self.hotkey_timer = QTimer(self)
        self.hotkey_timer.timeout.connect(self.check_hotkey)
        self.hotkey_timer.setInterval(self.HOTKEY_POLL_INTERVAL_MS)

    def _finalize_init(self):
        """Run profile init, scheduler, hotkeys, deferred runtime, and accessibility setup."""
        self.profile_controller.init_profiles()
        self.profile_hotkey_controller.refresh_status()
        self.init_scheduler_and_plugins()
        self.log_controller.bind_setting_logs()
        self.update_hotkeys()
        if self.defer_runtime:
            self.start_btn.setEnabled(False)
            self.single_step_btn.setEnabled(False)
            self.start_btn.setToolTip("识别运行库正在后台准备")
            schedule_widget_callback(
                self, 0, lambda owner: owner.initialize_runtime_async()
            )
        self.profiles_autosave_timer = QTimer(self)
        self.profiles_autosave_timer.timeout.connect(self.autosave_profiles_if_changed)
        self.profiles_autosave_timer.start(3000)
        if self._config_recovery_message:
            schedule_widget_callback(
                self, 0, lambda owner: owner.show_config_recovery_message()
            )
        if runtime_path_info().mode == "local_app_data" and not self.settings.value(
            "local_data_location_notice", False, type=bool
        ):
            schedule_widget_callback(
                self, 0, lambda owner: owner.show_local_data_location_notice()
            )
        self.apply_settings_mode()
        configure_main_window_accessibility(self)


    def show_local_data_location_notice(self):
        self.settings.setValue("local_data_location_notice", True)
        QMessageBox.information(
            self,
            "配置已保存到用户目录",
            "当前程序所在目录不可写，因此方案、日志、导入图片和凭据已自动保存到：\n\n"
            f"{self.base_dir}\n\n"
            "程序功能不受影响；设置中的“打开配置目录”会直接打开此位置。",
        )

    def show_config_recovery_message(self):
        """Show recovery details only while the main window is still in use."""
        if not self._config_recovery_message or not self.isVisible():
            return
        # On a genuine first launch there is no config to recover from.
        if not os.path.exists(self.config_path):
            return
        QMessageBox.warning(self, "配置恢复提示", self._config_recovery_message)

    def open_web_url(self, url):
        try:
            webbrowser.open(url, new=2)
        except Exception as e:
            QMessageBox.warning(self, "错误", f"无法打开网页: {e}")


    def show_settings_dialog(self):
        if self._runtime_editing_locked:
            return
        if self._ensure_settings_dialog() is None:
            return
        self.refresh_scale_memory_status()
        self.show_fast_response_tip_once()
        # INVARIANT: 只清除最小化标志，保留用户原有的最大化状态。
        dialog_state = self.settings_dialog.windowState()
        if dialog_state & Qt.WindowMinimized:
            self.settings_dialog.setWindowState(
                dialog_state & ~Qt.WindowMinimized
            )
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def show_fast_response_tip_once(self):
        tip = getattr(self, "fast_response_tip", None)
        if tip is None:
            return
        key = usage_notice("fast_response").one_time_key
        if not automatic_first_use_notice_pending(self.settings, key):
            tip.hide()
            return
        self.settings.setValue(key, True)
        self.settings.sync()
        tip.show()

    def dismiss_fast_response_tip(self):
        tip = getattr(self, "fast_response_tip", None)
        if tip is not None:
            tip.hide()

    def maybe_show_emergency_stop_tip_once(self):
        """Show the three emergency-stop methods the first time a script runs."""

        key = usage_notice("script_emergency_stop_methods").one_time_key
        if not automatic_first_use_notice_pending(self.settings, key):
            return
        self.settings.setValue(key, True)
        self.settings.sync()
        notice = usage_notice("script_emergency_stop_methods")
        QMessageBox.information(self, notice.title, notice.body)

    def on_first_use_notices_changed(self, enabled):
        """Persist the first-use notice master switch (auto prompts only)."""

        set_first_use_notices_enabled(self.settings, enabled)
        self.log_setting_change("首次使用提示", "开启" if enabled else "关闭")

    def schedule_first_launch_choice(self, *, existing_config):
        """Offer the one-time first-launch choice after the window is up.

        Only the interactive GUI calls this (self-test, smoke builds, the Runner
        and background helpers never do). A prior configuration means an existing
        user: keep current behaviour and never re-prompt them as a fresh install.
        """

        if bool(self.settings.value(FIRST_RUN_CHOICE_DONE_KEY, False, type=bool)):
            return
        if existing_config:
            return
        schedule_widget_callback(
            self, 0, lambda owner: owner.show_first_launch_choice()
        )

    def show_first_launch_choice(self):
        if bool(self.settings.value(FIRST_RUN_CHOICE_DONE_KEY, False, type=bool)):
            return
        if not self.isVisible():
            return
        from .usage_notices_dialog import show_first_run_choice

        show_first_run_choice(self, self.settings)

    def close_settings_dialog(self):
        dialog = getattr(self, "settings_dialog", None)
        if dialog is None:
            return
        dialog.save_dialog_geometry()
        self.dismiss_fast_response_tip()
        dialog.hide()

    def register_task_config_dialog(self, dialog):
        if not dialog:
            return
        dialog.performance_owner_ref = weakref.ref(self)
        self.task_config_dialogs.add(dialog)
        if hasattr(dialog, "apply_settings_mode"):
            dialog.apply_settings_mode(self.current_settings_mode())
        dialog.destroyed.connect(lambda *_args, d=dialog: self.task_config_dialogs.discard(d))

    def close_all_task_config_dialogs(self):
        for dialog in list(getattr(self, "task_config_dialogs", set())):
            try:
                dialog.close()
            except RuntimeError:
                self.task_config_dialogs.discard(dialog)

    def close_coordinate_pickers(self, rows=None):
        """Close open coordinate pickers, optionally only for *rows*.

        FR-20261006-013: a picker captures a callback bound to the row widget that
        opened it. Deleting that row left the picker alive, so the user's click was
        delivered into a detached widget: nothing appeared to happen and the picked
        coordinate was silently lost. Step-settings dialogs already had
        ``close_all_task_config_dialogs``; pickers had no counterpart, which is why
        the delete path missed them.

        The row->widget lookup lives here rather than in the caller so the caller
        needs no helper of its own (``WorkflowStepEditMixin`` is at its method
        budget, guarded by ``tests/test_workflow_editor_split.py``).

        Args:
            rows: Restrict to these row **indexes**. ``None`` closes every open
                picker, which is what a wholesale list rebuild needs; passing a
                collection keeps unrelated pickers usable.
        """
        if rows is None:
            widgets = self._live_task_row_widgets()
        else:
            widgets = []
            for index in rows:
                try:
                    widget = self.task_list.itemWidget(self.task_list.item(index))
                except RuntimeError:
                    continue
                if widget is not None:
                    widgets.append(widget)
        for row in widgets:
            picker = getattr(row, "coordinate_picker", None)
            if picker is None:
                continue
            # Already destroyed by Qt: clearing the reference is enough.
            with contextlib.suppress(RuntimeError):
                picker.close()
            if getattr(row, "coordinate_picker", None) is picker:
                row.coordinate_picker = None

    def _live_task_row_widgets(self):
        """Every instantiated row widget currently in the task list."""
        rows = []
        for index in range(self.task_list.count()):
            try:
                widget = self.task_list.itemWidget(self.task_list.item(index))
            except RuntimeError:
                continue
            if widget is not None:
                rows.append(widget)
        return rows

    # ------------------------------------------------------------------
    # 计划任务 + 插件系统 (design §3 / §5)
    # ------------------------------------------------------------------
    def _try_run_profile(self, profile_id):
        if not profile_id:
            return
        self.execution_controller.prepare_and_launch_profile(
            profile_id, "manual", "", "", None
        )

    def show_scheduled_tasks_window(self):
        if self._scheduled_tasks_window is None:
            from .scheduled_tasks_window import ScheduledTasksWindow

            self._scheduled_tasks_window = ScheduledTasksWindow(
                self.scheduled_service,
                self._list_profiles_for_scheduler,
                self._try_run_profile,
                self.settings,
                self._plugin_enabled_status,
                self,
                is_runtime_editing_locked=lambda: self._runtime_editing_locked,
            )
        self._scheduled_tasks_window.show()
        self._scheduled_tasks_window.raise_()
        self._scheduled_tasks_window.activateWindow()

    def _on_scheduled_missed_ask(self, schedule_id: str, task_name: str) -> None:
        """FR-b01-004: MISSED_ASK 错过策略的询问入口（由 missed_detected 信号触发）。

        延迟到事件循环空闲时再弹窗，避免 service.start() 在构造期发出的首次信号
        触发嵌套事件循环。主窗口不可见（仍在构造或已进入退出流程）时按跳过处理，
        不强行弹窗。
        """

        def _ask(owner) -> None:
            # FR-20260930-004: 经 owner 访问宿主，避免闭包自捕获 self 形成
            # Python 包装器 ⇄ C++ 子对象闭环，使「宿主销毁即取消」的保证
            # 降级为依赖显式 deleteLater（与 :724/:731/:737 先例一致）。
            if not owner.isVisible():
                owner.scheduled_service.resolve_missed_ask(schedule_id, False)
                return
            answer = QMessageBox.question(
                owner,
                "计划任务：错过触发",
                f"计划任务“{task_name}”在软件关闭或休眠期间错过触发。\n"
                "是否立即补跑一次？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            owner.scheduled_service.resolve_missed_ask(
                schedule_id, answer == QMessageBox.Yes
            )

        # FR-20260928-003: 延迟回调必须随宿主销毁取消；宿主已销毁时整段
        # 询问跳过（pending_ask 记录保留，行为与用户未应答一致）。
        schedule_widget_callback(self, 0, _ask)

    def show_plugin_center_window(self):
        if self._plugin_center_window is None:
            from .plugin_center_window import PluginCenterWindow

            self._plugin_center_window = PluginCenterWindow(
                self.plugin_controller,
                self.plugin_install_dir,
                self.settings,
                self,
            )
        self.plugin_controller.refresh()
        self._plugin_center_window.show()
        self._plugin_center_window.raise_()
        self._plugin_center_window.activateWindow()

    def show_auto_clicker_window(self):
        """打开连点器工具窗口（独立设置 + 独立开始/停止），不作为脚本步骤。"""

        engine = getattr(self, "engine", None)
        if self._auto_clicker_window is None:
            from .auto_clicker_tool import AutoClickerToolWindow

            self._auto_clicker_window = AutoClickerToolWindow(self, engine)
        self._auto_clicker_window.show()
        self._auto_clicker_window.raise_()
        self._auto_clicker_window.activateWindow()

    def closeEvent(self, event):
        """关闭主窗口即退出整个程序。

        工具窗口（按键映射/连点器/插件中心/计划任务）关闭时只隐藏、不销毁，
        它们仍是「打开的顶层窗口」；因此仅关主窗口时 Qt 不会触发
        lastWindowClosed，进程会留在后台且没有托盘入口。这里显式关闭所有
        工具窗口并退出应用，恢复「关主窗口就全关」的行为。
        """
        # 先走 MRO：FeedbackMiscForwarderMixin.closeEvent → shutdown_controller，
        # 运行中会被它置标记、停脚本、禁用界面并 event.ignore()。此处不得提前
        # return，否则安全停止链路被短路（R14 三次复验 B 项）。
        super().closeEvent(event)
        if not event.isAccepted():
            return
        # 放行后清掉可能残留的「等待 worker」标记与看门狗标记，避免下次关闭异常。
        self._close_after_worker = False
        self._shutdown_watchdog_started = False
        for attribute in (
            "_key_mapping_tool_window",
            # _auto_clicker_window 由 shutdown_controller 负责关闭，这里再关一次会
            # 在首次 wait 超时后再阻塞 2 秒（R15 审查②）。
            "_plugin_center_window",
            "_scheduled_tasks_window",
        ):
            window = getattr(self, attribute, None)
            if window is None:
                continue
            with contextlib.suppress(RuntimeError):
                window.close()
        application = QApplication.instance()
        if application is not None:
            application.quit()

    def show_key_mapping_tool_window(self):
        """打开「按键映射」工具窗口（映射配置编辑窗口）。

        懒创建单例：只构建一次，映射区与「按键映射模式」开关在此收揽并显式重定父对象，
        设置页不再保留第二套编辑控件。关闭只隐藏，不改变映射启用状态。
        """
        if self._key_mapping_tool_window is None:
            from .key_mapping_tool import KeyMappingToolWindow

            self._key_mapping_tool_window = KeyMappingToolWindow(self)
        self._key_mapping_tool_window.show_and_raise()

    def refresh_key_mapping_tool_profile(self):
        """切换方案后刷新工具窗口显示的方案名（窗口未创建时无需处理）。"""
        window = getattr(self, "_key_mapping_tool_window", None)
        if window is not None:
            window.refresh_profile_name()

    def auto_clicker_is_running(self):
        """Return whether the standalone clicker currently owns input."""
        window = self._auto_clicker_window
        return bool(window is not None and window.is_running)

    def auto_clicker_hotkey_bindings(self):
        """Expose visible clicker bindings to the shared global backend."""
        window = self._auto_clicker_window
        return window.hotkey_bindings() if window is not None else {}

    def auto_clicker_configured_hotkey_bindings(self):
        """Return configured clicker keys even before its window is opened."""
        window = self._auto_clicker_window
        if window is not None:
            start_text = window.start_key_edit.text().strip()
            stop_text = window.stop_key_edit.text().strip()
        else:
            start_text = str(
                self.settings.value("auto_clicker/start_hotkey", "F6") or "F6"
            )
            stop_text = str(
                self.settings.value("auto_clicker/stop_hotkey", "F7") or "F7"
            )
        start = parse_hotkey_text(start_text)
        stop = parse_hotkey_text(stop_text)
        toggle = bool(
            start
            and stop
            and hotkey_signature(start) == hotkey_signature(stop)
        )
        return {
            "start_hotkey": start_text,
            "stop_hotkey": stop_text,
            "toggle": toggle,
        }

    def start_auto_clicker_from_hotkey(self):
        """Start the configured clicker without stealing window focus."""
        window = self._auto_clicker_window
        if window is None or not window.isVisible():
            return False
        return window.request_start()

    def toggle_auto_clicker_from_hotkey(self):
        """Toggle the clicker from its single same-key global hotkey."""
        window = self._auto_clicker_window
        if window is None or not window.isVisible():
            return False
        return window.toggle_from_hotkey()

    def stop_auto_clicker(self):
        """Stop the standalone clicker without changing script lifecycle state."""
        window = self._auto_clicker_window
        if window is None or not window.is_running:
            return False
        window.request_stop()
        return True

    def log_setting_change(self, name, value, *, sensitive=None):
        if not self.is_switching_profile:
            self.mark_profile_dirty()
        # FR-20260809-D8-②: sensitive 透传与 host_view Protocol 声明对齐
        # （敏感值审计能力保留）。
        return self.log_controller.log_setting_change(
            name, value, sensitive=sensitive
        )

    def update_loop_ui(self, text):
        if text in ["指定次数", "指定时间(时)", "指定时间(分)", "指定时间(秒)"]:
            self.loop_val_edit.show()
        else:
            self.loop_val_edit.hide()

    def update_scale_memory_custom_ui(self, _=None):
        enabled = self.scale_memory_custom_chk.isChecked()
        self.scale_memory_preferred_spin.setEnabled(enabled)
        self.scale_memory_history_spin.setEnabled(enabled)

    def moveEvent(self, event):
        started_ns = time.perf_counter_ns()
        super().moveEvent(event)
        self.ui_performance.increment("ui.window.move_events")
        self.ui_performance.observe_since("ui.window.move_event", started_ns)
        self._schedule_live_interaction_idle("move")

    def resizeEvent(self, event):
        started_ns = time.perf_counter_ns()
        super().resizeEvent(event)
        self.ui_performance.increment("ui.window.resize_events")
        self.ui_performance.observe_since("ui.window.resize_event", started_ns)
        self._schedule_live_interaction_idle("resize")

    def nativeEvent(self, eventType, message):
        try:
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_ENTERSIZEMOVE:
                self._native_window_interaction = True
                self._live_interaction_idle_timer.stop()
                self.begin_live_window_interaction("move_or_resize")
            elif msg.message == WM_SIZING:
                self._native_window_interaction = True
                self.begin_live_window_interaction("resize")
            elif msg.message == WM_MOVING:
                self._native_window_interaction = True
                self.begin_live_window_interaction("move")
            elif msg.message == WM_EXITSIZEMOVE:
                self._native_window_interaction = False
                self._live_interaction_idle_timer.stop()
                self.end_live_window_interaction()
            if msg.message == WM_HOTKEY and self.hotkey_controller.handle_native_hotkey(
                msg.wParam
            ):
                return True, 0
        except (RuntimeError, ValueError, TypeError, AttributeError, OSError) as error:
            count = getattr(self, "_native_event_error_count", 0) + 1
            self._native_event_error_count = count
            if count <= 5:
                import sys

                sys.stderr.write(f"[fukuaRPA] nativeEvent error ({count}/5): {error}\n")
        return super().nativeEvent(eventType, message)

    def on_risk_tip_changed(self, enabled):
        self.settings.setValue("risk_tip_enabled", enabled)
        self.settings.sync()
        self.log_setting_change("运行前风险提示", "开启" if enabled else "关闭")

    def keyPressEvent(self, event):
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QTextEdit, QPlainTextEdit)):
            return super().keyPressEvent(event)
        editing_shortcut = (
            event.key() in (Qt.Key_Insert, Qt.Key_F8)
            or bool(event.modifiers() & Qt.ControlModifier)
            and event.key() in (Qt.Key_X, Qt.Key_V, Qt.Key_Z, Qt.Key_Y, Qt.Key_D)
        )
        if self._runtime_editing_locked and editing_shortcut:
            event.accept()
            return
        if event.modifiers() & Qt.ControlModifier:
            if event.key() == Qt.Key_C:
                self.copy_selected_row()
                return
            if event.key() == Qt.Key_X:
                self.cut_selected_row()
                return
            if event.key() == Qt.Key_V:
                self.paste_row_after_selected()
                return
            if event.key() == Qt.Key_Z:
                self.undo_task_change()
                return
            if event.key() == Qt.Key_Y:
                self.redo_task_change()
                return
            if event.key() == Qt.Key_D:
                self.insert_row_before_selected()
                return
        if event.key() == Qt.Key_Insert:
            self.insert_row_before_selected()
            return
        if event.key() == Qt.Key_F8:
            self.toggle_selected_breakpoint()
            return
        return super().keyPressEvent(event)

    def show_debug_variables_dialog(self, step, variables):
        values = dict(variables or {})
        lines = []
        for name in sorted(values):
            rendered = repr(values[name])
            if len(rendered) > 180:
                rendered = rendered[:177] + "..."
            lines.append(f"{name} = {rendered}")
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Information)
        message.setWindowTitle("运行变量")
        message.setText(
            f"当前暂停在第 {step} 步，共 {len(values)} 个可用变量。"
        )
        message.setInformativeText("内置变量和脚本自定义变量均为本次运行的即时快照。")
        message.setDetailedText("\n".join(lines) if lines else "暂无变量")
        message.setStandardButtons(QMessageBox.Ok)
        message.exec()
