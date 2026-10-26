# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Template precompute, image caching and the scale-memory policy used by the matching paths.

This mixin is composed into ``fukua_rpa.engine.vision_exec.VisionExecutionMixin``; application code must not import it standalone.
"""

import logging
import os
import time

from PIL import Image

from fukua_rpa.commands import DRAG_COMMANDS, IMAGE_TARGET_COMMANDS
from fukua_rpa.constants import (
    MAX_SINGLE_TEMPLATE_CACHE_BYTES,
    MAX_TEMPLATE_CACHE_BYTES,
    MAX_TEMPLATE_SOURCE_PIXELS,
    TASK_TYPE_MULTI_IMAGE_ACTION,
    TASK_TYPE_UNTIL,
)
from fukua_rpa.log_policy import (
    LOG_ADAPTIVE,
    LOG_BACKEND,
    LOG_CRITICAL,
    LOG_STEP,
)
from fukua_rpa.logging_service import write_log
from fukua_rpa.multi_image_actions import normalize_multi_image_rules
from fukua_rpa.privacy import private_debug_value, private_error_text, safe_error_name
from fukua_rpa.task_model import config_bool, parse_coordinate_text

from .core import (
    apply_template_mask_regions,
    build_scale_values,
    estimate_template_cache_bytes,
    normalize_template_mask_regions,
    template_alpha_channel,
    template_detail_status,
    template_image_detail_status,
    template_transparency_info,
)
from .scale_memory import ScaleMemoryPolicy
from .shared import (
    MAX_IMG_CACHE_ENTRIES,
)

logger = logging.getLogger(__name__)

class VisionPreloadMixin:
    """Template precompute, image caching and the scale-memory policy used by the matching paths."""
    def scale_memory_policy(self):
        return ScaleMemoryPolicy(
            enabled=bool(getattr(self, "use_native_scale_hint", True)),
            tier=str(getattr(self, "scale_memory_tier", "balanced")),
            manual_scales=tuple(getattr(self, "scale_memory_manual", ()) or ()),
            custom_enabled=bool(
                getattr(self, "scale_memory_custom_enabled", False)
            ),
            preferred_limit=int(
                getattr(self, "scale_memory_preferred_limit", 3)
            ),
            history_limit=int(
                getattr(self, "scale_memory_history_limit", 64)
            ),
        ).normalized()

    def scale_memory_context(self, img_path, cache_key, use_gray):
        path = os.path.abspath(str(img_path or ""))
        s_min, s_max, s_step = self.scale_options_for(cache_key)
        try:
            stat = os.stat(path)
            generation = (int(stat.st_mtime_ns), int(stat.st_size))
            valid_scales = (1.0, *build_scale_values(s_min, s_max, s_step))
        except (OSError, TypeError, ValueError):
            return None
        # Scale bounds are search parameters, not template identity. Keeping
        # them out of the key lets one image retain useful observations when
        # the user adjusts its current range. Generation resets stale history
        # when the file at this path is actually replaced.
        key = (path, bool(use_gray))
        return (
            key,
            generation,
            os.path.basename(path) or "图片",
            valid_scales,
        )

    def preferred_scales_for(self, img_path, cache_key, use_gray):
        context = self.scale_memory_context(img_path, cache_key, use_gray)
        if context is None:
            return (), None
        key, generation, label, valid_scales = context
        return self.scale_memory_store.preferred_scales(
            key,
            label,
            valid_scales,
            self.scale_memory_policy(),
            generation=generation,
        )

    def record_scale_match(self, img_path, cache_key, use_gray, scale, score=1.0):
        context = self.scale_memory_context(img_path, cache_key, use_gray)
        if context is None:
            return None
        key, generation, label, valid_scales = context
        summary, changed = self.scale_memory_store.record(
            key,
            label,
            valid_scales,
            scale,
            score,
            self.scale_memory_policy(),
            generation=generation,
        )
        if changed and summary is not None and self.verbose_log_enabled(LOG_ADAPTIVE):
            self.log(
                "<font color='gray'>    [缩放记忆] "
                f"{summary.status_text().replace(summary.label, '当前模板', 1)}</font>",
                LOG_ADAPTIVE,
            )
        return summary

    def scale_memory_summaries(self, maximum=8):
        return self.scale_memory_store.summaries(
            self.scale_memory_policy(), maximum=maximum
        )

    def remember_target_result(
        self,
        position_key,
        found,
        img_path=None,
        cache_key=None,
        use_gray=True,
    ):
        self.last_target_positions[position_key] = (found[0], found[1])
        try:
            scale = float(found[2])
        except (IndexError, TypeError, ValueError):
            return
        if scale > 0.0 and img_path is not None and cache_key is not None:
            score = found[3] if len(found) > 3 else 1.0
            self.record_scale_match(
                img_path, cache_key, use_gray, scale, score
            )

    def template_dimensions(self, img_path):
        try:
            with self._vision_lock:
                if img_path not in self.img_cache and os.path.exists(img_path):
                    self._load_into_img_cache(img_path)
                img = self.img_cache.get(img_path)
            if img:
                return img.size
        except Exception as exc:
            logger.debug("获取模板尺寸失败: %s", exc, exc_info=True)
        return (80, 80)

    def _get_cached_image(self, img_path):
        """Read from img_cache. Unified entry point for cache access."""
        return self.img_cache.get(img_path)

    def _load_into_img_cache(self, img_path):
        """Load image into LRU cache. Unified entry point for cache insertion."""
        img = Image.open(img_path)
        img.load()
        self.img_cache[img_path] = img
        if len(self.img_cache) > MAX_IMG_CACHE_ENTRIES:
            self.img_cache.popitem(last=False)
        return img

    def matching_template_status(self, img_path, use_gray):
        path = os.path.abspath(str(img_path or ""))
        try:
            stat = os.stat(path)
            signature = (path, bool(use_gray), int(stat.st_mtime_ns), int(stat.st_size))
        except OSError:
            return False, "图片不存在"
        cached = self.template_validation_cache.get(signature)
        if cached is None:
            cached = template_detail_status(path, use_gray)
            self.template_validation_cache[signature] = cached
        valid, reason = cached
        if not valid and signature not in self.template_validation_reported:
            self.template_validation_reported.add(signature)
            if self.log_enabled(LOG_CRITICAL, critical=True):
                self.log(
                    "<font color='red'>模板无法安全识别；文件名和路径已从日志隐藏。</font>",
                    LOG_CRITICAL,
                    critical=True,
                    private_msg=(
                        "<font color='red'>模板无法安全识别："
                        f"路径 {private_debug_value(path)}，原因 {private_debug_value(reason)}</font>"
                    ),
                )
        return valid, reason

    def template_uses_transparency(self, img_path):
        path = os.path.abspath(str(img_path or ""))
        cache = getattr(self, "template_transparency_cache", None)
        if cache is None:
            self.template_transparency_cache = {}
            cache = self.template_transparency_cache
        try:
            stat = os.stat(path)
        except OSError:
            return False
        # FR-20260809-D4-②: 仅保留 (path, mtime, size) 签名键——修复前 path 键
        # 命中即短路返回，使签名键的失效逻辑形同虚设：文件被替换（同路径新
        # mtime/size）后透明度结论仍然过期。签名键保证文件变化时重新检测。
        signature = (path, int(stat.st_mtime_ns), int(stat.st_size))
        cached = cache.get(signature)
        if cached is None:
            try:
                cached = bool(
                    template_transparency_info(path).has_transparency
                )
            except Exception as exc:
                logger.debug("检测模板透明度失败: %s", exc, exc_info=True)
                cached = False
            cache[signature] = cached
        return bool(cached)

    def opencv_template_data(self, image, use_gray):
        import cv2
        import numpy as np

        work_img = image.convert("L" if use_gray else "RGB")
        template = np.array(work_img)
        if not use_gray:
            template = cv2.cvtColor(template, cv2.COLOR_RGB2BGR)
        alpha = template_alpha_channel(image)
        mask = None
        if alpha is not None:
            mask = np.asarray(alpha, dtype=np.float32) / 255.0
        return template, mask

    def match_template_scores(self, screen_img, template, mask=None):
        import cv2
        import numpy as np

        if mask is None:
            return cv2.matchTemplate(
                screen_img, template, cv2.TM_CCOEFF_NORMED
            ), None
        result = cv2.matchTemplate(
            screen_img,
            template,
            cv2.TM_CCOEFF_NORMED,
            mask=mask,
        )
        # Masked normalized correlation can produce NaN/Inf where either side
        # has no variance. Such locations are not valid matches.
        # INVARIANT: nan_mask marks positions that must never participate in
        # inverted matching; without this, -(-1.0) = 1.0 would produce false
        # perfect-score hits. See docs/设计基准.md 反色匹配语义。
        nan_mask = np.isnan(result) | np.isinf(result)
        result = np.nan_to_num(
            result, copy=False, nan=-1.0, posinf=-1.0, neginf=-1.0
        )
        return np.clip(result, -1.0, 1.0, out=result), nan_mask

    def load_and_precompute(self, tasks):
        if not self.opencv_available:
            if any(
                normalize_template_mask_regions(
                    task.get("template_mask_regions", [])
                )
                for task in tasks
                if isinstance(task, dict)
            ):
                write_log(
                    "忽略局部变化需要 OpenCV 掩膜识别，当前运行库不可用。",
                    category=LOG_BACKEND,
                    critical=True,
                )
                return False
            return True

        import cv2
        import numpy as np

        errors = []
        write_log("正在预加载资源...",
                     category=LOG_BACKEND,
                 )

        def load_source_image(path):
            if path in self.img_cache:
                self.performance.increment("template.source_cache_hits")
                return self.img_cache[path]
            self.performance.increment("template.source_cache_misses")
            load_started_ns = time.perf_counter_ns()
            with Image.open(path) as source:
                width, height = source.size
                if width * height > MAX_TEMPLATE_SOURCE_PIXELS:
                    raise ValueError(f"图片像素过多，最多允许 {MAX_TEMPLATE_SOURCE_PIXELS:,} 像素")
                image = source.copy()
                image.load()
            self.img_cache[path] = image
            if len(self.img_cache) > MAX_IMG_CACHE_ENTRIES:
                self.img_cache.popitem(last=False)
            self.performance.observe_since("template.source_load", load_started_ns)
            return image

        def preload_one(
            path,
            cache_key,
            s_min,
            s_max,
            s_step,
            use_gray,
            mask_regions=(),
        ):
            started_ns = time.perf_counter_ns()
            try:
                return preload_one_impl(
                    path,
                    cache_key,
                    s_min,
                    s_max,
                    s_step,
                    use_gray,
                    mask_regions,
                )
            finally:
                self.performance.observe_since("template.precompute_one", started_ns)

        def preload_one_impl(
            path,
            cache_key,
            s_min,
            s_max,
            s_step,
            use_gray,
            mask_regions,
        ):
            if not path or not os.path.exists(path):
                return
            valid, reason = self.matching_template_status(path, use_gray)
            if not valid:
                raise ValueError(reason)

            scale_values = build_scale_values(s_min, s_max, s_step)
            normalized_mask_regions = normalize_template_mask_regions(mask_regions)
            estimated = estimate_template_cache_bytes(
                path,
                use_gray,
                scale_values,
                has_mask=bool(normalized_mask_regions),
            )
            if estimated > MAX_SINGLE_TEMPLATE_CACHE_BYTES:
                raise ValueError(
                    f"该图片预计占用 {estimated / 1024 / 1024:.1f} MB 模板缓存，"
                    f"单图上限为 {MAX_SINGLE_TEMPLATE_CACHE_BYTES / 1024 / 1024:.0f} MB"
                )
            if cache_key in self.scaled_templates_cache:
                self.performance.increment("template.scaled_cache_hits")
                self.scale_options_cache[str(cache_key)] = (s_min, s_max, s_step)
                return
            self.performance.increment("template.scaled_cache_misses")
            if self.scaled_template_cache_bytes + estimated > MAX_TEMPLATE_CACHE_BYTES:
                raise ValueError(
                    f"全部模板预计超过 {MAX_TEMPLATE_CACHE_BYTES / 1024 / 1024:.0f} MB 缓存上限，"
                    "请减少图片、缩放范围或缩放档位"
                )

            image = load_source_image(path)
            if normalized_mask_regions:
                image = apply_template_mask_regions(image, normalized_mask_regions)
                valid, reason = template_image_detail_status(image, use_gray)
                if not valid:
                    raise ValueError(f"忽略局部变化后剩余内容不足：{reason}")
                self.template_masked_cache_keys.add(str(cache_key))
            self.prepared_template_images_cache[str(cache_key)] = image
            self.scale_options_cache[str(cache_key)] = (s_min, s_max, s_step)
            template, template_mask = self.opencv_template_data(
                image, use_gray
            )
            # FR-20260812 文档#7: 透明度缓存写键必须与读取侧一致（(path, mtime,
            # size) 签名键）；此前写 abspath 字符串键、读签名键，预热写入是死写，
            # 读取侧永远缓存未命中重新检测。
            try:
                _stat = os.stat(path)
                _sig = (os.path.abspath(path), int(_stat.st_mtime_ns), int(_stat.st_size))
            except OSError:
                _sig = None
            if _sig is not None:
                self.template_transparency_cache[_sig] = bool(
                    template_transparency_info(path).has_transparency
                )
            self.base_templates_cache[cache_key] = template
            if template_mask is not None:
                self.base_template_masks_cache[cache_key] = template_mask

            templates_list = []
            actual_bytes = int(template.nbytes)
            scaled_masks = {}
            if template_mask is not None:
                actual_bytes += int(template_mask.nbytes)
            for scale in scale_values:
                if self.check_stop_flag():
                    raise RuntimeError("资源预加载已停止")
                rw = max(1, int(round(template.shape[1] * scale)))
                rh = max(1, int(round(template.shape[0] * scale)))
                interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
                resized_tpl = cv2.resize(template, (rw, rh), interpolation=interpolation)
                actual_bytes += int(resized_tpl.nbytes)
                templates_list.append((float(scale), resized_tpl))
                if template_mask is not None:
                    mask_interpolation = (
                        cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
                    )
                    resized_mask = cv2.resize(
                        template_mask,
                        (rw, rh),
                        interpolation=mask_interpolation,
                    )
                    resized_mask = np.clip(
                        resized_mask, 0.0, 1.0
                    ).astype(np.float32, copy=False)
                    scaled_masks[float(scale)] = resized_mask
                    actual_bytes += int(resized_mask.nbytes)
            self.scaled_templates_cache[cache_key] = templates_list
            if scaled_masks:
                self.scaled_template_masks_cache[cache_key] = scaled_masks
            self.scaled_template_cache_bytes += actual_bytes
            self.performance.increment("template.scale_variants", len(templates_list))
            self.performance.set_gauge(
                "template.cache_bytes", self.scaled_template_cache_bytes
            )

        for task_idx, task in enumerate(tasks, 1):
            if self.check_stop_flag():
                return False
            if config_bool(task.get("step_disabled", False)):
                continue
            try:
                cmd = float(task.get("type", 0))
            except (TypeError, ValueError):
                cmd = task.get("type")
            try:
                if cmd in IMAGE_TARGET_COMMANDS:
                    path = str(task.get("value", "")).strip()
                    if not path:
                        continue
                    # A coordinate is a valid non-image target; only a missing
                    # image path is an error (P2-1). Previously it was silently
                    # skipped, leaving the recognition step to wait forever.
                    if parse_coordinate_text(path):
                        continue
                    if not os.path.exists(path):
                        raise ValueError(
                            f"步骤 #{task_idx} 引用的模板图片不存在：{path}"
                        )
                    s_min, s_max, s_step, use_gray = self.image_target_options(task)

                    mask_regions = normalize_template_mask_regions(
                        task.get("template_mask_regions", [])
                    )
                    cache_key = self.template_cache_key(
                        path,
                        s_min,
                        s_max,
                        s_step,
                        use_gray,
                        mask_regions,
                    )
                    preload_one(
                        path,
                        cache_key,
                        s_min,
                        s_max,
                        s_step,
                        use_gray,
                        mask_regions,
                    )
                elif cmd in DRAG_COMMANDS:
                    # FR-20260930-013: 拖拽起/终点图片此前从不预计算，运行期又用
                    # `drag_source_*`/`drag_target_*` 自定义键，导致
                    # `scale_options_for` 回退全局默认倍率、
                    # `scaled_templates_cache` 永不命中——OpenCV 回退路径只搜 1.0
                    # 倍率，而原生路径按完整配置范围搜索，同一方案在两种后端下
                    # 结果翻转。此处与运行期共用
                    # `multi_image.drag_image_cache_key` 这一唯一构造点。
                    #
                    # 图片缺失时不在此抛错：`validation.py` 已在加载期拒绝缺失的
                    # 拖拽图片，运行期也会报「图片不存在」错误；预计算不引入新的
                    # 硬失败路径，仅跳过。
                    for field in ("drag_source_img", "drag_target_img"):
                        path = str(task.get(field, "") or "").strip()
                        if not path or not os.path.exists(path):
                            continue
                        s_min, s_max, s_step, use_gray = self.image_target_options(task)
                        mask_regions = normalize_template_mask_regions(
                            task.get("template_mask_regions", [])
                        )
                        cache_key = self.drag_image_cache_key(task, field)
                        preload_one(
                            path,
                            cache_key,
                            s_min,
                            s_max,
                            s_step,
                            use_gray,
                            mask_regions,
                        )
                elif cmd == TASK_TYPE_UNTIL:
                    for cond in self.until_conditions_from_task(task):
                        mode = cond.get("mode")
                        if mode == "区域发生变化":
                            continue
                        path = str(cond.get("image", "")).strip()
                        if not path:
                            continue
                        # P2-1: a referenced until-condition template that does
                        # not exist must fail loudly, not be silently skipped.
                        if not os.path.exists(path):
                            raise ValueError(
                                f"步骤 #{task_idx} 的“直到”条件引用的模板图片不存在：{path}"
                            )
                        if mode == "区域变成指定图片":
                            load_source_image(path)
                            continue
                        conf = self.condition_confidence(cond)
                        use_gray = self.enable_grayscale
                        cache_key = self.condition_cache_key(path, cond.get("index", 0), conf, use_gray)
                        preload_one(path, cache_key, self.min_scale, self.max_scale, self.scale_step, use_gray)
                elif cmd == TASK_TYPE_MULTI_IMAGE_ACTION:
                    s_min, s_max, s_step, use_gray = self.image_target_options(task)
                    rules = normalize_multi_image_rules(
                        task.get("multi_image_actions", [])
                    )
                    for rule in rules:
                        path = str(rule.get("image", "") or "").strip()
                        if not path:
                            continue
                        # P2-1: a referenced multi-image template that does not
                        # exist must fail loudly, not be silently skipped.
                        if not os.path.exists(path):
                            raise ValueError(
                                f"步骤 #{task_idx} 的多图动作引用的模板图片不存在：{path}"
                            )
                        # FR-SCOPE013-006: 与运行期共用同一构造函数，避免两处
                        # 拼接漂移导致预计算缓存永不命中。
                        cache_key = self.multi_image_rule_cache_key(
                            path, s_min, s_max, s_step, use_gray
                        )
                        preload_one(
                            path,
                            cache_key,
                            s_min,
                            s_max,
                            s_step,
                            use_gray,
                        )
            except Exception as error:
                errors.append(
                    (task_idx, safe_error_name(error), private_error_text(error))
                )

        if errors:
            for step_no, error_type, error_detail in errors[:20]:
                write_log(
                    f"预计算失败：第 {step_no} 步，错误类型 {error_type}",
                    private_message=(
                        f"预计算失败：第 {step_no} 步，{error_detail}"
                    ),
                             category=LOG_STEP,
                             critical=True,
                         )
            if len(errors) > 20:
                write_log(
                    f"预计算失败: 另有 {len(errors) - 20} 条错误未显示",
                    category=LOG_STEP,
                    critical=True,
                )
            return False
        write_log("资源预加载完成。",
                     category=LOG_BACKEND,
                 )
        return True
