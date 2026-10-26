"""Separate writable user data from bundled read-only application resources."""

import os
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache

PORTABLE_FLAG_NAME = "portable.flag"


@dataclass(frozen=True)
class RuntimePathInfo:
    """Resolved installation and data directory pair for the current runtime.

    Attributes:
        install_dir: Read-only application resource root.
        data_dir: Writable directory for user profiles, logs, and settings.
        mode: Detection outcome — one of ``"source"``, ``"portable"``,
            ``"portable_forced"``, or ``"local_app_data"``.
    """

    install_dir: str
    data_dir: str
    mode: str


def get_install_dir() -> str:
    """Return the read-only application resource root directory.

    In a frozen (PyInstaller) build this is the directory containing the
    executable; in a source checkout it is the repository root (parent of
    the ``fukua_rpa`` package).

    Returns:
        Absolute path to the installation directory.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _directory_is_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        descriptor, probe = tempfile.mkstemp(prefix=".fukua_write_probe_", dir=path)
        os.close(descriptor)
        os.remove(probe)
        return True
    except OSError:
        return False


@lru_cache(maxsize=1)
def runtime_path_info() -> RuntimePathInfo:
    """Detect and cache the install/data directory pair for this runtime.

    Detection order: source checkout → portable (install dir writable) →
    portable_forced (flag file present) → %LOCALAPPDATA% fallback.

    Returns:
        A frozen RuntimePathInfo with resolved directories and mode label.

    Raises:
        RuntimeError: If ``portable.flag`` exists but the install directory
            is not writable.
    """
    install_dir = get_install_dir()
    if not getattr(sys, "frozen", False):
        return RuntimePathInfo(install_dir, install_dir, "source")
    portable_forced = os.path.isfile(os.path.join(install_dir, PORTABLE_FLAG_NAME))
    if _directory_is_writable(install_dir):
        return RuntimePathInfo(
            install_dir,
            install_dir,
            "portable_forced" if portable_forced else "portable",
        )
    if portable_forced:
        raise RuntimeError(
            "检测到 portable.flag，但程序目录不可写。请把完整软件文件夹移动到有写入权限的位置。"
        )
    local_app_data = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local"
    )
    data_dir = os.path.join(local_app_data, "fukuaRPA")
    os.makedirs(data_dir, exist_ok=True)
    return RuntimePathInfo(install_dir, data_dir, "local_app_data")


def get_base_dir() -> str:
    """Return the writable user-data directory for profiles and logs.

    Returns:
        Absolute path resolved by :func:`runtime_path_info`.
    """
    return runtime_path_info().data_dir


def get_resource_path(relative_path: str) -> str:
    """Resolve a bundled read-only asset in both source and PyInstaller builds.

    Args:
        relative_path: Path relative to the resource root (e.g.
            ``"assets/icon.ico"``).

    Returns:
        Absolute path to the resource file.
    """
    if getattr(sys, "frozen", False):
        resource_root = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        resource_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(resource_root, relative_path)


def get_log_path(base_dir: str | None = None) -> str:
    """Return the absolute path to the debug log file.

    Args:
        base_dir: Override data directory; defaults to :func:`get_base_dir`.

    Returns:
        Absolute path to ``rpa_debug_log.txt``.
    """
    return os.path.join(base_dir or get_base_dir(), "rpa_debug_log.txt")
