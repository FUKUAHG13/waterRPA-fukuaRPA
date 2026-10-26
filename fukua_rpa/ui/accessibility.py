# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""Keyboard focus and assistive-technology helpers for Qt views."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QTextEdit,
    QWidget,
)

_DECORATION_PREFIX = re.compile(r"^[\s✓✔●•]+")


def _plain_label(text) -> str:
    value = str(text or "").replace("&", "").strip()
    value = _DECORATION_PREFIX.sub("", value)
    return value.rstrip(":：").strip()


def _field_widget(field):
    if isinstance(field, QWidget):
        if field.focusPolicy() != Qt.NoFocus:
            return field
        for child in field.findChildren(QWidget):
            if child.focusPolicy() != Qt.NoFocus:
                return child
    return None


def _layout_item_widget(item):
    if item is None:
        return None
    target = _field_widget(item.widget())
    if target is not None:
        return target
    layout = item.layout()
    if layout is None:
        return None
    for index in range(layout.count()):
        target = _layout_item_widget(layout.itemAt(index))
        if target is not None:
            return target
    return None


def _widget_layout(widget):
    if not isinstance(widget, QWidget):
        return None
    try:
        return QWidget.layout(widget)
    except (RuntimeError, TypeError):
        return None


def _layouts_in(root):
    seen = set()
    stack = [_widget_layout(root)]
    stack.extend(_widget_layout(widget) for widget in root.findChildren(QWidget))
    while stack:
        layout = stack.pop()
        if layout is None or id(layout) in seen:
            continue
        seen.add(id(layout))
        yield layout
        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item.layout() is not None:
                stack.append(item.layout())
            widget = item.widget()
            widget_layout = _widget_layout(widget)
            if widget_layout is not None:
                stack.append(widget_layout)


def _widget_name(widget) -> str:
    if isinstance(widget, QAbstractButton):
        return _plain_label(widget.text()) or _plain_label(widget.toolTip())
    if isinstance(widget, QComboBox):
        return _plain_label(widget.currentText())
    if isinstance(widget, (QLineEdit, QSpinBox)):
        return _plain_label(getattr(widget, "placeholderText", lambda: "")())
    return _plain_label(widget.toolTip())


def bind_form_labels(root: QWidget) -> None:
    """Connect QFormLayout labels to their first keyboard-focusable field."""

    for form in (layout for layout in _layouts_in(root) if isinstance(layout, QFormLayout)):
        for row in range(form.rowCount()):
            label_item = form.itemAt(row, QFormLayout.LabelRole)
            field_item = form.itemAt(row, QFormLayout.FieldRole)
            label = label_item.widget() if label_item is not None else None
            target = _layout_item_widget(field_item)
            if not isinstance(label, QLabel) or target is None:
                continue
            name = _plain_label(label.text())
            if name:
                label.setBuddy(target)
                if not target.accessibleName():
                    target.setAccessibleName(name)


def bind_inline_labels(root: QWidget) -> None:
    """Bind labels created inside ResponsiveRow groups to adjacent fields."""

    for group in root.findChildren(QWidget):
        layout = _widget_layout(group)
        if not group.property("responsiveGroup") or layout is None:
            continue
        pending_label = None
        for index in range(layout.count()):
            widget = layout.itemAt(index).widget()
            if isinstance(widget, QLabel) and widget.text().strip():
                pending_label = widget
                continue
            target = _field_widget(widget)
            if pending_label is None or target is None:
                continue
            name = _plain_label(pending_label.text())
            if name:
                pending_label.setBuddy(target)
                if not target.accessibleName():
                    target.setAccessibleName(name)
            pending_label = None


def apply_accessible_metadata(root: QWidget, *, root_name="") -> None:
    """Fill missing names/descriptions without overriding explicit metadata."""

    if root_name and not root.accessibleName():
        root.setAccessibleName(str(root_name))
    bind_form_labels(root)
    bind_inline_labels(root)
    for widget in root.findChildren(QWidget):
        if not widget.accessibleName():
            name = _widget_name(widget)
            if name:
                widget.setAccessibleName(name)
        if widget.toolTip() and not widget.accessibleDescription():
            widget.setAccessibleDescription(str(widget.toolTip()))


def set_accessible(widget: QWidget, name: str, description="") -> QWidget:
    widget.setAccessibleName(str(name))
    if description:
        widget.setAccessibleDescription(str(description))
    return widget


def set_tab_order(widgets) -> None:
    """Set a stable order while allowing Qt to skip hidden/disabled controls."""

    ordered = []
    for widget in widgets:
        if (
            isinstance(widget, QWidget)
            and widget.focusPolicy() != Qt.NoFocus
            and (not ordered or ordered[-1] is not widget)
        ):
            ordered.append(widget)
    for first, second in zip(ordered, ordered[1:], strict=False):
        QWidget.setTabOrder(first, second)


def configure_main_window_accessibility(host) -> None:
    set_accessible(host.profile_combo, "当前方案")
    set_accessible(
        host.task_list,
        "脚本步骤列表",
        "使用上下方向键选择步骤，Enter 或 F2 打开步骤设置，Ctrl+上下方向键调整顺序。",
    )
    set_accessible(host.loop_combo, "循环方式")
    set_accessible(host.loop_val_edit, "循环数值")
    set_accessible(host.log_text, "运行日志")
    set_accessible(host.cpu_label, "CPU 与运行资源状态")
    apply_accessible_metadata(host, root_name="fukuaRPA 主窗口")
    set_tab_order(
        (
            host.profile_combo,
            host.new_profile_btn,
            host.rename_profile_btn,
            host.delete_profile_btn,
            host.settings_btn,
            host.add_step_btn,
            host.insert_step_btn,
            host.redo_btn,
            host.record_btn,
            host.region_btn,
            host.region_help_btn,
            host.preview_points_btn,
            host.task_search_btn,
            host.script_check_btn,
            host.breakpoint_toggle_btn,
            host.single_step_btn,
            host.task_search_edit,
            host.task_search_prev_btn,
            host.task_search_next_btn,
            host.task_search_close_btn,
            host.task_list,
            host.loop_combo,
            host.loop_val_edit,
            host.mini_chk,
            host.top_chk,
            host.start_btn,
            host.stop_btn,
            host.debug_pause_btn,
            host.debug_continue_btn,
            host.debug_next_btn,
            host.debug_variables_btn,
            host.log_text,
        )
    )


def text_clipping_issues(root: QWidget) -> list[str]:
    """Return obvious single-line clipping issues for deterministic UI review."""

    issues = []
    for widget in root.findChildren(QWidget):
        if not widget.isVisible() or widget.width() <= 0 or widget.height() <= 0:
            continue
        text = ""
        horizontal_padding = 12
        if isinstance(widget, QAbstractButton):
            text = _plain_label(widget.text())
            if widget.objectName() == "helpButton" or widget.property("iconOnly"):
                horizontal_padding = 0
            else:
                horizontal_padding = 18 + (
                    widget.iconSize().width() if not widget.icon().isNull() else 0
                )
        elif isinstance(widget, QComboBox):
            text = str(widget.currentText() or "")
            horizontal_padding = 34
        elif isinstance(widget, QLabel) and not widget.wordWrap():
            text = str(widget.text() or "")
            horizontal_padding = 2
        elif isinstance(widget, (QTextEdit, QPlainTextEdit)):
            continue
        if not text or "\n" in text or "<" in text:
            continue
        needed = widget.fontMetrics().horizontalAdvance(text) + horizontal_padding
        if needed > widget.width() + 3:
            identifier = widget.objectName() or widget.accessibleName() or type(widget).__name__
            issues.append(f"{identifier}: 需要 {needed}px，实际 {widget.width()}px，文字“{text}”")
    return issues
