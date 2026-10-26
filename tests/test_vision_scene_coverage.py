"""Targeted coverage tests for fukua_rpa.vision.scene (VisionSceneMixin).

Covers: recognition_wake_context early returns, capture_scene_signatures
(native/fallback/exception), begin_scene_monitor cache init/eviction/dxgi
fallback, wake_probe_scales cursor init, probe_recognition_wake early returns
and pending_fast_matches init, wait_adaptive_scene zero-duration and dxgi
fallback anchor, _wait_dxgi_scene stop/sleep/fallback/eviction/timeout,
_wait_fingerprint_scene no-native interval/stop/sleep/empty/timeout.
"""

import os
import tempfile
import time
import unittest
from unittest import mock

from PIL import Image, ImageDraw

from fukua_rpa.engine import RPAEngine
from fukua_rpa.vision.scene_wake import (
    SceneSignature,
    make_scene_signature,
)


def make_engine():
    engine = RPAEngine(defer_backends=True)
    engine.log_level = -1
    return engine


def make_small_image(color="white", size=(160, 96)):
    return Image.new("RGB", size, color)


def make_changed_image(base, rect=(60, 30, 100, 60), fill="black"):
    changed = base.copy()
    ImageDraw.Draw(changed).rectangle(rect, fill=fill)
    return changed


class RecognitionWakeContextTests(unittest.TestCase):
    """Lines 40-44: early returns for disabled wake and missing path."""

    def test_disabled_wake_returns_none(self):
        engine = make_engine()
        engine.scene_wake_enabled = False
        result = engine.recognition_wake_context(
            "some.png", "key", 0.8, True
        )
        self.assertIsNone(result)

    def test_nonexistent_path_returns_none(self):
        engine = make_engine()
        engine.scene_wake_enabled = True
        result = engine.recognition_wake_context(
            "/nonexistent/no_such_file.png", "key", 0.8, True
        )
        self.assertIsNone(result)



class CaptureSceneSignaturesTests(unittest.TestCase):
    """Lines 65-114: native fingerprint, PIL fallback, exception handling."""

    def test_native_fingerprint_capture(self):
        engine = make_engine()
        native = mock.Mock()
        native.available = True
        native.has_scene_fingerprint = True
        pixels = b"\x80" * (16 * 16)
        native.capture_scene_signature.return_value = (16, 16, pixels)
        engine.native_core = native
        engine.use_native_core = True
        engine._native_disabled_for_run = False
        engine.effective_search_regions = mock.Mock(
            return_value=[(0, 0, 100, 100)]
        )

        result = engine.capture_scene_signatures()

        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], SceneSignature)
        self.assertEqual(result[0].width, 16)
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["wake.native_scene_captures"], 1)

    def test_native_fingerprint_failure_falls_back_to_pil(self):
        engine = make_engine()
        native = mock.Mock()
        native.available = True
        native.has_scene_fingerprint = True
        native.capture_scene_signature.return_value = None
        engine.native_core = native
        engine.use_native_core = True
        engine._native_disabled_for_run = False
        engine.effective_search_regions = mock.Mock(
            return_value=[(0, 0, 100, 100)]
        )
        img = make_small_image()
        engine.iter_search_screenshots = mock.Mock(
            return_value=iter([(img, 0, 0)])
        )
        engine.check_stop_flag = mock.Mock(return_value=False)

        result = engine.capture_scene_signatures()

        self.assertEqual(len(result), 1)
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["wake.native_scene_capture_failures"], 1)

    def test_pil_fallback_path(self):
        engine = make_engine()
        engine.use_native_core = False
        engine.native_core = None
        img = make_small_image()
        engine.iter_search_screenshots = mock.Mock(
            return_value=iter([(img, 0, 0)])
        )
        engine.check_stop_flag = mock.Mock(return_value=False)

        result = engine.capture_scene_signatures()

        self.assertEqual(len(result), 1)
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["wake.scene_captures"], 1)

    def test_stop_flag_during_pil_capture(self):
        engine = make_engine()
        engine.use_native_core = False
        engine.native_core = None
        img = make_small_image()
        engine.iter_search_screenshots = mock.Mock(
            return_value=iter([(img, 0, 0), (img, 0, 0)])
        )
        engine.check_stop_flag = mock.Mock(return_value=True)

        result = engine.capture_scene_signatures()
        self.assertEqual(result, ())

    def test_exception_returns_empty_tuple(self):
        engine = make_engine()
        engine.use_native_core = False
        engine.native_core = None
        engine.iter_search_screenshots = mock.Mock(
            side_effect=RuntimeError("capture boom")
        )

        result = engine.capture_scene_signatures()

        self.assertEqual(result, ())
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["wake.scene_capture_failures"], 1)

    def test_native_no_regions_uses_screen_bounds(self):
        engine = make_engine()
        native = mock.Mock()
        native.available = True
        native.has_scene_fingerprint = True
        pixels = b"\x80" * (16 * 16)
        native.capture_scene_signature.return_value = (16, 16, pixels)
        engine.native_core = native
        engine.use_native_core = True
        engine._native_disabled_for_run = False
        engine.effective_search_regions = mock.Mock(return_value=[])
        engine.screen_bounds = mock.Mock(return_value=(0, 0, 1920, 1080))

        result = engine.capture_scene_signatures()

        self.assertEqual(len(result), 1)
        native.capture_scene_signature.assert_called_once_with(
            (0, 0, 1920, 1080)
        )


class BeginSceneMonitorTests(unittest.TestCase):
    """Lines 132-133, 146-147, 156-157: cache init, eviction, dxgi fallback."""

    def test_cache_initialized_when_none(self):
        engine = make_engine()
        native = mock.Mock()
        native.available = True
        native.has_dxgi_scene_change = True
        native.dxgi_scene_usable = True
        native.poll_desktop_change.return_value = True
        engine.native_core = native
        engine.use_native_core = True
        engine._native_disabled_for_run = False
        engine.effective_search_regions = mock.Mock(
            return_value=[(0, 0, 100, 100)]
        )
        # Remove scene_signature_cache to force initialization
        if hasattr(engine, "scene_signature_cache"):
            engine.scene_signature_cache = None
        engine.capture_scene_signatures = mock.Mock(
            return_value=(SceneSignature(16, 16, b"\x80" * 256),)
        )
        context = {"key": "test_key", "search_regions": None}

        result = engine.begin_scene_monitor(context)

        self.assertEqual(result["mode"], "dxgi")
        self.assertIsNotNone(engine.scene_signature_cache)

    def test_cache_eviction_when_full(self):
        engine = make_engine()
        native = mock.Mock()
        native.available = True
        native.has_dxgi_scene_change = True
        native.dxgi_scene_usable = True
        native.poll_desktop_change.return_value = True
        engine.native_core = native
        engine.use_native_core = True
        engine._native_disabled_for_run = False
        engine.effective_search_regions = mock.Mock(
            return_value=[(0, 0, 100, 100)]
        )
        # Fill cache to max
        from fukua_rpa.vision.shared import _MAX_SCENE_SIGNATURE_CACHE

        engine.scene_signature_cache = {
            f"key_{i}": () for i in range(_MAX_SCENE_SIGNATURE_CACHE)
        }
        sig = SceneSignature(16, 16, b"\x80" * 256)
        engine.capture_scene_signatures = mock.Mock(return_value=(sig,))
        context = {"key": "new_key", "search_regions": None}

        result = engine.begin_scene_monitor(context)

        self.assertEqual(result["mode"], "dxgi")
        # Cache should have been evicted (half removed) then new entry added
        self.assertLessEqual(
            len(engine.scene_signature_cache), _MAX_SCENE_SIGNATURE_CACHE
        )

    def test_dxgi_poll_none_falls_back_to_fingerprint(self):
        engine = make_engine()
        native = mock.Mock()
        native.available = True
        native.has_dxgi_scene_change = True
        native.dxgi_scene_usable = True
        native.poll_desktop_change.return_value = None
        engine.native_core = native
        engine.use_native_core = True
        engine._native_disabled_for_run = False
        engine.effective_search_regions = mock.Mock(
            return_value=[(0, 0, 100, 100)]
        )
        sig = SceneSignature(16, 16, b"\x80" * 256)
        engine.capture_scene_signatures = mock.Mock(return_value=(sig,))
        context = {"key": "k", "search_regions": None}

        result = engine.begin_scene_monitor(context)

        self.assertEqual(result["mode"], "fingerprint")
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["wake.dxgi_fallbacks"], 1)


class WakeProbeScalesTests(unittest.TestCase):
    """Lines 171, 194-195: memory_context None, cursor initialization."""

    def test_returns_empty_when_memory_context_none(self):
        engine = make_engine()
        engine.scale_memory_context = mock.Mock(return_value=None)
        context = {
            "img_path": "/nonexistent.png",
            "cache_key": "k",
            "use_gray": True,
            "key": "k",
        }
        result = engine.wake_probe_scales(context)
        self.assertEqual(result, ())

    def test_cursor_initialized_when_none(self):
        engine = make_engine()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tpl.png")
            Image.new("RGB", (8, 8), "white").save(path)
            engine.min_scale = 0.8
            engine.max_scale = 1.2
            engine.scale_step = 0.1
            engine.scale_memory_manual = (1.0,)
            # Remove cursors to force initialization
            if hasattr(engine, "scene_probe_cursors"):
                del engine.scene_probe_cursors
            context = engine.recognition_wake_context(
                path, "cache", 0.8, True
            )
            result = engine.wake_probe_scales(context)
            self.assertIsInstance(result, tuple)
            self.assertTrue(hasattr(engine, "scene_probe_cursors"))


class ProbeRecognitionWakeTests(unittest.TestCase):
    """Lines 204, 207, 225-226, 230: early returns, pending init, verbose."""

    def test_none_context_returns_none(self):
        engine = make_engine()
        result = engine.probe_recognition_wake(None)
        self.assertIsNone(result)

    def test_empty_context_returns_none(self):
        engine = make_engine()
        result = engine.probe_recognition_wake({})
        self.assertIsNone(result)

    def test_fast_probe_disabled_returns_none(self):
        engine = make_engine()
        context = {"allow_fast_probe": False}
        result = engine.probe_recognition_wake(context)
        self.assertIsNone(result)

    def test_no_scales_returns_none(self):
        engine = make_engine()
        engine.wake_probe_scales = mock.Mock(return_value=())
        context = {"allow_fast_probe": True, "key": "k"}
        result = engine.probe_recognition_wake(context)
        self.assertIsNone(result)

    def test_pending_fast_matches_initialized_when_none(self):
        engine = make_engine()
        engine.wake_probe_scales = mock.Mock(return_value=(1.0,))
        engine.find_target_scales_only = mock.Mock(
            return_value=(10.0, 20.0, 1.0, 0.95)
        )
        # Remove pending_fast_matches to force initialization
        if hasattr(engine, "pending_fast_matches"):
            del engine.pending_fast_matches
        context = {
            "allow_fast_probe": True,
            "key": "test_key",
            "img_path": "tpl.png",
            "cache_key": "c",
            "task_conf": 0.8,
            "use_gray": True,
            "search_regions": None,
        }
        result = engine.probe_recognition_wake(context)

        self.assertEqual(result, (10.0, 20.0, 1.0, 0.95))
        self.assertTrue(hasattr(engine, "pending_fast_matches"))
        self.assertEqual(
            engine.pending_fast_matches["test_key"],
            (10.0, 20.0, 1.0, 0.95),
        )

    def test_probe_hit_with_verbose_log(self):
        engine = make_engine()
        engine.wake_probe_scales = mock.Mock(return_value=(1.0,))
        engine.find_target_scales_only = mock.Mock(
            return_value=(10.0, 20.0, 1.0, 0.95)
        )
        engine.verbose_log_enabled = mock.Mock(return_value=True)
        engine.log = mock.Mock()
        context = {
            "allow_fast_probe": True,
            "key": "k",
            "img_path": "tpl.png",
            "cache_key": "c",
            "task_conf": 0.8,
            "use_gray": True,
            "search_regions": None,
        }
        result = engine.probe_recognition_wake(context)
        self.assertIsNotNone(result)
        engine.log.assert_called_once()


class WaitAdaptiveSceneTests(unittest.TestCase):
    """Lines 240, 272: zero duration, dxgi fallback anchor."""

    def test_zero_duration_returns_not_stopped(self):
        engine = make_engine()
        engine.check_stop_flag = mock.Mock(return_value=False)
        result = engine.wait_adaptive_scene(0.0, {"key": "k"})
        self.assertTrue(result)

    def test_negative_duration_returns_not_stopped(self):
        engine = make_engine()
        engine.check_stop_flag = mock.Mock(return_value=False)
        result = engine.wait_adaptive_scene(-1.0, {"key": "k"})
        self.assertTrue(result)

    def test_zero_duration_stopped_returns_false(self):
        engine = make_engine()
        engine.check_stop_flag = mock.Mock(return_value=True)
        result = engine.wait_adaptive_scene(0.0, {"key": "k"})
        self.assertFalse(result)

    def test_none_context_returns_not_stopped(self):
        engine = make_engine()
        engine.check_stop_flag = mock.Mock(return_value=False)
        result = engine.wait_adaptive_scene(1.0, None)
        self.assertTrue(result)

    def test_dxgi_fallback_to_fingerprint(self):
        """When _wait_dxgi_scene returns None, fallback anchor is used."""
        engine = make_engine()
        engine.check_stop_flag = mock.Mock(return_value=False)
        base = make_small_image()
        sig = make_scene_signature(base)
        baseline = {
            "mode": "dxgi",
            "signatures": (sig,),
            "regions": [(0, 0, 100, 100)],
            "initial_dirty": False,
        }
        context = {"search_regions": None, "key": "k"}
        # _wait_dxgi_scene returns (None, fallback_anchor)
        fallback_sig = make_scene_signature(make_changed_image(base))
        engine._wait_dxgi_scene = mock.Mock(
            return_value=(None, (fallback_sig,))
        )
        engine._wait_fingerprint_scene = mock.Mock(return_value=True)

        result = engine.wait_adaptive_scene(0.5, context, baseline)

        self.assertTrue(result)
        engine._wait_fingerprint_scene.assert_called_once()
        # Verify fallback anchor was passed
        call_args = engine._wait_fingerprint_scene.call_args
        self.assertEqual(call_args[0][2], (fallback_sig,))


class WaitDxgiSceneTests(unittest.TestCase):
    """Lines 295, 297-298, 301, 306-310, 326, 335, 349-352, 360, 368."""

    def _make_args(self, engine, poll_side_effect=None):
        native = mock.Mock()
        native.poll_desktop_change = mock.Mock(
            side_effect=poll_side_effect or []
        )
        context = {"search_regions": None, "key": "test_key"}
        base = make_small_image()
        anchor = (make_scene_signature(base),)
        deadline = time.monotonic() + 0.3
        return context, "balanced", anchor, deadline, native

    def test_stop_flag_before_first_poll(self):
        engine = make_engine()
        engine.check_stop_flag = mock.Mock(return_value=True)
        context, sensitivity, anchor, deadline, native = self._make_args(
            engine
        )
        result, fallback = engine._wait_dxgi_scene(
            context, sensitivity, anchor, deadline, native,
            [(0, 0, 100, 100)], False, 0.18, 0.06,
        )
        self.assertFalse(result)
        self.assertIsNone(fallback)

    def test_stop_flag_after_sleep(self):
        engine = make_engine()
        # First check_stop_flag (top of loop) returns False,
        # second (after sleep) returns True
        engine.check_stop_flag = mock.Mock(
            side_effect=[False, True]
        )
        context, sensitivity, anchor, deadline, native = self._make_args(
            engine
        )
        native.poll_desktop_change = mock.Mock(return_value=False)
        result, fallback = engine._wait_dxgi_scene(
            context, sensitivity, anchor, deadline, native,
            [(0, 0, 100, 100)], False, 0.18, 0.06,
        )
        self.assertFalse(result)

    def test_poll_none_triggers_fallback(self):
        engine = make_engine()
        engine.check_stop_flag = mock.Mock(return_value=False)
        context, sensitivity, anchor, deadline, native = self._make_args(
            engine, poll_side_effect=[None]
        )
        fallback_sig = make_scene_signature(make_small_image("gray"))
        engine.capture_scene_signatures = mock.Mock(
            return_value=(fallback_sig,)
        )
        result, fallback = engine._wait_dxgi_scene(
            context, sensitivity, anchor, deadline, native,
            [(0, 0, 100, 100)], False, 0.18, 0.06,
        )
        self.assertIsNone(result)
        self.assertEqual(fallback, (fallback_sig,))
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["wake.dxgi_fallbacks"], 1)

    def test_empty_capture_during_confirmation_returns_fallback(self):
        engine = make_engine()
        engine.check_stop_flag = mock.Mock(return_value=False)
        context, sensitivity, anchor, deadline, native = self._make_args(
            engine, poll_side_effect=[True]
        )
        engine.capture_scene_signatures = mock.Mock(return_value=())
        result, fallback = engine._wait_dxgi_scene(
            context, sensitivity, anchor, deadline, native,
            [(0, 0, 100, 100)], True, 0.0, 0.06,
        )
        self.assertIsNone(result)
        self.assertEqual(fallback, ())

    def test_cache_eviction_during_confirmation(self):
        engine = make_engine()
        engine.check_stop_flag = mock.Mock(return_value=False)
        from fukua_rpa.vision.shared import _MAX_SCENE_SIGNATURE_CACHE

        engine.scene_signature_cache = {
            f"k{i}": () for i in range(_MAX_SCENE_SIGNATURE_CACHE)
        }
        base = make_small_image()
        changed = make_changed_image(base)
        anchor = (make_scene_signature(base),)
        current_sig = (make_scene_signature(changed),)
        context = {"search_regions": None, "key": "new_key"}
        native = mock.Mock()
        native.poll_desktop_change = mock.Mock(
            side_effect=[True, False, False]
        )
        engine.capture_scene_signatures = mock.Mock(
            return_value=current_sig
        )
        engine.probe_recognition_wake = mock.Mock(return_value=None)
        deadline = time.monotonic() + 0.5

        engine._wait_dxgi_scene(
            context, "balanced", anchor, deadline, native,
            [(0, 0, 100, 100)], True, 0.0, 0.06,
        )
        # Cache should have been evicted
        self.assertLessEqual(
            len(engine.scene_signature_cache), _MAX_SCENE_SIGNATURE_CACHE
        )

    def test_timeout_returns_not_stopped(self):
        engine = make_engine()
        engine.check_stop_flag = mock.Mock(return_value=False)
        context, sensitivity, anchor, _deadline, native = self._make_args(
            engine
        )
        # Deadline already passed
        deadline = time.monotonic() - 0.01
        native.poll_desktop_change = mock.Mock(return_value=False)
        result, fallback = engine._wait_dxgi_scene(
            context, sensitivity, anchor, deadline, native,
            [(0, 0, 100, 100)], False, 0.18, 0.06,
        )
        self.assertTrue(result)
        self.assertIsNone(fallback)

    def test_confirmed_change_with_verbose_log(self):
        engine = make_engine()
        engine.check_stop_flag = mock.Mock(return_value=False)
        engine.verbose_log_enabled = mock.Mock(return_value=True)
        engine.log = mock.Mock()
        engine.probe_recognition_wake = mock.Mock(return_value=None)
        base = make_small_image()
        changed = make_changed_image(base)
        anchor = (make_scene_signature(base),)
        current_sig = (make_scene_signature(changed),)
        context = {"search_regions": None, "key": "k"}
        native = mock.Mock()
        native.poll_desktop_change = mock.Mock(return_value=True)
        engine.capture_scene_signatures = mock.Mock(
            return_value=current_sig
        )
        deadline = time.monotonic() + 0.5

        result, fallback = engine._wait_dxgi_scene(
            context, "balanced", anchor, deadline, native,
            [(0, 0, 100, 100)], True, 0.0, 0.06,
        )
        self.assertTrue(result)
        engine.log.assert_called()
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["wake.dxgi_confirmed_changes"], 1)


class WaitFingerprintSceneTests(unittest.TestCase):
    """Lines 384, 389, 391-392, 395, 400, 410, 421, 429-430."""

    def test_no_native_increases_interval(self):
        """Line 384: interval bumped to 0.25 when native unavailable."""
        engine = make_engine()
        engine.use_native_core = False
        engine.native_core = None
        engine.check_stop_flag = mock.Mock(return_value=False)
        base = make_small_image()
        anchor = (make_scene_signature(base),)
        context = {"search_regions": None}
        # Use a very short deadline so the loop exits quickly
        deadline = time.monotonic() + 0.01
        engine.capture_scene_signatures = mock.Mock(return_value=anchor)

        result = engine._wait_fingerprint_scene(
            context, "balanced", anchor, deadline
        )
        self.assertTrue(result)

    def test_stop_flag_at_top_of_loop(self):
        engine = make_engine()
        engine.use_native_core = True
        native = mock.Mock()
        native.available = True
        native.has_scene_fingerprint = True
        engine.native_core = native
        engine.check_stop_flag = mock.Mock(return_value=True)
        context = {"search_regions": None}
        deadline = time.monotonic() + 1.0

        result = engine._wait_fingerprint_scene(
            context, "balanced", (), deadline
        )
        self.assertFalse(result)

    def test_stop_flag_after_sleep(self):
        engine = make_engine()
        engine.use_native_core = True
        native = mock.Mock()
        native.available = True
        native.has_scene_fingerprint = True
        engine.native_core = native
        engine.check_stop_flag = mock.Mock(
            side_effect=[False, True]
        )
        context = {"search_regions": None}
        deadline = time.monotonic() + 1.0
        engine.capture_scene_signatures = mock.Mock(return_value=())

        result = engine._wait_fingerprint_scene(
            context, "balanced", (), deadline
        )
        self.assertFalse(result)

    def test_empty_capture_continues(self):
        """Line 400: empty current continues the loop."""
        engine = make_engine()
        engine.use_native_core = True
        native = mock.Mock()
        native.available = True
        native.has_scene_fingerprint = True
        engine.native_core = native
        engine.check_stop_flag = mock.Mock(return_value=False)
        context = {"search_regions": None}
        # Short deadline so loop exits after one iteration
        deadline = time.monotonic() + 0.01
        engine.capture_scene_signatures = mock.Mock(return_value=())

        result = engine._wait_fingerprint_scene(
            context, "balanced", (), deadline
        )
        self.assertTrue(result)

    def test_scene_change_detected_with_previous_comparison(self):
        """Lines 409-414, 421: previous comparison branch and verbose log.

        intermediate 与 anchor 一致，使第一次迭代的 anchor 比较不触发变化，
        循环进入第二次迭代——此时 previous 已更新为 intermediate（非 anchor），
        从而真正行使 scene.py:409 的 `previous is not anchor` 比较分支。
        """
        engine = make_engine()
        engine.use_native_core = True
        native = mock.Mock()
        native.available = True
        native.has_scene_fingerprint = True
        engine.native_core = native
        engine.check_stop_flag = mock.Mock(return_value=False)
        engine.verbose_log_enabled = mock.Mock(return_value=True)
        engine.log = mock.Mock()
        engine.probe_recognition_wake = mock.Mock(return_value=None)

        base = make_small_image()
        changed = make_changed_image(base)
        anchor = (make_scene_signature(base),)
        # 第一次捕获返回与 anchor 一致的签名：anchor 比较不触发，previous 更新为它
        intermediate = base
        intermediate_sig = (make_scene_signature(intermediate),)
        changed_sig = (make_scene_signature(changed),)

        context = {"search_regions": None, "key": "k"}
        deadline = time.monotonic() + 2.0
        engine.capture_scene_signatures = mock.Mock(
            side_effect=[intermediate_sig, changed_sig]
        )

        result = engine._wait_fingerprint_scene(
            context, "sensitive", anchor, deadline
        )
        self.assertTrue(result)
        # 捕获被调用两次：证明第一次未触发、循环进入了第二次迭代，
        # 即 scene.py:409 的 previous 比较分支确实被执行。
        self.assertEqual(engine.capture_scene_signatures.call_count, 2)

    def test_timeout_returns_not_stopped(self):
        """Lines 429-430: loop exits on deadline."""
        engine = make_engine()
        engine.use_native_core = True
        native = mock.Mock()
        native.available = True
        native.has_scene_fingerprint = True
        engine.native_core = native
        engine.check_stop_flag = mock.Mock(return_value=False)
        base = make_small_image()
        anchor = (make_scene_signature(base),)
        context = {"search_regions": None}
        # Deadline already passed
        deadline = time.monotonic() - 0.01
        engine.capture_scene_signatures = mock.Mock(return_value=anchor)

        result = engine._wait_fingerprint_scene(
            context, "balanced", anchor, deadline
        )
        self.assertTrue(result)

    def test_change_detected_triggers_probe(self):
        engine = make_engine()
        engine.use_native_core = True
        native = mock.Mock()
        native.available = True
        native.has_scene_fingerprint = True
        engine.native_core = native
        engine.check_stop_flag = mock.Mock(return_value=False)
        engine.verbose_log_enabled = mock.Mock(return_value=False)
        engine.probe_recognition_wake = mock.Mock(return_value=None)

        base = make_small_image()
        changed = make_changed_image(base)
        anchor = (make_scene_signature(base),)
        changed_sig = (make_scene_signature(changed),)
        context = {"search_regions": None, "key": "k"}
        deadline = time.monotonic() + 1.0
        engine.capture_scene_signatures = mock.Mock(
            return_value=changed_sig
        )

        result = engine._wait_fingerprint_scene(
            context, "balanced", anchor, deadline
        )
        self.assertTrue(result)
        engine.probe_recognition_wake.assert_called_once_with(context)
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["wake.scene_triggers"], 1)


if __name__ == "__main__":
    unittest.main()
