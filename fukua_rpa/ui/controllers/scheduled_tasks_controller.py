# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Thin bridge between the scheduled-task service and the task table view.

This module owns the :class:`QAbstractTableModel` that the main window renders
and a small :class:`ScheduledTasksController` that wires service signals to the
model. It also exposes the pure formatting helpers shared by the window and the
editor so the human-readable text is defined in exactly one place.

The controller never schedules anything itself and never calls the host's run
entry — it only reflects the service's source-of-truth state (design §4.5).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    Qt,
)
from PySide6.QtGui import QColor

from ...scheduled_task_model import (
    CONFLICT_CATCHUP_ONCE,
    CONFLICT_SKIP,
    MISSED_ASK,
    MISSED_CATCHUP_ONCE,
    MISSED_SKIP,
    ScheduledTask,
    ScheduledTrigger,
    ScheduleStatus,
    TriggerKind,
)
from ...scheduled_task_service import ScheduledTaskService
from ..theme import COLOR_STATUS_ERROR

# ---------------------------------------------------------------------------
# Column layout
# ---------------------------------------------------------------------------
COL_NAME = 0
COL_PROFILE = 1
COL_RULE = 2
COL_NEXT = 3
COL_LAST = 4
COL_RESULT = 5
COL_DURATION = 6
COL_ENABLED = 7
COL_LOG = 8

COLUMN_COUNT = 9

HEADERS = [
    "任务名称",
    "方案名称",
    "执行规则",
    "下次执行",
    "上次执行",
    "上次结果",
    "用时",
    "启用",
    "日志",
]

# Columns that always keep a stable width when the window is narrowed.
FIXED_WIDTH_COLUMNS = frozenset({COL_LOG, COL_ENABLED})

ERROR_STATUSES = frozenset(
    {
        ScheduleStatus.PROFILE_MISSING,
        ScheduleStatus.INVALID_CONFIG,
        ScheduleStatus.PLUGIN_OR_CAPABILITY_MISSING,
        ScheduleStatus.LAST_RUN_FAILED,
    }
)


# ---------------------------------------------------------------------------
# Status filter options
# ---------------------------------------------------------------------------
STATUS_FILTER_ALL = "all"
STATUS_FILTER_ENABLED = "enabled"
STATUS_FILTER_DISABLED = "disabled"
STATUS_FILTER_ERROR = "error"

STATUS_FILTER_OPTIONS = [
    (STATUS_FILTER_ALL, "全部"),
    (STATUS_FILTER_ENABLED, "已启用"),
    (STATUS_FILTER_DISABLED, "已停用"),
    (STATUS_FILTER_ERROR, "异常"),
]

SORT_NEXT = "next"
SORT_LAST = "last"
SORT_NAME = "name"

SORT_OPTIONS = [
    (SORT_NEXT, "按下次执行"),
    (SORT_LAST, "按上次执行"),
    (SORT_NAME, "按任务名称"),
]


# ---------------------------------------------------------------------------
# Trigger-kind display names
# ---------------------------------------------------------------------------
TRIGGER_KIND_LABELS = [
    (TriggerKind.SINGLE, "单次执行"),
    (TriggerKind.INTERVAL, "间隔执行"),
    (TriggerKind.DAILY, "每天"),
    (TriggerKind.WEEKLY, "每周"),
    (TriggerKind.MONTHLY, "每月"),
    (TriggerKind.STARTUP, "启动后"),
]

_WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_WEEKDAY_LABELS_SHORT = ["一", "二", "三", "四", "五", "六", "日"]


# ---------------------------------------------------------------------------
# Formatting helpers (shared by window + editor)
# ---------------------------------------------------------------------------
def format_trigger_rule(trigger: ScheduledTrigger) -> str:
    """Human readable Chinese rule text, e.g. '每天 09:00' / '每周一、三 18:30'."""

    kind = trigger.kind
    if kind == TriggerKind.SINGLE:
        if trigger.once_at_local is not None:
            return "单次 " + format_local_datetime(trigger.once_at_local)
        return "单次（未设置时间）"
    if kind == TriggerKind.INTERVAL:
        secs = trigger.interval_seconds or 0
        return f"每隔 {secs} 秒"
    if kind == TriggerKind.DAILY:
        return "每天 " + _format_time(trigger.local_time)
    if kind == TriggerKind.WEEKLY:
        days = _format_weekdays(trigger.weekdays)
        return f"每周{days} {_format_time(trigger.local_time)}"
    if kind == TriggerKind.MONTHLY:
        dom = trigger.day_of_month if trigger.day_of_month else "—"
        return f"每月 {dom} 日 {_format_time(trigger.local_time)}"
    if kind == TriggerKind.STARTUP:
        delay = trigger.startup_delay_seconds or 0.0
        if delay > 0:
            return f"启动后 {delay:g} 秒"
        return "启动后"
    return "未知规则"


def _format_weekdays(weekdays) -> str:
    if not weekdays:
        return "（未选星期）"
    ordered = sorted(int(w) for w in weekdays)
    return "、".join(_WEEKDAY_LABELS_SHORT[w] for w in ordered)


def _format_time(t) -> str:
    if t is None:
        return "--:--"
    return f"{t.hour:02d}:{t.minute:02d}"


def status_reason(status: ScheduleStatus) -> str:
    """Reason shown in the '下次执行' cell when there is no next due time."""

    return {
        ScheduleStatus.DISABLED: "已停用",
        ScheduleStatus.PROFILE_MISSING: "方案缺失",
        ScheduleStatus.INVALID_CONFIG: "配置无效",
        ScheduleStatus.PLUGIN_OR_CAPABILITY_MISSING: "插件/能力缺失",
        ScheduleStatus.LAST_RUN_FAILED: "上次运行失败",
        ScheduleStatus.PENDING_CATCHUP: "等待补跑",
        ScheduleStatus.RUNNING: "运行中",
        ScheduleStatus.READY: "就绪",
        ScheduleStatus.WAITING: "无下次执行",
    }.get(status, "—")


def format_outcome(outcome: str | None) -> str:
    return {
        "success": "成功",
        "finished": "成功",  # FR-SCHED-001: #73 合法完成终局
        "failed": "失败",
        "stopped": "停止",
        "user_stopped": "停止",
        # FR-b01-003: 补齐 ARCHITECTURE #73 的独立 Runner 终局映射，避免
        # crashed/forced_stop/startup_failed 落入默认"未运行"误导用户。
        "crashed": "崩溃",
        "forced_stop": "强制停止",
        "startup_failed": "启动失败",
        "skipped": "跳过",
        "missed": "错过",
        "pending_ask": "等待确认",
        "watchdog_timeout": "运行超时",
        "timeout": "超时",
        "error": "失败",
        "running": "运行中",
    }.get(outcome or "", "未运行")


def format_duration(duration_ms: int | None) -> str:
    if duration_ms is None:
        return "-"
    seconds = duration_ms / 1000.0
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes = int(seconds // 60)
    rem = int(seconds % 60)
    if minutes < 60:
        return f"{minutes} 分 {rem} 秒"
    hours = minutes // 60
    return f"{hours} 时 {minutes % 60} 分 {rem} 秒"


def format_local_datetime(dt) -> str:
    """Format a naive-local or UTC-aware datetime as a local wall-clock string."""

    if dt is None:
        return "-"
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def format_epoch(ts: float) -> str:
    if not ts:
        return "未记录"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_uptime(seconds: int) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days} 天 {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_eta(delta_seconds: float) -> str:
    if delta_seconds is None:
        return "—"
    if delta_seconds < 0:
        return "即将执行"
    seconds = int(delta_seconds)
    if seconds < 60:
        return f"{seconds} 秒后"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {secs} 秒后"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 时 {minutes} 分后"


# ---------------------------------------------------------------------------
# Conflict / missed policy labels
# ---------------------------------------------------------------------------
CONFLICT_LABELS = [
    (CONFLICT_SKIP, "跳过"),
    (CONFLICT_CATCHUP_ONCE, "补跑一次"),
]

MISSED_LABELS = [
    (MISSED_SKIP, "跳过"),
    (MISSED_CATCHUP_ONCE, "补跑一次"),
    (MISSED_ASK, "询问"),
]


# ---------------------------------------------------------------------------
# Table model
# ---------------------------------------------------------------------------
class ScheduledTasksTableModel(QAbstractTableModel):
    """Read-only view over ``service.list_view()`` with client-side filtering.

    The model stores the raw rows from the service and derives a filtered /
    sorted view. It is fully rebuilt only when the service emits
    ``tasks_changed`` (or the window explicitly calls :meth:`refresh`), never
    on a timer; the 1-second timer only patches running-duration cells via
    :meth:`refresh_running_durations` (FR-20260812 文档#8 修订：原 "never on
    a timer" 表述过绝对，行数据本身会在定时器上被增量拉取）。
    """

    def __init__(
        self,
        service: ScheduledTaskService,
        plugin_status: Callable[[str], bool | None] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self._service = service
        self._plugin_status = plugin_status
        self._all_rows: list[dict] = []
        self._rows: list[dict] = []
        self._search = ""
        self._status_filter = STATUS_FILTER_ALL
        self._sort_key = SORT_NEXT
        self._in_set_data = False
        self.refresh()

    # -- filtering / sorting ------------------------------------------------
    def set_search(self, text: str) -> None:
        self._search = (text or "").strip().lower()
        self._rebuild_view()

    def set_status_filter(self, status_filter: str) -> None:
        self._status_filter = status_filter
        self._rebuild_view()

    def set_sort(self, sort_key: str) -> None:
        self._sort_key = sort_key
        self._rebuild_view()

    def refresh(self) -> None:
        if self._in_set_data:
            return
        self._all_rows = self._service.list_view()
        self._rebuild_view()

    def refresh_running_durations(self) -> None:
        """Lightweight refresh: only update duration/result cells for running tasks.

        Avoids a full beginResetModel/endResetModel so the table selection and
        scroll position survive the 1-second timer tick. Only the visible
        ``_rows`` are patched in place; structural changes (add/remove/reorder)
        still flow through ``refresh()`` via the service ``tasks_changed``
        signal.
        """
        if self._in_set_data:
            return
        # FR-20260812-D13-001（性能-2 建议）: 无运行中任务时短路，避免每秒
        # 全量 list_view 的 O(N) 健康计算纯浪费（N 大时持续 CPU 占用）。
        # FR-20260813-D17-003（部分合理，accepted）: list_view 对 N 个任务为 O(N)，
        # 运行态下每 tick 全量重算属已知轻量成本（status/next_due 需实时、无法避免）；
        # 不引入步骤配置缓存以避免陈旧数据回归，故接受。典型 N 为个位数，影响可忽略。
        if not self._service.has_running_tasks:
            return
        new_rows = self._service.list_view()
        # Build a map of schedule_id -> new row for quick lookup.
        new_by_id = {}
        for r in new_rows:
            sid = r["task"].schedule_id
            new_by_id[sid] = r
        # Only update cells that changed (running tasks' status/last_run/result/
        # duration columns).
        for row_idx, old_row in enumerate(self._rows):
            sid = old_row["task"].schedule_id
            new_row = new_by_id.get(sid)
            if new_row is None:
                continue
            old_last = old_row.get("last_run")
            new_last = new_row.get("last_run")
            if old_last != new_last:
                # Update the row data in place (rows are dicts / references).
                self._rows[row_idx]["last_run"] = new_last
                self._rows[row_idx]["status"] = new_row["status"]
                # Emit dataChanged for the affected columns (status-bearing
                # next/last/result/duration range).
                top_left = self.index(row_idx, COL_NEXT)
                bottom_right = self.index(row_idx, COL_DURATION)
                self.dataChanged.emit(top_left, bottom_right)

    def _matches_status(self, row: dict) -> bool:
        task: ScheduledTask = row["task"]
        status: ScheduleStatus = row["status"]
        if self._status_filter == STATUS_FILTER_ENABLED:
            return task.enabled
        if self._status_filter == STATUS_FILTER_DISABLED:
            return not task.enabled
        if self._status_filter == STATUS_FILTER_ERROR:
            return status in ERROR_STATUSES
        return True

    def _matches_search(self, row: dict) -> bool:
        if not self._search:
            return True
        task: ScheduledTask = row["task"]
        haystack = (task.name + " " + task.profile_name_snapshot).lower()
        return self._search in haystack

    def _sort_key_for(self, row: dict):
        task: ScheduledTask = row["task"]
        if self._sort_key == SORT_NAME:
            return (0, task.name.lower())
        if self._sort_key == SORT_LAST:
            last = row.get("last_run")
            ts = last.get("started_at") if last else None
            rank = 1 if ts is None else 0
            # Normalize: use a fixed aware-UTC max for None timestamps so the
            # comparison never mixes naive and aware datetimes.
            sort_ts = datetime.max.replace(tzinfo=UTC) if ts is None else ts
            return (rank, sort_ts)
        # SORT_NEXT
        nxt = row.get("next_due")
        rank = 1 if nxt is None else 0
        sort_ts = datetime.max if nxt is None else nxt
        return (rank, sort_ts)

    def _rebuild_view(self) -> None:
        rows = [
            r
            for r in self._all_rows
            if self._matches_status(r) and self._matches_search(r)
        ]
        rows.sort(key=self._sort_key_for)
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    # -- model interface ----------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else COLUMN_COUNT

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole and 0 <= section < len(HEADERS):
            return HEADERS[section]
        return None

    def row_at(self, index: QModelIndex) -> dict | None:
        if not index.isValid():
            return None
        if 0 <= index.row() < len(self._rows):
            return self._rows[index.row()]
        return None

    def schedule_id_at(self, index: QModelIndex) -> str | None:
        row = self.row_at(index)
        if row is None:
            return None
        return row["task"].schedule_id

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == COL_ENABLED:
            base |= Qt.ItemIsUserCheckable
        return base

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        row = self.row_at(index)
        if row is None:
            return None
        task: ScheduledTask = row["task"]
        status: ScheduleStatus = row["status"]
        col = index.column()

        if role == Qt.DisplayRole:
            if col == COL_NAME:
                return task.name
            if col == COL_PROFILE:
                return task.profile_name_snapshot or "（未指定方案）"
            if col == COL_RULE:
                return format_trigger_rule(task.trigger)
            if col == COL_NEXT:
                nxt = row.get("next_due")
                if nxt is None:
                    return status_reason(status)
                return format_local_datetime(nxt)
            if col == COL_LAST:
                last = row.get("last_run")
                if not last:
                    return "未运行"
                return format_local_datetime(last.get("started_at"))
            if col == COL_RESULT:
                last = row.get("last_run")
                if last:
                    return format_outcome(last.get("outcome"))
                if status == ScheduleStatus.LAST_RUN_FAILED:
                    return "失败"
                return "未运行"
            if col == COL_DURATION:
                last = row.get("last_run")
                if last:
                    return format_duration(last.get("duration_ms"))
                return "-"
            if col == COL_ENABLED:
                return None
            if col == COL_LOG:
                return "日志"
            return None

        if role == Qt.CheckStateRole and col == COL_ENABLED:
            return Qt.Checked if task.enabled else Qt.Unchecked

        if role == Qt.ForegroundRole:
            if col == COL_PROFILE and status in (
                ScheduleStatus.PROFILE_MISSING,
                ScheduleStatus.INVALID_CONFIG,
                ScheduleStatus.PLUGIN_OR_CAPABILITY_MISSING,
            ):
                return QColor(COLOR_STATUS_ERROR)  # profile missing / invalid
            if (
                col == COL_RESULT
                and row.get("last_run")
                and status == ScheduleStatus.LAST_RUN_FAILED
            ):
                return QColor(COLOR_STATUS_ERROR)
            return None

        if role == Qt.TextAlignmentRole:
            if col in (COL_ENABLED, COL_LOG):
                return Qt.AlignCenter
            return None

        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if index.column() == COL_ENABLED and role == Qt.CheckStateRole:
            sid = self.schedule_id_at(index)
            if sid is None:
                return False
            self._in_set_data = True
            try:
                self._service.set_enabled(sid, bool(value == Qt.Checked))
            finally:
                self._in_set_data = False
            # FR-20260812-D10-001: 守卫释放后再触发 refresh()，使行内 status /
            # next_due 与 service 实际状态一致。此前 _in_set_data 吞掉
            # tasks_changed 后仅发单格 dataChanged，勾选后"下次执行"等列保持
            # 切换前旧值。
            self.refresh()
            self.dataChanged.emit(index, index)
            return True
        return False


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------
class ScheduledTasksController:
    """Bridges the table model and the service. Holds no workflow logic.

    The host instantiates this once and hands :meth:`model` to the table view.
    Service change signals refresh the model; the controller never triggers a
    run or mutates scheduling policy beyond delegating to the service API.
    """

    def __init__(
        self,
        service: ScheduledTaskService,
        plugin_status: Callable[[str], bool | None] | None = None,
    ):
        self._service = service
        self._plugin_status = plugin_status
        self._model = ScheduledTasksTableModel(service, plugin_status)
        service.tasks_changed.connect(self._model.refresh)

    @property
    def service(self) -> ScheduledTaskService:
        return self._service

    def model(self) -> ScheduledTasksTableModel:
        return self._model

    def refresh(self) -> None:
        self._model.refresh()
