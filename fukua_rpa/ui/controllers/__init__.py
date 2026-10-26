"""Composition-based controllers for main-window responsibilities."""

from .diagnostics_controller import DiagnosticsController
from .execution_controller import ExecutionController
from .feedback_controller import VisualFeedbackController
from .hotkey_controller import HotkeyController
from .log_controller import LogController
from .mapping_controller import MappingController
from .profile_controller import ProfileController
from .profile_hotkey_controller import ProfileHotkeyController
from .recording_controller import RecordingController
from .run_controller import RunController
from .runtime_init_controller import RuntimeInitController
from .settings_controller import SettingsController
from .shutdown_controller import ShutdownController
from .window_performance_controller import WindowPerformanceController
from .workflow_editor import WorkflowEditorController
from .workflow_review_controller import WorkflowReviewController
from .workflow_transfer_controller import WorkflowTransferController

__all__ = [
    "DiagnosticsController",
    "ExecutionController",
    "VisualFeedbackController",
    "HotkeyController",
    "LogController",
    "MappingController",
    "ProfileController",
    "ProfileHotkeyController",
    "RecordingController",
    "RunController",
    "RuntimeInitController",
    "SettingsController",
    "ShutdownController",
    "WorkflowEditorController",
    "WorkflowReviewController",
    "WorkflowTransferController",
    "WindowPerformanceController",
]
