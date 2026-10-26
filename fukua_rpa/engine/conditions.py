# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Image and region conditions used by the 'until condition' task."""

import os
import time

from PIL import Image, ImageChops, ImageStat

from fukua_rpa.constants import (
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    UNTIL_OVERLAY_SCALE_DEFAULT,
    UNTIL_OVERLAY_SCALE_MAX,
    UNTIL_OVERLAY_SCALE_MIN,
    UNTIL_OVERLAY_THRESHOLD_DEFAULT,
)
from fukua_rpa.log_policy import LOG_CRITICAL, LOG_RECOGNITION
from fukua_rpa.privacy import safe_error_name
from fukua_rpa.task_model import format_region_text, parse_region_text, until_condition_list_from_data
from fukua_rpa.vision.shared import capture_returns_physical_pixels, restore_capture_provenance

# FR-20260809-D1-⑤: 条件区域比较最长边上限（原 260 魔法数字两处重复）。
_COMPARE_MAX_SIDE = 260


def _clamp(value: float, low: float, high: float) -> float:
    """Clamp *value* into [low, high]（三处阈值解析共用的有界转换）。"""
    return max(low, min(high, value))


class UntilConditionMixin:
    """Evaluate 'until condition' checks (image presence, region change, overlay match)."""

    def condition_screenshot(self, region):
        """Capture a screenshot for the given region, using a per-cycle cache.

        Args:
            region: Tuple of (x, y, w, h) defining the capture area.

        Returns:
            Tuple of (PIL.Image, offset_x, offset_y).
        """
        key = tuple(int(value) for value in region)
        cache = getattr(self, "_until_capture_cache", None)
        if cache is not None and key in cache:
            self.performance.increment("condition.screenshot_cache_hits")
            cached_capture, cached_physical = cache[key]
            # REVIEW-20260913-001 FR-RISK-005: 命中时不重新截图，必须把该图像采集
            # 当时的坐标空间来源写回线程本地标记；否则紧随其后的识别函数会读到另一
            # 区域的来源，对已处输入空间的命中坐标再做一次 physical→input 虚拟化。
            restore_capture_provenance(cached_physical)
            return cached_capture
        captured = self.capture_screenshot(region)
        if cache is not None:
            cache[key] = (captured, capture_returns_physical_pixels())
            self.performance.increment("condition.screenshot_cache_misses")
        return captured

    def until_conditions_from_task(self, task):
        """Extract the list of enabled condition dicts from a task.

        Args:
            task: Normalized task dictionary.

        Returns:
            List of condition dictionaries.
        """
        return until_condition_list_from_data(task or {})

    def until_task_state_key(self, step_info):
        """Derive a hashable state key for per-step condition tracking.

        Args:
            step_info: Dictionary with step metadata.

        Returns:
            Integer step number used as the state key.
        """
        step_no = int(step_info.get("step", 0)) if step_info else 0
        return step_no

    def condition_cache_key(self, image_path, cond_idx, task_conf, use_gray):
        """Build a unique cache key for a condition's template image lookup.

        Args:
            image_path: Path to the template image.
            cond_idx: Condition index within the task.
            task_conf: Confidence threshold for matching.
            use_gray: Whether grayscale matching is enabled.

        Returns:
            String cache key.
        """
        return f"until_{cond_idx}_{image_path}_{self.min_scale}_{self.max_scale}_{self.scale_step}_{task_conf}_{use_gray}"

    def condition_region(self, cond):
        """Parse the condition's region field into an (x, y, w, h) tuple.

        Args:
            cond: Condition dictionary.

        Returns:
            Region tuple, or None if unset/invalid.
        """
        return parse_region_text(cond.get("region", ""))

    def condition_sub_region(self, cond):
        """Return the optional ``until_sub_region`` as an (x, y, w, h) tuple.

        Coordinates are integer pixel offsets relative to the condition's main
        region. Returns ``None`` when the field is absent or invalid, in which
        case the caller falls back to full-region comparison.
        """

        raw = cond.get("sub_region")
        if not raw:
            return None
        try:
            parts = [part.strip() for part in raw.replace("，", ",").split(",")] if isinstance(raw, str) else list(raw)
            if len(parts) != 4:
                return None
            x, y, width, height = [int(float(part)) for part in parts]
        except (TypeError, ValueError, OverflowError):
            # REVIEW-20260913-001 FR-002: 非有限值（"inf"/"1e999"）经 float() 通过，
            # 但 int(float("inf")) 抛 OverflowError；此前未捕获会逃逸出本函数
            # docstring 声明的"invalid → None"契约，并让调用方（区域变化/区域不变
            # 条件）在运行中崩溃。nan 仍由 ValueError 覆盖。
            return None
        if width <= 0 or height <= 0:
            return None
        return x, y, width, height

    def crop_to_sub_region(self, screenshot_pil, sub_region):
        """Crop a region screenshot down to the requested sub-region.

        Returns the cropped ``PIL.Image`` or ``None`` when the sub-region does
        not overlap the screenshot. The caller treats ``None`` as "no valid
        comparison input yet" rather than an error.
        """

        if not sub_region:
            return screenshot_pil
        sx, sy, sw, sh = sub_region
        img_w, img_h = screenshot_pil.size
        left = max(0, sx)
        top = max(0, sy)
        right = min(img_w, sx + sw)
        bottom = min(img_h, sy + sh)
        if right <= left or bottom <= top:
            return None
        return screenshot_pil.crop((left, top, right, bottom))

    def condition_confidence(self, cond):
        """Return the clamped confidence threshold for a condition.

        Args:
            cond: Condition dictionary.

        Returns:
            Float confidence value within [CONFIDENCE_MIN, CONFIDENCE_MAX].
        """
        return _clamp(self.parse_float_value(cond.get("conf", 0.8), 0.8), CONFIDENCE_MIN, CONFIDENCE_MAX)

    def condition_diff_threshold(self, cond):
        """Return the clamped pixel-difference threshold (0-100%) for region change.

        Args:
            cond: Condition dictionary.

        Returns:
            Float threshold percentage.
        """
        return _clamp(self.parse_float_value(cond.get("diff", 8), 8.0), 0.0, 100.0)

    def condition_similarity_threshold(self, cond):
        """Return the clamped similarity threshold (0-100%) for region matching.

        Args:
            cond: Condition dictionary.

        Returns:
            Float threshold percentage.
        """
        return _clamp(self.parse_float_value(cond.get("similarity", 90), 90.0), 0.0, 100.0)

    def resized_for_compare(self, image, target_size=None, max_side=_COMPARE_MAX_SIDE):
        """Resize an image for comparison, capping the longest side.

        Args:
            image: PIL.Image to resize.
            target_size: Explicit (width, height) to resize to, or None.
            max_side: Maximum longest-side length when target_size is None.

        Returns:
            Resized RGB PIL.Image.
        """
        img = image.convert("RGB")
        if target_size:
            return img.resize(target_size)
        w, h = img.size
        longest = max(w, h)
        if longest > max_side:
            scale = max_side / float(longest)
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        return img

    def image_difference_percent(self, img_a, img_b):
        """Compute the mean pixel difference between two images as a percentage.

        Args:
            img_a: First PIL.Image.
            img_b: Second PIL.Image.

        Returns:
            Float difference percentage (0 = identical, 100 = maximally different).
        """
        a = self.resized_for_compare(img_a)
        b = self.resized_for_compare(img_b, a.size)
        diff = ImageChops.difference(a, b)
        stat = ImageStat.Stat(diff)
        mean = sum(stat.mean[:3]) / 3.0
        return mean / 255.0 * 100.0

    def image_similarity_percent(self, current_img, template_img):
        """Compute alpha-weighted similarity between a screenshot and a template.

        Args:
            current_img: Current region screenshot (PIL.Image).
            template_img: Template image with optional alpha channel (PIL.Image).

        Returns:
            Float similarity percentage (100 = identical).
        """
        template = template_img.convert("RGBA")
        width, height = template.size
        longest = max(width, height)
        if longest > _COMPARE_MAX_SIDE:
            scale = _COMPARE_MAX_SIDE / float(longest)
            template = template.resize(
                (max(1, int(width * scale)), max(1, int(height * scale)))
            )
        current = self.resized_for_compare(current_img, template.size)
        template_rgb = template.convert("RGB")
        diff = ImageChops.difference(current, template_rgb)
        alpha = template.getchannel("A")
        alpha_mean = float(ImageStat.Stat(alpha).mean[0]) / 255.0
        if alpha_mean <= 0.0:
            return 0.0
        if alpha.getextrema()[0] < 255:
            alpha_rgb = Image.merge("RGB", (alpha, alpha, alpha))
            diff = ImageChops.multiply(diff, alpha_rgb)
            mean = (sum(ImageStat.Stat(diff).mean[:3]) / 3.0) / alpha_mean
        else:
            mean = sum(ImageStat.Stat(diff).mean[:3]) / 3.0
        return max(0.0, 100.0 - (mean / 255.0 * 100.0))

    def find_condition_image(self, cond, step_info, use_gray):
        """Search for a condition's template image on screen or in a region.

        Args:
            cond: Condition dictionary with image path and settings.
            step_info: Dictionary with step metadata.
            use_gray: Whether to use grayscale matching.

        Returns:
            Tuple of (found: bool, detail: str, is_error: bool).
        """
        image_path = str(cond.get("image", "")).strip()
        if not image_path or not os.path.exists(image_path):
            return False, "图片不存在", True
        conf = self.condition_confidence(cond)
        cache_key = self.condition_cache_key(image_path, cond.get("index", 0), conf, use_gray)
        self.scale_options_cache[str(cache_key)] = (self.min_scale, self.max_scale, self.scale_step)
        region = self.condition_region(cond)

        if region:
            try:
                screenshot_pil, offset_x, offset_y = self.condition_screenshot(region)
                found = self.find_target_in_screenshot(image_path, cache_key, conf, use_gray, screenshot_pil, offset_x, offset_y)
            except Exception as e:
                return False, f"区域识别异常: {safe_error_name(e)}", True
        else:
            found = self.find_target_optimized(image_path, cache_key, conf, use_gray)

        if found:
            x, y = found[0], found[1]
            return True, f"找到指定图片 ({int(x)}, {int(y)})", False
        return False, "未找到指定图片", False

    def evaluate_region_changed_condition(self, cond, step_info):
        """Check whether a screen region has changed beyond a threshold.

        Args:
            cond: Condition dictionary with region and diff threshold.
            step_info: Dictionary with step metadata.

        Returns:
            Tuple of (matched: bool, detail: str).
        """
        region = self.condition_region(cond)
        if not region:
            return False, "未设置区域"
        try:
            screenshot_pil, _offset_x, _offset_y = self.condition_screenshot(region)
        except Exception as e:
            return False, f"截图异常: {safe_error_name(e)}"

        # DESIGN: 子区域变化检测——大区域背景可能持续变化（动画、视频、滚动
        # 内容），但用户只关心其中某个元素是否变化。这里在截取大区域后裁剪出
        # 相对偏移的 until_sub_region，仅对子区域做帧间差异比较，忽略其余区域。
        # 未提供 sub_region 时行为与原来完全一致（全区域比较），保持向后兼容。
        # 基准缓存键带上 sub_region，用户调整子区域后会重新记录基准。
        sub_region = self.condition_sub_region(cond)
        compare_image = self.crop_to_sub_region(screenshot_pil, sub_region)
        if compare_image is None:
            return False, "子区域超出范围"

        sub_key = format_region_text(sub_region) if sub_region else ""
        key = (self.until_task_state_key(step_info), int(cond.get("index", 0)), "changed", format_region_text(region), sub_key)
        baseline = self.until_condition_baselines.get(key)
        if baseline is None:
            self.until_condition_baselines[key] = compare_image.copy()
            return False, "已记录区域基准，等待变化"

        compare_started_ns = time.perf_counter_ns()
        diff = self.image_difference_percent(baseline, compare_image)
        self.performance.observe_since("condition.image_compare", compare_started_ns)
        threshold = self.condition_diff_threshold(cond)
        scope = "子区域变化" if sub_region else "区域变化"
        return diff >= threshold, f"{scope} {diff:.1f}% / 阈值 {threshold:.1f}%"

    def evaluate_region_stable_condition(self, cond, step_info):
        """等待框选区域停止变化（稳定）。

        将本次新截取的帧与上一帧比较；当帧间差异小于”变化阈值”(diff) 时，认为
        区域已稳定（不变），条件成立。这是相比”子区域/覆盖匹配”更简单的替代：
        用户只需框一个大区域，等待它不再变化（如加载圈结束、进度条完成）即可。

        DESIGN: stable_seconds > 0 时，要求区域连续稳定达到指定秒数才判定成立；
        任何一帧差异 >= 阈值都会重置计时。stable_seconds = 0（默认）保持原有行为：
        差异低于阈值立即成立。
        """

        region = self.condition_region(cond)
        if not region:
            return False, "未设置区域"
        try:
            screenshot_pil, _offset_x, _offset_y = self.capture_screenshot(region)
        except Exception as e:
            return False, f"截图异常: {safe_error_name(e)}"

        # 可选子区域（相对偏移）；缺省为整个大区域。
        sub_region = self.condition_sub_region(cond)
        compare_image = self.crop_to_sub_region(screenshot_pil, sub_region)
        if compare_image is None:
            return False, "子区域超出范围"

        key = (
            self.until_task_state_key(step_info),
            int(cond.get("index", 0)),
            "stable",
            format_region_text(region),
            format_region_text(sub_region) if sub_region else "",
        )

        # DESIGN: stable_seconds 持续时间参数。状态存储为 (prev_image, first_stable_time)
        # 元组；first_stable_time 为 None 表示尚未开始计时。
        # REVIEW-20260913-001 FR-002: 此前用裸 float() 解析，inf/nan/1e999 会通过，
        # 使 elapsed >= stable_seconds 恒为假、条件永不成立（静默失效）。改用本文件
        # 既有的 parse_float_value（委托 task_model.parse_float_text，拒绝非有限值，
        # 失败回退 0），与同文件 conf/diff/similarity 的门禁口径一致。
        stable_seconds = max(0.0, self.parse_float_value(cond.get("stable_seconds", "0"), 0.0))

        previous = self.until_condition_previous.get(key)
        if previous is None:
            self.until_condition_previous[key] = (compare_image.copy(), None)
            return False, "已记录首帧，等待区域稳定"

        # 兼容旧格式（纯图像，无元组）——防御性处理。
        if isinstance(previous, tuple):
            prev_image, first_stable_time = previous
        else:
            prev_image, first_stable_time = previous, None

        diff = self.image_difference_percent(prev_image, compare_image)
        threshold = self.condition_diff_threshold(cond)
        scope = "子区域不变" if sub_region else "区域不变"

        if stable_seconds <= 0:
            # 原有行为：差异低于阈值立即成立。
            self.until_condition_previous[key] = (compare_image.copy(), None)
            stable = diff < threshold
            return stable, f"{scope}变化 {diff:.1f}% / 阈值 {threshold:.1f}%"

        # 持续时间模式：需要连续稳定 stable_seconds 秒。
        now = time.perf_counter()
        if diff >= threshold:
            # 区域仍在变化，重置计时。
            self.until_condition_previous[key] = (compare_image.copy(), None)
            return False, f"{scope}变化 {diff:.1f}% / 阈值 {threshold:.1f}%（计时重置）"

        # 差异低于阈值，区域当前帧稳定。
        if first_stable_time is None:
            # 首次进入稳定状态，开始计时。
            self.until_condition_previous[key] = (compare_image.copy(), now)
            return False, f"{scope}变化 {diff:.1f}% / 阈值 {threshold:.1f}%（开始计时 {stable_seconds:.1f}s）"

        elapsed = now - first_stable_time
        if elapsed >= stable_seconds:
            # 已持续稳定足够时间，条件成立。
            self.until_condition_previous[key] = (compare_image.copy(), None)
            return True, f"{scope}变化 {diff:.1f}% / 阈值 {threshold:.1f}%（已稳定 {elapsed:.1f}s）"

        # 仍在计时中。
        self.until_condition_previous[key] = (compare_image.copy(), first_stable_time)
        return False, f"{scope}变化 {diff:.1f}% / 阈值 {threshold:.1f}%（已稳定 {elapsed:.1f}/{stable_seconds:.1f}s）"

    def evaluate_region_matches_image_condition(self, cond, step_info):
        """Check whether a screen region matches a reference image by similarity.

        Args:
            cond: Condition dictionary with region, image, and similarity threshold.
            step_info: Dictionary with step metadata.

        Returns:
            Tuple of (matched: bool, detail: str).
        """
        region = self.condition_region(cond)
        if not region:
            return False, "未设置区域"
        image_path = str(cond.get("image", "")).strip()
        if not image_path or not os.path.exists(image_path):
            return False, "图片不存在"
        try:
            screenshot_pil, _offset_x, _offset_y = self.condition_screenshot(region)
            template = self._get_cached_image(image_path)
            if template is None:
                template = self._load_into_img_cache(image_path)
            compare_started_ns = time.perf_counter_ns()
            similarity = self.image_similarity_percent(screenshot_pil, template)
            self.performance.observe_since("condition.image_compare", compare_started_ns)
        except Exception as e:
            return False, f"区域对比异常: {safe_error_name(e)}"

        threshold = self.condition_similarity_threshold(cond)
        return similarity >= threshold, f"区域相似 {similarity:.1f}% / 阈值 {threshold:.1f}%"

    def evaluate_until_condition(self, cond, step_info, use_gray):
        """Evaluate a single until-condition with performance instrumentation.

        Args:
            cond: Condition dictionary.
            step_info: Dictionary with step metadata.
            use_gray: Whether to use grayscale matching.

        Returns:
            Tuple of (matched: bool, detail: str).
        """
        started_ns = time.perf_counter_ns()
        try:
            return self._evaluate_until_condition_impl(cond, step_info, use_gray)
        finally:
            self.performance.observe_since("condition.evaluate", started_ns)
            self.performance.increment("condition.evaluations")

    def evaluate_overlay_match_condition(self, cond, step_info):
        """Match a small template image at a specific sub-position within a region.

        Args:
            cond: Condition dictionary with overlay image, rect, scale, threshold.
            step_info: Dictionary with step metadata.

        Returns:
            Tuple of (matched: bool, detail: str).
        """
        region = self.condition_region(cond)
        if not region:
            return False, "未设置大区域"
        overlay_image = str(cond.get("until_overlay_image", "")).strip()
        if not overlay_image or not os.path.exists(overlay_image):
            return False, "覆盖小图不存在"
        overlay_rect_raw = str(cond.get("until_overlay_rect", "")).strip()
        overlay_rect = parse_region_text(overlay_rect_raw)
        if not overlay_rect:
            return False, "未设置子区域位置"

        # 取值域与默认值来自 constants（单一来源），与运行前校验
        # ``validation._validate_until_conditions`` 使用同一组常量，避免两侧漂移。
        scale = max(
            UNTIL_OVERLAY_SCALE_MIN,
            min(
                UNTIL_OVERLAY_SCALE_MAX,
                self.parse_float_value(
                    cond.get("until_overlay_scale", UNTIL_OVERLAY_SCALE_DEFAULT),
                    UNTIL_OVERLAY_SCALE_DEFAULT,
                ),
            ),
        )
        threshold = max(
            CONFIDENCE_MIN,
            min(
                CONFIDENCE_MAX,
                self.parse_float_value(
                    cond.get("until_overlay_threshold", UNTIL_OVERLAY_THRESHOLD_DEFAULT),
                    UNTIL_OVERLAY_THRESHOLD_DEFAULT,
                ),
            ),
        )

        try:
            screenshot_pil, _offset_x, _offset_y = self.condition_screenshot(region)
        except Exception as e:
            return False, f"截图异常: {safe_error_name(e)}"

        try:
            import cv2
            import numpy as np
        except ImportError:
            return False, "OpenCV 不可用"

        try:
            # 裁剪子区域（相对于大区域的像素坐标）
            rx, ry, rw, rh = (int(v) for v in overlay_rect)
            img_w, img_h = screenshot_pil.size
            # INVARIANT: 子区域必须完全落在大区域内，否则匹配无意义。
            if rx < 0 or ry < 0 or rx + rw > img_w or ry + rh > img_h or rw <= 0 or rh <= 0:
                return False, f"子区域越界 ({rx},{ry},{rw},{rh}) 超出 {img_w}x{img_h}"

            sub_region = screenshot_pil.crop((rx, ry, rx + rw, ry + rh))

            # 加载小模板图
            template_pil = self._get_cached_image(overlay_image)
            if template_pil is None:
                template_pil = self._load_into_img_cache(overlay_image)
            if template_pil is None:
                return False, "无法加载覆盖小图"

            # 按 until_overlay_scale 缩放小图
            tpl_w, tpl_h = template_pil.size
            if scale != 1.0:
                new_w = max(1, int(tpl_w * scale))
                new_h = max(1, int(tpl_h * scale))
                template_pil = template_pil.resize((new_w, new_h), Image.LANCZOS)

            tpl_w, tpl_h = template_pil.size
            # INVARIANT: 模板尺寸不得大于子区域，否则 matchTemplate 会报错。
            if tpl_w > rw or tpl_h > rh:
                return False, f"小图 ({tpl_w}x{tpl_h}) 大于子区域 ({rw}x{rh})"

            compare_started_ns = time.perf_counter_ns()
            sub_np = np.array(sub_region.convert("RGB"))
            sub_bgr = cv2.cvtColor(sub_np, cv2.COLOR_RGB2BGR)
            tpl_np = np.array(template_pil.convert("RGB"))
            tpl_bgr = cv2.cvtColor(tpl_np, cv2.COLOR_RGB2BGR)

            result = cv2.matchTemplate(sub_bgr, tpl_bgr, cv2.TM_CCOEFF_NORMED)
            _min_val, max_val, _min_loc, _max_loc = cv2.minMaxLoc(result)
            score = float(max_val)
            self.performance.observe_since("condition.image_compare", compare_started_ns)
        except Exception as e:
            return False, f"覆盖匹配异常: {safe_error_name(e)}"

        matched = score >= threshold
        detail = f"覆盖匹配分数 {score:.3f} / 阈值 {threshold:.2f}"
        return matched, detail

    def _evaluate_until_condition_impl(self, cond, step_info, use_gray):
        """Dispatch to the appropriate condition evaluator by mode.

        Args:
            cond: Condition dictionary with a 'mode' field.
            step_info: Dictionary with step metadata.
            use_gray: Whether to use grayscale matching.

        Returns:
            Tuple of (matched: bool, detail: str).
        """
        mode = cond.get("mode", "图片出现")
        if mode == "图片出现":
            found, detail, _is_error = self.find_condition_image(cond, step_info, use_gray)
            return found, detail
        if mode == "图片消失":
            found, detail, is_error = self.find_condition_image(cond, step_info, use_gray)
            # DESIGN: 缺图时"图片出现"忽略 is_error 直接返回 False（未找到），
            # "图片消失"则显式检查 is_error 并同样返回 False（未满足）——用户可见
            # 行为一致（两种模式缺图都不触发条件成立）。不对称是有意为之：
            # "图片消失"把模板丢失视为错误而非"已消失"，避免模板文件意外丢失
            # 时误触发条件成立。参见 docs/设计基准.md 条件语义。
            if is_error:
                return False, detail
            return (not found), ("图片已消失" if found is False else detail)
        if mode == "区域发生变化":
            return self.evaluate_region_changed_condition(cond, step_info)
        if mode == "区域不变":
            return self.evaluate_region_stable_condition(cond, step_info)
        if mode == "区域变成指定图片":
            return self.evaluate_region_matches_image_condition(cond, step_info)
        if mode == "覆盖匹配":
            return self.evaluate_overlay_match_condition(cond, step_info)
        return False, "未知条件类型"

    def execute_until_conditions(self, task, step_info, use_gray):
        """Evaluate all until-conditions for a task and return the aggregate result.

        Args:
            task: Normalized task dictionary containing condition definitions.
            step_info: Dictionary with step metadata.
            use_gray: Whether to use grayscale matching.

        Returns:
            Status string: "condition_true", "condition_false", "error", or "stopped".
        """
        conditions = self.until_conditions_from_task(task)
        if not conditions:
            if self.log_enabled(LOG_CRITICAL, critical=True):
                self.log(
                    "<font color='red'>    [直到条件成立] 没有启用任何条件。</font>",
                    LOG_CRITICAL,
                    critical=True,
                )
            return "error"

        logic = str(task.get("until_logic", "全部满足"))
        results = []
        details = []
        previous_capture_cache = getattr(self, "_until_capture_cache", None)
        self._until_capture_cache = {}
        try:
            for cond in conditions:
                if self.check_stop_flag():
                    return "stopped"
                matched, detail = self.evaluate_until_condition(
                    cond, step_info, use_gray
                )
                results.append(bool(matched))
                details.append(f"条件{cond.get('index')}[{cond.get('mode')}]: {'满足' if matched else '未满足'}，{detail}")
                if logic == "任一满足" and matched:
                    break
                if logic != "任一满足" and not matched:
                    break
        finally:
            self._until_capture_cache = previous_capture_cache

        satisfied = any(results) if logic == "任一满足" else all(results)
        if self.log_level >= 1:
            color = "#4CAF50" if satisfied else "#FF9800"
            detail_text = "；".join(details)
            self.log(
                f"<font color='{color}'>    [直到条件成立] {'条件已满足' if satisfied else '条件未满足'}：{detail_text}</font>",
                LOG_RECOGNITION,
            )
        return "condition_true" if satisfied else "condition_false"

    def until_false_runtime(self, task, step_info):
        """Track condition-false counts and elapsed time for limit checking.

        Args:
            task: Normalized task dictionary with until_max_checks/until_max_seconds.
            step_info: Dictionary with step metadata.

        Returns:
            Tuple of (reached: bool, reason: str, false_count: int, elapsed: float).
        """
        key = self.until_task_state_key(step_info)
        if key not in self.until_condition_started_at:
            self.until_condition_started_at[key] = time.monotonic()
        self.until_condition_counts[key] = self.until_condition_counts.get(key, 0) + 1
        false_count = self.until_condition_counts[key]
        elapsed = time.monotonic() - self.until_condition_started_at.get(key, time.monotonic())
        max_checks = self.non_negative_int_value(task.get("until_max_checks", 0), 0)
        max_seconds = max(0.0, self.parse_float_value(task.get("until_max_seconds", 0), 0.0))
        reached = False
        reasons: list[str] = []
        if max_checks > 0 and false_count >= max_checks:
            reached = True
            reasons.append(f"未满足检查已达到 {false_count}/{max_checks} 次")
        if max_seconds > 0 and elapsed >= max_seconds:
            reached = True
            reasons.append(f"等待条件已达到 {elapsed:.1f}/{max_seconds:.1f} 秒")
        # FR-20260812 建议-体验-1: 两个上限同时到达时拼接说明，不再让第二个
        # reason 覆盖第一个（原实现后写覆盖前写，用户只看到其中一条）。
        reason = "；".join(reasons)
        return reached, reason, false_count, elapsed

    def reset_until_runtime(self, step_info):
        """Clear all per-step condition tracking state (counts, baselines, previous frames).

        Args:
            step_info: Dictionary with step metadata identifying the step to reset.
        """
        key = self.until_task_state_key(step_info)
        self.until_condition_counts.pop(key, None)
        self.until_condition_started_at.pop(key, None)
        # FR-20260809-D1-①: 基线清理改为 dict 推导（修复前逐项线性扫描删除）。
        self.until_condition_baselines = {
            baseline_key: baseline
            for baseline_key, baseline in self.until_condition_baselines.items()
            if not (baseline_key and baseline_key[0] == key)
        }
        previous_map = getattr(self, "until_condition_previous", None)
        if isinstance(previous_map, dict):
            for prev_key in list(previous_map.keys()):
                if prev_key and prev_key[0] == key:
                    previous_map.pop(prev_key, None)
