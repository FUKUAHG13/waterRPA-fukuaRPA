"""Thread-safe request object for cancellable script notification dialogs."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class ScriptPromptRequest:
    """Thread-safe handshake for one cancellable script notification dialog.

    A running step asks the UI to show a modal prompt; the UI resolves the
    request exactly once via :meth:`acknowledge` (user confirmed) or
    :meth:`cancel` (user dismissed, or the emergency stop fired). The waiting
    step blocks on :attr:`done` and then inspects the outcome flags.

    Note:
        Read ``acknowledged``/``cancelled`` only after acquiring ``_lock`` or
        after the prompt has been resolved. Concurrent reads without
        synchronization may observe intermediate states.

    Attributes:
        text: Prompt message displayed to the user.
        done: Event set exactly once when the request is resolved.
        acknowledged: ``True`` once :meth:`acknowledge` has won the race.
        cancelled: ``True`` once :meth:`cancel` has won the race.
    """

    text: str
    done: threading.Event = field(default_factory=threading.Event)
    acknowledged: bool = False
    cancelled: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def acknowledge(self) -> None:
        """Resolve the prompt as confirmed by the user.

        Idempotent: only the first resolution (acknowledge or cancel) takes
        effect; later calls are ignored.
        """
        with self._lock:
            if self.done.is_set():
                return
            self.acknowledged = True
            self.done.set()

    def cancel(self) -> None:
        """Resolve the prompt as dismissed/cancelled (e.g. by emergency stop).

        Idempotent: only the first resolution (acknowledge or cancel) takes
        effect; later calls are ignored.
        """
        with self._lock:
            if self.done.is_set():
                return
            self.cancelled = True
            self.done.set()
