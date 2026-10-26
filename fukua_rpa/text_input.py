"""Foreground Unicode text input without using or replacing the clipboard."""

from __future__ import annotations

import ctypes
import logging
from collections.abc import Callable

# Re-exported: this module keeps its public names while the process has
# exactly one INPUT identity (FR-20261016-SUP-001). ``SendInput`` must
# stay a module-level global so callers and tests can still stub it via
# mock.patch.object(text_input, "SendInput").
from .win32_input import (
    HARDWAREINPUT,
    INPUT,
    INPUT_UNION,
    KEYBDINPUT,
    MOUSEINPUT,
    SendInput,
)

logger = logging.getLogger(__name__)


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MAX_INPUT_TEXT_UNITS = 1_000_000

__all__ = [
    "HARDWAREINPUT",
    "INPUT",
    "INPUT_KEYBOARD",
    "INPUT_UNION",
    "KEYBDINPUT",
    "KEYEVENTF_KEYUP",
    "KEYEVENTF_UNICODE",
    "MAX_INPUT_TEXT_UNITS",
    "MOUSEINPUT",
    "SendInput",
    "send_unicode_text",
]


def _utf16_units(text: str) -> list[int]:
    """Encode *text* as a list of UTF-16LE code units.

    Args:
        text: Unicode string to encode.

    Returns:
        List of 16-bit integer code units.

    Raises:
        ValueError: If the unit count exceeds MAX_INPUT_TEXT_UNITS.
    """
    raw = str(text).encode("utf-16-le", errors="strict")
    # Check the unit count from the raw byte length before building the list,
    # so an over-limit input is rejected without allocating a million-element
    # Python int list first.
    if len(raw) // 2 > MAX_INPUT_TEXT_UNITS:
        raise ValueError("输入文本过长")
    return [int.from_bytes(raw[index : index + 2], "little") for index in range(0, len(raw), 2)]


def _release_partial_unicode_unit(unit: int) -> None:
    """Send a key-up event for a Unicode unit whose key-down was sent.

    Prevents a stuck key-down state when SendInput delivers an odd
    number of events from a paired batch.

    Args:
        unit: The UTF-16 code unit to release.
    """
    event = INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None),
    )
    SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))


def send_unicode_text(
    text: str, *, stop_requested: Callable[[], bool] | None = None
) -> bool:
    """Type Unicode text via SendInput without touching the clipboard.

    Sends paired key-down/key-up events in small batches of 64 code units
    to keep cancellation responsive while avoiding partial key states.

    Args:
        text: The Unicode string to type into the foreground window.
        stop_requested: Optional callback; return True to abort mid-input.

    Returns:
        True if all text was sent, False if cancelled.

    Raises:
        ValueError: If the text exceeds MAX_INPUT_TEXT_UNITS.
        OSError: If SendInput delivers fewer events than requested.
    """
    units = _utf16_units(text)
    if not units:
        return True
    should_stop = stop_requested or (lambda: False)
    # Small paired batches keep cancellation responsive without exposing a key-down
    # event across batches.
    for offset in range(0, len(units), 64):
        if should_stop():
            return False
        batch = units[offset : offset + 64]
        events = (INPUT * (len(batch) * 2))()
        for index, unit in enumerate(batch):
            events[index * 2] = INPUT(
                type=INPUT_KEYBOARD,
                ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE, 0, None),
            )
            events[index * 2 + 1] = INPUT(
                type=INPUT_KEYBOARD,
                ki=KEYBDINPUT(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None),
            )
        sent = int(SendInput(len(events), events, ctypes.sizeof(INPUT)))
        if sent != len(events):
            if sent % 2 == 1:
                try:
                    _release_partial_unicode_unit(batch[sent // 2])
                except Exception as exc:
                    logger.warning("释放部分 Unicode 输入失败: %s", exc, exc_info=True)
            raise OSError(f"Unicode 键盘输入不完整：计划 {len(events)} 个事件，实际 {sent} 个")
    return True
