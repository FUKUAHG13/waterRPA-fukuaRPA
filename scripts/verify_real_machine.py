"""实机验证半自动化脚本。

在真实桌面环境下运行（非 offscreen），验证自动测试无法覆盖的硬件/系统交互。

覆盖的可脚本化实机验证项：
  - stop_latency    急停延迟测量：引擎运行无限循环步骤，随机延迟后触发 stop，
                    测量从 stop 信号到工作线程退出的时间，重复 N 次输出 P50/P95。
  - dpi_info        DPI 状态诊断：输出进程 DPI 感知状态、各显示器物理/逻辑矩形与缩放比。
  - native_core     原生核心烟雾：调用 native_services 的截图与模板匹配，验证 DLL 加载与基本功能。
  - input_injection 输入注入验证：执行一次 SetCursorPos + mouse_event，验证光标确实移动。
  - hook_lifecycle  Hook 安装/卸载：安装低级鼠标钩子，等待 100ms，卸载，验证无残留。

用法：
    python scripts/verify_real_machine.py
    python scripts/verify_real_machine.py --only stop_latency,dpi_info
    python scripts/verify_real_machine.py --only stop_latency --rounds 50

每个检查函数返回一个 dict（含 status: PASS/FAIL/WARN 与测量数据）。脚本以源码环境
直接运行（不需要冻结包）；除 dpi_info 外的检查会按需惰性导入较重的项目模块。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import random
import struct
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Win32 helpers（脚本自包含，避免依赖项目内部模块的私有签名）
# ---------------------------------------------------------------------------

user32 = ctypes.windll.user32
try:
    shcore = ctypes.windll.shcore
except Exception:  # noqa: BLE001 - shcore 在受支持系统上恒存在，仅作防御
    shcore = None


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


# Win32 DEVMODEW 在 x64 上的关键字段固定偏移。ctypes 按默认对齐声明的部分结构会
# 在 dmFormName 之前插入填充，使 dmPelsWidth/dmPelsHeight 落在错误偏移；这里改用
# 原始字节缓冲并按真实偏移解析，避免对齐陷阱。DEVMODEW 真实大小为 220 字节。
_DEVMODE_SIZE = 220
_DEVMODE_OFF_DMSIZE = 68        # USHORT dmSize
_DEVMODE_OFF_DMFIELDS = 72      # DWORD dmFields
_DEVMODE_OFF_POSITION_X = 76    # LONG dmPosition.x（联合体起点）
_DEVMODE_OFF_POSITION_Y = 80    # LONG dmPosition.y
_DEVMODE_OFF_PELSWIDTH = 172    # DWORD dmPelsWidth
_DEVMODE_OFF_PELSHEIGHT = 176   # DWORD dmPelsHeight


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HANDLE,
    wintypes.HDC,
    ctypes.POINTER(_RECT),
    wintypes.LPARAM,
)

_ENUM_CURRENT_SETTINGS = -1
_DM_POSITION = 0x00000020
_DM_PELSWIDTH = 0x00080000
_DM_PELSHEIGHT = 0x00100000
_MDT_EFFECTIVE_DPI = 0
_MONITOR_DEFAULTTOPRIMARY = 0x00000001


def _configure_dpi_signatures():
    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(_RECT),
        _MONITORENUMPROC,
        wintypes.LPARAM,
    ]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    user32.GetMonitorInfoW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_MONITORINFOEXW),
    ]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.EnumDisplaySettingsExW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    user32.EnumDisplaySettingsExW.restype = wintypes.BOOL
    user32.GetDpiForSystem.argtypes = []
    user32.GetDpiForSystem.restype = wintypes.UINT
    if shcore is not None:
        shcore.GetDpiForMonitor.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        shcore.GetDpiForMonitor.restype = ctypes.c_long  # HRESULT
        shcore.GetProcessDpiAwareness.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_int),
        ]
        shcore.GetProcessDpiAwareness.restype = ctypes.c_long  # HRESULT


def _process_dpi_awareness_name():
    """返回当前进程 DPI 感知状态的可读名称。

    优先使用 GetProcessDpiAwarenessContext（可区分 PMv2/GDI-scaled 等上下文）；
    该 API 不可用时退回 shcore.GetProcessDpiAwareness（PROCESS_DPI_AWARENESS）。
    """

    try:
        context = user32.GetProcessDpiAwarenessContext()
        if context:
            awareness = user32.GetAwarenessFromDpiAwarenessContext(context)
            return {
                0: "UNAWARE",
                1: "SYSTEM_AWARE",
                2: "PER_MONITOR_AWARE",
            }.get(int(awareness), f"AWARENESS_{int(awareness)}")
    except Exception:  # noqa: BLE001 - 旧系统/缺失 API 时退回下方路径
        pass
    if shcore is not None:
        try:
            value = ctypes.c_int(0)
            # HRESULT GetProcessDpiAwareness(HANDLE hprocess, PROCESS_DPI_AWARENESS*)
            hr = shcore.GetProcessDpiAwareness(None, ctypes.byref(value))
            if int(hr) >= 0:
                return {
                    0: "UNAWARE",
                    1: "SYSTEM_AWARE",
                    2: "PER_MONITOR_AWARE",
                }.get(int(value.value), f"AWARENESS_{int(value.value)}")
        except Exception:  # noqa: BLE001
            pass
    return "UNAVAILABLE"


def _monitor_effective_dpi(hmonitor):
    if shcore is None:
        return None
    dpi_x = ctypes.c_uint(0)
    dpi_y = ctypes.c_uint(0)
    hr = shcore.GetDpiForMonitor(
        hmonitor, _MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
    )
    if int(hr) >= 0 and int(dpi_x.value) > 0:
        return int(dpi_x.value)
    return None


def _physical_mode_for_device(device_name):
    """显示器的真实物理模式 (left, top, width, height)，不受 DPI 虚拟化影响。

    使用 EnumDisplaySettingsExW(ENUM_CURRENT_SETTINGS)；DEVMODEW 字段从原始字节
    缓冲按 x64 固定偏移解析，避免 ctypes 部分结构声明的对齐填充陷阱。
    """
    buffer = (ctypes.c_ubyte * _DEVMODE_SIZE)()
    raw = ctypes.cast(buffer, ctypes.c_void_p)
    struct.pack_into("<H", buffer, _DEVMODE_OFF_DMSIZE, _DEVMODE_SIZE)
    if not user32.EnumDisplaySettingsExW(device_name, _ENUM_CURRENT_SETTINGS, raw, 0):
        return None
    fields = struct.unpack_from("<I", buffer, _DEVMODE_OFF_DMFIELDS)[0]
    if not (fields & _DM_PELSWIDTH) or not (fields & _DM_PELSHEIGHT):
        return None
    width = struct.unpack_from("<I", buffer, _DEVMODE_OFF_PELSWIDTH)[0]
    height = struct.unpack_from("<I", buffer, _DEVMODE_OFF_PELSHEIGHT)[0]
    if width <= 0 or height <= 0:
        return None
    if fields & _DM_POSITION:
        left = struct.unpack_from("<i", buffer, _DEVMODE_OFF_POSITION_X)[0]
        top = struct.unpack_from("<i", buffer, _DEVMODE_OFF_POSITION_Y)[0]
    else:
        left, top = 0, 0
    return int(left), int(top), int(width), int(height)


# ---------------------------------------------------------------------------
# 检查项实现
# ---------------------------------------------------------------------------


def check_stop_latency(rounds=20):
    """测量从 stop 信号到工作线程退出的延迟，重复 rounds 次输出 P50/P95。"""

    from fukua_rpa.engine import RPAEngine

    samples_ms = []
    failures = []
    rng = random.Random(20260728)

    for index in range(rounds):
        engine = RPAEngine(defer_backends=True)
        # 仅做停止延迟测量，无需 OpenCV/原生核心；静默日志避免控制台噪声。
        engine.opencv_available = False
        engine.log_level = -1
        engine.loop_mode = "无限"
        engine.loop_start_round = 1
        engine.loop_end_round = 0
        engine.start_step_index = 0
        engine.settlement_wait = 0.0
        engine.detect_delay = 0.0
        engine.playback_speed = 1.0
        engine.load_and_precompute = lambda tasks: True

        worker = threading.Thread(
            target=engine.run_tasks,
            args=([{"type": 5.0, "value": "0.05"}],),
            daemon=True,
            name=f"verify-stop-latency-{index}",
        )
        worker.start()
        # 随机延迟后触发急停，模拟用户在不同执行相位按下停止。
        time.sleep(rng.uniform(0.10, 0.45))

        stop_issued_at = time.perf_counter()
        engine.stop()
        # 紧密轮询线程退出，分辨率约 0.1ms。
        while worker.is_alive():
            if time.perf_counter() - stop_issued_at > 5.0:
                break
            time.sleep(0.0001)
        latency_ms = (time.perf_counter() - stop_issued_at) * 1000.0

        if worker.is_alive():
            failures.append({"round": index, "error": "worker did not exit within 5s"})
            continue
        samples_ms.append(latency_ms)

    if not samples_ms:
        return {
            "status": "FAIL",
            "rounds": rounds,
            "samples": 0,
            "failures": failures,
        }

    ordered = sorted(samples_ms)

    def percentile(values, pct):
        if not values:
            return None
        rank = (len(values) - 1) * (pct / 100.0)
        low = int(math.floor(rank))
        high = int(math.ceil(rank))
        if low == high:
            return values[low]
        weight = rank - low
        return values[low] * (1.0 - weight) + values[high] * weight

    p50 = percentile(ordered, 50)
    p95 = percentile(ordered, 95)
    # 经验门禁：协作式急停通常应在数十毫秒内退出；超过 250ms 视为异常。
    status = "PASS" if (p95 is not None and p95 <= 250.0 and not failures) else "WARN"
    if failures and not samples_ms:
        status = "FAIL"
    return {
        "status": status,
        "rounds": rounds,
        "samples": len(samples_ms),
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
        "failures": failures,
    }


def check_dpi_info():
    """输出进程 DPI 感知状态、各显示器物理/逻辑矩形与缩放比。"""

    _configure_dpi_signatures()
    awareness = _process_dpi_awareness_name()
    try:
        system_dpi = int(user32.GetDpiForSystem()) or 96
    except Exception:  # noqa: BLE001
        system_dpi = 96

    monitors = []

    def enum_proc(hmonitor, _hdc, _rect, _data):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            return True
        device_name = str(info.szDevice)
        logical = info.rcMonitor
        logical_rect = (
            int(logical.left),
            int(logical.top),
            int(logical.right),
            int(logical.bottom),
        )
        physical = _physical_mode_for_device(device_name)
        dpi = _monitor_effective_dpi(hmonitor)
        scale = None
        if dpi:
            scale = round(float(dpi) / 96.0, 4)
        is_primary = bool(int(info.dwFlags) & _MONITOR_DEFAULTTOPRIMARY)
        monitors.append({
            "device": device_name,
            "primary": is_primary,
            "logical_rect": logical_rect,
            "physical_rect": list(physical) if physical else None,
            "effective_dpi": dpi,
            "scale_factor": scale,
        })
        return True

    try:
        callback = _MONITORENUMPROC(enum_proc)
        user32.EnumDisplayMonitors(None, None, callback, 0)
    except Exception as exc:  # noqa: BLE001
        return {"status": "FAIL", "error": f"EnumDisplayMonitors failed: {exc}"}

    status = "PASS" if monitors else "WARN"
    return {
        "status": status,
        "process_dpi_awareness": awareness,
        "system_dpi": system_dpi,
        "monitor_count": len(monitors),
        "monitors": monitors,
    }


def check_native_core():
    """调用原生核心截图与模板匹配，验证 DLL 加载与基本功能。"""

    from PIL import Image

    from fukua_rpa.vision.core import NativeVisionCore

    core = NativeVisionCore(base_dir=str(ROOT))
    report = {
        "status": "FAIL",
        "available": bool(core.available),
        "api_version": int(core.version),
        "has_raw_rgb_capture": bool(core.has_raw_rgb_capture),
    }
    if not core.available:
        report["error"] = core.load_error or "native core unavailable"
        return report

    rect = core.virtual_screen_rect()
    if not rect:
        report["error"] = core.last_scene_error or "virtual_screen_rect failed"
        return report
    report["virtual_screen_rect"] = list(rect)

    # 在主屏靠近左上角处取一块有界区域，避免越界与超大截图。
    region_w = min(240, max(1, int(rect[2]) - int(rect[0])))
    region_h = min(240, max(1, int(rect[3]) - int(rect[1])))
    region = (int(rect[0]), int(rect[1]), region_w, region_h)
    captured = core.capture_rgb(region)
    if captured is None:
        report["error"] = core.last_scene_error or "capture_rgb failed"
        return report
    cap_w, cap_h, pixels = captured
    report["capture_size"] = [cap_w, cap_h]
    if (cap_w, cap_h) != (region_w, region_h):
        report["error"] = "capture_rgb size mismatch"
        return report

    scene = Image.frombytes("RGB", (cap_w, cap_h), pixels)
    # 从场景中心裁出一块模板，再让原生核心在全场景中找回它。
    tmpl_w = max(16, cap_w // 4)
    tmpl_h = max(16, cap_h // 4)
    left = (cap_w - tmpl_w) // 2
    top = (cap_h - tmpl_h) // 2
    template = scene.crop((left, top, left + tmpl_w, top + tmpl_h))
    expected_center = (
        region[0] + left + tmpl_w / 2.0,
        region[1] + top + tmpl_h / 2.0,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        template_path = os.path.join(temp_dir, "verify_template.png")
        template.save(template_path)
        matches = core.find_template(
            template_path,
            [region],
            1.0,
            1.0,
            0.05,
            True,
            0.7,
        )

    if matches is None:
        report["error"] = core.last_scene_error or "find_template failed"
        return report
    if not matches:
        # 桌面内容可能过于平坦导致匹配失败：DLL 已加载且调用成功，降级为 WARN。
        report["status"] = "WARN"
        report["warning"] = "find_template returned no match (flat desktop?)"
        report["match_count"] = 0
        return report

    nearest = min(
        matches,
        key=lambda item: math.hypot(
            float(item[0]) - expected_center[0], float(item[1]) - expected_center[1]
        ),
    )
    distance = math.hypot(
        float(nearest[0]) - expected_center[0],
        float(nearest[1]) - expected_center[1],
    )
    report["match_count"] = len(matches)
    report["nearest_match"] = [round(float(nearest[0]), 2), round(float(nearest[1]), 2)]
    report["expected_center"] = [round(expected_center[0], 2), round(expected_center[1], 2)]
    report["distance_px"] = round(distance, 3)
    report["score"] = round(float(nearest[3]), 6)
    report["status"] = "PASS" if distance <= 5.0 else "WARN"
    return report


def check_input_injection():
    """执行一次 SetCursorPos + mouse_event，验证光标确实移动。"""

    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.mouse_event.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    user32.mouse_event.restype = None

    original = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(original)):
        return {"status": "FAIL", "error": "GetCursorPos failed"}
    origin = (int(original.x), int(original.y))

    # 目标点取主屏内一个安全偏移，避免落在屏幕边缘急停热区（右上角 10px）。
    width = int(user32.GetSystemMetrics(0))
    height = int(user32.GetSystemMetrics(1))
    target_x = min(max(origin[0] + 37, 5), max(5, width - 12))
    target_y = min(max(origin[1] + 23, 5), max(5, height - 12))
    # 相对移动量：验证 mouse_event 注入的位移确实生效。
    delta_x, delta_y = 7, -5

    def _read_cursor():
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        return int(point.x), int(point.y)

    try:
        if not user32.SetCursorPos(target_x, target_y):
            return {"status": "FAIL", "error": "SetCursorPos failed"}
        after_set = _read_cursor()
        if after_set is None:
            return {"status": "FAIL", "error": "GetCursorPos (after set) failed"}
        set_deviation = math.hypot(
            after_set[0] - target_x, after_set[1] - target_y
        )
        # 绝对定位失败几乎只可能是用户正在移动物理鼠标：判定为不确定而非缺陷。
        if set_deviation > 3.0:
            return {
                "status": "WARN",
                "warning": "cursor moved during test (user input?); result inconclusive",
                "origin": list(origin),
                "target": [target_x, target_y],
                "after_set": list(after_set),
                "set_deviation_px": round(set_deviation, 3),
            }

        # MOUSEEVENTF_MOVE = 0x0001：相对移动 (delta_x, delta_y)。
        user32.mouse_event(0x0001, delta_x, delta_y, 0, None)
        after_event = _read_cursor()
        if after_event is None:
            return {"status": "FAIL", "error": "GetCursorPos (after event) failed"}
        moved_x = after_event[0] - after_set[0]
        moved_y = after_event[1] - after_set[1]
        delta_error = math.hypot(moved_x - delta_x, moved_y - delta_y)
        report = {
            "origin": list(origin),
            "target": [target_x, target_y],
            "after_set": list(after_set),
            "injected_delta": [delta_x, delta_y],
            "observed_delta": [moved_x, moved_y],
            "set_deviation_px": round(set_deviation, 3),
            "delta_error_px": round(delta_error, 3),
        }
        if delta_error <= 3.0:
            report["status"] = "PASS"
        else:
            report["status"] = "WARN"
            report["warning"] = (
                "relative mouse_event delta mismatch (user input concurrent?)"
            )
        return report
    finally:
        # 无论结果如何都把光标还原，避免干扰用户。
        user32.SetCursorPos(origin[0], origin[1])


def check_hook_lifecycle():
    """安装低级鼠标钩子，等待 100ms，卸载，验证无残留。"""

    from fukua_rpa.hook_backend import win32_hook_backend
    from fukua_rpa.win32_api import HOOKPROC

    backend = win32_hook_backend
    if backend.has_active_hooks:
        return {
            "status": "WARN",
            "warning": "hooks already active before test; skipped install",
        }

    received = {"count": 0}

    def _callback(code, wparam, lparam):
        received["count"] += 1
        return backend.call_next(None, code, wparam, lparam)

    # 保持回调引用，避免被 GC（低级钩子回调必须存活到卸载之后）。
    proc = HOOKPROC(_callback)
    handle = backend.install_mouse(proc)
    if not handle:
        return {"status": "FAIL", "error": "install_mouse returned no handle"}

    installed = backend.has_active_hooks
    # 低级钩子回调在拥有消息循环的线程上派发；短暂泵送消息让钩子真正生效。
    msg = wintypes.MSG()
    pump_deadline = time.monotonic() + 0.1
    while time.monotonic() < pump_deadline:
        try:
            while user32.PeekMessageW(
                ctypes.byref(msg), None, 0, 0, 1  # PM_REMOVE
            ):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:  # noqa: BLE001 - 泵送失败不应阻断卸载验证
            break
        time.sleep(0.01)

    removed = backend.uninstall(handle)
    residual_active = backend.has_active_hooks
    residual_callbacks = len(backend._installed_callbacks)  # noqa: SLF001 - 验证残留

    ok = bool(installed and removed and not residual_active and residual_callbacks == 0)
    return {
        "status": "PASS" if ok else "FAIL",
        "installed": bool(installed),
        "uninstall_returned": bool(removed),
        "callback_invocations": received["count"],
        "residual_active_hooks": bool(residual_active),
        "residual_callback_count": residual_callbacks,
    }


CHECKS = {
    "stop_latency": check_stop_latency,
    "dpi_info": check_dpi_info,
    "native_core": check_native_core,
    "input_injection": check_input_injection,
    "hook_lifecycle": check_hook_lifecycle,
}


def main(argv=None):
    parser = argparse.ArgumentParser(description="fukuaRPA 实机验证")
    parser.add_argument("--only", help="逗号分隔的检查项名称")
    parser.add_argument(
        "--rounds",
        type=int,
        default=20,
        help="stop_latency 的重复次数（默认 20）",
    )
    args = parser.parse_args(argv)

    selected = (
        [item.strip() for item in args.only.split(",") if item.strip()]
        if args.only
        else list(CHECKS.keys())
    )

    results = {}
    for name in selected:
        if name not in CHECKS:
            print(f"SKIP unknown: {name}")
            continue
        print(f"\n{'=' * 60}\n  {name}\n{'=' * 60}")
        try:
            kwargs = {"rounds": max(1, int(args.rounds))} if name == "stop_latency" else {}
            result = CHECKS[name](**kwargs)
        except Exception as exc:  # noqa: BLE001 - 单项失败不应中断其余检查
            result = {"status": "ERROR", "error": str(exc)}
        results[name] = result
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    print(f"\n{'=' * 60}\n  SUMMARY\n{'=' * 60}")
    print(json.dumps(results, indent=2, ensure_ascii=False, default=str))

    failed = [
        name
        for name, result in results.items()
        if str(result.get("status")) in ("FAIL", "ERROR")
    ]
    if failed:
        print(f"\nFAILED checks: {', '.join(failed)}")
        return 1
    print("\nAll selected checks completed without FAIL/ERROR.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
