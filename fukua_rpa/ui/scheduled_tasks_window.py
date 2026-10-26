# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false, reportOptionalCall=false, reportOptionalIterable=false, reportOptionalOperand=false, reportReturnType=false
"""Scheduled-tasks main window (design §4.1 / §4.2).

A non-modal, single-instance-friendly top-level window that lists the user's
scheduled plans, shows a live service summary, and lets the user create / edit /
duplicate / delete / enable plans and import / export them.

The window NEVER starts a run itself and NEVER simulates a main-window button
press. The only host entry it calls is ``on_try_run(profile_id)`` (for the
"立即运行" / "立即试运行" commands).  Per-plan logs open in their own
top-level window and never reuse the main application's log pane.
Everything else is delegated to the injected :class:`ScheduledTaskService`,
which remains the single source of truth (design §4.5).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable

from PySide6.QtCore import QEvent, QModelIndex, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionButton,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from .components import (
    FloatingSettingsDialog,
    NoWheelComboBox,
    fit_combo_to_contents,
    fit_text_button,
)
from .controllers.scheduled_tasks_controller import (
    COL_ENABLED,
    COL_LOG,
    SORT_OPTIONS,
    STATUS_FILTER_OPTIONS,
    ScheduledTasksController,
)
from .scheduled_task_log_window import ScheduledTaskLogWindow
from .scheduled_tasks_mixins import (
    ContextMenuMixin,
    ImportExportMixin,
    SummaryMixin,
)

COLUMN_WIDTHS_KEY = "scheduled_tasks_col_widths"


class _ButtonDelegate(QStyledItemDelegate):
    """Paints a push-button inside a table cell and reports clicks."""

    clicked = Signal(int, QModelIndex)

    def __init__(self, column: int, parent=None):
        super().__init__(parent)
        self._column = column

    def paint(self, painter, option, index):
        if not index.isValid():
            return
        painter.save()
        opt = QStyleOptionButton()
        opt.initFrom(option.widget)
        opt.rect = option.rect.adjusted(3, 3, -3, -3)
        opt.text = str(index.data(Qt.DisplayRole) or "")
        opt.state = QStyle.State_Enabled | QStyle.State_Active
        if option.state & QStyle.State_MouseOver:
            opt.state |= QStyle.State_MouseOver
        if option.state & QStyle.State_Selected:
            opt.state |= QStyle.State_Selected
        QApplication.style().drawControl(QStyle.CE_PushButton, opt, painter, option.widget)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            button_rect = option.rect.adjusted(3, 3, -3, -3)
            if button_rect.contains(event.position().toPoint()):
                self.clicked.emit(self._column, index)
                return True
        return False

    def sizeHint(self, option, index):
        return QSize(56, max(24, option.rect.height()))


class _CenterCheckboxDelegate(QStyledItemDelegate):
    """Renders the checkbox indicator centered in the cell."""

    def paint(self, painter, option, index):
        # Draw background (selection/hover effects)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # Clear text, keep only background
        opt.text = ""
        opt.features &= ~QStyleOptionViewItem.HasCheckIndicator
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, option.widget)
        # Draw checkbox indicator centered
        check_state = index.data(Qt.CheckStateRole)
        if check_state is None:
            return
        indicator_w = style.pixelMetric(QStyle.PM_IndicatorWidth, opt, option.widget)
        indicator_h = style.pixelMetric(QStyle.PM_IndicatorHeight, opt, option.widget)
        x = option.rect.x() + (option.rect.width() - indicator_w) // 2
        y = option.rect.y() + (option.rect.height() - indicator_h) // 2
        check_opt = QStyleOptionViewItem(opt)
        check_opt.rect = QRect(x, y, indicator_w, indicator_h)
        check_opt.checkState = Qt.CheckState(check_state)
        check_opt.features |= QStyleOptionViewItem.HasCheckIndicator
        check_opt.state &= ~(QStyle.State_On | QStyle.State_Off | QStyle.State_NoChange)
        if check_state == Qt.Checked:
            check_opt.state |= QStyle.State_On
        elif check_state == Qt.PartiallyChecked:
            check_opt.state |= QStyle.State_NoChange
        else:
            check_opt.state |= QStyle.State_Off
        style.drawPrimitive(
            QStyle.PE_IndicatorItemViewItemCheck,
            check_opt,
            painter,
            option.widget,
        )

    def editorEvent(self, event, model, option, index):
        # Make the centered checkbox actually toggle on click (report S9).
        # Mirrors _ButtonDelegate: the view forwards mouse events for enabled
        # items to the delegate, and the model already handles setData with
        # CheckStateRole via ScheduledTasksTableModel.setData.
        if (
            event.type() != QEvent.Type.MouseButtonRelease
            or event.button() != Qt.MouseButton.LeftButton
        ):
            return False
        current = index.data(Qt.CheckStateRole)
        if current is None:
            return False
        style = option.widget.style() if option.widget else QApplication.style()
        indicator_w = style.pixelMetric(
            QStyle.PM_IndicatorWidth,
            option,
            option.widget,
        )
        indicator_h = style.pixelMetric(
            QStyle.PM_IndicatorHeight,
            option,
            option.widget,
        )
        indicator_rect = QRect(
            option.rect.x() + (option.rect.width() - indicator_w) // 2,
            option.rect.y() + (option.rect.height() - indicator_h) // 2,
            indicator_w,
            indicator_h,
        )
        if not indicator_rect.contains(event.position().toPoint()):
            return False
        # DESIGN (UIC15): 启用列 checkbox 直接改写计划数据（setData → set_enabled），
        # 是唯一不走窗口方法入口的改写路径，必须同样受运行时编辑锁约束。
        #
        # RM-SCOPE014-001：**解析失败必须 fail-closed**。``option.widget`` 为 None
        # 或其 window() 不提供 ``_deny_while_locked`` 时，我们无法判断是否处于
        # 运行时编辑锁内。此前这两种情况都直接放行，于是「无法解析」变成了
        # 「允许改写」——恰恰是最需要拒绝的时候放行。拒绝必须留痕，不得静默。
        view = option.widget
        window = view.window() if view is not None else None
        deny = getattr(window, "_deny_while_locked", None)
        if deny is None:
            logger.warning(
                "计划列表启用列：无法解析所属窗口（widget=%r），已拒绝本次勾选",
                type(view).__name__ if view is not None else None,
            )
            return True
        if deny():
            return True
        new_state = Qt.Unchecked if current == Qt.Checked else Qt.Checked
        model.setData(index, new_state, Qt.CheckStateRole)
        return True


class ScheduledTasksWindow(
    ContextMenuMixin,
    ImportExportMixin,
    SummaryMixin,
    FloatingSettingsDialog,
):
    """Top-level scheduled-tasks browser."""

    def __init__(
        self,
        service,
        get_profiles: Callable[[], list[tuple]],
        on_try_run: Callable[[str], None],
        settings,
        plugin_status: Callable[[str], bool | None] | None = None,
        parent=None,
        is_runtime_editing_locked: Callable[[], bool] | None = None,
    ):
        super().__init__(settings, "scheduled_tasks_geometry", "计划任务", (1500, 700))
        self._service = service
        self._get_profiles = get_profiles
        self._on_try_run = on_try_run
        self._plugin_status = plugin_status
        # DESIGN (UIC15): 计划任务窗口默认不感知运行时编辑锁（旧行为，编辑/复制/
        # 删除/启停在运行中一直可用）；主窗口传入真实锁查询后，运行中禁止修改计划。
        # 兼容性：不传参时回退为「永不锁定」，保持既有调用方（如测试）行为不变。
        self._is_runtime_editing_locked = is_runtime_editing_locked or (
            lambda: False
        )

        self._controller = ScheduledTasksController(service, plugin_status)
        self._log_windows: dict[str, ScheduledTaskLogWindow] = {}
        self._eta_timer = QTimer(self)
        self._eta_timer.setInterval(1000)
        self._eta_timer.timeout.connect(self._tick_live_fields)

        self._build_ui()
        self._restore_column_widths()
        self._update_summary()

        service.summary_changed.connect(self._update_summary)
        # Keep the "current running" summary indicator live as runs start/finish.
        service.run_started.connect(self._update_summary)
        service.run_finished.connect(self._update_summary)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_summary_panel())
        root.addWidget(self._build_filter_bar())
        root.addWidget(self._build_table(), 1)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._btn_new = fit_text_button(QPushButton("新建计划"))
        self._btn_refresh = fit_text_button(QPushButton("刷新"))
        self._btn_toggle = fit_text_button(QPushButton("启用/停用所选"))
        self._btn_import = fit_text_button(QPushButton("导入计划"))
        self._btn_export = fit_text_button(QPushButton("导出所选"))
        self._btn_settings = fit_text_button(QPushButton("计划设置"))

        self._btn_new.clicked.connect(self._on_new)
        self._btn_refresh.clicked.connect(self._refresh_all)
        self._btn_toggle.clicked.connect(self._on_toggle_selected)
        self._btn_import.clicked.connect(self._on_import)
        self._btn_export.clicked.connect(self._on_export)
        self._btn_settings.clicked.connect(self._on_open_settings)

        for btn in (
            self._btn_new,
            self._btn_refresh,
            self._btn_toggle,
            self._btn_import,
            self._btn_export,
            self._btn_settings,
        ):
            layout.addWidget(btn)
        layout.addStretch(1)
        return bar

    def _build_summary_panel(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("summaryPanel")
        frame.setFrameShape(QFrame.StyledPanel)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(18)

        self._summary = {}
        fields = [
            ("service", "调度服务"),
            ("uptime", "已运行"),
            ("counts", "计划/启用/异常"),
            ("last_check", "最近检查"),
            ("next", "下次任务"),
            ("eta", "剩余时间"),
            ("running", "正在运行"),
        ]
        for key, label in fields:
            col = QVBoxLayout()
            col.setSpacing(2)
            title = QLabel(label)
            title.setObjectName("summaryTitle")
            value = QLabel("—")
            value.setObjectName("summaryValue")
            col.addWidget(title)
            col.addWidget(value)
            self._summary[key] = value
            layout.addLayout(col)
        layout.addStretch(1)
        return frame

    def _build_filter_bar(self) -> QWidget:
        bar = QWidget(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索任务名或方案名…")
        self._search_edit.textChanged.connect(self._on_search_changed)

        self._status_combo = NoWheelComboBox()
        for value, label in STATUS_FILTER_OPTIONS:
            self._status_combo.addItem(label, value)
        self._status_combo.setCurrentIndex(0)
        fit_combo_to_contents(self._status_combo)
        self._status_combo.currentIndexChanged.connect(self._on_status_changed)

        self._sort_combo = NoWheelComboBox()
        for value, label in SORT_OPTIONS:
            self._sort_combo.addItem(label, value)
        self._sort_combo.setCurrentIndex(0)
        fit_combo_to_contents(self._sort_combo)
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)

        layout.addWidget(QLabel("筛选："))
        layout.addWidget(self._search_edit, 1)
        layout.addWidget(self._status_combo)
        layout.addWidget(self._sort_combo)
        return bar

    def _build_table(self) -> QTableView:
        table = QTableView(self)
        table.setObjectName("scheduledTasksTable")
        table.setModel(self._controller.model())
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSortingEnabled(False)
        table.setContextMenuPolicy(Qt.CustomContextMenu)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(False)
        table.doubleClicked.connect(self._on_row_double_clicked)
        table.customContextMenuRequested.connect(self._on_table_context_menu)

        self._log_delegate = _ButtonDelegate(COL_LOG, table)
        table.setItemDelegateForColumn(COL_LOG, self._log_delegate)
        self._log_delegate.clicked.connect(self._on_log_clicked)

        self._checkbox_delegate = _CenterCheckboxDelegate(table)
        table.setItemDelegateForColumn(COL_ENABLED, self._checkbox_delegate)
        return table

    # ------------------------------------------------------------------
    # Filter handlers
    # ------------------------------------------------------------------
    def _on_search_changed(self, text: str) -> None:
        self._controller.model().set_search(text)

    def _on_status_changed(self, _index: int) -> None:
        self._controller.model().set_status_filter(self._status_combo.currentData())

    def _on_sort_changed(self, _index: int) -> None:
        self._controller.model().set_sort(self._sort_combo.currentData())

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------
    def _selected_schedule_ids(self) -> list[str]:
        rows = self._table().selectionModel().selectedRows()
        ids = []
        model = self._controller.model()
        for index in rows:
            sid = model.schedule_id_at(index)
            if sid:
                ids.append(sid)
        return ids

    def _table(self) -> QTableView:
        return self.findChild(QTableView, "scheduledTasksTable")

    # ------------------------------------------------------------------
    # Toolbar actions
    # ------------------------------------------------------------------
    def _on_new(self) -> None:
        if self._deny_while_locked():
            return
        self._open_editor(None)

    def _on_toggle_selected(self) -> None:
        if self._deny_while_locked():
            return
        ids = self._selected_schedule_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先选择至少一个计划。")
            return
        # FR-20260812 建议-架构-5：切换的语义是**逐条取反**，所以混合选中的行不能
        # 被统一成同一状态——必须按当前状态分成「将启用」与「将停用」两组，各调用
        # 一次 ``set_enabled_many``。此前逐条调 ``set_enabled``，N 行触发 N 次全量
        # 写盘 + N 次 tasks_changed + N 次重算，批量操作被放大成 N 倍开销。
        enable_ids: list[str] = []
        disable_ids: list[str] = []
        for sid in ids:
            row = self._find_row_by_id(sid)
            if row is None:
                continue
            (disable_ids if row["task"].enabled else enable_ids).append(sid)
        if enable_ids:
            self._service.set_enabled_many(enable_ids, True)
        if disable_ids:
            self._service.set_enabled_many(disable_ids, False)

    def _find_row_by_id(self, sid: str):
        for row in self._controller.model()._rows:
            if row["task"].schedule_id == sid:
                return row
        return None

    def _on_open_settings(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("计划设置")
        dlg.setMinimumWidth(360)
        layout = QVBoxLayout(dlg)
        layout.addWidget(
            QLabel(
                "计划调度服务由软件在运行时统一管理。\n"
                "导入的计划默认停用，需手动检查后启用。"
            )
        )
        clear_btn = fit_text_button(QPushButton("清除运行历史"))
        clear_btn.clicked.connect(self._clear_history_guarded)
        layout.addWidget(clear_btn)
        close_btn = fit_text_button(QPushButton("关闭"))
        close_btn.clicked.connect(dlg.accept)
        layout.addWidget(close_btn)
        dlg.exec()

    # ------------------------------------------------------------------
    # Editor
    # ------------------------------------------------------------------
    def _open_editor(self, schedule_id: str | None) -> None:
        if self._deny_while_locked():
            return
        # Close existing editor to maintain single-instance behavior
        existing = getattr(self, "_editor", None)
        if existing is not None:
            with contextlib.suppress(RuntimeError):
                existing.close()
            self._editor = None

        editor = ScheduledTaskEditor(
            self.settings,
            self._service,
            self._get_profiles,
            self._on_try_run,
            schedule_id=schedule_id,
            plugin_status=self._plugin_status,
            is_runtime_editing_locked=self._is_runtime_editing_locked,
        )
        editor.setAttribute(Qt.WA_DeleteOnClose)
        editor.destroyed.connect(lambda: setattr(self, "_editor", None))
        self._editor = editor  # prevent GC from destroying the window
        editor.show()
        editor.raise_()
        editor.activateWindow()

    def _on_row_double_clicked(self, index) -> None:
        sid = self._controller.model().schedule_id_at(index)
        if sid:
            self._open_editor(sid)

    def _clear_history_guarded(self) -> None:
        """清除运行历史（运行中锁定态下拦截，UIC15）。"""
        if self._deny_while_locked():
            return
        self._service.clear_history()
        QMessageBox.information(self, "完成", "运行历史已清除。")

    def _deny_while_locked(self) -> bool:
        """运行中编辑锁守卫：锁定态忽略本次修改操作并提示（UIC15）。

        返回 True 表示操作被拦截；调用方应直接 return，不执行修改。
        """
        if not self._is_runtime_editing_locked():
            return False
        QMessageBox.information(
            self,
            "无法修改",
            "方案正在运行中，不能修改计划任务。请先停止当前运行。",
        )
        return True

    # ------------------------------------------------------------------
    # Table cell handlers
    # ------------------------------------------------------------------
    def _on_log_clicked(self, _column: int, index) -> None:
        sid = self._controller.model().schedule_id_at(index)
        if sid:
            self._open_log_window(sid)

    def _open_log_window(self, sid: str) -> None:
        window = self._log_windows.get(sid)
        if window is None:
            window = ScheduledTaskLogWindow(self._service, sid, self.settings)
            window.setAttribute(Qt.WA_DeleteOnClose)
            window.destroyed.connect(
                lambda _obj=None, key=sid: self._log_windows.pop(key, None)
            )
            self._log_windows[sid] = window
        window.show()
        window.raise_()
        window.activateWindow()

    # ------------------------------------------------------------------
    # Column width persistence
    # ------------------------------------------------------------------
    def _restore_column_widths(self) -> None:
        table = self._table()
        if table is None:
            return
        try:
            raw = self.settings.value(COLUMN_WIDTHS_KEY)
        except Exception as exc:
            logger.debug("读取列宽设置失败: %s", exc, exc_info=True)
            raw = None
        if not raw:
            return
        try:
            widths = [int(w) for w in raw]
        except Exception as exc:
            logger.debug("解析列宽失败: %s", exc, exc_info=True)
            return
        for col, w in enumerate(widths):
            if 0 <= col < table.model().columnCount() and w > 0:
                table.setColumnWidth(col, w)

    def _save_column_widths(self) -> None:
        table = self._table()
        if table is None:
            return
        widths = [table.columnWidth(c) for c in range(table.model().columnCount())]
        try:
            self.settings.setValue(COLUMN_WIDTHS_KEY, widths)
        except Exception as exc:
            logger.warning("保存列宽设置失败: %s", exc, exc_info=True)
            pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        if not self._eta_timer.isActive():
            self._eta_timer.start()

    def hideEvent(self, event):
        self._eta_timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._save_column_widths()
        self._eta_timer.stop()
        # The service is app-level; closing this window must NOT stop it.
        super().closeEvent(event)


from .scheduled_task_editor import ScheduledTaskEditor  # noqa: E402

logger = logging.getLogger(__name__)
