# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Scene-signature capture plus wake/adaptive waiting.

This mixin is composed into ``fukua_rpa.engine.vision_exec.VisionExecutionMixin``; application code must not import it standalone.
"""

import logging
import os
import time

from fukua_rpa.log_policy import (
    LOG_ADAPTIVE,
)

from .scene_wake import (
    SceneSignature,
    compare_scene_sets,
    make_scene_signature,
    normalize_sensitivity,
)
from .shared import (
    _MAX_SCENE_SIGNATURE_CACHE,
)

logger = logging.getLogger(__name__)


def _scene_change_result(anchor, previous, current, sensitivity):
    """Return the first changed comparison between anchor/previous and current.

    FR-20260809-D4-⑤: 两个场景等待循环共用该判定——`previous is not anchor`
    的身份比较显式表达"本轮 previous 尚未覆盖 anchor 基线"的状态；未比较到
    变化返回 None。
    """

    comparisons = []
    if anchor:
        comparisons.append(
            compare_scene_sets(anchor, current, sensitivity=sensitivity)
        )
    if previous and previous is not anchor:
        comparisons.append(
            compare_scene_sets(previous, current, sensitivity=sensitivity)
        )
    return next(
        (result for result in comparisons if result.changed), None
    )


class VisionSceneMixin:
    """Scene-signature capture plus wake/adaptive waiting."""
    def recognition_wake_context(
        self,
        img_path,
        cache_key,
        task_conf,
        use_gray,
        search_regions=None,
        *,
        allow_fast_probe=True,
        allow_inverted=False,
    ):
        if not bool(getattr(self, "scene_wake_enabled", True)):
            return None
        path = os.path.abspath(str(img_path or ""))
        if not path or not os.path.exists(path):
            return None
        key = self.fast_match_key(
            path,
            cache_key,
            task_conf,
            use_gray,
            search_regions,
            allow_inverted,
        )
        return {
            "key": key,
            "img_path": path,
            "cache_key": cache_key,
            "task_conf": float(task_conf),
            "use_gray": bool(use_gray),
            "search_regions": search_regions,
            "allow_fast_probe": bool(allow_fast_probe and not allow_inverted),
            "allow_inverted": bool(allow_inverted),
        }

    def _native_physical_regions(self, search_regions):
        """FR-20260811-D7-001: 把输入/逻辑空间搜索区域换算为原生物理像素区域。

        与 shared._native_region_setup 同源（input_rect_to_physical_regions）；
        原生 DXGI 截图/指纹工作于物理像素空间，输入空间区域直传在混合 DPI
        （System DPI Aware + 副屏缩放 ≠ 主屏）下会错位。任一输入矩形映射为
        多个物理矩形（跨 DPI 屏）时返回 None，由调用方回退 Python 截图路径。
        """
        from fukua_rpa.coordinates import input_rect_to_physical_regions

        regions = self.effective_search_regions(search_regions)
        if not regions:
            regions = [self.screen_bounds()]
        physical = []
        for region in regions:
            rects = input_rect_to_physical_regions(region)
            if len(rects) != 1:
                return None
            physical.append(rects[0])
        return physical

    def capture_scene_signatures(self, search_regions=None):
        started_ns = time.perf_counter_ns()
        signatures = []
        try:
            native = getattr(self, "native_core", None)
            use_native_fingerprint = bool(
                getattr(self, "use_native_core", True)
                and not getattr(self, "_native_disabled_for_run", False)
                and native
                and native.available
                and getattr(native, "has_scene_fingerprint", False)
            )
            if use_native_fingerprint:
                regions = self._native_physical_regions(search_regions)
                if regions is None:
                    # 混合 DPI 多物理区域无法单区表达，走统一截图路径
                    # （原生/软件后端由 capture_screenshot 自行编排换算）。
                    use_native_fingerprint = False
                else:
                    for region in regions:
                        raw = native.capture_scene_signature(region)
                        if raw is None:
                            signatures.clear()
                            self.performance.increment(
                                "wake.native_scene_capture_failures"
                            )
                            break
                        width, height, pixels = raw
                        signatures.append(
                            SceneSignature(width, height, pixels)
                        )
                    if signatures:
                        self.performance.increment("wake.scene_captures")
                        self.performance.increment(
                            "wake.native_scene_captures", len(signatures)
                        )
                        return tuple(signatures)

            for screenshot_pil, _offset_x, _offset_y in self.iter_search_screenshots(
                search_regions
            ):
                if self.check_stop_flag():
                    return ()
                signature = make_scene_signature(screenshot_pil)
                if signature is not None:
                    signatures.append(signature)
            self.performance.increment("wake.scene_captures")
            return tuple(signatures)
        except Exception as exc:
            logger.debug("场景签名采集失败: %s", exc, exc_info=True)
            self.performance.increment("wake.scene_capture_failures")
            return ()
        finally:
            self.performance.observe_since("wake.scene_capture", started_ns)

    def begin_scene_monitor(self, context):
        native = getattr(self, "native_core", None)
        use_dxgi = bool(
            getattr(self, "use_native_core", True)
            and not getattr(self, "_native_disabled_for_run", False)
            and native
            and native.available
            and getattr(native, "has_dxgi_scene_change", False)
            and getattr(native, "dxgi_scene_usable", True)
        )
        if use_dxgi:
            try:
                regions = self._native_physical_regions(
                    context.get("search_regions")
                )
            except Exception as exc:
                # FR-20260811-D7-001: 区域换算异常不得向上传播（与
                # capture_scene_signatures 的外层 try 对称），回退 fingerprint。
                logger.debug("场景区域物理换算失败: %s", exc, exc_info=True)
                regions = None
            if regions is None:
                # FR-20260811-D7-001: 混合 DPI 多物理区域无法单区表达，
                # 回退 fingerprint 路径（其内部同样做物理换算）。
                use_dxgi = False
            else:
                cache = getattr(self, "scene_signature_cache", None)
                if cache is None:
                    self.scene_signature_cache = {}
                    cache = self.scene_signature_cache
                signatures = tuple(cache.get(context.get("key"), ()) or ())
                changed = native.poll_desktop_change(
                    regions,
                    reset_baseline=not bool(signatures),
                )
                if changed is not None:
                    if not signatures:
                        signatures = self.capture_scene_signatures(
                            context.get("search_regions")
                        )
                        if signatures:
                            if len(cache) >= _MAX_SCENE_SIGNATURE_CACHE:
                                for _k in list(cache.keys())[:_MAX_SCENE_SIGNATURE_CACHE // 2]:
                                    del cache[_k]
                            cache[context.get("key")] = signatures
                    self.performance.increment("wake.dxgi_baselines")
                    return {
                        "mode": "dxgi",
                        "regions": regions,
                        "signatures": signatures,
                        "initial_dirty": bool(changed),
                    }
                self.performance.increment("wake.dxgi_fallbacks")
        return {
            "mode": "fingerprint",
            "signatures": self.capture_scene_signatures(
                context.get("search_regions")
            ),
        }

    def wake_probe_scales(self, context):
        memory_context = self.scale_memory_context(
            context["img_path"],
            context["cache_key"],
            context["use_gray"],
        )
        if memory_context is None:
            return ()
        _key, _generation, _label, valid_scales = memory_context
        valid = tuple(dict.fromkeys(float(scale) for scale in valid_scales))
        preferred, _summary = self.preferred_scales_for(
            context["img_path"],
            context["cache_key"],
            context["use_gray"],
        )
        if not preferred:
            return ()
        selected = list(preferred[:8])
        remaining = [scale for scale in valid if scale not in selected]
        sensitivity = normalize_sensitivity(
            getattr(self, "scene_wake_sensitivity", "balanced")
        )
        batch_size = {
            "conservative": 1,
            "balanced": 2,
            "sensitive": 3,
        }[sensitivity]
        if remaining:
            cursors = getattr(self, "scene_probe_cursors", None)
            if cursors is None:
                self.scene_probe_cursors = {}
                cursors = self.scene_probe_cursors
            cursor = int(cursors.get(context["key"], 0)) % len(remaining)
            for offset in range(min(batch_size, len(remaining))):
                selected.append(remaining[(cursor + offset) % len(remaining)])
            cursors[context["key"]] = (cursor + batch_size) % len(remaining)
        return tuple(selected[:16])

    def probe_recognition_wake(self, context):
        if not context or not context.get("allow_fast_probe", True):
            return None
        scales = self.wake_probe_scales(context)
        if not scales:
            return None
        self.performance.increment("wake.scale_probes")
        self.performance.increment("wake.scale_variants", len(scales))
        started_ns = time.perf_counter_ns()
        try:
            found = self.find_target_scales_only(
                context["img_path"],
                context["cache_key"],
                context["task_conf"],
                context["use_gray"],
                scales,
                context.get("search_regions"),
            )
        finally:
            self.performance.observe_since("wake.scale_probe", started_ns)
        if found:
            pending = getattr(self, "pending_fast_matches", None)
            if pending is None:
                self.pending_fast_matches = {}
                pending = self.pending_fast_matches
            pending[context["key"]] = found
            self.performance.increment("wake.scale_probe_hits")
            if self.verbose_log_enabled(LOG_ADAPTIVE):
                self.log(
                    "<font color='gray'>    [画面唤醒] 常用/轮换倍率快速探测命中，"
                    f"倍率 {found[2]:g}x。</font>",
                    LOG_ADAPTIVE,
                )
        return found

    def wait_adaptive_scene(self, extra_delay, context, baseline=()):
        duration = max(0.0, float(extra_delay or 0.0))
        if duration <= 0.0 or not context:
            return not self.check_stop_flag()
        sensitivity = normalize_sensitivity(
            getattr(self, "scene_wake_sensitivity", "balanced")
        )
        dxgi_mode = bool(
            isinstance(baseline, dict)
            and baseline.get("mode") == "dxgi"
        )
        anchor = tuple(baseline.get("signatures", ()) or ()) if isinstance(baseline, dict) else tuple(baseline or ())
        deadline = time.monotonic() + duration

        if dxgi_mode:
            native = getattr(self, "native_core", None)
            dxgi_regions = baseline.get("regions", [])
            dxgi_dirty_pending = bool(baseline.get("initial_dirty", False))
            dxgi_confirmation_interval = {
                "conservative": 0.30,
                "balanced": 0.18,
                "sensitive": 0.08,
            }[sensitivity]
            interval = {
                "conservative": 0.10,
                "balanced": 0.06,
                "sensitive": 0.03,
            }[sensitivity]
            result, fallback_anchor = self._wait_dxgi_scene(
                context, sensitivity, anchor, deadline, native,
                dxgi_regions, dxgi_dirty_pending,
                dxgi_confirmation_interval, interval,
            )
            if result is not None:
                return result
            anchor = fallback_anchor

        return self._wait_fingerprint_scene(
            context, sensitivity, anchor, deadline
        )

    def _wait_dxgi_scene(
        self, context, sensitivity, anchor, deadline, native,
        dxgi_regions, dxgi_dirty_pending,
        dxgi_confirmation_interval, interval,
    ):
        """DXGI notification wait logic.

        Returns (result, fallback_anchor):
        - result is True/False when a definitive outcome is reached.
        - result is None when DXGI fell back to fingerprint mode;
          fallback_anchor holds the updated anchor for fingerprint polling.
        """
        previous = anchor
        dxgi_next_confirmation = 0.0
        first_check = True
        while time.monotonic() < deadline:
            if self.check_stop_flag():
                return False, None
            if not first_check:
                remaining = deadline - time.monotonic()
                time.sleep(min(interval, max(0.0, remaining)))
            first_check = False
            if self.check_stop_flag():
                return False, None
            changed = native.poll_desktop_change(dxgi_regions)
            self.performance.increment("wake.scene_checks")
            self.performance.increment("wake.dxgi_polls")
            if changed is None:
                self.performance.increment("wake.dxgi_fallbacks")
                fallback = self.capture_scene_signatures(
                    context.get("search_regions")
                )
                return None, (fallback or ())
            if changed:
                self.performance.increment("wake.dxgi_dirty_events")
                dxgi_dirty_pending = True
            if (
                dxgi_dirty_pending
                and time.monotonic() >= dxgi_next_confirmation
            ):
                dxgi_dirty_pending = False
                dxgi_next_confirmation = (
                    time.monotonic() + dxgi_confirmation_interval
                )
                current = self.capture_scene_signatures(
                    context.get("search_regions")
                )
                if not current:
                    return None, ()
                confirmed = _scene_change_result(
                    anchor, previous, current, sensitivity
                )
                if context.get("key") is not None:
                    if len(self.scene_signature_cache) >= _MAX_SCENE_SIGNATURE_CACHE:
                        for _k in list(self.scene_signature_cache.keys())[:_MAX_SCENE_SIGNATURE_CACHE // 2]:
                            del self.scene_signature_cache[_k]
                    self.scene_signature_cache[context.get("key")] = current
                previous = current
                if confirmed is not None:
                    self.performance.increment("wake.scene_triggers")
                    self.performance.increment(
                        "wake.dxgi_confirmed_changes"
                    )
                    if self.verbose_log_enabled(LOG_ADAPTIVE):
                        self.log(
                            "<font color='gray'>    [画面唤醒] DXGI 通知并经分块指纹确认变化，"
                            f"提前结束自适应等待（局部 {confirmed.peak_percent:.2f}%，"
                            f"整体 {confirmed.global_percent:.2f}%）。</font>",
                            LOG_ADAPTIVE,
                        )
                    self.probe_recognition_wake(context)
                    return (not self.check_stop_flag()), None
        return (not self.check_stop_flag()), None

    def _wait_fingerprint_scene(self, context, sensitivity, anchor, deadline):
        """Periodic fingerprint comparison loop for scene-change detection."""
        interval = {
            "conservative": 0.14,
            "balanced": 0.10,
            "sensitive": 0.07,
        }[sensitivity]
        native = getattr(self, "native_core", None)
        if not (
            getattr(self, "use_native_core", True)
            and native
            and native.available
            and getattr(native, "has_scene_fingerprint", False)
        ):
            interval = max(interval, 0.25)
        previous = anchor
        first_check = True
        while time.monotonic() < deadline:
            if self.check_stop_flag():
                return False
            if not first_check:
                remaining = deadline - time.monotonic()
                time.sleep(min(interval, max(0.0, remaining)))
            first_check = False
            if self.check_stop_flag():
                return False
            current = self.capture_scene_signatures(
                context.get("search_regions")
            )
            if not current:
                continue
            self.performance.increment("wake.scene_checks")
            changed = _scene_change_result(
                anchor, previous, current, sensitivity
            )
            if changed is not None:
                self.performance.increment("wake.scene_triggers")
                if self.verbose_log_enabled(LOG_ADAPTIVE):
                    self.log(
                        "<font color='gray'>    [画面唤醒] 检测到识别区域变化，"
                        f"提前结束自适应等待（局部 {changed.peak_percent:.2f}%，"
                        f"整体 {changed.global_percent:.2f}%）。</font>",
                        LOG_ADAPTIVE,
                    )
                self.probe_recognition_wake(context)
                return not self.check_stop_flag()
            previous = current
        return not self.check_stop_flag()
