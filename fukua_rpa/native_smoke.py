"""Real-screen native/OpenCV parity smoke shared by source and frozen builds."""

from __future__ import annotations

import ctypes
import logging
import math
import os
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from ctypes import wintypes
from typing import Any

logger = logging.getLogger(__name__)

import cv2  # noqa: E402
import mss  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageStat  # noqa: E402

from .constants import NATIVE_CORE_RELEASE_VERSION  # noqa: E402
from .paths import get_base_dir  # noqa: E402
from .timing_constants import POLL_INTERVAL_FAST_S, POLL_INTERVAL_S, POLL_INTERVAL_SLOW_S  # noqa: E402
from .vision.core import NativeVisionCore  # noqa: E402
from .vision.opencv_runtime import configure_opencv_threads  # noqa: E402

WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
SW_SHOWNOACTIVATE = 4
PARITY_TOLERANCE_PIXELS = 3.0
PARITY_TOLERANCE_SCORE = 0.01

# Self-contained by design: this smoke test binds its own ctypes handles
# instead of importing the win32_api module, so it can run independently in
# both source and frozen builds without pulling in unrelated dependencies.
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


def _configure_win32() -> None:
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.FillRect.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.HANDLE,
    ]
    user32.FillRect.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.UpdateWindow.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    # FR-20260812-D4-001: 64 位 Windows 上 HWND 可能 >2^31，GetForegroundWindow
    # 未声明 restype 时 ctypes 默认 c_int 会截断句柄，前台比对/消息投递失效。
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
    gdi32.CreateSolidBrush.restype = wintypes.HANDLE
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi32.DeleteObject.restype = wintypes.BOOL


def _module_handle() -> wintypes.HINSTANCE:
    kernel32 = ctypes.windll.kernel32
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    return kernel32.GetModuleHandleW(None)


def _colorref(red: int, green: int, blue: int) -> int:
    return int(red) | (int(green) << 8) | (int(blue) << 16)


def _fill_rect(
    device_context: wintypes.HDC,
    left: int,
    top: int,
    right: int,
    bottom: int,
    color: int,
) -> None:
    rect = wintypes.RECT(left, top, right, bottom)
    brush = gdi32.CreateSolidBrush(color)
    if not brush:
        raise RuntimeError("CreateSolidBrush failed")
    try:
        if not user32.FillRect(device_context, ctypes.byref(rect), brush):
            raise RuntimeError("FillRect failed")
    finally:
        gdi32.DeleteObject(brush)


def _draw_pattern(hwnd: wintypes.HWND, size: int = 128) -> None:
    device_context = user32.GetDC(hwnd)
    if not device_context:
        raise RuntimeError("GetDC failed")
    try:
        _fill_rect(
            device_context,
            0,
            0,
            size,
            size,
            _colorref(245, 245, 245),
        )
        cell = 16
        for row in range(size // cell):
            for column in range(size // cell):
                seed = row * 8 + column
                red = (37 * seed + 41) % 220 + 20
                green = (83 * seed + 17) % 220 + 20
                blue = (149 * seed + 67) % 220 + 20
                _fill_rect(
                    device_context,
                    column * cell,
                    row * cell,
                    (column + 1) * cell,
                    (row + 1) * cell,
                    _colorref(red, green, blue),
                )
        gdi32.GdiFlush()
    finally:
        user32.ReleaseDC(hwnd, device_context)


def _draw_change_marker(hwnd: wintypes.HWND) -> None:
    device_context = user32.GetDC(hwnd)
    if not device_context:
        raise RuntimeError("GetDC failed")
    try:
        _fill_rect(
            device_context,
            4,
            4,
            20,
            20,
            _colorref(0, 0, 0),
        )
        gdi32.GdiFlush()
    finally:
        user32.ReleaseDC(hwnd, device_context)


def _channel_signature_pattern(
    seed: int, size: int = 128, cell: int = 8
) -> Image.Image:
    """Build images that expose global-mean versus per-channel NCC drift."""

    rng = np.random.default_rng(seed)
    cells = size // cell
    colors = np.array([220, 120, 30], dtype=np.int16)
    colors = colors + rng.integers(-18, 19, size=(cells, cells, 3))
    pixels = np.repeat(np.repeat(colors, cell, axis=0), cell, axis=1)
    return Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), "RGB")


def _draw_grid_image(hwnd: wintypes.HWND, image: Image.Image, cell: int = 8) -> None:
    device_context = user32.GetDC(hwnd)
    if not device_context:
        raise RuntimeError("GetDC failed")
    try:
        pixels = image.load()
        for top in range(0, image.height, cell):
            for left in range(0, image.width, cell):
                red, green, blue = pixels[left, top]  # pyright: ignore[reportOptionalSubscript, reportGeneralTypeIssues]
                _fill_rect(
                    device_context,
                    left,
                    top,
                    min(image.width, left + cell),
                    min(image.height, top + cell),
                    _colorref(red, green, blue),
                )
        gdi32.GdiFlush()
    finally:
        user32.ReleaseDC(hwnd, device_context)


@contextmanager
def _generated_pattern_window(
    x: int = 160, y: int = 140, size: int = 128
) -> Iterator[tuple[wintypes.HWND, tuple[int, int, int, int]]]:
    _configure_win32()
    hwnd = user32.CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        "STATIC",
        "fukuaRPA native smoke",
        WS_POPUP | WS_VISIBLE,
        x,
        y,
        size,
        size,
        None,
        None,
        _module_handle(),
        None,
    )
    if not hwnd:
        raise RuntimeError("Unable to create native smoke-test window")
    try:
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        user32.UpdateWindow(hwnd)
        time.sleep(POLL_INTERVAL_S)
        _draw_pattern(hwnd, size)
        try:
            ctypes.windll.dwmapi.DwmFlush()
        except Exception as exc:
            logger.warning("DwmFlush 失败: %s", exc, exc_info=True)
        time.sleep(POLL_INTERVAL_S)
        yield hwnd, (x, y, size, size)
    finally:
        user32.DestroyWindow(hwnd)


def _capture_region(region: tuple[int, int, int, int]) -> Image.Image:
    x, y, width, height = region
    with mss.MSS() as capture:
        shot = capture.grab(
            {"left": x, "top": y, "width": width, "height": height}
        )
        return Image.frombytes("RGB", shot.size, shot.rgb)


def _opencv_match(
    scene: Image.Image,
    template: Image.Image,
    region: tuple[int, int, int, int],
    use_gray: bool,
) -> tuple[tuple[float, float], float]:
    scene_array = np.asarray(scene)
    template_array = np.asarray(template)
    if use_gray:
        scene_array = cv2.cvtColor(scene_array, cv2.COLOR_RGB2GRAY)
        template_array = cv2.cvtColor(template_array, cv2.COLOR_RGB2GRAY)
    else:
        scene_array = cv2.cvtColor(scene_array, cv2.COLOR_RGB2BGR)
        template_array = cv2.cvtColor(template_array, cv2.COLOR_RGB2BGR)
    scores = cv2.matchTemplate(scene_array, template_array, cv2.TM_CCOEFF_NORMED)
    _minimum, maximum, _minimum_location, location = cv2.minMaxLoc(scores)
    x, y, _width, _height = region
    center = (
        float(x + location[0] + template.width / 2.0),
        float(y + location[1] + template.height / 2.0),
    )
    return center, float(maximum)


def _nearest_match(
    matches: Any, point: tuple[float, float]
) -> tuple[float, float, float, float]:
    if not matches:
        raise RuntimeError("Native core returned no matches")
    return min(
        matches,
        key=lambda item: math.hypot(item[0] - point[0], item[1] - point[1]),
    )


def _distance(first: Sequence[float], second: Sequence[float]) -> float:
    return float(math.hypot(first[0] - second[0], first[1] - second[1]))


def run_native_smoke(
    base_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Exercise native matching modes and compare their coordinates with OpenCV.

    Args:
        base_dir: Directory used to locate the native core; defaults to the
            application base directory.

    Returns:
        A JSON-safe report dict. ``ok`` is True only when every native mode
        agrees with the OpenCV reference within tolerance.

    Raises:
        Never propagates; failures are captured in the returned ``error`` field.
    """

    # FR-SCOPE015D-001: 原型声明必须在**任何** HWND 读取之前生效。此前
    # _configure_win32() 只在 _generated_pattern_window() 内部调用，而
    # foreground_before 在进入该上下文之前就已读取，于是同一次冒烟里
    # foreground_before（未声明 restype ⇒ c_int 截断）与 foreground_after
    # （已声明 ⇒ 64 位 HWND）位宽不一致，foreground_unchanged 失真。
    # 该调用是幂等的，保留 _generated_pattern_window 内的调用以维持该函数
    # 对其他调用方（scripts/benchmark_native_scheduler.py）的自足性。
    #
    # ADV-20261005-015: 原型声明与 NativeVisionCore 构造原在 try 之外，
    # 与上方 "Never propagates" 的契约矛盾——构造期抛异常会绕过
    # fukuaRPA.py:189 的 ok 判定，使调用方拿到的是未捕获异常而非可读的
    # 冒烟失败报告。现把两者移入 try，使失败统一走 error 字段。
    started = time.perf_counter()
    base_report = {"format": "fukuaRPA_native_smoke_v2"}
    try:
        _configure_win32()
        core = NativeVisionCore(base_dir=os.path.abspath(base_dir or get_base_dir()))
        base_report["api_version"] = core.version
        base_report["abi"] = core.abi_snapshot()
        if not core.available:
            raise RuntimeError(core.load_error)
        if core.version < NATIVE_CORE_RELEASE_VERSION:
            raise RuntimeError(
                f"Native core API {core.version} is older than release API "
                f"{NATIVE_CORE_RELEASE_VERSION}"
            )
        configure_opencv_threads(cv2)
        foreground_before = int(user32.GetForegroundWindow() or 0)
        with _generated_pattern_window() as (smoke_hwnd, window_region):
            scene = _capture_region(window_region)
            template_box = (32, 32, 80, 80)
            template = scene.crop(template_box)
            detail = float(ImageStat.Stat(template.convert("L")).stddev[0])
            if detail < 3.0:
                raise RuntimeError("Generated native smoke template lacks detail")
            expected = (
                window_region[0] + (template_box[0] + template_box[2]) / 2.0,
                window_region[1] + (template_box[1] + template_box[3]) / 2.0,
            )
            opencv_gray, opencv_gray_score = _opencv_match(
                scene, template, window_region, True
            )
            opencv_color, opencv_color_score = _opencv_match(
                scene, template, window_region, False
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                template_path = os.path.join(temp_dir, "generated_template.png")
                template.save(template_path)
                core.reset_performance_stats()
                native_gray = core.find_template(
                    template_path,
                    [window_region],
                    1.0,
                    1.0,
                    0.05,
                    True,
                    0.9,
                )
                native_color = core.find_template(
                    template_path,
                    [window_region],
                    1.0,
                    1.0,
                    0.05,
                    False,
                    0.9,
                )
                native_multiscale = core.find_template(
                    template_path,
                    [window_region],
                    0.9,
                    1.1,
                    0.1,
                    True,
                    0.9,
                )
                native_single_thread = core.find_template(
                    template_path,
                    [window_region],
                    0.9,
                    1.1,
                    0.1,
                    True,
                    0.9,
                    parallel_mode="off",
                )
                native_forced_multi = core.find_template(
                    template_path,
                    [window_region],
                    0.9,
                    1.1,
                    0.1,
                    True,
                    0.9,
                    parallel_mode="force",
                )
                native_preferred = core.find_template(
                    template_path,
                    [window_region],
                    0.9,
                    1.1,
                    0.1,
                    True,
                    0.99,
                    preferred_scale=1.0,
                )
                native_preferred_fallback = core.find_template(
                    template_path,
                    [window_region],
                    0.9,
                    1.1,
                    0.1,
                    True,
                    0.999,
                    preferred_scale=0.9,
                )
                native_preferred_list = core.find_template(
                    template_path,
                    [window_region],
                    0.9,
                    1.1,
                    0.1,
                    True,
                    0.999,
                    preferred_scales=(0.9, 1.0),
                )
                native_explicit_hit = core.find_template(
                    template_path,
                    [window_region],
                    0.9,
                    1.1,
                    0.1,
                    True,
                    0.999,
                    preferred_scales=(1.0,),
                    explicit_scale_only=True,
                )
                native_explicit_miss = core.find_template(
                    template_path,
                    [window_region],
                    0.9,
                    1.1,
                    0.1,
                    True,
                    0.999,
                    preferred_scales=(0.9,),
                    explicit_scale_only=True,
                )
                native_all = core.find_template(
                    template_path,
                    [window_region],
                    1.0,
                    1.0,
                    0.05,
                    True,
                    0.9,
                    find_all=True,
                    max_matches=32,
                )
                excluded_region = (
                    window_region[0],
                    window_region[1],
                    24,
                    24,
                )
                target_region = (
                    window_region[0] + 24,
                    window_region[1] + 24,
                    80,
                    80,
                )
                excluded = core.find_template(
                    template_path,
                    [excluded_region],
                    1.0,
                    1.0,
                    0.05,
                    True,
                    0.9,
                )
                native_multi_region = core.find_template(
                    template_path,
                    [excluded_region, target_region],
                    1.0,
                    1.0,
                    0.05,
                    True,
                    0.9,
                )
                scene_fingerprint = core.capture_scene_signature(window_region)
                dxgi_baseline = core.poll_desktop_change(
                    [window_region], reset_baseline=True
                )
                _draw_change_marker(smoke_hwnd)
                try:
                    ctypes.windll.dwmapi.DwmFlush()
                except Exception as exc:
                    logger.warning("DwmFlush 失败: %s", exc, exc_info=True)
                dxgi_changed = False
                for _index in range(20):
                    detected = core.poll_desktop_change([window_region])
                    if detected is None:
                        break
                    if detected:
                        dxgi_changed = True
                        break
                    time.sleep(POLL_INTERVAL_FAST_S)

                # These two images have almost identical global RGB averages,
                # but unrelated per-channel detail. The pre-11400 formula
                # reported a false score near 0.98 instead of OpenCV's ~0.01.
                color_semantics_template = _channel_signature_pattern(11)
                color_semantics_scene = _channel_signature_pattern(29)
                color_semantics_path = os.path.join(
                    temp_dir, "color_semantics_template.png"
                )
                color_semantics_template.save(color_semantics_path)
                _draw_grid_image(smoke_hwnd, color_semantics_scene)
                try:
                    ctypes.windll.dwmapi.DwmFlush()
                except Exception as exc:
                    logger.warning("DwmFlush 失败: %s", exc, exc_info=True)
                time.sleep(POLL_INTERVAL_SLOW_S)
                captured_color_semantics_scene = _capture_region(window_region)
                _center, opencv_color_semantics_score = _opencv_match(
                    captured_color_semantics_scene,
                    color_semantics_template,
                    window_region,
                    False,
                )
                native_color_semantics = core.find_template(
                    color_semantics_path,
                    [window_region],
                    1.0,
                    1.0,
                    0.05,
                    False,
                    0.0,
                )
                native_color_false_positive = core.find_template(
                    color_semantics_path,
                    [window_region],
                    1.0,
                    1.0,
                    0.05,
                    False,
                    0.8,
                )
        foreground_after = int(user32.GetForegroundWindow() or 0)

        gray_match = _nearest_match(native_gray, expected)
        color_match = _nearest_match(native_color, expected)
        multiscale_match = _nearest_match(native_multiscale, expected)
        single_thread_match = _nearest_match(native_single_thread, expected)
        forced_multi_match = _nearest_match(native_forced_multi, expected)
        preferred_match = _nearest_match(native_preferred, expected)
        preferred_fallback_match = _nearest_match(
            native_preferred_fallback, expected
        )
        preferred_list_match = _nearest_match(native_preferred_list, expected)
        explicit_match = _nearest_match(native_explicit_hit, expected)
        all_match = _nearest_match(native_all, expected)
        multi_region_match = _nearest_match(native_multi_region, expected)
        color_semantics_match = _nearest_match(
            native_color_semantics, expected
        )
        distances = {
            "native_gray_to_expected": _distance(gray_match, expected),
            "native_color_to_expected": _distance(color_match, expected),
            "native_multiscale_to_expected": _distance(multiscale_match, expected),
            "native_single_thread_to_expected": _distance(
                single_thread_match, expected
            ),
            "native_forced_multi_to_expected": _distance(
                forced_multi_match, expected
            ),
            "native_preferred_to_expected": _distance(preferred_match, expected),
            "native_preferred_fallback_to_expected": _distance(
                preferred_fallback_match, expected
            ),
            "native_preferred_list_to_expected": _distance(
                preferred_list_match, expected
            ),
            "native_explicit_to_expected": _distance(
                explicit_match, expected
            ),
            "native_all_to_expected": _distance(all_match, expected),
            "native_multi_region_to_expected": _distance(
                multi_region_match, expected
            ),
            "native_gray_to_opencv": _distance(gray_match, opencv_gray),
            "native_color_to_opencv": _distance(color_match, opencv_color),
        }
        within_tolerance = all(
            value <= PARITY_TOLERANCE_PIXELS for value in distances.values()
        )
        score_deltas = {
            "native_color_to_opencv": abs(
                float(color_match[3]) - opencv_color_score
            ),
            "color_semantics_to_opencv": abs(
                float(color_semantics_match[3])
                - opencv_color_semantics_score
            ),
        }
        if excluded != []:
            raise RuntimeError(f"Excluded region did not return an authoritative miss: {excluded}")
        if abs(float(preferred_match[2]) - 1.0) > 1e-7:
            raise RuntimeError(
                f"Preferred scale did not win the fast path: {preferred_match[2]}"
            )
        if abs(float(preferred_fallback_match[2]) - 1.0) > 1e-7:
            raise RuntimeError(
                "Preferred-scale miss did not fall back to the complete range: "
                f"{preferred_fallback_match[2]}"
            )
        if abs(float(preferred_list_match[2]) - 1.0) > 1e-7:
            raise RuntimeError(
                f"Preferred scale list did not find the expected scale: {preferred_list_match[2]}"
            )
        if abs(float(explicit_match[2]) - 1.0) > 1e-7:
            raise RuntimeError(
                f"Explicit-scale search returned the wrong scale: {explicit_match[2]}"
            )
        if native_explicit_miss != []:
            raise RuntimeError(
                "Explicit-scale miss incorrectly fell back to another scale: "
                f"{native_explicit_miss}"
            )
        if not scene_fingerprint or not scene_fingerprint[2]:
            raise RuntimeError(
                f"Low-resolution scene fingerprint failed: {core.last_scene_error}"
            )
        if dxgi_baseline is None or not dxgi_changed:
            raise RuntimeError(
                f"DXGI scene-change smoke failed: {core.last_scene_error}"
            )
        if native_color_false_positive != []:
            raise RuntimeError(
                "Color matcher accepted a per-channel negative sample: "
                f"{native_color_false_positive}"
            )
        if any(
            value > PARITY_TOLERANCE_SCORE for value in score_deltas.values()
        ):
            raise RuntimeError(
                f"Native/OpenCV score parity drift: {score_deltas}"
            )
        if not within_tolerance:
            raise RuntimeError(f"Native/OpenCV parity drift: {distances}")
        smoke_became_foreground = foreground_after == int(smoke_hwnd)
        if smoke_became_foreground:
            raise RuntimeError(
                f"Smoke window became foreground: {foreground_before} -> {foreground_after}"
            )
        return {
            **base_report,
            "ok": True,
            "template_detail": round(detail, 3),
            "expected_center": [round(value, 3) for value in expected],
            "opencv": {
                "gray_score": round(opencv_gray_score, 6),
                "color_score": round(opencv_color_score, 6),
            },
            "native": {
                "gray_score": round(float(gray_match[3]), 6),
                "color_score": round(float(color_match[3]), 6),
                "multiscale_scale": round(float(multiscale_match[2]), 6),
                "single_thread_found": bool(native_single_thread),
                "forced_multi_found": bool(native_forced_multi),
                "preferred_scale": round(float(preferred_match[2]), 6),
                "preferred_fallback_scale": round(
                    float(preferred_fallback_match[2]), 6
                ),
                "preferred_list_scale": round(
                    float(preferred_list_match[2]), 6
                ),
                "explicit_scale": round(float(explicit_match[2]), 6),
                "explicit_miss_empty": native_explicit_miss == [],
                "all_match_count": len(native_all or []),
                "excluded_region_empty": excluded == [],
                "multi_region_found": bool(native_multi_region),
                "scene_fingerprint_size": list(scene_fingerprint[:2]),
                "dxgi_change_detected": bool(dxgi_changed),
                "color_semantics_score": round(
                    float(color_semantics_match[3]), 6
                ),
                "color_negative_rejected": native_color_false_positive == [],
            },
            "parity": {
                "tolerance_pixels": PARITY_TOLERANCE_PIXELS,
                "tolerance_score": PARITY_TOLERANCE_SCORE,
                "distances": {
                    name: round(value, 3) for name, value in distances.items()
                },
                "score_deltas": {
                    name: round(value, 6)
                    for name, value in score_deltas.items()
                },
                "opencv_color_semantics_score": round(
                    opencv_color_semantics_score, 6
                ),
                "within_tolerance": True,
            },
            "foreground_unchanged": foreground_before == foreground_after,
            "smoke_window_never_foreground": True,
            "opencv_threads": int(cv2.getNumThreads()),
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "native_stats": core.performance_stats(),
            "error": "",
        }
    except Exception as error:
        # FR-20261008-101: ``core`` is bound inside the ``try`` above, so a failure
        # in ``_configure_win32()`` or the ``NativeVisionCore(...)`` construction
        # leaves it unbound and referencing it directly raised UnboundLocalError —
        # escaping this function and breaking the documented "Never propagates"
        # contract (the caller then never writes the ``--native-smoke-file``
        # report). Read it through a local sentinel instead.
        core_obj = locals().get("core")
        if core_obj is not None and getattr(core_obj, "available", False):
            try:
                native_stats = core_obj.performance_stats()
            except Exception:
                # FR-RISK-20261008-903: 兜底不得重放同一失败——原始异常可能
                # 恰好来自 try 末尾的 performance_stats()（DLL 半故障），重放
                # 会让异常从本 except 逃逸、报告不落盘，破坏 "Never propagates"。
                native_stats = {}
        else:
            native_stats = {}
        return {
            **base_report,
            "ok": False,
            "parity": {"within_tolerance": False},
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "native_stats": native_stats,
            "error": str(error),
        }
