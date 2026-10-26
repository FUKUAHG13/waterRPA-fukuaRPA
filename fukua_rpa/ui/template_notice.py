# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false, reportIndexIssue=false, reportCallIssue=false
"""One-time guidance shown when a user deliberately selects a transparent template."""

import logging

from PySide6.QtWidgets import QMessageBox

from ..usage_notices import automatic_first_use_notice_pending, usage_notice
from ..vision.core import template_transparency_info

logger = logging.getLogger(__name__)
TRANSPARENT_TEMPLATE_NOTICE_KEY = (
    usage_notice("transparent_template").one_time_key
)


def show_transparent_template_warning_once(parent, settings, image_path):
    try:
        info = template_transparency_info(image_path)
    except Exception as exc:
        logger.debug("检查模板透明度失败: %s", exc, exc_info=True)
        return False
    if not info.has_transparency:
        return False
    if not automatic_first_use_notice_pending(
        settings, TRANSPARENT_TEMPLATE_NOTICE_KEY
    ):
        return False

    settings.setValue(TRANSPARENT_TEMPLATE_NOTICE_KEY, True)
    settings.sync()
    QMessageBox.information(
        parent,
        "透明图片识别提示",
        "检测到所选图片包含透明区域。\n\n"
        + usage_notice("transparent_template").body
        + "\n\n"
        "此提示只显示一次。",
    )
    return True
