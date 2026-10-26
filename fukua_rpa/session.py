"""Small crash marker used to distinguish clean and interrupted shutdowns."""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)

import contextlib  # noqa: E402

from fukua_rpa.constants import BUILD_NAME  # noqa: E402

from .config_store import atomic_write_json  # noqa: E402
from .json_safety import load_bounded_json_file  # noqa: E402

ERROR_ALREADY_EXISTS = 183
SESSION_FORMAT = "fukuaRPA_session"
SESSION_VERSION = 1
MAX_SESSION_MARKER_BYTES = 64 * 1024


class SingleInstanceGuard:
    """Per-portable-directory named mutex held for the lifetime of the process.

    The mutex name is derived from a SHA-256 digest of the normalized base
    directory, so each portable installation gets an independent lock.
    """

    def __init__(self, base_dir: str) -> None:
        """Initialize the guard for the given portable directory.

        Args:
            base_dir: Root directory of the portable installation.
        """
        normalized = os.path.normcase(os.path.abspath(base_dir))
        digest = hashlib.sha256(normalized.encode("utf-8", errors="ignore")).hexdigest()[:20]
        self.name: str = f"Local\\FUKUA_fukuaRPA_{digest}"
        self.handle: int | None = None

    def acquire(self) -> bool:
        """Attempt to create and own the named mutex.

        Returns:
            True if the mutex was successfully acquired, False if another
            instance already holds it or creation failed.
        """
        if self.handle:
            return True
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, True, self.name)
        error = int(kernel32.GetLastError())
        if not handle:
            return False
        if error == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        """Release and close the mutex handle if currently held."""
        if not self.handle:
            return
        try:
            ctypes.windll.kernel32.ReleaseMutex(self.handle)
        finally:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> SingleInstanceGuard:
        if not self.acquire():
            raise RuntimeError("fukuaRPA 已在当前目录运行")
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: object,
    ) -> None:
        self.release()


_kernel32: ctypes.WinDLL | None = None
_kernel32_lock = threading.Lock()


def _get_kernel32() -> ctypes.WinDLL:
    """Lazily initialize a shared kernel32 WinDLL instance (module-level cache)."""
    global _kernel32
    if _kernel32 is None:
        with _kernel32_lock:
            if _kernel32 is None:
                import ctypes
                import ctypes.wintypes

                dll = ctypes.WinDLL("kernel32", use_last_error=True)
                dll.OpenProcess.restype = ctypes.wintypes.HANDLE
                dll.OpenProcess.argtypes = [
                    ctypes.wintypes.DWORD,
                    ctypes.wintypes.BOOL,
                    ctypes.wintypes.DWORD,
                ]
                _kernel32 = dll
    return _kernel32


def _pid_is_running(pid: int) -> bool:
    """Check whether a process with the given PID is currently alive.

    Prefers psutil when available; falls back to OpenProcess with
    ERROR_ACCESS_DENIED treated as "process exists".

    Args:
        pid: Process identifier to probe.

    Returns:
        True if the process appears to be running, False otherwise.
    """
    if pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:
        try:
            kernel32 = _get_kernel32()
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            # ERROR_ACCESS_DENIED (5) means process exists but we lack permission
            if ctypes.get_last_error() == 5:
                return True
        except Exception as exc:
            logger.warning("检查进程存在性失败: %s", exc, exc_info=True)
    return False


@dataclass
class ApplicationSession:
    """Crash marker that distinguishes clean exits from interrupted shutdowns.

    On start, writes a JSON marker containing the current PID and a unique
    token.  On close, removes the marker only if the token still matches.
    A subsequent launch can inspect the leftover marker to detect an unclean
    shutdown of the same build.
    """

    base_dir: str
    marker_name: str = ".fukuaRPA_session.json"
    STALE_MARKER_SECONDS = 7200  # 2 hours

    def __post_init__(self) -> None:
        self.base_dir = os.path.abspath(self.base_dir)
        self.path: str = os.path.join(self.base_dir, self.marker_name)
        self.token: str = uuid.uuid4().hex
        self.previous_unclean: bool = False
        self.previous_details: dict[str, object] = {}
        self.started: bool = False

    def start(self) -> ApplicationSession:
        """Write the session marker and detect a previous unclean shutdown.

        Inspects any leftover marker from a prior run.  If the previous PID is
        dead, the marker is recent, and the build name matches, the session is
        flagged as previously unclean.

        Returns:
            This session instance for chaining.
        """
        previous = self._read_marker()
        if previous:
            try:
                previous_pid = int(previous.get("pid", 0) or 0)  # pyright: ignore[reportArgumentType]
            except (TypeError, ValueError, OverflowError):
                previous_pid = 0
            # A dead PID from a previous test build can look like an unclean
            # exit and mislead the user on first launch.  Treat markers older
            # than STALE_MARKER_SECONDS as stale — probably from a separate
            # test run rather than a crash of the current session.
            try:
                marker_time = time.mktime(
                    time.strptime(
                        str(previous.get("started_at", "")),
                        "%Y-%m-%d %H:%M:%S",
                    )
                )
                stale = (time.time() - marker_time) > self.STALE_MARKER_SECONDS
            except (ValueError, OSError, OverflowError):
                stale = False
            self.previous_unclean = bool(
                previous.get("_marker_error")
                or previous_pid <= 0
                or (
                    previous_pid != os.getpid()
                    and not _pid_is_running(previous_pid)
                    and not stale
                    # A marker from a different build is just a previous
                    # test run — don't flag it as an unclean shutdown.
                    and previous.get("build_name") == BUILD_NAME
                )
            )
            self.previous_details = previous
        atomic_write_json(
            self.path,
            {
                "format": SESSION_FORMAT,
                "version": SESSION_VERSION,
                "pid": os.getpid(),
                "token": self.token,
                "build_name": BUILD_NAME,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        self.started = True
        return self

    def _read_marker(self) -> dict[str, object]:
        """Read and validate the session marker file.

        Returns:
            Parsed marker dict, an error dict with a _marker_error key, or
            an empty dict when the file does not exist.
        """
        try:
            if os.path.getsize(self.path) > MAX_SESSION_MARKER_BYTES:
                return {"_marker_error": "会话标记文件过大"}
            value = load_bounded_json_file(
                self.path,
                max_bytes=MAX_SESSION_MARKER_BYTES,
                label="会话标记",
                max_depth=4,
                max_nodes=32,
            )
            if not isinstance(value, dict):
                return {"_marker_error": "会话标记根结构无效"}
            marker_format = value.get("format")
            if marker_format not in (None, SESSION_FORMAT):
                return {"_marker_error": "会话标记格式不受支持"}
            try:
                version = int(value.get("version", 1))
            except (TypeError, ValueError, OverflowError):
                return {"_marker_error": "会话标记版本无效"}
            if version != SESSION_VERSION:
                return {"_marker_error": "会话标记版本不受支持"}
            return value
        except (ValueError, TypeError) as error:
            return {"_marker_error": f"会话标记损坏：{error}"}
        except OSError:
            return {}

    def close(self) -> bool:
        """Remove the session marker if it still belongs to this session.

        Returns:
            True if the marker was removed (or never written), False if the
            marker token no longer matches (another session overwrote it).
        """
        if not self.started:
            return True
        current = self._read_marker()
        if current.get("token") != self.token:
            return False
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.path)
        self.started = False
        return True

    def __enter__(self) -> ApplicationSession:
        return self.start()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: object,
    ) -> None:
        self.close()
