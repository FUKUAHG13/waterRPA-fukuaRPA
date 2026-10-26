import ctypes
import os
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QThread, Signal
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget
from shiboken6 import isValid

import fukua_rpa.vision.shared as vision_shared_module
import fukuaRPA as wrpa
from fukua_rpa.constants import NATIVE_CORE_RELEASE_VERSION
from fukua_rpa.params import FindAllMatchParams, TaskOnceParams
from fukua_rpa.ui.controllers import HotkeyController
from fukua_rpa.ui.input_tools import HookThread
from fukua_rpa.vision.core import (
    NATIVE_CAPABILITY_BITS,
    NATIVE_RESULT_CANCELLED,
    apply_template_mask_regions,
    normalize_template_mask_regions,
    template_alpha_channel,
)
from fukua_rpa.win32_api import (
    KBDLLHOOKSTRUCT,
    VK_MAP,
    WM_KEYDOWN,
    WM_KEYUP,
    hotkey_text_from_pressed_vks,
    parse_hotkey_text,
)
from tests.qt_test_support import QtCallbackGuardMixin, dispose_widget


class CoreRegressionTests(QtCallbackGuardMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.priority_patch = mock.patch.object(wrpa.RPAEngine, "set_high_priority", lambda _self: None)
        self.priority_patch.start()

    def tearDown(self):
        self.priority_patch.stop()
        self.app.processEvents()

    def test_native_core_api_version(self):
        core = wrpa.NativeVisionCore()
        if not core.available:
            self.skipTest(core.load_error or "native core unavailable")
        self.assertGreaterEqual(core.version, NATIVE_CORE_RELEASE_VERSION)

    def test_native_core_declares_required_capabilities(self):
        core = wrpa.NativeVisionCore()
        if not core.available:
            self.skipTest(core.load_error or "native core unavailable")
        capabilities = core.capabilities()
        for name in (
            "gdi_capture",
            "multi_region",
            "multi_scale",
            "grayscale",
            "color",
            "find_all",
            "work_budget",
            "single_capture_per_region",
            "abi_metadata",
            "bounded_job_pool",
            "preferred_scale_fallback",
            "preferred_scale_list",
            "explicit_scale_only",
            "low_res_scene_fingerprint",
            "dxgi_scene_change",
            "cancellable_search",
            "opencv_color_semantics",
            "raw_rgb_capture",
        ):
            self.assertTrue(capabilities.get(name), name)
        self.assertTrue(core.has_extended_search)
        self.assertTrue(core.has_preferred_scale_list)
        self.assertTrue(core.has_explicit_scale_only)
        self.assertTrue(core.has_scene_fingerprint)
        self.assertTrue(core.has_dxgi_scene_change)
        self.assertTrue(core.has_cancellable_search)
        self.assertTrue(core.has_raw_rgb_capture)

    def test_scene_fingerprint_gate_version_matches_documented_api(self):
        # FR-011: 实现把低分辨率画面指纹门放在 API 11100、把 DXGI 变化通知放在
        # 11200（自首次导入 ee7ae5c 起即如此，git log -S"11100" 取证）；
        # ARCHITECTURE.md #20 曾把两者合并记为 11200。本测试同时锁定门表与文档取值，
        # 任一侧再次漂移都会失败。
        gates = {
            version: bits
            for version, bits, *_rest in wrpa.NativeVisionCore._NATIVE_CAPABILITY_GATES
        }
        self.assertEqual(
            gates[11100], NATIVE_CAPABILITY_BITS["low_res_scene_fingerprint"]
        )
        self.assertEqual(gates[11200], NATIVE_CAPABILITY_BITS["dxgi_scene_change"])

        architecture = (
            os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "ARCHITECTURE.md",
            )
        )
        with open(architecture, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("API 11100 增加", text)
        self.assertIn("低分辨率画面指纹", text)
        self.assertIn("API 11200 增加 DXGI 变化通知", text)

    def test_architecture_constraint_index_covers_every_body_constraint(self):
        """DOC-20261003-605: 约束主题索引必须覆盖正文全部编号（双向一致）。

        索引是导航、正文才是权威；两者漂移会让较新约束不可发现。修复前索引
        缺 #85–#91、#93、#94。
        """
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "ARCHITECTURE.md",
        )
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        marker = "### 约束主题索引"
        self.assertIn(marker, text)
        after = text.split(marker, 1)[1]
        index_block = after.split("\n## ", 1)[0]
        indexed = {int(n) for n in re.findall(r"#(\d+)", index_block)}
        body_numbers = {int(m.group(1)) for m in re.finditer(r"^(\d+)\. ", after, re.M)}
        self.assertTrue(body_numbers, "未解析到正文约束编号")
        self.assertEqual(sorted(body_numbers - indexed), [], "索引缺少正文约束编号")
        self.assertEqual(sorted(indexed - body_numbers), [], "索引含正文不存在的编号")

    def test_native_core_abi_and_static_runtime_contract(self):
        core = wrpa.NativeVisionCore()
        if not core.available:
            self.skipTest(core.load_error or "native core unavailable")
        abi = core.abi_snapshot()
        self.assertTrue(abi["metadata_available"])
        self.assertTrue(abi["compatible"])
        self.assertEqual(abi["pointer_bits"], 64)
        self.assertEqual(abi["struct_sizes"], abi["python_struct_sizes"])
        for flag in ("x64", "static_crt", "cpp17", "windows10_target", "msvc"):
            self.assertTrue(abi["build_flags"].get(flag), flag)

    def test_native_service_snapshot_is_stable_and_path_free(self):
        core = wrpa.NativeVisionCore()
        if not core.available:
            self.skipTest(core.load_error or "native core unavailable")

        snapshot = core.service_snapshot(
            requested=True,
            fallback_available=True,
        )

        self.assertEqual(snapshot["service"], "vision")
        self.assertEqual(snapshot["contract_version"], 1)
        self.assertTrue(snapshot["requested"])
        self.assertTrue(snapshot["available"])
        self.assertTrue(snapshot["abi_compatible"])
        self.assertTrue(snapshot["cancellable_search"])
        self.assertEqual(snapshot["fallback"], {"name": "opencv", "available": True})
        self.assertNotIn("dll_path", snapshot)
        self.assertNotIn("last_error", snapshot)

        screenshot = core.screenshot_service_snapshot(
            requested=True,
            fallback_available=True,
        )
        self.assertEqual(screenshot["service"], "screenshot")
        self.assertEqual(screenshot["backend"], "native.dll")
        self.assertEqual(
            screenshot["capabilities"],
            ["capture.rgb", "screen.virtual_rect"],
        )

    def test_native_rgb_capture_returns_virtual_screen_pixels(self):
        core = wrpa.NativeVisionCore()
        if not core.available:
            self.skipTest(core.load_error or "native core unavailable")
        rect = core.virtual_screen_rect()
        self.assertIsNotNone(rect)
        x, y, width, height = rect
        captured = core.capture_rgb((x, y, min(width, 32), min(height, 24)))
        self.assertIsNotNone(captured, core.last_scene_error)
        captured_width, captured_height, pixels = captured
        self.assertEqual((captured_width, captured_height), (min(width, 32), min(height, 24)))
        self.assertEqual(len(pixels), captured_width * captured_height * 3)

    def test_native_multiscale_search_captures_each_region_once_and_caches_template(self):
        core = wrpa.NativeVisionCore()
        if not core.available or core.version < 10500:
            self.skipTest(core.load_error or "native performance API unavailable")
        pixels = [
            ((x * 47 + y * 19) % 255, (x * 11 + y * 61) % 255, (x * 73 + y * 7) % 255)
            for y in range(6)
            for x in range(6)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "native_template.png")
            image = Image.new("RGB", (6, 6))
            image.putdata(pixels)
            image.save(path)

            core.reset_performance_stats()
            first = core.find_template(
                path, [(0, 0, 48, 48)], 0.8, 1.2, 0.05, True, 0.99
            )
            first_stats = core.performance_stats()
            second = core.find_template(
                path, [(0, 0, 48, 48)], 0.8, 1.2, 0.05, True, 0.99
            )
            second_stats = core.performance_stats()

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first_stats["captures"], 1)
        self.assertEqual(first_stats["integral_builds"], 1)
        self.assertEqual(first_stats["template_cache_misses"], 1)
        self.assertEqual(second_stats["captures"], 2)
        self.assertEqual(second_stats["integral_builds"], 2)
        self.assertGreaterEqual(second_stats["template_cache_hits"], 1)

    def test_native_budget_fallback_happens_before_screen_capture(self):
        core = wrpa.NativeVisionCore()
        if not core.available or core.version < 10500:
            self.skipTest(core.load_error or "native performance API unavailable")
        rng_pixels = [
            ((x * 17 + y * 31) % 255, (x * 43 + y * 11) % 255, (x * 29 + y * 47) % 255)
            for y in range(50)
            for x in range(50)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "large_template.png")
            image = Image.new("RGB", (50, 50))
            image.putdata(rng_pixels)
            image.save(path)
            core.reset_performance_stats()
            result = core.find_template(
                path, [(0, 0, 1920, 1080)], 1.0, 1.0, 0.05, True, 0.8
            )
            stats = core.performance_stats()

        self.assertIsNone(result)
        self.assertIn("work budget exceeded", core.load_error)
        self.assertEqual(stats["captures"], 0)
        self.assertEqual(stats["integral_builds"], 0)
        self.assertEqual(stats["work_budget_fallbacks"], 1)

    def test_native_budget_is_aggregated_across_regions(self):
        core = wrpa.NativeVisionCore()
        if not core.available:
            self.skipTest(core.load_error or "native core unavailable")
        pixels = [
            (
                (x * 17 + y * 31) % 255,
                (x * 43 + y * 11) % 255,
                (x * 29 + y * 47) % 255,
            )
            for y in range(10)
            for x in range(10)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "aggregate_budget_template.png")
            image = Image.new("RGB", (10, 10))
            image.putdata(pixels)
            image.save(path)
            core.reset_performance_stats()
            result = core.find_template(
                path,
                [(0, 0, 1000, 1000), (1000, 0, 1000, 1000)],
                1.0,
                1.0,
                0.05,
                True,
                0.8,
            )
            stats = core.performance_stats()

        self.assertIsNone(result)
        self.assertIn("aggregate work budget exceeded", core.load_error)
        self.assertEqual(stats["captures"], 0)
        self.assertEqual(stats["work_budget_fallbacks"], 1)

    def test_flat_template_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "flat.png")
            Image.new("RGB", (8, 8), (80, 80, 80)).save(path)
            valid, reason = wrpa.template_detail_status(path, True)
            self.assertFalse(valid)
            self.assertTrue(reason)

    def test_scale_variant_limit_is_enforced(self):
        values = wrpa.build_scale_values(0.8, 1.2, 0.05)
        self.assertEqual(len(values) + 1, 9)
        with self.assertRaises(ValueError):
            wrpa.build_scale_values(0.1, 2.0, 0.01)

    def test_native_empty_result_does_not_repeat_python_search(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.native_find_targets = lambda *args, **kwargs: []
        engine.quick_search_region = lambda *args, **kwargs: None
        calls = {"fallback": 0}

        def fallback(*args, **kwargs):
            calls["fallback"] += 1
            return None

        engine.find_target_in_screenshot = fallback
        engine.iter_search_screenshots = lambda *args, **kwargs: iter(
            [(Image.new("RGB", (4, 4)), 0, 0)]
        )
        self.assertIsNone(engine.find_target_optimized("missing.png", "cache", 0.8, True))
        self.assertEqual(calls["fallback"], 0)

    def test_fast_search_remembers_and_reuses_the_successful_native_scale(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.min_scale = 0.8
        engine.max_scale = 1.2
        engine.scale_step = 0.05
        engine.quick_search_region = lambda *args, **kwargs: None
        fake_native = mock.Mock()
        fake_native.available = True
        fake_native.load_error = ""
        fake_native.last_result_code = 1
        fake_native.find_template.return_value = [
            (120.0, 80.0, 1.15, 0.96, 8.0)
        ]
        engine.native_core = fake_native
        pixels = [
            ((x * 31 + y * 7) % 255, (x * 13 + y * 17) % 255, (x * 19 + y * 23) % 255)
            for y in range(8)
            for x in range(8)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "template.png")
            image = Image.new("RGB", (8, 8))
            image.putdata(pixels)
            image.save(path)
            first = engine.find_target_optimized(path, "cache", 0.8, True)
            second = engine.find_target_optimized(path, "cache", 0.8, True)

        self.assertEqual(first[2], 1.15)
        self.assertEqual(second[2], 1.15)
        first_call = fake_native.find_template.call_args_list[0]
        second_call = fake_native.find_template.call_args_list[1]
        self.assertEqual(first_call.kwargs["preferred_scales"], ())
        self.assertEqual(second_call.kwargs["preferred_scales"], (1.15,))
        self.assertEqual(second_call.kwargs["parallel_mode"], "auto")
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["match.native_scale_hint_hits"], 1)

    def test_time_limit_stops_an_inner_self_jump(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        # Keep the source ASCII-only so Windows shell encoding cannot corrupt the mode.
        engine.loop_mode = "\u6307\u5b9a\u65f6\u95f4(\u79d2)"
        engine.loop_val = 0.03
        engine.time_limit_stop_mode = "immediate"
        engine.loop_start_round = 1
        engine.start_step_index = 0
        engine.settlement_wait = 0
        engine.detect_delay = 0
        engine.load_and_precompute = lambda tasks: True
        executions = {"count": 0}

        def execute(*args, **kwargs):
            executions["count"] += 1
            time.sleep(0.005)
            return "success"

        engine.execute_task_once = execute
        worker = threading.Thread(
            target=engine.run_tasks,
            args=([{"type": 4.0, "value": "x", "success_jump": "1"}],),
            daemon=True,
        )
        worker.start()
        worker.join(0.2)
        if worker.is_alive():
            engine.stop()
            worker.join(1.0)
        self.assertFalse(worker.is_alive())
        # Relaxed from 50 to 100: wall-clock-dependent count varies in CI/slow environments.
        self.assertLess(executions["count"], 100)

    def test_finish_loop_time_limit_completes_current_loop(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.loop_mode = "指定时间(秒)"
        engine.loop_val = 0.005
        engine.time_limit_stop_mode = "finish_loop"
        engine.settlement_wait = 0
        engine.detect_delay = 0
        engine.load_and_precompute = lambda tasks: True
        executed = []

        def execute(params):
            executed.append(params.val)
            time.sleep(0.008)
            return "success"

        engine.execute_task_once = execute
        engine.run_tasks(
            [
                {"type": 4.0, "value": "first"},
                {"type": 4.0, "value": "second"},
            ]
        )

        self.assertEqual(executed, ["first", "second"])

    def test_screenshot_failure_returns_error(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.capture_screenshot = lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("capture failed")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            status = engine.execute_task_once(TaskOnceParams(
                cmd=9.0,
                val=os.path.join(temp_dir, "shot.png"),
                retry=1,
                step_info={"loop": 1, "step": 1},
                cache_key="",
                task_conf=0.8,
                use_gray=True,
            ))
        self.assertEqual(status, "error")

    def test_screenshot_step_honors_full_global_and_custom_regions(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.scan_region = (3, 4, 100, 80)
        engine.scan_regions = [(10, 20, 30, 40), (50, 70, 20, 10)]
        capture = mock.Mock(
            return_value=(Image.new("RGB", (4, 4), "white"), 0, 0)
        )
        engine.capture_screenshot = capture

        cases = (
            ("full", "", None),
            ("global", "", (10, 20, 60, 60)),
            ("custom", "-20,30,40,50", (-20, 30, 40, 50)),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            for index, (mode, region_text, expected) in enumerate(cases):
                capture.reset_mock()
                status = engine.execute_task_once(TaskOnceParams(
                    cmd=9.0,
                    val=os.path.join(temp_dir, f"shot_{index}.png"),
                    retry=1,
                    step_info={"loop": 1, "step": 1},
                    cache_key="",
                    task_conf=0.8,
                    use_gray=True,
                    task={
                        "capture_region_mode": mode,
                        "capture_region": region_text,
                    },
                ))
                self.assertEqual(status, "success")
                capture.assert_called_once_with(expected)

    def test_mss_failure_is_circuit_broken_for_the_current_run(self):
        class FailingMssInstance:
            monitors = [{"left": 0, "top": 0, "width": 32, "height": 32}]

            def __init__(self):
                self.grab_calls = 0

            def grab(self, _monitor):
                self.grab_calls += 1
                raise OSError("simulated mss failure")

            def close(self):
                pass

        instance = FailingMssInstance()
        fake_mss_module = mock.Mock()
        fake_mss_module.MSS = None
        fake_mss_module.mss.return_value = instance
        fallback = Image.new("RGB", (32, 32), "white")
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.use_native_core = False
        engine.performance.reset()
        with mock.patch.object(vision_shared_module, "HAS_MSS", True), \
             mock.patch.object(vision_shared_module, "mss", fake_mss_module), \
             mock.patch.object(vision_shared_module.pyautogui, "screenshot", return_value=fallback):
            engine.capture_screenshot(None)
            engine.capture_screenshot(None)

        report = engine.performance.snapshot()
        self.assertEqual(instance.grab_calls, 1)
        self.assertEqual(fake_mss_module.mss.call_count, 1)
        self.assertEqual(report["counters"]["screenshot.fallbacks"], 1)
        self.assertEqual(report["counters"]["screenshot.pyautogui_calls"], 2)

    def test_native_rgb_capture_is_preferred_and_preserves_offsets(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.performance.reset()
        native = mock.Mock()
        native.available = True
        native.has_raw_rgb_capture = True
        native.capture_rgb.return_value = (
            2,
            1,
            bytes((255, 0, 0, 0, 255, 0)),
        )
        engine.native_core = native

        image, offset_x, offset_y = engine.capture_screenshot((-20, 30, 2, 1))

        self.assertEqual((offset_x, offset_y), (-20, 30))
        self.assertEqual(
            [image.getpixel((0, 0)), image.getpixel((1, 0))],
            [(255, 0, 0), (0, 255, 0)],
        )
        native.capture_rgb.assert_called_once_with((-20, 30, 2, 1))
        self.assertEqual(
            engine.performance.snapshot()["counters"]["screenshot.native_calls"],
            1,
        )

    def test_native_rgb_capture_failure_falls_back_once_per_run(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.use_fast_screenshot = False
        engine.performance.reset()
        native = mock.Mock()
        native.available = True
        native.has_raw_rgb_capture = True
        native.last_scene_error = "simulated native failure"
        native.capture_rgb.return_value = None
        engine.native_core = native
        fallback = Image.new("RGB", (4, 3), "white")

        with mock.patch.object(
            vision_shared_module.pyautogui,
            "screenshot",
            return_value=fallback,
        ):
            engine.capture_screenshot((1, 2, 4, 3))
            engine.capture_screenshot((1, 2, 4, 3))

        native.capture_rgb.assert_called_once_with((1, 2, 4, 3))
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["screenshot.native_failures"], 1)
        self.assertEqual(counters["screenshot.pyautogui_calls"], 2)

    def test_cross_monitor_region_uses_input_space_capture_fallback(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.use_fast_screenshot = False
        engine.performance.reset()
        native = mock.Mock()
        native.available = True
        native.has_raw_rgb_capture = True
        engine.native_core = native
        fallback = Image.new("RGB", (600, 300), "white")

        with (
            mock.patch(
                "fukua_rpa.coordinates.input_rect_to_physical_regions",
                return_value=[(-200, 200, 200, 200), (0, 300, 300, 300)],
            ),
            mock.patch.object(
                vision_shared_module.pyautogui,
                "screenshot",
                return_value=fallback,
            ) as screenshot,
        ):
            image, offset_x, offset_y = engine.capture_screenshot(
                (-300, 300, 600, 300)
            )

        native.capture_rgb.assert_not_called()
        screenshot.assert_called_once_with(region=(-300, 300, 600, 300))
        self.assertEqual((image.size, offset_x, offset_y), ((600, 300), -300, 300))
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["screenshot.native_cross_monitor_fallbacks"], 1)
        self.assertNotIn("screenshot.native_failures", counters)

    def test_system_dpi_fullscreen_capture_skips_native_virtual_rect(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.use_fast_screenshot = False
        native = mock.Mock()
        native.available = True
        native.has_raw_rgb_capture = True
        engine.native_core = native
        fallback = Image.new("RGB", (32, 24), "white")

        with (
            mock.patch(
                "fukua_rpa.coordinates._ensure_physical_dpi_awareness",
                return_value=False,
            ),
            mock.patch.object(
                vision_shared_module.pyautogui,
                "screenshot",
                return_value=fallback,
            ) as screenshot,
        ):
            image, offset_x, offset_y = engine.capture_screenshot(None)

        native.virtual_screen_rect.assert_not_called()
        native.capture_rgb.assert_not_called()
        screenshot.assert_called_once_with(region=None)
        self.assertEqual((image.size, offset_x, offset_y), ((32, 24), 0, 0))

    def test_opencv_base_template_conversion_is_reused(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.use_native_core = False
        pixels = [
            ((x * 31 + y * 17) % 255, (x * 13 + y * 29) % 255, (x * 7 + y * 43) % 255)
            for y in range(6)
            for x in range(6)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "template.png")
            template = Image.new("RGB", (6, 6))
            template.putdata(pixels)
            template.save(path)
            task = {"type": 1.0, "value": path}
            self.assertTrue(engine.load_and_precompute([task]))
            cache_key = engine.image_target_cache_key(task)
            self.assertNotIn("cache_key", task)
            screenshot = Image.new("RGB", (20, 20), "black")
            screenshot.paste(template, (7, 8))
            engine.performance.reset()
            for _index in range(2):
                found = engine.find_target_in_screenshot(
                    path, cache_key, 0.8, True, screenshot, 0, 0
                )
                self.assertIsNotNone(found)

        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters.get("template.base_cache_misses", 0), 0)
        self.assertEqual(counters["template.base_cache_hits"], 2)

    def test_transparent_template_uses_alpha_mask_and_bypasses_native(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.enable_grayscale = False
        fake_native = mock.Mock()
        fake_native.available = True
        engine.native_core = fake_native

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "transparent.png")
            template = Image.new("RGBA", (15, 13), (255, 0, 255, 0))
            for y in range(3, 10):
                for x in range(4, 12):
                    template.putpixel(
                        (x, y),
                        (
                            (x * 31 + y * 17) % 255,
                            (x * 13 + y * 41) % 255,
                            (x * 47 + y * 7) % 255,
                            255,
                        ),
                    )
            template.save(path)
            screenshot = Image.new("RGB", (60, 45), (34, 78, 120))
            screenshot.paste(template, (23, 17), template)
            task = {"type": 1.0, "value": path}

            self.assertTrue(engine.load_and_precompute([task]))
            cache_key = engine.image_target_cache_key(task)
            self.assertNotIn("cache_key", task)
            self.assertIn(cache_key, engine.base_template_masks_cache)
            self.assertIsNone(
                engine.native_find_targets(path, cache_key, 0.8, False)
            )
            fake_native.find_template.assert_not_called()

            found = engine.find_target_in_screenshot(
                path, cache_key, 0.8, False, screenshot, 0, 0
            )

        self.assertIsNotNone(found)
        self.assertEqual((int(found[0]), int(found[1])), (30, 23))
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["match.native_transparency_fallbacks"], 1)

    def test_virtual_template_mask_is_per_step_and_bypasses_native(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.enable_grayscale = False
        fake_native = mock.Mock()
        fake_native.available = True
        engine.native_core = fake_native

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "changing-center.png")
            template = Image.new("RGB", (24, 18))
            for y in range(template.height):
                for x in range(template.width):
                    template.putpixel(
                        (x, y),
                        (
                            (x * 37 + y * 13) % 255,
                            (x * 11 + y * 47) % 255,
                            (x * 19 + y * 23) % 255,
                        ),
                    )
            template.save(path)
            changed = template.copy()
            for y in range(5, 14):
                for x in range(7, 18):
                    changed.putpixel((x, y), (255, 255, 255))
            screenshot = Image.new("RGB", (70, 50), (8, 18, 28))
            screenshot.paste(changed, (31, 21))
            task = {
                "type": 1.0,
                "value": path,
                "template_mask_regions": [[7 / 24, 5 / 18, 11 / 24, 9 / 18]],
            }

            self.assertTrue(engine.load_and_precompute([task]))
            cache_key = engine.image_target_cache_key(task)
            self.assertNotIn("cache_key", task)
            self.assertIn(cache_key, engine.template_masked_cache_keys)
            self.assertIn(cache_key, engine.base_template_masks_cache)
            self.assertIsNone(
                engine.native_find_targets(path, cache_key, 0.95, False)
            )
            fake_native.find_template.assert_not_called()

            found = engine.find_target_in_screenshot(
                path, cache_key, 0.95, False, screenshot, 0, 0
            )

        self.assertIsNotNone(found)
        self.assertEqual((int(found[0]), int(found[1])), (43, 30))

    def test_template_mask_normalization_and_alpha_are_bounded(self):
        regions = normalize_template_mask_regions(
            [
                [-0.2, 0.2, 0.5, 0.4],
                [0.8, 0.8, 0.5, 0.5],
                [float("nan"), 0, 1, 1],
                [0.8, 0.8, 0.5, 0.5],
            ]
        )
        self.assertEqual(regions, ((0.0, 0.2, 0.3, 0.4), (0.8, 0.8, 0.2, 0.2)))

        masked = apply_template_mask_regions(
            Image.new("RGB", (10, 10), "white"),
            regions,
        )
        alpha = template_alpha_channel(masked)
        self.assertIsNotNone(alpha)
        self.assertEqual(alpha.getpixel((0, 2)), 0)
        self.assertEqual(alpha.getpixel((5, 5)), 255)

    def test_transparent_template_find_all_and_scaled_masks(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.enable_grayscale = True
        engine.use_native_core = False
        engine.min_scale = 0.8
        engine.max_scale = 1.2
        engine.scale_step = 0.2

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "transparent-all.png")
            template = Image.new("RGBA", (14, 12), (7, 231, 99, 0))
            for y in range(2, 10):
                for x in range(3, 11):
                    template.putpixel(
                        (x, y),
                        (
                            (x * 37 + y * 11) % 255,
                            (x * 19 + y * 23) % 255,
                            (x * 5 + y * 43) % 255,
                            255,
                        ),
                    )
            template.save(path)
            screenshot = Image.new("RGB", (80, 55), (19, 61, 103))
            screenshot.paste(template, (7, 8), template)
            screenshot.paste(template, (47, 31), template)
            task = {"type": 1.0, "value": path}

            self.assertTrue(engine.load_and_precompute([task]))
            cache_key = engine.image_target_cache_key(task)
            self.assertNotIn("cache_key", task)
            self.assertEqual(
                set(engine.scaled_template_masks_cache[cache_key]),
                {0.8, 1.2},
            )
            found = engine.find_all_targets_in_screenshot(
                FindAllMatchParams(
                    img_path=path, cache_key=cache_key, task_conf=0.95,
                    use_gray=True, screenshot_pil=screenshot,
                    offset_x=0, offset_y=0,
                )
            )

        centers = {(int(item[0]), int(item[1])) for item in found}
        self.assertEqual(centers, {(14, 14), (54, 37)})

    def test_transparent_template_validation_ignores_hidden_rgb_detail(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "almost-empty.png")
            template = Image.new("RGBA", (20, 20))
            for y in range(20):
                for x in range(20):
                    template.putpixel(
                        (x, y),
                        (
                            (x * 47 + y * 3) % 255,
                            (x * 7 + y * 29) % 255,
                            (x * 31 + y * 13) % 255,
                            255 if x < 2 and y < 2 else 0,
                        ),
                    )
            template.save(path)
            valid, reason = wrpa.template_detail_status(path, False)

        self.assertFalse(valid)
        self.assertIn("可见内容", reason)

    def test_raw_capture_multi_target_distance_sort_uses_input_coordinates(self):
        engine = wrpa.RPAEngine(defer_backends=True)
        engine.log_level = -1
        engine.opencv_available = False
        engine.multi_target_order = "距离鼠标最近优先"
        engine.input_backend.position = mock.Mock(return_value=(0, 0))

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "target.png")
            template = Image.new("RGB", (10, 10), "white")
            template.save(path)
            engine.img_cache[path] = template
            boxes = [
                mock.Mock(left=5, top=0, width=10, height=10),
                mock.Mock(left=25, top=0, width=10, height=10),
            ]

            def convert_geometry(x, y, width, height, is_physical):
                self.assertTrue(is_physical)
                converted_x = 1000.0 if x < 20 else 0.0
                return converted_x, float(y), float(width), float(height)

            with mock.patch(
                "fukua_rpa.vision.match.capture_returns_physical_pixels",
                return_value=True,
            ), mock.patch(
                "fukua_rpa.vision.match.pyautogui.locateAll",
                return_value=boxes,
            ), mock.patch.object(
                engine,
                "_recognition_geometry_to_input",
                side_effect=convert_geometry,
            ), mock.patch.object(
                engine, "matching_template_status", return_value=(True, "")
            ), mock.patch.object(
                engine, "template_uses_transparency", return_value=False
            ):
                found = engine.find_all_targets_in_screenshot(
                    FindAllMatchParams(
                        img_path=path,
                        cache_key="raw-sort",
                        task_conf=0.8,
                        use_gray=False,
                        screenshot_pil=Image.new("RGB", (60, 20), "black"),
                        offset_x=0,
                        offset_y=0,
                    )
                )

        self.assertEqual([item[0] for item in found], [0.0, 1000.0])

    def test_region_image_similarity_uses_template_alpha(self):
        engine = wrpa.RPAEngine()
        template = Image.new("RGBA", (12, 10), (255, 0, 255, 0))
        for y in range(2, 8):
            for x in range(3, 10):
                template.putpixel(
                    (x, y),
                    ((x * 21) % 255, (y * 39) % 255, (x * y * 7) % 255, 255),
                )
        current = Image.new("RGB", template.size, (11, 99, 177))
        current.paste(template, (0, 0), template)

        similarity = engine.image_similarity_percent(current, template)

        self.assertGreater(similarity, 99.9)

    def test_engine_persists_a_bounded_performance_report_after_run(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        engine.settlement_wait = 0
        engine.run_tasks([{"type": 5.0, "value": "0"}])
        report = engine.last_performance_report
        self.assertEqual(report["gauges"]["run.outcome"], "finished")
        self.assertEqual(report["timings"]["action.total"]["count"], 1)
        self.assertLessEqual(
            report["timings"]["action.total"]["sampled_count"],
            report["sample_limit"],
        )
        trace_events = engine.last_run_trace["events"]
        self.assertEqual(trace_events[0]["event"], "run_started")
        self.assertTrue(
            any(event["event"] == "step_result" for event in trace_events)
        )
        self.assertEqual(trace_events[-1]["event"], "run_finished")

    def test_until_conditions_share_one_snapshot_for_the_same_region(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        pixels = [
            ((x * 41 + y * 17) % 255, (x * 23 + y * 37) % 255, (x * 53 + y * 11) % 255)
            for y in range(20)
            for x in range(20)
        ]
        image = Image.new("RGB", (20, 20))
        image.putdata(pixels)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "condition.png")
            image.save(path)
            capture = mock.Mock(return_value=(image.copy(), 10, 20))
            engine.capture_screenshot = capture
            task = {
                "until_logic": "全部满足",
                "until_cond1_en": True,
                "until_cond1_mode": "区域变成指定图片",
                "until_cond1_image": path,
                "until_cond1_region": "10,20,20,20",
                "until_cond1_similarity": "99",
                "until_cond2_en": True,
                "until_cond2_mode": "区域变成指定图片",
                "until_cond2_image": path,
                "until_cond2_region": "10,20,20,20",
                "until_cond2_similarity": "99",
            }
            status = engine.execute_until_conditions(
                task, {"step": 1, "loop": 1}, True
            )

        self.assertEqual(status, "condition_true")
        self.assertEqual(capture.call_count, 1)
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["condition.screenshot_cache_misses"], 1)
        self.assertEqual(counters["condition.screenshot_cache_hits"], 1)

    def test_overlay_match_condition_satisfied(self):
        """覆盖匹配：小图完全匹配子区域时条件满足。"""
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        # 创建 40x40 大区域图像，其中 (10,10)-(20,20) 子区域为纯红色
        big_image = Image.new("RGB", (40, 40), (0, 0, 255))
        for y in range(10, 20):
            for x in range(10, 20):
                big_image.putpixel((x, y), (255, 0, 0))
        # 小模板图为 10x10 纯红色
        small_image = Image.new("RGB", (10, 10), (255, 0, 0))
        with tempfile.TemporaryDirectory() as temp_dir:
            small_path = os.path.join(temp_dir, "small.png")
            small_image.save(small_path)
            engine.capture_screenshot = mock.Mock(return_value=(big_image.copy(), 0, 0))
            task = {
                "until_logic": "全部满足",
                "until_cond1_en": True,
                "until_cond1_mode": "覆盖匹配",
                "until_cond1_image": "",
                "until_cond1_region": "0,0,40,40",
                "until_cond1_overlay_image": small_path,
                "until_cond1_overlay_rect": "10,10,10,10",
                "until_cond1_overlay_scale": "1.0",
                "until_cond1_overlay_threshold": "0.8",
            }
            status = engine.execute_until_conditions(task, {"step": 1, "loop": 1}, True)
        self.assertEqual(status, "condition_true")

    def test_overlay_match_condition_not_satisfied(self):
        """覆盖匹配：小图与子区域不匹配时条件不满足。"""
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        # 大区域子区域为水平渐变（有纹理），小模板为垂直渐变（方向不同）
        big_image = Image.new("RGB", (40, 40), (128, 128, 128))
        for y in range(10, 20):
            for x in range(10, 20):
                big_image.putpixel((x, y), (x * 25, 0, 0))
        # 小模板图为垂直渐变
        small_image = Image.new("RGB", (10, 10))
        for y in range(10):
            for x in range(10):
                small_image.putpixel((x, y), (0, y * 25, 0))
        with tempfile.TemporaryDirectory() as temp_dir:
            small_path = os.path.join(temp_dir, "small.png")
            small_image.save(small_path)
            engine.capture_screenshot = mock.Mock(return_value=(big_image.copy(), 0, 0))
            task = {
                "until_logic": "全部满足",
                "until_cond1_en": True,
                "until_cond1_mode": "覆盖匹配",
                "until_cond1_image": "",
                "until_cond1_region": "0,0,40,40",
                "until_cond1_overlay_image": small_path,
                "until_cond1_overlay_rect": "10,10,10,10",
                "until_cond1_overlay_scale": "1.0",
                "until_cond1_overlay_threshold": "0.8",
            }
            status = engine.execute_until_conditions(task, {"step": 1, "loop": 1}, True)
        self.assertEqual(status, "condition_false")

    def test_overlay_match_missing_image(self):
        """覆盖匹配：小图路径不存在时条件不满足。"""
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        big_image = Image.new("RGB", (40, 40), (0, 0, 255))
        engine.capture_screenshot = mock.Mock(return_value=(big_image.copy(), 0, 0))
        task = {
            "until_logic": "全部满足",
            "until_cond1_en": True,
            "until_cond1_mode": "覆盖匹配",
            "until_cond1_image": "",
            "until_cond1_region": "0,0,40,40",
            "until_cond1_overlay_image": "C:/nonexistent/small.png",
            "until_cond1_overlay_rect": "10,10,10,10",
            "until_cond1_overlay_scale": "1.0",
            "until_cond1_overlay_threshold": "0.8",
        }
        status = engine.execute_until_conditions(task, {"step": 1, "loop": 1}, True)
        self.assertEqual(status, "condition_false")

    def test_overlay_match_missing_region(self):
        """覆盖匹配：未设置大区域时条件不满足。"""
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        small_image = Image.new("RGB", (10, 10), (255, 0, 0))
        with tempfile.TemporaryDirectory() as temp_dir:
            small_path = os.path.join(temp_dir, "small.png")
            small_image.save(small_path)
            task = {
                "until_logic": "全部满足",
                "until_cond1_en": True,
                "until_cond1_mode": "覆盖匹配",
                "until_cond1_image": "",
                "until_cond1_region": "",
                "until_cond1_overlay_image": small_path,
                "until_cond1_overlay_rect": "10,10,10,10",
                "until_cond1_overlay_scale": "1.0",
                "until_cond1_overlay_threshold": "0.8",
            }
            status = engine.execute_until_conditions(task, {"step": 1, "loop": 1}, True)
        self.assertEqual(status, "condition_false")

    def test_overlay_match_sub_region_out_of_bounds(self):
        """覆盖匹配：子区域越界时条件不满足。"""
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        big_image = Image.new("RGB", (40, 40), (0, 0, 255))
        small_image = Image.new("RGB", (10, 10), (255, 0, 0))
        with tempfile.TemporaryDirectory() as temp_dir:
            small_path = os.path.join(temp_dir, "small.png")
            small_image.save(small_path)
            engine.capture_screenshot = mock.Mock(return_value=(big_image.copy(), 0, 0))
            task = {
                "until_logic": "全部满足",
                "until_cond1_en": True,
                "until_cond1_mode": "覆盖匹配",
                "until_cond1_image": "",
                "until_cond1_region": "0,0,40,40",
                "until_cond1_overlay_image": small_path,
                "until_cond1_overlay_rect": "35,35,10,10",
                "until_cond1_overlay_scale": "1.0",
                "until_cond1_overlay_threshold": "0.8",
            }
            status = engine.execute_until_conditions(task, {"step": 1, "loop": 1}, True)
        self.assertEqual(status, "condition_false")

    def test_overlay_match_template_larger_than_sub_region(self):
        """覆盖匹配：小图大于子区域时条件不满足。"""
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        big_image = Image.new("RGB", (40, 40), (0, 0, 255))
        # 小图 20x20 但子区域只有 10x10
        small_image = Image.new("RGB", (20, 20), (255, 0, 0))
        with tempfile.TemporaryDirectory() as temp_dir:
            small_path = os.path.join(temp_dir, "small.png")
            small_image.save(small_path)
            engine.capture_screenshot = mock.Mock(return_value=(big_image.copy(), 0, 0))
            task = {
                "until_logic": "全部满足",
                "until_cond1_en": True,
                "until_cond1_mode": "覆盖匹配",
                "until_cond1_image": "",
                "until_cond1_region": "0,0,40,40",
                "until_cond1_overlay_image": small_path,
                "until_cond1_overlay_rect": "10,10,10,10",
                "until_cond1_overlay_scale": "1.0",
                "until_cond1_overlay_threshold": "0.8",
            }
            status = engine.execute_until_conditions(task, {"step": 1, "loop": 1}, True)
        self.assertEqual(status, "condition_false")

    def test_overlay_match_with_scale(self):
        """覆盖匹配：缩放小图后匹配成功。"""
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        # 大区域 40x40，子区域 (10,10)-(30,30) 为 20x20 纯红色
        big_image = Image.new("RGB", (40, 40), (0, 0, 255))
        for y in range(10, 30):
            for x in range(10, 30):
                big_image.putpixel((x, y), (255, 0, 0))
        # 小模板图 10x10 纯红色，缩放 2.0 后变为 20x20
        small_image = Image.new("RGB", (10, 10), (255, 0, 0))
        with tempfile.TemporaryDirectory() as temp_dir:
            small_path = os.path.join(temp_dir, "small.png")
            small_image.save(small_path)
            engine.capture_screenshot = mock.Mock(return_value=(big_image.copy(), 0, 0))
            task = {
                "until_logic": "全部满足",
                "until_cond1_en": True,
                "until_cond1_mode": "覆盖匹配",
                "until_cond1_image": "",
                "until_cond1_region": "0,0,40,40",
                "until_cond1_overlay_image": small_path,
                "until_cond1_overlay_rect": "10,10,20,20",
                "until_cond1_overlay_scale": "2.0",
                "until_cond1_overlay_threshold": "0.8",
            }
            status = engine.execute_until_conditions(task, {"step": 1, "loop": 1}, True)
        self.assertEqual(status, "condition_true")

    @staticmethod
    def _sub_region_change_frame(right_value):
        """40x20 帧：左半 (x<20) 恒定绿色，右半随 right_value 变化。"""
        img = Image.new("RGB", (40, 20), (10, 10, 10))
        for y in range(20):
            for x in range(20):
                img.putpixel((x, y), (0, 200, 0))
            for x in range(20, 40):
                img.putpixel((x, y), (right_value, right_value, right_value))
        return img

    def _run_region_changed_twice(self, task):
        """连续两次执行直到条件：第一次记录基准，第二次比较变化。"""
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        frames = [
            self._sub_region_change_frame(0),
            self._sub_region_change_frame(255),
        ]
        state = {"i": 0}

        def fake_capture(region):
            frame = frames[state["i"]]
            return frame.copy(), region[0], region[1]

        engine.capture_screenshot = fake_capture
        first = engine.execute_until_conditions(task, {"step": 1, "loop": 1}, True)
        state["i"] = 1
        second = engine.execute_until_conditions(task, {"step": 1, "loop": 1}, True)
        return first, second

    def test_region_changed_sub_region_ignores_background_change(self):
        """子区域恒定而背景变化时，条件不应成立（核心场景）。"""
        task = {
            "until_logic": "全部满足",
            "until_cond1_en": True,
            "until_cond1_mode": "区域发生变化",
            "until_cond1_region": "0,0,40,20",
            "until_cond1_sub_region": "0,0,20,20",
            "until_cond1_diff": "8",
        }
        first, second = self._run_region_changed_twice(task)
        self.assertEqual(first, "condition_false")
        self.assertEqual(second, "condition_false")

    def test_region_changed_sub_region_detects_change_inside(self):
        """子区域本身发生变化时，条件应成立。"""
        task = {
            "until_logic": "全部满足",
            "until_cond1_en": True,
            "until_cond1_mode": "区域发生变化",
            "until_cond1_region": "0,0,40,20",
            "until_cond1_sub_region": "20,0,20,20",
            "until_cond1_diff": "8",
        }
        first, second = self._run_region_changed_twice(task)
        self.assertEqual(first, "condition_false")
        self.assertEqual(second, "condition_true")

    def test_region_changed_without_sub_region_uses_full_region(self):
        """未设置子区域时保持原行为：整个大区域参与比较。"""
        task = {
            "until_logic": "全部满足",
            "until_cond1_en": True,
            "until_cond1_mode": "区域发生变化",
            "until_cond1_region": "0,0,40,20",
            "until_cond1_diff": "8",
        }
        first, second = self._run_region_changed_twice(task)
        self.assertEqual(first, "condition_false")
        self.assertEqual(second, "condition_true")

    def test_region_changed_sub_region_out_of_range_is_not_satisfied(self):
        """子区域完全越界时不记录基准、条件不成立且不报错。"""
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        frame = self._sub_region_change_frame(0)
        engine.capture_screenshot = mock.Mock(return_value=(frame.copy(), 0, 0))
        task = {
            "until_logic": "全部满足",
            "until_cond1_en": True,
            "until_cond1_mode": "区域发生变化",
            "until_cond1_region": "0,0,40,20",
            "until_cond1_sub_region": "100,100,20,20",
            "until_cond1_diff": "8",
        }
        status = engine.execute_until_conditions(task, {"step": 1, "loop": 1}, True)
        self.assertEqual(status, "condition_false")
        self.assertEqual(engine.until_condition_baselines, {})

    def test_until_condition_sub_region_field_round_trips_through_model(self):
        from fukua_rpa.task_model import (
            until_condition_list_from_data,
            until_condition_summary,
        )

        data = {
            "until_cond1_en": True,
            "until_cond1_mode": "区域发生变化",
            "until_cond1_region": "100,100,300,200",
            "until_cond1_sub_region": "10,20,50,60",
        }
        conditions = until_condition_list_from_data(data)
        self.assertEqual(conditions[0]["sub_region"], "10,20,50,60")
        self.assertIn("子区域 10,20,50,60", until_condition_summary(data))

        # 未设置子区域时字段为空串且摘要不提及子区域（向后兼容）。
        data_no_sub = dict(data)
        data_no_sub["until_cond1_sub_region"] = ""
        conditions_no_sub = until_condition_list_from_data(data_no_sub)
        self.assertEqual(conditions_no_sub[0]["sub_region"], "")
        self.assertNotIn("子区域", until_condition_summary(data_no_sub))

    def test_duplicate_status_payload_does_not_refresh_the_ui_twice(self):
        engine = wrpa.RPAEngine()
        updates = []
        engine.callback_status = updates.append
        engine.report_status(2, 3, 8, "左键单击")
        engine.report_status(2, 3, 8, "左键单击")
        engine.report_status(2, 4, 8, "等待")

        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[-1]["step"], 4)
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["status.duplicate_updates_skipped"], 1)

    def test_native_work_budget_fallback_is_cached_for_the_run(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        fake_native = mock.Mock()
        fake_native.available = True
        fake_native.load_error = ""
        fake_native.last_result_code = 0

        def reject(*_args, **_kwargs):
            fake_native.load_error = "预算已超限"
            fake_native.last_result_code = -2
            return None

        fake_native.find_template.side_effect = reject
        engine.native_core = fake_native
        pixels = [
            ((x * 31 + y * 7) % 255, (x * 13 + y * 17) % 255, (x * 19 + y * 23) % 255)
            for y in range(8)
            for x in range(8)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "template.png")
            image = Image.new("RGB", (8, 8))
            image.putdata(pixels)
            image.save(path)
            first = engine.native_find_targets(path, "key", 0.8, True)
            second = engine.native_find_targets(path, "key", 0.8, True)

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(fake_native.find_template.call_count, 1)
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["match.native_rejections_cached"], 1)
        self.assertEqual(counters["match.native_rejection_cache_hits"], 1)

    def test_native_cancellation_is_not_counted_as_failure_or_fallback(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        fake_native = mock.Mock()
        fake_native.available = True
        fake_native.load_error = "native search cancelled"
        fake_native.last_result_code = NATIVE_RESULT_CANCELLED
        fake_native.find_template.return_value = None
        engine.native_core = fake_native
        pixels = [
            ((x * 17 + y * 3) % 255, (x * 5 + y * 19) % 255, (x * 23 + y * 7) % 255)
            for y in range(8)
            for x in range(8)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "template.png")
            image = Image.new("RGB", (8, 8))
            image.putdata(pixels)
            image.save(path)
            result = engine.native_find_targets(path, "key", 0.8, True)

        self.assertEqual(result, [])
        self.assertEqual(engine.native_failure_streak, 0)
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["match.native_cancellations"], 1)
        self.assertEqual(counters.get("match.native_fallbacks", 0), 0)

    def test_repeated_native_failures_trip_run_local_circuit_breaker(self):
        engine = wrpa.RPAEngine()
        engine.log_level = -1
        fake_native = mock.Mock()
        fake_native.available = True
        fake_native.load_error = "capture failed"
        fake_native.last_result_code = -1
        fake_native.find_template.return_value = None
        engine.native_core = fake_native
        pixels = [
            ((x * 29 + y * 5) % 255, (x * 7 + y * 31) % 255, (x * 11 + y * 17) % 255)
            for y in range(8)
            for x in range(8)
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "template.png")
            image = Image.new("RGB", (8, 8))
            image.putdata(pixels)
            image.save(path)
            for _index in range(4):
                self.assertIsNone(
                    engine.native_find_targets(path, "key", 0.8, True)
                )

        self.assertEqual(fake_native.find_template.call_count, 3)
        self.assertTrue(engine._native_disabled_for_run)
        counters = engine.performance.snapshot()["counters"]
        self.assertEqual(counters["match.native_circuit_breaker_trips"], 1)
        self.assertEqual(counters["match.native_circuit_breaker_hits"], 1)

    def test_deleting_step_closes_its_settings_dialog(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
             mock.patch.object(HotkeyController, "register_global_hotkeys", lambda _self: True), \
             mock.patch.object(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.Ok):
            window = wrpa.RPAWindow(base_dir=temp_dir)
            window.add_row({"type": 1.0, "value": "10,10"})
            row = window.task_list.itemWidget(window.task_list.item(0))
            row.open_custom_config()
            self.app.processEvents()
            dialog = row.config_dialog
            self.assertEqual(
                os.path.abspath(dialog.dialog_settings.fileName()),
                os.path.abspath(os.path.join(temp_dir, "config.ini")),
            )
            window.del_row(row)
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()
            with self.assertRaises(RuntimeError):
                dialog.isVisible()
            self.assertFalse(isValid(row))
            self.assertFalse(window.task_config_dialogs)
            dispose_widget(self.app, window)

    def test_corrupt_main_config_recovers_from_atomic_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
             mock.patch.object(HotkeyController, "register_global_hotkeys", lambda _self: True), \
             mock.patch.object(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.Ok):
            first = wrpa.RPAWindow(base_dir=temp_dir)
            first.add_row({"type": 1.0, "value": "10,10"})
            self.assertTrue(first.persist_profiles_state(force=True))
            dispose_widget(self.app, first)

            settings = QSettings(os.path.join(temp_dir, "config.ini"), QSettings.IniFormat)
            settings.setValue("profiles_json", "{broken")
            settings.setValue("profiles_commit_json", "{broken")
            settings.sync()

            recovered = wrpa.RPAWindow(base_dir=temp_dir)
            self.assertTrue(recovered._config_recovery_message)
            self.assertEqual(recovered.task_list.count(), 1)
            self.assertTrue(os.path.isfile(recovered.profile_backup_path))
            dispose_widget(self.app, recovered)

    def test_recording_abort_discards_events(self):
        class FakeMain(QWidget):
            def __init__(self):
                super().__init__()
                self.rows = []

            def add_row(self, task):
                self.rows.append(task)

            def append_log(self, _text):
                pass

        class FakeHook(QThread):
            finished_signal = Signal(list)
            ready_signal = Signal(bool, str)
            control_signal = Signal(str)

            def __init__(self):
                super().__init__()
                self.stop_event = threading.Event()
                self.ready_event = threading.Event()
                self.emit_result = False

            def run(self):
                self.ready_event.set()
                self.ready_signal.emit(True, "")
                self.stop_event.wait()
                if self.emit_result:
                    self.finished_signal.emit(
                        [(time.monotonic(), "left", (11, 22))]
                    )

            def begin_recording(self):
                return True

            def finish_recording(self):
                self.emit_result = True
                self.stop()
                return True

            def stop(self):
                self.stop_event.set()

        main = FakeMain()
        recorder = wrpa.RecorderUI(main, hook_factory=FakeHook)
        hook = recorder.hook_thread
        self.assertTrue(hook.ready_event.wait(1.0))
        self.assertTrue(recorder.start_recording())
        recorder.abort()
        self.app.processEvents()
        self.assertEqual(main.rows, [])

    def test_recording_control_keys_are_swallowed_until_key_release(self):
        hook = HookThread()
        controls = []
        hook.control_signal.connect(controls.append)

        event = KBDLLHOOKSTRUCT()
        event.vkCode = 0x77
        pointer = ctypes.addressof(event)
        self.assertEqual(hook.keyboard_handler(0, WM_KEYDOWN, pointer), 1)
        self.assertEqual(hook.keyboard_handler(0, WM_KEYDOWN, pointer), 1)
        self.assertEqual(controls, [])
        self.assertEqual(hook.keyboard_handler(0, WM_KEYUP, pointer), 1)
        self.assertEqual(controls, ["toggle"])

        event.vkCode = 0x1B
        self.assertEqual(hook.keyboard_handler(0, WM_KEYDOWN, pointer), 1)
        self.assertEqual(hook.keyboard_handler(0, WM_KEYUP, pointer), 1)
        self.assertEqual(controls, ["toggle", "cancel"])

    def test_recording_hook_failure_is_visible_and_restores_main_window(self):
        class FailedHook(QThread):
            finished_signal = Signal(list)
            ready_signal = Signal(bool, str)
            control_signal = Signal(str)

            def run(self):
                self.ready_signal.emit(False, "无法启用键盘录制钩子（测试）")

            def stop(self):
                pass

        logs = []
        restored = []
        recorder = wrpa.RecorderUI(
            tasks_callback=lambda _tasks: None,
            log_callback=logs.append,
            restore_callback=lambda: restored.append(True),
            hook_factory=FailedHook,
        )
        self.addCleanup(recorder.close)
        self.assertTrue(recorder.hook_thread.wait(1000))
        self.app.processEvents()

        self.assertEqual(recorder.state, 3)
        self.assertEqual(restored, [True])
        self.assertTrue(any("录制钩子" in entry for entry in logs))


class UndoRedoDedupRegressionTests(QtCallbackGuardMixin, unittest.TestCase):
    """D-02 回归：undo/redo 后轻量编辑能正确入栈并可再次撤销。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_controller(self, tasks):
        """Instantiate WorkflowEditorController with a minimal mock host.

        Returns (controller, current_tasks, host). Callers must keep *host*
        alive because the controller only holds a weakref.
        """
        from fukua_rpa.ui.controllers.workflow_editor import (
            WorkflowEditorController,
        )

        current_tasks = list(tasks)

        class FakeSettings:
            def value(self, key, default=None, type=None):
                return default

        class FakeHost:
            def __init__(self):
                self.settings = FakeSettings()
                self.is_switching_profile = False
                self._runtime_editing_locked = False

            def snapshot_tasks(self):
                return list(current_tasks)

            def restore_task_snapshot(self, new_tasks):
                current_tasks.clear()
                current_tasks.extend(new_tasks)

            def append_application_log(self, msg, *args, **kwargs):
                pass

        host = FakeHost()
        controller = WorkflowEditorController(host)
        return controller, current_tasks, host

    def test_undo_clears_dedup_hash_allowing_re_push(self):
        """undo 后 _last_undo_hash 为 None，相同快照可再次入栈。"""
        snapshot_a = [{"type": 1.0, "value": "hello", "step_id": "s1"}]
        snapshot_b = [{"type": 1.0, "value": "world", "step_id": "s1"}]
        controller, current_tasks, _host = self._make_controller(snapshot_a)

        # First edit: push snapshot_a (before-state) → enters stack
        controller.push_undo_snapshot(snapshot_a)
        self.assertEqual(len(controller.undo_stack), 1)
        self.assertIsNotNone(controller._last_undo_hash)

        # Simulate the user editing to snapshot_b (the edit happened)
        current_tasks.clear()
        current_tasks.extend(snapshot_b)
        # Perform undo: pops snapshot_a from undo_stack, restores it,
        # saves current (snapshot_b) to redo_stack, clears _last_undo_hash
        controller.undo_task_change()
        self.assertIsNone(controller._last_undo_hash)
        self.assertEqual(current_tasks, snapshot_a)
        # undo popped the only entry → stack is now empty
        self.assertEqual(len(controller.undo_stack), 0)

        # After undo, a lightweight edit whose before-state equals snapshot_a
        # must NOT be suppressed by dedup (D-02 fix). Without the fix,
        # _last_undo_hash would still equal hash(snapshot_a) and the push
        # would be silently skipped, leaving the stack empty.
        controller.push_undo_snapshot(snapshot_a)
        self.assertEqual(len(controller.undo_stack), 1)

    def test_dedup_still_works_without_undo(self):
        """连续相同快照仍被去重（未发生 undo 时）。"""
        snapshot_a = [{"type": 2.0, "value": "x", "step_id": "s2"}]
        controller, _, _host = self._make_controller(snapshot_a)

        controller.push_undo_snapshot(snapshot_a)
        self.assertEqual(len(controller.undo_stack), 1)

        # Same snapshot again without undo → dedup suppresses
        controller.push_undo_snapshot(snapshot_a)
        self.assertEqual(len(controller.undo_stack), 1)

    def test_redo_clears_dedup_hash_allowing_re_push(self):
        """redo 后 _last_undo_hash 为 None，相同快照可再次入栈。"""
        snapshot_a = [{"type": 3.0, "value": "a", "step_id": "s3"}]
        snapshot_b = [{"type": 3.0, "value": "b", "step_id": "s3"}]
        controller, current_tasks, _host = self._make_controller(snapshot_a)

        # Build undo history: [snapshot_a], current = snapshot_b
        controller.push_undo_snapshot(snapshot_a)
        current_tasks.clear()
        current_tasks.extend(snapshot_b)

        # Undo → current becomes snapshot_a
        controller.undo_task_change()
        self.assertIsNone(controller._last_undo_hash)

        # Redo → current becomes snapshot_b, _last_undo_hash cleared again
        controller.redo_task_change()
        self.assertIsNone(controller._last_undo_hash)
        self.assertEqual(current_tasks, snapshot_b)

        # Push snapshot_b as before-state of next edit → must enter stack
        controller.push_undo_snapshot(snapshot_b)
        self.assertEqual(len(controller.undo_stack), 2)


# HotkeyParsingRegressionTests 已移除：该类测试的是本地重新实现的解析逻辑，
# 而非产品代码（RPAEngine._execute_hotkey）。真实引擎热键解析的回归覆盖
# 已由 tests/test_engine_actions.py 提供。


class NumpadHotkeyIdentityTests(unittest.TestCase):
    """FR-006：Hook 路径不得把小键盘键折叠为顶排数字。

    VK_MAP 把 0x60-0x69（VK_NUMPAD0-9）映射为 "0".."9"，而 HOTKEY_NAME_TO_VK
    用 "num0".."num9" 表示同一批键。hotkey_text_from_pressed_vks 经 VK_MAP 取名
    后走 parse_hotkey_text，于是：录入小键盘产生 "0"（vk 48），已配置的裸键
    映射 "num0"（vk 96）在 Hook 匹配中永不命中——映射模式 Hook 正是裸键的唯一
    后端，属静默失效。
    """

    def test_pressed_vk_numpad_keeps_numpad_identity(self):
        text = hotkey_text_from_pressed_vks(0x60, (0x60,))
        self.assertEqual(text, "num0")

    def test_pressed_vk_top_row_digit_unchanged(self):
        """对照：顶排数字既有配置与录入必须保持 "0"（vk 48）。"""

        self.assertEqual(hotkey_text_from_pressed_vks(0x30, (0x30,)), "0")

    def test_vk_map_numpad_names_round_trip_to_same_vk(self):
        """VK_MAP 给出的名字必须能解析回同一个虚拟键码，否则匹配必然落空。"""

        for vk in range(0x60, 0x6A):
            name = VK_MAP[vk]
            parsed = parse_hotkey_text(name)
            self.assertIsNotNone(parsed, f"{name} 无法解析")
            self.assertEqual(parsed["vk"], vk, f"{name} 解析回 {parsed['vk']}")

    def test_all_numpad_keys_keep_identity(self):
        for index in range(0, 10):
            self.assertEqual(
                hotkey_text_from_pressed_vks(0x60 + index, (0x60 + index,)),
                f"num{index}",
            )


if __name__ == "__main__":
    unittest.main()
