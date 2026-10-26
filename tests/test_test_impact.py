"""Verify conservative changed-path test selection and native rebuild decisions."""

import subprocess
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest import mock

from scripts.run_impacted_tests import run_plugin_tests, run_short_benchmarks
from scripts.test_impact import (
    ALL_TEST_MODULES,
    BUILD_ARTIFACT_PATTERNS,
    ROOT,
    SUITES,
    ImpactRule,
    collect_changed_paths,
    discover_plugin_test_dirs,
    native_sources_changed,
    normalize_path,
    plan_impacted_tests,
    stale_rule_patterns,
)
from scripts.test_impact import _impact_base_ref as impact_base_ref


class NonAsciiGitPathTests(unittest.TestCase):
    def test_quoted_git_defaults_preserve_chinese_paths_and_stale_detection(self):
        """新克隆的 Git 默认配置不得把中文路径变成不存在的规则。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for arguments in (
                ("init", "-q"),
                ("config", "core.quotepath", "true"),
            ):
                subprocess.run(
                    ["git", *arguments], cwd=root, check=True,
                    capture_output=True,
                )
            relative = "docs/中文规范.md"
            path = root / relative
            path.parent.mkdir()
            path.write_text("测试规范\n", encoding="utf-8")
            rules = (
                ImpactRule((relative, "docs/已删除规范.md"), ("tooling",), "路径守护"),
            )
            with mock.patch("scripts.test_impact.ROOT", root), mock.patch(
                "scripts.test_impact.RULES", rules
            ):
                # 未跟踪的新文档及已跟踪文档都必须识别，缺失规则仍然报错。
                self.assertEqual(stale_rule_patterns(), ("docs/已删除规范.md",))
                subprocess.run(
                    ["git", "add", "--", relative], cwd=root, check=True,
                    capture_output=True,
                )
                self.assertEqual(stale_rule_patterns(), ("docs/已删除规范.md",))


class TestImpactSelectionTests(unittest.TestCase):
    def test_every_test_module_belongs_to_an_impact_suite(self):
        covered = set().union(*(set(modules) for modules in SUITES.values()))
        self.assertEqual(set(ALL_TEST_MODULES) - covered, set())

    def test_every_impact_suite_module_exists(self):
        for suite, modules in SUITES.items():
            for module in modules:
                with self.subTest(suite=suite, module=module):
                    self.assertIsNotNone(find_spec(module))

    def test_ui_change_selects_ui_without_forcing_every_test(self):
        plan = plan_impacted_tests(["fukua_rpa/ui/components.py"])
        self.assertIn("ui", plan.suites)
        self.assertIn("tests.test_ui_conventions", plan.modules)
        self.assertIn("tests.test_ui_modernization", plan.modules)
        self.assertIn("tests.test_ui_accessibility", plan.modules)
        self.assertFalse(plan.full_suite)

    def test_auto_clicker_guard_sources_select_its_stop_suite(self):
        """FR-20261011-007：test_auto_clicker_stop 的守护源（连点器窗口、独立工具
        线程、热键控制器）全部位于 fukua_rpa/ui/**，改动它们时定向门禁必须运行该
        套件，而不是只登记在 smoke。"""
        for path in (
            "fukua_rpa/ui/controllers/hotkey_controller.py",
            "fukua_rpa/ui/auto_clicker_tool.py",
            "fukua_rpa/ui/auto_clicker_mixins/hotkey.py",
        ):
            with self.subTest(path=path):
                plan = plan_impacted_tests([path])
                self.assertIn("tests.test_auto_clicker_stop", plan.modules)

    def test_workflow_search_change_selects_editor_contract_and_ui(self):
        plan = plan_impacted_tests(
            ["fukua_rpa/ui/controllers/workflow_search.py"]
        )
        self.assertEqual(set(plan.suites), {"ui", "workflow"})
        self.assertIn("tests.test_workflow_editor_split", plan.modules)
        self.assertIn("tests.test_current_step_search_and_overlays", plan.modules)
        self.assertFalse(plan.full_suite)

    def test_ui_convention_sources_select_the_static_ui_gate(self):
        for path in (
            "scripts/check_ui_conventions.py",
            "docs/UI开发规范.md",
        ):
            with self.subTest(path=path):
                plan = plan_impacted_tests([path])
                self.assertIn("ui", plan.suites)
                self.assertIn("tests.test_ui_conventions", plan.modules)
                self.assertFalse(plan.full_suite)

    def test_quality_gate_configuration_does_not_force_full_runtime_suite(self):
        for path in (".github/workflows/windows-ci.yml", ".pre-commit-config.yaml"):
            with self.subTest(path=path):
                plan = plan_impacted_tests([path])
                expected = {"packaging", "smoke"}
                if path == ".github/workflows/windows-ci.yml":
                    expected.add("review")
                self.assertEqual(set(plan.suites), expected)
                self.assertFalse(plan.full_suite)

    def test_native_service_change_selects_all_backend_contract_consumers(self):
        plan = plan_impacted_tests(["fukua_rpa/native_services.py"])
        self.assertTrue({"runner", "vision", "input"}.issubset(plan.suites))
        self.assertIn("tests.test_runner_isolation", plan.modules)
        self.assertIn("tests.test_core_regressions", plan.modules)
        self.assertIn("tests.test_execution_cancellation", plan.modules)
        self.assertFalse(plan.full_suite)

    def test_rule_patterns_point_at_real_repository_paths(self):
        # GUARD: 规则只做字符串匹配，源文件改名或移入子包后旧模式会静默失效——
        # 命中它的变更要么退化为全量测试，要么被更宽的规则吸收而漏掉应跑的套件。
        # 历史上 fukua_rpa/runner_*.py、recording.py、startup.py 三处即因此漂移，
        # 且当时的测试断言了同一批虚构路径，导致漂移无法被发现。
        self.assertEqual(stale_rule_patterns(), ())

    def test_stale_pattern_detection_is_not_vacuous(self):
        # 门禁本身必须能失败：注入一个不存在的路径与一个仍然有效的路径，
        # 只有前者应被报告，避免 stale_rule_patterns() 退化为恒真断言。
        fabricated = (
            ImpactRule(
                ("fukua_rpa/definitely_removed_module.py", "fukua_rpa/paths.py"),
                ("config",),
                "fabricated rule for gate self-check",
            ),
        )
        with mock.patch("scripts.test_impact.RULES", fabricated):
            self.assertEqual(
                stale_rule_patterns(),
                ("fukua_rpa/definitely_removed_module.py",),
            )

    def test_build_artifact_exemptions_are_untracked_artifacts(self):
        # GUARD: BUILD_ARTIFACT_PATTERNS 是存在性门禁的豁免名单，天然有沦为
        # 「逃生舱」的风险——把一个真实源文件的漂移塞进来就能让门禁闭嘴。
        # 豁免的正当性只有一个：该路径确实是不受版本控制的构建产物。这里对每一
        # 项断言它未被 git 跟踪；一旦有人拿被跟踪的源文件来豁免，本测试即失败。
        tracked = {
            normalize_path(path)
            for path in subprocess.run(
                ["git", "ls-files"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout.splitlines()
            if path
        }
        self.assertTrue(tracked, "git ls-files 返回为空，无法校验豁免名单")
        for pattern in BUILD_ARTIFACT_PATTERNS:
            self.assertNotIn(
                pattern,
                tracked,
                f"{pattern} 已被 git 跟踪，不应留在构建产物豁免名单中",
            )

    def test_build_artifact_exemption_is_load_bearing(self):
        # 豁免必须真的在起作用：去掉名单后，构建产物模式应当被判为 stale。
        # 若此断言失败，说明豁免已是死代码（例如该模式已从 RULES 移除），
        # 应当连同名单一起清理，而不是留着掩人耳目。
        artifact_rule = (
            ImpactRule(
                tuple(BUILD_ARTIFACT_PATTERNS),
                ("vision",),
                "build artifacts only",
            ),
        )
        with mock.patch("scripts.test_impact.RULES", artifact_rule):
            self.assertEqual(stale_rule_patterns(), ())
            with mock.patch(
                "scripts.test_impact.BUILD_ARTIFACT_PATTERNS", frozenset()
            ):
                self.assertEqual(
                    set(stale_rule_patterns()), set(BUILD_ARTIFACT_PATTERNS)
                )

    def test_runner_subpackage_changes_use_runner_suite(self):
        for name in (
            "backend",
            "ipc",
            "isolated",
            "job",
            "process",
            "protocol",
        ):
            path = f"fukua_rpa/runner/{name}.py"
            with self.subTest(path=path):
                plan = plan_impacted_tests([path])
                self.assertIn("runner", plan.suites)
                self.assertIn("tests.test_runner_isolation", plan.modules)
                self.assertFalse(plan.full_suite)

    def test_recording_sources_select_input_and_workflow(self):
        model_plan = plan_impacted_tests(["fukua_rpa/recording_model.py"])
        self.assertTrue({"input", "workflow"}.issubset(model_plan.suites))
        self.assertIn("tests.test_recording_model", model_plan.modules)
        self.assertFalse(model_plan.full_suite)

        # 控制器同时落在 ui/** 规则内，但 ui 套件不含录制语义回归，
        # 因此 input/workflow 必须由录制规则显式补上。
        controller_plan = plan_impacted_tests(
            ["fukua_rpa/ui/controllers/recording_controller.py"]
        )
        self.assertTrue(
            {"input", "workflow", "ui"}.issubset(controller_plan.suites)
        )
        self.assertIn("tests.test_recording_model", controller_plan.modules)
        self.assertFalse(controller_plan.full_suite)

    def test_startup_assembly_selects_smoke_and_packaging(self):
        # fukua_rpa/ui/startup.py 会被 ui/** 吸收；若不显式登记，
        # 守护启动装配的 test_startup_architecture 将不会运行。
        plan = plan_impacted_tests(["fukua_rpa/ui/startup.py"])
        self.assertTrue({"smoke", "packaging", "ui"}.issubset(plan.suites))
        self.assertIn("tests.test_startup_architecture", plan.modules)
        self.assertIn("tests.test_module_boundaries", plan.modules)
        self.assertFalse(plan.full_suite)

    def test_input_and_hook_adapters_have_known_impact_rules(self):
        input_plan = plan_impacted_tests(["fukua_rpa/input_backend.py"])
        self.assertIn("input", input_plan.suites)
        self.assertFalse(input_plan.full_suite)
        hook_plan = plan_impacted_tests(["fukua_rpa/hook_backend.py"])
        self.assertTrue({"input", "mapping"}.issubset(hook_plan.suites))
        self.assertFalse(hook_plan.full_suite)

    def test_usage_notice_catalog_is_a_known_ui_source(self):
        plan = plan_impacted_tests(["fukua_rpa/usage_notices.py"])
        self.assertIn("ui", plan.suites)
        self.assertIn("tests.test_ui_modernization", plan.modules)
        self.assertIn("tests.test_usage_notices", plan.modules)
        self.assertFalse(plan.full_suite)

    def test_editor_metadata_selects_organization_and_batch_regressions(self):
        plan = plan_impacted_tests(["fukua_rpa/workflow_metadata.py"])
        self.assertTrue({"workflow", "logging"}.issubset(plan.suites))
        self.assertIn("tests.test_workflow_metadata", plan.modules)
        self.assertIn("tests.test_bulk_task_settings", plan.modules)
        self.assertIn("tests.test_application_audit_log", plan.modules)
        self.assertFalse(plan.full_suite)

        batch_plan = plan_impacted_tests(["fukua_rpa/bulk_task_settings.py"])
        self.assertIn("workflow", batch_plan.suites)
        self.assertIn("tests.test_bulk_task_settings", batch_plan.modules)
        self.assertFalse(batch_plan.full_suite)

    def test_coordinate_mapper_change_selects_ui_and_mapping(self):
        plan = plan_impacted_tests(["fukua_rpa/coordinates.py"])
        self.assertTrue({"ui", "mapping"}.issubset(plan.suites))
        self.assertIn("tests.test_ui_accessibility", plan.modules)
        self.assertIn("tests.test_preview_and_mapping_models", plan.modules)
        self.assertFalse(plan.full_suite)

    def test_cross_area_task_model_selects_workflow_and_ui_consumers(self):
        plan = plan_impacted_tests(["fukua_rpa/task_model.py"])
        self.assertIn("workflow", plan.suites)
        self.assertIn("tests.test_engine_scheduler_boundaries", plan.modules)
        self.assertFalse(plan.full_suite)

    def test_unknown_executable_source_falls_back_to_full_suite(self):
        plan = plan_impacted_tests(["fukua_rpa/new_unmapped_core.py"])
        self.assertTrue(plan.full_suite)
        self.assertEqual(set(plan.suites), {
            "smoke", "ui", "workflow", "vision", "input", "mapping",
            "config", "logging", "security", "packaging", "performance",
            "runner", "plugins", "review",
        })
        self.assertIn("tests.test_ui_modernization", plan.modules)
        self.assertIn("tests.test_release_tooling", plan.modules)

    def test_changed_test_module_runs_directly(self):
        plan = plan_impacted_tests(["tests/test_scene_wake.py"])
        self.assertEqual(plan.modules, ("tests.test_scene_wake",))

    def test_plugin_source_selects_plugin_suite_and_dirs(self):
        # Review item #3: a plugin source change must map to the plugin suite
        # and only the affected plugin's test directory.
        plan = plan_impacted_tests(["plugins/ocr-local/src/ocr_local/action.py"])
        self.assertIn("plugins", plan.suites)
        self.assertIn("plugins/ocr-local/tests", plan.plugin_test_dirs)
        self.assertNotIn("plugins/screen-recorder/tests", plan.plugin_test_dirs)
        self.assertFalse(plan.full_suite)

    def test_plugin_test_file_maps_to_its_dir(self):
        plan = plan_impacted_tests(["plugins/office-docs/tests/test_office_docs.py"])
        self.assertIn("plugins/office-docs/tests", plan.plugin_test_dirs)

    def test_shared_plugin_toolchain_change_fans_out_to_every_plugin(self):
        # plugins/tools/common.py 被每个 plugins/<id>/tools/build_plugin.py 导入，
        # 只跑 plugins/tools/tests 会漏掉 4 个随仓库插件的清单/打包一致性回归。
        plan = plan_impacted_tests(["plugins/tools/common.py"])
        self.assertIn("plugins", plan.suites)
        expected = {normalize_path(str(d)) for d in discover_plugin_test_dirs()}
        self.assertEqual(set(plan.plugin_test_dirs), expected)
        self.assertIn("plugins/browser-automation/tests", plan.plugin_test_dirs)
        self.assertIn("plugins/screen-recorder/tests", plan.plugin_test_dirs)
        self.assertFalse(plan.full_suite)

    def test_host_plugin_contract_change_fans_out_to_every_plugin(self):
        # FR-006: tests/TEST_MATRIX.md:13 把 `fukua_rpa/plugin_*` 列为 plugins 组的
        # 常见变更路径；宿主契约（清单 schema、包校验、IPC 帧、注册表）变更时
        # plugins/*/tests 的清单同步断言正是契约漂移的直接探针，必须一并展开，
        # 而不是只跑 packaging/security。
        expected = {normalize_path(str(d)) for d in discover_plugin_test_dirs()}
        self.assertTrue(expected, "仓库应至少提供一个 plugins/*/tests 目录")
        for module in (
            "fukua_rpa/plugin_manifest.py",
            "fukua_rpa/plugin_registry.py",
            "fukua_rpa/plugin_package.py",
            "fukua_rpa/plugin_protocol.py",
        ):
            with self.subTest(module=module):
                plan = plan_impacted_tests([module])
                self.assertEqual(set(plan.plugin_test_dirs), expected)
                self.assertFalse(plan.full_suite)

    def test_unrelated_source_change_does_not_fan_out_to_plugins(self):
        # 反向：普通源码变更不得被本规则误扩到全部插件测试。
        plan = plan_impacted_tests(["fukua_rpa/ui/components.py"])
        self.assertEqual(plan.plugin_test_dirs, ())

    def test_strict_plugin_test_failure_is_fatal(self):
        error = subprocess.CalledProcessError(1, ["pytest"])
        with mock.patch(
            "scripts.run_impacted_tests.subprocess.run", side_effect=error
        ), self.assertRaisesRegex(SystemExit, "插件测试失败"):
            run_plugin_tests(["plugins/demo/tests"], {}, strict=True)

    def test_strict_missing_pytest_is_fatal(self):
        """FR-010：strict 模式下 pytest 缺失必须阻断，不得静默跳过。

        ARCHITECTURE #68 要求严格模式"失败**或缺失**均阻断"。修复前
        FileNotFoundError 分支在 strict 下仍 print 后 return，插件测试可静默
        零执行，而同函数上方 CalledProcessError / TimeoutExpired 的 strict
        分支均 raise SystemExit——三条路径语义不一致。
        """

        with mock.patch(
            "scripts.run_impacted_tests.subprocess.run",
            side_effect=FileNotFoundError("pytest"),
        ), self.assertRaisesRegex(SystemExit, "pytest"):
            run_plugin_tests(["plugins/demo/tests"], {}, strict=True)

    def test_non_strict_missing_pytest_is_not_fatal(self):
        """对照组：非 strict 模式仍保持"视为环境/可选依赖问题"的宽容语义。"""

        with mock.patch(
            "scripts.run_impacted_tests.subprocess.run",
            side_effect=FileNotFoundError("pytest"),
        ):
            run_plugin_tests(["plugins/demo/tests"], {}, strict=False)

    def test_documentation_only_change_selects_no_runtime_test(self):
        plan = plan_impacted_tests(["README.md", "docs/guide.md"])
        self.assertEqual(plan.modules, ())
        self.assertFalse(plan.full_suite)

    def test_native_rebuild_detection_is_narrow(self):
        self.assertTrue(native_sources_changed(["native_core/fukua_rpa_core.cpp"]))
        self.assertTrue(native_sources_changed(["fukua_rpa_core.dll"]))
        self.assertFalse(native_sources_changed(["fukua_rpa/engine/core.py"]))

    def test_clean_tree_still_includes_paths_from_current_commit(self):
        with mock.patch(
            "scripts.test_impact._git_lines",
            side_effect=([], [], ["fukua_rpa/engine/core.py"]),
        ), mock.patch("scripts.test_impact._impact_base_ref", return_value=None):
            self.assertEqual(collect_changed_paths(), ("fukua_rpa/engine/core.py",))

    def test_unpushed_commits_since_upstream_are_all_captured(self):
        # Without this, only the latest commit is diffed and earlier unpushed
        # commits that changed source would be missed by impacted-test selection.
        with mock.patch(
            "scripts.test_impact._impact_base_ref",
            return_value="origin/main",
        ), mock.patch(
            "scripts.test_impact._git_lines",
            side_effect=(
                [],  # diff --name-only HEAD (unstaged working tree)
                [],  # ls-files --others --exclude-standard
                ["fukua_rpa/engine/core.py", "fukua_rpa/vision.py"],  # base..HEAD range
            ),
        ):
            self.assertEqual(
                collect_changed_paths(),
                ("fukua_rpa/engine/core.py", "fukua_rpa/vision.py"),
            )

    def test_short_benchmarks_follow_selected_product_area(self):
        with mock.patch("scripts.run_impacted_tests.run") as run:
            run_short_benchmarks({"ui"}, {})
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(len(commands), 1)
            self.assertIn("scripts/benchmark_ui_interaction.py", commands[0])

        with mock.patch("scripts.run_impacted_tests.run") as run:
            run_short_benchmarks({"workflow"}, {})
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(len(commands), 1)
            self.assertIn("scripts/benchmark_core.py", commands[0])

        with mock.patch("scripts.run_impacted_tests.run") as run:
            run_short_benchmarks({"vision"}, {})
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(len(commands), 2)
            self.assertIn("scripts/benchmark_vision.py", commands[0])
            self.assertIn("scripts/benchmark_native_scheduler.py", commands[1])


class ImpactGuardCoverageTests(unittest.TestCase):
    """FR-20260930-017 防回归：守卫测试必须被其被测源码变更选中。

    独立审查 REVIEW-20260930-002 实测出 5 组缺口：这些源码变更不会选中对应的
    守卫测试，使定向门禁静默漏测（正式发布的全量门禁仍会覆盖，故不阻断发布）。
    本用例把该契约固化为可执行断言，防止同类漂移再次发生。
    """

    #: (被测源码路径, 该源码变更必须选中的守卫测试模块)
    GUARDED_PAIRS = (
        (
            "fukua_rpa/ui/input_tools.py",
            "tests.test_batch6_launch_and_lifecycle_guards",
        ),
        (
            "fukua_rpa/ui/main_window.py",
            "tests.test_batch6_launch_and_lifecycle_guards",
        ),
        (
            "fukua_rpa/ui/controllers/workflow_step_edit.py",
            "tests.test_batch6_launch_and_lifecycle_guards",
        ),
        (
            "fukua_rpa/ui/controllers/workflow_transfer_controller.py",
            "tests.test_workflow_transfer_controller",
        ),
        (
            "fukua_rpa/workflow_analysis.py",
            "tests.test_workflow_analysis_coverage",
        ),
        (
            "fukua_rpa/scheduled_task_service.py",
            "tests.test_scheduled_task_ui_regressions",
        ),
        (
            "fukua_rpa/scheduled_task_service.py",
            "tests.test_scheduled_tasks_lock_guards",
        ),
        (
            "fukua_rpa/scheduled_task_model.py",
            "tests.test_scheduled_task_ui_regressions",
        ),
        (
            "fukua_rpa/scheduled_task_model.py",
            "tests.test_scheduled_tasks_lock_guards",
        ),
        (
            "fukua_rpa/window_actions.py",
            "tests.test_launched_process_reap",
        ),
        (
            "fukua_rpa/plugin_actions.py",
            "tests.test_p6b_contract",
        ),
        (
            "fukua_rpa/plugin_protocol.py",
            "tests.test_p6b_contract",
        ),
        (
            "fukua_rpa/window_actions.py",
            "tests.test_p6b_contract",
        ),
        (
            "fukua_rpa/win32_input.py",
            "tests.test_execution_cancellation",
        ),
        (
            "scripts/benchmark_util.py",
            "tests.test_benchmark_util_affinity",
        ),
    )

    def test_guarded_tests_are_registered(self):
        """守卫测试必须已登记在 SUITES 中，否则下述断言失去意义。"""
        registered = {module for modules in SUITES.values() for module in modules}
        for _, module in self.GUARDED_PAIRS:
            with self.subTest(module=module):
                self.assertIn(module, registered)

    def test_guarded_tests_are_selected_by_their_source(self):
        """每组被测源码变更都必须选中其守卫测试。"""
        for source, module in self.GUARDED_PAIRS:
            with self.subTest(source=source, module=module):
                plan = plan_impacted_tests([source])
                self.assertIn(
                    module,
                    plan.modules,
                    f"{source} 变更未选中守卫测试 {module}（实际 suites={plan.suites}）",
                )


class TestImpactBaseRefTests(unittest.TestCase):
    """FR-20261006-003：``base == HEAD`` 时已提交源码不得从选择范围中消失。

    历史缺陷：``_impact_base_ref()`` 首选 ``@{upstream}``，而已推送分支的
    upstream *就是* HEAD，使 ``git diff <base> HEAD`` 恒为空，
    ``collect_changed_paths()`` 丢失全部已提交源码。旧守护测试只构造了
    ``base != HEAD`` 与 ``base is None`` 两种状态，从未覆盖这一真实可达情形。
    """

    @staticmethod
    def _resolve(*, upstream, origin_main, head):
        """构造 _git_lines 替身；未提供的 ref 视为解析失败（返回空）。"""
        table = {"HEAD": [head]}
        if upstream is not None:
            table["@{upstream}"] = [upstream]
        if origin_main is not None:
            table["origin/main"] = [origin_main]

        def fake_git_lines(*args):
            if args and args[0] == "rev-parse":
                if len(args) >= 3 and args[1] == "--verify":
                    return table.get(args[-1], [])
                if len(args) == 2 and args[1] == "HEAD":
                    return table["HEAD"]
            return []

        return fake_git_lines

    def test_upstream_equal_to_head_is_skipped(self):
        """upstream == HEAD（已推送分支）时不得把 HEAD 当作基线。"""
        head = "a" * 40
        with mock.patch(
            "scripts.test_impact._git_lines",
            self._resolve(upstream=head, origin_main="b" * 40, head=head),
        ):
            base = impact_base_ref()
        self.assertEqual(base, "b" * 40, "upstream 等于 HEAD 时必须继续尝试下一个基线")

    def test_upstream_ahead_of_head_is_preferred(self):
        """upstream 落后于 HEAD（有未推送提交）时仍优先采用，保留原意图。"""
        head = "c" * 40
        with mock.patch(
            "scripts.test_impact._git_lines",
            self._resolve(upstream="d" * 40, origin_main="b" * 40, head=head),
        ):
            self.assertEqual(impact_base_ref(), "d" * 40)

    def test_all_candidates_equal_head_returns_none(self):
        """全部候选都等于 HEAD 时返回 None，交由 diff-tree 回退，不返回空基线。"""
        head = "e" * 40
        with mock.patch(
            "scripts.test_impact._git_lines",
            self._resolve(upstream=head, origin_main=head, head=head),
        ):
            self.assertIsNone(impact_base_ref())

    def test_no_candidate_returns_none(self):
        """完全没有候选可用时返回 None。"""
        with mock.patch(
            "scripts.test_impact._git_lines",
            self._resolve(upstream=None, origin_main=None, head="f" * 40),
        ):
            self.assertIsNone(impact_base_ref())


if __name__ == "__main__":
    unittest.main()
