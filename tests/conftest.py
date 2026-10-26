"""Pytest compatibility shim. The actual Qt callback guard is in
qt_test_support.py as QtCallbackGuardMixin (unittest-compatible).
This file is kept for developers who optionally run pytest."""

from __future__ import annotations

import sys
import threading
import traceback

import pytest


@pytest.fixture(autouse=True)
def fail_on_unhandled_callback_exception():
    """Turn Qt/Python callback exceptions into real test failures.

    This is the pytest equivalent of QtCallbackGuardMixin in
    qt_test_support.py. Both implementations exist so the test suite
    works under unittest (primary) and pytest (optional) runners.
    """

    failures = []
    previous_excepthook = sys.excepthook
    previous_unraisablehook = sys.unraisablehook
    previous_threading_excepthook = threading.excepthook

    def record_exception(exception_type, value, traceback_object):
        failures.append(
            "".join(
                traceback.format_exception(
                    exception_type, value, traceback_object
                )
            )
        )

    def record_unraisable(unraisable):
        detail = "".join(
            traceback.format_exception(
                unraisable.exc_type,
                unraisable.exc_value,
                unraisable.exc_traceback,
            )
        )
        if unraisable.err_msg:
            detail = f"{unraisable.err_msg}\n{detail}"
        failures.append(detail)

    sys.excepthook = record_exception
    sys.unraisablehook = record_unraisable
    threading.excepthook = lambda args: record_exception(
        args.exc_type, args.exc_value, args.exc_traceback
    )
    try:
        yield
    finally:
        sys.excepthook = previous_excepthook
        sys.unraisablehook = previous_unraisablehook
        threading.excepthook = previous_threading_excepthook
        if failures:
            pytest.fail(
                "Qt/Python callback exceptions during test:\n"
                + "\n---\n".join(failures),
                pytrace=False,
            )
