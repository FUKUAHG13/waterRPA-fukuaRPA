"""Tests for launched-subprocess reaping (P3-2 fix)."""

import subprocess
import tempfile
import unittest
from unittest import mock

from fukua_rpa import window_actions


class FakeProcess:
    def __init__(self, exited):
        self._exited = exited
        self.waited = False

    def poll(self):
        # Returncode when exited, None when still running.
        return 0 if self._exited else None

    def wait(self, timeout=None):
        self.waited = True
        return 0


class TestJoinAllLaunched(unittest.TestCase):
    def setUp(self):
        self._saved = dict(window_actions._LAUNCHED_PROCESSES)
        window_actions._LAUNCHED_PROCESSES.clear()

    def tearDown(self):
        window_actions._LAUNCHED_PROCESSES.clear()
        window_actions._LAUNCHED_PROCESSES.update(self._saved)

    def test_exited_processes_reaped(self):
        window_actions._LAUNCHED_PROCESSES[1001] = FakeProcess(exited=True)
        window_actions._LAUNCHED_PROCESSES[1002] = FakeProcess(exited=True)
        removed = window_actions.join_all_launched_applications(timeout=0.0)
        self.assertEqual(removed, 2)
        self.assertEqual(len(window_actions._LAUNCHED_PROCESSES), 0)

    def test_running_processes_kept_without_timeout(self):
        running = FakeProcess(exited=False)
        window_actions._LAUNCHED_PROCESSES[2001] = running
        removed = window_actions.join_all_launched_applications(timeout=0.0)
        self.assertEqual(removed, 0)
        self.assertIn(2001, window_actions._LAUNCHED_PROCESSES)
        self.assertFalse(running.waited)

    def test_running_process_waited_when_timeout_given(self):
        running = FakeProcess(exited=False)
        window_actions._LAUNCHED_PROCESSES[3001] = running
        removed = window_actions.join_all_launched_applications(timeout=0.01)
        self.assertEqual(removed, 1)
        self.assertTrue(running.waited)
        self.assertNotIn(3001, window_actions._LAUNCHED_PROCESSES)


class TestLaunchApplicationBreakaway(unittest.TestCase):
    """FR-701-001: 独立 Runner 的 Job 守卫（KILL_ON_JOB_CLOSE）默认使子进程继承 Job，
    工作流启动的外部程序若留在 Job 内，运行结束/主程序退出时会被误杀。launch_application
    必须在父进程位于 Job 内时传 CREATE_BREAKAWAY_FROM_JOB 使其脱离；无 Job 环境不得传
    （否则 CreateProcess 失败）。"""

    def _launch(self, command):
        fake = mock.Mock()
        fake.pid = 4242
        with mock.patch("subprocess.Popen", return_value=fake) as popen_cls:
            pid = window_actions.launch_application(command)
        return pid, popen_cls

    def test_breakaway_flag_when_in_job(self):
        with mock.patch(
            "fukua_rpa.window_actions._in_job_object", return_value=True
        ):
            pid, popen_cls = self._launch(r"C:\Windows\notepad.exe")
        self.assertEqual(pid, 4242)
        kwargs = popen_cls.call_args.kwargs
        self.assertEqual(
            kwargs["creationflags"], subprocess.CREATE_BREAKAWAY_FROM_JOB
        )

    def test_no_breakaway_flag_outside_job(self):
        with mock.patch(
            "fukua_rpa.window_actions._in_job_object", return_value=False
        ):
            _, popen_cls = self._launch(r"C:\Windows\notepad.exe")
        kwargs = popen_cls.call_args.kwargs
        self.assertEqual(kwargs["creationflags"], 0)

    def test_breakaway_flag_applies_to_file_branch(self):
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as tmp:
            path = tmp.name
        try:
            with mock.patch(
                "fukua_rpa.window_actions._in_job_object", return_value=True
            ):
                _, popen_cls = self._launch(path)
        finally:
            import os

            os.unlink(path)
        kwargs = popen_cls.call_args.kwargs
        self.assertEqual(
            kwargs["creationflags"], subprocess.CREATE_BREAKAWAY_FROM_JOB
        )
        self.assertIsInstance(popen_cls.call_args.args[0], list)


if __name__ == "__main__":
    unittest.main()
