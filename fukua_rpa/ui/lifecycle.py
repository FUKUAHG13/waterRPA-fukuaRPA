"""Safe lifetime and runtime-state helpers for the UI layer.

包含：无父 Qt 选择器/遮罩的安全生命周期工具（widget_is_alive / safe_close 等），
以及跨 UI 组件的运行时编辑锁统一查询（is_runtime_editing_locked）。编辑锁的
状态由主窗口/转发器持有（_runtime_editing_locked），本模块只提供读入口，
避免各控制器直读私有属性。
"""

from __future__ import annotations

import weakref

from PySide6.QtCore import QTimer
from shiboken6 import isValid


def widget_is_alive(widget) -> bool:
    try:
        return widget is not None and bool(isValid(widget))
    except (RuntimeError, TypeError):
        return False


def safe_close(widget) -> bool:
    if not widget_is_alive(widget):
        return False
    try:
        widget.close()
        return True
    except RuntimeError:
        return False


def schedule_widget_callback(widget, delay_ms, callback):
    """Run a callback only while its owning QWidget still exists."""

    if not widget_is_alive(widget):
        return None
    owner_ref = weakref.ref(widget)
    timer = QTimer(widget)
    timer.setSingleShot(True)

    def dispatch():
        owner = owner_ref()
        if widget_is_alive(owner):
            callback(owner)

    timer.timeout.connect(dispatch)
    timer.start(max(0, int(delay_ms)))
    return timer


def schedule_close(widget, delay_ms):
    return schedule_widget_callback(widget, delay_ms, lambda owner: owner.close())


def clear_attribute_if_same(owner, name, expected):
    try:
        if getattr(owner, name, None) is expected:
            setattr(owner, name, None)
    except RuntimeError:
        pass


def is_runtime_editing_locked(owner) -> bool:
    """检查运行时编辑锁（公共守卫，供所有编辑入口统一调用）。"""
    return bool(getattr(owner, "_runtime_editing_locked", False))
