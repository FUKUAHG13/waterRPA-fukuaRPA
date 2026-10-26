"""Qt cleanup helpers shared by UI regression tests."""

from __future__ import annotations

import contextlib
import gc
import shutil
import sys
import tempfile
import threading
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QMessageBox


def dispose_widget(app, widget):
    """Close and destroy a test widget before the shared QApplication is reused."""

    related_windows = []
    for candidate in app.topLevelWidgets():
        owner_ref = getattr(candidate, "performance_owner_ref", None)
        if callable(owner_ref) and owner_ref() is widget:
            related_windows.append(candidate)
    for candidate in related_windows:
        with contextlib.suppress(RuntimeError):
            candidate.close()
    with contextlib.suppress(RuntimeError):
        widget.close()
    # Let queued zero-delay callbacks observe the closed, still-valid widgets.
    app.processEvents()
    for candidate in related_windows:
        with contextlib.suppress(RuntimeError):
            candidate.deleteLater()
    with contextlib.suppress(RuntimeError):
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    gc.collect()


def create_test_window(test_case, base_dir=None, ensure_dialog=True):
    """Create an RPAWindow for testing with standard mocks and cleanup.

    Patches global-hotkey registration and modal dialogs while the
    window is constructed, then registers cleanup so the shared QApplication
    is left in a usable state for the next test.

    ``base_dir`` defaults to a **test-owned temporary directory**.  Leaving it
    as ``None`` used to resolve to the machine's real user-data directory (the
    repository root in a source checkout), so the window inherited whatever
    session marker happened to be lying there.  A leftover dead-PID marker
    makes ``previous_unclean`` true and schedules a real modal
    ``QMessageBox.information`` 800 ms later, which in offscreen mode blocks on
    the modal event loop or raises ``libshiboken`` ``RuntimeError`` during
    teardown (FR-20260923-002).  The temporary directory keeps every window
    isolated from the machine's own state and leaves no residue behind.
    """

    from fukua_rpa.ui.controllers import HotkeyController
    from fukua_rpa.ui.main_window import RPAWindow

    app = QApplication.instance()
    if base_dir is None:
        base_dir = tempfile.mkdtemp(prefix="fukua-ui-test-")
        test_case.addCleanup(shutil.rmtree, base_dir, True)
    hotkeys = mock.patch.object(
        HotkeyController, "register_global_hotkeys", lambda _self: True
    )
    warnings = mock.patch.object(
        QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.Ok
    )
    # FR-20260923-002: the window may schedule a modal ``QMessageBox.information``
    # after it is shown (e.g. the ``previous_unclean`` notice).  Unmocked it
    # either blocks the shared event loop or fires against an already-deleted
    # widget.  The real notice path keeps its dedicated coverage in
    # tests/test_ui_modernization.py.
    informations = mock.patch.object(
        QMessageBox, "information", lambda *args, **kwargs: QMessageBox.Ok
    )
    hotkeys.start()
    warnings.start()
    informations.start()
    test_case.addCleanup(hotkeys.stop)
    test_case.addCleanup(warnings.stop)
    test_case.addCleanup(informations.stop)
    window = RPAWindow(base_dir=base_dir)
    # C-1: the settings dialog is built lazily on first use. Tests that inspect
    # dialog widgets assume the pre-C-1 shape, so materialize it here. Deferral
    # itself is covered by the dedicated convergence check (no-dialog construction)
    # and by tests/test_c1_settings_two_phase.py, which passes ``ensure_dialog=False``
    # to exercise the two-phase capture/apply paths.
    if ensure_dialog:
        window._ensure_settings_dialog()
    test_case.addCleanup(dispose_widget, app, window)
    return window


class QtCallbackGuardMixin:
    """Mixin for unittest.TestCase that turns Qt/PySide unhandled callback
    exceptions into real test failures."""

    def setUp(self):
        super().setUp()
        self._qt_callback_errors = []
        self._old_excepthook = sys.excepthook
        self._old_unraisablehook = getattr(sys, 'unraisablehook', None)
        self._old_threading_excepthook = threading.excepthook
        sys.excepthook = self._qt_excepthook
        if hasattr(sys, 'unraisablehook'):
            sys.unraisablehook = self._qt_unraisablehook
        threading.excepthook = lambda args: self._qt_excepthook(
            args.exc_type, args.exc_value, args.exc_traceback
        )

    def tearDown(self):
        if not hasattr(self, '_old_excepthook'):
            super().tearDown()
            return
        sys.excepthook = self._old_excepthook
        if self._old_unraisablehook is not None:
            sys.unraisablehook = self._old_unraisablehook
        threading.excepthook = self._old_threading_excepthook
        errors = self._qt_callback_errors
        self._qt_callback_errors = []
        super().tearDown()
        if errors:
            self.fail("Qt callback exceptions:\n" + "\n---\n".join(errors))

    def _qt_excepthook(self, exc_type, exc_value, exc_tb):
        import traceback
        self._qt_callback_errors.append(
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        )

    def _qt_unraisablehook(self, unraisable):
        import traceback
        detail = "".join(
            traceback.format_exception(
                unraisable.exc_type,
                unraisable.exc_value,
                unraisable.exc_traceback,
            )
        )
        if unraisable.err_msg:
            detail = f"{unraisable.err_msg}\n{detail}"
        self._qt_callback_errors.append(detail)
