#!/usr/bin/env python3
"""浏览器自动化插件单元测试（使用 Mock 后端，无需安装 playwright）。

运行
----
    python tests/test_browser_automation.py
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from browser_automation.action import execute_action, run  # noqa: E402
from browser_automation.backend import MockBrowserBackend, PlaywrightBackend  # noqa: E402


class BrowserActionTests(unittest.TestCase):
    def setUp(self):
        self.backend = MockBrowserBackend()

    def _exec(self, action_id, payload=None):
        return execute_action(
            "browser-automation", action_id, payload or {},
            backend=self.backend, backend_name="mock",
        )

    def test_navigate(self):
        resp = self._exec("browser_navigate", {"url": "https://example.com"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["url"], "https://example.com")

    def test_navigate_missing_url(self):
        resp = self._exec("browser_navigate", {})
        self.assertFalse(resp["ok"])
        self.assertIn("url", resp["error"])

    def test_navigate_bad_wait_until(self):
        resp = self._exec("browser_navigate", {"url": "x", "wait_until": "never"})
        self.assertFalse(resp["ok"])

    def test_click_by_text(self):
        resp = self._exec("browser_click", {"by": "text", "value": "登录"})
        self.assertTrue(resp["ok"])
        el = resp["result"]["element"]
        self.assertEqual(el["text"], "登录")
        self.assertIsNotNone(el["bounding_box"])

    def test_click_index_out_of_range(self):
        resp = self._exec("browser_click", {"by": "text", "value": "登录", "index": 9})
        self.assertFalse(resp["ok"])

    def test_fill(self):
        resp = self._exec("browser_fill", {"by": "css", "value": "input[name=username]", "text": "alice"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["element"]["text"], "alice")

    def test_fill_missing_text(self):
        resp = self._exec("browser_fill", {"by": "text", "value": "用户名"})
        self.assertFalse(resp["ok"])

    def test_extract_text_page(self):
        resp = self._exec("browser_extract_text", {})
        self.assertTrue(resp["ok"])
        self.assertIn("欢迎", resp["result"]["text"])

    def test_extract_text_element(self):
        resp = self._exec("browser_extract_text", {"by": "text", "value": "登录"})
        self.assertTrue(resp["ok"])
        self.assertIn("登录", resp["result"]["text"])

    def test_wait_for(self):
        resp = self._exec("browser_wait_for", {"by": "text", "value": "登录", "timeout_ms": 1000})
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["result"]["found"])

    def test_get_attribute(self):
        resp = self._exec("browser_get_attribute", {"by": "text", "value": "登录", "attr": "id"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["value"], "btn-login")

    def test_screenshot(self):
        path = os.path.join(HERE, "_shot.png")
        if os.path.exists(path):
            os.remove(path)
        resp = self._exec("browser_screenshot", {"path": path})
        self.assertTrue(resp["ok"])
        self.assertTrue(os.path.isfile(resp["result"]["path"]))
        os.remove(path)

    def test_unknown_action(self):
        resp = self._exec("browser_nope", {})
        self.assertFalse(resp["ok"])
        self.assertIn("未知动作", resp["error"])

    def test_run_envelope_dispatch(self):
        # 模拟 P3 信封：action_id 位于 payload 内
        request = {
            "protocol": "fukuaRPA.plugin",
            "version": 1,
            "kind": "request",
            "payload": {
                "run_id": "r1", "step_id": "s1", "plugin_id": "browser-automation",
                "action_id": "browser_navigate", "deadline_ns": 0,
                "payload": {"url": "https://example.com"},
            },
        }
        resp = run(request, backend=self.backend, backend_name="mock")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["url"], "https://example.com")

    def test_run_unknown_action_via_envelope(self):
        request = {
            "payload": {
                "action_id": "browser_unknown",
                "payload": {},
            }
        }
        resp = run(request, backend=self.backend, backend_name="mock")
        self.assertFalse(resp["ok"])

    def test_click_negative_index(self):
        # 负 index 应被明确拒绝，而非静默选中末位元素（误点击风险）。
        resp = self._exec("browser_click", {"by": "text", "value": "登录", "index": -1})
        self.assertFalse(resp["ok"])
        self.assertIn("非负", resp["error"])

    def test_fill_negative_index(self):
        resp = self._exec(
            "browser_fill",
            {"by": "css", "value": "input[name=username]", "text": "alice", "index": -1},
        )
        self.assertFalse(resp["ok"])
        self.assertIn("非负", resp["error"])

    def test_run_envelope_flat(self):
        # 扁平形态：action_id 与参数都在请求根，run() 不应静默丢弃参数。
        request = {
            "plugin_id": "browser-automation",
            "action_id": "browser_navigate",
            "url": "https://example.com",
        }
        resp = run(request, backend=self.backend, backend_name="mock")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["url"], "https://example.com")


class WaitForErrorHandlingTests(unittest.TestCase):
    """修复 P3：wait_for 需区分「超时未出现」与「真实异常」。"""

    class _FakePage:
        def __init__(self, exc):
            self._exc = exc

        def wait_for_selector(self, sel, timeout=None):
            raise self._exc

    def _backend(self, exc):
        b = PlaywrightBackend()
        b._page = self._FakePage(exc)
        return b

    def test_timeout_returns_false(self):
        # 超时属预期的"未找到"，应返回 False 而非抛错。
        b = self._backend(TimeoutError("timed out"))
        self.assertFalse(b.wait_for("css", "x", 1000))

    def test_real_error_propagates(self):
        # 选择器语法错误等非超时异常不应被伪装成 False，需向上抛出。
        b = self._backend(ValueError("invalid selector"))
        with self.assertRaises(ValueError):
            b.wait_for("css", "x", 1000)


class AutoCloseOwnershipTests(unittest.TestCase):
    """FR-20260813-D11-001: 传入 backend 且 auto_close=True 时必须关闭，不得因
    own_backend 守卫漏关（CLI 自建 backend 未入 _BACKEND_CACHE，漏关会逃过 atexit
    造成浏览器子进程泄漏）。run() 宿主路径不传 auto_close，不受影响。"""

    def test_passed_in_backend_closed_on_auto_close(self):
        backend = MockBrowserBackend()
        resp = execute_action(
            "browser-automation", "browser_navigate",
            {"url": "https://example.com"},
            backend=backend, backend_name="mock", auto_close=True,
        )
        self.assertTrue(resp["ok"])
        self.assertIsNone(backend._url)  # close() 已执行

    def test_passed_in_backend_kept_when_auto_close_false(self):
        backend = MockBrowserBackend()
        execute_action(
            "browser-automation", "browser_navigate",
            {"url": "https://example.com"},
            backend=backend, backend_name="mock", auto_close=False,
        )
        self.assertIsNotNone(backend._url)  # 未关闭

    def test_run_host_path_does_not_close_shared_backend(self):
        backend = MockBrowserBackend()
        request = {
            "payload": {
                "run_id": "r1", "step_id": "s1", "plugin_id": "browser-automation",
                "action_id": "browser_navigate",
                "payload": {"url": "https://example.com"},
            }
        }
        run(request, backend=backend, backend_name="mock")
        self.assertIsNotNone(backend._url)  # 宿主共享 backend 不被关闭


if __name__ == "__main__":
    unittest.main(verbosity=2)
