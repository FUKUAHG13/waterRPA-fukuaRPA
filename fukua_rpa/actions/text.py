# pyright: reportAttributeAccessIssue=false
"""Text / secret-text input actions.

Mechanical extraction from the former monolithic ``engine_actions.py``.
"""

import logging

from fukua_rpa.constants import (
    TASK_TYPE_SECRET_TEXT,
)
from fukua_rpa.log_policy import (
    LOG_ACTION,
    LOG_CRITICAL,
)
from fukua_rpa.privacy import (
    private_debug_value,
    safe_error_name,
)
from fukua_rpa.text_input import send_unicode_text

from .shared import (
    action_handler,
)

logger = logging.getLogger(__name__)
class ActionsTextMixin:
    """Action primitives for the text family."""

    def _send_private_text(self, value, label, *, allow_unprotected_log=False):
        """Send unicode text via the input backend with privacy-safe logging.

        Args:
            value: Text to type.
            label: Action label used in the log summary.
            allow_unprotected_log: If True, include the text in private_msg.

        Returns:
            Outcome string: "success" or "stopped".
        """
        text = str(value)
        if self.log_level >= 1:
            self.log(
                f"    -> {label}（{len(text)} 个字符）",
                LOG_ACTION,
                private_msg=(
                    f"    -> {label}：{private_debug_value(text)}"
                    if allow_unprotected_log
                    else None
                ),
            )
        completed = send_unicode_text(text, stop_requested=self.check_stop_flag)
        return "success" if completed else "stopped"

    @action_handler(TASK_TYPE_SECRET_TEXT)
    def _execute_secret_text(self, context):
        """Handle the secret-text command: type a stored credential by name.

        The secret value is never logged, even when ordinary log privacy is
        disabled.

        Args:
            context: StepActionContext whose value is the credential name.

        Returns:
            Outcome string: "success", "stopped", or "error".
        """
        # SAFETY: secret text never supplies a private_message variant. The
        # credential hard boundary remains active even when ordinary log
        # privacy is explicitly disabled by the user.
        try:
            secret = self.credential_store().get(context.value)
        except Exception as error:
            if self.log_level >= 0:
                self.log(
                    f"<font color='red'>    [凭据错误] {safe_error_name(error)}</font>",
                    LOG_CRITICAL,
                    critical=True,
                )
            return "error"
        try:
            return self._send_private_text(secret, "正在输入秘密文本")
        except Exception as error:
            if self.log_level >= 0:
                self.log(
                    f"<font color='red'>    [秘密文本输入失败] {safe_error_name(error)}</font>",
                    LOG_CRITICAL,
                    critical=True,
                )
            return "error"

    @action_handler(4.0)
    def _execute_text(self, context):
        """Handle command 4.0: type ordinary text from the step value.

        Args:
            context: StepActionContext whose value is the text to type.

        Returns:
            Outcome string: "success" or "stopped".
        """
        return self._send_private_text(
            context.value,
            "正在输入文本",
            allow_unprotected_log=True,
        )
