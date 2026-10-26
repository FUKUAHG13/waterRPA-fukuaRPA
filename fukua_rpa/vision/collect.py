"""Pure multi-target collection model shared by the image-click action.

The image click action can gather targets over several frames before clicking:
a *first-hit* buffer gives late-appearing targets a chance to show up, and a
*last-hit* buffer ends collection once no new target appears. This module holds
the policy and the bounded cross-frame candidate registry so the timing and
counting rules are unit-testable without screenshots or Qt.

`docs/设计基准.md` describes the double-buffer semantics; a single frame's
deduplicated result comes from ``vision/match.py`` and is fed to
``MultiTargetCollector.add_frame``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

# Location tuples are ``(x, y, scale, score, matched_width, matched_height)``.
_DEFAULT_TOLERANCE_PX = 12.0
_TOLERANCE_RATIO = 0.5
_MAX_CANDIDATES = 512


@dataclass(frozen=True)
class CollectionPolicy:
    """Global defaults for the image-click collection stage.

    All buffers default to 0 so an upgraded profile keeps the previous
    immediate-click behaviour until the user opts in. ``max_collect_s > 0`` is a
    hard deadline measured from the *first* effective hit; 0 disables the cap.
    """

    first_hit_buffer_s: float = 0.0
    last_hit_buffer_s: float = 0.0
    # 0 = 不设最长收集时间（由单步超时约束；单步超时也为 0 时完全不设限）。
    max_collect_s: float = 0.0
    max_targets: int = 0
    allow_partial: bool = True

    @property
    def enabled(self) -> bool:
        """True when collection changes behaviour at all."""

        if self.max_targets > 0:
            return True
        return self.first_hit_buffer_s > 0 or self.last_hit_buffer_s > 0

    @property
    def waits_for_count(self) -> bool:
        """True when a finite N must be reached instead of clicking partial."""

        return self.max_targets > 0 and not self.allow_partial


@dataclass
class _Candidate:
    x: float
    y: float
    size: float
    hits: int = 1


@dataclass
class MultiTargetCollector:
    """Bounded cross-frame registry enforcing the double buffer and target count.

    INVARIANT: the registry only tracks *identity* (whether a target is new); the
    coordinates actually clicked always come from the latest frame
    (``latest_locations``), so a vanished target is never clicked at a stale
    position.
    """

    policy: CollectionPolicy
    started_at: float
    _candidates: list[_Candidate] = field(default_factory=list)
    _first_hit: float | None = None
    _last_new: float | None = None
    latest_locations: list[Sequence[float]] = field(default_factory=list)
    latest_new_count: int = 0

    @property
    def count(self) -> int:
        """Total distinct targets observed across frames (bounded)."""

        return len(self._candidates)

    @property
    def available_count(self) -> int:
        """Targets visible in the latest frame (the ones filtering may drop)."""

        return len(self.latest_locations)

    @property
    def first_hit_at(self) -> float | None:
        return self._first_hit

    @property
    def last_new_at(self) -> float | None:
        return self._last_new

    def _tolerance(self, size: float) -> float:
        if size <= 0:
            return _DEFAULT_TOLERANCE_PX
        return max(_DEFAULT_TOLERANCE_PX, _TOLERANCE_RATIO * size)

    @staticmethod
    def _location_size(location: Sequence[float]) -> float:
        if len(location) >= 6:
            try:
                return max(float(location[4]), float(location[5]))
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def _register(self, x: float, y: float, size: float) -> bool:
        """Return True when (x, y) is a target not seen before."""

        tolerance = self._tolerance(size)
        best_index = -1
        best_distance = None
        for index, candidate in enumerate(self._candidates):
            dx = x - candidate.x
            dy = y - candidate.y
            reach = max(tolerance, self._tolerance(candidate.size))
            distance = (dx * dx + dy * dy) ** 0.5
            if distance > reach:
                continue
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_index = index
        if best_index < 0:
            if len(self._candidates) < _MAX_CANDIDATES:
                self._candidates.append(_Candidate(x, y, size))
                return True
            # FR-20261011-003：候选表已满时不得把溢出目标当成“新目标”。此前该
            # 分支仍返回 True，于是每帧固定的溢出目标都会刷新末位缓冲，
            # `should_finish()` 永假；默认 `max_collect_s=0`（不设最长收集时间）
            # 时收集只能靠单步超时或急停结束。溢出目标按“已见”处理，收集仍由
            # 双缓冲、数量规则与单步超时正常收敛（设计基准：结束收集只表示配置
            # 时间内没有新增目标；目标数量与收集上限必须有界）。
            return False
        # Keep the newest geometry but never double-count the same target.
        candidate = self._candidates[best_index]
        candidate.x = x
        candidate.y = y
        candidate.size = size
        candidate.hits += 1
        return False

    def add_frame(
        self, locations: Iterable[Sequence[float]], now: float
    ) -> int:
        """Feed one frame's deduplicated targets; return how many were new.

        Identity is judged by position and size with tolerance, so a target that
        keeps being recognised — even at slightly different coordinates or while
        its confidence drifts — does not reset the last-hit timer.
        """

        self.latest_locations = [tuple(loc) for loc in locations]
        new_count = 0
        for location in self.latest_locations:
            try:
                x = float(location[0])
                y = float(location[1])
            except (IndexError, TypeError, ValueError):
                continue
            if self._register(x, y, self._location_size(location)):
                new_count += 1
        self.latest_new_count = new_count
        if new_count:
            if self._first_hit is None:
                self._first_hit = now
            self._last_new = now
        return new_count

    def required_quiet_time(self) -> float | None:
        """Time at which the enabled buffers are both satisfied."""

        if self._first_hit is None or self._last_new is None:
            return None
        return max(
            self._first_hit + max(0.0, self.policy.first_hit_buffer_s),
            self._last_new + max(0.0, self.policy.last_hit_buffer_s),
        )

    def expired(self, now: float) -> bool:
        """Whether the finite collection deadline has passed.

        ``max_collect_s <= 0`` disables the cap entirely: collection then ends
        only via the quiet buffers, the count rule with the step timeout, or a
        stop request (反馈 20261010-4：默认不设限).
        """

        if self._first_hit is None:
            return False
        if self.policy.max_collect_s <= 0:
            return False
        return now >= self._first_hit + self.policy.max_collect_s

    def count_satisfied(self) -> bool:
        """Whether the required target count rule is met (or not required).

        The rule counts targets *currently visible*, never the larger historical
        registry, so already-filtered or vanished candidates cannot be used to
        pretend a finite N was reached.
        """

        if not self.policy.waits_for_count:
            return True
        return self.available_count >= self.policy.max_targets

    def should_finish(self, now: float) -> bool:
        """Whether collection may end: quiet buffers satisfied and count ok.

        A finite N without the "click what exists" option keeps waiting until N
        is reached, the deadline expires, or the step times out — it never
        quietly clicks fewer targets and reports success.

        ``max_collect_s > 0`` is a *hard* cap: once it expires this returns True
        for any satisfied count, so a scene that keeps producing new targets can
        never extend the wait indefinitely (the caller still turns an unsatisfied
        count into the ordinary timeout branch). With the cap disabled (0) only
        the quiet buffers, the count rule and the step timeout end collection.

        A frame with no usable target does not end collection by itself: only the
        quiet buffers, the count rule and the deadline control it, per the
        requirement that a single empty frame must not be treated as a first
        search failure. The caller decides what to do when the final frame is
        empty (it must not click stale coordinates or report success).
        """

        if self._first_hit is None:
            # 从未命中过目标：不启动收集截止，交回外层搜索/重试语义。
            return False
        if self.expired(now):
            return self.count_satisfied()
        if not self.count_satisfied():
            return False
        quiet = self.required_quiet_time()
        if quiet is None:
            return False
        return now >= quiet
