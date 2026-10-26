# pyright: reportAttributeAccessIssue=false
"""Keyboard hotkey action.

Mechanical extraction from the former monolithic ``engine_actions.py``.
"""

import logging

from fukua_rpa.log_policy import (
    LOG_ACTION,
)

from .shared import (
    action_handler,
)

logger = logging.getLogger(__name__)
class ActionsKeyboardMixin:
    """Action primitives for the keyboard family."""

    @action_handler(7.0)
    def _execute_hotkey(self, context):
        """Handle command 7.0: trigger a system hotkey combination.

        Parses the "+"-separated key string, presses keys in order with an
        optional hold duration, then releases them in reverse order.

        Args:
            context: StepActionContext whose value is the hotkey string.

        Returns:
            Outcome string: "success", "stopped", or "error".
        """
        if self.log_level >= 1:
            self.log(f"    -> 触发系统按键组合: {context.value}", LOG_ACTION)
        keys = [key.strip() for key in str(context.value).lower().split("+") if key.strip()]
        if not keys:
            return "error"
        pressed = []
        hold_duration = context.key_hold_duration
        if hold_duration is None:
            # 步骤级未设置时回退到全局 key_hold_duration（默认 0 表示不按住）。
            hold_duration = getattr(self, "key_hold_duration", 0.0)
        try:
            for key in keys:
                if not self._tracked_key_down(key):
                    return "stopped"
                pressed.append(key)
            if hold_duration and hold_duration > 0 and not self._wait_interruptibly(hold_duration):
                return "stopped"
            return "stopped" if self.check_stop_flag() else "success"
        finally:
            for key in reversed(pressed):
                self._tracked_key_up(key)
