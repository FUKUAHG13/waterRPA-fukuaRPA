"""P4b guard test: verify engine_actions.py split into actions_* mixins.

Mechanical split only: public API (ActionExecutionMixin, StepActionContext,
registered_action_codes, the dataclasses, ACTION_HANDLERS) must be intact and
every registered action handler reachable through the composed mixin.
"""
import unittest

import fukua_rpa.engine.actions_exec as engine_actions_module
from fukua_rpa.actions.flow import ActionsFlowMixin
from fukua_rpa.actions.image import ActionsImageMixin
from fukua_rpa.actions.input_timeline import ActionsInputTimelineMixin
from fukua_rpa.actions.keyboard import ActionsKeyboardMixin
from fukua_rpa.actions.mouse import ActionsMouseMixin
from fukua_rpa.actions.shared import ActionsSharedMixin
from fukua_rpa.actions.text import ActionsTextMixin
from fukua_rpa.actions.window import ActionsWindowMixin
from fukua_rpa.engine import RPAEngine
from fukua_rpa.engine.actions_exec import ActionExecutionMixin

MIXIN_BY_NAME = {
    "ActionsSharedMixin": ActionsSharedMixin,
    "ActionsMouseMixin": ActionsMouseMixin,
    "ActionsKeyboardMixin": ActionsKeyboardMixin,
    "ActionsTextMixin": ActionsTextMixin,
    "ActionsWindowMixin": ActionsWindowMixin,
    "ActionsImageMixin": ActionsImageMixin,
    "ActionsFlowMixin": ActionsFlowMixin,
    "ActionsInputTimelineMixin": ActionsInputTimelineMixin,
}


class EngineActionsSplitTests(unittest.TestCase):
    def test_submodules_import_cleanly(self):
        # importing must not raise (no circular import)
        for mod in [
            engine_actions_module,
            ActionsSharedMixin,  # placeholder to keep import used
        ]:
            self.assertIsNotNone(mod)

    def test_facade_composes_all_mixins(self):
        mro = [c.__name__ for c in ActionExecutionMixin.__mro__]
        for mixin in [
            "ActionsSharedMixin",
            "ActionsMouseMixin",
            "ActionsKeyboardMixin",
            "ActionsTextMixin",
            "ActionsWindowMixin",
            "ActionsImageMixin",
            "ActionsFlowMixin",
            "ActionsInputTimelineMixin",
        ]:
            self.assertIn(mixin, mro, f"{mixin} missing from MRO")

    def test_action_handlers_registered(self):
        # 29 @action_handler-decorated handlers must be registered after import
        # （29.0 = 录制输入时序，批次 2 新增）
        self.assertEqual(len(engine_actions_module.ACTION_HANDLERS), 29)
        self.assertEqual(
            engine_actions_module.registered_action_codes(),
            frozenset(engine_actions_module.ACTION_HANDLERS),
        )
        # sanity: a couple of well-known codes are present
        self.assertIn(1.0, engine_actions_module.ACTION_HANDLERS)
        self.assertIn(2.0, engine_actions_module.ACTION_HANDLERS)

    def test_engine_still_constructs_and_has_action_api(self):
        engine = RPAEngine(defer_backends=True)
        for method in [
            "execute_task_once",
            "_execute_task_once_impl",
            "_execute_left_click",
            "mouseClick",
            "mouseDrag",
            "_execute_text",
            "_execute_hotkey",
            "_execute_launch_application",
            "_execute_screenshot",
            "_execute_plugin_action",
            # shared primitives
            "_bounded_action_duration",
            "release_injected_inputs",
        ]:
            self.assertTrue(
                hasattr(engine, method), f"RPAEngine missing action method {method}"
            )

    def test_expected_method_counts(self):
        import types

        expected = {
            "ActionsSharedMixin": 9,
            "ActionsMouseMixin": 34,
            "ActionsKeyboardMixin": 1,
            "ActionsTextMixin": 3,
            "ActionsWindowMixin": 4,
            "ActionsImageMixin": 7,
            "ActionsFlowMixin": 16,
        }
        for mixin_name, count in expected.items():
            mixin = MIXIN_BY_NAME[mixin_name]
            method_defs = [
                k
                for k, v in mixin.__dict__.items()
                if isinstance(v, (types.FunctionType, staticmethod, classmethod))
            ]
            self.assertEqual(
                len(method_defs),
                count,
                f"{mixin_name} expected {count} methods, got {len(method_defs)}",
            )


if __name__ == "__main__":
    unittest.main()
