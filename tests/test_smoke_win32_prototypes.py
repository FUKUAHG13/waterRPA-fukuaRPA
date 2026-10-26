"""FR-SCOPE015D-001 / FR-SCOPE015D-002：冒烟工具的 Win32 原型声明与调用时序。

两条缺陷同根因：Win32 ctypes 原型声明分散在多个模块，且**调用时序不一致**，
导致同一进程内出现位宽不一致或清理路径异常覆盖返回值。

  * ``FR-SCOPE015D-001``：``native_smoke.run_native_smoke`` 在
    ``_configure_win32()`` 生效**之前**读取 ``foreground_before``
    （``GetForegroundWindow`` 未声明 ``restype`` 时 ctypes 默认 ``c_int``，
    HWND > 2^31 被截断），而 ``foreground_after`` 在配置之后取得 ⇒ 两侧位宽
    不一致，``foreground_unchanged`` 失真。
    **边界**：``smoke_became_foreground``（``:644``）两侧均在配置之后取得，
    当前已正确，**不得**削弱。

  * ``FR-SCOPE015D-002``：``uia_smoke.run_uia_smoke`` 的清理路径
    （``:295-296``）直接调用 ``PostMessageW`` 但未声明 ``argtypes``，
    ``root_hwnd >= 2^32`` 时在 ``finally`` 中抛 ``ctypes.ArgumentError``；
    ``finally`` 的异常会**覆盖**正常返回值。
    **边界**：``finally`` 中 ``process.terminate()`` 兜底必须保留。

两条断言都必须在**全新子进程**中执行：同进程内只要有任何模块先声明过对应原型，
缺陷就会被掩盖（实测 ``win32_api`` 一旦被导入，``PostMessageW.argtypes``
即被设置，红侧用例会假通过）。子进程只导入被测模块，与真实 ``--native-smoke``
/ ``--uia-smoke`` 入口的进程状态一致。

这些用例不创建真实窗口、不投递真实按键，全部通过替身与原型断言完成。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import unittest
from ctypes import wintypes
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fukua_rpa import native_smoke  # noqa: E402
from fukua_rpa.constants import NATIVE_CORE_RELEASE_VERSION  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# 大于 2^32 的句柄：c_int 无法表示，用于暴露未声明的原型。
WIDE_HWND = 0x123456789

_EXPECTED_POST_MESSAGE_ARGTYPES = [
    wintypes.HWND.__name__,
    wintypes.UINT.__name__,
    wintypes.WPARAM.__name__,
    wintypes.LPARAM.__name__,
]


def _run_in_fresh_process(script: str, *, timeout: int = 180) -> list[str]:
    """在只导入被测模块的子进程中执行脚本，返回其输出行。"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"子进程失败（exit={completed.returncode}）：\n"
            f"{completed.stdout}\n{completed.stderr}"
        )
    return completed.stdout.strip().splitlines()


@contextlib.contextmanager
def _window_that_never_opens():
    """替身：进入即失败，避免真实创建窗口。"""

    raise RuntimeError("window creation skipped in this test")
    yield  # pragma: no cover


class NativeSmokePrototypeOrderTests(unittest.TestCase):
    """FR-SCOPE015D-001：原型声明必须早于任何前台窗口读取。"""

    def test_configure_win32_runs_before_the_first_foreground_read(self):
        """``run_native_smoke`` 内 ``_configure_win32()`` 必须先于前台读取。"""

        order: list[str] = []

        def configure() -> None:
            order.append("configure")

        fake_user32 = mock.MagicMock()

        def foreground():
            order.append("read_foreground")
            return 0

        fake_user32.GetForegroundWindow.side_effect = foreground
        core = mock.MagicMock()
        core.available = True
        core.version = NATIVE_CORE_RELEASE_VERSION
        core.abi_snapshot.return_value = {}
        core.performance_stats.return_value = {}

        with mock.patch.object(native_smoke, "user32", fake_user32), mock.patch.object(
            native_smoke, "_configure_win32", configure
        ), mock.patch.object(
            native_smoke, "NativeVisionCore", return_value=core
        ), mock.patch.object(
            native_smoke, "configure_opencv_threads", lambda *a, **k: None
        ), mock.patch.object(
            native_smoke, "_generated_pattern_window", _window_that_never_opens
        ):
            report = native_smoke.run_native_smoke(base_dir=".")

        self.assertIn("configure", order)
        self.assertIn("read_foreground", order)
        self.assertEqual(
            order[0],
            "configure",
            f"原型声明必须先于前台读取，实际顺序: {order}",
        )
        self.assertFalse(report["ok"])

    def test_construction_failure_returns_a_report_instead_of_raising(self):
        """FR-20261008-101：构造期异常必须走 ``error`` 字段，不得逃逸为异常。

        ``core`` 只在 ``try`` 内绑定；此前 ``except`` 直接引用它，于是
        ``_configure_win32()`` 或 ``NativeVisionCore(...)`` 抛异常时逃逸出
        ``UnboundLocalError``，既违反 docstring 的 "Never propagates" 契约，也让
        调用方拿不到 ``--native-smoke-file`` 报告。
        """

        def boom(*args, **kwargs):
            raise RuntimeError("simulated native core construction failure")

        with mock.patch.object(
            native_smoke, "NativeVisionCore", boom
        ), mock.patch.object(native_smoke, "_configure_win32", lambda: None):
            report = native_smoke.run_native_smoke(base_dir=".")

        self.assertFalse(report["ok"])
        self.assertIn("simulated native core construction failure", report["error"])
        self.assertEqual(report["native_stats"], {})

    def test_stats_failure_in_except_path_still_returns_a_report(self):
        """FR-RISK-20261008-903：except 兜底不得重放 ``performance_stats`` 失败。

        core 已绑定且 ``available=True`` 时，except 分支会再次调用
        ``performance_stats()``；若原始异常恰好来自该调用本身（DLL 半故障的
        典型表现，try 内 line ~713 与 except 内重放同一方法），兜底就会抛出
        同一个新异常——报告不落盘，"Never propagates" 契约仍未闭合。
        本用例让 try 内提前失败（窗口上下文抛错），并令 ``performance_stats``
        自身抛错：修复前后者从 except 逃逸，修复后报告必须返回且
        ``native_stats`` 为空 dict。
        """
        import contextlib

        core = mock.MagicMock()
        core.available = True
        core.version = NATIVE_CORE_RELEASE_VERSION
        core.abi_snapshot.return_value = {}
        core.performance_stats.side_effect = RuntimeError("stats boom")

        @contextlib.contextmanager
        def exploding_window():
            raise RuntimeError("simulated capture failure")
            yield  # pragma: no cover

        with mock.patch.object(
            native_smoke, "NativeVisionCore", return_value=core
        ), mock.patch.object(
            native_smoke, "_configure_win32", lambda: None
        ), mock.patch.object(
            native_smoke, "configure_opencv_threads", lambda *a, **k: None
        ), mock.patch.object(
            native_smoke, "_generated_pattern_window", exploding_window
        ):
            report = native_smoke.run_native_smoke(base_dir=".")

        self.assertFalse(report["ok"])
        self.assertIn("simulated capture failure", report["error"])
        self.assertEqual(report["native_stats"], {})

    def test_foreground_restype_is_bound_even_on_the_early_failure_path(self):
        """核心不可用而提前返回时，前台原型也必须已被声明。"""

        script = "\n".join(
            [
                "import ctypes, tempfile, sys",
                "from ctypes import wintypes",
                "from fukua_rpa import native_smoke",
                "before = ctypes.windll.user32.GetForegroundWindow.restype",
                "with tempfile.TemporaryDirectory() as empty:",
                "    report = native_smoke.run_native_smoke(base_dir=empty)",
                "after = ctypes.windll.user32.GetForegroundWindow.restype",
                "print('before=%s' % before.__name__)",
                "print('after=%s' % after.__name__)",
                "print('ok=%r' % bool(report.get('ok')))",
                "print('win32_api_imported=%r' % ('fukua_rpa.win32_api' in sys.modules))",
            ]
        )
        lines = _run_in_fresh_process(script)
        before, after, ok, imported = lines
        self.assertEqual(imported, "win32_api_imported=False")
        self.assertEqual(ok, "ok=False")
        # 前置条件：未声明时 GetForegroundWindow.restype 是 32 位有符号整型
        # （实测 c_long），无法承载 >2^31 的 HWND。
        self.assertNotEqual(
            before,
            "before=c_void_p",
            "前置条件：未声明时不应已经是 64 位句柄类型",
        )
        self.assertEqual(
            after,
            "after=c_void_p",
            "run_native_smoke 必须先声明 64 位 restype 再读取前台窗口",
        )


_UIA_CLEANUP_SCRIPT = "\n".join(
    [
        "import ctypes, sys",
        "from unittest import mock",
        "from fukua_rpa import uia_smoke",
        "",
        "WIDE = 0x123456789",
        "ready = ('ready', WIDE, 0x2, (10, 10), 0x3, (20, 20))",
        "",
        "class _Queue:",
        "    def get(self, timeout=None): return ready",
        "    def close(self): pass",
        "    def join_thread(self): pass",
        "",
        "class _Process:",
        "    def __init__(self, *args, **kwargs): pass",
        "    def start(self): pass",
        "    def join(self, timeout=None): pass",
        "    def is_alive(self): return False",
        "    def terminate(self): pass",
        "",
        "class _Backend:",
        "    def __init__(self): self.closed = False",
        "    def probe(self, *args, **kwargs): raise RuntimeError('probe skipped')",
        "    def close(self): self.closed = True",
        "",
        "before = ctypes.windll.user32.PostMessageW.argtypes",
        "backend = _Backend()",
        "try:",
        "    with mock.patch.object(uia_smoke.multiprocessing, 'Queue', lambda: _Queue()), \\",
        "         mock.patch.object(uia_smoke.multiprocessing, 'Process', _Process), \\",
        "         mock.patch.object(uia_smoke, 'UIAutomationBackend', lambda: backend):",
        "        report = uia_smoke.run_uia_smoke()",
        "    outcome = 'returned ok=%r format=%r' % (report.get('ok'), report.get('format'))",
        "except BaseException as exc:",
        "    outcome = 'raised %s: %s' % (type(exc).__name__, exc)",
        "",
        "print('before=%r' % (before,))",
        "print('outcome=%s' % outcome)",
        "print('backend_closed=%r' % backend.closed)",
        "print('after=%r' % ([t.__name__ for t in (ctypes.windll.user32.PostMessageW.argtypes or ())],))",
        "print('restype=%r' % (ctypes.windll.user32.PostMessageW.restype.__name__,))",
        "print('win32_api_imported=%r' % ('fukua_rpa.win32_api' in sys.modules))",
    ]
)


class Win32SignatureFailureVisibilityTests(unittest.TestCase):
    """FR-20261008-402：单个函数签名配置失败不得升级为模块导入失败。

    冻结的 windowed 构建中 ``sys.stderr`` 为 ``None``；失败提示此前直接写 stderr，
    于是"处理失败"的代码自身抛 ``AttributeError`` 并逃出 ``_configure_function_signatures``，
    使一个被安全软件挂钩的函数变成整个 ``win32_api`` 模块导入失败。
    """

    def test_signature_failure_with_no_stderr_does_not_escape(self):
        from fukua_rpa import win32_api

        class _Hooked:
            def __setattr__(self, name, value):
                raise RuntimeError("hooked by security software")

        with mock.patch("sys.stderr", None):
            # 不得抛出：处理失败本身必须比被处理的失败更健壮。
            win32_api._safe_configure(_Hooked(), [], None, "Fake")

    def test_double_click_helper_returns_false_when_stderr_is_missing(self):
        from fukua_rpa import win32_api

        with mock.patch("sys.stderr", None), mock.patch.object(
            win32_api.user32,
            "WindowFromPoint",
            side_effect=RuntimeError("boom"),
        ):
            self.assertIs(
                win32_api.post_double_click_to_window_at_point(1, 2), False
            )


class UiaSmokeCleanupPrototypeTests(unittest.TestCase):
    """FR-SCOPE015D-002：清理路径必须能承受 ≥2^32 的句柄。"""

    def test_cleanup_path_tolerates_a_wide_root_hwnd_in_a_fresh_process(self):
        """``root_hwnd >= 2^32`` 时 ``run_uia_smoke`` 仍须返回报告而非抛异常。"""

        lines = _run_in_fresh_process(_UIA_CLEANUP_SCRIPT)
        self.assertEqual(
            lines,
            [
                # 前置条件：该进程内没有任何模块先声明过 PostMessageW。
                "before=None",
                # 修复前：finally 中 PostMessageW 抛 ArgumentError 并覆盖返回值。
                "outcome=returned ok=False format='fukuaRPA_uia_smoke'",
                # 清理路径未被异常打断，terminate 兜底与 close 均保留。
                "backend_closed=True",
                f"after={_EXPECTED_POST_MESSAGE_ARGTYPES!r}",
                f"restype={wintypes.BOOL.__name__!r}",
                "win32_api_imported=False",
            ],
        )


if __name__ == "__main__":
    unittest.main()
