"""Cross-step timing and held-input context for v1.1.16 raw playback steps.

A raw recording converted by :func:`fukua_rpa.recording_raw.build_raw_playback_steps`
becomes a list of ordinary steps that share one ``timing_group_id`` and carry
``fidelity="raw"``. Unlike the single-step 29.0 timeline (whose held keys/buttons
live and die inside one ``finally``), these steps press on one row and release on
another, so the held state and the monotonic clock must live at run scope.

INVARIANT: at any segment boundary (non-raw step, jump, loop rollover, stop,
exception, finish) any key/button still held by this context is released in
reverse order (buttons before keys). The run-level ``release_injected_inputs``
stays as the last-resort backstop and is idempotent with these releases.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class RecordingTimingContext:
    """Run-scoped monotonic clock and held-input tracker for raw playback rows."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.segment_id: str = ""
        self.pressed: list[str] = []
        self.buttons: list[str] = []
        self.active: bool = False

    @property
    def has_segment(self) -> bool:
        return self.active and bool(self.segment_id)

    def is_raw_step(self, task: Any) -> bool:
        return isinstance(task, dict) and str(task.get("fidelity", "")) == "raw" and bool(
            str(task.get("timing_group_id", ""))
        )

    def _parse_pre_delay(self, task: dict) -> int:
        try:
            value = int(str(task.get("pre_delay_ms", "0")).strip() or "0")
        except (TypeError, ValueError):
            value = 0
        return max(0, value)

    def close_segment(self, engine: Any, *, reason: str = "") -> None:
        """Release any still-held inputs and forget the current segment."""
        if not self.active:
            return
        # Reverse order, buttons before keys, matching input_timeline.finally.
        for button in reversed(self.buttons):
            try:
                engine._tracked_mouse_up(button)
            except Exception:  # noqa: BLE001
                logger.debug("录制段清理鼠标按钮失败: %s", button, exc_info=True)
        for key in reversed(self.pressed):
            try:
                engine._tracked_key_up(key)
            except Exception:  # noqa: BLE001
                logger.debug("录制段清理按键失败: %s", key, exc_info=True)
        if reason and logger.isEnabledFor(logging.DEBUG):
            logger.debug("录制段结束(%s)：释放 %d 键 / %d 按钮", reason, len(self.pressed), len(self.buttons))
        self.reset()

    def add_paused_seconds(self, paused_seconds: float) -> None:
        """No-op kept for the debug-pause hook in ``RPAEngine._debug_allows_step``.

        DESIGN: pre-delay is waited relative to "now" at dispatch time, and the
        previous row's action duration has already elapsed on the real clock by
        then. A debug pause therefore already shifts "now" naturally and must
        not be added again — doing so would double-count it. Kept as a method so
        the engine hook stays stable; it intentionally changes no state.
        """
        return None

    def before_dispatch(self, engine: Any, task: dict) -> bool:
        """Wait this row's pre-delay, then let the handler run.

        Called from ``RPAEngine._dispatch_step`` before the normal per-step
        context. The converter stores ``pre_delay_ms`` as "this row's start minus
        the previous row's end" (end already includes that row's hold/trajectory
        duration). At dispatch time the previous row's action has finished, so
        ``time.monotonic()`` is the previous end; waiting exactly ``pre_delay_ms``
        lands on this row's start. No cumulative target is kept — a cumulative
        clock would omit action durations and drift earlier row by row.

        For non-raw steps, closes any open segment so a normal flow only ever
        meets a recording at a no-input boundary.
        """
        if not self.is_raw_step(task):
            if self.has_segment:
                self.close_segment(engine, reason="非录制步骤边界")
            return False

        sid = str(task["timing_group_id"])
        if not self.has_segment or self.segment_id != sid:
            # New segment (or re-entry after loop/jump): close old held state;
            # timing itself is relative and needs no absolute origin.
            if self.has_segment:
                self.close_segment(engine, reason="段切换")
            self.active = True
            self.segment_id = sid

        pre_delay_s = self._parse_pre_delay(task) / 1000.0
        if pre_delay_s > 0 and not engine._wait_interruptibly(pre_delay_s):
            self.close_segment(engine, reason="等待被中断")
            return True
        return True

    def on_key_edge(self, engine: Any, key: str, action: str) -> bool:
        """Dispatch a raw key down/up/repeat edge; False means stopped."""
        key = str(key or "").strip().lower()
        if not key:
            return False
        if action == "down":
            if not engine._tracked_key_down(key):
                return False
            if key not in self.pressed:
                self.pressed.append(key)
            return True
        if action == "up":
            engine._tracked_key_up(key)
            if key in self.pressed:
                self.pressed.remove(key)
            return True
        if action == "repeat":
            # A repeat must have a preceding down in this segment; defensive
            # fallback mirrors the 29.0 handler for a stripped pairing.
            if key not in engine._injected_keys:
                if not engine._tracked_key_down(key):
                    return False
                if key not in self.pressed:
                    self.pressed.append(key)
            return engine._send_key_repeat(key)
        return False

    def on_mouse_edge(self, engine: Any, button: str, action: str, x: int, y: int) -> bool:
        """Dispatch a raw mouse down/up edge (position before press)."""
        button = str(button or "left").strip().lower()
        if action == "down":
            engine.input_backend.move_to(x, y, duration=0)
            if not engine._tracked_mouse_down(button):
                return False
            if button not in self.buttons:
                self.buttons.append(button)
            return True
        engine._tracked_mouse_up(button)
        if button in self.buttons:
            self.buttons.remove(button)
        return True
