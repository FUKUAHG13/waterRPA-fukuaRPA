"""Exercise the native emergency-stop path against a deliberately heavy search."""

from __future__ import annotations

import ctypes
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fukua_rpa.vision.core import (  # noqa: E402
    NATIVE_RESULT_CANCELLED,
    NativeVisionCore,
)


def create_pattern(path: Path) -> None:
    image = Image.new("RGB", (8, 8))
    image.putdata(
        [
            (
                (x * 37 + y * 19) % 256,
                (x * 11 + y * 53) % 256,
                (x * 71 + y * 7) % 256,
            )
            for y in range(8)
            for x in range(8)
        ]
    )
    image.save(path, "PNG")


def run_smoke(max_stop_seconds=0.3) -> dict:
    core = NativeVisionCore(base_dir=str(ROOT))
    if not core.available or not core.has_cancellable_search:
        raise RuntimeError("Native core does not expose cancellable search")
    screen_width = max(320, int(ctypes.windll.user32.GetSystemMetrics(0)))
    screen_height = max(240, int(ctypes.windll.user32.GetSystemMetrics(1)))
    width = min(screen_width, 1200)
    height = min(screen_height, 700)
    # Keep one grayscale scale below the native 180M work budget, then repeat
    # the region enough times that even a small desktop enters real work before
    # cancellation. A broad scale range would be rejected with -2 before the
    # cancellable capture/match path starts and would therefore test nothing.
    template_size = 8
    per_region_work = (
        max(1, width - template_size + 1)
        * max(1, height - template_size + 1)
        * template_size
        * template_size
    )
    repeat_count = max(1, min(32, int(165_000_000 // per_region_work)))
    planned_work = per_region_work * repeat_count
    regions = [(0, 0, width, height)] * repeat_count
    result = []
    with tempfile.TemporaryDirectory() as temp_dir:
        template_path = Path(temp_dir) / "cancel-pattern.png"
        create_pattern(template_path)
        core.reset_performance_stats()

        thread = threading.Thread(
            target=lambda: result.append(
                core.find_template(
                    str(template_path),
                    regions,
                    1.0,
                    1.0,
                    0.05,
                    True,
                    0.99,
                    parallel_mode="force",
                )
            ),
            daemon=True,
            name="native-cancel-smoke",
        )
        thread.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            stats = core.performance_stats()
            if stats.get("captures", 0) > 0:
                break
            if result or not thread.is_alive():
                raise RuntimeError(
                    "Native search finished before the cancellation request"
                )
            time.sleep(0.0005)
        else:
            raise RuntimeError("Native search did not enter screen capture")
        cancel_started = time.monotonic()
        if not core.cancel_active_search():
            raise RuntimeError("Native cancellation request was rejected")
        thread.join(max(1.0, max_stop_seconds * 3.0))
        stop_seconds = time.monotonic() - cancel_started

    report = {
        "format": "fukuaRPA_native_cancellation_smoke",
        "native_version": core.version,
        "region_count": repeat_count,
        "planned_work": planned_work,
        "cancelled_result_code": core.last_result_code,
        "stop_latency_ms": round(stop_seconds * 1000.0, 3),
        "thread_stopped": not thread.is_alive(),
        "result_is_none": result == [None],
        "limit_ms": round(float(max_stop_seconds) * 1000.0, 3),
    }
    report["ok"] = bool(
        report["thread_stopped"]
        and report["result_is_none"]
        and core.last_result_code == NATIVE_RESULT_CANCELLED
        and stop_seconds <= max_stop_seconds
    )
    return report


def main() -> None:
    report = run_smoke()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise SystemExit("Native cancellation latency gate failed")


if __name__ == "__main__":
    main()
