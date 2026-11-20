import unittest

from PIL import Image, ImageDraw

from fukua_rpa.vision.scene_wake import (
    SceneSignature,
    compare_scene_sets,
    compare_scene_signatures,
    make_scene_signature,
    normalize_sensitivity,
    scene_signature_source,
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


class SceneBackendSourceTests(unittest.TestCase):
    """VS-20261027-05：两条指纹生产者取整不同，切换后端不得读成"画面变了"。"""

    REGION_SIZE = (300, 768)

    @staticmethod
    def native_fingerprint_size(image, max_width=160, max_height=96):
        """复刻 vision/core.py 的原生取整：``int(round(w * scale))``。"""
        scale = min(
            1.0,
            max(8, max_width) / image.width,
            max(8, max_height) / image.height,
        )
        return (
            max(1, round(image.width * scale)),
            max(1, round(image.height * scale)),
        )

    def native_signature(self, image):
        width, height = self.native_fingerprint_size(image)
        return SceneSignature(width, height, bytes([255]) * (width * height), "native")

    def setUp(self):
        self.base = Image.new("RGB", self.REGION_SIZE, "white")

    def test_signature_records_producing_backend(self):
        self.assertEqual(
            make_scene_signature(self.base).source, "pil"
        )
        self.assertEqual(self.native_signature(self.base).source, "native")

    def test_two_backends_disagree_on_fingerprint_size(self):
        # 实测的尺寸分歧就是伪唤醒的根因；若取整口径被统一，本用例先失败并提示可删除守卫。
        pil = make_scene_signature(self.base)
        native = self.native_signature(self.base)
        self.assertNotEqual((pil.width, pil.height), (native.width, native.height))

    def test_backend_switch_is_not_reported_as_scene_change(self):
        result = compare_scene_signatures(
            self.native_signature(self.base), make_scene_signature(self.base)
        )
        self.assertFalse(result.changed)
        self.assertEqual(result.global_percent, 0.0)

    def test_backend_switch_direction_does_not_matter(self):
        result = compare_scene_signatures(
            make_scene_signature(self.base), self.native_signature(self.base)
        )
        self.assertFalse(result.changed)

    def test_same_backend_resize_is_still_a_change(self):
        # 副作用配对核对：守卫只屏蔽跨后端比较，同后端的真实改尺寸必须照旧触发。
        before = SceneSignature(40, 96, bytes([255]) * (40 * 96), "pil")
        after = SceneSignature(37, 96, bytes([255]) * (37 * 96), "pil")
        self.assertTrue(compare_scene_signatures(before, after).changed)

    def test_scene_set_with_mixed_backend_pair_is_not_a_wake(self):
        changed_frame = self.base.copy()
        ImageDraw.Draw(changed_frame).rectangle(
            (0, 0, 199, 199), fill="black"
        )
        result = compare_scene_sets(
            (self.native_signature(self.base),),
            (make_scene_signature(changed_frame),),
        )
        self.assertFalse(result.changed)

    def test_scene_signature_source_reports_single_or_empty(self):
        pil = make_scene_signature(self.base)
        native = self.native_signature(self.base)
        self.assertEqual(scene_signature_source((pil,)), "pil")
        self.assertEqual(scene_signature_source((pil, pil)), "pil")
        self.assertEqual(scene_signature_source((pil, native)), "")
        self.assertEqual(scene_signature_source(()), "")
        self.assertEqual(scene_signature_source(None), "")


if __name__ == "__main__":
    unittest.main()
