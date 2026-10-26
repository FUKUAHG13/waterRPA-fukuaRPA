# -*- mode: python ; coding: utf-8 -*-

from fukua_rpa.constants import ONEFILE_BUILD_NAME
from pathlib import Path
from PyInstaller.utils.hooks import collect_dynamic_libs
from scripts.runtime_pruning import (
    PYINSTALLER_EXCLUDES,
    apply_runtime_pruning,
    discover_uiautomation_x64_dll_basenames,
)


# Discover the actual uiautomation X64 helper DLL(s) at build time instead of
# hardcoding the VC runtime label (e.g. "VC140") in the filename. The fallback
# keeps selecting only the X64 build if the installed package cannot be
# inspected (matching by the stable "UIAutomationClient" prefix + "X64" suffix).
uia_dll_names = discover_uiautomation_x64_dll_basenames()


def _is_uia_client_dll(destination: str) -> bool:
    name = Path(destination).name.casefold()
    if any(destination.endswith(dll_name) for dll_name in uia_dll_names):
        return True
    return name.startswith("uiautomationclient") and name.endswith("x64.dll")


uia_binaries = [
    item
    for item in collect_dynamic_libs("uiautomation")
    if item[0].endswith(".dll") and _is_uia_client_dll(item[0])
]

a = Analysis(
    ["fukuaRPA.py"],
    pathex=[],
    binaries=[("fukua_rpa_core.dll", "."), *uia_binaries],
    datas=[
        ("assets/fukuaRPA.ico", "assets"),
        ("assets/step_settings.svg", "assets"),
        ("assets/search.svg", "assets"),
        ("LICENSE", "."),
    ],
    hiddenimports=["fukua_rpa.native_smoke", "pyautogui"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(PYINSTALLER_EXCLUDES),
    noarchive=False,
    optimize=0,
)
apply_runtime_pruning(a)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=ONEFILE_BUILD_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/fukuaRPA.ico",
    version="assets/version_info_onefile.txt",
    manifest="assets/fukuaRPA.manifest",
)
