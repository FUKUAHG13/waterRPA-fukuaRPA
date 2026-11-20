"""Qt-aware unittest bootstrap.

The CI/build gate runs many UI test modules inside a *single* ``unittest``
process. Each UI module guards its setup with
``QApplication.instance() or QApplication([])`` in ``setUpClass``, which is fine
as long as a live application always exists. In practice, batch ordering plus
the interpreter's cyclic-GC of otherwise-unreferenced application objects can
leave ``QApplication.instance()`` returning ``None`` for a later module. The Qt
C++ layer then aborts the whole process (``QWidget: Cannot create a QWidget
without QApplication`` -> STATUS_STACK_BUFFER_OVERRUN) the moment that module
constructs a widget, killing the entire batch and the build gate with it.

This bootstrap creates one persistent ``QApplication`` *before* any test module
is imported and holds a strong reference to it for the process lifetime, so
``QApplication.instance()`` is never ``None`` during the run. UI modules
gracefully reuse the shared instance via their ``instance() or []`` guard.
"""

from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication

# Ensure the repository root (parent of scripts/) is importable so the
# ``tests.*`` modules passed on the command line can be loaded.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _pin_stdio_to_utf8() -> None:
    """固定本引导脚本自身输出为 UTF-8（TS-20261027-01）。

    门禁把本脚本的输出当子进程管道读取。不固定编码时 Python 按调用方 locale 的
    首选编码写出——本机 CP936/GBK、CI 常是 UTF-8——读取侧却按 UTF-8 解码，于是
    「零用例熔断」这类中文提示变成解不开的字节：父进程解码线程抛
    UnicodeDecodeError、stdout/stderr 退化成 None，门禁以一次与代码无关的
    TypeError 失败，看起来像偶发。写死 UTF-8 才是根治；
    ``errors="backslashreplace"`` 保证连无法编码的字符也只转义、不抛异常。

    Qt C++ 层仍会按 ANSI 代码页往同一个 fd 写原生告警，因此读取侧的
    ``errors="replace"`` 兜底必须保留，两侧各管一头。
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        # 被替换成非 TextIOWrapper（测试打桩、重定向到自定义流）时保持原样。
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="backslashreplace")


_pin_stdio_to_utf8()


def _build_unittest_argv(arguments: list[str]) -> list[str]:
    argv = list(arguments)
    if not any(arg in ("-v", "-q", "--verbose", "--quiet") for arg in argv):
        argv = (
            ["discover", "-v", *argv[1:]]
            if argv and argv[0] == "discover"
            else ["-v", *argv]
        )
    # unittest.main expects argv[0] to be a program label. Keeping it separate
    # is essential for the ``discover -s tests`` subcommand; otherwise
    # ``discover`` is consumed as argv[0] and ``-s`` is parsed by the wrong
    # command parser.
    return ["run_unittest_qt.py", *argv]


def main() -> int:
    app = QApplication.instance() or QApplication([])
    # Strong reference so the application outlives every test module and is
    # never collected/replaced mid-batch.
    _keep_alive = app

    # module=None mirrors ``python -m unittest``: argv entries are treated as
    # importable module/TestClass.method paths rather than attributes of
    # __main__ (the bootstrap itself).
    program = unittest.main(
        argv=_build_unittest_argv(list(sys.argv[1:])), module=None, exit=False
    )
    result = program.result
    # 零用例熔断。unittest 收集到 0 个用例时 wasSuccessful() 仍返回 True，
    # 于是「一个测试都没跑」会以退出码 0 通过门禁——本仓库曾因此让 15 个模块、
    # 近百个测试函数长期零执行（它们用顶层 def test_* 写法，标准 unittest 只
    # 收集 TestCase 子类，根本看不见）。把"没跑到任何东西"直接判为失败，
    # 是让这类静默漏测无处藏身的最后一道闸。
    #
    # 分流本身在 scripts/test_impact.py::split_modules_by_runner 完成：顶层函
    # 数模块会走 pytest 通道，不会送到这里。因此这里一旦触发，说明分流判断与
    # 模块实际写法脱节，或该模块的用例被某个条件分支整体跳过——两种都必须让人
    # 看见，而不是继续变绿。
    if result.testsRun == 0:
        target = " ".join(sys.argv[1:]) or "(discover)"
        print(
            f"\n[FAIL] 零用例熔断：{target} 一个测试都没有被收集到。\n"
            "  * 若该模块使用顶层 def test_* 写法，应由 pytest 通道执行——"
            "检查 scripts/test_impact.py::classify_test_module 的判定；\n"
            "  * 若该模块的 TestCase 定义在条件分支内（如缺少可选依赖），"
            "请补一个始终可收集的 skip 占位用例，让跳过是可见的。\n"
            "  退出码置为 1：不允许「什么都没跑」通过门禁。",
            file=sys.stderr,
        )
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
