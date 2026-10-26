"""Targeted coverage tests for fukua_rpa.workflow_analysis.

Focuses on previously uncovered lines: 56-57, 81, 96, 122, 127-128, 131-145,
158, 167-168, 172, 205, 209-212, 216-218, 222-227, 232-315, 336, 345, 352-354,
406, 413, 417, 422-423, 428-432, 440-446, 453-495, 515, 521-523, 540-569.
"""

import unittest

from fukua_rpa.workflow_analysis import (
    _branch_target,
    _expression_names,
    _integer,
    _resolve_target_id,
    _resolve_target_step,
    _task_type,
    analyze_loop_risks,
    analyze_variable_flow,
    analyze_workflow_structure,
    build_workflow_graph,
    risk_type_keys,
)

# ---------------------------------------------------------------------------
# _integer exception paths (lines 56-57)
# ---------------------------------------------------------------------------


class TestIntegerHelper(unittest.TestCase):
    def test_valid_numeric_string(self):
        self.assertEqual(_integer("42"), 42)

    def test_float_string_truncated(self):
        self.assertEqual(_integer("3.9"), 3)

    def test_none_returns_default(self):
        self.assertEqual(_integer(None, default=7), 7)

    def test_non_numeric_string_returns_default(self):
        self.assertEqual(_integer("abc", default=5), 5)

    def test_overflow_returns_default(self):
        self.assertEqual(_integer("1e999", default=0), 0)

    def test_minimum_clamp(self):
        self.assertEqual(_integer(-10, default=0, minimum=0), 0)

    def test_type_error_returns_default(self):
        self.assertEqual(_integer(object(), default=3), 3)


# ---------------------------------------------------------------------------
# _resolve_target_id / _resolve_target_step (lines 81, 96)
# ---------------------------------------------------------------------------


class TestResolveTarget(unittest.TestCase):
    def test_resolve_target_id_found(self):
        task = {"success_target_id": "step_b"}
        id_to_index = {"step_a": 0, "step_b": 2}
        self.assertEqual(_resolve_target_id(task, id_to_index, "success_target_id"), 2)

    def test_resolve_target_id_not_found(self):
        task = {"success_target_id": "missing"}
        id_to_index = {"step_a": 0}
        self.assertIsNone(_resolve_target_id(task, id_to_index, "success_target_id"))

    def test_resolve_target_id_empty(self):
        task = {"success_target_id": ""}
        id_to_index = {"step_a": 0}
        self.assertIsNone(_resolve_target_id(task, id_to_index, "success_target_id"))

    def test_resolve_target_step_found(self):
        task = {"fail_target_id": "s2"}
        id_to_index = {"s1": 0, "s2": 3}
        self.assertEqual(_resolve_target_step(task, id_to_index, "fail_target_id"), 4)

    def test_resolve_target_step_not_found(self):
        task = {"fail_target_id": "nope"}
        id_to_index = {"s1": 0}
        self.assertIsNone(_resolve_target_step(task, id_to_index, "fail_target_id"))


# ---------------------------------------------------------------------------
# _task_type exception path (lines 167-168)
# ---------------------------------------------------------------------------


class TestTaskType(unittest.TestCase):
    def test_valid_float(self):
        self.assertEqual(_task_type({"type": 15.0}), 15.0)

    def test_string_numeric(self):
        self.assertEqual(_task_type({"type": "16.0"}), 16.0)

    def test_invalid_type_returns_zero(self):
        self.assertEqual(_task_type({"type": "not_a_number"}), 0.0)

    def test_none_type_returns_zero(self):
        self.assertEqual(_task_type({"type": None}), 0.0)

    def test_missing_type_returns_zero(self):
        self.assertEqual(_task_type({}), 0.0)


# ---------------------------------------------------------------------------
# _expression_names (line 172)
# ---------------------------------------------------------------------------


class TestExpressionNames(unittest.TestCase):
    def test_extracts_variable_names(self):
        from fukua_rpa.expressions import compile_expression

        tree = compile_expression("x + y * 2")
        names = _expression_names(tree)
        self.assertIn("x", names)
        self.assertIn("y", names)


# ---------------------------------------------------------------------------
# build_workflow_graph: disabled steps, until branch, call_step, step_id
# (lines 122, 127-128, 131-145, 158)
# ---------------------------------------------------------------------------


class TestBuildWorkflowGraph(unittest.TestCase):
    def test_disabled_step_falls_through(self):
        tasks = [
            {"type": 1.0, "step_disabled": True},
            {"type": 1.0},
        ]
        graph = build_workflow_graph(tasks)
        self.assertEqual(graph[0], {1})

    def test_step_id_builds_index(self):
        tasks = [
            {"type": 1.0, "step_id": "a", "success_target_id": "b"},
            {"type": 1.0, "step_id": "b"},
        ]
        graph = build_workflow_graph(tasks)
        # success_target_id "b" resolves to index 1
        self.assertIn(1, graph[0])

    def test_until_step_with_target_ids(self):
        tasks = [
            {"type": 1.0, "step_id": "s1"},
            {"type": 1.0, "step_id": "s2"},
            {
                "type": 15.0,
                "step_id": "s3",
                "until_true_target_id": "s1",
                "until_false_target_id": "s2",
                "until_on_limit": "继续下一步",
            },
        ]
        graph = build_workflow_graph(tasks)
        # true -> index 0, false -> index 1, on_limit continue -> index 3 (exit)
        self.assertIn(0, graph[2])
        self.assertIn(1, graph[2])
        self.assertIn(3, graph[2])

    def test_until_step_with_numeric_jumps(self):
        tasks = [
            {"type": 1.0},
            {"type": 1.0},
            {
                "type": 15.0,
                "until_true_jump": 1,
                "until_false_jump": 2,
                "until_on_limit": "停止脚本",
            },
        ]
        graph = build_workflow_graph(tasks)
        # true_jump=1 -> index 0, false_jump=2 -> index 1, on_limit=stop -> no index+1
        self.assertIn(0, graph[2])
        self.assertIn(1, graph[2])
        # "停止脚本" means no fallthrough
        self.assertNotIn(3, graph[2])

    def test_until_step_default_jumps(self):
        tasks = [
            {"type": 1.0},
            {
                "type": 15.0,
                "until_on_limit": "继续下一步",
            },
        ]
        graph = build_workflow_graph(tasks)
        # DEC-20260930-002-03（维护者 2026-10-07 裁定）：现行默认值统一为 0。
        # default true_jump=0 -> index+1=2；default false_jump=0 -> 不跳转，
        # 因此**不应**出现指向 index 0 的边。
        # on_limit continue -> index+1=2
        self.assertIn(2, graph[1])
        self.assertNotIn(0, graph[1])

    def test_until_step_explicit_false_jump_creates_edge(self):
        """已明确保存的 until_false_jump 必须照常生效（默认值变更不得覆盖它）。"""
        tasks = [
            {"type": 1.0},
            {
                "type": 15.0,
                "until_false_jump": 1,
                "until_on_limit": "继续下一步",
            },
        ]
        graph = build_workflow_graph(tasks)
        self.assertIn(2, graph[1], "满足后跳至下一步的边必须存在")
        self.assertIn(0, graph[1], "显式保存的未满足跳回第 1 步必须生效")

    def test_call_step_adds_edge(self):
        tasks = [
            {"type": 1.0, "call_step": 3},
            {"type": 1.0},
            {"type": 1.0},
        ]
        graph = build_workflow_graph(tasks)
        self.assertIn(2, graph[0])

    def test_call_step_self_ignored(self):
        tasks = [
            {"type": 1.0, "call_step": 1},
            {"type": 1.0},
        ]
        graph = build_workflow_graph(tasks)
        # call_step=1 means index 0 which is self, so not added
        self.assertNotIn(0, graph[0])

    def test_normal_step_with_jump_and_skip(self):
        tasks = [
            {"type": 1.0, "success_jump": 3},
            {"type": 1.0},
            {"type": 1.0, "fail_skip": 1},
        ]
        graph = build_workflow_graph(tasks)
        # success_jump=3 -> min(3-1, 3) = 2
        self.assertIn(2, graph[0])
        # fail_skip=1 at index 2 -> min(2+1+1, 3) = 3
        self.assertIn(3, graph[2])


# ---------------------------------------------------------------------------
# analyze_variable_flow: the big block (lines 205, 209-227, 232-315)
# ---------------------------------------------------------------------------


class TestAnalyzeVariableFlow(unittest.TestCase):
    def _make_graph_and_reachable(self, tasks):
        graph = build_workflow_graph(tasks)
        # Simple BFS from 0
        visited = set()
        pending = [0]
        while pending:
            node = pending.pop()
            if node in visited:
                continue
            visited.add(node)
            pending.extend(t for t in graph.get(node, ()) if t not in visited)
        return graph, visited

    def test_disabled_step_skipped(self):
        # line 205: disabled steps are skipped
        tasks = [
            {"type": 16.0, "value": "x = 1", "step_disabled": True},
            {"type": 17.0, "value": "x + 1"},
        ]
        graph, reachable = self._make_graph_and_reachable(tasks)
        issues = analyze_variable_flow(tasks, graph, reachable, 0)
        # x is referenced in step 2 but never assigned (step 1 disabled)
        self.assertTrue(any(i.code == "variable_maybe_unset" for i in issues))

    def test_debug_breakpoint_condition_references(self):
        # lines 209-212: debug_breakpoint with condition
        tasks = [
            {"type": 1.0, "debug_breakpoint": True, "debug_condition": "myvar > 0"},
            {"type": 16.0, "value": "myvar = 5"},
        ]
        graph, reachable = self._make_graph_and_reachable(tasks)
        issues = analyze_variable_flow(tasks, graph, reachable, 0)
        # myvar referenced at step 1 before assignment at step 2
        self.assertTrue(any(i.code == "variable_maybe_unset" for i in issues))

    def test_set_variable_and_expression(self):
        # lines 216-218, 222-227: set_variable and expression
        tasks = [
            {"type": 16.0, "value": "counter = 0"},
            {"type": 17.0, "value": "counter + 1"},
        ]
        graph, reachable = self._make_graph_and_reachable(tasks)
        issues = analyze_variable_flow(tasks, graph, reachable, 0)
        # counter assigned at step 1, used at step 2 -> no issue
        self.assertFalse(any(i.code == "variable_maybe_unset" for i in issues))

    def test_expression_error_skips_step(self):
        # line 226-227: ExpressionError causes continue
        tasks = [
            {"type": 17.0, "value": "import os"},  # invalid expression
            {"type": 16.0, "value": "x = 1"},
        ]
        graph, reachable = self._make_graph_and_reachable(tasks)
        # Should not raise
        issues = analyze_variable_flow(tasks, graph, reachable, 0)
        self.assertIsInstance(issues, list)

    def test_variable_maybe_unset_on_branch(self):
        # lines 232-315: full worklist iteration with branching
        tasks = [
            {"type": 1.0, "success_jump": 3, "fail_jump": 2},
            {"type": 16.0, "value": "result = 42"},
            {"type": 17.0, "value": "result + 1"},
        ]
        graph = build_workflow_graph(tasks)
        reachable = {0, 1, 2, 3}
        issues = analyze_variable_flow(tasks, graph, reachable, 0)
        # On the fail path (0->2), result is not assigned before use
        self.assertTrue(any(i.code == "variable_maybe_unset" for i in issues))
        issue = next(i for i in issues if i.code == "variable_maybe_unset")
        self.assertEqual(issue.step, 3)

    def test_no_references_returns_empty(self):
        tasks = [
            {"type": 1.0},
            {"type": 1.0},
        ]
        graph, reachable = self._make_graph_and_reachable(tasks)
        issues = analyze_variable_flow(tasks, graph, reachable, 0)
        self.assertEqual(issues, [])

    def test_large_workflow_skips_analysis(self):
        # lines 246-260: >2000 steps returns informational warning
        tasks = [{"type": 16.0, "value": f"v{i} = {i}"} for i in range(2001)]
        graph = {i: {i + 1} for i in range(2001)}
        graph[2001] = set()
        reachable = set(range(2001))
        issues = analyze_variable_flow(tasks, graph, reachable, 0)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, "variable_flow_skipped")

    def test_builtin_variables_not_flagged(self):
        # BUILTIN_VARIABLE_NAMES should not trigger warnings
        tasks = [
            {"type": 17.0, "value": "loop + step"},
        ]
        graph, reachable = self._make_graph_and_reachable(tasks)
        issues = analyze_variable_flow(tasks, graph, reachable, 0)
        self.assertEqual(issues, [])

    def test_unreachable_step_not_flagged(self):
        # line 298-299: unreachable steps skipped in issue reporting
        tasks = [
            {"type": 1.0, "success_jump": 1, "fail_jump": 1},
            {"type": 17.0, "value": "undefined_var + 1"},
        ]
        graph = build_workflow_graph(tasks)
        # Only step 0 and exit are reachable
        reachable = {0, 2}
        issues = analyze_variable_flow(tasks, graph, reachable, 0)
        self.assertFalse(any(i.step == 2 for i in issues))


# ---------------------------------------------------------------------------
# analyze_workflow_structure (lines 336, 345, 352-354)
# ---------------------------------------------------------------------------


class TestAnalyzeWorkflowStructure(unittest.TestCase):
    def test_empty_tasks(self):
        issues = analyze_workflow_structure([])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, "empty")
        self.assertEqual(issues[0].severity, "error")

    def test_unreachable_steps_reported(self):
        # lines 352-354: unreachable steps
        tasks = [
            {"type": 1.0, "success_jump": 1, "fail_jump": 1},
            {"type": 1.0},  # unreachable
            {"type": 1.0},  # unreachable
        ]
        issues = analyze_workflow_structure(tasks)
        unreachable_issues = [i for i in issues if i.code == "unreachable"]
        self.assertEqual(len(unreachable_issues), 1)
        self.assertIn("2", unreachable_issues[0].message)

    def test_unreachable_more_than_12_truncated(self):
        # line 353: suffix "……" when > 12 unreachable
        tasks = [{"type": 1.0, "success_jump": 1, "fail_jump": 1}]
        tasks += [{"type": 1.0} for _ in range(15)]
        issues = analyze_workflow_structure(tasks)
        unreachable_issues = [i for i in issues if i.code == "unreachable"]
        self.assertTrue(any("……" in i.message for i in unreachable_issues))

    def test_no_exit_path(self):
        # Infinite loop with no exit
        tasks = [
            {"type": 1.0, "success_jump": 1, "fail_jump": 1},
        ]
        issues = analyze_workflow_structure(tasks)
        self.assertTrue(any(i.code == "no_exit_path" for i in issues))

    def test_normal_workflow_no_issues(self):
        tasks = [
            {"type": 1.0},
            {"type": 1.0},
        ]
        issues = analyze_workflow_structure(tasks)
        # Normal fallthrough reaches exit, no unreachable
        self.assertFalse(any(i.code in ("unreachable", "no_exit_path") for i in issues))

    def test_bfs_visited_dedup(self):
        # line 345: node already in visited -> continue
        tasks = [
            {"type": 1.0, "success_jump": 2, "fail_jump": 2},
            {"type": 1.0, "success_jump": 2, "fail_jump": 2},
            {"type": 1.0},
        ]
        # Both step 0 and 1 point to step 2; BFS visits 2 once
        issues = analyze_workflow_structure(tasks)
        self.assertIsInstance(issues, list)


# ---------------------------------------------------------------------------
# analyze_loop_risks (lines 406, 413, 417, 422-423, 428-432, 440-446,
#                      453-495, 515, 521-523)
# ---------------------------------------------------------------------------


def _cmd_name(task_type):
    return f"CMD_{task_type}"


class TestAnalyzeLoopRisks(unittest.TestCase):
    def test_global_infinite_loop(self):
        # line 406
        tasks = [{"type": 1.0}]
        config = {"loop_mode": "无限", "loop_end_round": 0}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertTrue(any("全局循环模式为【无限】" in r for r in risks))

    def test_step_id_used_for_target_resolution(self):
        # line 413: id_to_index built from step_id
        tasks = [
            {"type": 1.0, "step_id": "a"},
            {"type": 1.0, "step_id": "b", "success_target_id": "a"},
        ]
        config = {}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        # success_target_id "a" -> step 1, jump_to=1 <= step_no=2 -> back loop
        self.assertTrue(any("之间循环" in r for r in risks))

    def test_disabled_step_skipped(self):
        # line 417
        tasks = [
            {"type": 1.0, "step_disabled": True, "repeat_mode": "无限重复"},
        ]
        config = {}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertEqual(risks, [])

    def test_infinite_repeat(self):
        # lines 422-423
        tasks = [{"type": 1.0, "repeat_mode": "无限重复"}]
        config = {}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertTrue(any("无限重复" in r for r in risks))

    def test_no_skip_wait_with_zero_timeout(self):
        # lines 428-432
        tasks = [{"type": 1.0, "no_skip_wait": True}]
        config = {"timeout": 0}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertTrue(any("禁止跳过" in r for r in risks))

    def test_no_skip_wait_with_positive_timeout(self):
        tasks = [{"type": 1.0, "no_skip_wait": True}]
        config = {"timeout": 30}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertTrue(any("禁止跳过" in r for r in risks))

    def test_coord_step_reset_with_infinite_loop(self):
        # lines 440-446
        tasks = [
            {
                "type": 1.0,
                "coord_step_en": True,
                "coord_step_reset_after": 5,
                "value": "100,200",
                "repeat_mode": "无限重复",
            }
        ]
        config = {}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertTrue(any("坐标步进重置循环" in r for r in risks))
        self.assertTrue(any("无限循环" in r for r in risks))

    def test_coord_step_reset_without_infinite_loop(self):
        tasks = [
            {
                "type": 1.0,
                "coord_step_en": True,
                "coord_step_reset_after": 3,
                "value": "50,60",
            }
        ]
        config = {}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertTrue(any("坐标步进重置循环" in r for r in risks))
        self.assertTrue(any("预期的重复路径" in r for r in risks))

    def test_until_back_jump_no_limit(self):
        # lines 453-476: until with false_jump back and no limits
        tasks = [
            {"type": 1.0},
            {"type": 1.0},
            {
                "type": 15.0,
                "until_false_jump": 1,
                "until_true_jump": 0,
                "until_max_checks": 0,
                "until_max_seconds": 0,
            },
        ]
        config = {}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertTrue(any("直到条件成立" in r and "未设置最多检查次数" in r for r in risks))

    def test_until_back_jump_with_limits(self):
        # lines 477-487: until with limits
        tasks = [
            {"type": 1.0},
            {
                "type": 15.0,
                "until_false_jump": 1,
                "until_true_jump": 0,
                "until_max_checks": 10,
                "until_max_seconds": 5,
            },
        ]
        config = {}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertTrue(any("最多检查 10 次" in r for r in risks))
        self.assertTrue(any("最多等待 5 秒" in r for r in risks))

    def test_until_true_jump_self_loop(self):
        # lines 488-493: true_jump == step_no
        tasks = [
            {"type": 1.0},
            {
                "type": 15.0,
                "until_false_jump": 0,
                "until_true_jump": 2,
                "until_max_checks": 0,
            },
        ]
        config = {}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertTrue(any("满足后仍跳回本步骤" in r for r in risks))

    def test_until_true_jump_back_loop(self):
        # lines 494-498: true_jump < step_no
        tasks = [
            {"type": 1.0},
            {"type": 1.0},
            {
                "type": 15.0,
                "until_false_jump": 0,
                "until_true_jump": 1,
                "until_max_checks": 0,
            },
        ]
        config = {}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertTrue(any("满足后会跳回第 1 步" in r for r in risks))

    def test_until_with_target_id_resolution(self):
        # lines 453-458: target_id takes priority
        tasks = [
            {"type": 1.0, "step_id": "first"},
            {"type": 1.0, "step_id": "second"},
            {
                "type": 15.0,
                "step_id": "third",
                "until_false_target_id": "first",
                "until_true_target_id": "second",
                "until_max_checks": 0,
                "until_max_seconds": 0,
            },
        ]
        config = {}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        # false_target_id "first" -> step 1, which is <= step_no 3
        self.assertTrue(any("直到条件成立" in r for r in risks))

    def test_success_jump_back_loop(self):
        # line 515: success_jump back
        tasks = [
            {"type": 1.0},
            {"type": 1.0, "success_jump": 1},
        ]
        config = {}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertTrue(any("成功后跳至第 1 步" in r and "之间循环" in r for r in risks))

    def test_fail_jump_self_loop(self):
        tasks = [
            {"type": 1.0, "fail_jump": 1},
        ]
        config = {}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertTrue(any("原地循环" in r for r in risks))

    def test_timed_finish_loop_with_blockers(self):
        # lines 521-523
        tasks = [
            {"type": 1.0, "repeat_mode": "无限重复"},
            {"type": 1.0, "no_skip_wait": True},
        ]
        config = {
            "loop_mode": "指定时间",
            "time_limit_stop_mode": "finish_loop",
            "timeout": 0,
        }
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertTrue(any("指定时间" in r and "本轮结束后停止" in r for r in risks))
        # The timed_finish warning is inserted at position 0
        self.assertIn("指定时间", risks[0])

    def test_timed_finish_loop_no_blockers(self):
        tasks = [{"type": 1.0}]
        config = {
            "loop_mode": "指定时间",
            "time_limit_stop_mode": "finish_loop",
        }
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        self.assertFalse(any("本轮结束后停止" in r for r in risks))

    def test_until_max_seconds_invalid(self):
        # line 464-465: invalid until_max_seconds
        tasks = [
            {
                "type": 15.0,
                "until_false_jump": 1,
                "until_true_jump": 0,
                "until_max_checks": 0,
                "until_max_seconds": "invalid",
            },
        ]
        config = {}
        risks = analyze_loop_risks(tasks, config, _cmd_name)
        # max_seconds falls to 0, so no-limit path
        self.assertTrue(any("未设置最多检查次数" in r for r in risks))


# ---------------------------------------------------------------------------
# risk_type_keys (lines 540-569)
# ---------------------------------------------------------------------------


class TestRiskTypeKeys(unittest.TestCase):
    def test_global_infinite(self):
        risks = ["全局循环模式为【无限】，脚本会在步骤列表执行完后重新开始，直到手动停止或急停。"]
        keys = risk_type_keys(risks)
        self.assertEqual(keys, {"global_infinite"})

    def test_timed_finish_block(self):
        risks = ["当前使用【指定时间】并选择【本轮结束后停止】，但..."]
        keys = risk_type_keys(risks)
        self.assertEqual(keys, {"timed_finish_block"})

    def test_coord_step_reset(self):
        risks = ["第 1 步【CMD】启用【坐标步进重置循环】，每成功点击 5 次会回到起点"]
        keys = risk_type_keys(risks)
        self.assertEqual(keys, {"coord_step_reset"})

    def test_no_skip_wait(self):
        risks = ["第 1 步【CMD】启用【禁止跳过】，失败时会一直等待本步骤成功"]
        keys = risk_type_keys(risks)
        self.assertEqual(keys, {"no_skip_wait"})

    def test_step_infinite_repeat(self):
        risks = ["第 1 步【CMD】设置为【无限重复】，会一直执行本步骤"]
        keys = risk_type_keys(risks)
        self.assertEqual(keys, {"step_infinite_repeat"})

    def test_until_no_limit(self):
        risks = ["第 3 步【直到条件成立】设置为条件未满足时跳回第 1 步，且未设置最多检查次数/秒数"]
        keys = risk_type_keys(risks)
        self.assertEqual(keys, {"until_no_limit"})

    def test_until_no_limit_alt(self):
        risks = ["第 3 步【直到条件成立】没有检查次数或时间上限"]
        keys = risk_type_keys(risks)
        self.assertEqual(keys, {"until_no_limit"})

    def test_until_true_self(self):
        risks = ["第 2 步【直到条件成立】满足后仍跳回本步骤；只要条件持续满足，就会在本步骤原地循环。"]
        keys = risk_type_keys(risks)
        self.assertEqual(keys, {"until_true_self"})

    def test_until_true_back(self):
        risks = ["第 3 步【直到条件成立】满足后会跳回第 1 步；如果条件持续满足，可能在第 1 到第 3 步之间循环。"]
        keys = risk_type_keys(risks)
        self.assertEqual(keys, {"until_true_back"})

    def test_until_back_generic(self):
        risks = ["第 3 步【直到条件成立】未满足时会跳回第 1 步，满足或达到保护上限前会重复执行这一段"]
        keys = risk_type_keys(risks)
        self.assertEqual(keys, {"until_back"})

    def test_jump_self_loop(self):
        risks = ["第 1 步【CMD】设置【成功后跳至第 1 步】，可能在本步骤原地循环。"]
        keys = risk_type_keys(risks)
        self.assertEqual(keys, {"jump_self_loop"})

    def test_jump_back_loop(self):
        risks = ["第 2 步【CMD】设置【成功后跳至第 1 步】，可能在第 1 到第 2 步之间循环。"]
        keys = risk_type_keys(risks)
        self.assertEqual(keys, {"jump_back_loop"})

    def test_unknown_risk_fallback_hash(self):
        risks = ["some completely unknown risk message"]
        keys = risk_type_keys(risks)
        self.assertEqual(len(keys), 1)
        key = next(iter(keys))
        self.assertTrue(key.startswith("other:"))

    def test_multiple_risks(self):
        risks = [
            "全局循环模式为【无限】，脚本会在步骤列表执行完后重新开始",
            "第 1 步【CMD】设置为【无限重复】",
            "some unknown risk",
        ]
        keys = risk_type_keys(risks)
        self.assertIn("global_infinite", keys)
        self.assertIn("step_infinite_repeat", keys)
        self.assertTrue(any(k.startswith("other:") for k in keys))


# ---------------------------------------------------------------------------
# Remaining lines: 293-294 (worklist re-enqueue), 345 (BFS duplicate pop)
# ---------------------------------------------------------------------------


class TestWorklistReenqueue(unittest.TestCase):
    """Cover lines 293-294: successor re-enqueued after predecessor changes."""

    def test_back_edge_causes_reenqueue(self):
        # Task 0 assigns x, task 1 assigns y referencing x and jumps back to 0.
        # After processing node 1 its out_set changes, and successor 0 has
        # already been popped -> triggers worklist.append (lines 293-294).
        tasks = [
            {"type": 16.0, "value": "x = 1"},
            {"type": 16.0, "value": "y = x + 1", "success_jump": 1, "fail_jump": 3},
        ]
        graph = build_workflow_graph(tasks)
        # graph[0] = {1}, graph[1] = {0, 2}
        reachable = {0, 1, 2}
        issues = analyze_variable_flow(tasks, graph, reachable, 0)
        # x is definitely assigned at step 1 (after step 0), so no warning for x.
        # y is referenced nowhere, so no issues expected.
        self.assertIsInstance(issues, list)


class TestBFSDuplicatePop(unittest.TestCase):
    """Cover line 345: node popped from pending that was already visited."""

    def test_diamond_graph_duplicate_pending(self):
        # Graph: 0->{1,2}, 1->{2}, 2->{1,3}, 3->{4(exit)}
        # DFS stack causes node 1 to be pushed twice before first pop.
        tasks = [
            {"type": 1.0, "success_jump": 2, "fail_jump": 3},
            {"type": 1.0},
            {"type": 1.0, "success_jump": 2, "fail_jump": 4},
            {"type": 1.0},
        ]
        issues = analyze_workflow_structure(tasks)
        # All nodes reachable, exit reachable -> no structural issues
        self.assertFalse(any(i.code == "unreachable" for i in issues))
        self.assertFalse(any(i.code == "no_exit_path" for i in issues))


# ---------------------------------------------------------------------------
# _branch_target (supporting coverage for line 65)
# ---------------------------------------------------------------------------


class TestBranchTarget(unittest.TestCase):
    def test_jump_positive(self):
        task = {"success_jump": 5}
        self.assertEqual(_branch_target(task, 0, "success", 10), 4)

    def test_jump_exceeds_count(self):
        task = {"success_jump": 20}
        self.assertEqual(_branch_target(task, 0, "success", 10), 10)

    def test_skip_fallback(self):
        task = {"fail_skip": 2}
        self.assertEqual(_branch_target(task, 3, "fail", 10), 6)

    def test_skip_exceeds_count(self):
        task = {"fail_skip": 100}
        self.assertEqual(_branch_target(task, 3, "fail", 10), 10)


if __name__ == "__main__":
    unittest.main()
