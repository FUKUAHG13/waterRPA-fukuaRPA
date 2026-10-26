import unittest
from unittest import mock

from fukua_rpa.engine import RPAEngine
from fukua_rpa.expressions import (
    ExpressionError,
    compile_expression,
    evaluate_expression,
    parse_assignment,
)
from fukua_rpa.validation import validate_task_list

BASE_CONFIG = {
    "conf": "0.8",
    "scale_min": "1",
    "scale_max": "1",
    "scale_step": "0.1",
    "loop_mode": "单次",
    "loop_val": "1",
    "start_step": "1",
    "loop_start_round": "1",
    "loop_end_round": "0",
}


class SafeExpressionTests(unittest.TestCase):
    def test_arithmetic_boolean_and_comparison(self):
        value = evaluate_expression(
            "count * 2 >= 6 and last_success",
            {"count": 3, "last_success": True},
        )
        self.assertIs(value, True)

    def test_assignment_has_single_plain_target(self):
        assignment = parse_assignment("count = count + 1")
        self.assertEqual(assignment.name, "count")
        self.assertEqual(
            evaluate_expression(assignment.tree, {"count": 4}), 5
        )

    def test_builtin_variables_cannot_be_overwritten(self):
        with self.assertRaises(ExpressionError):
            parse_assignment("loop = 10")

    def test_calls_attributes_subscripts_and_collections_are_rejected(self):
        for expression in (
            "open('x')",
            "value.real",
            "value[0]",
            "[x for x in value]",
            "{'x': 1}",
        ):
            with self.subTest(expression=expression), self.assertRaises(ExpressionError):
                compile_expression(expression)

    def test_unbounded_results_are_rejected(self):
        with self.assertRaises(ExpressionError):
            evaluate_expression("2 ** 100", {})
        with self.assertRaises(ExpressionError):
            evaluate_expression("'x' * 2000", {})


class ExpressionEngineTests(unittest.TestCase):
    def create_engine(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine()
        engine.log_level = -1
        engine.detect_delay = 0
        engine.settlement_wait = 0
        engine.load_and_precompute = lambda _tasks: True
        return engine

    def test_runtime_variables_and_true_expression_execute(self):
        engine = self.create_engine()
        engine.run_tasks(
            [
                {"type": 16.0, "value": "count = 1"},
                {"type": 16.0, "value": "count = count + 1"},
                {"type": 17.0, "value": "count == 2 and loop == 1"},
            ]
        )
        self.assertEqual(engine.runtime_variables["count"], 2)
        statuses = [
            event.get("status")
            for event in engine.last_run_trace.get("events", [])
            if event.get("event") == "step_result"
        ]
        self.assertIn("condition_true", statuses)

    def test_false_expression_uses_failure_branch(self):
        engine = self.create_engine()
        engine.run_tasks(
            [
                {"type": 16.0, "value": "count = 1"},
                {"type": 17.0, "value": "count > 5", "fail_jump": "4"},
                {"type": 16.0, "value": "marker = 100"},
                {"type": 16.0, "value": "marker = 200"},
            ]
        )
        self.assertEqual(engine.runtime_variables["marker"], 200)
        self.assertNotIn(100, engine.runtime_variables.values())

    def test_validation_rejects_executable_python_syntax(self):
        error = validate_task_list(
            [{"type": 17.0, "value": "__import__('os').system('cmd')"}],
            BASE_CONFIG,
        )
        self.assertIn("不允许", error)

    def test_validation_accepts_assignment_and_expression_commands(self):
        error = validate_task_list(
            [
                {"type": 16.0, "value": "answer = 42"},
                {"type": 17.0, "value": "answer == 42"},
            ],
            BASE_CONFIG,
        )
        self.assertIsNone(error)

    def test_validation_rejects_unsafe_conditional_breakpoint(self):
        error = validate_task_list(
            [
                {
                    "type": 4.0,
                    "value": "text",
                    "debug_breakpoint": True,
                    "debug_condition": "__import__('os')",
                }
            ],
            BASE_CONFIG,
        )
        self.assertIn("断点条件", error)
        self.assertIn("不允许", error)


if __name__ == "__main__":
    unittest.main()
