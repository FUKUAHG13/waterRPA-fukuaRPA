"""Independent crash reporting that survives hard native crashes.

The regular logging service relies on ``sys.excepthook``, which only runs for
unhandled *Python* exceptions. A hard crash inside a native layer (C++ DLL,
Qt/shiboken, Win32 access violation) terminates the process immediately, so the
excepthook never fires and no log is produced.

This module adds a separate, lightweight watchdog **process**. The main
application launches the watchdog at startup; the watchdog simply waits for the
parent process to terminate and then inspects a small session-state file:

* if the parent wrote a clean-exit marker, the watchdog exits quietly;
* otherwise the parent died without cleaning up, so the watchdog writes a
  privacy-safe crash report describing the last known lifecycle breadcrumbs.

Every public helper here is defensive: nothing may raise into the caller, and
nothing may block the main UI thread. Privacy follows the same boundary as
diagnostics and logs — reports contain only structural information (stage
names, action-type names, exit code, versions); never credential values,
workflow text, variable values, or full local paths.
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import struct
import subprocess
import sys
import tempfile
import threading
import time
from io import TextIOWrapper

from .constants import APP_VERSION
from .io_utils import atomic_write_json
from .json_safety import loads_bounded_json
from .privacy import plain_log_text, redact_sensitive_paths

logger = logging.getLogger(__name__)


# --- Tunables ---------------------------------------------------------------

SESSION_STATE_FORMAT = "fukuaRPA_crash_session"
SESSION_STATE_VERSION = 1
CRASH_REPORT_DIR_NAME = "crash_reports"
SESSION_STATE_NAME = "session_state.json"
FAULT_HANDLER_LOG_NAME = "fault_handler.log"
WATCHDOG_ENV_FLAG = "FUKUA_CRASH_WATCHDOG"
WATCHDOG_ARG = "--crash-watchdog"

# Breadcrumbs are bounded so the state file stays tiny and writes stay fast.
MAX_BREADCRUMBS = 50
_MAX_STATE_BYTES = 1024 * 1024
_MAX_STAGE_CHARS = 64
_MAX_DETAIL_CHARS = 120

# After the parent exits, give the clean-exit marker a moment to flush to disk
# before deciding whether the shutdown was clean.
_CLEAN_EXIT_SETTLE_SECONDS = 2.5

# Maximum time to poll when OpenProcess returns ACCESS_DENIED before giving up.
_ACCESS_DENIED_MAX_WAIT_S = 30.0

# Windows process access rights used to wait on the parent process.
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_INFINITE = 0xFFFFFFFF

# subprocess creation flags: detached + no console window. The watchdog must
# not inherit or attach to the parent's console/stdio.
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000

_STATE_LOCK = threading.Lock()
# keep the faulthandler target alive for process life
_FAULT_HANDLER_FILE: TextIOWrapper | None = None


# --- Path helpers -----------------------------------------------------------


def report_dir_for(base_dir: str) -> str:
    """Return the crash-report directory for a runtime base directory.

    Args:
        base_dir: Runtime base directory (e.g. the user data directory).

    Returns:
        Absolute path of the ``crash_reports`` subdirectory under *base_dir*;
        the directory itself is not created here.
    """

    return os.path.join(os.path.abspath(base_dir), CRASH_REPORT_DIR_NAME)


def state_path_for(base_dir: str) -> str:
    """Return the session-state file path for a runtime base directory.

    Args:
        base_dir: Runtime base directory (e.g. the user data directory).

    Returns:
        Absolute path of ``session_state.json`` inside the crash-report
        directory; neither the file nor its directory is created here.
    """

    return os.path.join(report_dir_for(base_dir), SESSION_STATE_NAME)


# --- Small time / text helpers ----------------------------------------------


# retained (P7-1): no in-repo callers; intentional small time/text helper.
def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _epoch_to_text(epoch) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(epoch)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "未知"


def _safe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _bounded_token(value) -> str:
    """Bound a structural stage/action-type name (no user values expected).

    FR-20260809-D6-④: 纵深防御——崩溃报告始终受隐私保护，即使调用方把路径/
    用户名放进 stage 字段也不得原样落盘；脱敏失败返回占位符（fail-closed，
    与 _safe_detail 一致）。
    FR-20260811-D17-001（隐私域裁定）：脱敏为 path-scoped（redact_sensitive_paths），
    纯坐标/数值（如 "at 120,34"）不拦截——当前生产调用点仅传结构性阶段名
    （fukuaRPA.py 五处），无坐标/秘密值进入路径。扩展坐标/数值脱敏会误伤普通
    工作流文本（比例/时长等），收益低于误伤风险，故仅保持契约约束：调用方必须
    只传结构性名称，不得把用户值/坐标放入 stage/detail。
    """

    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    if len(text) > _MAX_STAGE_CHARS:
        text = text[:_MAX_STAGE_CHARS]
    try:
        return redact_sensitive_paths(text)
    except Exception:
        return "[stage unavailable]"


def _safe_detail(value) -> str:
    """Bound and privacy-redact an optional breadcrumb detail string."""

    text = str(value or "")
    if not text:
        return ""
    try:
        return plain_log_text(text, max_chars=_MAX_DETAIL_CHARS, redact_paths=True)
    except Exception as exc:
        # Redaction failed; never leak unredacted text — use a placeholder.
        logger.warning("面包屑详情脱敏失败: %s", exc, exc_info=True)
        return "[detail unavailable]"


def _exit_code_text(exit_code) -> str:
    if exit_code is None:
        return "未知（父进程无法打开或可能已退出）"
    try:
        code = int(exit_code)
    except (TypeError, ValueError, OverflowError):
        return str(exit_code)
    if code < 0:
        return f"{code} (0x{code & 0xFFFFFFFF:08X})"
    if code > 0x7FFFFFFF:
        return f"{code} (0x{code:08X})"
    return str(code)


def _windows_version_text() -> str:
    try:
        version = sys.getwindowsversion()
        return f"{version.major}.{version.minor} build {version.build}"
    except Exception as exc:
        logger.warning("获取 Windows 版本失败: %s", exc, exc_info=True)
        return "未知"


def _arch_text() -> str:
    try:
        bits = struct.calcsize("P") * 8
        return f"{bits}-bit {platform.machine()}"
    except Exception as exc:
        logger.warning("获取系统架构失败: %s", exc, exc_info=True)
        return "未知"


# --- State file IO (atomic, bounded, defensive) -----------------------------


def _read_state(path) -> dict | None:
    try:
        if not path or not os.path.isfile(path):
            return None
        if os.path.getsize(path) > _MAX_STATE_BYTES:
            return None
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_STATE_BYTES + 1)
        value = loads_bounded_json(
            raw, max_bytes=_MAX_STATE_BYTES, label="会话状态"
        )
        return value if isinstance(value, dict) else None
    except Exception as exc:
        logger.warning("会话状态文件读取失败: %s", exc, exc_info=True)
        return None


def _write_state(path, state) -> None:
    # 原子写入（mkstemp → write → fsync → os.replace），与项目约定一致。
    # indent=None 保持与原 json.dump 紧凑输出一致。
    atomic_write_json(path, state, ensure_ascii=False, indent=None)


def _fresh_state() -> dict:
    now = time.time()
    return {
        "format": SESSION_STATE_FORMAT,
        "version": SESSION_STATE_VERSION,
        "pid": os.getpid(),
        "started_at": _epoch_to_text(now),
        "started_epoch": now,
        "clean_exit": False,
        "breadcrumbs": [],
        "last_stage": "launch",
        "last_heartbeat": _epoch_to_text(now),
        "last_heartbeat_epoch": now,
    }


# --- Public API -------------------------------------------------------------


def install_fault_handler(base_dir: str) -> bool:
    """Route fatal Python faults (segfault etc.) to an independent log file.

    Failures are silent — this must never affect startup.

    Args:
        base_dir: Runtime base directory; the ``crash_reports`` subdirectory
            is created if needed and receives ``fault_handler.log``.

    Returns:
        ``True`` when ``faulthandler`` was successfully redirected, ``False``
        on any failure (logged as a warning, never raised).
    """

    global _FAULT_HANDLER_FILE
    try:
        import faulthandler

        # Close the previous handle to avoid leaking it on repeated calls.
        if _FAULT_HANDLER_FILE is not None:
            with contextlib.suppress(OSError):
                _FAULT_HANDLER_FILE.close()
            _FAULT_HANDLER_FILE = None

        directory = report_dir_for(base_dir)
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, FAULT_HANDLER_LOG_NAME)
        handle = open(target, "a", encoding="utf-8")  # noqa: SIM115
        # FR-20260812-D17-001: faulthandler 硬崩溃时经文件描述符直写原始回溯，
        # 绕过 Python 层 write()，Python 包装无法拦截。因此这里保持直写，
        # 由看门狗进程在父进程退出后对 fault_handler.log 统一脱敏
        # （见 _redact_fault_handler_log）。
        faulthandler.enable(file=handle)
        _FAULT_HANDLER_FILE = handle
        return True
    except Exception as exc:
        logger.warning("故障处理器安装失败: %s", exc, exc_info=True)
        return False


def record_breadcrumb(base_dir: str, stage: str, detail: str = "") -> None:
    """Append a bounded, privacy-safe lifecycle breadcrumb.

    Never raises and never blocks the caller for any meaningful time. Only
    structural information (stage / action-type names) should be passed; any
    detail text is bounded and path-redacted defensively.

    Args:
        base_dir: Runtime base directory holding the session-state file.
        stage: Short structural stage or action-type name; bounded to
            ``_MAX_STAGE_CHARS`` and stripped of newlines.
        detail: Optional free-form detail; bounded and path-redacted before
            being persisted. Defaults to no detail.
    """

    try:
        path = state_path_for(base_dir)
        with _STATE_LOCK:
            state = _read_state(path)
            if not isinstance(state, dict):
                state = _fresh_state()
            crumbs = state.get("breadcrumbs")
            if not isinstance(crumbs, list):
                crumbs = []
            now = time.time()
            crumbs.append(
                {
                    "stage": _bounded_token(stage),
                    "detail": _safe_detail(detail),
                    "at": _epoch_to_text(now),
                    "epoch": now,
                }
            )
            if len(crumbs) > MAX_BREADCRUMBS:
                crumbs = crumbs[-MAX_BREADCRUMBS:]
            state["breadcrumbs"] = crumbs
            state["last_stage"] = _bounded_token(stage)
            state["last_heartbeat"] = _epoch_to_text(now)
            state["last_heartbeat_epoch"] = now
            _write_state(path, state)
    except Exception as exc:
        logger.warning("面包屑记录失败: %s", exc, exc_info=True)


def mark_clean_exit(base_dir: str) -> None:
    """Write the clean-exit marker so the watchdog skips crash reporting.

    Args:
        base_dir: Runtime base directory holding the session-state file.
    """

    try:
        path = state_path_for(base_dir)
        with _STATE_LOCK:
            state = _read_state(path)
            if not isinstance(state, dict):
                state = _fresh_state()
            now = time.time()
            state["clean_exit"] = True
            state["clean_exit_at"] = _epoch_to_text(now)
            state["clean_exit_epoch"] = now
            _write_state(path, state)
    except Exception as exc:
        logger.warning("正常退出标记写入失败: %s", exc, exc_info=True)
        # FR-20260811-D17-1: 标记写入失败（磁盘满/权限）时尽力记录失败状态位，
        # 供看门狗区分"正常退出但标记失败"与"确实崩溃"，避免误报崩溃报告。
        # 若第二次写入也失败（磁盘完全不可写），保持原行为（看门狗按无标记处理）。
        try:
            with _STATE_LOCK:
                state = _read_state(path)
                if not isinstance(state, dict):
                    state = _fresh_state()
                state["clean_exit_failed"] = True
                _write_state(path, state)
        except Exception:
            pass


def launch_watchdog(base_dir: str, argv: list[str] | None = None) -> bool:
    """Spawn the detached watchdog process. Returns True on success.

    Any failure is swallowed and reported as False — launching the watchdog must
    never break the normal startup path.

    Args:
        base_dir: Runtime base directory; the crash-report directory is created
            and a fresh session-state file is written before the spawn.
        argv: Optional override for the parent command line used to derive the
            watchdog launch command; defaults to ``sys.argv``.

    Returns:
        ``True`` when the watchdog subprocess was spawned, ``False`` on any
        failure or when already running inside a watchdog (recursion guard).
    """

    try:
        if os.environ.get(WATCHDOG_ENV_FLAG) == "1":
            # We are already running inside a watchdog; never recurse.
            return False
        base = os.path.abspath(base_dir)
        directory = report_dir_for(base)
        os.makedirs(directory, exist_ok=True)
        state_file = os.path.join(directory, SESSION_STATE_NAME)
        with _STATE_LOCK:
            _write_state(state_file, _fresh_state())

        source_argv = list(argv) if argv else list(sys.argv)
        script = source_argv[0] if source_argv else sys.argv[0]
        # FR-20260811-D17-2: __main__.__file__ 兜底——python -c / -m / IDE 启动时
        # sys.argv[0] 可能不是可执行脚本路径（"-c"、空、模块名），直接拼进看门狗
        # 启动命令会导致看门狗进程启动失败、崩溃报告缺失。
        # 存在性判定用 abspath：Popen 以 cwd=base 启动，argv[0] 若为相对路径且
        # 在当前 cwd 可解析，必须绝对化后才不会在 base 下解析失败。
        if (
            not script
            or script in ("-c", "-m")
            or not os.path.exists(os.path.abspath(script))
        ):
            main_file = getattr(sys.modules.get("__main__"), "__file__", None)
            if main_file:
                script = main_file
        elif os.path.isabs(script):
            pass
        else:
            script = os.path.abspath(script)
        watchdog_args = [
            WATCHDOG_ARG,
            str(os.getpid()),
            state_file,
            directory,
        ]
        if getattr(sys, "frozen", False):
            command = [sys.executable, *watchdog_args]
        else:
            command = [sys.executable, script, *watchdog_args]

        env = dict(os.environ)
        env[WATCHDOG_ENV_FLAG] = "1"

        devnull = subprocess.DEVNULL
        subprocess.Popen(
            command,
            creationflags=_DETACHED_PROCESS | _CREATE_NO_WINDOW,
            close_fds=True,
            stdin=devnull,
            stdout=devnull,
            stderr=devnull,
            env=env,
            cwd=base,
        )
        return True
    except Exception as exc:
        logger.warning("看门狗进程启动失败: %s", exc, exc_info=True)
        return False


# --- Crash report formatting (pure, testable) -------------------------------


def _should_write_report(exit_code, state, parent_pid=None) -> bool:
    """Decide whether a crash report is warranted.

    A clean-exit marker means the parent shut down normally → no report. Any
    other outcome (missing marker, unreadable state, unknown exit code) is
    treated as a crash so we never silently lose a hard failure.
    """
    if isinstance(state, dict):
        # SAFETY: 状态文件位于固定路径，会被下一次启动的 _fresh_state 覆写。
        # 若读到的会话 pid 与本看门狗监视的父进程不一致，其 clean_exit 与
        # 面包屑都不属于本会话，跳过报告以免为已退出的旧会话误写崩溃报告
        # （审查 SEC-FR-003）；旧格式状态缺少 pid 字段时保持原判定语义。
        if (
            parent_pid is not None
            and state.get("pid") is not None
            and state.get("pid") != parent_pid
        ):
            return False
        if state.get("clean_exit"):
            return False
        # FR-20260811-D17-1: 父进程正常退出但标记写入失败（clean_exit_failed
        # 由 mark_clean_exit 尽力写入）→ 仅当退出码为 0 时不误报崩溃；非零
        # 退出码仍按崩溃处理，避免掩盖真实崩溃。
        if state.get("clean_exit_failed"):
            return exit_code != 0
    return True


def _format_crash_report(exit_code, state, now=None) -> str:
    """Build the privacy-safe plain-text crash report."""

    now = time.time() if now is None else float(now)
    lines = [
        "fukuaRPA 崩溃报告",
        "=" * 40,
        f"应用版本：{APP_VERSION}",
        f"崩溃时间：{_epoch_to_text(now)}",
        f"父进程退出码：{_exit_code_text(exit_code)}",
    ]

    started_epoch = _safe_float(state.get("started_epoch")) if isinstance(state, dict) else None
    if started_epoch is not None:
        duration = max(0.0, now - started_epoch)
        lines.append(f"运行时长：{duration:.1f} 秒")
    else:
        lines.append("运行时长：未知")

    lines.append(f"Windows 版本：{_windows_version_text()}")
    lines.append(f"架构：{_arch_text()}")
    lines.append(f"Python 版本：{sys.version.split()[0]}")
    lines.append(f"冻结包：{bool(getattr(sys, 'frozen', False))}")
    lines.append("")
    lines.append("崩溃前面包屑（仅阶段/动作类型，不含具体值）：")

    if isinstance(state, dict):
        lines.append(f"  最后阶段：{state.get('last_stage') or '未知'}")
        lines.append(f"  末次心跳：{state.get('last_heartbeat') or '未知'}")
        crumbs = state.get("breadcrumbs")
        if isinstance(crumbs, list) and crumbs:
            for crumb in crumbs[-MAX_BREADCRUMBS:]:
                if not isinstance(crumb, dict):
                    continue
                stage = crumb.get("stage", "")
                detail = crumb.get("detail", "")
                at = crumb.get("at", "")
                suffix = f"（{detail}）" if detail else ""
                lines.append(f"  - [{at}] {stage}{suffix}")
        else:
            lines.append("  （无面包屑）")
    else:
        lines.append("  （状态文件不可用）")

    lines.append("")
    lines.append("隐私说明：本报告不含凭据、工作流文本、变量值或完整本机路径。")
    return "\n".join(lines) + "\n"


def _write_crash_report(report_dir, exit_code, state, now=None) -> str | None:
    try:
        os.makedirs(report_dir, exist_ok=True)
        text = _format_crash_report(exit_code, state, now=now)
        stamp = time.strftime(
            "%Y%m%d_%H%M%S",
            time.localtime(now if now is not None else time.time()),
        )
        target = os.path.join(report_dir, f"crash_{stamp}.log")
        descriptor, temp_path = tempfile.mkstemp(
            prefix=".crash_report.", suffix=".tmp", dir=report_dir
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        finally:
            if os.path.exists(temp_path):
                with contextlib.suppress(OSError):
                    os.remove(temp_path)
        return target
    except Exception as exc:
        logger.warning("崩溃报告写入失败: %s", exc, exc_info=True)
        return None


# --- Watchdog process entry -------------------------------------------------


def _redact_fault_handler_log(report_dir: str) -> bool:
    """Redact full local paths from a leftover ``fault_handler.log`` in place.

    FR-20260812-D17-001: faulthandler writes raw tracebacks through the file
    descriptor on a hard crash, bypassing Python-level ``write`` wrappers, so
    the redaction cannot happen in the crashing process. The watchdog runs
    *after* the parent died, so it is the correct place to sanitize the file.
    The file may be large (bounded reads only), and failures are silent —
    redaction must never block report writing.

    Args:
        report_dir: Crash-report directory that should contain
            ``fault_handler.log``.

    Returns:
        ``True`` when the file existed and was rewritten, ``False`` otherwise
        (including "no file" and "rewrite failed").
    """

    try:
        path = os.path.join(report_dir, FAULT_HANDLER_LOG_NAME)
        if not os.path.isfile(path):
            return False
        stat = os.stat(path)
        if stat.st_size <= 0:
            return False
        # Read only a bounded head; a huge accumulated log is truncated rather
        # than fully redacted (the tail of repeated crashes has low value).
        with open(path, encoding="utf-8", errors="replace") as handle:
            raw = handle.read(_MAX_STATE_BYTES)
        redacted = plain_log_text(raw, max_chars=_MAX_STATE_BYTES)
        if redacted == raw:
            return False
        descriptor, temp_path = tempfile.mkstemp(
            prefix=".fault_log.", suffix=".tmp", dir=report_dir
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as out:
                out.write(redacted)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                with contextlib.suppress(OSError):
                    os.remove(temp_path)
        return True
    except Exception as exc:
        logger.warning("fault_handler.log 脱敏失败: %s", exc, exc_info=True)
        return False


def _get_process_creation_time(k32, handle, ctypes_mod) -> int | None:
    """Return the 64-bit FILETIME of process creation, or None on failure."""

    try:
        creation = ctypes_mod.c_uint64(0)
        exit_ft = ctypes_mod.c_uint64(0)
        kernel_ft = ctypes_mod.c_uint64(0)
        user_ft = ctypes_mod.c_uint64(0)
        if k32.GetProcessTimes(
            handle,
            ctypes_mod.byref(creation),
            ctypes_mod.byref(exit_ft),
            ctypes_mod.byref(kernel_ft),
            ctypes_mod.byref(user_ft),
        ):
            return creation.value
    except Exception as exc:
        logger.warning("获取进程创建时间失败: %s", exc, exc_info=True)
    return None


# Polling interval for the PID-reuse verification loop (milliseconds, as
# required by WaitForSingleObject).
_PID_REUSE_CHECK_MS = 2000


def _wait_for_parent(parent_pid, kernel32=None, state_file=None) -> int | None:
    """Block until the parent exits; return its exit code or None if unknown.

    Design note on PID reuse: the window between parent exit and this function's
    OpenProcess call is extremely small (the watchdog is spawned at startup and
    calls OpenProcess immediately). As an additional safeguard, we record the
    process creation time after opening and verify it periodically in the wait
    loop; if the PID was recycled the creation time will differ. The ultimate
    fallback is the clean_exit marker in the session-state file — even if we
    waited on a wrong process, _should_write_report checks the marker before
    writing any crash report.
    """

    try:
        import ctypes

        # FR-20260812-D17-002: 必须以 use_last_error=True 创建 WinDLL，否则
        # ctypes.get_last_error() 恒返回 0（ctypes 只在 use_last_error 下保存
        # 错误码），:err==5 的 ERROR_ACCESS_DENIED 保护分支真实环境不可达。
        # 与 session.py:_get_kernel32 的范本保持一致。
        k32 = (
            kernel32
            if kernel32 is not None
            else ctypes.WinDLL("kernel32", use_last_error=True)
        )
        try:
            # Configure signatures so 64-bit HANDLEs are not truncated to c_int
            # (mirrors win32_api._safe_configure). Best-effort: injected fake
            # kernel32 objects in tests may not support attribute assignment.
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            k32.WaitForSingleObject.restype = ctypes.c_ulong
            k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            k32.GetExitCodeProcess.restype = ctypes.c_int
            k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            k32.CloseHandle.restype = ctypes.c_int
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            k32.GetProcessTimes.restype = ctypes.c_int
            k32.GetProcessTimes.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
        except Exception as exc:
            logger.warning("Win32 API 签名配置失败: %s", exc, exc_info=True)
        # FR-20260812-D17-002: 调用前显式清零错误码，避免误读上一次调用的
        # 残留错误（session.py:106 同一模式；Python 3.13 为 set_last_error）。
        ctypes.set_last_error(0)
        handle = k32.OpenProcess(
            _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            int(parent_pid),
        )
        if not handle:
            # Distinguish "access denied" (process exists but we lack rights)
            # from "process not found" (already exited).
            try:
                err = ctypes.get_last_error()
            except Exception as exc:
                logger.warning("获取最后错误码失败: %s", exc, exc_info=True)
                err = 0
            if err == 5:  # ERROR_ACCESS_DENIED
                # Process exists but cannot be opened yet (e.g. security
                # software holding an exclusive lock).  Poll for up to
                # _ACCESS_DENIED_MAX_WAIT_S seconds: if the clean_exit marker
                # appears the parent shut down normally; if the process
                # becomes openable we fall into the normal wait path.
                deadline = time.monotonic() + _ACCESS_DENIED_MAX_WAIT_S
                while time.monotonic() < deadline:
                    time.sleep(_CLEAN_EXIT_SETTLE_SECONDS)
                    # Check whether the parent wrote its clean-exit marker.
                    if state_file:
                        st = _read_state(state_file)
                        if isinstance(st, dict) and st.get("clean_exit"):
                            return None
                    # Retry opening the process.
                    handle = k32.OpenProcess(
                        _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
                        False,
                        int(parent_pid),
                    )
                    if handle:
                        break  # Fall into the normal wait loop below.
                if not handle:
                    # FR-20261012-096: 轮询超时只说明在窗口内拿不到句柄，并不
                    # 证明父进程已消亡——直接返回 None 会让调用方把“退出码未知”
                    # 当成崩溃，在父进程仍存活时误写报告。超时后再探测一次：
                    # 能打开就回到正常等待路径，仍打不开才维持“宁可误报不漏报”。
                    handle = k32.OpenProcess(
                        _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION,
                        False,
                        int(parent_pid),
                    )
                    if not handle:
                        # Timed out: parent neither exited cleanly nor became
                        # accessible — a crash report is justified.
                        return None
            else:
                # Parent already gone; exit code unavailable.
                return None
        try:
            # Record creation time to detect PID reuse during the wait.
            original_creation = _get_process_creation_time(k32, handle, ctypes)
            while True:
                wait_result = k32.WaitForSingleObject(handle, _PID_REUSE_CHECK_MS)
                if wait_result == 0:  # WAIT_OBJECT_0 — process exited
                    exit_code = ctypes.c_ulong(0)
                    if k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        return int(exit_code.value)
                    return None
                # Timeout — verify the PID still refers to the same process.
                if original_creation is not None:
                    current_creation = _get_process_creation_time(k32, handle, ctypes)
                    if (
                        current_creation is not None
                        and current_creation != original_creation
                    ):
                        # PID was recycled; the original parent is gone.
                        return None
                # Otherwise keep waiting.
        finally:
            try:
                k32.CloseHandle(handle)
            except Exception as exc:
                logger.warning("进程句柄关闭失败: %s", exc, exc_info=True)
    except Exception as exc:
        logger.warning("等待父进程退出失败: %s", exc, exc_info=True)
        return None


def _watchdog_run(
    argv,
    *,
    sleep_func=time.sleep,
    wait_func=None,
    writer=_write_crash_report,
) -> int:
    args = list(argv or [])
    if WATCHDOG_ARG not in args:
        return 2
    index = args.index(WATCHDOG_ARG)
    rest = args[index + 1 :]
    if len(rest) < 3:
        return 2
    try:
        parent_pid = int(rest[0])
    except (TypeError, ValueError, OverflowError):
        return 2
    state_file = rest[1]
    report_dir = rest[2]

    try:
        if wait_func is not None:
            exit_code = wait_func(parent_pid)
        else:
            exit_code = _wait_for_parent(parent_pid, state_file=state_file)
    except Exception as exc:
        logger.warning("看门狗等待父进程异常: %s", exc, exc_info=True)
        exit_code = None

    try:
        sleep_func(_CLEAN_EXIT_SETTLE_SECONDS)
    except Exception as exc:
        logger.warning("看门狗等待延迟被中断: %s", exc, exc_info=True)

    state = _read_state(state_file)
    try:
        if _should_write_report(exit_code, state, parent_pid=parent_pid):
            writer(report_dir, exit_code, state)
        # FR-20260813-D20-001: 无论是否写新报告，干净退出路径也必须脱敏历史
        # fault_handler.log（脱敏幂等：文件缺失/未变时返回 False 且不写盘，不会误伤）。
        # 此前早退 return 0 会跳过脱敏，使上一次崩溃遗留的明文路径残留在盘。
        _redact_fault_handler_log(report_dir)
    except Exception as exc:
        logger.warning("看门狗崩溃报告写入/脱敏失败: %s", exc, exc_info=True)
    return 0


def watchdog_main(argv: list[str] | None) -> int:
    """Watchdog subprocess entry point. Never raises; returns a process code.

    Args:
        argv: Command line of the watchdog process; must contain the
            ``--crash-watchdog`` marker followed by parent PID, state-file path
            and report directory.

    Returns:
        Process exit code: ``0`` on normal completion (including after writing
        a crash report), ``2`` when the watchdog arguments are missing/invalid.
    """

    try:
        return _watchdog_run(list(argv or []))
    except Exception as exc:
        logger.warning("看门狗主循环异常: %s", exc, exc_info=True)
        return 0
