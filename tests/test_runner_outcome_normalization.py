"""FR-701-002 回归测试：Runner 终局只允许 ARCHITECTURE #73 的五分类。

引擎异常透传的 "error"、停止态改写等非白名单值必须归一为 crashed（异常终止），
避免计划日志/诊断出现契约之外枚举（#73）。"""

import unittest

from fukua_rpa.runner.isolated import (
    RUNNER_OUTCOME_ALLOWED,
    _normalize_runner_outcome,
)


class NormalizeRunnerOutcomeTests(unittest.TestCase):
    """FR-701-002: _normalize_runner_outcome 五分类白名单。"""

    def test_allows_five_canonical_outcomes(self):
        for outcome in (
            "finished",
            "user_stopped",
            "forced_stop",
            "startup_failed",
            "crashed",
        ):
            self.assertEqual(_normalize_runner_outcome(outcome), outcome)

    def test_error_maps_to_crashed(self):
        # 引擎异常未收 finished 时 _build_exit_report 曾输出 "error"；#73 不允许。
        self.assertEqual(_normalize_runner_outcome("error"), "crashed")

    def test_unknown_and_empty_map_to_crashed(self):
        # 引擎内部终局 stopped / preload_failed 已被 FR-RUNNER-002 修复为对齐 #73；
        # 仅真正未知/空值仍归一为 crashed。
        self.assertEqual(_normalize_runner_outcome(""), "crashed")
        self.assertEqual(_normalize_runner_outcome("whatever"), "crashed")

    def test_engine_internal_outcomes_map_to_terminal_classes(self):
        # FR-RUNNER-002：引擎内部终局经 finished 通道到达时须对齐 #73 五分类，
        # 不再被错误归一为 crashed，也不应触发"通信中断"误报。
        self.assertEqual(_normalize_runner_outcome("stopped"), "user_stopped")
        self.assertEqual(_normalize_runner_outcome("preload_failed"), "startup_failed")

    def test_whitelist_matches_documented_set(self):
        self.assertEqual(
            RUNNER_OUTCOME_ALLOWED,
            frozenset(
                {
                    "finished",
                    "user_stopped",
                    "forced_stop",
                    "startup_failed",
                    "crashed",
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()
