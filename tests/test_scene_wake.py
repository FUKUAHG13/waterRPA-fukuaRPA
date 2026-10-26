import unittest

from PIL import Image, ImageDraw

from fukua_rpa.vision.scene_wake import (
    compare_scene_sets,
    compare_scene_signatures,
    make_scene_signature,
    normalize_sensitivity,
)


class SceneWakeTests(unittest.TestCase):
    def setUp(self):
        self.base = Image.new("RGB", (1920, 1080), "white")

    def signature(self, image):
        return make_scene_signature(image)

    def test_identical_scene_does_not_wake(self):
        signature = self.signature(self.base)
        result = compare_scene_signatures(signature, signature)
        self.assertFalse(result.changed)
        self.assertEqual(result.global_percent, 0.0)

    def test_small_local_target_wakes_balanced_mode(self):
        changed = self.base.copy()
        ImageDraw.Draw(changed).rectangle((900, 470, 979, 549), fill="black")
        result = compare_scene_signatures(
            self.signature(self.base), self.signature(changed)
        )
        self.assertTrue(result.changed)
        self.assertGreater(result.peak_percent, result.global_percent)

    def test_tiny_change_can_be_sensitive_without_being_conservative(self):
        changed = self.base.copy()
        ImageDraw.Draw(changed).rectangle((950, 520, 969, 539), fill="black")
        before = self.signature(self.base)
        after = self.signature(changed)
        sensitive = compare_scene_signatures(
            before, after, sensitivity="sensitive"
        )
        conservative = compare_scene_signatures(
            before, after, sensitivity="conservative"
        )
        self.assertTrue(sensitive.changed)
        self.assertFalse(conservative.changed)

    def test_any_changed_region_wakes_scene_set(self):
        changed = self.base.copy()
        ImageDraw.Draw(changed).rectangle((0, 0, 199, 199), fill="black")
        result = compare_scene_sets(
            (self.signature(self.base), self.signature(self.base)),
            (self.signature(self.base), self.signature(changed)),
        )
        self.assertTrue(result.changed)

    def test_unknown_sensitivity_normalizes_to_balanced(self):
        self.assertEqual(normalize_sensitivity("unexpected"), "balanced")


if __name__ == "__main__":
    unittest.main()
