# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Action execution mixin facade.

This module is a thin composition layer. The real behavior lives in the
``actions_*`` submodules; ``ActionExecutionMixin`` re-exports the composed
mixin so existing callers (notably ``engine.RPAEngine``) are unchanged.
Module-level symbols consumed externally / by tests are re-exported here.
"""
import time

from fukua_rpa.actions.flow import ActionsFlowMixin
from fukua_rpa.actions.image import ActionsImageMixin
from fukua_rpa.actions.input_timeline import ActionsInputTimelineMixin
from fukua_rpa.actions.keyboard import ActionsKeyboardMixin
from fukua_rpa.actions.mouse import ActionsMouseMixin
from fukua_rpa.actions.shared import (
    ACTION_HANDLERS,
    ActionsSharedMixin,
    ClickRetry,
    ClickTarget,
    ClickTiming,
    StepActionContext,
    action_handler,
    logger,
    registered_action_codes,
)
from fukua_rpa.actions.text import ActionsTextMixin
from fukua_rpa.actions.window import ActionsWindowMixin
from fukua_rpa.commands import CLICK_COMMANDS, DRAG_COMMANDS
from fukua_rpa.constants import (
    TASK_TYPE_ACTIVATE_WINDOW,
    TASK_TYPE_AUTO_CLICKER,
    TASK_TYPE_CLOSE_WINDOW,
    TASK_TYPE_EXPRESSION,
    TASK_TYPE_LAUNCH_APP,
    TASK_TYPE_MULTI_IMAGE_ACTION,
    TASK_TYPE_PLUGIN_ACTION,
    TASK_TYPE_SECRET_TEXT,
    TASK_TYPE_SET_VARIABLE,
    TASK_TYPE_UIA_CLICK,
    TASK_TYPE_UIA_READ_VALUE,
    TASK_TYPE_UIA_SET_VALUE,
    TASK_TYPE_UNTIL,
    TASK_TYPE_WAIT_WINDOW,
)

# re-export imported action helpers consumed by external code/tests
from fukua_rpa.text_input import send_unicode_text
from fukua_rpa.window_actions import (
    activate_window,
    close_window,
    launch_application,
    wait_for_window,
)

__all__ = [
    "ActionExecutionMixin",
    "StepActionContext",
    "ClickTarget",
    "ClickTiming",
    "ClickRetry",
    "ACTION_HANDLERS",
    "action_handler",
    "registered_action_codes",
    "launch_application",
    "send_unicode_text",
    "activate_window",
    "close_window",
    "wait_for_window",
    "CLICK_COMMANDS",
    "DRAG_COMMANDS",
    "TASK_TYPE_ACTIVATE_WINDOW",
    "TASK_TYPE_AUTO_CLICKER",
    "TASK_TYPE_CLOSE_WINDOW",
    "TASK_TYPE_EXPRESSION",
    "TASK_TYPE_LAUNCH_APP",
    "TASK_TYPE_MULTI_IMAGE_ACTION",
    "TASK_TYPE_PLUGIN_ACTION",
    "TASK_TYPE_SECRET_TEXT",
    "TASK_TYPE_SET_VARIABLE",
    "TASK_TYPE_UIA_CLICK",
    "TASK_TYPE_UIA_READ_VALUE",
    "TASK_TYPE_UIA_SET_VALUE",
    "TASK_TYPE_UNTIL",
    "TASK_TYPE_WAIT_WINDOW",
    "time",
    "logger",
]


class ActionExecutionMixin(
    ActionsSharedMixin,
    ActionsInputTimelineMixin,
    ActionsMouseMixin,
    ActionsKeyboardMixin,
    ActionsTextMixin,
    ActionsWindowMixin,
    ActionsImageMixin,
    ActionsFlowMixin,
):
    """Composed action execution mixin (behavior lives in the submodules)."""
    pass
