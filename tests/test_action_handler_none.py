"""Regression test for report S4.

A step action handler that returns ``None`` (or any non-status value) used to
be silently treated as a successful step (or a graceful stop) because the
dispatch only classified recognized status strings. It must now be coerced to
an explicit ``"error"`` outcome so the failed attempt fails loudly.
"""

import unittest
from contextlib import contextmanager
from unittest import mock

from fukua_rpa.actions.shared import ACTION_HANDLERS
from fukua_rpa.engine import RPAEngine
from fukua_rpa.params import TaskOnceParams

_SENTINEL_COMMAND = 999.0


@contextmanager
def _sentinel_handler(handler):
    original = ACTION_HANDLERS.get(_SENTINEL_COMMAND)
    ACTION_HANDLERS[_SENTINEL_COMMAND] = handler
    try:
        yield
    finally:
        if original is None:
            ACTION_HANDLERS.pop(_SENTINEL_COMMAND, None)
        else:
            ACTION_HANDLERS[_SENTINEL_COMMAND] = original


class ActionHandlerNoneTests(unittest.TestCase):
    def _make_engine(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine()
        engine.log_level = -1
        engine.detect_delay = 0
        engine.settlement_wait = 0
        engine.load_and_precompute = lambda _tasks: True
        return engine

    def test_handler_returning_none_is_coerced_to_error(self):
        engine = self._make_engine()

        def _none_handler(self, context):
            return None

        with _sentinel_handler(_none_handler):
            result = engine.execute_task_once(TaskOnceParams(
                cmd=_SENTINEL_COMMAND,
                val="0",
                retry=-1,
                step_info={"loop": 1, "step": 1},
                cache_key="key",
                task_conf=0.8,
                use_gray=False,
            ))
        self.assertEqual(result, "error")

    def test_handler_returning_non_str_is_coerced_to_error(self):
        engine = self._make_engine()

        def _int_handler(self, context):
            return 0

        with _sentinel_handler(_int_handler):
            result = engine.execute_task_once(TaskOnceParams(
                cmd=_SENTINEL_COMMAND,
                val="0",
                retry=-1,
                step_info={"loop": 1, "step": 1},
                cache_key="key",
                task_conf=0.8,
                use_gray=False,
            ))
        self.assertEqual(result, "error")

    def test_valid_string_status_is_passed_through(self):
        engine = self._make_engine()

        def _ok_handler(self, context):
            return "success"

        with _sentinel_handler(_ok_handler):
            result = engine.execute_task_once(TaskOnceParams(
                cmd=_SENTINEL_COMMAND,
                val="0",
                retry=-1,
                step_info={"loop": 1, "step": 1},
                cache_key="key",
                task_conf=0.8,
                use_gray=False,
            ))
        self.assertEqual(result, "success")


if __name__ == "__main__":
    unittest.main()
