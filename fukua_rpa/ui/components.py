# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""Reusable layout, input and floating-dialog widgets.

Components may own generic presentation mechanics but not workflow, profile or
execution policy. Domain decisions belong in controllers and pure models.
"""

import contextlib
import logging
import time
from collections import OrderedDict

from PySide6.QtCore import QEvent, QPoint, QRect, QSignalBlocker, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .lifecycle import schedule_widget_callback
from .performance_hooks import (
    live_window_interaction,
    schedule_owner_interaction,
    ui_metrics,
)

logger = logging.getLogger(__name__)


def enable_work_window_controls(window: QWidget) -> None:
    """Give persistent work windows normal minimize/maximize controls.

    Transient pickers, overlays and confirmation dialogs deliberately do not
    use this helper because maximizing those surfaces would break their task.
    """

    window.setWindowFlags(
        window.windowFlags()
        | Qt.Window
        | Qt.WindowMinimizeButtonHint
        | Qt.WindowMaximizeButtonHint
        | Qt.WindowCloseButtonHint
    )

class FlowLayout(QLayout):
    """Lay out compact setting groups left-to-right and wrap as width changes."""

    def __init__(self, parent=None, margin=0, horizontal_spacing=10, vertical_spacing=7):
        super().__init__(parent)
        self._items = []
        self._horizontal_spacing = horizontal_spacing
        self._vertical_spacing = vertical_spacing
        self._height_cache = OrderedDict()
        self._height_cache_limit = 32
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)
        self.invalidate()

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        if not 0 <= index < len(self._items):
            return None
        item = self._items.pop(index)
        self.invalidate()
        return item

    def _clear_layout_caches(self):
        self._height_cache.clear()
        # QWidgetItem 自身缓存 sizeHint/minimumSize，QLayout::invalidate() 不会
        # 清除它们；只清 _height_cache 的话，子控件度量变化后 _do_layout 仍按
        # 旧条目度量布局（审查 UIS-RM-001）。逐条目失效让下次度量重算。
        for item in self._items:
            item.invalidate()

    def invalidate(self):
        """完整失效：清缓存并请求 Qt 重新布局（会向宿主重发 LayoutRequest）。

        SAFETY: 本方法经 QLayout::invalidate() 向宿主 post 新的 LayoutRequest。
        ResponsiveRow.event 处理 LayoutRequest 时**不得**调用本方法，否则形成
        无限自激（审查 FR-001），应改用 invalidate_caches()。
        """

        self._clear_layout_caches()
        super().invalidate()

    def invalidate_caches(self):
        """只清布局缓存，不请求 Qt 重发 LayoutRequest（审查 FR-001）。

        INVARIANT: 本方法是 LayoutRequest 事件处理路径**唯一**允许的失效入口。
        UIS-RM-001 依赖的两步（清高度缓存、逐条目 item.invalidate()）在此完整
        保留，只去掉 QLayout::invalidate() 的重发——否则 ResponsiveRow 会陷入
        "收到 LayoutRequest → 失效 → 再 post LayoutRequest"的自激，持续占满一个
        CPU 核心，违反 ARCHITECTURE #50 的高频事件有界性要求。
        """

        self._clear_layout_caches()

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        width = max(1, int(width))
        metrics = ui_metrics(self.parentWidget())
        cached = self._height_cache.get(width)
        if cached is not None:
            self._height_cache.move_to_end(width)
            if metrics is not None:
                metrics.increment("ui.layout.height_cache_hit")
            return cached
        started_ns = time.perf_counter_ns()
        height = self._do_layout(QRect(0, 0, width, 0), True)
        self._height_cache[width] = height
        self._height_cache.move_to_end(width)
        while len(self._height_cache) > self._height_cache_limit:
            self._height_cache.popitem(last=False)
        if metrics is not None:
            metrics.increment("ui.layout.height_cache_miss")
            metrics.observe_since("ui.layout.height_compute", started_ns)
            metrics.set_gauge(
                "ui.layout.height_cache_entries", len(self._height_cache)
            )
        return height

    def setGeometry(self, rect):
        super().setGeometry(rect)
        started_ns = time.perf_counter_ns()
        self._do_layout(rect, False)
        metrics = ui_metrics(self.parentWidget())
        if metrics is not None:
            metrics.observe_since("ui.layout.geometry_apply", started_ns)

    def horizontalSpacing(self):
        return self._horizontal_spacing

    def setHorizontalSpacing(self, spacing):
        spacing = max(0, int(spacing))
        if spacing == self._horizontal_spacing:
            return
        self._horizontal_spacing = spacing
        self.invalidate()

    def verticalSpacing(self):
        return self._vertical_spacing

    def setVerticalSpacing(self, spacing):
        spacing = max(0, int(spacing))
        if spacing == self._vertical_spacing:
            return
        self._vertical_spacing = spacing
        self.invalidate()

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(
            margins.left() + margins.right(),
            margins.top() + margins.bottom(),
        )

    def _do_layout(self, rect, test_only):
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        available_width = max(0, effective.width())
        lines = []
        line = []
        line_width = 0
        line_height = 0
        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            hint = item.sizeHint().expandedTo(item.minimumSize())
            required_width = hint.width() + (self._horizontal_spacing if line else 0)
            if line and line_width + required_width > available_width:
                lines.append((line, line_height))
                line = []
                line_width = 0
                line_height = 0
                required_width = hint.width()
            line.append((item, hint))
            line_width += required_width
            line_height = max(line_height, hint.height())
        if line:
            lines.append((line, line_height))

        if not lines:
            return margins.top() + margins.bottom()

        y = effective.y()
        for line_index, (line_items, current_line_height) in enumerate(lines):
            x = effective.x()
            for item_index, (item, hint) in enumerate(line_items):
                if not test_only:
                    widget = item.widget()
                    if (
                        widget is not None
                        and widget.property("flowTrailing")
                        and item_index == len(line_items) - 1
                    ):
                        x = max(
                            x,
                            effective.x() + available_width - hint.width(),
                        )
                    centered_y = y + max(0, (current_line_height - hint.height()) // 2)
                    item.setGeometry(QRect(QPoint(x, centered_y), hint))
                x += hint.width() + self._horizontal_spacing
            y += current_line_height
            if line_index < len(lines) - 1:
                y += self._vertical_spacing
        return y - rect.y() + margins.bottom()


class ResponsiveRow(QWidget):
    """A row of indivisible control groups that reflows at group boundaries."""

    def __init__(
        self,
        parent=None,
        horizontal_spacing=10,
        vertical_spacing=7,
        group_spacing=None,
    ):
        super().__init__(parent)
        self.setObjectName("responsiveRow")
        self._group_spacing = (
            int(horizontal_spacing) if group_spacing is None else int(group_spacing)
        )
        self._height_sync_pending = False
        self._height_sync_timer = QTimer(self)
        self._height_sync_timer.setSingleShot(True)
        self._height_sync_timer.timeout.connect(self._sync_height)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.flow_layout = FlowLayout(
            self,
            horizontal_spacing=horizontal_spacing,
            vertical_spacing=vertical_spacing,
        )

    def add_group(self, *parts):
        group = QWidget(self)
        group.setProperty("responsiveGroup", True)
        group.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        layout = QHBoxLayout(group)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(self._group_spacing)
        layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for part in parts:
            if part is None:
                continue
            if isinstance(part, str):
                label = QLabel(part)
                label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                layout.addWidget(label)
            else:
                layout.addWidget(part, 0, Qt.AlignVCenter)
        self.flow_layout.addWidget(group)
        self._schedule_height_sync()
        return group

    def add_trailing_group(self, *parts):
        """Add a final group that uses spare row width to stay right-aligned."""

        group = self.add_group(*parts)
        group.setProperty("flowTrailing", True)
        return group

    def add_widget(self, widget):
        self.flow_layout.addWidget(widget)
        self._schedule_height_sync()
        return widget

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.flow_layout.heightForWidth(max(1, int(width)))

    def sizeHint(self):
        width = max(1, self.contentsRect().width())
        minimum = self.flow_layout.minimumSize()
        return QSize(minimum.width(), self.heightForWidth(width))

    def minimumSizeHint(self):
        return self.flow_layout.minimumSize()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            metrics = ui_metrics(self)
            if metrics is not None:
                metrics.increment("ui.responsive_row.width_changes")
            self._schedule_height_sync()

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.LayoutRequest:
            # SAFETY: LayoutRequest 分支只清布局缓存，**不得**调用
            # flow_layout.invalidate()。后者经 QLayout::invalidate() 会向本行
            # 再次 post LayoutRequest，与本分支形成无限自激（审查 FR-001）。
            # UIS-RM-001 需要的"清高度缓存 + 逐条目失效"已由 invalidate_caches()
            # 完整保留，行高刷新语义不变（ARCHITECTURE #50）。
            self.flow_layout.invalidate_caches()
            self._schedule_height_sync()
        elif event.type() in (QEvent.FontChange, QEvent.StyleChange):
            # 字体/样式变化由用户显式触发且频率低，走完整失效是安全的：
            # 它重发的 LayoutRequest 会在上面的分支被收敛。
            self.flow_layout.invalidate()
            self._schedule_height_sync()
        return result

    def _schedule_height_sync(self):
        metrics = ui_metrics(self)
        if metrics is not None:
            metrics.increment("ui.responsive_row.sync_requests")
        if self._height_sync_pending:
            if metrics is not None:
                metrics.increment("ui.responsive_row.sync_coalesced")
            return
        self._height_sync_pending = True
        delay_ms = 33 if live_window_interaction(self) else 16
        self._height_sync_timer.start(delay_ms)

    def flush_height_sync(self):
        if not self._height_sync_pending:
            return
        self._height_sync_timer.stop()
        self._sync_height()

    def _sync_height(self):
        started_ns = time.perf_counter_ns()
        self._height_sync_pending = False
        width = max(1, self.contentsRect().width())
        target = max(0, self.flow_layout.heightForWidth(width))
        metrics = ui_metrics(self)
        if self.minimumHeight() == target and self.maximumHeight() == target:
            if metrics is not None:
                metrics.increment("ui.responsive_row.height_unchanged")
                metrics.observe_since("ui.responsive_row.sync", started_ns)
            return
        self.setMinimumHeight(target)
        self.setMaximumHeight(target)
        self.updateGeometry()
        if metrics is not None:
            metrics.increment("ui.responsive_row.height_applied")
            metrics.observe_since("ui.responsive_row.sync", started_ns)


class NoWheelComboBox(QComboBox):
    """Change options by direct selection, never by scrolling a closed box."""

    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
            return
        event.ignore()


class HistoryComboBox(NoWheelComboBox):
    """Emit once before a user interaction can change the selected value."""

    interactionStarted = Signal()

    def mousePressEvent(self, event):
        self.interactionStarted.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        self.interactionStarted.emit()
        super().keyPressEvent(event)


class HistoryLineEdit(QLineEdit):
    """Expose focus entry without installing a Python event filter."""

    focusStarted = Signal()

    def focusInEvent(self, event):
        self.focusStarted.emit()
        super().focusInEvent(event)


class ReorderableComboBox(NoWheelComboBox):
    """Combo box backed by a persistent popup list with internal reordering."""

    orderChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._order_popup = None
        self._order_list = None

    def item_order(self):
        return [self.itemText(index) for index in range(self.count())]

    def emit_order_changed(self):
        self.orderChanged.emit(self.item_order())

    def showPopup(self):
        if self._order_popup is not None and self._order_popup.isVisible():
            self._order_popup.raise_()
            return
        popup = QDialog(self, Qt.Popup | Qt.FramelessWindowHint)
        popup.setObjectName("profileOrderPopup")
        popup.setAttribute(Qt.WA_DeleteOnClose)
        popup_layout = QVBoxLayout(popup)
        popup_layout.setContentsMargins(1, 1, 1, 1)
        popup_layout.setSpacing(0)
        order_list = QListWidget(popup)
        order_list.setObjectName("profileOrderList")
        order_list.addItems(self.item_order())
        order_list.setDragEnabled(True)
        order_list.setAcceptDrops(True)
        order_list.setDropIndicatorShown(True)
        order_list.setDragDropMode(QAbstractItemView.InternalMove)
        order_list.setDefaultDropAction(Qt.MoveAction)
        order_list.setDragDropOverwriteMode(False)
        order_list.setToolTip("拖动方案可调整顺序；单击方案可切换")
        matches = order_list.findItems(self.currentText(), Qt.MatchExactly)
        if matches:
            order_list.setCurrentItem(matches[0])
            order_list.scrollToItem(matches[0])
        popup_layout.addWidget(order_list)
        order_list.itemClicked.connect(self._select_popup_item)
        order_list.model().rowsMoved.connect(
            lambda *_args: schedule_widget_callback(
                self, 0, lambda w: w._sync_popup_order()
            )
        )
        order_list.model().rowsInserted.connect(
            lambda *_args: schedule_widget_callback(
                self, 0, lambda w: w._sync_popup_order()
            )
        )
        order_list.model().rowsRemoved.connect(
            lambda *_args: schedule_widget_callback(
                self, 0, lambda w: w._sync_popup_order()
            )
        )
        popup.destroyed.connect(self._clear_order_popup)
        self._order_popup = popup
        self._order_list = order_list

        row_height = max(28, order_list.sizeHintForRow(0))
        visible_rows = min(max(1, order_list.count()), 12)
        popup_width = max(self.width(), order_list.sizeHintForColumn(0) + 48)
        popup_height = row_height * visible_rows + 4
        popup.resize(popup_width, popup_height)
        position = self.mapToGlobal(QPoint(0, self.height()))
        screen = QApplication.screenAt(position)
        if screen is not None:
            available = screen.availableGeometry()
            if position.x() + popup_width > available.right():
                position.setX(max(available.left(), available.right() - popup_width + 1))
            if position.y() + popup_height > available.bottom():
                position.setY(max(available.top(), self.mapToGlobal(QPoint(0, 0)).y() - popup_height))
        popup.move(position)
        popup.show()
        order_list.setFocus()

    def hidePopup(self):
        if self._order_popup is not None:
            self._order_popup.close()

    def _select_popup_item(self, item):
        self.setCurrentText(item.text())
        self.hidePopup()

    def _sync_popup_order(self):
        order_list = self._order_list
        if order_list is None:
            return
        names = [order_list.item(index).text() for index in range(order_list.count())]
        current_text = self.currentText()
        with QSignalBlocker(self):
            self.clear()
            self.addItems(names)
            self.setCurrentText(current_text)
        self.orderChanged.emit(names)

    def _clear_order_popup(self, *_args):
        self._order_popup = None
        self._order_list = None


class NoWheelSpinBox(QSpinBox):
    """Allow wheel changes only after the user explicitly focuses the editor."""

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
            return
        event.ignore()


class PersistentActionMenu(QMenu):
    """Keep option menus open while actions are toggled or presets are applied."""

    def _trigger_action_without_closing(self, action):
        if action is None or not action.isEnabled() or action.isSeparator():
            return False
        action.trigger()
        self.setActiveAction(action)
        return True

    def mouseReleaseEvent(self, event):
        position = (
            event.position().toPoint()
            if hasattr(event, "position")
            else event.pos()
        )
        if self._trigger_action_without_closing(self.actionAt(position)):
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space) and self._trigger_action_without_closing(self.activeAction()):
            event.accept()
            return
        super().keyPressEvent(event)


def fit_combo_to_contents(combo, minimum_width=0):
    """Keep every combo item readable without pinning the control to one width."""

    combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
    combo.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    combo.setMaximumWidth(16777215)
    if minimum_width:
        combo.setMinimumWidth(minimum_width)
    return combo


def fit_text_button(button, minimum_width=64):
    """Let translated button text grow with the active font/UI scale."""

    button.setProperty("fitText", True)
    button.setMaximumWidth(16777215)
    button.setMinimumWidth(minimum_width)
    button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    return button


def fit_compact_text_button(button, minimum_width=30):
    """Keep one-character utility buttons compact without clipping their text."""

    button.setProperty("compactText", True)
    button.setMinimumWidth(minimum_width)
    button.setMaximumWidth(16777215)
    button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    return button

class CollapsibleSection(QWidget):
    def __init__(self, title, parent=None, expanded=True):
        super().__init__(parent)
        self.title = title
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 8)
        self.main_layout.setSpacing(0)

        self.toggle_btn = QPushButton(f"▼ {self.title}")
        self.toggle_btn.setObjectName("sectionToggle")
        self.toggle_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setChecked(bool(expanded))
        self.toggle_btn.clicked.connect(self.on_toggle)

        self.content_widget = QFrame()
        self.content_widget.setObjectName("sectionContent")
        self.main_layout.addWidget(self.toggle_btn)
        self.main_layout.addWidget(self.content_widget)
        self.content_widget.setVisible(bool(expanded))
        self.on_toggle(bool(expanded))

    def set_content_layout(self, layout):
        layout.setContentsMargins(14, 12, 14, 12)
        self.content_widget.setLayout(layout)

    def set_expanded(self, expanded):
        expanded = bool(expanded)
        self.toggle_btn.setChecked(expanded)
        self.on_toggle(expanded)

    def is_expanded(self):
        return self.toggle_btn.isChecked()

    def on_toggle(self, checked):
        self.content_widget.setVisible(checked)
        self.toggle_btn.setText(f"▼ {self.title}" if checked else f"▶ {self.title}")
        self.updateGeometry()
        if self.parentWidget() is not None:
            self.parentWidget().updateGeometry()

class HelpBtn(QPushButton):
    def __init__(self, tip_text):
        super().__init__("?")
        self.setObjectName("helpButton")
        # Scale button size with the active font so it grows under UI scaling.
        _size = max(20, self.fontMetrics().height() + 2)
        self.setFixedSize(_size, _size)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName("帮助")
        self.tip_text = tip_text
        self.setAccessibleDescription(tip_text)
        self._popup = None
        self.clicked.connect(self.show_tip)

    def show_tip(self):
        previous = self._popup
        if previous is not None:
            with contextlib.suppress(RuntimeError):
                previous.close()
        popup = QFrame(None, Qt.Popup | Qt.FramelessWindowHint)
        popup.setObjectName("helpPopup")
        popup.setAttribute(Qt.WA_DeleteOnClose)
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(14, 12, 14, 12)
        label = QLabel(self.tip_text)
        label.setObjectName("helpPopupText")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(label)

        origin = self.mapToGlobal(QPoint(0, self.height() + 6))
        screen = QApplication.screenAt(origin) or QApplication.primaryScreen()
        bounds = screen.availableGeometry() if screen is not None else QRect()
        label.setMaximumWidth(
            max(220, min(440, bounds.width() - 40))
            if bounds.isValid()
            else 440
        )
        popup.adjustSize()
        x = origin.x()
        y = origin.y()
        if bounds.isValid():
            max_x = max(bounds.left(), bounds.right() - popup.width() + 1)
            x = min(max(bounds.left(), x), max_x)
            if y + popup.height() > bounds.bottom():
                y = self.mapToGlobal(QPoint(0, -popup.height() - 6)).y()
            max_y = max(bounds.top(), bounds.bottom() - popup.height() + 1)
            y = min(max(bounds.top(), y), max_y)
        popup.move(x, y)
        popup.destroyed.connect(lambda *_args: setattr(self, "_popup", None))
        self._popup = popup
        popup.show()
        popup.raise_()

class FloatingSettingsDialog(QDialog):
    visibilityChanged = Signal(bool)

    def __init__(self, settings, geometry_key, title, default_size):
        super().__init__(None)
        self.settings = settings
        self.geometry_key = geometry_key
        self.default_size = default_size
        self.setAttribute(Qt.WA_QuitOnClose, False)
        enable_work_window_controls(self)
        self.setWindowModality(Qt.NonModal)
        self.setWindowTitle(title)
        self.setMinimumSize(720, 520)

        geometry = self.settings.value(self.geometry_key)
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(*self.default_size)

    def save_dialog_geometry(self):
        try:
            self.settings.setValue(self.geometry_key, self.saveGeometry())
        except Exception as exc:
            logger.warning("保存窗口几何失败: %s", exc, exc_info=True)
            pass

    def disable_auto_default_buttons(self):
        """Prevent Enter in an editor from activating an unrelated command."""

        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

    def moveEvent(self, event):
        super().moveEvent(event)
        schedule_owner_interaction(self, "settings_move")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        schedule_owner_interaction(self, "settings_resize")

    def showEvent(self, event):
        self.disable_auto_default_buttons()
        super().showEvent(event)
        self.visibilityChanged.emit(True)

    def hideEvent(self, event):
        self.save_dialog_geometry()
        super().hideEvent(event)
        self.visibilityChanged.emit(False)

    def closeEvent(self, event):
        self.save_dialog_geometry()
        super().closeEvent(event)
