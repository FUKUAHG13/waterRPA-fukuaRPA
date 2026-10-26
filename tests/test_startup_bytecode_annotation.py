"""启动冒烟字节码条件标注的单测（2026-10-21）。

覆盖复核要求的三点：冷态写 `workspace_visible_warm_ms` + note、热态不写、
顶层判定值 `workspace_visible_ms` 在任何分支都不被覆盖。
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import verify_release


class StartupBytecodeAnnotationTests(unittest.TestCase):
    def setUp(self):
        verify_release._STARTUP_BYTECODE_STATE.clear()
        self.addCleanup(verify_release._STARTUP_BYTECODE_STATE.clear)

    def _write_report(self, directory, payload):
        path = Path(directory) / "startup.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_cold_condition_writes_reference_without_touching_judged_value(self):
        with TemporaryDirectory() as temp_dir:
            report = self._write_report(
                temp_dir, {"workspace_visible_ms": 700.0, "error": ""}
            )
            verify_release._STARTUP_BYTECODE_STATE.update(
                {"condition": "cold", "pyc_count": 0}
            )

            def fake_helper(command, **kwargs):
                warm_path = Path(command[command.index("--output") + 1])
                warm_path.write_text(
                    json.dumps({"workspace_visible_ms": 390.0}), encoding="utf-8"
                )
                return None

            with mock.patch.object(verify_release, "_run_helper", fake_helper):
                verify_release._annotate_startup_bytecode(report)

            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["workspace_visible_ms"], 700.0)
            self.assertEqual(data["workspace_visible_warm_ms"], 390.0)
            self.assertEqual(
                data["workspace_visible_warm_ms_note"], "参考值，不参与判定"
            )
            self.assertEqual(data["bytecode_condition"], "cold")
            self.assertEqual(data["pyc_count"], 0)

    def test_warm_condition_writes_no_reference_field(self):
        with TemporaryDirectory() as temp_dir:
            report = self._write_report(
                temp_dir, {"workspace_visible_ms": 420.0, "error": ""}
            )
            verify_release._STARTUP_BYTECODE_STATE.update(
                {"condition": "warm", "pyc_count": 131}
            )
            with mock.patch.object(
                verify_release,
                "_run_helper",
                side_effect=AssertionError("热态不应再跑一次"),
            ):
                verify_release._annotate_startup_bytecode(report)

            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["workspace_visible_ms"], 420.0)
            self.assertNotIn("workspace_visible_warm_ms", data)
            self.assertEqual(data["bytecode_condition"], "warm")

    def test_failed_reference_run_keeps_report_intact(self):
        with TemporaryDirectory() as temp_dir:
            report = self._write_report(
                temp_dir, {"workspace_visible_ms": 705.0, "error": ""}
            )
            verify_release._STARTUP_BYTECODE_STATE.update(
                {"condition": "cold", "pyc_count": 0}
            )
            with mock.patch.object(
                verify_release,
                "_run_helper",
                side_effect=RuntimeError("boom"),
            ):
                verify_release._annotate_startup_bytecode(report)

            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["workspace_visible_ms"], 705.0)
            self.assertNotIn("workspace_visible_warm_ms", data)
            self.assertEqual(data["bytecode_condition"], "cold")

    def test_record_condition_classifies_coverage(self):
        cases = [
            (0, 0, "unknown"),
            (100, 0, "cold"),
            (100, 100, "warm"),
            (100, 3, "partial"),
        ]
        for py_count, pyc_count, expected in cases:
            with self.subTest(py=py_count, pyc=pyc_count):
                with mock.patch.object(
                    verify_release,
                    "_cache_coverage",
                    return_value=(py_count, pyc_count),
                ):
                    verify_release._record_startup_bytecode_condition()
                self.assertEqual(
                    verify_release._STARTUP_BYTECODE_STATE["condition"], expected
                )
                self.assertEqual(
                    verify_release._STARTUP_BYTECODE_STATE["py_count"], py_count
                )

    def test_failed_run_with_existing_file_is_not_read(self):
        """反例：补测失败但文件确实存在时，也不得把该文件当成本次结果。"""
        with TemporaryDirectory() as temp_dir:
            report = self._write_report(
                temp_dir, {"workspace_visible_ms": 702.0, "error": ""}
            )
            verify_release._STARTUP_BYTECODE_STATE.update(
                {"condition": "cold", "pyc_count": 0, "py_count": 10}
            )

            def failing_helper(command, **kwargs):
                warm_path = Path(command[command.index("--output") + 1])
                warm_path.write_text(
                    json.dumps({"workspace_visible_ms": 380.0}), encoding="utf-8"
                )
                raise RuntimeError("run failed after writing")

            with mock.patch.object(verify_release, "_run_helper", failing_helper):
                verify_release._annotate_startup_bytecode(report)

            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["workspace_visible_ms"], 702.0)
            self.assertNotIn("workspace_visible_warm_ms", data)
            self.assertNotIn("workspace_visible_warm_source", data)

    def test_successful_run_records_source_file(self):
        with TemporaryDirectory() as temp_dir:
            report = self._write_report(
                temp_dir, {"workspace_visible_ms": 700.0, "error": ""}
            )
            verify_release._STARTUP_BYTECODE_STATE.update(
                {"condition": "cold", "pyc_count": 0, "py_count": 10}
            )

            def ok_helper(command, **kwargs):
                warm_path = Path(command[command.index("--output") + 1])
                warm_path.write_text(
                    json.dumps({"workspace_visible_ms": 395.0}), encoding="utf-8"
                )

            with mock.patch.object(verify_release, "_run_helper", ok_helper):
                verify_release._annotate_startup_bytecode(report)

            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["workspace_visible_warm_ms"], 395.0)
            self.assertTrue(data["workspace_visible_warm_source"].startswith("startup-warm-"))


if __name__ == "__main__":
    unittest.main()
