"""Coverage tests for fukua_rpa.plugin_actions — targets uncovered branches.

Covers: set/get_plugin_registry, validate_plugin_action_fields (all status
branches), collect_plugin_dependencies, _config_type_ok (boolean/number/array/
object), _validate_config_value (list-type, items, nested properties, enum
container guard), validate_plugin_config (action None / schema not Mapping /
props not Mapping / exception swallow), execute_plugin_action (missing ids,
client exception, response error), _redact_plugin_text, _log fallback paths.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fukua_rpa.constants import TASK_TYPE_PLUGIN_ACTION
from fukua_rpa.plugin_actions import (
    _config_type_ok,
    _log,
    _redact_plugin_text,
    _validate_config_value,
    collect_plugin_dependencies,
    execute_plugin_action,
    get_plugin_registry,
    set_plugin_registry,
    validate_plugin_action_fields,
    validate_plugin_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeAction:
    def __init__(self, schema=None):
        self.input_schema = schema


class _FakeRegistry:
    """Minimal registry stub supporting status() and get_action()."""

    def __init__(self, status_value="enabled", action=None, status_exc=None):
        self._status_value = status_value
        self._action = action
        self._status_exc = status_exc

    def status(self, plugin_id):
        if self._status_exc:
            raise self._status_exc
        return SimpleNamespace(value=self._status_value)

    def get_action(self, plugin_id, action_id):
        return self._action


def _task(type_val=TASK_TYPE_PLUGIN_ACTION, plugin_id="p1", action_id="a1", config=None):
    d = {"type": type_val, "plugin_id": plugin_id, "plugin_action_id": action_id}
    if config is not None:
        d["plugin_config"] = config
    return d


# ---------------------------------------------------------------------------
# set/get_plugin_registry
# ---------------------------------------------------------------------------

class TestRegistryAccessors(unittest.TestCase):
    def tearDown(self):
        set_plugin_registry(None)

    def test_set_and_get(self):
        reg = _FakeRegistry()
        set_plugin_registry(reg)
        self.assertIs(get_plugin_registry(), reg)

    def test_get_returns_none_initially(self):
        set_plugin_registry(None)
        self.assertIsNone(get_plugin_registry())


# ---------------------------------------------------------------------------
# validate_plugin_action_fields
# ---------------------------------------------------------------------------

class TestValidatePluginActionFields(unittest.TestCase):
    def tearDown(self):
        set_plugin_registry(None)

    def test_non_plugin_type_returns_none(self):
        # type != TASK_TYPE_PLUGIN_ACTION -> None (lines 76-77)
        self.assertIsNone(validate_plugin_action_fields({"type": 1.0}))

    def test_invalid_type_value_returns_none(self):
        # type that cannot be float -> None (lines 74-75)
        self.assertIsNone(validate_plugin_action_fields({"type": "abc"}))

    def test_missing_plugin_id(self):
        task = _task(plugin_id="")
        set_plugin_registry(_FakeRegistry())
        err = validate_plugin_action_fields(task)
        self.assertIn("plugin_id", err)

    def test_missing_action_id(self):
        task = _task(action_id="")
        set_plugin_registry(_FakeRegistry())
        err = validate_plugin_action_fields(task)
        self.assertIn("plugin_action_id", err)

    def test_no_registry(self):
        set_plugin_registry(None)
        err = validate_plugin_action_fields(_task())
        self.assertIn("注册表", err)

    def test_status_exception(self):
        reg = _FakeRegistry(status_exc=RuntimeError("boom"))
        set_plugin_registry(reg)
        err = validate_plugin_action_fields(_task())
        self.assertIn("未知插件", err)

    def test_status_corrupt(self):
        set_plugin_registry(_FakeRegistry(status_value="corrupt"))
        err = validate_plugin_action_fields(_task())
        self.assertIn("损坏", err)

    def test_status_incompatible(self):
        set_plugin_registry(_FakeRegistry(status_value="incompatible"))
        err = validate_plugin_action_fields(_task())
        self.assertIn("不兼容", err)

    def test_status_dependency_missing(self):
        set_plugin_registry(_FakeRegistry(status_value="dependency_missing"))
        err = validate_plugin_action_fields(_task())
        self.assertIn("依赖缺失", err)

    def test_status_disabled(self):
        set_plugin_registry(_FakeRegistry(status_value="disabled"))
        err = validate_plugin_action_fields(_task())
        self.assertIn("未启用", err)

    def test_action_not_found(self):
        reg = _FakeRegistry(status_value="enabled", action=None)
        set_plugin_registry(reg)
        err = validate_plugin_action_fields(_task())
        self.assertIn("不存在", err)

    def test_valid_action_no_config_error(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        reg = _FakeRegistry(status_value="enabled", action=_FakeAction(schema))
        set_plugin_registry(reg)
        self.assertIsNone(validate_plugin_action_fields(_task(config={"x": 1})))

    def test_config_error_propagates(self):
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        reg = _FakeRegistry(status_value="enabled", action=_FakeAction(schema))
        set_plugin_registry(reg)
        err = validate_plugin_action_fields(_task(config={"x": "bad"}))
        self.assertIsNotNone(err)


# ---------------------------------------------------------------------------
# collect_plugin_dependencies
# ---------------------------------------------------------------------------

class TestCollectPluginDependencies(unittest.TestCase):
    def test_collects_plugin_ids(self):
        tasks = [
            _task(plugin_id="p1"),
            _task(plugin_id="p2"),
            {"type": 1.0, "plugin_id": "ignored"},
        ]
        deps = collect_plugin_dependencies(tasks)
        self.assertEqual(deps, {"p1", "p2"})

    def test_skips_invalid_type(self):
        tasks = [{"type": "bad"}, _task(plugin_id="p1")]
        deps = collect_plugin_dependencies(tasks)
        self.assertEqual(deps, {"p1"})

    def test_skips_empty_plugin_id(self):
        tasks = [_task(plugin_id="")]
        deps = collect_plugin_dependencies(tasks)
        self.assertEqual(deps, set())


# ---------------------------------------------------------------------------
# _config_type_ok — uncovered type branches
# ---------------------------------------------------------------------------

class TestConfigTypeOk(unittest.TestCase):
    def test_boolean(self):
        self.assertTrue(_config_type_ok("boolean", True))
        self.assertFalse(_config_type_ok("boolean", 1))

    def test_number(self):
        self.assertTrue(_config_type_ok("number", 3.14))
        self.assertTrue(_config_type_ok("number", 7))
        self.assertFalse(_config_type_ok("number", True))

    def test_array(self):
        self.assertTrue(_config_type_ok("array", [1, 2]))
        self.assertTrue(_config_type_ok("array", (1, 2)))
        self.assertFalse(_config_type_ok("array", "no"))

    def test_object(self):
        self.assertTrue(_config_type_ok("object", {"a": 1}))
        self.assertFalse(_config_type_ok("object", [1]))

    def test_unknown_type_always_ok(self):
        self.assertTrue(_config_type_ok("whatever", object()))


# ---------------------------------------------------------------------------
# _validate_config_value — list type, items, nested properties
# ---------------------------------------------------------------------------

class TestValidateConfigValue(unittest.TestCase):
    def test_list_type_union(self):
        schema = {"type": ["string", "integer"]}
        self.assertIsNone(_validate_config_value("x", schema, "hello"))
        self.assertIsNone(_validate_config_value("x", schema, 42))
        err = _validate_config_value("x", schema, [1])
        self.assertIsNotNone(err)

    def test_items_type_check(self):
        schema = {"type": "array", "items": {"type": "integer"}}
        self.assertIsNone(_validate_config_value("x", schema, [1, 2, 3]))
        err = _validate_config_value("x", schema, [1, "bad"])
        self.assertIn("[1]", err)

    def test_nested_properties_continue_on_unknown(self):
        # properties sub-validation: unknown sub-keys are skipped (continue)
        schema = {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
        }
        # "b" is not in properties -> continue, no error
        self.assertIsNone(_validate_config_value("x", schema, {"a": 1, "b": "any"}))
        # "a" wrong type -> error
        err = _validate_config_value("x", schema, {"a": "no"})
        self.assertIsNotNone(err)


class TestEnumContainerGuard(unittest.TestCase):
    """RM-SCOPE015-002：`enum` 检查必须只在它是数组时生效。

    `plugin_manifest._parse_actions` 只要求 `input_schema` 是对象，不校验 `enum`
    内容；而 `validate_plugin_action_fields` 把这里返回的错误当作运行前阻断条件
    （设计 §5.5）。因此非数组 `enum` 造成的误报会真实挡住用户的运行。
    """

    def test_array_enum_is_member_checked(self):
        schema = {"type": "string", "enum": ["a", "b"]}
        self.assertIsNone(_validate_config_value("x", schema, "a"))
        self.assertIsNotNone(_validate_config_value("x", schema, "c"))
        # 成员判定，不是子串判定：前缀不得因为出现在某个成员里而通过。
        self.assertIsNotNone(_validate_config_value("x", schema, "ab"))

    def test_non_container_enum_is_skipped(self):
        """非数组 enum 按「无法检查」跳过，既不误报也不抛错。

        `str` / `Mapping` 会让 `value not in enum` 退化成子串或按键匹配；`int` /
        `None` 会直接抛 `TypeError`。
        """
        for enum_value in ("abc", {"a": 1}, 5, None, 1.5, True):
            with self.subTest(enum=enum_value):
                schema = {"type": "string", "enum": enum_value}
                self.assertIsNone(_validate_config_value("x", schema, "z"))
                self.assertIsNone(_validate_config_value("x", schema, "a"))

    def test_non_container_enum_does_not_block_a_run(self):
        """端到端：声明了非数组 enum 的插件不得因该参数被拒绝运行。"""
        action = _FakeAction(
            schema={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": "abc"},
                    "level": {"type": "integer", "enum": [1, 2]},
                },
            }
        )
        reg = _FakeRegistry(action=action)
        self.assertIsNone(validate_plugin_config("p", "a", {"mode": "x"}, reg))
        self.assertIsNone(validate_plugin_config("p", "a", {"level": 2}, reg))
        # 数组 enum 的成员判定不受影响。
        self.assertIsNotNone(validate_plugin_config("p", "a", {"level": 3}, reg))


# ---------------------------------------------------------------------------
# validate_plugin_config — edge branches
# ---------------------------------------------------------------------------

class TestValidatePluginConfigEdge(unittest.TestCase):
    def test_action_none_returns_none(self):
        reg = _FakeRegistry(action=None)
        self.assertIsNone(validate_plugin_config("p", "a", {"x": 1}, reg))

    def test_schema_not_mapping_returns_none(self):
        action = _FakeAction(schema="not_a_mapping")
        reg = _FakeRegistry(action=action)
        self.assertIsNone(validate_plugin_config("p", "a", {"x": 1}, reg))

    def test_props_not_mapping_treated_empty(self):
        action = _FakeAction(schema={"type": "object", "properties": "bad"})
        reg = _FakeRegistry(action=action)
        # props is not a Mapping -> treated as {} -> unknown keys pass
        self.assertIsNone(validate_plugin_config("p", "a", {"x": 1}, reg))

    def test_exception_swallowed(self):
        # Force an internal exception during validation
        action = _FakeAction(schema={"type": "object", "properties": {"x": {"type": "integer"}}})
        reg = _FakeRegistry(action=action)
        with patch(
            "fukua_rpa.plugin_actions._validate_config_value",
            side_effect=RuntimeError("boom"),
        ):
            result = validate_plugin_config("p", "a", {"x": 1}, reg)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# execute_plugin_action — error paths
# ---------------------------------------------------------------------------

class TestExecutePluginActionErrors(unittest.TestCase):
    def _engine(self):
        return SimpleNamespace(
            lifecycle=SimpleNamespace(active_run_id=99),
            log=MagicMock(),
        )

    def test_missing_plugin_id_returns_error(self):
        ctx = SimpleNamespace(
            task={"plugin_id": "", "plugin_action_id": "a1"},
            step_info={"loop": 1, "step": 1},
        )
        result = execute_plugin_action(self._engine(), ctx)
        self.assertEqual(result, "error")

    def test_missing_action_id_returns_error(self):
        ctx = SimpleNamespace(
            task={"plugin_id": "p1", "plugin_action_id": ""},
            step_info={"loop": 1, "step": 1},
        )
        result = execute_plugin_action(self._engine(), ctx)
        self.assertEqual(result, "error")

    def test_step_id_uses_stable_step_id_not_legacy_id_key(self):
        """FR-20261003-605: PluginRequest.step_id 取稳定 step_id，而非不存在的 "id"。"""
        ctx = SimpleNamespace(
            task={
                "type": TASK_TYPE_PLUGIN_ACTION,
                "plugin_id": "p1",
                "plugin_action_id": "a1",
                "step_id": "stable-uuid",
            },
            step_info={"loop": 1, "step": 3},
        )
        mock_response = SimpleNamespace(ok=True, result={}, error=None)
        mock_client = MagicMock()
        mock_client.call.return_value = mock_response
        with patch(
            "fukua_rpa.plugin_actions.get_plugin_host_client",
            return_value=mock_client,
        ):
            result = execute_plugin_action(self._engine(), ctx)
        self.assertEqual(result, "success")
        request = mock_client.call.call_args.args[0]
        self.assertEqual(request.step_id, "stable-uuid")

    def test_step_id_falls_back_to_step_number_when_missing(self):
        """FR-20261003-605: 旧数据缺失 step_id 时回退步号且不抛异常。"""
        ctx = SimpleNamespace(
            task={"plugin_id": "p1", "plugin_action_id": "a1"},
            step_info={"loop": 1, "step": 7},
        )
        mock_response = SimpleNamespace(ok=True, result={}, error=None)
        mock_client = MagicMock()
        mock_client.call.return_value = mock_response
        with patch(
            "fukua_rpa.plugin_actions.get_plugin_host_client",
            return_value=mock_client,
        ):
            result = execute_plugin_action(self._engine(), ctx)
        self.assertEqual(result, "success")
        request = mock_client.call.call_args.args[0]
        self.assertEqual(request.step_id, "7")

    def test_client_exception_returns_error(self):
        ctx = SimpleNamespace(
            task={"plugin_id": "p1", "plugin_action_id": "a1", "step_id": "s1"},
            step_info={"loop": 1, "step": 2},
        )
        mock_client = MagicMock()
        mock_client.call.side_effect = ConnectionError("pipe broken")
        with patch(
            "fukua_rpa.plugin_actions.get_plugin_host_client",
            return_value=mock_client,
        ):
            result = execute_plugin_action(self._engine(), ctx)
        self.assertEqual(result, "error")

    def test_response_not_ok_returns_error(self):
        ctx = SimpleNamespace(
            task={"plugin_id": "p1", "plugin_action_id": "a1", "step_id": "s1"},
            step_info={"loop": 1, "step": 2},
        )
        mock_response = SimpleNamespace(ok=False, error="C:\\Users\\secret\\file.txt failed")
        mock_client = MagicMock()
        mock_client.call.return_value = mock_response
        with patch(
            "fukua_rpa.plugin_actions.get_plugin_host_client",
            return_value=mock_client,
        ):
            result = execute_plugin_action(self._engine(), ctx)
        self.assertEqual(result, "error")

    def test_no_lifecycle_fallback(self):
        # engine without lifecycle attribute -> run_id falls back to ""
        engine = SimpleNamespace(log=MagicMock())
        ctx = SimpleNamespace(
            task={"plugin_id": "p1", "plugin_action_id": "a1", "step_id": "s1"},
            step_info={"loop": 1, "step": 2},
        )
        mock_response = SimpleNamespace(ok=True, result={}, error=None)
        mock_client = MagicMock()
        mock_client.call.return_value = mock_response
        with patch(
            "fukua_rpa.plugin_actions.get_plugin_host_client",
            return_value=mock_client,
        ):
            result = execute_plugin_action(engine, ctx)
        self.assertEqual(result, "success")


# ---------------------------------------------------------------------------
# _redact_plugin_text
# ---------------------------------------------------------------------------

class TestRedactPluginText(unittest.TestCase):
    def test_empty_text(self):
        self.assertEqual(_redact_plugin_text(""), "")
        self.assertEqual(_redact_plugin_text(None), "None")

    def test_privacy_disabled_returns_original(self):
        with patch(
            "fukua_rpa.logging_service.get_log_config",
            return_value={"privacy_protected": False},
        ):
            result = _redact_plugin_text("some error")
        self.assertEqual(result, "some error")

    def test_privacy_enabled_redacts(self):
        with patch(
            "fukua_rpa.logging_service.get_log_config",
            return_value={"privacy_protected": True},
        ), patch(
            "fukua_rpa.privacy.plain_log_text",
            return_value="[REDACTED]",
        ):
            result = _redact_plugin_text("C:\\Users\\me\\file.txt")
        self.assertEqual(result, "[REDACTED]")

    def test_log_config_exception_falls_back_protected(self):
        with patch(
            "fukua_rpa.logging_service.get_log_config",
            side_effect=RuntimeError("no config"),
        ), patch(
            "fukua_rpa.privacy.plain_log_text",
            return_value="[REDACTED]",
        ):
            result = _redact_plugin_text("path here")
        self.assertEqual(result, "[REDACTED]")

    def test_redact_exception_returns_placeholder(self):
        with patch(
            "fukua_rpa.logging_service.get_log_config",
            return_value={"privacy_protected": True},
        ), patch(
            "fukua_rpa.privacy.plain_log_text",
            side_effect=RuntimeError("redact fail"),
        ):
            # FR-20260809-D6-①: fail-closed——脱敏失败不落原文（与 crash_guard
            # 一致），避免可能的秘密值进入日志。
            result = _redact_plugin_text("original text")
        self.assertEqual(result, "<插件文本脱敏失败>")


# ---------------------------------------------------------------------------
# _log — fallback paths
# ---------------------------------------------------------------------------

class TestLogFallback(unittest.TestCase):
    def test_primary_log_works(self):
        engine = SimpleNamespace(log=MagicMock())
        with patch("fukua_rpa.log_policy.LOG_ACTION", 1), \
             patch("fukua_rpa.log_policy.LOG_CRITICAL", 2):
            _log(engine, "msg", False)
        engine.log.assert_called_once()

    def test_log_policy_import_fails_fallback(self):
        engine = SimpleNamespace(log=MagicMock())
        with patch.dict("sys.modules", {"fukua_rpa.log_policy": None}):
            _log(engine, "msg", True)
        # Fallback: engine.log(message) called with single arg
        engine.log.assert_called()

    def test_all_log_paths_fail(self):
        engine = SimpleNamespace(log=MagicMock(side_effect=RuntimeError("no log")))
        with patch.dict("sys.modules", {"fukua_rpa.log_policy": None}):
            # Should not raise
            _log(engine, "msg", True)


if __name__ == "__main__":
    unittest.main()
