# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Runtime variables and safe expression actions for the execution engine."""

from __future__ import annotations

import time

from fukua_rpa.expressions import (
    BUILTIN_VARIABLE_NAMES,
    ExpressionError,
    compile_expression,
    evaluate_expression,
    parse_assignment,
    validate_variable_name,
)

MAX_RUNTIME_VARIABLES = 128


class ExpressionExecutionMixin:
    """Manage runtime variables and safe expression evaluation for the engine."""

    # FR-20260809-D1-①: 表达式/赋值 AST 缓存容量上限（防动态源无界增长）。
    _EXPRESSION_CACHE_LIMIT = 512

    def reset_expression_runtime(self):
        """Reset all runtime variables, caches, and timing for a new run."""
        self.runtime_variables = {}
        self.expression_cache = {}
        self.assignment_cache = {}
        self.run_started_monotonic = time.monotonic()
        self.last_step_success = False

    def expression_context(self, step_info):
        """Build the variable mapping available to expression evaluation.

        Args:
            step_info: Dictionary with step/loop/attempt metadata.

        Returns:
            Dictionary mapping variable names to their current values.
        """
        step = int(step_info.get("step", 0) or 0)
        values = dict(self.runtime_variables)
        values.update(
            {
                "loop": int(step_info.get("loop", 0) or 0),
                "step": step,
                "attempt": int(step_info.get("attempt", 1) or 1),
                "success_count": int(step_info.get("success_count", 0) or 0),
                "execution_count": int(self.step_execution_counts.get(step, 0)),
                "elapsed": max(0.0, time.monotonic() - self.run_started_monotonic),
                "last_success": bool(self.last_step_success),
            }
        )
        return values

    def evaluate_runtime_expression(self, expression, step_info):
        """Compile (with caching) and evaluate an expression string.

        Args:
            expression: Expression source text.
            step_info: Dictionary with step metadata for context variables.

        Returns:
            The evaluated result value.

        Raises:
            ExpressionError: If compilation or evaluation fails.
        """
        source = str(expression or "").strip()
        tree = self.expression_cache.get(source)
        if tree is None:
            # FR-20260809-D1-①: 有界缓存——动态拼接的表达式源可能产生无限增长的
            # key；超限整体清空（AST 重算成本低），防止长时运行内存无界增长。
            if len(self.expression_cache) >= self._EXPRESSION_CACHE_LIMIT:
                self.expression_cache.clear()
            tree = compile_expression(source)
            self.expression_cache[source] = tree
        return evaluate_expression(tree, self.expression_context(step_info))

    def evaluate_breakpoint_condition(self, expression, step_info):
        """Evaluate a conditional breakpoint expression safely.

        Args:
            expression: Condition expression text (empty means always true).
            step_info: Dictionary with step metadata for context variables.

        Returns:
            Tuple of (active: bool, error_detail: str). On parse error the
            breakpoint remains active and error_detail describes the failure.
        """
        source = str(expression or "").strip()
        if not source:
            return True, ""
        try:
            return bool(self.evaluate_runtime_expression(source, step_info)), ""
        except ExpressionError as error:
            # A broken condition must never make a requested breakpoint vanish.
            return True, str(error)

    def set_runtime_variable(self, assignment_text, step_info):
        """Parse and execute a variable assignment expression.

        Args:
            assignment_text: Assignment source text (e.g. "x = loop + 1").
            step_info: Dictionary with step metadata for context variables.

        Returns:
            Tuple of (variable_name, assigned_value).

        Raises:
            ExpressionError: If parsing fails or variable limit is exceeded.
        """
        source = str(assignment_text or "").strip()
        assignment = self.assignment_cache.get(source)
        if assignment is None:
            # FR-20260809-D1-①: 有界缓存（同 expression_cache，见上）。
            if len(self.assignment_cache) >= self._EXPRESSION_CACHE_LIMIT:
                self.assignment_cache.clear()
            assignment = parse_assignment(source)
            self.assignment_cache[source] = assignment
        if (
            assignment.name not in self.runtime_variables
            and len(self.runtime_variables) >= MAX_RUNTIME_VARIABLES
        ):
            raise ExpressionError(
                f"本次运行最多允许 {MAX_RUNTIME_VARIABLES} 个自定义变量。"
            )
        value = evaluate_expression(
            assignment.tree, self.expression_context(step_info)
        )
        self.runtime_variables[assignment.name] = value
        return assignment.name, value

    def store_runtime_variable(self, name, value):
        """Store a pre-computed value into a named runtime variable.

        Args:
            name: Variable name (will be validated and normalized).
            value: Value to store.

        Returns:
            Tuple of (normalized_name, value).

        Raises:
            ExpressionError: If the name is invalid or variable limit is exceeded.
        """
        normalized = validate_variable_name(str(name or "").strip())
        if (
            normalized not in self.runtime_variables
            and len(self.runtime_variables) >= MAX_RUNTIME_VARIABLES
        ):
            raise ExpressionError(
                f"本次运行最多允许 {MAX_RUNTIME_VARIABLES} 个自定义变量。"
            )
        self.runtime_variables[normalized] = value
        return normalized, value

    def expression_debug_snapshot(self, step=0, loop=0):
        """Return a sorted snapshot of all visible variables for debugging.

        Args:
            step: Step number for context computation.
            loop: Loop number for context computation.

        Returns:
            Dictionary of variable names to values (builtins + user variables).
        """
        context = self.expression_context(
            {"step": int(step or 0), "loop": int(loop or 0)}
        )
        return {
            name: context[name]
            for name in sorted(context)
            if name in BUILTIN_VARIABLE_NAMES or name in self.runtime_variables
        }
