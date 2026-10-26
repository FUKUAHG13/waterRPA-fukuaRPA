"""Independent, live-updating log browser for one scheduled task."""

from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
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
    format_duration,
    format_local_datetime,
    format_outcome,
)

_FILTER_OPTIONS = (
    ("all", "全部结果"),
    ("running", "运行中"),
    ("success", "成功"),
    ("stopped", "已停止"),
    # FR-20261006-012 + ADV-20261006-006: 「待补跑」是独立的计划调度状态，
    # 既不是失败也不是跳过/错过。它表示"已记录触发、排队等待补跑"，与用户
    # 主动跳过的语义不同，混在失败组里会让计划看起来在出错。
    ("pending", "待补跑"),
    ("skipped", "跳过/错过"),
    ("failed", "失败"),
)


def _outcome_group(outcome: str) -> str:
    value = str(outcome or "").strip().lower()
    if value == "running":
        return "running"
    if value == "success":
        return "success"
    if value == "finished":  # FR-SCHED-001: #73 合法完成终局
        return "success"
    if value in {"stopped", "user_stopped"}:
        return "stopped"
    if value == "pending_catchup":  # FR-20261006-012: 曾落入末尾 "failed"
        return "pending"
    if value in {"skipped", "pending_ask", "missed"}:
        return "skipped"
    return "failed"


class ScheduledTaskLogWindow(FloatingSettingsDialog):
    """Show persisted run summaries and bounded detailed logs side by side."""

    def __init__(self, service, schedule_id: str, settings):
        self._service = service
        self._schedule_id = str(schedule_id)
        self._refresh_signature = None
        self._display_signature = None
        self._history_by_key: dict[str, dict] = {}
        key = f"scheduled_task_log_geometry/{self._schedule_id}"
        super().__init__(settings, key, "计划日志", (980, 640))
        self.setMinimumSize(780, 520)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._build_ui()
        self.refresh(force=True)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        toolbar = QWidget(self)
        row = QHBoxLayout(toolbar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self._title_label = QLabel("计划日志")
        self._title_label.setProperty("role", "title")
        self._filter_combo = NoWheelComboBox()
        for value, label in _FILTER_OPTIONS:
            self._filter_combo.addItem(label, value)
        fit_combo_to_contents(self._filter_combo)
        self._filter_combo.currentIndexChanged.connect(
            lambda _index: self.refresh(force=True)
        )
        refresh_btn = fit_text_button(QPushButton("刷新"))
        refresh_btn.clicked.connect(lambda: self.refresh(force=True))
        copy_btn = fit_text_button(QPushButton("复制日志"))
        copy_btn.clicked.connect(self._copy_current_log)
        row.addWidget(self._title_label)
        row.addStretch(1)
        row.addWidget(self._filter_combo)
        row.addWidget(refresh_btn)
        row.addWidget(copy_btn)
        root.addWidget(toolbar)

        splitter = QSplitter(Qt.Horizontal, self)
        self._history_table = QTableWidget(0, 3, splitter)
        self._history_table.setHorizontalHeaderLabels(["开始时间", "结果", "用时"])
        self._history_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._history_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._history_table.verticalHeader().setVisible(False)
        self._history_table.horizontalHeader().setStretchLastSection(True)
        self._history_table.setColumnWidth(0, 180)
        self._history_table.setColumnWidth(1, 90)
        self._history_table.itemSelectionChanged.connect(self._show_selected_log)

        self._log_text = QPlainTextEdit(splitter)
        self._log_text.setReadOnly(True)
        self._log_text.setPlaceholderText("所选运行暂时没有详细日志。")
        self._log_text.document().setMaximumBlockCount(6000)
        splitter.addWidget(self._history_table)
        splitter.addWidget(self._log_text)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([360, 600])
        root.addWidget(splitter, 1)

        footer = QLabel(
            "计划日志保存在本机并受日志隐私设置约束；达到容量上限后自动淘汰最早内容。"
        )
        footer.setProperty("role", "secondary")
        footer.setWordWrap(True)
        root.addWidget(footer)

    def refresh(self, *, force: bool = False) -> None:
        if force:
            self._display_signature = None
        task = self._service.task_by_id(self._schedule_id)
        task_name = task.name if task is not None else "已删除的计划"
        self.setWindowTitle(f"计划日志 - {task_name}")
        self._title_label.setText(task_name)

        rows: list[dict] = []
        running = self._service.running_info_for(self._schedule_id)
        if running:
            started = running.get("started_at")
            elapsed = None
            if isinstance(started, datetime):
                elapsed = max(
                    0,
                    int((datetime.now(UTC) - started).total_seconds() * 1000),
                )
            rows.append(
                {
                    "key": f"running:{running.get('run_id', '')}",
                    "run_id": str(running.get("run_id") or ""),
                    "started_at": started,
                    "duration_ms": elapsed,
                    "outcome": "running",
                    "reason": "",
                    "history": None,
                }
            )
        for item in reversed(self._service.history_for(self._schedule_id)):
            rows.append(
                {
                    "key": f"history:{item.history_id}",
                    "run_id": str(item.run_id or ""),
                    "started_at": item.started_at_utc or item.due_at_utc,
                    "duration_ms": item.duration_ms,
                    "outcome": item.outcome,
                    "reason": item.reason,
                    "history": item,
                }
            )

        selected_filter = str(self._filter_combo.currentData() or "all")
        if selected_filter != "all":
            rows = [
                row
                for row in rows
                if _outcome_group(row["outcome"]) == selected_filter
            ]
        signature = tuple(
            (
                row["key"],
                row["outcome"],
                row["duration_ms"],
            )
            for row in rows
        )
        current_key = self._selected_key()
        if force or signature != self._refresh_signature:
            self._refresh_signature = signature
            self._history_by_key = {row["key"]: row for row in rows}
            self._history_table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                started_item = QTableWidgetItem(format_local_datetime(row["started_at"]))
                started_item.setData(Qt.UserRole, row["key"])
                self._history_table.setItem(row_index, 0, started_item)
                self._history_table.setItem(
                    row_index,
                    1,
                    QTableWidgetItem(format_outcome(row["outcome"])),
                )
                self._history_table.setItem(
                    row_index,
                    2,
                    QTableWidgetItem(format_duration(row["duration_ms"])),
                )
            target_row = 0
            if current_key:
                for row_index in range(self._history_table.rowCount()):
                    item = self._history_table.item(row_index, 0)
                    if item is not None and item.data(Qt.UserRole) == current_key:
                        target_row = row_index
                        break
            if rows:
                self._history_table.selectRow(target_row)
            else:
                self._log_text.setPlainText("当前筛选条件下没有运行记录。")
        self._show_selected_log()

    def _selected_key(self) -> str:
        row = self._history_table.currentRow()
        item = self._history_table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.UserRole) or "") if item is not None else ""

    def _show_selected_log(self) -> None:
        selected_key = self._selected_key()
        row = self._history_by_key.get(selected_key)
        if row is None:
            return
        display_signature = (
            selected_key,
            row.get("outcome"),
            row.get("duration_ms"),
            self._service.log_revision(),
        )
        if display_signature == self._display_signature:
            return
        self._display_signature = display_signature
        run_id = row["run_id"]
        entries = (
            self._service.log_entries_for(self._schedule_id, run_id)
            if run_id
            else []
        )
        lines = [
            f"运行 ID：{run_id or '未实际启动'}",
            f"开始时间：{format_local_datetime(row['started_at'])}",
            f"结果：{format_outcome(row['outcome'])}",
            f"用时：{format_duration(row['duration_ms'])}",
        ]
        if row.get("reason"):
            lines.append(f"原因：{row['reason']}")
        lines.append("")
        if entries:
            for entry in entries:
                message = str(entry.get("message") or "")
                timestamp = str(entry.get("timestamp") or "")
                lines.append(f"[{timestamp}] {message}" if timestamp else message)
        else:
            lines.append("该次运行没有可显示的详细日志。")
        text = "\n".join(lines)
        if self._log_text.toPlainText() != text:
            scrollbar = self._log_text.verticalScrollBar()
            follow_tail = scrollbar.value() >= scrollbar.maximum() - 2
            self._log_text.setPlainText(text)
            if follow_tail:
                scrollbar.setValue(scrollbar.maximum())

    def _copy_current_log(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._log_text.toPlainText())

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh(force=True)
        if not self._timer.isActive():
            self._timer.start()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._timer.stop()
        super().closeEvent(event)
