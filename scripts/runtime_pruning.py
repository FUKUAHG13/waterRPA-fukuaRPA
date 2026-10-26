"""Conservative PyInstaller payload pruning shared by all release formats."""

from __future__ import annotations

import importlib.util
import re
from collections import Counter
from pathlib import Path

# These optional PyAutoGUI helpers are not used by fukuaRPA. Excluding them at
# graph-analysis time also prevents their tkinter dependency from being frozen.
# PySide6.QtNetwork and _ssl are excluded because the project never imports
# network modules (enforced by tests/test_module_boundaries.py).
PYINSTALLER_EXCLUDES = (
    "_tkinter",
    "mouseinfo",
    "pymsgbox",
    "tkinter",
    "_ssl",
    "PIL.AvifImagePlugin",
    "PIL.ImageCms",
    "PIL.ImageMath",
    "PIL.ImageTk",
    "PIL.WebPImagePlugin",
    "PySide6.QtNetwork",
)


_QT_OPTIONAL_DLLS = {
    "qt6opengl.dll",
    "qt6pdf.dll",
    "qt6qml.dll",
    "qt6qmlmeta.dll",
    "qt6qmlmodels.dll",
    "qt6qmlworkerscript.dll",
    "qt6quick.dll",
    "qt6virtualkeyboard.dll",
    "qt6network.dll",
}
_PIL_OPTIONAL_EXTENSIONS = (
    "_avif.",
    "_imagingcms.",
    "_imagingft.",
    "_imagingmath.",
    "_imagingtk.",
    "_webp.",
)
_CHINESE_QT_TRANSLATION = re.compile(r"_zh_(?:cn|tw)\.qm$", re.IGNORECASE)

# Windows desktop always uses qwindows.dll; qoffscreen is kept for headless
# diagnostics.  qdirect2d (Direct2D backend) and qminimal (minimal backend)
# are not used by fukuaRPA.
_QT_UNUSED_PLATFORM_PLUGINS = {
    "qdirect2d.dll",
    "qminimal.dll",
}

# Project template images are PNG/BMP/JPEG; UI icons use SVG/ICO.
# GIF, TIFF, TGA, WBMP, ICNS and WebP formats are never loaded.
_QT_UNUSED_IMAGEFORMAT_PLUGINS = {
    "qgif.dll",
    "qtiff.dll",
    "qtga.dll",
    "qwbmp.dll",
    "qicns.dll",
    "qwebp.dll",
}

# Mesa software OpenGL — fallback for missing/broken GPU drivers, RDP, VMs.
# Pruning this is OPTIONAL and disabled by default until validated on
# remote desktop, virtual machines and legacy GPU environments.
_QT_SOFTWARE_OPENGL = {
    "opengl32sw.dll",
}
PRUNE_OPENGL32SW = False  # Set True after RDP/VM/legacy-GPU validation.

# SSL/TLS payloads that should never appear in a no-network build.
# libcrypto is intentionally NOT listed: _hashlib links it for SHA-256.
_NETWORK_SSL_PAYLOADS = {
    "_ssl.pyd",
    "libssl-3-x64.dll",
    "libssl-3.dll",
}

# Qt 6 on supported Windows versions imports the unversioned Windows ICU ABI
# from System32. Agent hosts and document tools may prepend a private Poppler
# runtime whose versioned ICU binary was renamed to ``icuuc.dll``; collecting
# that file makes QtCore fail with ERROR_PROC_NOT_FOUND. Only package-root
# collisions are rejected so a future dependency-owned subdirectory remains an
# explicit packaging decision instead of being silently removed.
_FOREIGN_ICU_RUNTIME_DLLS = {"icuin.dll", "icuuc.dll"}
_FOREIGN_ICU_DATA_DLL = re.compile(r"icudt\d+\.dll$", re.IGNORECASE)


# The uiautomation wheel ships its native UIAutomationClient helper DLLs under
# <package>/bin/. The exact filename embeds a VC runtime label (e.g. the
# "VC140" in UIAutomationClient_VC140_X64.dll) that can change between
# releases, so we discover the actual X64 DLL at runtime instead of hardcoding
# the full name. The "X64" architecture suffix is stable and we must keep
# selecting only the X64 build (an X86 variant, UIAutomationClient_*_X86.dll,
# also ships alongside it).
def discover_uiautomation_x64_dll_basenames() -> set[str]:
    """Return basenames of the installed uiautomation X64 helper DLL(s)."""

    try:
        spec = importlib.util.find_spec("uiautomation")
    except (ImportError, ValueError, ModuleNotFoundError):
        return set()
    if spec is None or not spec.submodule_search_locations:
        return set()
    package_dir = Path(spec.submodule_search_locations[0])
    return {path.name for path in package_dir.rglob("UIAutomationClient*X64.dll")}


def find_uiautomation_x64_dll(search_root: Path) -> Path | None:
    """Locate the uiautomation X64 helper DLL beneath ``search_root``.

    Used for an already-built release tree (e.g. ``<release>/_internal``),
    where the DLL lives under ``uiautomation/bin/``. Returns the first match
    or ``None`` if no X64 helper DLL is present.
    """

    candidates = sorted(Path(search_root).rglob("UIAutomationClient*X64.dll"))
    return candidates[0] if candidates else None


def _normalized_destination(entry) -> str:
    return str(entry[0]).replace("\\", "/").removeprefix("./").casefold()


def pruning_group(entry) -> str | None:
    """Return the validated pruning group for one PyInstaller TOC entry."""

    destination = _normalized_destination(entry)
    name = destination.rsplit("/", 1)[-1]
    parts = destination.split("/")

    at_package_runtime_root = len(parts) == 1 or (
        len(parts) == 2 and parts[0] == "_internal"
    )
    if at_package_runtime_root and (
        name in _FOREIGN_ICU_RUNTIME_DLLS
        or _FOREIGN_ICU_DATA_DLL.fullmatch(name)
    ):
        return "foreign_icu_payloads"

    if name.startswith("opencv_videoio_ffmpeg") and name.endswith(".dll"):
        return "opencv_ffmpeg"

    if "/pyside6/" in f"/{destination}":
        if name in _QT_OPTIONAL_DLLS:
            return "qt_quick_qml_pdf_virtual_keyboard"
        if destination.endswith("/plugins/imageformats/qpdf.dll"):
            return "qt_quick_qml_pdf_virtual_keyboard"
        if destination.endswith(
            "/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll"
        ):
            return "qt_quick_qml_pdf_virtual_keyboard"
        if destination.endswith("/plugins/generic/qtuiotouchplugin.dll"):
            return "qt_quick_qml_pdf_virtual_keyboard"
        if "/translations/" in destination and not _CHINESE_QT_TRANSLATION.search(
            name
        ):
            return "non_chinese_qt_translations"
        if "/plugins/platforms/" in destination and name in _QT_UNUSED_PLATFORM_PLUGINS:
            return "qt_unused_platform_plugins"
        if (
            "/plugins/imageformats/" in destination
            and name in _QT_UNUSED_IMAGEFORMAT_PLUGINS
        ):
            return "qt_unused_imageformat_plugins"

    if PRUNE_OPENGL32SW and name in _QT_SOFTWARE_OPENGL:
        return "qt_software_opengl"

    if name in _NETWORK_SSL_PAYLOADS:
        return "network_ssl_payloads"

    if "/pil/" in f"/{destination}" and name.endswith(".pyd") and name.startswith(
        _PIL_OPTIONAL_EXTENSIONS
    ):
        return "pillow_optional_extensions"

    if name in {"_tkinter.pyd", "tcl86t.dll", "tk86t.dll"}:
        return "tcl_tk"
    # FR-20260811-D15-005: 改为对路径段匹配。原 startswith 只认包根相对路径
    # （_tcl_data/...），onedir 布局实际相对路径带 _internal/ 前缀（如
    # _internal/_tcl_data/...）时恒不成立，verify_release 的裁剪复现检查漏检。
    # 段匹配覆盖任意层级；tcl8 判定收窄为目录段（tcl8 或 tcl8.x），避免把
    # 任意 tcl8 开头命名的文件误归 tcl_tk。
    if "_tcl_data" in parts or "_tk_data" in parts or any(
        part == "tcl8" or part.startswith("tcl8.") for part in parts
    ):
        return "tcl_tk"
    return None


def _prune_toc(entries):
    kept = []
    removed = Counter()
    for entry in entries:
        group = pruning_group(entry)
        if group is None:
            kept.append(entry)
        else:
            removed[group] += 1
    return kept, removed


def apply_runtime_pruning(analysis) -> dict[str, int]:
    """Remove only payload groups covered by source and frozen smoke tests."""

    binaries, binary_counts = _prune_toc(analysis.binaries)
    datas, data_counts = _prune_toc(analysis.datas)
    analysis.binaries[:] = binaries
    analysis.datas[:] = datas
    counts = binary_counts + data_counts
    summary = dict(sorted(counts.items()))
    rendered = ", ".join(f"{name}={count}" for name, count in summary.items())
    print(f"fukuaRPA runtime pruning: {rendered or 'no matching payloads'}")
    return summary


def find_prunable_runtime_paths(root: Path) -> list[str]:
    """Find payloads that should never survive in a verified onedir build."""

    root = root.resolve()
    return [
        path.relative_to(root).as_posix()
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold())
        if path.is_file()
        and pruning_group((path.relative_to(root).as_posix(), "", "")) is not None
    ]
