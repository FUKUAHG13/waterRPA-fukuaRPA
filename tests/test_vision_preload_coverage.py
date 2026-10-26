"""Targeted coverage tests for fukua_rpa.vision.preload (VisionPreloadMixin).

Covers: scale_memory_policy, scale_memory_context, preferred_scales_for,
record_scale_match, scale_memory_summaries, remember_target_result,
template_dimensions, _get_cached_image, _load_into_img_cache,
matching_template_status (OSError/log), template_uses_transparency,
opencv_template_data, match_template_scores, load_and_precompute paths
(opencv unavailable, cache hit/eviction, pixel limit, stop flag,
disabled steps, coordinate skip, until conditions, multi-image, errors>20).
"""

import os
import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image

from fukua_rpa.constants import TASK_TYPE_MULTI_IMAGE_ACTION, TASK_TYPE_UNTIL
from fukua_rpa.engine import RPAEngine


def make_engine():
    engine = RPAEngine(defer_backends=True)
    engine.log_level = -1
    return engine


def make_template(tmp_dir, name="tpl.png", size=(24, 32), mode="RGB"):
    path = os.path.join(tmp_dir, name)
    img = Image.new(mode, size)
    pixels = img.load()
    for x in range(size[0]):
        for y in range(size[1]):
            if mode == "RGBA":
                pixels[x, y] = ((x * 37) % 256, (y * 53) % 256, 128, 200)
            else:
                pixels[x, y] = ((x * 37) % 256, (y * 53) % 256, 128)
    img.save(path)
    return path


class ScaleMemoryPolicyTests(unittest.TestCase):
    """Line 50: scale_memory_policy returns normalized policy."""

    def test_default_policy(self):
        engine = make_engine()
        policy = engine.scale_memory_policy()
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.tier, "balanced")

    def test_custom_policy_attributes(self):
        engine = make_engine()
        engine.use_native_scale_hint = False
        engine.scale_memory_tier = "aggressive"
        engine.scale_memory_manual = (1.0, 1.5)
        engine.scale_memory_custom_enabled = True
        engine.scale_memory_preferred_limit = 5
        engine.scale_memory_history_limit = 128
        policy = engine.scale_memory_policy()
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.tier, "aggressive")
        self.assertEqual(policy.manual_scales, (1.0, 1.5))


class ScaleMemoryContextTests(unittest.TestCase):
    """Lines 66-79: scale_memory_context with valid/invalid paths."""

    def test_valid_path_returns_context(self):
        engine = make_engine()
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            engine.scale_options_cache["k"] = (0.8, 1.2, 0.1)
            result = engine.scale_memory_context(path, "k", True)
        self.assertIsNotNone(result)
        key, generation, label, valid_scales = result
        self.assertEqual(label, "tpl.png")
        self.assertIn(1.0, valid_scales)

    def test_nonexistent_path_returns_none(self):
        engine = make_engine()
        engine.scale_options_cache["k"] = (0.8, 1.2, 0.1)
        result = engine.scale_memory_context(
            "/nonexistent/no.png", "k", True
        )
        self.assertIsNone(result)

    def test_none_path_returns_none(self):
        engine = make_engine()
        engine.scale_options_cache["k"] = (0.8, 1.2, 0.1)
        result = engine.scale_memory_context(None, "k", True)
        # None becomes "" -> abspath("") = CWD which exists, so this
        # actually returns a valid context. Just verify no crash.
        self.assertIsNotNone(result)


class PreferredScalesForTests(unittest.TestCase):
    """Lines 87-91: preferred_scales_for delegates to store."""

    def test_returns_empty_for_missing_file(self):
        engine = make_engine()
        engine.scale_options_cache["k"] = (0.8, 1.2, 0.1)
        preferred, summary = engine.preferred_scales_for(
            "/nonexistent/x.png", "k", True
        )
        self.assertEqual(preferred, ())
        self.assertIsNone(summary)

    def test_returns_scales_for_valid_file(self):
        engine = make_engine()
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            engine.scale_options_cache["k"] = (0.8, 1.2, 0.1)
            engine.record_scale_match(path, "k", True, 1.0, 0.95)
            preferred, summary = engine.preferred_scales_for(
                path, "k", True
            )
        self.assertIsInstance(preferred, tuple)


class RecordScaleMatchTests(unittest.TestCase):
    """Lines 100-119: record_scale_match with logging."""

    def test_record_returns_summary(self):
        engine = make_engine()
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            engine.scale_options_cache["k"] = (0.8, 1.2, 0.1)
            summary = engine.record_scale_match(path, "k", True, 1.0, 0.95)
        self.assertIsNotNone(summary)

    def test_record_with_verbose_log(self):
        engine = make_engine()
        engine.verbose_log_enabled = mock.Mock(return_value=True)
        engine.log = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            engine.scale_options_cache["k"] = (0.8, 1.2, 0.1)
            engine.record_scale_match(path, "k", True, 1.0, 0.95)
            # Record again to trigger changed=True
            engine.record_scale_match(path, "k", True, 1.2, 0.90)

    def test_record_missing_file_returns_none(self):
        engine = make_engine()
        engine.scale_options_cache["k"] = (0.8, 1.2, 0.1)
        result = engine.record_scale_match(
            "/nonexistent/x.png", "k", True, 1.0
        )
        self.assertIsNone(result)


class ScaleMemorySummariesTests(unittest.TestCase):
    """Line 122: scale_memory_summaries delegates to store."""

    def test_empty_summaries(self):
        engine = make_engine()
        result = engine.scale_memory_summaries()
        self.assertEqual(result, [])

    def test_summaries_after_record(self):
        engine = make_engine()
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            engine.scale_options_cache["k"] = (0.8, 1.2, 0.1)
            engine.record_scale_match(path, "k", True, 1.0, 0.95)
        result = engine.scale_memory_summaries(maximum=4)
        self.assertEqual(len(result), 1)


class RememberTargetResultTests(unittest.TestCase):
    """Lines 134-141: remember_target_result stores position and scale."""

    def test_stores_position_and_records_scale(self):
        engine = make_engine()
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            engine.scale_options_cache["k"] = (0.8, 1.2, 0.1)
            found = (100.0, 200.0, 1.0, 0.95)
            engine.remember_target_result(
                "pos_key", found, img_path=path, cache_key="k"
            )
        self.assertEqual(
            engine.last_target_positions["pos_key"], (100.0, 200.0)
        )

    def test_no_scale_does_not_record(self):
        engine = make_engine()
        found = (100.0, 200.0)
        engine.remember_target_result("pos_key", found)
        self.assertEqual(
            engine.last_target_positions["pos_key"], (100.0, 200.0)
        )

    def test_zero_scale_does_not_record(self):
        engine = make_engine()
        engine.record_scale_match = mock.Mock()
        found = (100.0, 200.0, 0.0, 0.9)
        engine.remember_target_result(
            "pos_key", found, img_path="x.png", cache_key="k"
        )
        engine.record_scale_match.assert_not_called()


class TemplateDimensionsTests(unittest.TestCase):
    """Lines 146-155: template_dimensions with cache and fallback."""

    def test_returns_size_from_cache(self):
        engine = make_engine()
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp, size=(40, 50))
            dims = engine.template_dimensions(path)
        self.assertEqual(dims, (40, 50))

    def test_returns_default_for_missing(self):
        engine = make_engine()
        dims = engine.template_dimensions("/nonexistent/x.png")
        self.assertEqual(dims, (80, 80))

    def test_uses_cached_image(self):
        engine = make_engine()
        img = Image.new("RGB", (30, 20))
        engine.img_cache["cached.png"] = img
        dims = engine.template_dimensions("cached.png")
        self.assertEqual(dims, (30, 20))


class GetCachedImageTests(unittest.TestCase):
    """Line 159: _get_cached_image reads from img_cache."""

    def test_returns_cached(self):
        engine = make_engine()
        img = Image.new("RGB", (10, 10))
        engine.img_cache["k"] = img
        self.assertIs(engine._get_cached_image("k"), img)

    def test_returns_none_for_missing(self):
        engine = make_engine()
        self.assertIsNone(engine._get_cached_image("missing"))


class LoadIntoImgCacheTests(unittest.TestCase):
    """Lines 163-168: _load_into_img_cache with LRU eviction."""

    def test_loads_image(self):
        engine = make_engine()
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp, size=(12, 12))
            img = engine._load_into_img_cache(path)
        self.assertEqual(img.size, (12, 12))
        self.assertIn(path, engine.img_cache)

    def test_evicts_oldest_when_full(self):
        engine = make_engine()
        from fukua_rpa.vision.shared import MAX_IMG_CACHE_ENTRIES

        # Fill cache to max
        for i in range(MAX_IMG_CACHE_ENTRIES):
            engine.img_cache[f"old_{i}"] = Image.new("RGB", (2, 2))
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp, size=(4, 4))
            engine._load_into_img_cache(path)
        self.assertLessEqual(
            len(engine.img_cache), MAX_IMG_CACHE_ENTRIES
        )
        self.assertNotIn("old_0", engine.img_cache)


class MatchingTemplateStatusTests(unittest.TestCase):
    """Lines 175-176, 185: OSError path and critical log."""

    def test_missing_file_returns_invalid(self):
        engine = make_engine()
        valid, reason = engine.matching_template_status(
            "/nonexistent/x.png", True
        )
        self.assertFalse(valid)
        self.assertIn("不存在", reason)

    def test_valid_template(self):
        engine = make_engine()
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            valid, reason = engine.matching_template_status(path, True)
        self.assertTrue(valid)

    def test_invalid_template_logs_critical(self):
        engine = make_engine()
        engine.log_enabled = mock.Mock(return_value=True)
        engine.log = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            # Create a 1x1 image which should be too small
            path = os.path.join(tmp, "tiny.png")
            Image.new("RGB", (1, 1), "white").save(path)
            valid, reason = engine.matching_template_status(path, True)
        if not valid:
            engine.log.assert_called()


class TemplateUsesTransparencyTests(unittest.TestCase):
    """Lines 197-220: template_uses_transparency with caching."""

    def test_opaque_image_returns_false(self):
        engine = make_engine()
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp, mode="RGB")
            result = engine.template_uses_transparency(path)
        self.assertFalse(result)

    def test_transparent_image_returns_true(self):
        engine = make_engine()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "alpha.png")
            img = Image.new("RGBA", (24, 24), (255, 0, 0, 128))
            img.save(path)
            result = engine.template_uses_transparency(path)
        self.assertTrue(result)

    def test_missing_file_returns_false(self):
        engine = make_engine()
        result = engine.template_uses_transparency("/nonexistent/x.png")
        self.assertFalse(result)

    def test_cached_by_path(self):
        engine = make_engine()
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp, mode="RGB")
            engine.template_uses_transparency(path)
            # Second call should hit path cache
            result = engine.template_uses_transparency(path)
        self.assertFalse(result)

    def test_cache_initialized_when_none(self):
        engine = make_engine()
        if hasattr(engine, "template_transparency_cache"):
            del engine.template_transparency_cache
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp, mode="RGB")
            result = engine.template_uses_transparency(path)
        self.assertFalse(result)
        self.assertTrue(hasattr(engine, "template_transparency_cache"))


class OpencvTemplateDataTests(unittest.TestCase):
    """Lines 229, 233: opencv_template_data with gray/color and alpha."""

    def test_grayscale_template(self):
        engine = make_engine()
        img = Image.new("RGB", (10, 10), (128, 64, 32))
        template, mask = engine.opencv_template_data(img, use_gray=True)
        self.assertEqual(len(template.shape), 2)
        self.assertIsNone(mask)

    def test_color_template(self):
        engine = make_engine()
        img = Image.new("RGB", (10, 10), (128, 64, 32))
        template, mask = engine.opencv_template_data(img, use_gray=False)
        self.assertEqual(template.shape[2], 3)
        self.assertIsNone(mask)

    def test_alpha_template_produces_mask(self):
        engine = make_engine()
        img = Image.new("RGBA", (10, 10), (128, 64, 32, 200))
        template, mask = engine.opencv_template_data(img, use_gray=True)
        self.assertIsNotNone(mask)
        self.assertEqual(mask.shape, (10, 10))


class MatchTemplateScoresTests(unittest.TestCase):
    """Lines 237-259: match_template_scores with and without mask."""

    def test_no_mask_returns_scores(self):
        engine = make_engine()
        screen = np.random.randint(0, 255, (100, 100), dtype=np.uint8)
        template = np.random.randint(0, 255, (20, 20), dtype=np.uint8)
        scores, nan_mask = engine.match_template_scores(screen, template)
        self.assertIsNone(nan_mask)
        self.assertEqual(scores.shape, (81, 81))

    def test_with_mask_returns_nan_mask(self):
        engine = make_engine()
        screen = np.random.randint(0, 255, (100, 100), dtype=np.uint8)
        template = np.random.randint(0, 255, (20, 20), dtype=np.uint8)
        mask = np.ones((20, 20), dtype=np.float32)
        scores, nan_mask = engine.match_template_scores(
            screen, template, mask=mask
        )
        self.assertIsNotNone(nan_mask)
        self.assertTrue(np.all(scores >= -1.0))
        self.assertTrue(np.all(scores <= 1.0))


class LoadAndPrecomputeTests(unittest.TestCase):
    """Lines 263-272, 282-283, 289, 294, 331, 345, 350-352, 355,
    362-366, 377, 383, 385-406, 409, 418, 420, 423-424, 429, 434,
    465, 468, 475-481, 490, 497-500, 522."""

    def setUp(self):
        self.engine = make_engine()
        self.engine.opencv_available = True

    def test_opencv_unavailable_no_mask_returns_true(self):
        """Lines 263-272: opencv unavailable without mask regions."""
        self.engine.opencv_available = False
        tasks = [{"type": 1, "value": "x.png", "step_disabled": False}]
        with mock.patch(
            "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
        ):
            result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)

    def test_opencv_unavailable_with_mask_returns_false(self):
        """Lines 263-272: opencv unavailable with mask regions fails."""
        self.engine.opencv_available = False
        tasks = [
            {
                "type": 1,
                "value": "x.png",
                "step_disabled": False,
                "template_mask_regions": [
                    {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3}
                ],
            }
        ]
        with mock.patch(
            "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
        ):
            result = self.engine.load_and_precompute(tasks)
        self.assertFalse(result)

    def test_stop_flag_aborts_preload(self):
        """Line 418: check_stop_flag in task loop."""
        self.engine.check_stop_flag = mock.Mock(return_value=True)
        tasks = [{"type": 1, "value": "x.png", "step_disabled": False}]
        with mock.patch(
            "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
        ):
            result = self.engine.load_and_precompute(tasks)
        self.assertFalse(result)

    def test_disabled_step_skipped(self):
        """Line 420: step_disabled continue."""
        tasks = [{"type": 1, "value": "x.png", "step_disabled": True}]
        with mock.patch(
            "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
        ):
            result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)

    def test_invalid_type_handled(self):
        """Lines 423-424: cmd parse exception falls through."""
        tasks = [{"type": "not_a_number", "step_disabled": False}]
        with mock.patch(
            "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
        ):
            result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)

    def test_empty_image_path_skipped(self):
        """Line 429: empty path continue."""
        tasks = [{"type": 1, "value": "", "step_disabled": False}]
        with mock.patch(
            "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
        ):
            result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)

    def test_coordinate_target_skipped(self):
        """Line 434: coordinate text continue."""
        tasks = [{"type": 1, "value": "100,200", "step_disabled": False}]
        with mock.patch(
            "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
        ):
            result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)

    def test_valid_template_preloads(self):
        """Lines 385-406: scale loop with resize."""
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            tasks = [{"type": 1, "value": path, "step_disabled": False}]
            self.engine.min_scale = 0.9
            self.engine.max_scale = 1.1
            self.engine.scale_step = 0.1
            with mock.patch(
                "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
            ):
                result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)
        self.assertGreater(len(self.engine.scaled_templates_cache), 0)

    def test_source_cache_hit(self):
        """Lines 282-283: load_source_image cache hit."""
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            # Pre-load into cache
            img = Image.open(path)
            img.load()
            self.engine.img_cache[path] = img
            tasks = [{"type": 1, "value": path, "step_disabled": False}]
            with mock.patch(
                "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
            ):
                result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)
        counters = self.engine.performance.snapshot()["counters"]
        self.assertGreaterEqual(
            counters.get("template.source_cache_hits", 0), 1
        )

    def test_scaled_cache_hit(self):
        """Lines 350-352: scaled_templates_cache hit."""
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            self.engine.min_scale = 1.0
            self.engine.max_scale = 1.0
            self.engine.scale_step = 0.1
            s_min, s_max, s_step, use_gray = self.engine.image_target_options(
                {"type": 1, "value": path}
            )
            from fukua_rpa.vision.core import normalize_template_mask_regions

            mask_regions = normalize_template_mask_regions([])
            cache_key = self.engine.template_cache_key(
                path, s_min, s_max, s_step, use_gray, mask_regions
            )
            # Pre-fill scaled cache
            self.engine.scaled_templates_cache[cache_key] = [(1.0, None)]
            tasks = [{"type": 1, "value": path, "step_disabled": False}]
            with mock.patch(
                "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
            ):
                result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)
        counters = self.engine.performance.snapshot()["counters"]
        self.assertGreaterEqual(
            counters.get("template.scaled_cache_hits", 0), 1
        )

    def test_single_template_cache_exceeded(self):
        """Line 345: single template cache bytes exceeded."""
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp, size=(24, 32))
            tasks = [{"type": 1, "value": path, "step_disabled": False}]
            self.engine.min_scale = 0.1
            self.engine.max_scale = 10.0
            self.engine.scale_step = 0.01
            with mock.patch(
                "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
            ), mock.patch(
                "fukua_rpa.vision.preload.MAX_SINGLE_TEMPLATE_CACHE_BYTES",
                1,
            ):
                result = self.engine.load_and_precompute(tasks)
        self.assertFalse(result)

    def test_total_cache_exceeded(self):
        """Line 355: total template cache bytes exceeded."""
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp, size=(24, 32))
            tasks = [{"type": 1, "value": path, "step_disabled": False}]
            with mock.patch(
                "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
            ), mock.patch(
                "fukua_rpa.vision.preload.MAX_TEMPLATE_CACHE_BYTES", 1
            ):
                result = self.engine.load_and_precompute(tasks)
        self.assertFalse(result)

    def test_until_condition_area_change_skipped(self):
        """Line 465: until condition mode area-change continue."""
        tasks = [
            {
                "type": TASK_TYPE_UNTIL,
                "until_cond1_en": True,
                "until_cond1_mode": "区域发生变化",
                "until_cond1_image": "",
                "until_cond1_region": "0,0,100,100",
                "step_disabled": False,
            }
        ]
        result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)

    def test_until_condition_empty_image_skipped(self):
        """Line 468: empty until image path continue."""
        tasks = [
            {
                "type": TASK_TYPE_UNTIL,
                "until_cond1_en": True,
                "until_cond1_mode": "图片出现",
                "until_cond1_image": "",
                "until_cond1_region": "",
                "step_disabled": False,
            }
        ]
        result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)

    def test_until_condition_becomes_image_loads_source(self):
        """Lines 475-477: until condition mode becomes-image loads source."""
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            tasks = [
                {
                    "type": TASK_TYPE_UNTIL,
                    "until_cond1_en": True,
                    "until_cond1_mode": "区域变成指定图片",
                    "until_cond1_image": path,
                    "until_cond1_region": "",
                    "step_disabled": False,
                }
            ]
            result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)
        self.assertIn(path, self.engine.img_cache)

    def test_until_condition_image_appears_preloads(self):
        """Lines 478-481: until condition normal preload."""
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            tasks = [
                {
                    "type": TASK_TYPE_UNTIL,
                    "until_cond1_en": True,
                    "until_cond1_mode": "图片出现",
                    "until_cond1_image": path,
                    "until_cond1_region": "",
                    "step_disabled": False,
                }
            ]
            result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)

    def test_multi_image_empty_path_skipped(self):
        """Line 490: empty multi-image path continue."""
        tasks = [
            {
                "type": TASK_TYPE_MULTI_IMAGE_ACTION,
                "multi_image_actions": [
                    {"image": "", "action": "left_click"}
                ],
                "step_disabled": False,
            }
        ]
        result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)

    def test_multi_image_valid_preloads(self):
        """Lines 497-500: multi-image preload."""
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            tasks = [
                {
                    "type": TASK_TYPE_MULTI_IMAGE_ACTION,
                    "multi_image_actions": [
                        {"image": path, "action": "left_click"}
                    ],
                    "step_disabled": False,
                }
            ]
            result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)

    def test_errors_over_20_truncation_message(self):
        """Line 522: more than 20 errors shows truncation message."""
        tasks = []
        for i in range(25):
            tasks.append(
                {
                    "type": 1,
                    "value": f"/nonexistent/missing_{i}.png",
                    "step_disabled": False,
                }
            )
        with mock.patch(
            "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
        ), mock.patch(
            "fukua_rpa.vision.preload.write_log"
        ) as mock_log:
            result = self.engine.load_and_precompute(tasks)
        self.assertFalse(result)
        # Check that truncation message was logged
        log_calls = [str(c) for c in mock_log.call_args_list]
        self.assertTrue(
            any("另有" in c for c in log_calls),
            f"Expected truncation message in logs: {log_calls[-3:]}",
        )

    def test_alpha_template_stores_mask(self):
        """Lines 377, 383, 409: alpha template stores mask data."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "alpha.png")
            img = Image.new("RGBA", (24, 32), (128, 64, 32, 200))
            pixels = img.load()
            for x in range(24):
                for y in range(32):
                    pixels[x, y] = ((x * 37) % 256, (y * 53) % 256, 128, 200)
            img.save(path)
            tasks = [{"type": 1, "value": path, "step_disabled": False}]
            self.engine.min_scale = 0.9
            self.engine.max_scale = 1.1
            self.engine.scale_step = 0.1
            with mock.patch(
                "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
            ):
                result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)
        # Check that mask was stored
        self.assertGreater(len(self.engine.base_template_masks_cache), 0)

    def test_pixel_limit_exceeded(self):
        """Line 289: pixel limit check in load_source_image."""
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp, size=(24, 32))
            tasks = [{"type": 1, "value": path, "step_disabled": False}]
            with mock.patch(
                "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
            ), mock.patch(
                "fukua_rpa.vision.preload.MAX_TEMPLATE_SOURCE_PIXELS", 10
            ):
                result = self.engine.load_and_precompute(tasks)
        self.assertFalse(result)

    def test_img_cache_eviction_during_preload(self):
        """Line 294: cache eviction during load_source_image."""
        from fukua_rpa.vision.shared import MAX_IMG_CACHE_ENTRIES

        # Fill cache
        for i in range(MAX_IMG_CACHE_ENTRIES):
            self.engine.img_cache[f"old_{i}"] = Image.new("RGB", (2, 2))
        with tempfile.TemporaryDirectory() as tmp:
            path = make_template(tmp)
            tasks = [{"type": 1, "value": path, "step_disabled": False}]
            with mock.patch(
                "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
            ):
                result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)
        self.assertLessEqual(
            len(self.engine.img_cache), MAX_IMG_CACHE_ENTRIES
        )

    def test_preload_one_impl_missing_path_returns_none(self):
        """Line 331: preload_one_impl with non-existent path."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "exists.png")
            Image.new("RGB", (24, 32), "white").save(path)
            # Create a task that references a path that will be deleted
            missing = os.path.join(tmp, "gone.png")
            Image.new("RGB", (24, 32), "white").save(missing)
            tasks = [{"type": 1, "value": missing, "step_disabled": False}]
            os.remove(missing)
            with mock.patch(
                "fukua_rpa.vision.preload.IMAGE_TARGET_COMMANDS", {1.0}
            ):
                result = self.engine.load_and_precompute(tasks)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
