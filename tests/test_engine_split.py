"""Guard tests for the P4d split of ``fukua_rpa.engine``.

P4d extracted three mixin modules out of the monolithic ``RPAEngine`` class:

* ``loop_scheduler.py``  -> ``LoopSchedulerMixin``      (loop / scheduling)
* ``jump_resolver.py``   -> ``JumpResolverMixin``       (jump / branch resolution)
* ``run_lifecycle_glue.py`` -> ``RunLifecycleMixin``    (run lifecycle glue)

``engine.py`` keeps ``__init__`` plus the 8 single-step execution core methods.
The split is mechanical: every method body is copied verbatim, so ``RPAEngine``
must expose exactly the same public/private surface it had before, resolved via
MRO across the new mixins. This module locks that contract.

It deliberately avoids importing ``Qt`` widgets (the engine must stay Qt-free).
"""

import ast
import os

from fukua_rpa.constants import (
    TASK_TYPE_EXPRESSION,
    TASK_TYPE_STOP_RUN,
    TASK_TYPE_UNTIL,
    TASK_TYPE_WAIT,
    TASK_TYPE_WAIT_WINDOW,
)
from fukua_rpa.engine import RPAEngine
from fukua_rpa.jump_resolver import JumpResolverMixin
from fukua_rpa.loop_scheduler import LoopSchedulerMixin
from fukua_rpa.run_lifecycle_glue import RunLifecycleMixin
from fukua_rpa.step_runtime import StepExecutionPolicy

# The 62 method names (incl. __init__) that lived on RPAEngine *before* the P4d
# split. Derived from ``git show HEAD:fukua_rpa/engine.py`` via ast. If any of
# these is lost, renamed, or unexpectedly gained, the invariant tests below
# fail -- catching an accidental regression in the mechanical split.
ORIGINAL_METHODS = {
    "__init__",
    "initialize_backends",
    "set_high_priority",
    "check_engine_status",
    "_log_native_core_status",
    "reserve_run",
    "active_run_matches",
    "finish_run",
    "stop",
    "is_running",
    "stop_requested",
    "run_state",
    "close_screenshot_backend",
    "positive_int_value",
    "non_negative_int_value",
    "is_wait_command",
    "task_active_in_loop",
    "task_exhausted_for_run",
    "task_runnable_in_loop",
    "loop_number_allowed",
    "next_runnable_loop",
    "next_interval_task",
    "_get_next_runnable_tables",
    "_next_runnable_index_from",
    "should_wait_step_interval",
    "get_cmd_name",
    "parse_coordinate",
    "parse_float_value",
    "task_window_backend",
    "close_task_window_backend",
    "credential_store",
    "emit_debug_event",
    "_clear_run_callbacks",
    "_on_auxiliary_click_error",
    "_reset_run_state",
    "_initialize_run_runtime",
    "_preload_run_templates",
    "_abort_preload_run",
    "_finalize_run_runtime",
    "_debug_allows_step",
    "_build_step_execution_context",
    "_stop_for_step_timeout",
    "_step_attempt_limit_directive",
    "_step_recognition_wait_details",
    "_apply_step_attempt_status",
    "_execute_single_step_attempt",
    "_execute_step_attempts",
    "_resolve_until_true_transition",
    "_resolve_until_false_transition",
    "_resolve_branch_transition",
    "_resolve_step_transition",
    "_prepare_run",
    "_loop_limit_reached",
    "_prepare_loop_execution",
    "_task_skipped_for_loop",
    "_record_step_transition",
    "_call_target_index",
    "_image_target_cache_key_override",
    "_execute_called_step",
    "_dispatch_step",
    "_execute_loop_steps",
    "_plan_after_no_progress",
    "_run_loop_iteration",
    "run_tasks",
    "apply_scan_region",
}

# The 10 methods that must remain defined directly on RPAEngine in engine/core.py
# (the 8 single-step execution core methods + __init__ + apply_scan_region).
CORE_IN_ENGINE = {
    "__init__",
    "_debug_allows_step",
    "_build_step_execution_context",
    "_stop_for_step_timeout",
    "_step_attempt_limit_directive",
    "_step_recognition_wait_details",
    "_apply_step_attempt_status",
    "_execute_single_step_attempt",
    "_execute_step_attempts",
    "apply_scan_region",
}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _class_method_names(rel_path, class_name):
    """Return the set of class-level method names defined in *class_name*."""
    with open(os.path.join(ROOT, rel_path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                b.name
                for b in node.body
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return set()


ENGINE_METHODS = _class_method_names("fukua_rpa/engine/core.py", "RPAEngine")
LOOP_METHODS = _class_method_names("fukua_rpa/loop_scheduler.py", "LoopSchedulerMixin")
JUMP_METHODS = _class_method_names("fukua_rpa/jump_resolver.py", "JumpResolverMixin")
LIFECYCLE_METHODS = _class_method_names(
    "fukua_rpa/run_lifecycle_glue.py", "RunLifecycleMixin"
)


def test_split_mixin_modules_importable():
    assert LoopSchedulerMixin is not None
    assert JumpResolverMixin is not None
    assert RunLifecycleMixin is not None


def test_new_mixins_present_in_rpaengine_mro():
    mro_names = [c.__name__ for c in RPAEngine.__mro__]
    assert "LoopSchedulerMixin" in mro_names
    assert "JumpResolverMixin" in mro_names
    assert "RunLifecycleMixin" in mro_names
    # The three new mixins must sit after the older behaviour mixins and before
    # object, i.e. they are real participants in the MRO resolution order.
    assert mro_names.index("LoopSchedulerMixin") > mro_names.index("ActionExecutionMixin")
    assert mro_names.index("RunLifecycleMixin") < mro_names.index("object")


def test_no_original_method_lost_from_rpaengine():
    resolved = set(dir(RPAEngine))
    missing = ORIGINAL_METHODS - resolved
    assert not missing, f"RPAEngine lost methods after split: {sorted(missing)}"


def test_class_level_method_union_matches_original_exactly():
    """拆分守护：四个拆分文件不得丢失任何原方法。

    FR-008：本测试原先要求与拆分前快照**精确相等**。拆分之后为修复 ENG-FR-003
    合法新增的方法（如 ``_abort_failed_run_init``，由 ``4c6f995`` 引入）不是
    "漂移"，因此守护改为超集检查：快照中的每个方法仍须由四个文件之一定义。
    方法**丢失**仍是硬失败；新增方法在断言消息中一并列出以便观察。
    （测试名保留原样以维持历史报告引用的 nodeid 可追溯。）
    """
    union = ENGINE_METHODS | LOOP_METHODS | JUMP_METHODS | LIFECYCLE_METHODS
    missing = ORIGINAL_METHODS - union
    assert not missing, (
        "Method-set drift across the four split files: original methods lost. "
        f"only-in-original={sorted(missing)} "
        f"added-since-snapshot={sorted(union - ORIGINAL_METHODS)}"
    )


def test_engine_py_defines_only_the_core_methods():
    assert ENGINE_METHODS == CORE_IN_ENGINE, (
        f"engine.py should define exactly the core methods, got {sorted(ENGINE_METHODS)}"
    )


def test_mixin_method_sets_are_disjoint():
    assert not (LOOP_METHODS & JUMP_METHODS)
    assert not (LOOP_METHODS & LIFECYCLE_METHODS)
    assert not (JUMP_METHODS & LIFECYCLE_METHODS)


def test_no_duplicate_method_definitions_across_split_files():
    all_sets = [ENGINE_METHODS, LOOP_METHODS, JUMP_METHODS, LIFECYCLE_METHODS]
    for i, a in enumerate(all_sets):
        for b in all_sets[i + 1:]:
            overlap = a & b
            assert not overlap, f"Duplicate method definitions: {overlap}"


def test_rpaengine_instantiable_with_deferred_backends():
    engine = RPAEngine(defer_backends=True)
    assert hasattr(engine, "run_tasks")
    assert hasattr(engine, "run_tasks") and callable(engine.run_tasks)
    # A representative method from each new mixin must resolve on the instance.
    for method in (
        "run_tasks",          # loop_scheduler
        "_resolve_step_transition",  # jump_resolver
        "initialize_backends",       # run_lifecycle_glue
    ):
        assert hasattr(engine, method), f"instance missing {method}"


def test_until_expression_forced_single_step_on_crafted_infinite_repeat():
    # P1-1: crafted / imported task data that sets repeat_mode="无限重复" on a
    # flow / wait / stop command must not be able to make the inner attempt loop
    # hang (target_successes=None never reached). FR-20260809-D1-④ 把强制单次收进
    # StepExecutionPolicy.from_task（_FLOW_ONCE_COMMANDS，引擎唯一构造路径——
    # jump_resolver / run_lifecycle_glue 均经 from_task），crafted task 在源头即被
    # 归一化为"执行一次"。此处经真实防线构造断言，而非直接构造 policy 绕过它。
    engine = RPAEngine(defer_backends=True)
    for command in (
        TASK_TYPE_UNTIL,
        TASK_TYPE_EXPRESSION,
        TASK_TYPE_WAIT,
        TASK_TYPE_STOP_RUN,
        TASK_TYPE_WAIT_WINDOW,
    ):
        crafted_task = {
            "type": command,
            "value": "1==1",
            "repeat_mode": "无限重复",
        }
        policy = StepExecutionPolicy.from_task(
            crafted_task,
            default_confidence=engine.confidence,
            default_grayscale=engine.enable_grayscale,
            min_scale=engine.min_scale,
            max_scale=engine.max_scale,
            scale_step=engine.scale_step,
        )
        assert policy.repeat_mode == "执行一次"
        assert policy.target_successes == 1
        ctx = engine._build_step_execution_context(
            task=crafted_task,
            task_count=1,
            index=0,
            loop_count=1,
            policy=policy,
        )
        assert ctx.policy.repeat_mode == "执行一次"
        assert ctx.policy.target_successes == 1


def test_until_expression_default_repeat_unchanged():
    # Normal UI-constructed data (default 执行一次) must be idempotent: the
    # from_task normalization must not alter already-correct policy.
    engine = RPAEngine(defer_backends=True)
    for command in (
        TASK_TYPE_UNTIL,
        TASK_TYPE_EXPRESSION,
        TASK_TYPE_WAIT,
        TASK_TYPE_STOP_RUN,
        TASK_TYPE_WAIT_WINDOW,
    ):
        normal_task = {
            "type": command,
            "value": "1==1",
            "repeat_mode": "执行一次",
        }
        policy = StepExecutionPolicy.from_task(
            normal_task,
            default_confidence=engine.confidence,
            default_grayscale=engine.enable_grayscale,
            min_scale=engine.min_scale,
            max_scale=engine.max_scale,
            scale_step=engine.scale_step,
        )
        ctx = engine._build_step_execution_context(
            task=normal_task,
            task_count=1,
            index=0,
            loop_count=1,
            policy=policy,
        )
        assert ctx.policy.repeat_mode == "执行一次"
        assert ctx.policy.target_successes == 1
