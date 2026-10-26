"""Regression test for report S9.

The centered checkbox delegate used for the "enabled" column of the
scheduled-tasks window only painted an indicator but never implemented
``editorEvent``, so clicking the cell did nothing. The model already
supports toggling via ``setData(..., Qt.CheckStateRole)``; this test
confirms the delegate now forwards clicks to it.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QPointF, QRect, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QStyleOptionViewItem,
    QWidget,
)

from fukua_rpa.ui.scheduled_tasks_window import _ButtonDelegate, _CenterCheckboxDelegate

_ROOT_INDEX = QModelIndex()
_APP = QApplication.instance() or QApplication([])


class _LockAwareWindow(QWidget):
    """最小顶层窗口替身：只提供启用列委托所需的 ``_deny_while_locked``。"""

    def __init__(self) -> None:
        super().__init__()
        self.locked = False

    def _deny_while_locked(self) -> bool:
        return self.locked


# 保持引用：`QStyleOptionViewItem.widget` 存的是指针，需要一个存活的宿主。
_OWNER_WINDOW = _LockAwareWindow()


def _option(owner: QWidget | None = None) -> QStyleOptionViewItem:
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 40, 30)
    # RM-SCOPE014-001: 启用列委托必须能经 `option.widget.window()` 解析到「能给出
    # 锁结论的窗口」才会改写数据（原先 fail-open，解析失败时静默放行；现已改为
    # fail-closed）。裸 `QStyleOptionViewItem` 的 widget 为 None，是 Qt 视图不会
    # 产生的状态，因此这里挂一个最小顶层窗口替身，模拟真实视图提供的 option。
    # 解析失败时的 fail-closed 行为由
    # tests/test_scheduled_tasks_lock_guards.py 的
    # test_unresolvable_widget_denies_checkbox_toggle 专门覆盖。
    option.widget = owner if owner is not None else _OWNER_WINDOW
    return option


def _release(button, x=20, y=15) -> QMouseEvent:
    point = QPointF(x, y)
    return QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        point,
        point,
        button,
        button,
        Qt.KeyboardModifier.NoModifier,
    )


class _StubEnabledModel(QAbstractTableModel):
    """Minimal model that behaves like ScheduledTasksTableModel's
    COL_ENABLED column for the purpose of exercising the delegate."""

    def __init__(self) -> None:
        super().__init__()
        self.enabled = True
        self.set_enabled_calls: list = []

    def rowCount(self, parent: QModelIndex = _ROOT_INDEX) -> int:  # noqa: N802
        return 1

    def columnCount(self, parent: QModelIndex = _ROOT_INDEX) -> int:  # noqa: N802
        return 1

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:  # noqa: N802
        return Qt.ItemIsEnabled | Qt.ItemIsUserCheckable

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):  # noqa: N802
        if role == Qt.CheckStateRole:
            return Qt.Checked if self.enabled else Qt.Unchecked
        return None

    def setData(  # noqa: N802
        self, index: QModelIndex, value, role: int = Qt.EditRole
    ) -> bool:
        if role == Qt.CheckStateRole:
            self.enabled = bool(value == Qt.Checked)
            self.set_enabled_calls.append(value)
            self.dataChanged.emit(index, index)
            return True
        return False


def test_checkbox_delegate_toggles_on_click() -> None:
    model = _StubEnabledModel()
    delegate = _CenterCheckboxDelegate()
    index = model.index(0, 0)
    release = _release(Qt.MouseButton.LeftButton)
    option = _option()

    # Initially checked -> a click should uncheck it.
    assert delegate.editorEvent(release, model, option, index) is True
    assert model.enabled is False

    # A second click should check it again.
    assert delegate.editorEvent(release, model, option, index) is True
    assert model.enabled is True

    # Only release events toggle; a press is ignored.
    press = QEvent(QEvent.Type.MouseButtonPress)
    assert delegate.editorEvent(press, model, option, index) is False
    assert model.enabled is True

    # Right-click opens the row menu and must not toggle the checkbox.
    right_release = _release(Qt.MouseButton.RightButton)
    assert delegate.editorEvent(right_release, model, option, index) is False
    assert model.enabled is True

    # A left-click elsewhere in the cell selects the row but does not toggle.
    outside = _release(Qt.MouseButton.LeftButton, 1, 1)
    assert delegate.editorEvent(outside, model, option, index) is False
    assert model.enabled is True


def test_checkbox_delegate_ignores_cells_without_check_state() -> None:
    model = _StubEnabledModel()
    # Simulate a cell that has no check state by returning None.
    model.data = lambda idx, role=Qt.DisplayRole: None  # type: ignore[assignment]
    delegate = _CenterCheckboxDelegate()
    index = model.index(0, 0)
    release = _release(Qt.MouseButton.LeftButton)
    assert delegate.editorEvent(release, model, _option(), index) is False


def test_log_button_delegate_ignores_right_click() -> None:
    model = _StubEnabledModel()
    index = model.index(0, 0)
    delegate = _ButtonDelegate(0)
    clicks = []
    delegate.clicked.connect(lambda column, clicked: clicks.append((column, clicked.row())))

    assert (
        delegate.editorEvent(
            _release(Qt.MouseButton.RightButton), model, _option(), index
        )
        is False
    )
    assert clicks == []
    assert (
        delegate.editorEvent(
            _release(Qt.MouseButton.LeftButton), model, _option(), index
        )
        is True
    )
    assert clicks == [(0, 0)]
