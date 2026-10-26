"""Section builders used by the per-step settings dialog."""

from .capture import build_capture_controls
from .conditions import build_until_conditions
from .debug import build_debug_section
from .execution import build_execution_section
from .multi_image import build_multi_image_section

__all__ = [
    "build_debug_section",
    "build_capture_controls",
    "build_execution_section",
    "build_until_conditions",
    "build_multi_image_section",
]
