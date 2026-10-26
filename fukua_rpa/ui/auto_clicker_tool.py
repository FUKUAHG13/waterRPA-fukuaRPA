# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false, reportOptionalCall=false, reportOptionalIterable=false, reportOptionalOperand=false
"""连点器独立工具窗口（极简默认 + 高级展开）。

连点器（原生指令 28.0）已从步骤命令列表移除，改由"工具"菜单打开本窗口承载：
配置参数后点"开始"立即运行，不作为脚本步骤。运行复用引擎的
``_execute_auto_clicker`` 逻辑，并把工具级停止事件以*局部谓词*
（``stop_requested=...``）注入连点器循环，从而能在独立开关下干净停止，
且**不改动共享的 ``engine.check_stop_flag``**。窗口可在脚本运行时查看和配置，
但脚本与连点器的启动入口互相拦截，避免双线程并发操作同一引擎。

UI 分两层：
- 默认视图（极简）：点击类型 + 速度预设 + 启动/停止快捷键 + 启停横幅。
- 高级视图（展开）：大循环/小循环/连击三组参数，以及随机间隔、
  矩形/椭圆落点、移动轨迹等选项。
  每项参数附带小问号提示说明（点击弹出说明框）。
"""

from __future__ import annotations

import contextlib
import logging
import threading
import weakref

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..constants import AUTO_CLICKER_COUNT_MAX
from ..random_distribution import CONCENTRATION_PRESETS
from ..win32_api import (
    HOTKEY_NAME_TO_VK,
    MOD_ORDER,
    hotkey_display_text,
)
from .auto_clicker_core import (
    _SPEED_PRESETS,
    _START_HOTKEY_KEY,
    _STOP_HOTKEY_KEY,
)
from .auto_clicker_mixins import (
    ClickerOptionsMixin,
    HotkeyMixin,
    RunnerLifecycleMixin,
)
from .components import (
    HelpBtn,
    NoWheelComboBox,
    enable_work_window_controls,
    fit_compact_text_button,
    fit_text_button,
)

logger = logging.getLogger(__name__)

# 高级参数说明文本
_HELP_TEXTS = {
    "big_loop": "大循环：最外层重复次数。\n两个值都为 0 = 无限循环直到手动停止；"
    "0～N 会按 1～N 随机，避免有限范围意外变成无限。\n例如：设 5～5 表示整段小循环重复 5 次。",
    "big_interval": "大循环间隔：每完成一轮小循环后等待的时间范围（毫秒）。\n例如：20000–35000 表示随机等待 20~35 秒。",
    "sub_loop": "小循环：每轮大循环内的点击序列重复次数。\n1 = 只执行一组连击；"
    "两个值都为 0 表示持续执行直到停止，0～N 会按 1～N 随机。",
    "sub_interval": "小循环间隔：每完成一组连击后等待的时间范围（毫秒）。",
    "jitter_xy": "随机坐标偏移：每次点击都在以原始点为中心的范围内重新取点。\n"
    "矩形：X/Y 分别是水平和垂直最大偏移；椭圆：X/Y 分别是水平和垂直半轴。\n"
    "落点集中程度 0 为范围内均匀分布，数值越大越靠近中心。\n"
    "预设值：均匀 0、自然 50、集中 80；选择自定义可填写 0～100。",
    "click_count": "连击次数：一次小循环内连续执行的点击次数。完成这里设定的点击次数后，记作完成 1 次小循环。\n"
    "例如：150000–250000 表示每次小循环随机点击 15~25 万次。",
    "hold": "保持时长：每次点击按下到松开的持续时间（毫秒）。\n"
    "必须先松开才能完成一次点击；默认 10 毫秒兼顾高速与兼容性，老旧程序若漏点可尝试 40 毫秒。\n"
    "实际每次点击周期约为保持时长加点击间隔，因此增加保持时长会降低最高点击速度。",
    "random_interval": "随机间隔：仅在点击速度选择“自定义”时可用。\n"
    "每次点击间隔会在“基础间隔 ± 随机范围”内连续随机变化，并保持不小于 0 秒。\n"
    "集中程度 0 为整个范围内均匀分布，数值越大越靠近基础间隔。\n"
    "预设值：均匀 0、自然 50、集中 80；选择自定义可填写 0～100。",
    "curve_strength": "弯曲强度：0.00 = 直线移动，1.00 = 最大弯曲。\n"
    "内部使用平滑曲线生成鼠标路径；仅在鼠标确实需要移动且移动耗时大于 0 时可见。",
    "fixed_center": "固定坐标：勾选后以指定坐标为点击中心；也可直接点“取点”，选取后会自动勾选并回填 X/Y。\n"
    "不勾选则跟随当前鼠标位置（点击后鼠标会回到原位）。",
}


class _ConcentrationSelector(QWidget):
    """Three readable presets plus an optional exact 0..100 value."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.combo = NoWheelComboBox()
        for label, value in CONCENTRATION_PRESETS:
            self.combo.addItem(label, value)
        self.combo.addItem("自定义", None)
        self.custom_spin = QSpinBox()
        self.custom_spin.setRange(0, 100)
        self.custom_spin.setValue(50)
        self.custom_spin.setMinimumWidth(72)
        layout.addWidget(self.combo)
        layout.addWidget(self.custom_spin)
        layout.addStretch(1)
        self.combo.currentIndexChanged.connect(self._sync_custom_visibility)
        self._sync_custom_visibility()

    def _sync_custom_visibility(self):
        self.custom_spin.setVisible(self.combo.currentData() is None)

    def value(self) -> int:
        preset = self.combo.currentData()
        return self.custom_spin.value() if preset is None else int(preset)


class AutoClickerToolWindow(ClickerOptionsMixin, HotkeyMixin, RunnerLifecycleMixin, QDialog):
    """连点器工具窗口：极简默认视图 + 可展开高级参数。"""

    def __init__(self, parent=None, engine=None):
        super().__init__(None)
        self._host_ref = weakref.ref(parent) if parent is not None else lambda: None
        self.engine = engine
        self._stop_event = threading.Event()
        self._thread = None
        self._advanced_visible = False
        self._hotkeys_active = False
        self._fixed_point_picker = None
        settings = getattr(parent, "settings", None)
        self._initial_start_hotkey = self._load_hotkey(
            settings, _START_HOTKEY_KEY, "F6"
        )
        self._initial_stop_hotkey = self._load_hotkey(
            settings, _STOP_HOTKEY_KEY, "F7"
        )
        # 同键启停已受支持：不再在加载时把相同的两个键拆分改写。

        self.setAttribute(Qt.WA_QuitOnClose, False)
        enable_work_window_controls(self)
        self.setWindowModality(Qt.NonModal)
        self.setWindowTitle("连点器")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self._build_simple_view(layout)
        self._build_advanced_view(layout)
        self._build_status_bar(layout)

        self._advanced_panel.setVisible(False)
        self._on_speed_changed(self.speed_combo.currentIndex())
        self._on_fixed_toggled(self.chk_fixed_center.isChecked())
        self._on_jitter_toggled(self.chk_jitter.isChecked())
        self._on_curve_toggled(self.chk_curve.isChecked())
        self._adjust_size()

    @property
    def is_running(self):
        """Return whether this tool currently owns its worker thread."""
        return bool(self._thread is not None and self._thread.isRunning())

    # ==================================================================
    # 极简默认视图
    # ==================================================================
    def _build_simple_view(self, parent_layout: QVBoxLayout):
        form = QFormLayout()
        form.setSpacing(10)

        # 点击类型
        self.btn_type_combo = NoWheelComboBox()
        self.btn_type_combo.addItem("鼠标左键", "left")
        self.btn_type_combo.addItem("鼠标中键", "middle")
        self.btn_type_combo.addItem("鼠标右键", "right")
        self.btn_type_combo.currentIndexChanged.connect(
            self._on_button_type_changed
        )
        form.addRow("点击类型:", self.btn_type_combo)

        # 速度预设
        self.speed_combo = NoWheelComboBox()
        for label, _, _ in _SPEED_PRESETS:
            self.speed_combo.addItem(label)
        self.speed_combo.setCurrentIndex(0)
        self.speed_combo.currentIndexChanged.connect(self._on_speed_changed)
        form.addRow("点击速度:", self.speed_combo)

        # 自定义基础间隔（仅"自定义"时显示）；随机范围位于本工具的高级选项。
        self.custom_interval_row = QWidget()
        ci_layout = QHBoxLayout(self.custom_interval_row)
        ci_layout.setContentsMargins(0, 0, 0, 0)
        self.interval_base_spin = QDoubleSpinBox()
        self.interval_base_spin.setRange(0.0, 3600.0)
        self.interval_base_spin.setDecimals(3)
        self.interval_base_spin.setSingleStep(0.01)
        self.interval_base_spin.setValue(0.1)
        self.interval_base_spin.setSuffix(" s")
        self.interval_base_spin.setMinimumWidth(120)
        self.interval_base_spin.valueChanged.connect(
            self._sync_interval_variation_limit
        )
        ci_layout.addWidget(self.interval_base_spin)
        ci_layout.addStretch(1)
        self.simple_form = form
        form.addRow("自定义间隔:", self.custom_interval_row)

        # 启动快捷键（QLineEdit 显示 + 录入按钮，复用 KeyCaptureDialog）
        start_row = QWidget()
        sr_layout = QHBoxLayout(start_row)
        sr_layout.setContentsMargins(0, 0, 0, 0)
        self.start_key_edit = QLineEdit(self._initial_start_hotkey)
        self.start_key_edit.setReadOnly(True)
        self._configure_hotkey_edit(self.start_key_edit)
        start_record_btn = QPushButton("录入")
        start_record_btn.setFixedWidth(60)
        start_record_btn.clicked.connect(lambda: self._capture_hotkey(self.start_key_edit))
        sr_layout.addWidget(self.start_key_edit)
        sr_layout.addWidget(start_record_btn)
        sr_layout.addStretch(1)
        form.addRow("启动快捷键:", start_row)

        # 停止快捷键
        stop_row = QWidget()
        sp_layout = QHBoxLayout(stop_row)
        sp_layout.setContentsMargins(0, 0, 0, 0)
        self.stop_key_edit = QLineEdit(self._initial_stop_hotkey)
        self.stop_key_edit.setReadOnly(True)
        self._configure_hotkey_edit(self.stop_key_edit)
        stop_record_btn = QPushButton("录入")
        stop_record_btn.setFixedWidth(60)
        stop_record_btn.clicked.connect(lambda: self._capture_hotkey(self.stop_key_edit))
        sp_layout.addWidget(self.stop_key_edit)
        sp_layout.addWidget(stop_record_btn)
        sp_layout.addStretch(1)
        form.addRow("停止快捷键:", stop_row)

        parent_layout.addLayout(form)

        # 启停横幅
        self.banner_label = QLabel("按 F6 开始 · F7 停止 · 左键连点")
        self.banner_label.setObjectName("autoClickerBanner")
        self.banner_label.setAlignment(Qt.AlignCenter)
        self.banner_label.setMinimumHeight(58)
        banner_font = self.banner_label.font()
        app = QApplication.instance()
        scale = float(app.property("_fukua_ui_scale") or 1.0) if app else 1.0
        banner_font.setPointSizeF(max(banner_font.pointSizeF() + 2.0, 14.0 * scale))
        banner_font.setBold(True)
        self.banner_label.setFont(banner_font)
        self._set_banner_state("idle")
        parent_layout.addWidget(self.banner_label)

        # 按钮行
        btn_row = QHBoxLayout()
        self.start_btn = QPushButton("开始")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.start_btn.setMinimumWidth(100)
        self.stop_btn.setMinimumWidth(100)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addStretch(1)
        self.advanced_toggle = QPushButton("高级设置 ▸")
        self.advanced_toggle.setObjectName("autoClickerAdvancedToggle")
        fit_text_button(self.advanced_toggle, 104)
        self.advanced_toggle.setFlat(True)
        self.advanced_toggle.clicked.connect(self._toggle_advanced)
        btn_row.addWidget(self.advanced_toggle)
        parent_layout.addLayout(btn_row)

        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)

        # 注册默认快捷键
        self._register_shortcuts()

    @staticmethod
    def _configure_hotkey_edit(edit: QLineEdit) -> None:
        """按当前字体为项目支持的最长快捷键保留完整显示空间。"""

        metrics = edit.fontMetrics()
        display_candidates = (
            hotkey_display_text("+".join((*MOD_ORDER, key_name)))
            for key_name in HOTKEY_NAME_TO_VK
        )
        longest_expected = max(
            metrics.horizontalAdvance(text) for text in display_candidates
        )
        padding = metrics.horizontalAdvance("  ")
        edit.setMinimumWidth(
            max(edit.sizeHint().width(), longest_expected + padding)
        )
        edit.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    # ==================================================================
    # 高级展开视图
    # ==================================================================
    def _build_advanced_view(self, parent_layout: QVBoxLayout):
        self._advanced_panel = QWidget()
        adv_layout = QVBoxLayout(self._advanced_panel)
        adv_layout.setContentsMargins(0, 8, 0, 0)
        adv_layout.setSpacing(8)

        # --- 大循环参数 ---
        big_group = QGroupBox("大循环参数")
        big_form = QFormLayout(big_group)
        big_form.setSpacing(6)
        big_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.big_loop_min = QSpinBox()
        self.big_loop_min.setRange(0, 999999)
        self.big_loop_min.setValue(0)
        self.big_loop_min.setMinimumWidth(90)
        self.big_loop_max = QSpinBox()
        self.big_loop_max.setRange(0, 999999)
        self.big_loop_max.setValue(0)
        self.big_loop_max.setMinimumWidth(90)
        big_form.addRow(
            self._help_label("循环:", "big_loop"),
            self._range_row(self.big_loop_min, self.big_loop_max, "次（0=无限）"),
        )
        self.big_interval_min = QSpinBox()
        self.big_interval_min.setRange(0, 3600000)
        self.big_interval_min.setValue(0)
        self.big_interval_min.setSuffix(" ms")
        self.big_interval_min.setMinimumWidth(90)
        self.big_interval_max = QSpinBox()
        self.big_interval_max.setRange(0, 3600000)
        self.big_interval_max.setValue(0)
        self.big_interval_max.setSuffix(" ms")
        self.big_interval_max.setMinimumWidth(90)
        big_form.addRow(
            self._help_label("间隔:", "big_interval"),
            self._range_row(self.big_interval_min, self.big_interval_max),
        )
        adv_layout.addWidget(big_group)

        # --- 小循环参数 ---
        sub_group = QGroupBox("小循环参数")
        sub_form = QFormLayout(sub_group)
        sub_form.setSpacing(6)
        sub_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.sub_loop_min = QSpinBox()
        self.sub_loop_min.setRange(0, 999999)
        self.sub_loop_min.setValue(1)
        self.sub_loop_min.setMinimumWidth(90)
        self.sub_loop_max = QSpinBox()
        self.sub_loop_max.setRange(0, 999999)
        self.sub_loop_max.setValue(1)
        self.sub_loop_max.setMinimumWidth(90)
        sub_form.addRow(
            self._help_label("循环:", "sub_loop"),
            self._range_row(self.sub_loop_min, self.sub_loop_max, "次"),
        )
        self.sub_interval_min = QSpinBox()
        self.sub_interval_min.setRange(0, 3600000)
        self.sub_interval_min.setValue(0)
        self.sub_interval_min.setSuffix(" ms")
        self.sub_interval_min.setMinimumWidth(90)
        self.sub_interval_max = QSpinBox()
        self.sub_interval_max.setRange(0, 3600000)
        self.sub_interval_max.setValue(0)
        self.sub_interval_max.setSuffix(" ms")
        self.sub_interval_max.setMinimumWidth(90)
        sub_form.addRow(
            self._help_label("间隔:", "sub_interval"),
            self._range_row(self.sub_interval_min, self.sub_interval_max),
        )
        adv_layout.addWidget(sub_group)

        # --- 连击参数 ---
        click_group = QGroupBox("连击参数")
        click_form = QFormLayout(click_group)
        click_form.setSpacing(6)
        click_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.click_count_min = QSpinBox()
        self.click_count_min.setRange(1, AUTO_CLICKER_COUNT_MAX)
        self.click_count_min.setValue(1)
        self.click_count_min.setMinimumWidth(90)
        self.click_count_max = QSpinBox()
        self.click_count_max.setRange(1, AUTO_CLICKER_COUNT_MAX)
        self.click_count_max.setValue(1)
        self.click_count_max.setMinimumWidth(90)
        click_form.addRow(
            self._help_label("连击:", "click_count"),
            self._range_row(self.click_count_min, self.click_count_max, "次"),
        )
        self.hold_min = QSpinBox()
        self.hold_min.setRange(0, 10000)
        self.hold_min.setValue(10)
        self.hold_min.setSuffix(" ms")
        self.hold_min.setMinimumWidth(90)
        self.hold_max = QSpinBox()
        self.hold_max.setRange(0, 10000)
        self.hold_max.setValue(10)
        self.hold_max.setSuffix(" ms")
        self.hold_max.setMinimumWidth(90)
        click_form.addRow(
            self._help_label("保持:", "hold"),
            self._range_row(self.hold_min, self.hold_max),
        )
        adv_layout.addWidget(click_group)

        # --- 自定义点击速度的随机时间范围 ---
        self.random_interval_group = QGroupBox("自定义间隔随机化")
        random_interval_group_layout = QVBoxLayout(self.random_interval_group)
        random_interval_group_layout.setSpacing(8)
        self.random_interval_row = QWidget()
        random_interval_layout = QVBoxLayout(self.random_interval_row)
        random_interval_layout.setContentsMargins(0, 0, 0, 0)
        random_interval_layout.setSpacing(6)
        random_interval_header = QHBoxLayout()
        self.chk_random_interval = QCheckBox("随机间隔")
        self.chk_random_interval.toggled.connect(self._sync_random_interval_ui)
        random_interval_header.addWidget(self.chk_random_interval)
        random_interval_header.addWidget(self._help_btn("random_interval"))
        random_interval_header.addStretch(1)
        random_interval_layout.addLayout(random_interval_header)

        self.random_interval_controls = QWidget()
        random_interval_form = QFormLayout(self.random_interval_controls)
        random_interval_form.setContentsMargins(24, 0, 0, 0)
        random_interval_form.setSpacing(6)
        random_interval_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.interval_variation_spin = QDoubleSpinBox()
        self.interval_variation_spin.setRange(0.0, self.interval_base_spin.value())
        self.interval_variation_spin.setDecimals(3)
        self.interval_variation_spin.setSingleStep(0.01)
        self.interval_variation_spin.setValue(0.05)
        self.interval_variation_spin.setSuffix(" s")
        self.interval_variation_spin.setMinimumWidth(110)
        self.interval_concentration = _ConcentrationSelector()
        random_interval_form.addRow("随机范围 ±:", self.interval_variation_spin)
        random_interval_form.addRow("时间集中程度:", self.interval_concentration)
        random_interval_layout.addWidget(self.random_interval_controls)
        random_interval_group_layout.addWidget(self.random_interval_row)
        adv_layout.addWidget(self.random_interval_group)

        # --- 点击位置与鼠标移动 ---
        self.position_movement_group = QGroupBox("点击位置与移动")
        opts_layout = QVBoxLayout(self.position_movement_group)
        opts_layout.setSpacing(8)

        # 随机坐标偏移：形状控制边界，集中程度控制区域内的密度。
        jitter_header = QHBoxLayout()
        self.chk_jitter = QCheckBox("随机坐标偏移")
        self.chk_jitter.toggled.connect(self._on_jitter_toggled)
        jitter_header.addWidget(self.chk_jitter)
        jitter_header.addWidget(self._help_btn("jitter_xy"))
        jitter_header.addStretch(1)
        opts_layout.addLayout(jitter_header)

        self.jitter_controls = QWidget()
        jitter_form = QFormLayout(self.jitter_controls)
        jitter_form.setContentsMargins(24, 0, 0, 0)
        jitter_form.setSpacing(6)
        jitter_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.jitter_shape_combo = NoWheelComboBox()
        self.jitter_shape_combo.addItem("矩形", "rectangle")
        self.jitter_shape_combo.addItem("椭圆形", "ellipse")
        jitter_form.addRow("范围形状:", self.jitter_shape_combo)
        self.jitter_x_spin = QSpinBox()
        self.jitter_x_spin.setRange(0, 500)
        self.jitter_x_spin.setSuffix(" px")
        self.jitter_x_spin.setMinimumWidth(90)
        self.jitter_y_spin = QSpinBox()
        self.jitter_y_spin.setRange(0, 500)
        self.jitter_y_spin.setSuffix(" px")
        self.jitter_y_spin.setMinimumWidth(90)
        jitter_form.addRow(
            "最大偏移 X/Y:",
            self._range_row(self.jitter_x_spin, self.jitter_y_spin),
        )
        self.jitter_concentration = _ConcentrationSelector()
        jitter_form.addRow("落点集中程度:", self.jitter_concentration)
        opts_layout.addWidget(self.jitter_controls)

        # 随机移动轨迹 + 弯曲强度（属于连点器自身，不受主程序视图模式影响）。
        curve_row = QHBoxLayout()
        self.chk_curve = QCheckBox("随机移动轨迹")
        self.chk_curve.toggled.connect(self._on_curve_toggled)
        curve_row.addWidget(self.chk_curve)
        self.curve_strength_label = QLabel("弯曲强度:")
        curve_row.addWidget(self.curve_strength_label)
        self.curve_strength_spin = QDoubleSpinBox()
        self.curve_strength_spin.setRange(0.0, 1.0)
        self.curve_strength_spin.setValue(0.5)
        self.curve_strength_spin.setSingleStep(0.1)
        self.curve_strength_spin.setDecimals(2)
        self.curve_strength_spin.setMinimumWidth(90)
        curve_row.addWidget(self.curve_strength_spin)
        curve_row.addWidget(self._help_btn("curve_strength"))
        curve_row.addStretch(1)
        opts_layout.addLayout(curve_row)

        # 固定坐标
        fixed_row = QHBoxLayout()
        self.chk_fixed_center = QCheckBox("固定坐标中心")
        fixed_row.addWidget(self.chk_fixed_center)
        fixed_row.addWidget(self._help_btn("fixed_center"))
        self.fixed_x_spin = QSpinBox()
        self.fixed_x_spin.setRange(-32768, 32768)
        self.fixed_x_spin.setMinimumWidth(80)
        self.fixed_y_spin = QSpinBox()
        self.fixed_y_spin.setRange(-32768, 32768)
        self.fixed_y_spin.setMinimumWidth(80)
        self._fixed_x_label = QLabel("X:")
        fixed_row.addWidget(self._fixed_x_label)
        fixed_row.addWidget(self.fixed_x_spin)
        self._fixed_y_label = QLabel("Y:")
        fixed_row.addWidget(self._fixed_y_label)
        fixed_row.addWidget(self.fixed_y_spin)
        self.fixed_pick_btn = fit_compact_text_button(QPushButton("取点"), 48)
        self.fixed_pick_btn.clicked.connect(self._start_fixed_point_pick)
        fixed_row.addWidget(self.fixed_pick_btn)
        fixed_row.addStretch(1)
        self.chk_fixed_center.toggled.connect(self._on_fixed_toggled)
        opts_layout.addLayout(fixed_row)

        adv_layout.addWidget(self.position_movement_group)
        parent_layout.addWidget(self._advanced_panel)

    def _set_banner_state(self, state):
        state = str(state)
        if state not in {"idle", "running", "error"}:
            state = "idle"
        if self.banner_label.property("bannerState") == state:
            return
        self.banner_label.setProperty("bannerState", state)
        style = self.banner_label.style()
        style.unpolish(self.banner_label)
        style.polish(self.banner_label)
        self.banner_label.updateGeometry()

    # ==================================================================
    # 状态栏
    # ==================================================================
    def _build_status_bar(self, parent_layout: QVBoxLayout):
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("autoClickerStatus")
        self.status_label.setProperty("role", "muted")
        parent_layout.addWidget(self.status_label)

    # ==================================================================
    # 辅助构建方法
    # ==================================================================
    @staticmethod
    def _range_row(spin_a: QSpinBox, spin_b: QSpinBox, suffix: str = "") -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(spin_a)
        layout.addWidget(QLabel("–"))
        layout.addWidget(spin_b)
        if suffix:
            layout.addWidget(QLabel(suffix))
        layout.addStretch(1)
        return row

    @staticmethod
    def _help_label(text: str, help_key: str) -> QWidget:
        """创建带小问号的标签行（使用项目标准 HelpBtn）。"""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(QLabel(text))
        layout.addWidget(HelpBtn(_HELP_TEXTS.get(help_key, "")))
        layout.addStretch(1)
        return row

    @staticmethod
    def _help_btn(help_key: str) -> HelpBtn:
        """创建小问号按钮（使用项目标准 HelpBtn）。"""
        return HelpBtn(_HELP_TEXTS.get(help_key, ""))

    def _adjust_size(self):
        banner_width = (
            self.banner_label.fontMetrics().horizontalAdvance(
                self.banner_label.text()
            )
            + 72
        )
        minimum_width = max(614, banner_width)
        self.setMinimumWidth(minimum_width)
        self.setMaximumWidth(16_777_215)
        if self._advanced_visible:
            self.setWindowTitle("连点器 - 高级设置")
        else:
            self.setWindowTitle("连点器")
        if self.width() < minimum_width:
            self.resize(minimum_width, self.height())

    # ==================================================================
    # 展开/收起 + 速度切换
    # ==================================================================
    def _toggle_advanced(self):
        preserved_width = self.width()
        self._advanced_visible = not self._advanced_visible
        self._advanced_panel.setVisible(self._advanced_visible)
        self.advanced_toggle.setText(
            "◂ 收起" if self._advanced_visible else "高级设置 ▸"
        )
        self.advanced_toggle.updateGeometry()
        self._adjust_size()
        target_height = max(self.minimumHeight(), self.sizeHint().height())
        self.resize(max(preserved_width, self.minimumWidth()), target_height)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._hotkeys_active:
            self._hotkeys_active = True
            self._register_shortcuts()

    def closeEvent(self, event):
        self._hotkeys_active = False
        host = self._host_ref()
        controller = getattr(host, "hotkey_controller", None)
        if controller is not None and not getattr(
            host, "_close_state_saved", False
        ):
            controller.refresh_hotkey_backend()
        if host is not None:
            host.hide_running_status_overlay("连点器")
        picker = self._fixed_point_picker
        if picker is not None:
            with contextlib.suppress(RuntimeError):
                picker.close()
            self._fixed_point_picker = None
        thread = self._thread
        if thread is not None and thread.isRunning():
            self.request_stop()
            if not thread.wait(2000):
                logger.warning("连点器工具窗口关闭时工作线程未在 2 秒内结束")
                # SAFETY: 设计基准"独立输入工具"——关闭工具窗口必须发出停止并有界
                # 等待，**超时应记录并继续执行统一输入释放**，不能让鼠标键或键盘键
                # 保持按下（ARCHITECTURE #87，审查 FR-005）。线程卡在中间状态
                # （长按保持中）时逐次 try/finally 尚未执行，此处是唯一兜底。
                # release 幂等，且只释放本引擎注入的输入（tracked 集合），
                # 与残存 worker 并发安全。
                with contextlib.suppress(Exception):
                    self.engine.release_injected_inputs()
        super().closeEvent(event)
