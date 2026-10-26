# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.

import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_OFFSCREEN = os.environ.get("QT_QPA_PLATFORM") == "offscreen"

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QSize, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from fukua_rpa.ui.components import ResponsiveRow
from tests.qt_test_support import (
    QtCallbackGuardMixin,
    create_test_window,
    dispose_widget,
)


class UiPerformanceTests(QtCallbackGuardMixin, unittest.TestCase):
    """Protect resize throttling and long-list virtualization contracts."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def create_window(self, base_dir):
        return create_test_window(self, base_dir)

    def populate_virtual_tasks(self, window, count=150):
        window.task_list.clear()
        for index in range(count):
            window.add_row(
                {"type": 1.0, "value": f"{index},{index}"},
                record_undo=False,
                select=False,
                materialize=False,
                update_indexes=False,
            )
        window.update_indexes()
        window.show()
        self.app.processEvents()
        window.task_list.schedule_virtual_refresh(immediate=True)
        self.app.processEvents()

    def test_long_list_materializes_only_visible_rows_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.populate_virtual_tasks(window)

            self.assertEqual(window.task_list.count(), 150)
            self.assertLess(window.task_list.materialized_row_count(), 40)
            snapshot = window.snapshot_tasks()
            self.assertEqual(len(snapshot), 150)
            self.assertEqual(snapshot[0]["value"], "0,0")
            self.assertEqual(snapshot[-1]["value"], "149,149")

    def test_scrolling_recycles_rows_without_losing_offscreen_steps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.populate_virtual_tasks(window)
            last = window.task_list.item(149)
            window.task_list.setCurrentItem(last)
            window.task_list.scrollToItem(last)
            self.app.processEvents()
            window.task_list.schedule_virtual_refresh(immediate=True)
            self.app.processEvents()

            self.assertIsNone(window.task_list.itemWidget(window.task_list.item(0)))
            self.assertIsNotNone(window.task_list.itemWidget(last))
            snapshot = window.snapshot_tasks()
            self.assertEqual(snapshot[0]["value"], "0,0")
            self.assertEqual(snapshot[-1]["value"], "149,149")

    def test_open_config_dialog_row_is_not_recycled_when_scrolled_away(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.populate_virtual_tasks(window)
            task_list = window.task_list

            protected = window.ensure_task_item_widget(task_list.item(0))
            # Materialize a neighbour so recycling has something to reclaim.
            window.ensure_task_item_widget(task_list.item(1))
            dialog = QDialog(window)
            dialog.show()
            self.app.processEvents()
            protected.config_dialog = dialog
            self.addCleanup(dialog.close)
            self.assertTrue(dialog.isVisible())

            last = task_list.item(149)
            task_list.setCurrentItem(last)
            task_list.scrollToItem(last)
            self.app.processEvents()
            task_list.schedule_virtual_refresh(immediate=True)
            self.app.processEvents()

            # The row with an open dialog survives; the unprotected row recycles.
            self.assertIsNotNone(task_list.itemWidget(task_list.item(0)))
            self.assertIsNone(task_list.itemWidget(task_list.item(1)))

    def test_locating_an_unmaterialized_step_materializes_and_reveals_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.populate_virtual_tasks(window)
            task_list = window.task_list

            target = task_list.item(100)
            self.assertIsNone(task_list.itemWidget(target))

            # Locating a step (e.g. jumping to a debug breakpoint) makes it the
            # current row and scrolls it into view; virtualization must then
            # materialize it even though it lies far outside the old viewport.
            task_list.setCurrentItem(target)
            task_list.scrollToItem(target)
            self.app.processEvents()
            task_list.schedule_virtual_refresh(immediate=True)
            self.app.processEvents()

            self.assertIsNotNone(task_list.itemWidget(target))
            self.assertEqual(task_list.currentRow(), 100)

    @unittest.skipIf(
        _OFFSCREEN,
        "输入焦点保护需要真实窗口激活，offscreen 下无法稳妥建立焦点",
    )
    def test_focused_row_is_not_recycled_and_keeps_focus_when_scrolled_away(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.populate_virtual_tasks(window)
            task_list = window.task_list

            widget = window.ensure_task_item_widget(task_list.item(0))
            window.activateWindow()
            focus_target = widget.findChild(QLineEdit) or widget
            focus_target.setFocus(Qt.OtherFocusReason)
            self.app.processEvents()
            focus = QApplication.focusWidget()
            if focus is None or not (
                focus is widget or widget.isAncestorOf(focus)
            ):
                self.skipTest("当前环境无法把输入焦点设置到步骤行")

            last = task_list.item(149)
            task_list.setCurrentItem(last)
            task_list.scrollToItem(last)
            self.app.processEvents()
            task_list.schedule_virtual_refresh(immediate=True)
            self.app.processEvents()

            self.assertIsNotNone(task_list.itemWidget(task_list.item(0)))
            focus_after = QApplication.focusWidget()
            self.assertTrue(
                focus_after is widget or widget.isAncestorOf(focus_after)
            )

    def test_recorded_task_batch_uses_one_index_refresh_and_one_undo_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.task_list.clear()
            window.undo_stack.clear()
            tasks = [
                {"type": 1.0, "value": f"{index},{index}"}
                for index in range(150)
            ]

            with mock.patch.object(
                window, "update_indexes", wraps=window.update_indexes
            ) as update_indexes:
                inserted = window.add_rows(tasks)

            self.assertEqual(inserted, 150)
            self.assertEqual(update_indexes.call_count, 1)
            self.assertEqual(len(window.undo_stack), 1)
            self.assertLess(window.task_list.materialized_row_count(), 40)
            self.assertEqual(window.snapshot_tasks()[-1]["value"], "149,149")

    def test_reordering_offscreen_data_updates_numbers_without_materializing_all(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.populate_virtual_tasks(window)
            moved = window.task_list.takeItem(120)
            window.task_list.insertItem(0, moved)
            window.update_indexes()
            window.task_list.schedule_virtual_refresh(immediate=True)

            snapshot = window.snapshot_tasks()
            self.assertEqual(snapshot[0]["value"], "120,120")
            self.assertLess(window.task_list.materialized_row_count(), 40)
            self.assertTrue(
                str(window.task_list.item(0).data(Qt.UserRole + 1)).startswith("1.")
            )

    def test_reorder_preprocessing_closes_config_dialog_and_detaches_orphan(self):
        # FR-20260809-1401: 改序路径（拖拽提交 / 上移下移）在 takeItem 前调用
        # close_and_detach_row_widget。回归目标：打开配置弹窗的行被改序时，
        # 弹窗必须关闭、旧行控件必须从 item 分离，避免 NonModal 弹窗绑定到
        # 孤儿行（编辑写入不可见行，见 FR-20260809-1101）。
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.populate_virtual_tasks(window)
            task_list = window.task_list

            protected = window.ensure_task_item_widget(task_list.item(0))
            window.ensure_task_item_widget(task_list.item(1))
            dialog = QDialog(window)
            dialog.show()
            self.app.processEvents()
            protected.config_dialog = dialog
            self.addCleanup(dialog.close)
            self.assertTrue(dialog.isVisible())

            # 模拟改序路径预处理：takeItem 前关闭弹窗并分离旧行控件。
            task_list.close_and_detach_row_widget(0)
            moved = task_list.takeItem(0)
            task_list.insertItem(120, moved)
            task_list.schedule_virtual_refresh(immediate=True)
            self.app.processEvents()

            # 弹窗已关闭；被移动行的旧控件已分离（不会以孤儿形态残留）。
            # 注意：改序后 item(0) 是原位置 1 的行（其 widget 物化过，应保留）；
            # 断言聚焦于被移动的 item（moved）不再携带旧控件。
            self.assertFalse(dialog.isVisible())
            self.assertIsNone(task_list.itemWidget(moved))

            # 分离的旧行控件经 deleteLater 被销毁（处理 DeferredDelete 后 C++ 失效）。
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()
            self.assertFalse(isValid(protected))

            # materialized 计数器未因 removeItemWidget 而高估（与全扫描一致）。
            self.assertEqual(
                task_list.materialized_row_count(),
                sum(
                    1
                    for row in range(task_list.count())
                    if task_list.itemWidget(task_list.item(row)) is not None
                ),
            )

    def test_move_selected_task_closes_config_dialog_before_take_item(self):
        # FR-20260809-1401: 真实改序路径（move_selected_task 上移/下移）必须在
        # takeItem 前调用 close_and_detach_row_widget（接线回归：若调用点被误删，
        # 此测试失败）。
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.populate_virtual_tasks(window)
            task_list = window.task_list

            protected = window.ensure_task_item_widget(task_list.item(1))
            dialog = QDialog(window)
            dialog.show()
            self.app.processEvents()
            protected.config_dialog = dialog
            self.addCleanup(dialog.close)
            task_list.setCurrentItem(task_list.item(1))

            with mock.patch.object(
                task_list,
                "close_and_detach_row_widget",
                wraps=task_list.close_and_detach_row_widget,
            ) as detach_spy:
                self.assertTrue(window.move_selected_task(-1))
            # 上移路径在 takeItem 前对当前行调用了辅助方法；弹窗已关闭。
            self.assertEqual(detach_spy.call_count, 1)
            self.assertEqual(detach_spy.call_args.args[0], 1)
            self.assertFalse(dialog.isVisible())
            # 旧行控件已被分离并销毁（不再作为孤儿残留）。注意：移动后虚拟刷新
            # 会为新位置物化新行控件，因此不断言 itemWidget(item(0)) 为 None，
            # 而是断言原控件对象已失效。
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()
            self.assertFalse(isValid(protected))

    def test_responsive_height_cache_records_hit_after_one_computation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            row = window.settings_dialog.findChildren(ResponsiveRow)[0]
            row.flow_layout.invalidate()
            window.ui_performance.reset()
            width = max(320, row.width())
            first = row.heightForWidth(width)
            second = row.heightForWidth(width)
            counters = window.ui_performance.snapshot()["counters"]

            self.assertEqual(first, second)
            self.assertEqual(counters["ui.layout.height_cache_miss"], 1)
            self.assertGreaterEqual(counters["ui.layout.height_cache_hit"], 1)

    def test_live_resize_pauses_only_nonessential_timers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertTrue(window.cpu_timer.isActive())
            self.assertTrue(window.profiles_autosave_timer.isActive())
            hotkey_timer_active = window.hotkey_timer.isActive()

            window.begin_live_window_interaction("resize")
            self.assertFalse(window.cpu_timer.isActive())
            self.assertFalse(window.profiles_autosave_timer.isActive())
            self.assertEqual(window.hotkey_timer.isActive(), hotkey_timer_active)

            window.end_live_window_interaction()
            self.assertTrue(window.cpu_timer.isActive())
            self.assertTrue(window.profiles_autosave_timer.isActive())

    def test_clean_autosave_does_not_capture_or_serialize_the_ui(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            self.assertTrue(window.persist_profiles_state(force=True))
            self.assertFalse(window.profile_has_unsaved_changes())

            with mock.patch.object(
                window,
                "get_current_ui_config",
                wraps=window.get_current_ui_config,
            ) as capture:
                window.autosave_profiles_if_changed()
                window.autosave_profiles_if_changed()

            capture.assert_not_called()

    def test_dirty_autosave_captures_once_then_returns_to_fast_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self.create_window(temp_dir)
            window.add_row({"type": 1.0, "value": "10,20"})
            self.assertTrue(window.profile_has_unsaved_changes())

            with mock.patch.object(
                window,
                "get_current_ui_config",
                wraps=window.get_current_ui_config,
            ) as capture:
                window.autosave_profiles_if_changed()
                window.autosave_profiles_if_changed()

            self.assertEqual(capture.call_count, 1)
            self.assertFalse(window.profile_has_unsaved_changes())


class _VariableHintChild(QWidget):
    """sizeHint 可编程的子控件，用于确定性构造“子控件度量变化”。"""

    def __init__(self):
        super().__init__()
        self._hint = QSize(60, 20)

    def set_hint(self, size):
        self._hint = QSize(size)
        self.updateGeometry()

    def sizeHint(self):
        return QSize(self._hint)

    def minimumSizeHint(self):
        return QSize(self._hint)


class _LayoutRequestCounter(QObject):
    """统计投递到目标控件的 QEvent.LayoutRequest 次数。"""

    def __init__(self):
        super().__init__()
        self.count = 0

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.LayoutRequest:
            self.count += 1
        return False


def _pump_events(app, timeout_ms):
    """按有界切片泵事件，避免单次 processEvents 无限展开。"""

    deadline = time.perf_counter() + timeout_ms / 1000.0
    while time.perf_counter() < deadline:
        app.processEvents(QEventLoop.AllEvents, 10)


# FR-001 回归阈值：一次触发的收敛行为是 O(1) 量级事件；24 允许同一窗口内
# 出现约 8 次彼此独立的触发，同时保持在实测风暴速率（每 500ms 约 3800~4700 次，
# 见 REVIEW-20260829-002 05 报告 FR-001）的两个数量级以下。
_MAX_LAYOUT_REQUESTS_PER_500MS = 24


class ResponsiveRowLayoutRequestTests(QtCallbackGuardMixin, unittest.TestCase):
    """UIS-RM-001 验证：行宽不变时子控件度量变化必须刷新行高（ARCHITECTURE #50）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_child_metric_change_via_layout_request_refreshes_row_height(self):
        host = QWidget()
        try:
            host_layout = QVBoxLayout(host)
            child = _VariableHintChild()
            row = ResponsiveRow()
            row.add_group(child)
            host_layout.addWidget(row)
            host.resize(360, 200)
            host.show()
            self.app.processEvents()
            row.flush_height_sync()
            old_height = row.minimumHeight()
            self.assertGreater(old_height, 0)

            # 子控件度量变化只会让行收到 LayoutRequest，不触发行级
            # FontChange/StyleChange——这是 #50 缓存失效契约的已知缺口。
            # 注意：直发的 sendEvent 不会分发到 Python 事件覆写，须用
            # postEvent + processEvents 走真实投递路径。
            child.set_hint(QSize(60, old_height + 60))
            self.app.postEvent(row, QEvent(QEvent.Type.LayoutRequest))
            self.app.processEvents()
            row.flush_height_sync()

            self.assertGreater(row.minimumHeight(), old_height)
        finally:
            dispose_widget(self.app, host)


class ResponsiveRowLayoutRequestStormTests(QtCallbackGuardMixin, unittest.TestCase):
    """FR-001 回归：LayoutRequest 处理不得自激（ARCHITECTURE #50 高频事件有界性）。

    自激机制：``ResponsiveRow.event`` 收到 LayoutRequest 后调用
    ``FlowLayout.invalidate()``，其末段 ``QLayout::invalidate()`` 会向行再次
    post LayoutRequest，形成无限自激；每个周期还全量重排并清空高度缓存。
    修复必须保留"清高度缓存 + 逐条目 item.invalidate()"（UIS-RM-001 本体），
    只去掉 ``QLayout::invalidate()`` 的重发。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _build_row(self):
        host = QWidget()
        host_layout = QVBoxLayout(host)
        row = ResponsiveRow()
        counter = _LayoutRequestCounter()
        row.installEventFilter(counter)
        child = _VariableHintChild()
        row.add_group(QPushButton("B0"), QLabel("L0"))
        row.add_group(child)
        for i in range(1, 4):
            row.add_group(QPushButton(f"B{i}"), QLabel(f"L{i}"))
        host_layout.addWidget(row)
        host.resize(360, 200)
        host.show()
        return host, row, child, counter

    def test_single_layout_request_does_not_self_excite(self):
        host, row, _child, counter = self._build_row()
        try:
            _pump_events(self.app, 300)
            counter.count = 0
            self.app.postEvent(row, QEvent(QEvent.Type.LayoutRequest))
            _pump_events(self.app, 500)
            self.assertLessEqual(
                counter.count,
                _MAX_LAYOUT_REQUESTS_PER_500MS,
                f"单次 LayoutRequest 后出现自激：500ms 内收到 {counter.count} 次",
            )
        finally:
            dispose_widget(self.app, host)

    def test_single_child_update_geometry_does_not_self_excite(self):
        host, row, child, counter = self._build_row()
        try:
            _pump_events(self.app, 300)
            counter.count = 0
            child.set_hint(QSize(60, 80))
            _pump_events(self.app, 500)
            self.assertLessEqual(
                counter.count,
                _MAX_LAYOUT_REQUESTS_PER_500MS,
                f"子控件 updateGeometry 后出现自激：500ms 内收到 {counter.count} 次",
            )
        finally:
            dispose_widget(self.app, host)

    def test_quiet_period_does_not_keep_growing(self):
        host, _row, _child, counter = self._build_row()
        try:
            _pump_events(self.app, 300)
            counter.count = 0
            _pump_events(self.app, 500)
            self.assertLessEqual(
                counter.count,
                _MAX_LAYOUT_REQUESTS_PER_500MS,
                f"静默期仍在持续自激：500ms 内收到 {counter.count} 次",
            )
        finally:
            dispose_widget(self.app, host)


if __name__ == "__main__":
    unittest.main()
