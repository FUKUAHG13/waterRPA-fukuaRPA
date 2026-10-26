"""针对 fukua_rpa/actions/flow.py 未覆盖路径的定向补充测试。

覆盖目标包括：execute_task_once / _execute_task_once_impl 分发器、
鼠标轨迹归一化、WAV 时长解析、until/变量/表达式/弹窗/停止/声音/等待等
流程控制动作处理器，以及连点器 _execute_auto_clicker 的遗留间隔、随机
间隔、参数解析失败、固定坐标回退、位置读取异常等分支与 _auto_clicker_click
点击原语的各路径。

所有测试均通过 mock 构造引擎上下文与原生输入/音频依赖，不触发真实鼠标、
键盘或声音输出。
"""

from __future__ import annotations

import os
import time
import unittest
import wave
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fukua_rpa.actions import flow as flow_module
from fukua_rpa.actions.flow import (
    ActionsFlowMixin,
    _mouse_trail_override,
    _wav_duration_seconds,
)
from fukua_rpa.actions.shared import ACTION_HANDLERS, StepActionContext
from fukua_rpa.constants import (
    TASK_TYPE_AUTO_CLICKER,
    TASK_TYPE_EXPRESSION,
    TASK_TYPE_PLUGIN_ACTION,
    TASK_TYPE_SET_VARIABLE,
    TASK_TYPE_UNTIL,
)
from fukua_rpa.engine import RPAEngine
from fukua_rpa.expressions import ExpressionError
from fukua_rpa.params import TaskOnceParams


def _context(command, value="", task=None, step_info=None):
    return StepActionContext(
        command=float(command),
        value=value,
        retry=1,
        step_info=step_info or {"step": 1, "loop": 1, "cmd": "test"},
        cache_key="test",
        confidence=0.8,
        use_gray=True,
        task=task if task is not None else {},
    )


def _params(cmd, val="", task=None):
    return TaskOnceParams(
        cmd=cmd,
        val=val,
        retry=1,
        step_info={"step": 1, "loop": 1, "cmd": "test"},
        cache_key="test",
        task_conf=0.8,
        use_gray=True,
        task=task if task is not None else {},
    )


class FlowEngineTestBase(unittest.TestCase):
    """构造一个延迟后端的真实引擎，并屏蔽高优先级设置。"""

    def setUp(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            self.engine = RPAEngine(defer_backends=True)
        self.engine.callback_msg = mock.Mock()
        self.engine.log_level = 2
        self.engine.move_duration = 0.0
        run_id = self.engine.reserve_run()
        self.engine.lifecycle.mark_running(run_id)
        self.run_id = run_id

    def tearDown(self):
        self.engine.finish_run(self.run_id)


class MouseTrailOverrideTests(unittest.TestCase):
    """_mouse_trail_override 把每步轨迹模式归一化为三态。"""

    def test_on_returns_true(self):
        self.assertIs(_mouse_trail_override({"mouse_trail_mode": "on"}), True)
        self.assertIs(_mouse_trail_override({"mouse_trail_mode": " ON "}), True)

    def test_off_returns_false(self):
        self.assertIs(_mouse_trail_override({"mouse_trail_mode": "off"}), False)

    def test_inherit_and_unknown_return_none(self):
        self.assertIsNone(_mouse_trail_override({"mouse_trail_mode": "inherit"}))
        self.assertIsNone(_mouse_trail_override({"mouse_trail_mode": "weird"}))
        self.assertIsNone(_mouse_trail_override({}))
        self.assertIsNone(_mouse_trail_override(None))


class WavDurationTests(unittest.TestCase):
    """_wav_duration_seconds 读取真实 WAV 并在失败时返回 None。"""

    def test_valid_wav_returns_duration(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = tmp.name
        self.addCleanup(os.remove, path)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(1000)
            wf.writeframes(b"\x00\x00" * 500)

        duration = _wav_duration_seconds(path)
        self.assertIsNotNone(duration)
        self.assertAlmostEqual(duration, 0.5, delta=0.01)

    def test_missing_file_returns_none(self):
        self.assertIsNone(
            _wav_duration_seconds("Z:/definitely/not/here/missing.wav")
        )

    def test_zero_framerate_returns_none(self):
        fake_wf = mock.MagicMock()
        fake_wf.getnframes.return_value = 100
        fake_wf.getframerate.return_value = 0
        fake_wave = mock.MagicMock()
        fake_wave.open.return_value.__enter__.return_value = fake_wf
        with mock.patch.dict("sys.modules", {"wave": fake_wave}):
            self.assertIsNone(_wav_duration_seconds("C:/a.wav"))


class DispatcherTests(FlowEngineTestBase):
    """execute_task_once / _execute_task_once_impl 的分发与异常路径。"""

    def test_unregistered_command_returns_error(self):
        result = self.engine.execute_task_once(_params(999.0, "x"))
        self.assertEqual(result, "error")

    def test_invalid_command_code_is_coerced_and_reports_error(self):
        # cmd 无法解析为 float：_execute_task_once_impl 内部回退为 -1.0，
        # execute_task_once 的 finally 把命令键归一为 "invalid"。
        result = self.engine.execute_task_once(_params("not-a-number", "x"))
        self.assertEqual(result, "error")

    def test_click_command_builds_context_with_step_overrides(self):
        captured = {}

        def handler(_engine, context):
            captured["ctx"] = context
            return "success"

        task = {
            "move_jitter": "0.3",
            "click_hld_jitter": "0.2",
            "move_curve_strength": "0.5",
            "click_spatial_jitter": "7",
            "mouse_trail_mode": "on",
            "click_return": True,
        }
        ACTION_HANDLERS[901.0] = handler
        self.addCleanup(ACTION_HANDLERS.pop, 901.0, None)
        # 临时把 901.0 视作点击指令，以走点击专属的抖动/偏移解析分支。
        with mock.patch.object(
            flow_module, "CLICK_COMMANDS", frozenset({901.0})
        ):
            result = self.engine._execute_task_once_impl(_params(901.0, "v", task))

        self.assertEqual(result, "success")
        ctx = captured["ctx"]
        self.assertEqual(ctx.move_jitter, 0.3)
        self.assertEqual(ctx.hold_jitter, 0.2)
        self.assertEqual(ctx.curve_strength, 0.5)
        self.assertEqual(ctx.spatial_jitter, 7.0)
        self.assertIs(ctx.trail_enabled, True)
        self.assertTrue(ctx.restore_position)

    def test_key_command_parses_key_hold_duration(self):
        captured = {}

        def handler(_engine, context):
            captured["ctx"] = context
            return "success"

        original = ACTION_HANDLERS.get(7.0)
        ACTION_HANDLERS[7.0] = handler
        try:
            result = self.engine._execute_task_once_impl(
                _params(7.0, "ctrl+a", {"key_hold_duration": "1.5"})
            )
        finally:
            if original is not None:
                ACTION_HANDLERS[7.0] = original
            else:
                ACTION_HANDLERS.pop(7.0, None)

        self.assertEqual(result, "success")
        self.assertEqual(captured["ctx"].key_hold_duration, 1.5)

    def test_handler_exception_returns_error(self):
        def handler(_engine, _context):
            raise RuntimeError("boom")

        ACTION_HANDLERS[903.0] = handler
        self.addCleanup(ACTION_HANDLERS.pop, 903.0, None)
        result = self.engine._execute_task_once_impl(_params(903.0, "v"))
        self.assertEqual(result, "error")

    def test_handler_returning_none_returns_error(self):
        def handler(_engine, _context):
            return None

        ACTION_HANDLERS[904.0] = handler
        self.addCleanup(ACTION_HANDLERS.pop, 904.0, None)
        result = self.engine._execute_task_once_impl(_params(904.0, "v"))
        self.assertEqual(result, "error")

    def test_handler_returning_non_string_returns_error(self):
        def handler(_engine, _context):
            return 42

        ACTION_HANDLERS[905.0] = handler
        self.addCleanup(ACTION_HANDLERS.pop, 905.0, None)
        result = self.engine._execute_task_once_impl(_params(905.0, "v"))
        self.assertEqual(result, "error")


class UntilConditionHandlerTests(FlowEngineTestBase):
    def test_delegates_to_execute_until_conditions(self):
        task = {"until_conditions": []}
        with mock.patch.object(
            self.engine,
            "execute_until_conditions",
            return_value="condition_true",
        ) as until:
            result = self.engine._execute_until_condition(
                _context(TASK_TYPE_UNTIL, "", task)
            )
        self.assertEqual(result, "condition_true")
        until.assert_called_once()


class SetVariableHandlerTests(FlowEngineTestBase):
    def test_success_logs_and_returns_success(self):
        with mock.patch.object(
            self.engine, "set_runtime_variable", return_value=("x", 5)
        ):
            result = self.engine._execute_set_variable(
                _context(TASK_TYPE_SET_VARIABLE, "x = 5")
            )
        self.assertEqual(result, "success")

    def test_expression_error_returns_error(self):
        with mock.patch.object(
            self.engine,
            "set_runtime_variable",
            side_effect=ExpressionError("bad"),
        ):
            result = self.engine._execute_set_variable(
                _context(TASK_TYPE_SET_VARIABLE, "x =")
            )
        self.assertEqual(result, "error")


class ExpressionHandlerTests(FlowEngineTestBase):
    def test_true_result_returns_condition_true(self):
        with mock.patch.object(
            self.engine, "evaluate_runtime_expression", return_value=True
        ):
            result = self.engine._execute_expression_condition(
                _context(TASK_TYPE_EXPRESSION, "1 == 1")
            )
        self.assertEqual(result, "condition_true")

    def test_false_result_returns_condition_false(self):
        with mock.patch.object(
            self.engine, "evaluate_runtime_expression", return_value=0
        ):
            result = self.engine._execute_expression_condition(
                _context(TASK_TYPE_EXPRESSION, "1 == 2")
            )
        self.assertEqual(result, "condition_false")

    def test_expression_error_returns_error(self):
        with mock.patch.object(
            self.engine,
            "evaluate_runtime_expression",
            side_effect=ExpressionError("bad"),
        ):
            result = self.engine._execute_expression_condition(
                _context(TASK_TYPE_EXPRESSION, "oops")
            )
        self.assertEqual(result, "error")


class MessageHandlerTests(FlowEngineTestBase):
    def test_missing_callback_returns_error(self):
        self.engine.callback_prompt = None
        result = self.engine._execute_message(_context(12.0, "hello"))
        self.assertEqual(result, "error")

    def test_acknowledged_returns_success_and_clears_active_request(self):
        def callback(request):
            request.acknowledge()

        self.engine.callback_prompt = callback
        result = self.engine._execute_message(_context(12.0, "hello"))
        self.assertEqual(result, "success")
        self.assertIsNone(self.engine._active_prompt_request)

    def test_cancelled_request_returns_stopped(self):
        def callback(request):
            request.cancel()

        self.engine.callback_prompt = callback
        result = self.engine._execute_message(_context(12.0, "hello"))
        self.assertEqual(result, "stopped")

    def test_stop_flag_during_wait_returns_stopped(self):
        # 回调不解决请求；引擎停止标志使等待循环退出并取消请求。
        self.engine.callback_prompt = lambda request: None
        with mock.patch.object(
            self.engine, "check_stop_flag", return_value=True
        ):
            result = self.engine._execute_message(_context(12.0, "hello"))
        self.assertEqual(result, "stopped")
        self.assertIsNone(self.engine._active_prompt_request)


class StopHandlerTests(FlowEngineTestBase):
    def test_stop_invokes_engine_stop_and_returns_success(self):
        with mock.patch.object(self.engine, "stop") as stop:
            result = self.engine._execute_stop(_context(13.0, "note"))
        self.assertEqual(result, "success")
        stop.assert_called_once()


class SoundHandlerTests(FlowEngineTestBase):
    def test_per_step_audio_and_seconds_take_priority(self):
        task = {
            "sound_alert_audio_path": "C:/step.wav",
            "sound_alert_play_seconds": "5",
        }
        self.engine.sound_alert_audio_path = "C:/global.wav"
        self.engine.sound_alert_play_seconds = 30.0
        with mock.patch.object(self.engine, "_play_sound_alert") as play:
            result = self.engine._execute_sound(_context(14.0, "note", task))
        self.assertEqual(result, "success")
        play.assert_called_once_with("C:/step.wav", max_seconds=5.0)

    def test_global_audio_fallback_when_per_step_blank(self):
        self.engine.sound_alert_audio_path = "C:/global.wav"
        self.engine.sound_alert_play_seconds = 12.0
        with mock.patch.object(self.engine, "_play_sound_alert") as play:
            result = self.engine._execute_sound(_context(14.0, "note", {}))
        self.assertEqual(result, "success")
        play.assert_called_once_with("C:/global.wav", max_seconds=12.0)

    def test_blank_per_step_seconds_falls_back_to_global(self):
        task = {"sound_alert_audio_path": "C:/step.wav"}
        self.engine.sound_alert_audio_path = ""
        self.engine.sound_alert_play_seconds = 8.0
        with mock.patch.object(self.engine, "_play_sound_alert") as play:
            result = self.engine._execute_sound(_context(14.0, "note", task))
        self.assertEqual(result, "success")
        play.assert_called_once_with("C:/step.wav", max_seconds=8.0)


class PlaySoundAlertTests(FlowEngineTestBase):
    """_play_sound_alert 的 WAV/WinMM/蜂鸣分支（winsound 全程 mock）。"""

    def _winsound(self):
        ws = mock.MagicMock()
        ws.SND_FILENAME = 1
        ws.SND_ASYNC = 2
        return ws

    def test_wav_shorter_than_limit_plays_async(self):
        ws = self._winsound()
        with mock.patch.dict("sys.modules", {"winsound": ws}), mock.patch(
            "os.path.isfile", return_value=True
        ), mock.patch.object(
            flow_module, "_wav_duration_seconds", return_value=1.0
        ):
            self.engine._play_sound_alert("C:/a.wav", max_seconds=5.0)
        ws.PlaySound.assert_called_once()
        ws.MessageBeep.assert_not_called()

    def test_wav_async_play_failure_falls_back_to_beep(self):
        ws = self._winsound()
        ws.PlaySound.side_effect = RuntimeError("play failed")
        with mock.patch.dict("sys.modules", {"winsound": ws}), mock.patch(
            "os.path.isfile", return_value=True
        ), mock.patch.object(
            flow_module, "_wav_duration_seconds", return_value=1.0
        ):
            self.engine._play_sound_alert("C:/a.wav", max_seconds=5.0)
        ws.MessageBeep.assert_called_once()

    def test_wav_longer_than_limit_is_truncated(self):
        ws = self._winsound()
        with mock.patch.dict("sys.modules", {"winsound": ws}), mock.patch(
            "os.path.isfile", return_value=True
        ), mock.patch.object(
            flow_module, "_wav_duration_seconds", return_value=10.0
        ), mock.patch.object(
            self.engine, "_wait_interruptibly", return_value=True
        ) as wait:
            self.engine._play_sound_alert("C:/a.wav", max_seconds=2.0)
        wait.assert_called_once_with(2.0)
        # 截断后会调用 PlaySound 停止（None, 0）。
        self.assertTrue(ws.PlaySound.called)

    def test_wav_truncated_play_failure_is_suppressed(self):
        ws = self._winsound()
        ws.PlaySound.side_effect = RuntimeError("truncated play failed")
        with mock.patch.dict("sys.modules", {"winsound": ws}), mock.patch(
            "os.path.isfile", return_value=True
        ), mock.patch.object(
            flow_module, "_wav_duration_seconds", return_value=10.0
        ), mock.patch.object(
            self.engine, "_wait_interruptibly", return_value=True
        ):
            # 不应抛出异常；finally 仍会尝试停止播放。
            self.engine._play_sound_alert("C:/a.wav", max_seconds=2.0)

    def test_non_wav_delegates_to_winmm(self):
        ws = self._winsound()
        with mock.patch.dict("sys.modules", {"winsound": ws}), mock.patch(
            "os.path.isfile", return_value=True
        ), mock.patch.object(
            self.engine, "_play_audio_via_winmm", return_value=True
        ) as winmm:
            self.engine._play_sound_alert("C:/a.mp3", max_seconds=3.0)
        winmm.assert_called_once_with("C:/a.mp3", 3.0)
        ws.MessageBeep.assert_not_called()

    def test_falls_back_to_system_beep_when_no_file(self):
        ws = self._winsound()
        with mock.patch.dict("sys.modules", {"winsound": ws}), mock.patch(
            "os.path.isfile", return_value=False
        ):
            self.engine._play_sound_alert("C:/missing.wav", max_seconds=3.0)
        ws.MessageBeep.assert_called_once()

    def test_winmm_failure_falls_back_to_beep(self):
        ws = self._winsound()
        with mock.patch.dict("sys.modules", {"winsound": ws}), mock.patch(
            "os.path.isfile", return_value=True
        ), mock.patch.object(
            self.engine, "_play_audio_via_winmm", return_value=False
        ):
            self.engine._play_sound_alert("C:/a.mp3", max_seconds=None)
        ws.MessageBeep.assert_called_once()

    def test_play_exception_is_swallowed(self):
        ws = self._winsound()
        ws.MessageBeep.side_effect = RuntimeError("no audio device")
        with mock.patch.dict("sys.modules", {"winsound": ws}), mock.patch(
            "os.path.isfile", return_value=False
        ):
            # 不应抛出异常。
            self.engine._play_sound_alert("", max_seconds=None)


class PlayAudioViaWinmmTests(FlowEngineTestBase):
    """_play_audio_via_winmm 通过 mock 的 winmm DLL 覆盖各播放分支。

    FR-SCOPE003-001: 播放一律异步启动，再由 _wait_mci_playback 分片轮询
    `status <alias> mode`。因此所有替身都必须回答 mode 查询，断言也从“发出
    play … wait”改为“不使用同步 wait + 在播放窗口内轮询停止标志”。
    """

    def _patch_winmm(self, mci):
        windll = mock.MagicMock()
        windll.winmm.mciSendStringW.side_effect = mci
        ctypes_mod = mock.MagicMock()
        ctypes_mod.windll = windll
        ctypes_mod.create_unicode_buffer = lambda size: mock.MagicMock(value="0")
        return mock.patch.dict("sys.modules", {"ctypes": ctypes_mod})

    @staticmethod
    def _recorder(calls, *, length="1000", mode=None):
        """构造替身：记录命令，并回答 length / mode 查询。

        ``mode`` 为列表时按序弹出，只剩最后一项后重复返回；为 None 时始终返回
        "playing"（模拟音频尚未播完）。
        """

        script = list(mode) if mode else []

        def mci(command, buf, buflen, hwnd):
            calls.append(command)
            if command.startswith("status") and command.endswith("length"):
                buf.value = length
            elif command.startswith("status") and command.endswith("mode"):
                buf.value = script.pop(0) if len(script) > 1 else (script[0] if script else "playing")
            return 0

        return mci

    def test_open_failure_returns_false(self):
        def mci(command, buf, buflen, hwnd):
            return 1  # open 失败

        with self._patch_winmm(mci):
            self.assertFalse(
                self.engine._play_audio_via_winmm("C:/a.mp3", None)
            )

    def test_full_play_returns_true_without_blocking_wait(self):
        calls = []
        mci = self._recorder(calls, length="1000", mode=["playing", "playing", "stopped"])
        with self._patch_winmm(mci):
            self.assertTrue(
                self.engine._play_audio_via_winmm("C:/a.mp3", None)
            )
        # FR-SCOPE003-001: 不得再使用 MCI 的同步 wait。
        self.assertFalse(any("wait" in c for c in calls))
        self.assertTrue(any(c.startswith("play") for c in calls))
        self.assertTrue(any(c.endswith("mode") for c in calls))
        self.assertTrue(any(c.startswith("close") for c in calls))
        # 自然收尾不需要 stop：无上限时音频仍须完整播放。
        self.assertFalse(any(c.startswith("stop") for c in calls))

    def test_non_numeric_length_is_treated_as_unknown(self):
        calls = []
        mci = self._recorder(calls, length="not-a-number", mode=["playing", "stopped"])
        with self._patch_winmm(mci):
            self.assertTrue(
                self.engine._play_audio_via_winmm("C:/a.mp3", 5.0)
            )
        # 时长未知时按完整播放处理：靠 mode 轮询收尾，而不是同步 wait。
        self.assertFalse(any("wait" in c for c in calls))
        self.assertTrue(any(c.endswith("mode") for c in calls))

    def test_truncated_play_stops_after_limit(self):
        calls = []
        mci = self._recorder(calls, length="10000")  # 10 秒音频，上限 0.05 秒
        started = time.monotonic()
        with self._patch_winmm(mci):
            self.assertTrue(
                self.engine._play_audio_via_winmm("C:/a.mp3", 0.05)
            )
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(any(c.startswith("stop") for c in calls))
        self.assertFalse(any("wait" in c for c in calls))

    def test_stop_request_interrupts_playback_without_waiting_for_the_clip(self):
        """FR-SCOPE003-001 判别项：急停必须在播放窗口内生效。"""

        calls = []
        mci = self._recorder(calls, length="1000")  # 1 秒音频，无播放上限
        started = time.monotonic()
        with self._patch_winmm(mci), mock.patch.object(
            self.engine, "check_stop_flag", return_value=True
        ) as stop_flag:
            self.assertTrue(
                self.engine._play_audio_via_winmm("C:/a.mp3", None)
            )
        elapsed = time.monotonic() - started
        # 停止标志在播放窗口内被轮询到，并据此发出了 stop。
        self.assertTrue(stop_flag.called)
        self.assertTrue(any(c.startswith("stop") for c in calls))
        # 不得阻塞到音频自然结束（1 秒音频 + 收尾余量）。
        self.assertLess(elapsed, 0.5)
        self.assertFalse(any("wait" in c for c in calls))

    def test_stop_flag_is_polled_repeatedly_during_long_playback(self):
        """停止标志须分片轮询，而不是在整个播放窗口内只查一次。"""

        calls = []
        mci = self._recorder(calls, length="60000")  # 60 秒音频
        polls = []

        def stop_flag():
            polls.append(time.monotonic())
            return len(polls) >= 3

        with self._patch_winmm(mci), mock.patch.object(
            self.engine, "check_stop_flag", side_effect=stop_flag
        ):
            self.assertTrue(
                self.engine._play_audio_via_winmm("C:/a.mp3", None)
            )
        self.assertEqual(len(polls), 3)
        self.assertTrue(any(c.startswith("stop") for c in calls))
        # 相邻轮询间隔远小于音频时长（60 秒），即按分片刻度推进。
        self.assertLess(polls[1] - polls[0], 0.5)

    def test_unknown_length_without_limit_plays_until_stopped(self):
        """时长未知且无上限时仍按“播完整段”语义轮询到播放结束。"""

        calls = []
        mci = self._recorder(calls, length="0", mode=["playing", "playing", "stopped"])
        started = time.monotonic()
        with self._patch_winmm(mci):
            self.assertTrue(
                self.engine._play_audio_via_winmm("C:/a.mp3", 0)
            )
        self.assertLess(time.monotonic() - started, 1.0)
        # 自然收尾不需要 stop；别名仍须关闭。
        self.assertFalse(any(c.startswith("stop") for c in calls))
        self.assertTrue(any(c.startswith("close") for c in calls))
        self.assertFalse(any("wait" in c for c in calls))

    def test_exception_returns_false(self):
        with mock.patch.dict("sys.modules", {"ctypes": None}):
            self.assertFalse(
                self.engine._play_audio_via_winmm("C:/a.mp3", None)
            )


class WaitHandlerTests(FlowEngineTestBase):
    def test_wait_scaled_by_playback_speed(self):
        self.engine.playback_speed = 2.0
        with mock.patch.object(
            self.engine, "_wait_interruptibly", return_value=True
        ) as wait:
            result = self.engine._execute_wait(_context(5.0, "4"))
        self.assertEqual(result, "success")
        wait.assert_called_once_with(2.0)

    def test_wait_interrupted_returns_stopped(self):
        self.engine.playback_speed = 1.0
        with mock.patch.object(
            self.engine, "_wait_interruptibly", return_value=False
        ):
            result = self.engine._execute_wait(_context(5.0, "1"))
        self.assertEqual(result, "stopped")


class PluginActionHandlerTests(FlowEngineTestBase):
    def test_delegates_to_plugin_subsystem(self):
        ctx = _context(TASK_TYPE_PLUGIN_ACTION, "payload")
        with mock.patch.object(
            flow_module.plugin_actions,
            "execute_plugin_action",
            return_value="success",
        ) as execute:
            result = self.engine._execute_plugin_action(ctx)
        self.assertEqual(result, "success")
        execute.assert_called_once_with(self.engine, ctx)


def _clicker_task(**overrides):
    task = {
        "ac_button": "left",
        "ac_center_mode": "current",
        "ac_big_loop_min": "1",
        "ac_big_loop_max": "1",
        "ac_sub_loop_min": "1",
        "ac_sub_loop_max": "1",
        "ac_click_count_min": "1",
        "ac_click_count_max": "1",
        "ac_interval_base": "0",
        "ac_interval_random_en": False,
        "ac_press_duration_min": "0",
        "ac_press_duration_max": "0",
        "ac_sub_interval_min": "0",
        "ac_sub_interval_max": "0",
        "ac_big_interval_min": "0",
        "ac_big_interval_max": "0",
    }
    task.update(overrides)
    return task


class AutoClickerIntervalBranchTests(FlowEngineTestBase):
    """覆盖连点器的遗留间隔、随机间隔与参数解析失败分支。"""

    def test_legacy_interval_range_is_used_when_base_absent(self):
        task = _clicker_task()
        task.pop("ac_interval_base")
        task.pop("ac_interval_random_en")
        task["ac_interval_min"] = "0.4"
        task["ac_interval_max"] = "0.4"
        # 两次点击才会产生间隔等待。
        task["ac_click_count_min"] = "2"
        task["ac_click_count_max"] = "2"
        waits = []

        def record_wait(seconds, **_kwargs):
            waits.append(seconds)
            return True

        with mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ) as click, mock.patch.object(
            self.engine, "_wait_interruptibly", side_effect=record_wait
        ):
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))

        self.assertEqual(result, "success")
        self.assertEqual(click.call_count, 2)
        self.assertIn(0.4, waits)

    def test_random_interval_samples_within_bounds(self):
        task = _clicker_task(
            ac_click_count_min="2",
            ac_click_count_max="2",
            ac_interval_base="0.5",
            ac_interval_random_en=True,
            ac_interval_variation="0.1",
            ac_interval_concentration="50",
        )
        waits = []

        def record_wait(seconds, **_kwargs):
            waits.append(seconds)
            return True

        with mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ), mock.patch.object(
            self.engine, "_wait_interruptibly", side_effect=record_wait
        ):
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))

        self.assertEqual(result, "success")
        self.assertTrue(waits)
        for w in waits:
            self.assertGreaterEqual(w, 0.4 - 1e-9)
            self.assertLessEqual(w, 0.6 + 1e-9)

    def test_parameter_parse_failure_returns_error(self):
        # 参数解析阶段抛出的异常应被外层 except 捕获并返回 "error"。
        # 通过让解析依赖之一抛出异常来触发该防御分支。
        task = _clicker_task()
        with mock.patch(
            "fukua_rpa.actions.flow.normalize_concentration",
            side_effect=ValueError("bad concentration"),
        ):
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))
        self.assertEqual(result, "error")

    def test_invalid_button_falls_back_to_left(self):
        task = _clicker_task(ac_button="pinky")
        captured = {}

        def fake_click(x, y, button, *args, **kwargs):
            captured["button"] = button
            return True

        with mock.patch.object(
            self.engine, "_auto_clicker_click", side_effect=fake_click
        ):
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))
        self.assertEqual(result, "success")
        self.assertEqual(captured["button"], "left")

    def test_invalid_jitter_shape_falls_back_to_rectangle(self):
        task = _clicker_task(
            ac_center_mode="fixed",
            ac_fixed_x="100",
            ac_fixed_y="100",
            ac_jitter_h="5",
            ac_jitter_v="5",
            ac_jitter_shape="triangle",
        )
        with mock.patch(
            "fukua_rpa.actions.flow.sample_pointer_offset",
            return_value=(0.0, 0.0),
        ) as sample_offset, mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ):
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))
        self.assertEqual(result, "success")
        self.assertEqual(sample_offset.call_args.args[2], "rectangle")


class AutoClickerStopAndPositionTests(FlowEngineTestBase):
    """覆盖连点器的工具级停止、位置读取异常与固定坐标回退分支。"""

    def test_tool_local_stop_predicate_stops_immediately(self):
        task = _clicker_task()
        with mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ) as click:
            result = self.engine._execute_auto_clicker(
                _context(28.0, "", task), stop_requested=lambda: True
            )
        self.assertEqual(result, "stopped")
        click.assert_not_called()

    def test_engine_stop_flag_stops_before_click(self):
        task = _clicker_task()
        with mock.patch.object(
            self.engine, "check_stop_flag", return_value=True
        ), mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ) as click:
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))
        self.assertEqual(result, "stopped")
        click.assert_not_called()

    def test_position_read_failure_raises_visible_error(self):
        # FR-20260809-0101: 读取鼠标位置失败是异常中止，必须抛出带可读原因的
        # RuntimeError（由连点器线程的 error 信号呈现给用户），不得静默当作
        # "用户停止"返回 stopped。
        task = _clicker_task()  # current 模式且无偏移 -> 读取鼠标位置
        with mock.patch.object(
            self.engine.input_backend,
            "position",
            side_effect=RuntimeError("no session"),
        ), self.assertRaises(RuntimeError) as ctx:
            self.engine._execute_auto_clicker(_context(28.0, "", task))
        self.assertIn("读取鼠标位置失败", str(ctx.exception))

    def test_fixed_mode_without_coordinates_falls_back_to_current(self):
        # fixed 模式但缺少坐标 -> 走当前位置分支（无偏移则不移动）。
        task = _clicker_task(ac_center_mode="fixed")
        with mock.patch.object(
            self.engine.input_backend, "position", return_value=(50, 60)
        ), mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ) as click:
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))
        self.assertEqual(result, "success")
        self.assertEqual(click.call_args.args[:2], (50, 60))
        self.assertFalse(click.call_args.kwargs["move_first"])

    def test_current_mode_with_jitter_moves_and_returns(self):
        task = _clicker_task(ac_jitter_h="10", ac_jitter_v="10")
        with mock.patch.object(
            self.engine.input_backend, "position", return_value=(100, 100)
        ), mock.patch(
            "fukua_rpa.actions.flow.sample_pointer_offset",
            return_value=(3.0, -2.0),
        ), mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ) as click, mock.patch.object(
            self.engine, "_move_mouse_interruptibly", return_value=True
        ) as move:
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))
        self.assertEqual(result, "success")
        self.assertEqual(click.call_args.args[:2], (103.0, 98.0))
        # 点击后应把鼠标移回原始位置。
        move.assert_called_once()
        self.assertEqual(move.call_args.args[:2], (100, 100))

    def test_return_move_failure_stops(self):
        task = _clicker_task(ac_jitter_h="10", ac_jitter_v="10")
        with mock.patch.object(
            self.engine.input_backend, "position", return_value=(100, 100)
        ), mock.patch(
            "fukua_rpa.actions.flow.sample_pointer_offset",
            return_value=(1.0, 1.0),
        ), mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ), mock.patch.object(
            self.engine, "_move_mouse_interruptibly", return_value=False
        ):
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))
        self.assertEqual(result, "stopped")

    def test_click_primitive_failure_stops(self):
        task = _clicker_task()
        with mock.patch.object(
            self.engine.input_backend, "position", return_value=(1, 2)
        ), mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=False
        ):
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))
        self.assertEqual(result, "stopped")

    def test_infinite_clicks_stop_via_flag_after_first(self):
        # 0..0 表示无限连击；停止标志在第一次点击后触发。
        task = _clicker_task(
            ac_click_count_min="0",
            ac_click_count_max="0",
            ac_interval_base="0",
        )
        flags = iter([False, False, True])
        with mock.patch.object(
            self.engine.input_backend, "position", return_value=(1, 2)
        ), mock.patch.object(
            self.engine,
            "check_stop_flag",
            side_effect=lambda: next(flags, True),
        ), mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ) as click:
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))
        self.assertEqual(result, "stopped")
        self.assertGreaterEqual(click.call_count, 1)

    def test_sub_and_big_interval_waits_are_honored(self):
        task = _clicker_task(
            ac_big_loop_min="2",
            ac_big_loop_max="2",
            ac_sub_loop_min="2",
            ac_sub_loop_max="2",
            ac_click_count_min="1",
            ac_click_count_max="1",
            ac_sub_interval_min="0.3",
            ac_sub_interval_max="0.3",
            ac_big_interval_min="0.9",
            ac_big_interval_max="0.9",
        )
        waits = []

        def record_wait(seconds, **_kwargs):
            waits.append(seconds)
            return True

        with mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ) as click, mock.patch.object(
            self.engine, "_wait_interruptibly", side_effect=record_wait
        ), mock.patch.object(
            # FR-20260809-0101: current 模式需读取位置；显式 mock 隔离环境。
            self.engine.input_backend,
            "position",
            return_value=(100, 100),
        ):
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))

        self.assertEqual(result, "success")
        self.assertEqual(click.call_count, 4)
        # 每个大循环内两次小循环之间有 1 次小循环间隔，共 2 次；
        # 两个大循环之间有 1 次大循环间隔。
        self.assertEqual(waits.count(0.3), 2)
        self.assertEqual(waits.count(0.9), 1)
    def test_sub_interval_wait_interruption_stops(self):
        task = _clicker_task(
            ac_sub_loop_min="2",
            ac_sub_loop_max="2",
            ac_sub_interval_min="0.3",
            ac_sub_interval_max="0.3",
        )

        def wait(seconds, **_kwargs):
            # 小循环间隔等待被中断。
            return seconds != 0.3

        with mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ), mock.patch.object(
            self.engine, "_wait_interruptibly", side_effect=wait
        ), mock.patch.object(
            # FR-20260809-0101: 同 big_interval 测试，显式 mock position。
            self.engine.input_backend,
            "position",
            return_value=(100, 100),
        ):
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))
        self.assertEqual(result, "stopped")

    def test_click_interval_wait_interruption_stops(self):
        task = _clicker_task(
            ac_click_count_min="2",
            ac_click_count_max="2",
            ac_interval_base="0.5",
            ac_interval_random_en=False,
        )

        def wait(seconds, **_kwargs):
            # 两次点击之间的点击间隔等待被中断。
            return seconds != 0.5

        with mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ) as click, mock.patch.object(
            self.engine, "_wait_interruptibly", side_effect=wait
        ), mock.patch.object(
            # FR-20260809-0101: current 模式需读取位置；显式 mock 隔离环境。
            self.engine.input_backend,
            "position",
            return_value=(100, 100),
        ):
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))
        self.assertEqual(result, "stopped")
        self.assertEqual(click.call_count, 1)

    def test_big_interval_wait_interruption_stops(self):
        task = _clicker_task(
            ac_big_loop_min="2",
            ac_big_loop_max="2",
            ac_big_interval_min="0.9",
            ac_big_interval_max="0.9",
        )

        def wait(seconds, **_kwargs):
            # 大循环之间的间隔等待被中断。
            return seconds != 0.9

        with mock.patch.object(
            self.engine, "_auto_clicker_click", return_value=True
        ), mock.patch.object(
            self.engine, "_wait_interruptibly", side_effect=wait
        ), mock.patch.object(
            # FR-20260809-0101: current 模式需读取位置；显式 mock 以隔离
            # 环境（离屏缺 pyautogui 时 position 抛错），让本测试专注等待中断。
            self.engine.input_backend,
            "position",
            return_value=(100, 100),
        ):
            result = self.engine._execute_auto_clicker(_context(28.0, "", task))
        self.assertEqual(result, "stopped")


class AutoClickerClickPrimitiveTests(FlowEngineTestBase):
    """_auto_clicker_click 原语的移动/按压/停止路径。"""

    def test_stop_after_move_in_click_aborts(self):
        calls = {"n": 0}

        def stop_after_move():
            calls["n"] += 1
            # 第一次（移动前）不停；移动之后停止。
            return calls["n"] > 1

        with mock.patch.object(
            self.engine, "_move_mouse_interruptibly", return_value=True
        ), mock.patch.object(
            self.engine, "_tracked_mouse_down", return_value=True
        ) as down:
            result = self.engine._auto_clicker_click(
                10,
                20,
                "left",
                0.0,
                0.1,
                stop_requested=stop_after_move,
                move_first=True,
            )
        self.assertFalse(result)
        down.assert_not_called()

    def test_successful_click_with_move_and_press(self):
        with mock.patch.object(
            self.engine, "_move_mouse_interruptibly", return_value=True
        ) as move, mock.patch.object(
            self.engine, "_tracked_mouse_down", return_value=True
        ) as down, mock.patch.object(
            self.engine, "_tracked_mouse_up", return_value=True
        ) as up, mock.patch.object(
            self.engine, "_wait_interruptibly", return_value=True
        ) as wait:
            result = self.engine._auto_clicker_click(
                10, 20, "left", 0.05, 0.1, move_first=True
            )
        self.assertTrue(result)
        move.assert_called_once()
        down.assert_called_once_with("left")
        up.assert_called_once_with("left")
        wait.assert_called_once()

    def test_move_failure_aborts(self):
        with mock.patch.object(
            self.engine, "_move_mouse_interruptibly", return_value=False
        ), mock.patch.object(
            self.engine, "_tracked_mouse_down", return_value=True
        ) as down:
            result = self.engine._auto_clicker_click(
                10, 20, "left", 0.0, 0.1, move_first=True
            )
        self.assertFalse(result)
        down.assert_not_called()

    def test_stop_after_move_aborts_before_press(self):
        with mock.patch.object(
            self.engine, "_move_mouse_interruptibly", return_value=True
        ), mock.patch.object(
            self.engine, "_tracked_mouse_down", return_value=True
        ) as down:
            result = self.engine._auto_clicker_click(
                10,
                20,
                "left",
                0.0,
                0.1,
                stop_requested=lambda: True,
                move_first=True,
            )
        self.assertFalse(result)
        down.assert_not_called()

    def test_mouse_down_failure_returns_false(self):
        with mock.patch.object(
            self.engine, "_move_mouse_interruptibly", return_value=True
        ), mock.patch.object(
            self.engine, "_tracked_mouse_down", return_value=False
        ):
            result = self.engine._auto_clicker_click(
                10, 20, "left", 0.0, 0.0, move_first=True
            )
        self.assertFalse(result)

    def test_press_wait_interrupted_still_releases(self):
        with mock.patch.object(
            self.engine, "_move_mouse_interruptibly", return_value=True
        ), mock.patch.object(
            self.engine, "_tracked_mouse_down", return_value=True
        ), mock.patch.object(
            self.engine, "_tracked_mouse_up", return_value=True
        ) as up, mock.patch.object(
            self.engine, "_wait_interruptibly", return_value=False
        ):
            result = self.engine._auto_clicker_click(
                10, 20, "left", 0.5, 0.0, move_first=True
            )
        self.assertFalse(result)
        up.assert_called_once_with("left")


class MixinHelperTests(unittest.TestCase):
    """直接覆盖模块级辅助与混入类的静态边界。"""

    def test_mixin_exposes_handlers(self):
        # 确保流程控制处理器都已注册到分发表。
        self.assertIn(TASK_TYPE_UNTIL, ACTION_HANDLERS)
        self.assertIn(TASK_TYPE_SET_VARIABLE, ACTION_HANDLERS)
        self.assertIn(TASK_TYPE_EXPRESSION, ACTION_HANDLERS)
        self.assertIn(TASK_TYPE_AUTO_CLICKER, ACTION_HANDLERS)
        self.assertIn(TASK_TYPE_PLUGIN_ACTION, ACTION_HANDLERS)
        self.assertTrue(issubclass(ActionsFlowMixin, object))


if __name__ == "__main__":
    unittest.main()


class SoundAlertPlaySecondsTests(unittest.TestCase):
    """FR-20261005-201: 全局播放时长 0 表示不截断，不得被改写成 30。"""

    def _run(self, global_seconds, per_step_text):
        from types import SimpleNamespace

        class _Stub(ActionsFlowMixin):
            pass

        stub = _Stub()
        stub.log_level = -1
        stub.sound_alert_audio_path = ""
        stub.sound_alert_play_seconds = global_seconds
        captured: dict = {}
        stub._play_sound_alert = lambda audio, max_seconds=None: captured.update(  # type: ignore[method-assign]
            {"max_seconds": max_seconds}
        )
        stub._execute_sound(SimpleNamespace(task={"sound_alert_play_seconds": per_step_text}))
        return captured["max_seconds"]

    def test_global_zero_is_not_rewritten_to_thirty(self):
        self.assertEqual(self._run(0.0, ""), 0.0)

    def test_global_missing_defaults_to_thirty(self):
        self.assertEqual(self._run(None, ""), 30.0)

    def test_blank_per_step_falls_back_to_the_global_value(self):
        self.assertEqual(self._run(0.0, ""), 0.0)
        self.assertEqual(self._run(45.0, ""), 45.0)

    def test_per_step_value_wins(self):
        self.assertEqual(self._run(0.0, "5"), 5.0)

    def test_invalid_per_step_text_falls_back_to_the_global_value(self):
        """FR-20261008-902：非法步骤级播放时长必须回退全局值。

        ``_parse_optional_float`` 的契约是非法/非有限输入返回 None、由调用方
        回落全局值；此前调用点把 None 映射为 0.0（不限时长），既构成静默
        降级，又覆盖了用户设置的全局时长（例如 45 秒）。
        """
        self.assertEqual(self._run(45.0, "abc"), 45.0)
        self.assertEqual(self._run(45.0, "1e999"), 45.0)
