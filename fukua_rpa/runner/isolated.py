"""Qt bridge for the optional out-of-process automation Runner.

Each run gets one child process. Failure is reported and cleaned up, never
replayed automatically, because completed side effects cannot be inferred from
a broken IPC channel. See ADR 0026-0029.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Listener
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from fukua_rpa.logging_service import ProcessedLogEvent, relay_processed_log_event
from fukua_rpa.timing_constants import POLL_INTERVAL_S

from .ipc import (
    RUNNER_IPC_FAMILY,
    create_runner_pipe_name,
    validate_runner_pipe_name,
)
from .job import RunnerJobGuard
from .protocol import (
    RUNNER_HEARTBEAT_STALE_SECONDS,
    RUNNER_READY_TIMEOUT_SECONDS,
    RunnerConnection,
    RunnerProtocolError,
    normalize_runner_runtime_summary,
    validate_runner_hello,
)

RUNNER_CONNECT_TIMEOUT_SECONDS = 20.0
RUNNER_FORCE_STOP_SECONDS = 1.5
# FR-20260809-0206: 子进程已自报 finished 后的次级收尾宽限。正常收尾（日志落盘/
# 输入清理）通常 <1s；超过此宽限仍不退出则视为收尾挂起，恢复强杀以保证急停有界。
RUNNER_FINISHED_GRACE_SECONDS = 5.0
# DEC-20260930-002-02: 子进程已自报终局后，收尾阶段等待其退出的探测窗口。
# 超窗才强制终止，但**不得**因此把一次已完成的运行改判为 forced_stop。
RUNNER_TERMINATE_PROBE_SECONDS = 1.5

# FR-701-002 / ARCHITECTURE #73：终局只允许这五类确定结果。子进程经 finished 通道
# 自报的 outcome 可能是引擎内部终局（stopped / preload_failed），须先映射到 #73 五分类
# 再判定；未知/非法值（含非白名单的 "error"）归一为 crashed（异常终止），避免计划日志/
# 诊断出现契约之外枚举。引擎内部终局 -> #73 映射（见 DOC-RUNNER-001 / DOC-ENGINE-001）：
#   stopped        -> user_stopped  （引擎自主停止/步骤"停止运行"，属正常停止，非崩溃）
#   preload_failed -> startup_failed（模板/资源预载失败，属启动期失败）
# 若维护者决定把 "error" 或上述内部终局正式纳入契约，须同步 #73 与协议。
RUNNER_OUTCOME_ALLOWED = frozenset(
    {"finished", "user_stopped", "forced_stop", "startup_failed", "crashed"}
)

# 引擎内部终局 -> ARCHITECTURE #73 五分类。仅做语义对齐，不引入新枚举。
# 该映射对应 FR-RUNNER-002 的修复：preload_failed / stopped 经 finished 通道到达时
# 不应被错误归一为 crashed，也不应触发"通信中断"误报。
_ENGINE_OUTCOME_TO_TERMINAL = {
    "stopped": "user_stopped",
    "preload_failed": "startup_failed",
}


def _normalize_runner_outcome(outcome: str) -> str:
    """Map any outcome string onto the five-class set of ARCHITECTURE #73.

    先对齐引擎内部终局（stopped / preload_failed）到 #73 五分类，再对仍不在
    白名单的值归一为 crashed。
    """
    outcome = _ENGINE_OUTCOME_TO_TERMINAL.get(outcome, outcome)
    return outcome if outcome in RUNNER_OUTCOME_ALLOWED else "crashed"


class RemoteScriptPromptRequest:
    """UI-facing prompt object whose answer is relayed to the child process.

    Created on the QThread when a prompt message arrives from the Runner
    child. The UI emits acknowledge/cancel, which sends the response back
    over IPC. close_from_runner is used during shutdown without sending.
    """

    def __init__(
        self,
        prompt_id: str,
        text: str,
        response_callback: object,
    ) -> None:
        self.prompt_id = str(prompt_id)
        self.text = str(text)
        self.done = threading.Event()
        self.acknowledged: bool = False
        self.cancelled: bool = False
        self._response_callback = response_callback
        self._lock = threading.Lock()

    def acknowledge(self) -> None:
        """Accept the prompt and relay the positive response to the child."""
        self._finish(True, send=True)

    def cancel(self) -> None:
        """Reject the prompt and relay the negative response to the child."""
        self._finish(False, send=True)

    def close_from_runner(self) -> None:
        """Mark the prompt as cancelled without sending (Runner is gone)."""
        self._finish(False, send=False)

    def _finish(self, accepted: bool, *, send: bool) -> None:
        """Resolve the prompt exactly once, optionally notifying the child."""
        with self._lock:
            if self.done.is_set():
                return
            self.acknowledged = bool(accepted)
            self.cancelled = not bool(accepted)
            self.done.set()
        if send:
            self._response_callback(self.prompt_id, bool(accepted))  # type: ignore[operator]


class IsolatedRunnerThread(QThread):
    """QThread that manages the full lifecycle of an out-of-process Runner.

    Spawns a child process, performs the IPC handshake, forwards control
    commands (stop/pause/step), relays events (log/status/click/debug) to
    Qt signals, monitors heartbeats, and escalates to forced kill if the
    child does not exit within the stop deadline.
    """

    log_signal = Signal(str)
    status_signal = Signal(dict)
    click_signal = Signal(dict)
    debug_signal = Signal(dict)
    prompt_signal = Signal(object)
    runtime_state_signal = Signal(dict)
    runner_info_signal = Signal(dict)
    health_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(
        self,
        tasks: list[dict[str, object]],
        config: dict[str, object],
        *,
        base_dir: str | None,
        profile_name: str,
        logging_options: dict[str, object] | None,
        log_context: dict[str, object] | None = None,
        scale_memory_state: dict[str, object] | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.tasks = list(tasks)
        self.config = dict(config)
        self.config.pop("tasks", None)
        self.base_dir = os.path.abspath(str(base_dir or os.getcwd()))
        self.profile_name = str(profile_name or "当前方案")
        self.logging_options = dict(logging_options or {})
        self.log_context = dict(log_context or {})
        self.scale_memory_state = dict(scale_memory_state or {})
        self._channel = None
        self._channel_lock = threading.Lock()
        self._process = None
        self._stop_requested = threading.Event()
        # _stop_deadline is written by the GUI thread (request_stop) and read by
        # QThread (run loop / _accept_connection). Guarded by _stop_deadline_lock
        # so correctness no longer assumes the GIL.
        self._stop_deadline = None
        self._stop_deadline_lock = threading.RLock()
        self._received_finished = False
        self._received_started = False
        self._received_error = False
        self._child_outcome = ""
        self._child_error = ""
        self._local_error_reported = False
        self._forced_stop = False
        # FR-20260930-015: 父进程是否**亲眼观察到**子进程自行退出。用于把
        # 「崩溃后被收尾」与「父进程主动终止仍存活的子进程」区分开：前者必须
        # 保持 crashed，后者才是 forced_stop。见 _terminate_live_process。
        self._observed_child_exit = False
        self._hello_info = {}
        self.runner_info = {}
        self._last_heartbeat_at = None
        self._health_unresponsive = False
        self._tracked_buttons = []
        self._tracked_keys = []
        # _prompts dict: written by QThread (_handle_prompt), popped by UI thread
        # (_respond_to_prompt via signal), cleared in QThread's finally
        # (_close_prompts). All access is guarded by _prompts_lock so correctness
        # no longer assumes the GIL; _close_prompts pops atomically under the lock.
        self._prompts = {}
        self._prompts_lock = threading.RLock()
        self._job_guard = None
        self.last_exit_report = {}

    @property
    def process_id(self):
        """Return the live child PID without exposing the Popen object."""
        process = self._process
        if process is None:
            return None
        try:
            if process.poll() is not None:
                return None
            reported_pid = self.runner_info.get("pid") or self._hello_info.get("pid")
            return int(reported_pid or process.pid)  # pyright: ignore[reportArgumentType]
        except (OSError, TypeError, ValueError):
            return None

    def request_stop(self) -> bool:
        """Request a cooperative stop with a forced-kill deadline.

        Returns:
            True if the stop was sent; False if the thread is not running.
        """
        if not self.isRunning():
            return False
        self._stop_requested.set()
        with self._stop_deadline_lock:
            if self._stop_deadline is None:
                self._stop_deadline = time.monotonic() + RUNNER_FORCE_STOP_SECONDS
        self._send("stop")
        return True

    def request_pause(self) -> bool:
        """Send a pause command to the child Runner."""
        return self._send("pause")

    def continue_run(self) -> bool:
        """Send a continue command to resume the child after a pause."""
        return self._send("continue")

    def step_over(self) -> bool:
        """Send a step-over command to the child Runner."""
        return self._send("step_over")

    def _respond_to_prompt(self, prompt_id: str, accepted: bool) -> None:
        """Relay a prompt response from the UI to the child process."""
        with self._prompts_lock:
            self._prompts.pop(str(prompt_id), None)
        self._send(
            "prompt_response",
            {"id": str(prompt_id), "accepted": bool(accepted)},
        )

    def _send(self, kind: str, payload: dict[str, object] | None = None) -> bool:
        """Send a command to the child, returning False if the channel is gone."""
        with self._channel_lock:
            channel = self._channel
            if channel is None:
                return False
            try:
                channel.send(kind, payload)
                return True
            except (EOFError, OSError, RunnerProtocolError):
                return False

    def _runner_command(self, pipe_name: object) -> list[str]:
        """Build the subprocess command line for the Runner child."""
        # SAFETY: auth key is passed via FUKUARPA_RUNNER_AUTH env var, not
        # command-line args, so it is not visible to other users' processes.
        args = [
            "--runner-child",
            "--runner-pipe",
            validate_runner_pipe_name(pipe_name),
        ]
        if getattr(sys, "frozen", False):
            return [sys.executable, *args]
        entry_point = Path(__file__).resolve().parents[2] / "fukuaRPA.py"
        return [sys.executable, str(entry_point), *args]

    def _launch_process(self, listener: Listener, auth_hex: str) -> subprocess.Popen[bytes]:
        """Spawn the Runner child process and attach it to a Job Object guard.

        Raises:
            RuntimeError: If the Job Object cannot be created or attached.
        """
        command = self._runner_command(listener.address)
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        # SAFETY: auth key travels via env var (same-user visibility only),
        # not command-line args (visible to all users via process listing).
        environment["FUKUARPA_RUNNER_AUTH"] = str(auth_hex)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            cwd=self.base_dir,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        guard = RunnerJobGuard()
        if not guard.create() or not guard.attach(process):
            guard_error = guard.error or "独立 Runner 无法建立父进程退出保护"
            guard.close()
            try:
                process.terminate()
                process.wait(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            raise RuntimeError(guard_error)
        self._job_guard = guard
        return process

    def _accept_connection(
        self, listener: Listener, process: subprocess.Popen[bytes]
    ) -> object:
        """Wait for the child to connect, with stop-deadline and exit checks.

        Raises:
            RuntimeError: If the child exits early or the connection fails.
            TimeoutError: If the child does not connect within 20 seconds.
        """
        result = queue.Queue(maxsize=1)

        def accept():
            try:
                result.put((True, listener.accept()))
            except Exception as error:
                result.put((False, error))

        thread = threading.Thread(
            target=accept,
            daemon=True,
            name="fukuaRPA-runner-accept",
        )
        thread.start()
        try:
            deadline = time.monotonic() + RUNNER_CONNECT_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                with self._stop_deadline_lock:
                    stop_deadline = self._stop_deadline
                if stop_deadline is not None and time.monotonic() >= stop_deadline:
                    self._force_stop_process(reason="startup")
                    raise RuntimeError("独立 Runner 在启动期间被停止")
                try:
                    ok, value = result.get(timeout=POLL_INTERVAL_S)
                except queue.Empty:
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"独立 Runner 启动后提前退出（代码 {process.returncode}）"
                        ) from None
                    continue
                if not ok:
                    raise RuntimeError(f"独立 Runner 连接失败：{value}") from value
                return value
            raise TimeoutError("独立 Runner 在 20 秒内未完成连接")
        finally:
            # FR-20260809-0208: 有界等待 accept 线程。成功路径线程已结束立即返回；
            # 超时/被停止/提前退出路径线程仍阻塞在 listener.accept()（daemon 线程
            # 不阻止进程退出，listener.close() 不保证唤醒），有界 join 避免遗留
            # 阻塞线程累积到进程生命周期结束。
            thread.join(timeout=0.5)

    _MESSAGE_HANDLERS: dict[str, str] = {
        "started": "_handle_started",
        "heartbeat": "_handle_heartbeat",
        "log": "_handle_log",
        "status": "_handle_status",
        "click": "_handle_click",
        "debug": "_handle_debug",
        "prompt": "_handle_prompt",
        "input_state": "_handle_input_state",
        "runtime_state": "_handle_runtime_state",
        "error": "_handle_error",
        "finished": "_handle_finished",
    }

    def _handle_message(self, message: dict) -> None:
        kind = message["kind"]
        payload = message["payload"]
        handler_name = self._MESSAGE_HANDLERS.get(kind)
        if handler_name is None:
            raise RunnerProtocolError(f"独立 Runner 发送了未知消息：{kind}")
        handler = getattr(self, handler_name)
        handler(payload)

    def _handle_started(self, payload: dict) -> None:
        pid = self._message_pid(payload, "started")
        try:
            run_id = int(payload.get("run_id"))  # pyright: ignore[reportArgumentType]
        except (TypeError, ValueError) as error:
            raise RunnerProtocolError("独立 Runner run_id 无效") from error
        runtime = normalize_runner_runtime_summary(payload.get("runtime"))
        if run_id <= 0 or runtime["pid"] != pid:
            raise RunnerProtocolError("独立 Runner 启动摘要无效")
        self._received_started = True
        self.runner_info = {
            "pid": pid,
            "run_id": run_id,
            "capabilities": list(
                self._hello_info.get("capabilities", ())  # pyright: ignore[reportArgumentType]
            ),
            "runtime": dict(runtime),
        }
        self.runner_info_signal.emit(dict(self.runner_info))

    def _handle_log(self, payload: dict) -> None:
        text = str(payload.get("text", ""))
        if payload.get("processed") is True:
            relay_processed_log_event(
                ProcessedLogEvent(
                    message=text,
                    timestamp=str(payload.get("timestamp") or ""),
                    event_time_ns=payload.get("event_time_ns") or 0,
                    context=(
                        dict(payload.get("context"))
                        if isinstance(payload.get("context"), dict)
                        else {}
                    ),
                    structured=(
                        dict(payload.get("structured"))
                        if isinstance(payload.get("structured"), dict)
                        else {}
                    ),
                )
            )
            if not bool(self.logging_options.get("log_to_ui", True)):
                return
        self.log_signal.emit(text)

    def _handle_status(self, payload: dict) -> None:
        data = payload.get("data")
        self.status_signal.emit(dict(data) if isinstance(data, dict) else {})

    def _handle_click(self, payload: dict) -> None:
        data = payload.get("data")
        self.click_signal.emit(dict(data) if isinstance(data, dict) else {})

    def _handle_debug(self, payload: dict) -> None:
        data = payload.get("data")
        self.debug_signal.emit(dict(data) if isinstance(data, dict) else {})

    def _handle_prompt(self, payload: dict) -> None:
        prompt_id = str(payload.get("id", ""))
        if prompt_id:
            request = RemoteScriptPromptRequest(
                prompt_id,
                payload.get("text", ""),
                self._respond_to_prompt,
            )
            with self._prompts_lock:
                self._prompts[prompt_id] = request
            self.prompt_signal.emit(request)

    def _handle_input_state(self, payload: dict) -> None:
        data = payload.get("data")
        if isinstance(data, dict):
            self._tracked_buttons = [
                str(item) for item in data.get("mouse_buttons", [])
            ]
            self._tracked_keys = [str(item) for item in data.get("keys", [])]

    def _handle_runtime_state(self, payload: dict) -> None:
        runtime_state = dict(payload)
        if "runner" in runtime_state:
            runtime_state["runner"] = normalize_runner_runtime_summary(
                runtime_state["runner"]
            )
        self.runtime_state_signal.emit(runtime_state)

    def _handle_error(self, payload: dict) -> None:
        self._received_error = True
        self.error_signal.emit(str(payload.get("message") or "独立 Runner 异常"))

    def _handle_finished(self, payload: dict) -> None:
        self._received_finished = True
        # FR-701-002: 子进程自报 outcome 必须落在 #73 五分类；未知/非法值归一为 crashed。
        self._child_outcome = _normalize_runner_outcome(
            str(payload.get("outcome") or "finished")
        )
        self._child_error = str(payload.get("error") or "")

    def _emit_local_error(self, message):
        if self._local_error_reported:
            return
        self._local_error_reported = True
        self.error_signal.emit(str(message))

    def _message_pid(self, payload, label):
        raw_pid = payload.get("pid") if hasattr(payload, "get") else None
        if isinstance(raw_pid, bool):
            raise RunnerProtocolError(f"独立 Runner {label} PID 无效")
        try:
            pid = int(raw_pid)  # pyright: ignore[reportArgumentType]
        except (AttributeError, TypeError, ValueError) as error:
            raise RunnerProtocolError(f"独立 Runner {label} PID 无效") from error
        process = self._process
        expected = int(process.pid) if process is not None else 0
        if pid <= 0:
            raise RunnerProtocolError(f"独立 Runner {label} PID 无效")
        if expected and pid != expected:
            raw_parent_pid = payload.get("ppid")
            if isinstance(raw_parent_pid, bool):
                raise RunnerProtocolError(f"独立 Runner {label} 父 PID 无效")
            try:
                parent_pid = int(raw_parent_pid)
            except (TypeError, ValueError):
                parent_pid = 0
            if parent_pid != expected:
                raise RunnerProtocolError(
                    f"独立 Runner {label} PID 不匹配：收到 {pid}，需要 {expected}"
                )
        return pid

    def _handle_heartbeat(self, payload: dict[str, object]) -> None:
        """Process a heartbeat message, updating health state and emitting signals."""
        pid = self._message_pid(payload, "heartbeat")
        try:
            sequence = int(payload.get("sequence"))  # pyright: ignore[reportArgumentType]
        except (TypeError, ValueError) as error:
            raise RunnerProtocolError("独立 Runner 心跳序号无效") from error
        phase = str(payload.get("phase") or "unknown")[:48]
        if sequence <= 0:
            raise RunnerProtocolError("独立 Runner 心跳序号无效")
        recovered = self._health_unresponsive
        self._health_unresponsive = False
        self._last_heartbeat_at = time.monotonic()
        health = {
            "state": "healthy",
            "pid": pid,
            "phase": phase,
            "sequence": sequence,
            "engine_running": bool(payload.get("engine_running")),
            "stop_requested": bool(payload.get("stop_requested")),
        }
        self.health_signal.emit(health)
        if recovered:
            self.log_signal.emit("独立 Runner 心跳已恢复。")

    def _check_heartbeat_health(self, process: subprocess.Popen[bytes]) -> None:
        """Emit an unresponsive health signal if the heartbeat is stale."""
        last = self._last_heartbeat_at
        if last is None or process.poll() is not None:
            return
        age = time.monotonic() - last
        if age < RUNNER_HEARTBEAT_STALE_SECONDS or self._health_unresponsive:
            return
        self._health_unresponsive = True
        health = {
            "state": "unresponsive",
            "pid": int(process.pid),
            "age_seconds": round(age, 2),
        }
        self.health_signal.emit(health)
        self.log_signal.emit(
            "<font color='orange'><b>独立 Runner 心跳暂时中断；"
            "任务不会被自动终止，可使用急停结束。</b></font>"
        )

    def _force_stop_process(self, reason: str = "stop_timeout") -> None:
        """Kill the live child now and release tracked input state."""
        self._terminate_live_process(self._process, reason=reason)

    def _terminate_live_process(self, process, reason: str = "stop_timeout") -> None:
        """Boundedly terminate a leftover child and release tracked input.

        DEC-20260930-002-02（维护者 2026-10-06 裁定）：子进程**已经自报终局**
        （``_received_finished``）时，收尾阶段超过探测窗口才强制终止，不得因此
        把一次已完成的运行归类为 ``forced_stop``——``_build_exit_report`` 把
        ``forced_stop`` 放在阶梯首位，误置会把「完成」显示成「强制停止」。

        ``_forced_stop`` 因此只保留给两种情形：用户确实请求了停止之外的强杀，或
        子进程**从未**自报过终局就被杀掉。真实急停（``_stop_requested``）与真实
        崩溃的分类不受本裁定影响。

        有界终止本身不变：悬挂的子进程仍会被杀掉、其记录的输入仍会被释放，所以
        本裁定不可能留下悬挂进程或残留输入。
        """
        # FR-20260930-015（DESIGN）：置位判定必须在**探测窗口之后**重新 poll，
        # 不能只看窗口开始前的那一次。外部强杀（含崩溃恢复）会让管道先断开，
        # 而 ``process.poll()`` 在收尾瞬间仍可能返回 ``None``；若据此立即判定，
        # 一个在窗口内自行退出的真实崩溃会被误报成 ``forced_stop``
        #（REVIEW-20260930-002 实施期实测到该回归）。
        if process is not None and process.poll() is None and not self._observed_child_exit:
            deadline = time.monotonic() + RUNNER_TERMINATE_PROBE_SECONDS
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
        # 窗口后仍存活、且没有任何终局证据、也不是用户要求，才是我方主动强杀。
        #   - 已自报终局（DEC-20260930-002-02）→ 用子进程自报结果
        #   - 已观察到子进程退出              → 崩溃路径
        #   - 从未 reported started           → 启动失败路径
        if (
            process is not None
            and process.poll() is None
            and not self._stop_requested.is_set()
            and not self._received_finished
            and not self._observed_child_exit
            and self._received_started
        ):
            self._forced_stop = True
        if process is not None and process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
            deadline = time.monotonic() + RUNNER_TERMINATE_PROBE_SECONDS
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            if process.poll() is None:
                with contextlib.suppress(OSError):
                    process.kill()
        self._release_tracked_inputs()
        if reason == "startup":
            # FR-20261008-905: 启动期急停路径（_accept_connection 的停止检查）
            # 在子进程尚未连接、从未上报任何输入状态时就会走到这里；运行期
            # 文案的"未在急停时限内退出"与"释放其记录的输入状态"两处均与
            # 事实不符。按 #73 的终局措辞精度要求区分启动期与运行期。
            self.log_signal.emit(
                "<font color='red'><b>独立 Runner 在启动期间被急停，"
                "已终止子进程。</b></font>"
            )
        else:
            self.log_signal.emit(
                "<font color='red'><b>独立 Runner 未在急停时限内退出，"
                "已强制结束并释放其记录的输入状态。</b></font>"
            )

    def _build_exit_report(self, process: subprocess.Popen[bytes] | None) -> dict[str, object]:
        """Assemble the final exit report for diagnostics and UI display."""
        return_code = process.poll() if process is not None else None
        if self._forced_stop:
            outcome = "forced_stop"
        elif self._stop_requested.is_set():
            outcome = "user_stopped"
        elif self._received_finished:
            # FR-20261006-002：终局**只**由子进程自报结果决定，不受主进程收尾方式
            # 影响。Windows 上``terminate()`` 走 TerminateProcess，``poll()`` 必
            # 返回非零；把它当作失败会把一次已完成的运行误报为 crashed。非零退出码
            # 在此分支只说明"主进程收尾时终止了它"。
            outcome = self._child_outcome or "finished"
        elif not self._received_started:
            outcome = "startup_failed"
        elif self._received_error and not self._received_finished:
            # FR-701-002: 引擎异常但未收 finished，属于异常终止，归一为 crashed，
            # 不再输出契约之外的 "error"。
            outcome = "crashed"
        else:
            outcome = "crashed"
        outcome = _normalize_runner_outcome(outcome)
        guard = self._job_guard
        return {
            "outcome": outcome,
            "return_code": None if return_code is None else int(return_code),
            "started": bool(self._received_started),
            "finished_message": bool(self._received_finished),
            "error_message": bool(self._received_error),
            "child_error": self._child_error,
            "forced": bool(self._forced_stop),
            "job_guard": guard.snapshot() if guard is not None else {
                "available": False,
                "attached": False,
                "kill_on_parent_exit": False,
                "child_processes_escape": False,
            },
        }

    def _release_tracked_inputs(self) -> None:
        """Release mouse buttons and keys tracked from the child's input state."""
        if not self._tracked_buttons and not self._tracked_keys:
            return
        try:
            from fukua_rpa.pyautogui_runtime import pyautogui

            for key in reversed(self._tracked_keys):
                try:
                    pyautogui.keyUp(key)  # pyright: ignore[reportCallIssue]
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "输入释放失败: key %s", key, exc_info=exc
                    )
            for button in self._tracked_buttons:
                try:
                    pyautogui.mouseUp(button=button)  # pyright: ignore[reportCallIssue]
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "输入释放失败: button %s", button, exc_info=exc
                    )
        finally:
            self._tracked_buttons = []
            self._tracked_keys = []

    def _close_prompts(self) -> None:
        """Cancel all outstanding prompts without sending responses."""
        with self._prompts_lock:
            prompts = list(self._prompts.values())
            self._prompts.clear()
        for request in prompts:
            request.close_from_runner()

    def run(self) -> None:
        """Execute the full Runner lifecycle: spawn, handshake, relay, cleanup.

        This is the QThread entry point. It creates a named-pipe listener,
        launches the child process, performs the protocol handshake, then
        enters the message relay loop until the child exits or is stopped.
        """
        listener = None
        channel = None
        process = None
        try:
            auth = secrets.token_bytes(32)
            pipe_name = create_runner_pipe_name()
            listener = Listener(pipe_name, family=RUNNER_IPC_FAMILY, authkey=auth)
            process = self._launch_process(listener, auth.hex())
            self._process = process
            connection = self._accept_connection(listener, process)
            channel = RunnerConnection(connection)
            with self._channel_lock:
                self._channel = channel
            if not channel.poll(5.0):
                raise TimeoutError("独立 Runner 未发送协议握手")
            hello = channel.receive()
            self._hello_info = validate_runner_hello(
                hello,
                expected_pid=process.pid,
            )
            self._last_heartbeat_at = time.monotonic()
            channel.send(
                "start",
                {
                    "tasks": self.tasks,
                    "config": self.config,
                    "base_dir": self.base_dir,
                    "profile_name": self.profile_name,
                    "logging": self.logging_options,
                    "log_context": self.log_context,
                    "scale_memory": self.scale_memory_state,
                },
            )
            if self._stop_requested.is_set():
                self._send("stop")
            ready_deadline = time.monotonic() + RUNNER_READY_TIMEOUT_SECONDS

            while True:
                with self._stop_deadline_lock:
                    deadline = self._stop_deadline
                # FR-20260809-0206: 已收 finished 的子进程给次级收尾宽限
                # （RUNNER_FINISHED_GRACE_SECONDS），正常收尾不再被急停强杀；
                # 但宽限到期仍不退出则恢复强杀，保证急停路径始终有界。
                force_deadline = (
                    deadline + RUNNER_FINISHED_GRACE_SECONDS
                    if deadline is not None and self._received_finished
                    else deadline
                )
                if (
                    force_deadline is not None
                    and time.monotonic() >= force_deadline
                    and process.poll() is None
                ):
                    self._force_stop_process()
                    break
                if (
                    not self._received_started
                    and time.monotonic() >= ready_deadline
                ):
                    raise TimeoutError(
                        f"独立 Runner 在 {RUNNER_READY_TIMEOUT_SECONDS:.0f} 秒内未准备完成"
                    )
                if channel.poll(POLL_INTERVAL_S):
                    try:
                        self._handle_message(channel.receive())
                    except RunnerProtocolError as exc:
                        # FR-20260809-0207: 记录具体协议错误（诊断），再退出主循环；
                        # 原实现 break 丢弃异常对象，只留下笼统"通信中断"提示。
                        self.log_signal.emit(
                            f"<font color='orange'>独立 Runner 协议错误：{exc}</font>"
                        )
                        break
                    except (EOFError, OSError):
                        break
                self._check_heartbeat_health(process)
                return_code = process.poll()
                if return_code is not None:
                    # 亲眼观察到退出，才能把随后的收尾归为 crashed 而非 forced_stop。
                    self._observed_child_exit = True
                    while channel.poll(0.0):
                        try:
                            self._handle_message(channel.receive())
                        except (EOFError, OSError, RunnerProtocolError):
                            break
                    if (
                        not self._received_finished
                        and not self._received_error
                        and not self._stop_requested.is_set()
                    ):
                        # SAFETY: surface the crash and end the session. Never
                        # restart/replay tasks whose last side effect is unknown.
                        self._emit_local_error(
                            f"独立 Runner 意外退出（代码 {return_code}）"
                        )
                    break
        except Exception as error:
            if not self._stop_requested.is_set():
                self._emit_local_error(str(error))
        finally:
            final_pid = int(process.pid) if process is not None else 0
            self._close_prompts()
            with self._channel_lock:
                self._channel = None
            if channel is not None:
                channel.close()
            if listener is not None:
                with contextlib.suppress(OSError):
                    listener.close()
            if (
                process is not None
                and process.poll() is None
                and self._received_finished
            ):
                # Once the child has sent its authoritative terminal event, the
                # pipe may close a few milliseconds before process teardown
                # finishes. Give its input/log cleanup a bounded grace period;
                # terminating immediately would misreport a successful run as
                # exit code 1 and could cut off its final persisted records.
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    # DEC-20260930-002-02：收尾超时属于需要记录的异常，不得被
                    # contextlib.suppress 静默吞掉；自报终局值一并写出，便于
                    # 事后判断"已完成但未退出"还是"未完成"。
                    self.log_signal.emit(
                        "<font color='orange'>独立 Runner 收尾超时（已自报终局="
                        f"{self._received_finished}），将转入强制终止。</font>"
                    )
                except OSError:
                    pass
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    with contextlib.suppress(OSError):
                        process.kill()
            self._release_tracked_inputs()
            self.last_exit_report = self._build_exit_report(process)
            if (
                self.last_exit_report.get("outcome")
                in {"crashed", "startup_failed"}
                and not self._received_finished
                and not self._received_error
                and not self._stop_requested.is_set()
            ):
                self._emit_local_error(
                    "独立 Runner 通信中断或意外退出"
                    f"（代码 {self.last_exit_report.get('return_code')}）"
                )
            if self.runner_info:
                terminal_info = dict(self.runner_info)
                terminal_info["exit"] = dict(self.last_exit_report)
                self.runner_info_signal.emit(terminal_info)
            guard = self._job_guard
            self._job_guard = None
            if guard is not None:
                guard.close()
            self.health_signal.emit(
                {
                    "state": "stopped",
                    "pid": final_pid,
                    "forced": bool(self._forced_stop),
                    "outcome": self.last_exit_report.get("outcome", "unknown"),
                    "return_code": self.last_exit_report.get("return_code"),
                }
            )
            self._process = None
