import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from fukua_rpa.commands import effective_task_settings
from fukua_rpa.constants import TASK_TYPE_UNTIL
from fukua_rpa.engine import RPAEngine
from fukua_rpa.loop_scheduler import LoopSchedulerMixin
from fukua_rpa.run_lifecycle_glue import ctypes as run_lifecycle_ctypes
from fukua_rpa.step_runtime import StepExecutionPolicy


class EngineSchedulerBoundaryTests(unittest.TestCase):
    def create_engine(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine()
        engine.log_level = -1
        engine.detect_delay = 0
        engine.settlement_wait = 0
        engine.load_and_precompute = lambda _tasks: True
        return engine

    def create_policy(self, task):
        return StepExecutionPolicy.from_task(
            task,
            default_confidence=0.8,
            default_grayscale=True,
            min_scale=1.0,
            max_scale=1.0,
            scale_step=0.05,
        )

    def test_empty_task_list_finishes_without_spinning(self):
        engine = self.create_engine()
        started = time.monotonic()
        engine.run_tasks([])
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertFalse(engine.is_running)
        self.assertEqual(engine.lifecycle.snapshot().last_outcome, "finished")

    def test_future_step_range_jumps_directly_to_first_runnable_loop(self):
        engine = self.create_engine()
        engine.loop_mode = "无限"
        engine.loop_end_round = 100
        loops = []

        def execute(params):
            loops.append(params.step_info["loop"])
            return "success"

        engine.execute_task_once = execute
        engine.run_tasks(
            [
                {
                    "type": 4.0,
                    "value": "x",
                    "step_loop_start": "100",
                    "step_loop_end": "100",
                }
            ]
        )
        self.assertEqual(loops, [100])

    def test_step_beyond_global_end_never_executes(self):
        engine = self.create_engine()
        engine.loop_mode = "无限"
        engine.loop_end_round = 20
        engine.execute_task_once = mock.Mock(return_value="success")
        engine.run_tasks(
            [
                {
                    "type": 4.0,
                    "value": "x",
                    "step_loop_start": "21",
                    "step_loop_end": "30",
                }
            ]
        )
        engine.execute_task_once.assert_not_called()
        self.assertFalse(engine.is_running)

    def test_run_max_execution_makes_infinite_loop_converge(self):
        engine = self.create_engine()
        engine.loop_mode = "无限"
        engine.execute_task_once = mock.Mock(return_value="success")
        engine.run_tasks([{"type": 4.0, "value": "x", "run_max_executions": "3"}])
        self.assertEqual(engine.execute_task_once.call_count, 3)
        self.assertFalse(engine.is_running)

    def test_string_boolean_matching_options_are_not_truthy_by_accident(self):
        engine = self.create_engine()
        engine.confidence = 0.77
        engine.enable_grayscale = True
        observed = []

        def execute(params):
            observed.append((params.task_conf, params.use_gray))
            return "success"

        engine.execute_task_once = execute
        engine.run_tasks(
            [
                {
                    "type": 1.0,
                    "value": "unused.png",
                    "custom_en": "false",
                    "custom_conf": "0.2",
                    "custom_gray": "false",
                },
                {
                    "type": 1.0,
                    "value": "unused.png",
                    "custom_en": "true",
                    "custom_conf": "0.2",
                    "custom_gray": "false",
                },
            ]
        )

        self.assertEqual(observed, [(0.77, True), (0.2, False)])

    def test_comma_in_template_filename_is_not_treated_as_coordinate(self):
        engine = self.create_engine()
        with self.subTest("wake context"):
            import os
            import tempfile

            with tempfile.TemporaryDirectory() as temp_dir:
                path = os.path.join(temp_dir, "target,variant.png")
                pixels = [
                    ((x * 31) % 255, (y * 47) % 255, ((x + y) * 29) % 255)
                    for y in range(8)
                    for x in range(8)
                ]
                image = Image.new("RGB", (8, 8))
                image.putdata(pixels)
                image.save(path)

                context = engine.recognition_wake_context(
                    path, "comma-template", 0.8, True
                )
                self.assertIsNotNone(context)

                engine.native_core = mock.Mock()
                engine.native_core.available = True
                engine.native_core.find_template.return_value = []
                engine.use_native_core = True
                engine.matching_template_status = lambda *_args: (True, "")
                result = engine.native_find_targets(path, "comma-template", 0.8, True)
                self.assertEqual(result, [])
                engine.native_core.find_template.assert_called_once()

    def test_high_priority_uses_non_owning_process_pseudo_handle(self):
        kernel32 = mock.Mock()
        kernel32.GetCurrentProcess.return_value = -1
        fake_windll = SimpleNamespace(kernel32=kernel32)
        engine = object.__new__(RPAEngine)

        with mock.patch.object(run_lifecycle_ctypes, "windll", fake_windll):
            engine.set_high_priority()

        kernel32.GetCurrentProcess.assert_called_once_with()
        kernel32.SetPriorityClass.assert_called_once_with(-1, 0x00000080)
        kernel32.OpenProcess.assert_not_called()

    def test_disabled_step_is_skipped_without_breakpoint_or_execution(self):
        engine = self.create_engine()
        executed = []
        debug_events = []

        def execute(params):
            executed.append(params.val)
            return "success"

        engine.execute_task_once = execute
        engine.run_tasks(
            [
                {"type": 4.0, "value": "first"},
                {
                    "type": 4.0,
                    "value": "disabled",
                    "step_disabled": True,
                    "debug_breakpoint": True,
                },
                {"type": 4.0, "value": "last"},
            ],
            callback_debug=debug_events.append,
        )

        self.assertEqual(executed, ["first", "last"])
        self.assertFalse(any(event.get("state") == "paused" for event in debug_events))
        self.assertFalse(engine.is_running)

    def test_all_disabled_steps_finish_without_spinning(self):
        engine = self.create_engine()
        engine.loop_mode = "无限"
        engine.execute_task_once = mock.Mock(return_value="success")
        started = time.monotonic()
        engine.run_tasks([{"type": 4.0, "value": "disabled", "step_disabled": True}])
        self.assertLess(time.monotonic() - started, 0.2)
        engine.execute_task_once.assert_not_called()
        self.assertFalse(engine.is_running)

    def test_jump_only_step_skips_sequential_entry_but_accepts_branch_entry(self):
        engine = self.create_engine()
        executed = []

        def execute(params):
            executed.append(params.val)
            return "success"

        engine.execute_task_once = execute
        engine.run_tasks(
            [
                {"type": 4.0, "value": "first", "success_skip": "1"},
                {"type": 4.0, "value": "middle"},
                {"type": 4.0, "value": "branch-only", "jump_only": True},
            ]
        )
        self.assertEqual(executed, ["first", "branch-only"])

        executed.clear()
        engine.run_tasks(
            [
                {"type": 4.0, "value": "first"},
                {"type": 4.0, "value": "sequential-only", "jump_only": True},
                {"type": 4.0, "value": "last"},
            ]
        )
        self.assertEqual(executed, ["first", "last"])

    def test_called_step_returns_to_caller_branch_and_ignores_own_branch(self):
        engine = self.create_engine()
        executed = []

        def execute(params):
            executed.append(params.val)
            return "error" if params.val == "called" else "success"

        engine.execute_task_once = execute
        engine.run_tasks(
            [
                {
                    "type": 4.0,
                    "value": "caller",
                    "call_step": "3",
                    "success_jump": "4",
                },
                {"type": 4.0, "value": "must-not-run"},
                {
                    "type": 4.0,
                    "value": "called",
                    "jump_only": True,
                    "fail_jump": "2",
                },
                {"type": 4.0, "value": "caller-target"},
            ]
        )

        self.assertEqual(executed, ["caller", "called", "caller-target"])

    def test_stop_interrupts_long_wait_promptly(self):
        engine = self.create_engine()
        status_seen = threading.Event()
        worker = threading.Thread(
            target=engine.run_tasks,
            args=([{"type": 5.0, "value": "30"}],),
            kwargs={"callback_status": lambda _data: status_seen.set()},
            daemon=True,
        )
        worker.start()
        self.assertTrue(status_seen.wait(1.0))
        stop_started = time.monotonic()
        engine.stop()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - stop_started, 1.0)
        self.assertEqual(engine.lifecycle.snapshot().last_outcome, "stopped")

    def test_repeated_stop_and_finish_are_idempotent(self):
        engine = self.create_engine()
        run_id = engine.reserve_run()
        self.assertIsNotNone(run_id)
        self.assertTrue(engine.stop())
        self.assertTrue(engine.stop())
        self.assertTrue(engine.finish_run(run_id, "stopped"))
        self.assertFalse(engine.finish_run(run_id, "stopped"))
        self.assertFalse(engine.stop())

    def test_engine_breakpoint_pauses_before_execution(self):
        engine = self.create_engine()
        engine.execute_task_once = mock.Mock(return_value="success")
        paused = threading.Event()
        worker = threading.Thread(
            target=engine.run_tasks,
            args=([{"type": 4.0, "value": "x", "debug_breakpoint": True}],),
            kwargs={
                "callback_debug": lambda event: (
                    paused.set() if event.get("state") == "paused" else None
                )
            },
            daemon=True,
        )
        worker.start()
        self.assertTrue(paused.wait(1.0))
        engine.execute_task_once.assert_not_called()
        self.assertTrue(engine.debug_session.continue_run())
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        engine.execute_task_once.assert_called_once()

    def test_false_conditional_breakpoint_does_not_pause(self):
        engine = self.create_engine()
        engine.execute_task_once = mock.Mock(return_value="success")
        debug_events = []

        engine.run_tasks(
            [
                {
                    "type": 4.0,
                    "value": "x",
                    "debug_breakpoint": True,
                    "debug_condition": "loop > 1",
                }
            ],
            callback_debug=debug_events.append,
        )

        engine.execute_task_once.assert_called_once()
        self.assertFalse(any(event.get("state") == "paused" for event in debug_events))

    def test_true_conditional_breakpoint_pauses_with_specific_reason(self):
        engine = self.create_engine()
        engine.execute_task_once = mock.Mock(return_value="success")
        paused = threading.Event()
        events = []

        def on_debug(event):
            events.append(event)
            if event.get("state") == "paused":
                paused.set()

        worker = threading.Thread(
            target=engine.run_tasks,
            args=(
                [
                    {
                        "type": 4.0,
                        "value": "x",
                        "debug_breakpoint": True,
                        "debug_condition": "loop == 1 and execution_count == 0",
                    }
                ],
            ),
            kwargs={"callback_debug": on_debug},
            daemon=True,
        )
        worker.start()
        self.assertTrue(paused.wait(1.0))
        self.assertEqual(events[0]["reason"], "conditional_breakpoint")
        self.assertTrue(engine.debug_session.continue_run())
        worker.join(1.0)
        self.assertFalse(worker.is_alive())
        engine.execute_task_once.assert_called_once()

    def test_unset_breakpoint_variable_pauses_instead_of_hiding_error(self):
        engine = self.create_engine()
        engine.execute_task_once = mock.Mock(return_value="success")
        paused = threading.Event()
        events = []

        def on_debug(event):
            events.append(event)
            if event.get("state") == "paused":
                paused.set()

        worker = threading.Thread(
            target=engine.run_tasks,
            args=(
                [
                    {
                        "type": 4.0,
                        "value": "x",
                        "debug_breakpoint": True,
                        "debug_condition": "missing_value > 0",
                    }
                ],
            ),
            kwargs={"callback_debug": on_debug},
            daemon=True,
        )
        worker.start()
        self.assertTrue(paused.wait(1.0))
        self.assertEqual(events[0]["reason"], "breakpoint_condition_error")
        self.assertIn("missing_value", events[0]["detail"])
        engine.debug_session.continue_run()
        worker.join(1.0)
        self.assertFalse(worker.is_alive())

    def test_until_true_transition_uses_configured_absolute_step(self):
        engine = self.create_engine()
        engine.reset_until_runtime = mock.Mock()
        task = {"type": TASK_TYPE_UNTIL, "until_true_jump": "4"}

        transition = engine._resolve_step_transition(
            task=task,
            policy=self.create_policy(task),
            index=1,
            last_status="condition_true",
            failed_branch=False,
            skipped_without_branch=False,
        )

        self.assertEqual(transition.next_index, 3)
        self.assertTrue(transition.condition_branch_handled)
        self.assertFalse(transition.failed_branch)
        engine.reset_until_runtime.assert_called_once_with({"step": 2})

    def test_until_false_transition_jumps_back_before_limit(self):
        engine = self.create_engine()
        engine.until_false_runtime = mock.Mock(return_value=(False, "", 2, 0.5))
        task = {"type": TASK_TYPE_UNTIL, "until_false_jump": "1"}

        transition = engine._resolve_step_transition(
            task=task,
            policy=self.create_policy(task),
            index=2,
            last_status="condition_false",
            failed_branch=False,
            skipped_without_branch=False,
        )

        self.assertEqual(transition.next_index, 0)
        self.assertTrue(transition.condition_branch_handled)
        self.assertFalse(transition.stop_run)

    def test_until_limit_legacy_failure_action_matches_continue(self):
        """FR-SCOPE001-002 / FR-SCOPE001-001：直到步骤不叠加普通失败分支。

        生产路径（`run_lifecycle_glue.py:601-602`、`jump_resolver.py:419-420`）先经
        ``effective_task_settings()`` 取得有效副本，再构造 ``StepExecutionPolicy``；
        该副本对 ``TASK_TYPE_UNTIL`` 丢弃 ``CONTROL_FAILURE_BRANCH``，把
        ``fail_jump`` / ``fail_skip`` / ``fail_target_id`` 归零。因此旧方案里保存的
        「按失败处理」在运行期与「继续下一步」**执行流向等价**——这是
        `docs/步骤设置显隐矩阵.md`「不叠加普通分支」的既定语义，不是一个可用的失败分支。

        本用例**必须**经 ``effective_task_settings()`` 构造；直接用手写任务字典
        断言 ``fail_jump="5"`` 会命中生产路径不可达的组合（原 FR-SCOPE001-002）。
        同时断言两条路径逐字段等价，使「等价」成为被守护的事实而非口头说明。
        """
        engine = self.create_engine()
        engine.until_false_runtime = mock.Mock(
            return_value=(True, "已达到检查次数", 5, 1.5)
        )
        engine.reset_until_runtime = mock.Mock()

        def resolve(task):
            effective = effective_task_settings(task)
            return effective, engine._resolve_step_transition(
                task=effective,
                policy=self.create_policy(effective),
                index=2,
                last_status="condition_false",
                failed_branch=False,
                skipped_without_branch=False,
            )

        legacy_effective, transition = resolve(
            {
                "type": TASK_TYPE_UNTIL,
                "until_on_limit": "按失败处理",
                "fail_jump": "5",
            }
        )
        # 生产副本确实归零了失败分支字段 —— 这是「不叠加普通分支」的实现证据。
        self.assertEqual(legacy_effective["fail_jump"], "0")

        continue_effective, reference = resolve(
            {
                "type": TASK_TYPE_UNTIL,
                "until_on_limit": "继续下一步",
                "fail_jump": "5",
            }
        )
        self.assertEqual(continue_effective["fail_jump"], "0")

        self.assertEqual(transition.next_index, 3)
        self.assertFalse(transition.condition_branch_handled)
        self.assertTrue(transition.failed_branch)
        self.assertFalse(transition.stop_run)
        # 用户可观察的执行流向：两者相同。
        self.assertEqual(transition.next_index, reference.next_index)
        self.assertEqual(transition.entry_reason, reference.entry_reason)
        self.assertEqual(transition.stop_run, reference.stop_run)

    def test_until_limit_stop_action_stops_without_normal_branch(self):
        engine = self.create_engine()
        engine.until_false_runtime = mock.Mock(
            return_value=(True, "已达到等待时间", 3, 2.0)
        )
        engine.reset_until_runtime = mock.Mock()
        engine.stop = mock.Mock()
        task = {
            "type": TASK_TYPE_UNTIL,
            "until_on_limit": "停止脚本",
            "fail_jump": "5",
        }

        transition = engine._resolve_step_transition(
            task=task,
            policy=self.create_policy(task),
            index=2,
            last_status="condition_false",
            failed_branch=False,
            skipped_without_branch=False,
        )

        self.assertTrue(transition.stop_run)
        self.assertEqual(transition.next_index, 3)
        engine.stop.assert_called_once_with()

    def test_prepare_run_init_failure_after_mark_running_finishes_run(self):
        # ENG-FR-003：mark_running 成功后初始化抛异常时，run_tasks 的 finally
        # 不会执行，生命周期必须由 _prepare_run 自行收束到 finish_run，
        # 否则引擎永久卡在 RUNNING、后续 reserve_run() 全部返回 None。
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine(defer_backends=True)
        engine._initialize_run_runtime = mock.Mock(side_effect=RuntimeError("boom"))
        messages = []

        run_id = engine._prepare_run(
            [],
            run_id=None,
            callback_msg=messages.append,
            callback_status=None,
            callback_click_indicator=None,
            callback_debug=None,
            callback_prompt=None,
        )

        self.assertIsNone(run_id)
        self.assertFalse(engine.lifecycle.is_active)
        # 引擎必须恢复可复用：下一次 reserve 正常返回。
        self.assertIsNotNone(engine.reserve_run())
        self.assertTrue(any("运行初始化失败" in str(m) for m in messages))

    def test_loop_wait_branch_honors_stop_flag(self):
        # ENG-FR-001：“等待循环范围”分支此前不检查停止标志，无限循环模式下
        # 异常大的起始轮次会让停止/急停永久失效（ARCHITECTURE #52）。
        host = _LoopWaitStopProbe()
        plan = host._prepare_loop_execution([], 3)
        self.assertTrue(plan.stop_run)


class _LoopWaitStopProbe(LoopSchedulerMixin):
    """最小宿主：仅提供 _prepare_loop_execution 等待分支依赖的属性与方法。"""

    def __init__(self):
        self.loop_mode = "无限"
        self.loop_start_round = 10
        self.loop_end_round = 0
        self.loop_val = 1_000_000
        self.log_level = 0
        self.reported = []

    def check_stop_flag(self):
        return True

    def global_time_limit_reached(self):
        return False

    def report_status(self, *args, **kwargs):
        self.reported.append(args)

    def log(self, *args, **kwargs):
        pass


if __name__ == "__main__":
    unittest.main()
