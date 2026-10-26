"""Repeatable screenshot/template-matching benchmark for Stage 4 optimization."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
import psutil
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fukua_rpa.vision.core import NativeVisionCore  # noqa: E402
from fukua_rpa.vision.opencv_runtime import configure_opencv_threads  # noqa: E402
from scripts.benchmark_util import apply_benchmark_affinity, percentile  # noqa: E402


def timed(callable_, rounds):
    wall_samples = []
    cpu_samples = []
    process = psutil.Process()
    rss_before = process.memory_info().rss
    rss_peak = rss_before
    for _index in range(max(1, rounds)):
        wall_started = time.perf_counter()
        cpu_started = time.process_time()
        callable_()
        rss_peak = max(rss_peak, process.memory_info().rss)
        cpu_samples.append((time.process_time() - cpu_started) * 1000.0)
        wall_samples.append((time.perf_counter() - wall_started) * 1000.0)
    total_wall = sum(wall_samples)
    return {
        "rounds": len(wall_samples),
        "p50_ms": round(percentile(wall_samples, 50), 3),
        "p95_ms": round(percentile(wall_samples, 95), 3),
        "max_ms": round(max(wall_samples), 3),
        "cpu_ms_total": round(sum(cpu_samples), 3),
        "cpu_to_wall_percent": round(
            100.0 * sum(cpu_samples) / total_wall if total_wall else 0.0, 1
        ),
        "rss_before_bytes": int(rss_before),
        "rss_peak_bytes": int(rss_peak),
        "rss_growth_bytes": int(max(0, rss_peak - rss_before)),
    }


def deterministic_scene(width, height, channels=1, seed=20260711):
    rng = np.random.default_rng(seed + width + height + channels)
    shape = (height, width) if channels == 1 else (height, width, channels)
    scene = rng.integers(0, 256, shape, dtype=np.uint8)
    template = rng.integers(
        0, 256, (24, 32) if channels == 1 else (24, 32, channels), dtype=np.uint8
    )
    x = max(0, width * 2 // 3)
    y = max(0, height // 3)
    scene[y:y + 24, x:x + 32] = template
    return scene, template


def evaluate_opencv_match(scene, template, expected) -> dict:
    response = cv2.matchTemplate(scene, template, cv2.TM_CCOEFF_NORMED)
    _minimum, score, _minimum_location, location = cv2.minMaxLoc(response)
    distance = float(
        np.hypot(location[0] - expected[0], location[1] - expected[1])
    )
    return {
        "ok": distance <= 0.01 and float(score) >= 0.999,
        "expected_top_left": [int(expected[0]), int(expected[1])],
        "actual_top_left": [int(location[0]), int(location[1])],
        "distance_pixels": round(distance, 3),
        "score": round(float(score), 6),
    }


def benchmark_opencv(rounds):
    report = {}
    for label, width, height in (
        ("1080p", 1920, 1080),
        ("2k", 2560, 1440),
        ("4k", 3840, 2160),
    ):
        scene, template = deterministic_scene(width, height, channels=1)
        metric = timed(
            lambda s=scene, t=template: cv2.matchTemplate(
                s, t, cv2.TM_CCOEFF_NORMED
            ),
            rounds,
        )
        metric["working_set_bytes"] = int(scene.nbytes + template.nbytes)
        metric["correctness"] = evaluate_opencv_match(
            scene,
            template,
            (width * 2 // 3, height // 3),
        )
        report[f"gray_single_{label}"] = metric
        del scene, template

    color_scene, color_template = deterministic_scene(1920, 1080, channels=3)
    color_metric = timed(
        lambda s=color_scene, t=color_template: cv2.matchTemplate(
            s, t, cv2.TM_CCOEFF_NORMED
        ),
        rounds,
    )
    color_metric["working_set_bytes"] = int(
        color_scene.nbytes + color_template.nbytes
    )
    color_metric["correctness"] = evaluate_opencv_match(
        color_scene,
        color_template,
        (1920 * 2 // 3, 1080 // 3),
    )
    report["color_single_1080p"] = color_metric
    del color_scene, color_template

    scene, template = deterministic_scene(1920, 1080, channels=1)
    regions = (
        scene[80:320, 120:440],
        scene[80:320, 1480:1800],
        scene[760:1000, 120:440],
        scene[760:1000, 1480:1800],
    )

    def match_regions():
        return [cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED) for region in regions]

    report["gray_four_regions_1080p"] = timed(match_regions, rounds)
    report["gray_four_regions_1080p"]["searched_pixels"] = int(
        sum(region.shape[0] * region.shape[1] for region in regions)
    )
    negative_peak = max(
        float(cv2.minMaxLoc(response)[1]) for response in match_regions()
    )
    report["gray_four_regions_1080p"]["correctness"] = {
        "ok": negative_peak < 0.99,
        "negative_peak_score": round(negative_peak, 6),
    }

    scale_values = (0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2)
    variants = [
        cv2.resize(
            template,
            (
                max(1, int(round(template.shape[1] * scale))),
                max(1, int(round(template.shape[0] * scale))),
            ),
            interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC,
        )
        for scale in scale_values
    ]
    report["gray_nine_scales_1080p"] = timed(
        lambda: [
            cv2.matchTemplate(scene, variant, cv2.TM_CCOEFF_NORMED)
            for variant in variants
        ],
        rounds,
    )
    report["gray_nine_scales_1080p"]["variant_bytes"] = int(
        sum(variant.nbytes for variant in variants)
    )
    report["gray_nine_scales_1080p"]["correctness"] = evaluate_opencv_match(
        scene,
        variants[scale_values.index(1.0)],
        (1920 * 2 // 3, 1080 // 3),
    )
    return report


def benchmark_native(base_dir, rounds, template_path):
    core = NativeVisionCore(base_dir=base_dir)
    if not core.available:
        return {"available": False, "error": core.load_error}
    core.reset_performance_stats()
    region = [(0, 0, 320, 180)]
    results = []

    def find_negative_sample():
        result = core.find_template(
            template_path,
            region,
            0.8,
            1.2,
            0.05,
            True,
            0.999,
            find_all=False,
        )
        results.append(result)
        return result

    timing = timed(
        find_negative_sample,
        rounds,
    )
    return {
        "available": True,
        "api_version": core.version,
        "timing": timing,
        "stats": core.performance_stats(),
        "correctness": {
            "ok": not any(bool(result) for result in results),
            "unexpected_match_count": sum(bool(result) for result in results),
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--assert-limits", action="store_true")
    parser.add_argument("--compare-native-base", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    rounds = max(1, args.rounds)
    configure_opencv_threads(cv2)
    # OPEN-RC-20261015-PERF: bind this benchmark process to the machine-reported top
    # core class (runtime-derived mask + read-back self-check; aborts on failure).
    affinity_env = apply_benchmark_affinity()

    pixels = [
        ((x * 47 + y * 19) % 255, (x * 11 + y * 61) % 255, (x * 73 + y * 7) % 255)
        for y in range(6)
        for x in range(6)
    ]
    with tempfile.TemporaryDirectory() as temp_dir:
        template_path = os.path.join(temp_dir, "native_benchmark_template.png")
        template = Image.new("RGB", (6, 6))
        template.putdata(pixels)
        template.save(template_path)
        report = {
            "format": "fukuaRPA_vision_benchmark_v1",
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "opencv_version": cv2.__version__,
            "opencv_threads": cv2.getNumThreads(),
            "rounds": rounds,
            "opencv": benchmark_opencv(rounds),
            "native_current": benchmark_native(str(ROOT), rounds, template_path),
            **affinity_env,
        }
        if args.compare_native_base:
            report["native_comparison"] = benchmark_native(
                os.path.abspath(args.compare_native_base), rounds, template_path
            )

    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    print(serialized)
    if args.output:
        output_path = os.path.abspath(args.output)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(serialized + "\n")

    if args.assert_limits:
        failures = []
        if int(report.get("opencv_threads", 0)) > 2:
            failures.append(f"opencv thread limit not applied: {report.get('opencv_threads')}")
        for name, metric in report["opencv"].items():
            limit = 3000.0 if "4k" in name or "nine_scales" in name else 1500.0
            if float(metric["p95_ms"]) > limit:
                failures.append(f"{name}>{limit}ms")
            if not metric.get("correctness", {}).get("ok"):
                failures.append(f"{name} returned an incorrect match")
        native = report["native_current"]
        if native.get("available"):
            if not native.get("correctness", {}).get("ok"):
                failures.append("native negative sample produced a false match")
            stats = native.get("stats", {})
            if stats and int(stats.get("captures", 0)) > rounds:
                failures.append("native repeated captures per scale")
            if stats and int(stats.get("integral_builds", 0)) > rounds:
                failures.append("native repeated integral builds per scale")
        if failures:
            raise SystemExit(f"Vision benchmark limits exceeded: {failures}")


if __name__ == "__main__":
    main()
