# pyright: reportAttributeAccessIssue=false, reportArgumentType=false
"""Coverage tests for ``fukua_rpa.engine.conditions.UntilConditionMixin``.

The mixin implements the "until condition" task: building condition descriptors
from flat task dicts and evaluating them (image appear/disappear, region change,
region stable, region-matches-image, overlay match). Evaluation depends on the
vision/screenshot layer, which is mocked here so the pure decision logic can be
exercised offscreen.
"""

import os
import sys
import types
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image

from fukua_rpa.engine import RPAEngine
from fukua_rpa.vision.shared import _CAPTURE_PROVENANCE, capture_returns_physical_pixels


def make_engine():
    """Create an offscreen RPAEngine with backends deferred and logging muted."""
    with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
        engine = RPAEngine(defer_backends=True)
    engine.callback_msg = mock.Mock()
    engine.log_level = 1
    return engine


def solid(color, size=(40, 40)):
    """Return a solid-color RGB PIL image."""
    return Image.new("RGB", size, color)


def half_image(top_color, bottom_color, size=(40, 40)):
    """Return an image whose top half is ``top_color`` and bottom half ``bottom_color``."""
    width, height = size
    img = Image.new("RGB", size, top_color)
    for y in range(height // 2, height):
        for x in range(width):
            img.putpixel((x, y), bottom_color)
    return img


def make_fake_cv2(score=0.99):
    """Build a stand-in ``cv2`` module with a deterministic match score.

    The real OpenCV wheel is unusable under the test interpreter (its bootstrap
    looks for a missing ``config-3.14.py``), so overlay-match tests inject this
    fake to exercise the decision logic deterministically.
    """
    fake = mock.MagicMock()
    fake.COLOR_RGB2BGR = 4
    fake.TM_CCOEFF_NORMED = 5
    fake.cvtColor.side_effect = lambda arr, _code: arr
    fake.matchTemplate.return_value = object()
    fake.minMaxLoc.return_value = (0.0, float(score), (0, 0), (0, 0))
    return fake


def make_fake_numpy():
    """Build a minimal ``numpy`` stand-in.

    ``evaluate_overlay_match_condition`` only calls ``np.array(...)`` and feeds
    the result to the (fake) cv2 routines, so an identity ``array`` suffices.
    Injecting a fake avoids importing the real numpy, which can reach a broken
    partially-loaded state mid-session under the test interpreter.
    """
    fake = types.ModuleType("numpy")
    fake.array = lambda obj, *args, **kwargs: obj
    return fake


def patch_cv2(score=0.99):
    """Patch ``sys.modules`` so ``import cv2``/``import numpy`` resolve to fakes."""
    return mock.patch.dict(
        sys.modules, {"cv2": make_fake_cv2(score), "numpy": make_fake_numpy()}
    )


class ConditionScreenshotTests(unittest.TestCase):
    """condition_screenshot uses a per-cycle capture cache."""

    def setUp(self):
        self.engine = make_engine()

    def test_no_cache_calls_capture_every_time(self):
        self.engine._until_capture_cache = None
        img = solid((1, 2, 3))
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(img, 0, 0)
        ) as capture:
            first = self.engine.condition_screenshot((0, 0, 10, 10))
            second = self.engine.condition_screenshot((0, 0, 10, 10))
        self.assertIs(first[0], img)
        self.assertIs(second[0], img)
        self.assertEqual(capture.call_count, 2)

    def test_cache_hit_and_miss(self):
        self.engine._until_capture_cache = {}
        img = solid((9, 9, 9))
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(img, 5, 6)
        ) as capture:
            first = self.engine.condition_screenshot((0, 0, 10, 10))
            second = self.engine.condition_screenshot((0, 0, 10, 10))
            third = self.engine.condition_screenshot((1, 1, 10, 10))
        self.assertEqual(capture.call_count, 2)
        self.assertIs(first[0], img)
        self.assertIs(second[0], img)
        self.assertEqual(first[1:], (5, 6))
        self.assertIn((0, 0, 10, 10), self.engine._until_capture_cache)
        self.assertIn((1, 1, 10, 10), self.engine._until_capture_cache)
        self.assertIsNotNone(third)

    def test_cache_hit_restores_the_captured_provenance(self):
        """REVIEW-20260913-001 FR-RISK-005：缓存命中时必须回写坐标空间来源。

        ``condition_screenshot`` 的周期缓存在命中时不重新截图，但
        ``capture_returns_physical_pixels()`` 记录的是**本线程最近一次截图**的来源。
        若命中路径不回写，紧随其后的识别函数会读到另一区域的来源，对已处输入空间的
        命中坐标再做一次 physical→input 虚拟化，在 System-DPI 回退路径上产生系统性偏移。
        """
        engine = make_engine()
        engine._until_capture_cache = {}
        img = solid((1, 2, 3))
        region_a = (0, 0, 10, 10)
        region_b = (20, 20, 10, 10)

        def fake_capture(region):
            # 区域 A 走 mss（虚拟化坐标 → False）；区域 B 走原生 GDI 截图（物理像素 → True）。
            _CAPTURE_PROVENANCE.physical = tuple(region) == tuple(region_b)
            return (img, 0, 0)

        with mock.patch.object(engine, "capture_screenshot", side_effect=fake_capture):
            engine.condition_screenshot(region_a)
            engine.condition_screenshot(region_b)
            engine.condition_screenshot(region_a)  # 缓存命中，不重新截图

        self.assertFalse(
            capture_returns_physical_pixels(),
            "缓存命中后标记必须描述被返回图像的真实来源（区域 A 为虚拟化坐标）",
        )


class ConditionHelperTests(unittest.TestCase):
    """Small parsing/clamping helpers around condition dicts."""

    def setUp(self):
        self.engine = make_engine()

    def test_until_conditions_from_task(self):
        task = {
            "until_cond1_en": True,
            "until_cond1_mode": "图片出现",
            "until_cond2_en": False,
            "until_cond3_en": False,
        }
        conditions = self.engine.until_conditions_from_task(task)
        self.assertEqual(len(conditions), 1)
        self.assertEqual(conditions[0]["mode"], "图片出现")
        self.assertEqual(conditions[0]["index"], 1)

    def test_until_conditions_from_task_none_uses_defaults(self):
        # ``None`` is coerced to ``{}``; condition slot 1 defaults to enabled.
        conditions = self.engine.until_conditions_from_task(None)
        self.assertEqual(len(conditions), 1)
        self.assertEqual(conditions[0]["index"], 1)
        self.assertEqual(conditions[0]["mode"], "图片出现")

    def test_until_task_state_key(self):
        self.assertEqual(self.engine.until_task_state_key({"step": 3}), 3)
        self.assertEqual(self.engine.until_task_state_key(None), 0)
        self.assertEqual(self.engine.until_task_state_key({}), 0)

    def test_condition_cache_key(self):
        key = self.engine.condition_cache_key("img.png", 2, 0.8, True)
        self.assertIn("until_2_img.png", key)
        self.assertIn("True", key)

    def test_condition_sub_region_rejects_non_finite_segments(self):
        """REVIEW-20260913-001 FR-002: 子区域含 "inf"/"1e999" 时 int(float(...))
        抛 OverflowError；该异常此前未捕获，会逃逸出本方法 docstring 声明的
        "invalid → None" 契约并让条件评估崩溃。必须返回 None（调用方回退全区域）。"""
        self.assertEqual(self.engine.condition_sub_region({"sub_region": "0,0,10,10"}), (0, 0, 10, 10))
        self.assertIsNone(self.engine.condition_sub_region({"sub_region": "inf,0,10,10"}))
        self.assertIsNone(self.engine.condition_sub_region({"sub_region": "1e999,0,10,10"}))
        self.assertIsNone(self.engine.condition_sub_region({"sub_region": "nan,0,10,10"}))
        self.assertIsNone(self.engine.condition_sub_region({"sub_region": "0,0,0,10"}))
        self.assertIsNone(self.engine.condition_sub_region({"sub_region": "0,0,10"}))
        self.assertIsNone(self.engine.condition_sub_region({"sub_region": ""}))

    def test_condition_region(self):
        self.assertEqual(self.engine.condition_region({"region": "1,2,3,4"}), (1, 2, 3, 4))
        self.assertIsNone(self.engine.condition_region({"region": ""}))
        self.assertIsNone(self.engine.condition_region({}))

    def test_condition_sub_region_valid_string(self):
        self.assertEqual(
            self.engine.condition_sub_region({"sub_region": "1,2,3,4"}), (1, 2, 3, 4)
        )

    def test_condition_sub_region_fullwidth_comma(self):
        self.assertEqual(
            self.engine.condition_sub_region({"sub_region": "1，2，3，4"}), (1, 2, 3, 4)
        )

    def test_condition_sub_region_list_input(self):
        self.assertEqual(
            self.engine.condition_sub_region({"sub_region": [1, 2, 3, 4]}), (1, 2, 3, 4)
        )

    def test_condition_sub_region_empty(self):
        self.assertIsNone(self.engine.condition_sub_region({"sub_region": ""}))
        self.assertIsNone(self.engine.condition_sub_region({}))

    def test_condition_sub_region_wrong_length(self):
        self.assertIsNone(self.engine.condition_sub_region({"sub_region": "1,2,3"}))

    def test_condition_sub_region_invalid_number(self):
        self.assertIsNone(self.engine.condition_sub_region({"sub_region": "a,b,c,d"}))

    def test_condition_sub_region_non_positive_size(self):
        self.assertIsNone(self.engine.condition_sub_region({"sub_region": "1,2,0,4"}))
        self.assertIsNone(self.engine.condition_sub_region({"sub_region": "1,2,3,0"}))

    def test_crop_to_sub_region_none_returns_original(self):
        img = solid((0, 0, 0))
        self.assertIs(self.engine.crop_to_sub_region(img, None), img)

    def test_crop_to_sub_region_valid(self):
        img = solid((0, 0, 0), size=(40, 40))
        cropped = self.engine.crop_to_sub_region(img, (5, 5, 10, 10))
        self.assertIsNotNone(cropped)
        self.assertEqual(cropped.size, (10, 10))

    def test_crop_to_sub_region_clamped_to_bounds(self):
        img = solid((0, 0, 0), size=(20, 20))
        cropped = self.engine.crop_to_sub_region(img, (10, 10, 50, 50))
        self.assertIsNotNone(cropped)
        self.assertEqual(cropped.size, (10, 10))

    def test_crop_to_sub_region_out_of_range(self):
        img = solid((0, 0, 0), size=(20, 20))
        self.assertIsNone(self.engine.crop_to_sub_region(img, (30, 30, 5, 5)))

    def test_condition_confidence_clamped(self):
        self.assertAlmostEqual(self.engine.condition_confidence({"conf": "0.9"}), 0.9)
        self.assertAlmostEqual(self.engine.condition_confidence({"conf": "5"}), 1.0)
        self.assertAlmostEqual(self.engine.condition_confidence({"conf": "-1"}), 0.05)
        self.assertAlmostEqual(self.engine.condition_confidence({}), 0.8)

    def test_condition_diff_threshold_clamped(self):
        self.assertAlmostEqual(self.engine.condition_diff_threshold({"diff": "20"}), 20.0)
        self.assertAlmostEqual(self.engine.condition_diff_threshold({"diff": "200"}), 100.0)
        self.assertAlmostEqual(self.engine.condition_diff_threshold({"diff": "-5"}), 0.0)
        self.assertAlmostEqual(self.engine.condition_diff_threshold({}), 8.0)

    def test_condition_similarity_threshold_clamped(self):
        self.assertAlmostEqual(
            self.engine.condition_similarity_threshold({"similarity": "70"}), 70.0
        )
        self.assertAlmostEqual(
            self.engine.condition_similarity_threshold({"similarity": "200"}), 100.0
        )
        self.assertAlmostEqual(
            self.engine.condition_similarity_threshold({"similarity": "-1"}), 0.0
        )
        self.assertAlmostEqual(self.engine.condition_similarity_threshold({}), 90.0)


class ImageCompareTests(unittest.TestCase):
    """resized_for_compare / difference / similarity pixel math."""

    def setUp(self):
        self.engine = make_engine()

    def test_resized_explicit_target(self):
        img = solid((0, 0, 0), size=(100, 100))
        resized = self.engine.resized_for_compare(img, target_size=(30, 20))
        self.assertEqual(resized.size, (30, 20))
        self.assertEqual(resized.mode, "RGB")

    def test_resized_caps_longest_side(self):
        img = solid((0, 0, 0), size=(520, 260))
        resized = self.engine.resized_for_compare(img, max_side=260)
        self.assertEqual(resized.size, (260, 130))

    def test_resized_small_image_unchanged(self):
        img = solid((0, 0, 0), size=(100, 50))
        resized = self.engine.resized_for_compare(img, max_side=260)
        self.assertEqual(resized.size, (100, 50))

    def test_difference_identical_is_zero(self):
        a = solid((10, 20, 30))
        b = solid((10, 20, 30))
        self.assertAlmostEqual(self.engine.image_difference_percent(a, b), 0.0, places=3)

    def test_difference_maximal(self):
        a = solid((0, 0, 0))
        b = solid((255, 255, 255))
        self.assertAlmostEqual(self.engine.image_difference_percent(a, b), 100.0, places=3)

    def test_similarity_identical_is_100(self):
        current = solid((50, 60, 70))
        template = solid((50, 60, 70))
        self.assertAlmostEqual(
            self.engine.image_similarity_percent(current, template), 100.0, places=3
        )

    def test_similarity_opaque_different_is_low(self):
        current = solid((0, 0, 0))
        template = solid((255, 255, 255))
        self.assertAlmostEqual(
            self.engine.image_similarity_percent(current, template), 0.0, places=3
        )

    def test_similarity_large_template_is_downscaled(self):
        current = solid((5, 5, 5), size=(300, 300))
        template = solid((5, 5, 5), size=(300, 300))
        self.assertAlmostEqual(
            self.engine.image_similarity_percent(current, template), 100.0, places=3
        )

    def test_similarity_fully_transparent_alpha_is_zero(self):
        current = solid((10, 10, 10))
        template = Image.new("RGBA", (40, 40), (10, 10, 10, 0))
        self.assertEqual(self.engine.image_similarity_percent(current, template), 0.0)

    def test_similarity_partial_alpha_uses_weighted_branch(self):
        current = solid((100, 100, 100))
        template = Image.new("RGBA", (40, 40), (100, 100, 100, 128))
        similarity = self.engine.image_similarity_percent(current, template)
        self.assertAlmostEqual(similarity, 100.0, places=3)


class FindConditionImageTests(unittest.TestCase):
    """find_condition_image: template lookup on screen or within a region."""

    def setUp(self):
        self.engine = make_engine()

    def test_missing_path_returns_error(self):
        found, detail, is_error = self.engine.find_condition_image(
            {"image": ""}, {"step": 1}, True
        )
        self.assertFalse(found)
        self.assertTrue(is_error)
        self.assertEqual(detail, "图片不存在")

    def test_nonexistent_file_returns_error(self):
        with mock.patch("os.path.exists", return_value=False):
            found, _detail, is_error = self.engine.find_condition_image(
                {"image": "nope.png"}, {"step": 1}, True
            )
        self.assertFalse(found)
        self.assertTrue(is_error)

    def test_full_screen_found(self):
        with mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "find_target_optimized", return_value=(123.7, 45.2)
        ):
            found, detail, is_error = self.engine.find_condition_image(
                {"image": "tpl.png", "index": 1}, {"step": 1}, True
            )
        self.assertTrue(found)
        self.assertFalse(is_error)
        self.assertIn("(123, 45)", detail)
        self.assertTrue(any(k.startswith("until_1_tpl.png") for k in self.engine.scale_options_cache))

    def test_full_screen_not_found(self):
        with mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "find_target_optimized", return_value=None
        ):
            found, detail, is_error = self.engine.find_condition_image(
                {"image": "tpl.png", "index": 1}, {"step": 1}, True
            )
        self.assertFalse(found)
        self.assertFalse(is_error)
        self.assertEqual(detail, "未找到指定图片")

    def test_region_found(self):
        screenshot = solid((0, 0, 0))
        with mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot", return_value=(screenshot, 10, 20)
        ), mock.patch.object(
            self.engine, "find_target_in_screenshot", return_value=(5.0, 6.0)
        ):
            found, detail, is_error = self.engine.find_condition_image(
                {"image": "tpl.png", "region": "0,0,40,40", "index": 1}, {"step": 1}, False
            )
        self.assertTrue(found)
        self.assertFalse(is_error)
        self.assertIn("(5, 6)", detail)

    def test_region_exception_returns_error(self):
        with mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot", side_effect=RuntimeError("boom")
        ):
            found, detail, is_error = self.engine.find_condition_image(
                {"image": "tpl.png", "region": "0,0,40,40"}, {"step": 1}, False
            )
        self.assertFalse(found)
        self.assertTrue(is_error)
        self.assertIn("区域识别异常", detail)


class RegionChangedConditionTests(unittest.TestCase):
    """evaluate_region_changed_condition: baseline + frame difference."""

    def setUp(self):
        self.engine = make_engine()
        self.step = {"step": 1}

    def test_no_region(self):
        matched, detail = self.engine.evaluate_region_changed_condition({}, self.step)
        self.assertFalse(matched)
        self.assertEqual(detail, "未设置区域")

    def test_screenshot_exception(self):
        cond = {"region": "0,0,40,40"}
        with mock.patch.object(
            self.engine, "condition_screenshot", side_effect=RuntimeError("cap")
        ):
            matched, detail = self.engine.evaluate_region_changed_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("截图异常", detail)

    def test_sub_region_out_of_range(self):
        cond = {"region": "0,0,40,40", "sub_region": "100,100,5,5"}
        with mock.patch.object(
            self.engine, "condition_screenshot", return_value=(solid((0, 0, 0)), 0, 0)
        ):
            matched, detail = self.engine.evaluate_region_changed_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertEqual(detail, "子区域超出范围")

    def test_baseline_recorded_then_change_detected(self):
        cond = {"region": "0,0,40,40", "diff": "8"}
        black = solid((0, 0, 0))
        white = solid((255, 255, 255))
        with mock.patch.object(
            self.engine, "condition_screenshot", return_value=(black, 0, 0)
        ):
            matched, detail = self.engine.evaluate_region_changed_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("已记录区域基准", detail)

        with mock.patch.object(
            self.engine, "condition_screenshot", return_value=(white, 0, 0)
        ):
            matched, detail = self.engine.evaluate_region_changed_condition(cond, self.step)
        self.assertTrue(matched)
        self.assertIn("区域变化", detail)

    def test_no_change_below_threshold(self):
        cond = {"region": "0,0,40,40", "diff": "8"}
        black = solid((0, 0, 0))
        with mock.patch.object(
            self.engine, "condition_screenshot", return_value=(black, 0, 0)
        ):
            self.engine.evaluate_region_changed_condition(cond, self.step)
            matched, detail = self.engine.evaluate_region_changed_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("区域变化", detail)

    def test_sub_region_change_detected(self):
        cond = {"region": "0,0,40,40", "sub_region": "0,0,40,20", "diff": "8"}
        # Top half changes between frames; bottom half stays constant.
        frame_a = half_image((0, 0, 0), (255, 255, 255))
        frame_b = half_image((255, 255, 255), (255, 255, 255))
        with mock.patch.object(
            self.engine, "condition_screenshot", return_value=(frame_a, 0, 0)
        ):
            self.engine.evaluate_region_changed_condition(cond, self.step)
        with mock.patch.object(
            self.engine, "condition_screenshot", return_value=(frame_b, 0, 0)
        ):
            matched, detail = self.engine.evaluate_region_changed_condition(cond, self.step)
        self.assertTrue(matched)
        self.assertIn("子区域变化", detail)


class RegionStableConditionTests(unittest.TestCase):
    """evaluate_region_stable_condition: frame-to-frame stability + duration."""

    def setUp(self):
        self.engine = make_engine()
        self.step = {"step": 7}

    def test_no_region(self):
        matched, detail = self.engine.evaluate_region_stable_condition({}, self.step)
        self.assertFalse(matched)
        self.assertEqual(detail, "未设置区域")

    def test_capture_exception(self):
        cond = {"region": "0,0,40,40"}
        with mock.patch.object(
            self.engine, "capture_screenshot", side_effect=RuntimeError("cap")
        ):
            matched, detail = self.engine.evaluate_region_stable_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("截图异常", detail)

    def test_sub_region_out_of_range(self):
        cond = {"region": "0,0,40,40", "sub_region": "100,100,5,5"}
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(solid((0, 0, 0)), 0, 0)
        ):
            matched, detail = self.engine.evaluate_region_stable_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertEqual(detail, "子区域超出范围")

    def test_first_frame_recorded(self):
        cond = {"region": "0,0,40,40"}
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(solid((0, 0, 0)), 0, 0)
        ):
            matched, detail = self.engine.evaluate_region_stable_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("已记录首帧", detail)

    def test_stable_when_identical_frames(self):
        cond = {"region": "0,0,40,40", "diff": "8"}
        black = solid((0, 0, 0))
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(black, 0, 0)
        ):
            self.engine.evaluate_region_stable_condition(cond, self.step)
            matched, detail = self.engine.evaluate_region_stable_condition(cond, self.step)
        self.assertTrue(matched)
        self.assertIn("区域不变", detail)

    def test_unstable_when_frames_differ(self):
        cond = {"region": "0,0,40,40", "diff": "8"}
        black = solid((0, 0, 0))
        white = solid((255, 255, 255))
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(black, 0, 0)
        ):
            self.engine.evaluate_region_stable_condition(cond, self.step)
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(white, 0, 0)
        ):
            matched, detail = self.engine.evaluate_region_stable_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("区域不变", detail)

    def test_legacy_previous_format_is_handled(self):
        cond = {"region": "0,0,40,40", "diff": "8"}
        black = solid((0, 0, 0))
        # Seed a legacy (non-tuple) previous frame directly.
        key = (7, 0, "stable", "0,0,40,40", "")
        self.engine.until_condition_previous[key] = black.copy()
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(black, 0, 0)
        ):
            matched, _detail = self.engine.evaluate_region_stable_condition(cond, self.step)
        self.assertTrue(matched)

    def test_invalid_stable_seconds_falls_back_to_zero(self):
        cond = {"region": "0,0,40,40", "diff": "8", "stable_seconds": "abc"}
        black = solid((0, 0, 0))
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(black, 0, 0)
        ):
            self.engine.evaluate_region_stable_condition(cond, self.step)
            matched, _detail = self.engine.evaluate_region_stable_condition(cond, self.step)
        self.assertTrue(matched)

    def test_non_finite_stable_seconds_falls_back_to_zero(self):
        """REVIEW-20260913-001 FR-002: inf/nan/1e999 此前能通过裸 float() 解析，
        使 elapsed >= stable_seconds 恒为假、条件永不成立（静默失效）。它们必须
        与 "abc" 一样回退 0，即在第 2 帧就判定成立。"""
        black = solid((0, 0, 0))
        for raw in ("inf", "-inf", "nan", "1e999", "Infinity"):
            with self.subTest(stable_seconds=raw):
                engine = make_engine()
                cond = {"region": "0,0,40,40", "diff": "8", "stable_seconds": raw}
                with mock.patch.object(
                    engine, "capture_screenshot", return_value=(black, 0, 0)
                ):
                    engine.evaluate_region_stable_condition(cond, self.step)
                    matched, detail = engine.evaluate_region_stable_condition(
                        cond, self.step
                    )
                self.assertTrue(
                    matched,
                    f"stable_seconds={raw!r} 应回退 0 并在第 2 帧成立，实际 detail={detail}",
                )

    def test_duration_mode_reset_start_and_satisfy(self):
        cond = {"region": "0,0,40,40", "diff": "8", "stable_seconds": "1.0"}
        black = solid((0, 0, 0))
        white = solid((255, 255, 255))

        # Frame 1: record first frame.
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(black, 0, 0)
        ):
            matched, detail = self.engine.evaluate_region_stable_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("已记录首帧", detail)

        # Frame 2: large change -> timer reset.
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(white, 0, 0)
        ):
            matched, detail = self.engine.evaluate_region_stable_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("计时重置", detail)

        # Frame 3: stable again, start timing (perf_counter -> 100.0).
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(white, 0, 0)
        ), mock.patch(
            "fukua_rpa.engine.conditions.time.perf_counter", return_value=100.0
        ):
            matched, detail = self.engine.evaluate_region_stable_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("开始计时", detail)

        # Frame 4: still stable but not long enough (100.5 - 100.0 < 1.0).
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(white, 0, 0)
        ), mock.patch(
            "fukua_rpa.engine.conditions.time.perf_counter", return_value=100.5
        ):
            matched, detail = self.engine.evaluate_region_stable_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("已稳定", detail)

        # Frame 5: stable long enough (102.0 - 100.0 >= 1.0) -> satisfied.
        with mock.patch.object(
            self.engine, "capture_screenshot", return_value=(white, 0, 0)
        ), mock.patch(
            "fukua_rpa.engine.conditions.time.perf_counter", return_value=102.0
        ):
            matched, detail = self.engine.evaluate_region_stable_condition(cond, self.step)
        self.assertTrue(matched)
        self.assertIn("已稳定", detail)


class RegionMatchesImageConditionTests(unittest.TestCase):
    """evaluate_region_matches_image_condition: similarity vs reference image."""

    def setUp(self):
        self.engine = make_engine()
        self.step = {"step": 1}

    def test_no_region(self):
        matched, detail = self.engine.evaluate_region_matches_image_condition({}, self.step)
        self.assertFalse(matched)
        self.assertEqual(detail, "未设置区域")

    def test_missing_image(self):
        cond = {"region": "0,0,40,40", "image": ""}
        matched, detail = self.engine.evaluate_region_matches_image_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertEqual(detail, "图片不存在")

    def test_nonexistent_image_file(self):
        cond = {"region": "0,0,40,40", "image": "nope.png"}
        with mock.patch("os.path.exists", return_value=False):
            matched, detail = self.engine.evaluate_region_matches_image_condition(
                cond, self.step
            )
        self.assertFalse(matched)
        self.assertEqual(detail, "图片不存在")

    def test_similarity_above_threshold_matches(self):
        cond = {"region": "0,0,40,40", "image": "ref.png", "similarity": "90"}
        screenshot = solid((30, 40, 50))
        template = solid((30, 40, 50))
        with mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot", return_value=(screenshot, 0, 0)
        ), mock.patch.object(
            self.engine, "_get_cached_image", return_value=template
        ):
            matched, detail = self.engine.evaluate_region_matches_image_condition(
                cond, self.step
            )
        self.assertTrue(matched)
        self.assertIn("区域相似", detail)

    def test_similarity_below_threshold_does_not_match(self):
        cond = {"region": "0,0,40,40", "image": "ref.png", "similarity": "90"}
        screenshot = solid((0, 0, 0))
        template = solid((255, 255, 255))
        with mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot", return_value=(screenshot, 0, 0)
        ), mock.patch.object(
            self.engine, "_get_cached_image", return_value=template
        ):
            matched, detail = self.engine.evaluate_region_matches_image_condition(
                cond, self.step
            )
        self.assertFalse(matched)
        self.assertIn("区域相似", detail)

    def test_cache_miss_loads_template(self):
        cond = {"region": "0,0,40,40", "image": "ref.png", "similarity": "90"}
        screenshot = solid((30, 40, 50))
        template = solid((30, 40, 50))
        with mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot", return_value=(screenshot, 0, 0)
        ), mock.patch.object(
            self.engine, "_get_cached_image", return_value=None
        ), mock.patch.object(
            self.engine, "_load_into_img_cache", return_value=template
        ) as loader:
            matched, _detail = self.engine.evaluate_region_matches_image_condition(
                cond, self.step
            )
        self.assertTrue(matched)
        loader.assert_called_once_with("ref.png")

    def test_compare_exception_returns_error(self):
        cond = {"region": "0,0,40,40", "image": "ref.png"}
        with mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot", side_effect=RuntimeError("cap")
        ):
            matched, detail = self.engine.evaluate_region_matches_image_condition(
                cond, self.step
            )
        self.assertFalse(matched)
        self.assertIn("区域对比异常", detail)


class OverlayMatchConditionTests(unittest.TestCase):
    """evaluate_overlay_match_condition: template match at a sub-position."""

    def setUp(self):
        self.engine = make_engine()
        self.step = {"step": 1}
        # 60x60 white screenshot with a 20x20 black square at (10,10).
        self.screenshot = Image.new("RGB", (60, 60), (255, 255, 255))
        for y in range(10, 30):
            for x in range(10, 30):
                self.screenshot.putpixel((x, y), (0, 0, 0))
        self.template = Image.new("RGB", (20, 20), (0, 0, 0))

    def _cond(self, **overrides):
        cond = {
            "region": "0,0,60,60",
            "until_overlay_image": "overlay.png",
            "until_overlay_rect": "10,10,20,20",
            "until_overlay_scale": "1.0",
            "until_overlay_threshold": "0.8",
        }
        cond.update(overrides)
        return cond

    def test_no_region(self):
        matched, detail = self.engine.evaluate_overlay_match_condition({}, self.step)
        self.assertFalse(matched)
        self.assertEqual(detail, "未设置大区域")

    def test_missing_overlay_image(self):
        cond = self._cond(**{"until_overlay_image": ""})
        matched, detail = self.engine.evaluate_overlay_match_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertEqual(detail, "覆盖小图不存在")

    def test_nonexistent_overlay_file(self):
        cond = self._cond()
        with mock.patch("os.path.exists", return_value=False):
            matched, detail = self.engine.evaluate_overlay_match_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertEqual(detail, "覆盖小图不存在")

    def test_no_overlay_rect(self):
        cond = self._cond(**{"until_overlay_rect": ""})
        with mock.patch("os.path.exists", return_value=True):
            matched, detail = self.engine.evaluate_overlay_match_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertEqual(detail, "未设置子区域位置")

    def test_screenshot_exception(self):
        cond = self._cond()
        with mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot", side_effect=RuntimeError("cap")
        ):
            matched, detail = self.engine.evaluate_overlay_match_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("截图异常", detail)

    def test_opencv_unavailable(self):
        cond = self._cond()
        # Setting a sys.modules entry to None makes its import raise ImportError.
        with mock.patch.dict(sys.modules, {"cv2": None, "numpy": None}), mock.patch(
            "os.path.exists", return_value=True
        ), mock.patch.object(
            self.engine, "condition_screenshot",
            return_value=(self.screenshot, 0, 0),
        ):
            matched, detail = self.engine.evaluate_overlay_match_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertEqual(detail, "OpenCV 不可用")

    def test_sub_region_out_of_bounds(self):
        cond = self._cond(**{"until_overlay_rect": "50,50,20,20"})
        with patch_cv2(), mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot",
            return_value=(self.screenshot, 0, 0),
        ):
            matched, detail = self.engine.evaluate_overlay_match_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("子区域越界", detail)

    def test_template_load_failure(self):
        cond = self._cond()
        with patch_cv2(), mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot",
            return_value=(self.screenshot, 0, 0),
        ), mock.patch.object(
            self.engine, "_get_cached_image", return_value=None
        ), mock.patch.object(
            self.engine, "_load_into_img_cache", return_value=None
        ):
            matched, detail = self.engine.evaluate_overlay_match_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertEqual(detail, "无法加载覆盖小图")

    def test_template_larger_than_sub_region(self):
        cond = self._cond()
        big_template = Image.new("RGB", (30, 30), (0, 0, 0))
        with patch_cv2(), mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot",
            return_value=(self.screenshot, 0, 0),
        ), mock.patch.object(
            self.engine, "_get_cached_image", return_value=big_template
        ):
            matched, detail = self.engine.evaluate_overlay_match_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("大于子区域", detail)

    def test_match_succeeds_above_threshold(self):
        cond = self._cond()
        with patch_cv2(score=0.99), mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot",
            return_value=(self.screenshot, 0, 0),
        ), mock.patch.object(
            self.engine, "_get_cached_image", return_value=self.template
        ):
            matched, detail = self.engine.evaluate_overlay_match_condition(cond, self.step)
        self.assertTrue(matched)
        self.assertIn("覆盖匹配分数", detail)

    def test_match_with_scale_and_cache_miss(self):
        cond = self._cond(**{"until_overlay_scale": "0.5"})
        with patch_cv2(score=0.95), mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot",
            return_value=(self.screenshot, 0, 0),
        ), mock.patch.object(
            self.engine, "_get_cached_image", return_value=None
        ), mock.patch.object(
            self.engine, "_load_into_img_cache", return_value=self.template
        ) as loader:
            matched, detail = self.engine.evaluate_overlay_match_condition(cond, self.step)
        self.assertTrue(matched)
        self.assertIn("覆盖匹配分数", detail)
        loader.assert_called_once_with("overlay.png")

    def test_match_below_threshold(self):
        cond = self._cond()
        with patch_cv2(score=0.10), mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot",
            return_value=(self.screenshot, 0, 0),
        ), mock.patch.object(
            self.engine, "_get_cached_image", return_value=self.template
        ):
            matched, detail = self.engine.evaluate_overlay_match_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("覆盖匹配分数", detail)

    def test_match_exception_path(self):
        cond = self._cond()
        with patch_cv2(), mock.patch("os.path.exists", return_value=True), mock.patch.object(
            self.engine, "condition_screenshot",
            return_value=(self.screenshot, 0, 0),
        ), mock.patch.object(
            self.engine, "_get_cached_image",
            side_effect=RuntimeError("decode"),
        ):
            matched, detail = self.engine.evaluate_overlay_match_condition(cond, self.step)
        self.assertFalse(matched)
        self.assertIn("覆盖匹配异常", detail)


class DispatcherTests(unittest.TestCase):
    """_evaluate_until_condition_impl dispatches by mode."""

    def setUp(self):
        self.engine = make_engine()
        self.step = {"step": 1}

    def test_image_appear_mode(self):
        cond = {"mode": "图片出现"}
        with mock.patch.object(
            self.engine, "find_condition_image", return_value=(True, "ok", False)
        ):
            matched, detail = self.engine._evaluate_until_condition_impl(cond, self.step, True)
        self.assertTrue(matched)
        self.assertEqual(detail, "ok")

    def test_image_disappear_when_found(self):
        cond = {"mode": "图片消失"}
        with mock.patch.object(
            self.engine, "find_condition_image", return_value=(True, "still there", False)
        ):
            matched, detail = self.engine._evaluate_until_condition_impl(cond, self.step, True)
        self.assertFalse(matched)
        self.assertEqual(detail, "still there")

    def test_image_disappear_when_gone(self):
        cond = {"mode": "图片消失"}
        with mock.patch.object(
            self.engine, "find_condition_image", return_value=(False, "not found", False)
        ):
            matched, detail = self.engine._evaluate_until_condition_impl(cond, self.step, True)
        self.assertTrue(matched)
        self.assertEqual(detail, "图片已消失")

    def test_image_disappear_error_is_not_satisfied(self):
        cond = {"mode": "图片消失"}
        with mock.patch.object(
            self.engine, "find_condition_image", return_value=(False, "图片不存在", True)
        ):
            matched, detail = self.engine._evaluate_until_condition_impl(cond, self.step, True)
        self.assertFalse(matched)
        self.assertEqual(detail, "图片不存在")

    def test_region_changed_mode(self):
        cond = {"mode": "区域发生变化"}
        with mock.patch.object(
            self.engine, "evaluate_region_changed_condition", return_value=(True, "changed")
        ):
            matched, detail = self.engine._evaluate_until_condition_impl(cond, self.step, True)
        self.assertTrue(matched)
        self.assertEqual(detail, "changed")

    def test_region_stable_mode(self):
        cond = {"mode": "区域不变"}
        with mock.patch.object(
            self.engine, "evaluate_region_stable_condition", return_value=(True, "stable")
        ):
            matched, detail = self.engine._evaluate_until_condition_impl(cond, self.step, True)
        self.assertTrue(matched)
        self.assertEqual(detail, "stable")

    def test_region_matches_image_mode(self):
        cond = {"mode": "区域变成指定图片"}
        with mock.patch.object(
            self.engine, "evaluate_region_matches_image_condition",
            return_value=(True, "matched"),
        ):
            matched, detail = self.engine._evaluate_until_condition_impl(cond, self.step, True)
        self.assertTrue(matched)
        self.assertEqual(detail, "matched")

    def test_overlay_mode(self):
        cond = {"mode": "覆盖匹配"}
        with mock.patch.object(
            self.engine, "evaluate_overlay_match_condition", return_value=(True, "overlay")
        ):
            matched, detail = self.engine._evaluate_until_condition_impl(cond, self.step, True)
        self.assertTrue(matched)
        self.assertEqual(detail, "overlay")

    def test_unknown_mode(self):
        cond = {"mode": "不存在的模式"}
        matched, detail = self.engine._evaluate_until_condition_impl(cond, self.step, True)
        self.assertFalse(matched)
        self.assertEqual(detail, "未知条件类型")

    def test_default_mode_is_image_appear(self):
        cond = {}
        with mock.patch.object(
            self.engine, "find_condition_image", return_value=(False, "nope", False)
        ):
            matched, detail = self.engine._evaluate_until_condition_impl(cond, self.step, True)
        self.assertFalse(matched)
        self.assertEqual(detail, "nope")


class EvaluateUntilConditionWrapperTests(unittest.TestCase):
    """evaluate_until_condition adds performance instrumentation."""

    def test_wrapper_records_metrics(self):
        engine = make_engine()
        with mock.patch.object(
            engine, "_evaluate_until_condition_impl", return_value=(True, "done")
        ):
            matched, detail = engine.evaluate_until_condition({"mode": "图片出现"}, {"step": 1}, True)
        self.assertTrue(matched)
        self.assertEqual(detail, "done")
        snapshot = engine.performance.snapshot()
        counters = snapshot.get("counters", {})
        self.assertGreaterEqual(counters.get("condition.evaluations", 0), 1)


class ExecuteUntilConditionsTests(unittest.TestCase):
    """execute_until_conditions aggregates per-condition results."""

    def setUp(self):
        self.engine = make_engine()
        self.step = {"step": 1}

    def _task(self, logic="全部满足", **extra):
        task = {
            "until_logic": logic,
            "until_cond1_en": True,
            "until_cond1_mode": "图片出现",
            "until_cond2_en": True,
            "until_cond2_mode": "图片出现",
            "until_cond3_en": False,
        }
        task.update(extra)
        return task

    def test_no_conditions_returns_error(self):
        task = {"until_cond1_en": False, "until_cond2_en": False, "until_cond3_en": False}
        with mock.patch.object(self.engine, "log_enabled", return_value=True), mock.patch.object(
            self.engine, "log"
        ) as log_mock:
            result = self.engine.execute_until_conditions(task, self.step, True)
        self.assertEqual(result, "error")
        self.assertTrue(log_mock.called)

    def test_all_logic_short_circuits_on_first_false(self):
        task = self._task(logic="全部满足")
        with mock.patch.object(
            self.engine, "evaluate_until_condition", return_value=(False, "no")
        ) as evaluator, mock.patch.object(
            self.engine, "check_stop_flag", return_value=False
        ):
            result = self.engine.execute_until_conditions(task, self.step, True)
        self.assertEqual(result, "condition_false")
        self.assertEqual(evaluator.call_count, 1)

    def test_all_logic_all_true(self):
        task = self._task(logic="全部满足")
        with mock.patch.object(
            self.engine, "evaluate_until_condition", return_value=(True, "yes")
        ), mock.patch.object(
            self.engine, "check_stop_flag", return_value=False
        ):
            result = self.engine.execute_until_conditions(task, self.step, True)
        self.assertEqual(result, "condition_true")

    def test_any_logic_short_circuits_on_first_true(self):
        task = self._task(logic="任一满足")
        with mock.patch.object(
            self.engine, "evaluate_until_condition", return_value=(True, "yes")
        ) as evaluator, mock.patch.object(
            self.engine, "check_stop_flag", return_value=False
        ):
            result = self.engine.execute_until_conditions(task, self.step, True)
        self.assertEqual(result, "condition_true")
        self.assertEqual(evaluator.call_count, 1)

    def test_any_logic_all_false(self):
        task = self._task(logic="任一满足")
        with mock.patch.object(
            self.engine, "evaluate_until_condition", return_value=(False, "no")
        ), mock.patch.object(
            self.engine, "check_stop_flag", return_value=False
        ):
            result = self.engine.execute_until_conditions(task, self.step, True)
        self.assertEqual(result, "condition_false")

    def test_stop_flag_returns_stopped(self):
        task = self._task()
        with mock.patch.object(
            self.engine, "check_stop_flag", return_value=True
        ):
            result = self.engine.execute_until_conditions(task, self.step, True)
        self.assertEqual(result, "stopped")

    def test_capture_cache_restored_after_run(self):
        task = self._task()
        self.engine._until_capture_cache = None
        with mock.patch.object(
            self.engine, "evaluate_until_condition", return_value=(True, "yes")
        ), mock.patch.object(
            self.engine, "check_stop_flag", return_value=False
        ):
            self.engine.execute_until_conditions(task, self.step, True)
        self.assertIsNone(self.engine._until_capture_cache)


class UntilRuntimeTests(unittest.TestCase):
    """until_false_runtime / reset_until_runtime tracking."""

    def setUp(self):
        self.engine = make_engine()
        self.step = {"step": 4}

    def test_counts_increment_and_no_limit(self):
        task = {"until_max_checks": "0", "until_max_seconds": "0"}
        reached, reason, count, _elapsed = self.engine.until_false_runtime(task, self.step)
        self.assertFalse(reached)
        self.assertEqual(reason, "")
        self.assertEqual(count, 1)
        reached, _reason, count, _elapsed = self.engine.until_false_runtime(task, self.step)
        self.assertEqual(count, 2)
        self.assertFalse(reached)

    def test_max_checks_reached(self):
        task = {"until_max_checks": "2", "until_max_seconds": "0"}
        self.engine.until_false_runtime(task, self.step)
        reached, reason, count, _elapsed = self.engine.until_false_runtime(task, self.step)
        self.assertTrue(reached)
        self.assertEqual(count, 2)
        self.assertIn("2/2", reason)

    def test_max_seconds_reached(self):
        task = {"until_max_checks": "0", "until_max_seconds": "5"}
        key = self.engine.until_task_state_key(self.step)
        self.engine.until_condition_started_at[key] = 0.0
        with mock.patch("fukua_rpa.engine.conditions.time.monotonic", return_value=1000.0):
            reached, reason, _count, elapsed = self.engine.until_false_runtime(task, self.step)
        self.assertTrue(reached)
        self.assertGreaterEqual(elapsed, 5.0)
        self.assertIn("秒", reason)

    def test_invalid_limits_treated_as_zero(self):
        task = {"until_max_checks": "abc", "until_max_seconds": "xyz"}
        reached, reason, count, _elapsed = self.engine.until_false_runtime(task, self.step)
        self.assertFalse(reached)
        self.assertEqual(reason, "")
        self.assertEqual(count, 1)

    def test_both_limits_reached_joins_reasons(self):
        """FR-20260812 建议-体验-1: 检查次数与等待时长两个上限同时到达时，
        reason 必须拼接两条说明，不得后写覆盖前写。"""
        task = {"until_max_checks": "1", "until_max_seconds": "5"}
        key = self.engine.until_task_state_key(self.step)
        self.engine.until_condition_started_at[key] = 0.0
        with mock.patch("fukua_rpa.engine.conditions.time.monotonic", return_value=1000.0):
            reached, reason, _count, _elapsed = self.engine.until_false_runtime(
                task, self.step
            )
        self.assertTrue(reached)
        self.assertIn("1/1", reason)
        self.assertIn("5.0", reason)
        self.assertIn("；", reason)

    def test_reset_clears_all_state_for_step(self):
        task = {"until_max_checks": "0", "until_max_seconds": "0"}
        self.engine.until_false_runtime(task, self.step)
        key = self.engine.until_task_state_key(self.step)
        self.engine.until_condition_baselines[(key, 1, "changed", "r", "")] = object()
        self.engine.until_condition_baselines[(999, 1, "changed", "r", "")] = object()
        self.engine.until_condition_previous[(key, 1, "stable", "r", "")] = object()
        self.engine.until_condition_previous[(999, 1, "stable", "r", "")] = object()

        self.engine.reset_until_runtime(self.step)

        self.assertNotIn(key, self.engine.until_condition_counts)
        self.assertNotIn(key, self.engine.until_condition_started_at)
        self.assertNotIn((key, 1, "changed", "r", ""), self.engine.until_condition_baselines)
        self.assertIn((999, 1, "changed", "r", ""), self.engine.until_condition_baselines)
        self.assertNotIn((key, 1, "stable", "r", ""), self.engine.until_condition_previous)
        self.assertIn((999, 1, "stable", "r", ""), self.engine.until_condition_previous)


if __name__ == "__main__":
    unittest.main()
