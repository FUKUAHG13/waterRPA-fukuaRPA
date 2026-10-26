# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Screenshot capture, region geometry, scale-memory and key helpers shared across the vision mixins. Also hosts the module-level tuning constants and the optional mss availability flag.

This mixin is composed into ``fukua_rpa.engine.vision_exec.VisionExecutionMixin``; application code must not import it standalone.
"""

import ctypes
import logging
import math
import os
import threading
import time

from PIL import Image

from fukua_rpa.log_policy import (
    LOG_BACKEND,
)
from fukua_rpa.privacy import private_error_text, safe_error_name
from fukua_rpa.pyautogui_runtime import pyautogui

MAX_IMG_CACHE_ENTRIES = 64
MAX_MATCH_CANDIDATES = 2000
FAST_SEARCH_MIN_RADIUS = 120
_MAX_SCENE_SIGNATURE_CACHE = 512
try:
    import mss
    HAS_MSS = True
except ImportError:
    mss = None
    HAS_MSS = False

logger = logging.getLogger(__name__)

# DESIGN: 截图坐标空间来源的线程本地存储。capture_screenshot 与随后消费其结果的
# 识别函数始终在同一线程内顺序执行，而主执行线程与附属点击线程可能并发截图（且
# until 条件评估会在不持有 _vision_lock 的情况下截图）。若用普通实例属性记录来源，
# 可能被另一线程的截图覆盖，导致识别层读到错误的坐标空间标记。改用 threading.local
# 后，每个线程各自记录“最近一次截图是否为真实物理像素（原生 DXGI 后端）”，互不干扰；
# mss/pyautogui 截图记为 False（已是 SetCursorPos 期望的虚拟化坐标）。
_CAPTURE_PROVENANCE = threading.local()


def capture_returns_physical_pixels() -> bool:
    """当前线程最近一次截图是否返回真实物理像素坐标（原生 DXGI 后端）。

    返回 False 表示坐标已是输入注入期望的虚拟化空间（mss/pyautogui），或当前线程
    尚未截图。识别层据此决定命中坐标是否需要经 coordinates.physical_to_input_coordinates
    转换；该转换在 PMv2 激活时恒等返回，因此本标记不会改变已正确的路径。
    """
    return bool(getattr(_CAPTURE_PROVENANCE, "physical", False))


def restore_capture_provenance(physical: bool) -> None:
    """重新写回某张已采集图像的坐标空间来源标记。

    REVIEW-20260913-001 FR-RISK-005: 条件截图的周期缓存在命中时不重新截图，但
    ``capture_returns_physical_pixels()`` 描述的是"本线程最近一次截图"。若命中路径
    不回写，紧随其后的识别函数会读到**另一区域**的来源，对已处输入空间的命中坐标
    再做一次 ``physical_to_input_coordinates`` 虚拟化，在 System-DPI 回退路径上产生
    系统性偏移。调用方在缓存命中时用采集当时记录的布尔值调用本函数。
    """
    _CAPTURE_PROVENANCE.physical = bool(physical)


def match_input_dimensions(
    match,
    template_width: float,
    template_height: float,
) -> tuple[float, float]:
    """Return a match's width and height in the canonical input space.

    Recognition results use ``(x, y, scale, score, width, height)``.  The last
    two fields are important for native/raw captures because their physical
    pixels do not equal input coordinates in the bounded System-DPI fallback.
    Four-field results are still accepted for coordinate steps, tests and
    older internal callers; their dimensions are derived from template size.
    """

    try:
        if len(match) >= 6:
            width = float(match[4])
            height = float(match[5])
            if math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0:
                return width, height
    except (TypeError, ValueError, IndexError):
        pass
    try:
        scale = max(0.01, float(match[2]))
    except (TypeError, ValueError, IndexError):
        scale = 1.0
    return (
        max(1.0, float(template_width) * scale),
        max(1.0, float(template_height) * scale),
    )


class VisionSharedMixin:
    """Screenshot capture, region geometry, scale-memory and key helpers shared across the vision mixins. Also hosts the module-level tuning constants and the optional mss availability flag."""
    def create_mss_instance(self):
        factory = getattr(mss, "MSS", None) or mss.mss
        return factory()

    def recognition_key(self, img_path, step_info):
        step = step_info.get("step", 0) if step_info else 0
        return (step, os.path.abspath(str(img_path)))

    def record_recognition_miss(self, img_path, step_info):
        key = self.recognition_key(img_path, step_info)
        self.miss_streaks[key] = self.miss_streaks.get(key, 0) + 1
        return self.miss_streaks[key]

    def reset_recognition_miss(self, img_path, step_info):
        self.miss_streaks.pop(self.recognition_key(img_path, step_info), None)

    def as_bool(self, value):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "启用", "是")
        return bool(value)

    def normalized_regions(self, regions):
        result = []
        for region in regions or []:
            try:
                x, y, w, h = [int(float(v)) for v in region]
                if w > 0 and h > 0:
                    result.append((x, y, w, h))
            except Exception as exc:
                logger.warning("解析搜索区域失败: %s", exc, exc_info=True)
                continue
        return result

    def capture_screenshot(self, region=None):
        # DESIGN: 截图坐标空间来源标记。每次截图都在线程本地 _CAPTURE_PROVENANCE 中
        # 记录本次返回的图像/偏移处于哪个坐标空间，供识别层（capture_returns_physical_pixels）
        # 决定命中坐标是否需要经 coordinates.physical_to_input_coordinates 转换后再交给
        # SetCursorPos：
        #   - 原生 DLL（DXGI）后端不受进程 DPI 感知影响，始终返回“真实物理像素”
        #     坐标 -> 标记 True。
        #   - mss / pyautogui 后端跟随进程感知，在 System DPI Aware 下返回的就是
        #     SetCursorPos 期望的虚拟化坐标 -> 标记 False（无需转换）。
        # 仅当 PMv2 升级失败、进程停留在 System DPI Aware 且副屏 DPI ≠ 主屏时，物理
        # 像素与虚拟化坐标才会出现偏移；PMv2 激活时两者一致，转换函数恒等返回，本标记
        # 不改变任何行为。识别函数在截图之后同一线程内立即读取该标记，时序可靠。
        # FR-20260809-D4-③: 三段后端（native/mss/pyautogui）拆为私有方法，主函数
        # 只做回退编排；每段内部处理自己的性能观测、失败禁用标记与回退日志。
        total_started_ns = time.perf_counter_ns()
        try:
            native_capture_region, native_region_supported = (
                self._native_region_setup(region)
            )
            captured = self._capture_native(
                region, native_capture_region, native_region_supported
            )
            if captured is not None:
                return captured
            captured = self._capture_mss(region)
            if captured is not None:
                return captured
            return self._capture_pyautogui(region)
        finally:
            self.performance.observe_since("screenshot.total", total_started_ns)

    def _native_region_setup(self, region):
        """Precompute native backend eligibility and physical capture region."""
        native = getattr(self, "native_core", None)
        native_capture_region = None
        native_region_supported = True
        if region:
            from fukua_rpa.coordinates import input_rect_to_physical_regions

            physical_regions = input_rect_to_physical_regions(region)
            if len(physical_regions) == 1:
                native_capture_region = physical_regions[0]
            else:
                # One PIL image has only one offset. A mixed-DPI input rect
                # becomes multiple physical rects, so use the input-space
                # MSS/PyAutoGUI backend rather than inventing a wrong bbox.
                native_region_supported = False
                self.performance.increment(
                    "screenshot.native_cross_monitor_fallbacks"
                )
        elif native is not None:
            from fukua_rpa.coordinates import _ensure_physical_dpi_awareness

            # A System-DPI process cannot trust native GetSystemMetrics as
            # a physical full-desktop rectangle. The input-space fallbacks
            # remain authoritative until PMv2 is available.
            native_region_supported = bool(
                _ensure_physical_dpi_awareness()
            )
        return native_capture_region, native_region_supported

    def _capture_native(self, region, native_capture_region, native_region_supported):
        native = getattr(self, "native_core", None)
        if not (
            native_region_supported
            and getattr(self, "use_native_core", False)
            and native is not None
            and native.available
            and getattr(native, "has_raw_rgb_capture", False)
            and not getattr(self, "_native_capture_disabled_for_run", False)
        ):
            return None
        backend_started_ns = time.perf_counter_ns()
        try:
            # P1-2: 传入的 region 处于输入（虚拟化）坐标空间；原生 DXGI 截图
            # 工作在真实物理像素空间。交给 capture_rgb 前反算为物理像素。
            capture_region = (
                native_capture_region
                if region
                else native.virtual_screen_rect()
            )
            captured = native.capture_rgb(capture_region) if capture_region else None
            if captured is None:
                raise OSError(native.last_scene_error or "原生截图失败")
            width, height, pixels = captured
            image = Image.frombytes("RGB", (width, height), pixels)
            offset_x, offset_y = int(capture_region[0]), int(capture_region[1])
            self.performance.observe_since("screenshot.native", backend_started_ns)
            self.performance.increment("screenshot.native_calls")
            self.performance.increment("screenshot.pixels", width * height)
            # 原生 DXGI 截图返回真实物理像素坐标（见方法头部 DESIGN）。
            _CAPTURE_PROVENANCE.physical = True
            return image, offset_x, offset_y
        except Exception as error:
            self.performance.observe_since(
                "screenshot.native_failed", backend_started_ns
            )
            self.performance.increment("screenshot.native_failures")
            self.performance.increment("screenshot.fallbacks")
            self._native_capture_disabled_for_run = True
            if (
                self.verbose_log_enabled(LOG_BACKEND)
                and not self._native_capture_failure_logged
            ):
                self._native_capture_failure_logged = True
                self.log(
                    "<font color='orange'>    [截图] DLL原生截图失败，"
                    f"本次运行将回退到MSS/PyAutoGUI: {safe_error_name(error)}</font>",
                    LOG_BACKEND,
                    private_msg=(
                        "<font color='orange'>    [截图] DLL原生截图失败，"
                        "本次运行将回退到MSS/PyAutoGUI: "
                        f"{private_error_text(error)}</font>"
                    ),
                )
            return None

    def _capture_mss(self, region):
        if not (
            self.use_fast_screenshot
            and HAS_MSS
            and not getattr(self, "_mss_disabled_for_run", False)
        ):
            return None
        backend_started_ns = time.perf_counter_ns()
        try:
            instance = self._mss_instance
            if instance is None:
                instance = self.create_mss_instance()
                # double-check: 若另一线程已创建，丢弃本次创建的孤儿
                if self._mss_instance is None:
                    self._mss_instance = instance
                else:
                    instance = self._mss_instance
            if region:
                x, y, w, h = [int(v) for v in region]
                monitor = {"left": x, "top": y, "width": w, "height": h}
                offset_x, offset_y = x, y
            else:
                monitor = instance.monitors[0]
                offset_x = int(monitor.get("left", 0))
                offset_y = int(monitor.get("top", 0))
            shot = instance.grab(monitor)
            image = Image.frombytes("RGB", shot.size, shot.rgb)
            self.performance.observe_since("screenshot.mss", backend_started_ns)
            self.performance.increment("screenshot.mss_calls")
            self.performance.increment("screenshot.pixels", image.width * image.height)
            # mss 跟随进程 DPI 感知，返回虚拟化（输入）坐标，无需转换。
            _CAPTURE_PROVENANCE.physical = False
            return image, offset_x, offset_y
        except Exception as error:
            self.performance.observe_since("screenshot.mss_failed", backend_started_ns)
            self.performance.increment("screenshot.mss_failures")
            self.performance.increment("screenshot.fallbacks")
            self._mss_disabled_for_run = True
            if self.verbose_log_enabled(LOG_BACKEND) and not self._mss_failure_logged:
                self._mss_failure_logged = True
                self.log(
                    "<font color='orange'>    [截图] mss快速截图失败，"
                    f"本次运行将持续回退到pyautogui: {safe_error_name(error)}</font>",
                    LOG_BACKEND,
                    private_msg=(
                        "<font color='orange'>    [截图] mss快速截图失败，"
                        "本次运行将持续回退到pyautogui: "
                        f"{private_error_text(error)}</font>"
                    ),
                )
            self.close_screenshot_backend()
            return None

    def _capture_pyautogui(self, region):
        backend_started_ns = time.perf_counter_ns()
        screenshot_pil = pyautogui.screenshot(region=region)
        self.performance.observe_since("screenshot.pyautogui", backend_started_ns)
        self.performance.increment("screenshot.pyautogui_calls")
        self.performance.increment(
            "screenshot.pixels", screenshot_pil.width * screenshot_pil.height
        )
        offset_x = region[0] if region else 0
        offset_y = region[1] if region else 0
        # pyautogui 跟随进程 DPI 感知，返回虚拟化（输入）坐标，无需转换。
        _CAPTURE_PROVENANCE.physical = False
        return screenshot_pil, offset_x, offset_y

    def region_bounding_rect(self, regions):
        left = min(r[0] for r in regions)
        top = min(r[1] for r in regions)
        right = max(r[0] + r[2] for r in regions)
        bottom = max(r[1] + r[3] for r in regions)
        return (left, top, right - left, bottom - top)

    def screen_bounds(self):
        if HAS_MSS and not getattr(self, "_mss_disabled_for_run", False):
            try:
                if self._mss_instance is None:
                    self._mss_instance = self.create_mss_instance()
                monitor = self._mss_instance.monitors[0]
                return (int(monitor.get("left", 0)), int(monitor.get("top", 0)), int(monitor["width"]), int(monitor["height"]))
            except Exception as exc:
                logger.debug("获取屏幕边界失败: %s", exc, exc_info=True)
                self.performance.increment("screenshot.mss_bounds_failures")
                self._mss_disabled_for_run = True
                self.close_screenshot_backend()
        # Fallback when MSS is unavailable: cover the whole virtual desktop
        # (all monitors, possibly with a negative origin) rather than only the
        # primary screen (P3-1). Prefer Win32 virtual-screen metrics; degrade to
        # pyautogui primary size only if the Win32 call is unavailable.
        try:
            user32 = ctypes.windll.user32
            # SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN / SM_CXVIRTUALSCREEN /
            # SM_CYVIRTUALSCREEN — the bounding rect of every display.
            left = int(user32.GetSystemMetrics(76))
            top = int(user32.GetSystemMetrics(77))
            width = int(user32.GetSystemMetrics(78))
            height = int(user32.GetSystemMetrics(79))
            if width > 0 and height > 0:
                return (left, top, width, height)
        except Exception as exc:
            logger.debug("获取虚拟桌面边界失败: %s", exc, exc_info=True)
        w, h = pyautogui.size()
        return (0, 0, int(w), int(h))

    def clip_region_to_bounds(self, region, bounds):
        x, y, w, h = [int(v) for v in region]
        bx, by, bw, bh = [int(v) for v in bounds]
        left = max(x, bx)
        top = max(y, by)
        right = min(x + w, bx + bw)
        bottom = min(y + h, by + bh)
        if right <= left or bottom <= top:
            return None
        return (left, top, right - left, bottom - top)

    def should_batch_regions(self, regions):
        if len(regions) < 2:
            return False
        total_area = sum(w * h for _x, _y, w, h in regions)
        _bx, _by, bw, bh = self.region_bounding_rect(regions)
        bbox_area = bw * bh
        return bbox_area <= max(total_area * 3, total_area + 50000)

    @staticmethod
    def _physical_crop_rect(x, y, w, h):
        """Convert an input-space region to its physical-pixel rect (FR-003).

        仅在截图 provenance 为物理像素时使用。跨显示器时输入区域会被拆成多个物理
        矩形，单个裁剪窗口无法表达，故抛 ``ValueError``。

        该异常的处置取决于抛出时机（见 ``iter_search_screenshots`` 的批量分支）：
        **仅当本批次尚未产出任何区域**时才回退到既有的"逐区域截图"（保守：宁可回退，
        也不静默给错窗口）；一旦已经产出过部分区域，则**如实抛出而不回退**，否则同一
        区域会被产出两次、消费方重复识别。
        """
        from fukua_rpa.coordinates import input_rect_to_physical_regions

        regions = input_rect_to_physical_regions((x, y, w, h))
        if len(regions) != 1:
            raise ValueError(
                f"批量裁剪无法用单个物理矩形表达跨屏区域：{(x, y, w, h)}"
            )
        return regions[0]

    def effective_search_regions(self, search_regions=None):
        explicit_regions = self.normalized_regions(search_regions)
        if explicit_regions:
            return explicit_regions
        active_regions = self.normalized_regions(getattr(self, "scan_regions", []))
        if active_regions:
            return active_regions
        if self.scan_region:
            return self.normalized_regions([self.scan_region])
        return []

    def iter_search_screenshots(self, search_regions=None):
        active_regions = self.effective_search_regions(search_regions)
        if not active_regions:
            yield self.capture_screenshot(None)
            return

        if self.should_batch_regions(active_regions):
            bbox = self.region_bounding_rect(active_regions)
            yielded_any = False
            try:
                screenshot_pil, offset_x, offset_y = self.capture_screenshot(bbox)
                # SAFETY/FR-003: 裁剪框必须与截图偏移处于**同一坐标空间**。
                # 原生 DLL 截图后端是 GDI、跟随**进程** DPI 感知：PMv2 激活时其
                # 图像/偏移与输入空间一致；System-DPI Aware 回退（PMv2 升级失败）
                # 且副屏缩放 ≠ 系统 DPI 时可能是**物理像素**（该回退态尚未实机验证，
                # 见 FR-RISK-20261003-621），而 active_regions 是**输入空间**坐标。
                # 此前直接用输入空间 x/y 减物理偏移，裁剪窗口整体偏移（识别在错误
                # 区域搜索）；越界时 PIL 静默补黑，也不会触发下方的逐区域回退。此处
                # 按 provenance 先把请求区域换算到图像所在空间。输入空间与物理空间
                # 一致时（PMv2 激活）换算恒等，本改动不改变任何行为。
                physical_pixels = capture_returns_physical_pixels()
                for x, y, w, h in active_regions:
                    target = (
                        self._physical_crop_rect(x, y, w, h)
                        if physical_pixels
                        else (x, y, w, h)
                    )
                    target_x, target_y, target_w, target_h = target
                    crop_started_ns = time.perf_counter_ns()
                    crop_box = (
                        target_x - offset_x,
                        target_y - offset_y,
                        target_x - offset_x + target_w,
                        target_y - offset_y + target_h,
                    )
                    cropped = screenshot_pil.crop(crop_box)
                    self.performance.observe_since("screenshot.region_crop", crop_started_ns)
                    self.performance.increment("screenshot.region_crops")
                    # INVARIANT: 标记必须在 yield 之前置位——一旦已经产出过区域，
                    # 下面的 except 不得再回退到逐区域循环，否则同一区域会被产出两次
                    # （消费方会重复识别）。跨屏 ValueError 使该路径可达，故必须守住。
                    yielded_any = True
                    # FR-20260930-002: 裁剪图像与 crop_box 已按 provenance 换算到
                    # 物理像素空间，yield 的偏移必须与图像同空间；此前固定返回输入
                    # 空间 (x, y)，使消费侧 `res.left + offset` 混入两种量纲，在
                    # System-DPI 回退下产生系统性偏移。PMv2 下 target == (x, y)
                    # 恒等，本改动不改变既有行为。
                    yield cropped, target_x, target_y
                return
            except Exception as e:
                if yielded_any:
                    # 已产出部分区域：不能再走逐区域回退（会重复产出），如实抛出。
                    raise
                if self.verbose_log_enabled(LOG_BACKEND):
                    self.log(
                        f"<font color='orange'>    [截图] 多区域合并截图失败，改为逐区域截图: {safe_error_name(e)}</font>",
                        LOG_BACKEND,
                        private_msg=(
                            "<font color='orange'>    [截图] 多区域合并截图失败，改为逐区域截图: "
                            f"{private_error_text(e)}</font>"
                        ),
                    )

        for region in active_regions:
            if self.check_stop_flag():
                return
            yield self.capture_screenshot(region)

    def target_position_key(
        self, img_path, cache_key, task_conf, use_gray, allow_inverted=False
    ):
        return (
            os.path.abspath(str(img_path)),
            str(cache_key),
            float(task_conf),
            bool(use_gray),
            bool(allow_inverted),
        )
