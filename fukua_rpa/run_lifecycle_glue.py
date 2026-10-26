# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Run lifecycle, backend bootstrap and run-state glue for RPAEngine.

Docstring 修订（FR-20260812 文档#1）：最初从 engine.py 提取，但该模块后续有
独立演进（如 join_all_launched_applications 等 P3-2 独有逻辑），已不再是
"verbatim" 副本。方法仍通过 ``self`` 相互调用（及调用引擎其余部分），行为
与提取时保持一致。"""

import ctypes
import logging
import time
from collections import OrderedDict

from .auxiliary_clicks import AuxiliaryClickScheduler
from .commands import (
    effective_task_settings,
)
from .constants import (
    MAX_AUXILIARY_CLICK_REQUESTS,
)
from .log_policy import (
    LOG_ACTION,
    LOG_BACKEND,
    LOG_CRITICAL,
    LOG_PARAMETERS,
    LOG_RUN,
    LOG_TIMING,
)
from .log_telemetry import (
    format_complete_payload,
)
from .logging_service import write_log
from .performance import concise_performance_summary
from .privacy import private_error_text, safe_error_name
from .runtime_state import RunState
from .step_runtime import (
    StepDispatchResult,
    StepExecutionPolicy,
)
from .vision.opencv_runtime import configure_opencv_threads

logger = logging.getLogger(__name__)


class RunLifecycleMixin:
    def initialize_backends(self):
        """幂等地初始化执行后端。

        完成 OpenCV 可用性检测、原生核心状态日志和进程优先级提升；
        已初始化时直接返回。

        Returns:
            始终返回 True。
        """
        if self.backends_ready:
            return True
        with self._backend_init_lock:
            if self.backends_ready:
                return True
            self.check_engine_status()
            self._log_native_core_status()
            self.set_high_priority()
            self.backends_ready = True
        return True


    def set_high_priority(self):
        """尽力将当前进程提升为高优先级。

        提升失败（如权限不足）时仅记录日志，进程继续以普通优先级运行。
        """
        try:
            kernel32 = ctypes.windll.kernel32
            # GetCurrentProcess returns a pseudo handle. Unlike OpenProcess,
            # it never needs CloseHandle and cannot leak across engine starts.
            process = kernel32.GetCurrentProcess()
            if process:
                kernel32.SetPriorityClass(process, 0x00000080)
        except Exception as error:
            # Best-effort optimization; a failure (e.g. denied privilege) only
            # means the run uses normal priority, so log it without surfacing.
            write_log(
                f"无法提升进程优先级（将继续以普通优先级运行）：{safe_error_name(error)}",
                         category=LOG_RUN,
                         critical=True,
                     )


    def check_engine_status(self):
        """检测 OpenCV/NumPy 引擎是否可用。

        可用时配置 OpenCV 线程数并置 opencv_available 为 True，
        否则禁用并记录日志。
        """
        try:
            import cv2
            import numpy

            self.opencv_threads = configure_opencv_threads(cv2)
            img = numpy.zeros((10, 10, 3), dtype=numpy.uint8)
            cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
            self.opencv_available = True
            write_log(f"OpenCV/NumPy 引擎就绪（最多 {self.opencv_threads} 线程）。",
                         category=LOG_RUN,
                     )
        except Exception:
            self.opencv_available = False
            self.opencv_threads = 0
            write_log("OpenCV 引擎不可用。",
                         category=LOG_BACKEND,
                         critical=True,
                     )


    def _log_native_core_status(self):
        if self.native_core.available:
            capabilities = self.native_core.capabilities()
            write_log(
                f"Native vision core ready (v{self.native_core.version}, "
                f"capabilities=0x{capabilities.get('mask', 0):X}).",
                         category=LOG_RUN,
                     )
        else:
            write_log("Native vision core unavailable; fallback enabled.",
                         category=LOG_BACKEND,
                         critical=True,
                     )


    def reserve_run(self):
        """预约一次新的运行。

        Returns:
            新运行的唯一 ID；若已有活动运行则返回 None。
        """
        return self.lifecycle.reserve()


    def active_run_matches(self, run_id):
        """判断给定运行 ID 是否为当前活动运行。

        Args:
            run_id: 待校验的运行 ID。

        Returns:
            与当前活动运行匹配返回 True，否则 False。
        """
        return self.lifecycle.matches(run_id)


    def finish_run(self, run_id, outcome="finished", error=""):
        """结束指定运行并清理全局时间上限状态。

        Args:
            run_id: 要结束的运行 ID。
            outcome: 运行结果，如 "finished"、"stopped"、"error"。
            error: 错误描述（仅出错时有意义）。

        Returns:
            成功结束返回 True；运行 ID 不匹配返回 False。
        """
        finished = self.lifecycle.finish(run_id, outcome, error)
        if finished:
            self.global_deadline = None
            self.time_limit_reached = False
            # P3-2: 运行结束回收已退出的启动型子进程，释放 Popen 引用与句柄。
            try:
                from .window_actions import join_all_launched_applications

                join_all_launched_applications(timeout=0.0)
            except Exception as exc:  # noqa: BLE001
                logger.warning("回收启动型子进程失败: %s", exc, exc_info=True)
        return finished


    def stop(self):
        """Request cooperative cancellation and begin best-effort cleanup.

        SAFETY: native, debug and prompt cancellation failures are contained
        individually. Normal stop and run finalization both release inputs
        tracked by this engine.
        """
        stopped = self.lifecycle.request_stop()
        if not stopped:
            return False

        scheduler = getattr(self, "auxiliary_click_scheduler", None)
        if scheduler is not None:
            try:
                scheduler.close(cancel_pending=True, wait=False)
            except Exception as exc:
                # SAFETY: 任何可选后端的异常都不得阻断急停序列的剩余取消与
                # 输入释放（ARCHITECTURE #52）；与下方 native/debug/prompt 同款隔离。
                logger.warning("取消附属点击调度失败: %s", exc, exc_info=True)
        native_core = getattr(self, "native_core", None)
        if native_core is not None:
            try:
                native_core.cancel_active_search()
            except Exception as exc:
                # Emergency stop must continue even when an optional backend is
                # already broken or is being torn down concurrently.
                logger.warning("取消原生搜索失败: %s", exc, exc_info=True)
        try:
            self.debug_session.cancel()
        except Exception as exc:
            logger.warning("取消调试会话失败: %s", exc, exc_info=True)
        with self._prompt_lock:
            prompt = self._active_prompt_request
        if prompt is not None:
            try:
                prompt.cancel()
            except Exception as exc:
                logger.warning("取消提示请求失败: %s", exc, exc_info=True)
        self.release_injected_inputs()
        # P3-2: 急停时也尽力回收已退出的启动型子进程（非阻塞，不误杀仍在运行的）。
        try:
            from .window_actions import join_all_launched_applications

            join_all_launched_applications(timeout=0.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("急停回收启动型子进程失败: %s", exc, exc_info=True)
        return True


    @property
    def is_running(self):
        """当前是否存在活动运行。"""
        return self.lifecycle.is_active


    @property
    def stop_requested(self):
        """是否已收到停止请求。"""
        return self.lifecycle.stop_requested


    @property
    def run_state(self):
        """当前运行生命周期状态。"""
        return self.lifecycle.state


    def close_screenshot_backend(self):
        """关闭并释放截图后端实例（若存在）。"""
        # Intentionally not under _vision_lock: called only at lifecycle
        # boundaries (run start, preload abort, run finalize after auxiliary
        # threads are joined).  If a rare race with capture_screenshot occurs,
        # the local-reference pattern there converts it into a caught fallback.
        instance = self._mss_instance
        self._mss_instance = None
        if instance is not None:
            try:
                instance.close()
            except Exception as exc:
                logger.warning("关闭截图后端失败: %s", exc, exc_info=True)


    def credential_store(self):
        """返回凭据存储，首次调用时惰性创建。

        Returns:
            绑定到基础目录的 CredentialStore 实例。
        """
        store = self._credential_store
        if store is None:
            from .credentials import CredentialStore
            from .paths import get_base_dir

            store = CredentialStore(self.base_dir or get_base_dir())
            self._credential_store = store
        return store


    def emit_debug_event(self, payload):
        """记录调试事件并通知界面回调。

        事件状态为 "paused" 时会附带当前表达式变量快照。

        Args:
            payload: 调试事件字典，包含 state/loop/step/command 等字段。
        """
        event = dict(payload or {})
        self.run_trace.record(
            f"debug_{event.get('state', 'event')}",
            loop=event.get("loop", 0),
            step=event.get("step", 0),
            command=event.get("command", ""),
            status=event.get("reason", ""),
            duration_ms=event.get("paused_ms", 0.0),
        )
        if event.get("state") == "paused":
            event["variables"] = self.expression_debug_snapshot(
                event.get("step", 0), event.get("loop", 0)
            )
        if self.callback_debug:
            try:
                self.callback_debug(event)
            except Exception as exc:
                logger.warning("调试回调失败: %s", exc, exc_info=True)


    def _clear_run_callbacks(self):
        self.callback_msg = None
        self.callback_status = None
        self.callback_click_indicator = None
        self.callback_debug = None
        self.callback_prompt = None


    def _on_auxiliary_click_error(self, error):
        self.performance.increment("action.auxiliary_click_errors")
        if self.log_enabled(LOG_CRITICAL, critical=True):
            self.log(
                "<font color='red'>    [附属点击异常] 执行失败："
                f"{safe_error_name(error)}</font>",
                LOG_CRITICAL,
                critical=True,
                private_msg=(
                    "<font color='red'>    [附属点击异常] 执行失败："
                    f"{private_error_text(error)}</font>"
                ),
            )


    def _reset_run_state(self):
        """Reset per-run caches, counters and flags.

        Shared by ``__init__`` and ``_initialize_run_runtime`` so the two
        cannot drift apart. Run-specific setup (scheduler, callbacks,
        performance gauges, run trace, debug session) stays in
        ``_initialize_run_runtime``.
        """

        self._auxiliary_queue_warning_logged = False
        self._mss_disabled_for_run = False
        self._mss_failure_logged = False
        self._native_capture_disabled_for_run = False
        self._native_capture_failure_logged = False
        self._last_status_payload = None
        self.img_cache = OrderedDict()
        self.base_templates_cache = {}
        self.base_template_masks_cache = {}
        self.prepared_template_images_cache = {}
        self.template_masked_cache_keys = set()
        self.scaled_templates_cache = {}
        self.scaled_template_masks_cache = {}
        self.template_transparency_cache = {}
        self.transparent_template_reported = set()
        self.scale_options_cache = {}
        self.point_click_counts = {}
        self.coord_step_states = {}
        self.coord_sequence_states = {}
        self.step_execution_counts = {}
        self.until_condition_baselines = {}
        self.until_condition_previous = {}
        self.until_condition_counts = {}
        self.until_condition_started_at = {}
        self._until_capture_cache = None
        self.miss_streaks = {}
        self.last_target_positions = {}
        self.scene_probe_cursors = {}
        self.pending_fast_matches = {}
        self.scene_signature_cache = {}
        self.native_rejection_cache = set()
        self.native_failure_streak = 0
        self._native_disabled_for_run = False
        self._native_disable_reason = ""
        self.template_validation_cache = {}
        self.template_validation_reported = set()
        self.scaled_template_cache_bytes = 0
        self.reset_expression_runtime()


    def _initialize_run_runtime(
        self,
        tasks,
        *,
        callback_msg,
        callback_status,
        callback_click_indicator,
        callback_debug,
        callback_prompt,
    ):
        previous_scheduler = getattr(self, "auxiliary_click_scheduler", None)
        if previous_scheduler is not None:
            previous_scheduler.close(cancel_pending=True, wait=True)
        self.auxiliary_click_scheduler = AuxiliaryClickScheduler(
            self.execute_auxiliary_click,
            error_callback=self._on_auxiliary_click_error,
            max_pending=MAX_AUXILIARY_CLICK_REQUESTS,
        )
        self._reset_run_state()
        self.close_screenshot_backend()
        self.performance.reset()
        self.performance.set_gauge("run.task_count", len(tasks))
        self.performance.set_gauge(
            "opencv.threads", int(getattr(self, "opencv_threads", 0))
        )
        self.run_trace.reset(task_count=len(tasks))
        self.run_trace.record("run_started")
        if getattr(self, "native_core", None):
            self.native_core.reset_performance_stats()
        self.callback_msg = callback_msg
        self.callback_status = callback_status
        self.callback_click_indicator = callback_click_indicator
        self.callback_debug = callback_debug
        self.callback_prompt = callback_prompt
        self.debug_session.reset()
        if self.verbose_log_enabled(LOG_PARAMETERS):
            self.log(
                format_complete_payload(
                    "完全/运行参数",
                    self.complete_runtime_parameters(len(tasks)),
                ),
                LOG_PARAMETERS,
                private_msg=format_complete_payload(
                    "完全/运行参数",
                    self.complete_runtime_parameters(
                        len(tasks), privacy_protected=False
                    ),
                ),
            )


    def _preload_run_templates(self, tasks):
        preload_started_ns = time.perf_counter_ns()
        preload_ok = self.load_and_precompute(tasks)
        self.performance.observe_since("template.preload", preload_started_ns)
        elapsed_ms = (time.perf_counter_ns() - preload_started_ns) / 1_000_000.0
        if self.verbose_log_enabled(LOG_BACKEND):
            self.log(
                format_complete_payload(
                    "完全/资源预载",
                    {
                        "status": "success" if preload_ok else "failed",
                        "elapsed_ms": round(elapsed_ms, 3),
                        "task_count": len(tasks),
                        "source_template_count": len(self.base_templates_cache),
                        "scaled_template_count": sum(
                            len(items) for items in self.scaled_templates_cache.values()
                        ),
                        "scaled_template_cache_bytes": self.scaled_template_cache_bytes,
                    },
                ),
                LOG_BACKEND,
            )
        self.run_trace.record(
            "preload_finished",
            status="success" if preload_ok else "failed",
            duration_ms=elapsed_ms,
        )
        return bool(preload_ok), elapsed_ms


    def _abort_preload_run(self, run_id, callback_msg, elapsed_ms):
        self.last_run_trace = self.run_trace.snapshot()
        self.close_screenshot_backend()
        self.finalize_performance_report("preload_failed")
        self._clear_run_callbacks()
        with self._prompt_lock:
            self._active_prompt_request = None
        self.release_injected_inputs()
        scheduler = getattr(self, "auxiliary_click_scheduler", None)
        if scheduler is not None:
            scheduler.close(cancel_pending=True, wait=True)
        self.auxiliary_click_scheduler = None
        self.debug_session.finish()
        self.finish_run(run_id, "preload_failed")
        if callback_msg:
            callback_msg("资源预加载失败，运行已取消")


    def _finalize_run_runtime(
        self,
        *,
        run_id,
        run_outcome,
        run_error,
        callback_msg,
    ):
        if run_outcome == "finished" and self.lifecycle.state is RunState.STOPPING:
            run_outcome = "stopped"
        scheduler = getattr(self, "auxiliary_click_scheduler", None)
        if scheduler is not None:
            if run_outcome == "finished":
                pending_count = scheduler.pending_count
                if pending_count and self.log_level >= 0:
                    self.log(
                        f"    -> 主流程已完成，正在等待 {pending_count} 个附属点击；"
                        "停止运行可立即取消。",
                        LOG_ACTION,
                    )
                scheduler.wait_until_idle(stop_requested=self.check_stop_flag)
                if self.lifecycle.state is RunState.STOPPING:
                    run_outcome = "stopped"
            scheduler.close(
                cancel_pending=run_outcome != "finished",
                wait=True,
            )
        self.auxiliary_click_scheduler = None
        with self._prompt_lock:
            prompt = self._active_prompt_request
            self._active_prompt_request = None
        if prompt is not None:
            try:
                prompt.cancel()
            except Exception as exc:
                logger.warning("取消提示请求失败: %s", exc, exc_info=True)
        self.release_injected_inputs()
        self.close_screenshot_backend()
        self.close_task_window_backend()
        self.run_trace.record("run_finished", status=run_outcome)
        self.last_run_trace = self.run_trace.snapshot()
        report = self.finalize_performance_report(run_outcome)
        if self.log_enabled(LOG_TIMING):
            self.log(
                "<font color='gray'>性能摘要："
                f"{concise_performance_summary(report)}</font>",
                LOG_TIMING,
            )
        if self.verbose_log_enabled(LOG_TIMING):
            self.log(
                format_complete_payload(
                    "完全/运行性能报告",
                    report,
                ),
                LOG_TIMING,
            )
        self._clear_run_callbacks()
        self.debug_session.finish()
        self.finish_run(run_id, run_outcome, run_error)
        policy = self.current_log_policy()
        if callback_msg and policy and policy.allows(LOG_RUN):
            write_log(
                "结束",
                callback_msg,
                ui_timestamp=policy.timestamp_enabled,
            )


    def _abort_failed_run_init(self, run_id, callback_msg, exc):
        """Abort a run whose runtime initialization raised after mark_running.

        SAFETY/INVARIANT: mark_running 之后生命周期必须最终到达 finish_run；
        若初始化异常直接向上冒泡，run_tasks 的 finally 不会执行，lifecycle 将
        永久停留在 RUNNING，后续 reserve_run() 全部返回 None（审查 ENG-FR-003）。
        清理与用户通知的任一步失败都不得阻断 finish_run。
        """
        logger.warning("运行初始化失败: %s", exc, exc_info=True)
        try:
            self.last_run_trace = self.run_trace.snapshot()
            self.close_screenshot_backend()
            self.finalize_performance_report("error")
            self._clear_run_callbacks()
            with self._prompt_lock:
                self._active_prompt_request = None
            self.release_injected_inputs()
            scheduler = getattr(self, "auxiliary_click_scheduler", None)
            if scheduler is not None:
                try:
                    scheduler.close(cancel_pending=True, wait=False)
                except Exception:
                    logger.warning("关闭附属点击调度失败", exc_info=True)
            self.auxiliary_click_scheduler = None
            self.debug_session.finish()
            if callback_msg:
                callback_msg(f"运行初始化失败：{safe_error_name(exc)}")
        except Exception:
            logger.warning("运行初始化失败后的清理出现异常", exc_info=True)
        finally:
            self.finish_run(run_id, "error", safe_error_name(exc))

    def _prepare_run(
        self,
        tasks,
        *,
        run_id,
        callback_msg,
        callback_status,
        callback_click_indicator,
        callback_debug,
        callback_prompt,
    ):
        if run_id is None:
            run_id = self.reserve_run()
        elif not self.active_run_matches(run_id):
            return None
        if run_id is None or not self.lifecycle.mark_running(run_id):
            return None

        try:
            self._initialize_run_runtime(
                tasks,
                callback_msg=callback_msg,
                callback_status=callback_status,
                callback_click_indicator=callback_click_indicator,
                callback_debug=callback_debug,
                callback_prompt=callback_prompt,
            )
            preload_ok, preload_elapsed_ms = self._preload_run_templates(tasks)
            if not preload_ok:
                self._abort_preload_run(run_id, callback_msg, preload_elapsed_ms)
                return None
            self.configure_global_deadline()
        except Exception as exc:
            self._abort_failed_run_init(run_id, callback_msg, exc)
            return None
        return run_id


    def _dispatch_step(self, tasks, task, index, loop_count):
        task = effective_task_settings(task)
        policy = StepExecutionPolicy.from_task(
            task,
            default_confidence=self.confidence,
            default_grayscale=self.enable_grayscale,
            min_scale=self.min_scale,
            max_scale=self.max_scale,
            scale_step=self.scale_step,
            cache_key_override=self._image_target_cache_key_override(task),
        )
        context = self._build_step_execution_context(
            task=task,
            task_count=len(tasks),
            index=index,
            loop_count=loop_count,
            policy=policy,
        )
        attempt_result = self._execute_step_attempts(context)
        if attempt_result.stop_run:
            return StepDispatchResult(
                index,
                attempt_result.made_progress,
                True,
            )

        called_step = 0
        called_result = None
        if not attempt_result.skipped_without_branch:
            call_target_index = self._call_target_index(task, tasks, index)
            if call_target_index is not None:
                called_step = call_target_index + 1
                called_result = self._execute_called_step(
                    tasks,
                    call_target_index,
                    loop_count,
                )
                if called_result is not None and called_result.stop_run:
                    return StepDispatchResult(
                        index,
                        attempt_result.made_progress
                        or called_result.made_progress,
                        True,
                    )

        transition_started_ns = time.perf_counter_ns()
        transition = self._resolve_step_transition(
            task=task,
            policy=policy,
            index=index,
            last_status=attempt_result.last_status,
            failed_branch=attempt_result.failed_branch,
            skipped_without_branch=attempt_result.skipped_without_branch,
        )
        if transition.stop_run:
            return StepDispatchResult(
                transition.next_index,
                attempt_result.made_progress,
                True,
            )

        interval_waited = self.should_wait_step_interval(
            tasks,
            policy.command,
            transition.next_index,
            loop_count,
        )
        if interval_waited and not self.wait_step_interval():
            return StepDispatchResult(
                transition.next_index,
                attempt_result.made_progress,
                True,
            )
        self._record_step_transition(
            tasks=tasks,
            context=context,
            attempt_result=attempt_result,
            transition=transition,
            interval_waited=interval_waited,
            transition_started_ns=transition_started_ns,
            called_step=called_step,
        )
        return StepDispatchResult(
            transition.next_index,
            attempt_result.made_progress
            or bool(called_result and called_result.made_progress),
            entry_reason=transition.entry_reason,
        )

