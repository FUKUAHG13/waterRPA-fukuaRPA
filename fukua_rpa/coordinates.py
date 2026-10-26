"""Physical Windows desktop to logical Qt coordinate conversion."""

import ctypes
import logging
import struct
from ctypes import wintypes
from typing import Any

from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication

from .dpi_awareness import (
    DPI_AWARENESS_PER_MONITOR_AWARE,
    current_dpi_awareness,
    enable_per_monitor_v2,
)
from .win32_api import MONITORENUMPROC, MONITORINFOEXW, user32

logger = logging.getLogger(__name__)

# SAFETY: 本模块所有坐标转换/DPI 查询失败时均返回安全默认值（原坐标、96 DPI、None）
# 而非抛出异常。原因：坐标转换失败不应阻断整个工作流运行——RPA 执行器在步骤链中
# 调用本模块，若因单次转换异常中止全部后续步骤，对用户造成的损失远大于一次可能的
# 坐标偏移。失败时记录 WARNING 日志以便事后排查，但执行流继续。

# DESIGN: 混合 DPI 多屏坐标转换策略。
# Qt 的 screen.geometry()/virtualGeometry 以“逻辑像素”描述虚拟桌面，每个屏幕
# 各自带 devicePixelRatio(DPR)；而 SendInput/SetCursorPos 使用的 Windows 虚拟桌面
# 坐标系以主屏左上角为原点、以“物理像素”为单位。要让 local_to_physical 把 Qt 逻辑
# 点正确落到物理屏幕，必须让 EnumDisplayMonitors 也返回物理像素矩形。
#
# 进程入口和冻结包清单都声明 Per-Monitor DPI Aware V2。该上下文会让
# EnumDisplayMonitors 返回未经虚拟化的真实物理像素矩形，且与 SendInput 的坐标空间
# 一致。随后对每个屏幕独立换算：物理点 = 该屏幕物理起点 + (Qt 逻辑点 - 该屏幕逻辑
# 起点) × 该屏幕缩放比(物理尺寸/逻辑尺寸)。缩放比逐屏取自配对矩形，因而天然等于
# 该屏幕 DPR，副屏不再受主屏 DPI 牵连。单屏或各屏 DPI 一致时，虚拟化矩形与真实
# 物理矩形相同，本切换不改变任何换算结果，行为保持不变。
# DESIGN: 三态标记 None=未尝试, True=PMv2 已激活, False=激活失败（Qt 已锁定等）。
# 首次调用后缓存结果，避免重复系统调用和日志刷屏。调用方据返回值决定
# 是否使用物理像素几何（RegionWindow）或回退到逻辑坐标路径。
_dpi_awareness_state = None


def _ensure_physical_dpi_awareness() -> bool:
    """Best-effort switch to Per-Monitor V2 so monitor rects are true physical pixels.

    Returns True if PMv2 is active (either activated now or by a prior call),
    False if the switch failed (older Windows, awareness locked by Qt, etc.).
    Callers should fall back to logical-coordinate geometry when False is returned.
    """

    global _dpi_awareness_state
    if _dpi_awareness_state is not None:
        return _dpi_awareness_state
    try:
        # SAFETY: 不能以 SetProcessDpiAwarenessContext 的 BOOL 返回值判断最终
        # 状态。进程已由 manifest/入口设为 PMv2 时，再次调用会返回 FALSE，旧实现
        # 因而把正确的物理坐标误送进 System-DPI 二次缩放路径。
        actual_before = current_dpi_awareness()
        result = (
            True
            if actual_before == DPI_AWARENESS_PER_MONITOR_AWARE
            else enable_per_monitor_v2()
        )
        actual_after = current_dpi_awareness()
        if result or actual_after == DPI_AWARENESS_PER_MONITOR_AWARE:
            _dpi_awareness_state = True
            # DESIGN: 诊断日志——明确记录最终 DPI 感知状态，便于排查混合 DPI 多屏
            # 下“预览点位正确、实际点击偏移”的问题。PMv2 激活时 EnumDisplayMonitors
            # 返回真实物理像素矩形，且与 SendInput/SetCursorPos 坐标空间一致，截图
            # 后端返回的坐标可直接用于输入注入，无需任何转换。
            logger.info(
                "坐标映射：已确认 Per-Monitor DPI 感知；"
                "显示器矩形为真实物理像素，截图坐标与输入注入坐标空间一致"
            )
        else:
            _dpi_awareness_state = False
            logger.warning(
                "坐标映射：未能启用 Per-Monitor DPI 感知（宿主可能已锁定 DPI 上下文），"
                "回退到 GetSystemMetrics/逻辑坐标路径"
            )
            # DESIGN: 诊断日志——PMv2 升级失败时进程停留在 System DPI Aware。此时
            # EnumDisplayMonitors/GetSystemMetrics 返回系统 DPI 虚拟化矩形，而原生
            # DXGI 截图后端仍返回真实物理像素；副屏 DPI ≠ 主屏时二者不一致，必须经
            # physical_to_input_coordinates 虚拟化后才能交给 SetCursorPos。
            logger.info(
                "坐标映射：进程停留在 System DPI Aware；"
                "原生截图的物理像素坐标将在输入注入前按显示器 DPI 虚拟化"
            )
    except Exception as exc:
        _dpi_awareness_state = False
        logger.warning(
            "坐标映射：无法切换到 Per-Monitor V2 DPI 感知，回退到 GetSystemMetrics/逻辑坐标路径: %s",
            exc, exc_info=True,
        )
        logger.info(
            "坐标映射：进程停留在 System DPI Aware；"
            "原生截图的物理像素坐标将在输入注入前按显示器 DPI 虚拟化"
        )
    return _dpi_awareness_state


# ---------------------------------------------------------------------------
# System DPI Aware 回退路径：物理像素 -> 输入注入（虚拟化）坐标转换
# ---------------------------------------------------------------------------
#
# DESIGN: 坐标空间模型（仅 PMv2 升级失败、进程停留在 System DPI Aware 时生效）。
#   - 输入注入（pyautogui.moveTo -> SetCursorPos）在 System DPI Aware 下接受“系统 DPI
#     虚拟化”坐标：整个虚拟桌面被表示成所有显示器都按系统 DPI（即主屏登录 DPI）渲染。
#     这与 EnumDisplayMonitors/GetMonitorInfoW/GetSystemMetrics 在该模式下返回的矩形
#     是同一坐标空间。
#   - 原生 DXGI 截图/识别后端不受进程 DPI 感知影响，始终返回“真实物理像素”坐标
#     （wrpa_get_virtual_screen_rect 文档即声明返回物理虚拟桌面矩形，capture_rgb 按
#     显示器原生分辨率取像素）。mss/pyautogui 截图则跟随进程感知，在 System DPI Aware
#     下返回的正是虚拟化坐标，与 SetCursorPos 一致，无需转换。
#   - 因此只有“原生后端返回的物理像素坐标”在副屏 DPI ≠ 主屏时会与 SetCursorPos 期望
#     的虚拟化坐标产生偏移，需要逐屏换算：
#         虚拟化点 = 该屏虚拟化原点 + (物理点 - 该屏物理原点)
#                    × (该屏虚拟化尺寸 / 该屏物理尺寸)
#     缩放比直接由同一显示器的两套矩形推导，不依赖 GetDpiForMonitor。后者在 System
#     DPI Aware 进程中可能对所有屏只返回系统 DPI，无法可靠反映副屏缩放。
#
# DESIGN: 物理矩形来源。System DPI Aware 下 EnumDisplayMonitors 返回的是虚拟化矩形，
# 无法直接得到物理原点/尺寸；但 EnumDisplaySettingsExW(ENUM_CURRENT_SETTINGS) 返回的
# DEVMODEW.dmPosition/dmPelsWidth/dmPelsHeight 是显示器的真实物理模式，不受 DPI 虚拟
# 化影响。虚拟化矩形取自 GetMonitorInfoW，两者按设备名配对。
#
# DESIGN: 安全降级。任何一步失败（API 不可用、枚举失败、DEVMODE 缺少位置/尺寸标志、
# 无法定位点所在显示器）都返回原坐标，等价于当前行为，绝不引入错误的缩放。PMv2 激活
# 时（_dpi_awareness_state 为 True）直接恒等返回，保证不改变已正确的路径。

_ENUM_CURRENT_SETTINGS = -1
_DM_POSITION = 0x00000020
_DM_PELSWIDTH = 0x00080000
_DM_PELSHEIGHT = 0x00100000

# Win32 DEVMODEW 在 x64 上的固定布局。旧实现用“部分 ctypes 结构”描述联合体，
# 把 dmDisplayOrientation/dmDisplayFixedOutput 重复计算了一次，导致宽高字段整体
# 错后 8 字节。使用原始缓冲和官方固定偏移可以避免联合体对齐再次被写错。
_DEVMODE_SIZE = 220
_DEVMODE_OFF_DMSIZE = 68
_DEVMODE_OFF_DMFIELDS = 72
_DEVMODE_OFF_POSITION_X = 76
_DEVMODE_OFF_POSITION_Y = 80
_DEVMODE_OFF_PELSWIDTH = 172
_DEVMODE_OFF_PELSHEIGHT = 176


def _configure_dpi_conversion_signatures() -> None:
    try:
        user32.EnumDisplaySettingsExW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        user32.EnumDisplaySettingsExW.restype = wintypes.BOOL
    except Exception as exc:  # noqa: BLE001
        logger.warning("EnumDisplaySettingsExW 签名配置失败: %s", exc, exc_info=True)


_configure_dpi_conversion_signatures()

# 缓存：{"monitors": [...], "vscreen": (l, t, r, b)}。vscreen 用于检测显示器拓扑变化。
_physical_input_map_cache = None


def _decode_display_mode(raw) -> tuple[int, int, int, int] | None:
    """Decode the physical display rectangle from a DEVMODEW byte buffer."""

    if raw is None or len(raw) < _DEVMODE_SIZE:
        return None
    fields = struct.unpack_from("<I", raw, _DEVMODE_OFF_DMFIELDS)[0]
    if not (fields & _DM_PELSWIDTH) or not (fields & _DM_PELSHEIGHT):
        return None
    width = struct.unpack_from("<I", raw, _DEVMODE_OFF_PELSWIDTH)[0]
    height = struct.unpack_from("<I", raw, _DEVMODE_OFF_PELSHEIGHT)[0]
    if width <= 0 or height <= 0:
        return None
    if fields & _DM_POSITION:
        left = struct.unpack_from("<i", raw, _DEVMODE_OFF_POSITION_X)[0]
        top = struct.unpack_from("<i", raw, _DEVMODE_OFF_POSITION_Y)[0]
    else:
        left, top = 0, 0
    return int(left), int(top), int(width), int(height)


def _physical_mode_for_device(device_name) -> tuple[int, int, int, int] | None:
    """Return the true physical (left, top, width, height) for a display device.

    Uses EnumDisplaySettingsExW(ENUM_CURRENT_SETTINGS), which reports the actual
    display mode and is not subject to DPI virtualization. Returns None when the
    mode or required DEVMODE fields are unavailable.
    """
    try:
        mode = (ctypes.c_ubyte * _DEVMODE_SIZE)()
        struct.pack_into("<H", mode, _DEVMODE_OFF_DMSIZE, _DEVMODE_SIZE)
        if not user32.EnumDisplaySettingsExW(
            device_name,
            _ENUM_CURRENT_SETTINGS,
            ctypes.cast(mode, ctypes.c_void_p),
            0,
        ):
            return None
        return _decode_display_mode(mode)
    except Exception as exc:  # noqa: BLE001 - 任一失败都让上层安全降级
        logger.warning(
            "获取显示器 %s 物理模式失败（使用默认值）: %s",
            device_name, exc, exc_info=True,
        )
        return None


def _virtual_screen_rect() -> tuple[int, int, int, int] | None:
    """Bounding virtual-desktop rect via GetSystemMetrics (SM_*VIRTUALSCREEN)."""
    try:
        left = int(user32.GetSystemMetrics(76))
        top = int(user32.GetSystemMetrics(77))
        width = int(user32.GetSystemMetrics(78))
        height = int(user32.GetSystemMetrics(79))
        if width > 0 and height > 0:
            return left, top, left + width, top + height
    except Exception as exc:  # noqa: BLE001
        logger.warning("GetSystemMetrics 虚拟桌面矩形查询失败: %s", exc, exc_info=True)
    return None


def _build_physical_input_map() -> dict[str, Any] | None:
    """Build the per-monitor physical->virtualized mapping, or None on failure.

    Each entry carries the physical rect (from the display mode), the virtualized
    rect (from GetMonitorInfoW under System DPI Aware) and independent x/y scale
    factors derived from those rectangles.
    """
    collected = []

    def enum_proc(hmonitor, _hdc, _rectangle, _data) -> bool:
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            return True
        device_name = str(info.szDevice)
        virtual = info.rcMonitor
        physical = _physical_mode_for_device(device_name)
        if physical is None:
            # 无法取得物理模式：放弃整张表，让转换安全退化为恒等。
            collected.clear()
            collected.append(None)
            return False
        phys_left, phys_top, phys_width, phys_height = physical
        virtual_width = int(virtual.right - virtual.left)
        virtual_height = int(virtual.bottom - virtual.top)
        if min(phys_width, phys_height, virtual_width, virtual_height) <= 0:
            collected.clear()
            collected.append(None)
            return False
        scale_x = float(virtual_width) / float(phys_width)
        scale_y = float(virtual_height) / float(phys_height)
        collected.append({
            "physical": (phys_left, phys_top, phys_width, phys_height),
            "virtual": (
                int(virtual.left),
                int(virtual.top),
                virtual_width,
                virtual_height,
            ),
            "scale_x": scale_x,
            "scale_y": scale_y,
            # Kept for old in-memory/test maps; new math uses the axis values.
            "scale": (scale_x + scale_y) / 2.0,
        })
        return True

    try:
        callback = MONITORENUMPROC(enum_proc)
        user32.EnumDisplayMonitors(None, None, callback, 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("EnumDisplayMonitors 枚举显示器失败，坐标映射表不可用: %s", exc, exc_info=True)
        return None
    if not collected or any(item is None for item in collected):
        return None
    return {"monitors": collected, "vscreen": _virtual_screen_rect()}


def _get_physical_input_map() -> dict[str, Any] | None:
    """Return a cached physical->virtualized map, rebuilding on topology change."""
    global _physical_input_map_cache
    current = _virtual_screen_rect()
    cached = _physical_input_map_cache
    if cached is not None and cached.get("vscreen") == current and current is not None:
        return cached
    rebuilt = _build_physical_input_map()
    if rebuilt is not None:
        _physical_input_map_cache = rebuilt
        return rebuilt
    # 重建失败时沿用旧表（若有），否则返回 None 触发恒等降级。
    return cached


def physical_to_input_coordinates(x: int, y: int) -> tuple[int, int]:
    """Convert physical pixel coordinates to input injection coordinates.

    Under Per-Monitor V2 DPI awareness, physical pixels equal input coordinates
    (identity transform). Under System DPI Aware fallback, applies per-monitor
    virtualization scaling so SetCursorPos targets the correct physical position.

    Only native DXGI backend coordinates need this conversion; mss/pyautogui
    backends already return virtualized coordinates under System DPI Aware and
    callers should not pass those through this function.

    Args:
        x: Physical pixel X coordinate.
        y: Physical pixel Y coordinate.

    Returns:
        Coordinate pair suitable for SetCursorPos/input injection. When the
        mapping cannot be determined (API unavailable, enumeration failure,
        point outside all monitors), returns the original coordinates unchanged.
    """
    if _ensure_physical_dpi_awareness() is not False:
        # PMv2 已激活（或此前已确认激活）：两个坐标空间一致，恒等返回。
        return int(x), int(y)

    mapping = _get_physical_input_map()
    if not mapping:
        return int(x), int(y)

    px = float(x)
    py = float(y)
    monitors = mapping["monitors"]

    # 优先选择物理矩形包含该点的显示器。
    entry = None
    for item in monitors:
        left, top, width, height = item["physical"]
        if left <= px < left + width and top <= py < top + height:
            entry = item
            break

    if entry is None:
        # 点落在所有物理显示器之外（如越界坐标）：选物理矩形距离最近的显示器，
        # 仍按该屏缩放比换算；若连这也做不到则恒等返回。
        def _distance(item) -> float:
            left, top, width, height = item["physical"]
            dx = max(left - px, 0.0, px - (left + width))
            dy = max(top - py, 0.0, py - (top + height))
            return dx * dx + dy * dy

        if not monitors:
            return int(x), int(y)
        entry = min(monitors, key=_distance)

    phys_left, phys_top, _w, _h = entry["physical"]
    virt_left, virt_top, _vr, _vb = entry["virtual"]
    scale_x = float(entry.get("scale_x", entry.get("scale", 1.0)))
    scale_y = float(entry.get("scale_y", entry.get("scale", 1.0)))
    input_x = virt_left + (px - phys_left) * scale_x
    input_y = virt_top + (py - phys_top) * scale_y
    return int(round(input_x)), int(round(input_y))


def input_to_physical_coordinates(x: int, y: int) -> tuple[int, int]:
    """Convert input injection (virtualized) coordinates back to physical pixels.

    This is the inverse of :func:`physical_to_input_coordinates`. The native DXGI
    backend and the recognition core operate in true physical pixels, while the
    regions stored in ``last_target_positions`` and user scan regions live in the
    virtualized space that ``SetCursorPos`` consumes. Before handing such a region
    to the native DLL (``find_template`` / ``capture_rgb``) it must be converted
    to physical pixels first, otherwise a System DPI Aware fallback mislocates
    the search window (P1-2).

    Under Per-Monitor V2 DPI awareness the two spaces coincide, so this returns
    the input unchanged (identity transform). Under System DPI Aware fallback it
    reverses the per-monitor virtualization. When the mapping cannot be
    determined it degrades to identity, so callers never receive a wrong scale.
    """
    if _ensure_physical_dpi_awareness() is not False:
        # PMv2 已激活（或此前已确认激活）：两个坐标空间一致，恒等返回。
        return int(x), int(y)

    mapping = _get_physical_input_map()
    if not mapping:
        return int(x), int(y)

    ix = float(x)
    iy = float(y)
    monitors = mapping["monitors"]

    # 优先选择虚拟矩形包含该点的显示器。
    entry = None
    for item in monitors:
        left, top, width, height = item["virtual"]
        if left <= ix < left + width and top <= iy < top + height:
            entry = item
            break

    if entry is None:
        # 点落在所有虚拟显示器之外：选虚拟矩形距离最近者，仍按该屏缩放比换算；
        # 若连这也做不到则恒等返回。
        def _distance(item) -> float:
            left, top, width, height = item["virtual"]
            dx = max(left - ix, 0.0, ix - (left + width))
            dy = max(top - iy, 0.0, iy - (top + height))
            return dx * dx + dy * dy

        if not monitors:
            return int(x), int(y)
        entry = min(monitors, key=_distance)

    phys_left, phys_top, _w, _h = entry["physical"]
    virt_left, virt_top, _vr, _vb = entry["virtual"]
    scale_x = float(entry.get("scale_x", entry.get("scale", 1.0)))
    scale_y = float(entry.get("scale_y", entry.get("scale", 1.0)))
    if not scale_x or not scale_y:
        return int(x), int(y)
    physical_x = phys_left + (ix - virt_left) / scale_x
    physical_y = phys_top + (iy - virt_top) / scale_y
    return int(round(physical_x)), int(round(physical_y))


def input_to_physical_rect(rect) -> tuple[int, int, int, int] | None:
    """Return the physical bounding rectangle for one input-space rectangle.

    Call :func:`input_rect_to_physical_regions` when the consumer accepts more
    than one rectangle. A cross-monitor region is piecewise under mixed DPI;
    its bounding rectangle is retained only for single-rectangle Win32 APIs.
    """
    regions = input_rect_to_physical_regions(rect)
    if not regions:
        return None
    left = min(region[0] for region in regions)
    top = min(region[1] for region in regions)
    right = max(region[0] + region[2] for region in regions)
    bottom = max(region[1] + region[3] for region in regions)
    return left, top, max(1, right - left), max(1, bottom - top)


def input_rect_to_physical_regions(rect) -> list[tuple[int, int, int, int]]:
    """Split and convert an input-space rectangle at monitor boundaries."""

    if not rect:
        return []
    x, y, w, h = (int(v) for v in rect)
    if w <= 0 or h <= 0:
        return []
    if _ensure_physical_dpi_awareness() is not False:
        return [(x, y, w, h)]

    mapping = _get_physical_input_map()
    if not mapping:
        first = input_to_physical_coordinates(x, y)
        last = input_to_physical_coordinates(x + w, y + h)
        left, right = sorted((first[0], last[0]))
        top, bottom = sorted((first[1], last[1]))
        return [(left, top, max(1, right - left), max(1, bottom - top))]

    source_left = float(x)
    source_top = float(y)
    source_right = float(x + w)
    source_bottom = float(y + h)
    regions = []
    for entry in mapping["monitors"]:
        virt_left, virt_top, virt_width, virt_height = entry["virtual"]
        intersect_left = max(source_left, float(virt_left))
        intersect_top = max(source_top, float(virt_top))
        intersect_right = min(source_right, float(virt_left + virt_width))
        intersect_bottom = min(source_bottom, float(virt_top + virt_height))
        if intersect_right <= intersect_left or intersect_bottom <= intersect_top:
            continue

        phys_left, phys_top, _phys_width, _phys_height = entry["physical"]
        scale_x = float(entry.get("scale_x", entry.get("scale", 1.0)))
        scale_y = float(entry.get("scale_y", entry.get("scale", 1.0)))
        if not scale_x or not scale_y:
            continue
        converted_left = phys_left + (intersect_left - virt_left) / scale_x
        converted_top = phys_top + (intersect_top - virt_top) / scale_y
        converted_right = phys_left + (intersect_right - virt_left) / scale_x
        converted_bottom = phys_top + (intersect_bottom - virt_top) / scale_y
        left, right = sorted((int(round(converted_left)), int(round(converted_right))))
        top, bottom = sorted((int(round(converted_top)), int(round(converted_bottom))))
        if right > left and bottom > top:
            regions.append((left, top, right - left, bottom - top))

    if regions:
        return regions
    first = input_to_physical_coordinates(x, y)
    last = input_to_physical_coordinates(x + w, y + h)
    left, right = sorted((first[0], last[0]))
    top, bottom = sorted((first[1], last[1]))
    return [(left, top, max(1, right - left), max(1, bottom - top))]


def physical_to_input_rect(rect) -> tuple[int, int, int, int] | None:
    """Convert one physical bounding rectangle to input-space coordinates."""

    if not rect:
        return None
    x, y, w, h = (int(v) for v in rect)
    if w <= 0 or h <= 0:
        return None
    first = physical_to_input_coordinates(x, y)
    last = physical_to_input_coordinates(x + w, y + h)
    left, right = sorted((first[0], last[0]))
    top, bottom = sorted((first[1], last[1]))
    return left, top, max(1, right - left), max(1, bottom - top)


def system_dpi_physical_monitor_regions() -> list[tuple[int, int, int, int]]:
    """Return per-monitor physical regions only for the System-DPI fallback."""

    if _ensure_physical_dpi_awareness() is not False:
        return []
    mapping = _get_physical_input_map()
    if not mapping:
        return []
    regions = []
    for entry in mapping["monitors"]:
        left, top, width, height = entry["physical"]
        regions.append(
            (int(left), int(top), int(width), int(height))
        )
    return regions


class ScreenCoordinateMapper:
    """Bidirectional mapper between Qt logical screen coordinates and physical desktop pixels.

    Pairs each Qt QScreen (logical geometry, device-pixel-ratio scaled) with its
    corresponding physical monitor rectangle obtained via EnumDisplayMonitors under
    Per-Monitor V2 DPI awareness. Provides point and rectangle conversions in both
    directions for use by the preview overlay and input injection subsystems.
    """

    def __init__(self, *, screens=None, physical_monitors=None) -> None:
        """Initialize the mapper by pairing Qt screens with physical monitors.

        Args:
            screens: Optional list of QScreen objects. Defaults to
                QApplication.screens().
            physical_monitors: Optional pre-enumerated physical monitor dicts.
                When None, enumerates via EnumDisplayMonitors internally.
        """
        self.screens = list(
            QApplication.screens() if screens is None else screens
        )
        self.virtual_rect = QRect()
        for screen in self.screens:
            rectangle = screen.geometry()
            self.virtual_rect = (
                QRect(rectangle) if self.virtual_rect.isNull() else self.virtual_rect.united(rectangle)
            )
        if self.virtual_rect.isNull():
            primary = QApplication.primaryScreen()
            self.virtual_rect = (
                primary.virtualGeometry()
                if primary is not None
                else QRect(0, 0, 1, 1)
            )

        physical_items = (
            self._physical_monitors()
            if physical_monitors is None
            else list(physical_monitors)
        )
        physical_by_name = {
            str(item["name"]).upper(): dict(item) for item in physical_items
        }
        unused = list(physical_by_name.values())
        self.entries = []
        for screen in self.screens:
            name = str(screen.name()).upper()
            physical = physical_by_name.get(name)
            if physical in unused:
                unused.remove(physical)
            if physical is None and unused:
                physical = min(
                    unused,
                    key=lambda item: abs(
                        item["width"] - screen.geometry().width() * screen.devicePixelRatio()
                    ) + abs(item["height"] - screen.geometry().height() * screen.devicePixelRatio()),
                )
                unused.remove(physical)
                logger.debug(
                    "坐标映射：屏幕 %s 名称不匹配，按尺寸回退配对到 %s",
                    name,
                    physical.get("name", "?"),
                )
            if physical is None:
                rectangle = screen.geometry()
                ratio = max(0.01, float(screen.devicePixelRatio()))
                physical = {
                    "name": name,
                    "left": int(round(rectangle.x() * ratio)),
                    "top": int(round(rectangle.y() * ratio)),
                    "width": max(1, int(round(rectangle.width() * ratio))),
                    "height": max(1, int(round(rectangle.height() * ratio))),
                }
            self.entries.append({"logical": QRect(screen.geometry()), "physical": physical})

    def _physical_monitors(self) -> list[dict[str, Any]]:
        monitors = []

        def enum_proc(hmonitor, _hdc, _rectangle, _data) -> bool:
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                rectangle = info.rcMonitor
                device_name = str(info.szDevice)
                physical_mode = _physical_mode_for_device(device_name)
                if physical_mode is not None:
                    left, top, width, height = physical_mode
                else:
                    # Safe fallback: under PMv2 rcMonitor is already physical;
                    # under a locked System-DPI host this keeps the old behavior.
                    left = int(rectangle.left)
                    top = int(rectangle.top)
                    width = max(1, int(rectangle.right - rectangle.left))
                    height = max(1, int(rectangle.bottom - rectangle.top))
                monitors.append({
                    "name": device_name,
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                })
            return True

        try:
            # DESIGN: 入口通常已经启用 PMv2；若宿主锁定为 System DPI，仍通过
            # EnumDisplaySettingsExW 取得真实物理模式，不能把虚拟化 rcMonitor
            # 冒充成物理矩形。
            _ensure_physical_dpi_awareness()
            callback = MONITORENUMPROC(enum_proc)
            user32.EnumDisplayMonitors(None, None, callback, 0)
        except Exception as exc:
            logger.warning("枚举显示器失败: %s", exc, exc_info=True)
            return []
        return monitors

    def apply_to(self, widget) -> None:
        """Set a widget's geometry to cover the entire virtual desktop.

        Args:
            widget: QWidget to resize and reposition.
        """
        widget.setGeometry(self.virtual_rect)

    def _entry_for_physical(self, x, y) -> dict[str, Any] | None:
        for entry in self.entries:
            rectangle = entry["physical"]
            if (
                rectangle["left"] <= x < rectangle["left"] + rectangle["width"]
                and rectangle["top"] <= y < rectangle["top"] + rectangle["height"]
            ):
                return entry
        if self.entries:
            logger.debug(
                "physical point (%s, %s) outside all monitors; falling back to entries[0]",
                x,
                y,
            )
        return self.entries[0] if self.entries else None

    def _entry_for_logical(self, x, y) -> dict[str, Any] | None:
        point = QPoint(int(round(x)), int(round(y)))
        for entry in self.entries:
            if entry["logical"].contains(point):
                return entry
        if self.entries:
            logger.debug(
                "logical point (%s, %s) outside all screens; falling back to entries[0]",
                x,
                y,
            )
        return self.entries[0] if self.entries else None

    def physical_to_local(self, x, y) -> QPoint:
        """Convert physical desktop pixel coordinates to widget-local Qt coordinates.

        Locates the monitor containing the physical point, applies per-screen
        scaling (physical -> logical), then translates into the overlay widget's
        local coordinate space (origin at virtual_rect top-left).

        Args:
            x: Physical pixel X coordinate.
            y: Physical pixel Y coordinate.

        Returns:
            QPoint in widget-local logical coordinates.
        """
        entry = self._entry_for_physical(float(x), float(y))
        if entry is None:
            return QPoint(int(round(x)), int(round(y)))
        logical = entry["logical"]
        physical = entry["physical"]
        global_x = logical.x() + (float(x) - physical["left"]) * max(1, logical.width()) / physical["width"]
        global_y = logical.y() + (float(y) - physical["top"]) * max(1, logical.height()) / physical["height"]
        return QPoint(
            int(round(global_x - self.virtual_rect.x())),
            int(round(global_y - self.virtual_rect.y())),
        )

    def local_to_physical(self, point) -> tuple[int, int]:
        """Convert widget-local Qt logical coordinates to physical desktop pixels.

        Translates the local point to global logical coordinates, locates the
        containing screen, and applies per-screen scaling (logical -> physical).
        The result is suitable for SendInput/SetCursorPos.

        Args:
            point: QPoint in widget-local logical coordinates.

        Returns:
            Tuple (x, y) of physical pixel coordinates.
        """
        global_x = float(point.x() + self.virtual_rect.x())
        global_y = float(point.y() + self.virtual_rect.y())
        entry = self._entry_for_logical(global_x, global_y)
        if entry is None:
            return int(round(global_x)), int(round(global_y))
        logical = entry["logical"]
        physical = entry["physical"]
        physical_x = physical["left"] + (
            (global_x - logical.x()) * physical["width"] / max(1, logical.width())
        )
        physical_y = physical["top"] + (
            (global_y - logical.y()) * physical["height"] / max(1, logical.height())
        )
        return int(round(physical_x)), int(round(physical_y))

    def local_rect_to_physical(self, rectangle) -> tuple[int, int, int, int]:
        """Convert a widget-local logical QRect to a physical pixel rectangle.

        Maps both corners through local_to_physical and normalizes the result
        into (left, top, width, height) with minimum 1px dimensions.

        Args:
            rectangle: QRect in widget-local logical coordinates.

        Returns:
            Tuple (left, top, width, height) in physical pixels.
        """
        first = self.local_to_physical(rectangle.topLeft())
        last = self.local_to_physical(
            QPoint(rectangle.x() + rectangle.width(), rectangle.y() + rectangle.height())
        )
        left, right = sorted((first[0], last[0]))
        top, bottom = sorted((first[1], last[1]))
        return left, top, max(1, right - left), max(1, bottom - top)
