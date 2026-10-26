"""R14 审查指出的「无测试守护」项，逐项补上守护。

覆盖：29.0 脱敏（清空 input_events + 停用）、`validate_cache` 空清单、`closeEvent`
尊重 `event.ignore()`、导入后本地映射保持、序号三位数不裁切、采样探针返回副本内模块。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

from fukua_rpa.constants import TASK_TYPE_INPUT_TIMELINE  # noqa: E402
from fukua_rpa.workflow_security import sanitize_profile_for_sharing  # noqa: E402
from scripts.startup_measurement import validate_cache  # noqa: E402
from tests.qt_test_support import dispose_widget  # noqa: E402

APP = QApplication.instance() or QApplication([])


class SanitizeTimelineTests(unittest.TestCase):
    def _profile(self, events, value="1,2"):
        return {
            "tasks": [
                {
                    "type": TASK_TYPE_INPUT_TIMELINE,
                    "value": value,
                    "input_events": list(events),
                    "step_id": "s1",
                }
            ],
            "key_mappings": [],
            "scan_regions": [],
        }

    def test_events_cleared_and_step_disabled(self):
        result = sanitize_profile_for_sharing(self._profile([{"kind": "key"}]))
        step = result.profile["tasks"][0]
        self.assertEqual(step.get("input_events"), [])
        self.assertTrue(step.get("step_disabled"))

    def test_empty_value_still_disabled(self):
        result = sanitize_profile_for_sharing(self._profile([], value=""))
        step = result.profile["tasks"][0]
        self.assertTrue(step.get("step_disabled"))
        self.assertEqual(step.get("input_events"), [])


class CacheNoModulesTests(unittest.TestCase):
    def test_empty_list_is_not_ok(self):
        result = validate_cache([])
        self.assertFalse(result.ok)
        self.assertIn("no_modules", result.reasons)


class CloseEventTests(unittest.TestCase):
    def test_accepted_close_quits_and_closes_tools(self):
        from fukua_rpa.ui.main_window import RPAWindow

        temp_dir = tempfile.mkdtemp()
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.HotkeyController.register_global_hotkeys",
            lambda self: True,
        ), mock.patch.object(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok), \
           mock.patch.object(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok):
            window = RPAWindow(base_dir=temp_dir)
            tool = mock.Mock()
            window._key_mapping_tool_window = tool
            event = QCloseEvent()
            event.accept()
            with mock.patch.object(
                type(window), "worker", new_callable=mock.PropertyMock, return_value=None
            ), mock.patch.object(QApplication, "quit") as quit_mock:
                window.closeEvent(event)
            quit_mock.assert_called_once()
            tool.close.assert_called_once()



    def test_stale_close_after_worker_flag_does_not_block_close(self):
        """worker 已结束但标记残留时，仍必须能正常关闭（R14 复验 C）。"""
        from fukua_rpa.ui.main_window import RPAWindow

        temp_dir = tempfile.mkdtemp()
        with mock.patch(
            "fukua_rpa.ui.controllers.hotkey_controller.HotkeyController.register_global_hotkeys",
            lambda self: True,
        ), mock.patch.object(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok),            mock.patch.object(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok):
            window = RPAWindow(base_dir=temp_dir)
            tool = mock.Mock()
            window._key_mapping_tool_window = tool
            window._close_after_worker = True
            finished = mock.Mock()
            finished.isRunning.return_value = False
            event = QCloseEvent()
            event.accept()
            with mock.patch.object(
                type(window), "worker", new_callable=mock.PropertyMock, return_value=finished
            ), mock.patch.object(QApplication, "quit") as quit_mock:
                window.closeEvent(event)
            quit_mock.assert_called_once()
            tool.close.assert_called_once()
            self.assertFalse(window._close_after_worker)


class ImportKeepsMappingsTests(unittest.TestCase):
    def test_sanitized_profile_needs_shared_mappings(self):
        from fukua_rpa.ui.controllers.workflow_transfer_controller import (
            needs_shared_mappings,
        )

        self.assertTrue(needs_shared_mappings({"key_mappings": []}))
        self.assertFalse(needs_shared_mappings({"key_mappings": [{"hotkey": "F7"}]}))
        self.assertFalse(needs_shared_mappings(None))


class IndexLabelWidthTests(unittest.TestCase):
    def test_three_digit_label_wider_than_two_digit(self):
        """三位数必须比两位数宽，否则会被裁切（原用例 no-op 也通过，属自证）。"""
        from fukua_rpa.ui.controllers.mapping_mixins.row_management import (
            _align_mapping_index_label,
        )

        two, three = QPushButton("映射99"), QPushButton("映射100")
        _align_mapping_index_label(two)
        _align_mapping_index_label(three)
        self.assertGreater(three.minimumWidth(), two.minimumWidth())


class TimelineCapabilityTests(unittest.TestCase):
    """裁定 ⑤：按 input_events 实际内容统计能力。"""

    def _capabilities(self, events):
        from fukua_rpa.constants import TASK_TYPE_INPUT_TIMELINE
        from fukua_rpa.workflow_security import analyze_workflow_capabilities

        result = analyze_workflow_capabilities(
            {"tasks": [{"type": TASK_TYPE_INPUT_TIMELINE, "input_events": events}]}
        )
        return result

    def test_keyboard_only_counts_keyboard(self):
        caps = self._capabilities([{"kind": "key"}])
        self.assertEqual(
            (caps.mouse_action_count, caps.keyboard_action_count), (0, 1)
        )

    def test_mouse_only_counts_mouse(self):
        caps = self._capabilities([{"kind": "move"}])
        self.assertEqual(
            (caps.mouse_action_count, caps.keyboard_action_count), (1, 0)
        )

    def test_mixed_counts_both_once(self):
        # kind 必须与播放器一致：key / mouse / move / wheel
        caps = self._capabilities([{"kind": "key"}, {"kind": "mouse"}])
        self.assertEqual(
            (caps.mouse_action_count, caps.keyboard_action_count), (1, 1)
        )
        self.assertEqual(caps.step_count, 1)

    def test_unknown_kind_not_counted_as_capability(self):
        """未知 kind 不得计入能力，也不得当作安全（R17 审查 ④）。"""
        caps = self._capabilities([{"kind": "key"}, {"kind": "click"}])
        self.assertEqual(
            (caps.mouse_action_count, caps.keyboard_action_count), (0, 1)
        )
        self.assertEqual(caps.unknown_action_steps, (1,))

    def test_timeline_still_dangerous(self):
        from fukua_rpa.constants import TASK_TYPE_INPUT_TIMELINE
        from fukua_rpa.workflow_security import DANGEROUS_ACTIONS

        self.assertIn(TASK_TYPE_INPUT_TIMELINE, DANGEROUS_ACTIONS)


class RuntimeModulesProbeTests(unittest.TestCase):
    def test_probe_reports_repo_modules(self):
        from scripts import run_startup_baseline as driver

        modules = driver.runtime_modules(Path(driver.ROOT), dict(os.environ))
        self.assertTrue(any("fukua_rpa" in item for item in modules))


class ForceExitTests(unittest.TestCase):
    """R16/R17 共同指出的缺口：force_exit 此前无任何自动化测试。"""

    def _host(self, engine=None, stop_raises=False):
        host = mock.Mock()
        host.run_controller = mock.Mock()
        host.run_controller.engine = engine
        if stop_raises:
            host.run_controller.stop.side_effect = RuntimeError("boom")
        return host

    def test_releases_inputs_then_exits(self):
        from fukua_rpa.ui.controllers import shutdown_controller as sc

        engine = mock.Mock()
        host = self._host(engine)
        with mock.patch.object(sc, "flush_logs", return_value=True), mock.patch.object(
            sc.os, "_exit"
        ) as exit_mock:
            sc.force_exit(host)
        engine.release_injected_inputs.assert_called_once()
        host.run_controller.stop.assert_called_once()
        exit_mock.assert_called_once_with(1)

    def test_release_failure_still_exits(self):
        from fukua_rpa.ui.controllers import shutdown_controller as sc

        engine = mock.Mock()
        engine.release_injected_inputs.side_effect = RuntimeError("lock")
        host = self._host(engine, stop_raises=True)
        with mock.patch.object(sc, "flush_logs", return_value=True), mock.patch.object(
            sc.os, "_exit"
        ) as exit_mock:
            sc.force_exit(host)
        exit_mock.assert_called_once_with(1)

    def test_flush_failure_falls_back_to_direct_write(self):
        from fukua_rpa.ui.controllers import shutdown_controller as sc

        host = self._host(mock.Mock())
        with mock.patch.object(sc, "flush_logs", return_value=False), mock.patch(
            "fukua_rpa.logging_service._append_direct"
        ) as direct, mock.patch.object(sc.os, "_exit"):
            sc.force_exit(host)
        direct.assert_called_once()


class LogQueueWriteFailureTests(unittest.TestCase):
    """真实队列路径：通道开启 → 入队 → worker 命中 writer → 注入 OSError → flush 判否。"""

    def setUp(self):
        import fukua_rpa.logging_service as ls

        self.ls = ls
        self._saved = dict(ls.GLOBAL_CONFIG)
        self.addCleanup(lambda: ls.GLOBAL_CONFIG.update(self._saved))

    def test_text_channel_failure_makes_flush_false(self):
        import time

        self.ls.update_log_config(log_to_file=True)
        self.ls.flush_logs(0.2)
        before = self.ls._WRITE_ERRORS
        with mock.patch("builtins.open", side_effect=OSError("disk")):
            self.ls.write_log("probe", category="application")
            time.sleep(0.3)
            self.assertGreater(self.ls._WRITE_ERRORS, before)
            self.assertFalse(self.ls.flush_logs(1.0))

    def test_disabled_channel_is_not_a_write_failure(self):
        """通道关闭不是写失败：flush 应为 True，且不得改动产品默认开关。"""
        import time

        self.ls.update_log_config(log_to_file=False)
        self.ls.flush_logs(0.2)
        before = self.ls._WRITE_ERRORS
        with mock.patch("builtins.open", side_effect=OSError("disk")):
            self.ls.write_log("probe", category="application")
            time.sleep(0.2)
        self.assertEqual(self.ls._WRITE_ERRORS, before)
        self.assertTrue(self.ls.flush_logs(0.5))


class ForceExitPromptTests(unittest.TestCase):
    """覆盖缺口：15s 提示与「运行中」分支此前无任何测试。"""

    def _host(self, running=True):
        host = mock.Mock()
        host.worker = mock.Mock()
        host.worker.isRunning.return_value = running
        return host

    def test_worker_finished_returns_without_prompt(self):
        from fukua_rpa.ui.controllers import shutdown_controller as sc

        host = self._host(running=False)
        with mock.patch.object(sc, "force_exit") as forced:
            sc.prompt_force_exit(host)
        forced.assert_not_called()

    def test_force_button_exits_when_worker_still_running(self):
        from PySide6.QtWidgets import QMessageBox

        from fukua_rpa.ui.controllers import shutdown_controller as sc

        host = self._host(running=True)
        with mock.patch.object(QMessageBox, "exec", lambda self: 0), mock.patch.object(
            QMessageBox, "clickedButton", lambda self: self.buttons()[1]
        ), mock.patch.object(sc, "force_exit") as forced:
            sc.prompt_force_exit(host)
        forced.assert_called_once()

    def test_wait_button_does_not_exit(self):
        from PySide6.QtWidgets import QMessageBox

        from fukua_rpa.ui.controllers import shutdown_controller as sc

        host = self._host(running=True)
        with mock.patch.object(QMessageBox, "exec", lambda self: 0), mock.patch.object(
            QMessageBox, "clickedButton", lambda self: self.buttons()[0]
        ), mock.patch.object(sc, "force_exit") as forced:
            sc.prompt_force_exit(host)
        forced.assert_not_called()

    def test_force_clicked_after_worker_finished_does_not_kill(self):
        """提示期间 worker 已结束：即使点了「强制退出」也不得强杀。"""
        from PySide6.QtWidgets import QMessageBox

        from fukua_rpa.ui.controllers import shutdown_controller as sc

        host = self._host(running=True)
        host.worker.isRunning.side_effect = [True, False]
        with mock.patch.object(QMessageBox, "exec", lambda self: 0), mock.patch.object(
            QMessageBox, "clickedButton", lambda self: self.buttons()[1]
        ), mock.patch.object(sc, "force_exit") as forced:
            sc.prompt_force_exit(host)
        forced.assert_not_called()

    def test_wait_button_reschedules_prompt(self):
        """继续等待必须重新安排提示，且必须走随宿主销毁取消的生命周期入口。"""
        from PySide6.QtWidgets import QMessageBox, QWidget

        from fukua_rpa.ui.controllers import shutdown_controller as sc

        host = QWidget()
        self.addCleanup(dispose_widget, APP, host)
        host.worker = mock.Mock()
        host.worker.isRunning.return_value = True
        with mock.patch.object(QMessageBox, "exec", lambda self: 0), mock.patch.object(
            QMessageBox, "clickedButton", lambda self: self.buttons()[0]
        ), mock.patch.object(sc, "schedule_widget_callback") as schedule, mock.patch.object(
            sc, "force_exit"
        ):
            sc.prompt_force_exit(host)
        schedule.assert_called_once_with(host, sc.FORCE_EXIT_PROMPT_MS, sc.prompt_force_exit)

    def test_non_qt_host_arms_no_real_timer(self):
        """FR-20261021-001：非 Qt 宿主不得武装裸定时器（否则跨用例到期即挂起套件）。"""
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QMessageBox

        from fukua_rpa.ui.controllers import shutdown_controller as sc

        host = self._host(running=True)
        with mock.patch.object(QMessageBox, "exec", lambda self: 0), mock.patch.object(
            QMessageBox, "clickedButton", lambda self: self.buttons()[0]
        ), mock.patch.object(QTimer, "singleShot") as single_shot, mock.patch.object(
            sc, "schedule_widget_callback"
        ) as schedule, mock.patch.object(sc, "force_exit"):
            sc.prompt_force_exit(host)
        single_shot.assert_not_called()
        schedule.assert_not_called()


class ForceExitBoundedTests(unittest.TestCase):
    """缺口 (a)：有界性与子进程过滤此前零测试。"""

    def test_blocking_release_still_exits_within_bound(self):
        import time

        from fukua_rpa.ui.controllers import shutdown_controller as sc

        engine = mock.Mock()
        engine.release_injected_inputs.side_effect = lambda: time.sleep(5)
        host = mock.Mock()
        host.run_controller.engine = engine
        with mock.patch.object(sc, "flush_logs", return_value=True), mock.patch.object(
            sc.os, "_exit"
        ) as exit_mock:
            started = time.monotonic()
            sc.force_exit(host)
            elapsed = time.monotonic() - started
        exit_mock.assert_called_once_with(1)
        self.assertLess(elapsed, 4.0, f"释放阻塞时退出耗时 {elapsed:.2f}s")

    def test_only_owned_children_are_killed(self):
        from fukua_rpa.ui.controllers import shutdown_controller as sc

        own = mock.Mock()
        own.cmdline.return_value = ["fukuaRPA_v1.0.173.exe"]
        user = mock.Mock()
        user.cmdline.return_value = ["C:/Games/Roblox.exe"]
        host = mock.Mock()
        host.run_controller.engine = None
        fake_proc = mock.Mock()
        fake_proc.children.return_value = [own, user]
        with mock.patch("psutil.Process", return_value=fake_proc), mock.patch.object(
            sc, "flush_logs", return_value=True
        ), mock.patch.object(sc.os, "_exit"):
            sc.force_exit(host)
        own.kill.assert_called_once()
        user.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
