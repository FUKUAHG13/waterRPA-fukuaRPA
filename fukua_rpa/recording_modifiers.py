"""Left/right modifier-key normalization for raw capture.

Win32 low-level keyboard hooks report *physical* VK codes: left shift is
0xA0, right shift 0xA1 (and similarly for ctrl/alt/win), while the generic
VK_SHIFT/CTRL/ALT (0x10/0x11/0x12) may also appear. The playback backend only
understands logical names (``shift``/``ctrl``/``alt``/``win``).

If we naively emitted a logical ``down`` for each physical press and an ``up``
for each physical release, holding left+right shift together would produce
``down, down, up, up`` — playback would release ``shift`` on the *first* up
while the other physical side is still held. This tracker implements the
required semantics: emit ``down`` on the first physical press of a logical
key and ``up`` only when its last physical source releases.

Pure Python, no clock, no OS calls — directly unit-testable with the real
Win32 VK codes.
"""
from __future__ import annotations

# Physical VK -> logical name. Generic VKs are included because some drivers
# report them even for a specific side.
_MODIFIER_VK_TO_NAME: dict[int, str] = {
    0x10: "shift", 0xA0: "shift", 0xA1: "shift",
    0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
    0x12: "alt", 0xA4: "alt", 0xA5: "alt",
    0x5B: "win", 0x5C: "win",
}


def raw_modifier_name(vk: int) -> str | None:
    """Return the logical modifier name for a physical VK, or None."""
    return _MODIFIER_VK_TO_NAME.get(int(vk))


class RawModifierTracker:
    """Emit first-down / last-up logical modifier events from physical VKs."""

    def __init__(self) -> None:
        # logical name -> set of physical VKs currently held
        self._sources: dict[str, set[int]] = {}

    def reset(self) -> None:
        self._sources.clear()

    def press(self, vk: int) -> tuple[str, str] | None:
        """Register a physical key-down.

        Returns ``(name, "down")`` on the first physical press of a logical
        key, ``(name, "repeat")`` on an auto-repeat of an already-held side,
        or None for a non-modifier VK.
        """
        name = raw_modifier_name(vk)
        if name is None:
            return None
        sources = self._sources.setdefault(name, set())
        if int(vk) in sources:
            # 同一物理侧的自动重复
            return (name, "repeat")
        if sources:
            # 逻辑键已被另一侧按住：只登记新的物理来源，不再发 down。
            # 这样序列是干净的「首次 down … 最后 up」，回放不会重复按下。
            sources.add(int(vk))
            return None
        sources.add(int(vk))
        return (name, "down")

    def release(self, vk: int) -> tuple[str, str] | None:
        """Register a physical key-up.

        Returns ``(name, "up")`` only when the last physical source of a
        logical key releases (so releasing one side while the other is held
        does not prematurely release the logical key).
        """
        name = raw_modifier_name(vk)
        if name is None:
            return None
        sources = self._sources.get(name)
        if not sources or int(vk) not in sources:
            # Unmatched up (e.g. recording started while already held): ignore
            # rather than emit a spurious release.
            return None
        sources.discard(int(vk))
        if not sources:
            self._sources.pop(name, None)
            return (name, "up")
        return None

    def active_logical_keys(self) -> frozenset[str]:
        return frozenset(self._sources)
