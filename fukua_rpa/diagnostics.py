"""Offline runtime checks used by source and packaged-build smoke tests."""

from __future__ import annotations

import importlib
import logging
import os
import platform
import struct
import sys
import tempfile
import time
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)

from .constants import (  # noqa: E402
    APP_VERSION,
    BUILD_NAME,
    NATIVE_CORE_DLL_NAME,
    NATIVE_CORE_RELEASE_VERSION,
    SUPPORTED_WINDOWS_TEXT,
)
from .logging_service import log_queue_stats  # noqa: E402
from .paths import get_base_dir  # noqa: E402
from .privacy import safe_error_name  # noqa: E402
from .vision.core import NativeVisionCore  # noqa: E402
from .vision.opencv_runtime import configure_opencv_threads  # noqa: E402


@dataclass(frozen=True)
class DiagnosticCheck:
    """Single named diagnostic probe result.

    Attributes:
        name: Identifier for the check (e.g. ``import:cv2``).
        ok: Whether the check passed.
        detail: Human-readable detail or version string.
    """

    name: str
    ok: bool
    detail: str


def _windows_support_check() -> tuple[bool, str]:
    """Verify the current platform is a supported Windows version.

    Returns:
        A (supported, detail) tuple where *supported* is True only on
        Windows 10 1809+ (build >= 17763) or Windows 11.
    """
    if sys.platform != "win32":
        return False, f"当前平台为 {sys.platform}，仅支持 Windows"
    version = sys.getwindowsversion()
    if version.major < 10 or (version.major == 10 and version.build < 17763):
        return False, f"Windows build {version.build} 低于 1809 (17763)"
    return True, f"Windows {version.major}.{version.minor} build {version.build}"


def _uia_x64_helper_present(bin_dir: str) -> bool:
    """Whether a UIAutomationClient X64 helper DLL exists under *bin_dir*.

    DOC-20261006-009: matched by the stable ``UIAutomationClient`` prefix and the
    ``X64`` suffix so any VC runtime label is accepted — matching the decision
    already recorded in both PyInstaller specs. Variants without the X64 marker
    (32-bit) are deliberately not accepted.
    """
    try:
        names = os.listdir(bin_dir)
    except OSError:
        return False
    for name in names:
        lowered = name.casefold()
        if lowered.startswith("uiautomationclient") and lowered.endswith("x64.dll"):
            return True
    return False


def run_runtime_diagnostics(base_dir: str | None = None) -> dict[str, object]:
    """Run all offline runtime checks and assemble a diagnostic report.

    Probes architecture, Windows version, third-party imports, input and
    screenshot backends, OpenCV thread limits, native core DLL health,
    directory writability, and frozen-build plugin presence.

    Args:
        base_dir: Override for the runtime base directory.  Defaults to
            the application base directory.

    Returns:
        Report dict with format metadata, per-check results, native
        performance counters, and an overall ``ok`` flag.
    """
    # Structural overview (each section appends DiagnosticCheck entries):
    #   1. architecture + Windows version support
    #   2. third-party dependency imports (PySide6, cv2, numpy, ...)
    #   3. pyautogui input + screenshot backends
    #   4. OpenCV thread limit
    #   5. native core: DLL file, API version, performance counters,
    #      capabilities, ABI and build flags (wrapped so a faulty DLL cannot
    #      crash the whole diagnostic run)
    #   6. runtime directory writability probe
    #   7. frozen-build Qt platform plugin + UIA bitmap helper presence
    #   8. native health summary, assembled into the returned report
    base = os.path.abspath(base_dir or get_base_dir())
    checks: list[DiagnosticCheck] = []
    bits = struct.calcsize("P") * 8
    checks.append(DiagnosticCheck("architecture", bits == 64, f"{bits}-bit {platform.machine()}"))
    supported, detail = _windows_support_check()
    checks.append(DiagnosticCheck("windows_version", supported, detail))

    for module_name in (
        "PySide6",
        "cv2",
        "numpy",
        "PIL",
        "pyautogui",
        "mss",
        "uiautomation",
        "comtypes",
    ):
        try:
            module = importlib.import_module(module_name)
            version = getattr(module, "__version__", "bundled")
            checks.append(DiagnosticCheck(f"import:{module_name}", True, str(version)))
        except Exception as error:
            checks.append(
                DiagnosticCheck(
                    f"import:{module_name}", False, safe_error_name(error)
                )
            )

    try:
        pyautogui_module = importlib.import_module("pyautogui")
        position = pyautogui_module.position()
        screen_size = pyautogui_module.size()
        input_backend_ok = (
            len(position) == 2
            and len(screen_size) == 2
            and int(screen_size[0]) > 0
            and int(screen_size[1]) > 0
        )
        checks.append(
            DiagnosticCheck(
                "pyautogui_input_backend",
                input_backend_ok,
                f"screen={tuple(screen_size)}, cursor_position_redacted=true",
            )
        )
    except Exception as error:
        pyautogui_module = None
        checks.append(
            DiagnosticCheck(
                "pyautogui_input_backend", False, safe_error_name(error)
            )
        )

    try:
        if pyautogui_module is None:
            raise RuntimeError("PyAutoGUI input backend is unavailable")
        screenshot = pyautogui_module.screenshot(region=(0, 0, 1, 1))
        screenshot_size = tuple(getattr(screenshot, "size", ()))
        checks.append(
            DiagnosticCheck(
                "pyautogui_screenshot_backend",
                screenshot_size == (1, 1),
                f"size={screenshot_size}",
            )
        )
    except Exception as error:
        checks.append(
            DiagnosticCheck(
                "pyautogui_screenshot_backend", False, safe_error_name(error)
            )
        )

    try:
        cv2_module = importlib.import_module("cv2")
        opencv_threads = configure_opencv_threads(cv2_module)
        checks.append(
            DiagnosticCheck(
                "opencv_thread_limit",
                opencv_threads <= 2,
                f"threads={opencv_threads}",
            )
        )
    except Exception as error:
        opencv_threads = 0
        checks.append(
            DiagnosticCheck(
                "opencv_thread_limit", False, safe_error_name(error)
            )
        )

    resource_root = os.path.abspath(getattr(sys, "_MEIPASS", base))
    dll_candidates = [
        os.path.join(base, NATIVE_CORE_DLL_NAME),
        os.path.join(resource_root, NATIVE_CORE_DLL_NAME),
    ]
    dll_path = next((path for path in dll_candidates if os.path.isfile(path)), dll_candidates[0])
    dll_present = os.path.isfile(dll_path)
    checks.append(
        DiagnosticCheck(
            "native_dll_file",
            dll_present,
            "present" if dll_present else "missing",
        )
    )
    native = None
    native_performance: dict = {}
    native_capabilities: dict = {}
    native_abi: dict = {}
    try:
        native = NativeVisionCore(base_dir=base)
        checks.append(
            DiagnosticCheck(
                "native_dll_api",
                native.available and native.version >= NATIVE_CORE_RELEASE_VERSION,
                f"version={native.version}" if native.available else "unavailable",
            )
        )
        native_performance = native.performance_stats() if native.available else {}
        native_capabilities = native.capabilities() if native.available else {}
        native_abi = native.abi_snapshot()
        checks.append(
            DiagnosticCheck(
                "native_performance_api",
                bool(native_performance),
                (
                    f"captures={native_performance.get('captures', 0)}, "
                    f"cache_entries={native_performance.get('cache_entries', 0)}"
                    if native_performance
                    else "performance counters unavailable"
                ),
            )
        )
        required_native_capabilities = (
            "gdi_capture",
            "multi_scale",
            "grayscale",
            "color",
            "abi_metadata",
            "bounded_job_pool",
            "preferred_scale_fallback",
            "preferred_scale_list",
            "explicit_scale_only",
            "low_res_scene_fingerprint",
            "dxgi_scene_change",
            "cancellable_search",
            "opencv_color_semantics",
            "raw_rgb_capture",
        )
        checks.append(
            DiagnosticCheck(
                "native_capabilities",
                bool(native_capabilities)
                and all(native_capabilities.get(name) for name in required_native_capabilities),
                f"mask=0x{native_capabilities.get('mask', 0):X}",
            )
        )
        checks.append(
            DiagnosticCheck(
                "native_abi",
                native.available
                and native_abi.get("compatible", False)
                and native_abi.get("pointer_bits") == 64,
                (
                    f"pointer={native_abi.get('pointer_bits', 0)}, "
                    f"structs={native_abi.get('struct_sizes', {})}"
                ),
            )
        )
        required_build_flags = ("x64", "static_crt", "cpp17", "windows10_target", "msvc")
        build_flags = native_abi.get("build_flags", {})
        checks.append(
            DiagnosticCheck(
                "native_build_flags",
                native.available
                and all(build_flags.get(name, False) for name in required_build_flags),
                f"mask=0x{int(build_flags.get('mask', 0)):X}",
            )
        )
    except Exception as error:
        # FR-20261008-102: the first ``native_dll_api`` check is appended right
        # after construction, so a later native call raising re-appended the same
        # name (two contradictory rows) and dropped the capability/ABI/build-flag
        # rows entirely. Append only the names still missing so every check name
        # stays unique and the report keeps its structure.
        existing = {check.name for check in checks}
        detail = f"不可用: {safe_error_name(error)}"
        for name in (
            "native_dll_api",
            "native_performance_api",
            "native_capabilities",
            "native_abi",
            "native_build_flags",
        ):
            if name not in existing:
                checks.append(DiagnosticCheck(name, False, detail))

    writable = False
    write_detail = "writable"
    try:
        descriptor, probe = tempfile.mkstemp(prefix=".fukua_write_probe_", dir=base)
        os.close(descriptor)
        os.remove(probe)
        writable = True
    except OSError as error:
        write_detail = safe_error_name(error)
    checks.append(DiagnosticCheck("runtime_directory_writable", writable, write_detail))

    if getattr(sys, "frozen", False):
        qt_platform = os.path.join(
            resource_root, "PySide6", "plugins", "platforms", "qwindows.dll"
        )
        qt_platform_present = os.path.isfile(qt_platform)
        checks.append(
            DiagnosticCheck(
                "qt_windows_platform_plugin",
                qt_platform_present,
                "present" if qt_platform_present else "missing",
            )
        )
        uia_bin_dir = os.path.join(resource_root, "uiautomation", "bin")
        # DOC-20261006-009: 此前硬编码 "UIAutomationClient_VC140_X64.dll"，而两个
        # spec 的既定决定是"文件名通过前缀+后缀动态获取，不硬编码 VC runtime 标签"。
        # 上游换标签（如 VC142）时冻结包自检会误报 missing 并阻断 verify_release。
        # 这里按同一口径匹配：UIAutomationClient 前缀 + X64 后缀，接受任何 VC 标签，
        # 且排除 32 位与 ARM64 变体。
        uia_helper_present = _uia_x64_helper_present(uia_bin_dir)
        checks.append(
            DiagnosticCheck(
                "uia_bitmap_helper",
                uia_helper_present,
                "present" if uia_helper_present else "missing",
            )
        )

    native_health: dict = {}
    try:
        if native is not None:
            native_health = native.health_snapshot()
            native_health.pop("dll_path", None)
            native_health["last_error_present"] = bool(
                native_health.pop("last_error", "")
            )
            native_health["scene_error_present"] = bool(
                native_health.pop("scene_error", "")
            )
    except Exception as exc:
        logger.warning("收集原生核心健康状态失败: %s", exc, exc_info=True)
        native_health = {}
    executable_dir = os.path.dirname(os.path.abspath(sys.executable))
    runtime_directory_role = (
        "executable_directory"
        if os.path.normcase(base) == os.path.normcase(executable_dir)
        else "separate_data_directory"
    )

    return {
        "format": "fukuaRPA_runtime_diagnostics",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "application_version": APP_VERSION,
        "build_name": BUILD_NAME,
        "supported_windows": SUPPORTED_WINDOWS_TEXT,
        "runtime_directory_role": runtime_directory_role,
        "private_paths_redacted": True,
        "frozen": bool(getattr(sys, "frozen", False)),
        "ok": all(check.ok for check in checks),
        "native_performance": native_performance,
        "opencv_threads": opencv_threads,
        "native_health": native_health,
        "log_queue": log_queue_stats(),
        "checks": [asdict(check) for check in checks],
    }
