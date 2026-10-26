# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false, reportOptionalCall=false, reportOptionalIterable=false, reportOptionalOperand=false, reportAssignmentType=false

"""Editable workflow step row and its draft-preserving UI boundary.

The row displays one action but retains all action-specific drafts. Capability
rules decide what is editable/effective; changing the combo must not discard
settings that may become relevant again. See ADR 0025.
"""

import logging
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QLineEdit,
    QSlider,
)

from ..commands import (
    SETTING_COORDINATE_PATH,
    command_names,
)
from ..constants import (
    TASK_TYPE_DIRECTORY_SELECT,
    TASK_TYPE_LAUNCH_APP,
)
from ..multi_image_actions import multi_image_action_summary
from ..task_model import (
    config_bool,
    normalize_coord_step_direction,
    parse_coord_step_manual_points,
    task_data_defaults,
    until_condition_summary,
)
from ..workflow_metadata import normalize_step_note

# 摘要实现已迁到无 Qt 依赖的 workflow_summary（ADR 0040）；此处重导出以保持
# ``from fukua_rpa.ui.task_row import task_drag_summary`` 的既有调用方可用。
from ..workflow_summary import task_drag_summary
from .task_row_binding import apply_task_row_data, collect_task_row_data
from .task_row_controllers import TaskConfigDialogController, TemplateCaptureController
from .task_row_mixins import (
    TaskRowConfigCapabilityMixin,
    TaskRowEditHistoryMixin,
    TaskRowPickerMixin,
    TaskRowPresentationMixin,
)
from .task_row_presentation import (
    apply_task_type_presentation,
    show_multi_image_action_summary,
    show_until_condition_summary,
)
from .task_row_view import RefreshableHoverButton, build_task_row_view
from .template_notice import show_transparent_template_warning_once

logger = logging.getLogger(__name__)
__all__ = ["RefreshableHoverButton", "TaskRow", "task_drag_summary"]


class TaskRow(TaskRowConfigCapabilityMixin, TaskRowEditHistoryMixin, TaskRowPickerMixin, TaskRowPresentationMixin, QFrame):
    def __init__(self, delete_callback, settings_mode="simple"):
        super().__init__()
        self.setObjectName("taskRow")
        self.setMinimumHeight(42)
        self.parent_item = None
        self._list_captured = False
        self._value_edit_snapshot = None
        self._value_edit_original = ""
        self._type_edit_snapshot = None
        self._type_edit_original = ""
        self._last_type_name = ""
        self._loading_data = False
        self._unsupported_task_type = None
        self._unsupported_task_label = ""
        self._unsupported_task_payload = None
        self._show_note_display = False
        self._hidden_reveal_effect = None
        self.template_region_window = None
        self.template_capture_controller = TemplateCaptureController(self)
        self.config_dialog_controller = TaskConfigDialogController(self)
        self.settings_mode = (
            "advanced" if str(settings_mode) == "advanced" else "simple"
        )
        self.custom_data = task_data_defaults()

        build_task_row_view(self, delete_callback)

    def set_runtime_editing_enabled(self, enabled):
        controls = [
            self.type_combo,
            self.value_input,
            self.file_btn,
            self.template_capture_btn,
            self.recognition_test_btn,
            self.pick_btn,
            self.cfg_btn,
            self.del_btn,
            *getattr(self, "group_toggle_buttons", ()),
        ]
        # DESIGN: 备注输入框同样属于运行时编辑面，锁定时必须一并禁用，
        # 否则用户仍可在运行期间改写步骤备注（见 docs/设计基准.md 运行时编辑锁）。
        if getattr(self, "note_display", None) is not None:
            controls.append(self.note_display)
        enabled = bool(enabled)
        if not enabled:
            if not hasattr(self, "_runtime_control_states"):
                self._runtime_control_states = {
                    control: control.isEnabled() for control in controls
                }
            for control in controls:
                control.setEnabled(False)
            return
        states = getattr(self, "_runtime_control_states", None)
        if states is None:
            return
        try:
            from shiboken6 import isValid
        except ImportError:
            def isValid(w):
                return True
        for control, was_enabled in states.items():
            if isValid(control):
                control.setEnabled(was_enabled)
        del self._runtime_control_states

    def set_settings_mode(self, mode):
        mode = "advanced" if str(mode) == "advanced" else "simple"
        current_name = self.type_combo.currentText()
        supported_options = command_names(
            include_advanced=mode == "advanced",
            preserve_names=(current_name,),
        )
        unsupported_label = self._unsupported_task_label
        options = (
            [unsupported_label, *supported_options]
            if unsupported_label and current_name == unsupported_label
            else supported_options
        )
        self.settings_mode = mode
        self.type_combo.blockSignals(True)
        self.type_combo.clear()
        self.type_combo.addItems(options)
        if current_name in options:
            self.type_combo.setCurrentText(current_name)
        self.type_combo.blockSignals(False)
        self.update_markers()

    def set_parent_item(self, item):
        self.parent_item = item
        self.sync_data()

    # Interactive controls keep their native behaviour (edit/focus/activate)
    # instead of being forwarded to the list gesture machine.
    _FORWARD_EXCLUDE_TYPES = (
        QAbstractButton,
        QComboBox,
        QLineEdit,
        QAbstractSpinBox,
        QSlider,
    )

    def _press_landed_on_control(self, pos):
        child = self.childAt(pos)
        while child is not None:
            if isinstance(child, self._FORWARD_EXCLUDE_TYPES):
                return True
            if child is self:
                break
            child = child.parent()
        return False

    def mousePressEvent(self, event):
        self._list_captured = False
        if (
            event.button() == Qt.LeftButton
            and self.parent_item is not None
            and not self._press_landed_on_control(event.pos())
        ):
            task_list = self.parent_item.listWidget()
            route = getattr(task_list, "route_row_mouse_event", None)
            if callable(route):
                route(event, self, "press")
                self._list_captured = True
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._list_captured and event.buttons() & Qt.LeftButton:
            task_list = self.parent_item.listWidget()
            route = getattr(task_list, "route_row_mouse_event", None)
            if callable(route):
                route(event, self, "move")
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._list_captured and event.button() == Qt.LeftButton:
            task_list = self.parent_item.listWidget()
            route = getattr(task_list, "route_row_mouse_event", None)
            if callable(route):
                route(event, self, "release")
                self._list_captured = False
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def set_selected(self, selected):
        self.setProperty("selected", bool(selected))
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_note_column_width()

    def _sync_note_column_width(self):
        """Keep values aligned while optional buttons consume note space.

        DESIGN: rows expose different action buttons (notably image testing).
        With inline notes enabled, the value width is derived only from the row
        width, so an extra button shortens the note instead of shifting the
        value boundary relative to neighboring rows.
        """

        if not getattr(self, "_show_note_display", False):
            minimum, maximum = 0, 16_777_215
        else:
            row_width = max(0, self.controls_widget.width())
            minimum = maximum = max(180, int(row_width * 0.42))
        if self.value_input.minimumWidth() != minimum:
            self.value_input.setMinimumWidth(minimum)
        if self.value_input.maximumWidth() != maximum:
            self.value_input.setMaximumWidth(maximum)

    def sync_data(self):
        text = self.type_combo.currentText()
        coord_mode = self.is_direct_coordinate_value(text)
        self.update_markers()
        if text == "直到条件成立":
            self.cfg_btn.setVisible(True)
            self.cfg_btn.setToolTip(
                "步骤设置\n设置图片出现/消失、区域变化或多条件组合；未满足时可跳回指定步骤。"
            )
        elif text == "多图动作映射":
            self.cfg_btn.setVisible(True)
            self.cfg_btn.setToolTip("步骤设置\n按顺序配置多张图片及各自命中后执行的动作")
        elif "单击" in text or "双击" in text or "悬停" in text:
            self.cfg_btn.setVisible(True)
            if coord_mode:
                self.cfg_btn.setToolTip(
                    "步骤设置\n当前参数是屏幕坐标，图片识别参数会自动忽略；重复和条件分支仍然生效"
                )
            else:
                self.cfg_btn.setToolTip(
                    "步骤设置\n包含图片识别参数、图片内点击点、重复次数、同点点击上限和条件分支"
                )
        else:
            self.cfg_btn.setVisible(True)
            self.cfg_btn.setToolTip("步骤设置\n包含重复次数和条件分支")

        if text == "系统按键":
            self.pick_btn.setVisible(True)
            self.pick_btn.setText("键")
            self.pick_btn.setToolTip(
                "录入按键或组合键\n点击后直接按下要填写的键，例如 A、Enter、Ctrl+C"
            )
        elif text in ("等待窗口", "激活窗口", "关闭窗口"):
            self.pick_btn.setVisible(True)
            self.pick_btn.setText("窗")
            self.pick_btn.setToolTip("从屏幕上选取目标程序窗口")
        elif text in ("点击窗口控件", "设置控件文本", "读取控件文本"):
            self.pick_btn.setVisible(True)
            self.pick_btn.setText("控")
            self.pick_btn.setToolTip(
                "从屏幕上选取目标控件\n目标程序需保持打开；运行时会按窗口与控件特征重新定位"
            )
        else:
            self.pick_btn.setText("取")
            self.pick_btn.setToolTip(
                "选取屏幕坐标\n单击/悬停：左键单击目标位置\n拖拽：按住左键拖动并松开\n右键取消"
            )
            self.pick_btn.setVisible(self.is_coordinate_pickable(text))

        self.recognition_test_btn.setVisible(self.image_test_available())

        if getattr(self, "parent_item", None):
            try:
                self.parent_item.setData(Qt.UserRole, self.get_data())
                self.parent_item.setData(Qt.UserRole + 1, self.drag_summary())
                self.parent_item.setText("")
            except RuntimeError:
                pass  # C++ object already deleted (debounce fired after row removal)
        self.refresh_config_dialog_context()

    def drag_summary(self):
        value = self.value_input.text().replace("\n", " ").strip()
        if len(value) > 80:
            value = value[:77] + "..."
        marks = []
        if self.is_step_disabled():
            marks.append("停")
        if normalize_step_note(self.custom_data.get("step_note", "")):
            marks.append("注")
        if self.settings_mode == "advanced" and config_bool(
            self.custom_data.get("editor_hidden", False)
        ):
            marks.append("隐")
        if self.settings_mode == "advanced" and self.custom_data.get("editor_groups"):
            marks.append("组")
        if self.has_breakpoint() and self.settings_mode == "advanced":
            marks.append("断")
        if self.has_coord_step_manual_points():
            marks.append("修")
        mark = f" [{'|'.join(marks)}]" if marks else ""
        if self.type_combo.currentText() == "直到条件成立":
            value = until_condition_summary(self.custom_data)
        elif self.type_combo.currentText() == "多图动作映射":
            value = multi_image_action_summary(
                self.custom_data.get("multi_image_actions", [])
            )
        return (
            f"{self.index_label.text()}{mark} {self.type_combo.currentText()} | {value}"
        )

    def has_coord_step_manual_points(self):
        return (
            SETTING_COORDINATE_PATH in self.settings_capabilities()
            and
            config_bool(self.custom_data.get("coord_step_en", False))
            and normalize_coord_step_direction(
                str(self.custom_data.get("coord_step_direction", ""))
            )
            == "移动到新点位"
            and bool(
                parse_coord_step_manual_points(
                    self.custom_data.get("coord_step_manual_points", "{}")
                )
            )
        )

    def update_manual_marker(self):
        self.manual_mark_label.setVisible(self.has_coord_step_manual_points())

    def has_breakpoint(self):
        return config_bool(self.custom_data.get("debug_breakpoint", False))

    def is_step_disabled(self):
        return config_bool(self.custom_data.get("step_disabled", False))

    def set_breakpoint(self, enabled):
        self.custom_data["debug_breakpoint"] = bool(enabled)
        self.update_markers()
        self.sync_data()

    def set_debug_paused(self, paused):
        self.setProperty("debugPaused", bool(paused))
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def current_step_index(self):
        try:
            return int(str(self.index_label.text()).strip().rstrip("."))
        except Exception as exc:
            logger.debug("解析步骤索引失败: %s", exc, exc_info=True)
            return None

    def on_type_changed(self, text):
        return apply_task_type_presentation(self, text)

    def show_until_condition_summary(self):
        return show_until_condition_summary(self)

    def show_multi_image_action_summary(self):
        return show_multi_image_action_summary(self)

    def set_data(self, data):
        return apply_task_row_data(self, data)

    def select_file(self):
        cmd_type = self.get_data()["type"]
        if cmd_type == TASK_TYPE_LAUNCH_APP:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "选择要启动的程序",
                os.getcwd(),
                "程序 (*.exe *.com *.bat *.cmd);;所有文件 (*)",
            )
            if path:
                self.commit_external_edit(
                    "程序文件选择", lambda: self.value_input.setText(path)
                )
        elif cmd_type == TASK_TYPE_DIRECTORY_SELECT:
            folder = QFileDialog.getExistingDirectory(
                self, "选择保存文件夹", os.getcwd()
            )
            if folder:
                self.commit_external_edit(
                    "目录选择", lambda: self.value_input.setText(folder)
                )
        else:
            path, _ = QFileDialog.getOpenFileName(
                self, "选择", filter="Images (*.png *.jpg *.bmp)"
            )
            if path:
                self.commit_external_edit(
                    "图片选择", lambda: self.value_input.setText(path)
                )
                main_window = self.window()
                settings = getattr(main_window, "settings", None)
                if settings is not None:
                    show_transparent_template_warning_once(
                        self, settings, path
                    )

    def get_data(self):
        return collect_task_row_data(self)

    def set_index(self, index, maximum_index=None):
        self.index_label.set_index(index, maximum_index)
        # FR-20261008-904: 行级可访问名称只在创建时写入一次（apply_accessible_
        # metadata 的 not-root.accessibleName() 守卫使已有名称永不更新），而
        # 拖拽重排只拆散被拖行 widget、存续行仅刷新可见序号标签。这里同步
        # 刷新，使读屏播报的步骤号在重排/删除/撤销后跟随新序号。行级名称无
        # 显式元数据来源，直接覆盖安全。
        self.setAccessibleName(f"步骤 {int(index)}")
        self.refresh_config_dialog_context()
