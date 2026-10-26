"""Bounded global configuration for profile launch/stop hotkey slots."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from .json_safety import loads_bounded_json
from .task_model import config_bool
from .win32_api import parse_hotkey_text

MAX_PROFILE_HOTKEY_SLOTS = 16
MAX_PROFILE_HOTKEY_JSON_CHARS = 64 * 1024


def _canonical_hotkey(value: object) -> str:
    text = str(value or "").strip()[:64]
    parsed = parse_hotkey_text(text)
    return str(parsed.get("display", "")) if parsed else text


def normalize_profile_hotkey_slots(raw: object) -> list[dict[str, Any]]:
    """Normalize untrusted QSettings data into a bounded slot list."""

    if isinstance(raw, str):
        if len(raw) > MAX_PROFILE_HOTKEY_JSON_CHARS:
            return []
        try:
            # FR-20260809-0503: 改用 loads_bounded_json（ARCHITECTURE #61）——
            # 拒绝重复键、NaN/Infinity，并限制嵌套深度（原生 json.loads 对深层
            # 嵌套 64KB 内即可触发 RecursionError，且静默接受重复键/NaN）。
            raw = loads_bounded_json(
                raw,
                max_bytes=MAX_PROFILE_HOTKEY_JSON_CHARS,
                label="方案热键配置",
            )
        except (TypeError, ValueError):
            return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return []
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in list(raw)[:MAX_PROFILE_HOTKEY_SLOTS]:
        if not isinstance(item, Mapping):
            continue
        slot_id = str(item.get("slot_id", "") or "").strip()[:64]
        if not slot_id or slot_id in seen_ids:
            slot_id = uuid.uuid4().hex
        seen_ids.add(slot_id)
        result.append(
            {
                "slot_id": slot_id,
                "enabled": config_bool(item.get("enabled", True)),
                "profile_id": str(item.get("profile_id", "") or "").strip()[:128],
                "start_hotkey": _canonical_hotkey(item.get("start_hotkey", "")),
                "stop_hotkey": _canonical_hotkey(item.get("stop_hotkey", "")),
            }
        )
    return result


def serialize_profile_hotkey_slots(slots: object) -> str:
    """Serialize normalized slots for QSettings without machine-specific data."""

    normalized = normalize_profile_hotkey_slots(slots)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
