"""Native matching fallback contract tests for ``VisionMatchMixin.native_find_targets``.

These lock in the three documented native fallback behaviors that the health
review (§5.11) flagged as lacking direct unit coverage:

* ``NATIVE_RESULT_CANCELLED`` (-3): must return ``[]`` -- an authoritative empty
  result -- never ``None``. Returning ``None`` would authorize an expensive
  OpenCV retry after the user already requested stop (SAFETY comment in source).
* ``NATIVE_RESULT_WORK_BUDGET`` (-2): must return ``None`` AND cache the
  rejection key so the identical call is not repeated.
* Circuit breaker (熔断): three consecutive generic native failures disable the
  native core for the remainder of the run; subsequent calls return ``None``
  without invoking the native DLL again.

``VisionMatchMixin`` is normally composed into ``VisionExecutionMixin``; here we
build a minimal harness that inherits the mixin and stubs every collaborating
attribute/method so the contract can be exercised in isolation.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from fukua_rpa.vision.core import (
    NATIVE_RESULT_CANCELLED,
    NATIVE_RESULT_WORK_BUDGET,
)
from fukua_rpa.vision.match import VisionMatchMixin


class _FakePerformance:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def increment(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def observe_since(self, _name: str, _ns: int) -> None:  # pragma: no cover
        return None


class _FakeNativeCore:
    def __init__(self, find_template_result, last_result_code=0, load_error="") -> None:
        self.available = True
        self._find_template_result = find_template_result
        self.last_result_code = last_result_code
        self.load_error = load_error
        self.call_count = 0
        self.last_args = ()
        self.last_kwargs = {}

    def find_template(self, *args, **kwargs):
        self.call_count += 1
        self.last_args = args
        self.last_kwargs = kwargs
        return self._find_template_result


class _NativeContractHarness(VisionMatchMixin):
    """Minimal object that provides everything ``native_find_targets`` touches."""

    # VS-20261027-02 后 native_find_targets 会先把配置区域裁到当前桌面，再用
    # "命中点必须落在本次生效区域内"的统一守卫过滤返回。原来的 (0,0,100,100)
    # 装不下本文件的命中点 (100,200)/(150,300)，会被正确判为区域外命中，因此夹具
    # 区域扩大到桌面范围内的 (0,0,400,400)；离屏与区域外的行为由专门用例覆盖。
    DESKTOP_LTRB = (0, 0, 4000, 2000)

    def __init__(self, native_core: _FakeNativeCore) -> None:
        self.use_native_core = True
        self.native_core = native_core
        self.performance = _FakePerformance()
        self.template_masked_cache_keys: set = set()
        self.transparent_template_reported: set = set()
        self.native_rejection_cache: set = set()
        self.native_parallel_mode = "auto"
        self.search_regions = [(0, 0, 400, 400)]

    # --- collaborating stubs ------------------------------------------------
    def matching_template_status(self, path, use_gray):  # noqa: ANN001
        return True, ""

    def template_uses_transparency(self, path) -> bool:  # noqa: ANN001
        return False

    def native_search_regions(self, quick_region, search_regions):  # noqa: ANN001
        return list(self.search_regions)

    def scale_options_for(self, cache_key):  # noqa: ANN001
        return (0.8, 1.2, 0.1)

    def preferred_scales_for(self, img_path, cache_key, use_gray):  # noqa: ANN001
        return ((), None)

    def template_dimensions(self, path):  # noqa: ANN001
        return (100, 40)

    def _recognition_point_to_input(self, x, y, is_physical):  # noqa: ANN001
        return (x, y)

    def check_stop_flag(self) -> bool:
        return False

    def verbose_log_enabled(self, backend) -> bool:  # noqa: ANN001
        return False

    def log_enabled(self, backend, critical=False) -> bool:  # noqa: ANN001
        return False

    def log(self, msg, backend, critical=False) -> None:  # noqa: ANN001
        return None


class TestNativeFindTargetsContract(unittest.TestCase):
    def setUp(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp:
            # Content is irrelevant: native_find_targets only checks existence.
            temp.write(b"\x89PNG\r\n\x1a\n")
            self.img = temp.name
        # input_rect_to_physical_regions is environment/multi-monitor dependent; pin it
        # to identity so the contract test is deterministic and offline.
        self._patcher = mock.patch(
            "fukua_rpa.coordinates.input_rect_to_physical_regions",
            side_effect=lambda region: [region],
        )
        self._patcher.start()
        # 同样固定"当前桌面范围"，否则 VS-20261027-02 的裁剪会随本机显示器变化。
        self._desktop_patcher = mock.patch(
            "fukua_rpa.coordinates._virtual_screen_rect",
            return_value=_NativeContractHarness.DESKTOP_LTRB,
        )
        self._desktop_patcher.start()

    def tearDown(self) -> None:
        self._desktop_patcher.stop()
        self._patcher.stop()
        os.unlink(self.img)

    def _harness(self, find_template_result, last_result_code=0, load_error=""):
        core = _FakeNativeCore(find_template_result, last_result_code, load_error)
        return _NativeContractHarness(core), core

    def test_success_returns_converted_matches(self) -> None:
        h, core = self._harness([(100, 200, 1.0, 0.95, 5)])
        out = h.native_find_targets(self.img, "k", 0.8, False)
        self.assertEqual(out, [(100, 200, 1.0, 0.95, 100.0, 40.0)])
        self.assertEqual(core.call_count, 1)

    def test_system_dpi_converts_native_center_and_matched_size(self) -> None:
        h, _core = self._harness([(100, 200, 1.0, 0.95, 5)])
        h._recognition_point_to_input = mock.Mock(return_value=(150, 300))
        with mock.patch(
            "fukua_rpa.coordinates.physical_to_input_rect",
            return_value=(75, 270, 150, 60),
        ):
            out = h.native_find_targets(self.img, "k", 0.8, False)

        self.assertEqual(out, [(150, 300, 1.0, 0.95, 150.0, 60.0)])

    def test_cross_monitor_regions_are_expanded_before_native_call(self) -> None:
        h, core = self._harness([(100, 200, 1.0, 0.95, 5)])
        expected = [(-200, 200, 200, 200), (0, 300, 300, 300)]
        with mock.patch(
            "fukua_rpa.coordinates.input_rect_to_physical_regions",
            return_value=expected,
        ):
            h.native_find_targets(self.img, "k", 0.8, False)
        self.assertEqual(core.last_args[1], expected)

    def test_system_dpi_fullscreen_search_uses_monitor_regions(self) -> None:
        h, core = self._harness([(100, 200, 1.0, 0.95, 5)])
        h.native_search_regions = lambda *_args, **_kwargs: []
        expected = [(0, 0, 2560, 1600), (-1920, 0, 1920, 1080)]
        with mock.patch(
            "fukua_rpa.coordinates.system_dpi_physical_monitor_regions",
            return_value=expected,
        ):
            h.native_find_targets(self.img, "k", 0.8, False)
        self.assertEqual(core.last_args[1], expected)

    def test_cancelled_returns_empty_list_not_none(self) -> None:
        # SAFETY: -3 must authorize NO OpenCV retry (would be None), but must
        # return [] so the run does not waste work after a user stop.
        h, core = self._harness(
            None, last_result_code=NATIVE_RESULT_CANCELLED, load_error=""
        )
        out = h.native_find_targets(self.img, "k", 0.8, False)
        self.assertEqual(out, [])
        self.assertEqual(core.call_count, 1)
        self.assertEqual(h.performance.counts.get("match.native_cancellations"), 1)

    def test_work_budget_caches_rejection_and_returns_none(self) -> None:
        h, core = self._harness(
            None,
            last_result_code=NATIVE_RESULT_WORK_BUDGET,
            load_error="work budget exceeded",
        )
        out = h.native_find_targets(self.img, "k", 0.8, False)
        self.assertIsNone(out)
        self.assertEqual(core.call_count, 1)
        # rejection key cached
        self.assertTrue(len(h.native_rejection_cache) >= 1)
        # identical second call hits the rejection cache and does NOT call the DLL
        out2 = h.native_find_targets(self.img, "k", 0.8, False)
        self.assertIsNone(out2)
        self.assertEqual(core.call_count, 1)

    def test_circuit_breaker_trips_after_three_failures(self) -> None:
        h, core = self._harness(None, last_result_code=0, load_error="boom")
        for _ in range(3):
            self.assertIsNone(h.native_find_targets(self.img, "k", 0.8, False))
        self.assertTrue(h._native_disabled_for_run)
        self.assertEqual(h.performance.counts.get("match.native_circuit_breaker_trips"), 1)
        # 4th call: circuit open, returns None WITHOUT invoking the native DLL
        out = h.native_find_targets(self.img, "k", 0.8, False)
        self.assertIsNone(out)
        self.assertEqual(core.call_count, 3)
        self.assertEqual(h.performance.counts.get("match.native_circuit_breaker_hits"), 1)

    def test_success_resets_failure_streak(self) -> None:
        h, core = self._harness([(1, 2, 1.0, 0.9, 5)])
        h.native_find_targets(self.img, "k", 0.8, False)  # success resets streak
        core._find_template_result = None  # now fail once
        h.native_find_targets(self.img, "k", 0.8, False)
        # a single failure after a success must not trip the breaker
        self.assertFalse(getattr(h, "_native_disabled_for_run", False))

    # --- VS-20261027-02: 空/离屏搜索区域与统一命中点守卫 --------------------

    def test_fully_offscreen_region_is_refused_without_calling_native(self) -> None:
        # 拔掉副屏后配置区域整体离屏：必须按未命中返回，绝不能让原生的
        # "裁空即回退整虚拟桌面"把搜索范围放大。
        h, core = self._harness([(100, 200, 1.0, 0.9, 5)])
        h.search_regions = [(4200, 100, 200, 100)]
        out = h.native_find_targets(self.img, "k", 0.8, False)
        self.assertEqual(out, [])
        self.assertEqual(core.call_count, 0)
        self.assertEqual(
            h.performance.counts.get("match.native_offscreen_regions_rejected"), 1
        )
        self.assertEqual(
            h.performance.counts.get("match.native_offscreen_search_refusals"), 1
        )

    def test_partially_offscreen_region_is_clipped_before_native_call(self) -> None:
        # 部分离屏：只裁掉超出桌面的那一段，可见部分照常搜索（不能整区域丢弃）。
        h, core = self._harness([(3950, 150, 1.0, 0.9, 5)])
        h.search_regions = [(3900, 100, 400, 100)]
        out = h.native_find_targets(self.img, "k", 0.8, False)
        self.assertEqual(core.last_args[1], [(3900, 100, 100, 100)])
        self.assertEqual(len(out), 1)
        self.assertIsNone(h.performance.counts.get("match.native_out_of_region_hits"))

    def test_hit_outside_effective_region_is_filtered(self) -> None:
        # 拔屏等效注入：即使后端返回了范围外的命中（原生自己的裁空回退），统一
        # 守卫也不把它交给注入侧，避免点击到配置区域之外的位置。
        h, core = self._harness([(2000, 1500, 1.0, 0.9, 5)])
        h.search_regions = [(0, 0, 400, 400)]
        out = h.native_find_targets(self.img, "k", 0.8, False)
        self.assertEqual(out, [])
        self.assertEqual(core.call_count, 1)
        self.assertEqual(h.performance.counts.get("match.native_out_of_region_hits"), 1)

    def test_multi_region_partial_offscreen_keeps_visible_hits_only(self) -> None:
        h, core = self._harness([(350, 350, 1.0, 0.9, 5), (2500, 2500, 1.0, 0.88, 5)])
        h.search_regions = [(300, 300, 200, 200), (4200, 0, 100, 100)]
        out = h.native_find_targets(self.img, "k", 0.8, False, find_all=True)
        self.assertEqual(core.last_args[1], [(300, 300, 200, 200)])
        self.assertEqual([hit[0] for hit in out], [350])
        self.assertEqual(h.performance.counts.get("match.native_out_of_region_hits"), 1)

    def test_unconfigured_search_still_uses_whole_desktop_and_keeps_hits(self) -> None:
        # 用户没有配置区域时，整桌面搜索本身就是意图，守卫不得反过来误杀命中。
        h, core = self._harness([(2000, 1500, 1.0, 0.9, 5)])
        h.search_regions = []
        expected = [(0, 0, 4000, 2000)]
        with mock.patch(
            "fukua_rpa.coordinates.system_dpi_physical_monitor_regions",
            return_value=expected,
        ):
            out = h.native_find_targets(self.img, "k", 0.8, False)
        self.assertEqual(core.last_args[1], expected)
        self.assertEqual(len(out), 1)


class TestEmergencyStopDoesNotSatisfyDisappearCondition(unittest.TestCase):
    """急停不得使"图片消失"条件成立（VS-20261027-04 的跨层链路）。

    链路：原生取消码 ``-3`` → ``native_find_targets`` 返回 ``[]`` → 条件层把它读成
    "未找到且非错误" → "图片消失"据此判为满足 → 聚合返回 ``condition_true``，日志与
    轨迹就把用户按下急停的那一轮记成"条件已满足"，并可能据此解析分支跳转。上面的
    ``test_cancelled_returns_empty_list_not_none`` 只锁住 ``[]`` 契约；本类用真实
    ``RPAEngine`` 覆盖从取消到条件聚合的完整链路。
    """

    def _engine(self):
        from fukua_rpa.engine import RPAEngine

        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine(defer_backends=True)
        engine.callback_msg = mock.Mock()
        engine.log_level = -1
        return engine

    def test_stop_requested_during_recognition_returns_stopped(self) -> None:
        engine = self._engine()
        run_id = engine.lifecycle.reserve()
        self.assertIsNotNone(run_id)
        calls = []

        def _cancelled_disappearance(*_args, **_kwargs):
            # 识别被取消：native 折叠成 [] → 条件层读到 (未找到, 详情, 非错误)。
            calls.append(1)
            engine.lifecycle.request_stop()
            return False, "识别已取消", False

        engine.find_condition_image = mock.Mock(side_effect=_cancelled_disappearance)
        task = {
            "until_logic": "任一满足",
            "until_cond1_en": True,
            "until_cond1_mode": "图片消失",
            "until_cond1_image": "C:/condition-template.png",
        }
        status = engine.execute_until_conditions(task, {"step": 1, "loop": 1}, True)

        self.assertEqual(len(calls), 1)
        self.assertEqual(status, "stopped")
        engine.lifecycle.finish(run_id, "stopped")

    def test_without_stop_disappearance_still_satisfies(self) -> None:
        # 副作用配对核对：修复不得让真实的"图片已消失"也判停。
        engine = self._engine()
        engine.find_condition_image = mock.Mock(return_value=(False, "图片已消失", False))
        task = {
            "until_logic": "任一满足",
            "until_cond1_en": True,
            "until_cond1_mode": "图片消失",
            "until_cond1_image": "C:/condition-template.png",
        }
        status = engine.execute_until_conditions(task, {"step": 1, "loop": 1}, True)
        self.assertEqual(status, "condition_true")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
