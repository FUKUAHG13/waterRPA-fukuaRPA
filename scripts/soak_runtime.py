"""Fast lifecycle and simulated-engine soak test without real mouse input."""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from pathlib import Path
from unittest import mock

import psutil

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fukua_rpa.engine import RPAEngine  # noqa: E402
from fukua_rpa.runtime_state import RunLifecycle  # noqa: E402


def main():
    import threading

    def _timeout_exit():
        import os

        print("Soak test exceeded 1 hour; forcing exit.", file=sys.stderr)
        os._exit(2)

    _timer = threading.Timer(3600, _timeout_exit)
    _timer.daemon = True
    _timer.start()

    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=20000)
    parser.add_argument("--assert-limits", action="store_true")
    args = parser.parse_args()
    rounds = max(1, args.rounds)
    process = psutil.Process()
    gc.collect()
    rss_before = process.memory_info().rss
    rss_peak = rss_before
    lifecycle = RunLifecycle()
    start = time.perf_counter()
    for index in range(rounds):
        run_id = lifecycle.reserve()
        if run_id is None or not lifecycle.mark_running(run_id):
            raise RuntimeError(f"Unable to reserve lifecycle at round {index}")
        if index % 3 == 0:
            lifecycle.request_stop()
        if not lifecycle.finish(run_id, "stopped" if index % 3 == 0 else "finished"):
            raise RuntimeError(f"Unable to finish lifecycle at round {index}")
        if index % 1000 == 0:
            rss_peak = max(rss_peak, process.memory_info().rss)

    with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
        engine = RPAEngine()
    engine.log_level = -1
    # Permissive lambda stubs are intentional: the soak test targets memory-leak
    # detection over many lifecycle iterations, not functional verification of
    # task execution, so real task logic is replaced with cheap no-op success.
    engine.load_and_precompute = lambda _tasks: True
    engine.execute_task_once = lambda *_args, **_kwargs: "success"
    for _index in range(min(rounds, 1000)):
        engine.run_tasks([{"type": 4.0, "value": "simulated"}])
        if engine.is_running:
            raise RuntimeError("Engine lifecycle leaked after simulated run")
        if _index % 100 == 0:
            rss_peak = max(rss_peak, process.memory_info().rss)
    gc.collect()
    rss_after = process.memory_info().rss
    rss_peak = max(rss_peak, rss_after)
    elapsed = time.perf_counter() - start
    report = {
        "rounds": rounds,
        "simulated_engine_runs": min(rounds, 1000),
        "elapsed_seconds": elapsed,
        "operations_per_second": rounds / elapsed,
        "rss_before_bytes": rss_before,
        "rss_after_bytes": rss_after,
        "rss_peak_bytes": rss_peak,
        "rss_growth_bytes": max(0, rss_after - rss_before),
        "last_performance_sample_limit": engine.last_performance_report.get(
            "sample_limit", 0
        ),
        "ok": True,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.assert_limits:
        if report["rss_growth_bytes"] > 64 * 1024 * 1024:
            raise SystemExit("Soak memory growth exceeded 64 MB")
        if report["last_performance_sample_limit"] > 512:
            raise SystemExit("Performance sample bound unexpectedly increased")


if __name__ == "__main__":
    main()
