"""连点器局部停止、输入互斥与窗口关闭回归测试。"""

from __future__ import annotations

import os
import threading
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from fukua_rpa.ui.auto_clicker_core import _AutoClickerRunner
from fukua_rpa.ui.auto_clicker_mixins.lifecycle import RunnerLifecycleMixin
from fukua_rpa.ui.auto_clicker_tool import AutoClickerToolWindow
from fukua_rpa.ui.controllers.execution_controller import ExecutionController
from fukua_rpa.ui.controllers.hotkey_controller import HotkeyController
from fukua_rpa.ui.controllers.mapping_controller import MappingController
from fukua_rpa.win32_api import (
    HOTKEY_ID_AUTO_CLICKER_START,
    HOTKEY_ID_AUTO_CLICKER_STOP,
    HOTKEY_ID_PROFILE_START_BASE,
    HOTKEY_ID_PROFILE_STOP_BASE,
)


class AutoClickerWindowTests(unittest.TestCase):
    """The tool owns a local stop event and never rewrites engine state."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_window(self):
        engine = mock.MagicMock()
        engine.lifecycle.is_active = False
        engine.check_stop_flag.return_value = False
        window = AutoClickerToolWindow(parent=None, engine=engine)
        self.addCleanup(window.deleteLater)
        return window, engine

    def test_close_event_requests_stop_and_waits_for_worker(self):
        window, _engine = self._make_window()
        worker = mock.MagicMock()
        worker.isRunning.return_value = True
        worker.wait.return_value = True
        window._thread = worker

        with mock.patch("fukua_rpa.ui.auto_clicker_tool.QDialog.closeEvent"):
            window.closeEvent(mock.MagicMock())

        self.assertTrue(window._stop_event.is_set())
        worker.wait.assert_called_once_with(2000)

    def test_close_event_timeout_releases_injected_inputs(self):
        """FR-005：关闭超时必须执行统一输入释放（设计基准 :253-254 / #87）。

        工作线程卡在中间状态（长按保持中）时，逐次 try/finally 尚未执行，
        超时分支若不补一次 release，鼠标键或键盘键会残留按下。
        """

        window, engine = self._make_window()
        worker = mock.MagicMock()
        worker.isRunning.return_value = True
        worker.wait.return_value = False  # 2 秒内有界等待超时
        window._thread = worker

        with mock.patch("fukua_rpa.ui.auto_clicker_tool.QDialog.closeEvent"):
            window.closeEvent(mock.MagicMock())

        engine.release_injected_inputs.assert_called_once_with()

    def test_close_event_success_does_not_force_release(self):
        """对照组：线程正常结束时不应额外调用释放（release 幂等，但不得产生副作用）。"""

        window, engine = self._make_window()
        worker = mock.MagicMock()
        worker.isRunning.return_value = True
        worker.wait.return_value = True
        window._thread = worker

        with mock.patch("fukua_rpa.ui.auto_clicker_tool.QDialog.closeEvent"):
            window.closeEvent(mock.MagicMock())

        engine.release_injected_inputs.assert_not_called()

    def test_stale_thread_finished_does_not_touch_new_thread(self):
        # FR-20260809-1502: 旧线程的 finished 信号若在新线程启动后才被处理，
        # _on_thread_finished 必须按 sender 识别——不得删除/清空新线程（否则
        # is_running 失真、极端情况重复启动）。
        window, _engine = self._make_window()
        old_thread = QThread()
        new_thread = QThread()
        self.addCleanup(old_thread.deleteLater)
        self.addCleanup(new_thread.deleteLater)

        window._thread = new_thread  # 模拟热键已启动新线程
        old_thread.finished.connect(window._on_thread_finished)
        old_thread.finished.emit()  # 旧线程的 finished 迟到

        self.assertIs(window._thread, new_thread, "旧线程迟到 finished 不得清空新线程")

        # 新线程自己的 finished 正常清理。
        new_thread.finished.connect(window._on_thread_finished)
        new_thread.finished.emit()
        self.assertIsNone(window._thread)

    def test_thread_error_state_survives_finished(self):
        # FR-20260811-D13-001: error 先 emit、finished 后 emit 时，_on_thread_finished
        # 不得把"运行出错"重置为"已停止"/idle（错误详情对用户丢失）。
        window, _engine = self._make_window()
        window._thread = mock.MagicMock()
        window._thread_error_occurred = False

        window._on_thread_error("boom")
        self.assertTrue(window._thread_error_occurred)
        window._on_thread_finished()

        self.assertEqual(window.status_label.text(), "运行出错: boom")
        self.assertEqual(window.banner_label.text(), "运行出错")
        # 按钮仍恢复可用（可重新启动）。
        self.assertTrue(window.start_btn.isEnabled())

    def test_normal_finished_resets_to_stopped(self):
        # 无错误时 finished 仍重置为"已停止"/idle（原语义）。
        window, _engine = self._make_window()
        window._thread = mock.MagicMock()
        window._thread_error_occurred = False

        window._on_thread_finished()

        self.assertEqual(window.status_label.text(), "已停止")
        # 错误标志复位供下次启动。
        self.assertFalse(window._thread_error_occurred)

    def test_stale_thread_error_does_not_stop_new_thread(self):
        # P3: 旧线程 error 迟到（热键已启动新线程）时，_on_thread_error 不得
        # set 停止事件误停新线程（与 _on_thread_finished 的 sender 防护对称）。
        window, _engine = self._make_window()
        old_thread = _AutoClickerRunner(mock.MagicMock(), {}, threading.Event())
        new_thread = _AutoClickerRunner(mock.MagicMock(), {}, threading.Event())
        self.addCleanup(old_thread.deleteLater)
        self.addCleanup(new_thread.deleteLater)

        window._thread = new_thread  # 新线程已启动
        window._thread_error_occurred = False  # 模拟启动路径已复位
        window._stop_event = mock.MagicMock()
        old_thread.error.connect(window._on_thread_error)
        old_thread.error.emit("stale boom")  # 旧线程 error 迟到

        window._stop_event.set.assert_not_called()
        self.assertFalse(window._thread_error_occurred)
        self.assertIs(window._thread, new_thread)

    def test_close_event_does_not_wait_for_idle_worker(self):
        window, _engine = self._make_window()
        worker = mock.MagicMock()
        worker.isRunning.return_value = False
        window._thread = worker

        with mock.patch("fukua_rpa.ui.auto_clicker_tool.QDialog.closeEvent"):
            window.closeEvent(mock.MagicMock())

        worker.wait.assert_not_called()

    def test_local_stop_does_not_assign_engine_stop_requested(self):
        window, engine = self._make_window()
        engine.stop_requested = "unchanged"

        window.request_stop()

        self.assertTrue(window._stop_event.is_set())
        self.assertEqual(engine.stop_requested, "unchanged")

    def test_runner_injects_tool_local_stop_predicate(self):
        engine = mock.MagicMock()
        stop_event = threading.Event()
        runner = _AutoClickerRunner(engine, {}, stop_event)

        runner.run()

        kwargs = engine._execute_auto_clicker.call_args.kwargs
        self.assertTrue(callable(kwargs["stop_requested"]))
        self.assertFalse(kwargs["stop_requested"]())
        stop_event.set()
        self.assertTrue(kwargs["stop_requested"]())


class _ExecutionHost:
    def __init__(self):
        self.engine = mock.MagicMock()
        self.engine.is_running = False
        self.run_controller = mock.MagicMock()
        self.run_controller.is_active = False
        self.messages = []
        self.stop_calls = 0

    def auto_clicker_is_running(self):
        return True

    def stop_auto_clicker(self):
        self.stop_calls += 1
        return True

    def show_information_message(self, title, message):
        self.messages.append((title, message))

    def append_application_log(self, message, _level):
        self.messages.append(("log", message))


class _IndicatorStub:
    def isChecked(self):
        return False


class _MappingHost:
    """Minimal host that routes mapping dispatch the way RPAWindow does.

    FR-001: ``execute_key_mapping`` must delegate back into the controller so
    the controller's input-ownership gate is actually exercised instead of
    being bypassed by the stub.
    """

    def __init__(self, clicker_running, session_active=False):
        self.engine = mock.MagicMock()
        self.engine.is_running = False
        self.run_controller = mock.MagicMock()
        self.run_controller.is_active = session_active
        self._clicker_running = clicker_running
        self.controller = None
        self.messages = []
        self.click_indicator_chk = _IndicatorStub()

    def auto_clicker_is_running(self):
        return self._clicker_running

    def active_key_mappings(self):
        return [{
            "index": 2,
            "normalized": "f5",
            "hotkey": "F5",
            "coord": (10, 20),
            "action": "左键单击",
        }]

    def execute_key_mapping(self, index):
        self.controller.execute_key_mapping(index)

    def perform_key_mapping_click(
        self, x, y, button, click_times, window_binding=None
    ):
        # 真实宿主把点击转发回控制器；桩保持同一路由，便于在控制器层观察注入。
        return self.controller.perform_key_mapping_click(
            x, y, button, click_times, window_binding
        )

    def show_click_indicator_overlay(self, payload):
        self.messages.append(("overlay", payload))

    def append_application_log(self, message, _level=None, private_msg=None):
        self.messages.append(("log", message))


def _make_mapping_host(clicker_running, session_active=False):
    host = _MappingHost(clicker_running, session_active)
    controller = MappingController(host)
    host.controller = controller
    return host, controller


class AutoClickerInputOwnershipTests(unittest.TestCase):
    """All script launch paths reject an active standalone input owner."""

    def test_common_manual_launch_boundary_rejects_running_clicker(self):
        host = _ExecutionHost()
        controller = ExecutionController(host)

        started = controller._launch_prepared_tasks([], {}, "test")

        self.assertFalse(started)
        self.assertIn("连点器正在运行", host.messages[0][1])
        host.run_controller.start.assert_not_called()

    def test_common_scheduled_launch_boundary_logs_instead_of_showing_dialog(self):
        host = _ExecutionHost()
        controller = ExecutionController(host)

        started = controller._launch_prepared_tasks(
            [], {}, "scheduled", run_meta={"source": "scheduled"}
        )

        self.assertFalse(started)
        self.assertEqual(host.messages[0][0], "log")
        self.assertIn("本次未启动", host.messages[0][1])

    def test_script_stop_route_does_not_take_over_clicker_stop(self):
        host = _ExecutionHost()
        controller = ExecutionController(host)

        controller.stop_task()

        self.assertEqual(host.stop_calls, 0)
        host.run_controller.stop.assert_not_called()

    def test_hotkey_activity_gate_includes_running_clicker(self):
        host = _ExecutionHost()

        controller = HotkeyController(host)

        self.assertTrue(controller._input_operation_is_running())

    def test_registered_mapping_is_suppressed_while_clicker_runs(self):
        host = _ExecutionHost()
        controller = HotkeyController(host)
        controller.mapping_ids[9001] = 2

        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.schedule_widget_callback"
        ) as schedule:
            handled = controller.handle_native_hotkey(9001)

        self.assertTrue(handled)
        schedule.assert_not_called()

    def test_bare_key_mapping_hook_is_suppressed_while_clicker_runs(self):
        # FR-001：裸键映射的 Hook 分发路径此前没有任何输入所有权门，
        # 连点器运行期间仍会注入真实点击（与注册热键路径不一致）。
        host, controller = _make_mapping_host(clicker_running=True)

        with mock.patch.object(controller, "perform_key_mapping_click") as click:
            controller.execute_key_mapping_by_hotkey("f5")

        click.assert_not_called()
        self.assertTrue(host.messages)

    def test_bare_key_mapping_hook_is_suppressed_while_script_session_is_active(self):
        # FR-001 返修（08_修复复验 §3.12）：新门必须与注册热键路径**等价**，
        # 即同时覆盖 run_controller.is_active。"会话活跃但引擎空闲"是可达状态
        # （进程内 Runner 的收尾窗口；独立 Runner 模式下为整个运行期）。
        host, controller = _make_mapping_host(
            clicker_running=False, session_active=True
        )

        with mock.patch.object(controller, "perform_key_mapping_click") as click:
            controller.execute_key_mapping_by_hotkey("f5")

        click.assert_not_called()
        self.assertTrue(host.messages)

    def test_mapping_paths_agree_across_input_ownership_states(self):
        # FR-001 返修（08_修复复验 §3.12）：注册热键路径与裸键 Hook 路径必须对同一
        # 输入所有权状态给出一致判定——注册路径谓词为真时，Hook 路径也不得注入。
        cases = (
            ("全部空闲", False, False),
            ("连点器运行中", True, False),
            ("会话活跃但引擎空闲", False, True),
        )
        for label, clicker_running, session_active in cases:
            with self.subTest(case=label):
                host, controller = _make_mapping_host(
                    clicker_running=clicker_running,
                    session_active=session_active,
                )
                registered_gate = HotkeyController(
                    host
                )._input_operation_is_running()
                with mock.patch.object(
                    controller, "perform_key_mapping_click", return_value="真实鼠标点击"
                ) as click:
                    controller.execute_key_mapping_by_hotkey("f5")
                injected = click.call_count > 0
                self.assertEqual(
                    registered_gate,
                    not injected,
                    f"{label}: 注册路径谓词={registered_gate}，Hook 路径注入={injected}",
                )

    def test_bare_key_mapping_hook_injects_when_no_clicker_runs(self):
        # 反向对照：无连点器运行时映射必须恢复注入（门不得永久吞掉映射）。
        host, controller = _make_mapping_host(clicker_running=False)

        with mock.patch.object(
            controller, "perform_key_mapping_click", return_value="真实鼠标点击"
        ) as click:
            controller.execute_key_mapping_by_hotkey("f5")

        click.assert_called_once()


class _TextStub:
    def __init__(self, value):
        self._value = value

    def text(self):
        return self._value


class _ProfileHotkeyStub:
    def __init__(self):
        self.start_calls = []
        self.stop_calls = []

    @staticmethod
    def active_bindings():
        return [
            {
                "slot_id": "slot-a",
                "profile_id": "profile-a",
                "profile_name": "方案 A",
                "start_hotkey": "Ctrl+Alt+1",
                "stop_hotkey": "Ctrl+Alt+2",
            }
        ]

    def start_profile(self, profile_id, slot_id=""):
        self.start_calls.append((profile_id, slot_id))

    def stop_profile(self, profile_id):
        self.stop_calls.append(profile_id)


class _GlobalHotkeyHost:
    HOTKEY_POLL_INTERVAL_MS = 30

    def __init__(self):
        self.engine = mock.Mock(is_running=False)
        self.run_controller = mock.Mock(is_active=False)
        self.hotkey_start_edit = _TextStub("F9")
        self.hotkey_stop_edit = _TextStub("F10")
        self.profile_hotkey_controller = _ProfileHotkeyStub()
        self.clicker_running = False
        self.clicker_start_calls = 0
        self.clicker_stop_calls = 0
        self.clicker_toggle_calls = 0
        self.clicker_bindings = {"start_hotkey": "F6", "stop_hotkey": "F7"}

    @staticmethod
    def winId():
        return 123

    @staticmethod
    def active_key_mappings():
        return []

    def auto_clicker_hotkey_bindings(self):
        return dict(self.clicker_bindings)

    def auto_clicker_is_running(self):
        return self.clicker_running

    def start_auto_clicker_from_hotkey(self):
        self.clicker_start_calls += 1

    def stop_auto_clicker(self):
        self.clicker_stop_calls += 1

    def toggle_auto_clicker_from_hotkey(self):
        self.clicker_toggle_calls += 1
        return True


class GlobalToolHotkeyTests(unittest.TestCase):
    def test_clicker_and_profile_bindings_use_win32_global_registration(self):
        host = _GlobalHotkeyHost()
        controller = HotkeyController(host)
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32"
        ) as user32:
            user32.RegisterHotKey.return_value = True
            user32.UnregisterHotKey.return_value = True
            self.assertTrue(controller.register_global_hotkeys())

        self.assertEqual(
            controller.auto_clicker_ids,
            {
                HOTKEY_ID_AUTO_CLICKER_START: "start",
                HOTKEY_ID_AUTO_CLICKER_STOP: "stop",
            },
        )
        self.assertEqual(
            controller.profile_start_ids[HOTKEY_ID_PROFILE_START_BASE],
            ("profile-a", "slot-a"),
        )
        self.assertEqual(
            controller.profile_stop_ids[HOTKEY_ID_PROFILE_STOP_BASE],
            "profile-a",
        )

    def test_optional_registration_failure_falls_back_instead_of_going_silent(self):
        host = _GlobalHotkeyHost()
        controller = HotkeyController(host)
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32"
        ) as user32:
            user32.RegisterHotKey.side_effect = [True, True, False]
            user32.UnregisterHotKey.return_value = True
            self.assertFalse(controller.register_global_hotkeys())

        self.assertFalse(controller.registered)
        self.assertEqual(controller.auto_clicker_ids, {})

    def test_native_clicker_start_is_dispatched_without_window_focus(self):
        host = _GlobalHotkeyHost()
        controller = HotkeyController(host)
        controller.auto_clicker_ids[HOTKEY_ID_AUTO_CLICKER_START] = "start"
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.schedule_widget_callback",
            side_effect=lambda owner, _delay, callback: callback(owner),
        ):
            self.assertTrue(
                controller.handle_native_hotkey(HOTKEY_ID_AUTO_CLICKER_START)
            )

        self.assertEqual(host.clicker_start_calls, 1)

    def test_same_key_binding_registers_single_toggle_hotkey(self):
        """同键启停：只注册一条 toggle 绑定，不再注册独立停止热键。"""
        host = _GlobalHotkeyHost()
        host.clicker_bindings = {
            "start_hotkey": "F6",
            "stop_hotkey": "F6",
            "toggle": True,
        }
        controller = HotkeyController(host)
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32"
        ) as user32:
            user32.RegisterHotKey.return_value = True
            user32.UnregisterHotKey.return_value = True
            self.assertTrue(controller.register_global_hotkeys())

        self.assertEqual(
            controller.auto_clicker_ids,
            {HOTKEY_ID_AUTO_CLICKER_START: "toggle"},
        )
        self.assertNotIn(HOTKEY_ID_AUTO_CLICKER_STOP, controller.auto_clicker_ids)

    def test_native_same_key_toggle_dispatches_once_even_while_clicker_runs(self):
        host = _GlobalHotkeyHost()
        controller = HotkeyController(host)
        controller.auto_clicker_ids[HOTKEY_ID_AUTO_CLICKER_START] = "toggle"
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.schedule_widget_callback",
            side_effect=lambda owner, _delay, callback: callback(owner),
        ):
            self.assertTrue(
                controller.handle_native_hotkey(HOTKEY_ID_AUTO_CLICKER_START)
            )
            # 连点器自身运行时也必须能收到同键停止请求（不能叠加输入所有权门槛）。
            host.clicker_running = True
            self.assertTrue(
                controller.handle_native_hotkey(HOTKEY_ID_AUTO_CLICKER_START)
            )

        self.assertEqual(host.clicker_toggle_calls, 2)
        self.assertEqual(host.clicker_start_calls, 0)
        self.assertEqual(host.clicker_stop_calls, 0)

    def test_polling_same_key_toggle_fires_on_press_edge_only(self):
        host = _GlobalHotkeyHost()
        host.clicker_bindings = {
            "start_hotkey": "F6",
            "stop_hotkey": "F6",
            "toggle": True,
        }
        controller = HotkeyController(host)

        def _only_f6_down(parsed):
            return bool(parsed) and parsed.get("display") == "F6"

        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.hotkey_is_down",
            side_effect=_only_f6_down,
        ):
            controller.check_hotkey()
            self.assertEqual(host.clicker_toggle_calls, 1, "按下沿应触发一次切换")
            controller.check_hotkey()
            self.assertEqual(host.clicker_toggle_calls, 1, "保持按住不得连续切换")

        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.hotkey_is_down",
            return_value=False,
        ):
            controller.check_hotkey()

        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.hotkey_is_down",
            side_effect=_only_f6_down,
        ):
            controller.check_hotkey()
            self.assertEqual(
                host.clicker_toggle_calls, 2, "松开后再次按下应再次切换"
            )


class ClickerToggleStateTests(unittest.TestCase):
    """同键 toggle 的四态状态机（空闲/运行/启动中/停止中）。"""

    class _Stub:
        def __init__(self):
            self._thread = None
            self._stop_event = threading.Event()
            self.start_calls = 0
            self.stop_calls = 0

        def _on_start(self):
            self.start_calls += 1
            thread = mock.Mock()
            thread.isRunning.return_value = True
            self._thread = thread

        def request_stop(self):
            self.stop_calls += 1
            self._stop_event.set()

    def _toggle(self, stub):
        return RunnerLifecycleMixin.toggle_from_hotkey(stub)

    def test_idle_press_starts(self):
        stub = self._Stub()
        self.assertTrue(self._toggle(stub))
        self.assertEqual(stub.start_calls, 1)
        self.assertIsNotNone(stub._thread)

    def test_running_press_requests_stop_and_repeat_is_ignored(self):
        stub = self._Stub()
        thread = mock.Mock()
        thread.isRunning.return_value = True
        stub._thread = thread
        self.assertTrue(self._toggle(stub))
        self.assertEqual(stub.stop_calls, 1)
        # 停止中重复按下：不得再次请求，也不得排队第二个实例。
        self.assertTrue(self._toggle(stub))
        self.assertEqual(stub.stop_calls, 1)
        self.assertEqual(stub.start_calls, 0)

    def test_starting_press_cancels_instead_of_queuing_second_instance(self):
        stub = self._Stub()
        thread = mock.Mock()
        thread.isRunning.return_value = False
        stub._thread = thread
        self.assertTrue(self._toggle(stub))
        self.assertEqual(stub.start_calls, 0)
        self.assertEqual(stub.stop_calls, 1)

    def test_press_can_start_again_after_stop_finished(self):
        stub = self._Stub()
        thread = mock.Mock()
        thread.isRunning.return_value = True
        stub._thread = thread
        self._toggle(stub)
        # 线程真正结束、finished 已处理后恢复空闲。
        stub._thread = None
        stub._stop_event.clear()
        self.assertTrue(self._toggle(stub))
        self.assertEqual(stub.start_calls, 1)


class AutoClickerInputLockTests(unittest.TestCase):
    """ENG-FR-004：连点器步骤的实际输入必须经过统一输入锁（ARCHITECTURE #77）。

    其他输入路径（mouse.py 的点击/拖拽/后台点击）都在调用点持
    ``_input_action_lock``；旧版工作流保留的连点器步骤（指令 28.0）此前完全
    不持锁，可与附属点击调度线程的注入交错。
    """

    def test_auto_clicker_click_holds_input_action_lock(self):
        from fukua_rpa.engine import RPAEngine

        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine(defer_backends=True)
        acquisitions = []
        inner = engine._input_action_lock

        class _TrackingRLock:
            def __enter__(self):
                acquisitions.append(1)
                return inner.__enter__()

            def __exit__(self, *exc_info):
                return inner.__exit__(*exc_info)

        engine._input_action_lock = _TrackingRLock()
        engine.input_backend.mouse_down = mock.Mock(return_value=True)
        engine.input_backend.mouse_up = mock.Mock()

        result = engine._auto_clicker_click(
            10,
            10,
            "left",
            0.0,
            0.0,
            stop_requested=lambda: False,
            move_first=False,
        )

        self.assertEqual(result, True)
        self.assertGreaterEqual(len(acquisitions), 1)
        engine.input_backend.mouse_down.assert_called_once()
        engine.input_backend.mouse_up.assert_called_once()


class _MappingHotkeyHost(_GlobalHotkeyHost):
    """Host exposing one safe-global key mapping so mapping registration is exercised."""

    MAPPING_HOTKEY = "ctrl+f1"
    MAPPING_ID = 9001

    def __init__(self):
        super().__init__()
        self.mapping_mode_chk = None
        self.mapping_executed: list[int] = []

    def active_key_mappings(self):
        return [
            {
                "id": self.MAPPING_ID,
                "index": 0,
                "hotkey": self.MAPPING_HOTKEY,
                "normalized": self.MAPPING_HOTKEY,
                "modifiers": 0x0002,
                "vk": 0x70,
                "safe_global": True,
            }
        ]

    def execute_key_mapping(self, index):
        self.mapping_executed.append(index)


class MappingHotkeyVisibilityTests(unittest.TestCase):
    """REVIEW-20260913-001 FR-006：按键映射注册失败不得被伪装成成功。

    ``register_global_hotkeys`` 此前对映射注册失败只写一条日志，随后仍置
    ``registered = True`` 并返回 True，导致 ``refresh_hotkey_backend`` 停掉轮询
    兜底定时器——该映射静默失效，违反设计基准「执行结果可解释，失败、停止和
    后端限制不被伪装成成功」。连点器与方案热键在同样失败时都会返回 False。
    """

    def test_mapping_registration_failure_is_recorded_and_not_silent(self):
        """返修轮 01（08 §4 方案 b）：映射注册失败必须可见且可查，但**不**让整个
        热键后端失败回退到轮询。

        形态依据：ARCHITECTURE #90 只要求"工具/方案热键"失败时切换轮询，未点名按键
        映射；同一循环内的"与启停热键冲突"和"裸键"两个分支也都是记录日志后
        ``continue``。因此本分支保持后端继续使用 ``RegisterHotKey``，把失败记入
        ``mapping_registration_failures`` 并给出可操作的日志，而不是静默丢弃。
        """
        host = _MappingHotkeyHost()
        controller = HotkeyController(host)

        def fake_register(_hwnd, hotkey_id, _modifiers, _vk):
            # 除该映射外全部注册成功，模拟"组合键已被其他程序占用"。
            return hotkey_id != host.MAPPING_ID

        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32"
        ) as user32, mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.write_log"
        ) as write_log:
            user32.RegisterHotKey.side_effect = fake_register
            user32.UnregisterHotKey.return_value = True
            registered = controller.register_global_hotkeys()

        # 后端仍以 RegisterHotKey 工作，不因单个映射失败而整体回退轮询。
        self.assertTrue(registered)
        self.assertTrue(controller.registered)
        # 失败的映射既未进入可派发表，也没有被静默丢弃——它被显式记录。
        self.assertNotIn(host.MAPPING_ID, controller.mapping_ids)
        self.assertEqual(controller.mapping_registration_failures, {0: host.MAPPING_HOTKEY})
        # 必须给出可见日志，且日志不得承诺未启用的轮询兜底。
        messages = [str(call.args[0]) for call in write_log.call_args_list if call.args]
        self.assertTrue(
            any("注册失败" in message for message in messages),
            f"映射注册失败必须产生可见日志，实际={messages}",
        )
        self.assertFalse(
            any("轮询" in message for message in messages),
            "本形态不启用轮询兜底，日志不得声称已回退轮询",
        )

    def test_failure_exit_clears_the_stale_mapping_failure_notice(self):
        """FR-20261005-401: 失败出口必须清空过期的失败清单。"""
        host = _MappingHotkeyHost()
        controller = HotkeyController(host)
        controller.mapping_registration_failures = {0: "STALE-HOTKEY"}

        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32"
        ) as user32, mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.write_log"
        ):
            # 启动/停止热键本身注册失败 → 后端回退轮询。
            user32.RegisterHotKey.return_value = False
            user32.UnregisterHotKey.return_value = True
            registered = controller.register_global_hotkeys()

        self.assertFalse(registered)
        self.assertFalse(controller.registered)
        self.assertEqual(controller.mapping_registration_failures, {})

    def test_exception_exit_clears_the_stale_mapping_failure_notice(self):
        """FR-20261005-401: 异常出口同样不得保留过期失败清单。"""
        host = _MappingHotkeyHost()
        controller = HotkeyController(host)
        controller.mapping_registration_failures = {0: "STALE-HOTKEY"}

        with mock.patch.object(
            controller, "hotkey_hwnd", side_effect=RuntimeError("boom")
        ), mock.patch("fukua_rpa.ui.controllers.hotkey_controller.write_log"):
            registered = controller.register_global_hotkeys()

        self.assertFalse(registered)
        self.assertEqual(controller.mapping_registration_failures, {})

    def test_mapping_registration_failure_record_clears_on_later_success(self):
        """失败记录必须可自愈：该映射下次注册成功时清除，避免过期状态误导用户。"""
        host = _MappingHotkeyHost()
        controller = HotkeyController(host)

        def failing_register(_hwnd, hotkey_id, _modifiers, _vk):
            return hotkey_id != host.MAPPING_ID

        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32"
        ) as user32, mock.patch("fukua_rpa.ui.controllers.hotkey_controller.write_log"):
            user32.RegisterHotKey.side_effect = failing_register
            user32.UnregisterHotKey.return_value = True
            controller.register_global_hotkeys()
        self.assertIn(0, controller.mapping_registration_failures)

        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32"
        ) as user32, mock.patch("fukua_rpa.ui.controllers.hotkey_controller.write_log"):
            user32.RegisterHotKey.return_value = True
            user32.UnregisterHotKey.return_value = True
            controller.register_global_hotkeys()

        self.assertEqual(controller.mapping_registration_failures, {})
        self.assertIn(host.MAPPING_ID, controller.mapping_ids)

    def test_tool_hotkey_failure_still_falls_back_to_polling(self):
        """ARCHITECTURE #90 的既有要求不得因本返修而改变：工具/方案热键注册失败
        时后端必须整体回退到轮询（由既有用例覆盖 `register_global_hotkeys`
        返回 False）。这里补一条相邻断言，固定"映射失败"与"工具热键失败"的差异。"""
        host = _MappingHotkeyHost()
        controller = HotkeyController(host)

        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.user32"
        ) as user32, mock.patch("fukua_rpa.ui.controllers.hotkey_controller.write_log"):
            # 让连点器热键（第 3 次注册）失败 → 必须整体失败。
            user32.RegisterHotKey.side_effect = [True, True, False]
            user32.UnregisterHotKey.return_value = True
            registered = controller.register_global_hotkeys()

        self.assertFalse(registered)
        self.assertFalse(controller.registered)

    def test_held_mapping_key_does_not_refire_after_run_ends(self):
        """运行期按住的映射键不得在运行结束后补触发一次。

        注册路径在运行期对映射"消费但不执行"；轮询路径此前在运行期跳过整个
        映射检测块，使 ``mapping_poll_pressed`` 被写成空集，运行结束后仍按住的
        组合键会满足边沿条件并补触发一次用户未意图的映射动作。
        """
        host = _MappingHotkeyHost()
        controller = HotkeyController(host)

        def only_mapping_down(parsed):
            return isinstance(parsed, dict) and parsed.get("text") == host.MAPPING_HOTKEY

        # 运行期：只记录按下，不执行映射。
        host.engine.is_running = True
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.hotkey_is_down",
            side_effect=only_mapping_down,
        ):
            controller.check_hotkey()
        self.assertEqual(host.mapping_executed, [], "运行期不得执行映射")

        # 运行结束但组合键仍按住：不得补触发。
        host.engine.is_running = False
        host.run_controller.is_active = False
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.hotkey_is_down",
            side_effect=only_mapping_down,
        ):
            controller.check_hotkey()

        self.assertEqual(
            host.mapping_executed,
            [],
            "运行结束后仍按住的映射键不得补触发一次",
        )

    def test_fresh_mapping_press_after_run_still_fires_once(self):
        """修复不得误伤正常语义：运行结束后新按下的映射键仍应触发一次。"""
        host = _MappingHotkeyHost()
        controller = HotkeyController(host)

        def only_mapping_down(parsed):
            return isinstance(parsed, dict) and parsed.get("text") == host.MAPPING_HOTKEY

        # 运行期该键未按下。
        host.engine.is_running = True
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.hotkey_is_down",
            return_value=False,
        ):
            controller.check_hotkey()

        # 运行结束后用户新按下该映射键。
        host.engine.is_running = False
        host.run_controller.is_active = False
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.hotkey_is_down",
            side_effect=only_mapping_down,
        ):
            controller.check_hotkey()

        self.assertEqual(host.mapping_executed, [0])


if __name__ == "__main__":
    unittest.main()
