"""Thread-safe execution lifecycle with backwards-compatible engine semantics."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import StrEnum


class RunState(StrEnum):
    """Lifecycle states of an execution run.

    Transitions: IDLE -> PREPARING -> RUNNING -> IDLE.
    A stop request moves any active state to STOPPING before returning to IDLE.
    """

    IDLE = "idle"
    PREPARING = "preparing"
    RUNNING = "running"
    STOPPING = "stopping"


@dataclass(frozen=True)
class RunSnapshot:
    """Immutable point-in-time view of the run lifecycle.

    Returned by :meth:`RunLifecycle.snapshot` for safe cross-thread inspection
    without holding the lifecycle lock.
    """

    state: RunState
    active_run_id: int | None
    stop_requested: bool
    started_at: float | None
    last_run_id: int | None
    last_outcome: str
    last_error: str


class RunLifecycle:
    """Own run identity, cancellation and terminal metadata under one lock."""

    def __init__(self) -> None:
        """Initialize the lifecycle in IDLE state with no active run."""
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._sequence = 0
        self._state = RunState.IDLE
        self._active_run_id: int | None = None
        self._started_at: float | None = None
        self._last_run_id: int | None = None
        self._last_outcome = ""
        self._last_error = ""

    def reserve(self) -> int | None:
        """Atomically reserve the next run ID and enter PREPARING state.

        Returns:
            The newly assigned run ID, or None if a run is already active.
        """
        with self._lock:
            if self._state is not RunState.IDLE:
                return None
            self._sequence += 1
            self._active_run_id = self._sequence
            self._state = RunState.PREPARING
            self._started_at = time.monotonic()
            self._stop_event.clear()
            return self._active_run_id

    def mark_running(self, run_id: int) -> bool:
        """Transition the reserved run to RUNNING state.

        If a stop was requested during preparation, the state remains
        STOPPING but the call still succeeds.

        Args:
            run_id: The run ID previously returned by :meth:`reserve`.

        Returns:
            True if the transition was accepted; False if *run_id* does not
            match the active run or the lifecycle is idle.
        """
        with self._lock:
            if self._active_run_id != run_id or self._state is RunState.IDLE:
                return False
            if self._state is not RunState.STOPPING:
                self._state = RunState.RUNNING
            return True

    def matches(self, run_id: int | None) -> bool:
        """Check whether *run_id* is the currently active run.

        Args:
            run_id: The run ID to compare against the active one.

        Returns:
            True if the lifecycle is not idle and *run_id* matches.
        """
        with self._lock:
            return self._state is not RunState.IDLE and self._active_run_id == run_id

    @property
    def active_run_id(self) -> int | None:
        """The ID of the currently active run, or None if idle."""
        with self._lock:
            return self._active_run_id

    def request_stop(self) -> bool:
        """Signal the active run to stop at the next safe checkpoint.

        Thread-safe; may be called from any thread (UI, watchdog, hotkey).
        Sets the internal stop event and moves state to STOPPING.

        Returns:
            True if a stop was signalled; False if no run is active.
        """
        with self._lock:
            if self._state is RunState.IDLE:
                return False
            self._state = RunState.STOPPING
            self._stop_event.set()
            return True

    def finish(self, run_id: int, outcome: str = "finished", error: str = "") -> bool:
        """Finalize the active run and return the lifecycle to IDLE.

        Records terminal metadata (outcome, error) for later inspection
        and clears the stop event for the next run.

        Args:
            run_id: The run ID to finalize; must match the active run.
            outcome: Short label for the terminal state (e.g. "finished",
                "worker_error", "cancelled").
            error: Optional error detail string.

        Returns:
            True if the run was finalized; False if *run_id* does not match.
        """
        with self._lock:
            if self._active_run_id != run_id:
                return False
            self._last_run_id = run_id
            self._last_outcome = str(outcome or "finished")
            self._last_error = str(error or "")
            self._active_run_id = None
            self._started_at = None
            self._state = RunState.IDLE
            self._stop_event.clear()
            return True

    @property
    def is_active(self) -> bool:
        """True if a run is currently in progress (any non-IDLE state)."""
        with self._lock:
            return self._state is not RunState.IDLE

    @property
    def stop_requested(self) -> bool:
        """True if a stop has been requested for the current run.

        Lock-free read of the underlying threading.Event; safe from any thread.
        """
        return self._stop_event.is_set()

    @property
    def state(self) -> RunState:
        """The current lifecycle state."""
        with self._lock:
            return self._state

    def snapshot(self) -> RunSnapshot:
        """Return an immutable snapshot of the full lifecycle state.

        Safe to call from any thread; the returned dataclass will not
        change even if the lifecycle is subsequently modified.
        """
        with self._lock:
            return RunSnapshot(
                state=self._state,
                active_run_id=self._active_run_id,
                stop_requested=self._stop_event.is_set(),
                started_at=self._started_at,
                last_run_id=self._last_run_id,
                last_outcome=self._last_outcome,
                last_error=self._last_error,
            )
