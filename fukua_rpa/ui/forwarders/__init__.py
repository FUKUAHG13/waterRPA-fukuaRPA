"""Backward-compatibility forwarder Mixins for RPAWindow.

Each Mixin groups thin delegation methods by target controller, keeping
``RPAWindow`` focused on composition-root wiring while preserving the public
method surface expected by external callers, signal connections and tests.
"""

from .execution import ExecutionForwarderMixin
from .feedback_misc import FeedbackMiscForwarderMixin
from .mapping import MappingForwarderMixin
from .profile_log import ProfileLogForwarderMixin
from .settings import SettingsForwarderMixin
from .workflow import WorkflowForwarderMixin

__all__ = [
    "ExecutionForwarderMixin",
    "FeedbackMiscForwarderMixin",
    "MappingForwarderMixin",
    "ProfileLogForwarderMixin",
    "SettingsForwarderMixin",
    "WorkflowForwarderMixin",
]
