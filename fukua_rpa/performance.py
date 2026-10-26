"""Low-overhead bounded runtime timing and counter collection."""

from __future__ import annotations

import math
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

DEFAULT_SAMPLE_LIMIT = 256


def _percentile(sorted_values: list[int], percentile: float) -> int:
    """Compute a percentile value from a pre-sorted list of integers.

    Uses the nearest-rank method: ceil(percentile/100 * N) - 1.

    Args:
        sorted_values: A sorted list of integer sample values.
        percentile: The percentile to compute (0-100).

    Returns:
        The value at the computed rank, or 0 if the list is empty.
    """
    if not sorted_values:
        return 0
    rank = max(0, math.ceil((float(percentile) / 100.0) * len(sorted_values)) - 1)
    return sorted_values[min(rank, len(sorted_values) - 1)]


@dataclass
class _TimingSeries:
    """Bounded rolling sample series for a single named timing metric.

    Retains at most *sample_limit* recent observations for percentile
    computation while tracking unbounded count and total for run-lifetime
    averages.
    """

    sample_limit: int
    count: int = 0
    total_ns: int = 0
    max_ns: int = 0
    samples_ns: deque[int] = field(init=False)

    def __post_init__(self) -> None:
        """Initialize the bounded sample deque after dataclass field setup."""
        self.samples_ns = deque(maxlen=self.sample_limit)

    def observe(self, elapsed_ns: int) -> None:
        """Record a single timing observation in nanoseconds.

        Args:
            elapsed_ns: Elapsed time in nanoseconds (clamped to >= 0).
        """
        value = max(0, int(elapsed_ns))
        self.count += 1
        self.total_ns += value
        self.max_ns = max(self.max_ns, value)
        self.samples_ns.append(value)

    def snapshot(self) -> dict[str, Any]:
        """Produce a statistical summary of this timing series.

        Returns:
            A dictionary with count, sampled_count, total_ms, mean_ms,
            p50_ms, p95_ms, and max_ms.
        """
        samples = sorted(self.samples_ns)
        sampled_count = len(samples)
        return {
            "count": self.count,
            "sampled_count": sampled_count,
            "total_ms": round(self.total_ns / 1_000_000.0, 3),
            "mean_ms": round(
                (self.total_ns / self.count) / 1_000_000.0 if self.count else 0.0,
                3,
            ),
            "p50_ms": round(_percentile(samples, 50) / 1_000_000.0, 3),
            "p95_ms": round(_percentile(samples, 95) / 1_000_000.0, 3),
            "max_ms": round(self.max_ns / 1_000_000.0, 3),
        }


class PerformanceMetrics:
    """Collect bounded timings plus unbounded integer counters and gauges.

    Timings retain only a small rolling sample for percentiles while totals keep
    the full-run count. This keeps memory stable during indefinite automation.

    NOTE: 键集合由代码静态决定（非用户输入），不存在无限增长风险。
    """

    def __init__(self, sample_limit: int = DEFAULT_SAMPLE_LIMIT) -> None:
        """Initialize the metrics collector.

        Args:
            sample_limit: Maximum number of recent samples retained per timing
                key for percentile computation (minimum 16).
        """
        self.sample_limit = max(16, int(sample_limit))
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        """Reset all timings, counters, and gauges to their initial state."""
        with self._lock:
            self._started_ns = time.perf_counter_ns()
            self._timings: dict[str, _TimingSeries] = {}
            self._counters: defaultdict[str, int] = defaultdict(int)
            self._gauges: dict[str, Any] = {}

    def observe_ns(self, name: str, elapsed_ns: int) -> None:
        """Record a timing observation in nanoseconds.

        Args:
            name: The timing metric key.
            elapsed_ns: Elapsed time in nanoseconds.
        """
        key = str(name)
        with self._lock:
            series = self._timings.get(key)
            if series is None:
                series = _TimingSeries(self.sample_limit)
                self._timings[key] = series
            series.observe(elapsed_ns)

    def observe_since(self, name: str, started_ns: int) -> None:
        """Record elapsed time since a monotonic nanosecond timestamp.

        Args:
            name: The timing metric key.
            started_ns: The start timestamp from time.perf_counter_ns().
        """
        self.observe_ns(name, time.perf_counter_ns() - int(started_ns))

    def increment(self, name: str, amount: int = 1) -> None:
        """Increment a named counter by the given amount.

        Args:
            name: The counter key.
            amount: The integer amount to add (default 1).
        """
        with self._lock:
            self._counters[str(name)] += int(amount)

    def set_gauge(self, name: str, value: Any) -> None:
        """Set a named gauge to an arbitrary value.

        Args:
            name: The gauge key.
            value: The value to store.
        """
        with self._lock:
            self._gauges[str(name)] = value

    def snapshot(self) -> dict[str, Any]:
        """Produce a complete point-in-time snapshot of all metrics.

        Returns:
            A dictionary with elapsed_seconds, sample_limit, timings (per-key
            statistical summaries), counters, and gauges.
        """
        with self._lock:
            elapsed_ns = max(0, time.perf_counter_ns() - self._started_ns)
            return {
                "elapsed_seconds": round(elapsed_ns / 1_000_000_000.0, 3),
                "sample_limit": self.sample_limit,
                "timings": {
                    name: series.snapshot()
                    for name, series in sorted(self._timings.items())
                },
                "counters": dict(sorted(self._counters.items())),
                "gauges": dict(sorted(self._gauges.items())),
            }


# retained (P7-1): no in-repo callers; public snapshot-merge utility API surface.
def merge_performance_snapshots(
    *snapshots: tuple[Any, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return named snapshots unchanged while filtering absent values.

    Args:
        *snapshots: Pairs of (name, snapshot_dict) to merge.

    Returns:
        A dictionary mapping names to their non-empty snapshot dictionaries.
    """
    return {
        str(name): report
        for name, report in snapshots
        if isinstance(report, dict) and report
    }


def concise_performance_summary(report: dict[str, Any] | None) -> str:
    """Format high-signal run metrics without exposing task content or paths.

    Produces a compact human-readable summary of key timing percentiles and
    notable counters from a performance snapshot.

    Args:
        report: A performance snapshot dictionary, or None.

    Returns:
        A semicolon-separated summary string in Chinese, or a placeholder
        message if no data is available.
    """
    if not isinstance(report, dict):
        return "暂无性能数据"
    timings = report.get("timings", {})
    counters = report.get("counters", {})
    parts = [f"运行 {float(report.get('elapsed_seconds', 0.0)):.1f}s"]
    for key, label in (
        ("vision.search", "识别"),
        ("screenshot.total", "截图"),
        ("match.native", "DLL匹配"),
        ("match.opencv", "OpenCV匹配"),
        ("action.total", "步骤执行"),
        ("ui.log_append", "日志刷新"),
    ):
        metric = timings.get(key)
        if metric and metric.get("count", 0):
            parts.append(
                f"{label} P50/P95 {metric['p50_ms']:.1f}/{metric['p95_ms']:.1f}ms"
            )
    fallback_count = int(counters.get("screenshot.fallbacks", 0))
    if fallback_count:
        parts.append(f"截图回退 {fallback_count} 次")
    return "；".join(parts)
