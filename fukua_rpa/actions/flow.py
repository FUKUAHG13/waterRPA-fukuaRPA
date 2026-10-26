# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOperatorIssue=false
"""Flow-control actions and the ``execute_task_once`` dispatcher.

Mechanical extraction from the former monolithic ``engine_actions.py``.
"""

import contextlib
import logging
import math
import random
import time
from collections.abc import Callable

from fukua_rpa import plugin_actions
from fukua_rpa.commands import CLICK_COMMANDS, DRAG_COMMANDS
from fukua_rpa.constants import (
    AUTO_CLICKER_COUNT_MAX,
    TASK_TYPE_AUTO_CLICKER,
    TASK_TYPE_EXPRESSION,
    TASK_TYPE_PLUGIN_ACTION,
    TASK_TYPE_SET_VARIABLE,
    TASK_TYPE_UNTIL,
)
from fukua_rpa.expressions import ExpressionError
from fukua_rpa.log_policy import (
    LOG_ACTION,
    LOG_CRITICAL,
    LOG_FLOW,
    LOG_PARAMETERS,
)
from fukua_rpa.params import TaskOnceParams
from fukua_rpa.privacy import (
    private_debug_value,
    private_error_text,
    safe_error_name,
    sensitive_text_metadata,
    value_metadata,
)
from fukua_rpa.random_distribution import (
    normalize_concentration,
    sample_interval_seconds,
    sample_pointer_offset,
)
from fukua_rpa.script_prompt import ScriptPromptRequest
from fukua_rpa.task_model import config_bool

from .shared import (
    ACTION_HANDLERS,
    StepActionContext,
    _parse_jitter_value,
    _parse_optional_float,
    action_handler,
)

logger = logging.getLogger(__name__)


def _mouse_trail_override(task) -> bool | None:
    """Normalize a per-step trail mode; None means follow the global setting."""

    mode = str((task or {}).get("mouse_trail_mode", "inherit") or "inherit")
    mode = mode.strip().lower()
    if mode == "on":
        return True
    if mode == "off":
        return False
    return None


def _wav_duration_seconds(path: str) -> float | None:
    """Return the duration of a WAV file in seconds, or None if unknown."""

    try:
        import wave

        with wave.open(path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return None
            return frames / float(rate)
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取 wav 时长失败: %s", exc, exc_info=True)
        return None


class ActionsFlowMixin:
    """Action primitives for the flow family."""

    def execute_task_once(self, params: TaskOnceParams):
        """执行单个步骤的一次动作分发。

        按指令码选择已注册的动作处理器执行，并记录动作耗时与调用计数。
        具体执行细节见 _execute_task_once_impl。

        Args:
            params: 不可变参数包，包含以下字段：
                cmd: 指令码。
                val: 指令参数值。
                retry: 重试次数（-1 表示无限重试）。
                step_info: 步骤上下文信息，用于日志。
                cache_key: 识别缓存键。
                task_conf: 识别置信度。
                use_gray: 是否灰度识别。
                point_limit_en: 是否启用同点点击上限。
                point_limit_count: 同点点击上限次数。
                coord_step_config: 坐标步进配置。
                image_click_config: 图片点击点配置。
                task: 步骤配置字典。
                coord_sequence_config: 自定义点位序列配置。
                search_regions: 识别区域列表。

        Returns:
            执行结果字符串，如 "success"、"not_found"、"error"、"stopped" 等。
        """
        started_ns = time.perf_counter_ns()
        try:
            return self._execute_task_once_impl(params)
        finally:
            try:
                command_key = f"{float(params.cmd):g}"
            except (TypeError, ValueError) as exc:
                logger.debug("指令码格式化失败: %s", exc, exc_info=True)
                command_key = "invalid"
            self.performance.observe_since("action.total", started_ns)
            self.performance.observe_since(f"action.command.{command_key}", started_ns)
            self.performance.increment("action.calls")

    def _execute_task_once_impl(self, ctx: TaskOnceParams):
        """执行单个步骤的一次动作（内部实现，接收聚合上下文）。

        见 execute_task_once 的公共入口与 TaskOnceParams 定义。
        """
        try:
            command = float(ctx.cmd)
        except (TypeError, ValueError) as exc:
            logger.debug("指令码解析失败: %s", exc, exc_info=True)
            command = -1.0
        # 步骤级随机延迟覆盖（仅鼠标点击与拖拽）。覆盖值作为局部变量沿执行
        # 路径向下传递，不临时改写实例属性，避免与读取同一属性的附属点击
        # 线程产生竞态。
        move_jitter = None
        hold_jitter = None
        spatial_jitter = None
        curve_strength = None
        trail_enabled = None
        restore_position = False
        key_hold_duration = None
        # DESIGN: move/hold jitter applicability is determined by command code
        # (CLICK_COMMANDS | DRAG_COMMANDS) because these two random-delay fields
        # do not have independent settings capabilities. See bulk_task_settings.py.
        if command in CLICK_COMMANDS or command in DRAG_COMMANDS:
            move_jitter = _parse_jitter_value(
                (ctx.task or {}).get("move_jitter")
            )
            hold_jitter = _parse_jitter_value(
                (ctx.task or {}).get("click_hld_jitter")
            )
            # 移动曲线同时适用于点击和拖拽，留空则回退全局。
            curve_strength = _parse_optional_float(
                (ctx.task or {}).get("move_curve_strength"), 1.0
            )
        if command in CLICK_COMMANDS:
            # 落点偏移只适用于真实点击；拖拽起终点必须保持用户设定坐标。
            spatial_jitter = _parse_optional_float(
                (ctx.task or {}).get("click_spatial_jitter"), 20
            )
        if command in CLICK_COMMANDS or command in DRAG_COMMANDS or command == 8.0:
            trail_enabled = _mouse_trail_override(ctx.task)
        if command in CLICK_COMMANDS:
            # “点击后返回”仅对单击/双击有意义（拖拽已有自身起点）。
            restore_position = config_bool((ctx.task or {}).get("click_return", False))
        if command == 7.0:
            # 系统按键：按住时长覆盖（秒），留空则回退全局 key_hold_duration。
            key_hold_duration = _parse_optional_float(
                (ctx.task or {}).get("key_hold_duration"), 3600
            )
        context = StepActionContext(
            command=command,
            value=ctx.val,
            retry=ctx.retry,
            step_info=ctx.step_info,
            cache_key=ctx.cache_key,
            confidence=ctx.task_conf,
            use_gray=ctx.use_gray,
            point_limit_enabled=ctx.point_limit_en,
            point_limit_count=ctx.point_limit_count,
            coordinate_step=ctx.coord_step_config,
            image_click=ctx.image_click_config,
            task=ctx.task,
            coordinate_sequence=ctx.coord_sequence_config,
            search_regions=ctx.search_regions,
            move_jitter=move_jitter,
            hold_jitter=hold_jitter,
            spatial_jitter=spatial_jitter,
            curve_strength=curve_strength,
            trail_enabled=trail_enabled,
            restore_position=restore_position,
            key_hold_duration=key_hold_duration,
        )
        handler = ACTION_HANDLERS.get(command)
        if handler is None:
            if self.log_enabled(LOG_CRITICAL, critical=True):
                self.log(
                    f"<font color='red'>    [严重异常] 循环#{ctx.step_info['loop']} 步"
                    f"{ctx.step_info['step']}: 未注册的指令类型 {ctx.cmd}</font>",
                    LOG_CRITICAL,
                    critical=True,
                )
            return "error"
        try:
            result = handler(self, context)
        except Exception as error:
            if self.log_enabled(LOG_CRITICAL, critical=True):
                self.log(
                    f"<font color='red'>    [严重异常] 循环#{ctx.step_info['loop']} 步"
                    f"{ctx.step_info['step']}: 执行崩溃 -> {safe_error_name(error)}</font>",
                    LOG_CRITICAL,
                    critical=True,
                    private_msg=(
                        f"<font color='red'>    [严重异常] 循环#{ctx.step_info['loop']} 步"
                        f"{ctx.step_info['step']}: 执行崩溃 -> {private_error_text(error)}</font>"
                    ),
                )
            return "error"
        # S4: a handler returning None (or any non-status value) is ambiguous
        # and must not be silently treated as a successful step or a graceful
        # stop. Coerce it to an explicit error so the attempt fails loudly
        # instead of masking a bug. (The deliberate stop sentinel from the
        # pre-execute wait path returns None upstream of this dispatch and is
        # handled separately as a stop.)
        if result is None or not isinstance(result, str):
            if self.log_enabled(LOG_CRITICAL, critical=True):
                self.log(
                    f"<font color='red'>    [严重异常] 循环#{ctx.step_info['loop']} 步"
                    f"{ctx.step_info['step']}: 动作处理器返回了非预期结果 "
                    f"({type(result).__name__})，已按异常处置</font>",
                    LOG_CRITICAL,
                    critical=True,
                )
            return "error"
        return result

    @action_handler(TASK_TYPE_UNTIL)
    def _execute_until_condition(self, context):
        """Handle the until-loop command: evaluate loop-exit conditions.

        Args:
            context: StepActionContext for the until step.

        Returns:
            Outcome string from execute_until_conditions.
        """
        return self.execute_until_conditions(
            context.task or {}, context.step_info, context.use_gray
        )

    @action_handler(TASK_TYPE_SET_VARIABLE)
    def _execute_set_variable(self, context):
        """Handle the set-variable command: assign a runtime variable.

        Args:
            context: StepActionContext for the set-variable step.

        Returns:
            Outcome string: "success" or "error".
        """
        try:
            name, value = self.set_runtime_variable(
                context.value, context.step_info
            )
        except ExpressionError as error:
            if self.log_level >= 0:
                self.log(
                    f"<font color='red'>    [变量错误] 第 {context.step_info['step']} 步：{safe_error_name(error)}</font>",
                    LOG_CRITICAL,
                    critical=True,
                    private_msg=(
                        f"<font color='red'>    [变量错误] 第 {context.step_info['step']} 步："
                        f"{private_error_text(error)}</font>"
                    ),
                )
            return "error"
        if self.log_level >= 1:
            self.log(f"    -> 运行变量 {name} 已更新", LOG_ACTION)
        if self.log_level >= 2:
            self.log(
                f"    <font color='gray'>变量结果：{name}（{value_metadata(value)}）</font>",
                LOG_PARAMETERS,
                private_msg=(
                    f"    <font color='gray'>变量结果：{name} = "
                    f"{private_debug_value(value)}</font>"
                ),
            )
        return "success"

    @action_handler(TASK_TYPE_EXPRESSION)
    def _execute_expression_condition(self, context):
        """Handle the expression command: evaluate a boolean runtime expression.

        Args:
            context: StepActionContext for the expression step.

        Returns:
            "condition_true", "condition_false", or "error".
        """
        try:
            result = self.evaluate_runtime_expression(
                context.value, context.step_info
            )
        except ExpressionError as error:
            if self.log_level >= 0:
                self.log(
                    f"<font color='red'>    [表达式错误] 第 {context.step_info['step']} 步：{safe_error_name(error)}</font>",
                    LOG_CRITICAL,
                    critical=True,
                    private_msg=(
                        f"<font color='red'>    [表达式错误] 第 {context.step_info['step']} 步："
                        f"{private_error_text(error)}</font>"
                    ),
                )
            return "error"
        matched = bool(result)
        if self.log_level >= 1:
            self.log(
                "    -> 表达式判断结果：" + ("满足" if matched else "不满足"),
                LOG_FLOW,
            )
        return "condition_true" if matched else "condition_false"

    @action_handler(12.0)
    def _execute_message(self, context):
        """Handle command 12.0: show a blocking prompt dialog.

        Args:
            context: StepActionContext whose value is the prompt body.

        Returns:
            Outcome string: "success", "stopped", or "error".
        """
        if self.log_level >= 1:
            self.log(
                f"    -> 触发弹窗提醒（正文 {len(str(context.value))} 个字符）",
                LOG_ACTION,
                private_msg=(
                    "    -> 触发弹窗提醒，正文："
                    f"{private_debug_value(context.value)}"
                ),
            )
        request = ScriptPromptRequest(str(context.value))
        callback = getattr(self, "callback_prompt", None)
        if callback is None:
            self.log(
                "    [弹窗错误] 当前执行入口没有可用的界面对话框回调。",
                LOG_CRITICAL,
                critical=True,
            )
            return "error"
        with self._prompt_lock:
            self._active_prompt_request = request
        try:
            callback(request)
            while not request.done.wait(0.05):
                if self.check_stop_flag():
                    request.cancel()
                    return "stopped"
            if request.cancelled or self.check_stop_flag():
                return "stopped"
            return "success" if request.acknowledged else "error"
        finally:
            with self._prompt_lock:
                if self._active_prompt_request is request:
                    self._active_prompt_request = None

    @action_handler(13.0)
    def _execute_stop(self, context):
        """Handle command 13.0: request the script to stop.

        Args:
            context: StepActionContext whose value is an optional note.

        Returns:
            Always "success".
        """
        if self.log_level >= 1:
            self.log(
                f"    -> 触发停止指令，脚本即将终止（备注 {sensitive_text_metadata(context.value)}）",
                LOG_ACTION,
                private_msg=(
                    "    -> 触发停止指令，脚本即将终止，备注："
                    f"{private_debug_value(context.value)}"
                ),
            )
        self.stop()
        return "success"

    @action_handler(14.0)
    def _execute_sound(self, context):
        """Handle command 14.0: play a sound alert.

        Args:
            context: StepActionContext for the sound step.

        Returns:
            Always "success".
        """
        if self.log_level >= 0:
            self.log(
                f"<br><font color='#00BCD4' size='4'><b>声音提示（备注 {sensitive_text_metadata(context.value)}）</b></font><br>",
                LOG_ACTION,
                private_msg=(
                    "<br><font color='#00BCD4' size='4'><b>声音提示（备注 "
                    f"{private_debug_value(context.value)}）</b></font><br>"
                ),
            )
        task = context.task if isinstance(context.task, dict) else {}
        # 每步音频优先于全局默认；每步播放时长同理，未设置时回退全局默认值。
        per_step_audio = (str(task.get("sound_alert_audio_path", "") or "").strip())
        global_audio = (getattr(self, "sound_alert_audio_path", "") or "").strip()
        audio_path = per_step_audio or global_audio
        per_step_seconds_text = str(
            task.get("sound_alert_play_seconds", "") or ""
        ).strip()
        # FR-20261008-902 / REG-20261008-908: parsed_seconds 的计算与原 if/else
        # 逐值等价（有步骤级文本则解析、否则 None）；后续分支为有意行为变更
        # （步骤级非法文本回退全局值而非旧版 0.0，见下方注释）。三元形态满足
        # ruff SIM108——该命令由 pre-commit 提交门禁与
        # test_precommit_ruff_hook_command_is_executable 真实执行守护。
        parsed_seconds = (
            _parse_optional_float(per_step_seconds_text, 600)
            if per_step_seconds_text
            else None
        )
        if parsed_seconds is not None:
            play_seconds = parsed_seconds
        else:
            # 全局值 0 是**合法输入**（表示不限播放时长），`or 30.0` 会把它
            # 悄悄改写成 30 秒。只在属性缺失或为 None 时才回落默认值。
            # FR-20261008-902: 步骤级非法文本同样走这里回退全局值——
            # _parse_optional_float 的契约就是 None 让调用方回落全局；
            # 把 None 映射为 0.0 会构成静默降级并覆盖全局设置。
            global_seconds = getattr(self, "sound_alert_play_seconds", None)
            play_seconds = 30.0 if global_seconds is None else float(global_seconds)
        self._play_sound_alert(audio_path, max_seconds=play_seconds)
        return "success"

    def _play_sound_alert(self, audio_path: str, max_seconds: float | None = None) -> None:
        """Play a local audio file for the sound-alert step.

        Falls back to the system beep when no path is configured or the file
        cannot be played. Windows-only playback primitives are wrapped so a
        missing codec never crashes the run. ``max_seconds`` truncates playback
        when the audio is longer than that many seconds (0/None means no limit).
        """

        try:
            import os
            import winsound

            if audio_path and os.path.isfile(audio_path):
                lowered = audio_path.lower()
                if lowered.endswith(".wav"):
                    duration = _wav_duration_seconds(audio_path)
                    if (
                        max_seconds in (None, 0)
                        or duration is None
                        or duration <= max_seconds
                    ):
                        try:
                            winsound.PlaySound(
                                audio_path,
                                winsound.SND_FILENAME | winsound.SND_ASYNC,
                            )
                            return
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("wav 播放失败，回退系统蜂鸣: %s", exc)
                    else:
                        # 截断：异步播放 max_seconds 秒后停止。
                        try:
                            winsound.PlaySound(
                                audio_path,
                                winsound.SND_FILENAME | winsound.SND_ASYNC,
                            )
                            self._wait_interruptibly(max_seconds)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("wav 截断播放失败: %s", exc)
                        finally:
                            with contextlib.suppress(Exception):  # noqa: BLE001
                                winsound.PlaySound(None, 0)
                        return
                else:
                    if self._play_audio_via_winmm(audio_path, max_seconds):
                        return
            winsound.MessageBeep(0x00000040)
        except Exception as exc:  # noqa: BLE001
            logger.debug("声音提示播放失败: %s", exc, exc_info=True)

    def _play_audio_via_winmm(self, audio_path: str, max_seconds: float | None = None) -> bool:
        """Play mp3/other formats through the Windows MCI decoder.

        Returns True on a successful start. When ``max_seconds`` is set and the
        audio is longer than that, playback is stopped after ``max_seconds``
        seconds; otherwise it blocks until the clip finishes. The alias is
        always closed afterwards.
        """

        try:
            import ctypes
            from ctypes import wintypes

            winmm = ctypes.windll.winmm
            winmm.mciSendStringW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.LPWSTR,
                wintypes.UINT,
                wintypes.HANDLE,
            ]
            winmm.mciSendStringW.restype = wintypes.DWORD
            alias = "fukuaRPA_alert"
            if winmm.mciSendStringW(
                f'open "{audio_path}" alias {alias}', None, 0, 0
            ) != 0:
                return False
            try:
                length_ms = 0
                buf = ctypes.create_unicode_buffer(64)
                if (
                    winmm.mciSendStringW(
                        f"status {alias} length", buf, len(buf), 0
                    )
                    == 0
                ):
                    try:
                        length_ms = int(buf.value)
                    except ValueError:
                        length_ms = 0
                duration = length_ms / 1000.0 if length_ms > 0 else None
                # INVARIANT: 一律**异步**启动（不带 wait），再由分片轮询收尾。
                # 早前用 "play <alias> wait" 会阻塞到音频自然结束，期间急停无效，
                # 且无长度上限时会把运行循环整段占住。
                self._play_started_at = time.monotonic()
                winmm.mciSendStringW(f"play {alias}", None, 0, 0)
                self._wait_mci_playback(winmm, alias, ctypes, max_seconds, duration)
            finally:
                winmm.mciSendStringW(f"close {alias}", None, 0, 0)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("winmm 播放失败: %s", exc, exc_info=True)
            return False

    #: _wait_mci_playback 的单次轮询间隔（秒）。足够细以保证急停及时生效。
    _MCI_POLL_INTERVAL_SECONDS = 0.02

    def _wait_mci_playback(
        self,
        winmm,
        alias: str,
        ctypes_mod,
        max_seconds,
        duration: float | None,
    ) -> None:
        """Poll MCI playback until it ends, the cap is hit, or a stop is requested.

        FR-SCOPE003-001: 播放已异步启动，这里只负责有界地等待。轮询而非阻塞等待，
        是为了让急停在播放窗口内就能生效。

        Args:
            winmm: The ``winmm`` DLL wrapper.
            alias: MCI alias opened by the caller.
            ctypes_mod: The ``ctypes`` module (injected so tests can substitute it).
            max_seconds: Optional playback cap; ``None``/``0`` means no cap.
            duration: Clip length in seconds when known, else ``None``.
        """
        deadline = None
        if max_seconds not in (None, 0):
            deadline = time.monotonic() + float(max_seconds)
        buf = ctypes_mod.create_unicode_buffer(64)
        while True:
            if self.check_stop_flag():
                winmm.mciSendStringW(f"stop {alias}", None, 0, 0)
                return
            if deadline is not None and time.monotonic() >= deadline:
                winmm.mciSendStringW(f"stop {alias}", None, 0, 0)
                return
            # 到达已知时长即可收尾，不必再多轮询一次。
            if duration is not None and time.monotonic() >= self._play_started_at + duration:
                return
            if winmm.mciSendStringW(f"status {alias} mode", buf, len(buf), 0) == 0:
                if str(buf.value or "").strip().lower() in {"stopped", "paused", "playing "}:
                    pass
                mode = str(buf.value or "").strip().lower()
                if mode in {"stopped", "paused"}:
                    return
            time.sleep(self._MCI_POLL_INTERVAL_SECONDS)

    @action_handler(5.0)
    def _execute_wait(self, context):
        """Handle command 5.0: interruptible wait scaled by playback speed.

        Args:
            context: StepActionContext whose value is the wait duration.

        Returns:
            Outcome string: "success" or "stopped".
        """
        configured_wait = self._bounded_action_duration(
            context.value, maximum=self._MAX_WAIT_DURATION_SECONDS
        )
        wait_time = configured_wait / max(self.playback_speed, 0.1)
        wait_time = min(wait_time, self._MAX_WAIT_DURATION_SECONDS)
        if self.log_level >= 1:
            self.log(
                f"    -> 强制静默等待 {wait_time:.2f} 秒 (原设定 {configured_wait:g}s, "
                f"倍速 {self.playback_speed}x)...",
                LOG_ACTION,
            )
        return "success" if self._wait_interruptibly(wait_time) else "stopped"

    @action_handler(6.0)
    def _execute_scroll(self, context):
        """Handle command 6.0: mouse wheel scroll by a clamped amount.

        Args:
            context: StepActionContext whose value is the scroll amount.

        Returns:
            Outcome string: "success" or "error".
        """
        if self.log_level >= 1:
            self.log(f"    -> 鼠标滚轮滑动 {context.value}", LOG_ACTION)
        try:
            raw = float(context.value)
        except (TypeError, ValueError):
            self.log(
                "<font color='red'>    [滚轮参数无效] 滚动数值无法解析，请检查填写内容。</font>",
                LOG_CRITICAL,
                critical=True,
            )
            return "error"
        if not math.isfinite(raw):
            self.log(
                "<font color='red'>    [滚轮参数无效] 滚动数值必须为有限数字。</font>",
                LOG_CRITICAL,
                critical=True,
            )
            return "error"
        scroll_amount = max(-10000, min(10000, int(raw)))
        with self._input_action_lock:
            self.input_backend.scroll(scroll_amount * self._WHEEL_DELTA)
        return "success"

    @action_handler(TASK_TYPE_AUTO_CLICKER)
    def _execute_auto_clicker(
        self,
        context,
        stop_requested: Callable[[], bool] | None = None,
    ):
        """Run the standalone clicker's bounded three-level loop model.

        Big loops contain small loops; each small loop contains a randomly
        selected number of consecutive clicks.  Timing and spatial randomness
        use bounded distributions and every wait/move honours the tool-local
        stop predicate.  A range of ``0..0`` means infinite for big/small loop
        counts; mixed ``0..N`` ranges draw from ``1..N`` so randomness can never
        unexpectedly turn a finite run into an infinite one.
        """
        task = context.task if isinstance(context.task, dict) else {}

        def _should_stop():
            # 工具级停止谓词（连接连点器窗口的停止事件）与引擎自身停止标志取或，
            # 避免直接改写共享的 engine.check_stop_flag 而误停正在运行的脚本。
            # FR-20260811-D13-002（引擎域裁定，2026-08-12）：保留取或语义——连点器
            # 作为引擎动作必须响应引擎级停止（输入安全边界），且两条"残留标志"可达
            # 路径均不可达：主脚本 finish/reserve 都会 clear() 共享 _stop_event，
            # 连点器窗口启动前有 _script_is_active 互斥检查（脚本运行中禁止启动），
            # 主脚本 IDLE 时 request_stop 返回 False 不置位。见 runtime_state.py
            # finish/reserve 与 ui/auto_clicker_mixins/lifecycle.py _script_is_active。
            if stop_requested is not None and stop_requested():
                return True
            return self.check_stop_flag()

        def _number_pair(min_key, max_key, legacy_key, default, maximum):
            raw_min = task.get(min_key, task.get(legacy_key, default))
            raw_max = task.get(max_key, raw_min)
            value_min = _parse_optional_float(raw_min, maximum)
            value_max = _parse_optional_float(raw_max, maximum)
            value_min = float(default if value_min is None else value_min)
            value_max = float(value_min if value_max is None else value_max)
            return (
                (value_max, value_min)
                if value_max < value_min
                else (value_min, value_max)
            )

        def _sample_count(bounds):
            low, high = (int(bounds[0]), int(bounds[1]))
            if low <= 0 and high <= 0:
                return 0
            low = max(1, low)
            high = max(low, high)
            return random.randint(low, high)

        def _sample_seconds(bounds):
            low, high = bounds
            return float(low) if high <= low else random.uniform(low, high)

        try:
            button = str(task.get("ac_button", "left") or "left").lower()
            if button not in ("left", "middle", "right"):
                button = "left"
            click_count_range = _number_pair(
                "ac_click_count_min", "ac_click_count_max", "ac_click_count", 1, AUTO_CLICKER_COUNT_MAX
            )
            sub_loop_range = _number_pair(
                "ac_sub_loop_min", "ac_sub_loop_max", "ac_sub_loop_count", 1, AUTO_CLICKER_COUNT_MAX
            )
            big_loop_range = _number_pair(
                "ac_big_loop_min", "ac_big_loop_max", "ac_big_loop_count", 1, AUTO_CLICKER_COUNT_MAX
            )

            if "ac_interval_base" in task:
                interval_base = _parse_optional_float(
                    task.get("ac_interval_base"), 3600
                ) or 0.0
                interval_random = config_bool(
                    task.get("ac_interval_random_en", False)
                )
                interval_variation = (
                    _parse_optional_float(
                        task.get("ac_interval_variation"), 3600
                    )
                    or 0.0
                )
                interval_concentration = normalize_concentration(
                    task.get("ac_interval_concentration", 0)
                )
            else:
                legacy_interval = _number_pair(
                    "ac_interval_min", "ac_interval_max", "ac_interval_min", 0.0, 3600
                )
                interval_base = (legacy_interval[0] + legacy_interval[1]) / 2.0
                interval_variation = (
                    legacy_interval[1] - legacy_interval[0]
                ) / 2.0
                interval_random = interval_variation > 0.0
                interval_concentration = 0

            press_duration_range = _number_pair(
                "ac_press_duration_min",
                "ac_press_duration_max",
                "ac_press_duration",
                0.0,
                10.0,
            )
            sub_interval_range = _number_pair(
                "ac_sub_interval_min",
                "ac_sub_interval_max",
                "ac_sub_loop_interval",
                0.0,
                3600.0,
            )
            big_interval_range = _number_pair(
                "ac_big_interval_min",
                "ac_big_interval_max",
                "ac_big_loop_interval",
                0.0,
                3600.0,
            )
            center_mode = str(task.get("ac_center_mode", "current") or "current").lower()
            fixed_x = _parse_optional_float(task.get("ac_fixed_x"), self._MAX_ABS_COORDINATE)
            fixed_y = _parse_optional_float(task.get("ac_fixed_y"), self._MAX_ABS_COORDINATE)
            jitter_h = _parse_optional_float(task.get("ac_jitter_h"), 100_000) or 0.0
            jitter_v = _parse_optional_float(task.get("ac_jitter_v"), 100_000) or 0.0
            jitter_shape = str(
                task.get("ac_jitter_shape", "rectangle") or "rectangle"
            ).lower()
            if jitter_shape not in {"rectangle", "ellipse"}:
                jitter_shape = "rectangle"
            jitter_concentration = normalize_concentration(
                task.get("ac_jitter_concentration", 0)
            )
            curve_strength = (
                _parse_optional_float(task.get("ac_move_curve_strength"), 1.0)
                or 0.0
            )
        except (TypeError, ValueError) as exc:
            if self.log_level >= 0:
                self.log(
                    f"    [连点器] 参数解析失败：{private_error_text(exc)}", LOG_ACTION
                )
            return "error"

        move_duration = getattr(self, "move_duration", 0.0) or 0.0
        big_loop_count = _sample_count(big_loop_range)
        big_infinite = big_loop_count <= 0
        big_total = big_loop_count if not big_infinite else 0

        def _click_interval():
            if not interval_random:
                return interval_base
            return sample_interval_seconds(
                interval_base,
                interval_variation,
                interval_concentration,
            )

        has_completed_click = False

        def run_click_sequence():
            nonlocal has_completed_click
            click_count = _sample_count(click_count_range)
            click_infinite = click_count <= 0
            click_total = click_count if not click_infinite else 0
            click_index = 0
            while click_infinite or click_index < click_total:
                if _should_stop():
                    return "stopped"
                # INVARIANT: 点击间隔约束任意两次实际点击，而不是只约束
                # 同一连击组内部。否则“每组 1 次”会完全绕过速度设置。
                if has_completed_click and not self._wait_interruptibly(
                    _click_interval(),
                    stop_requested=_should_stop,
                ):
                    return "stopped"
                # 计算本次点击目标坐标
                offset_x, offset_y = sample_pointer_offset(
                    jitter_h,
                    jitter_v,
                    jitter_shape,
                    jitter_concentration,
                )
                if center_mode == "fixed" and fixed_x is not None and fixed_y is not None:
                    tx = fixed_x + offset_x
                    ty = fixed_y + offset_y
                    return_to = None
                    need_move = True
                else:
                    try:
                        pos = self.input_backend.position()
                        bx, by = pos[0], pos[1]
                    except Exception as exc:
                        # FR-20260809-0101: 读取鼠标位置失败（如显示会话不可用）是
                        # 异常中止，不得静默当作"用户停止"——抛出带可读原因的错误，
                        # 由连点器线程的 error 信号呈现给用户，避免连点器突然停止
                        # 而用户不知原因。
                        raise RuntimeError(
                            f"连点器读取鼠标位置失败，已中止：{private_error_text(exc)}"
                        ) from exc
                    has_jitter = jitter_h > 0 or jitter_v > 0
                    if has_jitter:
                        tx = bx + offset_x
                        ty = by + offset_y
                        return_to = (bx, by)
                        need_move = True
                    else:
                        # DESIGN: 无偏移时不移动鼠标——连点器核心语义是"在用户
                        # 当前鼠标位置点击"，用户自由控制鼠标，连点器只负责点击。
                        tx, ty = bx, by
                        return_to = None
                        need_move = False
                if not self._auto_clicker_click(
                    tx,
                    ty,
                    button,
                    _sample_seconds(press_duration_range),
                    move_duration,
                    curve_strength=curve_strength,
                    stop_requested=_should_stop,
                    move_first=need_move,
                ):
                    return "stopped"
                if return_to is not None and not self._move_mouse_interruptibly(
                    return_to[0],
                    return_to[1],
                    0.0,
                    trail_enabled=False,
                    stop_requested=_should_stop,
                ):
                    return "stopped"
                has_completed_click = True
                click_index += 1
            return "success"

        def run_small_loops():
            sub_loop_count = _sample_count(sub_loop_range)
            sub_infinite = sub_loop_count <= 0
            sub_total = sub_loop_count if not sub_infinite else 0
            sub_index = 0
            while sub_infinite or sub_index < sub_total:
                if run_click_sequence() == "stopped":
                    return "stopped"
                sub_index += 1
                if (not sub_infinite) and sub_index >= sub_total:
                    break
                if not self._wait_interruptibly(
                    _sample_seconds(sub_interval_range),
                    stop_requested=_should_stop,
                ):
                    return "stopped"
            return "success"

        big_idx = 0
        while big_infinite or big_idx < big_total:
            if _should_stop():
                return "stopped"
            if run_small_loops() == "stopped":
                return "stopped"
            big_idx += 1
            # 大循环之间的间隔；有限大循环的最后一次后不再等待
            if (not big_infinite) and big_idx >= big_total:
                break
            if not self._wait_interruptibly(
                _sample_seconds(big_interval_range), stop_requested=_should_stop
            ):
                return "stopped"
        return "stopped" if _should_stop() else "success"

    def _auto_clicker_click(
        self, x, y, button, press_duration, move_duration, stop_requested=None,
        move_first=True, curve_strength=0.0,
    ):
        """在 (x, y) 处执行一次按下-抬起点击，返回是否成功（未因停止而中断）。

        Args:
            move_first: 为 False 时跳过鼠标移动，直接在当前位置点击
                （连点器"跟随鼠标"模式且无偏移时使用，避免抢占用户的鼠标控制）。
        """

        def _local_stop():
            return bool(stop_requested is not None and stop_requested())

        if _local_stop():
            return False
        # SAFETY (ENG-FR-004 / ARCHITECTURE #77): 连点器步骤与附属点击调度线程
        # 共用真实输入；移动-按下-保持-抬起必须在统一输入锁内原子完成，与
        # mouse._perform_mouse_click_impl 的持锁粒度一致，避免与附属点击交错
        # 注入。循环间隔等待保持在锁外，不改变连点器整体时序。
        with self._input_action_lock:
            if move_first and not self._move_mouse_interruptibly(
                x,
                y,
                move_duration,
                curve_strength=curve_strength,
                trail_enabled=False,
                stop_requested=stop_requested,
            ):
                return False
            if _local_stop():
                return False
            if not self._tracked_mouse_down(button):
                return False
            try:
                if press_duration > 0 and not self._wait_interruptibly(
                    press_duration, stop_requested=stop_requested
                ):
                    return False
            finally:
                self._tracked_mouse_up(button)
        return True

    @action_handler(TASK_TYPE_PLUGIN_ACTION)
    def _execute_plugin_action(self, context):
        """Handle the plugin-action command: delegate to the plugin subsystem.

        Args:
            context: StepActionContext for the plugin step.

        Returns:
            Outcome string from plugin_actions.execute_plugin_action.
        """
        return plugin_actions.execute_plugin_action(self, context)
