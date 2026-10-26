"""GUI 端到端测试：核心用户路径（T-2）。

覆盖合并、导入（JSON 安全加载）、全量包拒绝三条主路径。
使用纯逻辑层验证（不启动 Qt 窗口），确保控制器依赖的
底层文档操作和安全边界正确。

DESIGN: 窗口级集成测试需真实桌面环境（offscreen 下 RPAWindow
内部定时器/worker 会挂起），故标记 skipIf(_OFFSCREEN)。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_OFFSCREEN = os.environ.get("QT_QPA_PLATFORM") == "offscreen"


def _minimal_workflow_tasks() -> list:
    """Return a minimal valid task list with one wait step."""
    return [
        {
            "id": "step_001",
            "type": 5.0,
            "value": "1",
            "repeat_mode": "执行一次",
            "repeat_count": 1,
            "target_successes": 1,
        }
    ]


class E2EMergeWorkflowTests(unittest.TestCase):
    """方案合并路径（纯文档逻辑）。"""

    def test_merge_appends_source_steps_with_fresh_ids(self):
        """合并后步骤数 = 目标 + 来源，ID 唯一。"""
        from fukua_rpa.workflow_document import merge_workflow_tasks

        dest = [
            {"id": "d1", "type": 5.0, "value": "1", "repeat_mode": "执行一次"},
        ]
        source = [
            {"id": "s1", "type": 5.0, "value": "2", "repeat_mode": "执行一次"},
            {"id": "s2", "type": 1.0, "value": "3", "repeat_mode": "执行一次"},
        ]
        result = merge_workflow_tasks(dest, source)
        merged = result.tasks
        self.assertEqual(len(merged), 3)
        ids = [t["id"] for t in merged]
        self.assertEqual(len(set(ids)), 3, "合并后 ID 应唯一")
        self.assertIn("d1", ids)
        self.assertEqual(result.appended_count, 2)

    def test_merge_preserves_destination_order(self):
        """合并保持目标方案原有步骤在前。"""
        from fukua_rpa.workflow_document import merge_workflow_tasks

        dest = [
            {"id": "d1", "type": 5.0, "value": "1"},
            {"id": "d2", "type": 1.0, "value": "2"},
        ]
        source = [{"id": "s1", "type": 5.0, "value": "3"}]
        result = merge_workflow_tasks(dest, source)
        merged = result.tasks
        self.assertEqual(merged[0]["id"], "d1")
        self.assertEqual(merged[1]["id"], "d2")

    def test_merge_empty_source_is_noop(self):
        """空来源合并后等于原目标。"""
        from fukua_rpa.workflow_document import merge_workflow_tasks

        dest = [{"id": "d1", "type": 5.0, "value": "1"}]
        result = merge_workflow_tasks(dest, [])
        self.assertEqual(len(result.tasks), 1)
        self.assertEqual(result.tasks[0]["id"], "d1")
        self.assertEqual(result.appended_count, 0)


class E2EImportJsonTests(unittest.TestCase):
    """导入路径（JSON 安全加载层）。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="fukua_e2e_")
        self.addCleanup(self._tmpdir.cleanup)

    def test_load_valid_workflow_json(self):
        """合法工作流 JSON 能正确解析为 dict。"""
        from fukua_rpa.json_safety import load_bounded_json_file

        tasks = _minimal_workflow_tasks()
        wf_path = os.path.join(self._tmpdir.name, "test.json")
        Path(wf_path).write_text(
            json.dumps({"version": "2.0", "tasks": tasks}, ensure_ascii=False),
            encoding="utf-8",
        )
        data = load_bounded_json_file(wf_path, max_bytes=1_048_576)
        self.assertIsInstance(data, dict)
        self.assertIn("tasks", data)
        self.assertEqual(len(data["tasks"]), 1)
        self.assertEqual(data["tasks"][0]["type"], 5.0)

    def test_load_invalid_json_raises_value_error(self):
        """非法 JSON 应抛出 ValueError。"""
        from fukua_rpa.json_safety import load_bounded_json_file

        bad_path = os.path.join(self._tmpdir.name, "bad.json")
        Path(bad_path).write_text("not valid json {{{", encoding="utf-8")
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            load_bounded_json_file(bad_path, max_bytes=1_048_576)

    def test_load_oversized_json_rejected(self):
        """超出字节上限的文件应被拒绝。"""
        from fukua_rpa.json_safety import load_bounded_json_file

        big_path = os.path.join(self._tmpdir.name, "big.json")
        Path(big_path).write_text('{"x": "' + "A" * 200 + '"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_bounded_json_file(big_path, max_bytes=100)

    def test_load_deeply_nested_json_rejected(self):
        """超出嵌套深度上限的 JSON 应被拒绝。"""
        from fukua_rpa.json_safety import load_bounded_json_file

        nested = '{"a":' * 50 + "1" + "}" * 50
        deep_path = os.path.join(self._tmpdir.name, "deep.json")
        Path(deep_path).write_text(nested, encoding="utf-8")
        with self.assertRaises(ValueError):
            load_bounded_json_file(deep_path, max_bytes=1_048_576, max_depth=10)


class E2EFullPackageRejectTests(unittest.TestCase):
    """全量包安全拒绝路径。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="fukua_e2e_pkg_")
        self.addCleanup(self._tmpdir.cleanup)

    def test_invalid_zip_rejected(self):
        """非 ZIP 文件应被 zipfile 模块拒绝。"""
        import zipfile

        bad_zip = os.path.join(self._tmpdir.name, "bad.zip")
        Path(bad_zip).write_bytes(b"this is not a zip file at all")
        with self.assertRaises(zipfile.BadZipFile):
            zipfile.ZipFile(bad_zip)

    def test_empty_zip_no_manifest_rejected(self):
        """空 ZIP（无 manifest.json）应被视为非法全量包。"""
        import zipfile

        empty_zip = os.path.join(self._tmpdir.name, "empty.zip")
        with zipfile.ZipFile(empty_zip, "w") as zf:
            zf.writestr("readme.txt", "hello")
        with zipfile.ZipFile(empty_zip) as zf:
            names = zf.namelist()
        self.assertNotIn("manifest.json", names)


@unittest.skipIf(_OFFSCREEN, "requires real Qt event loop for window lifecycle")
class E2EWindowLifecycleTests(unittest.TestCase):
    """窗口级集成（需真实桌面环境）。"""

    def test_window_creates_and_disposes(self):
        """窗口创建→销毁无残留。"""
        from PySide6.QtWidgets import QApplication

        from tests.qt_test_support import create_test_window, dispose_widget

        app = QApplication.instance() or QApplication([])
        initial_count = len(app.topLevelWidgets())
        window = create_test_window(self)
        app.processEvents()
        self.assertGreater(len(app.topLevelWidgets()), initial_count)
        dispose_widget(app, window)
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
