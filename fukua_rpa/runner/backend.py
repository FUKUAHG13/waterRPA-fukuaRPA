"""Stable Runner session interface used by the Qt run controller.

The in-process Worker remains the default; the isolated process is an explicit
experimental choice and is never a silent fallback target. See ADR 0026-0029.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fukua_rpa.worker import WorkerThread

from .isolated import IsolatedRunnerThread

if TYPE_CHECKING:
    from fukua_rpa.engine import RPAEngine


RUNNER_MODE_IN_PROCESS = "in_process"
RUNNER_MODE_ISOLATED = "isolated_process"


class RunnerBackendError(RuntimeError):
    """Raised when a Runner backend cannot be created or is misconfigured."""


class InProcessRunnerSession:
    """Runner session that executes tasks on a WorkerThread in the main process.

    This is the default backend. The engine and worker share the same address
    space, so control operations (stop, pause, step) are direct method calls.
    """

    mode: str = RUNNER_MODE_IN_PROCESS

    def __init__(
        self,
        engine: RPAEngine,
        tasks: list[dict[str, Any]],
        log_context: dict[str, Any] | None = None,
    ) -> None:
        """Reserve a run slot and create the in-process worker thread.

        Args:
            engine: The shared RPA engine instance.
            tasks: Mutable task list to execute.
            log_context: Explicit attribution for this run's log records.

        Raises:
            RunnerBackendError: If the engine is already busy.
        """
        self.engine = engine
        self.run_id = engine.reserve_run()
        if self.run_id is None:
            raise RunnerBackendError("执行引擎正忙")
        # FR-RISK-20261005-301: 槽位在 WorkerThread **之前**就已预留。若构造失败
        # （QThread 创建被拒、引擎断言失败等），异常向上抛给调用方，而预留的槽位
        # 无人释放——引擎会永久保持"正忙"，后续所有启动都被 "执行引擎正忙" 拒绝，
        # 且不留下任何线索。构造失败必须与启动失败同等对待：释放槽位并标记
        # ``start_failed``，再把原始异常抛给调用方。
        try:
            self.worker = WorkerThread(engine, tasks, self.run_id, log_context)
        except BaseException as error:
            self.engine.finish_run(self.run_id, "start_failed", str(error))
            raise

    @property
    def is_active(self) -> bool:
        """Return True if the engine or worker thread is still running."""
        return self.engine.is_running or self.worker.isRunning()

    def stop(self) -> Any:
        """Request a cooperative stop on the engine."""
        return self.engine.stop()

    def pause_at_next_step(self) -> Any:
        """Request the debug session to pause before the next step."""
        return self.engine.debug_session.request_pause()

    def continue_run(self) -> Any:
        """Resume execution after a debug pause."""
        return self.engine.debug_session.continue_run()

    def step_over(self) -> Any:
        """Execute a single step then pause again."""
        return self.engine.debug_session.step_over()

    def start_failed(self, error: BaseException) -> None:
        """Mark the reserved run as failed due to a startup error.

        Args:
            error: The exception that prevented the run from starting.
        """
        self.engine.finish_run(self.run_id, "start_failed", str(error))


class IsolatedRunnerSession:
    """Runner session that delegates execution to a separate child process.

    The child process is managed by IsolatedRunnerThread which handles IPC,
    heartbeats, and forced-stop escalation. Construction failures propagate
    to the caller; there is no silent fallback to in-process execution.
    """

    mode: str = RUNNER_MODE_ISOLATED

    def __init__(
        self,
        engine: RPAEngine,
        tasks: list[dict[str, Any]],
        config: dict[str, Any] | None,
        *,
        base_dir: str | None,
        profile_name: str,
        logging_options: dict[str, Any] | None,
        log_context: dict[str, Any] | None,
    ) -> None:
        """Create an isolated Runner session backed by a child process.

        Args:
            engine: The shared RPA engine (used for scale-memory export).
            tasks: Task list to send to the child process.
            config: Run configuration dict; must not be None.
            base_dir: Working directory for the child process.
            profile_name: Human-readable profile label.
            logging_options: Logging configuration forwarded to the child.
            log_context: Explicit attribution forwarded with processed events.

        Raises:
            RunnerBackendError: If config is missing or not a dict.
        """
        if not isinstance(config, dict):
            raise RunnerBackendError("独立 Runner 缺少运行配置")
        self.engine = engine
        self.worker = IsolatedRunnerThread(
            tasks,
            config,
            base_dir=base_dir,
            profile_name=profile_name,
            logging_options=logging_options,
            log_context=log_context,
            scale_memory_state=engine.scale_memory_store.export_state(),
        )

    @property
    def is_active(self) -> bool:
        """Return True if the child-process worker thread is running."""
        return self.worker.isRunning()

    def stop(self) -> bool:
        """Request a cooperative stop forwarded to the child process."""
        return self.worker.request_stop()

    def pause_at_next_step(self) -> bool:
        """Request the child to pause before its next step."""
        return self.worker.request_pause()

    def continue_run(self) -> bool:
        """Tell the child to resume after a pause."""
        return self.worker.continue_run()

    def step_over(self) -> bool:
        """Tell the child to execute one step then pause."""
        return self.worker.step_over()

    def start_failed(self, _error: BaseException) -> None:
        """Request stop on the child when startup fails locally."""
        self.worker.request_stop()


def create_runner_session(
    mode: str | None,
    engine: RPAEngine,
    tasks: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None = None,
    base_dir: str | None = None,
    profile_name: str = "",
    logging_options: dict[str, Any] | None = None,
    log_context: dict[str, Any] | None = None,
) -> InProcessRunnerSession | IsolatedRunnerSession:
    """Create a Runner session for the requested backend mode.

    Args:
        mode: Backend identifier; defaults to in-process when None or empty.
        engine: The shared RPA engine instance.
        tasks: Task list to execute.
        config: Run configuration (required for isolated mode).
        base_dir: Working directory for the isolated child.
        profile_name: Human-readable profile label.
        logging_options: Logging configuration for the isolated child.
        log_context: Explicit run-scoped logging attribution.

    Returns:
        A session object whose interface is uniform across backends.

    Raises:
        RunnerBackendError: If mode is unrecognized or isolated config is
            missing.
    """
    normalized = str(mode or RUNNER_MODE_IN_PROCESS)
    if normalized not in (RUNNER_MODE_IN_PROCESS, RUNNER_MODE_ISOLATED):
        raise RunnerBackendError(f"未知 Runner 后端：{normalized}")
    if normalized == RUNNER_MODE_ISOLATED:
        # DESIGN: construction failures propagate to the UI. Silently choosing
        # the in-process backend would ignore the user's explicit selection and
        # make the displayed execution backend untrustworthy.
        return IsolatedRunnerSession(
            engine,
            tasks,
            config,
            base_dir=base_dir,
            profile_name=profile_name,
            logging_options=logging_options,
            log_context=log_context,
        )
    return InProcessRunnerSession(engine, tasks, log_context)
