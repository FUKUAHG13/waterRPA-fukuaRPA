import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEAVY_MODULES = ("cv2", "numpy", "pyautogui", "uiautomation")


class StartupArchitectureTests(unittest.TestCase):
    def run_probe(self, source):
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, "-c", source],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        return json.loads(completed.stdout.strip())

    def test_early_startup_calls_have_visible_fallback(self):
        """FR-20260812-D17-003: 看门狗安装前的早期启动调用（创建数据目录、
        SingleInstanceGuard 构造）必须位于 try 兜底内，冻结包下启动失败不得
        静默退出且无提示。"""
        import ast

        source = (ROOT / "fukuaRPA.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        main_fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )

        # 收集所有 try 块直接 body 中语句的 AST 文本（含嵌套 try 的调用）。
        guarded_texts: set[str] = set()
        for node in ast.walk(main_fn):
            if isinstance(node, ast.Try):
                for stmt in node.body:
                    guarded_texts.add(ast.dump(stmt, include_attributes=False))

        # 每个 try body 语句的文本里出现目标调用即可认为被兜底。
        found_makedirs = any(
            "startup_runtime_dir" in text and "makedirs" in text
            for text in guarded_texts
        )
        found_guard = any(
            "SingleInstanceGuard" in text for text in guarded_texts
        )
        self.assertTrue(found_makedirs, "os.makedirs(startup_runtime_dir) 不在 try 兜底内")
        self.assertTrue(found_guard, "SingleInstanceGuard 构造不在 try 兜底内")

    def test_crash_guard_install_failure_is_visible_and_returns_nonzero(self):
        """FR-20261012-726: crash_guard 的安装段（lazy import +
        install_fault_handler + launch_watchdog）位于主 try 之外。冻结包
        （console=False）下这段抛异常时进程静默退出，既无提示也无崩溃报告。"""
        source = """
import ctypes, json, sys, tempfile
from unittest import mock
import fukuaRPA
from fukua_rpa import crash_guard
runtime_dir = tempfile.mkdtemp()
argv = ["fukuaRPA.py", "--startup-runtime-dir", runtime_dir]
with mock.patch.object(sys, "argv", argv), mock.patch.object(
    ctypes.windll.user32, "MessageBoxW"
) as box, mock.patch.object(
    crash_guard, "install_fault_handler", side_effect=RuntimeError("boom")
):
    try:
        code = fukuaRPA.main()
    except BaseException as exc:  # 未兜底时会传播出来
        code = "UNCAUGHT:%s" % type(exc).__name__
print(json.dumps({"code": code, "box_calls": box.call_count}))
"""
        report = self.run_probe(source)
        self.assertEqual(
            report["code"], 4, "看门狗安装失败必须返回非零，不得静默退出"
        )
        self.assertEqual(report["box_calls"], 1, "必须给出可见错误提示")

    def test_late_paint_does_not_override_fallback_sentinel(self):
        """FR-20260812-D16-001: shell_first_painted 在 fallback 已启动工作区
        加载后（_workspace_load_started 为真）必须直接返回，不得用晚到 paintEvent
        覆写 -1.0 哨兵，避免冒烟排序断言偶发误报。"""
        import ast

        source = (ROOT / "fukuaRPA.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        main_fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        # 定位 shell_first_painted 闭包：首个引用 startup_metrics 且定义了
        # _workspace_load_started nonlocal 的函数。
        painted_fn = None
        for node in ast.walk(main_fn):
            if not isinstance(node, ast.FunctionDef) or node.name != "shell_first_painted":
                continue
            painted_fn = node
            break
        self.assertIsNotNone(painted_fn, "未找到 shell_first_painted")
        body_text = ast.dump(painted_fn, include_attributes=False)
        # 覆写前必须检查 _workspace_load_started 并提前返回
        self.assertIn("_workspace_load_started", body_text)
        self.assertIn("Return", body_text)
        # 设置 _workspace_load_started=True 与写入首帧指标之间不得颠倒顺序：
        # 先置标志再写指标（写指标在 Return 之后、置标志之前会漏检）。
        # 这里以"置标志语句出现在写指标语句之前"的文本顺序做轻量断言。
        source_fn = ast.get_source_segment(source, painted_fn) or ""
        set_marker = source_fn.find("_workspace_load_started = True")
        write_metric = source_fn.find("python_to_shell_first_paint_ms")
        self.assertGreaterEqual(
            set_marker, 0, "缺少 _workspace_load_started = True 置位"
        )
        self.assertGreaterEqual(write_metric, 0, "缺少首帧指标写入")
        self.assertLess(
            set_marker, write_metric,
            "必须先置 _workspace_load_started 再写首帧指标（防止晚到 paint 覆写）",
        )

    def test_entrypoint_import_keeps_workspace_and_heavy_backends_deferred(self):
        report = self.run_probe(
            "import json,sys; import fukuaRPA; "
            "names=('fukua_rpa.ui.main_window','cv2','numpy','pyautogui','uiautomation'); "
            "print(json.dumps({name: name in sys.modules for name in names}))"
        )
        self.assertFalse(any(report.values()), report)

    def test_workspace_import_does_not_eagerly_load_optional_backends(self):
        report = self.run_probe(
            "import json,sys; import fukua_rpa.ui.main_window; "
            f"names={HEAVY_MODULES!r}; "
            "print(json.dumps({name: name in sys.modules for name in names}))"
        )
        self.assertFalse(any(report.values()), report)

    def test_deferred_engine_construction_does_not_initialize_opencv(self):
        report = self.run_probe(
            "import json,sys; from fukua_rpa.engine import RPAEngine; "
            "engine=RPAEngine(defer_backends=True); "
            "print(json.dumps({'ready':engine.backends_ready,"
            "'cv2':'cv2' in sys.modules,'numpy':'numpy' in sys.modules}))"
        )
        self.assertFalse(report["ready"])
        self.assertFalse(report["cv2"])
        self.assertFalse(report["numpy"])

    def test_standard_qt_context_menus_load_the_bundled_chinese_translation(self):
        report = self.run_probe(
            "import json; import fukuaRPA; "
            "from PySide6.QtWidgets import QApplication,QLineEdit; "
            "app=QApplication([]); ok=fukuaRPA.install_qt_chinese_translator(app); "
            "edit=QLineEdit(); menu=edit.createStandardContextMenu(); "
            "texts=[a.text() for a in menu.actions() if a.text()]; "
            "print(json.dumps({'ok':ok,'texts':texts},ensure_ascii=False))"
        )
        self.assertTrue(report["ok"])
        joined = " ".join(report["texts"])
        self.assertIn("复制", joined)
        self.assertIn("粘贴", joined)
        self.assertIn("全选", joined)

    def test_startup_shell_keeps_its_pre_theme_pixel_font_baseline(self):
        report = self.run_probe(
            "import json; "
            "from PySide6.QtWidgets import QApplication; "
            "from fukua_rpa.ui.startup import StartupShell; "
            "app=QApplication([]); shell=StartupShell(); qss=shell.styleSheet(); "
            "print(json.dumps({'brand':'font-size: 34px' in qss,"
            "'version':'font-size: 13px' in qss,"
            "'status':'font-size: 14px' in qss}))"
        )
        self.assertTrue(all(report.values()), report)

    def test_startup_shell_colors_follow_theme_tokens(self):
        """FR-20260809-0702: 启动画面颜色必须与 theme.py 语义 token 一致
        （样式表渲染自 token），保证主题调整后启动画面跟随。"""
        report = self.run_probe(
            "import json; "
            "from PySide6.QtWidgets import QApplication; "
            "from fukua_rpa.ui.theme import ("
            "    STARTUP_BODY_LIGHT, STARTUP_BRAND_LIGHT, STARTUP_CHUNK_LIGHT,"
            "    STARTUP_TRACK_LIGHT, STARTUP_VERSION_LIGHT, STARTUP_STATUS_LIGHT); "
            "from fukua_rpa.ui.startup import StartupShell; "
            "app=QApplication([]); shell=StartupShell(); qss=shell.styleSheet(); "
            "print(json.dumps({'body': STARTUP_BODY_LIGHT in qss,"
            "'brand': STARTUP_BRAND_LIGHT in qss,"
            "'chunk': STARTUP_CHUNK_LIGHT in qss,"
            "'track': STARTUP_TRACK_LIGHT in qss,"
            "'version': STARTUP_VERSION_LIGHT in qss,"
            "'status': STARTUP_STATUS_LIGHT in qss}))"
        )
        self.assertTrue(all(report.values()), report)

    def test_startup_shell_has_no_hardcoded_hex_colors(self):
        """FR-20260809-0702: startup.py 源码不得散落硬编码 hex 色值（须从
        theme.py token 取色）——修复前 12 处硬编码 hex 无回归保护。"""
        import re

        source = (ROOT / "fukua_rpa" / "ui" / "startup.py").read_text(
            encoding="utf-8"
        )
        hardcoded = re.findall(r"#[0-9A-Fa-f]{6}", source)
        self.assertEqual(
            hardcoded,
            [],
            f"startup.py 不应含硬编码 hex 色值，应改从 theme.py token 取色: {hardcoded}",
        )

    def test_startup_shell_consumes_all_theme_tokens(self):
        """FR-20260809-0702(8804): startup.py 必须引用 theme.py 的全部 STARTUP
        token（含深色分支 6 个）——防止某分支 token 未被消费后该分支配色失去
        主题跟随。"""
        import re

        source = (ROOT / "fukua_rpa" / "ui" / "startup.py").read_text(
            encoding="utf-8"
        )
        theme = (ROOT / "fukua_rpa" / "ui" / "theme.py").read_text(
            encoding="utf-8"
        )
        tokens = re.findall(r"STARTUP_[A-Z_]+(?=\s*=)", theme)
        self.assertGreaterEqual(len(tokens), 12)
        missing = [token for token in tokens if token not in source]
        self.assertEqual(
            missing,
            [],
            f"startup.py 未引用的 theme token（含深色分支）: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
