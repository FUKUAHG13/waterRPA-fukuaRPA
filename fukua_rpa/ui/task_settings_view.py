# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""Layout assembly for the per-step settings dialog."""

import contextlib

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QFrame,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .accessibility import apply_accessible_metadata
from .components import (
    HelpBtn,
    ResponsiveRow,
)
from .task_settings_sections import (
    build_debug_section,
    build_execution_section,
    build_multi_image_section,
    build_until_conditions,
)
from .task_settings_view_parts import (
    build_coordinate_target_section,
    build_drag_target_section,
    build_image_target_section,
    build_plugin_action_section,
)


def _set_container_enabled_except_help(container, enabled):
    """Set enabled state on *container*'s descendants, keeping HelpBtn clickable.

    Qt propagates a disabled parent to all children, so calling
    ``container.setEnabled(False)`` would grey out any HelpBtn inside.
    Instead we iterate over descendants and disable them individually,
    skipping HelpBtn instances and their ancestor chain up to *container*.

    A dynamic property ``controlsEnabled`` is stored on *container* so that
    callers can query the logical enabled state.
    """
    container.setProperty("controlsEnabled", enabled)

    if enabled:
        container._section_disabled = False
        for child in getattr(container, "_disabled_by_section", []):
            with contextlib.suppress(RuntimeError):  # widget was deleted
                child.setEnabled(True)
        container._disabled_by_section = []
        return

    # INVARIANT: 幂等保护——若已被本函数禁用则不重复记录，
    # 避免第二次 False 调用因所有子控件已禁用而覆写为空列表。
    if getattr(container, "_section_disabled", False):
        return
    container._section_disabled = True

    # Collect HelpBtn instances and every ancestor between them and container.
    protected: set = set()
    for btn in container.findChildren(HelpBtn):
        protected.add(btn)
        parent = btn.parentWidget()
        while parent is not None and parent is not container:
            protected.add(parent)
            parent = parent.parentWidget()

    disabled_by_us = []
    for child in container.findChildren(QWidget):
        if child not in protected and child.isEnabled():
            child.setEnabled(False)
            disabled_by_us.append(child)
    container._disabled_by_section = disabled_by_us


def build_task_settings_view(dialog, data):
    """Populate *dialog* without importing its concrete class."""

    self = dialog
    outer_layout = QVBoxLayout(self)
    outer_layout.setContentsMargins(14, 14, 14, 14)
    outer_layout.setSpacing(10)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    body = QWidget()
    body.setObjectName("dialogBody")
    layout = QVBoxLayout(body)
    layout.setContentsMargins(4, 4, 4, 4)
    layout.setSpacing(10)
    layout.setAlignment(Qt.AlignTop)
    self.body_layout = layout
    scroll.setWidget(body)
    outer_layout.addWidget(scroll, 1)

    def inline_row(*groups):
        row = ResponsiveRow(horizontal_spacing=10, vertical_spacing=7)
        for group in groups:
            if isinstance(group, (tuple, list)):
                row.add_group(*group)
            else:
                row.add_group(group)
        return row

    build_image_target_section(self, data, layout, inline_row)
    build_coordinate_target_section(self, data, layout)
    build_drag_target_section(self, data, layout)

    build_multi_image_section(self, data, layout)
    build_plugin_action_section(self, data, layout)

    build_until_conditions(self, data, layout)
    build_execution_section(self, data, layout, inline_row)
    build_debug_section(self, data, layout, inline_row)
    layout.addStretch(1)

    self.update_repeat_ui()
    self.update_debug_preset_ui()
    self.update_debug_breakpoint_ui()
    self.update_point_limit_ui()
    self.update_image_click_point_ui()
    self.update_template_mask_ui()
    self.update_step_region_ui()
    self.update_capture_region_ui()
    self.update_coord_step_ui()
    self.update_coord_step_manual_ui()
    self.update_coord_sequence_ui()
    self.update_until_condition_ui()
    self.apply_action_settings_visibility()

    btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    btn_box.button(QDialogButtonBox.Ok).setText("保存")
    btn_box.button(QDialogButtonBox.Cancel).setText("取消")
    btn_box.accepted.connect(self.accept)
    btn_box.rejected.connect(self.reject)
    outer_layout.addWidget(btn_box)

    geometry = self.dialog_settings.value("task_config_dialog_geometry")
    if geometry:
        self.restoreGeometry(geometry)
    else:
        self.resize(980, 650)
    self.update_context_note()
    apply_accessible_metadata(self, root_name="步骤设置")
