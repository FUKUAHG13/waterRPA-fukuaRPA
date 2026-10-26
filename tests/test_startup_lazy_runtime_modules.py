"""启动依赖守护：运行期模块按需导入（ADR 0041）。

守护「启动路径实际导入了什么」与「首次使用时仍能工作」。隔离类与首次使用类断言
放在**独立子进程**里执行，避免同进程内其它用例提前导入模块把断言变成空转；
失败注入用例在进程内改包属性与 ``sys.modules``，让 ``from ... import`` 真正失败。
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

ROOT = Path(__file__).resolve().parents[1]
PROBE_TIMEOUT = 120
DEFERRED_MODULES = ("fukua_rpa.validation", "fukua_rpa.ui.overlays")


def run_probe(source):
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


def hide_module(module_name: str, package: str) -> tuple:
    """Make ``from <package> import <leaf>`` fail until restored."""

    package_module = sys.modules[package]
    attribute = module_name.rsplit(".", 1)[1]
    saved_attribute = package_module.__dict__.pop(attribute, None)
    saved_module = sys.modules.pop(module_name, None)
    sys.modules[module_name] = None
    return package_module, attribute, saved_attribute, saved_module


def restore_module(state: tuple, module_name: str) -> None:
    package_module, attribute, saved_attribute, saved_module = state
    if saved_module is None:
        sys.modules.pop(module_name, None)
    else:
        sys.modules[module_name] = saved_module
    if saved_attribute is not None:
        setattr(package_module, attribute, saved_attribute)


class StartupImportIsolationTests(unittest.TestCase):
    def test_workspace_import_does_not_load_runtime_only_modules(self):
        report = run_probe(
            "import json,sys; import fukua_rpa.ui.main_window; "
            f"names={DEFERRED_MODULES!r}; "
            "print(json.dumps({name: name in sys.modules for name in names}))"
        )
        self.assertEqual([name for name, loaded in report.items() if loaded], [])

    def test_workspace_import_keeps_runtime_controllers(self):
        """延迟导入不得牵连控制器本身：它们必须在启动时可用。"""

        report = run_probe(
            "import json,sys; import fukua_rpa.ui.main_window; "
            "names=('fukua_rpa.ui.controllers.workflow_review_controller',"
            "'fukua_rpa.ui.controllers.feedback_controller'); "
            "print(json.dumps({name: name in sys.modules for name in names}))"
        )
        self.assertEqual(report, dict.fromkeys(report, True))


class _Host(QWidget):
    """最小宿主：只提供控制器构造与校验路径需要的接口。"""

    def __init__(self):
        super().__init__()
        self.base_dir = ROOT

    def get_current_ui_config(self):
        return {}


class RunValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = _Host()
        self.addCleanup(self.host.deleteLater)

    def test_run_validation_still_works(self):
        from fukua_rpa.ui.controllers.workflow_review_controller import (
            WorkflowReviewController,
        )

        controller = WorkflowReviewController(self.host)
        self.assertIsNone(
            controller.validate_tasks([{"type": 1.0, "value": "10,20"}])
        )
        self.assertIsInstance(
            controller.validate_tasks([{"type": 1.0, "value": ""}]), str
        )
        self.assertIn("fukua_rpa.validation", sys.modules)

    def test_validation_import_failure_is_logged_and_retryable(self):
        from fukua_rpa.ui.controllers import workflow_review_controller as module

        controller = module.WorkflowReviewController(self.host)
        target = "fukua_rpa.validation"
        state = hide_module(target, "fukua_rpa")
        try:
            with mock.patch.object(module, "logger") as logger, self.assertRaises(ImportError):
                controller.validate_tasks([{"type": 1.0, "value": "10,20"}])
            logger.critical.assert_called_once()
        finally:
            restore_module(state, target)
        # 失败不被缓存：恢复后同一实例可以重试成功。
        self.assertIsNone(
            controller.validate_tasks([{"type": 1.0, "value": "10,20"}])
        )


class OverlayFirstUseTests(unittest.TestCase):
    def test_overlay_first_use_builds_widget(self):
        report = run_probe(
            "import json,os,sys;"
            "os.environ.setdefault('QT_QPA_PLATFORM','offscreen');"
            "from PySide6.QtWidgets import QApplication,QWidget;"
            "app=QApplication([]);"
            "from fukua_rpa.ui.controllers.feedback_controller import "
            "VisualFeedbackController;"
            # 取 before 必须在导入控制器**之后**：否则它恒为 False，是空断言。
            "before='fukua_rpa.ui.overlays' in sys.modules;"
            "host=QWidget(); host.base_dir=os.getcwd();"
            "controller=VisualFeedbackController(host);"
            "controller.show_click_indicator_overlay({'x':1,'y':2,'text':'1'});"
            "print(json.dumps({'before':before,"
            "'after':'fukua_rpa.ui.overlays' in sys.modules,"
            "'count':len(controller.click_indicator_overlays),"
            "'visible':controller.click_indicator_overlays[0].isVisible()}))"
        )
        self.assertFalse(report["before"], "导入工作区时不应加载 overlays")
        self.assertTrue(report["after"], "首次使用时必须加载 overlays")
        self.assertEqual(report["count"], 1)
        self.assertTrue(report["visible"])

    def test_overlay_cleanup_after_close(self):
        report = run_probe(
            "import json,os,sys;"
            "os.environ.setdefault('QT_QPA_PLATFORM','offscreen');"
            "from PySide6.QtWidgets import QApplication,QWidget;"
            "app=QApplication([]);"
            "from fukua_rpa.ui.controllers.feedback_controller import "
            "VisualFeedbackController;"
            "host=QWidget(); host.base_dir=os.getcwd();"
            "controller=VisualFeedbackController(host);"
            "controller.show_click_indicator_overlay({'x':1,'y':2,'text':'1'});"
            "shown=len(controller.click_indicator_overlays);"
            "controller.close_all();"
            "print(json.dumps({'shown':shown,"
            "'left':len(controller.click_indicator_overlays)}))"
        )
        self.assertEqual(report["shown"], 1)
        self.assertEqual(report["left"], 0, "关闭后必须清空点击提示浮层")


class OverlayFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_overlay_import_failure_is_logged_and_retryable(self):
        from fukua_rpa.ui.controllers import feedback_controller as module

        host = _Host()
        self.addCleanup(host.deleteLater)
        controller = module.VisualFeedbackController(host)
        target = "fukua_rpa.ui.overlays"
        state = hide_module(target, "fukua_rpa.ui")
        try:
            with mock.patch.object(module, "logger") as logger, self.assertRaises(ImportError):
                controller.show_click_indicator_overlay(
                    {"x": 1, "y": 2, "text": "1"}
                )
            logger.critical.assert_called_once()
            self.assertEqual(controller.click_indicator_overlays, [])
        finally:
            restore_module(state, target)
        controller.show_click_indicator_overlay({"x": 1, "y": 2, "text": "1"})
        self.assertEqual(len(controller.click_indicator_overlays), 1)


if __name__ == "__main__":
    unittest.main()
