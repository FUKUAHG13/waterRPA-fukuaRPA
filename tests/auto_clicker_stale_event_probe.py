"""Out-of-process regression probe for stale finished/error events.

Run 1 finishes while its finished delivery is still queued (the main thread was
blocked in wait()). Run 2 is then started through the real request_start entry,
which is the same path used by the global hotkey. The old sender() guard treats
the destroyed sender (None) as a direct call and cleans the *new* running thread,
which aborts with 0xC0000409 when DeferredDelete runs.

Expected on the fixed implementation: the new thread stays alive and running,
and the process converges after an explicit stop + wait. No real input is used.

Usage: python -B -m tests.auto_clicker_stale_event_probe
"""

from __future__ import annotations

import gc
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.dont_write_bytecode = True

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fukua_rpa.ui.auto_clicker_tool import AutoClickerToolWindow  # noqa: E402


class _FakeEngine:
    """Never injects input; optionally blocks until stop or release."""

    def __init__(self, block=0.0, stop_responsive=True, release_gate=None):
        self.block = block
        self.stop_responsive = stop_responsive
        self.release_gate = release_gate
        self.stop_seen = False
        self.release_calls = 0

    def _execute_auto_clicker(self, context, stop_requested=None):
        if not self.block:
            return "success"
        deadline = time.monotonic() + self.block
        while time.monotonic() < deadline:
            if self.stop_responsive and stop_requested is not None and stop_requested():
                self.stop_seen = True
                return "stopped"
            time.sleep(0.005)
        return "success"

    def release_injected_inputs(self):
        self.release_calls += 1
        if self.release_gate is not None:
            self.release_gate.set()


def main(argv=None):
    app = QApplication([])
    window = AutoClickerToolWindow(parent=None, engine=None)
    window.engine = _FakeEngine()

    # Run 1 finishes immediately; wait() keeps its finished/error events queued.
    if not window.request_start():
        print("run1_not_started", flush=True)
        return 2
    window._thread.wait(3000)
    print(
        "run1_finished",
        window._thread is not None,
        window._thread.isRunning() if window._thread else None,
        flush=True,
    )

    # Start run 2 through the hotkey entry before pumping the stale event.
    window.engine = _FakeEngine(block=3.0)
    if not window.request_start():
        print("run2_not_started", flush=True)
        return 2
    new_thread = window._thread
    print(
        "run2_started",
        window._thread is not None,
        new_thread.isRunning(),
        flush=True,
    )

    # The old run-1 wrapper was dropped when _thread was replaced; collect cycles
    # so its queued event is delivered without a Python sender.
    gc.collect()
    app.processEvents()
    kept = window._thread is not None and window._thread.isRunning()
    print(
        "after_stale_delivery",
        "thread_is_none", window._thread is None,
        "status", window.status_label.text(),
        "new_thread_running", kept,
        flush=True,
    )
    print("before_deferred_delete", flush=True)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    print("after_deferred_delete_survived", flush=True)

    if not kept:
        print("STALE_EVENT_LOST_NEW_THREAD", flush=True)
        return 3

    window.request_stop()
    thread = window._thread
    if thread is not None:
        thread.wait(5000)
    end = time.monotonic() + 0.2
    while time.monotonic() < end:
        app.processEvents()
    print(
        "final",
        "thread_is_none", window._thread is None,
        "status", window.status_label.text(),
        flush=True,
    )
    print("guard_kept_new_thread True", flush=True)
    print("survived", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
