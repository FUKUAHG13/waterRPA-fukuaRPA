# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Per-step settings dialog lifecycle and section/controller composition.

Visible sections live in ``task_settings_sections`` and data conversion lives
in ``task_settings_binding``. This dialog owns one editable draft and must not
write back to a row until the user saves it.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QCheckBox, QDialog

from ..commands import (
    CLICK_COMMANDS,
    CONTROL_CALL_STEP,
    CONTROL_FAILURE_BRANCH,
    CONTROL_FAILURE_POLICY,
    CONTROL_JUMP_ONLY,
    CONTROL_LOOP_RANGE,
    CONTROL_REPEAT,
    CONTROL_RUN_MAX,
    CONTROL_SUCCESS_BRANCH,
    DRAG_COMMANDS,
    GENERIC_CONTROL_CAPABILITIES,
    SETTING_BACKGROUND_CLICK,
    SETTING_CAPTURE_REGION,
    SETTING_CLICK_MOTION,
    SETTING_CLICK_RETURN,
    SETTING_CLICK_SPATIAL_JITTER,
    SETTING_COORDINATE_PATH,
    SETTING_DELAYED_SECOND_CLICK,
    SETTING_IMAGE_CLICK_POINT,
    SETTING_IMAGE_CONFIRM_CLICK,
    SETTING_IMAGE_MASK,
    SETTING_IMAGE_POINT_LIMIT,
    SETTING_IMAGE_RECOGNITION,
    SETTING_INVERTED_MATCH,
    SETTING_KEY_HOLD,
    SETTING_MOUSE_TRAIL,
    SETTING_MULTI_IMAGE_ACTIONS,
    SETTING_STEP_REGION,
    SETTING_UNTIL_CONDITIONS,
    TASK_TYPE_AUTO_CLICKER,
    command_code,
    step_control_capabilities,
    step_settings_capabilities,
    step_target_kind,
)
from ..constants import (
    CLICK_ANCHOR_RATIO,
    TASK_TYPE_LEFT_DRAG,
    TASK_TYPE_MULTI_IMAGE_ACTION,
    TASK_TYPE_PLUGIN_ACTION,
    TASK_TYPE_RIGHT_DRAG,
    TASK_TYPE_SOUND_ALERT,
)
from ..log_policy import (
    LOG_APPLICATION,
)
from ..paths import get_base_dir
from ..task_model import parse_coord_step_manual_points
from .components import enable_work_window_controls
from .image_click_point_dialog import ImageClickPointDialog, ImageClickPointWidget
from .lifecycle import safe_close
from .performance_hooks import schedule_owner_interaction
from .task_settings_binding import collect_task_settings
from .task_settings_controllers import (
    BackgroundClickSettingsController,
    CaptureSettingsController,
    CoordinateSettingsController,
    DebugSettingsController,
    ImageSettingsController,
    UntilConditionController,
    infer_debug_preset,
)
from .task_settings_forwarders import (
    AuxSettingsForwarderMixin,
    CoordinateSettingsForwarderMixin,
    ImageSettingsForwarderMixin,
)
from .task_settings_view import _set_container_enabled_except_help, build_task_settings_view

__all__ = ["ImageClickPointDialog", "ImageClickPointWidget", "TaskConfigDialog", "TaskConfigParams"]


@dataclass
class TaskConfigParams:
    """Encapsulates all parameters for TaskConfigDialog construction."""

    data: dict = field(default_factory=dict)
    image_settings_available: bool = True
    point_limit_available: bool = False
    coordinate_step_available: bool = False
    base_coordinate: tuple | None = None
    image_path: str = ""
    image_click_point_available: bool = False
    base_coordinate_changed: Callable | None = None
    step_index: int | None = None
    step_type: str = ""
    base_dir: str | None = None
    settings_mode: str | None = None
    screenshot_settings_available: bool = False
    step_value: str = ""
    settings_capabilities_override: Any | None = None
    control_capabilities_override: Any | None = None
    batch_mode: bool = False
    batch_count: int = 0


class TaskConfigDialog(
    AuxSettingsForwarderMixin,
    CoordinateSettingsForwarderMixin,
    ImageSettingsForwarderMixin,
    QDialog,
):
    COORD_STEP_DIRECTIONS = (
        "向上",
        "向下",
        "向左",
        "向右",
        "自定义偏移",
        "移动到新点位",
    )

    def moveEvent(self, event):
        super().moveEvent(event)
        schedule_owner_interaction(self, "step_settings_move")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        schedule_owner_interaction(self, "step_settings_resize")

    def __init__(
        self,
        parent=None,
        data=None,
        image_settings_available=True,
        point_limit_available=False,
        coordinate_step_available=False,
        base_coordinate=None,
        image_path="",
        image_click_point_available=False,
        base_coordinate_changed=None,
        step_index=None,
        step_type="",
        base_dir=None,
        settings_mode=None,
        screenshot_settings_available=False,
        step_value="",
        settings_capabilities_override=None,
        control_capabilities_override=None,
        batch_mode=False,
        batch_count=0,
        *,
        params=None,
    ):
        """Task configuration dialog.

        Prefer passing a TaskConfigParams instance via the `params` keyword.
        The positional argument interface is retained for backward compatibility
        and will be removed in a future version.
        """
        if params is not None:
            data = params.data
            image_settings_available = params.image_settings_available
            point_limit_available = params.point_limit_available
            coordinate_step_available = params.coordinate_step_available
            base_coordinate = params.base_coordinate
            image_path = params.image_path
            image_click_point_available = params.image_click_point_available
            base_coordinate_changed = params.base_coordinate_changed
            step_index = params.step_index
            step_type = params.step_type
            base_dir = params.base_dir
            settings_mode = params.settings_mode
            screenshot_settings_available = params.screenshot_settings_available
            step_value = params.step_value
            settings_capabilities_override = params.settings_capabilities_override
            control_capabilities_override = params.control_capabilities_override
            batch_mode = params.batch_mode
            batch_count = params.batch_count
        if data is None:
            data = {}
        super().__init__(None)
        self.setAttribute(Qt.WA_QuitOnClose, False)
        enable_work_window_controls(self)
        self.setWindowModality(Qt.NonModal)
        self.step_index = step_index
        self.step_type = step_type
        self.step_value = str(step_value or "")
        self.batch_mode = bool(batch_mode)
        self.batch_count = max(0, int(batch_count or 0))
        self._settings_capabilities_override = (
            frozenset(settings_capabilities_override)
            if settings_capabilities_override is not None
            else None
        )
        self._control_capabilities_override = (
            frozenset(control_capabilities_override)
            if control_capabilities_override is not None
            else None
        )
        self.image_settings_available = image_settings_available
        self.point_limit_available = point_limit_available
        self.coordinate_step_available = coordinate_step_available
        self.base_coordinate = base_coordinate
        self.image_path = image_path
        self.image_click_point_available = image_click_point_available
        self.screenshot_settings_available = bool(screenshot_settings_available)
        self.base_coordinate_changed = base_coordinate_changed
        self.coord_step_picker = None
        self.coord_sequence_picker = None
        self.coord_step_preview = None
        self.step_region_window = None
        self.capture_region_window = None
        self.until_region_windows = {}
        self.background_click_binding = (
            dict(data.get("background_click_binding", {}))
            if isinstance(data.get("background_click_binding", {}), dict)
            else {}
        )
        self.background_window_picker = None
        self.coord_step_manual_points = parse_coord_step_manual_points(
            data.get("coord_step_manual_points", "{}")
        )
        self.coord_step_max_steps_initial = str(data.get("coord_step_max_steps", "0"))
        self.coord_step_clearing_due_to_count = False
        settings_dir = os.path.abspath(base_dir or get_base_dir())
        self.dialog_settings = QSettings(
            os.path.join(settings_dir, "config.ini"), QSettings.IniFormat
        )
        saved_settings_mode = (
            settings_mode
            if settings_mode is not None
            else self.dialog_settings.value("settings_view_mode", "simple")
        )
        self.settings_mode = (
            "advanced" if str(saved_settings_mode) == "advanced" else "simple"
        )
        # FR-20260812 建议-架构-1: 移除死状态 _coord_direction_locked_for_simple
        # （恒 False，无置 True 写点）。
        self._last_target_section_kind = None
        self.background_click_settings_controller = (
            BackgroundClickSettingsController(self)
        )
        self.until_condition_controller = UntilConditionController(self)
        self.debug_settings_controller = DebugSettingsController(self)
        self.image_settings_controller = ImageSettingsController(self)
        self.capture_settings_controller = CaptureSettingsController(self)
        self.coordinate_settings_controller = CoordinateSettingsController(self)
        self.update_window_title()
        self.setMinimumSize(900, 520)
        self.data = dict(data)  # working copy; must exist before view construction
        build_task_settings_view(self, data)
        # FR-SCOPE009-004: 模式应用属于协调层职责，必须在整棵视图构建完成之后执行。
        # 此前它藏在 `build_debug_section` 末尾，导致区块构建函数驱动整窗模式应用
        # （docs/UI开发规范.md「架构边界」第 1、3 条）。这里显式承接：所有区块
        # 都已创建完毕，`apply_settings_mode` 依赖的控件全部就绪。
        self.apply_settings_mode(self.settings_mode)
        if hasattr(self, "plugin_action_btn"):
            self.plugin_action_btn.clicked.connect(self._open_plugin_action_settings)
            self._refresh_plugin_action_label()
        self.batch_apply_mixed_chk = None
        if self.batch_mode:
            self.batch_apply_mixed_chk = QCheckBox(
                "同时把未改动但当前值不同的项目统一为窗口中显示的值"
            )
            self.batch_apply_mixed_chk.setToolTip(
                "默认关闭时，只批量应用您在本窗口中实际改动的项目。开启后，"
                "选中步骤之间原本不同、但您未碰过的项目也会统一为第一步当前显示的值。"
            )
            self.body_layout.insertWidget(1, self.batch_apply_mixed_chk)
            self.update_window_title()
            self.update_context_note()

    def apply_settings_mode(self, mode):
        self.settings_mode = "advanced" if str(mode) == "advanced" else "simple"
        advanced = self.settings_mode == "advanced"
        self.editor_hidden_chk.setVisible(advanced)
        self._refresh_coord_step_direction_options(advanced)
        self.debug_section.setVisible(advanced)
        self.update_capture_region_ui()
        self.control_note.setText(
            "跳至填 0 表示关闭；同一结果里“跳至”优先于“跳过”。循环范围只控制本步骤在哪些脚本循环轮次生效，被范围跳过不会触发成功/失败分支。开启禁止跳过后，连续失败次数暂不生效，失败分支会等到成功或超时后再处理。移动到新点位时，“路径点位”表示起点到目标点之间的总点位数；“重置循环”可让本路径点击指定次数后回到起点。"
            if advanced
            else "跳至填 0 表示关闭；同一结果里“跳至”优先于“跳过”。开启禁止跳过后，连续失败次数暂不生效，失败分支会等到成功或超时后再处理。自定义点位启用后，每次进入本步骤按其批次设置点击，通用的“本步骤重复”会暂时忽略。"
        )
        self.update_coord_step_ui()
        self.update_until_condition_ui()
        self.update_auxiliary_click_ui()
        self.apply_action_settings_visibility()

    def settings_capabilities(self):
        """Return the action-specific settings supported by the current step."""

        if self._settings_capabilities_override is not None:
            return self._settings_capabilities_override

        if str(self.step_type).strip():
            return step_settings_capabilities(self.step_type, self.step_value)

        # Keep direct dialog construction compatible with tests and extensions
        # that predate the command capability registry.
        capabilities = set()
        if self.image_settings_available and not self.coordinate_step_available:
            capabilities.update(
                {SETTING_IMAGE_RECOGNITION, SETTING_STEP_REGION}
            )
        if self.point_limit_available and not self.coordinate_step_available:
            capabilities.add(SETTING_IMAGE_POINT_LIMIT)
        if self.image_click_point_available and not self.coordinate_step_available:
            capabilities.add(SETTING_IMAGE_CLICK_POINT)
        if self.coordinate_step_available:
            capabilities.add(SETTING_COORDINATE_PATH)
        if self.screenshot_settings_available:
            capabilities.add(SETTING_CAPTURE_REGION)
        if self.is_until_condition_task():
            capabilities.add(SETTING_UNTIL_CONDITIONS)
        return frozenset(capabilities)

    def control_capabilities(self):
        if self._control_capabilities_override is not None:
            return self._control_capabilities_override
        if not str(self.step_type).strip():
            return GENERIC_CONTROL_CAPABILITIES
        return step_control_capabilities(self.step_type, self.step_value)

    def is_dual_target_click(self):
        if self.batch_mode:
            capabilities = self.settings_capabilities()
            return bool(
                SETTING_IMAGE_RECOGNITION in capabilities
                and SETTING_COORDINATE_PATH in capabilities
            )
        return command_code(self.step_type) in CLICK_COMMANDS

    def is_multi_image_action(self):
        if self.batch_mode:
            return SETTING_MULTI_IMAGE_ACTIONS in self.settings_capabilities()
        return command_code(self.step_type) == TASK_TYPE_MULTI_IMAGE_ACTION

    def is_plugin_action(self):
        if self.batch_mode:
            return False
        return command_code(self.step_type) == TASK_TYPE_PLUGIN_ACTION

    # ------------------------------------------------------------------
    # 插件动作设置入口 (design §5.4)
    # ------------------------------------------------------------------
    def _open_plugin_action_settings(self):
        from ..plugin_actions import get_plugin_registry
        from .plugin_action_settings import PluginActionSettings

        registry = get_plugin_registry()
        dialog = PluginActionSettings(
            self.dialog_settings,
            self.data,
            registry,
            self._on_plugin_action_accept,
            self,
        )
        dialog.exec()
        self._refresh_plugin_action_label()

    def _on_plugin_action_accept(self, task):
        # The dialog mutates self.data in place; this is a backup hook for any
        # future side effects. self.data is the working copy written back on save.
        self.data.update(task)

    def _refresh_plugin_action_label(self):
        if not hasattr(self, "plugin_action_label"):
            return
        pid = self.data.get("plugin_id", "")
        aid = self.data.get("plugin_action_id", "")
        if pid and aid:
            label = self.data.get("value") or f"{pid}/{aid}"
            self.plugin_action_label.setText(f"已配置：{label}")
        else:
            self.plugin_action_label.setText("尚未配置插件动作。")

    def _set_action_row_state(
        self,
        row,
        relevant,
        *,
        advanced_only=False,
    ):
        advanced = self.settings_mode == "advanced"
        visible = relevant and (advanced or not advanced_only)
        form = getattr(self, "action_row_forms", {}).get(
            row, self.control_form
        )
        form.setRowVisible(row, visible)
        _set_container_enabled_except_help(row, relevant)
        label = form.labelForField(row)
        if label is not None:
            label.setEnabled(relevant)

    def _set_control_row_state(self, row, relevant, *, advanced_only=False):
        advanced = self.settings_mode == "advanced"
        self.control_form.setRowVisible(
            row, relevant and (advanced or not advanced_only)
        )
        _set_container_enabled_except_help(row, relevant)
        label = self.control_form.labelForField(row)
        if label is not None:
            label.setEnabled(relevant)

    def apply_action_settings_visibility(self):
        """Show only controls that can affect the current action and target."""

        capabilities = self.settings_capabilities()
        controls = self.control_capabilities()
        advanced = self.settings_mode == "advanced"
        dual_target_click = self.is_dual_target_click()
        multi_image_action = self.is_multi_image_action()
        target_kind = step_target_kind(self.step_type, self.step_value)
        image_relevant = SETTING_IMAGE_RECOGNITION in capabilities
        coordinate_relevant = SETTING_COORDINATE_PATH in capabilities
        if self.batch_mode:
            show_image = image_relevant
            show_coordinate = coordinate_relevant
        elif dual_target_click:
            show_image = target_kind in {"image", "unset"}
            show_coordinate = target_kind in {"coordinate", "unset"}
        else:
            show_image = image_relevant
            show_coordinate = coordinate_relevant
        if multi_image_action and not advanced:
            show_image = False
        self.image_target_section.setVisible(show_image)
        self.image_settings_note.setVisible(show_image)
        # FR-010: docs/步骤设置显隐矩阵.md:33 约定"已忽略区域计数详情"只在高级模式
        # 出现（简易模式仅保留编辑/清除/裁剪入口）。该标签位于 image_mask_row 内部，
        # 而行本身注册为 advanced_only=False，因此必须单独按模式切换标签可见性。
        self.template_mask_info.setVisible(advanced)
        self.image_settings_note.setEnabled(image_relevant)
        self.enable_chk.setVisible(image_relevant)
        self.enable_chk.setEnabled(image_relevant)
        self.form_widget.setVisible(image_relevant)
        _set_container_enabled_except_help(
            self.form_widget,
            image_relevant and self.enable_chk.isChecked(),
        )
        self.coordinate_target_section.setVisible(show_coordinate)

        if target_kind != self._last_target_section_kind:
            self.image_target_section.set_expanded(target_kind == "image")
            self.coordinate_target_section.set_expanded(
                target_kind == "coordinate"
            )
            self._last_target_section_kind = target_kind

        for row, capability, advanced_only in (
            (self.point_limit_row, SETTING_IMAGE_POINT_LIMIT, True),
            (self.image_point_row, SETTING_IMAGE_CLICK_POINT, False),
            (self.image_mask_row, SETTING_IMAGE_MASK, False),
            (self.step_region_row, SETTING_STEP_REGION, True),
            (self.inverse_match_row, SETTING_INVERTED_MATCH, True),
            (
                self.image_confirm_click_row,
                SETTING_IMAGE_CONFIRM_CLICK,
                True,
            ),
            (self.capture_region_row, SETTING_CAPTURE_REGION, False),
            (self.coord_every_row, SETTING_COORDINATE_PATH, True),
            (self.coord_direction_row, SETTING_COORDINATE_PATH, True),
            (self.coord_point_row, SETTING_COORDINATE_PATH, True),
            (self.coord_limit_row, SETTING_COORDINATE_PATH, True),
            (self.coord_reset_row, SETTING_COORDINATE_PATH, True),
            (self.coord_sequence_box, SETTING_COORDINATE_PATH, False),
            (self.background_click_row, SETTING_BACKGROUND_CLICK, True),
        ):
            self._set_action_row_state(
                row,
                capability in capabilities,
                advanced_only=advanced_only,
            )

        show_coord_notice = (
            not advanced
            and (coordinate_relevant or dual_target_click)
            and self.coord_step_chk.isChecked()
        )
        self.coordinate_target_form.setRowVisible(
            self.coord_advanced_notice, show_coord_notice
        )

        until_relevant = SETTING_UNTIL_CONDITIONS in capabilities
        self.until_group.setVisible(until_relevant)
        _set_container_enabled_except_help(self.until_group, until_relevant)
        multi_rules_relevant = SETTING_MULTI_IMAGE_ACTIONS in capabilities
        self.multi_image_section.setVisible(advanced and multi_rules_relevant)
        _set_container_enabled_except_help(self.multi_image_section, multi_rules_relevant)
        delayed_click_relevant = SETTING_DELAYED_SECOND_CLICK in capabilities
        self.auxiliary_click_section.setVisible(advanced and delayed_click_relevant)
        _set_container_enabled_except_help(self.auxiliary_click_section, delayed_click_relevant)
        drag_relevant = self.is_drag_task()
        self.drag_target_section.setVisible(drag_relevant)
        _set_container_enabled_except_help(self.drag_target_section, drag_relevant)
        if drag_relevant:
            self.drag_target_section.set_expanded(
                bool(self.drag_source_img_edit.text().strip())
                or bool(self.drag_target_img_edit.text().strip())
            )
        self._update_drag_source_state()
        self.update_drag_target_ui()

        # 坐标点击 / 拖拽专属：移动与按住随机延迟
        #
        # FR-20261006-007：显隐对象改为 jitter_section 本身。该行现挂在独立的
        # 「随机延迟」折叠区里；继续对 coordinate_target_form 调 setRowVisible 既
        # 找不到该行（已迁走），又无法解决拖拽场景——那里的坐标目标折叠区会被整体
        # 隐藏，祖先不可见时行控件的显隐设置对用户不可见。
        #
        # REG-20261008-001：判据**必须**含 `coordinate_relevant`。修 FR-007 时我
        # 把它收窄为仅 DRAG_COMMANDS，等于对坐标点击关闭了该组——但该字段的既定
        # 范围自 07233db（"仅坐标点击与拖拽、仅高级模式"）起就包含坐标点击，且
        # 引擎（flow.py: `command in CLICK_COMMANDS or command in DRAG_COMMANDS`）
        # 与批量路径（bulk_task_settings.JITTER_COMMANDS）都仍按点击+拖拽消费它。
        # 收窄 UI 会造成"单步隐藏、批量仍有效"的不对称：已保存的值引擎照常生效，
        # 用户却无法查看或修改。DOC-20261006-003 已把该问题定性为**矩阵缺失**
        # （而非代码多余），故正确处置是保留可见性并补记矩阵，不是收窄代码。
        jitter_relevant = (
            coordinate_relevant or command_code(self.step_type) in DRAG_COMMANDS
        )
        self.jitter_section.setVisible(advanced and jitter_relevant)
        _set_container_enabled_except_help(self.jitter_row, jitter_relevant)

        # 同一个折叠区内也按字段能力显示。拖拽只支持移动曲线，不显示
        # 点击落点偏移或点击后返回。
        click_spatial_relevant = SETTING_CLICK_SPATIAL_JITTER in capabilities
        move_curve_relevant = SETTING_CLICK_MOTION in capabilities
        click_return_relevant = SETTING_CLICK_RETURN in capabilities
        self.click_spatial_group.setVisible(click_spatial_relevant)
        self.move_curve_group.setVisible(move_curve_relevant)
        self.click_return_group.setVisible(click_return_relevant)
        click_form_relevant = any(
            (click_spatial_relevant, move_curve_relevant, click_return_relevant)
        )
        self.click_form_section.setVisible(advanced and click_form_relevant)
        _set_container_enabled_except_help(
            self.click_form_section, click_form_relevant
        )
        self.click_form_section.set_expanded(click_form_relevant)
        mouse_trail_relevant = SETTING_MOUSE_TRAIL in capabilities
        self.mouse_trail_section.setVisible(advanced and mouse_trail_relevant)
        _set_container_enabled_except_help(
            self.mouse_trail_section, mouse_trail_relevant
        )
        self.mouse_trail_section.set_expanded(advanced and mouse_trail_relevant)
        # 本步系统按键按住时长：仅对系统按键（7.0）生效
        key_hold_relevant = SETTING_KEY_HOLD in capabilities
        self.key_hold_section.setVisible(advanced and key_hold_relevant)
        _set_container_enabled_except_help(self.key_hold_section, key_hold_relevant)
        self.key_hold_section.set_expanded(key_hold_relevant)
        # 连点器（原生指令 28.0）专属参数：仅对该指令生效
        auto_clicker_relevant = command_code(self.step_type) == TASK_TYPE_AUTO_CLICKER
        self.auto_clicker_section.setVisible(auto_clicker_relevant)
        _set_container_enabled_except_help(
            self.auto_clicker_section, auto_clicker_relevant
        )
        self.auto_clicker_section.set_expanded(auto_clicker_relevant)

        # 声音提示（14.0）每步覆盖：仅高级模式 + 该指令类型可见
        sound_alert_relevant = command_code(self.step_type) == TASK_TYPE_SOUND_ALERT
        sound_alert_visible = sound_alert_relevant and advanced
        self.sound_alert_section.setVisible(sound_alert_visible)
        _set_container_enabled_except_help(
            self.sound_alert_section, sound_alert_relevant
        )
        self.sound_alert_section.set_expanded(sound_alert_visible)

        plugin_relevant = self.is_plugin_action()
        if hasattr(self, "plugin_action_section"):
            self.plugin_action_section.setVisible(plugin_relevant)
            _set_container_enabled_except_help(
                self.plugin_action_section, plugin_relevant
            )
            self._refresh_plugin_action_label()

        for row, capability, advanced_only in (
            (self.repeat_row, CONTROL_REPEAT, False),
            (self.step_loop_row, CONTROL_LOOP_RANGE, True),
            (self.failure_policy_row, CONTROL_FAILURE_POLICY, False),
            (self.run_max_row, CONTROL_RUN_MAX, True),
            (self.success_branch_row, CONTROL_SUCCESS_BRANCH, False),
            (self.failure_branch_row, CONTROL_FAILURE_BRANCH, False),
            (self.call_step_row, CONTROL_CALL_STEP, True),
            (self.jump_only_row, CONTROL_JUMP_ONLY, True),
        ):
            self._set_control_row_state(
                row,
                capability in controls,
                advanced_only=advanced_only,
            )

        # Enabling a relevant row also enables all of its children. Reapply
        # each feature's own checkbox/mode dependencies afterwards so a
        # visible row does not make an inactive editor look usable.
        self.update_point_limit_ui()
        self.update_image_click_point_ui()
        self.update_template_mask_ui()
        self.update_step_region_ui()
        self.update_capture_region_ui()
        self.update_coord_step_ui()
        self.update_coord_sequence_ui()
        self.update_background_click_ui()
        self.update_until_condition_ui()
        self.update_auxiliary_click_ui()

    def _refresh_coord_step_direction_options(self, advanced):
        combo = self.coord_step_direction_combo
        current = combo.currentText() or "向下"
        # FR-20260812 建议-架构-1: 移除死状态赋值。
        options = list(self.COORD_STEP_DIRECTIONS)
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(options)
        combo.setCurrentText(current if current in options else options[0])
        combo.blockSignals(False)
        combo.setToolTip("")

    def update_window_title(self):
        if self.batch_mode:
            title = f"批量修改 {self.batch_count} 个步骤"
        elif self.step_index:
            title = f"第{self.step_index}步设置"
        else:
            title = "步骤设置"
        if self.step_type:
            title += f" - {self.step_type}"
        self.setWindowTitle(title)

    def update_context_note(self):
        if self.batch_mode:
            self.context_note.setText(
                "批量设置默认只应用您在本窗口中实际改动的项目；保存时还会按每个步骤的动作类型过滤，"
                "不适用的图片、坐标或条件参数会保留原值且不参与执行。"
            )
            return
        target_kind = step_target_kind(self.step_type, self.step_value)
        if self.is_until_condition_task():
            text = "当前步骤用于判断条件：不直接点击；条件未满足时按设置跳回，满足后继续或跳至指定步骤。"
        elif self.is_multi_image_action():
            text = "当前步骤会按规则顺序找图，并执行第一条命中规则绑定的动作；请在“多图动作规则”中编辑。"
        elif target_kind == "coordinate":
            text = "当前参数是屏幕坐标：图片识别相关设置会自动忽略，坐标步进可用。"
        elif target_kind == "image":
            if self.image_click_point_available:
                text = "当前参数是有效图片路径：图片识别、同点上限和图片内点击点可按需启用。"
            else:
                text = "当前参数按图片识别处理；图片内点击点需要填写存在的图片文件后才可启用。"
        elif target_kind == "unset" and self.is_dual_target_click():
            text = "请先在主步骤中填写图片路径或屏幕坐标；当前图片参数和坐标参数均只读。"
        elif self.screenshot_settings_available:
            text = "当前步骤会保存屏幕截图；可选择整个屏幕、沿用全局识别区域或本步自定义区域。"
        else:
            text = "当前步骤不使用图片识别或坐标步进；这里只保留重复、循环范围和条件分支等通用设置。"
        self.context_note.setText(text)

    def is_until_condition_task(self):
        if self.batch_mode:
            return SETTING_UNTIL_CONDITIONS in self.settings_capabilities()
        return str(getattr(self, "step_type", "")) == "直到条件成立"

    def update_step_context(
        self,
        image_settings_available=None,
        point_limit_available=None,
        coordinate_step_available=None,
        base_coordinate=None,
        image_path=None,
        image_click_point_available=None,
        step_index=None,
        step_type=None,
        screenshot_settings_available=None,
        step_value=None,
    ):
        if step_index is not None:
            self.step_index = step_index
        if step_type is not None:
            self.step_type = step_type
        if step_value is not None:
            self.step_value = str(step_value)
        self.update_window_title()

        if image_settings_available is not None:
            self.image_settings_available = bool(image_settings_available)
        if point_limit_available is not None:
            self.point_limit_available = bool(point_limit_available)
        if coordinate_step_available is not None:
            self.coordinate_step_available = bool(coordinate_step_available)
        if base_coordinate is not None or not self.coordinate_step_available:
            self.base_coordinate = base_coordinate
        if image_path is not None:
            self.image_path = str(image_path)
        if image_click_point_available is not None:
            self.image_click_point_available = bool(image_click_point_available)
        if screenshot_settings_available is not None:
            self.screenshot_settings_available = bool(
                screenshot_settings_available
            )

        self.enable_chk.setEnabled(self.image_settings_available)
        self.point_limit_chk.setEnabled(self.point_limit_available)
        self.image_click_point_chk.setEnabled(self.image_click_point_available)
        self.step_region_chk.setEnabled(self.image_settings_available)
        self.coord_step_chk.setEnabled(self.coordinate_step_available)
        self.coord_sequence_chk.setEnabled(self.coordinate_step_available)
        if not self.coordinate_step_available:
            self.close_coord_step_preview()

        self.update_image_settings_enabled()
        self.update_point_limit_ui()
        self.update_image_click_point_ui()
        self.update_step_region_ui()
        self.update_capture_region_ui()
        self.update_coord_step_ui()
        self.update_coord_sequence_ui()
        self.update_until_condition_ui()
        self.update_auxiliary_click_ui()
        self.apply_action_settings_visibility()
        self.update_context_note()
        self.refresh_coord_step_preview_points()

    def refresh_reference_numbers(self, data):
        for name, editor in getattr(self, "reference_editors", {}).items():
            if name not in self.reference_fields_dirty:
                editor.setText(str(data.get(name, "0")))

    def update_repeat_ui(self, _=None):
        sequence_enabled = bool(
            getattr(self, "coord_sequence_chk", None)
            and self.coord_sequence_chk.isChecked()
            and self.coordinate_step_available
        )
        self.repeat_combo.setEnabled(not sequence_enabled)
        self.repeat_count_edit.setEnabled(
            not sequence_enabled and self.repeat_combo.currentText() == "指定次数"
        )
        self.repeat_combo.setToolTip(
            "启用自定义点位后，点击数量由“每次进入本步骤”控制；"
            "这里的重复设置会保留但暂不执行。"
            if sequence_enabled
            else ""
        )

    @staticmethod
    def infer_debug_preset(expression):
        return infer_debug_preset(expression)

    def update_auxiliary_click_ui(self, _=None):
        capabilities = self.settings_capabilities()
        confirm_available = SETTING_IMAGE_CONFIRM_CLICK in capabilities
        delayed_available = SETTING_DELAYED_SECOND_CLICK in capabilities
        self.image_confirm_click_chk.setEnabled(confirm_available)
        self.image_confirm_click_delay_edit.setEnabled(
            confirm_available and self.image_confirm_click_chk.isChecked()
        )
        self.delayed_second_click_chk.setEnabled(delayed_available)
        self.delayed_second_click_delay_edit.setEnabled(
            delayed_available and self.delayed_second_click_chk.isChecked()
        )

    # --- 拖拽终点图片 ---

    def is_drag_task(self):
        if self.batch_mode:
            return False
        code = command_code(self.step_type)
        return code in (TASK_TYPE_LEFT_DRAG, TASK_TYPE_RIGHT_DRAG)

    def update_drag_target_ui(self, _=None):
        if not hasattr(self, "drag_target_img_edit"):
            return
        img_path = self.drag_target_img_edit.text().strip()
        has_img = bool(img_path)
        self.drag_target_point_edit.setEnabled(has_img)
        self.drag_target_point_select_btn.setEnabled(has_img)
        self.drag_target_img_clear_btn.setEnabled(has_img)
        point_text = self.drag_target_point_edit.text().strip()
        if has_img and point_text:
            self.drag_target_point_info.setText(f"偏移: {point_text}")
        elif has_img:
            self.drag_target_point_info.setText("默认点击图片中心")
        else:
            self.drag_target_point_info.setText("")

    def browse_drag_target_img(self):
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "选择拖拽终点图片", filter="Images (*.png *.jpg *.bmp)"
        )
        if path:
            self.drag_target_img_edit.setText(path)
            self.update_drag_target_ui()

    def clear_drag_target_img(self):
        self.drag_target_img_edit.clear()
        self.drag_target_point_edit.clear()
        self.update_drag_target_ui()

    def _select_drag_point(self, img_edit, point_edit, missing_msg, refresh):
        img_path = img_edit.text().strip()
        if not img_path or not os.path.isfile(img_path):
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(self, "无法选择", missing_msg)
            return
        point_text = point_edit.text().strip()
        rx, ry = CLICK_ANCHOR_RATIO, CLICK_ANCHOR_RATIO
        if point_text:
            try:
                parts = point_text.replace("，", ",").split(",")
                if len(parts) == 2:
                    rx = float(parts[0].strip())
                    ry = float(parts[1].strip())
            except (TypeError, ValueError):
                pass
        try:
            dialog = ImageClickPointDialog(
                img_path, rx, ry, selectable=True, parent=self
            )
        except Exception as exc:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(self, "图片打开失败", str(exc))
            return
        if dialog.exec() == QDialog.Accepted:
            selected_rx, selected_ry = dialog.selected_ratio()
            point_edit.setText(f"{selected_rx:.6f},{selected_ry:.6f}")
            refresh()

    def select_drag_target_point(self):
        self._select_drag_point(
            self.drag_target_img_edit,
            self.drag_target_point_edit,
            "请先选择有效的终点图片文件。",
            self.update_drag_target_ui,
        )

    # --- 拖拽起点图片 ---

    def _update_drag_source_state(self, _=None):
        if not hasattr(self, "drag_source_img_edit"):
            return
        img_path = self.drag_source_img_edit.text().strip()
        has_img = bool(img_path)
        self.drag_source_point_edit.setEnabled(has_img)
        self.drag_source_point_select_btn.setEnabled(has_img)
        self.drag_source_img_clear_btn.setEnabled(has_img)
        point_text = self.drag_source_point_edit.text().strip()
        if has_img and point_text:
            self.drag_source_point_info.setText(f"偏移: {point_text}")
        elif has_img:
            self.drag_source_point_info.setText("默认点击图片中心")
        else:
            self.drag_source_point_info.setText("")

    def browse_drag_source_img(self):
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "选择拖拽起点图片", filter="Images (*.png *.jpg *.bmp)"
        )
        if path:
            self.drag_source_img_edit.setText(path)
            self._update_drag_source_state()

    def clear_drag_source_img(self):
        self.drag_source_img_edit.clear()
        self.drag_source_point_edit.clear()
        self._update_drag_source_state()

    def select_drag_source_point(self):
        self._select_drag_point(
            self.drag_source_img_edit,
            self.drag_source_point_edit,
            "请先选择有效的起点图片文件。",
            self._update_drag_source_state,
        )

    def save_dialog_geometry(self):
        try:
            self.dialog_settings.setValue(
                "task_config_dialog_geometry", self.saveGeometry()
            )
        except Exception as exc:
            from ..logging_service import write_log

            write_log(f"保存设置对话框几何信息失败: {exc}",
                         category=LOG_APPLICATION,
                     )

    def accept(self):
        self.close_child_tools()
        self.save_dialog_geometry()
        super().accept()

    def reject(self):
        self.close_child_tools()
        self.save_dialog_geometry()
        super().reject()

    def close_child_tools(self):
        safe_close(getattr(self, "coord_step_picker", None))
        safe_close(getattr(self, "coord_sequence_picker", None))
        safe_close(getattr(self, "step_region_window", None))
        safe_close(getattr(self, "capture_region_window", None))
        for win in list(getattr(self, "until_region_windows", {}).values()):
            safe_close(win)
        editor = getattr(self, "multi_image_action_editor", None)
        if editor is not None:
            editor.close_tools()
        self.close_coord_step_preview()
        self.background_click_settings_controller.close()

    def closeEvent(self, event):
        self.close_child_tools()
        self.save_dialog_geometry()
        super().closeEvent(event)

    def get_data(self):
        return collect_task_settings(self)
