# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""引擎计时与停止控制服务。从 RPAEngine 提取的非执行职责。"""

from __future__ import annotations

import time

from fukua_rpa.log_policy import LOG_RUN
from fukua_rpa.timing_constants import INTERRUPTIBLE_WAIT_TICK_S


class EngineTimingMixin:
    """计时、等待和停止标志管理（Mixin，由 RPAEngine 继承）。"""

    def check_stop_flag(self):
        """判断当前是否应停止运行。

        Returns:
            收到停止请求，或在“立即停止”模式下已达到全局时间上限时返回 True。
        """
        if self.stop_requested:
            return True
        return (
            self.time_limit_stop_mode == "immediate"
            and self.global_time_limit_reached()
        )

    def global_time_limit_reached(self):
        """判断全局运行时间上限是否已达到。

        Returns:
            已达到指定运行时间返回 True；未设置上限或尚未到达返回 False。
        """
        deadline = self.global_deadline
        if deadline is None or time.monotonic() < deadline:
            return False
        if not self.time_limit_reached:
            self.time_limit_reached = True
            if self.log_level >= 0:
                self.log(
                    "<font color='green'>>>> 提示: 已达到指定运行时间，"
                    + (
                        "正在尽快停止当前步骤"
                        if self.time_limit_stop_mode == "immediate"
                        else "当前循环已完成，任务正常结束"
                    )
                    + "</font>",
                    LOG_RUN,
                )
        return True

    def configure_global_deadline(self):
        """根据循环模式计算并设置全局运行截止时间。

        仅当循环模式为“指定时间(时/分/秒)”时设置 global_deadline，
        否则清除截止时间。
        """
        units = {
            "指定时间(时)": 3600.0,
            "指定时间(分)": 60.0,
            "指定时间(秒)": 1.0,
        }
        multiplier = units.get(self.loop_mode)
        self.time_limit_reached = False
        if multiplier is None:
            self.global_deadline = None
            return
        duration = self._bounded_action_duration(
            max(0.0, float(self.loop_val)) * multiplier,
            maximum=365.0 * 24.0 * 60.0 * 60.0,
        )
        self.global_deadline = time.monotonic() + duration

    def _wait_interruptibly(self, wait_time, stop_requested=None):
        wait_time = self._bounded_action_duration(
            wait_time,
            maximum=self._MAX_WAIT_DURATION_SECONDS,
        )
        # 允许调用方注入额外的停止谓词（如工具级停止事件），与引擎自身
        # 停止标志取或；默认仍使用 engine.check_stop_flag，行为不变。
        should_stop = (
            stop_requested if stop_requested is not None else self.check_stop_flag
        )
        if wait_time <= 0:
            return not should_stop()
        end_time = time.monotonic() + wait_time
        while time.monotonic() < end_time:
            if should_stop():
                return False
            time.sleep(min(INTERRUPTIBLE_WAIT_TICK_S, max(0.0, end_time - time.monotonic())))
        return True

    def wait_recognition_interval(self, extra_delay=0.0, wake_context=None):
        """执行识别前的等待间隔。

        先等待基础识别延迟，再等待自适应额外延迟；提供 wake_context 时
        采用场景唤醒等待。等待期间可被停止请求打断。

        Args:
            extra_delay: 自适应额外等待秒数。
            wake_context: 场景唤醒上下文，为 None 时退化为普通等待。

        Returns:
            等待完成返回 True；被停止请求打断返回 False。
        """
        base_delay = max(0.0, float(self.detect_delay or 0.0))
        adaptive_delay = max(0.0, float(extra_delay or 0.0))
        use_scene_wake = bool(
            wake_context
            and adaptive_delay > 0.0
            and getattr(self, "scene_wake_enabled", True)
        )
        baseline = ()
        if use_scene_wake:
            baseline = self.begin_scene_monitor(wake_context)
        base_started_ns = time.perf_counter_ns()
        if not self._wait_interruptibly(base_delay):
            return False
        self.performance.observe_since("wait.recognition_base", base_started_ns)
        if adaptive_delay <= 0.0:
            return True
        adaptive_started_ns = time.perf_counter_ns()
        try:
            if use_scene_wake:
                return self.wait_adaptive_scene(adaptive_delay, wake_context, baseline)
            return self._wait_interruptibly(adaptive_delay)
        finally:
            self.performance.observe_since(
                "wait.recognition_adaptive", adaptive_started_ns
            )

    def wait_step_interval(self):
        """执行步骤之间的稳定等待间隔。

        Returns:
            等待完成返回 True；被停止请求打断返回 False。
        """
        started_ns = time.perf_counter_ns()
        try:
            return self._wait_interruptibly(self.settlement_wait)
        finally:
            self.performance.observe_since("wait.step_interval", started_ns)
