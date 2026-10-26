#!/usr/bin/env python3
"""本地 OCR 插件单元测试（标准库即可运行；引擎阈值测试在 numpy 可用时额外执行）。

运行
----
    python -m pytest plugins/ocr-local/tests/test_ocr_local.py -v
或
    python plugins/ocr-local/tests/test_ocr_local.py
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from ocr_local.action import execute_action, run  # noqa: E402
from ocr_local.backend import MockBackend, create_backend  # noqa: E402
from ocr_local.engine import ocr_image  # noqa: E402
from ocr_local.matcher import find_target, match_text  # noqa: E402
from ocr_local.models import OcrResult, OcrWord  # noqa: E402

try:
    import numpy as _np  # noqa: F401
    HAS_NUMPY = True
except Exception:  # noqa: BLE001
    HAS_NUMPY = False


class TestModels(unittest.TestCase):
    def test_geometry(self):
        w = OcrWord(text="确定", box=[(100, 100), (200, 100), (200, 140), (100, 140)], score=0.9)
        self.assertEqual(w.left, 100)
        self.assertEqual(w.right, 200)
        self.assertEqual(w.top, 100)
        self.assertEqual(w.bottom, 140)
        self.assertEqual(w.width, 100)
        self.assertEqual(w.height, 40)
        self.assertEqual(w.center, (150.0, 120.0))

    def test_point_at(self):
        w = OcrWord(text="x", box=[(0, 0), (100, 0), (100, 50), (0, 50)], score=1.0)
        self.assertEqual(w.point_at(0.0, 0.0), (0.0, 0.0))
        self.assertEqual(w.point_at(1.0, 1.0), (100.0, 50.0))
        self.assertEqual(w.point_at(0.5, 0.5), (50.0, 25.0))

    def test_result_text(self):
        r = OcrResult(words=[
            OcrWord(text="行一", box=[(0, 0), (10, 0), (10, 10), (0, 10)], score=1.0),
            OcrWord(text="行二", box=[(0, 20), (10, 20), (10, 30), (0, 30)], score=1.0),
        ])
        self.assertEqual(r.text, "行一\n行二")


class TestMatcher(unittest.TestCase):
    def test_contains(self):
        self.assertTrue(match_text("点击确定按钮", "确定", "contains"))
        self.assertFalse(match_text("取消", "确定", "contains"))

    def test_equals(self):
        self.assertTrue(match_text("确定", "确定", "equals"))
        # equals 为严格相等，不忽略空格
        self.assertFalse(match_text(" 确定 ", "确定", "equals"))

    def test_regex(self):
        self.assertTrue(match_text("错误码 E102", "E\\d+", "regex"))
        self.assertFalse(match_text("成功", "E\\d+", "regex"))

    def test_find_target_order(self):
        words = [
            OcrWord(text="注册", box=[(0, 0), (1, 0), (1, 1), (0, 1)], score=1.0),
            OcrWord(text="登录", box=[(0, 0), (1, 0), (1, 1), (0, 1)], score=1.0),
        ]
        r = find_target(OcrResult(words=words), ["登录", "注册"])
        self.assertIsNotNone(r)
        self.assertEqual(r.text, "登录")  # 候选顺序优先


class TestActionMock(unittest.TestCase):
    """用 MockBackend 跑通动作逻辑；不加载任何真实模型 / 图像。"""

    def setUp(self):
        self.backend = MockBackend()
        import ocr_local.action as action_mod
        import ocr_local.engine as engine_mod
        self._orig_action_get = action_mod._get_backend
        self._orig_load = engine_mod.load_image
        self._orig_get = engine_mod.get_backend
        # mock 后端忽略图像内容，故用桩代替真实解码
        engine_mod.load_image = lambda x: None
        # execute_action/run 在 action 层维护自己的后端缓存入口；两个入口都
        # 显式替换，确保测试不因本机恰好装了 RapidOCR 而读取 x.png。
        action_mod._get_backend = lambda name="rapidocr": MockBackend()
        engine_mod.get_backend = lambda name="rapidocr", **kw: MockBackend()

    def tearDown(self):
        import ocr_local.action as action_mod
        import ocr_local.engine as engine_mod
        action_mod._get_backend = self._orig_action_get
        engine_mod.load_image = self._orig_load
        engine_mod.get_backend = self._orig_get

    def test_read_region(self):
        resp = execute_action(
            "ocr-local", "ocr_read_region", {"image_path": "x.png"}, backend=self.backend
        )
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["result"]["found"])
        self.assertIn("确定", resp["result"]["text"])

    def test_find_click_found(self):
        resp = execute_action(
            "ocr-local", "ocr_find_click",
            {"image_path": "x.png", "target_text": "确定"}, backend=self.backend,
        )
        self.assertTrue(resp["ok"])
        r = resp["result"]
        self.assertTrue(r["found"])
        self.assertEqual(r["matched_text"], "确定")
        self.assertIn("click_x", r)
        self.assertIn("click_y", r)

    def test_find_click_not_found(self):
        resp = execute_action(
            "ocr-local", "ocr_find_click",
            {"image_path": "x.png", "target_text": "不存在的文字"}, backend=self.backend,
        )
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["result"]["found"])

    def test_find_click_no_target_only_returns_words(self):
        resp = execute_action(
            "ocr-local", "ocr_find_click", {"image_path": "x.png"}, backend=self.backend
        )
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["result"]["found"])
        self.assertIn("words", resp["result"])

    def test_find_click_local_space(self):
        # 修复潜在 P1：未提供 region_origin 时坐标为局部，并标注 coordinate_space=local。
        resp = execute_action(
            "ocr-local", "ocr_find_click",
            {"image_path": "x.png", "target_text": "确定"}, backend=self.backend,
        )
        self.assertTrue(resp["ok"])
        r = resp["result"]
        self.assertEqual(r["coordinate_space"], "local")
        self.assertIn("note", r)
        self.assertIn("区域局部坐标", r["note"])

    def test_find_click_region_origin_absolute(self):
        # 修复潜在 P1：提供 region_origin 则叠加为绝对屏幕坐标。
        resp = execute_action(
            "ocr-local", "ocr_find_click",
            {"image_path": "x.png", "target_text": "确定", "region_origin": [1000, 500]},
            backend=self.backend,
        )
        self.assertTrue(resp["ok"])
        r = resp["result"]
        self.assertEqual(r["coordinate_space"], "absolute")
        # MockBackend 的「确定」包围盒中心为 (150, 120)；叠加原点后应为 (1150, 620)。
        self.assertEqual(r["click_x"], 1150.0)
        self.assertEqual(r["click_y"], 620.0)

    def test_find_click_region_origin_invalid_keeps_local(self):
        # region_origin 非法时回退为局部坐标，不抛错。
        resp = execute_action(
            "ocr-local", "ocr_find_click",
            {"image_path": "x.png", "target_text": "确定", "region_origin": "bad"},
            backend=self.backend,
        )
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["coordinate_space"], "local")

    def test_run_envelope_standard(self):
        # 契约标准信封：action_id / plugin_id 嵌套在 request["payload"] 内。
        resp = run({
            "protocol": "fukuaRPA.plugin",
            "version": 1,
            "kind": "request",
            "payload": {
                "run_id": "r1", "step_id": "s1", "plugin_id": "ocr-local",
                "action_id": "ocr_find_click", "deadline_ns": 0,
                "payload": {"image_path": "x.png", "target_text": "取消"},
            },
        })
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["matched_text"], "取消")

    def test_run_envelope_flat(self):
        # 兼容形态：action_id 直接放在请求根。
        resp = run({
            "payload": {"image_path": "x.png", "target_text": "取消"},
            "plugin_id": "ocr-local",
            "action_id": "ocr_find_click",
        })
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["matched_text"], "取消")

    def test_run_unknown_action(self):
        resp = run({"payload": {}, "plugin_id": "ocr-local", "action_id": "nope"})
        self.assertFalse(resp["ok"])
        self.assertIn("未知动作", resp["error"])

    def test_missing_image(self):
        resp = execute_action("ocr-local", "ocr_find_click", {}, backend=self.backend)
        self.assertFalse(resp["ok"])
        self.assertIn("未提供图像", resp["error"])


class TestBackendRegistry(unittest.TestCase):
    def test_create_mock(self):
        self.assertIsInstance(create_backend("mock"), MockBackend)

    def test_unknown_backend(self):
        with self.assertRaises(ValueError):
            create_backend("does-not-exist")


@unittest.skipUnless(HAS_NUMPY, "需要 numpy 才能跑引擎阈值测试")
class TestEngineThreshold(unittest.TestCase):
    def test_threshold_filters_low_score(self):
        import numpy as np
        from ocr_local.models import OcrWord

        backend = MockBackend(words=[
            OcrWord(text="弱", box=[(0, 0), (10, 0), (10, 10), (0, 10)], score=0.1),
        ])
        arr = np.zeros((10, 10, 3), dtype=np.uint8)
        self.assertEqual(len(ocr_image(arr, backend=backend, score_threshold=0.5).words), 0)
        self.assertEqual(len(ocr_image(arr, backend=backend, score_threshold=0.05).words), 1)


class TestInputClickConsistency(unittest.TestCase):
    """FR-20260812-OCR-1/3: move_to 与 click_at 坐标口径一致 + SendInput 检查。"""

    class _FakeUser32:
        def __init__(self, virtual=(0, 0, 1920, 1080), send_result=1):
            self._virtual = virtual
            self._send_result = send_result
            self.send_inputs = []

        def GetSystemMetrics(self, index):
            return self._virtual[index - 76]

        def SendInput(self, count, buffer, size):
            self.send_inputs.append(count)
            return self._send_result

    def _virtual(self, x, y):
        from ocr_local import input_click as ic

        vx, vy, vw, vh = ic._virtual_desktop_metrics(self._FakeUser32((x, y, 100, 100)))
        return vx, vy, vw, vh

    def test_virtual_desktop_metrics_shared_by_click_and_move(self):
        # 负坐标副屏：虚拟桌面原点非 (0,0)
        self.assertEqual(self._virtual(-100, -50), (-100, -50, 100, 100))

    def test_move_to_uses_virtual_desktop_flags(self):
        from ocr_local import input_click as ic

        # D8-001（部分合理收窄）：原断言仅检查「有输入发出」，即使 flags 构造
        # 被误删（漏 VIRTUALDESK/ABSOLUTE）仍会通过，无法防多屏回归。此处改为
        # 实际捕获 _send_mouse_input 的 flags 并断言三项标志齐全。
        captured = []
        def fake_send(user32, flags, dx, dy):
            captured.append(flags)
        with mock.patch.object(ic, "_send_mouse_input", side_effect=fake_send):
            ic.move_to(10.0, 10.0)
        self.assertEqual(len(captured), 1)
        flags = captured[0]
        self.assertTrue(flags & ic.MOUSEEVENTF_MOVE)
        self.assertTrue(flags & ic.MOUSEEVENTF_ABSOLUTE)
        self.assertTrue(flags & ic.MOUSEEVENTF_VIRTUALDESK)

    def test_send_input_failure_raises(self):
        from ocr_local import input_click as ic

        fake = self._FakeUser32(send_result=0)
        with mock.patch(
            "ocr_local.input_click.ctypes.windll.user32", fake
        ):
            with self.assertRaises(OSError):
                ic.click_at(10.0, 10.0, delay=0)


class TestRegexRedosSafety(unittest.TestCase):
    """FR-20260813-D8-003: ocr-local matcher 的 regex 模式无 office-docs 侧那种
    _reject_catastrophic_pattern 守卫，可控嵌套量词 + 长 OCR 主体文本会触发灾难性回溯。

    本测试为表征/边界测试：在 FU_RPA_SLOW_TESTS=1 时，对嵌套量词模式 + 长主体构造
    输入，断言 match_text 在超时内返回（证明 ReDoS 可达，而非即时报错）。修复后（如加
    正则超时或模式守卫）应保持通过且显著更快；届时可收紧为「< 时间预算」断言防回归。
    """

    def test_nested_quantifier_redos_reachable(self):
        if not os.environ.get("FU_RPA_SLOW_TESTS"):
            self.skipTest(
                "D8-003 表征测试：设 FU_RPA_SLOW_TESTS=1 启用（避免默认挂起测试套件）"
            )
        import threading

        # 嵌套量词模式 + 长主体（全 'a' 后接非匹配字符）触发指数回溯。
        pattern = "(a+)+$"
        subject = "a" * 27 + "!"
        holder: dict[str, object] = {}

        def _worker():
            holder["r"] = match_text(subject, pattern, "regex")

        t = threading.Thread(target=_worker)
        t.start()
        t.join(timeout=20)
        self.assertFalse(
            t.is_alive(), "ReDoS 确认：match_text 在 20s 内未返回（灾难性回溯）"
        )
        # match_text 的 regex 分支返回 bool（re.search(...) is not None）；关键是不挂起。
        self.assertIsInstance(holder["r"], bool)


class TestBackendLockConsistency(unittest.TestCase):
    """FR-20260812-OCR-2: backend.get_backend 与 action._get_backend 共用同一锁。"""

    def test_same_lock_object(self):
        from ocr_local import action
        from ocr_local.backend import _BACKEND_LOCK

        self.assertIs(action._BACKEND_LOCK, _BACKEND_LOCK)


class TestCliCoordinateSpace(unittest.TestCase):
    """FR-20260812-OCR-4: CLI --click 必须提供 --origin，禁止把图像局部坐标
    直接当屏幕坐标点击。"""

    def _load_cli(self):
        """加载 tools/ocr_cli.py（其内部已把 src 加入 sys.path）。"""
        import importlib.util

        cli_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools",
            "ocr_cli.py",
        )
        spec = importlib.util.spec_from_file_location("ocr_cli_test", cli_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_click_without_origin_rejected(self):
        import tempfile

        cli = self._load_cli()
        hit = {
            "ok": True,
            "result": {
                "found": True,
                "matched_text": "确定",
                "click_x": 50.0,
                "click_y": 60.0,
                "score": 0.9,
            },
        }
        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "shot.png")
            with open(img, "wb") as fh:
                fh.write(b"fake-png")
            with mock.patch.object(cli, "execute_action", return_value=hit), mock.patch(
                "ocr_local.input_click.click_at"
            ) as click_stub, mock.patch.object(
                sys, "argv", ["ocr_cli", img, "--target", "确定", "--backend", "mock", "--click"]
            ):
                code = cli.main()
        self.assertEqual(code, 1)
        click_stub.assert_not_called()

    def test_click_with_origin_calls_click_at(self):
        import tempfile

        cli = self._load_cli()
        hit = {
            "ok": True,
            "result": {
                "found": True,
                "matched_text": "确定",
                "click_x": 50.0,
                "click_y": 60.0,
                "score": 0.9,
            },
        }
        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "shot.png")
            with open(img, "wb") as fh:
                fh.write(b"fake-png")
            with mock.patch.object(cli, "execute_action", return_value=hit), mock.patch(
                "ocr_local.input_click.click_at"
            ) as click_stub, mock.patch.object(
                sys,
                "argv",
                [
                    "ocr_cli", img, "--target", "确定", "--backend", "mock",
                    "--click", "--origin", "100", "200",
                ],
            ):
                code = cli.main()
        self.assertEqual(code, 0)
        # 局部坐标 (50,60) + 原点 (100,200) = 屏幕坐标 (150,260)
        click_stub.assert_called_once_with(150.0, 260.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
