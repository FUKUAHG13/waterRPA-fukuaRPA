"""Regression tests for invariant #81: hidden step execution.

A step marked ``editor_hidden`` is a UI-only display flag (see
task_model.TASK_DATA_DEFAULTS: "editor_hidden and editor_groups organize the
editor only. They do not disable execution; step_disabled is the sole
persisted disable flag.").  The engine must therefore STILL execute hidden
steps; the hidden flag must not feed the execution-skip decision.

These tests drive the engine's pure skip/runnable decision logic directly
(RPAEngine is constructed headlessly, no Qt and no real automation).
"""

import unittest
from unittest import mock

from fukua_rpa.engine import RPAEngine


class HiddenStepInvariantTests(unittest.TestCase):
    def create_engine(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine()
        engine.log_level = -1
        engine.detect_delay = 0
        engine.settlement_wait = 0
        engine.load_and_precompute = lambda _tasks: True
        return engine

    def test_editor_hidden_step_is_not_exhausted_for_run(self):
        engine = self.create_engine()
        hidden = {"type": 0.0, "editor_hidden": True, "step_disabled": False}
        self.assertFalse(engine.task_exhausted_for_run(hidden, 1))

        # Negative control: a genuinely disabled step IS exhausted (skipped).
        disabled = {"type": 0.0, "editor_hidden": True, "step_disabled": True}
        self.assertTrue(engine.task_exhausted_for_run(disabled, 1))

    def test_editor_hidden_step_is_not_skipped_in_loop(self):
        engine = self.create_engine()
        hidden = {"type": 0.0, "editor_hidden": True, "step_disabled": False}
        self.assertFalse(
            engine._task_skipped_for_loop(hidden, index=0, loop_count=1)
        )

    def test_editor_hidden_step_is_runnable_in_loop(self):
        engine = self.create_engine()
        hidden = {
            "type": 0.0,
            "editor_hidden": True,
            "step_disabled": False,
            "jump_only": False,
        }
        self.assertTrue(engine.task_runnable_in_loop(hidden, step_no=1, loop_count=1))

    def test_editor_hidden_does_not_appear_in_skip_decision(self):
        # The skip decision must consult step_disabled (not editor_hidden).
        # Toggling editor_hidden alone must not change runnability.
        engine = self.create_engine()
        visible = {"type": 0.0, "editor_hidden": False, "step_disabled": False}
        hidden = {"type": 0.0, "editor_hidden": True, "step_disabled": False}
        self.assertEqual(
            engine.task_exhausted_for_run(visible, 1),
            engine.task_exhausted_for_run(hidden, 1),
        )


if __name__ == "__main__":
    unittest.main()
