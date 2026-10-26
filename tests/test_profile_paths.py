import os
import tempfile
import unittest

from fukua_rpa.profile_paths import (
    MANAGED_DATA_PREFIX,
    managed_path_reference,
    resolve_profile_path,
    resolve_task_image_paths,
)


class ManagedProfilePathTests(unittest.TestCase):
    def test_managed_and_external_paths_have_distinct_references(self):
        with tempfile.TemporaryDirectory() as base_dir, tempfile.TemporaryDirectory() as external:
            managed = os.path.join(base_dir, "templates", "target.png")
            outside = os.path.join(external, "target.png")
            os.makedirs(os.path.dirname(managed))

            reference = managed_path_reference(managed, base_dir)

            self.assertTrue(reference.startswith(MANAGED_DATA_PREFIX))
            self.assertEqual(resolve_profile_path(reference, base_dir), managed)
            self.assertEqual(
                managed_path_reference(outside, base_dir),
                os.path.abspath(outside),
            )

    def test_managed_reference_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as base_dir, self.assertRaisesRegex(ValueError, "不安全路径"):
            resolve_profile_path(
                MANAGED_DATA_PREFIX + "../outside.png", base_dir
            )

    def test_task_resolution_only_rewrites_image_semantics(self):
        with tempfile.TemporaryDirectory() as base_dir:
            image_reference = MANAGED_DATA_PREFIX + "templates/target.png"
            tasks = [
                {
                    "type": 1.0,
                    "value": image_reference,
                    "until_cond1_image": image_reference,
                },
                {"type": 10.0, "value": image_reference},
            ]

            resolved = resolve_task_image_paths(tasks, base_dir)

            expected = os.path.join(base_dir, "templates", "target.png")
            self.assertEqual(resolved[0]["value"], expected)
            self.assertEqual(resolved[0]["until_cond1_image"], expected)
            self.assertEqual(resolved[1]["value"], image_reference)
            self.assertEqual(tasks[0]["value"], image_reference)


if __name__ == "__main__":
    unittest.main()
