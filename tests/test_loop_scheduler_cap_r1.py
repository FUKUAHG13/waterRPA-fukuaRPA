"""R-1 回归基线：引擎单趟步骤循环 ``_execute_loop_steps`` 当前【无】显式最大迭代上限。

背景
----
``engine.py`` 的 ``_execute_loop_steps``（约 1803-1831 行）使用
``while index < len(tasks):`` 的单趟步骤调度循环，**没有**任何硬编码的最大迭代
次数上限；它只被两类条件约束：

* 内部边界：``index < len(tasks)``（步骤做完即终止）；
* 外部边界：``check_stop_flag()``（手动停止 / 单步超时由 ``_dispatch_step`` 内部负责）。

未来 P4 重构会在此循环上加一个显式 cap。本文件把「当前无 cap」的行为钉死为
**绿色回归基线**——现在一定通过；当 cap 加入后，本文件的相关断言需要扩展
（例如改为断言 cap 存在且行为正确）。

本文件验证：
  * 循环在步骤做完后正确终止（不提前、不无限，dispatch 次数 == len(tasks)）；
  * 即便任务列表很大，完成边界也只由 ``len(tasks)`` 决定，而非某个固定上限；
  * stop 标志是唯一的外部边界（单步超时/手动停止由此进入）；
  * 源码特征化：循环头部是 ``while index < len(tasks)``，且方法体内没有
    “迭代计数 vs 上限” 的硬编码上限守卫（P4 加入 cap 后此特征会改变）。
"""

from __future__ import annotations

import inspect
import unittest
from unittest import mock

from fukua_rpa.engine import RPAEngine
from fukua_rpa.step_runtime import LoopExecutionResult, StepDispatchResult


def _make_engine() -> RPAEngine:
    # 与仓库现有 engine 测试一致：屏蔽高优先级设置，避免触碰 Windows 调度。
    with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
        engine = RPAEngine()
    engine.log_level = -1
    return engine


class LoopSchedulerCapR1Regression(unittest.TestCase):
    def _drive_loop(self, tasks, dispatch_side_effect, stop_flag=None):
        engine = _make_engine()
        engine.check_stop_flag = (
            stop_flag if stop_flag is not None else mock.Mock(return_value=False)
        )
        engine._task_skipped_for_loop = mock.Mock(return_value=False)
        engine._debug_allows_step = mock.Mock(return_value=True)
        engine._dispatch_step = mock.Mock(side_effect=dispatch_side_effect)
        result = engine._execute_loop_steps(
            tasks, loop_count=1, start_index=0, callback_msg=None
        )
        return engine, result

    def test_loop_terminates_on_task_completion(self):
        """5 个任务、dispatch 每次前进 1 步 -> 恰好执行 5 次并正常终止。"""
        tasks = [{"type": 4.0, "value": str(i)} for i in range(5)]

        def dispatch(_tasks, _task, index, _loop_count):
            return StepDispatchResult(next_index=index + 1, made_progress=True)

        engine, result = self._drive_loop(tasks, dispatch)
        self.assertEqual(engine._dispatch_step.call_count, 5)
        self.assertIsInstance(result, LoopExecutionResult)
        self.assertTrue(result.made_progress)
        self.assertFalse(result.stop_run)

    def test_loop_does_not_truncate_large_task_list(self):
        """行为证明：完成边界由 len(tasks) 决定，而非某个固定上限。

        若循环存在硬编码 cap（例如 1000），一个 50 任务的列表仍能完整跑完，
        说明当前并无此类截断；这正是 P4 加 cap 后会改变的前提。
        """
        n = 50
        tasks = [{"type": 4.0, "value": str(i)} for i in range(n)]

        def dispatch(_tasks, _task, index, _loop_count):
            return StepDispatchResult(next_index=index + 1, made_progress=True)

        engine, result = self._drive_loop(tasks, dispatch)
        self.assertEqual(engine._dispatch_step.call_count, n)
        self.assertFalse(result.stop_run)

    def test_manual_stop_is_the_only_external_bound(self):
        """stop 标志是唯一的外部边界：在第 3 次进入循环前置位 -> 仅执行 2 步。"""
        tasks = [{"type": 4.0, "value": str(i)} for i in range(10)]
        state = {"entered": 0}

        def stop_flag():
            state["entered"] += 1
            return state["entered"] >= 3  # 第 3 次进入循环顶部时返回 True

        def dispatch(_tasks, _task, index, _loop_count):
            return StepDispatchResult(next_index=index + 1, made_progress=True)

        engine, result = self._drive_loop(tasks, dispatch, stop_flag=stop_flag)
        self.assertEqual(engine._dispatch_step.call_count, 2)
        self.assertTrue(result.stop_run)

    def test_loop_has_no_explicit_iteration_cap(self):
        """源码特征化：当前没有显式迭代上限守卫（P4 加 cap 后此断言需扩展）。

        断言循环头部为 ``while index < len(tasks)``，且方法体内不存在
        “迭代计数 vs 上限” 的硬编码守卫模式。
        """
        src = inspect.getsource(RPAEngine._execute_loop_steps)
        self.assertIn("while index < len(tasks)", src)

        cap_patterns = [
            r"max_?iter",
            r"iteration_?cap",
            r"MAX_STEPS",
            r"STEP_CAP",
            r"step_?cap",
            r"iterations?\s*>=\s*\w+",
            r"iteration_count\s*>\s*\w+",
            r"loop_step_count\s*>\s*\w+",
        ]
        for pattern in cap_patterns:
            self.assertNotRegex(
                src, pattern, msg=f"检测到疑似迭代上限守卫: {pattern}"
            )


if __name__ == "__main__":
    unittest.main()
