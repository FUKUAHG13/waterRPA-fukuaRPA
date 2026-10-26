# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Low-cost scene-change fingerprints used while recognition is backed off."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass

from PIL import Image

SCENE_WAKE_SENSITIVITIES = ("conservative", "balanced", "sensitive")


@dataclass(frozen=True)
class SceneSignature:
    """Compact grayscale fingerprint of a recognition region."""

    width: int
    height: int
    pixels: bytes


@dataclass(frozen=True)
class SceneChange:
    """Outcome of comparing two scene signatures."""

    changed: bool
    peak_percent: float
    top_percent: float
    global_percent: float


_THRESHOLDS = {
    "conservative": (8.0, 1.6, 0.25),
    "balanced": (4.0, 0.8, 0.10),
    "sensitive": (0.4, 0.2, 0.015),
}


def normalize_sensitivity(value: object) -> str:
    """Coerce a config value to a valid scene-wake sensitivity name.

    Args:
        value: Raw sensitivity setting; may be None or any string-like object.

    Returns:
        One of ``SCENE_WAKE_SENSITIVITIES``; unrecognized values become
        ``"balanced"``.
    """
    text = str(value or "balanced").strip().lower()
    return text if text in SCENE_WAKE_SENSITIVITIES else "balanced"


def make_scene_signature(
    image: Image.Image | None, *, max_width: int = 160, max_height: int = 96
) -> SceneSignature | None:
    """Create a small grayscale fingerprint without importing NumPy/OpenCV.

    DESIGN: scene wake is a cheap scheduling hint, not recognition. Keeping
    this path PIL-only avoids importing a large runtime merely to decide when
    the next real template search may start. See ADR 0020.

    Args:
        image: Source frame, or None to skip fingerprinting.
        max_width: Maximum fingerprint width in pixels (clamped to >= 8).
        max_height: Maximum fingerprint height in pixels (clamped to >= 8).

    Returns:
        A downscaled grayscale :class:`SceneSignature`, or None when the input
        is missing or degenerate.
    """
    if image is None:
        return None
    work = image.convert("L")
    work.thumbnail(
        (max(8, int(max_width)), max(8, int(max_height))),
        Image.Resampling.BILINEAR,
    )
    if work.width <= 0 or work.height <= 0:
        return None
    return SceneSignature(work.width, work.height, work.tobytes())


def compare_scene_signatures(
    before: SceneSignature | None,
    after: SceneSignature | None,
    *,
    sensitivity: str = "balanced",
    columns: int = 8,
    rows: int = 6,
) -> SceneChange:
    """Measure global and localized pixel change between two fingerprints.

    Args:
        before: Baseline fingerprint, or None when no baseline exists.
        after: Current fingerprint, or None when no fresh frame exists.
        sensitivity: One of ``SCENE_WAKE_SENSITIVITIES``.
        columns: Number of horizontal tiles used for localized scoring.
        rows: Number of vertical tiles used for localized scoring.

    Returns:
        A :class:`SceneChange` describing whether the scene changed and the
        peak/top/global change percentages.
    """
    if before is None or after is None:
        return SceneChange(False, 0.0, 0.0, 0.0)
    if before.width != after.width or before.height != after.height:
        return SceneChange(True, 100.0, 100.0, 100.0)
    pixel_count = before.width * before.height
    if pixel_count <= 0:
        return SceneChange(False, 0.0, 0.0, 0.0)
    if len(before.pixels) != len(after.pixels):
        return SceneChange(True, 100.0, 100.0, 100.0)

    columns = max(1, min(int(columns), before.width))
    rows = max(1, min(int(rows), before.height))
    width = before.width
    height = before.height

    # DESIGN: use NumPy only when another feature already imported it. Scene
    # wake must not turn an otherwise light startup path into an eager import.
    if "numpy" in sys.modules:
        import numpy as np

        a = np.frombuffer(before.pixels, dtype=np.uint8)
        b = np.frombuffer(after.pixels, dtype=np.uint8)
        diff = np.abs(a.astype(np.int16) - b.astype(np.int16))
        total = int(diff.sum())

        ys = np.arange(height)
        xs = np.arange(width)
        tile_ys = np.minimum(rows - 1, ys * rows // height)
        tile_xs = np.minimum(columns - 1, xs * columns // width)
        tile_indices = (tile_ys[:, None] * columns + tile_xs[None, :]).ravel()
        tile_sums = np.bincount(
            tile_indices, weights=diff, minlength=columns * rows
        ).tolist()
        tile_counts = np.bincount(
            tile_indices, minlength=columns * rows
        ).tolist()
    else:
        tile_sums = [0] * (columns * rows)
        tile_counts = [0] * (columns * rows)
        total = 0

        for y in range(height):
            tile_y = min(rows - 1, y * rows // height)
            row_offset = y * width
            for x in range(width):
                delta = abs(
                    before.pixels[row_offset + x] - after.pixels[row_offset + x]
                )
                total += delta
                tile_x = min(columns - 1, x * columns // width)
                tile_index = tile_y * columns + tile_x
                tile_sums[tile_index] += delta
                tile_counts[tile_index] += 1

    tile_percentages = sorted(
        (
            100.0 * value / (255.0 * max(1, tile_counts[index]))
            for index, value in enumerate(tile_sums)
        ),
        reverse=True,
    )
    peak_percent = tile_percentages[0] if tile_percentages else 0.0
    top_count = min(3, len(tile_percentages))
    top_percent = (
        sum(tile_percentages[:top_count]) / top_count if top_count else 0.0
    )
    global_percent = 100.0 * total / (255.0 * pixel_count)
    peak_threshold, top_threshold, global_threshold = _THRESHOLDS[
        normalize_sensitivity(sensitivity)
    ]
    changed = global_percent >= global_threshold or (
        peak_percent >= peak_threshold and top_percent >= top_threshold
    )
    return SceneChange(
        bool(changed),
        float(peak_percent),
        float(top_percent),
        float(global_percent),
    )


def compare_scene_sets(
    before: Iterable[SceneSignature | None],
    after: Iterable[SceneSignature | None],
    *,
    sensitivity: str = "balanced",
) -> SceneChange:
    """Return the strongest change among corresponding recognition regions.

    Args:
        before: Baseline signatures for each tracked region.
        after: Current signatures for each tracked region.
        sensitivity: One of ``SCENE_WAKE_SENSITIVITIES``.

    Returns:
        The strongest :class:`SceneChange` across all region pairs; a length
        mismatch is reported as a full change.
    """
    previous = tuple(before or ())
    current = tuple(after or ())
    if not previous or not current:
        return SceneChange(False, 0.0, 0.0, 0.0)
    if len(previous) != len(current):
        return SceneChange(True, 100.0, 100.0, 100.0)
    comparisons = [
        compare_scene_signatures(
            first,
            second,
            sensitivity=sensitivity,
        )
        for first, second in zip(previous, current, strict=True)
    ]
    strongest = max(
        comparisons,
        key=lambda result: (
            result.changed,
            result.global_percent,
            result.top_percent,
            result.peak_percent,
        ),
    )
    # INVARIANT: max() 已按 changed 优先排序，此分支仅在排序逻辑被修改时可达。
    # 保留为防御性断言，不可删除。
    if any(item.changed for item in comparisons) and not strongest.changed:
        strongest = next(item for item in comparisons if item.changed)
    return strongest
