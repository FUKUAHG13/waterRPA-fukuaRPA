import ast
import ctypes
import inspect
import os
import textwrap
import unittest

from fukua_rpa.text_input import KEYBDINPUT
from fukua_rpa.win32_api import KBDLLHOOKSTRUCT, MSLLHOOKSTRUCT
from fukua_rpa.window_diagnostics import (
    _handle_is_open,
    classify_control,
    format_window_inspection,
    integrity_label_from_rid,
    process_integrity,
)

try:
    # FR-SCOPE015D-003: 修复后新增的统一换行净化助手。守卫导入是为了让「助手
    # 缺失」在修复前表现为干净的断言失败，而不是整个测试模块的收集错误。
    from fukua_rpa.window_diagnostics import _single_line
except ImportError:  # pragma: no cover - 仅修复前走到
    _single_line = None


class WindowDiagnosticsTests(unittest.TestCase):
    def test_win32_pointer_sized_fields_use_opaque_pointer_type(self):
        self.assertIs(dict(KEYBDINPUT._fields_)["dwExtraInfo"], ctypes.c_void_p)
        self.assertIs(
            dict(KBDLLHOOKSTRUCT._fields_)["dwExtraInfo"], ctypes.c_void_p
        )
        self.assertIs(
            dict(MSLLHOOKSTRUCT._fields_)["dwExtraInfo"], ctypes.c_void_p
        )

    def test_handle_guard_accepts_integer_and_ctypes_handles(self):
        self.assertFalse(_handle_is_open(None))
        self.assertFalse(_handle_is_open(0))
        self.assertFalse(_handle_is_open(ctypes.c_void_p()))
        self.assertTrue(_handle_is_open(123))
        self.assertTrue(_handle_is_open(ctypes.c_void_p(123)))

    def test_integrity_rids_have_stable_user_facing_labels(self):
        self.assertEqual(integrity_label_from_rid(0x1000), "低")
        self.assertEqual(integrity_label_from_rid(0x2000), "中")
        self.assertEqual(integrity_label_from_rid(0x3000), "高（管理员）")
        self.assertEqual(integrity_label_from_rid(0x4000), "系统")

    def test_control_compatibility_is_conservative(self):
        standard = classify_control("Button")
        canvas = classify_control("Chrome_RenderWidgetHostHWND")
        elevated = classify_control("Button", permission_blocked=True)
        self.assertTrue(standard["standard_win32_control"])
        self.assertIn("较可能有效", standard["classification"])
        self.assertTrue(canvas["custom_surface"])
        self.assertIn("兼容性较低", canvas["classification"])
        self.assertIn("权限阻止", elevated["classification"])

    def test_current_process_integrity_can_be_read(self):
        report = process_integrity(os.getpid())
        self.assertTrue(report["available"], report)
        self.assertGreaterEqual(report["rid"], 0x1000)
        self.assertTrue(report["label"])


#: 注入用的伪造行标记：独立成行即说明净化缺失。
_FORGED_LINE = "警告：伪造"
_FORGED_PAYLOAD = f"A\r\n{_FORGED_LINE}"

#: 全部**外部可控**字段：(标签, report 内的所在层, 字段名)。
#: 被检查的程序控制这些值，而报告文本可被复制到剪贴板（binding.py:163/:174）。
_EXTERNAL_FIELDS = [
    ("root.title", "root", "title"),
    ("root.class", "root", "class"),
    ("process.name", "process", "name"),
    ("process.path", "process", "path"),
    ("target.class", "target", "class"),
    ("uia_control.name", "uia_control", "name"),
    ("uia_control.control_type", "uia_control", "control_type"),
    ("uia_control.framework", "uia_control", "framework"),
    ("uia.error", "uia", "error"),
    ("child.class", "child", "class"),
    ("child.title", "child", "title"),
]


def _benign_report() -> dict:
    """A fully benign inspection report with no embedded line breaks."""
    return {
        "valid": True,
        "root": {"hwnd": 100, "class": "Notepad", "title": "无标题 - 记事本"},
        "target": {
            "hwnd": 200,
            "class": "Edit",
            "control_id": 15,
            "client_rect": [0, 0, 800, 600],
        },
        "process": {
            "name": "notepad.exe",
            "path": r"C:\Windows\notepad.exe",
            "pid": 4321,
        },
        "integrity": {"fukuaRPA": {"label": "中"}, "target": {"label": "中"}},
        "compatibility": {"classification": "标准控件", "ui_automation_status": "可用"},
        "ui_automation": {
            "actionable": True,
            "matched_bound_hwnd": False,
            "nodes_scanned": 12,
            "candidate_controls": 3,
            "control": {
                "name": "文本编辑器",
                "control_type": "Edit",
                "framework": "Win32",
                "action": "ValuePattern",
            },
        },
        "child_count": 2,
        "children": [
            {"hwnd": 201, "class": "Edit", "control_id": 15, "title": "正文", "depth": 0},
            {"hwnd": 202, "class": "Static", "control_id": 16, "title": "", "depth": 1},
        ],
        "warnings": [],
    }


def _inject(report: dict, where: str, field: str) -> dict:
    """Return a shallow copy of `report` with a forged-line payload in one field."""
    mutated = {
        key: (
            dict(value)
            if isinstance(value, dict)
            else [dict(item) for item in value]
            if isinstance(value, list)
            else value
        )
        for key, value in report.items()
    }
    if where == "uia_control":
        mutated["ui_automation"] = dict(mutated["ui_automation"])
        control = dict(mutated["ui_automation"]["control"])
        control[field] = _FORGED_PAYLOAD
        mutated["ui_automation"]["control"] = control
    elif where == "uia":
        mutated["ui_automation"] = dict(mutated["ui_automation"])
        mutated["ui_automation"][field] = _FORGED_PAYLOAD
        if field == "error":
            # 「UIA 说明」行只在 not actionable 时输出；不切到该分支的话
            # 「未泄漏」只是「没被渲染」造成的假通过。
            mutated["ui_automation"]["actionable"] = False
    elif where == "child":
        children = [dict(child) for child in mutated["children"]]
        children[0][field] = _FORGED_PAYLOAD
        mutated["children"] = children
    else:
        mutated[where] = dict(mutated[where])
        mutated[where][field] = _FORGED_PAYLOAD
    return mutated


class WindowInspectionReportInjectionTests(unittest.TestCase):
    """FR-SCOPE015D-003: 诊断报告不得被目标程序注入伪造行。

    报告文本经 `ui/controllers/mapping_mixins/binding.py:163` 进入只读视图，
    并经 `:174-175` 的「复制报告」进入剪贴板。此前只有子控件标题做了换行净化，
    其余外部可控字段未净化 ⇒ 被检查的程序可伪造行。

    `ADV-SCOPE015D-002` 要求统一净化助手覆盖**所有**外部可控字段，因此这里按
    「所有外部可控字段」而不是仅当时定位到的 5 处设断言。
    """

    def test_every_externally_controlled_field_cannot_forge_a_line(self):
        report = _benign_report()
        for label, where, field in _EXTERNAL_FIELDS:
            with self.subTest(field=label):
                text = format_window_inspection(_inject(report, where, field))
                self.assertNotIn("\r", text)
                self.assertFalse(
                    any(line.strip() == _FORGED_LINE for line in text.split("\n")),
                    f"{label} 仍可向诊断报告注入独立伪造行",
                )

    def test_report_layout_is_unchanged(self):
        lines = format_window_inspection(_benign_report()).split("\n")
        # 11 行表头 + 空行 + 子控件表头 + 2 个子控件行。
        self.assertEqual(len(lines), 11 + 1 + 1 + 2)
        self.assertEqual(lines[11], "")
        self.assertEqual(lines[12], "子控件（最多 128 项）：")
        self.assertTrue(lines[13].startswith("  HWND 201"))
        self.assertTrue(lines[14].startswith("    HWND 202"))

    def test_benign_values_are_not_altered(self):
        text = format_window_inspection(_benign_report())
        # 不得过度替换：含空格的标题、反斜杠路径、空标题回退文案都要原样保留。
        self.assertIn("无标题 - 记事本", text)
        self.assertIn(r"C:\Windows\notepad.exe", text)
        self.assertIn("（无文本）", text)
        self.assertIn("文本编辑器", text)

    def test_sanitizing_is_shared_not_hand_rolled(self):
        # ADV-SCOPE015D-002：必须是**统一助手**，不能回到逐字段手工 replace
        # （那正是 5 处遗漏的成因）。
        if _single_line is None:
            self.fail("FR-SCOPE015D-003: 缺少统一换行净化助手 _single_line")
        self.assertEqual(_single_line(None), "")
        # 逐字符替换（而非把 "\r\n" 折叠成一个空格）是刻意保留的既有变换：
        # 原先子控件标题就写作 `.replace("\r", " ").replace("\n", " ")`，
        # 统一助手必须与它逐字节等价，否则会顺带改变既有输出。
        self.assertEqual(_single_line("a\r\nb\rc\nd"), "a  b c d")
        source = textwrap.dedent(inspect.getsource(format_window_inspection))
        called = {
            node.func.id
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("_single_line", called)
        self.assertNotIn('.replace("\\r"', source)
        self.assertNotIn(".replace('\\r'", source)

    def test_single_line_folds_unicode_line_separators(self):
        # FR-20260930-011：Unicode 强制/行边界字符（U+2028/U+2029/U+0085、
        # \v、\f、\x1c-\x1e）也必须折叠，否则被检查程序可用它们伪造报告行。
        if _single_line is None:
            self.fail("FR-SCOPE015D-003: 缺少统一换行净化助手 _single_line")
        for ch in ("\u2028", "\u2029", "\x85", "\v", "\f", "\x1c", "\x1d", "\x1e"):
            self.assertEqual(_single_line(f"a{ch}b"), "a b")
            self.assertNotIn(ch, _single_line(f"title{ch}injected"))
        # 既有逐字符行为保持不变（每个 \r / \n → 一个空格）。
        self.assertEqual(_single_line("a\r\nb"), "a  b")


if __name__ == "__main__":
    unittest.main()


class ChildrenTruncationTests(unittest.TestCase):
    """FR-20261005-301: 截断状态必须来自 EnumChildWindows 的返回值。"""

    def _inspector(self):
        from fukua_rpa.window_diagnostics import WindowInspector

        inspector = WindowInspector.__new__(WindowInspector)
        inspector.window_info = lambda hwnd, root_hwnd=None: {"hwnd": hwnd}  # type: ignore[method-assign]
        return inspector

    def test_exactly_at_cap_is_not_reported_as_truncated(self):
        from unittest import mock

        from fukua_rpa import window_diagnostics as module

        inspector = self._inspector()

        def fake_enum(hwnd, callback, lparam):
            for index in range(128):
                self.assertTrue(callback(index, 0))
            return 1  # every child enumerated

        with mock.patch.object(module, "user32") as fake_user32:
            fake_user32.EnumChildWindows.side_effect = fake_enum
            children, truncated = inspector.enumerate_children_with_truncation(1, 128)
        self.assertEqual(len(children), 128)
        self.assertFalse(truncated)

    def test_more_than_cap_is_reported_as_truncated(self):
        from unittest import mock

        from fukua_rpa import window_diagnostics as module

        inspector = self._inspector()

        def fake_enum(hwnd, callback, lparam):
            for index in range(200):
                if not callback(index, 0):
                    return 0  # callback aborted the enumeration at the cap
            return 1

        with mock.patch.object(module, "user32") as fake_user32:
            fake_user32.EnumChildWindows.side_effect = fake_enum
            children, truncated = inspector.enumerate_children_with_truncation(1, 128)
        self.assertEqual(len(children), 128)
        self.assertTrue(truncated)

    def test_inspect_children_truncated_uses_enumeration_flag_not_recount(self):
        """FR-20261008-907：``inspect`` 的 children_truncated 必须来自枚举结果。

        此前 ``inspect`` 调用丢弃标志的 ``enumerate_children``，再用
        ``len(children) >= 128`` 重算——恰好 128 个子控件时枚举自然结束
        （truncated=False），重算却把 128 >= 128 误判为 True，向用户宣称
        「已截断」；且 128 是 max_items 默认值的复制粘贴，未来其它上限传入
        时该推导会静默漂移。
        """
        from unittest import mock

        from fukua_rpa import window_diagnostics as module

        inspector = self._inspector()
        inspector.process_id = 1234
        inspector.process_info = lambda hwnd: {"pid": 1}  # type: ignore[method-assign]
        children = [{"hwnd": index} for index in range(128)]

        with mock.patch.object(module, "user32") as fake_user32, \
                mock.patch.object(
                    module, "process_integrity", return_value={"available": False}
                ), mock.patch.object(
                    module, "classify_control",
                    return_value={"custom_surface": False},
                ), mock.patch.object(
                    inspector,
                    "enumerate_children_with_truncation",
                    return_value=(children, False),
                ) as trunc:
            fake_user32.GetAncestor.return_value = 0
            fake_user32.IsWindow.return_value = True
            report = inspector.inspect(1, 1)

        trunc.assert_called_once()
        self.assertEqual(report["child_count"], 128)
        self.assertFalse(
            report["children_truncated"],
            "恰好 128 个子控件时枚举自然结束，不得重算为「已截断」",
        )

    def test_inspect_children_truncated_still_true_when_enumeration_hit_cap(self):
        """同一边界的另一侧：枚举真因上限停止时仍必须报 True（防放宽）。"""
        from unittest import mock

        from fukua_rpa import window_diagnostics as module

        inspector = self._inspector()
        inspector.process_id = 1234
        inspector.process_info = lambda hwnd: {"pid": 1}  # type: ignore[method-assign]
        children = [{"hwnd": index} for index in range(128)]

        with mock.patch.object(module, "user32") as fake_user32, \
                mock.patch.object(
                    module, "process_integrity", return_value={"available": False}
                ), mock.patch.object(
                    module, "classify_control",
                    return_value={"custom_surface": False},
                ), mock.patch.object(
                    inspector,
                    "enumerate_children_with_truncation",
                    return_value=(children, True),
                ):
            fake_user32.GetAncestor.return_value = 0
            fake_user32.IsWindow.return_value = True
            report = inspector.inspect(1, 1)

        self.assertEqual(report["child_count"], 128)
        self.assertTrue(report["children_truncated"])
