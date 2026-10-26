"""Section builders used by the global settings dialog."""

from .automation import build_automation_sections
from .mapping import build_mapping_controls, build_support_links
from .recognition import build_recognition_sections
from .system import build_system_sections

__all__ = [
    "build_automation_sections",
    "build_mapping_controls",
    "build_support_links",
    "build_recognition_sections",
    "build_system_sections",
]
