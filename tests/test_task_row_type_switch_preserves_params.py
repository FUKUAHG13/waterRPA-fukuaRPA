"""TST02 / issue #71: 切换动作类型不得清空图片/坐标/截图区域/条件参数。

``apply_task_type_presentation`` 是行内动作类型切换的展示处理函数；它只调整控件
可见性、占位符与 ``value_input`` 的摘要草稿，绝不应清除 ``custom_data`` 中已录入的
图片、坐标、截图区域与条件参数。本测试锁定该不变量。
"""

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
    """最小化替身：仅提供 ``apply_task_type_presentation`` 读写到的属性。"""

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
        self.sync_data_called = False

    def sync_data(self):
        self.sync_data_called = True


class TaskRowTypeSwitchPreservesParamsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _row(self, custom_data):
        row = _FakeRow()
        row.custom_data = dict(custom_data)
        return row

    def _assert_four_groups_intact(self, row, base):
        # 校验 base 中提供的四类参数（图片/坐标/截图区域/条件）在切换后既不丢失也不被改写。
        for key in base:
            self.assertIn(key, row.custom_data, f"切换动作后丢失参数：{key}")
            self.assertEqual(
                row.custom_data[key], base[key], f"切换动作后参数被改写：{key}"
            )
        # 切回普通动作后，摘要类型写入的草稿键应已被弹出，避免坐标/路径永久残留。
        self.assertNotIn("_value_draft", row.custom_data, "切回普通动作后残留摘要草稿键")
        self.assertNotIn(
            "saved_value_draft", row.custom_data, "切回普通动作后残留持久化草稿键"
        )

    def test_switch_to_summary_then_back_preserves_params(self):
        base = {
            "image_path": "C:/Users/Alice/target.png",
            "recognition": "template",
            "click_points": [[10, 20]],
            "coord_x": 100,
            "coord_y": 200,
            "coord_step_en": True,
            "capture_region": {"x": 0, "y": 0, "width": 800, "height": 600},
            "until_condition": {"type": "appear", "image": "C:/Users/Alice/c.png"},
            "repeat_count": 3,
        }
        for summary in ("直到条件成立", "多图动作映射"):
            with self.subTest(summary=summary):
                row = self._row(base)
                apply_task_type_presentation(row, summary)
                apply_task_type_presentation(row, "左键单击")
                self._assert_four_groups_intact(row, base)

    def test_switch_between_normal_actions_preserves_params(self):
        base = {
            "image_path": "C:/Users/Alice/t.png",
            "coord_x": 5,
            "coord_y": 6,
            "capture_region": {"x": 1, "y": 2, "width": 3, "height": 4},
            "until_condition": {"type": "disappear"},
            "repeat_count": 1,
        }
        row = self._row(base)
        apply_task_type_presentation(row, "右键双击")
        apply_task_type_presentation(row, "鼠标悬停")
        self._assert_four_groups_intact(row, base)

    def test_summary_draft_survives_reload_with_persisted_key(self):
        """FR-20260812-D12-001: 摘要草稿必须跨保存→重载恢复。

        进入"直到条件成立"时原始值写入可持久化键 saved_value_draft；
        模拟重载（custom_data 从落盘数据重建、value_input 恢复摘要文本）后
        切离摘要类型，原始值不应被清空。
        """
        base = {
            "coord_x": 100,
            "coord_y": 200,
            "repeat_count": 3,
            "saved_value_draft": "-1521,144",  # 落盘后的持久化草稿
        }
        row = self._row(base)
        # 模拟 apply_task_row_data 加载摘要类型：value_input 先被设为摘要文本，
        # 然后 show_until_condition_summary 发现已是摘要前缀，不覆盖草稿。
        row.value_input.setText("请点击右侧小齿轮修改 | 图片出现")
        row._last_type_name = "直到条件成立"
        # 重载完成，用户把动作切回普通动作
        apply_task_type_presentation(row, "左键单击")
        self.assertEqual(row.value_input.text(), "-1521,144")
        self.assertNotIn("saved_value_draft", row.custom_data)

    def test_legacy_underscore_draft_still_restored(self):
        """旧会话内 _value_draft 草稿在切离时仍可恢复（向后兼容）。"""
        base = {
            "coord_x": 100,
            "coord_y": 200,
            "_value_draft": "10,20",
        }
        row = self._row(base)
        row._last_type_name = "直到条件成立"
        apply_task_type_presentation(row, "左键单击")
        self.assertEqual(row.value_input.text(), "10,20")
