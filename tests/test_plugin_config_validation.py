"""Regression tests for plugin_config validation against input_schema (#8)."""

import unittest

from fukua_rpa.plugin_actions import validate_plugin_config


class _FakeAction:
    def __init__(self, schema):
        self.input_schema = schema


class _FakeRegistry:
    def __init__(self, action=None):
        self._action = action

    def get_action(self, plugin_id, action_id):
        return self._action


class TestPluginConfigValidation(unittest.TestCase):
    def test_none_registry_is_none(self):
        # Without a registry we cannot validate; never block the run.
        self.assertIsNone(validate_plugin_config("p", "a", {"x": 1}))

    def test_valid_passes(self):
        schema = {
            "type": "object",
            "properties": {"fps": {"type": "integer", "minimum": 1, "maximum": 60}},
        }
        reg = _FakeRegistry(_FakeAction(schema))
        self.assertIsNone(validate_plugin_config("p", "a", {"fps": 30}, reg))

    def test_out_of_range_fails(self):
        schema = {
            "type": "object",
            "properties": {"fps": {"type": "integer", "minimum": 1, "maximum": 60}},
        }
        reg = _FakeRegistry(_FakeAction(schema))
        self.assertIsNotNone(validate_plugin_config("p", "a", {"fps": 99}, reg))

    def test_wrong_type_fails(self):
        schema = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
        }
        reg = _FakeRegistry(_FakeAction(schema))
        self.assertIsNotNone(validate_plugin_config("p", "a", {"url": 123}, reg))

    def test_enum_fails(self):
        schema = {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["a", "b"]}},
        }
        reg = _FakeRegistry(_FakeAction(schema))
        self.assertIsNotNone(validate_plugin_config("p", "a", {"mode": "c"}, reg))

    def test_unknown_key_allowed(self):
        # Unknown keys are deferred to the plugin, not rejected.
        schema = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
        }
        reg = _FakeRegistry(_FakeAction(schema))
        self.assertIsNone(
            validate_plugin_config("p", "a", {"extra": 1, "url": "x"}, reg)
        )

    def test_required_enforced(self):
        schema = {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        }
        reg = _FakeRegistry(_FakeAction(schema))
        self.assertIsNotNone(validate_plugin_config("p", "a", {}, reg))
        self.assertIsNone(validate_plugin_config("p", "a", {"url": "x"}, reg))

    def test_non_dict_config_fails(self):
        reg = _FakeRegistry(_FakeAction({}))
        self.assertIsNotNone(validate_plugin_config("p", "a", "notadict", reg))

    def test_none_config_is_valid_empty(self):
        # Mirror execute_plugin_action: a missing config is an empty object and
        # must not be rejected (most actions take no required parameters).
        schema = {
            "type": "object",
            "properties": {"fps": {"type": "integer", "minimum": 1, "maximum": 60}},
        }
        reg = _FakeRegistry(_FakeAction(schema))
        self.assertIsNone(validate_plugin_config("p", "a", None, reg))

    def test_bool_not_integer(self):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
        }
        reg = _FakeRegistry(_FakeAction(schema))
        self.assertIsNotNone(validate_plugin_config("p", "a", {"n": True}, reg))

    def test_nested_object_validated(self):
        schema = {
            "type": "object",
            "properties": {
                "region": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "integer", "minimum": 0},
                        "y": {"type": "integer", "minimum": 0},
                    },
                }
            },
        }
        reg = _FakeRegistry(_FakeAction(schema))
        # Negative x violates the nested minimum -> error.
        self.assertIsNotNone(
            validate_plugin_config("p", "a", {"region": {"x": -1, "y": 5}}, reg)
        )
        # Valid nested object -> no error.
        self.assertIsNone(
            validate_plugin_config("p", "a", {"region": {"x": 0, "y": 5}}, reg)
        )


if __name__ == "__main__":
    unittest.main()
