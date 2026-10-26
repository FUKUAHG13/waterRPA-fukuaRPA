"""FR-20260811-D13-002 裁定回归：共享停止标志不得跨运行态残留。

引擎域裁定（2026-08-12）：flow.py `_should_stop` 保留"工具级谓词与引擎共享
停止标志取或"语义；本测试锁定 `RunLifecycle.finish`/`reserve` 都会 clear()
停止标志，保证连点器等工具不会被上一轮主脚本的残留停止标志误停。
"""
from __future__ import annotations

import unittest

from fukua_rpa.runtime_state import RunLifecycle, RunState


class StopFlagLifetimeTests(unittest.TestCase):
    def test_finish_clears_stop_flag(self):
        lifecycle = RunLifecycle()
        run_id = lifecycle.reserve()
        lifecycle.mark_running(run_id)
        lifecycle.request_stop()
        self.assertTrue(lifecycle.stop_requested)
        lifecycle.finish(run_id, outcome="cancelled")
        # 主脚本终止后共享停止标志必须清除，避免残留影响后续工具（连点器）。
        self.assertFalse(lifecycle.stop_requested)
        self.assertEqual(lifecycle.state, RunState.IDLE)

    def test_reserve_clears_stop_flag_before_next_run(self):
        lifecycle = RunLifecycle()
        run_id = lifecycle.reserve()
        lifecycle.request_stop()
        lifecycle.finish(run_id)
        # 下一轮 reserve 再次清空（双保险）。
        next_run = lifecycle.reserve()
        self.assertIsNotNone(next_run)
        self.assertFalse(lifecycle.stop_requested)

    def test_idle_request_stop_does_not_set_flag(self):
        """IDLE 状态下 request_stop 返回 False 且不置位——连点器运行时主脚本
        处于 IDLE（互斥），按主脚本停止热键不会污染共享标志。"""
        lifecycle = RunLifecycle()
        self.assertFalse(lifecycle.request_stop())
        self.assertFalse(lifecycle.stop_requested)


if __name__ == "__main__":
    unittest.main()
