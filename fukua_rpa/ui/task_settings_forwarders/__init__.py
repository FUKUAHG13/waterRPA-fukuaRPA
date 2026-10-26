"""Backward-compatibility forwarder Mixins for TaskConfigDialog.

Each Mixin groups thin delegation methods by target task-settings controller,
keeping ``TaskConfigDialog`` focused on dialog wiring and layout while
preserving the public method surface expected by signal connections and tests.
"""

from .auxiliary import AuxSettingsForwarderMixin
from .coordinate import CoordinateSettingsForwarderMixin
from .image import ImageSettingsForwarderMixin

__all__ = [
    "AuxSettingsForwarderMixin",
    "CoordinateSettingsForwarderMixin",
    "ImageSettingsForwarderMixin",
]
