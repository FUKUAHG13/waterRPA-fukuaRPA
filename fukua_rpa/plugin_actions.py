"""Generic plugin-action step: validation, dependency collection, dispatch.

This module is the bridge between the engine (which runs a step of type
``TASK_TYPE_PLUGIN_ACTION``) and the plugin host. It is intentionally Qt-free so
it can be imported from the worker thread that executes engine steps.

Responsibilities (design §5.4 / §5.7):
* Validate a plugin-action step's fields (plugin_id / plugin_action_id present
  and resolvable against the installed registry).
* Collect the set of plugin ids a profile depends on (so the run can be blocked
  before launch if any dependency is missing — design §5.5).
* Dispatch a call to the plugin host client and translate the response into the
  engine's "success" / "error" step result.

The registry is supplied by the application at startup via :func:`set_plugin_registry`
(the registry lives on the main thread; read-only access from the worker thread
is safe under the GIL for the frozen-status reads this module performs).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from typing import Any, cast

from .constants import TASK_TYPE_PLUGIN_ACTION
from .plugin_host_client import get_plugin_host_client
from .plugin_protocol import PluginRequest
from .plugin_registry import PluginRegistry

logger = logging.getLogger(__name__)

# 插件请求的相对截止时间（秒）。deadline_ns 计算为 `now_ns + 此值对应的纳秒`，
# 闭合 R-9 契约（插件宿主据此判定请求是否已超时）。
PLUGIN_REQUEST_DEADLINE_SECONDS = 30.0


# Process-wide registry reference, set by the application on startup.
_plugin_registry: PluginRegistry | None = None


def set_plugin_registry(registry: PluginRegistry | None) -> None:
    """Install the app-level plugin registry used for validation/dispatch."""

    global _plugin_registry
    _plugin_registry = registry


def get_plugin_registry() -> PluginRegistry | None:
    """Return the process-wide plugin registry, or None if not yet installed.

    Returns:
        The registry previously passed to :func:`set_plugin_registry`, or
        None when the application has not wired one yet.
    """
    return _plugin_registry


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------

def validate_plugin_action_fields(task: Mapping[str, Any]) -> str | None:
    """Return a human-readable error string if *task* is an invalid plugin action.

    Returns ``None`` when the step is well-formed. A referenced plugin/action that
    is missing, corrupt, incompatible, disabled, or dependency-missing is reported
    as an error so the run is blocked *before* launch (design §5.5).
    """

    try:
        task_type = float(task.get("type", 0))
    except (TypeError, ValueError):
        return None
    if task_type != TASK_TYPE_PLUGIN_ACTION:
        return None

    plugin_id = task.get("plugin_id")
    action_id = task.get("plugin_action_id")
    if not plugin_id:
        return "插件动作缺少 plugin_id"
    if not action_id:
        return "插件动作缺少 plugin_action_id"

    registry = _plugin_registry
    if registry is None:
        # No registry wired yet — treat as a configuration problem, not a crash.
        return "插件注册表尚未初始化，无法校验插件动作"

    try:

        status = registry.status(plugin_id)
    except Exception as exc:
        logger.warning("查询插件状态失败 (%s): %s", plugin_id, exc, exc_info=True)
        return f"未知插件：{plugin_id}"

    if status.value == "corrupt":
        return f"插件已损坏：{plugin_id}"
    if status.value == "incompatible":
        return f"插件不兼容当前版本：{plugin_id}"
    if status.value == "dependency_missing":
        return f"插件依赖缺失：{plugin_id}"
    if status.value != "enabled":
        return f"插件未启用：{plugin_id}"

    action = registry.get_action(plugin_id, action_id)
    if action is None:
        return f"插件动作不存在：{plugin_id}/{action_id}"

    # Validate the plugin_config payload against the action's input_schema
    # (review item #8). This blocks obviously-invalid runs before launch.
    config_error = validate_plugin_config(
        plugin_id, action_id, task.get("plugin_config"), registry
    )
    if config_error:
        return config_error
    return None


def collect_plugin_dependencies(tasks: Iterable[Mapping[str, Any]]) -> set[str]:
    """Return the set of plugin ids referenced by any plugin-action step."""

    deps: set[str] = set()
    for task in tasks:
        try:
            task_type = float(task.get("type", 0))
        except (TypeError, ValueError):
            continue
        if task_type == TASK_TYPE_PLUGIN_ACTION:
            pid = task.get("plugin_id")
            if pid:
                deps.add(pid)
    return deps


# ---------------------------------------------------------------------------
# plugin_config validation against the action input_schema (review item #8)
# ---------------------------------------------------------------------------

def _config_type_ok(expected: str, value: Any) -> bool:
    """Return True when *value* satisfies the JSON-schema-lite *expected* type."""

    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        # JSON Schema: booleans are NOT integers.
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "object":
        return isinstance(value, Mapping)
    return True


def _validate_config_value(name: str, schema: Mapping[str, Any], value: Any) -> str | None:
    """Validate a single *value* against one property *schema*.

    Returns a Chinese error fragment (without the leading param name) or None.
    """

    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_config_type_ok(t, value) for t in expected):
            return f"类型应为 {expected} 之一，实际为 {type(value).__name__}"
    elif expected and not _config_type_ok(expected, value):
        return f"类型应为 {expected}，实际为 {type(value).__name__}"

    # RM-SCOPE015-002: the enum check only makes sense when ``enum`` is an array.
    # ``plugin_manifest._parse_actions`` requires ``input_schema`` to be an object
    # but does not validate ``enum``'s content, and this function's result is used
    # as a pre-run blocking condition (设计 §5.5). A non-array ``enum`` therefore
    # produced a false positive that really did stop the user's run:
    # ``str``/``Mapping`` degrade ``value not in enum`` into a substring or key
    # match, while ``int``/``None`` raise ``TypeError``. Skip such a schema as
    # "unable to check" rather than reporting a bogus error.
    enum_values = schema.get("enum")
    if isinstance(enum_values, (list, tuple)) and value not in enum_values:
        return f"取值不在允许范围：{enum_values}"

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # A JSON Schema's bounds must themselves be numbers for the comparison to
        # mean anything; a manifest carrying ``"minimum": "1"`` would otherwise make
        # this raise ``TypeError`` and escape the pre-run blocking check. Bind them
        # to locals so the numeric guard is explicit (and visible to the type
        # checker) instead of comparing against an unconstrained ``Any``.
        minimum = schema.get("minimum")
        if (
            isinstance(minimum, (int, float))
            and not isinstance(minimum, bool)
            and value < minimum
        ):
            return f"小于最小值 {minimum}"
        maximum = schema.get("maximum")
        if (
            isinstance(maximum, (int, float))
            and not isinstance(maximum, bool)
            and value > maximum
        ):
            return f"大于最大值 {maximum}"

    items = schema.get("items")
    if isinstance(items, Mapping) and isinstance(value, (list, tuple)):
        item_type = items.get("type")
        for idx, item in enumerate(value):
            if item_type and not _config_type_ok(item_type, item):
                return f"[{idx}] 类型应为 {item_type}，实际为 {type(item).__name__}"

    props = schema.get("properties")
    if isinstance(props, Mapping) and isinstance(value, Mapping):
        for key, item_value in value.items():
            sub = props.get(key)
            if sub is None:
                continue
            err = _validate_config_value(f"{name}.{key}", sub, item_value)
            if err:
                return err
    return None


def validate_plugin_config(
    plugin_id: str,
    action_id: str,
    config: Any,
    registry: PluginRegistry | None = None,
) -> str | None:
    """Validate a plugin_config payload against the action's input_schema.

    Returns a human-readable error (Chinese) when the config violates the
    declared schema, or ``None`` when it is valid or cannot be checked.

    Design choices (review item #8):
    * Unknown keys are *allowed* (deferred to the plugin) so adding a new
      optional field never breaks existing configs.
    * ``required`` is only enforced when the schema declares it (none of the
      shipped schemas do, so optional-by-default configs keep working).
    * Any internal error is swallowed so a validator bug can never block a run.
    """

    try:
        # Mirror execute_plugin_action: a missing config is an empty object,
        # which is valid for actions that take no required parameters.
        config = config or {}
        if not isinstance(config, dict):
            return "plugin_config 必须是对象（键值对）"
        reg = registry or _plugin_registry
        if reg is None:
            return None
        action = reg.get_action(plugin_id, action_id)
        if action is None:
            return None
        schema = getattr(action, "input_schema", None)
        if not isinstance(schema, Mapping):
            return None
        props = schema.get("properties")
        if not isinstance(props, Mapping):
            props = {}
        for key, value in config.items():
            prop = props.get(key)
            if prop is None:
                continue
            err = _validate_config_value(key, prop, value)
            if err:
                return f"插件 {plugin_id}/{action_id} 参数 {err}"
        required = schema.get("required")
        if isinstance(required, (list, tuple)):
            missing = [r for r in required if r not in config]
            if missing:
                return (
                    f"插件 {plugin_id}/{action_id} 缺少必填参数："
                    f"{', '.join(missing)}"
                )
        return None
    except Exception as exc:
        logger.warning("校验插件动作参数失败: %s", exc, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Execution dispatch (called from engine_actions via the registered handler)
# ---------------------------------------------------------------------------

def execute_plugin_action(engine: Any, context: Any) -> str:
    """Run a single plugin-action step.

    Returns the engine step result: ``"success"`` on success, ``"error"`` on
    failure. Never raises — failures are logged and reported as ``"error"`` so
    the run stops cleanly instead of crashing the worker thread.
    """

    # Alias as ``Any`` on purpose: ``isinstance(..., dict)`` narrows to an
    # *unparameterised* ``dict``, so re-declaring it as ``dict[str, Any]`` would not
    # narrow anything and would itself be reported as a partially-unknown variable.
    raw_task = context.task
    task: Any = raw_task if isinstance(raw_task, dict) else {}
    plugin_id = task.get("plugin_id", "")
    action_id = task.get("plugin_action_id", "")
    # ``step_info`` is untyped engine context; ``cast`` (rather than an annotated
    # alias) is what actually suppresses the partially-unknown diagnostic, because
    # the checker otherwise keeps the inferred bare-``dict`` type.
    step_info = cast(Mapping[str, Any], context.step_info or {})
    step_label = f"循环#{step_info.get('loop')} 步{step_info.get('step')}"

    if not plugin_id or not action_id:
        _log(engine, f"<font color='red'>    [插件动作] {step_label}: 缺少 plugin_id 或 plugin_action_id</font>", True)
        return "error"

    # R-9: 闭合插件请求契约。
    # run_id 取 engine 当前活动运行的真实 ID（worker 执行 step 期间必为非空）；
    # 不可得时回退空串以兼容既有协议。
    run_id = ""
    lifecycle = getattr(engine, "lifecycle", None)
    if lifecycle is not None:
        active = lifecycle.active_run_id
        if active is not None:
            run_id = str(active)
    # deadline_ns 计算为相对截止时刻的纳秒时间戳（>0），闭合契约第二项。
    deadline_ns = time.monotonic_ns() + int(
        PLUGIN_REQUEST_DEADLINE_SECONDS * 1_000_000_000
    )
    request = PluginRequest(
        run_id=run_id,
        # FR-20261003-605: 稳定身份是 ``step_id``；任务里并不存在 ``id`` 键，旧代码
        # 读它必然落空，于是每一步都退化成步号，宿主侧无法区分同一脚本的两次运行。
        # 旧数据缺失 step_id 时回退步号，保持可用。
        step_id=str(task.get("step_id") or step_info.get("step", "")),
        plugin_id=plugin_id,
        action_id=action_id,
        deadline_ns=deadline_ns,
        payload=task.get("plugin_config") or {},
    )

    try:
        client = get_plugin_host_client()
        response = client.call(request)
    except Exception as error:  # noqa: BLE001 - surface, never crash the run
        _log(engine, f"<font color='red'>    [插件动作] {step_label}: 调用插件宿主失败 -> {error}</font>", True)
        return "error"

    if not response.ok:
        # Review item #11 / #53: redact user file paths from the error before it
        # is persisted to the run log, honouring the privacy-protected setting.
        error_text = _redact_plugin_text(response.error)
        _log(engine, f"<font color='red'>    [插件动作] {step_label}: 插件返回错误 -> {error_text}</font>", True)
        return "error"

    _log(engine, f"    [插件动作] {step_label}: {plugin_id}/{action_id} 执行完成", False)
    return "success"


def _redact_plugin_text(text: Any) -> str:
    """Redact plugin error/result text before persisting to the run log.

    FR-20260809-D6-①: 复用 ``plain_log_text``——修复前仅脱敏路径（
    ``redact_sensitive_paths``），插件错误文本中的秘密值形态（password=、
    Bearer、JWT 等）会走漏；plain_log_text 的第二道擦除层无条件擦除秘密值
    （设计基准 #46/#53：凭据值无论隐私开关如何都不得进日志）。隐私保护关闭时
    仍放行普通路径文本（仅关闭路径脱敏，秘密值擦除不受开关影响）。
    """
    if not text:
        return str(text)
    try:
        from .logging_service import get_log_config

        protected = bool(get_log_config().get("privacy_protected", True))
    except Exception as exc:
        logger.warning("读取日志隐私配置失败: %s", exc, exc_info=True)
        protected = True
    try:
        from .privacy import plain_log_text

        # redact_paths 跟随隐私开关；秘密值擦除在 plain_log_text 内无条件执行。
        return plain_log_text(text, redact_paths=protected)
    except Exception as exc:
        logger.warning("插件文本脱敏失败: %s", exc, exc_info=True)
        # 脱敏失败不落原文（与 crash_guard 的 fail-closed 一致）——插件错误
        # 文本宁可显示占位符也不能让可能的秘密值进入日志。
        return "<插件文本脱敏失败>"


def _log(engine: Any, message: str, critical: bool) -> None:
    try:
        from .log_policy import LOG_ACTION, LOG_CRITICAL

        engine.log(message, LOG_CRITICAL if critical else LOG_ACTION, critical=critical)
    except Exception as exc:
        # Last-resort fallback if log policy is unavailable.
        logger.warning("插件日志记录失败: %s", exc, exc_info=True)
        try:
            engine.log(message)
        except Exception as exc:
            logger.warning("插件日志回退记录失败: %s", exc, exc_info=True)
