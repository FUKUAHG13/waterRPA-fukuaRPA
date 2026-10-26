import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from unittest import mock

import numpy as np
from PIL import Image

from fukua_rpa.constants import CONFIDENCE_MAX, CONFIDENCE_MIN
from fukua_rpa.engine import RPAEngine


def make_engine():
    """Create a minimal RPAEngine without initializing real backends."""
    engine = RPAEngine(defer_backends=True)
    engine.log_level = -1
    return engine


class MatchTemplateScoresNanTests(unittest.TestCase):
    """match_template_scores NaN mask exclusion and inverted-path safety."""

    def setUp(self):
        self.engine = make_engine()

    def test_no_mask_returns_none_nan_mask(self):
        """Without a mask, nan_mask is None (no NaN sanitization needed)."""
        np.random.seed(20260811)  # FR-20260809-1404: 固定种子保证可复现
        screen = np.random.randint(0, 255, (64, 64), dtype=np.uint8)
        template = np.random.randint(0, 255, (16, 16), dtype=np.uint8)
        result, nan_mask = self.engine.match_template_scores(screen, template)
        self.assertIsNone(nan_mask)
        self.assertEqual(result.shape, (49, 49))

    def test_nan_mask_marks_nan_positions(self):
        """nan_mask correctly identifies NaN/Inf positions in masked result."""
        raw = np.array([[0.5, np.nan, 0.8], [np.inf, -0.3, 0.9]], dtype=np.float64)
        with mock.patch("cv2.matchTemplate", return_value=raw.copy()):
            screen = np.zeros((10, 10), dtype=np.uint8)
            template = np.zeros((5, 5), dtype=np.uint8)
            mask = np.ones((5, 5), dtype=np.float32)
            result, nan_mask = self.engine.match_template_scores(
                screen, template, mask
            )

        self.assertIsNotNone(nan_mask)
        # NaN at (0,1), Inf at (1,0)
        self.assertTrue(nan_mask[0, 1])
        self.assertTrue(nan_mask[1, 0])
        self.assertFalse(nan_mask[0, 0])
        self.assertFalse(nan_mask[0, 2])
        self.assertFalse(nan_mask[1, 1])
        self.assertFalse(nan_mask[1, 2])

    def test_nan_replaced_by_sentinel_not_false_peak(self):
        """After nan_to_num, NaN positions become -1.0, not a false peak."""
        raw = np.array([[0.5, np.nan, 0.8], [0.2, -0.3, np.nan]], dtype=np.float64)
        with mock.patch("cv2.matchTemplate", return_value=raw.copy()):
            screen = np.zeros((10, 10), dtype=np.uint8)
            template = np.zeros((5, 5), dtype=np.uint8)
            mask = np.ones((5, 5), dtype=np.float32)
            result, nan_mask = self.engine.match_template_scores(
                screen, template, mask
            )

        # NaN positions must be -1.0 sentinel
        self.assertAlmostEqual(float(result[0, 1]), -1.0)
        self.assertAlmostEqual(float(result[1, 2]), -1.0)
        # Valid positions retain their values
        self.assertAlmostEqual(float(result[0, 0]), 0.5)
        self.assertAlmostEqual(float(result[0, 2]), 0.8)

    def test_inverted_path_excludes_nan_positions(self):
        """In inverted matching, NaN sentinel positions are set to -1.0.

        Without exclusion, -(-1.0) = 1.0 would produce a false perfect score.
        This replicates the logic in _find_target_in_screenshot_impl.
        """
        raw = np.array(
            [[0.3, np.nan, 0.4], [np.nan, 0.2, 0.5]], dtype=np.float64
        )
        with mock.patch("cv2.matchTemplate", return_value=raw.copy()):
            screen = np.zeros((10, 10), dtype=np.uint8)
            template = np.zeros((5, 5), dtype=np.uint8)
            mask = np.ones((5, 5), dtype=np.float32)
            result, nan_mask = self.engine.match_template_scores(
                screen, template, mask
            )

        # Simulate the inverted path from _find_target_in_screenshot_impl
        inverted_res = -result
        if nan_mask is not None and nan_mask.any():
            inverted_res = inverted_res.copy()
            inverted_res[nan_mask] = -1.0

        # NaN positions must NOT become 1.0 (false perfect score)
        self.assertAlmostEqual(float(inverted_res[0, 1]), -1.0)
        self.assertAlmostEqual(float(inverted_res[1, 0]), -1.0)
        # Valid positions are properly inverted
        self.assertAlmostEqual(float(inverted_res[0, 0]), -0.3)
        self.assertAlmostEqual(float(inverted_res[1, 2]), -0.5)
        # Maximum of inverted result should be a legitimate value, not 1.0
        self.assertLess(float(inverted_res.max()), 0.0)

    def test_result_clipped_to_valid_range(self):
        """Result values are clipped to [-1.0, 1.0]."""
        raw = np.array([[1.5, -2.0, 0.7]], dtype=np.float64)
        with mock.patch("cv2.matchTemplate", return_value=raw.copy()):
            screen = np.zeros((10, 10), dtype=np.uint8)
            template = np.zeros((5, 5), dtype=np.uint8)
            mask = np.ones((5, 5), dtype=np.float32)
            result, _nan_mask = self.engine.match_template_scores(
                screen, template, mask
            )

        self.assertAlmostEqual(float(result[0, 0]), 1.0)
        self.assertAlmostEqual(float(result[0, 1]), -1.0)
        self.assertAlmostEqual(float(result[0, 2]), 0.7)


class LoadAndPrecomputeBoundaryTests(unittest.TestCase):
    """load_and_precompute boundary conditions."""

    def setUp(self):
        self.engine = make_engine()
        self.engine.opencv_available = True

    def test_empty_tasks_returns_true(self):
        """Empty task list succeeds trivially."""
        self.assertTrue(self.engine.load_and_precompute([]))

    def test_nonexistent_template_raises(self):
        """A task pointing to a missing image must fail loudly (P2-1).

        Previously a missing template was silently skipped, which left the
        recognition step waiting forever. The engine now surfaces the failure
        by returning False (with an error log) instead of skipping it.
        """
        tasks = [
            {
                "type": 1,
                "value": "/nonexistent/path/template.png",
                "step_disabled": False,
            }
        ]
        # IMAGE_TARGET_COMMANDS includes 1 (click image)
        with mock.patch(
            "fukua_rpa.engine.vision_exec.IMAGE_TARGET_COMMANDS", {1.0}
        ):
            self.assertFalse(self.engine.load_and_precompute(tasks))

    def test_disabled_step_skipped(self):
        """Disabled steps are skipped entirely."""
        tasks = [
            {
                "type": 1,
                "value": "/nonexistent/path/template.png",
                "step_disabled": True,
            }
        ]
        result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)

    def test_valid_template_precomputed_with_correct_dimensions(self):
        """A valid template is loaded and cached with correct dimensions."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tpl.png")
            # Non-uniform image to pass template_detail_status stddev check
            img = Image.new("RGB", (24, 32))
            pixels = img.load()
            for x in range(24):
                for y in range(32):
                    pixels[x, y] = ((x * 37) % 256, (y * 53) % 256, 128)
            img.save(path)

            tasks = [
                {
                    "type": 1,
                    "value": path,
                    "step_disabled": False,
                }
            ]
            with mock.patch(
                "fukua_rpa.engine.vision_exec.IMAGE_TARGET_COMMANDS", {1.0}
            ):
                result = self.engine.load_and_precompute(tasks)

            self.assertTrue(result)
            # Template should be in img_cache with correct size
            self.assertIn(path, self.engine.img_cache)
            self.assertEqual(self.engine.img_cache[path].size, (24, 32))

    def test_opencv_unavailable_no_mask_returns_true(self):
        """Without OpenCV, tasks without mask regions still succeed."""
        self.engine.opencv_available = False
        tasks = [
            {
                "type": 1,
                "value": "/some/path.png",
                "step_disabled": False,
            }
        ]
        result = self.engine.load_and_precompute(tasks)
        self.assertTrue(result)

    def test_opencv_unavailable_with_mask_returns_false(self):
        """Without OpenCV, mask-region tasks fail with a clear message."""
        self.engine.opencv_available = False
        tasks = [
            {
                "type": 1,
                "value": "/some/path.png",
                "step_disabled": False,
                "template_mask_regions": [[0, 0, 10, 10]],
            }
        ]
        result = self.engine.load_and_precompute(tasks)
        self.assertFalse(result)


class DedupeTargetsNMSTests(unittest.TestCase):
    """Multi-target deduplication (non-maximum suppression) logic."""

    def setUp(self):
        self.engine = make_engine()

    def test_empty_matches_returns_empty(self):
        """Empty input returns empty output."""
        self.assertEqual(self.engine._dedupe_targets([]), [])

    def test_non_overlapping_candidates_all_kept(self):
        """Candidates far apart are all retained."""
        matches = [
            {"x": 10.0, "y": 10.0, "score": 0.9, "radius": 5.0, "scale": 1.0},
            {"x": 200.0, "y": 200.0, "score": 0.85, "radius": 5.0, "scale": 1.0},
            {"x": 400.0, "y": 10.0, "score": 0.8, "radius": 5.0, "scale": 1.0},
        ]
        result = self.engine._dedupe_targets(matches)
        self.assertEqual(len(result), 3)

    def test_overlapping_candidates_keep_highest_score(self):
        """Overlapping candidates: only the highest-score one survives."""
        matches = [
            {"x": 100.0, "y": 100.0, "score": 0.7, "radius": 10.0, "scale": 1.0},
            {"x": 103.0, "y": 102.0, "score": 0.95, "radius": 10.0, "scale": 1.0},
            {"x": 101.0, "y": 101.0, "score": 0.8, "radius": 10.0, "scale": 1.0},
        ]
        result = self.engine._dedupe_targets(matches)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["score"], 0.95)
        self.assertAlmostEqual(result[0]["x"], 103.0)

    def test_nms_suppresses_within_radius(self):
        """Candidates within max(radius) distance are suppressed."""
        # Two candidates 4 pixels apart, radius 10 → suppressed
        # Third candidate 50 pixels away → kept
        matches = [
            {"x": 50.0, "y": 50.0, "score": 0.9, "radius": 10.0, "scale": 1.0},
            {"x": 54.0, "y": 50.0, "score": 0.85, "radius": 10.0, "scale": 1.0},
            {"x": 150.0, "y": 50.0, "score": 0.88, "radius": 10.0, "scale": 1.0},
        ]
        result = self.engine._dedupe_targets(matches)
        self.assertEqual(len(result), 2)
        scores = sorted([m["score"] for m in result], reverse=True)
        self.assertAlmostEqual(scores[0], 0.9)
        self.assertAlmostEqual(scores[1], 0.88)

    def test_dedupe_preserves_score_order(self):
        """Output is sorted by descending score (highest first)."""
        matches = [
            {"x": 10.0, "y": 10.0, "score": 0.6, "radius": 4.0, "scale": 1.0},
            {"x": 300.0, "y": 300.0, "score": 0.95, "radius": 4.0, "scale": 1.0},
            {"x": 500.0, "y": 10.0, "score": 0.75, "radius": 4.0, "scale": 1.0},
        ]
        result = self.engine._dedupe_targets(matches)
        self.assertEqual(len(result), 3)
        self.assertAlmostEqual(result[0]["score"], 0.95)
        self.assertAlmostEqual(result[1]["score"], 0.75)
        self.assertAlmostEqual(result[2]["score"], 0.6)


class ConfidenceThresholdTests(unittest.TestCase):
    """Confidence threshold boundary and filtering behavior."""

    def setUp(self):
        self.engine = make_engine()

    def test_confidence_constants(self):
        """CONFIDENCE_MIN and CONFIDENCE_MAX have expected values."""
        self.assertAlmostEqual(CONFIDENCE_MIN, 0.05)
        self.assertAlmostEqual(CONFIDENCE_MAX, 1.0)

    def test_recognize_task_rejects_below_min(self):
        """recognize_task_once rejects confidence below CONFIDENCE_MIN."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tpl.png")
            Image.new("RGB", (16, 16), "red").save(path)
            task = {
                "type": 1,
                "value": path,
                "custom_en": True,
                "custom_conf": 0.01,  # below CONFIDENCE_MIN
            }
            with mock.patch(
                "fukua_rpa.engine.vision_exec.IMAGE_TARGET_COMMANDS", {1.0}
            ), mock.patch(
                "fukua_rpa.engine.vision_exec.CLICK_COMMANDS", {1.0}
            ):
                with self.assertRaises(ValueError) as ctx:
                    self.engine.recognize_task_once(task)
                self.assertIn("0.05", str(ctx.exception))

    def test_recognize_task_rejects_above_max(self):
        """recognize_task_once rejects confidence above CONFIDENCE_MAX."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tpl.png")
            Image.new("RGB", (16, 16), "blue").save(path)
            task = {
                "type": 1,
                "value": path,
                "custom_en": True,
                "custom_conf": 1.5,  # above CONFIDENCE_MAX
            }
            with mock.patch(
                "fukua_rpa.engine.vision_exec.IMAGE_TARGET_COMMANDS", {1.0}
            ), mock.patch(
                "fukua_rpa.engine.vision_exec.CLICK_COMMANDS", {1.0}
            ):
                with self.assertRaises(ValueError) as ctx:
                    self.engine.recognize_task_once(task)
                self.assertIn("1", str(ctx.exception))

    def test_collect_template_matches_filters_below_threshold(self):
        """_collect_template_matches only returns candidates >= task_conf."""

        # Create a screen with a bright square and a template that matches it
        screen = np.zeros((64, 64), dtype=np.uint8)
        screen[20:36, 20:36] = 255
        template = np.zeros((16, 16), dtype=np.uint8)
        template[:, :] = 255

        # With a very high threshold, no match should pass
        matches_high = self.engine._collect_template_matches(
            screen, template, 0.99, 0, 0, 1.0
        )
        # With a low threshold, at least one match should pass
        matches_low = self.engine._collect_template_matches(
            screen, template, 0.3, 0, 0, 1.0
        )
        # High threshold should yield fewer (or equal) matches than low
        self.assertLessEqual(len(matches_high), len(matches_low))
        # All returned matches must meet the threshold
        for match in matches_low:
            self.assertGreaterEqual(match["score"], 0.3)

    def test_find_target_returns_none_below_threshold(self):
        """_find_target_in_screenshot_impl returns None when score < conf."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tpl.png")
            # Template: solid white
            Image.new("RGB", (16, 16), "white").save(path)
            # Screenshot: solid black (no match possible)
            screenshot = Image.new("RGB", (64, 64), "black")

            self.engine.img_cache[path] = Image.open(path).copy()
            self.engine.opencv_available = True
            cache_key = "test_key"
            self.engine.scale_options_cache[str(cache_key)] = (1.0, 1.0, 0.05)

            result = self.engine._find_target_in_screenshot_impl(
                path, cache_key, 0.95, True, screenshot, 0, 0
            )
            self.assertIsNone(result)


class CollectTemplateMatchesInvertedNanTests(unittest.TestCase):
    """_collect_template_matches inverted path NaN exclusion."""

    def setUp(self):
        self.engine = make_engine()

    def test_inverted_nan_positions_excluded_from_candidates(self):
        """NaN sentinel positions do not produce inverted candidates."""
        # Construct a result matrix where NaN positions would become 1.0
        # if not properly excluded in the inverted path
        res = np.full((10, 10), 0.2, dtype=np.float32)
        res[3, 3] = np.nan  # would become 1.0 if naively inverted
        res[7, 7] = 0.6  # legitimate peak

        nan_mask = np.isnan(res)
        res_clean = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)

        with mock.patch.object(
            self.engine,
            "match_template_scores",
            return_value=(res_clean, nan_mask),
        ):
            template = np.zeros((4, 4), dtype=np.uint8)
            matches = self.engine._collect_template_matches(
                np.zeros((20, 20), dtype=np.uint8),
                template,
                0.5,
                0,
                0,
                1.0,
                allow_inverted=True,
            )

        # No candidate should be at the NaN position (3,3) with score ~1.0
        for match in matches:
            # Center offset is w//2=2, h//2=2, so NaN at (3,3) → center (5,5)
            if abs(match["x"] - 5.0) < 1 and abs(match["y"] - 5.0) < 1:
                self.assertLess(
                    match["score"], 0.9,
                    "NaN position must not produce a false high-score match",
                )


class MultiTargetFallbackErrorTests(unittest.TestCase):
    """VIS-FR-001：多目标 OpenCV 回退的异常分类必须与单目标路径一致。

    单目标主路径仅捕 (OSError, ValueError, TypeError)（match.py 窄元组），
    其余异常向上传播为步骤错误；多目标回退不得把内部异常吞成权威“未命中”。
    """

    def setUp(self):
        self.engine = make_engine()

    def _probe(self, error):
        engine = self.engine
        engine.native_find_targets = mock.Mock(return_value=None)
        engine.iter_search_screenshots = mock.Mock(return_value=[(object(), 0, 0)])
        engine.find_all_targets_in_screenshot = mock.Mock(side_effect=error)
        engine.template_dimensions = mock.Mock(return_value=(4, 4))
        engine._finalize_targets = mock.Mock(return_value=("finalized", []))
        return engine

    def test_unexpected_error_propagates(self):
        engine = self._probe(RuntimeError("cv2 broken"))
        with self.assertRaises(RuntimeError):
            engine._find_all_targets_optimized_impl(
                "img.png", "cache-key", 0.8, False, search_regions=None
            )

    def test_expected_error_family_still_tolerated(self):
        engine = self._probe(ValueError("bad region"))
        result = engine._find_all_targets_optimized_impl(
            "img.png", "cache-key", 0.8, False, search_regions=None
        )
        self.assertEqual(result, [])


class MultiTargetCollectorTests(unittest.TestCase):
    """反馈项：全部匹配的首次命中缓冲、末位缓冲、数量与不足处理。"""

    @staticmethod
    def _loc(x, y, w=10, h=10):
        return (x, y, 1.0, 0.9, w, h)

    def _policy(self, **kwargs):
        from fukua_rpa.vision.collect import CollectionPolicy

        return CollectionPolicy(**kwargs)

    def test_buffers_end_after_last_new_target_not_after_first_hit(self):
        from fukua_rpa.vision.collect import MultiTargetCollector

        collector = MultiTargetCollector(
            self._policy(first_hit_buffer_s=0.5, last_hit_buffer_s=0.5),
            started_at=0.0,
        )
        collector.add_frame([self._loc(0, 0)], 0.0)
        self.assertFalse(collector.should_finish(0.4))
        collector.add_frame([self._loc(0, 0), self._loc(200, 0)], 0.35)
        self.assertFalse(collector.should_finish(0.60))
        collector.add_frame(
            [self._loc(0, 0), self._loc(200, 0), self._loc(0, 200)], 0.70
        )
        # 两个条件并行：0.70 + 0.5 = 1.20 才是结束点，而不是 0.5+0.5。
        self.assertFalse(collector.should_finish(1.10))
        self.assertTrue(collector.should_finish(1.20))

    def test_repeat_observations_do_not_reset_last_hit_buffer(self):
        from fukua_rpa.vision.collect import MultiTargetCollector

        collector = MultiTargetCollector(
            self._policy(last_hit_buffer_s=0.5), started_at=0.0
        )
        collector.add_frame([self._loc(100, 100)], 0.0)
        # 同一目标轻微抖动/置信度变化：不得算作新目标。
        for offset, when in ((2, 0.1), (-3, 0.2), (1, 0.3)):
            collector.add_frame([self._loc(100 + offset, 100)], when)
        self.assertEqual(collector.count, 1)
        self.assertEqual(collector.last_new_at, 0.0)
        self.assertTrue(collector.should_finish(0.5))

    def test_candidate_overflow_does_not_keep_collection_open(self):
        """FR-20261011-003：候选表满后溢出的目标不得再算作“新目标”，否则末位
        缓冲会被每帧刷新，默认不设最长收集时间时收集永不结束。"""
        from fukua_rpa.vision.collect import _MAX_CANDIDATES, MultiTargetCollector

        collector = MultiTargetCollector(
            self._policy(last_hit_buffer_s=0.5), started_at=0.0
        )
        frame = [self._loc(20 * i, 0) for i in range(_MAX_CANDIDATES + 40)]
        collector.add_frame(frame, 0.0)
        self.assertEqual(collector.count, _MAX_CANDIDATES)
        # 后续帧内容完全相同：不得再产生“新目标”，末位缓冲得以到期。
        collector.add_frame(frame, 0.1)
        collector.add_frame(frame, 0.2)
        self.assertEqual(collector.last_new_at, 0.0)
        self.assertTrue(collector.should_finish(0.5))

    def test_same_count_but_new_position_counts_as_new_target(self):
        from fukua_rpa.vision.collect import MultiTargetCollector

        collector = MultiTargetCollector(
            self._policy(last_hit_buffer_s=0.5), started_at=0.0
        )
        collector.add_frame([self._loc(0, 0)], 0.0)
        # 旧目标消失、新位置出现：数量不变但仍是新目标。
        collector.add_frame([self._loc(500, 400)], 0.4)
        self.assertEqual(collector.last_new_at, 0.4)
        self.assertFalse(collector.should_finish(0.6))
        self.assertTrue(collector.should_finish(0.9))

    def test_finite_count_without_partial_keeps_waiting(self):
        from fukua_rpa.vision.collect import MultiTargetCollector

        collector = MultiTargetCollector(
            self._policy(
                last_hit_buffer_s=0.1,
                max_collect_s=1.0,
                max_targets=3,
                allow_partial=False,
            ),
            started_at=0.0,
        )
        collector.add_frame([self._loc(0, 0), self._loc(200, 0)], 0.0)
        self.assertFalse(collector.count_satisfied())
        self.assertFalse(collector.should_finish(5.0))
        self.assertTrue(collector.expired(10.0))
        collector.add_frame(
            [self._loc(0, 0), self._loc(200, 0), self._loc(400, 0)], 10.1
        )
        self.assertTrue(collector.count_satisfied())
        self.assertTrue(collector.should_finish(10.2))

    def test_finite_count_with_partial_finishes_on_quiet_buffer(self):
        from fukua_rpa.vision.collect import MultiTargetCollector

        collector = MultiTargetCollector(
            self._policy(last_hit_buffer_s=0.5, max_targets=5, allow_partial=True),
            started_at=0.0,
        )
        collector.add_frame([self._loc(0, 0)], 0.0)
        self.assertTrue(collector.count_satisfied())
        self.assertTrue(collector.should_finish(0.5))

    def test_deadline_is_measured_from_first_hit_and_bounded(self):
        from fukua_rpa.vision.collect import MultiTargetCollector

        collector = MultiTargetCollector(
            self._policy(last_hit_buffer_s=0.0, max_collect_s=1.0), started_at=0.0
        )
        self.assertFalse(collector.expired(0.0))
        collector.add_frame([self._loc(0, 0)], 2.0)
        self.assertFalse(collector.expired(2.5))
        self.assertTrue(collector.expired(3.0))

    def test_hard_deadline_ends_collection_even_while_new_targets_keep_arriving(self):
        """复核 D1：max_collect_s 必须是不能被新增目标续期的硬上限。"""
        from fukua_rpa.vision.collect import MultiTargetCollector

        collector = MultiTargetCollector(
            self._policy(last_hit_buffer_s=0.5, max_collect_s=1.0, max_targets=0),
            started_at=0.0,
        )
        for step in range(12):
            # 每次都在新位置出现目标：末位缓冲永远不满足。
            collector.add_frame([self._loc(step * 200, 0)], step * 0.1)
        self.assertTrue(collector.should_finish(1.0), "到达硬上限就必须结束收集")

    def test_hard_deadline_reports_timeout_when_count_or_targets_missing(self):
        """复核 D1/D3：到点仍收不齐时必须走超时，而不是无限等待。"""
        from fukua_rpa.vision.collect import MultiTargetCollector

        short = MultiTargetCollector(
            self._policy(
                last_hit_buffer_s=0.2, max_collect_s=1.0, max_targets=3, allow_partial=False
            ),
            started_at=0.0,
        )
        short.add_frame([self._loc(0, 0)], 0.0)
        # 生产循环的判据：应该结束（should_finish）为假、硬上限（expired）为真。
        self.assertFalse(short.should_finish(1.0))
        self.assertTrue(short.expired(1.0))
        self.assertFalse(short.count_satisfied())

        empty = MultiTargetCollector(
            self._policy(last_hit_buffer_s=0.2, max_collect_s=1.0), started_at=0.0
        )
        # 从未命中过目标时不启动收集截止：收集阶段必须交回外层搜索/重试语义。
        self.assertIsNone(empty.required_quiet_time())
        self.assertFalse(empty.expired(1.0))
        self.assertFalse(empty.should_finish(1.0))

    def test_zero_max_collect_disables_hard_cap(self):
        """反馈 20261010-4：max_collect_s=0 表示不设最长收集时间。"""
        from fukua_rpa.vision.collect import MultiTargetCollector

        collector = MultiTargetCollector(
            self._policy(last_hit_buffer_s=0.5, max_collect_s=0.0, max_targets=0),
            started_at=0.0,
        )
        for step in range(12):
            # 新目标持续到达：末位缓冲永远不满足；此时只有时长上限能结束，
            # 而 0 = 不设上限，因此必须一直等待（由本步超时/停止热键接管）。
            collector.add_frame([self._loc(step * 200, 0)], step * 0.1)
        # 最后一帧在 1.1s；旧语义 max_collect_s=1.0 在 1.2s 早已到期。
        self.assertFalse(collector.expired(1.2), "0 不得触发收集硬上限")
        self.assertFalse(collector.should_finish(1.2))
        # 安静缓冲满足后仍按原语义结束。
        collector.add_frame([self._loc(0, 0)], 1.0e9)
        self.assertTrue(collector.should_finish(1.0e9 + 0.5))

    def test_zero_max_collect_with_unsatisfied_count_keeps_waiting(self):
        """max_collect_s=0 且数量未收齐、不允许部分结果：应持续等待。"""
        from fukua_rpa.vision.collect import MultiTargetCollector

        collector = MultiTargetCollector(
            self._policy(
                last_hit_buffer_s=0.2, max_collect_s=0.0, max_targets=3,
                allow_partial=False,
            ),
            started_at=0.0,
        )
        collector.add_frame([self._loc(0, 0)], 0.0)
        self.assertFalse(collector.count_satisfied())
        self.assertFalse(collector.should_finish(3600.0))
        self.assertFalse(collector.expired(3600.0))

    def test_disabled_policy_reports_not_enabled(self):
        policy = self._policy()
        self.assertFalse(policy.enabled)
        self.assertTrue(
            self._policy(first_hit_buffer_s=0.5).enabled
        )
        self.assertTrue(self._policy(max_targets=1).enabled)


class MultiTargetSortingTests(unittest.TestCase):
    """反馈项：第二排序条件只在主条件相同时生效。"""

    def setUp(self):
        self.engine = make_engine()

    def _sorted(self, points, order, order_b):
        self.engine.multi_target_order = order
        self.engine.multi_target_order_b = order_b
        targets = [{"x": x, "y": y} for x, y in points]
        return [(p["x"], p["y"]) for p in self.engine._sort_targets_for_click(targets)]

    def test_second_condition_breaks_primary_tie(self):
        points = [(10, 5), (30, 5), (20, 50)]
        self.assertEqual(
            self._sorted(points, "从上到下", "从右到左"),
            [(30, 5), (10, 5), (20, 50)],
        )
        self.assertEqual(
            self._sorted(points, "从上到下", "从左到右"),
            [(10, 5), (30, 5), (20, 50)],
        )

    def test_primary_condition_still_dominates(self):
        points = [(90, 5), (10, 6)]
        self.assertEqual(
            self._sorted(points, "从上到下", "从右到左"),
            [(90, 5), (10, 6)],
        )

    def test_secondary_supports_vertical_direction(self):
        points = [(5, 10), (5, 30), (40, 20)]
        self.assertEqual(
            self._sorted(points, "从左到右", "从下到上"),
            [(5, 30), (5, 10), (40, 20)],
        )

    def test_disabled_secondary_keeps_deterministic_fallback(self):
        points = [(30, 5), (10, 5)]
        self.assertEqual(
            self._sorted(points, "从上到下", "不启用"),
            [(10, 5), (30, 5)],
        )

    def test_random_primary_ignores_secondary(self):
        points = [(10, 5), (30, 5)]
        with mock.patch("fukua_rpa.vision.match.random.shuffle") as shuffle:
            self._sorted(points, "随机顺序", "从右到左")
        shuffle.assert_called_once()


if __name__ == "__main__":
    unittest.main()
