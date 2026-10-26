import threading
import time
import unittest
from unittest import mock

from fukua_rpa import text_input
from fukua_rpa.engine import RPAEngine
from fukua_rpa.engine.actions_exec import StepActionContext
from fukua_rpa.log_policy import LOG_MODE_DETAILED
from fukua_rpa.logging_service import flush_logs
from fukua_rpa.worker import FailsafeWatchdog


def action_context(command, value, task=None):
    return StepActionContext(
        command=float(command),
        value=value,
        retry=1,
        step_info={"step": 1, "loop": 1, "cmd": "test"},
        cache_key="test",
        confidence=0.8,
        use_gray=True,
        task=task or {},
    )


class ExecutionCancellationTests(unittest.TestCase):
    def create_active_engine(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine(defer_backends=True)
        run_id = engine.reserve_run()
        self.assertIsNotNone(run_id)
        self.assertTrue(engine.lifecycle.mark_running(run_id))
        return engine, run_id

    def test_long_mouse_hold_stops_and_releases_button_promptly(self):
        engine, run_id = self.create_active_engine()
        engine.click_hold = 5.0
        pressed = threading.Event()
        result = []
        with mock.patch.object(
            engine.input_backend,
            "mouse_down",
            side_effect=lambda **_kwargs: pressed.set(),
        ), mock.patch.object(
            engine.input_backend,
            "mouse_up",
        ) as mouse_up:
            thread = threading.Thread(
                target=lambda: result.append(engine._press_mouse_button("left"))
            )
            thread.start()
            self.assertTrue(pressed.wait(0.5))
            started = time.monotonic()
            self.assertTrue(engine.stop())
            thread.join(0.5)

        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(result, [False])
        mouse_up.assert_called_with(button="left")
        engine.finish_run(run_id, "stopped")

    def test_drag_stop_releases_button_before_reaching_destination(self):
        engine, run_id = self.create_active_engine()
        engine.move_duration = 0.0
        engine.click_hold = 0.0
        pressed = threading.Event()
        result = []
        with mock.patch.object(
            engine.input_backend,
            "position",
            return_value=(0, 0),
        ), mock.patch.object(
            engine.input_backend,
            "move_to",
        ), mock.patch.object(
            engine.input_backend,
            "mouse_down",
            side_effect=lambda **_kwargs: pressed.set(),
        ), mock.patch.object(
            engine.input_backend,
            "mouse_up",
        ) as mouse_up:
            thread = threading.Thread(
                target=lambda: result.append(
                    engine.mouseDrag(
                        "left",
                        "10,20 -> 500,600",
                        {"step": 1, "loop": 1},
                        recorded_duration=5.0,
                    )
                )
            )
            thread.start()
            self.assertTrue(pressed.wait(0.5))
            self.assertTrue(engine.stop())
            thread.join(0.5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ["stopped"])
        mouse_up.assert_called_with(button="left")
        engine.finish_run(run_id, "stopped")

    def test_script_prompt_is_nonblocking_and_stop_cancels_it(self):
        engine, run_id = self.create_active_engine()
        received = threading.Event()
        requests = []
        result = []

        def prompt_callback(request):
            requests.append(request)
            received.set()

        engine.callback_prompt = prompt_callback
        thread = threading.Thread(
            target=lambda: result.append(
                engine._execute_message(
                    action_context(12.0, "PRIVATE_PROMPT_SENTINEL")
                )
            )
        )
        thread.start()
        self.assertTrue(received.wait(0.5))
        self.assertTrue(engine.stop())
        thread.join(0.5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ["stopped"])
        self.assertTrue(requests[0].cancelled)
        engine.finish_run(run_id, "stopped")

    def test_backend_cancel_failure_cannot_block_input_release(self):
        engine, run_id = self.create_active_engine()
        engine.native_core = mock.Mock()
        engine.native_core.cancel_active_search.side_effect = RuntimeError("broken")
        engine.debug_session.cancel = mock.Mock(side_effect=RuntimeError("broken"))
        engine.release_injected_inputs = mock.Mock()

        self.assertTrue(engine.stop())

        engine.native_core.cancel_active_search.assert_called_once_with()
        engine.debug_session.cancel.assert_called_once_with()
        engine.release_injected_inputs.assert_called_once_with()
        engine.finish_run(run_id, "stopped")

    def test_scheduler_close_failure_cannot_block_input_release(self):
        # ENG-FR-002：附属点击调度器 close() 抛异常时，急停序列的剩余取消与
        # release_injected_inputs 仍必须执行（ARCHITECTURE #52）。
        engine, run_id = self.create_active_engine()
        engine.auxiliary_click_scheduler = mock.Mock()
        engine.auxiliary_click_scheduler.close.side_effect = RuntimeError("broken")
        engine.release_injected_inputs = mock.Mock()

        self.assertTrue(engine.stop())

        engine.auxiliary_click_scheduler.close.assert_called_once_with(
            cancel_pending=True, wait=False
        )
        engine.release_injected_inputs.assert_called_once_with()
        engine.finish_run(run_id, "stopped")

    def test_failed_release_stays_tracked_for_a_final_retry(self):
        engine, run_id = self.create_active_engine()
        engine._injected_mouse_buttons.add("left")
        engine._injected_keys.append("ctrl")
        with mock.patch.object(
            engine.input_backend,
            "mouse_up",
            side_effect=(RuntimeError("busy"), None),
        ), mock.patch.object(
            engine.input_backend,
            "key_up",
            side_effect=(RuntimeError("busy"), None),
        ):
            engine.release_injected_inputs()
            self.assertEqual(engine._injected_mouse_buttons, {"left"})
            self.assertEqual(engine._injected_keys, ["ctrl"])
            engine.release_injected_inputs()

        self.assertEqual(engine._injected_mouse_buttons, set())
        self.assertEqual(engine._injected_keys, [])
        engine.finish_run(run_id)

    def test_unicode_text_checks_stop_between_small_paired_batches(self):
        checks = iter((False, True))
        with mock.patch.object(
            text_input,
            "SendInput",
            side_effect=lambda count, _events, _size: count,
        ) as send:
            completed = text_input.send_unicode_text(
                "A" * 130,
                stop_requested=lambda: next(checks),
            )

        self.assertFalse(completed)
        self.assertEqual(send.call_count, 1)

    def test_action_durations_reject_nan_infinity_and_extreme_values(self):
        engine, run_id = self.create_active_engine()
        for value in (float("nan"), float("inf"), -1, 301):
            with self.subTest(value=value), self.assertRaises(ValueError):
                engine._bounded_action_duration(value)
        engine.finish_run(run_id)

    def test_cancelled_recognition_reports_stopped_not_not_found(self):
        """FR-20261008-401：搜索进行中被急停，不得把该步上报为「未找到」。

        原生搜索被取消时返回空结果集。``mouseClick`` 的循环顶部虽已检查停止标志，
        但取消发生在**搜索进行中**——此时顶部检查已过，空 locations 分支此前未复查
        停止标志，于是把该步记为 ``not_found``（计入 consecutive_failures 并置失败
        分支），用户主动停止却显示「未找到」。本用例让 ``_resolve_click_targets``
        在返回空结果前设置停止标志，复现该竞态。
        """
        engine, run_id = self.create_active_engine()

        def cancel_then_miss(_ctx):
            engine.lifecycle.request_stop()
            return [], 0.0, None, None

        with mock.patch.object(
            engine, "_resolve_click_targets", side_effect=cancel_then_miss
        ), mock.patch.object(engine, "record_recognition_miss") as miss:
            result = engine.mouseClick(
                1,
                "left",
                "img.png",
                0,
                step_info={"step": 1, "loop": 1, "cmd": "test"},
            )

        self.assertEqual(result, "stopped")
        miss.assert_not_called()
        engine.finish_run(run_id, "stopped")

    def test_collect_multi_targets_resets_find_time_clock_per_attempt(self):
        """FR-20261010-2201：收集分支每轮进入时必须刷新 collect_started_at，
        否则 reTry==-1 首轮未命中返回 [] 后外层等待重试，「底层找图耗时」把
        识别间隔/自适应退避/画面唤醒等待全部计入本轮识别耗时（日志失真）。"""
        from types import SimpleNamespace

        from fukua_rpa.vision.collect import CollectionPolicy

        engine, run_id = self.create_active_engine()
        ctx = SimpleNamespace(
            collect_policy=CollectionPolicy(first_hit_buffer_s=0.05),
            img_path="img.png",
            cache_key="k",
            task_conf=0.8,
            use_gray=True,
            search_regions=[],
            allow_inverted=False,
            coord=False,
            point_limit_en=False,
            point_limit_count=0,
            image_click_config=None,
            step_info={"step": 1, "loop": 1},
            start_time=time.monotonic(),
            collect_started_at=time.monotonic(),
        )
        with mock.patch.object(
            engine, "find_all_targets_optimized", return_value=[]
        ):
            first = engine._collect_multi_targets(ctx)
            first_ts = ctx.collect_started_at
            time.sleep(0.02)
            second = engine._collect_multi_targets(ctx)
            second_ts = ctx.collect_started_at

        self.assertEqual(first, [])
        self.assertEqual(second, [])
        self.assertGreaterEqual(
            second_ts,
            first_ts + 0.01,
            "第二次进入收集分支必须刷新收集起始时钟（FR-20261010-2201）",
        )
        engine.finish_run(run_id)

    def test_filtered_click_targets_reports_stopped_not_not_found(self):
        """FR-20261008-903：命中后全部过滤分支与 FR-20261008-401 空结果分支
        属同一停止契约——返回 not_found 前必须复查停止标志。

        触发：搜索返回**非空**命中集后，``_order_multi_targets`` 的纯 CPU
        过滤（识别区域/同点点击上限）把命中清空；若停止标志在搜索返回之后、
        过滤完成之前被另一线程置位，此前该分支直接返回 ``not_found``，把
        用户主动停止记成识别失败（计入 consecutive_failures 并置失败分支）。
        """
        engine, run_id = self.create_active_engine()

        def hit_then_stop(_ctx):
            engine.lifecycle.request_stop()
            return [(100, 100)], 0.0, None, None

        with mock.patch.object(
            engine, "_resolve_click_targets", side_effect=hit_then_stop
        ), mock.patch.object(
            engine, "_order_multi_targets", return_value=None
        ), mock.patch.object(engine, "record_recognition_miss") as miss:
            result = engine.mouseClick(
                1,
                "left",
                "img.png",
                0,
                step_info={"step": 1, "loop": 1, "cmd": "test"},
            )

        self.assertEqual(result, "stopped")
        miss.assert_not_called()
        engine.finish_run(run_id)

    def test_collect_multi_targets_clicks_only_the_last_visible_frame(self):
        """反馈项：收集阶段只点击结束前仍然可见的目标，历史坐标不得复用。"""
        engine, run_id = self.create_active_engine()
        from fukua_rpa.vision.collect import CollectionPolicy

        ctx = mock.Mock(
            collect_policy=CollectionPolicy(last_hit_buffer_s=0.06),
            collect_started_at=time.monotonic(),
            start_time=time.monotonic(),
            img_path="img.png",
            cache_key="k",
            task_conf=0.8,
            use_gray=True,
            search_regions=None,
            allow_inverted=False,
            coord=None,
            reTry=1,
            point_limit_en=False,
            point_limit_count=0,
            image_click_config=None,
            step_info={"step": 1, "loop": 1, "cmd": "test"},
        )
        only_first = (0, 0, 1.0, 0.9, 10, 10)
        moved = (200, 0, 1.0, 0.9, 10, 10)
        calls = {"n": 0}

        def frame(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return [only_first]
            if calls["n"] == 2:
                return [only_first, moved]
            # 之后原目标消失，只有移动后的目标仍在：结束时应点击这一张截图里的目标。
            return [moved]

        with mock.patch(
            "fukua_rpa.actions.mouse._COLLECT_FRAME_INTERVAL_S", 0.02
        ), mock.patch.object(
            engine, "find_all_targets_optimized", side_effect=frame
        ):
            result = engine._collect_multi_targets(ctx)

        self.assertGreater(calls["n"], 2, "末位缓冲应等待到安静后再结束")
        self.assertEqual(
            result,
            [moved],
            "只应点击最后一张截图中仍可见的目标",
        )
        engine.finish_run(run_id)

    def test_collect_multi_targets_stop_is_honoured_between_frames(self):
        """停止必须能在任一收集帧之间立即生效。"""
        engine, run_id = self.create_active_engine()
        from fukua_rpa.vision.collect import CollectionPolicy

        ctx = mock.Mock(
            collect_policy=CollectionPolicy(first_hit_buffer_s=5.0),
            collect_started_at=time.monotonic(),
            start_time=time.monotonic(),
            img_path="img.png",
            cache_key="k",
            task_conf=0.8,
            use_gray=True,
            search_regions=None,
            allow_inverted=False,
            coord=None,
            reTry=1,
            point_limit_en=False,
            point_limit_count=0,
            image_click_config=None,
            step_info={"step": 1, "loop": 1, "cmd": "test"},
        )

        def frame_then_stop(*_args, **_kwargs):
            engine.lifecycle.request_stop()
            return [(0, 0, 1.0, 0.9, 10, 10)]

        with mock.patch.object(
            engine, "find_all_targets_optimized", side_effect=frame_then_stop
        ):
            result = engine._collect_multi_targets(ctx)

        self.assertEqual(result, "stopped")
        engine.finish_run(run_id)

    def test_collect_multi_targets_without_partial_times_out_instead_of_succeeding(self):
        """有限 N + 关闭“不足处理”：收不齐时按超时处理，不得少点后标成功。"""
        engine, run_id = self.create_active_engine()
        from fukua_rpa.vision.collect import CollectionPolicy

        ctx = mock.Mock(
            collect_policy=CollectionPolicy(
                max_targets=3, allow_partial=False, max_collect_s=0.5
            ),
            collect_started_at=time.monotonic(),
            start_time=time.monotonic(),
            img_path="img.png",
            cache_key="k",
            task_conf=0.8,
            use_gray=True,
            search_regions=None,
            allow_inverted=False,
            coord=None,
            reTry=1,
            point_limit_en=False,
            point_limit_count=0,
            image_click_config=None,
            step_info={"step": 1, "loop": 1, "cmd": "test"},
        )
        with mock.patch(
            "fukua_rpa.actions.mouse._COLLECT_FRAME_INTERVAL_S", 0.0
        ), mock.patch.object(
            engine,
            "find_all_targets_optimized",
            return_value=[(0, 0, 1.0, 0.9, 10, 10)],
        ):
            result = engine._collect_multi_targets(ctx)

        self.assertEqual(result, "timeout")
        engine.finish_run(run_id)

    def test_collect_multi_targets_does_not_start_when_policy_disabled(self):
        """缓冲/数量全部为 0 时保持原有“首轮命中立即处理”路径。"""
        engine, run_id = self.create_active_engine()
        self.assertIsNone(engine._multi_collect_policy(True))
        self.assertIsNone(engine._multi_collect_policy(False))
        engine.finish_run(run_id)

    def test_collection_is_scoped_to_all_match_mode(self):
        """复核 R2：收集阶段只在“全部匹配”生效，不波及“快速一个+同点上限”。"""
        engine, run_id = self.create_active_engine()
        engine.multi_collect_first_hit_s = 0.5
        self.assertIsNone(
            engine._multi_collect_policy(False), "非全部匹配步骤不得启用收集"
        )
        self.assertIsNotNone(engine._multi_collect_policy(True))
        engine.finish_run(run_id)

    def test_empty_frame_does_not_end_collection_immediately(self):
        """复核 D3：进入收集后单帧空白不得当作首次搜索失败直接退出。"""
        engine, run_id = self.create_active_engine()
        from fukua_rpa.vision.collect import CollectionPolicy

        ctx = mock.Mock(
            collect_policy=CollectionPolicy(last_hit_buffer_s=0.06),
            collect_started_at=time.monotonic(),
            start_time=time.monotonic(),
            img_path="img.png",
            cache_key="k",
            task_conf=0.8,
            use_gray=True,
            search_regions=None,
            allow_inverted=False,
            coord=None,
            reTry=0,
            point_limit_en=False,
            point_limit_count=0,
            image_click_config=None,
            step_info={"step": 1, "loop": 1, "cmd": "test"},
        )
        target = (0, 0, 1.0, 0.9, 10, 10)
        calls = {"n": 0}

        def frame(*_args, **_kwargs):
            calls["n"] += 1
            # 命中一帧 → 空白一帧 → 恢复命中：空白帧不得让整步失败。
            if calls["n"] == 2:
                return []
            return [target]

        with mock.patch(
            "fukua_rpa.actions.mouse._COLLECT_FRAME_INTERVAL_S", 0.01
        ), mock.patch.object(
            engine, "find_all_targets_optimized", side_effect=frame
        ):
            result = engine._collect_multi_targets(ctx)

        self.assertGreater(calls["n"], 2, "空白帧后必须继续收集")
        self.assertEqual(result, [target])
        engine.finish_run(run_id)

    def test_collection_returns_empty_when_targets_vanish_before_finish(self):
        """进入收集后目标全部消失：不得复用历史坐标，也不得标成功。"""
        engine, run_id = self.create_active_engine()
        from fukua_rpa.vision.collect import CollectionPolicy

        ctx = mock.Mock(
            collect_policy=CollectionPolicy(last_hit_buffer_s=0.04),
            collect_started_at=time.monotonic(),
            start_time=time.monotonic(),
            img_path="img.png",
            cache_key="k",
            task_conf=0.8,
            use_gray=True,
            search_regions=None,
            allow_inverted=False,
            coord=None,
            reTry=0,
            point_limit_en=False,
            point_limit_count=0,
            image_click_config=None,
            step_info={"step": 1, "loop": 1, "cmd": "test"},
        )
        calls = {"n": 0}

        def frame(*_args, **_kwargs):
            calls["n"] += 1
            return [(0, 0, 1.0, 0.9, 10, 10)] if calls["n"] == 1 else []

        with mock.patch(
            "fukua_rpa.actions.mouse._COLLECT_FRAME_INTERVAL_S", 0.01
        ), mock.patch.object(
            engine, "find_all_targets_optimized", side_effect=frame
        ):
            result = engine._collect_multi_targets(ctx)

        self.assertEqual(result, [], "已消失的目标不得作为点击坐标，也不得标成功")
        engine.finish_run(run_id)

    def test_input_backend_rejects_unmapped_key_names(self):
        """复核 D2：SendInput 模式遇到未识别键名必须报错，不得静默丢键。"""
        from fukua_rpa.input_backend import InputInjectionError, SendInputInputBackend

        backend = SendInputInputBackend()
        with self.assertRaises(InputInjectionError):
            backend.key_down("shiftleft")
        with self.assertRaises(InputInjectionError):
            backend.key_up("numlock")

    def test_collect_counts_only_usable_targets_after_filters(self):
        """复核 D3：被区域/同点过滤掉的候选不能用来凑“设定数量”。"""
        engine, run_id = self.create_active_engine()
        from fukua_rpa.vision.collect import CollectionPolicy

        ctx = mock.Mock(
            collect_policy=CollectionPolicy(
                last_hit_buffer_s=0.05, max_targets=2, allow_partial=False,
                max_collect_s=0.3,
            ),
            collect_started_at=time.monotonic(),
            start_time=time.monotonic(),
            img_path="img.png",
            cache_key="k",
            task_conf=0.8,
            use_gray=True,
            search_regions=None,
            allow_inverted=False,
            coord=None,
            reTry=1,
            point_limit_en=False,
            point_limit_count=0,
            image_click_config=None,
            step_info={"step": 1, "loop": 1, "cmd": "test"},
        )
        # 同点上限过滤掉其中一个目标后，可用目标恒为 1 个 < N=2。
        ctx.point_limit_en = True
        ctx.point_limit_count = 5
        with mock.patch(
            "fukua_rpa.actions.mouse._COLLECT_FRAME_INTERVAL_S", 0.02
        ), mock.patch.object(
            engine,
            "find_all_targets_optimized",
            return_value=[(0, 0, 1.0, 0.9, 10, 10), (200, 0, 1.0, 0.9, 10, 10)],
        ), mock.patch.object(
            engine,
            "_filter_point_limit_targets",
            side_effect=lambda locations, *_args, **_kwargs: locations[:1],
        ):
            result = engine._collect_multi_targets(ctx)

        self.assertEqual(
            result, "timeout", "过滤后不足设定数量时必须按超时处理，不得少点标成功"
        )
        engine.finish_run(run_id)

    def test_input_backend_failure_is_not_silent(self):
        """复核 D2：显式 SendInput 注入数量不足必须明确报错，不能静默当成功。"""
        from fukua_rpa.input_backend import InputInjectionError, SendInputInputBackend

        backend = SendInputInputBackend()
        with mock.patch.object(
            backend, "_send", side_effect=InputInjectionError("只发送了 0/1 个输入事件")
        ):
            with self.assertRaises(InputInjectionError):
                backend.mouse_down(button="left")
            with self.assertRaises(InputInjectionError):
                backend.key_down("a")
        with mock.patch(
            "ctypes.windll.user32.SendInput", return_value=0
        ), self.assertRaises(InputInjectionError):
            backend.move_to(10, 20)

    def test_mouse_position_read_failure_degrades_to_instant_move(self):
        """FR-20261008-403：读取鼠标位置失败不得让整步变成 error。
        ``duration > 0`` 分支此前未保护 ``input_backend.position()``，而同一函数内的
        拖影分支已按"瞬时失败不得抑制真实移动/点击"处理；两条分支对同一失败不一致。
        修复后应退化为瞬时移动并正常返回，而不是把异常抛给上层记 error。
        """
        engine, run_id = self.create_active_engine()
        with mock.patch.object(
            engine.input_backend,
            "position",
            side_effect=RuntimeError("display session unavailable"),
        ), mock.patch.object(engine.input_backend, "move_to") as move_to:
            result = engine._move_mouse_interruptibly(10, 10, duration=0.3)

        self.assertTrue(result)
        move_to.assert_called_once()
        self.assertEqual(move_to.call_args.kwargs.get("duration"), 0)
        engine.finish_run(run_id)

    def test_mouse_position_read_failure_is_visible_in_action_log(self):
        """FR-20261009-001：退化到瞬时移动必须能在动作类别日志中被观察到。

        ``duration > 0`` 分支的退化记录此前用模块级 ``logger.debug``：冻结版无任何
        handler，DEBUG 低于 ``logging.lastResort`` 阈值，该退化在任何环境下都不会
        输出。改为经项目日志类别通道输出后，在启用「动作」类别时应可观察到。
        """
        engine, run_id = self.create_active_engine()
        # FR-RISK-20261010-3102：断言失败时也保证 run 收尾（finish_run 幂等）。
        self.addCleanup(engine.finish_run, run_id)
        engine.configure_log_policy(LOG_MODE_DETAILED)
        messages = []
        engine.callback_msg = messages.append
        with mock.patch.object(
            engine.input_backend,
            "position",
            side_effect=RuntimeError("display session unavailable"),
        ), mock.patch.object(engine.input_backend, "move_to") as move_to:
            result = engine._move_mouse_interruptibly(10, 10, duration=0.3)

        self.assertTrue(result)
        self.assertEqual(move_to.call_args.kwargs.get("duration"), 0)
        self.assertTrue(flush_logs())
        joined = "\n".join(str(item) for item in messages)
        self.assertIn("退化为瞬时移动", joined)
        engine.finish_run(run_id)

    def test_watchdog_detector_failure_does_not_disable_other_emergency_stops(self):
        class FakeEngine:
            enable_key_stop = False
            enable_tr_stop = True
            enable_tm_stop = True
            stop_requested = False

            def __init__(self):
                self.stop_calls = 0
                self.log_calls = []

            def log(self, message, *_args, **_kwargs):
                self.log_calls.append(message)

            def stop(self):
                self.stop_requested = True
                self.stop_calls += 1

        engine = FakeEngine()
        # FR-20260812: mock 必须在 FailsafeWatchdog 构造**之前**替换类方法——
        # __init__ 会把 self._corner_stop_reason / _task_manager_stop_reason 的
        # 绑定引用捕获进 _detectors 元组；构造后替换实例属性对元组无效，检测器
        # 走真实实现（前台窗口非任务管理器）导致 run() 无限轮询挂起。
        with mock.patch.object(
            FailsafeWatchdog,
            "_corner_stop_reason",
            side_effect=RuntimeError("position backend unavailable"),
        ) as corner_reason, mock.patch.object(
            FailsafeWatchdog,
            "_task_manager_stop_reason",
            return_value="检测到【任务管理器】前台",
        ) as task_manager_reason:
            watchdog = FailsafeWatchdog(engine)
            with mock.patch("fukua_rpa.worker.write_log"), mock.patch(
                "fukua_rpa.worker.ctypes.windll.user32.MessageBeep"
            ):
                watchdog.run()

        self.assertEqual(engine.stop_calls, 1)
        corner_reason.assert_called_once_with()
        task_manager_reason.assert_called_once_with()


class InputBackendCompatibilityTests(unittest.TestCase):
    """反馈项：显式可选的 SendInput / 扫描码 / 移动后等待，默认关闭。"""

    def test_default_backend_is_unchanged_pyautogui(self):
        from fukua_rpa.input_backend import (
            PyAutoGUIInputBackend,
            SendInputInputBackend,
            create_input_backend,
        )

        self.assertIsInstance(create_input_backend("默认"), PyAutoGUIInputBackend)
        self.assertIsInstance(create_input_backend(None), PyAutoGUIInputBackend)
        self.assertIsInstance(create_input_backend("未知模式"), PyAutoGUIInputBackend)
        self.assertIsInstance(create_input_backend("SendInput"), SendInputInputBackend)

    def test_virtual_desktop_mapping_handles_negative_origin_and_clamping(self):
        from fukua_rpa.input_backend import virtual_desktop_absolute

        metrics = (-1920, -100, 3840, 1180)
        self.assertEqual(virtual_desktop_absolute(-1920, -100, metrics), (0, 0))
        self.assertEqual(virtual_desktop_absolute(1919, 1079, metrics), (65535, 65535))
        # 左侧屏幕的负坐标必须映射到虚拟桌面内部，而不是被当成屏幕外丢弃。
        abs_x, _abs_y = virtual_desktop_absolute(-1000, 0, metrics)
        self.assertTrue(0 < abs_x < 65535)
        # 越界坐标被夹取而不是溢出 16 位字段。
        self.assertEqual(virtual_desktop_absolute(-99999, -99999, metrics), (0, 0))
        self.assertEqual(virtual_desktop_absolute(99999, 99999, metrics), (65535, 65535))

    def test_scan_code_flag_and_extended_keys(self):
        from fukua_rpa.input_backend import key_scan_codes

        plain_vk, _scan, plain_flags = key_scan_codes("a", scan_codes=False)
        self.assertEqual(plain_vk, ord("A"))
        self.assertEqual(plain_flags, 0)

        # 方向键必须带扩展键标记；扫描码模式再加 SCANCODE 标记。
        _vk, _scan, flags = key_scan_codes("left", scan_codes=True)
        self.assertTrue(flags & 0x0001, "方向键必须带扩展键标记")
        self.assertTrue(flags & 0x0008, "扫描码模式必须带 KEYEVENTF_SCANCODE")

        self.assertEqual(key_scan_codes("f5", scan_codes=False)[0], 0x74)
        self.assertIsNone(key_scan_codes("不是按键", scan_codes=False))

    def test_punctuation_keys_use_authoritative_oem_virtual_keys(self):
        """SendInput 标点键名必须与 win32_api.HOTKEY_NAME_TO_VK 权威映射一致，
        不得把 ASCII 码当 VK 注入（FR-20261010-2302：'.' 曾注入 Delete、
        '[' 曾弹开始菜单、"'" 曾变方向键）。"""
        from fukua_rpa.input_backend import key_scan_codes
        from fukua_rpa.win32_api import HOTKEY_NAME_TO_VK

        punctuation = [";", "=", ",", "-", ".", "/", "`", "[", "\\", "]", "'"]
        for name in punctuation:
            expected = HOTKEY_NAME_TO_VK[name]
            vk, _scan, _flags = key_scan_codes(name, scan_codes=False)
            self.assertEqual(
                vk,
                expected,
                f"标点键 {name!r} 必须映射到 0x{expected:02X}，而不是 ASCII 回退值",
            )

    def test_single_char_fallback_limited_to_alnum_and_unknown_key_fails_explicitly(self):
        """单字符回退仅保留字母/数字（ASCII 与 VK 一致）；其余单字符显式
        返回 None 由后端抛 InputInjectionError，不得把任意 ASCII 当 VK。"""
        from fukua_rpa.input_backend import key_scan_codes

        self.assertEqual(key_scan_codes("q", scan_codes=False)[0], ord("Q"))
        self.assertEqual(key_scan_codes("7", scan_codes=False)[0], 0x37)
        self.assertIsNone(key_scan_codes("±", scan_codes=False))

    def test_numpad_key_names_match_authoritative_table(self):
        """小键盘键名必须与 win32_api.HOTKEY_NAME_TO_VK 的权威映射一致
        （FR-20261011-009）：缺少时 SendInput 模式对 num0..num9 抛
        InputInjectionError，而默认 PyAutoGUI 后端接受同样的键名。"""
        from fukua_rpa.input_backend import key_scan_codes
        from fukua_rpa.win32_api import HOTKEY_NAME_TO_VK

        for index in range(10):
            name = f"num{index}"
            expected = 0x60 + index
            self.assertEqual(HOTKEY_NAME_TO_VK.get(name), expected)
            result = key_scan_codes(name, scan_codes=False)
            self.assertIsNotNone(
                result, f"{name} 必须被 SendInput 键表接受，而不是返回 None"
            )
            self.assertEqual(
                result[0], expected, f"{name} 必须映射到 0x{expected:02X}"
            )

    def test_win_keys_carry_extended_flag(self):
        """LWin/RWin 物理扫描码带 E0 前缀，扫描码模式必须加扩展位
        （FR-RISK-20261010-2102），与方向键同理。"""
        from fukua_rpa.input_backend import key_scan_codes

        for name in ("win", "0x5B", "0x5C"):
            _vk, _scan, flags = key_scan_codes(name, scan_codes=True)
            self.assertTrue(
                flags & 0x0001, f"{name} 必须携带 KEYEVENTF_EXTENDEDKEY"
            )

    def test_volume_keys_carry_extended_flag_in_scan_code_mode(self):
        """音量三键的物理扫描码带 E0 前缀（FR-20261012-637）。

        扫描码模式（wVk=0、wScan=扫描码）缺少 KEYEVENTF_EXTENDEDKEY 时，
        0x30/0x2E/0x20 会被系统译为 'B'/'C'/'D'，SendInput 仍返回成功，
        步骤把错误字母当成已执行的音量键。
        """
        import ctypes

        from fukua_rpa.input_backend import create_input_backend, key_scan_codes

        map_virtual_key = ctypes.windll.user32.MapVirtualKeyW
        map_virtual_key.restype = ctypes.c_uint
        map_virtual_key.argtypes = [ctypes.c_uint, ctypes.c_uint]
        _MAPVK_VSC_TO_VK_EX = 3

        backend = create_input_backend("SendInput", key_scan_codes=True)
        for name, expected in (
            ("volumeup", 0xAF),
            ("volumedown", 0xAE),
            ("volumemute", 0xAD),
        ):
            vk, scan, flags = key_scan_codes(name, scan_codes=True)
            self.assertEqual(vk, expected)
            self.assertTrue(
                flags & 0x0001,
                f"{name} 的扫描码模式必须携带 KEYEVENTF_EXTENDEDKEY，"
                f"否则 0x{scan:02X} 会被系统译为字母键",
            )
            # 带 E0 前缀的完整扫描码必须反查回原 VK。
            self.assertEqual(
                int(map_virtual_key(scan | 0xE000, _MAPVK_VSC_TO_VK_EX)),
                expected,
                f"{name} 的 (scan=0x{scan:02X} + E0) 必须反查回 0x{expected:02X}",
            )
            # 真实入口：key_down 构造的事件必须带扩展位（_send 被替换，
            # 本用例不向系统注入任何输入）。
            with mock.patch.object(backend, "_send", return_value=True) as send:
                backend.key_down(name)
            self.assertEqual(send.call_count, 1)
            keyboard = send.call_args[0][0].ki
            self.assertTrue(keyboard.dwFlags & 0x0001)
            self.assertTrue(keyboard.dwFlags & 0x0008)

    def test_hex_virtual_key_beyond_word_range_is_rejected(self):
        """十六进制 VK 超过 0xFFFF 会被 ctypes WORD 截断（FR-20261012-638）：
        0x10000 → wVk=0、0x10041 → 'A'，失败被伪装成成功。"""
        from fukua_rpa.input_backend import (
            InputInjectionError,
            create_input_backend,
            key_scan_codes,
        )

        self.assertIsNone(
            key_scan_codes("0x10000", scan_codes=False),
            "超出 16 位的十六进制 VK 必须返回 None，不得静默截断后报成功",
        )
        self.assertIsNone(key_scan_codes("0x10041", scan_codes=True))
        self.assertIsNone(key_scan_codes("0xFFFFF", scan_codes=False))
        # 边界内的 VK 仍须放行。
        self.assertEqual(key_scan_codes("0x0", scan_codes=False)[0], 0x0)
        self.assertEqual(key_scan_codes("0xFFFF", scan_codes=False)[0], 0xFFFF)
        # 真实入口：必须抛 InputInjectionError，不得注入 VK 0。
        backend = create_input_backend("SendInput")
        with mock.patch.object(
            backend, "_send", return_value=True
        ) as send, self.assertRaises(InputInjectionError):
            backend.key_down("0x10000")
        self.assertEqual(send.call_count, 0)

    def test_virtual_desktop_absolute_reads_metrics_when_omitted(self):
        """docstring 承诺缺省 metrics 时从 Windows 读取，不得抛 TypeError
        （FR-20261010-2301）。"""
        from fukua_rpa.input_backend import (
            SendInputInputBackend,
            virtual_desktop_absolute,
        )

        metrics = (-1920, -100, 3840, 1180)
        with mock.patch.object(
            SendInputInputBackend, "_virtual_desktop_metrics", return_value=metrics
        ):
            self.assertEqual(
                virtual_desktop_absolute(-1000, 0),
                virtual_desktop_absolute(-1000, 0, metrics),
            )

    def test_position_raises_when_getcursorpos_fails(self):
        """GetCursorPos 失败必须显式抛错，让移动退化路径生效
        （FR-RISK-20261010-2101），不得静默返回 (0, 0)。"""
        import ctypes

        from fukua_rpa.input_backend import SendInputInputBackend

        backend = SendInputInputBackend()
        with (
            mock.patch.object(ctypes.windll.user32, "GetCursorPos", return_value=0),
            self.assertRaises(OSError),
        ):
            backend.position()

    def test_configure_input_backend_only_switches_on_explicit_mode(self):
        import fukua_rpa.input_backend as backend_module
        from fukua_rpa.engine import RPAEngine
        from fukua_rpa.input_backend import PyAutoGUIInputBackend, SendInputInputBackend

        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine(defer_backends=True)

        default_backend = engine.configure_input_backend("默认")
        self.assertIsInstance(default_backend, PyAutoGUIInputBackend)
        self.assertEqual(engine.input_backend_mode, "默认")
        self.assertFalse(engine.keyboard_scan_code)

        switched = engine.configure_input_backend("SendInput", key_scan_codes=True)
        self.assertIsInstance(switched, SendInputInputBackend)
        self.assertTrue(engine.keyboard_scan_code)
        self.assertIs(engine.input_backend, switched)
        del backend_module

    def test_move_settle_default_is_immediate_and_bounded(self):
        from fukua_rpa.engine import RPAEngine

        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine(defer_backends=True)
        engine.mouse_move_settle_s = 0.0
        self.assertTrue(engine._settle_after_move())
        engine.mouse_move_settle_s = 0.05
        waited = []
        with mock.patch.object(
            engine, "_wait_interruptibly", side_effect=lambda seconds, **kw: waited.append(seconds) or True
        ):
            self.assertTrue(engine._settle_after_move())
        self.assertEqual(waited, [0.05])
        # 停止请求必须让等待中断并返回 False。
        with mock.patch.object(engine, "_wait_interruptibly", return_value=False):
            self.assertFalse(engine._settle_after_move())

    def test_run_config_rejects_invalid_input_mode_and_settle(self):
        from fukua_rpa.run_config import EngineRunConfig, RunConfigError

        config = {"tasks": []}
        config["input_backend_mode"] = "不存在"
        with self.assertRaises(RunConfigError):
            EngineRunConfig.from_mapping(config, 0)
        config["input_backend_mode"] = "默认"
        config["mouse_move_settle_s"] = "9"
        with self.assertRaises(RunConfigError):
            EngineRunConfig.from_mapping(config, 0)

    def test_run_config_max_collect_zero_is_unlimited_and_bounded(self):
        """反馈 20261010-4：最长收集时间 0=不设限，且仍拒绝越界值。"""
        from fukua_rpa.run_config import EngineRunConfig, RunConfigError

        config = {"tasks": [], "multi_collect_max_s": "0"}
        parsed = EngineRunConfig.from_mapping(config, 0)
        self.assertEqual(parsed.multi_collect_max_s, 0.0)

        config["multi_collect_max_s"] = -1
        with self.assertRaises(RunConfigError):
            EngineRunConfig.from_mapping(config, 0)
        config["multi_collect_max_s"] = 601
        with self.assertRaises(RunConfigError):
            EngineRunConfig.from_mapping(config, 0)

    def test_sendinput_scroll_forwards_raw_wheel_delta(self):
        """FR-20261010-001（REVIEW-20261010-001）：滚轮增量必须原样传递。

        调用方（actions/flow._execute_scroll）传入的已经是"格数 × 120"的原始
        wheel delta（pyautogui Windows _scroll 直传 dwData=clicks，不内部放大），
        SendInput 后端不得再次乘以 WHEEL_DELTA，否则滚动量放大 120 倍。
        """
        from fukua_rpa.input_backend import SendInputInputBackend

        backend = SendInputInputBackend()
        sent = []
        with mock.patch.object(backend, "_send", side_effect=sent.append):
            backend.scroll(120)  # 1 格：调用方已换算为原始增量 120。
            backend.scroll(-240)  # -2 格：原始增量 -240。
            backend.scroll(0)
        self.assertEqual([event.mi.mouseData for event in sent], [120, 0xFFFFFF10, 0])
        # DWORD 字段的带符号语义：原始增量 -240 → 0xFFFFFF10（4294967056）。
        self.assertEqual(sent[1].mi.mouseData, (2**32 - 240))
        self.assertEqual(sent[0].mi.mouseData, 120)


    def test_input_abi_has_a_single_identity_across_modules(self):
        """FR-20261016-SUP-001：全进程只能有一个 INPUT 类型与一处签名绑定。

        此前 ``text_input`` 与 ``input_backend`` 各自定义结构，却共同调用同一个
        被 ctypes 缓存的 ``user32.SendInput``；于是 ``text_input`` 在导入时设置的
        ``argtypes`` 拒绝了后端构造的数组，显式可选的 SendInput 后端在任何事件
        进入 OS 之前就抛 ``ArgumentError``。修复后两者必须解析到同一个类。
        """
        import ctypes

        from fukua_rpa import input_backend, win32_input

        self.assertIs(text_input.INPUT, input_backend._INPUT)
        self.assertIs(text_input.INPUT, win32_input.INPUT)
        self.assertEqual(ctypes.sizeof(text_input.INPUT), 40)
        # INVARIANT：结构身份被 tests/test_window_diagnostics.py 守护，不得改型。
        self.assertIs(
            dict(text_input.KEYBDINPUT._fields_)["dwExtraInfo"], ctypes.c_void_p
        )

        argtypes = ctypes.windll.user32.SendInput.argtypes
        self.assertIsNotNone(argtypes, "共享 SendInput 必须已声明签名")
        self.assertIs(argtypes[1]._type_, text_input.INPUT)

    def test_unicode_text_still_uses_the_text_input_binding(self):
        """FR-20261016-SUP-001 回归守护：``text_input.SendInput`` 必须仍是被
        实际调用的打桩点。

        若修复把 Unicode 路径改到别处调用，现有守护的打桩会失效，
        ``send_unicode_text`` 将向真实前台窗口注入文本。本用例同时封死真实
        ``SendInput``，一旦被打桩绕开即失败。
        """
        counts = []

        def fake_send_input(count, _events, _size):
            counts.append(count)
            return count

        with (
            mock.patch.object(text_input, "SendInput", side_effect=fake_send_input),
            mock.patch(
                "ctypes.windll.user32.SendInput",
                side_effect=AssertionError("真实 SendInput 被打桩绕开"),
            ),
        ):
            completed = text_input.send_unicode_text("A" * 130)

        self.assertTrue(completed)
        # 130 个 UTF-16 单元按 64 分批：64/64/2 单元 → 128/128/4 个事件。
        self.assertEqual(counts, [128, 128, 4])

    def test_input_backend_send_still_uses_the_shared_cached_function(self):
        """FR-20261016-SUP-001 回归守护：``_send`` 必须仍在调用点动态解析
        ``ctypes.windll.user32.SendInput``，否则既有打桩失效并导致测试期真实注入。
        """
        from fukua_rpa.input_backend import InputInjectionError, SendInputInputBackend

        backend = SendInputInputBackend()
        with mock.patch(
            "ctypes.windll.user32.SendInput", return_value=0
        ), self.assertRaises(InputInjectionError):
            backend.move_to(10, 20)

    def test_input_abi_is_import_order_independent(self):
        """FR-20261016-SUP-001：导入顺序不得再决定 SendInput 是否可用。

        在只导入被测模块的全新子进程中核对；同进程内其它模块先声明过原型会掩盖
        问题（与 tests/test_smoke_win32_prototypes.py 记录的同类危害一致）。
        所有调用一律 ``cInputs == 0``，不注入任何事件。
        """
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        script = (
            "import ctypes, importlib\n"
            "for name in {mods!r}:\n"
            "    importlib.import_module(name)\n"
            "ti = importlib.import_module('fukua_rpa.text_input')\n"
            "ib = importlib.import_module('fukua_rpa.input_backend')\n"
            "assert ti.INPUT is ib._INPUT, 'INPUT identity differs'\n"
            "argtypes = ctypes.windll.user32.SendInput.argtypes\n"
            "assert argtypes and argtypes[1]._type_ is ti.INPUT\n"
            "arr = (ib._INPUT * 1)(ib._INPUT())\n"
            "ctypes.windll.user32.SendInput(0, arr, ctypes.sizeof(ib._INPUT))\n"
            "print('ok')\n"
        )
        orders = [
            ["fukua_rpa.input_backend", "fukua_rpa.text_input"],
            ["fukua_rpa.text_input", "fukua_rpa.input_backend"],
            ["fukua_rpa.input_backend"],
            ["fukua_rpa.text_input"],
        ]
        for mods in orders:
            with self.subTest(order=mods):
                completed = subprocess.run(
                    [sys.executable, "-c", script.format(mods=mods)],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    f"导入顺序 {mods} 失败：\n{completed.stdout}\n{completed.stderr}",
                )
                self.assertIn("ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
