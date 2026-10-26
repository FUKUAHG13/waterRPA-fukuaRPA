"""Bounded random distributions shared by timing and pointer jitter features.

The user-facing concentration scale is always 0..100:

* 0 is uniform across the configured range.
* larger values increasingly favour the centre while keeping the full range
  reachable.

Timing and spatial jitter deliberately share this scale so identical preset
labels have identical meaning, even though one result is one-dimensional and
the other is mapped into a rectangle or ellipse.
"""

from __future__ import annotations

import math
import random

CONCENTRATION_MIN = 0
CONCENTRATION_MAX = 100
CONCENTRATION_PRESETS: tuple[tuple[str, int], ...] = (
    ("均匀（0）", 0),
    ("自然（50）", 50),
    ("集中（80）", 80),
)


def normalize_concentration(value: object) -> int:
    """Return a finite integer concentration clamped to 0..100."""

    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(parsed):
        return 0
    return max(CONCENTRATION_MIN, min(CONCENTRATION_MAX, int(round(parsed))))


def concentration_alpha(value: object) -> float:
    """Map the public 0..100 scale to a symmetric beta-distribution shape."""

    ratio = normalize_concentration(value) / CONCENTRATION_MAX
    # A quadratic mapping leaves useful adjustment room near the uniform end.
    # alpha=1 is exactly uniform; alpha=10 remains random but strongly centred.
    return 1.0 + 9.0 * ratio * ratio


def sample_centered_unit(value: object, *, rng=None) -> float:
    """Sample from -1..1, increasingly concentrated around zero."""

    source = rng or random
    concentration = normalize_concentration(value)
    if concentration <= 0:
        return float(source.uniform(-1.0, 1.0))
    alpha = concentration_alpha(concentration)
    return float(source.betavariate(alpha, alpha) * 2.0 - 1.0)


def sample_interval_seconds(
    base_seconds: object,
    variation_seconds: object,
    concentration: object,
    *,
    rng=None,
) -> float:
    """Sample a non-negative interval inside ``base +/- variation``."""

    try:
        base = float(str(base_seconds))
    except (TypeError, ValueError):
        base = 0.0
    try:
        variation = float(str(variation_seconds))
    except (TypeError, ValueError):
        variation = 0.0
    if not math.isfinite(base):
        base = 0.0
    if not math.isfinite(variation):
        variation = 0.0
    base = max(0.0, base)
    variation = max(0.0, variation)
    sampled = base + variation * sample_centered_unit(concentration, rng=rng)
    return max(0.0, sampled)


def sample_pointer_offset(
    maximum_x: object,
    maximum_y: object,
    shape: object,
    concentration: object,
    *,
    rng=None,
) -> tuple[float, float]:
    """Sample an offset inside a centred rectangle or ellipse.

    ``maximum_x`` and ``maximum_y`` are horizontal/vertical half extents.  A
    zero extent degenerates the ellipse to a line without biasing the remaining
    axis.  Ellipse rejection sampling keeps concentration=0 uniform by area.
    """

    source = rng or random
    try:
        max_x = abs(float(str(maximum_x)))
    except (TypeError, ValueError):
        max_x = 0.0
    try:
        max_y = abs(float(str(maximum_y)))
    except (TypeError, ValueError):
        max_y = 0.0
    if not math.isfinite(max_x):
        max_x = 0.0
    if not math.isfinite(max_y):
        max_y = 0.0
    if max_x <= 0.0 and max_y <= 0.0:
        return 0.0, 0.0

    normalized_shape = str(shape or "rectangle").strip().lower()
    if normalized_shape != "ellipse":
        return (
            max_x * sample_centered_unit(concentration, rng=source),
            max_y * sample_centered_unit(concentration, rng=source),
        )

    if max_x <= 0.0:
        return 0.0, max_y * sample_centered_unit(concentration, rng=source)
    if max_y <= 0.0:
        return max_x * sample_centered_unit(concentration, rng=source), 0.0

    # Acceptance is at least pi/4 for the uniform case and rises as samples
    # concentrate, so the loop is cheap even for high-frequency clicking.
    for _attempt in range(64):
        unit_x = sample_centered_unit(concentration, rng=source)
        unit_y = sample_centered_unit(concentration, rng=source)
        if unit_x * unit_x + unit_y * unit_y <= 1.0:
            return max_x * unit_x, max_y * unit_y
    return 0.0, 0.0
