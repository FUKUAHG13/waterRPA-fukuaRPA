"""UI 开发规范静态检查器的单元测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent
from unittest import mock

from scripts.check_ui_conventions import (
    check_source,
    collect_changed_ui_lines,
    parse_unified_diff,
)


def _codes(findings):
    return {finding.code for finding in findings}


def _source(text: str) -> str:
    return dedent(text).lstrip()


class UiConventionCheckerTests(unittest.TestCase):
    def test_detects_colors_and_local_stylesheet(self):
        source = _source("""
        from PySide6.QtWidgets import QLabel

        label = QLabel()
        label.setStyleSheet("color: #123456; background: rgba(1, 2, 3, 4);")
        """)

        findings = check_source(source, path="fukua_rpa/ui/example.py")

        self.assertEqual(_codes(findings), {"UI001", "UI002"})

    def test_issue_number_and_docstring_example_are_not_color_violations(self):
        source = _source('''
        """文档示例可以讨论 #123456，但不属于界面颜色实现。"""
        label.setText("关联工单 #123456")
        ''')

        self.assertEqual(
            check_source(source, path="fukua_rpa/ui/example.py"),
            (),
        )

    def test_theme_source_is_the_single_source_for_colors_and_stylesheet(self):
        source = _source("""
        APP_STYLESHEET = "QLabel { color: #123456; }"

        def apply(widget):
            widget.setStyleSheet(APP_STYLESHEET)
        """)

        self.assertEqual(
            check_source(source, path="fukua_rpa/ui/theme.py"),
            (),
        )

    def test_detects_fixed_geometry_layout_metrics_and_font_size(self):
        source = _source("""
        widget.setFixedSize(320, 80)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)
        font.setPointSize(12)
        """)

        findings = check_source(source, path="fukua_rpa/ui/example.py")

        self.assertEqual(_codes(findings), {"UI003", "UI004", "UI005"})

    def test_zero_layout_metrics_are_allowed(self):
        source = _source("""
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        """)

        self.assertEqual(
            check_source(source, path="fukua_rpa/ui/example.py"),
            (),
        )

    def test_named_layout_metrics_are_allowed(self):
        source = _source("""
        layout.setContentsMargins(PADDING, PADDING, PADDING, PADDING)
        layout.setSpacing(SPACING_MEDIUM)
        widget.setMinimumWidth(required_width)
        """)

        self.assertEqual(
            check_source(source, path="fukua_rpa/ui/example.py"),
            (),
        )

    def test_numeric_value_nested_in_scale_expression_is_still_checked(self):
        source = _source("""
        layout.setSpacing(int(6 * scale))
        widget.setMinimumWidth(round(120 * scale))
        """)

        self.assertEqual(
            _codes(check_source(source, path="fukua_rpa/ui/example.py")),
            {"UI003", "UI004"},
        )

    def test_rule_specific_exception_requires_a_reason(self):
        allowed = _source("""
        # UI-CONVENTION: allow fixed-geometry reason=图标按钮需要稳定命中区域
        button.setFixedSize(size, size)
        """)
        invalid = _source("""
        # UI-CONVENTION: allow fixed-geometry
        button.setFixedSize(size, size)
        """)

        self.assertEqual(check_source(allowed, path="fukua_rpa/ui/example.py"), ())
        self.assertEqual(
            _codes(check_source(invalid, path="fukua_rpa/ui/example.py")),
            {"UI000", "UI003"},
        )

    def test_unknown_exception_target_is_not_an_escape_hatch(self):
        source = _source("""
        # UI-CONVENTION: allow whatever reason=绕过所有检查
        button.setFixedWidth(120)
        """)

        self.assertEqual(
            _codes(check_source(source, path="fukua_rpa/ui/example.py")),
            {"UI000", "UI003"},
        )

    def test_directive_text_inside_a_string_is_not_an_exception(self):
        source = _source('''
        note = "# UI-CONVENTION: allow fixed-geometry reason=这只是用户可见文字"
        button.setFixedWidth(120)
        ''')

        self.assertEqual(
            _codes(check_source(source, path="fukua_rpa/ui/example.py")),
            {"UI003"},
        )

    def test_changed_syntax_error_cannot_pass_when_parser_points_elsewhere(self):
        source = "value = (\n"

        self.assertEqual(
            _codes(
                check_source(
                    source,
                    path="fukua_rpa/ui/example.py",
                    selected_lines={1},
                )
            ),
            {"UI099"},
        )

    def test_numeric_qcolor_is_checked(self):
        source = _source("""
        color = QColor(15, 23, 42, 80)
        other = QtGui.QColor.fromRgb(1, 2, 3)
        """)

        findings = check_source(source, path="fukua_rpa/ui/example.py")
        self.assertEqual(_codes(findings), {"UI001"})
        self.assertEqual(len(findings), 2)

    def test_unrelated_from_rgb_method_is_not_treated_as_qcolor(self):
        source = "image.fromRgb(1, 2, 3)\n"

        self.assertEqual(
            check_source(source, path="fukua_rpa/ui/example.py"),
            (),
        )

    def test_changed_line_filter_does_not_recheck_untouched_legacy_lines(self):
        source = _source("""
        old_button.setFixedWidth(120)
        new_button.setFixedWidth(130)
        """)

        findings = check_source(
            source,
            path="fukua_rpa/ui/example.py",
            selected_lines={2},
        )

        self.assertEqual([finding.line for finding in findings], [2])

    def test_unified_diff_parser_tracks_added_lines(self):
        diff = """\
diff --git a/fukua_rpa/ui/example.py b/fukua_rpa/ui/example.py
--- a/fukua_rpa/ui/example.py
+++ b/fukua_rpa/ui/example.py
@@ -4,2 +4,4 @@
 old line
+new line one
+new line two
 unchanged
@@ -20 +22 @@
-removed
+replacement
"""

        self.assertEqual(
            parse_unified_diff(diff),
            {"fukua_rpa/ui/example.py": frozenset({5, 6, 22})},
        )

    def test_diff_body_line_starting_with_plus_plus_is_not_a_file_header(self):
        """FR-20260811-D15-002 场景 B: 新增行内容以 ``+++ `` 开头（如 ASCII 分隔线）
        不得被误判为文件头。修复前该行改写 current_path 为伪路径，后续 hunk 被
        ``fukua_rpa/ui/`` 前缀过滤全部丢弃，门禁 fail-open（报告 0 文件通过）。"""
        diff = """\
diff --git a/fukua_rpa/ui/example.py b/fukua_rpa/ui/example.py
--- a/fukua_rpa/ui/example.py
+++ b/fukua_rpa/ui/example.py
@@ -1,3 +1,5 @@
 line one
 line two
+++ 分区 +++
+real new line
"""
        self.assertEqual(
            parse_unified_diff(diff),
            {"fukua_rpa/ui/example.py": frozenset({3, 4})},
        )

    def test_diff_body_deletion_starting_with_dash_dash_is_skipped(self):
        """FR-20260811-D15-002: 删除行内容以 ``--- `` 开头（``-- `` 或 ``--- `` 前缀）
        按删除行处理，不占用新文件行号；后续新增行行号不受污染。"""
        diff = """\
diff --git a/fukua_rpa/ui/example.py b/fukua_rpa/ui/example.py
--- a/fukua_rpa/ui/example.py
+++ b/fukua_rpa/ui/example.py
@@ -1,3 +1,3 @@
--- 旧分隔
 line two
+new line three
"""
        self.assertEqual(
            parse_unified_diff(diff),
            {"fukua_rpa/ui/example.py": frozenset({2})},
        )

    def test_added_line_with_triple_plus_no_space_is_counted(self):
        """FR-20260811-D15-002: hunk 内新增行以 ``+`` 开头即计入（内容形如
        ``++xxx`` / ``+++xxx``），不因 ``startswith("+++")`` 误判为文件头而漏检。"""
        diff = """\
diff --git a/fukua_rpa/ui/example.py b/fukua_rpa/ui/example.py
--- a/fukua_rpa/ui/example.py
+++ b/fukua_rpa/ui/example.py
@@ -1 +1,2 @@
 old
+++marker
"""
        self.assertEqual(
            parse_unified_diff(diff),
            {"fukua_rpa/ui/example.py": frozenset({2})},
        )

    def test_multi_file_diff_keeps_paths_isolated(self):
        """P1 回归守卫：单次 diff 含多个文件时，第二文件段的 ``+++ `` 头必须被
        识别为文件头（而非误判为新增行），否则后续文件全部漏检。"""
        diff = """\
diff --git a/fukua_rpa/ui/alpha.py b/fukua_rpa/ui/alpha.py
--- a/fukua_rpa/ui/alpha.py
+++ b/fukua_rpa/ui/alpha.py
@@ -1 +1,2 @@
 old
+added in alpha
diff --git a/fukua_rpa/ui/beta.py b/fukua_rpa/ui/beta.py
--- a/fukua_rpa/ui/beta.py
+++ b/fukua_rpa/ui/beta.py
@@ -3 +3,2 @@
 unchanged
+added in beta
"""
        self.assertEqual(
            parse_unified_diff(diff),
            {
                "fukua_rpa/ui/alpha.py": frozenset({2}),
                "fukua_rpa/ui/beta.py": frozenset({4}),
            },
        )

    def test_changed_collection_uses_explicit_base_and_includes_untracked_files(self):
        diff = """\
diff --git a/fukua_rpa/ui/existing.py b/fukua_rpa/ui/existing.py
--- a/fukua_rpa/ui/existing.py
+++ b/fukua_rpa/ui/existing.py
@@ -1 +1 @@
-old
+new
"""
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            new_path = root / "fukua_rpa" / "ui" / "new.py"
            new_path.parent.mkdir(parents=True)
            new_path.write_text("first\nsecond\n", encoding="utf-8")
            with mock.patch(
                "scripts.check_ui_conventions.ROOT", root
            ), mock.patch(
                "scripts.check_ui_conventions._run_git",
                side_effect=(diff, "fukua_rpa/ui/new.py\n"),
            ) as run_git:
                changed = collect_changed_ui_lines("base-sha")

        self.assertEqual(
            changed,
            {
                "fukua_rpa/ui/existing.py": frozenset({1}),
                "fukua_rpa/ui/new.py": frozenset({1, 2}),
            },
        )
        self.assertEqual(run_git.call_args_list[0].args[4], "base-sha")


if __name__ == "__main__":
    unittest.main()
