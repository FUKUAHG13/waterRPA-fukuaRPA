"""Regression tests for P1-2: process-internal max_runtime cap reaches the engine.

This file pins two facts about the scheduled-run max_runtime ceiling:

1. (FIXED BUG) When a scheduled run supplies ``max_runtime_seconds``, the value
   is rewritten into the run config's ``timeout`` and applied to the shared
   in-process engine via ``EngineRunConfig.from_mapping(cfg).apply_to(engine)``
   (see ``fukua_rpa.ui.controllers.execution_controller.ExecutionController
   ._launch_prepared_tasks``). The cap therefore reaches ``engine.timeout_val``
   instead of being silently ignored.

2. (RESIDUAL SMELL / BASELINE) ``fukua_rpa.runner.backend.InProcessRunnerSession``
   is still constructed as ``InProcessRunnerSession(engine, tasks)`` (fukua_rpa/runner/backend.py:121)
   with no ``config`` argument. The in-process worker reuses the host engine, so
   this is acceptable today, but the missing parameter is a documented baseline:
   if a future change adds a ``config`` knob here, these assertions must be
   revisited.
"""

from __future__ import annotations

import inspect
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest import mock

from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.engine import RPAEngine
from fukua_rpa.runner.backend import (
    RUNNER_MODE_IN_PROCESS,
    InProcessRunnerSession,
    create_runner_session,
)
from fukua_rpa.ui.controllers.execution_controller import ExecutionController


class MaxRuntimeP1RegressionTests(unittest.TestCase):
    def _make_mocked_host(self, engine):
        """Build a MagicMock host good enough to drive _launch_prepared_tasks.

        We bypass the Qt-heavy public entry points and call the private launch
        helper directly, stubbing every GUI/native piece so no Qt event loop or
        real runner is started.
        """

        host = mock.MagicMock()
        # Make `range(host.task_list.count())` iterate zero times.
        host.task_list.count.return_value = 0
        # Skip the LOG_RUN branch (avoids touching more cfg fields + log UI).
        host.current_log_policy.return_value = mock.MagicMock(
            allows=mock.MagicMock(return_value=False)
        )
        # run_controller.start must return a 2-tuple (started, error).
        host.run_controller.start.return_value = (True, None)
        # No plugin registry -> plugin acquire/ref-count path is skipped.
        host.plugin_registry = None
        host.engine = engine
        host.runtime_ready = True
        return host

    def _apply_max_runtime_through_controller(self, max_runtime_seconds, existing_timeout=None):
        """Drive a scheduled-style run through the P1-2 public apply path.

        If ``existing_timeout`` is provided, the incoming run config already
        carries that timeout (simulating a profile/config that set one before
        the cap), so the interaction between a pre-existing timeout and the cap
        can be asserted.
        """

        engine = RPAEngine(defer_backends=True)  # no native/Qt backend init
        host = self._make_mocked_host(engine)
        controller = ExecutionController(host)

        cfg = default_profile_config()
        if existing_timeout is not None:
            cfg["timeout"] = str(existing_timeout)
            # Simulate the earlier profile apply_to(engine) that configured the
            # engine with this timeout before the cap is considered.
            engine.timeout_val = float(existing_timeout)
        controller._launch_prepared_tasks(
            tasks=[],
            cfg=cfg,
            run_label="P1-2 regression",
            max_runtime_seconds=max_runtime_seconds,
            plugin_dependencies=(),
        )
        return engine

    def test_p1_2_max_runtime_cap_reaches_engine_timeout_val(self):
        """The fixed bug: max_runtime_seconds is applied to engine.timeout_val."""

        engine = self._apply_max_runtime_through_controller(max_runtime_seconds=5)
        # P1-2 fix: the cap is rewritten into cfg["timeout"] and applied to the
        # engine, so timeout_val must equal the requested ceiling.
        self.assertEqual(engine.timeout_val, 5.0)

    def test_p1_2_larger_existing_timeout_is_shortened_to_cap(self):
        """A too-long pre-existing timeout must be lowered to the cap.

        When the incoming config already carries a timeout larger than
        ``max_runtime_seconds``, ``_launch_prepared_tasks`` rewrites it down to
        the cap so the run cannot outlive the scheduled limit.
        """
        engine = self._apply_max_runtime_through_controller(
            max_runtime_seconds=5, existing_timeout=10
        )
        self.assertEqual(engine.timeout_val, 5.0)

    def test_p1_2_smaller_existing_timeout_is_left_unchanged(self):
        """A reasonable pre-existing timeout below the cap is not raised to it.

        The cap only ever *lowers* an over-long timeout; it must not extend a
        shorter one, which would artificially lengthen the run.
        """
        engine = self._apply_max_runtime_through_controller(
            max_runtime_seconds=5, existing_timeout=2
        )
        self.assertEqual(engine.timeout_val, 2.0)

    def test_p1_2_missing_cap_does_not_overwrite_existing_timeout(self):
        """Without a cap the launch helper must NOT rewrite engine.timeout_val.

        In a real scheduled run the engine is configured earlier by
        ``run_request.config.apply_to(self.host.engine)``; ``_launch_prepared_tasks``
        only touches the timeout when a ``max_runtime_seconds`` ceiling is present.
        So with no cap supplied, the engine keeps whatever was already applied.
        """

        engine = RPAEngine(defer_backends=True)
        engine.timeout_val = 12.0  # simulate the earlier apply_to(engine)
        host = self._make_mocked_host(engine)
        controller = ExecutionController(host)

        cfg = default_profile_config()
        controller._launch_prepared_tasks(
            tasks=[],
            cfg=cfg,
            run_label="no-cap regression",
            max_runtime_seconds=None,  # scheduled cap not supplied
            plugin_dependencies=(),
        )
        self.assertEqual(engine.timeout_val, 12.0)
        # Sanity: EngineRunConfig still applies a timeout when present, so the
        # cap is the *only* path that rewrites it inside this helper.
        engine.timeout_val = 0.0
        engine2 = self._apply_max_runtime_through_controller(max_runtime_seconds=5)
        self.assertNotEqual(engine2.timeout_val, 0.0)

    def test_in_process_session_accepts_log_context_but_not_profile_config(self):
        """Run attribution is explicit without coupling the session to UI config."""

        params = [
            name
            for name in inspect.signature(InProcessRunnerSession.__init__).parameters
            if name != "self"
        ]
        self.assertEqual(params, ["engine", "tasks", "log_context"])
        self.assertNotIn("config", params)

    def test_in_process_factory_forwards_log_context_not_profile_config(self):
        """Factory forwards run attribution while profile config stays upstream."""

        fake_engine = mock.MagicMock()
        fake_tasks = mock.MagicMock()
        sentinel_config = {"hotkey_start": "F9", "tasks": []}
        sentinel_log_context = {"profile_name": "测试方案", "source": "manual"}

        with mock.patch(
            "fukua_rpa.runner.backend.InProcessRunnerSession",
            autospec=False,
            side_effect=lambda *a, **k: mock.Mock(spec=InProcessRunnerSession),
        ) as patched:
            session = create_runner_session(
                RUNNER_MODE_IN_PROCESS,
                fake_engine,
                fake_tasks,
                config=sentinel_config,
                log_context=sentinel_log_context,
            )

        patched.assert_called_once_with(
            fake_engine, fake_tasks, sentinel_log_context
        )
        call_kwargs = patched.call_args.kwargs
        self.assertNotIn("config", call_kwargs)
        self.assertIsInstance(session, mock.Mock)


if __name__ == "__main__":
    unittest.main()
