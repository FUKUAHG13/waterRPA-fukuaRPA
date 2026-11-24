# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.
"""v1.1.17 修复 3：鼠标轨迹/插件动作的悬停与占位提示随类型更新。"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QLineEdit,
    QPushButton,
)

from fukua_rpa.ui.task_row_presentation import (  # noqa: E402
    apply_task_type_presentation,
)


class _FakeRow:
    def __init__(self):
        self._unsupported_task_label = ""
        self._loading_data = False
        self._unsupported_task_type = None
        self._unsupported_task_payload = None
        self._last_type_name = "左键单击"
        self.custom_data = {}
        self.value_input = QLineEdit()
        self.type_combo = QComboBox()
        self.file_btn = QPushButton()
        self.template_capture_btn = QPushButton()

    def sync_data(self):
        pass


class TaskRowTypeTipsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _row_after(self, type_name, previous="左键单击"):
        row = _FakeRow()
        row._last_type_name = previous
        # 模拟上一类型留下的提示与占位。
        row.type_combo.setToolTip("【左键单击】\n旧提示")
        row.value_input.setToolTip("【左键单击】\n旧提示")
        row.value_input.setPlaceholderText("输入图片路径 或 屏幕坐标，如：960,540")
        apply_task_type_presentation(row, type_name)
        return row

    def test_mouse_trajectory_tips_replace_previous(self):
        row = self._row_after("鼠标轨迹")
        self.assertIn("鼠标轨迹", row.type_combo.toolTip())
        self.assertIn("鼠标轨迹", row.value_input.toolTip())
        self.assertNotIn("左键单击", row.value_input.toolTip())
        self.assertNotIn("960,540", row.value_input.placeholderText())

    def test_plugin_action_tips_replace_previous_text(self):
        row = self._row_after("插件动作", previous="输入文本")
        self.assertIn("插件动作", row.type_combo.toolTip())
        self.assertIn("插件动作", row.value_input.toolTip())
        self.assertIn("齿轮", row.value_input.toolTip())
        self.assertNotIn("输入想要发送的文本内容", row.value_input.placeholderText())

    def test_plugin_action_from_click_then_back_to_text(self):
        row = _FakeRow()
        row._last_type_name = "左键单击"
        apply_task_type_presentation(row, "插件动作")
        self.assertIn("插件动作", row.value_input.toolTip())
        # 切回输入文本后提示恢复为输入文本专用，不残留插件文案。
        apply_task_type_presentation(row, "输入文本")
        self.assertIn("输入文本", row.value_input.toolTip())
        self.assertNotIn("插件动作", row.value_input.toolTip())

    def test_unsupported_action_tips_take_priority(self):
        # 未知动作保护：is_unsupported 分支保持最高优先级，不被通用提示覆盖。
        row = _FakeRow()
        row._last_type_name = "输入文本"
        row._unsupported_task_label = "未知动作(99.0)"
        apply_task_type_presentation(row, "未知动作(99.0)")
        self.assertIn("无法识别", row.value_input.toolTip())
        self.assertIn("不支持", row.value_input.placeholderText())

    def test_unregistered_type_does_not_inherit_previous_tips(self):
        # 未登记专用提示的动作不应拿到上一个动作的提示/占位。
        row = self._row_after("某个未来动作")
        self.assertNotIn("左键单击", row.value_input.toolTip())
        self.assertNotIn("960,540", row.value_input.placeholderText())


if __name__ == "__main__":
    unittest.main()
