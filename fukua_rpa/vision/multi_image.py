# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Multi-image recognition and image-click region resolution.

This mixin is composed into ``fukua_rpa.engine.vision_exec.VisionExecutionMixin``; application code must not import it standalone.
"""

import logging
import os
from collections import OrderedDict

from fukua_rpa.commands import CLICK_COMMANDS, IMAGE_TARGET_COMMANDS
from fukua_rpa.constants import (
    CLICK_ANCHOR_RATIO,
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    TASK_TYPE_MULTI_IMAGE_ACTION,
)
from fukua_rpa.task_model import config_bool, parse_coordinate_text, parse_region_text

from .core import (
    build_scale_values,
    normalize_template_mask_regions,
    template_mask_signature,
)
from .shared import (
    FAST_SEARCH_MIN_RADIUS,
    match_input_dimensions,
)

logger = logging.getLogger(__name__)

class VisionMultiImageMixin:
    """Multi-image recognition and image-click region resolution."""
    def image_click_point_options(self, task):
        if not task or not self.as_bool(task.get("image_click_point_en", False)):
            return None
        rx = max(0.0, min(1.0, self.parse_float_value(task.get("image_click_point_rx", CLICK_ANCHOR_RATIO), CLICK_ANCHOR_RATIO)))
        ry = max(0.0, min(1.0, self.parse_float_value(task.get("image_click_point_ry", CLICK_ANCHOR_RATIO), CLICK_ANCHOR_RATIO)))
        return {"rx": rx, "ry": ry}

    def recognize_task_once(self, task):
        """Run one image-step recognition using its real runtime settings."""

        if self.is_running:
            raise RuntimeError("运行进行中不能执行单步识别测试")
        working = dict(task or {})
        try:
            command = float(working.get("type", 0))
        except (TypeError, ValueError) as error:
            raise ValueError("当前步骤类型无效") from error
        from fukua_rpa.profile_paths import resolve_profile_path

        path = resolve_profile_path(
            working.get("value", ""), self.base_dir or os.getcwd()
        )
        working["value"] = path
        if command not in IMAGE_TARGET_COMMANDS:
            raise ValueError("当前步骤不支持图片识别测试")
        if parse_coordinate_text(path):
            raise ValueError("坐标步骤不需要图片识别测试")
        if not os.path.isfile(path):
            raise ValueError("当前步骤的图片文件不存在")

        if config_bool(working.get("custom_en", False)):
            task_conf = float(working.get("custom_conf", self.confidence))
            use_gray = config_bool(
                working.get("custom_gray", self.enable_grayscale)
            )
            scale_min = float(
                working.get("custom_scale_min", self.min_scale)
            )
            scale_max = float(
                working.get("custom_scale_max", self.max_scale)
            )
            scale_step = float(
                working.get("custom_scale_step", self.scale_step)
            )
        else:
            task_conf = float(self.confidence)
            use_gray = bool(self.enable_grayscale)
            scale_min = float(self.min_scale)
            scale_max = float(self.max_scale)
            scale_step = float(self.scale_step)
        if not CONFIDENCE_MIN <= task_conf <= CONFIDENCE_MAX:
            raise ValueError("本步骤相似度必须在 0.05 到 1 之间")
        build_scale_values(scale_min, scale_max, scale_step)

        mask_regions = normalize_template_mask_regions(
            working.get("template_mask_regions", [])
        )
        cache_key = self.template_cache_key(
            path,
            scale_min,
            scale_max,
            scale_step,
            use_gray,
            mask_regions,
        )
        working["cache_key"] = cache_key

        # A test must reflect the file and settings visible right now, not stale
        # templates or target positions left by an earlier run.
        self.close_screenshot_backend()
        self.img_cache = OrderedDict()
        self.base_templates_cache = {}
        self.base_template_masks_cache = {}
        self.prepared_template_images_cache = {}
        self.template_masked_cache_keys = set()
        self.scaled_templates_cache = {}
        self.scaled_template_masks_cache = {}
        self.template_transparency_cache = {}
        self.transparent_template_reported = set()
        self.scale_options_cache = {
            str(cache_key): (scale_min, scale_max, scale_step)
        }
        self.scaled_template_cache_bytes = 0
        self.last_target_positions = {}
        self.pending_fast_matches = {}
        self.native_rejection_cache = set()
        self.native_failure_streak = 0
        self._native_disabled_for_run = False
        self._native_disable_reason = ""

        if not self.load_and_precompute([working]):
            raise ValueError("当前图片或缩放参数无法完成识别准备")
        search_regions = self.step_search_regions(working, command, path)
        found = self.find_target_optimized(
            path,
            cache_key,
            task_conf,
            use_gray,
            search_regions,
            config_bool(working.get("inverse_match_en", False)),
        )
        if not found:
            return {
                "found": False,
                "confidence": task_conf,
                "search_regions": list(search_regions or []),
            }

        center_x, center_y = float(found[0]), float(found[1])
        scale = float(found[2])
        score = float(found[3]) if len(found) > 3 else task_conf
        image_click = (
            self.image_click_point_options(working)
            if command in CLICK_COMMANDS
            else None
        )
        template_width, template_height = self.template_dimensions(path)
        matched_width, matched_height = match_input_dimensions(
            found, template_width, template_height
        )
        click_x, click_y = self.adjusted_image_click_point(
            path, found, image_click
        )
        return {
            "found": True,
            "center_x": center_x,
            "center_y": center_y,
            "click_x": float(click_x),
            "click_y": float(click_y),
            "matched_width": matched_width,
            "matched_height": matched_height,
            "scale": scale,
            "score": score,
            "confidence": task_conf,
            "search_regions": list(search_regions or []),
        }

    def step_search_regions(self, task, cmd, val):
        if cmd not in IMAGE_TARGET_COMMANDS and cmd != TASK_TYPE_MULTI_IMAGE_ACTION:
            return None
        if cmd != TASK_TYPE_MULTI_IMAGE_ACTION and self.parse_coordinate(val):
            return None
        if not self.as_bool((task or {}).get("step_region_en", False)):
            return None
        region = parse_region_text((task or {}).get("step_region", ""))
        return [region] if region else None

    def adjusted_image_click_point(self, img_path, location_tuple, image_click_config):
        x, y = location_tuple[0], location_tuple[1]
        if not image_click_config:
            return x, y
        tpl_w, tpl_h = self.template_dimensions(img_path)
        matched_w, matched_h = match_input_dimensions(
            location_tuple, tpl_w, tpl_h
        )
        rx = image_click_config.get("rx", CLICK_ANCHOR_RATIO)
        ry = image_click_config.get("ry", CLICK_ANCHOR_RATIO)
        click_x = x + (rx - 0.5) * matched_w
        click_y = y + (ry - 0.5) * matched_h
        return click_x, click_y

    def point_in_search_regions(self, x, y, search_regions):
        regions = self.normalized_regions(search_regions)
        if not regions:
            return True
        px, py = float(x), float(y)
        return any(rx <= px < rx + rw and ry <= py < ry + rh for rx, ry, rw, rh in regions)

    def quick_search_region(
        self,
        img_path,
        cache_key,
        task_conf,
        use_gray,
        search_regions=None,
        allow_inverted=False,
    ):
        if search_regions is not None:
            return None
        if self.normalized_regions(getattr(self, "scan_regions", [])):
            return None
        key = self.target_position_key(
            img_path, cache_key, task_conf, use_gray, allow_inverted
        )
        last = self.last_target_positions.get(key)
        if not last:
            return None

        tpl_w, tpl_h = self.template_dimensions(img_path)
        radius = max(FAST_SEARCH_MIN_RADIUS, int(max(tpl_w, tpl_h) * 3))
        x = int(last[0] - radius)
        y = int(last[1] - radius)
        region = (x, y, radius * 2, radius * 2)
        bounds = self.scan_region if self.scan_region else self.screen_bounds()
        return self.clip_region_to_bounds(region, bounds)

    def scale_options_for(self, cache_key):
        return self.scale_options_cache.get(str(cache_key), (self.min_scale, self.max_scale, self.scale_step))

    def template_cache_key(
        self,
        path,
        scale_min,
        scale_max,
        scale_step,
        use_gray,
        mask_regions=(),
    ):
        signature = template_mask_signature(mask_regions)
        return (
            f"{path}_{scale_min}_{scale_max}_{scale_step}_{use_gray}"
            f"_mask={signature or '-'}"
        )

    def drag_image_cache_key(self, task, field):
        """Return the cache key for one drag end-point image.

        FR-DRAG-SCALE: drag images must live in the **same key space** as
        ``scale_options_cache`` so that a non-default scale configured on a drag
        step is actually honoured. The previous ``drag_source_*``/``drag_target_*``
        keys carried no scale triple, so ``scale_options_for`` never hit and the
        run silently fell back to the default scale.

        The key is deliberately built by the same helper the preload path uses, so
        the two can never drift apart again.
        """
        path = str(task.get(field, "") or "").strip()
        if not path:
            return f"{field}空"
        scale_min, scale_max, scale_step, use_gray = self.image_target_options(task)[:4]
        return self.template_cache_key(path, scale_min, scale_max, scale_step, use_gray)

    def drag_source_cache_key(self, task):
        """Cache key for the drag start-point image."""
        return self.drag_image_cache_key(task, "drag_source_img")

    def drag_target_cache_key(self, task):
        """Cache key for the drag end-point image."""
        return self.drag_image_cache_key(task, "drag_target_img")

    def drag_scale_options(self, task):
        """Return the (min, max, step) triple a drag step should search with.

        FR-DRAG-SCALE: the drag end-points must resolve their scaling exactly the
        way image steps do — a third, private copy of this lookup inside
        ``actions/mouse.py`` is precisely how the two drifted before. Sharing
        ``image_target_options`` also keeps the drag cache key and the ratio table
        registered under it derived from one source.
        """
        return self.image_target_options(task)[:3]

    def multi_image_rule_cache_key(self, path, scale_min, scale_max, scale_step, use_gray):
        """Return the shared cache key for one multi-image rule.

        FR-SCOPE013-006: the preload path and the runtime path must build this key
        the **same** way. Two independent f-strings can drift, and the symptom is
        silent — the pre-computed scaled template never hits, so a custom
        multi-scale search degrades to a single scale without any error.

        Built by :meth:`template_cache_key` so the multi-image rule and the
        ordinary image path share one key space.
        """
        return self.template_cache_key(
            path, scale_min, scale_max, scale_step, use_gray
        )

    def image_target_options(self, task):
        """Return image-step scaling settings without modifying *task*."""

        if config_bool(task.get("custom_en", False)):
            scale_min = float(task.get("custom_scale_min", self.min_scale))
            scale_max = float(task.get("custom_scale_max", self.max_scale))
            scale_step = float(task.get("custom_scale_step", self.scale_step))
            use_gray = config_bool(
                task.get("custom_gray", self.enable_grayscale)
            )
        else:
            scale_min = self.min_scale
            scale_max = self.max_scale
            scale_step = self.scale_step
            use_gray = self.enable_grayscale
        return scale_min, scale_max, scale_step, use_gray

    def image_target_cache_key(self, task):
        """Build the same image cache key used by precompute and execution."""

        path = str(task.get("value", ""))
        scale_min, scale_max, scale_step, use_gray = self.image_target_options(task)
        mask_regions = normalize_template_mask_regions(
            task.get("template_mask_regions", [])
        )
        return self.template_cache_key(
            path,
            scale_min,
            scale_max,
            scale_step,
            use_gray,
            mask_regions,
        )

    def native_search_regions(self, quick_region=None, search_regions=None):
        if quick_region:
            return [quick_region]
        explicit_regions = self.normalized_regions(search_regions)
        if explicit_regions:
            return explicit_regions
        active_regions = self.normalized_regions(getattr(self, "scan_regions", []))
        if active_regions:
            return active_regions
        if self.scan_region:
            return [self.scan_region]
        return []
