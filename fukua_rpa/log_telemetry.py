"""Readable, privacy-conscious formatting for detailed execution telemetry."""

from __future__ import annotations

import html
import json
from typing import Any

KNOWN_STEP_TIMING_KEYS = (
    "wait.recognition_base",
    "wait.recognition_adaptive",
    "wait.step_interval",
    "wake.scene_capture",
    "wake.scale_probe",
    "vision.search",
    "screenshot.total",
    "match.native",
    "match.opencv",
    "action.total",
    "action.mouse_click",
    "log.dispatch",
)


def _timing_total(report: dict[str, Any], name: str) -> float:
    """Extract the total_ms value for a named timing from a snapshot report.

    Args:
        report: A performance snapshot dictionary.
        name: The timing key to look up.

    Returns:
        The total milliseconds as a float, or 0.0 on any extraction failure.
    """
    try:
        return float(report.get("timings", {}).get(name, {}).get("total_ms", 0.0))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def performance_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """Return timing totals and counters added between two bounded snapshots.

    Computes the non-negative difference for each known timing key and each
    counter present in either snapshot.

    Args:
        before: The earlier performance snapshot (or None).
        after: The later performance snapshot (or None).

    Returns:
        A dictionary with ``timings_ms`` (name -> delta float) and ``counters``
        (name -> int difference) containing only non-zero entries.
    """
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    before_timings = before.get("timings", {})
    after_timings = after.get("timings", {})
    timing_names: set[str] = set(KNOWN_STEP_TIMING_KEYS)
    if isinstance(before_timings, dict):
        timing_names.update(str(name) for name in before_timings)
    if isinstance(after_timings, dict):
        timing_names.update(str(name) for name in after_timings)
    timings = {
        name: max(0.0, _timing_total(after, name) - _timing_total(before, name))
        for name in sorted(timing_names)
    }
    before_counters = before.get("counters", {})
    after_counters = after.get("counters", {})
    counters: dict[str, int] = {}
    for name in set(before_counters) | set(after_counters):
        try:
            difference = int(after_counters.get(name, 0)) - int(
                before_counters.get(name, 0)
            )
        except (TypeError, ValueError):
            continue
        if difference:
            counters[str(name)] = difference
    return {"timings_ms": timings, "counters": dict(sorted(counters.items()))}


def complete_step_timing_payload(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    total_ms: float,
    pre_execute_wait_ms: float,
    execute_ms: float,
) -> dict[str, Any]:
    """Build a structured timing breakdown for a single step execution.

    Combines top-level phase durations with nested timing deltas from the
    performance snapshot pair to produce a diagnostic payload.

    Args:
        before: Performance snapshot taken before step execution.
        after: Performance snapshot taken after step execution.
        total_ms: Total elapsed time for the step in milliseconds.
        pre_execute_wait_ms: Time spent in pre-execution waits.
        execute_ms: Time spent in the execute call itself.

    Returns:
        A dictionary containing total, top-level, nested-phase, and
        counter-change breakdowns suitable for telemetry logging.
    """
    delta = performance_delta(before, after)
    timings = delta["timings_ms"]
    accounted = max(0.0, float(pre_execute_wait_ms)) + max(
        0.0, float(execute_ms)
    )
    return {
        "total_ms": round(max(0.0, float(total_ms)), 3),
        "top_level": {
            "pre_execute_wait_ms": round(max(0.0, float(pre_execute_wait_ms)), 3),
            "execute_call_ms": round(max(0.0, float(execute_ms)), 3),
            "unattributed_ms": round(
                max(0.0, float(total_ms) - accounted), 3
            ),
        },
        "nested_phases_ms": {
            "all_recognition_waits": round(
                timings["wait.recognition_base"]
                + timings["wait.recognition_adaptive"],
                3,
            ),
            "base_recognition_wait": round(
                timings["wait.recognition_base"], 3
            ),
            "adaptive_recognition_wait": round(
                timings["wait.recognition_adaptive"], 3
            ),
            "step_interval": round(timings["wait.step_interval"], 3),
            "scene_fingerprint": round(timings["wake.scene_capture"], 3),
            "wake_scale_probe": round(timings["wake.scale_probe"], 3),
            "vision_search": round(timings["vision.search"], 3),
            "screenshot": round(timings["screenshot.total"], 3),
            "native_match": round(timings["match.native"], 3),
            "opencv_match": round(timings["match.opencv"], 3),
            "action_container": round(timings["action.total"], 3),
            "mouse_click": round(timings["action.mouse_click"], 3),
            "log_enqueue": round(timings["log.dispatch"], 3),
        },
        "all_timing_changes_ms": {
            name: round(value, 3)
            for name, value in timings.items()
            if value > 0.0
        },
        "counter_changes": delta["counters"],
        "note": "nested_phases 和 all_timing_changes 是包含关系的诊断值，不能直接相加",
    }


def format_complete_payload(label: str, payload: dict[str, Any]) -> str:
    """Format a telemetry payload as an HTML-escaped, styled log block.

    Serializes the payload as indented JSON wrapped in a colored HTML label
    and a ``<pre>`` block, safe for display in rich-text log views.

    Args:
        label: A short category label shown as the block header.
        payload: The dictionary payload to serialize.

    Returns:
        An HTML string with the escaped label and formatted JSON body.
    """
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    )
    return (
        "<font color='#607D8B'><b>"
        f"[{html.escape(str(label))}]</b></font>"
        f"<pre>{html.escape(serialized)}</pre>"
    )
