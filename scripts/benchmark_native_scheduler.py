"""Benchmark native multiscale scheduling against a real visible target.

FR-20260811-D16（差异文档化）：本基准是**原生专属**基准（无原生核心则无被测
对象），原生缺失时硬失败（SystemExit，见 main）——这与 benchmark_vision.py 的
混合回退基准不同（后者原生缺失时优雅跳过、报告 available:False 并继续测
Python 路径）。这是语义差异而非不一致：原生专属基准没有可测的 Python 等价物，
硬失败是正确行为；统一两者处理会丢失"基准无意义"的信号。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fukua_rpa.native_smoke import (  # noqa: E402
    _capture_region,
    _generated_pattern_window,
)
from fukua_rpa.vision.core import NativeVisionCore  # noqa: E402
from scripts.benchmark_util import apply_benchmark_affinity, percentile  # noqa: E402


def time_search(core, rounds, call):
    warmup = call()
    if not warmup:
        raise RuntimeError(core.load_error or "native benchmark warmup found no target")
    core.reset_performance_stats()
    process = psutil.Process()
    wall_samples = []
    cpu_samples = []
    result = None
    for _index in range(max(1, rounds)):
        wall_started = time.perf_counter()
        cpu_started = time.process_time()
        result = call()
        cpu_samples.append((time.process_time() - cpu_started) * 1000.0)
        wall_samples.append((time.perf_counter() - wall_started) * 1000.0)
        if not result:
            raise RuntimeError(core.load_error or "native benchmark lost the target")
    total_wall = sum(wall_samples)
    return {
        "rounds": len(wall_samples),
        "p50_ms": round(percentile(wall_samples, 50), 3),
        "p95_ms": round(percentile(wall_samples, 95), 3),
        "mean_ms": round(statistics.fmean(wall_samples), 3),
        "cpu_ms_total": round(sum(cpu_samples), 3),
        "cpu_to_wall_percent": round(
            100.0 * sum(cpu_samples) / total_wall if total_wall else 0.0, 1
        ),
        "rss_bytes": int(process.memory_info().rss),
        "match": [round(float(value), 6) for value in result[0][:4]],
        "native_stats": core.performance_stats(),
    }


def benchmark_core(core, template_path, region, rounds, *, extended):
    common = {
        "image_path": template_path,
        "regions": [region],
        "min_scale": 0.8,
        "max_scale": 1.2,
        "scale_step": 0.05,
        "use_gray": True,
        "threshold": 0.999,
    }
    scenarios = {
        "auto_full": lambda: core.find_template(**common, parallel_mode="auto"),
    }
    if extended:
        scenarios.update(
            {
                "single_thread_full": lambda: core.find_template(
                    **common, parallel_mode="off"
                ),
                "forced_multi_full": lambda: core.find_template(
                    **common, parallel_mode="force"
                ),
                "preferred_hit": lambda: core.find_template(
                    **common, parallel_mode="auto", preferred_scale=1.0
                ),
                "preferred_miss_fallback": lambda: core.find_template(
                    **common, parallel_mode="auto", preferred_scale=0.9
                ),
                "preferred_list_hit": lambda: core.find_template(
                    **common,
                    parallel_mode="auto",
                    preferred_scales=(0.9, 1.0),
                ),
                "preferred_list_miss_fallback": lambda: core.find_template(
                    **common,
                    parallel_mode="auto",
                    preferred_scales=(0.9, 1.1),
                ),
                "explicit_scale_hit": lambda: core.find_template(
                    **common,
                    parallel_mode="auto",
                    preferred_scales=(1.0,),
                    explicit_scale_only=True,
                ),
            }
        )
    return {
        "api_version": core.version,
        "capabilities": core.capabilities(),
        "scenarios": {
            name: time_search(core, rounds, call)
            for name, call in scenarios.items()
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--compare-native-base", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--assert-limits", action="store_true")
    args = parser.parse_args()
    # OPEN-RC-20261015-PERF: bind this benchmark process to the machine-reported top
    # core class (runtime-derived mask + read-back self-check; aborts on failure).
    affinity_env = apply_benchmark_affinity()
    current = NativeVisionCore(base_dir=str(ROOT))
    if not current.available:
        raise SystemExit(current.load_error)

    with _generated_pattern_window() as (_hwnd, region):
        scene = _capture_region(region)
        template_box = (32, 32, 80, 80)
        expected = (
            region[0] + (template_box[0] + template_box[2]) / 2.0,
            region[1] + (template_box[1] + template_box[3]) / 2.0,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            template_path = os.path.join(temp_dir, "scheduler_template.png")
            scene.crop(template_box).save(template_path)
            report = {
                "format": "fukuaRPA_native_scheduler_benchmark_v1",
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "expected_center": [round(value, 3) for value in expected],
                "current": benchmark_core(
                    current, template_path, region, args.rounds, extended=True
                ),
                **affinity_env,
            }
            if args.compare_native_base:
                comparison = NativeVisionCore(
                    base_dir=os.path.abspath(args.compare_native_base)
                )
                if comparison.available:
                    report["comparison"] = benchmark_core(
                        comparison,
                        template_path,
                        region,
                        args.rounds,
                        extended=False,
                    )
                else:
                    report["comparison"] = {
                        "available": False,
                        "error": comparison.load_error,
                    }

    for group_name in ("current", "comparison"):
        group = report.get(group_name, {})
        for metric in group.get("scenarios", {}).values():
            x, y = metric["match"][:2]
            metric["distance_to_expected"] = round(
                math.hypot(x - expected[0], y - expected[1]), 3
            )

    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    print(serialized)
    if args.output:
        output_path = os.path.abspath(args.output)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        Path(output_path).write_text(serialized + "\n", encoding="utf-8")

    if args.assert_limits:
        # extended=True (passed to benchmark_core above) guarantees all scenario
        # keys below are present in the report; direct access is safe.
        current_report = report["current"]
        if current_report["api_version"] < 11000:
            raise SystemExit("native scheduler benchmark requires API 11000+")
        for name in (
            "bounded_job_pool",
            "preferred_scale_fallback",
            "preferred_scale_list",
            "explicit_scale_only",
            "low_res_scene_fingerprint",
            "dxgi_scene_change",
        ):
            if not current_report["capabilities"].get(name):
                raise SystemExit(f"missing native capability: {name}")
        for name, metric in current_report["scenarios"].items():
            if metric["distance_to_expected"] > 3.0:
                raise SystemExit(f"{name} coordinate drifted")
        if current_report["scenarios"]["preferred_hit"]["match"][2] != 1.0:
            raise SystemExit("preferred scale did not win")
        if (
            current_report["scenarios"]["preferred_miss_fallback"]["match"][2]
            != 1.0
        ):
            raise SystemExit("preferred-scale miss did not fully fall back")
        if current_report["scenarios"]["preferred_list_hit"]["match"][2] != 1.0:
            raise SystemExit("preferred-scale list did not find its matching scale")
        if (
            current_report["scenarios"]["preferred_list_miss_fallback"]["match"][2]
            != 1.0
        ):
            raise SystemExit("preferred-scale list miss did not fully fall back")
        if current_report["scenarios"]["explicit_scale_hit"]["match"][2] != 1.0:
            raise SystemExit("explicit-scale search did not find its requested scale")


if __name__ == "__main__":
    main()
