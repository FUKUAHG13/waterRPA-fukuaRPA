"""FR-20261006-007: the drag jitter row must actually reach the user.

05's report and 09's first verdict both judged visibility with ``isHidden()``,
which only reflects a widget's **own** explicit hiding and ignores the ancestor
chain. The row was mounted inside ``coordinate_target_form``, the content layout
of ``coordinate_target_section`` — a section that is explicitly hidden for drags
(their capabilities lack ``SETTING_COORDINATE_PATH``). So the row was reported
"visible" while no ancestor allowed it on screen.

Every assertion here uses ``isVisible()`` (ancestor-aware). Collapsed sections are
the dialog's existing design (``expanded=False`` everywhere), so each case expands
the section first and then asserts the row is genuinely on screen.
"""

from __future__ import annotations

import tempfile
import unittest

from PySide6.QtWidgets import QApplication

from fukua_rpa.ui.task_settings_dialog import TaskConfigDialog
from tests.qt_test_support import dispose_widget

_APP = None


def _app() -> QApplication:
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


def _hidden_ancestor(widget) -> object | None:
    """The nearest explicitly-hidden ancestor, or None when fully reachable."""
    node = widget.parentWidget()
    while node is not None:
        if node.isHidden():
            return node
        node = node.parentWidget()
    return None


class DragJitterVisibilityTests(unittest.TestCase):
    """左键/右键拖拽在高级模式下必须能看到并编辑「移动随机/按住随机」。"""

    def _dialog(self, case: unittest.TestCase, mode: str) -> TaskConfigDialog:
        app = _app()
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        dialog = TaskConfigDialog(None, {}, base_dir=tmp.name, settings_mode=mode)
        case.addCleanup(dispose_widget, app, dialog)
        app.processEvents()
        dialog.show()
        app.processEvents()
        return dialog

    def _prepare_drag(self, case: unittest.TestCase, mode: str, name: str):
        dialog = self._dialog(case, mode)
        # A real drag step: the value form is what selects the drag code path.
        dialog.update_step_context(step_type=name, step_value="10,20 -> 30,40")
        _app().processEvents()
        dialog.jitter_section.set_expanded(True)
        _app().processEvents()
        return dialog

    def test_row_has_no_hidden_ancestor_in_advanced_mode(self) -> None:
        for name in ("左键拖拽", "右键拖拽"):
            with self.subTest(step=name):
                dialog = self._prepare_drag(self, "advanced", name)
                hidden = _hidden_ancestor(dialog.jitter_row)
                self.assertIsNone(
                    hidden,
                    f"{name} 的随机延迟行存在被隐藏的祖先"
                    f"（{type(hidden).__name__ if hidden else ''}），用户永远看不到",
                )

    def test_row_is_visible_after_expanding_in_advanced_mode(self) -> None:
        for name in ("左键拖拽", "右键拖拽"):
            with self.subTest(step=name):
                dialog = self._prepare_drag(self, "advanced", name)
                self.assertTrue(
                    dialog.jitter_row.isVisible(),
                    f"{name} 高级模式展开后随机延迟行仍不可见",
                )

    def test_row_is_hidden_in_simple_mode(self) -> None:
        """简易模式不提供该能力（矩阵：仅高级模式可见）。"""
        for name in ("左键拖拽", "右键拖拽"):
            with self.subTest(step=name):
                dialog = self._prepare_drag(self, "simple", name)
                self.assertFalse(dialog.jitter_row.isVisible())

    def test_coordinate_click_reaches_the_jitter_section(self) -> None:
        """REG-20261008-001: 坐标点击同样必须能看到并编辑移动/按住随机。

        修 FR-20261006-007 时判据被收窄为 ``command in DRAG_COMMANDS``，去掉了
        ``coordinate_relevant or``。但该字段的既定范围**自 07233db 起就是
        「仅坐标点击与拖拽」**，且引擎（``flow.py``）与批量路径
        （``bulk_task_settings.JITTER_COMMANDS``）都仍按
        ``CLICK_COMMANDS | DRAG_COMMANDS`` 消费它——单步隐藏而批量仍有效，用户
        无法查看或修改实际生效的值。

        B 的反证另指出：旧用例用的步骤名是「左键点击」，而注册名是「左键单击」
        （command_code 对前者返回 None），断言恒成立、整项空过，还把回归固化成了
        期望行为。此处改用真实命令名并反转断言。
        """
        for name in ("左键单击", "左键双击", "右键单击"):
            with self.subTest(step=name):
                dialog = self._dialog(self, "advanced")
                dialog.update_step_context(step_type=name, step_value="10,20")
                _app().processEvents()
                dialog.jitter_section.set_expanded(True)
                _app().processEvents()
                self.assertTrue(
                    dialog.jitter_row.isVisible(),
                    f"{name}（坐标目标）必须能看到移动/按住随机——引擎仍消费该字段",
                )

    def test_coordinate_click_hidden_in_simple_mode(self) -> None:
        """REG-20261008-001 必须保留：简易模式不提供该能力。"""
        dialog = self._dialog(self, "simple")
        dialog.update_step_context(step_type="左键单击", step_value="10,20")
        _app().processEvents()
        dialog.jitter_section.set_expanded(True)
        _app().processEvents()
        self.assertFalse(dialog.jitter_row.isVisible())

    def test_jitter_section_hidden_for_unrelated_steps(self) -> None:
        """与坐标/拖拽无关的步骤（如截图保存）不展示该区。"""
        dialog = self._dialog(self, "advanced")
        dialog.update_step_context(step_type="截图保存", step_value="")
        _app().processEvents()
        dialog.jitter_section.set_expanded(True)
        _app().processEvents()
        self.assertFalse(dialog.jitter_row.isVisible())

    def test_image_target_click_hides_the_jitter_section(self) -> None:
        """REG-20261008-002: 图片目标点击**不**展示随机延迟组。

        上一轮我在 `docs/步骤设置显隐矩阵.md` 第 22 行（图片点击）补记了
        「移动随机、按住随机」，理由是 `SETTING_COORDINATE_PATH` 出现在
        `_IMAGE_CLICK_SETTINGS` 里。但那只读了**静态** settings 集合，漏掉
        `step_settings_capabilities`（`commands.py`）随目标类型的**动态**处理：

            if coordinate:
                capabilities.difference_update(IMAGE_SETTING_CAPABILITIES)
            else:
                capabilities.discard(SETTING_COORDINATE_PATH)

        非坐标目标一律丢弃该能力，故图片点击不显示随机延迟组——矩阵条目与代码
        相反。本项与 `test_coordinate_click_reaches_the_jitter_section` 构成一对
        正反守护：坐标点击显示、图片点击不显示。矩阵自述是"新增动作时的回归
        依据"，两侧都需要钉住。
        """
        from fukua_rpa.commands import (
            SETTING_COORDINATE_PATH,
            step_settings_capabilities,
            step_target_kind,
        )

        self.assertEqual(
            step_target_kind("左键单击", "C:/img/a.png"),
            "image",
            "前置条件：图片路径必须被判为 image 目标，否则本用例测不到目标行为",
        )
        self.assertNotIn(
            SETTING_COORDINATE_PATH,
            step_settings_capabilities("左键单击", "C:/img/a.png"),
            "能力集依据：图片目标丢弃坐标路径能力（commands.py 的动态分支）",
        )

        dialog = self._dialog(self, "advanced")
        dialog.update_step_context(step_type="左键单击", step_value="C:/img/a.png")
        _app().processEvents()
        dialog.jitter_section.set_expanded(True)
        _app().processEvents()
        self.assertFalse(
            dialog.jitter_row.isVisible(),
            "图片目标点击必须隐藏随机延迟组——引擎按 command 消费但 UI 无坐标语义，"
            "矩阵第 22 行不得声称此处显示该组",
        )

    def test_drag_section_is_never_used_as_the_jitter_host(self) -> None:
        """Regression: the row must not be a child of coordinate_target_form.

        That form belongs to ``coordinate_target_section``, which is hidden for
        drags — the original defect.
        """
        dialog = self._prepare_drag(self, "advanced", "左键拖拽")
        form = dialog.coordinate_target_form
        labels = [form.labelForField(w) for w in (dialog.jitter_row,)]
        self.assertTrue(
            all(label is None for label in labels),
            "随机延迟行不得再挂到 coordinate_target_form（拖拽时该容器被隐藏）",
        )


class VisibilityJudgementTests(unittest.TestCase):
    """Guard the judgement itself: isHidden() must not be used for reachability."""

    def test_isHidden_is_false_while_isVisible_is_false_for_hidden_ancestor(self) -> None:
        app = _app()
        parent = TaskConfigDialog(None, {}, base_dir=tempfile.mkdtemp(), settings_mode="advanced")
        self.addCleanup(dispose_widget, app, parent)
        app.processEvents()
        parent.show()
        app.processEvents()
        row = parent.jitter_row
        # Collapse the host section, then confirm the two predicates disagree —
        # this is exactly the trap that produced the wrong 09 verdict.
        parent.jitter_section.set_expanded(False)
        app.processEvents()
        self.assertFalse(row.isHidden())
        self.assertFalse(
            row.isVisible(),
            "折叠态下 isVisible 应为假；若此断言失败，说明隐藏祖先链已改变",
        )


if __name__ == "__main__":  # pragma: no cover - manual probe entry
    unittest.main()
