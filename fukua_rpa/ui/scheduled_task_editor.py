# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false, reportOptionalCall=false, reportOptionalIterable=false, reportOptionalOperand=false, reportReturnType=false
"""Scheduled-task create / edit window (design §4.2).

A non-modal, single-instance-friendly floating dialog. It collects the task
fields, validates them, and builds a :class:`ScheduledTask` via the model
constructors, then calls ``service.add_task`` / ``service.update_task``.

The editor NEVER runs a script. "计算未来五次" only previews the next five
trigger times through ``service.future_runs`` (a pure computation). "立即试运行"
is the single host-wired command and simply calls ``on_try_run(profile_id)``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, time

from PySide6.QtCore import QTime
from PySide6.QtWidgets import (
    QCheckBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from ..scheduled_task_model import (
    CONFLICT_SKIP,
    MISSED_SKIP,
    ScheduledTask,
    ScheduledTaskClock,
    ScheduledTrigger,
    TriggerKind,
    new_schedule_id,
)
from .components import (
    CollapsibleSection,
    FloatingSettingsDialog,
    NoWheelComboBox,
    NoWheelSpinBox,
    fit_combo_to_contents,
    fit_text_button,
)
from .controllers.scheduled_tasks_controller import (
    CONFLICT_LABELS,
    MISSED_LABELS,
    TRIGGER_KIND_LABELS,
)

_WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_CLOCK = ScheduledTaskClock()


def _qtime_to_time(qtime) -> datetime.time:
    return time(qtime.hour(), qtime.minute(), qtime.second())


def _local_to_utc(naive_local: datetime) -> datetime:
    return _CLOCK.to_utc(naive_local)


class ScheduledTaskEditor(FloatingSettingsDialog):
    """Create or edit a single scheduled task."""

    def __init__(
        self,
        settings,
        service,
        get_profiles: Callable[[], list[tuple]],
        on_try_run: Callable[[str], None],
        schedule_id: str | None = None,
        plugin_status: Callable[[str], bool | None] | None = None,
        parent=None,
        is_runtime_editing_locked: Callable[[], bool] | None = None,
    ):
        super().__init__(
            settings, "scheduled_task_editor_geometry", "计划任务编辑器", (560, 700)
        )
        self._service = service
        self._get_profiles = get_profiles
        self._on_try_run = on_try_run
        self._plugin_status = plugin_status
        self._schedule_id = schedule_id
        self._existing: ScheduledTask | None = None
        # DESIGN (UIC15): 编辑器可能在运行开始前已打开；运行中保存同样属于改写
        # 计划数据，必须在保存入口二次校验锁状态（窗口打开时的拦截覆盖不了
        # 「先打开、后开始运行」的窗口期）。缺省不锁定，保持旧行为与测试兼容。
        self._is_runtime_editing_locked = is_runtime_editing_locked or (
            lambda: False
        )

        self._profiles: list[tuple[str, str]] = []
        self._profile_id_to_name: dict = {}

        self._build_ui()
        self._load_existing()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        root.addWidget(self._build_basic_section())
        root.addWidget(self._build_trigger_section())
        root.addWidget(self._build_advanced_section())
        root.addStretch(1)
        root.addWidget(self._build_button_row())

    def _build_basic_section(self) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("basicSection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # name
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("任务名称"))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("例如：每天整理报表")
        name_row.addWidget(self._name_edit, 1)
        layout.addLayout(name_row)

        # profile
        profile_row = QHBoxLayout()
        profile_row.addWidget(QLabel("目标方案"))
        self._profile_combo = NoWheelComboBox()
        self._refresh_profiles()
        fit_combo_to_contents(self._profile_combo, minimum_width=200)
        profile_row.addWidget(self._profile_combo, 1)
        layout.addLayout(profile_row)

        # enabled
        self._enabled_chk = QCheckBox("启用此计划")
        self._enabled_chk.setChecked(True)
        layout.addWidget(self._enabled_chk)
        return frame

    def _build_trigger_section(self) -> QWidget:
        frame = QFrame(self)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        kind_row = QHBoxLayout()
        kind_row.addWidget(QLabel("触发类型"))
        self._kind_combo = NoWheelComboBox()
        for value, label in TRIGGER_KIND_LABELS:
            self._kind_combo.addItem(label, value.value)
        fit_combo_to_contents(self._kind_combo, minimum_width=160)
        self._kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        kind_row.addWidget(self._kind_combo, 1)
        layout.addLayout(kind_row)

        self._param_stack = QStackedWidget(self)
        self._param_stack.addWidget(self._build_single_page())   # SINGLE
        self._param_stack.addWidget(self._build_interval_page()) # INTERVAL
        self._param_stack.addWidget(self._build_daily_page())    # DAILY
        self._param_stack.addWidget(self._build_weekly_page())   # WEEKLY
        self._param_stack.addWidget(self._build_monthly_page())  # MONTHLY
        self._param_stack.addWidget(self._build_startup_page())  # STARTUP
        layout.addWidget(self._param_stack)
        return frame

    def _build_single_page(self) -> QWidget:
        w = QWidget(self)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("执行时间"))
        self._single_dt = QDateTimeEdit(self)
        self._single_dt.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self._single_dt.setCalendarPopup(True)
        self._single_dt.setDateTime(self._single_dt.dateTime().currentDateTime().addDays(1))
        layout.addWidget(self._single_dt, 1)
        return w

    def _build_interval_page(self) -> QWidget:
        w = QWidget(self)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("间隔（秒）"))
        self._interval_spin = NoWheelSpinBox(self)
        self._interval_spin.setRange(1, 60 * 60 * 24 * 365)
        self._interval_spin.setValue(3600)
        layout.addWidget(self._interval_spin, 1)
        note = QLabel("保存后以当前时刻为锚点，按固定间隔重复执行。")
        note.setObjectName("hintLabel")
        layout.addWidget(note)
        return w

    def _build_daily_page(self) -> QWidget:
        w = QWidget(self)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("执行时间"))
        self._daily_time = QTimeEdit(self)
        self._daily_time.setTime(QTime(9, 0, 0))
        layout.addWidget(self._daily_time, 1)
        return w

    def _build_weekly_page(self) -> QWidget:
        w = QWidget(self)
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("执行时间"))
        self._weekly_time = QTimeEdit(self)
        self._weekly_time.setTime(QTime(18, 30, 0))
        time_row.addWidget(self._weekly_time, 1)
        layout.addLayout(time_row)

        day_row = QHBoxLayout()
        day_row.addWidget(QLabel("星期"))
        self._weekday_checks = []
        for i, name in enumerate(_WEEKDAY_NAMES):
            chk = QCheckBox(name)
            chk.setProperty("weekday", i)
            if i == 0:
                chk.setChecked(True)
            self._weekday_checks.append(chk)
            day_row.addWidget(chk)
        day_row.addStretch(1)
        layout.addLayout(day_row)
        return w

    def _build_monthly_page(self) -> QWidget:
        w = QWidget(self)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("每月"))
        self._monthly_day = NoWheelSpinBox(self)
        self._monthly_day.setRange(1, 31)
        self._monthly_day.setValue(1)
        layout.addWidget(self._monthly_day)
        layout.addWidget(QLabel("日  时间"))
        self._monthly_time = QTimeEdit(self)
        self._monthly_time.setTime(QTime(9, 0, 0))
        layout.addWidget(self._monthly_time, 1)
        return w

    def _build_startup_page(self) -> QWidget:
        w = QWidget(self)
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("启动后延迟（秒）"))
        self._startup_delay = QDoubleSpinBox(self)
        self._startup_delay.setRange(0.0, 3600.0)
        self._startup_delay.setSingleStep(1.0)
        self._startup_delay.setValue(0.0)
        layout.addWidget(self._startup_delay, 1)
        return w

    def _build_advanced_section(self) -> CollapsibleSection:
        section = CollapsibleSection("高级设置", self, expanded=False)
        inner = QVBoxLayout()
        inner.setSpacing(8)

        # active_from
        from_row = QHBoxLayout()
        self._active_from_chk = QCheckBox("启用生效时间")
        self._active_from_chk.toggled.connect(self._on_active_from_toggled)
        from_row.addWidget(self._active_from_chk)
        self._active_from_dt = QDateTimeEdit(self)
        self._active_from_dt.setCalendarPopup(True)
        self._active_from_dt.setEnabled(False)
        from_row.addWidget(self._active_from_dt, 1)
        inner.addLayout(from_row)

        # active_until
        until_row = QHBoxLayout()
        self._active_until_chk = QCheckBox("启用失效时间")
        self._active_until_chk.toggled.connect(self._on_active_until_toggled)
        until_row.addWidget(self._active_until_chk)
        self._active_until_dt = QDateTimeEdit(self)
        self._active_until_dt.setCalendarPopup(True)
        self._active_until_dt.setEnabled(False)
        until_row.addWidget(self._active_until_dt, 1)
        inner.addLayout(until_row)

        # conflict policy
        conflict_row = QHBoxLayout()
        conflict_row.addWidget(QLabel("运行时冲突"))
        self._conflict_combo = NoWheelComboBox()
        for value, label in CONFLICT_LABELS:
            self._conflict_combo.addItem(label, value)
        fit_combo_to_contents(self._conflict_combo)
        conflict_row.addWidget(self._conflict_combo)
        inner.addLayout(conflict_row)

        # missed policy
        missed_row = QHBoxLayout()
        missed_row.addWidget(QLabel("错过处理"))
        self._missed_combo = NoWheelComboBox()
        for value, label in MISSED_LABELS:
            self._missed_combo.addItem(label, value)
        fit_combo_to_contents(self._missed_combo)
        missed_row.addWidget(self._missed_combo)
        inner.addLayout(missed_row)

        # max runtime
        runtime_row = QHBoxLayout()
        runtime_row.addWidget(QLabel("单次运行上限（秒，0=不限制）"))
        self._max_runtime = NoWheelSpinBox(self)
        self._max_runtime.setRange(0, 24 * 3600)
        self._max_runtime.setValue(0)
        runtime_row.addWidget(self._max_runtime)
        inner.addLayout(runtime_row)

        section.set_content_layout(inner)
        return section

    def _build_button_row(self) -> QWidget:
        bar = QWidget(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._btn_future = fit_text_button(QPushButton("计算未来五次"))
        self._btn_try = fit_text_button(QPushButton("立即试运行"))
        self._btn_save = fit_text_button(QPushButton("保存"))
        self._btn_cancel = fit_text_button(QPushButton("取消"))

        self._btn_future.clicked.connect(self._on_calc_future)
        self._btn_try.clicked.connect(self._on_try_run_clicked)
        self._btn_save.clicked.connect(self._on_save)
        self._btn_cancel.clicked.connect(self.close)

        layout.addWidget(self._btn_future)
        layout.addWidget(self._btn_try)
        layout.addStretch(1)
        layout.addWidget(self._btn_save)
        layout.addWidget(self._btn_cancel)
        return bar

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------
    def _refresh_profiles(self) -> None:
        self._profiles = list(self._get_profiles() or [])
        self._profile_id_to_name = dict(self._profiles)
        self._profile_combo.clear()
        self._profile_combo.addItem("（未选择方案）", None)
        for pid, name in self._profiles:
            self._profile_combo.addItem(name, pid)

    def _selected_profile(self) -> tuple[str | None, str]:
        pid = self._profile_combo.currentData()
        if pid is None:
            return None, ""
        return pid, self._profile_id_to_name.get(pid, "")

    # ------------------------------------------------------------------
    # Trigger-kind switching
    # ------------------------------------------------------------------
    def _on_kind_changed(self, _index: int) -> None:
        kind = TriggerKind(self._kind_combo.currentData())
        page = {
            TriggerKind.SINGLE: 0,
            TriggerKind.INTERVAL: 1,
            TriggerKind.DAILY: 2,
            TriggerKind.WEEKLY: 3,
            TriggerKind.MONTHLY: 4,
            TriggerKind.STARTUP: 5,
        }.get(kind, 0)
        self._param_stack.setCurrentIndex(page)

    def _on_active_from_toggled(self, checked: bool) -> None:
        self._active_from_dt.setEnabled(checked)

    def _on_active_until_toggled(self, checked: bool) -> None:
        self._active_until_dt.setEnabled(checked)

    # ------------------------------------------------------------------
    # Load existing
    # ------------------------------------------------------------------
    def _load_existing(self) -> None:
        if not self._schedule_id:
            return
        data = self._service.export_task(self._schedule_id)
        if not data:
            return
        self._existing = ScheduledTask.from_dict(data)
        task = self._existing

        self._name_edit.setText(task.name)
        self._enabled_chk.setChecked(task.enabled)
        idx = self._profile_combo.findData(task.profile_id)
        if idx >= 0:
            self._profile_combo.setCurrentIndex(idx)
        else:
            self._profile_combo.setCurrentIndex(0)

        self._kind_combo.setCurrentIndex(self._kind_combo.findData(task.trigger.kind.value))
        self._on_kind_changed(0)
        self._populate_trigger(task.trigger)

        self._active_from_chk.setChecked(task.active_from is not None)
        self._active_until_chk.setChecked(task.active_until is not None)
        if task.active_from is not None:
            self._active_from_dt.setDateTime(_utc_to_qdatetime(task.active_from))
        if task.active_until is not None:
            self._active_until_dt.setDateTime(_utc_to_qdatetime(task.active_until))

        self._conflict_combo.setCurrentIndex(self._conflict_combo.findData(task.conflict_policy))
        self._missed_combo.setCurrentIndex(self._missed_combo.findData(task.missed_policy))
        self._max_runtime.setValue(int(task.max_runtime_seconds or 0))

    def _populate_trigger(self, trigger: ScheduledTrigger) -> None:
        kind = trigger.kind
        if kind == TriggerKind.SINGLE:
            if trigger.once_at_local is not None:
                self._single_dt.setDateTime(_naive_to_qdatetime(trigger.once_at_local))
        elif kind == TriggerKind.INTERVAL:
            self._interval_spin.setValue(int(trigger.interval_seconds or 3600))
        elif kind == TriggerKind.DAILY:
            if trigger.local_time is not None:
                self._daily_time.setTime(_time_to_qtime(trigger.local_time))
        elif kind == TriggerKind.WEEKLY:
            if trigger.local_time is not None:
                self._weekly_time.setTime(_time_to_qtime(trigger.local_time))
            selected = set(trigger.weekdays)
            for chk in self._weekday_checks:
                chk.setChecked(chk.property("weekday") in selected)
        elif kind == TriggerKind.MONTHLY:
            self._monthly_day.setValue(int(trigger.day_of_month or 1))
            if trigger.local_time is not None:
                self._monthly_time.setTime(_time_to_qtime(trigger.local_time))
        elif kind == TriggerKind.STARTUP:
            self._startup_delay.setValue(float(trigger.startup_delay_seconds or 0.0))

    # ------------------------------------------------------------------
    # Build trigger / task from UI
    # ------------------------------------------------------------------
    def _build_trigger(self) -> ScheduledTrigger:
        kind: TriggerKind = TriggerKind(self._kind_combo.currentData())
        if kind == TriggerKind.SINGLE:
            return ScheduledTrigger(
                kind=kind,
                once_at_local=_strip_tz(self._single_dt.dateTime().toPython()),
            )
        if kind == TriggerKind.INTERVAL:
            return ScheduledTrigger(
                kind=kind,
                interval_seconds=self._interval_spin.value(),
                anchor_at_utc=datetime.now(UTC),
            )
        if kind == TriggerKind.DAILY:
            return ScheduledTrigger(
                kind=kind,
                local_time=_qtime_to_time(self._daily_time.time()),
            )
        if kind == TriggerKind.WEEKLY:
            weekdays = frozenset(
                chk.property("weekday")
                for chk in self._weekday_checks
                if chk.isChecked()
            )
            return ScheduledTrigger(
                kind=kind,
                local_time=_qtime_to_time(self._weekly_time.time()),
                weekdays=weekdays,
            )
        if kind == TriggerKind.MONTHLY:
            return ScheduledTrigger(
                kind=kind,
                local_time=_qtime_to_time(self._monthly_time.time()),
                day_of_month=self._monthly_day.value(),
            )
        # STARTUP
        return ScheduledTrigger(
            kind=kind,
            startup_delay_seconds=self._startup_delay.value(),
        )

    def _build_task(self) -> ScheduledTask:
        profile_id, display_name = self._selected_profile()
        name = self._name_edit.text().strip()
        trigger = self._build_trigger()
        now = datetime.now(UTC)

        active_from = (
            _local_to_utc(_strip_tz(self._active_from_dt.dateTime().toPython()))
            if self._active_from_chk.isChecked()
            else None
        )
        active_until = (
            _local_to_utc(_strip_tz(self._active_until_dt.dateTime().toPython()))
            if self._active_until_chk.isChecked()
            else None
        )
        conflict = self._conflict_combo.currentData() or CONFLICT_SKIP
        missed = self._missed_combo.currentData() or MISSED_SKIP
        max_rt = self._max_runtime.value() or None

        if self._existing is not None:
            task = self._existing
            task.name = name
            task.profile_id = profile_id
            task.profile_name_snapshot = display_name
            task.enabled = self._enabled_chk.isChecked()
            task.trigger = trigger
            task.active_from = active_from
            task.active_until = active_until
            task.conflict_policy = conflict
            task.missed_policy = missed
            task.max_runtime_seconds = max_rt
            task.updated_at_utc = now
            return task

        return ScheduledTask(
            schedule_id=new_schedule_id(),
            name=name,
            profile_id=profile_id,
            profile_name_snapshot=display_name,
            enabled=self._enabled_chk.isChecked(),
            trigger=trigger,
            active_from=active_from,
            active_until=active_until,
            conflict_policy=conflict,
            missed_policy=missed,
            max_runtime_seconds=max_rt,
            created_at_utc=now,
            updated_at_utc=now,
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _on_calc_future(self) -> None:
        trigger = self._build_trigger()
        temp = ScheduledTask(
            schedule_id="preview",
            name=self._name_edit.text().strip() or "预览",
            profile_id=None,
            profile_name_snapshot="",
            enabled=True,
            trigger=trigger,
        )
        runs = self._service.future_runs(temp, count=5)
        if not runs:
            QMessageBox.information(
                self, "未来五次", "未来无可计算的触发时间，请检查触发规则设置。"
            )
            return
        text = "未来五次预计触发时间：\n\n" + "\n".join(
            f"{i + 1}. {r.strftime('%Y-%m-%d %H:%M:%S')}" for i, r in enumerate(runs)
        )
        QMessageBox.information(self, "未来五次", text)

    def _on_try_run_clicked(self) -> None:
        profile_id, _name = self._selected_profile()
        if not profile_id:
            QMessageBox.information(self, "提示", "请先选择一个目标方案再试运行。")
            return
        self._on_try_run(profile_id)

    def _on_save(self) -> None:
        if self._is_runtime_editing_locked():
            QMessageBox.warning(
                self,
                "无法保存",
                "方案正在运行中，不能修改计划任务。请先停止当前运行。",
            )
            return
        name = self._name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "无法保存", "请填写任务名称。")
            return
        profile_id, _name = self._selected_profile()
        if not profile_id:
            QMessageBox.warning(self, "无法保存", "请选择一个目标方案。")
            return
        kind: TriggerKind = TriggerKind(self._kind_combo.currentData())
        if kind == TriggerKind.WEEKLY and not any(
            c.isChecked() for c in self._weekday_checks
        ):
            QMessageBox.warning(self, "无法保存", "请至少选择一个星期。")
            return
        if (
            kind == TriggerKind.SINGLE
            and self._enabled_chk.isChecked()
            and self._single_dt.dateTime().toPython() < datetime.now()
        ):
            QMessageBox.warning(self, "无法保存", "单次执行时间已过去，请设置一个未来的时间。")
            return
        if (
            self._active_from_chk.isChecked()
            and self._active_until_chk.isChecked()
            and self._active_from_dt.dateTime() > self._active_until_dt.dateTime()
        ):
            QMessageBox.warning(self, "无法保存", "生效时间不能晚于失效时间。")
            return

        task = self._build_task()
        try:
            if self._existing is not None:
                self._service.update_task(task)
            else:
                self._service.add_task(task)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", f"写入计划失败：{exc}")
            return
        self.close()


# ---------------------------------------------------------------------------
# QDateTime <-> datetime helpers
# ---------------------------------------------------------------------------
def _naive_to_qdatetime(dt: datetime):
    from PySide6.QtCore import QDateTime

    return QDateTime.fromString(dt.strftime("%Y-%m-%dT%H:%M:%S"), "yyyy-MM-ddThh:mm:ss")


def _utc_to_qdatetime(dt: datetime):
    from PySide6.QtCore import QDateTime

    local = dt.astimezone()
    return QDateTime.fromString(local.strftime("%Y-%m-%dT%H:%M:%S"), "yyyy-MM-ddThh:mm:ss")


def _time_to_qtime(t: datetime.time):
    from PySide6.QtCore import QTime

    return QTime(t.hour, t.minute, t.second)


def _strip_tz(dt: datetime) -> datetime:
    """QDateTime.toPython() may yield a tz-aware datetime; the model works with
    naive local wall-clock time, so always strip any tzinfo before storing."""

    return dt.replace(tzinfo=None) if dt is not None else None
