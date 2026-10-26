import os
import tempfile
import unittest
from unittest import mock

from PIL import Image, ImageDraw

from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.engine import RPAEngine
from fukua_rpa.run_config import EngineRunConfig, RunConfigError
from fukua_rpa.vision.scene_wake import make_scene_signature


class SceneWakeEngineTests(unittest.TestCase):
    def make_engine(self):
        engine = RPAEngine(defer_backends=True)
        engine.log_level = -1
        return engine

    def test_recognition_wait_preserves_base_delay_then_uses_wake(self):
        engine = self.make_engine()
        engine.detect_delay = 0.1
        context = {"search_regions": None}
        engine.begin_scene_monitor = mock.Mock(
            return_value={"mode": "fingerprint", "signatures": ("base",)}
        )
        engine._wait_interruptibly = mock.Mock(return_value=True)
        engine.wait_adaptive_scene = mock.Mock(return_value=True)

        self.assertTrue(engine.wait_recognition_interval(0.4, context))

        engine.begin_scene_monitor.assert_called_once_with(context)
        engine._wait_interruptibly.assert_called_once_with(0.1)
        engine.wait_adaptive_scene.assert_called_once_with(
            0.4,
            context,
            {"mode": "fingerprint", "signatures": ("base",)},
        )

    def test_scene_wake_config_defaults_validate_and_apply(self):
        config = default_profile_config()
        runtime = EngineRunConfig.from_mapping(config)
        engine = self.make_engine()
        runtime.apply_to(engine)

        self.assertTrue(runtime.scene_wake_enabled)
        self.assertEqual(runtime.scene_wake_sensitivity, "balanced")
        self.assertTrue(engine.scene_wake_enabled)
        self.assertEqual(engine.scene_wake_sensitivity, "balanced")

        config["scene_wake_sensitivity"] = "invalid"
        with self.assertRaises(RunConfigError):
            EngineRunConfig.from_mapping(config)

    def test_engine_merges_one_image_across_scale_range_changes(self):
        engine = self.make_engine()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "template.png")
            Image.new("RGB", (8, 8), "white").save(path)
            engine.scale_options_cache["wide"] = (0.8, 1.2, 0.1)
            engine.scale_options_cache["narrow"] = (0.9, 1.1, 0.1)
            engine.record_scale_match(path, "wide", True, 1.2, 0.95)
            engine.record_scale_match(path, "wide", True, 1.0, 0.95)

            preferred, summary = engine.preferred_scales_for(
                path, "narrow", True
            )

        self.assertEqual(len(engine.scale_memory_summaries()), 1)
        self.assertEqual(summary.observed_count, 1)
        self.assertEqual(preferred, (1.0,))

    def test_disabled_wake_uses_plain_combined_waits(self):
        engine = self.make_engine()
        engine.detect_delay = 0.1
        engine.scene_wake_enabled = False
        engine._wait_interruptibly = mock.Mock(return_value=True)
        engine.begin_scene_monitor = mock.Mock()

        self.assertTrue(
            engine.wait_recognition_interval(0.4, {"search_regions": None})
        )

        self.assertEqual(
            engine._wait_interruptibly.call_args_list,
            [mock.call(0.1), mock.call(0.4)],
        )
        engine.begin_scene_monitor.assert_not_called()

    def test_scene_change_wakes_immediately_and_runs_probe(self):
        engine = self.make_engine()
        base = Image.new("RGB", (800, 600), "white")
        changed = base.copy()
        ImageDraw.Draw(changed).rectangle((360, 260, 439, 339), fill="black")
        baseline = (make_scene_signature(base),)
        engine.capture_scene_signatures = mock.Mock(
            return_value=(make_scene_signature(changed),)
        )
        engine.probe_recognition_wake = mock.Mock(return_value=None)

        self.assertTrue(
            engine.wait_adaptive_scene(
                0.4,
                {"search_regions": None},
                baseline,
            )
        )

        engine.probe_recognition_wake.assert_called_once()
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["wake.scene_triggers"], 1)

    def test_dxgi_dirty_region_is_the_primary_low_cost_wake_path(self):
        engine = self.make_engine()
        native = mock.Mock()
        native.available = True
        native.has_dxgi_scene_change = True
        native.dxgi_scene_usable = True
        native.has_scene_fingerprint = False
        native.poll_desktop_change.side_effect = [False, True]
        engine.native_core = native
        engine.probe_recognition_wake = mock.Mock(return_value=None)
        context = {"search_regions": [(10, 20, 300, 200)]}
        base = Image.new("RGB", (800, 600), "white")
        changed = base.copy()
        ImageDraw.Draw(changed).rectangle((360, 260, 439, 339), fill="black")
        engine.capture_scene_signatures = mock.Mock(
            side_effect=[
                (make_scene_signature(base),),
                (make_scene_signature(changed),),
            ]
        )

        baseline = engine.begin_scene_monitor(context)
        self.assertEqual(baseline["mode"], "dxgi")
        self.assertTrue(engine.wait_adaptive_scene(0.4, context, baseline))

        self.assertEqual(
            native.poll_desktop_change.call_args_list,
            [
                mock.call([(10, 20, 300, 200)], reset_baseline=True),
                mock.call([(10, 20, 300, 200)]),
            ],
        )
        engine.probe_recognition_wake.assert_called_once_with(context)

    def test_probe_batches_rotate_across_nonpreferred_scales(self):
        engine = self.make_engine()
        engine.min_scale = 0.8
        engine.max_scale = 1.2
        engine.scale_step = 0.1
        engine.scale_memory_manual = (1.0,)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "template.png")
            Image.new("RGB", (8, 8), "white").save(path)
            context = engine.recognition_wake_context(
                path, "cache", 0.8, True
            )
            first = engine.wake_probe_scales(context)
            second = engine.wake_probe_scales(context)

        self.assertEqual(len(first), 3)
        self.assertEqual(len(second), 3)
        self.assertEqual(first[0], 1.0)
        self.assertEqual(second[0], 1.0)
        self.assertNotEqual(first, second)

    def test_probe_skips_extra_matching_until_a_preferred_scale_exists(self):
        engine = self.make_engine()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "template.png")
            Image.new("RGB", (8, 8), "white").save(path)
            context = engine.recognition_wake_context(
                path, "cache", 0.8, True
            )

            self.assertEqual(engine.wake_probe_scales(context), ())

    def test_probe_hit_is_consumed_without_another_full_search(self):
        engine = self.make_engine()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "template.png")
            Image.new("RGB", (8, 8), "white").save(path)
            context = engine.recognition_wake_context(
                path, "cache", 0.8, True
            )
            engine.wake_probe_scales = mock.Mock(return_value=(1.0,))
            engine.find_target_scales_only = mock.Mock(
                return_value=(100.0, 120.0, 1.0, 0.95)
            )
            engine.probe_recognition_wake(context)
            engine.quick_search_region = mock.Mock(
                side_effect=AssertionError("full search should not run")
            )

            found = engine.find_target_optimized(
                path, "cache", 0.8, True
            )

        self.assertEqual(found, (100.0, 120.0, 1.0, 0.95))
        engine.quick_search_region.assert_not_called()

    def test_explicit_probe_passes_only_requested_scales_to_native(self):
        engine = self.make_engine()
        engine.native_find_targets = mock.Mock(
            return_value=[(20.0, 30.0, 1.2, 0.93)]
        )
        engine.remember_target_result = mock.Mock()

        found = engine.find_target_scales_only(
            "template.png",
            "cache",
            0.8,
            True,
            (1.2, 0.9),
        )

        self.assertEqual(found[2], 1.2)
        self.assertEqual(
            engine.native_find_targets.call_args.kwargs["explicit_scales"],
            (1.2, 0.9),
        )


if __name__ == "__main__":
    unittest.main()
