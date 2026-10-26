# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Bounded, session-local scale learning for repeated template searches."""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fukua_rpa.constants import SCALE_MAX, SCALE_MIN

SCALE_MEMORY_TIERS = ("conservative", "balanced", "aggressive")
MAX_MANUAL_SCALES = 12
MIN_HISTORY_LIMIT = 8
MAX_HISTORY_LIMIT = 512
MAX_LEARNED_PREFERRED = 12


def normalize_scale(value: object) -> float:
    """Coerce *value* to a finite scale within ``SCALE_MIN``/``SCALE_MAX``.

    Args:
        value: Raw scale, convertible to float.

    Returns:
        The scale rounded to 8 decimal places.

    Raises:
        ValueError: If *value* is non-finite or outside the supported range.
    """
    scale = float(value)
    if not math.isfinite(scale) or not SCALE_MIN <= scale <= SCALE_MAX:  # constants.SCALE_MIN/MAX
        raise ValueError(f"缩放倍率必须是 {SCALE_MIN:g} 到 {SCALE_MAX:g} 之间的有限数字")
    return float(round(scale, 8))


def format_scale(value: object) -> str:
    """Format *value* as a compact decimal string (trailing zeros stripped)."""
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return text or "0"


def parse_manual_scales(
    value: object, *, maximum: int = MAX_MANUAL_SCALES
) -> tuple[float, ...]:
    """Parse a manual preferred-scale list into normalized unique scales.

    Args:
        value: Either an iterable of scales or a delimited string.
        maximum: Maximum number of scales permitted.

    Returns:
        De-duplicated normalized scales in first-seen order.

    Raises:
        ValueError: If any entry is invalid or the count exceeds *maximum*.
    """
    if isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        text = str(value or "").strip()
        if not text:
            return ()
        parts = [part for part in re.split(r"[\s,，;；]+", text) if part]
    result = []
    for part in parts:
        scale = normalize_scale(part)
        if scale not in result:
            result.append(scale)
        if len(result) > int(maximum):
            raise ValueError(f"手动优先倍率最多允许 {maximum} 个")
    return tuple(result)


def format_manual_scales(scales: Iterable[float]) -> str:
    """Join *scales* into a comma-separated compact decimal string."""
    return ", ".join(format_scale(scale) for scale in scales)


@dataclass(frozen=True)
class ScaleMemoryPolicy:
    """User-facing policy that governs scale learning behavior."""

    enabled: bool = True
    tier: str = "balanced"
    manual_scales: tuple[float, ...] = ()
    custom_enabled: bool = False
    preferred_limit: int = 3
    history_limit: int = 64

    def normalized(self) -> ScaleMemoryPolicy:
        """Return a copy with tier/limits clamped and manual scales parsed."""
        tier = self.tier if self.tier in SCALE_MEMORY_TIERS else "balanced"
        manual = parse_manual_scales(self.manual_scales)
        preferred_limit = max(
            1, min(MAX_LEARNED_PREFERRED, int(self.preferred_limit))
        )
        history_limit = max(
            MIN_HISTORY_LIMIT,
            min(MAX_HISTORY_LIMIT, int(self.history_limit)),
        )
        return ScaleMemoryPolicy(
            enabled=bool(self.enabled),
            tier=tier,
            manual_scales=manual,
            custom_enabled=bool(self.custom_enabled),
            preferred_limit=preferred_limit,
            history_limit=history_limit,
        )


@dataclass
class ScaleMemorySummary:
    """Read-only snapshot of learned scale state for one template."""

    label: str
    preferred_scales: tuple[float, ...]
    manual_scales: tuple[float, ...]
    learned_scales: tuple[float, ...]
    observed_count: int
    unique_count: int
    history_limit: int

    @property
    def signature(self) -> tuple[tuple[float, ...], int]:
        """Identity used to detect changes worth logging."""
        return (
            self.preferred_scales,
            self.history_limit,
        )

    def status_text(self) -> str:
        """Render a localized human-readable status line."""
        preferred = format_manual_scales(self.preferred_scales) or "暂无"
        return (
            f"{self.label}：已记录 {self.observed_count} 次、"
            f"{self.unique_count} 个倍率；当前优先 {len(self.preferred_scales)} 个 "
            f"[{preferred}]；历史上限 {self.history_limit}"
        )


@dataclass
class _ScaleMemoryEntry:
    label: str
    valid_scales: tuple[float, ...]
    generation: tuple | None = None
    history: list[tuple[float, float, int]] = field(default_factory=list)
    updated_sequence: int = 0
    last_logged_signature: tuple | None = None


class ScaleMemoryStore:
    """Thread-safe learning cache that intentionally lives only for this process.

    DESIGN: learned history is not profile state and is never persisted across
    application restarts. ``export_state`` exists only to hand the current
    session to an isolated Runner and back. See ADR 0018.
    """

    def __init__(self, max_entries: int = 256) -> None:
        self.max_entries = max(1, int(max_entries))
        self._entries: dict[tuple, _ScaleMemoryEntry] = {}
        self._sequence = 0
        self._lock = threading.RLock()

    @staticmethod
    def _snap_manual_scales(
        manual_scales: Iterable[float], valid_scales: Iterable[float]
    ) -> tuple[float, ...]:
        valid = tuple(sorted({normalize_scale(scale) for scale in valid_scales}))
        if not valid:
            return ()
        gaps = [b - a for a, b in zip(valid, valid[1:], strict=False) if b > a]
        tolerance = max(1e-7, (min(gaps) * 0.11) if gaps else 1e-7)
        snapped = []
        for requested in manual_scales:
            nearest = min(valid, key=lambda scale: abs(scale - requested))
            if abs(nearest - requested) <= tolerance and nearest not in snapped:
                snapped.append(nearest)
        return tuple(snapped)

    @staticmethod
    def _effective_history_limit(
        entry: _ScaleMemoryEntry, policy: ScaleMemoryPolicy
    ) -> int:
        if policy.custom_enabled:
            return policy.history_limit
        scale_count = max(1, len(entry.valid_scales))
        unique_count = len({item[0] for item in entry.history})
        maturity = min(len(entry.history) // 4, scale_count * 2)
        base = 10 + scale_count * 2 + unique_count * 4 + maturity
        factor = {
            "conservative": 0.68,
            "balanced": 1.0,
            "aggressive": 1.48,
        }[policy.tier]
        return max(
            MIN_HISTORY_LIMIT,
            min(256, int(round(base * factor))),
        )

    @staticmethod
    def _ranked_scales(
        entry: _ScaleMemoryEntry,
    ) -> tuple[list[float], dict[float, float]]:
        scores: dict[float, float] = {}
        last_seen: dict[float, int] = {}
        for age, (scale, score, sequence) in enumerate(reversed(entry.history)):
            recency = math.pow(0.985, age)
            quality = 0.75 + 0.25 * max(0.0, min(1.0, float(score)))
            scores[scale] = scores.get(scale, 0.0) + recency * quality
            last_seen[scale] = max(last_seen.get(scale, 0), sequence)
        return sorted(
            scores,
            key=lambda scale: (-scores[scale], -last_seen[scale], scale),
        ), scores

    @staticmethod
    def _auto_preferred_limit(
        entry: _ScaleMemoryEntry,
        policy: ScaleMemoryPolicy,
        ranked: list[float],
        weights: dict[float, float],
    ) -> int:
        if not ranked:
            return 0
        if policy.custom_enabled:
            return min(policy.preferred_limit, len(ranked))
        coverage = {
            "conservative": 0.68,
            "balanced": 0.84,
            "aggressive": 0.95,
        }[policy.tier]
        tendency = {
            "conservative": 0.7,
            "balanced": 1.0,
            "aggressive": 1.45,
        }[policy.tier]
        dynamic_cap = max(
            1,
            min(
                MAX_LEARNED_PREFERRED,
                int(math.ceil(math.sqrt(max(1, len(entry.valid_scales))) * tendency)),
            ),
        )
        total = sum(weights[scale] for scale in ranked)
        running = 0.0
        selected = 0
        for scale in ranked:
            running += weights[scale]
            selected += 1
            if total <= 0.0 or running / total >= coverage:
                break
        return min(max(1, selected), dynamic_cap, len(ranked))

    def _entry(
        self,
        key: Any,
        label: object,
        valid_scales: Iterable[float],
        generation: tuple[int, ...] | None = None,
    ) -> _ScaleMemoryEntry:
        valid = tuple(sorted({normalize_scale(scale) for scale in valid_scales}))
        entry = self._entries.get(key)
        if entry is None:
            entry = _ScaleMemoryEntry(
                str(label or "图片"),
                valid,
                generation=generation,
            )
            self._entries[key] = entry
        else:
            entry.label = str(label or entry.label)
            if (
                generation is not None
                and entry.generation is not None
                and generation != entry.generation
            ):
                # The path still identifies the same logical template, but the
                # file contents changed. Old scale observations are no longer
                # trustworthy for the replacement image.
                entry.history.clear()
                entry.last_logged_signature = None
            if generation is not None:
                entry.generation = generation
            if valid and entry.valid_scales != valid:
                entry.valid_scales = valid
                allowed = set(valid)
                entry.history = [item for item in entry.history if item[0] in allowed]
        return entry

    def _summary(
        self, entry: _ScaleMemoryEntry, policy: ScaleMemoryPolicy
    ) -> ScaleMemorySummary:
        policy = policy.normalized()
        history_limit = self._effective_history_limit(entry, policy)
        if len(entry.history) > history_limit:
            del entry.history[:-history_limit]
        ranked, weights = self._ranked_scales(entry)
        learned_limit = self._auto_preferred_limit(
            entry, policy, ranked, weights
        )
        learned = tuple(ranked[:learned_limit])
        manual = self._snap_manual_scales(
            policy.manual_scales, entry.valid_scales
        )
        preferred = list(manual)
        for scale in learned:
            if scale not in preferred:
                preferred.append(scale)
        return ScaleMemorySummary(
            label=entry.label,
            preferred_scales=tuple(preferred),
            manual_scales=manual,
            learned_scales=learned,
            observed_count=len(entry.history),
            unique_count=len({item[0] for item in entry.history}),
            history_limit=history_limit,
        )

    def preferred_scales(
        self,
        key: Any,
        label: object,
        valid_scales: Iterable[float],
        policy: ScaleMemoryPolicy,
        *,
        generation: tuple[int, ...] | None = None,
    ) -> tuple[tuple[float, ...], ScaleMemorySummary | None]:
        """Return preferred-scale ordering hints for a template key.

        Args:
            key: Hashable identity of the template (e.g. path-based tuple).
            label: Human-readable template label for status reporting.
            valid_scales: The configured scale range to constrain hints to.
            policy: Active scale-memory policy.
            generation: Optional content generation; a change clears history.

        Returns:
            A ``(preferred_scales, summary)`` tuple. Both are empty/None when
            the policy is disabled.
        """
        policy = policy.normalized()
        if not policy.enabled:
            return (), None
        with self._lock:
            entry = self._entry(
                key,
                label,
                valid_scales,
                generation=generation,
            )
            # REVIEW-20260913-001 FR-003: 只读访问也是一次"使用"。此前只读路径
            # 既不刷新 updated_sequence 也不回收条目，导致 _entries 可突破
            # max_entries，且这些 sequence 恒为 0 的条目会在任意一次 record 触发的
            # 回收中被集体优先淘汰（既非 LRU 也非预期语义）。
            self._sequence += 1
            entry.updated_sequence = self._sequence
            summary = self._summary(entry, policy)
            self._evict_old_entries()
            # DESIGN: these are ordering hints, not a reduced search range. The
            # recognition backend must fall back to every configured scale.
            return summary.preferred_scales, summary

    def record(
        self,
        key: Any,
        label: object,
        valid_scales: Iterable[float],
        scale: float,
        score: float,
        policy: ScaleMemoryPolicy,
        *,
        generation: tuple[int, ...] | None = None,
    ) -> tuple[ScaleMemorySummary | None, bool]:
        """Record one observed scale/score outcome for a template key.

        Args:
            key: Hashable identity of the template.
            label: Human-readable template label for status reporting.
            valid_scales: The configured scale range; out-of-range scales are
                ignored.
            scale: The scale that produced the match.
            score: The match quality score in ``[0, 1]``.
            policy: Active scale-memory policy.
            generation: Optional content generation; a change clears history.

        Returns:
            A ``(summary, changed)`` tuple. ``summary`` is None when the policy
            is disabled; ``changed`` is True when the preferred-scale signature
            changed and should be logged.
        """
        policy = policy.normalized()
        if not policy.enabled:
            return None, False
        scale = normalize_scale(scale)
        with self._lock:
            entry = self._entry(
                key,
                label,
                valid_scales,
                generation=generation,
            )
            if scale not in entry.valid_scales:
                return self._summary(entry, policy), False
            self._sequence += 1
            entry.updated_sequence = self._sequence
            entry.history.append((scale, float(score), self._sequence))
            summary = self._summary(entry, policy)
            changed = summary.signature != entry.last_logged_signature
            if changed:
                entry.last_logged_signature = summary.signature
            self._evict_old_entries()
            return summary, changed

    def _evict_old_entries(self) -> None:
        while len(self._entries) > self.max_entries:
            # O(n) scan is acceptable: max_entries <= 256, and eviction only runs
            # when the store is actually over the cap (called after both record
            # and read-only preferred_scales lookups).
            oldest_key = min(
                self._entries,
                key=lambda key: self._entries[key].updated_sequence,
            )
            del self._entries[oldest_key]

    def summaries(
        self, policy: ScaleMemoryPolicy, maximum: int = 8
    ) -> list[ScaleMemorySummary]:
        """Return up to *maximum* most-recently-updated template summaries."""
        policy = policy.normalized()
        with self._lock:
            entries = sorted(
                self._entries.values(),
                key=lambda entry: entry.updated_sequence,
                reverse=True,
            )
            return [
                self._summary(entry, policy)
                for entry in entries[: max(1, int(maximum))]
            ]

    def clear(self) -> None:
        """Drop all learned entries and reset the update sequence."""
        with self._lock:
            self._entries.clear()
            self._sequence = 0

    def export_state(self) -> dict[str, Any]:
        """Return a bounded JSON-safe snapshot for an isolated Runner."""

        with self._lock:
            entries = []
            for key, entry in sorted(
                self._entries.items(),
                key=lambda item: item[1].updated_sequence,
                reverse=True,
            )[: self.max_entries]:
                entries.append(
                    {
                        "key": list(key) if isinstance(key, tuple) else [key],
                        "label": entry.label,
                        "valid_scales": list(entry.valid_scales),
                        "generation": (
                            list(entry.generation)
                            if isinstance(entry.generation, tuple)
                            else None
                        ),
                        "history": [list(item) for item in entry.history],
                        "updated_sequence": int(entry.updated_sequence),
                    }
                )
            return {
                "format": "fukuaRPA_scale_memory",
                "version": 1,
                "sequence": int(self._sequence),
                "entries": entries,
            }

    def import_state(self, payload: Mapping[str, Any] | None) -> bool:
        """Replace session state from a validated Runner snapshot."""

        if not isinstance(payload, Mapping):
            return False
        if payload.get("format") != "fukuaRPA_scale_memory":
            return False
        if payload.get("version") != 1:
            return False
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            return False
        try:
            sequence = max(0, int(payload.get("sequence", 0)))
            parsed: dict[tuple, _ScaleMemoryEntry] = {}
            for raw in raw_entries[: self.max_entries]:
                if not isinstance(raw, Mapping):
                    raise ValueError
                raw_key = raw.get("key")
                if not isinstance(raw_key, list) or not raw_key:
                    raise ValueError
                key = tuple(self._freeze_state_value(value) for value in raw_key)
                valid_scales = tuple(
                    sorted(
                        {
                            normalize_scale(value)
                            for value in list(raw.get("valid_scales") or [])
                        }
                    )
                )
                generation_raw = raw.get("generation")
                generation = (
                    tuple(int(value) for value in generation_raw)
                    if isinstance(generation_raw, list)
                    else None
                )
                history = []
                for history_item in list(raw.get("history") or [])[
                    -MAX_HISTORY_LIMIT:
                ]:
                    if not isinstance(history_item, list) or len(history_item) != 3:
                        raise ValueError
                    scale = normalize_scale(history_item[0])
                    score = float(history_item[1])
                    item_sequence = max(0, int(history_item[2]))
                    if not math.isfinite(score):
                        raise ValueError
                    if scale in valid_scales:
                        history.append((scale, score, item_sequence))
                parsed[key] = _ScaleMemoryEntry(
                    label=str(raw.get("label") or "图片")[:260],
                    valid_scales=valid_scales,
                    generation=generation,
                    history=history,
                    updated_sequence=max(0, int(raw.get("updated_sequence", 0))),
                )
        except (TypeError, ValueError, OverflowError):
            return False

        with self._lock:
            self._entries = parsed
            self._sequence = max(
                sequence,
                max(
                    (entry.updated_sequence for entry in parsed.values()),
                    default=0,
                ),
            )
        return True

    @staticmethod
    def _freeze_state_value(value: object) -> object:
        if isinstance(value, list):
            return tuple(ScaleMemoryStore._freeze_state_value(item) for item in value)
        if isinstance(value, (str, bool, int, float)) or value is None:
            return value
        raise ValueError("缩放记忆键包含无效类型")
