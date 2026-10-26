# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Mouse click / drag / hover / auxiliary-click primitives.

Mechanical extraction from the former monolithic ``engine_actions.py``.
"""

import logging
import math
import os
import random
import time
from dataclasses import dataclass

from fukua_rpa.actions.coordinate_math import (
    bezier_control_point,
    parse_drag_coordinates,
    verification_search_region,
)
from fukua_rpa.auxiliary_clicks import AuxiliaryClickRequest
from fukua_rpa.log_policy import (
    LOG_ACTION,
    LOG_COORDINATES,
    LOG_CRITICAL,
    LOG_FLOW,
    LOG_RECOGNITION,
    LOG_STEP,
    LOG_TIMING,
)
from fukua_rpa.privacy import (
    private_debug_value,
    private_error_text,
    safe_error_name,
    value_metadata,
)
from fukua_rpa.task_model import config_bool, parse_float_text
from fukua_rpa.vision.collect import CollectionPolicy, MultiTargetCollector
from fukua_rpa.vision.scale_memory import format_scale
from fukua_rpa.vision.shared import match_input_dimensions

from .shared import (
    ClickRetry,
    ClickTarget,
    ClickTiming,
    action_handler,
    activate_window_for_click,
    root_window_at_point,
    uipi_blocks_input,
)

logger = logging.getLogger(__name__)

# 收集阶段的识别周期与停止轮询粒度：只在“全部匹配 + 启用收集”时生效，
# 目的不是承诺识别耗时，而是给出可中断的、有界的帧间隔。
_COLLECT_FRAME_INTERVAL_S = 0.05
_COLLECT_STOP_POLL_S = 0.02


def _bounded_unit_offset(text: str, default: float = 0.5) -> float:
    """Parse a relative in-image click offset and clamp it into ``[0, 1]``.

    REVIEW-20260913-001 FR-002: 拖拽起点/终点偏移此前用裸 ``float()`` 解析，
    ``"inf"``/``"nan"``/``"1e999"`` 会通过，随后 ``int()`` 转换抛
    ``OverflowError``/``ValueError``。``parse_float_text`` 已在共享边界拒绝非有限
    值（失败回退 *default*），因此这里只需在**确认有限之后**再夹取——不要写成
    ``if value < 0 / elif value > 1`` 形式，那样 ``nan`` 会原样穿透比较运算。
    """
    parsed = parse_float_text(text, default)
    if not math.isfinite(parsed):
        return default
    return min(1.0, max(0.0, parsed))


class ActionsMouseMixin:
    """Action primitives for the mouse family."""

    @dataclass
    class _ClickContext:
        """Shared state passed between mouseClick sub-methods."""
        click_times: int
        lOrR: str
        img_path: str
        reTry: int
        step_info: dict
        cache_key: object
        task_conf: float
        use_gray: bool
        point_limit_en: bool
        point_limit_count: int
        coord_step_config: object
        image_click_config: object
        coord_sequence_config: object
        search_regions: object
        background_click_config: object
        allow_inverted: bool
        auxiliary_click_config: object
        coord: object
        use_all_targets: bool
        need_all_matches: bool
        start_time: float
        collect_policy: object = None
        collect_started_at: float = 0.0
        waiting_logged: bool = False
        move_jitter: float | None = None
        hold_jitter: float | None = None
        spatial_jitter: float | None = None
        curve_strength: float | None = None
        trail_enabled: bool | None = None
        restore_position: bool = False

    def _move_mouse_interruptibly(
        self,
        x,
        y,
        duration=0.0,
        curve_strength=None,
        *,
        trail_enabled=None,
        stop_requested=None,
    ):
        """Move the mouse to (x, y) over a duration, abortable via the stop flag.

        Args:
            x: Target x coordinate.
            y: Target y coordinate.
            duration: Movement duration in seconds; 0 moves instantly.
            curve_strength: Bezier curve strength override (0..1); None uses global.
            trail_enabled: Visual trail override; None uses the global setting.
            stop_requested: Optional extra cancellation predicate used by
                standalone input tools with a local stop domain.

        Returns:
            True if the target was reached; False if a stop was requested.
        """
        target_x = self._safe_input_coordinate(x)
        target_y = self._safe_input_coordinate(y)
        duration = self._bounded_action_duration(
            duration, maximum=self._MAX_ACTION_DURATION_SECONDS
        )

        def _should_stop():
            if self.check_stop_flag():
                return True
            if stop_requested is None:
                return False
            try:
                return bool(stop_requested())
            except Exception as exc:  # noqa: BLE001
                logger.warning("鼠标移动取消谓词异常: %s", exc, exc_info=True)
                return True

        if _should_stop():
            return False
        trail_active = (
            bool(getattr(self, "show_mouse_trail", False))
            if trail_enabled is None
            else bool(trail_enabled)
        )
        start = None
        if duration > 0.0:
            # Timed movement needs the real start point to build its cancellable
            # path. FR-20261008-403: reading the pointer can fail (display session
            # torn down, driver error). The trail branch below already tolerates
            # that; this branch must too, otherwise a transient read failure turns
            # the whole click/drag step into an "error" while the same failure is
            # tolerated one branch down. Degrade to an instant move.
            try:
                start = self.input_backend.position()
            except Exception as exc:  # noqa: BLE001
                # FR-20261009-001: 这里原用模块级 logger.debug，记录无任何 handler
                # （冻结版），且 DEBUG 低于 logging.lastResort 阈值，因此该退化在
                # 任何环境下都不可观察。改经项目日志类别通道输出（ADR-0021 决策 3
                # 明列「动作」类别），启用该类别时可观察；控制流与退化行为不变。
                self.log(
                    f"读取鼠标位置失败，退化为瞬时移动: {safe_error_name(exc)}",
                    LOG_ACTION,
                    private_msg=(
                        "读取鼠标位置失败，退化为瞬时移动: "
                        f"{private_error_text(exc)}"
                    ),
                )
                duration = 0.0
        elif trail_active:
            # DESIGN: the trail is optional visual feedback. A transient failure to
            # read the pointer must never suppress the real instant movement/click.
            try:
                start = self.input_backend.position()
            except Exception as exc:  # noqa: BLE001
                logger.debug("读取鼠标位置失败，本次不显示移动拖影: %s", exc)
        if duration <= 0.0:
            self.input_backend.move_to(target_x, target_y, duration=0)
            if trail_active and start is not None:
                start_point = (float(start[0]), float(start[1]))
                if start_point != (float(target_x), float(target_y)):
                    self.report_mouse_trail(
                        [start_point, (target_x, target_y)], new_path=True
                    )
            if not self._settle_after_move():
                return False
            return not _should_stop()

        assert start is not None
        start_x = float(start[0])
        start_y = float(start[1])
        steps = max(1, int(math.ceil(duration / self._INPUT_SLICE_SECONDS)))

        # DESIGN: 贝塞尔控制点——中点 + 随机垂直偏移。curve_strength=0 退化为直线。
        # 步骤级 curve_strength 覆盖优先，None 时沿用引擎全局 move_curve_strength。
        if curve_strength is None:
            curve_strength = getattr(self, "move_curve_strength", 0.0)
        ctrl = bezier_control_point(
            start_x, start_y, target_x, target_y, curve_strength, steps
        )
        ctrl_x = ctrl[0] if ctrl else None
        ctrl_y = ctrl[1] if ctrl else None

        started = time.monotonic()
        trail_batch = [(start_x, start_y)] if trail_active else []
        trail_new_path = trail_active
        last_trail_emit = started
        for index in range(1, steps + 1):
            if _should_stop():
                return False
            ratio = index / steps
            if ctrl_x is not None:
                # 二次贝塞尔：B(t) = (1-t)²P0 + 2(1-t)tP1 + t²P2
                inv = 1.0 - ratio
                current_x = round(inv * inv * start_x + 2 * inv * ratio * ctrl_x + ratio * ratio * target_x)
                current_y = round(inv * inv * start_y + 2 * inv * ratio * ctrl_y + ratio * ratio * target_y)
            else:
                current_x = round(start_x + (target_x - start_x) * ratio)
                current_y = round(start_y + (target_y - start_y) * ratio)
            self.input_backend.move_to(current_x, current_y, duration=0)
            now = time.monotonic()
            if trail_active:
                trail_batch.append((current_x, current_y))
                if now - last_trail_emit >= (1.0 / 30.0) or index == steps:
                    self.report_mouse_trail(
                        trail_batch, new_path=trail_new_path
                    )
                    trail_batch = []
                    trail_new_path = False
                    last_trail_emit = now
            remaining = started + duration * ratio - now
            if remaining > 0 and not self._wait_interruptibly(
                remaining, stop_requested=stop_requested
            ):
                return False
        # INVARIANT: ratio=1.0 时贝塞尔 B(1)=P2=target，精确到达目标。
        if _should_stop():
            return False
        return self._settle_after_move()

    def _settle_after_move(self):
        """Wait the configured move-then-settle delay before pressing a button.

        DESIGN: this is an explicit compatibility parameter, separate from the
        move duration and the step interval; 0 keeps the previous immediate
        click. It is interruptible, so a stop request never waits it out.
        """

        try:
            settle = float(getattr(self, "mouse_move_settle_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            return True
        if settle <= 0 or not math.isfinite(settle):
            return True
        return self._wait_interruptibly(min(settle, 2.0))

    def _press_mouse_button(self, button, click_times=1, hold_jitter=None):
        """Perform one or more press-and-release clicks with hold timing.

        Args:
            button: Mouse button name.
            click_times: Number of consecutive clicks.
            hold_jitter: Per-step hold jitter override; None uses global.

        Returns:
            True if all clicks completed; False if a stop was requested.
        """
        for _ in range(max(1, int(click_times))):
            if self.check_stop_flag():
                return False
            pressed = self._tracked_mouse_down(button)
            if not pressed:
                return False
            try:
                hold_range = (
                    self.click_hold_jitter if hold_jitter is None else hold_jitter
                )
                hold = max(0.01, self._bounded_action_duration(
                    self.click_hold, maximum=10.0
                ) + self._jitter_delay(hold_range))
                if not self._wait_interruptibly(hold):
                    return False
            finally:
                self._tracked_mouse_up(button)
            if click_times > 1 and not self._wait_interruptibly(0.02):
                return False
        return True

    def perform_mouse_click(
        self, x, y, clickTimes, lOrR, indicator_text="", *,
        move_jitter=None, hold_jitter=None,
        spatial_jitter=None, curve_strength=None, trail_enabled=None,
        restore_position=False,
    ):
        """在输入锁保护下执行一次前台鼠标点击。

        Args:
            x: 点击横坐标。
            y: 点击纵坐标。
            clickTimes: 点击次数。
            lOrR: 按键，"left" 或 "right"。
            indicator_text: 点击指示器文本。
            move_jitter: 本步骤移动抖动覆盖；None 沿用全局值。
            hold_jitter: 本步骤按住抖动覆盖；None 沿用全局值。
            spatial_jitter: 本步骤点击偏移覆盖（像素）；None 沿用全局值。
            curve_strength: 本步骤移动曲线强度覆盖（0~1）；None 沿用全局值。
            trail_enabled: 本步骤视觉拖影覆盖；None 沿用全局值。
            restore_position: 点击完成后是否移回点击前的鼠标位置。

        Returns:
            点击完成返回 True；被停止或失败返回 False。
        """
        started_ns = time.perf_counter_ns()
        try:
            with self._input_action_lock:
                return self._perform_mouse_click_impl(
                    x, y, clickTimes, lOrR, indicator_text,
                    move_jitter=move_jitter, hold_jitter=hold_jitter,
                    spatial_jitter=spatial_jitter,
                    curve_strength=curve_strength,
                    trail_enabled=trail_enabled,
                    restore_position=restore_position,
                )
        finally:
            self.performance.observe_since("action.mouse_click", started_ns)
            self.performance.increment("action.mouse_clicks", clickTimes)

    def perform_background_window_click(
        self,
        x,
        y,
        click_times,
        button,
        binding,
        indicator_text="",
    ):
        """通过窗口后端向目标窗口发送后台点击。

        不移动真实鼠标。当点击结果未知（UIA 超时）时会停止运行，
        以避免重试造成重复点击。

        Args:
            x: 点击横坐标。
            y: 点击纵坐标。
            click_times: 点击次数。
            button: 按键，"left" 或 "right"。
            binding: 目标窗口绑定信息。
            indicator_text: 点击指示器文本。

        Returns:
            后台点击成功返回 True，否则 False。
        """
        with self._input_action_lock:
            if self.check_stop_flag():
                return False
            backend = self.task_window_backend()
            adjusted = backend.binding_for_coordinate(binding, x, y)
            if not adjusted:
                return False
            started_ns = time.perf_counter_ns()
            success = backend.background_click(
                adjusted,
                button,
                click_times,
                stop_requested=self.check_stop_flag,
            )
        self.performance.observe_since("action.background_click", started_ns)
        self.performance.increment("action.background_clicks", click_times)
        if success:
            label = indicator_text or (
                f"后台{'左键' if button == 'left' else '右键'}点击"
            )
            self.report_click_indicator(x, y, label)
            if self.log_level >= 2:
                method = backend.last_background_method or "窗口消息"
                self.log(
                    f"       后台点击已发送：({int(x)}, {int(y)})，方式 {method}",
                    LOG_ACTION,
                )
        else:
            # SAFETY: outcome_unknown means the UIA COM call timed out but may
            # still complete asynchronously. Stop the run to prevent a retry from
            # delivering a duplicate action (constraint #60 / ADR 0007).
            detail = getattr(backend, "last_background_detail", None) or {}
            if detail.get("outcome_unknown"):
                self.log(
                    "<font color='red'><b>    [后台点击结果未知] UIA 超时，"
                    "为避免重复点击，脚本已停止。</b></font>",
                    LOG_CRITICAL,
                    critical=True,
                )
                self.stop()
            elif not self.check_stop_flag() and self.log_level >= 0:
                self.log(
                    "<font color='orange'>    [后台点击失败] 目标窗口已失效、拒绝消息或权限不匹配；"
                    "未改用真实鼠标。</font>",
                    LOG_ACTION,
                )
        return success

    def _prepare_foreground_click(self, x, y):
        """前台真实点击前的窗口准备：可选激活目标窗口并检查 UIPI 权限隔离。

        DESIGN: 真实鼠标点击对部分窗口无效有两个常见可缓解原因——目标窗口
        不是前台（一些应用会忽略发给非前台窗口的点击），以及目标进程以更高
        完整性级别（管理员）运行导致 UIPI 静默丢弃 SendInput。前者在用户启用
        activate_before_click 时尽力激活目标窗口；后者无论是否激活都检测并提
        示。两者都不改变“后台点击失败不偷偷改用真实鼠标”等既有安全取舍。

        SAFETY: 本方法只做尽力而为的准备，任何失败都不得阻断随后的点击；UIPI
        命中时仅告警。告警按目标进程去重，避免高频点击场景刷屏。
        """
        hwnd = root_window_at_point(x, y)
        if not hwnd:
            return
        # DESIGN: 激活默认关闭。某些流程依赖目标窗口保持非前台（例如同时操作
        # 多个窗口），强制激活会破坏这些场景；仅当用户明确启用时才激活。
        if getattr(self, "activate_before_click", False):
            activate_window_for_click(hwnd)
        blocked, pid = uipi_blocks_input(hwnd)
        if not blocked:
            return
        with self._injected_input_lock:
            warned = getattr(self, "_uipi_warned_pids", None)
            if warned is None:
                warned = set()
                self._uipi_warned_pids = warned
            already_warned = pid in warned
            warned.add(pid)
        if already_warned:
            return
        if self.log_level >= 0:
            self.log(
                "<font color='orange'>    [权限] 目标窗口以更高权限（管理员）运行，"
                "真实点击可能被系统静默丢弃；请以管理员身份运行 fukuaRPA。</font>",
                LOG_ACTION,
            )

    def _perform_mouse_click_impl(
        self, x, y, clickTimes, lOrR, indicator_text="", *,
        move_jitter=None, hold_jitter=None,
        spatial_jitter=None, curve_strength=None, trail_enabled=None,
        restore_position=False,
    ):
        """Execute a foreground click sequence: move, press, optional dodge and restore.

        Args:
            x: Click x coordinate.
            y: Click y coordinate.
            clickTimes: Number of clicks.
            lOrR: Button, "left" or "right".
            indicator_text: Click indicator label.
            move_jitter: Per-step move jitter override; None uses global.
            hold_jitter: Per-step hold jitter override; None uses global.
            spatial_jitter: Per-step click offset override (px); None uses global.
            curve_strength: Per-step move curve strength override; None uses global.
            trail_enabled: Per-step visual trail override; None uses global.
            restore_position: Whether to move back after clicking.

        Returns:
            True if the click completed; False if stopped or failed.
        """
        # DESIGN: 点击空间抖动——在目标点周围随机偏移 ±N 像素，模拟人类不会每次
        # 精确点击同一像素。步骤级覆盖优先，None 时沿用全局 click_spatial_jitter。
        spatial = spatial_jitter if spatial_jitter is not None else getattr(
            self, "click_spatial_jitter", 0
        )
        if spatial and spatial > 0:
            x = x + random.randint(-int(spatial), int(spatial))
            y = y + random.randint(-int(spatial), int(spatial))

        # 在最终点击坐标上做前台准备：可选激活目标窗口 + UIPI 权限告警。
        # 仅尽力而为，绝不阻断点击。
        self._prepare_foreground_click(x, y)

        move_range = (
            self.move_duration_jitter if move_jitter is None else move_jitter
        )
        # 点击后返回：记录点击前的鼠标位置，点击（及避让）完成后移回原位。
        restore_point = None
        if restore_position:
            try:
                pos = self.input_backend.position()
                restore_point = (int(pos[0]), int(pos[1]))
            except Exception:  # noqa: BLE001
                restore_point = None
        if not self._move_mouse_interruptibly(
            x, y, max(0.0, self.move_duration + self._jitter_delay(move_range)),
            curve_strength=curve_strength,
            trail_enabled=trail_enabled,
        ):
            return False
        if not self._press_mouse_button(lOrR, clickTimes, hold_jitter=hold_jitter):
            return False

        if clickTimes >= 2:
            # P3-1: 前台双击在物理两次单击之外，向落点窗口补发 WM_*BUTTONDBLCLK，
            # 与后台 PostMessage 路径（mapping_backend）保持一致；对未声明 CS_DBLCLKS
            # 的窗口也能确保收到双击。FR-401-001: 落点坐标直接使用输入坐标——PMv2 下
            # 即物理像素；System-DPI 回退下 WindowFromPoint/GetWindowRect 按虚拟化
            # 空间解释，不得先经 input_to_physical_coordinates 反算（否则混合 DPI 副屏
            # 落点窗口选择与客户区坐标错位）。
            try:
                from fukua_rpa.win32_api import post_double_click_to_window_at_point

                post_double_click_to_window_at_point(x, y, lOrR)
            except Exception:  # noqa: BLE001
                pass

        self.report_click_indicator(x, y, indicator_text or f"{'左键' if lOrR == 'left' else '右键'}点击")

        if self.enable_dodge:
            final_x, final_y = self.dodge_x1, self.dodge_y1
            if (final_x, final_y) != (x, y) and not self._move_mouse_interruptibly(
                final_x, final_y, 0, trail_enabled=trail_enabled
            ):
                return False
            if self.enable_double_dodge:
                if not self._wait_interruptibly(self.double_dodge_wait):
                    return False
                next_x, next_y = self.dodge_x2, self.dodge_y2
                if (next_x, next_y) != (final_x, final_y) and not self._move_mouse_interruptibly(
                    next_x, next_y, 0, trail_enabled=trail_enabled
                ):
                    return False
                final_x, final_y = next_x, next_y
            dodge_button = {
                "left": "left",
                "right": "right",
            }.get(str(getattr(self, "dodge_click_action", "none")))
            if dodge_button:
                if not self._press_mouse_button(
                    dodge_button, hold_jitter=hold_jitter
                ):
                    return False
                self.performance.increment("action.dodge_clicks")
                label = "避让后左键单击" if dodge_button == "left" else "避让后右键单击"
                self.report_click_indicator(final_x, final_y, label)
        return not (
            restore_position
            and restore_point is not None
            and not self._move_mouse_interruptibly(
                restore_point[0],
                restore_point[1],
                0.0,
                trail_enabled=trail_enabled,
            )
        )

    def click_supplement_options(self, task, *, image_target, trail_enabled=None):
        """从步骤配置解析附属补点选项。

        Args:
            task: 步骤配置字典。
            image_target: 该步骤是否为图片识别目标。

        Returns:
            含延时二次点击与识别确认补点配置的字典；两者均未启用时返回 None。
        """
        task = task if isinstance(task, dict) else {}
        delayed_enabled = config_bool(task.get("delayed_second_click_en", False))
        confirm_enabled = bool(image_target) and config_bool(
            task.get("image_confirm_click_en", False)
        )
        if not delayed_enabled and not confirm_enabled:
            return None
        return {
            "delayed_enabled": delayed_enabled,
            "delayed_seconds": (
                self._bounded_action_duration(
                    task.get("delayed_second_click_delay", 0.5), maximum=300.0
                )
                if delayed_enabled
                else 0.0
            ),
            "confirm_enabled": confirm_enabled,
            "confirm_seconds": (
                self._bounded_action_duration(
                    task.get("image_confirm_click_delay", 0.5), maximum=300.0
                )
                if confirm_enabled
                else 0.0
            ),
            "trail_enabled": trail_enabled,
        }

    def _queue_auxiliary_click(self, request, delay_seconds):
        """Schedule an auxiliary click request after a delay.

        Args:
            request: AuxiliaryClickRequest to enqueue.
            delay_seconds: Delay before execution.

        Returns:
            True if queued; False if no scheduler, stopped, or queue full.
        """
        scheduler = getattr(self, "auxiliary_click_scheduler", None)
        if scheduler is None or self.check_stop_flag():
            return False
        queued = scheduler.schedule(request, delay_seconds)
        if queued:
            self.performance.increment("action.auxiliary_clicks_scheduled")
        else:
            self.performance.increment("action.auxiliary_clicks_rejected")
            if self.log_level >= 1 and not getattr(
                self, "_auxiliary_queue_warning_logged", False
            ):
                self._auxiliary_queue_warning_logged = True
                self.log(
                    "<font color='orange'>    [附属点击] 待执行队列已满，本次补点已忽略。</font>",
                    LOG_ACTION,
                )
        return queued

    def schedule_click_supplements(
        self,
        *,
        img_path,
        cache_key,
        confidence,
        use_gray,
        allow_inverted,
        center,
        scale,
        click_point,
        button,
        binding,
        config,
        matched_size=None,
    ):
        """按配置调度附属补点请求。

        根据 config 中的开关，将延时二次点击和识别确认补点加入
        附属点击调度队列延迟执行。

        Args:
            img_path: 识别图片路径（识别确认补点用）。
            cache_key: 识别缓存键。
            confidence: 识别置信度。
            use_gray: 是否灰度识别。
            allow_inverted: 是否允许反色匹配。
            center: 命中目标中心坐标。
            scale: 命中缩放比例。
            click_point: 实际点击坐标。
            button: 按键。
            binding: 后台窗口绑定，前台点击为 None。
            config: 由 click_supplement_options 产生的补点配置。
            matched_size: 可选的 input-space 命中宽高；原生识别在
                System-DPI 回退下必须保留该尺寸，不能用物理模板像素重算。
        """
        if not config:
            return
        click_x, click_y = click_point
        if config.get("delayed_enabled"):
            self._queue_auxiliary_click(
                AuxiliaryClickRequest(
                    kind="delayed_second_click",
                    x=float(click_x),
                    y=float(click_y),
                    button=str(button),
                    label="延时二次点击",
                    binding=dict(binding) if isinstance(binding, dict) else None,
                    trail_enabled=config.get("trail_enabled"),
                ),
                config.get("delayed_seconds", 0.5),
            )
        if config.get("confirm_enabled") and img_path:
            self._queue_auxiliary_click(
                AuxiliaryClickRequest(
                    kind="image_confirm_click",
                    x=float(click_x),
                    y=float(click_y),
                    button=str(button),
                    label="识别确认补点",
                    verification={
                        "img_path": str(img_path),
                        "cache_key": str(cache_key),
                        "confidence": float(confidence),
                        "use_gray": bool(use_gray),
                        "allow_inverted": bool(allow_inverted),
                        "center_x": float(center[0]),
                        "center_y": float(center[1]),
                        "scale": float(scale),
                        "matched_width": (
                            float(matched_size[0]) if matched_size else None
                        ),
                        "matched_height": (
                            float(matched_size[1]) if matched_size else None
                        ),
                    },
                    trail_enabled=config.get("trail_enabled"),
                ),
                config.get("confirm_seconds", 0.5),
            )

    def _verify_auxiliary_image(self, verification):
        """Verify an image-confirm target still matches near its original location.

        Args:
            verification: Dict describing the original match and recognition params.

        Returns:
            True if the target is still present within tolerance; False otherwise.
        """
        if not isinstance(verification, dict) or self.check_stop_flag():
            return False
        path = str(verification.get("img_path", "") or "")
        if not path or not os.path.isfile(path):
            return False
        scale = max(0.01, float(verification.get("scale", 1.0)))
        center_x = float(verification.get("center_x", 0.0))
        center_y = float(verification.get("center_y", 0.0))
        template_width, template_height = self.template_dimensions(path)
        matched_width = verification.get("matched_width")
        matched_height = verification.get("matched_height")
        try:
            matched_width = float(matched_width)
            matched_height = float(matched_height)
        except (TypeError, ValueError):
            matched_width = max(1.0, float(template_width) * scale)
            matched_height = max(1.0, float(template_height) * scale)
        if not math.isfinite(matched_width) or matched_width <= 0:
            matched_width = max(1.0, float(template_width) * scale)
        if not math.isfinite(matched_height) or matched_height <= 0:
            matched_height = max(1.0, float(template_height) * scale)
        region = verification_search_region(
            center_x, center_y, matched_width, matched_height, 1.0
        )
        with self._vision_lock:
            screenshot, offset_x, offset_y = self.capture_screenshot(region)
            found = self.find_target_in_screenshot(
                path,
                verification.get("cache_key"),
                float(verification.get("confidence", self.confidence)),
                bool(verification.get("use_gray", self.enable_grayscale)),
                screenshot,
                offset_x,
                offset_y,
                allowed_scales=(scale,),
                allow_inverted=bool(verification.get("allow_inverted", False)),
            )
        if not found:
            return False
        tolerance = max(8.0, min(matched_width, matched_height) * 0.3)
        return math.hypot(float(found[0]) - center_x, float(found[1]) - center_y) <= tolerance

    def execute_auxiliary_click(self, request):
        """执行一个附属点击请求。

        识别确认补点会先校验目标是否仍在原命中位置，已变化则放弃点击；
        随后按请求选择后台或前台点击。

        Args:
            request: 待执行的 AuxiliaryClickRequest。
        """
        if self.check_stop_flag():
            return
        if request.verification and not self._verify_auxiliary_image(
            dict(request.verification)
        ):
            self.performance.increment("action.image_confirm_skipped")
            if self.verbose_log_enabled(LOG_RECOGNITION):
                self.log(
                    "    <font color='gray'>[识别确认补点] 原命中位置已变化，本次不再点击。</font>",
                    LOG_RECOGNITION,
                )
            return
        if request.binding:
            clicked = self.perform_background_window_click(
                request.x,
                request.y,
                1,
                request.button,
                dict(request.binding),
                request.label,
            )
        else:
            clicked = self.perform_mouse_click(
                request.x,
                request.y,
                1,
                request.button,
                request.label,
                trail_enabled=request.trail_enabled,
            )
        if clicked:
            self.performance.increment("action.auxiliary_clicks_executed")
            if self.log_level >= 1:
                self.log(
                    f"    -> {request.label}已执行：({int(request.x)}, {int(request.y)})",
                    LOG_ACTION,
                )

    def _resolve_click_targets(self, ctx):
        """Resolve click targets from coordinates, sequences, stepping, or image search.

        Returns a string for early exit (e.g. "skipped"), or a tuple
        (locations, find_time, coord_state, seq_state).
        """
        coord = ctx.coord
        if coord:
            if ctx.coord_sequence_config:
                seq_points, seq_state, seq_status = self._coord_sequence_locations(
                    ctx.step_info, ctx.coord_sequence_config
                )
                if seq_status == "done":
                    if self.log_level >= 1:
                        self.log("<font color='gray'>    -> 自定义点位已全部执行，本步骤按设置跳过。</font>", LOG_COORDINATES)
                    return "skipped"
                if not seq_points:
                    if self.log_level >= 1:
                        self.log("<font color='gray'>    -> 自定义点位为空，已跳过本步骤。</font>", LOG_COORDINATES)
                    return "skipped"
                locations = [
                    (point[0], point[1], 1.0, 1.0) for point in seq_points
                ]
                return locations, 0.0, None, seq_state
            elif ctx.coord_step_config:
                _step_key, coord_state = self._get_coord_step_state(ctx.step_info, coord[0], coord[1])
                locations = [(coord_state["x"], coord_state["y"], 1.0, 1.0)]
                return locations, 0.0, coord_state, None
            else:
                locations = [(coord[0], coord[1], 1.0, 1.0)]
                return locations, 0.0, None, None
        elif ctx.need_all_matches:
            if ctx.collect_policy is not None and ctx.collect_policy.enabled:
                collected = self._collect_multi_targets(ctx)
                if isinstance(collected, str):
                    return collected
                return collected, self._collect_find_time(ctx), None, None
            find_start = time.monotonic()
            locations = self.find_all_targets_optimized(
                ctx.img_path,
                ctx.cache_key,
                ctx.task_conf,
                ctx.use_gray,
                ctx.search_regions,
                ctx.allow_inverted,
            )
            find_time = time.monotonic() - find_start
            return locations, find_time, None, None
        else:
            find_start = time.monotonic()
            location_tuple = self.find_target_optimized(
                ctx.img_path,
                ctx.cache_key,
                ctx.task_conf,
                ctx.use_gray,
                ctx.search_regions,
                ctx.allow_inverted,
            )
            find_time = time.monotonic() - find_start
            if location_tuple and len(location_tuple) >= 4:
                locations = [tuple(location_tuple)]
            elif location_tuple:
                locations = [
                    (
                        location_tuple[0],
                        location_tuple[1],
                        location_tuple[2],
                        ctx.task_conf,
                    )
                ]
            else:
                locations = []
            return locations, find_time, None, None

    def _multi_collect_policy(self, use_all_targets):
        """Build the collection policy from global settings.

        Returns None when collection is not applicable or fully disabled, so the
        historical immediate-click path is preserved. Collection is scoped to the
        “全部匹配” mode only (按反馈要求：仅在全部匹配图片点击中生效).
        """

        if not use_all_targets:
            return None
        try:
            policy = CollectionPolicy(
                first_hit_buffer_s=float(
                    getattr(self, "multi_collect_first_hit_s", 0.0) or 0.0
                ),
                last_hit_buffer_s=float(
                    getattr(self, "multi_collect_last_hit_s", 0.0) or 0.0
                ),
                max_collect_s=max(
                    0.0, float(getattr(self, "multi_collect_max_s", 0.0) or 0.0)
                ),
                max_targets=int(
                    getattr(self, "multi_collect_max_targets", 0) or 0
                ),
                allow_partial=bool(
                    getattr(self, "multi_collect_allow_partial", True)
                ),
            )
        except (TypeError, ValueError):
            self.log(
                "收集参数无法解析，本次按“首轮命中立即处理”执行",
                LOG_RECOGNITION,
            )
            return None
        return policy if policy.enabled else None

    def _collect_find_time(self, ctx):
        """Total time spent recognising targets during the collection stage."""

        return max(0.0, time.monotonic() - ctx.collect_started_at)

    def _collection_sleep(self, seconds):
        """Bounded, stop-aware pause between collection frames."""

        end = time.monotonic() + max(0.0, float(seconds))
        while True:
            if self.check_stop_flag():
                return False
            remaining = end - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(_COLLECT_STOP_POLL_S, remaining))

    def _collect_multi_targets(self, ctx):
        """Gather all-match targets over several frames, then return the last frame.

        Returns the final frame's locations (the only coordinates that may be
        clicked), or a status string: ``"stopped"`` or ``"timeout"``. An empty
        result is returned as ``[]`` and is classified as ``not_found`` by the
        caller (``mouseClick``), never by this helper itself.
        Collection never clicks during gathering, so the click itself cannot
        change what counts as a "new" target.
        """

        policy = ctx.collect_policy
        # 每轮进入收集分支都刷新起始时钟：reTry==-1 首轮未命中返回 [] 后外层
        # 会等待并重试，不刷新会把识别间隔/退避/唤醒等待全部计入「底层找图
        # 耗时」（FR-20261010-2201）。
        ctx.collect_started_at = time.monotonic()
        collector = MultiTargetCollector(policy, started_at=time.monotonic())
        while True:
            if self.check_stop_flag():
                return "stopped"
            if self.timeout_val > 0 and (
                time.monotonic() - ctx.start_time > self.timeout_val
            ):
                if self.log_level >= 1:
                    self.log(
                        f"<font color='orange'>    [超时] 循环#{ctx.step_info['loop']} 步{ctx.step_info['step']}: 等待目标超时</font>",
                        LOG_STEP,
                    )
                return "timeout"
            locations = self.find_all_targets_optimized(
                ctx.img_path,
                ctx.cache_key,
                ctx.task_conf,
                ctx.use_gray,
                ctx.search_regions,
                ctx.allow_inverted,
            )
            now = time.monotonic()
            # 先做区域/同点过滤再计数：被过滤掉的候选不能用来凑“设定数量”。
            usable = self._usable_collect_targets(locations, ctx)
            if usable and not ctx.coord:
                self.reset_recognition_miss(ctx.img_path, ctx.step_info)
            collector.add_frame(usable, now)
            if collector.first_hit_at is None:
                # 从未命中过任何目标：沿用原有搜索/重试语义（识别间隔、重试、超时与
                # “未找到”日志都会继续生效），收集阶段不会在空画面上无限等待。
                return []
            if collector.should_finish(now):
                selected = list(collector.latest_locations)
                if not selected:
                    # 进入收集后目标全部消失：不得点击历史坐标，也不得把零次点击标成
                    # 成功；交回外层的“未找到/重试”路径。
                    return []
                # 只点击结束前这张截图里仍然可见的目标。
                if self.log_level >= 1:
                    self.log(
                        f"    -> 收集完成：累计发现 {collector.count} 个目标，"
                        f"当前可见 {len(selected)} 个",
                        LOG_RECOGNITION,
                    )
                return selected
            if collector.expired(now):
                # 总收集时间的硬上限：收不齐时按超时处理，绝不偷偷少点后标成功。
                if self.log_level >= 1:
                    self.log(
                        f"<font color='orange'>    [超时] 循环#{ctx.step_info['loop']} 步{ctx.step_info['step']}: "
                        f"收集到 {collector.available_count} 个可用目标，"
                        f"未达到设定数量 {policy.max_targets}</font>",
                        LOG_STEP,
                    )
                return "timeout"
            if not self._collection_sleep(_COLLECT_FRAME_INTERVAL_S):
                return "stopped"

    def _usable_collect_targets(self, locations, ctx):
        """Apply the region and same-point filters used before clicking.

        Collection must count only targets that would really be clicked, so the
        filters run inside the loop instead of after it.
        """

        usable = locations
        if ctx.search_regions and not ctx.coord:
            usable = [
                loc
                for loc in usable
                if self.point_in_search_regions(
                    *self.adjusted_image_click_point(ctx.img_path, loc, ctx.image_click_config),
                    ctx.search_regions,
                )
            ]
        if ctx.point_limit_en:
            # 收集阶段每帧都会调用，重复日志没有信息量：由外层汇总一次即可。
            usable = self._filter_point_limit_targets(
                usable,
                ctx.img_path,
                ctx.step_info,
                ctx.point_limit_en,
                ctx.point_limit_count,
                log_skipped=False,
            )
        return usable

    def _order_multi_targets(self, locations, ctx):
        """Apply region filtering, point-limit filtering, and target selection.

        Returns the final click_locations list, or None if all targets were filtered out.
        """
        coord = ctx.coord
        if ctx.search_regions and not coord:
            before_count = len(locations)
            locations = [
                loc for loc in locations
                if self.point_in_search_regions(*self.adjusted_image_click_point(ctx.img_path, loc, ctx.image_click_config), ctx.search_regions)
            ]
            if not locations:
                self.record_recognition_miss(ctx.img_path, ctx.step_info)
                if self.log_level >= 1:
                    self.log(f"<font color='orange'>    [跳过] 循环#{ctx.step_info['loop']} 步{ctx.step_info['step']}: 命中目标的实际点击点不在本步识别区域内，已过滤 {before_count} 个点位</font>", LOG_RECOGNITION)
                return None
        if ctx.point_limit_en:
            locations = self._filter_point_limit_targets(locations, ctx.img_path, ctx.step_info, ctx.point_limit_en, ctx.point_limit_count)
            if not locations:
                if not coord:
                    self.record_recognition_miss(ctx.img_path, ctx.step_info)
                if self.log_level >= 1:
                    self.log(f"<font color='orange'>    [跳过] 循环#{ctx.step_info['loop']} 步{ctx.step_info['step']}: 当前图片所有识别点位都已达到同点点击上限</font>", LOG_RECOGNITION)
                return None

        click_locations = (
            locations
            if ctx.use_all_targets or (coord and ctx.coord_sequence_config)
            else locations[:1]
        )
        if ctx.use_all_targets and ctx.collect_policy is not None:
            limit = ctx.collect_policy.max_targets
            if limit > 0:
                # 数量上限在区域/同点过滤之后生效：被过滤掉的候选不能充数。
                click_locations = click_locations[:limit]
        return click_locations

    def _execute_click_sequence(self, click_locations, ctx, find_time, coord_state, seq_state):
        """Execute clicks on all resolved targets with logging and auxiliary scheduling.

        Returns "success", "stopped", or "error".
        """
        try:
            if self.verbose_log_enabled(LOG_TIMING):
                self.log(
                    "    <font color='gray'>"
                    f"底层找图耗时 {find_time:.3f}s</font>",
                    LOG_TIMING,
                )

            if ctx.use_all_targets:
                if self.log_level >= 1:
                    self.log(f"    -> 共识别到 {len(click_locations)} 个可点击目标，按【{self.multi_target_order}】顺序执行点击", LOG_RECOGNITION)
            elif self.log_level >= 1:
                x, y, scale, _score = click_locations[0][:4]
                click_x, click_y = self.adjusted_image_click_point(ctx.img_path, click_locations[0], ctx.image_click_config)
                if ctx.image_click_config:
                    self.log(
                        f"    -> 已在坐标 ({int(x)}, {int(y)}) 锁定目标，"
                        f"缩放 {format_scale(scale)}x，实际点击图片内位置 "
                        f"({int(click_x)}, {int(click_y)})",
                        LOG_RECOGNITION,
                    )
                else:
                    self.log(
                        f"    -> 已在坐标 ({int(x)}, {int(y)}) 锁定目标并执行点击，"
                        f"缩放 {format_scale(scale)}x",
                        LOG_RECOGNITION,
                    )

            for target_idx, location_tuple in enumerate(click_locations, 1):
                if self.check_stop_flag():
                    return "stopped"
                x, y, scale, score = location_tuple[:4]
                click_x, click_y = self.adjusted_image_click_point(ctx.img_path, location_tuple, ctx.image_click_config)
                if ctx.use_all_targets and self.log_level >= 2:
                    if ctx.image_click_config:
                        self.log(f"       多目标 {target_idx}/{len(click_locations)} -> 命中中心({int(x)}, {int(y)})，点击({int(click_x)}, {int(click_y)})，相似度 {score:.3f} 缩放 {format_scale(scale)}x", LOG_RECOGNITION)
                    else:
                        self.log(f"       多目标 {target_idx}/{len(click_locations)} -> ({int(x)}, {int(y)}) 相似度 {score:.3f} 缩放 {format_scale(scale)}x", LOG_RECOGNITION)
                if ctx.coord_step_config and coord_state and self.log_level >= 2:
                    self.log(self.coord_step_log_message(x, y, coord_state, ctx.coord_step_config), LOG_COORDINATES)
                click_label = ("左键" if ctx.lOrR == "left" else "右键") + ("双击" if ctx.click_times == 2 else "单击")
                if ctx.background_click_config:
                    clicked = self.perform_background_window_click(
                        click_x,
                        click_y,
                        ctx.click_times,
                        ctx.lOrR,
                        ctx.background_click_config.get("binding", {}),
                        "后台" + click_label,
                    )
                else:
                    clicked = self.perform_mouse_click(
                        click_x, click_y, ctx.click_times, ctx.lOrR, click_label,
                        move_jitter=ctx.move_jitter, hold_jitter=ctx.hold_jitter,
                        spatial_jitter=ctx.spatial_jitter,
                        curve_strength=ctx.curve_strength,
                        trail_enabled=ctx.trail_enabled,
                        restore_position=ctx.restore_position,
                    )
                if not clicked:
                    return "stopped" if self.check_stop_flag() else "error"
                self.schedule_click_supplements(
                    img_path=None if ctx.coord else ctx.img_path,
                    cache_key=ctx.cache_key,
                    confidence=ctx.task_conf,
                    use_gray=ctx.use_gray,
                    allow_inverted=ctx.allow_inverted,
                    center=(x, y),
                    scale=scale,
                    click_point=(click_x, click_y),
                    button=ctx.lOrR,
                    binding=(
                        ctx.background_click_config.get("binding", {})
                        if ctx.background_click_config
                        else None
                    ),
                    config=ctx.auxiliary_click_config,
                    matched_size=(
                        match_input_dimensions(
                            location_tuple,
                            *self.template_dimensions(ctx.img_path),
                        )
                        if not ctx.coord and ctx.img_path
                        else None
                    ),
                )
                advance_result = self._advance_click_state(
                    ctx, coord_state, seq_state, x, y, target_idx, len(click_locations)
                )
                if advance_result:
                    return advance_result

        except Exception as e:
            if self.log_enabled(LOG_CRITICAL, critical=True):
                self.log(
                    f"<font color='red'>    [错误] 循环#{ctx.step_info['loop']} 步{ctx.step_info['step']}: {safe_error_name(e)}</font>",
                    LOG_CRITICAL,
                    critical=True,
                    private_msg=(
                        f"<font color='red'>    [错误] 循环#{ctx.step_info['loop']} 步{ctx.step_info['step']}: "
                        f"{private_error_text(e)}</font>"
                    ),
                )
            return "error"
        return "success"

    def _advance_click_state(self, ctx, coord_state, seq_state, x, y, target_idx, total_targets):
        """Advance coordinate stepping, sequence index, and point-limit counters after a click.

        Returns "stopped" to abort the sequence, or None to continue.
        """
        if ctx.coord_step_config and coord_state:
            step_result = self._advance_coord_step_state(coord_state, ctx.coord_step_config, ctx.step_info)
            if step_result == "locked_stop":
                if self.log_level >= 0:
                    self.log("<font color='red'><b>    -> 坐标步进达到移动上限，已按设置停止脚本。</b></font>", LOG_FLOW)
                self.stop()
                return "stopped"
        if ctx.coord_sequence_config and ctx.coord:
            self._advance_coord_sequence(seq_state)
        if ctx.point_limit_en:
            used_count = self._record_point_click(ctx.img_path, ctx.step_info, x, y)
            if self.log_level >= 2:
                self.log(f"       同点位已点击 {used_count}/{ctx.point_limit_count} 次", LOG_RECOGNITION)
        if (
            ctx.coord_sequence_config
            and ctx.coord
            and target_idx < total_targets
            and not self.wait_step_interval()
        ):
            return "stopped"
        return None

    def mouseClick(self, target, *args, timing=None, retry=None, step_info=None, **legacy_kwargs):
        """Execute a mouse click action.

        New-style call:
            mouseClick(ClickTarget(...), timing=ClickTiming(...), retry=ClickRetry(...))

        Legacy call (backward compatible):
            mouseClick(clickTimes, lOrR, img_path, reTry, *, step_info=..., ...)
        """
        # DESIGN: 向后兼容——旧调用方式以位置参数传入 clickTimes/lOrR/img_path/reTry，
        # 此处检测并转换为 dataclass，保持运行时行为不变。
        if not isinstance(target, ClickTarget):
            clickTimes = target
            lOrR = args[0] if len(args) > 0 else "left"
            img_path = args[1] if len(args) > 1 else ""
            reTry = args[2] if len(args) > 2 else 0
            target = ClickTarget(
                img_path=img_path,
                click_times=clickTimes,
                button=lOrR,
                point_limit_en=legacy_kwargs.pop("point_limit_en", False),
                point_limit_count=legacy_kwargs.pop("point_limit_count", 0),
            )
            timing = ClickTiming(
                move_jitter=legacy_kwargs.pop("move_jitter", None),
                hold_jitter=legacy_kwargs.pop("hold_jitter", None),
                spatial_jitter=legacy_kwargs.pop("spatial_jitter", None),
                curve_strength=legacy_kwargs.pop("curve_strength", None),
                trail_enabled=legacy_kwargs.pop("trail_enabled", None),
                restore_position=legacy_kwargs.pop("restore_position", False),
                image_click_config=legacy_kwargs.pop("image_click_config", None),
                coord_step_config=legacy_kwargs.pop("coord_step_config", None),
                coord_sequence_config=legacy_kwargs.pop("coord_sequence_config", None),
                background_click_config=legacy_kwargs.pop("background_click_config", None),
                auxiliary_click_config=legacy_kwargs.pop("auxiliary_click_config", None),
            )
            retry = ClickRetry(
                retry=reTry,
                confidence=legacy_kwargs.pop("task_conf", 0.8),
                use_gray=legacy_kwargs.pop("use_gray", True),
                cache_key=legacy_kwargs.pop("cache_key", None),
                search_regions=legacy_kwargs.pop("search_regions", None),
                allow_inverted=legacy_kwargs.pop("allow_inverted", False),
            )
        if timing is None:
            timing = ClickTiming()
        if retry is None:
            retry = ClickRetry()

        # Unpack dataclass fields into local names used by the method body.
        clickTimes = target.click_times
        lOrR = target.button
        img_path = target.img_path
        reTry = retry.retry
        cache_key = retry.cache_key
        task_conf = retry.confidence
        use_gray = retry.use_gray
        point_limit_en = target.point_limit_en
        point_limit_count = target.point_limit_count
        coord_step_config = timing.coord_step_config
        image_click_config = timing.image_click_config
        coord_sequence_config = timing.coord_sequence_config
        search_regions = retry.search_regions
        background_click_config = timing.background_click_config
        allow_inverted = retry.allow_inverted
        auxiliary_click_config = timing.auxiliary_click_config
        move_jitter = timing.move_jitter
        hold_jitter = timing.hold_jitter
        spatial_jitter = timing.spatial_jitter
        curve_strength = timing.curve_strength
        trail_enabled = timing.trail_enabled
        restore_position = timing.restore_position

        if step_info is None:
            step_info = {"step": 0, "loop": 0, "cmd": ""}
        coord = self.parse_coordinate(img_path)
        use_all_targets = (not coord and self.multi_target_mode == "全部匹配")
        try:
            point_limit_count = max(0, int(float(point_limit_count)))
        except Exception as exc:
            logger.debug("同点点击上限参数解析失败: %s", exc, exc_info=True)
            point_limit_count = 0
        point_limit_en = bool(point_limit_en) and not coord and point_limit_count > 0
        need_all_matches = not coord and (use_all_targets or point_limit_en)

        ctx = self._ClickContext(
            click_times=clickTimes, lOrR=lOrR, img_path=img_path, reTry=reTry,
            step_info=step_info, cache_key=cache_key, task_conf=task_conf,
            use_gray=use_gray, point_limit_en=point_limit_en,
            point_limit_count=point_limit_count, coord_step_config=coord_step_config,
            image_click_config=image_click_config, coord_sequence_config=coord_sequence_config,
            search_regions=search_regions, background_click_config=background_click_config,
            allow_inverted=allow_inverted, auxiliary_click_config=auxiliary_click_config,
            coord=coord, use_all_targets=use_all_targets,
            need_all_matches=need_all_matches, start_time=time.monotonic(),
            collect_policy=self._multi_collect_policy(use_all_targets),
            collect_started_at=time.monotonic(),
            move_jitter=move_jitter, hold_jitter=hold_jitter,
            spatial_jitter=spatial_jitter, curve_strength=curve_strength,
            trail_enabled=trail_enabled,
            restore_position=restore_position,
        )

        if reTry == -1 and self.timeout_val <= 0 and self.log_level >= 1:
            self.log(
                f"    -> 步{step_info['step']}: 无限重试且无超时，将持续等待直到找到目标或手动停止",
                LOG_RECOGNITION,
            )

        while True:
            if self.check_stop_flag():
                return "stopped"
            if self.timeout_val > 0 and (time.monotonic() - ctx.start_time > self.timeout_val):
                if self.log_level >= 1:
                    self.log(f"<font color='orange'>    [超时] 循环#{step_info['loop']} 步{step_info['step']}: 等待目标超时</font>", LOG_STEP)
                return "timeout"

            resolved = self._resolve_click_targets(ctx)
            if isinstance(resolved, str):
                return resolved
            locations, find_time, coord_state, seq_state = resolved

            if locations:
                if not coord:
                    self.reset_recognition_miss(img_path, step_info)
                click_locations = self._order_multi_targets(locations, ctx)
                if click_locations is None:
                    # FR-20261008-903: same contract as the empty-result branch
                    # below (FR-20261008-401) — the pure-CPU region/same-point
                    # filters may finish after another thread set the stop flag;
                    # reporting that as an ordinary "not_found" marks the step
                    # failed and makes the stop look like a recognition miss.
                    if self.check_stop_flag():
                        return "stopped"
                    return "not_found"
                return self._execute_click_sequence(click_locations, ctx, find_time, coord_state, seq_state)
            else:
                # FR-20261008-401: a native search cancelled by the user's stop
                # returns an empty result set. Reporting that as an ordinary
                # "not_found" marks the step failed (consecutive_failures /
                # failed_branch) and prints 「未找到」 instead of 「已停止」, so the
                # stop looks like a recognition miss. Re-check the stop flag before
                # recording a miss or reporting not_found.
                if self.check_stop_flag():
                    return "stopped"
                if not coord:
                    self.record_recognition_miss(img_path, step_info)
                if reTry != -1:
                    if self.log_level >= 1:
                        self.log(f"<font color='orange'>    [未找到] 循环#{step_info['loop']} 步{step_info['step']}: 未能识别到指定目标图片</font>", LOG_RECOGNITION)
                    return "not_found"
                else:
                    if not ctx.waiting_logged and self.log_level >= 1:
                        self.log("    -> 未发现目标，进入持续监听等待状态...", LOG_RECOGNITION)
                        ctx.waiting_logged = True
                    extra_delay = self.adaptive_extra_delay(img_path, step_info)
                    wake_context = None
                    if extra_delay > 0.0:
                        wake_context = self.recognition_wake_context(
                            img_path,
                            cache_key,
                            task_conf,
                            use_gray,
                            search_regions,
                            allow_fast_probe=not need_all_matches,
                            allow_inverted=allow_inverted,
                        )
                    if not self.wait_recognition_interval(extra_delay, wake_context):
                        return "stopped"
                    continue

    def mouseDrag(
        self,
        button,
        val,
        step_info,
        recorded_duration=0.0,
        *,
        move_jitter=None,
        curve_strength=None,
        trail_enabled=None,
    ):
        """在输入锁保护下执行一次鼠标拖拽。

        Args:
            button: 拖拽按键，"left" 或 "right"。
            val: 拖拽坐标文本，格式为 "x1,y1 -> x2,y2"。
            step_info: 步骤上下文信息，用于日志。
            recorded_duration: 录制时记录的拖拽时长，用于回放。
            move_jitter: 本步骤移动抖动覆盖；None 沿用全局值。
            curve_strength: 本步骤移动曲线覆盖；None 沿用全局值。
            trail_enabled: 本步骤视觉拖影覆盖；None 沿用全局值。

        Returns:
            结果字符串："success"、"stopped" 或 "error"。
        """
        with self._input_action_lock:
            return self._mouse_drag_impl(
                button,
                val,
                step_info,
                recorded_duration,
                move_jitter=move_jitter,
                curve_strength=curve_strength,
                trail_enabled=trail_enabled,
            )

    def _mouse_drag_impl(
        self,
        button,
        val,
        step_info,
        recorded_duration=0.0,
        *,
        move_jitter=None,
        curve_strength=None,
        trail_enabled=None,
    ):
        """Execute a drag from "x1,y1" to "x2,y2" parsed from val.

        Args:
            button: Drag button, "left" or "right".
            val: Drag coordinate text "x1,y1 -> x2,y2".
            step_info: Step context for logging.
            recorded_duration: Recorded drag duration for replay.
            move_jitter: Per-step move jitter override; None uses global.
            curve_strength: Per-step curve override; None uses global.
            trail_enabled: Per-step visual trail override; None uses global.

        Returns:
            Outcome string: "success", "stopped", or "error".
        """
        try:
            x1, y1, x2, y2 = parse_drag_coordinates(val)
        except (ValueError, Exception):
            logger.warning("拖拽坐标格式异常（应为 'x1,y1 -> x2,y2'）: %s", val)
            if self.log_enabled(LOG_CRITICAL, critical=True):
                self.log(
                    f"<font color='red'>    [错误] 循环#{step_info['loop']} "
                    f"步{step_info['step']}: 拖拽坐标格式错误，应为 "
                    "x1,y1 -> x2,y2</font>",
                    LOG_CRITICAL,
                    critical=True,
                )
            return "error"

        if self.log_level >= 1:
            self.log(f"    -> 正在从 ({x1},{y1}) 拖拽到 ({x2},{y2})", LOG_COORDINATES)

        move_range = (
            self.move_duration_jitter if move_jitter is None else move_jitter
        )
        if not self._move_mouse_interruptibly(
            x1,
            y1,
            max(0.0, self.move_duration + self._jitter_delay(move_range)),
            curve_strength=curve_strength,
            trail_enabled=trail_enabled,
        ):
            return "stopped"
        pressed = False
        try:
            pressed = self._tracked_mouse_down(button)
            if not pressed:
                return "stopped"
            hold = self._bounded_action_duration(self.click_hold, maximum=10.0)
            if not self._wait_interruptibly(hold):
                return "stopped"
            replay_duration = self._bounded_action_duration(recorded_duration or 0.0)
            replay_duration /= max(float(self.playback_speed or 1.0), 0.1)
            replay_duration = min(replay_duration, self._MAX_ACTION_DURATION_SECONDS)
            if not self._move_mouse_interruptibly(
                x2,
                y2,
                max(
                    replay_duration,
                    self.move_duration + self._jitter_delay(move_range),
                    0.3,
                ),
                curve_strength=curve_strength,
                trail_enabled=trail_enabled,
            ):
                return "stopped"
        finally:
            if pressed:
                self._tracked_mouse_up(button)
        self.report_click_indicator(x2, y2, "拖拽结束")
        return "success"

    def _click_from_context(self, context, times, button):
        """Build click dataclasses from a step context and dispatch mouseClick.

        Args:
            context: StepActionContext for the click step.
            times: Number of clicks.
            button: Button, "left" or "right".

        Returns:
            Outcome string from mouseClick.
        """
        task = context.task if isinstance(context.task, dict) else {}
        image_target = self.parse_coordinate(context.value) is None
        background_click_config = None
        if config_bool(task.get("background_click_en", False)):
            binding = task.get("background_click_binding", {})
            if isinstance(binding, dict) and binding:
                background_click_config = {"binding": dict(binding)}
        return self.mouseClick(
            ClickTarget(
                img_path=context.value,
                click_times=times,
                button=button,
                point_limit_en=context.point_limit_enabled,
                point_limit_count=context.point_limit_count,
            ),
            timing=ClickTiming(
                move_jitter=context.move_jitter,
                hold_jitter=context.hold_jitter,
                spatial_jitter=context.spatial_jitter,
                curve_strength=context.curve_strength,
                trail_enabled=context.trail_enabled,
                restore_position=context.restore_position,
                image_click_config=context.image_click,
                coord_step_config=context.coordinate_step,
                coord_sequence_config=context.coordinate_sequence,
                background_click_config=background_click_config,
                auxiliary_click_config=self.click_supplement_options(
                    task,
                    image_target=image_target,
                    trail_enabled=context.trail_enabled,
                ),
            ),
            retry=ClickRetry(
                retry=context.retry,
                confidence=context.confidence,
                use_gray=context.use_gray,
                cache_key=context.cache_key,
                search_regions=context.search_regions,
                allow_inverted=config_bool(task.get("inverse_match_en", False)),
            ),
            step_info=context.step_info,
        )

    @action_handler(1.0)
    def _execute_left_click(self, context):
        """Handle command 1.0: single left click."""
        return self._click_from_context(context, 1, "left")

    @action_handler(2.0)
    def _execute_left_double_click(self, context):
        """Handle command 2.0: double left click."""
        return self._click_from_context(context, 2, "left")

    @action_handler(3.0)
    def _execute_right_click(self, context):
        """Handle command 3.0: single right click."""
        return self._click_from_context(context, 1, "right")

    def _resolve_drag_target_image(self, context):
        """Resolve drag end-point via image recognition when drag_target_img is set.

        Returns (end_x, end_y) on success, None if drag_target_img is empty
        (caller should use original coordinates), "error" when the image file
        does not exist, or "not_found" when the image is not matched on screen.
        """
        task = context.task or {}
        drag_target_img = str(task.get("drag_target_img", "") or "").strip()
        if not drag_target_img:
            return None
        if not os.path.isfile(drag_target_img):
            if self.log_enabled(LOG_CRITICAL, critical=True):
                self.log(
                    f"<font color='red'>    [错误] 循环#{context.step_info['loop']} "
                    f"步{context.step_info['step']}: 拖拽终点图片不存在: "
                    f"{value_metadata(drag_target_img)}</font>",
                    LOG_CRITICAL,
                    critical=True,
                    private_msg=(
                        f"<font color='red'>    [错误] 拖拽终点图片不存在: "
                        f"{private_debug_value(drag_target_img)}</font>"
                    ),
                )
            return "error"
        confidence = float(context.confidence)
        use_gray = bool(context.use_gray)
        allow_inverted = config_bool(task.get("inverse_match_en", False))
        # FR-DRAG-SCALE / FR-SCOPE013-006: 拖拽键必须由共享构造函数生成，
        # 否则运行期与预计算处在不同键空间，预计算的倍率模板永不命中，
        # OpenCV 只搜 1.0 倍率。旧的本地 f-string 还把 confidence 混进了键里，
        # 同一张图在不同置信度下会重复占用缓存条目。
        cache_key = self.drag_target_cache_key(task)
        self.scale_options_cache.setdefault(
            str(cache_key), self.drag_scale_options(task)
        )
        if self.log_level >= 1:
            self.log(
                "    -> 正在识别拖拽终点图片...",
                LOG_RECOGNITION,
                private_msg=(
                    f"    -> 正在识别拖拽终点图片: "
                    f"{private_debug_value(drag_target_img)}"
                ),
            )
        found = self.find_target_optimized(
            drag_target_img,
            cache_key,
            confidence,
            use_gray,
            context.search_regions,
            allow_inverted,
        )
        if not found:
            # FR-20261008-401: distinguish a user stop from a real miss.
            if self.check_stop_flag():
                return "stopped"
            if self.log_enabled(LOG_CRITICAL, critical=True):
                self.log(
                    f"<font color='red'>    [失败] 循环#{context.step_info['loop']} "
                    f"步{context.step_info['step']}: 未找到拖拽终点图片</font>",
                    LOG_CRITICAL,
                    critical=True,
                    private_msg=(
                        f"<font color='red'>    [失败] 未找到拖拽终点图片: "
                        f"{private_debug_value(drag_target_img)}</font>"
                    ),
                )
            return "not_found"
        end_x, end_y = float(found[0]), float(found[1])
        drag_target_point = str(task.get("drag_target_point", "") or "").strip()
        if drag_target_point:
            image_click_config = {"rx": 0.5, "ry": 0.5}
            parts = drag_target_point.replace("，", ",").split(",")
            if len(parts) == 2:
                image_click_config["rx"] = _bounded_unit_offset(parts[0].strip())
                image_click_config["ry"] = _bounded_unit_offset(parts[1].strip())
            end_x, end_y = self.adjusted_image_click_point(
                drag_target_img, found, image_click_config
            )
        if self.log_level >= 1:
            self.log(
                f"    -> 拖拽终点图片命中: ({int(end_x)}, {int(end_y)})",
                LOG_RECOGNITION,
            )
        return end_x, end_y

    def _resolve_drag_source_image(self, context):
        """Resolve drag start-point via image recognition when drag_source_img is set.

        Returns (start_x, start_y) on success, None if drag_source_img is empty
        (caller should use original coordinates), "error" when the image file
        does not exist, or "not_found" when the image is not matched on screen.
        """
        task = context.task or {}
        drag_source_img = str(task.get("drag_source_img", "") or "").strip()
        if not drag_source_img:
            return None
        if not os.path.isfile(drag_source_img):
            if self.log_enabled(LOG_CRITICAL, critical=True):
                self.log(
                    f"<font color='red'>    [错误] 循环#{context.step_info['loop']} "
                    f"步{context.step_info['step']}: 拖拽起点图片不存在: "
                    f"{value_metadata(drag_source_img)}</font>",
                    LOG_CRITICAL,
                    critical=True,
                    private_msg=(
                        f"<font color='red'>    [错误] 拖拽起点图片不存在: "
                        f"{private_debug_value(drag_source_img)}</font>"
                    ),
                )
            return "error"
        confidence = float(context.confidence)
        use_gray = bool(context.use_gray)
        allow_inverted = config_bool(task.get("inverse_match_en", False))
        # FR-DRAG-SCALE / FR-SCOPE013-006: 同上，改用共享构造函数。
        cache_key = self.drag_source_cache_key(task)
        self.scale_options_cache.setdefault(
            str(cache_key), self.drag_scale_options(task)
        )
        if self.log_level >= 1:
            self.log(
                "    -> 正在识别拖拽起点图片...",
                LOG_RECOGNITION,
                private_msg=(
                    f"    -> 正在识别拖拽起点图片: "
                    f"{private_debug_value(drag_source_img)}"
                ),
            )
        found = self.find_target_optimized(
            drag_source_img,
            cache_key,
            confidence,
            use_gray,
            context.search_regions,
            allow_inverted,
        )
        if not found:
            # FR-20261008-401: distinguish a user stop from a real miss.
            if self.check_stop_flag():
                return "stopped"
            if self.log_enabled(LOG_CRITICAL, critical=True):
                self.log(
                    f"<font color='red'>    [失败] 循环#{context.step_info['loop']} "
                    f"步{context.step_info['step']}: 未找到拖拽起点图片</font>",
                    LOG_CRITICAL,
                    critical=True,
                    private_msg=(
                        f"<font color='red'>    [失败] 未找到拖拽起点图片: "
                        f"{private_debug_value(drag_source_img)}</font>"
                    ),
                )
            return "not_found"
        start_x, start_y = float(found[0]), float(found[1])
        drag_source_point = str(task.get("drag_source_point", "") or "").strip()
        if drag_source_point:
            image_click_config = {"rx": 0.5, "ry": 0.5}
            parts = drag_source_point.replace("，", ",").split(",")
            if len(parts) == 2:
                image_click_config["rx"] = _bounded_unit_offset(parts[0].strip())
                image_click_config["ry"] = _bounded_unit_offset(parts[1].strip())
            start_x, start_y = self.adjusted_image_click_point(
                drag_source_img, found, image_click_config
            )
        if self.log_level >= 1:
            self.log(
                f"    -> 拖拽起点图片命中: ({int(start_x)}, {int(start_y)})",
                LOG_RECOGNITION,
            )
        return start_x, start_y

    def _execute_drag_with_target_image(self, button, context):
        """Shared drag execution with optional image-based start/end points."""
        # Resolve start point
        source_resolved = self._resolve_drag_source_image(context)
        if isinstance(source_resolved, str):
            return source_resolved  # "error" or "not_found"

        # Resolve end point
        target_resolved = self._resolve_drag_target_image(context)
        if isinstance(target_resolved, str):
            return target_resolved  # "error" or "not_found"

        # Build the coordinate string
        if source_resolved is not None:
            start_x, start_y = source_resolved
            start_part = f"{int(start_x)},{int(start_y)}"
        else:
            # Use original start from val
            try:
                parts = str(context.value).split("->")
                start_part = parts[0].strip()
            except Exception as exc:
                logger.warning("拖拽起点坐标解析失败: %s", exc, exc_info=True)
                start_part = str(context.value)

        if target_resolved is not None:
            end_x, end_y = target_resolved
            end_part = f"{int(end_x)},{int(end_y)}"
        else:
            # Use original end from val
            try:
                parts = str(context.value).split("->")
                end_part = parts[1].strip()
            except Exception as exc:
                logger.warning("拖拽终点坐标解析失败: %s", exc, exc_info=True)
                end_part = "0,0"

        modified_value = f"{start_part} -> {end_part}"
        return self.mouseDrag(
            button,
            modified_value,
            context.step_info,
            (context.task or {}).get("recorded_duration", 0.0),
            move_jitter=context.move_jitter,
            curve_strength=context.curve_strength,
            trail_enabled=context.trail_enabled,
        )

    @action_handler(10.0)
    def _execute_left_drag(self, context):
        """Handle command 10.0: left-button drag."""
        return self._execute_drag_with_target_image("left", context)

    @action_handler(11.0)
    def _execute_right_drag(self, context):
        """Handle command 11.0: right-button drag."""
        return self._execute_drag_with_target_image("right", context)

    @action_handler(8.0)
    def _execute_hover(self, context):
        """Handle command 8.0: hover over a coordinate or recognized image.

        Args:
            context: StepActionContext for the hover step.

        Returns:
            Outcome string: "success", "not_found", or "stopped".
        """
        coordinate = self.parse_coordinate(context.value)
        if coordinate:
            location = (coordinate[0], coordinate[1], 1.0)
            find_time = 0.0
        else:
            find_start = time.monotonic()
            location = self.find_target_optimized(
                context.value,
                context.cache_key,
                context.confidence,
                context.use_gray,
                context.search_regions,
                config_bool((context.task or {}).get("inverse_match_en", False)),
            )
            find_time = time.monotonic() - find_start
        if not location:
            # FR-20261008-401: distinguish a user stop from a real miss.
            if self.check_stop_flag():
                return "stopped"
            if self.log_level >= 1:
                self.log(
                    f"<font color='orange'>    [异常] 循环#{context.step_info['loop']} 步"
                    f"{context.step_info['step']}: 悬停失败，未识别到目标</font>",
                    LOG_RECOGNITION,
                )
            return "not_found"
        x, y, scale = location[0], location[1], location[2]
        if self.verbose_log_enabled(LOG_TIMING):
            self.log(
                "    <font color='gray'>=> 底层找图耗时 "
                f"{find_time:.3f}s，缩放: {format_scale(scale)}x</font>",
                LOG_TIMING,
            )
        if self.log_level >= 1:
            self.log(f"    -> 已悬停在坐标 ({int(x)}, {int(y)})", LOG_COORDINATES)
        with self._input_action_lock:
            if not self._move_mouse_interruptibly(
                x,
                y,
                max(0.0, self.move_duration + self._jitter_delay(self.move_duration_jitter)),
                trail_enabled=context.trail_enabled,
            ):
                return "stopped"
        self.report_click_indicator(x, y, "悬停")
        return "success"
