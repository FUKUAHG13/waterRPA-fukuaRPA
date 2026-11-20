"""Out-of-process regression probe: clicker thread end must not destroy a running QThread.

Runs the real AutoClickerToolWindow with a fake engine (no real clicks/keys/focus).
Each round starts the worker, lets it finish, then pumps the Qt event loop. If the
cleanup callback is delivered while the QThread still reports isRunning(), the
probe forces the same DeferredDelete service the Qt event loop uses; on the old
implementation this aborts the process with 0xC0000409 (Qt6Core qFatal).

Usage: python -B -m tests.auto_clicker_thread_exit_probe [rounds]
"""

from __future__ import annotations

import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.dont_write_bytecode = True

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Slot  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fukua_rpa.ui.auto_clicker_tool import AutoClickerToolWindow  # noqa: E402


class _FakeEngine:
    """Never injects input; each round waits until the probe opens its gate."""

    def __init__(self):
        self.gate = threading.Event()

    def _execute_auto_clicker(self, context, stop_requested=None):
        self.gate.wait(5)
        return "success"


class _Recorder(QObject):
    def __init__(self):
        super().__init__()
        self.delivered = 0
        self.running_at_delivery = 0

    @Slot()
    def record(self):
        self.delivered += 1
        sender = self.sender()
        try:
            if sender is not None and sender.isRunning():
                self.running_at_delivery += 1
        except RuntimeError:
            pass


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    rounds = int(args[0]) if args else 400
    app = QApplication([])
    window = AutoClickerToolWindow(parent=None, engine=None)
    recorder = _Recorder()

    for index in range(rounds):
        if index % 100 == 0:
            print(f"progress {index}", flush=True)
        engine = _FakeEngine()
        window.engine = engine
        before = recorder.running_at_delivery
        if not window.request_start():
            print(f"START_FAILED {index}", flush=True)
            return 2
        thread = window._thread
        if thread is None:
            print(f"NO_THREAD {index}", flush=True)
            return 2
        thread.finished.connect(recorder.record)
        engine.gate.set()
        del thread
        deadline = time.monotonic() + 3.0
        while window._thread is not None and time.monotonic() < deadline:
            app.processEvents()
        if window._thread is not None:
            print(f"CLEANUP_STUCK {index}", flush=True)
            return 2
        if recorder.running_at_delivery > before:
            # Same service the Qt event loop performs for deleteLater events.
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        end = time.monotonic() + 0.02
        while time.monotonic() < end:
            app.processEvents()

    print(
        f"iterations {rounds} delivered {recorder.delivered} "
        f"early {recorder.running_at_delivery}",
        flush=True,
    )
    print("survived", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
