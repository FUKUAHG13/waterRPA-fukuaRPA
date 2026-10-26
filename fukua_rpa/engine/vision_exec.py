# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Vision execution mixin facade.

``VisionExecutionMixin`` is now a pure re-export that composes behavior from the focused mixin submodules under ``fukua_rpa.vision_*``. The old monolithic implementation in this file was split in P4a; the public name and the contract consumed by ``fukua_rpa.engine.RPAEngine`` are unchanged.
"""
from fukua_rpa.commands import CLICK_COMMANDS, IMAGE_TARGET_COMMANDS
from fukua_rpa.vision.adaptive import VisionAdaptiveMixin
from fukua_rpa.vision.match import VisionMatchMixin
from fukua_rpa.vision.multi_image import VisionMultiImageMixin
from fukua_rpa.vision.preload import VisionPreloadMixin
from fukua_rpa.vision.scene import VisionSceneMixin
from fukua_rpa.vision.shared import HAS_MSS, VisionSharedMixin, mss, pyautogui

__all__ = [
    "VisionExecutionMixin",
    "CLICK_COMMANDS",
    "IMAGE_TARGET_COMMANDS",
    "HAS_MSS",
    "mss",
    "pyautogui",
]


class VisionExecutionMixin(
    VisionSharedMixin,
    VisionPreloadMixin,
    VisionMatchMixin,
    VisionSceneMixin,
    VisionMultiImageMixin,
    VisionAdaptiveMixin,
):
    """Composed vision execution mixin (behavior lives in the submodules)."""
    pass
