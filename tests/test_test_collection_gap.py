"""门禁：测试收集缺口不得复现。

背景
----
标准 unittest **只**收集 ``unittest.TestCase`` 子类，完全不收集顶层
``def test_*`` 函数；而 ``unittest.main()`` 在收集到 0 个用例时
``wasSuccessful()`` 仍返回 True、退出码为 0。两者叠加的后果是：一个只写顶层
测试函数的模块，在 unittest 门禁下既不报错也不执行，只输出一行
``NO TESTS RAN`` 就变绿。

本仓库曾长期处于这一状态：15 个模块、近百个测试函数从未被任何门禁执行过，
其中掩盖了 3 个真实失败（包括「插件严格契约校验静默跳过」这一已被判定为
"已修复"的历史缺陷的二次复发）。

本模块把修复后的机制钉死为可断言的事实：

1. 分类器 ``classify_test_module`` 的判定语义正确（含条件分支内定义、
   间接继承、局部作用域干扰等边界）。
2. 每个 ``tests/test_*.py`` 都至少被一个通道收集到，没有模块被路由丢弃。
3. 分类是**动态**的：新增顶层函数模块会被自动纳入 pytest 通道，
   不依赖任何人记得维护清单。
4. ``run_unittest_qt.py`` 对零收集硬失败——这是兜底闸，
   即使分类器判错也不会再有"什么都没跑"的绿灯。
5. 两个门禁脚本（run_impacted_tests / verify_release）都确实接上了 pytest 通道。
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from scripts import run_impacted_tests, verify_release
from scripts.test_impact import (
    ALL_TEST_MODULES,
    ROOT,
    classify_test_module,
    hybrid_test_modules,
    module_path,
    pytest_required_modules,
    split_modules_by_runner,
)

QT_UNITTEST_BOOTSTRAP = ROOT / "scripts" / "run_unittest_qt.py"


def _write(tmpdir: Path, name: str, body: str) -> Path:
    path = tmpdir / name
    path.write_text(body, encoding="utf-8")
    return path


def _function_body_source(func) -> str:
    """返回函数**去掉 docstring 后**的源码。

    用于"实现里不许出现 X"这类断言：docstring 里往往正需要提到 X 来解释为什么
    不能用它，直接对整段源码做字符串匹配会把说明文字当成违规。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    node = tree.body[0]
    statements = list(node.body)
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
        and isinstance(statements[0].value.value, str)
    ):
        statements = statements[1:]
    return "\n".join(ast.unparse(statement) for statement in statements)


class ClassifierSemanticsTests(unittest.TestCase):
    """分类器必须准确反映两个 runner 的真实收集能力。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_plain_testcase_module_is_unittest_only(self):
        path = _write(
            self.tmpdir,
            "test_a.py",
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n        pass\n",
        )
        channels = classify_test_module(path)
        self.assertTrue(channels.unittest)
        self.assertFalse(channels.pytest)

    def test_top_level_function_module_is_pytest_only(self):
        path = _write(self.tmpdir, "test_b.py", "def test_x():\n    pass\n")
        channels = classify_test_module(path)
        self.assertFalse(channels.unittest)
        self.assertTrue(channels.pytest)

    def test_pytest_style_test_class_is_pytest_only(self):
        """``class TestFoo:`` + ``test_*`` 方法：pytest 收得到，unittest 收不到。

        pytest 默认 ``python_classes = Test*``。这是"静默零执行"缺陷的**另一种
        写法**——只识别顶层 ``def test_*`` 会漏掉它，同类漏测就能继续在发布门禁
        上假绿。首版分类器正是漏了这条，由独立审查线程作为 P1 打回。
        """
        path = _write(
            self.tmpdir,
            "test_c1.py",
            "class TestFoo:\n    def test_x(self):\n        assert True\n",
        )
        channels = classify_test_module(path)
        self.assertIs(channels.pytest, True)
        self.assertIs(channels.unittest, False)

    def test_pytest_style_class_with_init_is_not_collectable(self):
        """带 ``__init__`` 的 ``class Test*`` pytest 会跳过，不能计入 pytest 通道。

        判成 ``(False, False)`` 是刻意的：它会落回 unittest 通道，被零用例熔断
        当场打成非零退出——响亮失败，而不是路由到 pytest 后拿一条 warning 假绿。
        """
        path = _write(
            self.tmpdir,
            "test_c2.py",
            "class TestFoo:\n"
            "    def __init__(self):\n        self.x = 1\n"
            "    def test_x(self):\n        assert True\n",
        )
        channels = classify_test_module(path)
        self.assertIs(channels.pytest, False)
        self.assertIs(channels.unittest, False)

    def test_testcase_named_with_test_prefix_stays_unittest_only(self):
        """``class TestFoo(unittest.TestCase)`` 名字也匹配 ``Test*``，但不得双算。

        双算会把它变成 hybrid，被两个通道各跑一遍——正是 ``hybrid_test_modules()``
        要禁掉的情形（隔离语义冲突，见该函数说明）。
        """
        path = _write(
            self.tmpdir,
            "test_c3.py",
            "import unittest\n"
            "class TestFoo(unittest.TestCase):\n"
            "    def test_x(self):\n        pass\n",
        )
        channels = classify_test_module(path)
        self.assertIs(channels.unittest, True)
        self.assertIs(channels.pytest, False)

    def test_pytest_style_class_without_test_method_is_not_counted(self):
        """``class TestHelper:`` 但没有 ``test_*`` 方法 → pytest 收集不到任何用例。"""
        path = _write(
            self.tmpdir,
            "test_c4.py",
            "class TestHelper:\n    def build(self):\n        return 1\n",
        )
        channels = classify_test_module(path)
        self.assertIs(channels.pytest, False)
        self.assertIs(channels.unittest, False)

    def test_string_filesystem_path_is_not_parsed_as_dotted_module(self):
        """字符串形式的**路径**必须按路径解析，不能被 ``split(".")`` 切碎。

        切碎后得到 ``tests/test_foo/py`` 这种垃圾路径，落进 ``except OSError``
        兜底，**静默**返回 ``(True, False)``：一个纯 pytest 模块就这样被判给
        unittest 通道。审查项 P2-7。
        """
        path = _write(self.tmpdir, "test_c5.py", "def test_x():\n    pass\n")
        channels = classify_test_module(str(path))
        self.assertIs(channels.pytest, True)
        self.assertIs(channels.unittest, False)

    def test_hybrid_module_needs_both_channels(self):
        path = _write(
            self.tmpdir,
            "test_c.py",
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n        pass\n"
            "def test_y():\n    pass\n",
        )
        channels = classify_test_module(path)
        self.assertTrue(channels.unittest)
        self.assertTrue(channels.pytest)

    def test_testcase_defined_inside_conditional_is_detected(self):
        """条件分支内定义的 TestCase 同样绑定模块级名字，必须识别。

        tests/test_json_safety_property.py 就是这种写法（TestCase 藏在
        ``if _HAS_HYPOTHESIS:`` 里）。只看 tree.body 会误判为"无用例"，
        进而把它错分到 pytest 通道或触发误报熔断。
        """
        path = _write(
            self.tmpdir,
            "test_d.py",
            "import unittest\n"
            "FLAG = True\n"
            "if FLAG:\n"
            "    class T(unittest.TestCase):\n"
            "        def test_x(self):\n            pass\n",
        )
        self.assertTrue(classify_test_module(path).unittest)

    def test_testcase_defined_inside_try_is_detected(self):
        path = _write(
            self.tmpdir,
            "test_e.py",
            "import unittest\n"
            "try:\n"
            "    class T(unittest.TestCase):\n"
            "        def test_x(self):\n            pass\n"
            "except Exception:\n"
            "    pass\n",
        )
        self.assertTrue(classify_test_module(path).unittest)

    def test_indirect_testcase_inheritance_is_detected(self):
        """``class Foo(_Base)`` 且 ``_Base(unittest.TestCase)`` 必须识别。"""
        path = _write(
            self.tmpdir,
            "test_f.py",
            "import unittest\n"
            "class _Base(unittest.TestCase):\n    pass\n"
            "class Foo(_Base):\n"
            "    def test_x(self):\n        pass\n",
        )
        self.assertTrue(classify_test_module(path).unittest)

    def test_nested_function_named_test_is_not_counted(self):
        """局部作用域里的 ``def test_*`` 两个 runner 都收不到，不能算作有用例。"""
        path = _write(
            self.tmpdir,
            "test_g.py",
            "def helper():\n"
            "    def test_inner():\n        pass\n"
            "    return test_inner\n",
        )
        channels = classify_test_module(path)
        self.assertFalse(channels.pytest)
        self.assertFalse(channels.unittest)

    def test_method_named_test_in_plain_class_is_not_counted(self):
        """普通类（非 TestCase）里的 test 方法不构成模块级用例。"""
        path = _write(
            self.tmpdir,
            "test_h.py",
            "class Plain:\n"
            "    def test_x(self):\n        pass\n",
        )
        channels = classify_test_module(path)
        self.assertFalse(channels.unittest)
        self.assertFalse(channels.pytest)

    def test_unreadable_module_falls_back_to_unittest_channel(self):
        """语法错误/文件缺失时保持既有行为，把问题留给运行期熔断喊出来。"""
        path = _write(self.tmpdir, "test_i.py", "def broken(:\n")
        channels = classify_test_module(path)
        self.assertTrue(channels.unittest)
        self.assertFalse(channels.pytest)


class RepositoryRoutingTests(unittest.TestCase):
    """全仓路由不变量。"""

    def test_every_test_module_is_routed_to_some_channel(self):
        """没有任何测试模块可以两个通道都不进——那等于被门禁悄悄丢弃。"""
        unittest_modules, pytest_modules = split_modules_by_runner(ALL_TEST_MODULES)
        routed = set(unittest_modules) | set(pytest_modules)
        missing = sorted(set(ALL_TEST_MODULES) - routed)
        self.assertFalse(
            missing, f"以下测试模块未被任何运行器通道覆盖：{missing}"
        )

    def test_no_module_is_invisible_to_both_runners(self):
        """全仓不存在 ``(False, False)`` 模块。

        上面那条断言看不见这种情况：``split_modules_by_runner`` 会把双不进的模块
        **兜底塞进 unittest 通道**，路由集合因此始终完整。但它们进去必然零收集，
        原因只有两种，都必须当场暴露：
        * 分类器漏了某种测试写法（P1-1 就是这么被发现的）；
        * 这个文件根本没有可执行用例（命名成 test_* 却是个 helper）。

        兜底的零用例熔断只在**实际被选中执行**时才会响；这条断言把它提前到
        每次跑本文件时静态检查一遍全仓。
        """
        orphans = []
        for module in ALL_TEST_MODULES:
            channels = classify_test_module(module)
            if not channels.unittest and not channels.pytest:
                orphans.append(module)
        self.assertFalse(
            orphans,
            "以下模块两个 runner 都收集不到（分类器漏写法，或该文件没有用例）："
            f"{sorted(orphans)}",
        )

    def test_no_module_mixes_testcase_and_pytest_style(self):
        """禁止同一模块混用 TestCase 与 pytest 写法（hybrid）。

        hybrid 会被两个通道各跑一遍，而两个通道的隔离语义是冲突的：unittest 通道
        逐模块起独立进程，pytest 通道单进程批量。详见
        ``test_impact.hybrid_test_modules`` 的说明。审查项 P2-4。
        """
        hybrid = hybrid_test_modules()
        self.assertEqual(
            hybrid,
            (),
            "以下模块同时含 TestCase 与 pytest 风格用例，会被双通道各跑一遍："
            f"{list(hybrid)}；请把顶层函数改写成 TestCase 方法，或拆到独立模块。",
        )

    def test_pytest_channel_matches_independent_ast_scan(self):
        """用另写一份的 AST 扫描，在全仓范围上差分校验分类结果。

        这里不复用 test_impact 的任何内部函数，而是照着 pytest 的两条默认收集规则
        （``python_functions = test*``、``python_classes = Test*``）另写一份判定，
        覆盖全部 158 个真实模块。

        定位要说清楚，免得高估它：这是一份**差分实现**，不是独立真值。真值基准在
        ``RealRunnerCollectionTests``——那里拿 pytest / unittest 的实际收集结果对
        分类器做校验，只是样本是少量探针。两者互补：这条管全仓广度（生产实现的
        作用域遍历、闭包等被改坏时会立刻变红），那边管判定正确性的地基。
        """

        def base_labels(node: ast.ClassDef) -> list[str]:
            labels = []
            for base in node.bases:
                if isinstance(base, ast.Attribute):
                    labels.append(base.attr)
                elif isinstance(base, ast.Name):
                    labels.append(base.id)
            return labels

        def module_scope(body) -> list[ast.AST]:
            """模块级语句，下探 if/try/for/while/with 的分支体，但不进函数/类体。"""
            out: list[ast.AST] = []
            stack = list(body)
            while stack:
                node = stack.pop()
                out.append(node)
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    continue
                for field in ("body", "orelse", "finalbody"):
                    stack.extend(getattr(node, field, None) or [])
                for handler in getattr(node, "handlers", None) or []:
                    stack.extend(handler.body)
            return out

        def testcase_names(classes: list[ast.ClassDef]) -> set[str]:
            """TestCase 子类名闭包——必须做传递闭包。

            本仓库大量使用 ``class _Base(unittest.TestCase)`` +
            ``class TestFoo(_Base)`` 的两级写法（test_missed_run_detector、
            test_scheduled_task_service_coverage 等）。只看直接基类会把这些
            Test* 类误当成 pytest 风格类，本条断言首次运行就是这么报警的。
            """
            names: set[str] = set()
            while True:
                grown = False
                for cls in classes:
                    if cls.name in names:
                        continue
                    if any(
                        label.endswith("TestCase") or label in names
                        for label in base_labels(cls)
                    ):
                        names.add(cls.name)
                        grown = True
                if not grown:
                    return names

        def scan(path: Path) -> bool:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            nodes = module_scope(tree.body)
            if any(
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test")
                for node in nodes
            ):
                return True
            classes = [node for node in nodes if isinstance(node, ast.ClassDef)]
            known_testcases = testcase_names(classes)
            for cls in classes:
                if not cls.name.startswith("Test") or cls.name in known_testcases:
                    continue
                methods = [
                    child
                    for child in cls.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                if any(method.name == "__init__" for method in methods):
                    continue  # pytest 跳过带 __init__ 的类
                if any(method.name.startswith("test") for method in methods):
                    return True
            return False

        expected = {m for m in ALL_TEST_MODULES if scan(module_path(m))}
        self.assertEqual(set(pytest_required_modules()), expected)

    def test_pytest_channel_is_derived_not_hardcoded(self):
        """分类必须是动态派生的，不能退化成人工维护的静态清单。

        做法：临时在 tests/ 下放一个只含顶层 test 函数的模块，
        断言它被自动纳入 pytest 通道。若哪天有人把分类改成硬编码列表，
        这条会立刻变红。
        """
        # ADV-20261012-911: 探针改写到临时目录。原先直接落在 tests/ 下，
        # 进程异常中断（崩溃/超时被杀）时 finally 不执行，残留文件会污染
        # 工作树并被后续真实收集门禁当作新增模块。
        with tempfile.TemporaryDirectory() as tmpdir:
            probe = Path(tmpdir) / "test_zzz_collection_probe_tmp.py"
            probe.write_text("def test_probe():\n    pass\n", encoding="utf-8")
            channels = classify_test_module(probe)
            self.assertTrue(channels.pytest)
            self.assertFalse(channels.unittest)
            _, pytest_modules = split_modules_by_runner(
                (*ALL_TEST_MODULES, str(probe))
            )
            self.assertIn(str(probe), pytest_modules)

    def test_no_pytest_only_module_is_sent_to_unittest(self):
        """顶层函数模块绝不能被送进 unittest 通道（送进去必然触发熔断）。"""
        unittest_modules, _ = split_modules_by_runner(ALL_TEST_MODULES)
        wrong = sorted(
            m for m in unittest_modules
            if classify_test_module(m).pytest and not classify_test_module(m).unittest
        )
        self.assertFalse(wrong, f"以下纯顶层函数模块被错误路由到 unittest：{wrong}")


class RealRunnerCollectionTests(unittest.TestCase):
    """把分类器判定对着两个 runner 的**真实收集结果**核一遍。

    前面那些分类器用例检验的是"我以为的规则"，这里检验"工具实际怎么做"。
    P1-1 那个漏判（``class TestFoo:`` 写法被判成两边都不进）之所以能通过首轮
    自测，正是因为当时只有前者：规则想错了，照着规则写的断言自然也是绿的。
    只有拿真实收集结果当基准，这类"整套自洽但整套是错的"才会被抓住。
    """

    # 文件名 -> (源码, 期望 classifier.unittest, 期望 classifier.pytest)
    PROBES = {
        "test_probe_func.py": ("def test_x():\n    assert True\n", False, True),
        "test_probe_class.py": (
            "class TestFoo:\n    def test_x(self):\n        assert True\n",
            False,
            True,
        ),
        "test_probe_class_init.py": (
            "class TestFoo:\n"
            "    def __init__(self):\n        self.x = 1\n"
            "    def test_x(self):\n        assert True\n",
            False,
            False,
        ),
        "test_probe_testcase.py": (
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n        pass\n",
            True,
            False,
        ),
        "test_probe_helper.py": (
            "class Plain:\n    def test_x(self):\n        pass\n",
            False,
            False,
        ),
    }

    def setUp(self):
        # pytest 是本仓库门禁的必需组件（pytest 通道明确不允许降级），
        # 缺失即为环境故障，必须响亮报错而不是 skip 掉这层基准校验。
        self.assertIsNotNone(
            importlib.util.find_spec("pytest"),
            "未安装 pytest：pytest 通道无法验证，门禁处于不可信状态",
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._purge_probe_imports)
        for name, (source, _, _) in self.PROBES.items():
            (self.tmpdir / name).write_text(source, encoding="utf-8")

    @staticmethod
    def _purge_probe_imports():
        """discover() 会真的 import 探针模块，跑完必须清干净，避免污染后续用例。"""
        for name in [n for n in sys.modules if n.startswith("test_probe_")]:
            del sys.modules[name]

    def _pytest_collected_files(self) -> set[str]:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                str(self.tmpdir),
            ],
            cwd=str(self.tmpdir),
            capture_output=True,
            # FR-20261005-007: 同族加固——本文件所有子进程输出统一以
            # errors="replace" 解码，避免任何字节序列导致 strict 解码崩溃。
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        collected: set[str] = set()
        for line in proc.stdout.splitlines():
            if "::" not in line:
                continue
            head = line.split("::", 1)[0].strip().replace("\\", "/")
            collected.add(head.rsplit("/", 1)[-1])
        self.assertTrue(
            collected,
            "pytest 未收集到任何探针用例，环境异常\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}",
        )
        return collected

    def _unittest_collected_files(self) -> set[str]:
        original_path = list(sys.path)
        try:
            suite = unittest.TestLoader().discover(
                str(self.tmpdir),
                pattern="test_probe_*.py",
                top_level_dir=str(self.tmpdir),
            )
        finally:
            sys.path[:] = original_path
        collected: set[str] = set()
        stack = [suite]
        while stack:
            item = stack.pop()
            if isinstance(item, unittest.TestSuite):
                stack.extend(item)
            else:
                collected.add(f"{type(item).__module__}.py")
        return collected

    def test_classifier_agrees_with_real_runner_collection(self):
        really_pytest = self._pytest_collected_files()
        really_unittest = self._unittest_collected_files()
        for name, (_, expect_unittest, expect_pytest) in self.PROBES.items():
            with self.subTest(probe=name):
                channels = classify_test_module(self.tmpdir / name)
                self.assertIs(channels.unittest, expect_unittest, "分类器 unittest 判定")
                self.assertIs(channels.pytest, expect_pytest, "分类器 pytest 判定")

                # 不变量 1：unittest 标志必须精确预测 discover 的结果。
                self.assertIs(
                    name in really_unittest,
                    expect_unittest,
                    f"{name}：unittest discover 真实收集与分类不符"
                    f"（真实={name in really_unittest}）",
                )
                # 不变量 2：判给 pytest 通道，pytest 就必须真的收得到用例，
                # 否则路由过去等于什么都没跑。
                if channels.pytest:
                    self.assertIn(
                        name, really_pytest, f"{name}：判给 pytest 却收集不到用例"
                    )
                # 不变量 3："两边都不进"的判定必须可信——确实两个 runner 都收不到，
                # 此时兜底的零用例熔断才是正确响应。若 pytest 其实收得到，
                # 这就是又一个 P1-1 式的静默漏测。
                if not channels.unittest and not channels.pytest:
                    self.assertNotIn(
                        name,
                        really_pytest,
                        f"{name}：被判成两边都不进，但 pytest 实际收得到 —— 静默漏测",
                    )

    def test_pytest_really_collects_testcase_too(self):
        """守住分类器语义的前提：pytest 其实**也**能收 TestCase。

        所以 ``pytest=False`` 读作"不需要 pytest 通道"，而不是"pytest 收不到"。
        哪天这个前提不成立了（例如换 runner、改配置），hybrid 双跑的隔离冲突
        分析和 ``_defines_pytest_test_class`` 里排除 TestCase 的理由都要重估。
        """
        self.assertIn("test_probe_testcase.py", self._pytest_collected_files())


class ZeroCollectionTripwireTests(unittest.TestCase):
    """零用例熔断：兜底闸，即使分类判错也不许"什么都没跑"变绿。"""

    def test_bootstrap_fails_when_nothing_collected(self):
        pytest_only = pytest_required_modules()
        self.assertTrue(pytest_only, "仓库中应存在顶层函数写法的模块作为样本")
        target = pytest_only[0]
        proc = subprocess.run(
            [sys.executable, str(QT_UNITTEST_BOOTSTRAP), "-v", target],
            cwd=ROOT,
            capture_output=True,
            # FR-20261005-007: 必须显式固定编码。子进程 stderr 是**混合编码流**——
            # Qt C++ 层（qt.qpa.window）的原生告警按 ANSI 代码页（本机 CP936）写入，
            # 与 Python 的 UTF-8 文本混入同一管道。`text=True` 而不指定 encoding 时，
            # 父进程按 locale PreferredEncoding（本机 GBK，CI 通常 UTF-8）以 strict
            # 模式解码，遇到无法解码的字节即抛 UnicodeDecodeError，使读取线程崩溃、
            # proc.stderr 变为 None，随后 assertIn(None) 抛 TypeError 并连带 teardown
            # error——表现为"偶发"失败，实则取决于调用形态（重定向必败、tty 必过）。
            # errors="replace" 保证不因任何字节序列崩溃，断言仍检查真实内容。
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        self.assertEqual(
            proc.returncode,
            1,
            "unittest 收集到 0 个用例时必须以非零退出码失败，"
            f"实际退出码 {proc.returncode}\n--- stdout ---\n{proc.stdout}"
            f"\n--- stderr ---\n{proc.stderr}",
        )
        self.assertIn("零用例熔断", proc.stderr)


class GateWiringTests(unittest.TestCase):
    """两个门禁脚本都必须真正接上 pytest 通道。"""

    def test_impacted_gate_routes_through_both_channels(self):
        src = inspect.getsource(run_impacted_tests.main)
        self.assertIn("split_modules_by_runner", src)
        self.assertIn("run_pytest_modules", src)

    def test_release_gate_runs_pytest_only_modules(self):
        self.assertIn(
            "verify_pytest_only_tests", inspect.getsource(verify_release.verify_source)
        )
        with mock.patch.object(verify_release, "run") as mock_run:
            verify_release.verify_pytest_only_tests({"DUMMY": "1"})
        mock_run.assert_called_once()
        command = [str(part) for part in mock_run.call_args.args[0]]
        self.assertIn("-m", command)
        self.assertIn("pytest", command)
        expected_files = {
            module_path(module).relative_to(ROOT).as_posix()
            for module in pytest_required_modules()
        }
        self.assertTrue(expected_files, "pytest 通道模块集合不应为空")
        self.assertTrue(
            expected_files.issubset(set(command)),
            f"发布门禁未覆盖全部 pytest 通道模块：缺 {sorted(expected_files - set(command))}",
        )

    def test_release_gate_goes_through_the_shared_run_helper(self):
        """verify_source 的每一步都必须经由 verify_release.run 执行。

        绕过它有两处实害：(1) 超时与非零退出语义与其余步骤漂移；
        (2) tests/test_verify_release_s11.py 里 ``patch("scripts.verify_release.run")``
        的用例会真的拉起 pytest 子进程，单元测试悄悄退化成集成测试。
        本改动初版就踩中过 (2)——一次全量回归里多跑了 3 遍完整 pytest 通道。
        """
        body = _function_body_source(verify_release.verify_pytest_only_tests)
        # 只看代码，不看 docstring：上面那段 INVARIANT 注释本身就提到了
        # run_pytest_modules（作为反例），按整段源码字符串匹配会自我误伤。
        self.assertNotIn("run_pytest_modules", body)
        self.assertNotIn("subprocess", body)
        self.assertIn("run(", body)

    def test_verify_source_stays_hermetic_when_run_is_patched(self):
        """行为级锁定：patch 掉 run 之后，verify_source 不得再触发任何真实子进程。"""
        with (
            mock.patch.object(verify_release, "run") as mock_run,
            mock.patch.object(verify_release, "verify_plugin_tests"),
            mock.patch("importlib.util.find_spec", return_value=None),
            mock.patch("subprocess.Popen") as mock_popen,
            mock.patch("subprocess.run") as mock_subprocess_run,
            # pyright 缺失 → 形式发布门禁按设计抛 RuntimeError。
            self.assertRaises(RuntimeError),
        ):
            verify_release.verify_source()
        self.assertTrue(mock_run.called, "verify_source 应至少经由 run 跑过一步")
        mock_popen.assert_not_called()
        mock_subprocess_run.assert_not_called()

    def test_pytest_channel_failure_is_not_downgraded(self):
        """pytest 通道不得像插件测试那样"环境容忍"降级——降级即回到静默漏测。"""
        src = inspect.getsource(run_impacted_tests.run_pytest_modules)
        self.assertIn("return False", src)
        self.assertNotIn("非致命", src)


if __name__ == "__main__":
    unittest.main()
