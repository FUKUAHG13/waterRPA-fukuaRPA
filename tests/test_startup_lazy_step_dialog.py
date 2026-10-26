"""启动依赖守护：步骤设置对话框按需导入、纯摘要与界面模块解耦（ADR 0040）。

守护目标不是「某个函数被调用」，而是**启动路径实际导入了什么**：子进程里导入
工作区模块后检查 ``sys.modules``，避免用测试内的桩掩盖真实的模块耦合。
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from fukua_rpa.ui.task_row import TaskRow
from tests.qt_test_support import dispose_widget

ROOT = Path(__file__).resolve().parents[1]
PROBE_TIMEOUT = 120
STEP_DIALOG_MODULES = (
    "fukua_rpa.ui.task_settings_dialog",
    "fukua_rpa.ui.task_settings_sections",
    "fukua_rpa.ui.task_settings_controllers",
    "fukua_rpa.ui.task_settings_view",
    "fukua_rpa.ui.task_settings_forwarders",
)


def run_probe(source):
    """Run ``source`` in a fresh interpreter and return its last JSON line."""

    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=PROBE_TIMEOUT,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


class StartupImportIsolationTests(unittest.TestCase):
    def test_workspace_import_does_not_load_step_settings_dialog(self):
        report = run_probe(
            "import json,sys; import fukua_rpa.ui.main_window; "
            f"names={STEP_DIALOG_MODULES!r}; "
            "print(json.dumps({name: name in sys.modules for name in names}))"
        )
        self.assertEqual(
            [name for name, loaded in report.items() if loaded],
            [],
            "启动导入工作区时不应加载步骤设置对话框子树",
        )

    def test_workspace_import_keeps_step_row_available(self):
        """对话框延后，但构建步骤行所需的类必须仍然在启动时可用。"""

        report = run_probe(
            "import json,sys; import fukua_rpa.ui.main_window; "
            "names=('fukua_rpa.ui.task_row','fukua_rpa.ui.task_row_controllers'); "
            "print(json.dumps({name: name in sys.modules for name in names}))"
        )
        self.assertEqual(report, dict.fromkeys(report, True))

    def test_summary_only_controllers_do_not_import_task_row(self):
        """只用摘要的控制器不得再静态导入可编辑行控件。

        这里不能用「导入子模块再看 sys.modules」的探针：导入
        ``fukua_rpa.ui.controllers.workflow_search`` 会先执行包 ``__init__``，
        而包里包含 ``workflow_step_edit``，必然带出 ``task_row``。故用 AST 直接
        检查这三个模块自身的导入来源。
        """

        import ast

        for relative in (
            "fukua_rpa/ui/controllers/settings_controller.py",
            "fukua_rpa/ui/controllers/workflow_context.py",
            "fukua_rpa/ui/controllers/workflow_search.py",
        ):
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            imports = {
                (node.level, node.module)
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            self.assertNotIn(
                (2, "task_row"), imports, f"{relative} 不应再导入 ..task_row"
            )
            self.assertIn(
                (3, "workflow_summary"),
                imports,
                f"{relative} 应从 ...workflow_summary 取摘要",
            )

    def test_summary_module_has_no_qt_dependency(self):
        report = run_probe(
            "import json,sys; from fukua_rpa.workflow_summary import task_drag_summary; "
            "print(json.dumps({'pyside': any(m.split('.')[0]=='PySide6' for m in sys.modules),"
            "'text': task_drag_summary({'type': 1.0, 'value': '10,20'}, 1)}))"
        )
        self.assertFalse(report["pyside"], "纯摘要模块不得依赖 PySide6")
        self.assertEqual(report["text"], "1. 左键单击 | 10,20")


class SummarySingleImplementationTests(unittest.TestCase):
    def test_summary_has_single_implementation(self):
        """迁移后不得出现两套摘要逻辑：两处必须是同一个函数对象。"""

        from fukua_rpa import workflow_summary
        from fukua_rpa.ui import task_row

        self.assertIs(task_row.task_drag_summary, workflow_summary.task_drag_summary)

    def test_summary_keeps_marker_and_unsupported_format(self):
        from fukua_rpa.workflow_summary import task_drag_summary

        self.assertEqual(
            task_drag_summary({"type": 1.0, "value": "10,20", "step_disabled": True}, 2),
            "2. [停] 左键单击 | 10,20",
        )
        self.assertEqual(
            task_drag_summary({"type": 999.0, "value": "x"}, 3),
            "3. 当前版本不支持（类型 999.0） | x",
        )
        self.assertEqual(
            task_drag_summary(
                {"type": 1.0, "value": "10,20", "debug_breakpoint": True}, 4, "advanced"
            ),
            "4. [断] 左键单击 | 10,20",
        )


class StepDialogLazyLoadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.row = TaskRow(lambda _row: None)
        self.addCleanup(dispose_widget, self.app, self.row)

    def test_gear_first_open_loads_dialog_and_works(self):
        self.assertIsNone(getattr(self.row, "config_dialog", None))
        self.row.open_custom_config()
        dialog = getattr(self.row, "config_dialog", None)
        self.assertIsNotNone(dialog, "首次打开小齿轮必须真正构建对话框")
        self.assertEqual(type(dialog).__name__, "TaskConfigDialog")
        self.assertIn("fukua_rpa.ui.task_settings_dialog", sys.modules)

    def test_gear_open_failure_is_logged_and_leaves_no_state(self):
        """导入失败：只记 critical 日志，不弹窗，也不留下半初始化的编辑状态。"""

        target = (
            "fukua_rpa.ui.task_row_controllers.config_dialog_controller"
            ".load_task_config_dialog"
        )
        with mock.patch(target, side_effect=ImportError("missing")), mock.patch(
            "fukua_rpa.ui.task_row_controllers.config_dialog_controller.logger"
        ) as logger, mock.patch(
            "fukua_rpa.ui.task_row_mixins.config_capability"
        ):
            result = self.row.open_custom_config()

        self.assertIsNone(result)
        logger.critical.assert_called_once()
        self.assertIsNone(getattr(self.row, "config_dialog", None))
        for attribute in (
            "_config_dialog_touched_value",
            "_config_edit_snapshot",
            "_config_value_before_dialog_change",
            "_config_dialog_last_value",
        ):
            self.assertFalse(
                hasattr(self.row, attribute),
                f"导入失败不得写入 {attribute}",
            )


class BatchSettingsImportFailureTests(unittest.TestCase):
    def test_batch_import_failure_returns_false_without_snapshot(self):
        from fukua_rpa.ui.controllers.workflow_group import WorkflowGroupMixin

        class _Host:
            def __init__(self):
                self.snapshot_calls = 0

            def snapshot_tasks(self):
                self.snapshot_calls += 1
                return []

        class _Editor(WorkflowGroupMixin):
            def __init__(self):
                self.host = _Host()

            def selected_row_indexes(self):
                return [0]

        editor = _Editor()
        target = (
            "fukua_rpa.ui.controllers.workflow_group.load_task_config_dialog"
        )
        with mock.patch(target, side_effect=ImportError("missing")), mock.patch(
            "fukua_rpa.ui.controllers.workflow_group.is_runtime_editing_locked",
            return_value=False,
        ), mock.patch(
            "fukua_rpa.ui.controllers.workflow_group.logger"
        ) as logger:
            result = editor.open_batch_task_settings()

        self.assertFalse(result)
        logger.critical.assert_called_once()
        self.assertEqual(editor.host.snapshot_calls, 0)


if __name__ == "__main__":
    unittest.main()
