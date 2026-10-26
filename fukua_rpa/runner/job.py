"""Windows Job Object guard for parent-death Runner cleanup."""

from __future__ import annotations

import ctypes
from ctypes import wintypes

JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

_kernel32 = None


def _get_kernel32() -> ctypes.WinDLL:
    """Return a cached kernel32 WinDLL with all required signatures configured."""
    global _kernel32
    if _kernel32 is None:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        _kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        _kernel32.SetInformationJobObject.restype = wintypes.BOOL
        _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _kernel32.CloseHandle.restype = wintypes.BOOL
    return _kernel32


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class RunnerJobGuard:
    """Kill only the Runner on parent exit while letting launched apps escape.

    Uses a Windows Job Object with KILL_ON_JOB_CLOSE so the child Runner
    process is terminated if the parent dies unexpectedly, while
    SILENT_BREAKAWAY_OK allows grandchild processes (user-launched apps)
    to leave the job and survive independently.
    """

    def __init__(self) -> None:
        self.handle: int | None = None
        self.available: bool = False
        self.attached: bool = False
        self.error: str = ""

    def create(self) -> bool:
        """Create and configure the Job Object with kill-on-close limits.

        Returns:
            True if the Job Object was created and configured successfully.
        """
        if not hasattr(ctypes, "WinDLL"):
            self.error = "当前平台不支持 Windows Job Object"
            return False
        kernel32 = _get_kernel32()
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            self.error = self._last_error("创建 Runner Job Object 失败")
            return False
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
        )
        if not kernel32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            self.handle = handle
            self.error = self._last_error("配置 Runner Job Object 失败")
            self.close()
            return False
        self.handle = handle
        self.available = True
        self.error = ""
        return True

    def attach(self, process: object) -> bool:
        """Assign a child process to this Job Object.

        Args:
            process: A subprocess.Popen-like object with a ``_handle`` attr.

        Returns:
            True if the process was successfully assigned.
        """
        if not self.available or not self.handle:
            return False
        try:
            # Access CPython-internal _handle to get the Win32 process handle for
            # AssignProcessToJobObject. No public API exists for this. Protected by
            # try/except (AttributeError, TypeError, ValueError) below.
            process_handle = wintypes.HANDLE(int(process._handle))  # type: ignore[attr-defined]
        except (AttributeError, TypeError, ValueError):
            self.error = "无法取得 Runner 进程句柄"
            return False
        kernel32 = _get_kernel32()
        if not kernel32.AssignProcessToJobObject(self.handle, process_handle):
            self.error = self._last_error("Runner 加入 Job Object 失败")
            return False
        self.attached = True
        self.error = ""
        return True

    def snapshot(self) -> dict[str, bool]:
        """Return a serializable summary of the guard state for diagnostics."""
        return {
            "available": bool(self.available),
            "attached": bool(self.attached),
            "kill_on_parent_exit": bool(self.attached),
            "child_processes_escape": bool(self.attached),
        }

    def close(self) -> None:
        """Release the Job Object handle and reset all state."""
        handle = self.handle
        self.handle = None
        self.available = False
        self.attached = False
        if handle:
            kernel32 = _get_kernel32()
            kernel32.CloseHandle(handle)

    @staticmethod
    def _last_error(prefix: str) -> str:
        """Format a Win32 error message with the last-error code if nonzero."""
        code = int(ctypes.get_last_error())
        return f"{prefix}（Windows 错误码 {code}）" if code else prefix

    def __enter__(self) -> RunnerJobGuard:
        """Create the Job Object or raise on failure."""
        if not self.create():
            raise RuntimeError("Failed to create Job Object for Runner guard")
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        """Close the Job Object handle on context exit."""
        self.close()
