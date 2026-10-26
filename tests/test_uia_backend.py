import queue
import threading
import time
import unittest
from unittest import mock

from fukua_rpa.uia_backend import (
    UIAutomationBackend,
    _candidate_score,
    _pattern_candidates,
    _perform_uia_job,
    _point_in_rect,
    _rect_area,
    _UIAutomationService,
)


class _Rect:
    left = 10
    top = 20
    right = 110
    bottom = 70


class _NoPatternControl:
    """A control that exposes no pattern at all (``GetPattern`` -> ``None``).

    ``uiautomation`` returns ``None`` from ``GetPattern`` when the control does
    not support the requested pattern; that is normal semantics, not an error.
    """

    def GetPattern(self, _pattern_id):  # noqa: N802 - mirrors the COM API name
        return None


class _ReadOnlyValueControl:
    def __init__(self):
        self.IsReadOnly = True

    def GetPattern(self, _pattern_id):  # noqa: N802 - mirrors the COM API name
        return self


class UIAutomationBackendTests(unittest.TestCase):
    def test_point_and_area_helpers_reject_edges_consistently(self):
        rect = _Rect()
        self.assertTrue(_point_in_rect(rect, 10, 20))
        self.assertTrue(_point_in_rect(rect, 109, 69))
        self.assertFalse(_point_in_rect(rect, 110, 69))
        self.assertEqual(_rect_area(rect), 5000.0)

    def test_pattern_candidates_skip_controls_without_value_pattern(self):
        """REVIEW-20260913-001 FR-004: ``GetPattern`` 返回 ``None`` 是正常语义
        （控件不支持该模式）。必须直接跳过，不得靠 ``AttributeError`` 走异常路径
        并输出含堆栈的误导性 WARNING——该调用对坐标处每个候选控件各执行一次，
        否则单次 set_value 会刷出多条伪错误日志。"""
        with mock.patch("fukua_rpa.uia_backend.logger") as logger:
            candidates = _pattern_candidates(_NoPatternControl(), "set_value")

        self.assertEqual(candidates, [])
        logger.warning.assert_not_called()

    def test_pattern_candidates_drop_read_only_value_pattern_for_set_value(self):
        """写入前仍须跳过只读 Value 模式（原有语义不得因判空修复而丢失）。

        该桩控件对所有模式都返回自身，因此 set_value 下 Value 被只读判定丢弃、
        LegacyValue 仍在；read_value 不做只读过滤，Value 应保留。
        """
        control = _ReadOnlyValueControl()

        set_value_actions = [
            action for _priority, action, _pattern in _pattern_candidates(control, "set_value")
        ]
        self.assertNotIn("Value", set_value_actions)

        read_value_actions = [
            action for _priority, action, _pattern in _pattern_candidates(control, "read_value")
        ]
        self.assertIn("Value", read_value_actions)

    def test_backend_sends_only_plain_values_to_worker(self):
        service = mock.Mock()
        service.available = True
        service.request.return_value = {"available": True, "actionable": True}
        backend = UIAutomationBackend(service=service)

        report = backend.probe(123, 45, 67)

        self.assertTrue(report["actionable"])
        service.request.assert_called_once_with(
            "probe",
            {"root_hwnd": 123, "x": 45, "y": 67, "preferred_hwnd": 0},
        )

    def test_set_and_read_value_send_plain_payloads(self):
        service = mock.Mock()
        service.available = True
        service.request.return_value = {"available": True, "actionable": True}
        backend = UIAutomationBackend(service=service)

        backend.set_value(10, 20, 30, "text", preferred_hwnd=40)
        backend.read_value(11, 21, 31, preferred_hwnd=41)

        first, second = service.request.call_args_list
        self.assertEqual(first.args[0], "set_value")
        self.assertEqual(first.args[1]["value"], "text")
        self.assertEqual(second.args[0], "read_value")
        self.assertNotIn("value", second.args[1])

    def test_candidate_score_prefers_bound_hwnd_then_deepest_control(self):
        bound = _candidate_score(
            depth=2,
            area=5000,
            action_priority=10,
            native_hwnd=222,
            preferred_hwnd=222,
            root_hwnd=111,
        )
        deep_unbound = _candidate_score(
            depth=8,
            area=100,
            action_priority=40,
            native_hwnd=0,
            preferred_hwnd=222,
            root_hwnd=111,
        )
        self.assertGreater(bound, deep_unbound)

        deep_toggle = _candidate_score(
            depth=5,
            area=100,
            action_priority=20,
            native_hwnd=0,
            preferred_hwnd=111,
            root_hwnd=111,
        )
        shallow_invoke = _candidate_score(
            depth=2,
            area=1000,
            action_priority=40,
            native_hwnd=111,
            preferred_hwnd=111,
            root_hwnd=111,
        )
        self.assertGreater(deep_toggle, shallow_invoke)

    def test_backend_close_is_forwarded(self):
        service = mock.Mock()
        service.available = True
        backend = UIAutomationBackend(service=service)
        backend.close()
        service.close.assert_called_once_with()

    def test_cancelled_job_never_enters_uia_tree_or_action(self):
        cancelled = threading.Event()
        cancelled.set()

        result = _perform_uia_job(
            "activate",
            {"root_hwnd": 123, "x": 45, "y": 67},
            cancelled,
        )

        self.assertTrue(result["cancelled"])
        self.assertFalse(result.get("success", False))

    def test_request_observes_emergency_stop_without_waiting_for_timeout(self):
        service = object.__new__(_UIAutomationService)
        service._queue = queue.Queue(maxsize=1)
        service._ensure_started = lambda: True
        started = time.monotonic()

        result = service.request(
            "activate", {}, stop_requested=lambda: True
        )

        self.assertTrue(result["cancelled"])
        self.assertTrue(result["outcome_unknown"])
        self.assertLess(time.monotonic() - started, 0.2)


class _ComMarshallingPattern:
    """COM 替身：动作调用抛marshalling 异常（服务端是否已执行不可知）。"""

    def __init__(self):
        self.calls = 0

    def Invoke(self, waitTime=0):  # noqa: N802 - 模拟 COM 接口命名
        self.calls += 1
        raise OSError("RPC_E_DISCONNECTED")

    def SetValue(self, value, waitTime=0):  # noqa: N802
        self.calls += 1
        raise OSError("RPC_E_CALL_REJECTED")

    @property
    def Value(self):  # noqa: N802
        raise OSError("read failed")


class MarshallingExceptionOutcomeTests(unittest.TestCase):
    """FR-20261006-001：COM 调用**抛异常**必须回报结果未知。

    超时与急停路径早已置``outcome_unknown``，异常路径此前未置，
    导致 ``mapping_backend`` 回退 PostMessage，一次点击被投递两次。
    """

    def _job(self, operation, pattern):
        found = {
            "available": True,
            "actionable": True,
            "_pattern": pattern,
            "_action": "Invoke",
        }
        with mock.patch(
            "fukua_rpa.uia_backend._find_actionable_control", return_value=found
        ):
            return _perform_uia_job(operation, {"root_hwnd": 1, "x": 2, "y": 3})

    def test_activate_com_exception_reports_unknown_outcome(self):
        result = self._job("activate", _ComMarshallingPattern())
        self.assertFalse(result["success"])
        self.assertTrue(
            result.get("outcome_unknown"),
            "COM 异常时必须回报结果未知，否则调用方会回退 PostMessage 造成重复点击",
        )

    def test_set_value_com_exception_reports_unknown_outcome(self):
        result = self._job("set_value", _ComMarshallingPattern())
        self.assertFalse(result["success"])
        self.assertTrue(result.get("outcome_unknown"))

    def test_read_value_exception_does_not_report_unknown(self):
        """只读操作无副作用，按设计不置该标志，允许普通失败重试。"""
        result = self._job("read_value", _ComMarshallingPattern())
        self.assertFalse(result["success"])
        self.assertIsNone(result.get("outcome_unknown"))

    def test_pattern_returns_false_is_not_unknown(self):
        """COM 明确返回 False 属"调用失败"，不是结果未知。"""

        class _FalsePattern:
            def Invoke(self, waitTime=0):  # noqa: N802
                return False

        result = self._job("activate", _FalsePattern())
        self.assertFalse(result["success"])
        self.assertIsNone(result.get("outcome_unknown"))

    def test_worker_fallback_reports_unknown_for_side_effecting_ops(self):
        """``_worker`` 兜底捕获同样可能发生在有副作用操作上，必须置该标志。"""
        from fukua_rpa import uia_backend

        results = {}
        for operation in ("activate", "set_value", "read_value"):
            service = uia_backend._UIAutomationService.__new__(
                uia_backend._UIAutomationService
            )
            service._queue = queue.Queue(maxsize=4)
            service._thread = None
            service._ready = threading.Event()
            service._stop_event = threading.Event()
            service._initializer = None

            holder = {}
            completed = threading.Event()
            service._queue.put(
                (operation, {"root_hwnd": 1, "x": 1, "y": 1}, completed, None, holder)
            )
            # ``_worker`` 循环到 ``_stop_event`` 置位或收到 None 哨兵才退出，
            # 必须显式投递哨兵，否则测试会永久阻塞。
            service._queue.put(None)
            with mock.patch.object(
                uia_backend, "_perform_uia_job", side_effect=OSError("unexpected")
            ):
                uia_backend._UIAutomationService._worker(service)
            self.assertTrue(completed.is_set(), f"{operation} 未完成")
            results[operation] = bool(holder["result"].get("outcome_unknown"))

        self.assertTrue(results["activate"], "activate 兜底必须回报结果未知")
        self.assertTrue(results["set_value"], "set_value 兜底必须回报结果未知")
        self.assertFalse(results["read_value"], "read_value 兜底不应回报结果未知")


if __name__ == "__main__":
    unittest.main()
