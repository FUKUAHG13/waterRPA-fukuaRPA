# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Adaptive extra-delay logic.

This mixin is composed into ``fukua_rpa.engine.vision_exec.VisionExecutionMixin``; application code must not import it standalone.
"""

import logging

from fukua_rpa.constants import (
    DEFAULT_CONFIDENCE,
)

logger = logging.getLogger(__name__)

class VisionAdaptiveMixin:
    """Adaptive extra-delay logic."""
    def adaptive_extra_delay(self, img_path, step_info):
        if not self.adaptive_backoff:
            return 0.0
        miss_count = self.miss_streaks.get(self.recognition_key(img_path, step_info), 0)
        if miss_count < 3:
            return 0.0
        return min(DEFAULT_CONFIDENCE, 0.05 * (miss_count - 2))
