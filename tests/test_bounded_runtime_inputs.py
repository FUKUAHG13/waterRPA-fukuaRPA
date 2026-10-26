import json
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fukua_rpa.constants import TASK_CLIPBOARD_KEY
from fukua_rpa.task_model import parse_coord_step_manual_points
from fukua_rpa.ui.controllers.workflow_editor import parse_task_clipboard


class BoundedRuntimeInputTests(unittest.TestCase):
    def test_manual_points_parse_valid_coordinates(self):
        self.assertEqual(
            parse_coord_step_manual_points('{"1": [10, 20], "3": [30.4, 40.6]}'),
            {1: (10.0, 20.0), 3: (30.4, 40.6)},
        )

    def test_manual_points_fail_closed_on_deep_or_non_object_json(self):
        deeply_nested = "[" * 30 + "0" + "]" * 30
        self.assertEqual(parse_coord_step_manual_points(deeply_nested), {})
        self.assertEqual(parse_coord_step_manual_points("[]"), {})

    def test_clipboard_parser_accepts_wrapped_task(self):
        task = {"type": 1.0, "value": "10,20"}
        text = json.dumps({TASK_CLIPBOARD_KEY: task}, ensure_ascii=False)

        self.assertEqual(parse_task_clipboard(text), task)

    def test_clipboard_parser_rejects_duplicate_or_deep_json(self):
        with self.assertRaisesRegex(ValueError, "重复字段"):
            parse_task_clipboard(
                '{"fukuaRPA_task": {"type": 1}, '
                '"fukuaRPA_task": {"type": 2}}'
            )
        deeply_nested = "[" * 30 + "0" + "]" * 30
        with self.assertRaisesRegex(ValueError, "嵌套"):
            parse_task_clipboard(
                '{"fukuaRPA_task": {"type": 1, "unknown": '
                + deeply_nested
                + "}}"
            )

    def test_manual_points_empty_null_and_four_byte_utf8_are_safe(self):
        # Empty, missing, and null-byte input parse to an empty mapping rather
        # than raising or crashing.
        for value in ("", None, "\x00", '{"1": [10, 20]}\x00'):
            with self.subTest(value=repr(value)):
                self.assertEqual(parse_coord_step_manual_points(value), {})
        # Four-byte UTF-8 (emoji) in keys or coordinate values is rejected
        # gracefully (no surrogate/decode crash) while valid points still parse.
        self.assertEqual(
            parse_coord_step_manual_points('{"\U0001F600": [10, 20]}'), {}
        )
        self.assertEqual(
            parse_coord_step_manual_points('{"1": ["\U0001F600", 20]}'), {}
        )
        self.assertEqual(
            parse_coord_step_manual_points('{"2": [30, 40]}'),
            {2: (30.0, 40.0)},
        )

    def test_clipboard_empty_and_null_byte_are_rejected(self):
        # An empty or null-byte clipboard is refused fail-closed; callers wrap
        # this in try/except and treat it as "no task on the clipboard".
        for value in ("", None, "\x00"):
            with self.subTest(value=repr(value)), self.assertRaisesRegex(ValueError, "格式无效"):
                parse_task_clipboard(value)

    def test_clipboard_four_byte_utf8_value_round_trips(self):
        task = {"type": 1.0, "value": "点击 \U0001F600 按钮"}
        text = json.dumps({TASK_CLIPBOARD_KEY: task}, ensure_ascii=False)
        self.assertEqual(parse_task_clipboard(text), task)
        # A dict without the task key is not treated as a pasted task.
        self.assertIsNone(parse_task_clipboard('{"\U0001F600": 1}'))


if __name__ == "__main__":
    unittest.main()
