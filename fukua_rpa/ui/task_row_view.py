# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Qt control assembly for an editable workflow step row."""

import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ..commands import command_names
from ..paths import get_resource_path
from .components import HistoryComboBox, HistoryLineEdit


class RefreshableHoverButton(QPushButton):
    """Keep a hover highlight when list reflow moves this button under the cursor."""

    def set_synthetic_hover(self, enabled):
        enabled = bool(enabled)
        if bool(self.property("syntheticHover")) == enabled:
            return
        self.setProperty("syntheticHover", enabled)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def enterEvent(self, event):
        self.set_synthetic_hover(False)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.set_synthetic_hover(False)
        super().leaveEvent(event)


class StepIndexLabel(QLabel):
    """Reserve enough width for the largest index in the current workflow."""

    BASE_WIDTH = 32

    def __init__(self, parent=None):
        super().__init__("1.", parent)
        self._maximum_index = 1
        self.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)

    def set_index(self, index, maximum_index=None):
        self._maximum_index = max(
            1,
            int(maximum_index if maximum_index is not None else index),
        )
        self.setText(f"{int(index)}.")
        self.updateGeometry()

    def sizeHint(self):
        hint = super().sizeHint()
        text_width = self.fontMetrics().horizontalAdvance(
            f"{self._maximum_index}."
        )
        return QSize(max(self.BASE_WIDTH, text_width + 12), hint.height())

    def minimumSizeHint(self):
        return self.sizeHint()


def build_task_row_view(row, delete_callback):
    self = row
    self.setFrameShape(QFrame.StyledPanel)
    self.set_selected(False)
    self.outer_layout = QVBoxLayout(self)
    self.outer_layout.setContentsMargins(0, 0, 0, 0)
    self.outer_layout.setSpacing(0)
    self.group_header_frames = []
    self.group_toggle_buttons = []
    self.group_header_labels = []
    self.group_header_counts = []
    for depth in range(3):
        header = QFrame()
        header.setObjectName("taskGroupHeader")
        header.setProperty("groupDepth", depth + 1)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8 + depth * 14, 3, 8, 3)
        header_layout.setSpacing(6)
        toggle = QPushButton("")
        toggle.setFixedSize(24, 24)
        toggle.setProperty("iconOnly", True)
        toggle.setProperty("variant", "ghost")
        toggle.clicked.connect(
            lambda _checked=False, index=depth: self.toggle_group_header(index)
        )
        title = QLabel("未命名分组")
        title.setProperty("role", "groupTitle")
        count = QLabel("0 步")
        count.setProperty("role", "muted")
        header_layout.addWidget(toggle)
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        header_layout.addWidget(count)
        header.hide()
        self.outer_layout.addWidget(header)
        self.group_header_frames.append(header)
        self.group_toggle_buttons.append(toggle)
        self.group_header_labels.append(title)
        self.group_header_counts.append(count)

    self.controls_widget = QWidget()
    self.controls_widget.setObjectName("taskControls")
    self.layout = QHBoxLayout(self.controls_widget)
    self.layout.setContentsMargins(8, 5, 7, 5)
    self.layout.setSpacing(6)
    self.outer_layout.addWidget(self.controls_widget)

    self.index_label = StepIndexLabel()
    self.index_label.setObjectName("stepIndex")
    self.index_label.setAlignment(Qt.AlignCenter)
    self.index_label.setCursor(Qt.PointingHandCursor)
    self.index_label.setToolTip("点击步骤序号可选中；按 Ctrl 或 Shift 可多选")
    self.layout.addWidget(self.index_label)

    self.manual_mark_label = QLabel("修")
    self.manual_mark_label.setObjectName("manualMark")
    self.manual_mark_label.setFixedSize(20, 20)
    self.manual_mark_label.setAlignment(Qt.AlignCenter)
    self.manual_mark_label.setToolTip("本步骤包含坐标步进手动修正点")
    self.manual_mark_label.hide()
    self.layout.addWidget(self.manual_mark_label)

    self.disabled_mark_label = QLabel("停")
    self.disabled_mark_label.setObjectName("disabledMark")
    self.disabled_mark_label.setFixedSize(20, 20)
    self.disabled_mark_label.setAlignment(Qt.AlignCenter)
    self.disabled_mark_label.setToolTip("本步骤已暂时禁用；运行和预览时会跳过")
    self.disabled_mark_label.hide()
    self.layout.addWidget(self.disabled_mark_label)

    self.breakpoint_mark_label = QLabel("断")
    self.breakpoint_mark_label.setObjectName("breakpointMark")
    self.breakpoint_mark_label.setFixedSize(20, 20)
    self.breakpoint_mark_label.setAlignment(Qt.AlignCenter)
    self.breakpoint_mark_label.setToolTip(
        "本步骤已设置调试断点；运行到这里会在执行前暂停"
    )
    self.breakpoint_mark_label.hide()
    self.layout.addWidget(self.breakpoint_mark_label)

    self.type_combo = HistoryComboBox()
    self.type_combo.addItems(
        command_names(include_advanced=self.settings_mode == "advanced")
    )
    self.type_combo.setMinimumWidth(130)
    self.type_combo.currentTextChanged.connect(self.on_type_changed)
    self.type_combo.activated.connect(self.finish_type_history)
    self.type_combo.interactionStarted.connect(self.begin_type_history)
    self.layout.addWidget(self.type_combo)

    self.value_input = HistoryLineEdit()
    self.value_input.setMinimumWidth(0)
    self.value_input.textChanged.connect(self.sync_data)
    self.value_input.editingFinished.connect(self.finish_value_history)
    self.value_input.focusStarted.connect(self.begin_value_history)
    self.layout.addWidget(self.value_input, 2)

    self.note_display = QLineEdit()
    self.note_display.setMinimumWidth(72)
    self.note_display.setReadOnly(False)
    self.note_display.setFocusPolicy(Qt.StrongFocus)
    self.note_display.setPlaceholderText("备注")
    self.note_display.setToolTip("点击备注框可直接修改；回车或点击别处保存")
    self.note_display.editingFinished.connect(self.on_note_edited)
    self.note_display.hide()
    self.layout.addWidget(self.note_display, 1)

    self.file_btn = QPushButton("图")
    self.file_btn.setFixedWidth(30)
    self.file_btn.setProperty("iconOnly", True)
    self.file_btn.setProperty("variant", "ghost")
    self.file_btn.setToolTip("选择图片文件")
    self.file_btn.clicked.connect(self.select_file)
    self.layout.addWidget(self.file_btn)

    self.template_capture_btn = QPushButton("框")
    self.template_capture_btn.setFixedWidth(30)
    self.template_capture_btn.setProperty("iconOnly", True)
    self.template_capture_btn.setProperty("variant", "tonal")
    self.template_capture_btn.setToolTip(
        "框选屏幕目标并自动保存为模板\n"
        "模板会保存到配置目录的 templates 文件夹，并自动填入当前步骤。"
    )
    self.template_capture_btn.clicked.connect(self.start_template_capture)
    self.layout.addWidget(self.template_capture_btn)

    self.recognition_test_btn = QPushButton("测")
    self.recognition_test_btn.setFixedWidth(30)
    self.recognition_test_btn.setProperty("iconOnly", True)
    self.recognition_test_btn.setProperty("variant", "tonal")
    self.recognition_test_btn.setToolTip(
        "测试识别（不会点击）\n"
        "按本步骤当前的缩放、灰度、相似度和识别区域查找一次；"
        "命中后只高亮图片范围与实际点击点。"
    )
    self.recognition_test_btn.clicked.connect(self.test_image_recognition)
    self.layout.addWidget(self.recognition_test_btn)

    self.pick_btn = QPushButton("取")
    self.pick_btn.setFixedWidth(30)
    self.pick_btn.setProperty("iconOnly", True)
    self.pick_btn.setProperty("variant", "tonal")
    self.pick_btn.setToolTip(
        "选取屏幕坐标\n单击/悬停：左键单击目标位置\n拖拽：按住左键拖动并松开\n右键取消"
    )
    self.pick_btn.clicked.connect(self.handle_pick_button)
    self.layout.addWidget(self.pick_btn)

    self.cfg_btn = QPushButton("")
    settings_icon = QIcon(
        get_resource_path(os.path.join("assets", "step_settings.svg"))
    )
    if settings_icon.isNull():
        settings_icon = self.style().standardIcon(QStyle.SP_FileDialogDetailedView)
    self.cfg_btn.setIcon(settings_icon)
    self.cfg_btn.setFixedWidth(30)
    self.cfg_btn.setProperty("iconOnly", True)
    self.cfg_btn.setProperty("variant", "ghost")
    self.cfg_btn.setToolTip(
        "步骤设置\n包含图片识别参数、重复次数、同点点击上限和条件分支"
    )
    self.cfg_btn.clicked.connect(self.open_custom_config)
    self.layout.addWidget(self.cfg_btn)

    self.del_btn = RefreshableHoverButton("")
    self.del_btn.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
    self.del_btn.setFixedWidth(30)
    self.del_btn.setProperty("iconOnly", True)
    self.del_btn.setProperty("variant", "dangerGhost")
    self.del_btn.setToolTip("删除此步骤")
    self.del_btn.clicked.connect(lambda: delete_callback(self))
    self.layout.addWidget(self.del_btn)

    self.on_type_changed(self.type_combo.currentText())
