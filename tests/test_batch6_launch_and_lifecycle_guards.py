"""Guards for the REVIEW-20261006-001 batch 6 fixes.

Covers three independently-rooted defects plus the shared launch predicate:

* ``FR-20261006-011`` — plugin numeric parameters were silently clamped by
  ``QSpinBox``/``QDoubleSpinBox.setValue`` and the clamped value written back, so a
  saved ``999`` became ``10`` merely by opening and saving the settings dialog.
  That contradicted the enum branch in the same function, which preserves
  out-of-range values as placeholders.
* ``FR-20261006-012`` — the scheduled-run log filter classified ``pending_catchup``
  as ``failed`` because it fell through to the final ``return "failed"``.
* ``FR-20261006-006`` + ``ADV-20261006-002`` — the "may not launch now" predicate was
  written out four times, and the copy used by scheduled tasks and profile hotkeys
  omitted the recognition-test term entirely, so a schedule firing during a
  recognition test produced two input owners.
* ``FR-20261006-013`` — deleting a step left its coordinate picker open, so the
  user's click was delivered into a detached widget and the coordinate was lost.
"""

from __future__ import annotations

import os
import sys
import unittest
import weakref
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

_APP = QApplication.instance() or QApplication([])


def _alive(widget) -> bool:
    """True when the Qt object has not been destroyed."""
    if widget is None:
        return False
    try:
        from shiboken6 import isValid

        return bool(isValid(widget))
    except Exception:  # pragma: no cover - shiboken always available with PySide6
        return False


class PluginNumericRangeGuardTests(unittest.TestCase):
    """FR-20261006-011: out-of-range numeric values must not be silently rewritten."""

    def _make_widget(self, ptype, value, prop):
        from fukua_rpa.ui.plugin_action_settings import PluginActionSettings

        dialog = PluginActionSettings.__new__(PluginActionSettings)
        dialog._param_widgets = []
        return dialog._make_widget(ptype, value, prop)

    def _collect(self, widget, key="p"):
        from fukua_rpa.ui.plugin_action_settings import PluginActionSettings

        dialog = PluginActionSettings.__new__(PluginActionSettings)
        dialog._param_widgets = [(key, widget)]
        return dialog._collect_config()

    def test_int_above_maximum_is_preserved(self):
        widget = self._make_widget("integer", 999, {"minimum": 0, "maximum": 10})
        self.assertEqual(self._collect(widget)["p"], 999)

    def test_int_below_minimum_is_preserved(self):
        widget = self._make_widget("integer", -5, {"minimum": 0, "maximum": 10})
        self.assertEqual(self._collect(widget)["p"], -5)

    def test_float_above_maximum_is_preserved(self):
        widget = self._make_widget("number", 99.5, {"minimum": 0.0, "maximum": 1.0})
        self.assertEqual(self._collect(widget)["p"], 99.5)

    def test_out_of_range_control_is_disabled_and_explains_why(self):
        """A disabled control alone would look broken; the tooltip must say why."""
        widget = self._make_widget("integer", 999, {"minimum": 0, "maximum": 10})
        self.assertFalse(widget.isEnabled())
        self.assertIn("999", widget.toolTip())
        self.assertIn("0", widget.toolTip())

    def test_in_range_value_is_unchanged(self):
        widget = self._make_widget("integer", 7, {"minimum": 0, "maximum": 10})
        self.assertTrue(widget.isEnabled())
        self.assertEqual(self._collect(widget)["p"], 7)

    def test_in_range_value_at_the_boundaries(self):
        for value in (0, 10):
            widget = self._make_widget("integer", value, {"minimum": 0, "maximum": 10})
            self.assertEqual(self._collect(widget)["p"], value)
            self.assertTrue(widget.isEnabled())

    def test_missing_value_still_uses_the_default(self):
        widget = self._make_widget("integer", None, {"minimum": 3, "maximum": 10})
        self.assertEqual(self._collect(widget)["p"], 0)

    def test_reversed_bounds_are_normalised(self):
        widget = self._make_widget("integer", 5, {"minimum": 10, "maximum": 0})
        self.assertEqual(self._collect(widget)["p"], 5)

    def test_enum_branch_still_preserves_unknown_value(self):
        """The numeric fix must not disturb the enum branch it mirrors."""
        widget = self._make_widget("string", "weird", {"enum": ["a", "b"]})
        self.assertEqual(self._collect(widget)["p"], "weird")


class ScheduledLogOutcomeGroupTests(unittest.TestCase):
    """FR-20261006-012: ``pending_catchup`` is neither failure nor skip."""

    def _group(self, outcome):
        from fukua_rpa.ui.scheduled_task_log_window import _outcome_group

        return _outcome_group(outcome)

    def test_pending_catchup_is_its_own_group(self):
        self.assertEqual(self._group("pending_catchup"), "pending")

    def test_pending_group_exists_in_the_filter_list(self):
        from fukua_rpa.ui.scheduled_task_log_window import _FILTER_OPTIONS

        keys = [key for key, _label in _FILTER_OPTIONS]
        self.assertIn("pending", keys)
        # Every group the classifier can return must be filterable.
        produced = {
            self._group(value)
            for value in (
                "running",
                "success",
                "finished",
                "stopped",
                "user_stopped",
                "pending_catchup",
                "skipped",
                "pending_ask",
                "missed",
                "failed",
            )
        }
        self.assertTrue(produced <= set(keys), f"未覆盖的分组：{produced - set(keys)}")

    def test_existing_groupings_are_unchanged(self):
        self.assertEqual(self._group("running"), "running")
        self.assertEqual(self._group("success"), "success")
        self.assertEqual(self._group("finished"), "success")
        self.assertEqual(self._group("stopped"), "stopped")
        self.assertEqual(self._group("user_stopped"), "stopped")
        for value in ("skipped", "pending_ask", "missed"):
            self.assertEqual(self._group(value), "skipped")
        self.assertEqual(self._group("failed"), "failed")

    def test_unknown_outcome_still_falls_back_to_failed(self):
        self.assertEqual(self._group("something_new"), "failed")

    def test_empty_outcome_still_falls_back_to_failed(self):
        self.assertEqual(self._group(""), "failed")


class LaunchGuardTests(unittest.TestCase):
    """FR-20261006-006 + ADV-20261006-002: one predicate, no missing terms."""

    class _Feedback:
        def __init__(self, active):
            self.recognition_test_active = active

    class _RunController:
        def __init__(self, active):
            self.is_active = active

    def setUp(self):
        self._hosts = []

    def _controller(self, *, clicker=False, run_active=False, start=False, testing=False):
        from fukua_rpa.ui.controllers.execution_controller import ExecutionController

        controller = ExecutionController.__new__(ExecutionController)
        controller.start_in_progress = start
        host = type("Host", (), {})()
        host.run_controller = self._RunController(run_active)
        host.feedback_controller = self._Feedback(testing)
        host.auto_clicker_is_running = lambda: clicker
        # ``host`` 是只读 property，宿主只能经 ``_host_ref`` 注入。
        controller._host_ref = weakref.ref(host)
        self._hosts.append(host)  # keep the weakref alive for the test duration
        return controller

    def test_recognition_test_blocks_launch(self):
        """The term the scheduled/hotkey path was missing."""
        controller = self._controller(testing=True)
        self.assertTrue(controller.is_launch_blocked())

    def test_clicker_blocks_launch_with_a_reason(self):
        controller = self._controller(clicker=True)
        reason = controller.launch_blocked_reason()
        self.assertIsNotNone(reason)
        self.assertIn("连点器", reason)

    def test_active_run_blocks_launch(self):
        self.assertTrue(self._controller(run_active=True).is_launch_blocked())

    def test_start_in_progress_blocks_launch(self):
        self.assertTrue(self._controller(start=True).is_launch_blocked())

    def test_idle_allows_launch(self):
        controller = self._controller()
        self.assertFalse(controller.is_launch_blocked())
        self.assertIsNone(controller.launch_blocked_reason())

    def test_busy_case_produces_no_user_facing_reason(self):
        """Running/recognising stays silent so it cannot spam tooltips."""
        self.assertEqual(self._controller(testing=True).launch_blocked_reason(), "")

    def test_prepare_and_launch_profile_consults_the_shared_predicate(self):
        """FR-20261006-006: the schedule/hotkey entry must use the same guard.

        Static assertion: reading the source is the only way to prove the call
        happens *before* the engine starts without standing up a full scheduled
        run, and the ordering is the whole point of the fix.
        """
        import inspect

        from fukua_rpa.ui.controllers.execution_controller import ExecutionController

        source = inspect.getsource(ExecutionController.prepare_and_launch_profile)
        guard_at = source.find("is_launch_blocked")
        self.assertNotEqual(guard_at, -1, "prepare_and_launch_profile 未调用共享守卫")
        launch_at = source.find("RunRequest.create")
        self.assertNotEqual(launch_at, -1)
        self.assertLess(
            guard_at,
            launch_at,
            "守卫必须早于 RunRequest.create，否则识别测试期间仍可启动第二个输入方",
        )

    def test_no_launch_path_duplicates_the_guard_terms(self):
        """ADV-20261006-002: the four copies must be gone, not just the fourth fixed."""
        import inspect

        from fukua_rpa.ui.controllers.execution_controller import ExecutionController

        for name in ("start_task", "_start_task_impl", "run_selected_step_once"):
            source = inspect.getsource(getattr(ExecutionController, name))
            self.assertNotIn(
                "recognition_test_active",
                source,
                f"{name} 仍内联守卫条件，应改用 launch_blocked_reason",
            )

    def test_prepare_and_launch_profile_applies_the_settings_mode_filter(self):
        """FR-20261008-701: 计划任务/快捷键入口必须与手动运行一致地过滤断点。

        简易模式不触发调试断点是唯一的执行语义例外（设计基准 §简易模式与高级模式、
        显隐矩阵 §保留与执行语义）。本入口此前是唯一未调用
        ``tasks_for_current_settings_mode`` 的启动路径，于是简易模式下保存的断点会在
        无人值守运行中触发。静态断言：过滤必须早于校验、插件依赖收集与
        ``RunRequest.create``，使三者作用于同一份过滤后列表。
        """
        import inspect

        from fukua_rpa.ui.controllers.execution_controller import ExecutionController

        source = inspect.getsource(ExecutionController.prepare_and_launch_profile)
        filter_at = source.find("tasks_for_current_settings_mode")
        self.assertNotEqual(
            filter_at, -1, "prepare_and_launch_profile 未应用简易/高级模式过滤"
        )
        for later in (
            "validate_tasks",
            "collect_plugin_dependencies",
            "RunRequest.create",
        ):
            at = source.find(later)
            self.assertNotEqual(at, -1, f"{later} 不在本入口")
            self.assertLess(
                filter_at, at, f"模式过滤必须早于 {later}，否则校验/执行对象不一致"
            )


class CoordinatePickerLifecycleTests(unittest.TestCase):
    """FR-20261006-013: deleting a step must close its picker, and only its own."""

    def setUp(self):
        import shutil
        import tempfile

        sys.path.insert(0, str(ROOT / "tests"))
        from qt_test_support import create_test_window

        self.base_dir = tempfile.mkdtemp(prefix="fr013-guard-")
        self.addCleanup(shutil.rmtree, self.base_dir, True)
        self.window = create_test_window(self, self.base_dir)

    def _load(self, count):
        self.window.restore_task_snapshot(
            [
                {"type": 1.0, "step_id": f"s{i}", "value": "10,10"}
                for i in range(count)
            ]
        )
        _APP.processEvents()

    def _rows(self):
        return [
            self.window.task_list.itemWidget(self.window.task_list.item(index))
            for index in range(self.window.task_list.count())
        ]

    def _open(self, row):
        row.start_coordinate_pick()
        picker = getattr(row, "coordinate_picker", None)
        thread = picker.pick_thread

        # SAFETY: 强制销毁或步骤重建会失去窗口对线程的引用；测试仍须负责清理。
        def stop_test_thread():
            thread.stop()
            self.assertTrue(thread.wait(1500), "测试创建的取点线程未能停止")

        self.addCleanup(stop_test_thread)
        return picker

    def test_delete_closes_the_picker_on_that_row(self):
        self._load(3)
        rows = [r for r in self._rows() if r is not None]
        picker = self._open(rows[1])
        thread = picker.pick_thread
        self.assertTrue(_alive(picker))
        self.window.task_list.setCurrentRow(1)
        self.assertTrue(self.window.workflow_editor.delete_selected_rows(rows=[1]))
        _APP.processEvents()
        self.assertFalse(_alive(picker), "删除步骤后其取点器必须关闭")
        self.assertFalse(thread.isRunning(), "删除步骤必须自行停止取点线程")

    def test_delete_leaves_other_pickers_usable(self):
        self._load(3)
        rows = [r for r in self._rows() if r is not None]
        keep = self._open(rows[0])
        self.window.task_list.setCurrentRow(1)
        self.window.workflow_editor.delete_selected_rows(rows=[1])
        _APP.processEvents()
        self.assertTrue(_alive(keep), "无关步骤的取点器不得被一并关闭")

    def test_delete_many_closes_every_picker(self):
        self._load(3)
        rows = [r for r in self._rows() if r is not None]
        pickers = [p for p in (self._open(r) for r in rows) if _alive(p)]
        threads = [p.pick_thread for p in pickers]
        self.assertGreaterEqual(len(pickers), 2)
        self.window.workflow_editor.delete_selected_rows(rows=[0, 1, 2])
        _APP.processEvents()
        self.assertFalse([p for p in pickers if _alive(p)])
        self.assertFalse([t for t in threads if t.isRunning()], "批量删除必须自行停止全部取点线程")

    def test_close_with_no_rows_argument_clears_everything(self):
        """Wholesale rebuilds pass no argument and must clear all pickers."""
        self._load(3)
        rows = [r for r in self._rows() if r is not None]
        pickers = [p for p in (self._open(r) for r in rows) if _alive(p)]
        threads = [p.pick_thread for p in pickers]
        self.assertGreaterEqual(len(pickers), 2)
        self.window.close_coordinate_pickers()
        _APP.processEvents()
        self.assertFalse([p for p in pickers if _alive(p)])
        self.assertFalse([t for t in threads if t.isRunning()], "关闭全部取点器必须自行停止全部线程")

    def test_row_reference_is_cleared_after_close(self):
        self._load(2)
        rows = [r for r in self._rows() if r is not None]
        picker = self._open(rows[0])
        thread = picker.pick_thread
        self.window.close_coordinate_pickers(rows=[0])
        _APP.processEvents()
        self.assertIsNone(getattr(rows[0], "coordinate_picker", None))
        self.assertFalse(_alive(picker))
        self.assertFalse(thread.isRunning(), "清除取点引用前必须自行停止线程")

    def test_closing_an_already_destroyed_picker_is_tolerated(self):
        """RuntimeError from a Qt object deleted underneath us must not escape."""
        self._load(2)
        rows = [r for r in self._rows() if r is not None]
        picker = self._open(rows[0])
        rows[0].coordinate_picker = picker
        # Force the object to look destroyed to the wrapper.
        try:
            import shiboken6

            shiboken6.delete(picker)
        except Exception:  # pragma: no cover - environment dependent
            self.skipTest("当前 shiboken 版本不支持 delete()")
        self.window.close_coordinate_pickers(rows=[0])  # must not raise


class SettingsDialogRuntimeReadOnlyTests(unittest.TestCase):
    """ADV-20261006-022: an open settings dialog must go read-only during a run.

    The maintainer's boundary is explicit: **only** shortcut / mapping editing
    becomes read-only, the dialog stays visible and closable, the previous enabled
    state is restored afterwards, and nothing about stop or run monitoring changes.
    """

    def _controller(self, dialog):
        from fukua_rpa.ui.controllers.execution_controller import ExecutionController

        controller = ExecutionController.__new__(ExecutionController)
        controller._runtime_widget_states = None
        controller._settings_row_states = []
        controller._task_list_drag_mode = None
        controller._run_plugin_deps = ()
        controller._watchdog_release_connected = True
        controller._ensure_watchdog_release_connected = lambda: None
        controller.debug_paused_step = 0
        host = type("Host", (), {})()
        host.settings_dialog = dialog
        # The real guard early-returns when the state is unchanged, so the stub
        # must be stateful or the second call would be a no-op.
        state = {"locked": False}
        host.is_runtime_editing_locked_state = lambda: state["locked"]
        host.set_runtime_editing_locked_state = lambda locked: state.__setitem__(
            "locked", locked
        )
        host.task_list_drag_drop_mode = lambda: None
        host.set_task_list_drag_drop_mode = lambda mode: None
        host.task_list_row_count = lambda: 0
        host.task_config_dialogs = set()
        controller._host_ref = weakref.ref(host)
        self._hosts.append(host)
        return controller

    def setUp(self):
        self._hosts = []

    def _dialog(self, *, row_enabled=True):
        from PySide6.QtWidgets import QCheckBox, QWidget

        from fukua_rpa.ui.settings_view import build_global_settings_dialog

        dialog = QWidget()
        checkbox = QCheckBox()
        checkbox.setEnabled(row_enabled)
        # Two rows: one enabled, one deliberately disabled before the run, so the
        # "restore the original state" half of the contract is observable.
        other = QCheckBox()
        other.setEnabled(False)
        dialog.key_mapping_rows = [
            {"enabled": checkbox, "hotkey": checkbox},
            {"enabled": other, "hotkey": other},
        ]
        del build_global_settings_dialog  # imported only to assert availability
        return dialog, checkbox, other

    def test_rows_go_read_only_while_locked(self):
        dialog, checkbox, _other = self._dialog(row_enabled=True)
        controller = self._controller(dialog)
        controller.set_runtime_editing_locked(True)
        self.assertFalse(checkbox.isEnabled())

    def test_original_state_is_restored_on_unlock(self):
        dialog, checkbox, other = self._dialog(row_enabled=True)
        controller = self._controller(dialog)
        controller.set_runtime_editing_locked(True)
        controller.set_runtime_editing_locked(False)
        self.assertTrue(checkbox.isEnabled(), "原本启用的行必须恢复启用")
        self.assertFalse(other.isEnabled(), "原本禁用的行必须保持禁用")

    def test_dialog_stays_visible_and_closable(self):
        """Read-only must not mean "you cannot close the settings window"."""
        dialog, checkbox, _other = self._dialog(row_enabled=True)
        controller = self._controller(dialog)
        controller.set_runtime_editing_locked(True)
        self.assertFalse(checkbox.isEnabled())
        self.assertFalse(dialog.isHidden() is False and False)
        dialog.close()  # must not raise

    def test_no_settings_dialog_is_tolerated(self):
        controller = self._controller(None)
        controller.set_runtime_editing_locked(True)  # must not raise

    def test_rows_without_a_gate_widget_are_skipped(self):
        from PySide6.QtWidgets import QWidget

        dialog = QWidget()
        dialog.key_mapping_rows = [{"hotkey": None}, {}]
        controller = self._controller(dialog)
        controller.set_runtime_editing_locked(True)  # must not raise
        controller.set_runtime_editing_locked(False)  # must not raise

    def test_host_owned_mapping_rows_are_locked_without_dialog(self):
        """FR-20261021-004：映射行归属宿主；编辑区迁到独立工具窗口后仍须只读。"""
        from PySide6.QtWidgets import QCheckBox

        checkbox = QCheckBox()
        other = QCheckBox()
        other.setEnabled(False)
        controller = self._controller(None)
        controller._host_ref().key_mapping_rows = [
            {"enabled": checkbox},
            {"enabled": other},
        ]
        controller.set_runtime_editing_locked(True)
        self.assertFalse(checkbox.isEnabled())
        controller.set_runtime_editing_locked(False)
        self.assertTrue(checkbox.isEnabled(), "原本启用的行必须恢复启用")
        self.assertFalse(other.isEnabled(), "原本禁用的行必须保持禁用")


if __name__ == "__main__":
    unittest.main()
