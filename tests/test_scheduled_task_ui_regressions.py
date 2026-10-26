"""Regression coverage for the scheduled-task browser and log window."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import UTC, datetime
from datetime import time as wall_time
from unittest import mock
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QMessageBox, QStyle, QStyleOptionViewItem

from fukua_rpa.logging_service import SCHEDULED_RUN_LOG_SOURCE, ProcessedLogEvent
from fukua_rpa.scheduled_task_model import (
    ScheduledRunHistory,
    ScheduledTask,
    ScheduledTrigger,
    TriggerKind,
)
from fukua_rpa.scheduled_task_service import ScheduledTaskService
from fukua_rpa.ui.controllers.scheduled_tasks_controller import (
    COL_ENABLED,
    COL_NEXT,
    COLUMN_COUNT,
    HEADERS,
)
from fukua_rpa.ui.scheduled_task_log_window import ScheduledTaskLogWindow
from fukua_rpa.ui.scheduled_tasks_window import (
    ScheduledTasksWindow,
    _CenterCheckboxDelegate,
)


def _task() -> ScheduledTask:
    return ScheduledTask(
        schedule_id="schedule-1",
        name="每天整理报表",
        profile_id="profile-1",
        profile_name_snapshot="报表方案",
        enabled=True,
        trigger=ScheduledTrigger(kind=TriggerKind.DAILY, local_time=wall_time(9, 0)),
    )


def _service(tmp: str) -> ScheduledTaskService:
    execution = MagicMock()
    execution.scheduled_health.return_value = (True, True, True)
    service = ScheduledTaskService(MagicMock(), execution, tmp)
    service.add_task(_task())
    return service


def _render_checkbox(delegate, model, index) -> bytes:
    image = QImage(44, 32, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    option = QStyleOptionViewItem()
    option.rect = image.rect()
    option.state = option.state | QStyle.State_Enabled
    delegate.paint(painter, option, index)
    painter.end()
    return bytes(image.constBits())


class TestScheduledTaskUiRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_operation_column_removed_and_checked_indicator_changes_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            window = ScheduledTasksWindow(
                service,
                lambda: [("profile-1", "报表方案")],
                lambda _profile: None,
                QSettings(os.path.join(tmp, "config.ini"), QSettings.IniFormat),
            )
            model = window._controller.model()
            self.assertEqual(COLUMN_COUNT, 9)
            self.assertEqual(HEADERS[-1], "日志")
            self.assertNotIn("操作", HEADERS)

            delegate = _CenterCheckboxDelegate()
            index = model.index(0, COL_ENABLED)
            checked = _render_checkbox(delegate, model, index)
            self.assertTrue(model.setData(index, Qt.Unchecked, Qt.CheckStateRole))
            unchecked = _render_checkbox(delegate, model, index)
            self.assertNotEqual(checked, unchecked)

            window.resize(1000, 620)
            window.show()
            self.app.processEvents()
            table = window._table()
            self.assertEqual(table.contextMenuPolicy(), Qt.CustomContextMenu)
            window.close()
            self.app.processEvents()

    def test_uptime_tick_and_explicit_refresh_update_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            settings = QSettings(os.path.join(tmp, "config.ini"), QSettings.IniFormat)
            window = ScheduledTasksWindow(
                service,
                lambda: [("profile-1", "报表方案")],
                lambda _profile: None,
                settings,
            )
            service._app_start_ts = time.time() - 65
            window._tick_live_fields()
            self.assertNotEqual(window._summary["uptime"].text(), "00:00:00")
            service._app_start_ts = time.time() - 125
            window._refresh_all()
            window._tick_live_fields()
            self.assertTrue(window._summary["uptime"].text().startswith("00:02"))
            window.close()
            self.app.processEvents()

    def test_toggle_enabled_refreshes_status_and_next_due_columns(self):
        """FR-20260812-D10-001: 勾选"启用"列后，行内状态/下次执行列必须与
        service 实际状态一致，不得保留切换前旧值。"""
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            settings = QSettings(os.path.join(tmp, "config.ini"), QSettings.IniFormat)
            window = ScheduledTasksWindow(
                service,
                lambda: [("profile-1", "报表方案")],
                lambda _profile: None,
                settings,
            )
            model = window._controller.model()
            index = model.index(0, COL_ENABLED)
            # 初始为启用：切换到停用
            self.assertTrue(model.setData(index, Qt.Unchecked, Qt.CheckStateRole))
            self.app.processEvents()
            self.assertFalse(service._store.get("schedule-1").enabled)
            # 勾选停用后，行状态应显示"已停用"而非旧值 WAITING
            row = model._rows[0]
            self.assertEqual(row["task"].enabled, False)
            self.assertEqual(
                row["task"].schedule_id, "schedule-1"
            )
            # D17-002 渲染层断言：停用后「下次执行」列必须显示「已停用」
            # （next_due 为 None 且 status=DISABLED），而非切换前的调度时间。
            # 旧实现列未刷新会残留静止值，该回归此前未被测试捕获。
            next_idx = model.index(0, COL_NEXT)
            self.assertEqual(model.data(next_idx, Qt.DisplayRole), "已停用")
            # 再切回启用
            self.assertTrue(model.setData(index, Qt.Checked, Qt.CheckStateRole))
            self.app.processEvents()
            self.assertTrue(service._store.get("schedule-1").enabled)
            self.assertEqual(model._rows[0]["task"].enabled, True)
            # 切回启用后，渲染层应回到调度时间而非「已停用」。
            self.assertNotEqual(model.data(next_idx, Qt.DisplayRole), "已停用")
            window.close()
            self.app.processEvents()

    def test_run_without_profile_id_shows_notice(self):
        """FR-20260812 建议-体验-4: "立即运行"时目标方案缺失（空 profile_id）
        必须明确提示，不得静默无反应。"""
        from fukua_rpa.scheduled_task_model import ScheduledTask
        from fukua_rpa.ui.scheduled_tasks_mixins.context_menu import (
            ContextMenuMixin,
        )

        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            empty_task = ScheduledTask(
                schedule_id="s-empty",
                name="空方案计划",
                profile_id="",
                profile_name_snapshot="",
                enabled=True,
                trigger=ScheduledTrigger(
                    kind=TriggerKind.DAILY, local_time=wall_time(8, 0)
                ),
            )
            service.add_task(empty_task)
            settings = QSettings(os.path.join(tmp, "config.ini"), QSettings.IniFormat)
            window = ScheduledTasksWindow(
                service,
                lambda: [("profile-1", "报表方案")],
                lambda _profile: None,
                settings,
            )
            self.assertIsInstance(window, ContextMenuMixin)
            with mock.patch.object(QMessageBox, "information") as info, mock.patch.object(
                window, "_on_try_run"
            ) as try_run:
                window._handle_run_choice(empty_task)
                info.assert_called_once()
                try_run.assert_not_called()
                # 有 profile_id 时走 _on_try_run
                window._handle_run_choice(_task())
                info.assert_called_once()  # 不再多弹
                try_run.assert_called_once_with("profile-1")
            window.close()
            self.app.processEvents()

    def test_independent_log_window_shows_persisted_run_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = _service(tmp)
            service._store.add_history(
                ScheduledRunHistory(
                    history_id="history-1",
                    schedule_id="schedule-1",
                    run_id="run-1",
                    profile_id="profile-1",
                    due_at_utc=datetime(2026, 8, 1, 3, 0, tzinfo=UTC),
                    started_at_utc=datetime(2026, 8, 1, 3, 0, tzinfo=UTC),
                    ended_at_utc=datetime(2026, 8, 1, 3, 0, 1, tzinfo=UTC),
                    duration_ms=1000,
                    outcome="success",
                )
            )
            service._log_store.append_processed(
                ProcessedLogEvent(
                    message="已执行第 1 步",
                    timestamp="2026-08-01 11:00:00.000",
                    event_time_ns=1,
                    context={
                        "log_source": SCHEDULED_RUN_LOG_SOURCE,
                        "schedule_id": "schedule-1",
                        "run_id": "run-1",
                    },
                    structured={},
                )
            )
            settings = QSettings(os.path.join(tmp, "config.ini"), QSettings.IniFormat)
            window = ScheduledTaskLogWindow(service, "schedule-1", settings)
            self.assertIsNone(window.parent())
            window.refresh(force=True)
            window._history_table.selectRow(0)
            window._show_selected_log()
            self.assertIn("每天整理报表", window.windowTitle())
            self.assertIn("已执行第 1 步", window._log_text.toPlainText())
            self.assertIn("成功", window._log_text.toPlainText())
            window.close()
            self.app.processEvents()


class FormatOutcomeRunnerFinalsTests(unittest.TestCase):
    """FR-b01-003: 独立 Runner 终局 crashed/forced_stop/startup_failed 必须可区分
    显示，不得落入默认"未运行"误导用户。"""

    def test_runner_finals_mapped_to_distinct_labels(self):
        from fukua_rpa.ui.controllers.scheduled_tasks_controller import (
            format_outcome,
        )

        self.assertEqual(format_outcome("crashed"), "崩溃")
        self.assertEqual(format_outcome("forced_stop"), "强制停止")
        self.assertEqual(format_outcome("startup_failed"), "启动失败")
        # FR-SCHED-001: #73 合法完成终局 "finished" 必须显示为成功。
        self.assertEqual(format_outcome("finished"), "成功")

    def test_existing_outcomes_unchanged(self):
        from fukua_rpa.ui.controllers.scheduled_tasks_controller import (
            format_outcome,
        )

        self.assertEqual(format_outcome("success"), "成功")
        self.assertEqual(format_outcome("finished"), "成功")
        self.assertEqual(format_outcome("missed"), "错过")
        self.assertEqual(format_outcome(None), "未运行")
        self.assertEqual(format_outcome("unknown_future_outcome"), "未运行")

    def test_outcome_group_finished_is_success(self):
        from fukua_rpa.ui.scheduled_task_log_window import _outcome_group

        # FR-SCHED-001: finished 属成功组，日志筛选"成功"可命中。
        self.assertEqual(_outcome_group("finished"), "success")
        self.assertEqual(_outcome_group("success"), "success")
        self.assertEqual(_outcome_group("crashed"), "failed")
