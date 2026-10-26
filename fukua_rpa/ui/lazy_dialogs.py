"""Lazy access to dialogs that startup never constructs.

``TaskConfigDialog`` pulls the per-step settings view, its section widgets and
five interaction controllers — about 24 modules. None of that is constructed
before the user opens a step's gear button or a group batch-settings dialog, so
a module-level import made every startup pay for it (ADR 0040).
"""

import logging

logger = logging.getLogger(__name__)

__all__ = ["load_task_config_dialog"]


def load_task_config_dialog():
    """Return ``TaskConfigDialog``, importing it on first use.

    The import is a literal ``from ... import`` inside the function on purpose:
    PyInstaller's static analysis walks nested code objects, so the dependency
    is still recorded for the frozen build (ADR 0019 uses the same shape for
    PyAutoGUI and UI Automation).

    Raises whatever the import raises. Callers decide how to surface it; none
    of them may leave partially initialised dialog state behind.
    """

    from .task_settings_dialog import TaskConfigDialog

    return TaskConfigDialog
