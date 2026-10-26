"""UIC15 守卫测试：计划任务窗口在运行中编辑锁下禁止修改计划。

方案 B（用户确认）：主窗口在运行中（``_runtime_editing_locked == True``）把锁查询
传给计划任务窗口；窗口的新建/编辑/复制/删除/启停/导入/清历史入口必须被拦截。

覆盖：
1. 锁定态下 ``_on_new`` / ``_open_editor`` / ``_on_toggle_selected`` /
   ``_on_import`` / ``_clear_history_guarded`` 不执行修改并提示；
2. 上下文菜单的复制/删除/启停/编辑入口在锁定态被拦截；
3. 未传锁查询（默认构造）时行为不变（兼容旧调用方）；
4. 解锁态下修改照常执行。
"""

import os
import tempfile
import unittest
from datetime import time as wall_time
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from fukua_rpa.scheduled_task_model import (
    ScheduledTask,
    ScheduledTrigger,
    TriggerKind,
)
from fukua_rpa.scheduled_task_service import ScheduledTaskService
from fukua_rpa.ui.scheduled_tasks_window import ScheduledTasksWindow
from tests.qt_test_support import QtCallbackGuardMixin, dispose_widget


def _service(tmp):
    execution = mock.MagicMock()
    execution.scheduled_health.return_value = (True, True, True)
    return ScheduledTaskService(mock.MagicMock(), execution, tmp)


def _task(schedule_id: str = "schedule-1", enabled: bool = True) -> ScheduledTask:
    return ScheduledTask(
        schedule_id=schedule_id,
        name="每天整理报表",
        profile_id="profile-1",
        profile_name_snapshot="报表方案",
        enabled=enabled,
        trigger=ScheduledTrigger(kind=TriggerKind.DAILY, local_time=wall_time(9, 0)),
    )


def _build_window(test_case, tmp, locked=False):
    """构造真实计划任务窗口（含真实 service/store），供本模块两类用例共用。"""
    lock_state = {"locked": locked}
    window = ScheduledTasksWindow(
        _service(tmp),
        lambda: [("profile-1", "报表方案")],
        lambda _profile: None,
        QSettings(os.path.join(tmp, "config.ini"), QSettings.IniFormat),
        is_runtime_editing_locked=lambda: lock_state["locked"],
    )
    test_case.addCleanup(dispose_widget, test_case.app, window)
    return window, lock_state


class ScheduledTasksLockGuardTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self, tmp, locked=False):
        return _build_window(self, tmp, locked=locked)

    # ------------------------------------------------------------------
    # 锁定态拦截
    # ------------------------------------------------------------------
    def test_locked_blocks_new_and_editor(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "fukua_rpa.ui.scheduled_tasks_window.QMessageBox.information"
        ) as info:
            window, _state = self.make_window(tmp, locked=True)
            with mock.patch.object(window, "_open_editor") as open_editor:
                window._on_new()
                open_editor.assert_not_called()
            info.assert_called_once()
            info.reset_mock()
            # 直接调 _open_editor：守卫必须拦截，ScheduledTaskEditor 不得被实例化。
            with mock.patch(
                "fukua_rpa.ui.scheduled_tasks_window.ScheduledTaskEditor"
            ) as editor_cls:
                window._open_editor("schedule-1")
                editor_cls.assert_not_called()
            info.assert_called_once()

    def test_locked_blocks_toggle_selected(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "fukua_rpa.ui.scheduled_tasks_window.QMessageBox.information"
        ) as info:
            window, _state = self.make_window(tmp, locked=True)
            with mock.patch.object(window._service, "set_enabled") as set_enabled:
                window._on_toggle_selected()
                set_enabled.assert_not_called()
            info.assert_called_once()

    def test_locked_blocks_import_and_clear_history(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "fukua_rpa.ui.scheduled_tasks_window.QMessageBox.information"
        ):
            window, _state = self.make_window(tmp, locked=True)
            with mock.patch.object(window, "_deny_while_locked", return_value=True):
                # 导入入口：锁定态下不应打开文件选择框。
                with mock.patch(
                    "fukua_rpa.ui.scheduled_tasks_mixins.import_export.QFileDialog"
                ) as fd:
                    window._on_import()
                    fd.getOpenFileName.assert_not_called()
                # 清除历史：锁定态下不应触碰 service。
                with mock.patch.object(window._service, "clear_history") as clear:
                    window._clear_history_guarded()
                    clear.assert_not_called()

    def test_locked_blocks_context_menu_mutations(self):
        from PySide6.QtCore import QPoint

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "fukua_rpa.ui.scheduled_tasks_window.QMessageBox.information"
        ):
            window, _state = self.make_window(tmp, locked=True)
            window._service.add_task(_task())
            window._controller.model().refresh()

            # 用 mock 表格 + mock QMenu 驱动真实 _on_table_context_menu 分发逻辑
            # （避免离屏真实表格布局与原生 QMenu.exec 阻塞）。addAction 按文本返回
            # 稳定动作对象，exec 动态返回指定文本的动作，使 choice 分发走真实分支
            # 并被锁定守卫拦截。
            index = mock.Mock()
            index.isValid.return_value = True
            index.row.return_value = 0
            table = mock.Mock()
            table.indexAt.return_value = index
            table.selectionModel().isRowSelected.return_value = True
            table.viewport().mapToGlobal.return_value = QPoint(10, 10)
            window._find_row_by_id = mock.Mock(
                return_value={"task": window._service.task_by_id("schedule-1")}
            )
            window._selected_schedule_ids = mock.Mock(return_value=["schedule-1"])

            actions: dict[str, mock.Mock] = {}

            def add_action(text):
                act = mock.Mock()
                act.text.return_value = text
                actions[text] = act
                return act

            menu_cls = mock.MagicMock()
            menu_cls.return_value.addAction.side_effect = add_action
            menu_cls.return_value.exec.side_effect = (
                lambda *_a, **_k: actions.get("复制计划")
            )

            service = window._service
            with mock.patch.object(service, "duplicate_task") as dup, \
                    mock.patch.object(service, "remove_task") as rm, \
                    mock.patch.object(service, "set_enabled") as enable, \
                    mock.patch.object(window, "_open_editor") as edit, \
                    mock.patch.object(window, "_table", return_value=table):
                action_texts = {
                    "复制计划": dup,
                    "删除计划": rm,
                    "启用所选": enable,
                    "停用所选": enable,
                    "编辑": edit,
                }
                for text, target in action_texts.items():
                    with self.subTest(action=text):
                        actions.clear()
                        menu_cls.return_value.exec.side_effect = (
                            lambda *_a, _t=text, **_k: actions.get(_t)
                        )
                        with mock.patch(
                            "fukua_rpa.ui.scheduled_tasks_mixins.context_menu.QMenu",
                            menu_cls,
                        ):
                            window._on_table_context_menu(QPoint(10, 10))
                        target.assert_not_called()
            dup.assert_not_called()
            rm.assert_not_called()
            enable.assert_not_called()
            edit.assert_not_called()

    # ------------------------------------------------------------------
    # 兼容性与解锁态
    # ------------------------------------------------------------------
    def test_default_constructor_without_lock_callback_stays_permissive(self):
        with tempfile.TemporaryDirectory() as tmp:
            window = ScheduledTasksWindow(
                _service(tmp),
                lambda: [("profile-1", "报表方案")],
                lambda _profile: None,
                QSettings(os.path.join(tmp, "config.ini"), QSettings.IniFormat),
            )
            self.addCleanup(dispose_widget, self.app, window)
            self.assertFalse(window._is_runtime_editing_locked())

    def test_unlocked_allows_editor_open(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "fukua_rpa.ui.scheduled_tasks_window.QMessageBox.information"
        ) as info:
            window, _state = self.make_window(tmp, locked=False)
            with mock.patch.object(window, "_open_editor") as open_editor:
                window._on_new()
                open_editor.assert_called_once()
            info.assert_not_called()

    # ------------------------------------------------------------------
    # P1-1：启用列 checkbox 点击路径
    # ------------------------------------------------------------------
    def test_locked_blocks_enable_checkbox_toggle(self):
        from PySide6.QtCore import QPoint, QRect, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QStyleOptionViewItem

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "fukua_rpa.ui.scheduled_tasks_window.QMessageBox.information"
        ):
            window, _state = self.make_window(tmp, locked=True)
            window._service.add_task(_task())
            window._controller.model().refresh()
            table = window._table()
            table.resize(1000, 620)
            table.show()
            self.app.processEvents()

            delegate = window._checkbox_delegate
            model = window._controller.model()
            index = model.index(0, 7)  # COL_ENABLED == 7
            # 手动构造足够小的 option 矩形：中心点必落在 delegate 计算的
            # indicator 矩形内（offscreen 下 visualRect 布局不可靠，不能用它）。
            option = QStyleOptionViewItem()
            option.widget = table
            option.rect = QRect(0, 0, 40, 24)
            center = option.rect.center()
            click_pos = QPoint(center.x(), center.y())
            event = QMouseEvent(
                QMouseEvent.MouseButtonRelease,
                click_pos,
                click_pos,
                Qt.LeftButton,
                Qt.LeftButton,
                Qt.NoModifier,
            )
            # 锁定态：点击 checkbox 必须被拦截，setData 不得改写服务状态。
            with mock.patch.object(model, "setData") as set_data:
                consumed = delegate.editorEvent(event, model, option, index)
                set_data.assert_not_called()
            self.assertTrue(consumed)
            self.assertTrue(
                window._service.task_by_id("schedule-1").enabled,
                "锁定态下不应改变启用状态",
            )

    def test_unresolvable_widget_denies_checkbox_toggle(self):
        """RM-SCOPE014-001：窗口解析失败必须 fail-closed。

        `option.widget` 为 None 或指向不提供 `_deny_while_locked` 的窗口时，此前
        守卫静默放行（fail-open），等于运行中编辑锁对该改写路径不生效。
        """
        from PySide6.QtCore import QEvent, QPoint, QRect, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QStyleOptionViewItem, QWidget

        from fukua_rpa.ui.scheduled_tasks_window import _CenterCheckboxDelegate

        class _Index:
            def data(self, _role):
                return Qt.Checked

        class _Model:
            def __init__(self):
                self.calls = []

            def setData(self, index, value, role):  # noqa: N802 - 对齐 Qt 命名
                self.calls.append((index, value, role))
                return True

        delegate = _CenterCheckboxDelegate(None)
        for label, widget in (("None", None), ("普通 QWidget", QWidget())):
            with self.subTest(widget=label):
                if widget is not None:
                    self.addCleanup(dispose_widget, self.app, widget)
                model = _Model()
                option = QStyleOptionViewItem()
                option.rect = QRect(0, 0, 40, 24)
                option.widget = widget
                point = QPoint(20, 12)
                event = QMouseEvent(
                    QEvent.Type.MouseButtonRelease,
                    point,
                    point,
                    Qt.LeftButton,
                    Qt.LeftButton,
                    Qt.NoModifier,
                )
                # 拒绝必须留痕：否则用户看到「点了没反应」却无从判断原因。
                # 用 logger 替身而非 assertLogs：后者在红侧会先于「未改写数据」
                # 断言失败，遮蔽本用例要测的主断言。
                with mock.patch(
                    "fukua_rpa.ui.scheduled_tasks_window.logger"
                ) as logger_mock:
                    consumed = delegate.editorEvent(event, model, option, _Index())
                self.assertEqual(model.calls, [], "解析失败时不得改写计划数据")
                self.assertTrue(consumed)
                self.assertTrue(logger_mock.warning.called, "拒绝必须留痕，不得静默")

    def test_resolved_window_still_toggles_when_unlocked(self):
        """不变量：窗口可解析且未锁定时，点击必须照常改写计划数据。"""

        from PySide6.QtCore import QEvent, QPoint, QRect, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QStyleOptionViewItem, QWidget

        from fukua_rpa.ui.scheduled_tasks_window import _CenterCheckboxDelegate

        class _Index:
            def data(self, _role):
                return Qt.Checked

        class _Model:
            def __init__(self):
                self.calls = []

            def setData(self, index, value, role):  # noqa: N802 - 对齐 Qt 命名
                self.calls.append((index, value, role))
                return True

        class _UnlockedWindow(QWidget):
            def _deny_while_locked(self):
                return False

        widget = _UnlockedWindow()
        self.addCleanup(dispose_widget, self.app, widget)
        model = _Model()
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 40, 24)
        option.widget = widget
        point = QPoint(20, 12)
        event = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            point,
            point,
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        consumed = _CenterCheckboxDelegate(None).editorEvent(
            event, model, option, _Index()
        )
        self.assertTrue(consumed)
        self.assertEqual(len(model.calls), 1, "未锁定时的点击必须仍然生效")

    # ------------------------------------------------------------------
    # P2-2：编辑器保存入口窗口期
    # ------------------------------------------------------------------
    def test_editor_save_blocked_while_locked(self):
        from fukua_rpa.ui.scheduled_task_editor import ScheduledTaskEditor

        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "fukua_rpa.ui.scheduled_task_editor.QMessageBox.warning"
        ) as warn:
            window, state = self.make_window(tmp, locked=False)
            state["locked"] = True
            editor = ScheduledTaskEditor(
                QSettings(os.path.join(tmp, "config.ini"), QSettings.IniFormat),
                window._service,
                lambda: [("profile-1", "报表方案")],
                lambda _profile: None,
                schedule_id=None,
                is_runtime_editing_locked=lambda: state["locked"],
            )
            self.addCleanup(dispose_widget, self.app, editor)
            editor._name_edit.setText("新计划")
            with mock.patch.object(window._service, "add_task") as add_task:
                editor._on_save()
                add_task.assert_not_called()
            warn.assert_called_once()


class ScheduledTasksToggleSelectedBatchTests(QtCallbackGuardMixin, unittest.TestCase):
    """FR-SCOPE014-001：工具栏「启用/停用所选」必须走批量接口，且保持切换语义。

    基线 `_on_toggle_selected` 对每个选中 id 逐条 `set_enabled`，而
    `ScheduledTaskService.set_enabled` 每条都全量写盘 + `tasks_changed.emit()` +
    `_recompute()` ⇒ N 条选中触发 N 次刷新放大。同一窗口的右键菜单
    （`scheduled_tasks_mixins/context_menu.py:86`/`:89`）早已使用 `set_enabled_many`，
    属遗漏迁移而非设计取舍。

    **语义陷阱**：工具栏是「切换」而不是「设为某值」⇒ 按当前 enabled 分组、各自
    取反，混合选中项不得被统一成同一状态。
    """

    #: 每个用例都从这个已知初始状态出发（2 启用 + 1 停用）。
    BASE_STATES = {"a-on": True, "b-on": True, "c-off": False}

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self, tmp):
        return _build_window(self, tmp)

    def _add_tasks(self, window):
        for sid, enabled in self.BASE_STATES.items():
            window._service.add_task(_task(sid, enabled))

    def _force_states(self, window):
        for sid, enabled in self.BASE_STATES.items():
            window._service.set_enabled(sid, enabled)

    def _states(self, window):
        return {
            row["task"].schedule_id: bool(row["task"].enabled)
            for row in window._service.list_view()
        }

    def _toggle(self, window, selected_ids):
        """调用 `_on_toggle_selected`，同时捕获批量与逐条两个入口。

        两个替身都用 `wraps=` 保留真实实现：否则状态不会被改写，切换语义的
        断言会因替身本身而失真。
        """
        real_many = window._service.set_enabled_many
        real_single = window._service.set_enabled
        with mock.patch.object(
            window._service, "set_enabled_many", wraps=real_many
        ) as many, mock.patch.object(
            window._service, "set_enabled", wraps=real_single
        ) as single, mock.patch.object(
            window, "_selected_schedule_ids", return_value=list(selected_ids)
        ):
            window._on_toggle_selected()
        return many, single

    @staticmethod
    def _calls(many):
        result = []
        for call in many.call_args_list:
            ids = call.args[0] if call.args else call.kwargs.get("schedule_ids", [])
            enabled = (
                call.args[1] if len(call.args) > 1 else call.kwargs.get("enabled")
            )
            result.append((tuple(sorted(ids)), bool(enabled)))
        return sorted(result)

    @staticmethod
    def _expect(*pairs):
        return sorted(
            (tuple(sorted(text.split(","))), bool(enabled))
            for text, enabled in pairs
        )

    def test_mixed_selection_uses_two_batched_calls_and_inverts_each_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            window, _state = self._make_window(tmp)
            self._add_tasks(window)
            self._force_states(window)
            before = self._states(window)

            many, single = self._toggle(window, ["a-on", "b-on", "c-off"])

            self.assertEqual(
                self._calls(many),
                self._expect(("a-on,b-on", False), ("c-off", True)),
                "混合选中必须按当前状态分成两组、各调用一次批量接口",
            )
            self.assertEqual(many.call_count, 2)
            self.assertEqual(single.call_count, 0, "不得再逐条调用 set_enabled")
            self.assertEqual(
                self._states(window),
                {sid: not value for sid, value in before.items()},
                "混合选中项必须逐条取反，不得被统一成同一状态",
            )

    def test_uniform_selection_uses_a_single_batched_call(self):
        for selected, expected in (
            (["a-on", "b-on"], (("a-on,b-on", False),)),
            (["c-off"], (("c-off", True),)),
        ):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as tmp:
                window, _state = self._make_window(tmp)
                self._add_tasks(window)
                self._force_states(window)

                many, single = self._toggle(window, selected)

                self.assertEqual(self._calls(many), self._expect(*expected))
                self.assertEqual(many.call_count, 1)
                self.assertEqual(single.call_count, 0)

    def test_no_selection_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "fukua_rpa.ui.scheduled_tasks_window.QMessageBox.information"
        ) as info:
            window, _state = self._make_window(tmp)
            self._add_tasks(window)
            self._force_states(window)
            before = self._states(window)

            many, single = self._toggle(window, [])

            self.assertEqual(info.call_count, 1)
            self.assertEqual(many.call_count, 0)
            self.assertEqual(single.call_count, 0)
            self.assertEqual(self._states(window), before)


if __name__ == "__main__":
    unittest.main()
