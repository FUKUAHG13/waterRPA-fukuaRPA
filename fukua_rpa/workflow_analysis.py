"""Static workflow graph and long-running-loop risk analysis."""

from __future__ import annotations

import ast
import contextlib
import hashlib
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .constants import (
    TASK_TYPE_EXPRESSION,
    TASK_TYPE_SET_VARIABLE,
    TASK_TYPE_UNTIL,
)
from .expressions import (
    BUILTIN_VARIABLE_NAMES,
    ExpressionError,
    compile_expression,
    parse_assignment,
)
from .task_model import (
    config_bool,
    parse_coordinate_text,
    task_is_disabled,
    until_condition_summary,
)


@dataclass(frozen=True)
class WorkflowIssue:
    """A single finding produced by the workflow analyzers.

    Immutable value object surfaced to the UI's pre-run check panel. Severity
    drives presentation only; ``code`` is the stable machine identifier that
    callers (e.g. risk deduplication) should key on rather than ``message``.

    Attributes:
        severity: ``"error"`` or ``"warning"``.
        code: Stable issue-type identifier (e.g. ``"unreachable"``).
        message: Human-readable Chinese description for display.
        step: Optional 1-based step number the issue refers to.
    """

    severity: str
    code: str
    message: str
    step: int | None = None


def _integer(value: Any, default: int = 0, minimum: int = 0) -> int:
    try:
        return max(minimum, int(float(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _branch_target(task: Mapping[str, Any], index: int, prefix: str, count: int) -> int:
    jump = _integer(task.get(f"{prefix}_jump", 0))
    if jump > 0:
        return min(jump - 1, count)
    skip = _integer(task.get(f"{prefix}_skip", 0))
    return min(index + skip + 1, count)


def _resolve_target_id(
    task: Mapping[str, Any],
    id_to_index: Mapping[str, int],
    target_id_key: str,
) -> int | None:
    """Resolve a *_target_id field to a 0-based index via id_to_index.

    Returns None when the target_id is absent, empty, or not found in the
    mapping — callers should fall back to the numeric jump field.
    """
    target_id = task.get(target_id_key)
    if not target_id:
        return None
    return id_to_index.get(str(target_id))


def _resolve_target_step(
    task: Mapping[str, Any],
    id_to_index: Mapping[str, int],
    target_id_key: str,
) -> int | None:
    """Resolve a *_target_id field to a 1-based step number for display.

    Returns None when the target_id is absent, empty, or not found.
    """
    idx = _resolve_target_id(task, id_to_index, target_id_key)
    if idx is None:
        return None
    return idx + 1


def build_workflow_graph(tasks: Sequence[Mapping[str, Any]]) -> dict[int, set[int]]:
    """Build a conservative control-flow graph of possible step transitions.

    Each step index maps to the set of step indices it may transfer control to.
    The synthetic node ``len(tasks)`` represents normal script exit and has no
    outgoing edges. Disabled steps always fall through to the next step. Branch
    targets are resolved from authoritative target_id fields first, falling back
    to numeric jump/skip fields.

    Args:
        tasks: Ordered sequence of step configuration dicts.

    Returns:
        Adjacency dict mapping each step index (and the exit node) to its set
        of possible successor indices.
    """

    count = len(tasks)
    # Defensive: prefer authoritative target_id fields over numeric jumps.
    id_to_index: dict[str, int] = {}
    for i, t in enumerate(tasks):
        sid = t.get("step_id")
        if sid:
            id_to_index[str(sid)] = i

    graph: dict[int, set[int]] = {}
    for index, task in enumerate(tasks):
        if task_is_disabled(task):
            graph[index] = {min(index + 1, count)}
            continue
        if _task_type(task) == TASK_TYPE_UNTIL:
            # Prefer target_id resolution; fall back to numeric jump fields.
            true_idx = _resolve_target_id(task, id_to_index, "until_true_target_id")
            if true_idx is not None:
                true_target = true_idx
            else:
                true_jump = _integer(task.get("until_true_jump", 0))
                true_target = true_jump - 1 if true_jump > 0 else index + 1
            false_idx = _resolve_target_id(task, id_to_index, "until_false_target_id")
            if false_idx is not None:
                false_target = false_idx
            else:
                # DEC-20260930-002-03（维护者 2026-10-07 裁定，A3）：现行默认值
                # 统一为 0，与 until_true_jump 同口径。旧缺省 1 会让新建的
                # 「直到条件成立」步骤默认跳回第 1 步，凭空生成一条指向 index 0
                # 的边——用户从未配置，画出的图里却有个循环。0 表示不跳转。
                false_jump = _integer(task.get("until_false_jump", 0))
                false_target = false_jump - 1 if false_jump > 0 else index + 1
            targets = {min(max(true_target, 0), count), min(max(false_target, 0), count)}
            if str(task.get("until_on_limit", "继续下一步")) != "停止脚本":
                targets.add(min(index + 1, count))
        else:
            # Prefer target_id resolution; fall back to numeric jump/skip fields.
            success_idx = _resolve_target_id(task, id_to_index, "success_target_id")
            fail_idx = _resolve_target_id(task, id_to_index, "fail_target_id")
            targets = {
                min(success_idx, count) if success_idx is not None
                else _branch_target(task, index, "success", count),
                min(fail_idx, count) if fail_idx is not None
                else _branch_target(task, index, "fail", count),
            }
        call_step = _integer(task.get("call_step", 0))
        if 1 <= call_step <= count and call_step - 1 != index:
            targets.add(call_step - 1)
        graph[index] = targets
    graph[count] = set()
    return graph


def _task_type(task: Mapping[str, Any]) -> float:
    try:
        return float(task.get("type", 0))
    except (TypeError, ValueError):
        return 0.0


def _expression_names(tree: ast.Expression) -> set[str]:
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def analyze_variable_flow(
    tasks: Sequence[Mapping[str, Any]],
    graph: Mapping[int, set[int]],
    reachable: set[int],
    start: int,
) -> list[WorkflowIssue]:
    """Detect variables that may be referenced before assignment on some paths.

    Performs a forward must-analysis (definite assignment) over the control-flow
    graph: a variable is considered definitely assigned at a step only if every
    reachable predecessor path assigns it first. Steps that reference a variable
    not definitely assigned produce a warning issue.

    Workflows exceeding 2000 steps skip analysis to avoid UI stalls and return a
    single informational warning instead.

    Args:
        tasks: Ordered sequence of step configuration dicts.
        graph: Control-flow adjacency as returned by build_workflow_graph.
        reachable: Set of step indices reachable from the start step.
        start: Zero-based index of the entry step.

    Returns:
        List of WorkflowIssue warnings for potentially unset variables.
    """
    count = len(tasks)
    assignments: dict[int, str] = {}
    references: dict[int, set[str]] = {}
    for index, task in enumerate(tasks):
        if task_is_disabled(task):
            continue
        command = _task_type(task)
        try:
            if config_bool(task.get("debug_breakpoint", False)):
                debug_condition = str(task.get("debug_condition", "") or "").strip()
                if debug_condition:
                    tree = compile_expression(debug_condition)
                    references.setdefault(index, set()).update(
                        _expression_names(tree)
                    )
            if command == TASK_TYPE_SET_VARIABLE:
                assignment = parse_assignment(task.get("value", ""))
                assignments[index] = assignment.name
                references.setdefault(index, set()).update(
                    _expression_names(assignment.tree)
                )
            elif command == TASK_TYPE_EXPRESSION:
                tree = compile_expression(task.get("value", ""))
                references.setdefault(index, set()).update(
                    _expression_names(tree)
                )
        except ExpressionError:
            continue

    custom_names = set(assignments.values())
    if not references:
        return []
    predecessors = {index: set() for index in range(count)}
    for source, targets in graph.items():
        if source >= count or source not in reachable:
            continue
        for target in targets:
            if target < count and target in reachable:
                predecessors[target].add(source)

    # Definite-assignment is a forward must analysis. Start at the lattice top
    # and intersect predecessors; the entry node is always empty for a fresh run.
    in_sets = {index: set(custom_names) for index in range(count)}
    out_sets = {index: set(custom_names) for index in range(count)}
    if start < count:
        in_sets[start] = set()
    if count > 2000:
        # O(n²) guard: the worklist fixed-point iteration below has worst-case
        # O(n * |vars| * predecessors) per node visit with up to O(n * |vars|)
        # visits, yielding O(n² * |vars|²) in degenerate graphs.  With
        # MAX_TASKS_PER_PROFILE = 5000 the unguarded path would cause perceptible
        # UI stalls; the 2000 threshold keeps worst-case under ~50 ms on typical
        # hardware while still covering the majority of real workflows.
        return [
            WorkflowIssue(
                "warning",
                "variable_flow_skipped",
                f"步骤数 {count} 超过 2000，已跳过变量流分析以避免性能开销；"
                "“变量可能未设置”检查本次未执行。",
            )
        ]
    # Worklist fixed-point iteration. This is a forward must-analysis: each
    # step's definite-assignment set starts at the lattice top (all custom
    # names) and only shrinks as predecessors are intersected, so every step
    # changes at most len(custom_names) times and the loop always terminates.
    # Only steps whose inputs actually changed re-enqueue their successors,
    # which avoids rescanning the whole workflow on every round.
    successors = graph
    worklist = deque(index for index in range(count) if index in reachable)
    in_worklist = set(worklist)
    max_iterations = count * (len(custom_names) + 1) * 4
    iterations = 0
    while worklist and iterations < max_iterations:
        iterations += 1
        index = worklist.popleft()
        in_worklist.discard(index)
        if index == start:
            new_in = set()
        else:
            incoming = [out_sets[parent] for parent in predecessors[index]]
            new_in = set.intersection(*incoming) if incoming else set()
        new_out = set(new_in)
        if index in assignments:
            new_out.add(assignments[index])
        if new_in != in_sets[index] or new_out != out_sets[index]:
            in_sets[index] = new_in
            out_sets[index] = new_out
            for successor in successors.get(index, ()):
                if (
                    successor < count
                    and successor in reachable
                    and successor not in in_worklist
                ):
                    worklist.append(successor)
                    in_worklist.add(successor)

    issues = []
    for index, names in sorted(references.items()):
        if index not in reachable:
            continue
        missing = sorted(
            name
            for name in names
            if name not in BUILTIN_VARIABLE_NAMES and name not in in_sets[index]
        )
        if missing:
            issues.append(
                WorkflowIssue(
                    "warning",
                    "variable_maybe_unset",
                    f"第 {index + 1} 步可能在变量 {'、'.join(missing)} 尚未设置时执行；"
                    "请确认所有成功/失败路径都会先经过对应的“设置变量”步骤。",
                    step=index + 1,
                )
            )
    return issues


def analyze_workflow_structure(
    tasks: Sequence[Mapping[str, Any]], start_step: int = 1
) -> list[WorkflowIssue]:
    """Analyze workflow reachability, exit paths, and variable flow.

    Builds the control-flow graph, computes the set of steps reachable from
    start_step, and reports: unreachable steps, absence of any natural exit
    path, and variable-flow issues (via analyze_variable_flow).

    Args:
        tasks: Ordered sequence of step configuration dicts.
        start_step: One-based step number where execution begins.

    Returns:
        List of WorkflowIssue entries (errors and warnings). An empty workflow
        produces a single error issue.
    """
    if not tasks:
        return [WorkflowIssue("error", "empty", "脚本中没有可执行步骤。")]
    graph = build_workflow_graph(tasks)
    count = len(tasks)
    start = min(max(int(start_step) - 1, 0), count - 1)
    visited: set[int] = set()
    pending = [start]
    while pending:
        node = pending.pop()
        if node in visited:
            continue
        visited.add(node)
        pending.extend(target for target in graph.get(node, ()) if target not in visited)

    issues: list[WorkflowIssue] = []
    unreachable = [index + 1 for index in range(start, count) if index not in visited]
    if unreachable:
        preview = "、".join(str(value) for value in unreachable[:12])
        suffix = "……" if len(unreachable) > 12 else ""
        issues.append(
            WorkflowIssue(
                "warning",
                "unreachable",
                f"从第 {start + 1} 步开始运行时，步骤 {preview}{suffix} 没有可到达路径。",
            )
        )
    if count not in visited:
        issues.append(
            WorkflowIssue(
                "warning",
                "no_exit_path",
                "从当前起始步骤出发，没有任何分支能够自然离开脚本；只能依赖停止、超时或执行上限结束。",
            )
        )
    issues.extend(analyze_variable_flow(tasks, graph, visited, start))
    return issues


def analyze_loop_risks(
    tasks: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    command_name: Callable[[Any], str],
) -> list[str]:
    """Identify configurations that may cause unexpectedly long or infinite loops.

    Inspects global loop mode, per-step repeat settings, no-skip-wait flags,
    coordinate-step reset cycles, until-condition back-jumps, and success/fail
    jump targets that point backwards. Also detects conflicts between timed
    finish-loop mode and steps that block indefinite.

    Args:
        tasks: Ordered sequence of step configuration dicts.
        config: Workflow-level configuration (loop_mode, timeout, etc.).
        command_name: Callable mapping a step type value to its display name.

    Returns:
        List of human-readable Chinese risk description strings suitable for
        display in a confirmation dialog before execution.
    """
    risks: list[str] = []
    global_loop_end = _integer(config.get("loop_end_round", 0))
    timed_finish_loop = (
        str(config.get("loop_mode", "")).startswith("指定时间")
        and str(config.get("time_limit_stop_mode", "finish_loop"))
        == "finish_loop"
    )
    timeout_seconds = 0.0
    with contextlib.suppress(TypeError, ValueError, OverflowError):
        timeout_seconds = max(0.0, float(config.get("timeout", 0)))
    time_limit_blockers: list[str] = []
    if config.get("loop_mode") == "无限" and global_loop_end <= 0:
        risks.append("全局循环模式为【无限】，脚本会在步骤列表执行完后重新开始，直到手动停止或急停。")

    # Defensive: prefer authoritative target_id fields over numeric jumps.
    id_to_index: dict[str, int] = {}
    for i, t in enumerate(tasks):
        sid = t.get("step_id")
        if sid:
            id_to_index[str(sid)] = i

    for index, task in enumerate(tasks):
        if task_is_disabled(task):
            continue
        step_no = index + 1
        command = command_name(task.get("type"))
        repeat_mode = str(task.get("repeat_mode", "执行一次"))
        if repeat_mode == "无限重复":
            time_limit_blockers.append(f"第 {step_no} 步为无限重复")
            risks.append(
                f"第 {step_no} 步【{command}】设置为【无限重复】，会一直执行本步骤，"
                "直到手动停止或触发失败/超时分支。"
            )
        if config_bool(task.get("no_skip_wait", False)):
            if timeout_seconds <= 0:
                time_limit_blockers.append(
                    f"第 {step_no} 步启用禁止跳过且单步超时为 0"
                )
            risks.append(
                f"第 {step_no} 步【{command}】启用【禁止跳过】，失败时会一直等待本步骤成功，"
                "直到满足目标、达到超时或触发急停。"
            )

        reset_after = _integer(task.get("coord_step_reset_after", 0))
        coordinate_step = config_bool(task.get("coord_step_en", False))
        if coordinate_step and reset_after > 0 and parse_coordinate_text(task.get("value", "")):
            if repeat_mode == "无限重复" or config.get("loop_mode") == "无限":
                risks.append(
                    f"第 {step_no} 步【{command}】启用【坐标步进重置循环】，每成功点击 "
                    f"{reset_after} 次会回到起点；当前又存在无限循环设置，可能会反复点击同一路径。"
                )
            else:
                risks.append(
                    f"第 {step_no} 步【{command}】启用【坐标步进重置循环】，每成功点击 "
                    f"{reset_after} 次会回到起点，请确认这是预期的重复路径。"
                )

        if _task_type(task) == TASK_TYPE_UNTIL:
            # Prefer target_id; fall back to numeric jump fields.
            false_step = _resolve_target_step(task, id_to_index, "until_false_target_id")
            if false_step is None:
                # DEC-20260930-002-03（A3）：缺省统一为 0，与上方 until_true_jump
                # 及 build_workflow_graph 保持同一口径，避免两处对"未配置"给出
                # 不同解释。
                false_step = _integer(task.get("until_false_jump", 0))
            true_step = _resolve_target_step(task, id_to_index, "until_true_target_id")
            if true_step is None:
                true_step = _integer(task.get("until_true_jump", 0))
            false_jump = false_step
            true_jump = true_step
            max_checks = _integer(task.get("until_max_checks", 0))
            try:
                max_seconds = max(0.0, float(task.get("until_max_seconds", 0)))
            except (TypeError, ValueError):
                max_seconds = 0.0
            conditions = until_condition_summary(task)
            if 0 < false_jump <= step_no:
                if max_checks <= 0 and max_seconds <= 0:
                    time_limit_blockers.append(
                        f"第 {step_no} 步条件循环没有检查次数或时间上限"
                    )
                    risks.append(
                        f"第 {step_no} 步【直到条件成立】设置为条件未满足时跳回第 {false_jump} 步，"
                        f"且未设置最多检查次数/秒数；会一直执行第 {false_jump} 到第 {step_no} 步，"
                        f"直到满足：{conditions}"
                    )
                else:
                    limits = []
                    if max_checks > 0:
                        limits.append(f"最多检查 {max_checks} 次")
                    if max_seconds > 0:
                        limits.append(f"最多等待 {max_seconds:g} 秒")
                    risks.append(
                        f"第 {step_no} 步【直到条件成立】未满足时会跳回第 {false_jump} 步，"
                        f"满足或达到保护上限前会重复执行这一段；条件：{conditions}；"
                        f"保护：{'，'.join(limits)}。"
                    )
            if 0 < true_jump <= step_no:
                if true_jump == step_no:
                    risks.append(
                        f"第 {step_no} 步【直到条件成立】满足后仍跳回本步骤；"
                        "只要条件持续满足，就会在本步骤原地循环。"
                    )
                else:
                    risks.append(
                        f"第 {step_no} 步【直到条件成立】满足后会跳回第 {true_jump} 步；"
                        f"如果条件持续满足，可能在第 {true_jump} 到第 {step_no} 步之间循环。"
                    )

        for key, label, tid_key in (
            ("success_jump", "成功后跳至", "success_target_id"),
            ("fail_jump", "失败后跳至", "fail_target_id"),
        ):
            # Prefer target_id; fall back to numeric jump field.
            jump_to = _resolve_target_step(task, id_to_index, tid_key)
            if jump_to is None:
                jump_to = _integer(task.get(key, 0))
            if 0 < jump_to <= step_no:
                if jump_to == step_no:
                    risks.append(
                        f"第 {step_no} 步【{command}】设置【{label}第 {jump_to} 步】，"
                        "可能在本步骤原地循环。"
                    )
                else:
                    risks.append(
                        f"第 {step_no} 步【{command}】设置【{label}第 {jump_to} 步】，"
                        f"可能在第 {jump_to} 到第 {step_no} 步之间循环。"
                    )

    if timed_finish_loop and time_limit_blockers:
        preview = "；".join(dict.fromkeys(time_limit_blockers[:5]))
        suffix = "；另有其他长轮次风险" if len(time_limit_blockers) > 5 else ""
        risks.insert(
            0,
            "当前使用【指定时间】并选择【本轮结束后停止】，但"
            f"{preview}{suffix}。到达设定时间后仍需等待本轮结束，"
            "因此可能明显超时甚至无法自动结束；如需接近设定时间停止，请改为“尽快停止当前步骤”。",
        )
    return risks


def risk_type_keys(risks: Sequence[str]) -> set[str]:
    """Map human-readable risk messages to stable risk-type identifiers.

    Used to deduplicate risk prompts per profile: a profile only re-prompts
    when a *new* risk type appears. The classifier keys on the dominant
    category embedded in each message, which stays stable across parameter
    edits (design request: same-type risks prompt only once per profile).
    """
    keys: set[str] = set()
    for text in risks:
        t = str(text)
        if "全局循环模式为【无限】" in t:
            keys.add("global_infinite")
        elif "指定时间" in t and "本轮结束后停止" in t:
            keys.add("timed_finish_block")
        elif "坐标步进重置循环" in t:
            keys.add("coord_step_reset")
        elif "禁止跳过" in t:
            keys.add("no_skip_wait")
        elif "无限重复" in t:
            keys.add("step_infinite_repeat")
        elif "直到条件成立" in t:
            if "未设置最多检查次数" in t or "没有检查次数或时间上限" in t:
                keys.add("until_no_limit")
            elif "满足后仍跳回本步骤" in t:
                keys.add("until_true_self")
            elif "满足后会跳回" in t:
                keys.add("until_true_back")
            else:
                keys.add("until_back")
        elif "原地循环" in t:
            keys.add("jump_self_loop")
        elif "之间循环" in t:
            keys.add("jump_back_loop")
        else:
            # Fallback: hash the text so unknown risks still dedupe correctly.
            keys.add("other:" + hashlib.md5(t.encode("utf-8")).hexdigest()[:8])
    return keys
