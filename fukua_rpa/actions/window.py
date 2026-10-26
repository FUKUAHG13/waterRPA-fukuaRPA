# pyright: reportAttributeAccessIssue=false
"""Window activate / close / wait / launch actions.

Mechanical extraction from the former monolithic ``engine_actions.py``.
"""

import logging

from fukua_rpa.constants import (
    TASK_TYPE_ACTIVATE_WINDOW,
    TASK_TYPE_CLOSE_WINDOW,
    TASK_TYPE_LAUNCH_APP,
    TASK_TYPE_WAIT_WINDOW,
)
from fukua_rpa.log_policy import (
    LOG_ACTION,
)
from fukua_rpa.privacy import (
    private_debug_value,
)
from fukua_rpa.window_actions import (
    activate_window,
    close_window,
    launch_application,
    wait_for_window,
)

from .shared import (
    action_handler,
)

logger = logging.getLogger(__name__)
class ActionsWindowMixin:
    """Action primitives for the window family."""

    @action_handler(TASK_TYPE_LAUNCH_APP)
    def _execute_launch_application(self, context):
        """Handle the launch-app command: start an external program.

        Args:
            context: StepActionContext whose value is the launch target.

        Returns:
            Always "success".
        """
        pid = launch_application(context.value)
        if self.log_level >= 1:
            self.log(
                f"    -> 程序已启动，进程 ID：{pid}",
                LOG_ACTION,
                private_msg=(
                    f"    -> 程序已启动，进程 ID：{pid}，启动目标："
                    f"{private_debug_value(context.value)}"
                ),
            )
        return "success"

    @action_handler(TASK_TYPE_WAIT_WINDOW)
    def _execute_wait_for_window(self, context):
        """Handle the wait-window command: block until a window appears.

        Args:
            context: StepActionContext whose value identifies the window.

        Returns:
            Outcome string: "success", "timeout", or "stopped".
        """
        configured = (context.task or {}).get("window_timeout", 0)
        timeout = self._bounded_action_duration(
            configured,
            maximum=self._MAX_WAIT_DURATION_SECONDS,
        )
        if timeout <= 0:
            timeout = self._bounded_action_duration(
                self.timeout_val if self.timeout_val > 0 else 10.0,
                maximum=self._MAX_WAIT_DURATION_SECONDS,
            )
        hwnd = wait_for_window(context.value, timeout, self.check_stop_flag)
        if self.check_stop_flag():
            return "stopped"
        if not hwnd:
            if self.log_level >= 1:
                self.log(
                    "<font color='orange'>    [超时] 未等到指定窗口</font>",
                    LOG_ACTION,
                    private_msg=(
                        "<font color='orange'>    [超时] 未等到指定窗口："
                        f"{private_debug_value(context.value)}</font>"
                    ),
                )
            return "timeout"
        if self.log_level >= 1:
            self.log(
                "    -> 已检测到指定窗口",
                LOG_ACTION,
                private_msg=(
                    "    -> 已检测到指定窗口："
                    f"{private_debug_value(context.value)}"
                ),
            )
        return "success"

    @action_handler(TASK_TYPE_ACTIVATE_WINDOW)
    def _execute_activate_window(self, context):
        """Handle the activate-window command: bring a window to the foreground.

        Args:
            context: StepActionContext whose value identifies the window.

        Returns:
            Outcome string: "success" or "not_found".
        """
        success = activate_window(context.value)
        if self.log_level >= 1:
            self.log(
                f"    -> {'已激活' if success else '未找到或无法激活'}指定窗口",
                LOG_ACTION,
                private_msg=(
                    f"    -> {'已激活' if success else '未找到或无法激活'}指定窗口："
                    f"{private_debug_value(context.value)}"
                ),
            )
        return "success" if success else "not_found"

    @action_handler(TASK_TYPE_CLOSE_WINDOW)
    def _execute_close_window(self, context):
        """Handle the close-window command: request a window to close.

        Args:
            context: StepActionContext whose value identifies the window.

        Returns:
            Outcome string: "success" or "not_found".
        """
        success = close_window(context.value)
        if self.log_level >= 1:
            self.log(
                f"    -> {'已请求关闭' if success else '未找到'}指定窗口",
                LOG_ACTION,
                private_msg=(
                    f"    -> {'已请求关闭' if success else '未找到'}指定窗口："
                    f"{private_debug_value(context.value)}"
                ),
            )
        return "success" if success else "not_found"
