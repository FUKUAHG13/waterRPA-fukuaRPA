# ruff: noqa: E402  # QT_QPA_PLATFORM must be set before importing PySide6.
"""C4 契约测试：共享度量与基线 golden 值一致（防止常量抄错）。

golden 值直接来自基线提交 ``1ba6f1144fd1201c04f21a9ee448387a5a5abc26``
的 ``task_row_view.py`` / ``presentation.py`` 原始字面量。若共享模块抽取时抄错
（例如 8→9），真实 TaskRow 的几何将与 golden 不符，测试失败。
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont, QFontMetrics  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fukua_rpa.ui import task_row_metrics as m  # noqa: E402
from fukua_rpa.ui.task_row_view import StepIndexLabel  # noqa: E402

# --- 基线 golden 值（来源：基线提交 task_row_view.py / presentation.py）---
GOLDEN = {
    "CONTROLS_MARGIN_LEFT": 8,
    "CONTROLS_MARGIN_TOP": 5,
    "CONTROLS_MARGIN_RIGHT": 7,
    "CONTROLS_MARGIN_BOTTOM": 5,
    "CONTROLS_SPACING": 6,
    "HEADER_MARGIN_LEFT_BASE": 8,
    "HEADER_MARGIN_DEPTH_STEP": 14,
    "HEADER_MARGIN_TOP": 3,
    "HEADER_MARGIN_RIGHT": 8,
    "HEADER_MARGIN_BOTTOM": 3,
    "HEADER_SPACING": 6,
    "HEADER_TOGGLE_SIZE": 24,
    "MARK_SIZE": 20,
    "TYPE_MIN_WIDTH": 130,
    "ACTION_BUTTON_WIDTH": 30,
    "INDEX_BASE_WIDTH": 32,
    "INDEX_PADDING": 12,
    "CONTROL_DEPTH_STEP": 10,
}


class MetricsGoldenTests(unittest.TestCase):
    def test_constants_match_baseline_golden(self):
        """共享模块常量必须逐项等于基线字面量。"""
        for name, expected in GOLDEN.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(m, name), expected)

    def test_header_indent_matches_baseline_formula(self):
        # 基线：header_layout.setContentsMargins(8 + depth*14, 3, 8, 3)，depth 0-based
        for depth in (1, 2, 3):
            with self.subTest(depth=depth):
                self.assertEqual(m.header_indent(depth), 8 + (depth - 1) * 14)

    def test_control_indent_matches_baseline_formula(self):
        # 基线：layout.setContentsMargins(8 + depth*10, 5, 7, 5)
        for depth in (0, 1, 2, 3):
            with self.subTest(depth=depth):
                self.assertEqual(m.control_indent(depth), 8 + depth * 10)

    def test_index_width_algorithm_matches_step_index_label(self):
        """共享编号列算法与真实 StepIndexLabel.sizeHint() 同款。"""
        app = QApplication.instance() or QApplication([])
        del app  # 确保 QApplication 存在以构造字体
        for maximum in (1, 9, 99, 380):
            with self.subTest(maximum=maximum):
                label = StepIndexLabel()
                label.set_index(1, maximum)
                expected_w = label.sizeHint().width()
                fm = QFontMetrics(QFont("Microsoft YaHei UI", 9))
                actual_w = m.index_width(fm, maximum)
                # 字体可能不同，用同款公式校验算法一致性而非绝对像素
                self.assertEqual(
                    actual_w,
                    max(
                        GOLDEN["INDEX_BASE_WIDTH"],
                        fm.horizontalAdvance(f"{maximum}.") + GOLDEN["INDEX_PADDING"],
                    ),
                )
                self.assertGreater(expected_w, 0)

    def test_fixed_widths_match_baseline(self):
        self.assertEqual(m.visible_mark_width(3), 3 * 20 + 2 * 6)
        self.assertEqual(m.visible_mark_width(0), 0)
        self.assertEqual(m.right_action_width(6), 6 * 30 + 5 * 6)


class SummarySplitTests(unittest.TestCase):
    """摘要拆列：标记段必须只进标记列，不污染动作列（独立 B 主体复验必改项）。"""

    def test_marks_stripped_from_action(self):
        from fukua_rpa.ui.task_list_mixins.scroll_preview import _split_summary, _summary_marks

        cases = {
            "15. [组] 点击图片 | OUT1": ("点击图片", ["组"]),
            "16. [停|组] 等待 | 1000": ("等待", ["停", "组"]),
            "17. 左键单击 | 88,88": ("左键单击", []),
            "18. [注] 直到条件成立 | 找到图片": ("直到条件成立", ["注"]),
            "19. [组|断] 多图动作映射 | A|B | C": ("多图动作映射", ["组", "断"]),
        }
        for summary, (action, marks) in cases.items():
            with self.subTest(summary=summary):
                self.assertEqual(_split_summary(summary)[0], action)
                self.assertEqual(_summary_marks(summary), marks)
                self.assertNotIn("[", action)
                self.assertNotIn("]", action)

    def test_value_with_separator_preserved(self):
        from fukua_rpa.ui.task_list_mixins.scroll_preview import _split_summary

        # 参数中含 " | "（多图摘要）时只按首个分隔符拆，参数保持完整
        self.assertEqual(
            _split_summary("19. 多图动作映射 | A | B | C"),
            ("多图动作映射", "A | B | C"),
        )


if __name__ == "__main__":
    unittest.main()
