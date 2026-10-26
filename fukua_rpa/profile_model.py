"""Qt-independent ordered profile collection used by the main window."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .config_schema import default_profile_config, migrate_profiles
from .constants import MAX_PROFILE_NAME_LENGTH, MAX_PROFILES


def normalize_profile_name(name: str) -> str:
    """Validate and normalize a profile name.

    Strips leading/trailing whitespace and rejects empty names or names
    exceeding MAX_PROFILE_NAME_LENGTH.

    Args:
        name: Raw profile name to validate.

    Returns:
        The trimmed, validated profile name.

    Raises:
        ValueError: If the name is empty or too long.
    """
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("方案名称不能为空")
    if len(normalized) > MAX_PROFILE_NAME_LENGTH:
        raise ValueError(f"方案名称最多允许 {MAX_PROFILE_NAME_LENGTH} 个字符")
    return normalized


@dataclass
class ProfileCollection:
    """Ordered, Qt-independent collection of profile configurations.

    Maintains insertion order, a current-profile pointer, and stable
    profile IDs that survive renames. All returned configs are deep copies
    to prevent accidental mutation of internal state.
    """

    profiles: dict[str, dict[str, Any]] = field(default_factory=lambda: {})
    current_name: str = "默认方案"

    def replace(self, profiles: Mapping[str, Any], current_name: str | None = None) -> bool:
        """Replace the entire collection with migrated profiles.

        Args:
            profiles: Raw mapping of profile names to config dicts.
            current_name: Optional profile name to select after replacement.

        Returns:
            True if any migration or normalization changed the data.
        """
        migrated, changed = migrate_profiles(profiles)
        if not migrated:
            migrated = {"默认方案": default_profile_config()}
            changed = True
        self.profiles = migrated
        requested = str(current_name or "")
        self.current_name = requested if requested in migrated else next(iter(migrated))
        self._ensure_ids()
        return changed

    def _ensure_ids(self) -> None:
        """Backfill a stable ``profile_id`` for every profile (design §4.2).

        The id lives *inside* the profile dict so it survives renames (which only
        move the dict under a new key). Scheduled tasks reference profiles by
        this id, so renaming a profile never orphans its schedules.
        """
        seen: set[str] = set()
        for config in self.profiles.values():
            existing = str(config.get("profile_id", "") or "").strip()
            if existing and existing not in seen:
                seen.add(existing)
                continue
            candidate = uuid.uuid4().hex
            while candidate in seen:
                candidate = uuid.uuid4().hex
            seen.add(candidate)
            config["profile_id"] = candidate

    def profile_id_of(self, name: str) -> str:
        """Return the stable profile ID for a given profile name.

        Args:
            name: Profile name to look up.

        Returns:
            The profile ID string, or empty string if not found.
        """
        profile = self.profiles.get(name)
        return str(profile.get("profile_id", "")) if profile else ""

    def find_by_id(self, profile_id: str) -> str | None:
        """Return the profile name for a stable id, or ``None`` if not found."""
        target = str(profile_id or "").strip()
        for name, config in self.profiles.items():
            if str(config.get("profile_id", "")) == target:
                return name
        return None

    def get_config_by_id(self, profile_id: str) -> dict[str, Any] | None:
        """Return a deep copy of a profile's config by stable id.

        Unlike :meth:`select`, this does NOT change ``current_name``, so it is
        safe to call for scheduled launches without disturbing the user's
        currently-selected profile in the UI.
        """
        name = self.find_by_id(profile_id)
        if name is None:
            return None
        return copy.deepcopy(self.profiles[name])

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Return a deep copy of all profiles for serialization."""
        return copy.deepcopy(self.profiles)

    def select(self, name: str) -> dict[str, Any]:
        """Return a deep copy of the profile dict for the given name.

        A deep copy is returned so callers can never mutate the collection's
        internal state by accident. Profile switching is a low-frequency
        operation, so the copy cost is acceptable.
        """
        if name not in self.profiles:
            raise KeyError(f"方案不存在：{name}")
        self.current_name = name
        return copy.deepcopy(self.profiles[name])

    def create(self, name: str, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Create a new profile and select it.

        Args:
            name: Name for the new profile (validated and normalized).
            config: Optional initial config; defaults to a fresh profile.

        Returns:
            A deep copy of the newly created profile config.

        Raises:
            ValueError: If the name is invalid, already exists, or the
                profile count limit is reached.
        """
        name = normalize_profile_name(name)
        if name in self.profiles:
            raise ValueError("方案名称已存在")
        if len(self.profiles) >= MAX_PROFILES:
            raise ValueError(f"方案数量已达到上限 {MAX_PROFILES}")
        value = default_profile_config() if config is None else copy.deepcopy(dict(config))
        migrated, _changed = migrate_profiles({name: value})
        self.profiles[name] = migrated[name]
        self._ensure_ids()
        self.current_name = name
        # Return a deep copy so callers can never mutate the collection's
        # internal state by accident, consistent with select().
        return copy.deepcopy(self.profiles[name])

    def rename(self, new_name: str) -> None:
        """Rename the current profile, preserving its position and config.

        Args:
            new_name: The desired new name (validated and normalized).

        Raises:
            ValueError: If the new name is empty, too long, or already taken.
        """
        new_name = normalize_profile_name(new_name)
        if new_name == self.current_name:
            return
        if new_name in self.profiles:
            raise ValueError("方案名称已存在")
        items: list[tuple[str, dict[str, Any]]] = []
        for name, config in self.profiles.items():
            items.append((new_name if name == self.current_name else name, config))
        self.profiles = dict(items)
        self.current_name = new_name

    def delete_current(self) -> str:
        """Delete the current profile and select an adjacent one.

        Returns:
            The name of the removed profile.

        Raises:
            ValueError: If only one profile remains.
        """
        if len(self.profiles) <= 1:
            raise ValueError("至少需要保留一个方案")
        removed = self.current_name
        names = list(self.profiles)
        index = names.index(removed)
        self.profiles.pop(removed)
        remaining = list(self.profiles)
        self.current_name = remaining[min(index, len(remaining) - 1)]
        return removed

    def move_current(self, offset: int) -> int:
        """Move the current profile by a positional offset.

        Args:
            offset: Number of positions to move (positive = down, negative = up).

        Returns:
            The new index of the current profile.
        """
        names = list(self.profiles)
        index = names.index(self.current_name)
        target = min(max(index + int(offset), 0), len(names) - 1)
        if target == index:
            return index
        names[index], names[target] = names[target], names[index]
        self.profiles = {name: self.profiles[name] for name in names}
        return target

    def reorder(self, names: Sequence[str]) -> None:
        """Reorder profiles to match the given name sequence.

        Args:
            names: Desired order of profile names (must be a permutation of
                the current profile names).

        Raises:
            ValueError: If the provided names do not match the current set.
        """
        ordered = [str(name) for name in names]
        current = list(self.profiles)
        if len(ordered) != len(current) or set(ordered) != set(current):
            raise ValueError("方案顺序与当前方案列表不一致")
        if ordered == current:
            return
        self.profiles = {name: self.profiles[name] for name in ordered}
