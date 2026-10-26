# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Template validation, cache estimation, and optional native matching adapter.

The native DLL is an optimization, not a second recognition definition. ABI
and capability checks protect OpenCV-equivalent semantics before use.
"""

import ctypes
import logging
import math
import os
import sys
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageStat

from fukua_rpa.constants import (
    MAX_NATIVE_CAPTURE_PIXELS,
    MAX_SCALES_PER_TEMPLATE,
    MAX_TEMPLATE_SOURCE_PIXELS,
    NATIVE_CORE_DLL_NAME,
    NATIVE_CORE_MAX_VERSION,
    NATIVE_CORE_MIN_VERSION,
)
from fukua_rpa.errors import MatchFailure
from fukua_rpa.native_services import (
    SCREENSHOT_SERVICE_CONTRACT_VERSION,
    VISION_SERVICE_CONTRACT_VERSION,
    BackendServiceDescriptor,
)
from fukua_rpa.params import MatchOptions
from fukua_rpa.paths import get_base_dir

logger = logging.getLogger(__name__)

NATIVE_CAPABILITY_BITS = {
    "gdi_capture": 1 << 0,
    "multi_region": 1 << 1,
    "multi_scale": 1 << 2,
    "grayscale": 1 << 3,
    "color": 1 << 4,
    "find_all": 1 << 5,
    "template_lru": 1 << 6,
    "performance_counters": 1 << 7,
    "work_budget": 1 << 8,
    "single_capture_per_region": 1 << 9,
    "abi_metadata": 1 << 10,
    "bounded_job_pool": 1 << 11,
    "preferred_scale_fallback": 1 << 12,
    "preferred_scale_list": 1 << 13,
    "explicit_scale_only": 1 << 14,
    "low_res_scene_fingerprint": 1 << 15,
    "dxgi_scene_change": 1 << 16,
    "cancellable_search": 1 << 17,
    "opencv_color_semantics": 1 << 18,
    "raw_rgb_capture": 1 << 19,
}
NATIVE_BUILD_FLAG_BITS = {
    "x64": 1 << 0,
    "static_crt": 1 << 1,
    "cpp17": 1 << 2,
    "windows10_target": 1 << 3,
    "msvc": 1 << 4,
}
NATIVE_STRUCT_IDS = {"rect": 1, "match": 2, "performance": 3}
NATIVE_RESULT_WORK_BUDGET = -2
NATIVE_RESULT_CANCELLED = -3
NATIVE_PARALLEL_MODES = {"off": 0, "auto": 1, "force": 2}
NATIVE_VISION_SERVICE_CONTRACT_VERSION = VISION_SERVICE_CONTRACT_VERSION
MAX_TEMPLATE_MASK_REGIONS = 32


@dataclass(frozen=True)
class TemplateTransparencyInfo:
    """Alpha-channel facts used by validation, matching, and UI guidance."""

    has_alpha_channel: bool
    has_transparency: bool
    fully_transparent: bool
    visible_pixels: int
    total_pixels: int


def template_alpha_channel(image):
    """Return a meaningful alpha channel, or ``None`` for an opaque image."""

    if "A" not in image.getbands() and "transparency" not in image.info:
        return None
    alpha = image.convert("RGBA").getchannel("A")
    minimum, _maximum = alpha.getextrema()
    return alpha if int(minimum) < 255 else None


def _template_transparency_info(image):
    total_pixels = int(image.width * image.height)
    has_alpha_channel = (
        "A" in image.getbands() or "transparency" in image.info
    )
    alpha = template_alpha_channel(image)
    if alpha is None:
        return TemplateTransparencyInfo(
            has_alpha_channel=has_alpha_channel,
            has_transparency=False,
            fully_transparent=False,
            visible_pixels=total_pixels,
            total_pixels=total_pixels,
        )
    histogram = alpha.histogram()
    visible_pixels = total_pixels - int(histogram[0])
    return TemplateTransparencyInfo(
        has_alpha_channel=True,
        has_transparency=True,
        fully_transparent=visible_pixels <= 0,
        visible_pixels=visible_pixels,
        total_pixels=total_pixels,
    )


def template_transparency_info(image_path):
    """Inspect whether a template contains pixels whose alpha is below 255."""

    with Image.open(image_path) as source:
        return _template_transparency_info(source)


def normalize_template_mask_regions(value: Any) -> tuple[tuple[float, float, float, float], ...]:
    """Return bounded normalized rectangles used to ignore template pixels."""

    if not isinstance(value, (list, tuple)):
        return ()
    normalized: list[tuple[float, float, float, float]] = []
    seen: set[tuple[float, float, float, float]] = set()
    for raw in value:
        if len(normalized) >= MAX_TEMPLATE_MASK_REGIONS:
            break
        try:
            values = (raw.get("x"), raw.get("y"), raw.get("w"), raw.get("h")) if isinstance(raw, dict) else tuple(raw)
            if len(values) != 4:
                continue
            x, y, width, height = (float(item) for item in values)
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(item) for item in (x, y, width, height)):
            continue
        left = max(0.0, min(1.0, x))
        top = max(0.0, min(1.0, y))
        right = max(left, min(1.0, x + width))
        bottom = max(top, min(1.0, y + height))
        if right - left <= 1e-6 or bottom - top <= 1e-6:
            continue
        region = tuple(round(item, 8) for item in (left, top, right - left, bottom - top))
        if region in seen:
            continue
        seen.add(region)
        normalized.append(region)
    return tuple(normalized)


def template_mask_signature(value: Any) -> str:
    """Compute a stable string signature from normalized mask regions.

    Args:
        value: Raw mask-region data (list of dicts or tuples).

    Returns:
        Semicolon-separated signature suitable for cache-key comparison.
    """
    regions = normalize_template_mask_regions(value)
    return ";".join(
        ",".join(f"{item:.8f}" for item in region) for region in regions
    )


def apply_template_mask_regions(image, value: Any):
    """Return a detached RGBA image with configured rectangles made transparent."""

    regions = normalize_template_mask_regions(value)
    if not regions:
        return image.copy()
    result = image.convert("RGBA")
    alpha = result.getchannel("A")
    draw = ImageDraw.Draw(alpha)
    width, height = result.size
    for x, y, region_width, region_height in regions:
        left = max(0, min(width - 1, int(math.floor(x * width))))
        top = max(0, min(height - 1, int(math.floor(y * height))))
        right = max(left, min(width - 1, int(math.ceil((x + region_width) * width)) - 1))
        bottom = max(top, min(height - 1, int(math.ceil((y + region_height) * height)) - 1))
        draw.rectangle((left, top, right, bottom), fill=0)
    result.putalpha(alpha)
    return result


def build_scale_values(min_scale, max_scale, scale_step, max_count=MAX_SCALES_PER_TEMPLATE):
    """Return extra scale variants; the original 1.0 template is searched separately."""
    min_scale = float(min_scale)
    max_scale = float(max_scale)
    scale_step = float(scale_step)
    if not all(math.isfinite(value) for value in (min_scale, max_scale, scale_step)):
        raise ValueError("缩放参数必须是有限数字")
    if min_scale <= 0 or max_scale <= 0:
        raise ValueError("缩放范围必须大于 0")
    if max_scale < min_scale:
        raise ValueError("最大缩放不能小于最小缩放")
    if scale_step <= 0:
        raise ValueError("缩放步长必须大于 0")

    values = []
    scale = min_scale
    guard = 0
    while scale <= max_scale + scale_step * 0.25:
        if not 0.99 < scale < 1.01:
            values.append(float(round(scale, 8)))
        guard += 1
        if 1 + len(values) > max_count or guard > max_count * 2:
            raise ValueError(f"单张图片最多允许 {max_count} 个缩放档位（包含原始 1.0 倍）")
        scale = min_scale + guard * scale_step
    return values


def template_image_detail_status(source, use_gray=True, min_stddev=1.0):
    """Validate one already-loaded template image."""

    width, height = source.size
    if width < 3 or height < 3:
        return False, "模板尺寸至少需要 3×3 像素"
    if width * height > MAX_TEMPLATE_SOURCE_PIXELS:
        return False, f"模板像素过多，最多允许 {MAX_TEMPLATE_SOURCE_PIXELS:,} 像素"
    sample = source.convert("L" if use_gray else "RGB")
    alpha = template_alpha_channel(source)
    detail_mask = None
    if alpha is not None:
        histogram = alpha.histogram()
        visible_pixels = sum(histogram[8:])
        if visible_pixels <= 0:
            return False, "模板完全透明，无法识别"
        visible_mask = alpha.point(lambda value: 255 if value >= 8 else 0)
        visible_box = visible_mask.getbbox()
        if visible_pixels < 9 or visible_box is None:
            return False, "模板中可见内容过少，无法安全识别"
        if (
            visible_box[2] - visible_box[0] < 3
            or visible_box[3] - visible_box[1] < 3
        ):
            return False, "模板中可见内容至少需要覆盖 3×3 像素"
    sample.thumbnail((512, 512))
    if alpha is not None:
        detail_mask = alpha.resize(sample.size, Image.Resampling.LANCZOS)
        detail_mask = detail_mask.point(
            lambda value: 255 if value >= 8 else 0
        )
    standard_deviation = ImageStat.Stat(sample, mask=detail_mask).stddev
    detail = max(float(value) for value in standard_deviation) if standard_deviation else 0.0
    if not math.isfinite(detail) or detail < float(min_stddev):
        return False, "图片几乎是纯色或缺少纹理，无法安全识别；请扩大截图并包含更多独特细节"
    return True, ""


def template_detail_status(image_path, use_gray=True, min_stddev=1.0):
    """Reject tiny or flat templates that make normalized correlation unsafe."""
    try:
        with Image.open(image_path) as source:
            return template_image_detail_status(source, use_gray, min_stddev)
    except Exception as error:
        return False, f"无法读取模板图片：{error}"


def estimate_template_cache_bytes(image_path, use_gray, scale_values, *, has_mask=False):
    """Estimate the in-memory byte cost of caching a template at all scales.

    Args:
        image_path: Path to the template image file.
        use_gray: Whether the template will be cached in grayscale.
        scale_values: Iterable of extra scale factors to include.
        has_mask: Whether an explicit mask is applied.

    Returns:
        Estimated total cache size in bytes.

    Raises:
        ValueError: If the image cannot be read or exceeds pixel limits.
    """
    try:
        with Image.open(image_path) as source:
            width, height = source.size
            mask_bytes_per_pixel = 4 if (
                has_mask or template_alpha_channel(source) is not None
            ) else 0
    except Exception as error:
        raise ValueError(f"无法读取模板图片：{error}") from error
    if width * height > MAX_TEMPLATE_SOURCE_PIXELS:
        raise ValueError(f"模板像素过多，最多允许 {MAX_TEMPLATE_SOURCE_PIXELS:,} 像素")
    channels = 1 if use_gray else 3
    bytes_per_pixel = channels + mask_bytes_per_pixel
    estimated = width * height * bytes_per_pixel
    for scale in scale_values:
        estimated += (
            max(1, int(round(width * scale)))
            * max(1, int(round(height * scale)))
            * bytes_per_pixel
        )
    return int(estimated)


class NativeRect(ctypes.Structure):
    """ABI-compatible screen rectangle (x, y, width, height) for the native DLL."""

    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("w", ctypes.c_int),
        ("h", ctypes.c_int),
    ]


class NativeMatch(ctypes.Structure):
    """ABI-compatible single match result (position, scale, score, radius)."""

    _fields_ = [
        ("x", ctypes.c_double),
        ("y", ctypes.c_double),
        ("scale", ctypes.c_double),
        ("score", ctypes.c_double),
        ("radius", ctypes.c_double),
    ]


class NativePerfStats(ctypes.Structure):
    """ABI-compatible performance counters exposed by the native DLL."""

    _fields_ = [
        ("calls", ctypes.c_int64),
        ("captures", ctypes.c_int64),
        ("integral_builds", ctypes.c_int64),
        ("template_cache_hits", ctypes.c_int64),
        ("template_cache_misses", ctypes.c_int64),
        ("template_variants_built", ctypes.c_int64),
        ("work_budget_fallbacks", ctypes.c_int64),
        ("capture_microseconds", ctypes.c_int64),
        ("template_microseconds", ctypes.c_int64),
        ("match_microseconds", ctypes.c_int64),
        ("cache_bytes", ctypes.c_int64),
        ("cache_entries", ctypes.c_int64),
    ]


class NativeVisionCore:
    """Adapter that loads, validates, and drives the optional native vision DLL.

    Performs ABI/capability handshakes on load and exposes template matching,
    scene fingerprinting, DXGI change detection, and raw RGB capture through
    a versioned C interface. Falls back gracefully when the DLL is absent.
    """

    # FR-20260809-D4-④: 版本能力门数据表（替代逐版本 if 链）——
    # (最低版本, 需声明能力掩码, 需具备接口属性, 能力缺失错误, 接口缺失错误)。
    # 新增 ABI 版本时在此追加一行即可；interface_attr 为 None 表示该级无接口检查。
    _NATIVE_CAPABILITY_GATES = (
        (
            10800,
            NATIVE_CAPABILITY_BITS["bounded_job_pool"]
            | NATIVE_CAPABILITY_BITS["preferred_scale_fallback"],
            "has_extended_search",
            "DLL API 10800+ 未声明多核调度或缩放回退能力",
            "DLL API 10800+ 缺少扩展识别接口",
        ),
        (
            10900,
            NATIVE_CAPABILITY_BITS["preferred_scale_list"],
            "has_preferred_scale_list",
            "DLL API 10900+ 未声明多倍率优先能力",
            "DLL API 10900+ 缺少多倍率扩展识别接口",
        ),
        (
            11000,
            NATIVE_CAPABILITY_BITS["explicit_scale_only"],
            "has_explicit_scale_only",
            "DLL API 11000+ 未声明指定倍率专搜能力",
            "DLL API 11000+ 缺少指定倍率专搜接口",
        ),
        (
            11100,
            NATIVE_CAPABILITY_BITS["low_res_scene_fingerprint"],
            "has_scene_fingerprint",
            "DLL API 11100+ 未声明低分辨率画面指纹能力",
            "DLL API 11100+ 缺少低分辨率画面指纹接口",
        ),
        (
            11200,
            NATIVE_CAPABILITY_BITS["dxgi_scene_change"],
            "has_dxgi_scene_change",
            "DLL API 11200+ 未声明 DXGI 画面变化能力",
            "DLL API 11200+ 缺少 DXGI 画面变化接口",
        ),
        (
            11300,
            NATIVE_CAPABILITY_BITS["cancellable_search"],
            "has_cancellable_search",
            "DLL API 11300+ 未声明可取消识别能力",
            "DLL API 11300+ 缺少识别取消接口",
        ),
        (
            11400,
            NATIVE_CAPABILITY_BITS["opencv_color_semantics"],
            None,
            "DLL API 11400+ 未声明 OpenCV 彩色匹配语义",
            None,
        ),
        (
            11500,
            NATIVE_CAPABILITY_BITS["raw_rgb_capture"],
            "has_raw_rgb_capture",
            "DLL API 11500+ 未声明原始 RGB 截图能力",
            "DLL API 11500+ 缺少原始 RGB 截图接口",
        ),
    )

    def __init__(self, base_dir=None):
        self.last_failure_category = None
        self.base_dir = os.path.abspath(base_dir or get_base_dir())
        self.dll = None
        self.available = False
        self.load_error = ""
        self.version = 0
        self.capability_mask = 0
        self.abi_bits = 0
        self.build_flag_mask = 0
        self.native_struct_sizes = {}
        self.has_extended_search = False
        self.has_preferred_scale_list = False
        self.has_explicit_scale_only = False
        self.has_scene_fingerprint = False
        self.has_dxgi_scene_change = False
        self.has_cancellable_search = False
        self.has_raw_rgb_capture = False
        self.dxgi_scene_usable = True
        self.dll_path = ""
        self.last_result_code = 0
        # Thread safety: last_result_code and load_error are only written by
        # find_template, which the engine calls under its _vision_lock on the
        # main matching path (vision/match.py:635/:1162). FR-20260812-D7-001
        # （文档修正）: 存在不持锁直达 find_template 的例外路径
        # （find_target_scales_only，vision/match.py:95），当前靠"引擎单线程
        # 不变量"保证安全；若未来引入并发识别，必须先在这些入口补锁再扩展
        # 调用方。
        # cancel_active_search only invokes the DLL cancel entry point and does
        # not read or write these Python attributes.  No additional lock needed.
        # Reusable output buffer for find_template (also guarded by the engine's
        # _vision_lock on the main path); grown on demand to avoid per-call allocation.
        self._match_buffer = None
        self._match_buffer_size = 0
        self.last_scene_error = ""
        self._load()

    def _candidate_paths(self):
        bases = [self.base_dir]
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            bases.append(sys._MEIPASS)
        bases.append(os.path.join(self.base_dir, "native_core"))
        for base in bases:
            yield os.path.join(base, NATIVE_CORE_DLL_NAME)

    def _load(self):
        last_error = ""
        for dll_path in self._candidate_paths():
            if not os.path.exists(dll_path):
                continue
            try:
                dll = ctypes.CDLL(dll_path)
                dll.wrpa_version.argtypes = []
                dll.wrpa_version.restype = ctypes.c_int
                dll.wrpa_find_template.argtypes = [
                    ctypes.POINTER(ctypes.c_wchar),
                    ctypes.POINTER(NativeRect),
                    ctypes.c_int,
                    ctypes.c_double,
                    ctypes.c_double,
                    ctypes.c_double,
                    ctypes.c_int,
                    ctypes.c_double,
                    ctypes.c_int,
                    ctypes.POINTER(NativeMatch),
                    ctypes.c_int,
                    ctypes.POINTER(ctypes.c_int),
                    ctypes.c_wchar_p,
                    ctypes.c_int,
                ]
                dll.wrpa_find_template.restype = ctypes.c_int
                self.version = int(dll.wrpa_version())
                if not NATIVE_CORE_MIN_VERSION <= self.version <= NATIVE_CORE_MAX_VERSION:
                    raise RuntimeError(
                        f"DLL接口版本不兼容：{self.version}，"
                        f"需要 {NATIVE_CORE_MIN_VERSION}-{NATIVE_CORE_MAX_VERSION}"
                    )
                if hasattr(dll, "wrpa_capabilities"):
                    dll.wrpa_capabilities.argtypes = []
                    dll.wrpa_capabilities.restype = ctypes.c_uint64
                    self.capability_mask = int(dll.wrpa_capabilities())
                else:
                    self.capability_mask = self._legacy_capability_mask()
                if self.version >= 10700 and not (
                    self.capability_mask & NATIVE_CAPABILITY_BITS["abi_metadata"]
                ):
                    raise RuntimeError("DLL API 10700+ 未声明 ABI 元数据能力")
                if hasattr(dll, "wrpa_find_template_ex"):
                    dll.wrpa_find_template_ex.argtypes = [
                        ctypes.POINTER(ctypes.c_wchar),
                        ctypes.POINTER(NativeRect),
                        ctypes.c_int,
                        ctypes.c_double,
                        ctypes.c_double,
                        ctypes.c_double,
                        ctypes.c_int,
                        ctypes.c_double,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_double,
                        ctypes.POINTER(NativeMatch),
                        ctypes.c_int,
                        ctypes.POINTER(ctypes.c_int),
                        ctypes.c_wchar_p,
                        ctypes.c_int,
                    ]
                    dll.wrpa_find_template_ex.restype = ctypes.c_int
                    self.has_extended_search = True
                if hasattr(dll, "wrpa_find_template_ex2"):
                    dll.wrpa_find_template_ex2.argtypes = [
                        ctypes.POINTER(ctypes.c_wchar),
                        ctypes.POINTER(NativeRect),
                        ctypes.c_int,
                        ctypes.c_double,
                        ctypes.c_double,
                        ctypes.c_double,
                        ctypes.c_int,
                        ctypes.c_double,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.POINTER(ctypes.c_double),
                        ctypes.c_int,
                        ctypes.POINTER(NativeMatch),
                        ctypes.c_int,
                        ctypes.POINTER(ctypes.c_int),
                        ctypes.c_wchar_p,
                        ctypes.c_int,
                    ]
                    dll.wrpa_find_template_ex2.restype = ctypes.c_int
                    self.has_preferred_scale_list = True
                if hasattr(dll, "wrpa_find_template_ex3"):
                    dll.wrpa_find_template_ex3.argtypes = [
                        ctypes.POINTER(ctypes.c_wchar),
                        ctypes.POINTER(NativeRect),
                        ctypes.c_int,
                        ctypes.c_double,
                        ctypes.c_double,
                        ctypes.c_double,
                        ctypes.c_int,
                        ctypes.c_double,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.POINTER(ctypes.c_double),
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.POINTER(NativeMatch),
                        ctypes.c_int,
                        ctypes.POINTER(ctypes.c_int),
                        ctypes.c_wchar_p,
                        ctypes.c_int,
                    ]
                    dll.wrpa_find_template_ex3.restype = ctypes.c_int
                    self.has_explicit_scale_only = True
                if hasattr(dll, "wrpa_capture_gray_fingerprint"):
                    dll.wrpa_capture_gray_fingerprint.argtypes = [
                        ctypes.POINTER(NativeRect),
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.POINTER(ctypes.c_ubyte),
                        ctypes.c_int,
                        ctypes.c_wchar_p,
                        ctypes.c_int,
                    ]
                    dll.wrpa_capture_gray_fingerprint.restype = ctypes.c_int
                    self.has_scene_fingerprint = True
                if hasattr(dll, "wrpa_poll_desktop_change"):
                    dll.wrpa_poll_desktop_change.argtypes = [
                        ctypes.POINTER(NativeRect),
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_wchar_p,
                        ctypes.c_int,
                    ]
                    dll.wrpa_poll_desktop_change.restype = ctypes.c_int
                    self.has_dxgi_scene_change = True
                if hasattr(dll, "wrpa_cancel_active_search"):
                    dll.wrpa_cancel_active_search.argtypes = []
                    dll.wrpa_cancel_active_search.restype = None
                    self.has_cancellable_search = True
                if (
                    hasattr(dll, "wrpa_get_virtual_screen_rect")
                    and hasattr(dll, "wrpa_capture_rgb")
                ):
                    dll.wrpa_get_virtual_screen_rect.argtypes = [
                        ctypes.POINTER(NativeRect),
                        ctypes.c_wchar_p,
                        ctypes.c_int,
                    ]
                    dll.wrpa_get_virtual_screen_rect.restype = ctypes.c_int
                    dll.wrpa_capture_rgb.argtypes = [
                        ctypes.POINTER(NativeRect),
                        ctypes.POINTER(ctypes.c_ubyte),
                        ctypes.c_int,
                        ctypes.c_wchar_p,
                        ctypes.c_int,
                    ]
                    dll.wrpa_capture_rgb.restype = ctypes.c_int
                    self.has_raw_rgb_capture = True
                for (
                    min_version,
                    required_mask,
                    interface_attr,
                    cap_error,
                    iface_error,
                ) in self._NATIVE_CAPABILITY_GATES:
                    if self.version < min_version:
                        continue
                    if self.capability_mask & required_mask != required_mask:
                        raise RuntimeError(cap_error)
                    if interface_attr and not getattr(
                        self, interface_attr, False
                    ):
                        raise RuntimeError(iface_error)
                self._load_abi_metadata(dll)
                if hasattr(dll, "wrpa_get_perf_stats"):
                    dll.wrpa_get_perf_stats.argtypes = [
                        ctypes.POINTER(NativePerfStats)
                    ]
                    dll.wrpa_get_perf_stats.restype = ctypes.c_int
                if hasattr(dll, "wrpa_reset_perf_stats"):
                    dll.wrpa_reset_perf_stats.argtypes = []
                    dll.wrpa_reset_perf_stats.restype = None
                self.dll = dll
                self.dll_path = os.path.abspath(dll_path)
                self.available = True
                self.load_error = ""
                return
            except Exception as error:
                last_error = f"{dll_path}: {error}"
                self.dll = None
                self.available = False
                self.version = 0
                self.capability_mask = 0
                self.abi_bits = 0
                self.build_flag_mask = 0
                self.native_struct_sizes = {}
                self.has_extended_search = False
                self.has_preferred_scale_list = False
                self.has_explicit_scale_only = False
                self.has_scene_fingerprint = False
                self.has_raw_rgb_capture = False
                self.has_dxgi_scene_change = False
                self.has_cancellable_search = False
                self.dxgi_scene_usable = True
                self.dll_path = ""
        self.load_error = last_error or f"{NATIVE_CORE_DLL_NAME} not found"

    def _load_abi_metadata(self, dll):
        names = ("wrpa_abi_bits", "wrpa_struct_size", "wrpa_build_flags")
        missing = [name for name in names if not hasattr(dll, name)]
        if missing:
            if self.version >= 10700:
                raise RuntimeError(f"DLL 缺少 ABI 元数据接口：{', '.join(missing)}")
            self.abi_bits = 0
            self.build_flag_mask = 0
            self.native_struct_sizes = {}
            return
        dll.wrpa_abi_bits.argtypes = []
        dll.wrpa_abi_bits.restype = ctypes.c_int
        dll.wrpa_struct_size.argtypes = [ctypes.c_int]
        dll.wrpa_struct_size.restype = ctypes.c_int
        dll.wrpa_build_flags.argtypes = []
        dll.wrpa_build_flags.restype = ctypes.c_uint64
        self.abi_bits = int(dll.wrpa_abi_bits())
        self.build_flag_mask = int(dll.wrpa_build_flags())
        self.native_struct_sizes = {
            name: int(dll.wrpa_struct_size(struct_id))
            for name, struct_id in NATIVE_STRUCT_IDS.items()
        }
        expected_bits = ctypes.sizeof(ctypes.c_void_p) * 8
        expected_sizes = self._expected_struct_sizes()
        if self.abi_bits != expected_bits:
            raise RuntimeError(
                f"DLL 位数不兼容：native={self.abi_bits}, python={expected_bits}"
            )
        mismatches = {
            name: (self.native_struct_sizes.get(name, 0), expected)
            for name, expected in expected_sizes.items()
            if self.native_struct_sizes.get(name, 0) != expected
        }
        if mismatches:
            raise RuntimeError(f"DLL 结构体布局不兼容：{mismatches}")

    @staticmethod
    def _expected_struct_sizes():
        return {
            "rect": ctypes.sizeof(NativeRect),
            "match": ctypes.sizeof(NativeMatch),
            "performance": ctypes.sizeof(NativePerfStats),
        }

    def find_template(
        self,
        image_path,
        regions,
        min_scale,
        max_scale,
        scale_step,
        use_gray,
        threshold,
        find_all=False,
        max_matches=512,
        parallel_mode="auto",
        preferred_scale=None,
        preferred_scales=None,
        explicit_scale_only=False,
    ):
        """Run one native search using a deliberate tri-state result.

        ``None`` means the native call was unavailable, failed, or was
        cancelled — callers distinguish cancellation from unavailability via
        ``last_result_code``. ``[]`` is an authoritative successful miss, so
        callers must not repeat the same search through another backend.
        Preferred scales are hints; unless ``explicit_scale_only`` is true,
        the DLL must continue through the full configured range. See ADR
        0010/0018.
        """
        options = MatchOptions(
            image_path=image_path,
            regions=regions,
            min_scale=min_scale,
            max_scale=max_scale,
            scale_step=scale_step,
            use_gray=use_gray,
            threshold=threshold,
            find_all=find_all,
            max_matches=max_matches,
            parallel_mode=parallel_mode,
            preferred_scale=preferred_scale,
            preferred_scales=preferred_scales,
            explicit_scale_only=explicit_scale_only,
        )
        return self._find_template_impl(options)

    def _find_template_impl(self, options: MatchOptions):
        """find_template 的内部实现：参数已聚合为 MatchOptions。

        返回语义与 find_template 完全一致（None/[]/列表三元组语义见
        find_template 文档）。本方法额外在每条返回路径上记录
        ``last_failure_category`` 以支撑失败分类（不改变对外返回值）。
        """
        if not self.available or not self.dll:
            self.last_failure_category = MatchFailure.UNAVAILABLE
            return None
        try:
            clean_regions = []
            for region in options.regions or []:
                x, y, width, height = [int(float(value)) for value in region]
                if width > 0 and height > 0:
                    clean_regions.append((x, y, width, height))
            rect_array = None
            rect_ptr = None
            if clean_regions:
                rect_array = (NativeRect * len(clean_regions))()
                for index, (x, y, width, height) in enumerate(clean_regions):
                    rect_array[index] = NativeRect(x, y, width, height)
                rect_ptr = rect_array

            max_matches = max(1, min(int(options.max_matches), 4096))
            if max_matches > self._match_buffer_size:
                self._match_buffer = (NativeMatch * max_matches)()
                self._match_buffer_size = max_matches
            out_array = self._match_buffer
            out_count = ctypes.c_int(0)
            error_buffer = ctypes.create_unicode_buffer(512)
            mode_id = NATIVE_PARALLEL_MODES.get(
                str(options.parallel_mode or "auto").strip().lower(), 1
            )
            try:
                preferred = float(options.preferred_scale)
            except (TypeError, ValueError):
                preferred = 0.0
            if options.find_all or not math.isfinite(preferred) or preferred <= 0.0:
                preferred = 0.0
            clean_preferred = []
            for value in options.preferred_scales or ():
                try:
                    scale = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(scale) and scale > 0.0 and scale not in clean_preferred:
                    clean_preferred.append(scale)
                if len(clean_preferred) >= 16:
                    break
            if preferred > 0.0 and preferred not in clean_preferred:
                clean_preferred.insert(0, preferred)
            if options.find_all:
                clean_preferred = []
            explicit_scale_only = bool(options.explicit_scale_only) and not options.find_all
            if explicit_scale_only and not clean_preferred:
                self.last_result_code = 0
                self.load_error = ""
                self.last_failure_category = MatchFailure.NO_MATCH
                return []
            if explicit_scale_only and not self.has_explicit_scale_only:
                self.last_result_code = 0
                self.load_error = "native explicit-scale search unavailable"
                self.last_failure_category = MatchFailure.UNAVAILABLE
                return None
            preferred_array = None
            preferred_ptr = None
            if clean_preferred:
                preferred_array = (ctypes.c_double * len(clean_preferred))(
                    *clean_preferred
                )
                preferred_ptr = preferred_array

            common_args = (
                os.path.abspath(str(options.image_path)),
                rect_ptr,
                len(clean_regions),
                float(options.min_scale),
                float(options.max_scale),
                float(options.scale_step),
                1 if options.use_gray else 0,
                float(options.threshold),
                1 if options.find_all else 0,
            )
            if explicit_scale_only:
                result_code = self.dll.wrpa_find_template_ex3(
                    *common_args,
                    mode_id,
                    preferred_ptr,
                    len(clean_preferred),
                    1,
                    out_array,
                    max_matches,
                    ctypes.byref(out_count),
                    error_buffer,
                    len(error_buffer),
                )
            elif self.has_preferred_scale_list:
                result_code = self.dll.wrpa_find_template_ex2(
                    *common_args,
                    mode_id,
                    preferred_ptr,
                    len(clean_preferred),
                    out_array,
                    max_matches,
                    ctypes.byref(out_count),
                    error_buffer,
                    len(error_buffer),
                )
            elif self.has_extended_search:
                # FR-20261006-009: `ex` 的第 11 位是**标量** c_double（单个优先
                # 倍率），不是 ex2/ex3 的「倍率数组指针 + 长度」。此前误传
                # preferred_ptr 与 len(clean_preferred)，实参 17 个而 argtypes 只有
                # 16 位，ctypes 直接抛 TypeError——异常逃逸了调用方依赖的
                # "回退 OpenCV" 契约。此分支只表达"单个优先倍率"这一语义，
                # 多倍率列表由 ex2/ex3 承载。
                result_code = self.dll.wrpa_find_template_ex(
                    *common_args,
                    mode_id,
                    float(preferred),
                    out_array,
                    max_matches,
                    ctypes.byref(out_count),
                    error_buffer,
                    len(error_buffer),
                )
            else:
                result_code = self.dll.wrpa_find_template(
                    *common_args,
                    out_array,
                    max_matches,
                    ctypes.byref(out_count),
                    error_buffer,
                    len(error_buffer),
                )
            self.last_result_code = int(result_code)
            if result_code < 0:
                self.load_error = error_buffer.value or f"native rc {result_code}"
                self.last_failure_category = MatchFailure.UNAVAILABLE
                return None
            self.load_error = ""
            self.last_failure_category = MatchFailure.MATCHED
            result = []
            for index in range(max(0, min(out_count.value, max_matches))):
                item = out_array[index]
                result.append((
                    float(item.x),
                    float(item.y),
                    float(item.scale),
                    float(item.score),
                    float(item.radius),
                ))
            return result
        except (OSError, RuntimeError, ValueError) as error:
            self.last_result_code = -999
            self.load_error = str(error)
            self.last_failure_category = MatchFailure.UNAVAILABLE
            return None
    def cancel_active_search(self):
        """Request cooperative cancellation of an in-progress native search.

        Returns:
            True if the cancel entry point was invoked, False if unavailable.
        """
        function = (
            getattr(self.dll, "wrpa_cancel_active_search", None)
            if self.dll
            else None
        )
        if function is None:
            return False
        try:
            function()
            return True
        except Exception as exc:
            logger.warning("原生函数探测失败: %s", exc, exc_info=True)
            return False

    def _legacy_capability_mask(self):
        names = {
            "gdi_capture",
            "multi_region",
            "multi_scale",
            "grayscale",
            "color",
            "find_all",
        }
        if self.version >= 10500:
            names.update(
                {
                    "template_lru",
                    "performance_counters",
                    "work_budget",
                    "single_capture_per_region",
                }
            )
        return sum(NATIVE_CAPABILITY_BITS[name] for name in names)

    def capture_scene_signature(
        self,
        region,
        *,
        max_width=160,
        max_height=96,
    ):
        """Capture a downscaled grayscale region without copying a full frame."""
        if (
            not self.available
            or not self.dll
            or not self.has_scene_fingerprint
        ):
            return None
        try:
            if len(region) != 4:
                return None
            x, y, width, height = [int(float(value)) for value in region]
            if width <= 0 or height <= 0:
                return None
            scale = min(
                1.0,
                max(8, int(max_width)) / width,
                max(8, int(max_height)) / height,
            )
            target_width = max(1, int(round(width * scale)))
            target_height = max(1, int(round(height * scale)))
            rect = NativeRect(x, y, width, height)
            capacity = target_width * target_height
            out_pixels = (ctypes.c_ubyte * capacity)()
            error_buffer = ctypes.create_unicode_buffer(512)
            result_code = self.dll.wrpa_capture_gray_fingerprint(
                ctypes.byref(rect),
                target_width,
                target_height,
                out_pixels,
                capacity,
                error_buffer,
                len(error_buffer),
            )
            if int(result_code) <= 0:
                self.last_scene_error = (
                    error_buffer.value or f"native scene rc {result_code}"
                )
                return None
            self.last_scene_error = ""
            return target_width, target_height, bytes(out_pixels)
        except Exception as error:
            self.last_scene_error = str(error)
            return None

    def virtual_screen_rect(self):
        """Return the physical virtual-desktop rectangle from the native ABI."""

        if not self.available or not self.dll or not self.has_raw_rgb_capture:
            return None
        try:
            rect = NativeRect()
            error_buffer = ctypes.create_unicode_buffer(512)
            result_code = self.dll.wrpa_get_virtual_screen_rect(
                ctypes.byref(rect),
                error_buffer,
                len(error_buffer),
            )
            if int(result_code) <= 0 or rect.w <= 0 or rect.h <= 0:
                self.last_scene_error = (
                    error_buffer.value or f"native screen rect rc {result_code}"
                )
                return None
            self.last_scene_error = ""
            return int(rect.x), int(rect.y), int(rect.w), int(rect.h)
        except Exception as error:
            self.last_scene_error = str(error)
            return None

    def capture_rgb(self, region):
        """Capture a bounded physical-screen region as packed RGB bytes."""

        if not self.available or not self.dll or not self.has_raw_rgb_capture:
            return None
        try:
            x, y, width, height = [int(float(value)) for value in region]
            pixels = width * height
            if width <= 0 or height <= 0 or pixels > MAX_NATIVE_CAPTURE_PIXELS:
                return None
            capacity = pixels * 3
            rect = NativeRect(x, y, width, height)
            out_pixels = (ctypes.c_ubyte * capacity)()
            error_buffer = ctypes.create_unicode_buffer(512)
            result_code = self.dll.wrpa_capture_rgb(
                ctypes.byref(rect),
                out_pixels,
                capacity,
                error_buffer,
                len(error_buffer),
            )
            if int(result_code) <= 0:
                self.last_scene_error = (
                    error_buffer.value or f"native RGB capture rc {result_code}"
                )
                return None
            self.last_scene_error = ""
            return width, height, bytes(out_pixels)
        except Exception as error:
            self.last_scene_error = str(error)
            return None

    def poll_desktop_change(self, regions=None, *, reset_baseline=False):
        """Poll DXGI dirty rectangles; None requests the fingerprint fallback."""
        if (
            not self.available
            or not self.dll
            or not self.has_dxgi_scene_change
            or not self.dxgi_scene_usable
        ):
            return None
        try:
            clean_regions = []
            for region in regions or ():
                x, y, width, height = [int(float(value)) for value in region]
                if width > 0 and height > 0:
                    clean_regions.append((x, y, width, height))
            rect_array = None
            rect_ptr = None
            if clean_regions:
                rect_array = (NativeRect * len(clean_regions))()
                for index, values in enumerate(clean_regions):
                    rect_array[index] = NativeRect(*values)
                rect_ptr = rect_array
            error_buffer = ctypes.create_unicode_buffer(512)
            result_code = self.dll.wrpa_poll_desktop_change(
                rect_ptr,
                len(clean_regions),
                1 if reset_baseline else 0,
                error_buffer,
                len(error_buffer),
            )
            if int(result_code) < 0:
                self.last_scene_error = (
                    error_buffer.value or f"native DXGI rc {result_code}"
                )
                self.dxgi_scene_usable = False
                return None
            self.last_scene_error = ""
            return bool(result_code)
        except Exception as error:
            self.last_scene_error = str(error)
            self.dxgi_scene_usable = False
            return None

    def capabilities(self):
        """Return the decoded native capability bitmask as a name-to-bool dict."""
        return {
            "mask": int(self.capability_mask),
            **{
                name: bool(self.capability_mask & bit)
                for name, bit in NATIVE_CAPABILITY_BITS.items()
            },
        }

    def build_flags(self):
        """Return the decoded native build-flag bitmask as a name-to-bool dict."""
        return {
            "mask": int(self.build_flag_mask),
            **{
                name: bool(self.build_flag_mask & bit)
                for name, bit in NATIVE_BUILD_FLAG_BITS.items()
            },
        }

    def abi_snapshot(self):
        """Return a diagnostic dict comparing native and Python struct layouts."""
        expected_bits = ctypes.sizeof(ctypes.c_void_p) * 8
        expected_sizes = self._expected_struct_sizes()
        metadata_available = bool(
            self.abi_bits and self.native_struct_sizes and self.build_flag_mask
        )
        return {
            "metadata_available": metadata_available,
            "compatible": bool(
                metadata_available
                and self.abi_bits == expected_bits
                and self.native_struct_sizes == expected_sizes
            ),
            "pointer_bits": int(self.abi_bits),
            "python_pointer_bits": expected_bits,
            "struct_sizes": dict(self.native_struct_sizes),
            "python_struct_sizes": expected_sizes,
            "build_flags": self.build_flags(),
        }

    def health_snapshot(self):
        """Return a comprehensive diagnostic snapshot of the native backend state."""
        return {
            "available": bool(self.available),
            "api_version": int(self.version),
            "dll_path": str(self.dll_path),
            "capabilities": self.capabilities(),
            "abi": self.abi_snapshot(),
            "last_result_code": int(self.last_result_code),
            "last_error": str(self.load_error or ""),
            "scene_error": str(self.last_scene_error or ""),
            "dxgi_scene_usable": bool(self.dxgi_scene_usable),
        }

    def service_snapshot(self, *, requested=True, fallback_available=True):
        """Return a path-free contract shared with isolated/native Runners."""

        abi = self.abi_snapshot()
        snapshot = BackendServiceDescriptor(
            service="vision",
            contract_version=NATIVE_VISION_SERVICE_CONTRACT_VERSION,
            backend="native.dll" if self.available else "unavailable",
            requested=bool(requested),
            available=bool(self.available),
            native=True,
            capabilities=tuple(
                name
                for name, bit in NATIVE_CAPABILITY_BITS.items()
                if self.capability_mask & bit
            ),
            cancellation=(
                "cooperative" if self.has_cancellable_search else "bounded_call"
            ),
            fallback_name="opencv",
            fallback_available=bool(fallback_available),
        ).snapshot()
        snapshot.update({
            "api_version": int(self.version),
            "capability_mask": int(self.capability_mask),
            "abi_compatible": bool(abi.get("compatible")),
            "cancellable_search": bool(self.has_cancellable_search),
        })
        return snapshot

    def screenshot_service_snapshot(self, *, requested=True, fallback_available=True):
        """Return a path-free contract descriptor for the native screenshot service."""
        return BackendServiceDescriptor(
            service="screenshot",
            contract_version=SCREENSHOT_SERVICE_CONTRACT_VERSION,
            backend="native.dll" if self.has_raw_rgb_capture else "unavailable",
            requested=bool(requested),
            available=bool(self.available and self.has_raw_rgb_capture),
            native=True,
            capabilities=("capture.rgb", "screen.virtual_rect")
            if self.has_raw_rgb_capture
            else (),
            cancellation="bounded_call",
            fallback_name="mss_pyautogui",
            fallback_available=bool(fallback_available),
        ).snapshot()

    def reset_performance_stats(self):
        """Reset optional DLL counters; older compatible DLLs simply have none."""
        function = getattr(self.dll, "wrpa_reset_perf_stats", None) if self.dll else None
        if function is None:
            return False
        try:
            function.argtypes = []
            function.restype = None
            function()
            return True
        except Exception as exc:
            logger.warning("原生函数探测失败: %s", exc, exc_info=True)
            return False

    def performance_stats(self):
        """Return optional native telemetry without making it an API requirement."""
        function = getattr(self.dll, "wrpa_get_perf_stats", None) if self.dll else None
        if function is None:
            return {}
        try:
            stats = NativePerfStats()
            if int(function(ctypes.byref(stats))) != 0:
                return {}
            return {
                "calls": int(stats.calls),
                "captures": int(stats.captures),
                "integral_builds": int(stats.integral_builds),
                "template_cache_hits": int(stats.template_cache_hits),
                "template_cache_misses": int(stats.template_cache_misses),
                "template_variants_built": int(stats.template_variants_built),
                "work_budget_fallbacks": int(stats.work_budget_fallbacks),
                "capture_ms": round(stats.capture_microseconds / 1000.0, 3),
                "template_ms": round(stats.template_microseconds / 1000.0, 3),
                "match_ms": round(stats.match_microseconds / 1000.0, 3),
                "cache_bytes": int(stats.cache_bytes),
                "cache_entries": int(stats.cache_entries),
            }
        except Exception as exc:
            logger.warning("读取原生性能统计失败: %s", exc, exc_info=True)
            return {}
