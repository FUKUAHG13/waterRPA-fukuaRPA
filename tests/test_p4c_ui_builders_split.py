"""P4c regression guard: UI builder split into grouped sub-builders.

Verifies that ``build_execution_section`` / ``build_task_settings_view`` remain
thin orchestrators and that the extracted section builders live in their own
modules and are re-exported from the original facade modules. This guards
against accidental deletion of the extracted builders or breakage of the
public entry points during later refactors.
"""

import ast
import inspect
import textwrap
import unittest

from fukua_rpa.ui import task_settings_view, task_settings_view_parts
from fukua_rpa.ui.task_settings_dialog import TaskConfigDialog
from fukua_rpa.ui.task_settings_sections import execution, execution_parts
from fukua_rpa.ui.task_settings_sections.debug import build_debug_section


class P4cUiBuildersSplitTests(unittest.TestCase):
    def test_execution_parts_exist_and_reexported(self):
        for name in (
            "build_auxiliary_click_section",
            "build_click_form_section",
            "build_key_hold_section",
            "build_auto_clicker_section",
        ):
            with self.subTest(builder=name):
                self.assertTrue(
                    hasattr(execution_parts, name),
                    f"{name} missing from execution_parts",
                )
                self.assertTrue(
                    hasattr(execution, name),
                    f"{name} not re-exported from execution facade",
                )
                self.assertTrue(callable(getattr(execution, name)))

    def test_task_settings_view_parts_exist_and_reexported(self):
        for name in (
            "build_image_target_section",
            "build_coordinate_target_section",
            "build_drag_target_section",
            "build_plugin_action_section",
        ):
            with self.subTest(builder=name):
                self.assertTrue(
                    hasattr(task_settings_view_parts, name),
                    f"{name} missing from task_settings_view_parts",
                )
                self.assertTrue(
                    hasattr(task_settings_view, name),
                    f"{name} not re-exported from task_settings_view facade",
                )
                self.assertTrue(callable(getattr(task_settings_view, name)))

    def test_primary_entry_points_intact(self):
        self.assertTrue(callable(execution.build_execution_section))
        self.assertTrue(callable(task_settings_view.build_task_settings_view))

    def test_no_circular_import_on_parts_module(self):
        # task_settings_view_parts only imports task_settings_view lazily
        # inside build_coordinate_target_section, so importing it standalone
        # must not raise.
        import importlib

        importlib.reload(task_settings_view_parts)
        self.assertTrue(
            hasattr(task_settings_view_parts, "build_coordinate_target_section")
        )


class SectionBuilderLayeringTests(unittest.TestCase):
    """FR-SCOPE009-004：区块构建函数只创建控件，模式应用归协调层。

    `docs/UI开发规范.md`「架构边界」第 1、3 条：页面负责组装控件、业务规则进
    控制器；设置区块只创建控件，不拥有第二份配置状态。基线里
    `task_settings_sections/debug.py` 的 `build_debug_section` 末尾调用
    `self.apply_settings_mode(self.settings_mode)`，让一个区块构建函数反过来驱动
    整窗模式应用——模式应用的时机与顺序被隐藏在该区块的实现里。

    调试区块的显隐行为本身由既有用例守护
    （`tests/test_ui_modernization.py` 在构造后立即断言
    `debug_section.isHidden()`），本类只固定分层结构。
    """

    @staticmethod
    def _called_attrs(func):
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        return {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

    def test_debug_section_builder_does_not_apply_settings_mode(self):
        self.assertNotIn(
            "apply_settings_mode",
            self._called_attrs(build_debug_section),
            "区块构建函数不得驱动整窗模式应用；模式应用属于协调层职责",
        )

    def test_dialog_applies_settings_mode_after_view_construction(self):
        source = inspect.getsource(TaskConfigDialog.__init__)
        build_call = "build_task_settings_view(self, data)"
        apply_call = "apply_settings_mode(self.settings_mode)"
        self.assertIn(
            apply_call,
            source,
            "协调层必须承接模式应用，否则构造后的初始显隐状态不会生效",
        )
        self.assertLess(
            source.index(build_call),
            source.index(apply_call),
            "模式应用必须在整棵视图构建完成之后执行",
        )


if __name__ == "__main__":
    unittest.main()
