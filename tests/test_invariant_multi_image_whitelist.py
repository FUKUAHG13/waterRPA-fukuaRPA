"""Regression tests for invariant #76: multi-image action whitelist +
budget + import/serialize/desensitization.

Invariants under test (ARCHITECTURE.md #76):
  (a) On parse, the rule list is truncated to MAX_MULTI_IMAGE_ACTION_RULES.
  (b) Unknown/invalid action types are rejected (whitelist enforcement).
  (c) A valid rule round-trips through parse -> serialize -> parse, and image
      paths are desensitized (cleared) when the profile is packaged for export.
"""

import json
import os
import tempfile
import unittest

from PIL import Image

from fukua_rpa.constants import MAX_MULTI_IMAGE_ACTION_RULES, TASK_TYPE_MULTI_IMAGE_ACTION
from fukua_rpa.multi_image_actions import (
    MULTI_IMAGE_ACTION_BY_CODE,
    normalize_multi_image_action_code,
    normalize_multi_image_rules,
)
from fukua_rpa.validation import TaskListValidator
from fukua_rpa.vision.core import build_scale_values
from fukua_rpa.workflow_security import sanitize_profile_for_sharing


class MultiImageWhitelistInvariantTests(unittest.TestCase):
    def test_parse_truncates_to_max_budget(self):
        rules = [
            {"image": f"img{i}.png", "action": "left_click"}
            for i in range(MAX_MULTI_IMAGE_ACTION_RULES + 10)
        ]
        parsed = normalize_multi_image_rules(rules)
        self.assertEqual(len(parsed), MAX_MULTI_IMAGE_ACTION_RULES)
        # truncation keeps the leading entries, in order
        self.assertEqual(parsed[0]["image"], "img0.png")
        self.assertEqual(parsed[-1]["image"], f"img{MAX_MULTI_IMAGE_ACTION_RULES - 1}.png")

    def test_validator_rejects_over_budget_rule_list(self):
        validator = TaskListValidator({})
        scale_values = build_scale_values(0.8, 1.2, 0.05)
        rules = [
            {"image": "x.png", "action": "left_click"}
        ] * (MAX_MULTI_IMAGE_ACTION_RULES + 5)
        task = {"type": TASK_TYPE_MULTI_IMAGE_ACTION, "multi_image_actions": rules}
        err = validator._validate_multi_image_action(task, 1, scale_values, True, 0.8)
        self.assertIsNotNone(err)
        self.assertIn(str(MAX_MULTI_IMAGE_ACTION_RULES), err)

    def test_unknown_action_rejected_by_validator(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            image_path = handle.name
        # A real, readable, textured PNG is required: the validator reads the
        # template image (after the whitelist check) and rejects pure-color
        # images, so an empty/solid file would mask the action-type rejection
        # we are asserting here.
        size = 16
        pixels = [
            ((x * 11) % 256, (y * 7) % 256, ((x + y) * 5) % 256)
            for y in range(size)
            for x in range(size)
        ]
        img = Image.new("RGB", (size, size))
        img.putdata(pixels)
        img.save(image_path)
        try:
            validator = TaskListValidator({})
            scale_values = build_scale_values(0.8, 1.2, 0.05)
            bad = {
                "type": TASK_TYPE_MULTI_IMAGE_ACTION,
                "multi_image_actions": [{"image": image_path, "action": "frobnicate"}],
            }
            err = validator._validate_multi_image_action(bad, 1, scale_values, True, 0.8)
            self.assertIsNotNone(err)
            self.assertIn("命中动作无效", err)

            good = {
                "type": TASK_TYPE_MULTI_IMAGE_ACTION,
                "multi_image_actions": [{"image": image_path, "action": "left_click"}],
            }
            self.assertIsNone(
                validator._validate_multi_image_action(good, 1, scale_values, True, 0.8)
            )
        finally:
            os.unlink(image_path)

    def test_whitelist_normalization_only_maps_known_codes(self):
        # A known code and a known label both normalize to the canonical code.
        self.assertEqual(normalize_multi_image_action_code("left_click"), "left_click")
        self.assertEqual(normalize_multi_image_action_code("匹配点左键单击"), "left_click")
        # Unknown actions are NOT silently converted into a valid whitelist code.
        unknown = normalize_multi_image_action_code("definitely_not_real")
        self.assertEqual(unknown, "definitely_not_real")
        self.assertNotIn(unknown, MULTI_IMAGE_ACTION_BY_CODE)

    def test_valid_rule_round_trips_through_parse_serialize_parse(self):
        rules = [
            {"image": "a.png", "action": "left_click", "value": "hi"},
            {"image": "b.png", "action": "text", "value": "x"},
        ]
        parsed = normalize_multi_image_rules(rules)
        # serialize exactly as the profile store would (JSON), then parse again
        serialized = json.loads(json.dumps(parsed, ensure_ascii=False))
        reparsed = normalize_multi_image_rules(serialized)
        self.assertEqual(reparsed, parsed)
        # path values are preserved on the normal serialization path
        self.assertEqual(reparsed[0]["image"], "a.png")
        self.assertEqual(reparsed[1]["action"], "text")

    def test_image_paths_desensitized_on_package_export(self):
        profile = {
            "tasks": [
                {
                    "type": TASK_TYPE_MULTI_IMAGE_ACTION,
                    "multi_image_actions": [
                        {"image": "C:/secret/path.png", "action": "left_click"}
                    ],
                }
            ]
        }
        result = sanitize_profile_for_sharing(profile)
        sanitized = result.profile
        task = sanitized["tasks"][0]
        # package rewrite strips the image path from the rule (desensitization)
        self.assertEqual(task.get("multi_image_actions"), [])
        # and the step is disabled so it cannot re-run with the redacted data
        self.assertTrue(task.get("step_disabled", False))
        self.assertGreaterEqual(result.removed_image_count, 1)


if __name__ == "__main__":
    unittest.main()
