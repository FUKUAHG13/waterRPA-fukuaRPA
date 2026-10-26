"""P6-2 守卫测试：插件 run_id/deadline_ns 契约闭合 + 子进程生命周期 join。

退出标准：插件请求契约测试断言 run_id 非空、deadline_ns > 0；
子进程生命周期提供 join(timeout) 避免孤儿进程。
"""
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import patch

from fukua_rpa import window_actions
from fukua_rpa.plugin_actions import execute_plugin_action
from fukua_rpa.plugin_protocol import PluginResponse


# --------------------------------------------------------------------------
# 插件请求契约（R-9）
# --------------------------------------------------------------------------
def _make_engine(active_run_id=42):
    return SimpleNamespace(
        lifecycle=SimpleNamespace(active_run_id=active_run_id),
        log=lambda *a, **k: None,
    )


def _make_context():
    return SimpleNamespace(
        task={
            "plugin_id": "p1",
            "plugin_action_id": "a1",
            "id": "s1",
            "plugin_config": {"k": "v"},
        },
        step_info={"loop": 1, "step": 2},
    )


class _StubPluginClient:
    def __init__(self, captured):
        self._captured = captured

    def call(self, request):
        self._captured.append(request)
        return PluginResponse(
            run_id=request.run_id,
            step_id=request.step_id,
            ok=True,
            result={},
            error=None,
        )


def test_plugin_request_contract_run_id_and_deadline():
    captured = []
    engine = _make_engine(active_run_id=42)
    context = _make_context()
    with patch(
        "fukua_rpa.plugin_actions.get_plugin_host_client",
        return_value=_StubPluginClient(captured),
    ):
        result = execute_plugin_action(engine, context)
    assert result == "success"
    assert len(captured) == 1
    req = captured[0]
    # R-9: run_id 取真实运行 ID（非空）
    assert req.run_id == "42", f"run_id 未闭合为真实运行 ID: {req.run_id!r}"
    # R-9: deadline_ns 计算为相对截止时刻的纳秒时间戳（>0 且在未来）
    assert req.deadline_ns > 0
    assert req.deadline_ns >= time.monotonic_ns()


def test_plugin_request_contract_falls_back_when_no_lifecycle():
    # 无 lifecycle / 无 active run_id 时回退空串（兼容既有协议，不崩溃）
    captured = []
    engine = SimpleNamespace(log=lambda *a, **k: None)  # 无 lifecycle
    context = _make_context()
    with patch(
        "fukua_rpa.plugin_actions.get_plugin_host_client",
        return_value=_StubPluginClient(captured),
    ):
        result = execute_plugin_action(engine, context)
    assert result == "success"
    assert captured[0].run_id == ""


# --------------------------------------------------------------------------
# 子进程生命周期（项 33）
# --------------------------------------------------------------------------
class _FakeProcess:
    def __init__(self, pid, *, slow=False):
        self.pid = pid
        self._slow = slow
        self._exited = False

    def poll(self):
        return 0 if self._exited else None

    def wait(self, timeout=None):
        if self._slow:
            raise subprocess.TimeoutExpired("x", timeout)
        self._exited = True

    def kill(self):
        self._exited = True


def test_launch_records_and_join():
    proc = _FakeProcess(12345)
    with patch.object(window_actions.subprocess, "Popen", lambda *a, **k: proc):
        pid = window_actions.launch_application("fakeapp")
    assert pid == 12345
    # 进程退出后 join 返回 True
    assert window_actions.join_launched_application(pid, timeout=5) is True
    # 退出后已从跟踪表移除：再次 join（未知 PID）返回 True
    assert window_actions.join_launched_application(pid, timeout=0) is True


def test_join_timeout_returns_false_for_slow_process():
    proc = _FakeProcess(999, slow=True)
    with patch.object(window_actions.subprocess, "Popen", lambda *a, **k: proc):
        pid = window_actions.launch_application("slowapp")
    try:
        assert window_actions.join_launched_application(pid, timeout=0.1) is False
    finally:
        # 清理，避免测试残留假进程态
        proc._exited = True
        window_actions.join_launched_application(pid, timeout=0)
