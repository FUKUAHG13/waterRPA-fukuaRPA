# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Own the QThread bridge while keeping visual state in the main window."""

from __future__ import annotations

import os
import weakref

from PySide6.QtCore import QObject, Signal

from ...logging_service import build_run_log_context
from ...params import RunConfig
from ...runner.backend import RUNNER_MODE_ISOLATED, create_runner_session


class RunController(QObject):
    completed = Signal()
    failed = Signal(str)
    # Design §4.9 / §7: fired on every run end with metadata so the scheduled
    # run coordinator can distinguish its own runs from manual ones. Empty meta
    # (manual run) is ignored by listeners that only care about scheduled runs.
    run_ended = Signal(object)
    runner_info_changed = Signal(dict)
    runner_health_changed = Signal(dict)

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.session = None
        self.worker = None
        self.backend_mode = "in_process"
        self.runner_info = {}
        self.runner_health = {}

    @property
    def is_active(self):
        return bool(self.session and self.session.is_active)

    def resource_process_target(self):
        """Return the process that currently carries the automation workload."""
        worker = self.worker
        if (
            self.backend_mode == RUNNER_MODE_ISOLATED
            and worker is not None
            and worker.isRunning()
        ):
            return getattr(worker, "process_id", None), "独立 Runner"
        return os.getpid(), "本程序"

    def start(
        self,
        tasks,
        log_callback,
        status_callback,
        click_callback,
        debug_callback=None,
        prompt_callback=None,
        *,
        backend_mode="in_process",
        config=None,
        base_dir=None,
        profile_name="",
        logging_options=None,
        run_meta=None,
    ):
        if self.is_active:
            return False, "已有脚本正在运行"
        # DESIGN: 把 start 的离散运行参数收敛为 RunConfig 聚合对象，便于后续
        # （P5 MVC 拆分）把构造/校验/转发逻辑整体迁移；当前仅用它统一向后传递
        # 给 create_runner_session，对外签名不变。
        run_config = RunConfig(
            tasks=tasks,
            log_callback=log_callback,
            status_callback=status_callback,
            click_callback=click_callback,
            debug_callback=debug_callback,
            prompt_callback=prompt_callback,
            backend_mode=backend_mode,
            config=config,
            base_dir=base_dir,
            profile_name=profile_name,
            logging_options=logging_options,
            run_meta=run_meta,
        )
        self._run_config = run_config
        session = None
        self._run_meta = dict(run_meta) if run_meta else {}
        self._run_failed = False
        self._runner_outcome = None
        try:
            session = create_runner_session(
                run_config.backend_mode,
                self.engine,
                run_config.tasks,
                config=run_config.config,
                base_dir=run_config.base_dir,
                profile_name=run_config.profile_name,
                logging_options=run_config.logging_options,
                log_context=build_run_log_context(self._run_meta),
            )
            worker = session.worker
            if hasattr(worker, "runtime_state_signal"):
                worker.runtime_state_signal.connect(
                    lambda data, current=worker: self._apply_runtime_state(
                        current, data
                    )
                )
            if hasattr(worker, "runner_info_signal"):
                worker.runner_info_signal.connect(
                    lambda data, current=worker: self._apply_runner_info(
                        current, data
                    )
                )
            if hasattr(worker, "health_signal"):
                worker.health_signal.connect(
                    lambda data, current=worker: self._apply_runner_health(
                        current, data
                    )
                )
            self.backend_mode = session.mode
            self.runner_info = {}
            self.runner_health = {}
            worker.log_signal.connect(log_callback)
            worker.status_signal.connect(status_callback)
            worker.click_signal.connect(click_callback)
            if debug_callback is not None:
                worker.debug_signal.connect(debug_callback)
            if prompt_callback is not None:
                worker.prompt_signal.connect(prompt_callback)
            worker.error_signal.connect(self._on_worker_error)
            worker_ref = weakref.ref(worker)
            worker.finished.connect(lambda: self._on_finished(worker_ref()))
            self.session = session
            self.worker = worker
            worker.start()
            return True, ""
        except Exception as error:
            if session is not None:
                session.start_failed(error)
            self.session = None
            self.worker = None
            # DESIGN: preserve the selected backend and report startup failure;
            # never make an implicit fallback look like the requested Runner.
            return False, str(error)

    def stop(self):
        return bool(self.session and self.session.stop())

    def pause_at_next_step(self):
        return bool(self.session and self.session.pause_at_next_step())

    def continue_run(self):
        return bool(self.session and self.session.continue_run())

    def step_over(self):
        return bool(self.session and self.session.step_over())

    def _apply_runtime_state(self, worker, payload):
        if self.worker is not worker or not isinstance(payload, dict):
            return
        scale_memory = payload.get("scale_memory")
        if scale_memory:
            self.engine.scale_memory_store.import_state(scale_memory)
        performance_report = payload.get("performance_report")
        if isinstance(performance_report, dict):
            self.engine.last_performance_report = performance_report
        run_trace = payload.get("run_trace")
        if isinstance(run_trace, dict):
            self.engine.last_run_trace = run_trace
        runner_runtime = payload.get("runner")
        if isinstance(runner_runtime, dict):
            updated = dict(self.runner_info)
            updated["runtime"] = dict(runner_runtime)
            self.runner_info = updated
            self.runner_info_changed.emit(dict(updated))

    def _apply_runner_info(self, worker, payload):
        if self.worker is not worker or not isinstance(payload, dict):
            return
        self.runner_info = dict(payload)
        self.runner_info_changed.emit(dict(self.runner_info))

    def _apply_runner_health(self, worker, payload):
        if self.worker is not worker or not isinstance(payload, dict):
            return
        self.runner_health = dict(payload)
        # Capture the authoritative terminal outcome reported by the Isolated
        # Runner (finished/user_stopped/forced_stop/startup_failed/crashed) so
        # _on_finished can propagate it instead of the binary success/failed
        # heuristic (ARCHITECTURE.md #73).
        outcome = payload.get("outcome")
        if outcome:
            self._runner_outcome = str(outcome)
        self.runner_health_changed.emit(dict(self.runner_health))

    def diagnostic_snapshot(self):
        return {
            "mode": str(self.backend_mode),
            "active": bool(self.is_active),
            "info": dict(self.runner_info),
            "health": dict(self.runner_health),
        }

    def _on_worker_error(self, message):
        self._run_failed = True
        self.failed.emit(message)

    def _on_finished(self, worker):
        if worker is None or self.worker is not worker:
            return
        self.completed.emit()
        meta = dict(getattr(self, "_run_meta", {}))
        # Prefer the authoritative terminal outcome reported by the Isolated
        # Runner via health_signal (finished/user_stopped/forced_stop/
        # startup_failed/crashed). Only fall back to the binary success/failed
        # heuristic when the runner did not report an explicit outcome.
        runner_outcome = getattr(self, "_runner_outcome", None)
        if runner_outcome:
            meta["outcome"] = runner_outcome
        else:
            meta["outcome"] = "failed" if getattr(self, "_run_failed", False) else "success"
        # INVARIANT: capture the finished worker BEFORE emitting run_ended. The
        # emit may synchronously start a new run (scheduled-task drain) which
        # replaces self.worker with a new worker; capturing afterwards would
        # capture that new worker and invert the cleanup guard below. Capturing
        # the truly-finished worker first means the guard
        # (self.worker is not finished_worker) correctly skips cleanup and
        # preserves the new worker reference for emergency stop.
        finished_worker = self.worker
        self.run_ended.emit(meta)
        # Delay cleanup to next event loop iteration to allow diagnostic access.
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, lambda: self._release_session_refs(finished_worker))

    def _release_session_refs(self, finished_worker):
        # A new run may have started synchronously via run_ended → coordinator
        # → _drain_pending → start().  In that case self.worker already points
        # to the new worker; clearing it would break emergency stop.
        if self.worker is not finished_worker:
            return
        self.session = None
        self.worker = None
