# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Target finding and matching (native wrapper with OpenCV fallback).

This mixin is composed into ``fukua_rpa.engine.vision_exec.VisionExecutionMixin``; application code must not import it standalone.
"""

import logging
import math
import os
import random
import time

from fukua_rpa.constants import (
    INDICATOR_RADIUS_RATIO,
)
from fukua_rpa.log_policy import (
    LOG_BACKEND,
    LOG_CRITICAL,
    LOG_RECOGNITION,
)
from fukua_rpa.params import FindAllMatchParams
from fukua_rpa.privacy import safe_error_name
from fukua_rpa.pyautogui_runtime import pyautogui

from .core import (
    NATIVE_RESULT_CANCELLED,
    NATIVE_RESULT_WORK_BUDGET,
)
from .shared import (
    MAX_MATCH_CANDIDATES,
    capture_returns_physical_pixels,
    match_input_dimensions,
)

logger = logging.getLogger(__name__)


def _inverted_scores(res, nan_mask):
    """Build inverted similarity scores for allow_inverted matching.

    FR-20260809-D4-⑤: 两处（单目标最佳匹配 / 全量候选）倒置逻辑共用——
    NaN 哨兵位置不得参与倒置匹配（-(-1.0)=1.0 会是假命中），须拷贝后置 -1.0。
    """

    inverted = -res
    if nan_mask is not None and nan_mask.any():
        inverted = inverted.copy()
        inverted[nan_mask] = -1.0
    return inverted


class VisionMatchMixin:
    """Target finding and matching (native wrapper with OpenCV fallback)."""
    def fast_match_key(
        self,
        img_path,
        cache_key,
        task_conf,
        use_gray,
        search_regions=None,
        allow_inverted=False,
    ):
        regions = tuple(
            tuple(int(value) for value in region)
            for region in self.native_search_regions(
                search_regions=search_regions
            )
        )
        return (
            os.path.abspath(str(img_path or "")),
            str(cache_key),
            float(task_conf),
            bool(use_gray),
            bool(allow_inverted),
            regions,
        )

    def find_target_scales_only(
        self,
        img_path,
        cache_key,
        task_conf,
        use_gray,
        scales,
        search_regions=None,
    ):
        allowed_scales = tuple(
            dict.fromkeys(float(round(float(scale), 8)) for scale in scales)
        )
        if not allowed_scales or self.check_stop_flag():
            return None
        position_key = self.target_position_key(
            img_path, cache_key, task_conf, use_gray
        )
        native_found = self.native_find_targets(
            img_path,
            cache_key,
            task_conf,
            use_gray,
            find_all=False,
            search_regions=search_regions,
            explicit_scales=allowed_scales,
        )
        if native_found is not None:
            if not native_found:
                return None
            found = native_found[0]
            self.remember_target_result(
                position_key, found, img_path, cache_key, use_gray
            )
            return found

        try:
            for screenshot_pil, offset_x, offset_y in self.iter_search_screenshots(
                search_regions
            ):
                if self.check_stop_flag():
                    return None
                found = self.find_target_in_screenshot(
                    img_path,
                    cache_key,
                    task_conf,
                    use_gray,
                    screenshot_pil,
                    offset_x,
                    offset_y,
                    allowed_scales=allowed_scales,
                )
                if found:
                    self.remember_target_result(
                        position_key, found, img_path, cache_key, use_gray
                    )
                    return found
        except (OSError, ValueError, TypeError) as exc:
            if self.verbose_log_enabled(LOG_RECOGNITION):
                self.log(
                    f"<font color='orange'>    [识别] 倍率探测回退路径异常: {safe_error_name(exc)}</font>",
                    LOG_RECOGNITION,
                )
            return None
        return None

    def _recognition_point_to_input(self, x, y, is_physical):
        """将识别命中坐标换算为输入注入（SetCursorPos）期望的坐标空间。

        Args:
            x: 命中横坐标（识别后端原始坐标）。
            y: 命中纵坐标。
            is_physical: 坐标是否来自“真实物理像素”来源。True 表示原生 DXGI 后端；
                False 表示 mss/pyautogui（在 System DPI Aware 下本就返回虚拟化坐标）。

        Returns:
            (x, y) 换算后的坐标。仅当 is_physical 为 True 时才调用
            coordinates.physical_to_input_coordinates：PMv2 激活时恒等返回，System
            DPI Aware 回退路径下按显示器 DPI 虚拟化；无法确定映射时同样恒等返回，
            因此本 helper 绝不会引入错误缩放。
        """
        if not is_physical:
            return x, y
        from fukua_rpa.coordinates import physical_to_input_coordinates

        return physical_to_input_coordinates(x, y)

    def _recognition_geometry_to_input(
        self,
        x,
        y,
        width,
        height,
        is_physical,
    ):
        """Convert a complete recognition hit to canonical input coordinates.

        The center alone is insufficient in the System-DPI fallback: image
        click anchors, preview bounds and confirmation regions also consume the
        matched width and height.  Native/raw-capture geometry is therefore
        converted as one rectangle, while MSS/PyAutoGUI geometry is retained.
        """

        input_x, input_y = self._recognition_point_to_input(x, y, is_physical)
        width = max(1.0, float(width))
        height = max(1.0, float(height))
        if not is_physical:
            return input_x, input_y, width, height

        from fukua_rpa.coordinates import physical_to_input_rect

        left = math.floor(float(x) - width / 2.0)
        top = math.floor(float(y) - height / 2.0)
        right = math.ceil(float(x) + width / 2.0)
        bottom = math.ceil(float(y) + height / 2.0)
        converted = physical_to_input_rect(
            (left, top, max(1, right - left), max(1, bottom - top))
        )
        if converted is None:
            return input_x, input_y, width, height
        return input_x, input_y, float(converted[2]), float(converted[3])

    def native_find_targets(
        self,
        img_path,
        cache_key,
        task_conf,
        use_gray,
        find_all=False,
        quick_region=None,
        search_regions=None,
        explicit_scales=None,
    ):
        """Return native matches, an authoritative empty list, or ``None``.

        ``None`` alone authorizes OpenCV fallback. An empty list means the DLL
        completed without a hit; repeating it would waste work and could alter
        timing-sensitive behavior. Cancellation is encoded as an empty list
        plus ``stop_requested``/the native result code.
        """
        if not self.use_native_core:
            return None
        if getattr(self, "_native_disabled_for_run", False):
            self.performance.increment("match.native_circuit_breaker_hits")
            return None
        if not getattr(self, "native_core", None) or not self.native_core.available:
            return None
        path = str(img_path)
        if not path or not os.path.exists(path):
            return None
        valid, _reason = self.matching_template_status(path, use_gray)
        if not valid:
            return []
        if (
            str(cache_key) in getattr(self, "template_masked_cache_keys", set())
            or self.template_uses_transparency(path)
        ):
            # DESIGN: alpha masks and user-defined ignored regions currently
            # use OpenCV's tested mask semantics. This is a capability fallback,
            # not evidence that native acceleration failed.
            self.performance.increment("match.native_transparency_fallbacks")
            if path not in self.transparent_template_reported:
                self.transparent_template_reported.add(path)
                if self.verbose_log_enabled(LOG_BACKEND):
                    self.log(
                        "<font color='gray'>    [识别] 透明模板已自动使用 "
                        "OpenCV Alpha 掩膜路径；普通不透明模板仍使用原生加速。</font>",
                        LOG_BACKEND,
                    )
            return None
        if self.check_stop_flag():
            return None

        s_min, s_max, s_step = self.scale_options_for(cache_key)
        max_matches = 1024 if find_all else 1
        native_regions = self.native_search_regions(quick_region, search_regions)
        # P1-2: native_regions 来自 last_target_positions / 用户扫描区域，处于输入
        # （虚拟化）坐标空间；原生 DLL 工作在真实物理像素空间。交给 find_template 前
        # 必须反算为物理像素，否则在 System DPI Aware 回退路径下搜索窗口被系统性偏移。
        # 跨混合-DPI 屏幕的矩形是分段变换，需按屏幕边界拆分；PMv2 下仍恒等返回一个区域。
        from fukua_rpa.coordinates import (
            input_rect_to_physical_regions,
            system_dpi_physical_monitor_regions,
        )

        physical_regions = []
        for region in native_regions:
            physical_regions.extend(input_rect_to_physical_regions(region))
        if not native_regions:
            physical_regions = system_dpi_physical_monitor_regions()
        native_regions = physical_regions
        explicit_scales = tuple(
            dict.fromkeys(float(scale) for scale in (explicit_scales or ()))
        )
        if explicit_scales and not getattr(
            self.native_core, "has_explicit_scale_only", False
        ):
            return None
        preferred_scales = explicit_scales
        if not find_all and not explicit_scales:
            preferred_scales, _memory_summary = self.preferred_scales_for(
                img_path, cache_key, use_gray
            )
        rejection_key = (
            os.path.abspath(path),
            tuple(tuple(int(value) for value in region) for region in native_regions),
            float(s_min),
            float(s_max),
            float(s_step),
            bool(use_gray),
            bool(find_all),
            explicit_scales,
        )
        if rejection_key in self.native_rejection_cache:
            self.performance.increment("match.native_rejection_cache_hits")
            return None
        native_started_ns = time.perf_counter_ns()
        try:
            matches = self.native_core.find_template(
                path,
                native_regions,
                s_min,
                s_max,
                s_step,
                use_gray,
                task_conf,
                find_all=find_all,
                max_matches=max_matches,
                parallel_mode=getattr(self, "native_parallel_mode", "auto"),
                preferred_scales=preferred_scales,
                explicit_scale_only=bool(explicit_scales),
            )
        finally:
            self.performance.observe_since("match.native", native_started_ns)
            self.performance.increment("match.native_calls")
        if matches is None:
            native_error = str(self.native_core.load_error or "")
            try:
                native_result_code = int(self.native_core.last_result_code)
            except (AttributeError, TypeError, ValueError):
                native_result_code = 0
            if native_result_code == NATIVE_RESULT_CANCELLED:
                self.performance.increment("match.native_cancellations")
                # SAFETY: never return None here. None would authorize an
                # expensive OpenCV retry after the user already requested stop.
                return []
            self.performance.increment("match.native_fallbacks")
            if (
                native_result_code == NATIVE_RESULT_WORK_BUDGET
                or "work budget exceeded" in native_error.lower()
            ):
                self.native_rejection_cache.add(rejection_key)
                self.performance.increment("match.native_rejections_cached")
            else:
                # 线程安全说明：native_failure_streak 仅在执行线程的 native_find_targets
                # 路径中读写，附属点击线程不进入此路径，无需加锁。
                self.native_failure_streak = int(
                    getattr(self, "native_failure_streak", 0)
                ) + 1
                if self.native_failure_streak >= 3:
                    self._native_disabled_for_run = True
                    self._native_disable_reason = native_error or "连续原生调用失败"
                    self.performance.increment("match.native_circuit_breaker_trips")
                    if self.log_enabled(LOG_CRITICAL, critical=True):
                        self.log(
                            "<font color='orange'>原生识别连续失败 3 次，本次运行已自动停用 DLL "
                            "并改用 OpenCV；下次启动脚本会重新尝试。</font>",
                            LOG_CRITICAL,
                            critical=True,
                        )
            if self.verbose_log_enabled(LOG_BACKEND) and self.native_core.load_error:
                self.log(
                    "<font color='gray'>    [native] 已回退到 OpenCV；底层错误详情未写入日志。</font>",
                    LOG_BACKEND,
                )
            return None
        self.native_failure_streak = 0
        if preferred_scales and not explicit_scales:
            if matches and any(
                abs(float(matches[0][2]) - float(scale)) <= 1e-7
                for scale in preferred_scales
            ):
                self.performance.increment("match.native_scale_hint_hits")
            else:
                self.performance.increment("match.native_scale_hint_fallbacks")
        if matches:
            self.performance.increment("match.native_hits")
        else:
            self.performance.increment("match.native_misses")
        # DESIGN: 原生后端返回真实物理像素坐标。PMv2 未激活（System DPI Aware）时
        # 需逐屏虚拟化后才能交给 SetCursorPos；PMv2 激活时该换算恒等返回。
        template_width, template_height = self.template_dimensions(path)
        converted = []
        for x, y, scale, score, _radius in matches:
            input_x, input_y, input_width, input_height = (
                self._recognition_geometry_to_input(
                    x,
                    y,
                    float(template_width) * float(scale),
                    float(template_height) * float(scale),
                    True,
                )
            )
            converted.append(
                (input_x, input_y, scale, score, input_width, input_height)
            )
        return converted

    def find_target_in_screenshot(
        self,
        img_path,
        cache_key,
        task_conf,
        use_gray,
        screenshot_pil,
        offset_x,
        offset_y,
        allowed_scales=None,
        allow_inverted=False,
    ):
        started_ns = time.perf_counter_ns()
        try:
            with self._vision_lock:
                return self._find_target_in_screenshot_impl(
                    img_path,
                    cache_key,
                    task_conf,
                    use_gray,
                    screenshot_pil,
                    offset_x,
                    offset_y,
                    allowed_scales,
                    allow_inverted,
                )
        finally:
            self.performance.observe_since("match.opencv", started_ns)
            self.performance.increment("match.opencv_calls")

    def _prepare_screenshot_and_templates(self, screenshot_pil, img_path, cache_key, use_gray):
        """Convert screenshot to OpenCV format and prepare base template data.

        Returns (screen_img, tpl_img, tpl_mask) on success.
        Returns None if img_path cannot be loaded into cache.
        tpl_img is None if base template preparation fails (scaled templates may still work).
        """
        import cv2
        import numpy as np

        screen_np = np.array(screenshot_pil)
        if use_gray:
            screen_img = cv2.cvtColor(screen_np, cv2.COLOR_RGB2GRAY)
        else:
            screen_img = cv2.cvtColor(screen_np, cv2.COLOR_RGB2BGR)

        if img_path not in self.img_cache:
            if os.path.exists(img_path):
                try:
                    self._load_into_img_cache(img_path)
                except Exception as exc:
                    logger.warning("加载模板到缓存失败: %s", exc, exc_info=True)
                    return None
            else:
                return None

        tpl_img = None
        tpl_mask = None
        try:
            tpl_img = self.base_templates_cache.get(cache_key)
            tpl_mask = self.base_template_masks_cache.get(cache_key)
            if tpl_img is None:
                self.performance.increment("template.base_cache_misses")
                pil_template = self.prepared_template_images_cache.get(
                    str(cache_key), self.img_cache[img_path]
                )
                tpl_img, tpl_mask = self.opencv_template_data(
                    pil_template, use_gray
                )
                self.base_templates_cache[cache_key] = tpl_img
                if tpl_mask is not None:
                    self.base_template_masks_cache[cache_key] = tpl_mask
            else:
                self.performance.increment("template.base_cache_hits")
        except Exception as exc:
            tpl_img = None
            tpl_mask = None
            if self.verbose_log_enabled(LOG_RECOGNITION):
                self.log(
                    f"<font color='orange'>    [模板] 基础模板准备失败: {safe_error_name(exc)}</font>",
                    LOG_RECOGNITION,
                )

        return screen_img, tpl_img, tpl_mask

    def _find_target_in_screenshot_impl(
        self,
        img_path,
        cache_key,
        task_conf,
        use_gray,
        screenshot_pil,
        offset_x,
        offset_y,
        allowed_scales=None,
        allow_inverted=False,
    ):
        # 截图坐标空间来源（见 vision_shared.capture_screenshot 头部 DESIGN）：
        # 原生后端为物理像素，需要在返回命中坐标前虚拟化；mss/pyautogui 已是虚拟化坐标。
        physical = capture_returns_physical_pixels()
        valid, _reason = self.matching_template_status(img_path, use_gray)
        if not valid:
            return None
        allowed_scale_set = None
        if allowed_scales is not None:
            allowed_scale_set = {
                float(round(float(scale), 8)) for scale in allowed_scales
            }
            if not allowed_scale_set:
                return None
        if not self.opencv_available:
            if allow_inverted:
                return None
            if (
                str(cache_key) in getattr(self, "template_masked_cache_keys", set())
                or self.template_uses_transparency(img_path)
            ):
                return None
            if allowed_scale_set is not None and 1.0 not in allowed_scale_set:
                return None
            if img_path in self.img_cache:
                try:
                    res = pyautogui.locate(self.img_cache[img_path], screenshot_pil, confidence=task_conf, grayscale=use_gray)
                    if res:
                        hit_x, hit_y, hit_width, hit_height = self._recognition_geometry_to_input(
                            res.left + (res.width / 2) + offset_x,
                            res.top + (res.height / 2) + offset_y,
                            res.width,
                            res.height,
                            physical,
                        )
                        return (hit_x, hit_y, 1.0, 1.0, hit_width, hit_height)
                except Exception as exc:
                    logger.warning("pyautogui缓存图片定位失败: %s", exc, exc_info=True)
                    self.performance.increment("screenshot.fallback_failures")
            elif os.path.exists(img_path):
                try:
                    res = pyautogui.locate(img_path, screenshot_pil, confidence=task_conf, grayscale=use_gray)
                    if res:
                        hit_x, hit_y, hit_width, hit_height = self._recognition_geometry_to_input(
                            res.left + (res.width / 2) + offset_x,
                            res.top + (res.height / 2) + offset_y,
                            res.width,
                            res.height,
                            physical,
                        )
                        return (hit_x, hit_y, 1.0, 1.0, hit_width, hit_height)
                except Exception as exc:
                    logger.warning("pyautogui路径定位失败: %s", exc, exc_info=True)
                    self.performance.increment("screenshot.fallback_failures")
            return None

        import cv2

        prepared = self._prepare_screenshot_and_templates(screenshot_pil, img_path, cache_key, use_gray)
        if prepared is None:
            return None
        screen_img, tpl_img, tpl_mask = prepared

        variants = []
        if tpl_img is not None:
            variants.append((1.0, tpl_img, tpl_mask))

        if cache_key in self.scaled_templates_cache:
            scaled_masks = self.scaled_template_masks_cache.get(cache_key, {})
            variants.extend(
                (scale, resized_tpl, scaled_masks.get(float(scale)))
                for scale, resized_tpl in self.scaled_templates_cache[cache_key]
            )

        if allowed_scale_set is not None:
            variants = [
                item
                for item in variants
                if float(round(float(item[0]), 8)) in allowed_scale_set
            ]

        preferred_scales, _summary = self.preferred_scales_for(
            img_path, cache_key, use_gray
        )
        preferred_order = {
            float(scale): index for index, scale in enumerate(preferred_scales)
        }
        variants = sorted(
            enumerate(variants),
            key=lambda item: (
                preferred_order.get(float(item[1][0]), len(preferred_order)),
                item[0],
            ),
        )
        for _original_index, (scale, resized_tpl, resized_mask) in variants:
            if self.check_stop_flag():
                return None
            try:
                if (
                    resized_tpl.shape[0] > screen_img.shape[0]
                    or resized_tpl.shape[1] > screen_img.shape[1]
                ):
                    continue
                res, nan_mask = self.match_template_scores(
                    screen_img, resized_tpl, resized_mask
                )
                min_v, max_v, min_l, max_l = cv2.minMaxLoc(res)
                best_score = float(max_v)
                best_location = max_l
                inverted = False
                if allow_inverted:
                    inverted_res = _inverted_scores(res, nan_mask)
                    _inv_min, inv_max, _inv_min_l, inv_max_l = cv2.minMaxLoc(
                        inverted_res
                    )
                    if float(inv_max) > best_score:
                        best_score = float(inv_max)
                        best_location = inv_max_l
                        inverted = True
                if best_score >= task_conf:
                    h, w = resized_tpl.shape[:2]
                    if inverted:
                        self.performance.increment("match.inverted_hits")
                    hit_x, hit_y, hit_width, hit_height = self._recognition_geometry_to_input(
                        best_location[0] + w // 2 + offset_x,
                        best_location[1] + h // 2 + offset_y,
                        w,
                        h,
                        physical,
                    )
                    return (
                        hit_x,
                        hit_y,
                        float(scale),
                        best_score,
                        hit_width,
                        hit_height,
                    )
            except Exception as exc:
                logger.debug("模板匹配倍率变体失败: %s", exc, exc_info=True)
                continue
        return None

    def find_target_optimized(
        self,
        img_path,
        cache_key,
        task_conf,
        use_gray,
        search_regions=None,
        allow_inverted=False,
    ):
        started_ns = time.perf_counter_ns()
        try:
            with self._vision_lock:
                return self._find_target_optimized_impl(
                    img_path,
                    cache_key,
                    task_conf,
                    use_gray,
                    search_regions,
                    allow_inverted,
                )
        finally:
            self.performance.observe_since("vision.search", started_ns)
            self.performance.increment("vision.search_calls")

    def _find_target_optimized_impl(
        self,
        img_path,
        cache_key,
        task_conf,
        use_gray,
        search_regions=None,
        allow_inverted=False,
    ):
        pending_key = self.fast_match_key(
            img_path,
            cache_key,
            task_conf,
            use_gray,
            search_regions,
            allow_inverted,
        )
        pending = getattr(self, "pending_fast_matches", {}).pop(
            pending_key, None
        )
        if pending is not None:
            self.performance.increment("wake.pending_hits_consumed")
            return pending
        position_key = self.target_position_key(
            img_path, cache_key, task_conf, use_gray, allow_inverted
        )
        quick_region = self.quick_search_region(
            img_path,
            cache_key,
            task_conf,
            use_gray,
            search_regions,
            allow_inverted,
        )
        if quick_region:
            native_found = None
            if not allow_inverted:
                native_found = self.native_find_targets(
                    img_path,
                    cache_key,
                    task_conf,
                    use_gray,
                    find_all=False,
                    quick_region=quick_region,
                )
            # INVARIANT: [] is a completed native miss; only None permits the
            # Python/OpenCV path. Keep this distinction at every native call site.
            if native_found is not None:
                if native_found:
                    found = native_found[0]
                    self.remember_target_result(
                        position_key, found, img_path, cache_key, use_gray
                    )
                    return found
                # Native returned a definitive miss (a cancellation also
                # collapses to []). If the run was stopped, do not keep scanning
                # the remaining quick regions or fall through to OpenCV.
                if self.check_stop_flag():
                    return None
            else:
                if self.check_stop_flag():
                    return None
                try:
                    screenshot_pil, offset_x, offset_y = self.capture_screenshot(quick_region)
                    found = self.find_target_in_screenshot(
                        img_path,
                        cache_key,
                        task_conf,
                        use_gray,
                        screenshot_pil,
                        offset_x,
                        offset_y,
                        allow_inverted=allow_inverted,
                    )
                    if found:
                        self.remember_target_result(
                            position_key, found, img_path, cache_key, use_gray
                        )
                        return found
                except Exception as exc:
                    logger.debug("快速区域OpenCV搜索失败: %s", exc, exc_info=True)
            self.last_target_positions.pop(position_key, None)

        native_found = None
        if not allow_inverted:
            native_found = self.native_find_targets(
                img_path,
                cache_key,
                task_conf,
                use_gray,
                find_all=False,
                search_regions=search_regions,
            )
        if native_found is not None:
            if native_found:
                found = native_found[0]
                self.remember_target_result(
                    position_key, found, img_path, cache_key, use_gray
                )
                return found
            return None

        try:
            for screenshot_pil, offset_x, offset_y in self.iter_search_screenshots(search_regions):
                if self.check_stop_flag():
                    return None
                found = self.find_target_in_screenshot(
                    img_path,
                    cache_key,
                    task_conf,
                    use_gray,
                    screenshot_pil,
                    offset_x,
                    offset_y,
                    allow_inverted=allow_inverted,
                )
                if found:
                    self.remember_target_result(
                        position_key, found, img_path, cache_key, use_gray
                    )
                    return found
        except (OSError, ValueError, TypeError) as exc:
            if self.verbose_log_enabled(LOG_RECOGNITION):
                self.log(
                    f"<font color='orange'>    [识别] 全目标搜索回退路径异常: {safe_error_name(exc)}</font>",
                    LOG_RECOGNITION,
                )
            return None
        return None

    def _collect_template_matches(
        self,
        screen_img,
        tpl_img,
        task_conf,
        offset_x,
        offset_y,
        scale,
        tpl_mask=None,
        allow_inverted=False,
    ):
        import cv2
        import numpy as np

        if tpl_img.shape[0] > screen_img.shape[0] or tpl_img.shape[1] > screen_img.shape[1]:
            return []

        res, nan_mask = self.match_template_scores(screen_img, tpl_img, tpl_mask)
        h, w = tpl_img.shape[:2]
        MAX_DILATE_KERNEL = 51  # 上限，防止大模板导致 dilate 耗时过长
        kernel_w = min(max(3, int(w * 0.6)), MAX_DILATE_KERNEL)
        kernel_h = min(max(3, int(h * 0.6)), MAX_DILATE_KERNEL)
        kernel = np.ones((kernel_h, kernel_w), dtype=np.uint8)
        peak_map = cv2.dilate(res, kernel)
        ys, xs = np.where((res >= task_conf) & (res == peak_map))
        scores = res[ys, xs]
        inverted_count = 0
        if allow_inverted:
            inverted_scores = _inverted_scores(res, nan_mask)
            inverted_peak_map = cv2.dilate(inverted_scores, kernel)
            inv_ys, inv_xs = np.where(
                (inverted_scores >= task_conf)
                & (inverted_scores == inverted_peak_map)
            )
            if len(inv_xs):
                inverted_count = len(inv_xs)
                xs = np.concatenate((xs, inv_xs))
                ys = np.concatenate((ys, inv_ys))
                scores = np.concatenate((scores, inverted_scores[inv_ys, inv_xs]))
        if len(xs) == 0:
            return []
        if inverted_count:
            self.performance.increment("match.inverted_hits", inverted_count)
        if len(xs) > MAX_MATCH_CANDIDATES:
            keep = np.argpartition(scores, -MAX_MATCH_CANDIDATES)[-MAX_MATCH_CANDIDATES:]
            xs, ys, scores = xs[keep], ys[keep], scores[keep]

        matches = []
        for x, y, score in zip(xs, ys, scores, strict=False):
            matches.append({
                "x": float(x + w // 2 + offset_x),
                "y": float(y + h // 2 + offset_y),
                "scale": float(scale),
                "score": float(score),
                "radius": max(4.0, min(w, h) * INDICATOR_RADIUS_RATIO),
                "matched_width": float(w),
                "matched_height": float(h),
            })
        return matches

    @staticmethod
    def _is_too_close_to_accepted(match, grid, cx, cy, cell_size):
        """Return True if *match* overlaps any already-accepted target nearby."""
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbor = grid.get((cx + dx, cy + dy))
                if not neighbor:
                    continue
                for item in neighbor:
                    ddx = match["x"] - item["x"]
                    ddy = match["y"] - item["y"]
                    radius = max(match["radius"], item["radius"])
                    if ddx * ddx + ddy * ddy <= radius * radius:
                        return True
        return False

    def _dedupe_targets(self, matches):
        if not matches:
            return matches
        sorted_matches = sorted(matches, key=lambda m: m["score"], reverse=True)
        max_radius = max(m["radius"] for m in sorted_matches)
        cell_size = max(1, int(max_radius))
        grid = {}
        accepted = []
        for match in sorted_matches:
            cx = int(match["x"]) // cell_size
            cy = int(match["y"]) // cell_size
            if not self._is_too_close_to_accepted(match, grid, cx, cy, cell_size):
                accepted.append(match)
                grid.setdefault((cx, cy), []).append(match)
        return accepted

    @staticmethod
    def _secondary_sort_key(order):
        """Tie-breaker key function for the second sort condition.

        Returns None when the condition is disabled or unknown.
        """

        def left_to_right(point):
            return (point["x"],)

        def right_to_left(point):
            return (-point["x"],)

        def top_to_bottom(point):
            return (point["y"],)

        def bottom_to_top(point):
            return (-point["y"],)

        return {
            "从左到右": left_to_right,
            "从右到左": right_to_left,
            "从上到下": top_to_bottom,
            "从下到上": bottom_to_top,
        }.get(order)

    def _sort_targets_for_click(self, targets):
        order = self.multi_target_order
        secondary_order = getattr(self, "multi_target_order_b", "不启用")
        if order == "随机顺序":
            # 随机顺序下禁用第二条件：主条件本身不产生确定性先后。
            random.shuffle(targets)
            return targets
        # 固定本轮排序使用的鼠标位置：点击途中鼠标移动不得改变顺序。
        mouse_origin = None
        if order == "距离鼠标最近优先":
            try:
                mouse_origin = tuple(self.input_backend.position())
            except Exception as exc:
                logger.debug("获取鼠标位置失败: %s", exc, exc_info=True)
                mouse_origin = None

        def primary_key(point):
            if order == "距离鼠标最近优先" and mouse_origin is not None:
                mx, my = mouse_origin
                return ((point["x"] - mx) ** 2 + (point["y"] - my) ** 2,)
            if order == "从左到右":
                return (point["x"],)
            if order == "从右到左":
                return (-point["x"],)
            return (point["y"],)

        secondary = self._secondary_sort_key(secondary_order)

        def sort_key(point):
            key = primary_key(point)
            if secondary is not None:
                key = key + secondary(point)
            # 完全相同的键值仍有确定性兜底，避免顺序随稳定排序实现漂移。
            return key + (point["y"], point["x"])

        targets.sort(key=sort_key)
        return targets

    def _point_limit_key(self, step_info, img_path, x, y):
        step = step_info.get("step", 0) if step_info else 0
        bucket_x = int(round(float(x) / 8.0) * 8)
        bucket_y = int(round(float(y) / 8.0) * 8)
        return (step, os.path.abspath(str(img_path)), bucket_x, bucket_y)

    def _filter_point_limit_targets(
        self,
        locations,
        img_path,
        step_info,
        point_limit_en,
        point_limit_count,
        *,
        log_skipped=True,
    ):
        """Drop targets that already reached the same-point click limit.

        ``log_skipped=False`` is used by the collection stage, which calls this
        once per frame and logs its own summary instead of one line per frame.
        """

        if not point_limit_en or point_limit_count <= 0:
            return locations

        with self._input_action_lock:
            counts = dict(self.point_click_counts)

        filtered = []
        skipped = 0
        for location_tuple in locations:
            x, y = location_tuple[0], location_tuple[1]
            key = self._point_limit_key(step_info, img_path, x, y)
            if counts.get(key, 0) >= point_limit_count:
                skipped += 1
                continue
            filtered.append(location_tuple)

        if skipped and log_skipped and self.log_level >= 1:
            self.log(f"    -> 同点点击上限已过滤 {skipped} 个已达上限的点位", LOG_RECOGNITION)
        return filtered

    def _record_point_click(self, img_path, step_info, x, y):
        key = self._point_limit_key(step_info, img_path, x, y)
        with self._input_action_lock:
            self.point_click_counts[key] = self.point_click_counts.get(key, 0) + 1
            return self.point_click_counts[key]

    def _finalize_targets(self, img_path, cache_key, use_gray, raw_targets):
        """Unify native/OpenCV find-all post-processing.

        FR-20260809-D4-①: 两处（原生 DLL / OpenCV 回退）对候选目标的维度换算、
        去重排序、倍率记录与输出塑形逻辑共用。本函数发射的 radius 为常量 8.0
        （历史值，保留以不改变输出契约、无可观测收益），但 radius 确有消费方：
        _dedupe_targets / _is_too_close_to_accepted 以 match["radius"] 作为两命中点
        最小区分像素距离做合并判据（见 :849；单目标路径则为
        max(4.0, min(w, h) * INDICATOR_RADIUS_RATIO)）。原注释称 radius「无消费方」
        与 :849 实际消费矛盾，此处修正。
        """

        template_width, template_height = self.template_dimensions(img_path)
        target_dicts = []
        for target in raw_targets:
            matched_width, matched_height = match_input_dimensions(
                target, template_width, template_height
            )
            target_dicts.append(
                {
                    "x": float(target[0]),
                    "y": float(target[1]),
                    "scale": float(target[2]),
                    "score": float(target[3]),
                    "radius": 8.0,
                    "matched_width": matched_width,
                    "matched_height": matched_height,
                }
            )
        targets = self._sort_targets_for_click(
            self._dedupe_targets(target_dicts)
        )
        for target in targets:
            self.record_scale_match(
                img_path,
                cache_key,
                use_gray,
                target["scale"],
                target["score"],
            )
        return [
            (
                p["x"],
                p["y"],
                p["scale"],
                p["score"],
                p["matched_width"],
                p["matched_height"],
            )
            for p in targets
        ]

    def find_all_targets_in_screenshot(self, params: FindAllMatchParams):
        """Find all matching targets in a screenshot.

        Note: Caller must hold self._vision_lock before calling this method.
        Unlike find_target_in_screenshot (which acquires the lock internally),
        this method relies on the caller holding the lock to allow batch operations.

        Args:
            params: Aggregated match parameters (see FindAllMatchParams).
        """
        started_ns = time.perf_counter_ns()
        try:
            return self._find_all_targets_in_screenshot_impl(params)
        finally:
            self.performance.observe_since("match.opencv", started_ns)
            self.performance.increment("match.opencv_calls")

    def _find_all_targets_in_screenshot_impl(self, params: FindAllMatchParams):
        img_path = params.img_path
        cache_key = params.cache_key
        task_conf = params.task_conf
        use_gray = params.use_gray
        screenshot_pil = params.screenshot_pil
        offset_x = params.offset_x
        offset_y = params.offset_y
        search_regions = params.search_regions
        allow_inverted = params.allow_inverted
        # 截图坐标空间来源（见 vision_shared.capture_screenshot 头部 DESIGN）。
        physical = capture_returns_physical_pixels()
        valid, _reason = self.matching_template_status(img_path, use_gray)
        if not valid:
            return []
        if not self.opencv_available:
            if allow_inverted:
                return []
            if (
                str(cache_key) in getattr(self, "template_masked_cache_keys", set())
                or self.template_uses_transparency(img_path)
            ):
                return []
            try:
                target = self.img_cache.get(img_path, img_path)
                boxes = list(pyautogui.locateAll(target, screenshot_pil, confidence=task_conf, grayscale=use_gray))
                matches = [{
                    "x": box.left + (box.width / 2) + offset_x,
                    "y": box.top + (box.height / 2) + offset_y,
                    "scale": 1.0,
                    "score": 1.0,
                    "radius": max(4.0, min(box.width, box.height) * INDICATOR_RADIUS_RATIO),
                    "matched_width": float(box.width),
                    "matched_height": float(box.height),
                } for box in boxes]
                deduped_targets = self._dedupe_targets(matches)
                converted_targets = []
                for p in deduped_targets:
                    input_x, input_y, input_width, input_height = (
                        self._recognition_geometry_to_input(
                            p["x"],
                            p["y"],
                            p["matched_width"],
                            p["matched_height"],
                            physical,
                        )
                    )
                    converted_targets.append(
                        {
                            "x": input_x,
                            "y": input_y,
                            "scale": p["scale"],
                            "score": p["score"],
                            "radius": max(
                                4.0,
                                min(input_width, input_height)
                                * INDICATOR_RADIUS_RATIO,
                            ),
                            "matched_width": input_width,
                            "matched_height": input_height,
                        }
                    )
                targets = self._sort_targets_for_click(converted_targets)
                return [
                    (
                        p["x"],
                        p["y"],
                        p["scale"],
                        p["score"],
                        p["matched_width"],
                        p["matched_height"],
                    )
                    for p in targets
                ]
            except Exception as exc:
                logger.warning("pyautogui多目标定位失败，降级为单目标: %s", exc, exc_info=True)
                self.performance.increment("screenshot.fallback_failures")
                one = self.find_target_optimized(img_path, cache_key, task_conf, use_gray, search_regions)
                return [one] if one else []

        prepared = self._prepare_screenshot_and_templates(screenshot_pil, img_path, cache_key, use_gray)
        if prepared is None:
            return []
        screen_img, tpl_img, tpl_mask = prepared

        matches = []
        if tpl_img is not None:
            matches.extend(
                self._collect_template_matches(
                    screen_img,
                    tpl_img,
                    task_conf,
                    offset_x,
                    offset_y,
                    1.0,
                    tpl_mask,
                    allow_inverted,
                )
            )

        if cache_key in self.scaled_templates_cache:
            scaled_masks = self.scaled_template_masks_cache.get(cache_key, {})
            for scale, resized_tpl in self.scaled_templates_cache[cache_key]:
                if self.check_stop_flag():
                    return []
                try:
                    matches.extend(
                        self._collect_template_matches(
                            screen_img,
                            resized_tpl,
                            task_conf,
                            offset_x,
                            offset_y,
                            scale,
                            scaled_masks.get(float(scale)),
                            allow_inverted,
                        )
                    )
                except Exception as exc:
                    logger.debug("多目标缩放模板匹配失败: %s", exc, exc_info=True)
                    continue

        deduped_targets = self._dedupe_targets(matches)
        converted_targets = []
        for p in deduped_targets:
            input_x, input_y, input_width, input_height = (
                self._recognition_geometry_to_input(
                    p["x"],
                    p["y"],
                    p["matched_width"],
                    p["matched_height"],
                    physical,
                )
            )
            converted_targets.append(
                {
                    "x": input_x,
                    "y": input_y,
                    "scale": p["scale"],
                    "score": p["score"],
                    "radius": max(
                        4.0,
                        min(input_width, input_height) * INDICATOR_RADIUS_RATIO,
                    ),
                    "matched_width": input_width,
                    "matched_height": input_height,
                }
            )
        targets = self._sort_targets_for_click(converted_targets)
        return [
            (
                p["x"],
                p["y"],
                p["scale"],
                p["score"],
                p["matched_width"],
                p["matched_height"],
            )
            for p in targets
        ]

    def find_all_targets_optimized(
        self,
        img_path,
        cache_key,
        task_conf,
        use_gray,
        search_regions=None,
        allow_inverted=False,
    ):
        started_ns = time.perf_counter_ns()
        try:
            with self._vision_lock:
                return self._find_all_targets_optimized_impl(
                    img_path,
                    cache_key,
                    task_conf,
                    use_gray,
                    search_regions,
                    allow_inverted,
                )
        finally:
            self.performance.observe_since("vision.search", started_ns)
            self.performance.increment("vision.search_calls")

    def _find_all_targets_optimized_impl(
        self,
        img_path,
        cache_key,
        task_conf,
        use_gray,
        search_regions=None,
        allow_inverted=False,
    ):
        native_targets = None
        if not allow_inverted:
            native_targets = self.native_find_targets(
                img_path,
                cache_key,
                task_conf,
                use_gray,
                find_all=True,
                search_regions=search_regions,
            )
        if native_targets is not None:
            return self._finalize_targets(
                img_path, cache_key, use_gray, native_targets
            )

        all_targets = []
        try:
            for screenshot_pil, offset_x, offset_y in self.iter_search_screenshots(search_regions):
                if self.check_stop_flag():
                    return []
                all_targets.extend(
                    self.find_all_targets_in_screenshot(FindAllMatchParams(
                        img_path=img_path,
                        cache_key=cache_key,
                        task_conf=task_conf,
                        use_gray=use_gray,
                        screenshot_pil=screenshot_pil,
                        offset_x=offset_x,
                        offset_y=offset_y,
                        search_regions=search_regions,
                        allow_inverted=allow_inverted,
                    ))
                )
        except (OSError, ValueError, TypeError) as exc:
            # 捕获范围与单目标主路径（本模块单目标实现的窄元组）保持一致：
            # cv2.error、MemoryError、断言异常等必须向上传播为步骤错误，不得
            # 被吞成权威“未命中”（审查 VIS-FR-001；``[]`` 是权威未命中契约）。
            logger.debug("多目标全搜索失败: %s", exc, exc_info=True)
            return []

        template_width, template_height = self.template_dimensions(img_path)
        return self._finalize_targets(
            img_path, cache_key, use_gray, all_targets
        )
