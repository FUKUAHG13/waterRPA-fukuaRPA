"""FR-20261005-006 回归守护：已自报终局的子进程不得被归类为 ``forced_stop``。

裁定来源
--------
``DEC-20260930-002-02``（维护者 2026-10-06 裁定）：用户未请求停止、已收到
有效 ``finished`` 终局时，保留子进程报告的归一结果；成功运行显示「完成」。
收尾超时仍须有界终止残留进程、释放输入，并记录收尾异常。该裁定**不改变**
真实用户急停、启动失败和真实崩溃的分类。

缺陷
----
``_terminate_live_process`` 的 ``_forced_stop`` 置位条件未排除
``_received_finished``。子进程发出 ``finished`` 后若收尾超过探测窗口仍未退出，
``_forced_stop`` 被置位；``_build_exit_report`` 把 ``_forced_stop`` 放在阶梯
首位，于是终局被归为 ``forced_stop``，把一次已完成的运行显示为「强制停止」。

反向验收（本文件同等重要）
--------------------------
裁定明确要求"不能只是清除 forced_stop 标记后又误报 crashed"。因此本文件
同时锁定：清除标记后，终局必须落到 ``_received_finished`` 分支的**子进程自报
结果**；若自报结果不可用也不得静默变成 ``crashed``。
"""

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from fukua_rpa.runner.isolated import IsolatedRunnerThread

ROOT = Path(__file__).resolve().parents[1]


class _FakeProcess:
    """最小 Popen 替身：可控存活状态、退出码与 wait 超时行为。

    FR-20261006-014：原替身死亡后 ``poll()`` 恒返回 0，与真实 Windows 进程
    语义不符——``subprocess`` 在 Windows 上对 ``terminate()`` 走
    ``TerminateProcess``，``poll()`` 必返回非零。该偏差掩盖了
    ``FR-20261006-002``（已自报 finished 的成功运行被误报为 crashed），
    使回归守护形同虚设。现由 ``exit_code`` 显式建模退出码（默认 0）；
    模拟收尾超时被强制终止的场景须传入非零值。
    """

    def __init__(self, alive_after_waits=True, terminate_alive=False, exit_code=0):
        self._alive = True
        self._alive_after_waits = alive_after_waits
        self._terminate_alive = terminate_alive
        self._exit_code = exit_code
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else self._exit_code

    def wait(self, timeout=None):
        if not self._alive:
            return self._exit_code
        if not self._alive_after_waits:
            self._alive = False
            return self._exit_code
        raise subprocess.TimeoutExpired(cmd="runner", timeout=timeout or 0)

    def terminate(self):
        self.terminated = True
        if not self._terminate_alive:
            self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False


def _session(**flags):
    """构造只含终局判定所需标志的会话替身。"""
    session = mock.MagicMock(spec=IsolatedRunnerThread)
    session._forced_stop = False
    session._stop_requested = mock.MagicMock()
    session._stop_requested.is_set.return_value = flags.get("stop_requested", False)
    session._observed_child_exit = flags.get("observed_child_exit", False)
    session._received_started = flags.get("received_started", True)
    session._received_finished = flags.get("received_finished", False)
    session._received_error = flags.get("received_error", False)
    session._child_outcome = flags.get("child_outcome")
    # 真实 _build_exit_report 还会读取以下字段（见 isolated.py:690-700 的
    # summary 组装），缺任一项MagicMock(spec=...) 都会抛 AttributeError。
    session._child_error = flags.get("child_error", "")
    session._job_guard = None
    # REG-20261006-003: 绑定**真实**的 _build_exit_report，而不是用lambda 复刻
    # 阶梯。裁定要求验收"最终结果"，若测试自带一份阶梯副本，阶梯本身的变更
    # （例如有人调整 forced_stop / crashed 的优先级）不会被本测试发现。
    # 绑定真实方法后，终局分类由生产代码决定，断言才具备独立性。
    session._build_exit_report = lambda process: IsolatedRunnerThread._build_exit_report(
        session, process
    )
    return session


class FinishedChildNotForcedStopTests(unittest.TestCase):
    """已自报终局 + 未急停 ⇒ 终局不得为 ``forced_stop``。"""

    def test_finished_child_not_forced_stop(self):
        """核心守护：收尾挂起不再把已完成运行误报为强制停止。"""
        session = _session(received_finished=True, child_outcome="finished")
        process = _FakeProcess(alive_after_waits=True)

        with mock.patch("fukua_rpa.runner.isolated.RUNNER_TERMINATE_PROBE_SECONDS", 0.01):
            IsolatedRunnerThread._terminate_live_process(session, process)

        self.assertFalse(
            session._forced_stop,
            "已自报 finished 终局的子进程不得被置为 _forced_stop"
            "（DEC-20260930-002-02 裁定）",
        )
        report = session._build_exit_report(process)
        self.assertEqual(
            report["outcome"],
            "finished",
            "清除标记后终局必须落到子进程自报结果，而非 crashed",
        )

    def test_finished_child_still_terminated_and_inputs_released(self):
        """反向验收：仍须有界终止残留进程（不得留下悬挂子进程）。"""
        session = _session(received_finished=True, child_outcome="finished")
        process = _FakeProcess(alive_after_waits=True, terminate_alive=True)

        with mock.patch("fukua_rpa.runner.isolated.RUNNER_TERMINATE_PROBE_SECONDS", 0.01):
            IsolatedRunnerThread._terminate_live_process(session, process)

        self.assertTrue(
            process.terminated,
            "收尾超时后仍须有界终止残留子进程（裁定要求）",
        )

    def test_normal_failed_child_still_forced_stop(self):
        """反向验收：未收到 finished 的真实强杀仍归类 ``forced_stop``。"""
        session = _session(received_finished=False)
        process = _FakeProcess(alive_after_waits=True)

        with mock.patch("fukua_rpa.runner.isolated.RUNNER_TERMINATE_PROBE_SECONDS", 0.01):
            IsolatedRunnerThread._terminate_live_process(session, process)

        self.assertTrue(
            session._forced_stop,
            "运行期未自报终局的强杀仍应归类 forced_stop（语义不得被本裁定改变）",
        )

    def test_user_stop_still_forced_stop(self):
        """反向验收：用户真实急停仍按 ``user_stopped`` 优先，不受本裁定影响。"""
        session = _session(received_finished=True, stop_requested=True, child_outcome="finished")
        process = _FakeProcess(alive_after_waits=True)

        with mock.patch("fukua_rpa.runner.isolated.RUNNER_TERMINATE_PROBE_SECONDS", 0.01):
            IsolatedRunnerThread._terminate_live_process(session, process)

        self.assertFalse(
            session._forced_stop,
            "用户已请求停止时不得置 _forced_stop（急停优先）",
        )
        self.assertEqual(
            session._build_exit_report(process)["outcome"],
            "user_stopped",
            "用户真实急停的分类不得被本裁定改变",
        )

    def test_startup_failure_still_startup_failed(self):
        """反向验收：启动期失败仍归类 ``startup_failed``。"""
        session = _session(received_started=False, received_finished=False)
        process = _FakeProcess(alive_after_waits=True)

        with mock.patch("fukua_rpa.runner.isolated.RUNNER_TERMINATE_PROBE_SECONDS", 0.01):
            IsolatedRunnerThread._terminate_live_process(session, process)

        self.assertFalse(
            session._forced_stop,
            "启动期失败由 startup_failed 分支归类，不得被 forced_stop 遮蔽",
        )
        self.assertEqual(
            session._build_exit_report(process)["outcome"],
            "startup_failed",
        )

    def test_observed_child_exit_still_crashed(self):
        """反向验收：已观察到子进程退出（真实崩溃）仍归类 ``crashed``。"""
        session = _session(
            received_finished=False, observed_child_exit=True
        )
        process = _FakeProcess(alive_after_waits=False)

        with mock.patch("fukua_rpa.runner.isolated.RUNNER_TERMINATE_PROBE_SECONDS", 0.01):
            IsolatedRunnerThread._terminate_live_process(session, process)

        self.assertFalse(
            session._forced_stop,
            "已观察到退出（崩溃路径）不得被归类 forced_stop",
        )


class TeardownDiagnosticsTests(unittest.TestCase):
    """REG-20261006-003: 裁定的"有界终止 + 释放输入 + 记录收尾异常"须有测试覆盖。

    07 只测了 ``_forced_stop`` 分类，未覆盖 finally 中的收尾宽限日志与
    ``_release_tracked_inputs``；裁定明确要求"记录收尾异常"，若无测试，该要求
    可被静默删除而不被发现。
    """

    def test_teardown_timeout_logs_warning(self):
        """收尾宽限超时必须写warning（含子进程自报终局值）。"""
        source = (
            ROOT / "fukua_rpa" / "runner" / "isolated.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "独立 Runner 收尾超时",
            source,
            "收尾超时必须被记录（DEC-20260930-002-02 裁定要求记录收尾异常）",
        )
        # 该warning 必须落在 subprocess.TimeoutExpired 的处理里，而非被 suppress 静默吞掉。
        suppressed_form = (
            "with contextlib.suppress(OSError, subprocess.TimeoutExpired):"
        )
        # 取 finally 收尾段：从"子进程已自报终局"的注释到调用输入释放之前。
        start = source.index("# Once the child has sent")
        end = source.index("self._release_tracked_inputs()", start)
        tail_section = source[start:end]
        self.assertNotIn(
            suppressed_form,
            tail_section,
            "收尾宽限不得再被 contextlib.suppress 静默吞掉（裁定要求记录收尾异常）",
        )
        self.assertIn("except subprocess.TimeoutExpired", tail_section)

    def test_release_tracked_inputs_releases_keys(self):
        """收尾必须释放按键/鼠标键（裁定要求"释放输入"）。"""
        session = _session(received_finished=True, child_outcome="finished")
        session._tracked_keys = ["a"]
        session._tracked_buttons = ["left"]
        released = {"keys": [], "buttons": []}

        class _FakePyAutoGUI:
            @staticmethod
            def keyUp(key):  # noqa: N802 - 匹配 PyAutoGUI 签名
                released["keys"].append(key)

            @staticmethod
            def mouseUp(button, **_kwargs):  # noqa: N802
                released["buttons"].append(button)

        fake_module = mock.MagicMock()
        fake_module.pyautogui = _FakePyAutoGUI
        with mock.patch.dict(
            "sys.modules", {"fukua_rpa.pyautogui_runtime": fake_module}
        ):
            IsolatedRunnerThread._release_tracked_inputs(session)

        self.assertEqual(released["keys"], ["a"], "按键必须被释放")
        self.assertEqual(released["buttons"], ["left"], "鼠标键必须被释放")


class NonZeroExitCodeTerminalTests(unittest.TestCase):
    """FR-20261006-002 / FR-20261006-014：终局分类不得被主进程收尾方式覆盖。

    真实 Windows 进程在 ``terminate()``（``TerminateProcess``）后 ``poll()``
    必返回非零。子进程若已通过 ``finished`` 自报终局，该非零退出码只说明
    "主进程收尾时终止了它"，不说明脚本失败。
    """

    def test_finished_child_with_nonzero_exit_is_not_crashed(self):
        """已自报 finished + 收尾被强制终止（非零退出码）→ finished。"""
        session = _session(received_finished=True, child_outcome="finished")
        process = _FakeProcess(alive_after_waits=False, exit_code=1)
        process.terminate()
        report = IsolatedRunnerThread._build_exit_report(session, process)
        self.assertEqual(
            report["outcome"],
            "finished",
            "已自报 finished 的运行不得因主进程强制终止而被误报为 crashed",
        )

    def test_child_self_reported_outcome_wins_over_exit_code(self):
        """子进程自报的内部语义优先于退出码归一。"""
        session = _session(received_finished=True, child_outcome="stopped")
        process = _FakeProcess(alive_after_waits=False, exit_code=1)
        process.terminate()
        report = IsolatedRunnerThread._build_exit_report(session, process)
        self.assertEqual(report["outcome"], "user_stopped")

    def test_no_finished_and_nonzero_exit_still_crashed(self):
        """未自报 finished 的非零退出码仍归 crashed（不得放宽）。"""
        session = _session(received_finished=False, received_error=True)
        process = _FakeProcess(alive_after_waits=False, exit_code=1)
        process.terminate()
        report = IsolatedRunnerThread._build_exit_report(session, process)
        self.assertEqual(report["outcome"], "crashed")

    def test_user_stop_beats_finished_and_exit_code(self):
        """用户急停优先于自报终局与非零退出码。"""
        session = _session(
            received_finished=True, stop_requested=True, child_outcome="finished"
        )
        process = _FakeProcess(alive_after_waits=False, exit_code=1)
        process.terminate()
        report = IsolatedRunnerThread._build_exit_report(session, process)
        self.assertEqual(report["outcome"], "user_stopped")

    def test_startup_failure_beats_finished_flag(self):
        """启动期失败仍归 startup_failed。"""
        session = _session(received_started=False, received_finished=False)
        report = IsolatedRunnerThread._build_exit_report(session, None)
        self.assertEqual(report["outcome"], "startup_failed")

    def test_forced_stop_beats_finished_and_exit_code(self):
        """强制停止优先于自报终局与非零退出码。"""
        session = _session(received_finished=True, child_outcome="finished")
        session._forced_stop = True
        process = _FakeProcess(alive_after_waits=False, exit_code=1)
        process.terminate()
        report = IsolatedRunnerThread._build_exit_report(session, process)
        self.assertEqual(report["outcome"], "forced_stop")


if __name__ == "__main__":
    unittest.main()
