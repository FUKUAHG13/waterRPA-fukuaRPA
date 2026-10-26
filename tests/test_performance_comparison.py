import unittest

from fukua_rpa.config_schema import default_profile_config
from scripts.benchmark_core import validate_workload
from scripts.benchmark_vision import deterministic_scene, evaluate_opencv_match
from scripts.compare_performance import compare_reports


class PerformanceComparisonTests(unittest.TestCase):
    def baseline(self):
        return {
            "metrics": [
                {
                    "section": "core",
                    "path": "migration.median_ms",
                    "baseline": 10,
                    "max_ratio": 1.5,
                    "slack": 2,
                }
            ]
        }

    def test_relative_regression_passes_inside_threshold(self):
        result = compare_reports(
            self.baseline(), {"core": {"migration": {"median_ms": 14}}}
        )
        self.assertTrue(result["ok"])

    def test_relative_regression_reports_exceeded_metric(self):
        result = compare_reports(
            self.baseline(), {"core": {"migration": {"median_ms": 16}}}
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"][0]["metric"], "core.migration.median_ms")

    def test_core_benchmark_validates_results_before_timing_claims(self):
        tasks = [
            {
                "type": 1.0,
                "value": f"{index},{index}",
                "step_loop_start": str(index % 50 + 1),
            }
            for index in range(1000)
        ]
        profile = default_profile_config()
        profile["tasks"] = tasks
        self.assertTrue(validate_workload(tasks, profile)["ok"])

    def test_vision_benchmark_validates_expected_match_position(self):
        scene, template = deterministic_scene(320, 180, channels=3)
        result = evaluate_opencv_match(
            scene,
            template,
            (320 * 2 // 3, 180 // 3),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["distance_pixels"], 0.0)


if __name__ == "__main__":
    unittest.main()
