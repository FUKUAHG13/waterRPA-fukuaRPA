"""回归测试：``start_task_from_step_once`` 的启动守卫条件（UIC-001）。

该方法此前在 tests/ 中零覆盖。这里覆盖其**守卫路径**——即所有应拒绝启动的
前置条件，因为误放行会导致连点器抢输入、重复启动或识别测试相互干扰。

Run with ``python -m unittest -v tests.test_start_task_from_step_once_guards``.
"""

from __future__ import annotations

import os
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.ui.controllers.execution_controller import ExecutionController


class _StubHost:
    """Minimal host stub exposing only what the guarded path touches."""

    def __init__(
        self,
        *,
        runtime_ready: bool = True,
        auto_clicker: bool = False,
        run_active: bool = False,
        recognition_active: bool = False,
    ) -> None:
        self.runtime_ready = runtime_ready
        # 仅在连点器运行时返回 True；缺该属性时守卫应视为未运行。
        self.auto_clicker_is_running = lambda: auto_clicker
        self.run_controller = types.SimpleNamespace(is_active=run_active)
        self.feedback_controller = types.SimpleNamespace(
            recognition_test_active=recognition_active
        )
        # ``prepare_and_launch_profile`` 会把运行配置写到 engine；用 MagicMock
        # 承载这些属性赋值即可，无需真实引擎。
        self.engine = mock.MagicMock()
        self.tooltips: list[str] = []

    def show_start_tooltip(self, message: str) -> None:
        self.tooltips.append(message)

    def tasks_for_current_settings_mode(self, tasks):
        """FR-20261008-701: ``prepare_and_launch_profile`` 现在会应用简易/高级过滤。

        本桩不模拟视图模式，按"高级模式原样返回"实现（与真实实现的高级模式分支一致）。
        """
        return list(tasks)


class StartTaskFromStepOnceGuardTests(unittest.TestCase):
    def _controller(self, host: _StubHost):
        controller = ExecutionController(host)  # type: ignore[arg-type]
        controller.start_in_progress = False
        return controller

    def test_blocked_when_runtime_not_ready(self):
        host = _StubHost(runtime_ready=False)
        controller = self._controller(host)
        self.assertFalse(controller.start_task_from_step_once(0))
        self.assertTrue(host.tooltips, "运行库未就绪时必须给出提示")

    def test_blocked_while_auto_clicker_holds_input(self):
        host = _StubHost(auto_clicker=True)
        controller = self._controller(host)
        self.assertFalse(controller.start_task_from_step_once(0))
        self.assertTrue(host.tooltips, "连点器占用输入时必须给出提示")

    def test_blocked_while_start_already_in_progress(self):
        host = _StubHost()
        controller = self._controller(host)
        controller.start_in_progress = True
        self.assertFalse(controller.start_task_from_step_once(0))
        # 已在同一轮启动中属于静默短路，不应重复弹提示。
        self.assertEqual(host.tooltips, [])

    def test_blocked_while_a_run_is_active(self):
        host = _StubHost(run_active=True)
        controller = self._controller(host)
        self.assertFalse(controller.start_task_from_step_once(0))

    def test_blocked_while_recognition_test_is_active(self):
        host = _StubHost(recognition_active=True)
        controller = self._controller(host)
        self.assertFalse(controller.start_task_from_step_once(0))

    def test_missing_auto_clicker_probe_treated_as_not_running(self):
        """A host without the optional probe must not be treated as busy."""
        host = _StubHost()
        del host.auto_clicker_is_running
        controller = self._controller(host)
        # 守卫不应因缺少可选探针而抛错或误判为运行中。
        self.assertFalse(controller._auto_clicker_is_running())


class _ProfileModelStub:
    def __init__(self, name, cfg):
        self._name = name
        self._cfg = cfg

    def find_by_id(self, profile_id):
        return self._name if profile_id == "p1" else None

    def get_config_by_id(self, profile_id):
        return self._cfg if profile_id == "p1" else None


class _ReviewControllerStub:
    def __init__(self, error=""):
        self.error = error
        self.calls = 0

    def validate_tasks(self, tasks, cfg=None):
        self.calls += 1
        return self.error


class PrepareAndLaunchProfileValidationTests(unittest.TestCase):
    """REVIEW-20260913-001 FR-005：计划任务启动路径的步骤校验守卫。

    ``prepare_and_launch_profile`` 是计划任务与方案快捷键共用的启动入口。方案
    快捷键在调用前自行调用 ``validate_tasks``，但计划任务协调器直接调用本方法，
    此前全程没有启动前步骤校验——无人值守运行会缺失与手动运行一致的提前阻断。
    """

    def _host(self, *, validation_error="", tasks=None):
        host = _StubHost()
        cfg = default_profile_config()
        cfg["tasks"] = tasks if tasks is not None else [{"type": 5.0, "value": "1"}]
        host.profile_model = _ProfileModelStub("方案A", cfg)
        host.workflow_review_controller = _ReviewControllerStub(validation_error)
        return host

    def test_scheduled_path_validates_tasks_before_launch(self):
        host = self._host(validation_error="未知指令类型：99.0")
        controller = ExecutionController(host)  # type: ignore[arg-type]

        with mock.patch.object(
            ExecutionController, "_launch_prepared_tasks", return_value=True
        ) as launch:
            started = controller.prepare_and_launch_profile(
                "p1", "scheduled", "s1", "run1", None
            )

        self.assertFalse(started, "步骤校验失败时不得启动")
        self.assertEqual(host.workflow_review_controller.calls, 1)
        launch.assert_not_called()

    def test_scheduled_path_launches_when_tasks_validate(self):
        host = self._host(validation_error="")
        controller = ExecutionController(host)  # type: ignore[arg-type]

        with mock.patch.object(
            ExecutionController, "_launch_prepared_tasks", return_value=True
        ) as launch:
            started = controller.prepare_and_launch_profile(
                "p1", "scheduled", "s1", "run1", None
            )

        self.assertTrue(started, "校验通过时必须继续启动")
        self.assertEqual(host.workflow_review_controller.calls, 1)
        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
