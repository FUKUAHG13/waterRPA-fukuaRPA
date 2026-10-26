"""Regression tests for S11: ``verify_source`` formal-release static gate.

``verify_source`` is the source gate run for a formal (stable) release. S11
hardens it so it (a) uses the project's *full* ruff configuration instead of a
minimal ``--select F,E9,E722`` subset, and (b) fails loudly with a
``RuntimeError`` when ``pyright`` is unavailable, rather than silently skipping
the strict type check.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import PurePosixPath
from unittest import mock

import scripts.verify_release as vr
from scripts.run_unittest_qt import _build_unittest_argv


class VerifySourceFormalGateTests(unittest.TestCase):
    @mock.patch("scripts.verify_release.verify_plugin_tests")
    @mock.patch("scripts.verify_release.run")
    @mock.patch("importlib.util.find_spec", return_value=None)
    def test_formal_discovery_preserves_test_package_relative_imports(
        self, mock_find_spec, mock_run, mock_plugin_tests
    ):
        """正式入口须按 tests 包发现，不能使包内相对导入失去父包。"""
        with self.assertRaises(RuntimeError):
            vr.verify_source()
        command = mock_run.call_args_list[0].args[0]
        self.assertIn("discover", command)
        self.assertIn("-t", command)
        self.assertEqual(command[command.index("-t") + 1], ".")

    def test_qt_bootstrap_preserves_discovery_subcommand(self):
        self.assertEqual(
            _build_unittest_argv(["discover", "-s", "tests", "-q"]),
            ["run_unittest_qt.py", "discover", "-s", "tests", "-q"],
        )

    def test_qt_bootstrap_adds_default_verbosity_after_discover(self):
        self.assertEqual(
            _build_unittest_argv(["discover", "-s", "tests"]),
            ["run_unittest_qt.py", "discover", "-v", "-s", "tests"],
        )

    def test_qt_bootstrap_adds_default_verbosity_for_module_runs(self):
        self.assertEqual(
            _build_unittest_argv(["tests.test_verify_release_s11"]),
            ["run_unittest_qt.py", "-v", "tests.test_verify_release_s11"],
        )

    @mock.patch("scripts.verify_release.verify_plugin_tests")
    @mock.patch("scripts.verify_release.run")
    @mock.patch("importlib.util.find_spec", return_value=None)
    def test_full_suite_uses_persistent_qapplication_bootstrap(
        self, mock_find_spec, mock_run, mock_plugin_tests
    ):
        with self.assertRaises(RuntimeError):
            vr.verify_source()

        first_command = mock_run.call_args_list[0].args[0]
        self.assertEqual(
            first_command,
            [
                vr.sys.executable,
                str(vr.QT_UNITTEST_BOOTSTRAP),
                "discover",
                "-s",
                "tests",
                "-t",
                ".",
                "-v",
            ],
        )

    @mock.patch("scripts.verify_release.verify_plugin_tests")
    @mock.patch("scripts.verify_release.run")
    @mock.patch("importlib.util.find_spec", return_value=None)
    def test_pyright_missing_raises_for_formal(
        self, mock_find_spec, mock_run, mock_plugin_tests
    ):
        # The formal gate must fail loudly (not silently skip) when pyright is
        # unavailable, so a CI without pyright cannot pass a formal release.
        with self.assertRaises(RuntimeError):
            vr.verify_source()
        # unittest-discover and the full-config ruff check must have run first
        # (proving the gate reached the pyright stage rather than short-circuiting).
        self.assertTrue(mock_run.called)
        mock_plugin_tests.assert_called_once()

    @mock.patch("scripts.verify_release.verify_plugin_tests")
    @mock.patch("scripts.verify_release.run")
    @mock.patch("importlib.util.find_spec", return_value=mock.MagicMock())
    def test_runs_full_ruff_config_not_subset(
        self, mock_find_spec, mock_run, mock_plugin_tests
    ):
        """主 ruff 调用必须使用完整项目配置，且覆盖全部自有代码目录。

        原始形态断言"任何 ruff 调用都不得带 --select"。后来插件子树按分级策略
        补了一条 ``--select F`` 的附加检查（见 verify_source 的说明），那条断言
        会被它误伤。**不能**因此简单删掉——它防的是"有人把某个目录挪进受限
        --select 里来躲避失败"。这里改成等价但更精确的两条：
        (a) 存在恰好一条不带 --select 的完整配置调用；
        (b) 它覆盖了全部自有代码目录。
        收窄型调用只允许**新增**覆盖，由下一条用例把关。
        """
        vr.verify_source()
        ruff_calls = [c for c in mock_run.call_args_list if "ruff" in c.args[0]]
        self.assertTrue(ruff_calls, "ruff check must be invoked")
        full_config = [c for c in ruff_calls if "--select" not in c.args[0]]
        self.assertEqual(
            len(full_config),
            1,
            "S11 要求恰好一条使用完整 ruff 配置的调用，"
            f"实际 {len(full_config)} 条：{[c.args[0] for c in ruff_calls]}",
        )
        targets = set(full_config[0].args[0])
        for required in ("fukuaRPA.py", "fukua_rpa", "tests", "scripts", "plugins/tools"):
            self.assertIn(
                required,
                targets,
                f"完整配置 ruff 未覆盖 {required}——该目录会脱离 lint 门禁"
                "（审查项 P1-2 就是 plugins/tools 这样漏掉的）",
            )
        mock_plugin_tests.assert_called_once()

    @mock.patch("scripts.verify_release.verify_plugin_tests")
    @mock.patch("scripts.verify_release.run")
    @mock.patch("importlib.util.find_spec", return_value=mock.MagicMock())
    def test_every_tracked_python_file_is_lint_gated(
        self, mock_find_spec, mock_run, mock_plugin_tests
    ):
        """每一个 git 跟踪的 .py 文件都必须落在某次 ruff 调用的目标之下。

        为什么用"git 跟踪"来派生，而不是写一份目录清单：

        * 清单是人维护的，漏了就静默失效——审查项 P1-2 正是这么发生的：
          plugins/tools 从来没被写进 ruff 目标，积压了 9 项违规（含一处真实
          的异常链丢失）而门禁一路绿。换成派生，新增源码目录会**默认变红**，
          必须有人显式决定"纳入门禁"或"说明为何排除"。
        * git 跟踪这条线天然把 vendored 第三方与本地实验目录排除在外
          （opencv_src/、opencv_build/、remotion-videos/、video-intro/ 都是
          未跟踪且已 gitignore 的），不需要再维护一份排除名单。

        注意这条只管**覆盖面**，不管规则强度；强度分级由前两条用例把关。
        """
        proc = subprocess.run(
            ["git", "ls-files", "*.py"],
            cwd=vr.ROOT,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            self.skipTest(f"git 不可用，无法枚举跟踪文件：{proc.stderr.strip()}")
        tracked = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        self.assertTrue(tracked, "未能列出任何被跟踪的 .py 文件，环境异常")

        vr.verify_source()
        targets: set[str] = set()
        for call in mock_run.call_args_list:
            argv = [str(part) for part in call.args[0]]
            if "ruff" not in argv:
                continue
            for part in argv[argv.index("check") + 1 :]:
                if part.startswith("-") or part == "F":
                    continue  # 跳过 --select 与它的规则实参
                targets.add(part.replace("\\", "/").rstrip("/"))

        uncovered = []
        for rel in tracked:
            path = PurePosixPath(rel)
            ancestors = {rel, *(str(parent) for parent in path.parents)}
            if not (ancestors & targets):
                uncovered.append(rel)
        # 用 bool() 而不是直接断言列表：脱管时动辄几十个文件，
        # 完整 repr 会把真正有用的提示淹掉。
        self.assertFalse(
            bool(uncovered),
            f"有 {len(uncovered)} 个被跟踪的 Python 文件不在任何 ruff 目标之下，"
            f"前 5 个：{uncovered[:5]}；请纳入门禁，或在此处说明排除理由",
        )
        mock_plugin_tests.assert_called_once()

    # ------------------------------------------------------------------
    # SCR01: --skip-source 连带跳过 verify_source 内的 UPX 检查（有意取舍）
    # ------------------------------------------------------------------
    @mock.patch("scripts.verify_release.verify_source")
    @mock.patch("scripts.verify_release.verify_dist")
    @mock.patch("scripts.verify_release.verify_onefile")
    def test_skip_source_skips_verify_source_including_upx_gate(
        self, mock_onefile, mock_dist, mock_source
    ):
        """``--skip-source`` 必须跳过 verify_source()（含其内的 UPX 禁用检查）。

        UPX 检查（fukuaRPA_onedir/onefile.spec 必须含 ``upx=False``）在
        verify_source() 内部（scripts/verify_release.py:317）。FR-20260811-D15-011
        （措辞更正）：普通构建路径**从未**跑过完整 verify_source——build_release 的
        verify_source_scope(formal_release=False) 只跑 run_impacted_tests，产物校验
        阶段再用 ``--skip-source`` 跳过 verify_source；UPX 检查仅在正式发布路径
        （不传 --skip-source）执行，普通构建依赖 spec 自带 ``upx=False`` 兜底。
        本测试锁定该语义：传 --skip-source 时 verify_source 不执行；UPX 检查
        随 verify_source 被跳过是设计行为而非缺陷（正式发布不传该参数）。
        """
        with mock.patch(
            "sys.argv",
            ["verify_release.py", "--skip-source", "--dist", "/tmp/dist"],
        ):
            vr.main()
        mock_source.assert_not_called()
        mock_dist.assert_called_once()
        mock_onefile.assert_not_called()

    @mock.patch("scripts.verify_release.verify_source")
    @mock.patch("scripts.verify_release.verify_dist")
    @mock.patch("scripts.verify_release.verify_onefile")
    def test_without_skip_source_runs_source_gate(
        self, mock_onefile, mock_dist, mock_source
    ):
        """未传 --skip-source 时 verify_source（含 UPX 检查）必须执行。"""
        with mock.patch(
            "sys.argv",
            ["verify_release.py", "--dist", "/tmp/dist"],
        ):
            vr.main()
        mock_source.assert_called_once()
        mock_dist.assert_called_once()


if __name__ == "__main__":
    unittest.main()
