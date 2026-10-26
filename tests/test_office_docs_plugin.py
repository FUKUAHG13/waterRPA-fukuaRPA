"""PLG03 / PLG04 regression tests for the office-docs plugin.

PLG03: 扁平（非嵌套）插件信封里的动作参数必须被识别，而非被静默丢弃。
PLG04: 引擎写类动作在目标文件缺失时记录的日志，必须对用户文件路径脱敏。
"""

import contextlib
import os
import sys
import unittest
from unittest.mock import patch

# 让 office_docs 作为顶层包可导入（其 action/engine 使用相对导入）。
_SRC = os.path.join(os.path.dirname(__file__), "..", "plugins", "office-docs", "src")
if os.path.abspath(_SRC) not in sys.path:
    sys.path.insert(0, os.path.abspath(_SRC))

from office_docs import action as office_action  # noqa: E402
from office_docs import engine as office_engine  # noqa: E402


class OfficeDocsFlatEnvelopeTests(unittest.TestCase):
    def test_flat_envelope_params_reach_action(self):
        # PLG03：扁平信封（参数位于信封根、混入 run_id/step_id 等元数据键）必须被
        # 识别为动作参数，而非被静默丢弃。
        flat = {
            "action_id": "excel_read_cell",
            "run_id": "r1",
            "step_id": "s1",
            "plugin_id": "office-docs",
            "path": "C:/Users/Alice/book.xlsx",
            "sheet": "Sheet1",
            "cell": "A1",
        }
        captured = {}

        def fake_dispatch(backend, action_id, payload):
            captured["action_id"] = action_id
            captured["payload"] = payload
            return {"ok": True, "result": payload, "error": None}

        with patch.object(office_action, "dispatch", fake_dispatch):
            res = office_action.run(flat, backend=object())
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("run_id"), "r1")
        self.assertEqual(captured["action_id"], "excel_read_cell")
        self.assertEqual(
            captured["payload"],
            {"path": "C:/Users/Alice/book.xlsx", "sheet": "Sheet1", "cell": "A1"},
        )

    def test_nested_envelope_still_supported(self):
        # 回归保护：标准嵌套信封（payload.payload）行为不受影响。
        nested = {
            "run_id": "r2",
            "action_id": "excel_read_cell",
            "payload": {
                "payload": {"path": "book.xlsx", "sheet": "S", "cell": "B2"},
                "run_id": "r2",
                "action_id": "excel_read_cell",
            },
        }
        captured = {}

        def fake_dispatch(backend, action_id, payload):
            captured["payload"] = payload
            return {"ok": True, "result": payload, "error": None}

        with patch.object(office_action, "dispatch", fake_dispatch):
            office_action.run(nested, backend=object())
        self.assertEqual(captured["payload"], {"path": "book.xlsx", "sheet": "S", "cell": "B2"})


class OfficeDocsPathRedactionTests(unittest.TestCase):
    def test_excel_write_missing_file_logs_redacted_path(self):
        # PLG04：excel_write 目标文件不存在时，日志中的用户路径必须被脱敏。
        raw = "C:/Users/Alice/Documents/new.xlsx"
        records = []

        class _FakeBackend:
            requires_real_file = True

            def open(self, *args, **kwargs):
                return None

            def create(self, *args, **kwargs):
                return None

        with patch.object(office_engine.os.path, "exists", return_value=False), \
                patch.object(
                    office_engine.logger, "info",
                    side_effect=lambda *a, **k: records.append(a),
                ), \
                contextlib.suppress(Exception):
            # 后端后续写操作可能与 Mock 后端不兼容而抛错，但路径脱敏日志已在之前触发。
            office_engine.dispatch(
                _FakeBackend(), "excel_write_cell",
                {"path": raw, "sheet": "S", "cell": "A1", "value": "x"},
            )

        joined = " ".join(str(part) for rec in records for part in rec)
        self.assertNotIn(raw, joined, "用户路径未被脱敏就写入了日志")
        self.assertIn("<隐藏路径>", joined)
