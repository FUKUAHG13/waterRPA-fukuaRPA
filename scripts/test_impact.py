"""Conservative changed-path to unittest-suite selection for local builds."""

from __future__ import annotations

import ast
import fnmatch
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from scripts.private_release_inputs import is_private_validation_path

ROOT = Path(__file__).resolve().parents[1]

# MAINTENANCE: SUITES 和 RULES 为人工维护的静态映射。
# 新增测试文件时须手动登记到 SUITES；新增源文件时须检查 RULES 是否需要补充触发规则。
# 未匹配任何规则的变更路径会回退到全量测试（安全但慢）。
# 建议定期（每月）抽查高频改动文件的测试归属是否正确。
#
# INVARIANT: RULES 中的每个模式都必须在仓库中命中至少一个真实路径。
# 规则只按字符串匹配变更路径，不接触文件系统；源文件被改名或移入子包后，旧模式会
# 静默失效——命中它的变更要么退化为全量测试（慢），要么被更宽的规则吸收而漏掉本
# 该运行的套件（漏测）。stale_rule_patterns() 把该不变量变成可断言的事实，并由
# tests/test_test_impact.py 门禁；新增或重命名源文件时必须同步更新此处模式。
SUITES = {
    "review": (
        "tests.test_review_workflow",
    ),
    "smoke": (
        "tests.test_module_boundaries",
        "tests.test_startup_architecture",
        # ADR 0040：步骤设置对话框按需导入、纯摘要与界面模块解耦。
        "tests.test_startup_lazy_step_dialog",
        # ADR 0041：运行期模块（校验层、浮层）按需导入。
        "tests.test_startup_lazy_runtime_modules",
        # FR-20261023-STARTUP-B2-001：识别完成后先清理，再按命中结果加载浮层。
        "tests.test_recognition_completion_import_failure",
        "tests.test_startup_copy_safety",
        "tests.test_dpi_awareness",
        "tests.test_test_impact",
        # 与 test_test_impact 同属"测试选择/门禁工具链"的元测试：前者管路径→套件
        # 的映射是否失效，后者管模块→runner 的分流是否失效。两者必须同进同出，
        # 否则改了本文件却只跑其中一个，另一个的门禁形同虚设。
        "tests.test_test_collection_gap",
        "tests.test_crash_guard",
        "tests.test_crash_guard_coverage",
        "tests.test_runtime_diagnostics_checks",
        "tests.test_scheduled_task_service",
        "tests.test_auto_clicker_stop",
        "tests.test_startup_entry_fr101",
    ),
    "ui": (
        "tests.test_ui_conventions",
        # 按键映射迁移（2026-10-19）：映射区迁入工具窗口，守护源为 ui/**.。
        "tests.test_key_mapping_tool",
        # 全局映射/默认设置（2026-10-21）：守护 settings_defaults_actions.py 与
        # settings_sections/mapping.py，必须随 ui 套件可达（交叉复核 C 第 4 项）。
        "tests.test_global_mappings_and_defaults",
        # R14 审查守护（29.0 脱敏、缓存空清单、关窗运行态、导入映射、序号宽度）。
        "tests.test_review_r14_guards",
        "tests.test_ui_modernization",
        "tests.test_ui_accessibility",
        "tests.test_ui_performance",
        "tests.test_drag_insert_preview",
        "tests.test_bulk_task_settings",
        "tests.test_workflow_metadata",
        "tests.test_usage_notices",
        "tests.test_dodge_onboarding",
        "tests.test_scene_wake_ui",
        "tests.test_task_settings_controllers",
        "tests.test_image_crop_dialog",
        "tests.test_main_window_extract",
        "tests.test_main_window_lifecycle_callbacks",
        "tests.test_overlay_link_p1_1",
        "tests.test_p4c_ui_builders_split",
        "tests.test_p5b1_host_view",
        "tests.test_p5b2_region_window_manager",
        "tests.test_p5c_controller_testability",
        "tests.test_gui_e2e",
        "tests.test_geometry_snapshot",
        "tests.test_scheduled_tasks_checkbox",
        "tests.test_scheduled_task_ui_regressions",
        "tests.test_runtime_editing_lock_guards",
        "tests.test_scheduled_tasks_lock_guards",
        "tests.test_task_row_type_switch_preserves_params",
        # FR-20261006-004（A1）：旧方案的 28.0 连点器步骤必须以**禁用占位项**
        # 往返，且不得被静默改写成下拉第一项。实测 task_row_binding.py 只选中
        # ("ui",)，故登记在本组。
        "tests.test_auto_clicker_legacy_placeholder",
        # FR-20261006-007：拖拽的「移动随机/按住随机」必须真的能被用户看到。该行
        # 曾挂在 coordinate_target_form 上，而拖拽会隐藏它所属的坐标目标折叠区；
        # 行自身 isHidden() 为假但祖先被隐藏，isVisible() 才是正确判据。实测
        # task_settings_sections/execution.py 与 task_settings_dialog.py 均只选中
        # ("ui",)，故登记在本组。
        "tests.test_drag_jitter_reachability",
        # FR-20261006-008：批量「统一未改字段」只覆盖当前可见的字段。实测
        # task_settings_binding.py 与 controllers/workflow_group.py 均只选中
        # ("ui",)，故登记在本组。
        "tests.test_bulk_visibility_scope",
        "tests.test_auto_clicker_behavior",
        # FR-20261011-007：test_auto_clicker_stop 的守护源（连点器窗口、独立工具
        # 线程、热键控制器、输入所有权）全部位于 fukua_rpa/ui/**，此前只登记在
        # smoke，改动这些源文件时定向门禁不会运行它。
        "tests.test_auto_clicker_stop",
        "tests.test_profile_hotkeys",
        "tests.test_mouse_trail",
        "tests.test_settings_binding_tasks",
        "tests.test_settings_dirty_mark_fixes",
        "tests.test_current_step_search_and_overlays",
        # start_task_from_step_once 的启动守卫。归 ui 而非 workflow/runner：
        # 被测文件 fukua_rpa/ui/controllers/execution_controller.py 经 plan_impacted_tests
        # 实测只命中 ("ui",) 一条规则，登记在 workflow 会使其永不触发。
        "tests.test_start_task_from_step_once_guards",
        # INP-FR-001：低级钩子安装失败必须可见（映射钩子/取点器路径均归 ui 组）。
        "tests.test_input_hook_failure_visibility",
        # FR-20261005-001：循环风险确认的去重键作用域（多方案快捷启动 vs 手动运行）。
        "tests.test_risk_ack_scope",
        # INVARIANT: 取点器正常关闭的线程守护须由真实 UI 源变更触发，不能只归 runner。
        "tests.test_batch6_launch_and_lifecycle_guards",
        # C-1：设置对话框延迟构建后的两段式守护。不再依赖「先 ensure 对话框」的
        # 统一夹具，专门覆盖 capture/apply 的宿主态、收养与构建失败路径。
        "tests.test_c1_settings_two_phase",
    ),
    "workflow": (
        "tests.test_advanced_image_actions",
        # FR-20261005-005：拖拽起/终点图片的多倍率缓存键守护（识别正确性）。
        "tests.test_drag_image_cache_key_scale",
        "tests.test_engine_scheduler_boundaries",
        "tests.test_step_runtime",
        "tests.test_core_regressions",
        "tests.test_execution_cancellation",
        "tests.test_stability_foundation",
        "tests.test_workflow_document",
        "tests.test_bulk_task_settings",
        "tests.test_workflow_metadata",
        "tests.test_debug_session",
        "tests.test_expressions",
        "tests.test_preview_and_mapping_models",
        "tests.test_scheduled_task_store",
        "tests.test_scheduled_task_logs",
        "tests.test_scheduled_task_model",
        "tests.test_scheduled_task_service",
        "tests.test_missed_run_detector",
        "tests.test_invariant_terminal_outcome",
        "tests.test_invariant_hidden_step",
        "tests.test_invariant_batch_mixed",
        "tests.test_invariant_multi_image_whitelist",
        "tests.test_engine_actions",
        "tests.test_new_features_guard",
        "tests.test_engine_actions_split",
        "tests.test_engine_split",
        "tests.test_workflow_editor_split",
        "tests.test_p5b3_execution_decouple",
        "tests.test_loop_scheduler_cap_r1",
        "tests.test_loop_scheduler_next_interval",
        "tests.test_action_handler_none",
        "tests.test_e2e_data_integrity",
        "tests.test_launched_process_reap",
        "tests.test_defect_fixes_v1_0_57",
        "tests.test_validation_coverage",
        "tests.test_scheduled_task_service_coverage",
        "tests.test_actions_flow_coverage",
        "tests.test_engine_conditions_coverage",
        "tests.test_actions_shared_coverage",
        "tests.test_engine_coordinates_coverage",
        "tests.test_worker_coverage",
        "tests.test_scheduled_task_store_coverage",
        "tests.test_runtime_state_stop_flag",
        "tests.test_profile_switch_undo_fr901",
        "tests.test_missed_ask_prompt_frb0104",
        "tests.test_current_step_search_and_overlays",
    ),
    "runner": (
        "tests.test_runner_isolation",
        # FR-20261005-006：已自报 finished 终局的子进程不得被归类 forced_stop。
        "tests.test_runner_finished_outcome",
        "tests.test_execution_cancellation",
        "tests.test_engine_scheduler_boundaries",
        "tests.test_stability_foundation",
        "tests.test_p6a_lock",
        "tests.test_p6b_contract",
        "tests.test_watchdog_drain_p1_3",
        "tests.test_scheduled_coordinator_affinity",
        "tests.test_runner_process_coverage",
        "tests.test_runner_outcome_normalization",
        # 第 6 批（启动互斥与运行期一致性）守护。实测 plan_impacted_tests 对
        # ``runner/isolated.py`` 与 ``runner/backend.py`` 均选中 ("runner", ...)，
        # 故登记在本组，改动这两个源文件时该守护必然被触发。
        "tests.test_batch6_launch_and_lifecycle_guards",
    ),
    "vision": (
        "tests.test_advanced_image_actions",
        "tests.test_core_regressions",
        "tests.test_engine_vision",
        "tests.test_opencv_runtime",
        "tests.test_scale_memory",
        "tests.test_scene_wake",
        "tests.test_scene_wake_engine",
        "tests.test_vision",
        "tests.test_engine_vision_split",
        "tests.test_p5a_scan_region_sink",
        "tests.test_precache_abort_p2_1",
        "tests.test_match_native_contract",
        "tests.test_coordinates_inverse",
        "tests.test_vision_shared_coverage",
        "tests.test_coordinates_coverage",
        "tests.test_vision_scene_coverage",
        "tests.test_vision_preload_coverage",
        "tests.test_vision_scene_physical",
        # FR-20261005-005（REG 复验阻断项）：拖拽起/终点图片的多倍率缓存键守护。
        # 必须登记进本组——`vision/multi_image.py` 与 `vision/preload.py` 的改动
        # 实际只选中 ("performance", "vision")（见 plan_impacted_tests 实测），
        # 若只登记在 workflow 组，则这两个关键源文件改动时该P1 回归守护不会被
        # 触发，"测试存在但在关键路径不可达"。TEST_MATRIX.md 本行自述的守护范围
        # 正是这两个 vision 源文件 + actions/mouse.py，故此处按组语义登记。
        "tests.test_drag_image_cache_key_scale",
        # FR-20261006-009：``wrpa_find_template_ex`` 实参与 argtypes 位数必须一致
        # （ex=16 / ex2=17 / ex3=18）。实测 plan_impacted_tests 对
        # ``vision/core.py`` 选中 ("performance", "vision")，故登记在本组——
        # ABI 不一致会让 ctypes 抛 TypeError 并逃逸「回退 OpenCV」契约。
        "tests.test_vision_native_abi_arity",
    ),
    "input": (
        "tests.test_advanced_image_actions",
        "tests.test_recording_model",
        # 录制长按采集契约（2026-10-19 批次 1）：守护源为录制钩子与录制模型，
        # 与 recording_model 同组登记。
        "tests.test_recording_hold_capture",
        # 录制输入时序（2026-10-20 批次 2）：时间线转换、回放与校验。
        "tests.test_input_timeline",
        # 全局映射与默认设置模板（2026-10-21）：映射不随方案走、新建沿用模板、
        # 默认映射热键不越界。
        "tests.test_global_mappings_and_defaults",
        "tests.test_hook_backend",
        "tests.test_execution_cancellation",
        "tests.test_credentials_and_actions",
        "tests.test_ui_modernization",
        "tests.test_engine_actions",
        "tests.test_make_mouse_lparam",
        "tests.test_auto_clicker_behavior",
        "tests.test_mouse_trail",
        "tests.test_win32_api_coverage",
        "tests.test_smoke_win32_prototypes",
        "tests.test_mouse_double_click_fr401",
    ),
    "mapping": (
        "tests.test_preview_and_mapping_models",
        "tests.test_uia_backend",
        "tests.test_window_diagnostics",
        "tests.test_ui_modernization",
    ),
    "config": (
        "tests.test_paths",
        "tests.test_profile_paths",
        "tests.test_profile_package",
        "tests.test_profile_package_coverage",
        "tests.test_workflow_transfer_controller",
        "tests.test_config_store_coverage",
        "tests.test_workflow_analysis_coverage",
        "tests.test_profile_validation",
        "tests.test_json_safety",
        "tests.test_json_safety_property",
        "tests.test_bounded_runtime_inputs",
        "tests.test_stability_foundation",
        "tests.test_credentials_and_actions",
        "tests.test_new_features_guard",
        "tests.test_max_runtime_p1_2",
        "tests.test_e2e_data_integrity",
    ),
    "logging": (
        "tests.test_application_audit_log",
        "tests.test_log_policy",
        "tests.test_log_telemetry",
        "tests.test_logging_levels",
        "tests.test_logging_service",
        "tests.test_scheduled_task_logs",
        "tests.test_runtime_trace",
        "tests.test_performance_metrics",
        # A5 日志类别迁移的反向验证：关闭普通类别后，真实错误/急停/严重警告
        # 一条都不能丢。实测 ``logging_service.py`` 选中 ("logging", ...)，
        # ``engine/logging.py`` 选中 ("logging", "workflow")，故登记在本组。
        "tests.test_batch8_log_category_reverse",
        # 第 7 批守护：坐标方向别名不得按前缀猜测（``task_model.py`` /
        # ``validation.py``），以及 ``diagnostics.py`` 的 UIAutomation DLL 不得
        # 硬编码旧标签。实测这两组源文件都选中 ("logging", ...)：
        # task_model.py / validation.py -> ("logging", "workflow")，
        # diagnostics.py -> ("logging", "performance", "security")，
        # 交集即本组，故登记在此才能在两类改动下都被触发。
        "tests.test_batch7_doc_alignment_guards",
        # FR-20261008-906: 运行自检报告结构的守护（诊断检查项名称唯一、
        # 原生检查五项完整）。其被守护源码 ``fukua_rpa/diagnostics.py`` 实测
        # 选中 ("logging", "performance", "security")——仅登记 smoke 组时，
        # 改 diagnostics.py 永远触发不了该守护（探针：
        # plan_impacted_tests(["fukua_rpa/diagnostics.py"]) 不含它）。
        # 按 TEST_MATRIX「一个文件可以同时选中多个测试组」同时登记本组。
        "tests.test_runtime_diagnostics_checks",
    ),
    "security": (
        "tests.test_application_audit_log",
        "tests.test_credentials_and_actions",
        "tests.test_json_safety",
        "tests.test_json_safety_property",
        "tests.test_bounded_runtime_inputs",
        "tests.test_profile_package",
        "tests.test_integrity",
        "tests.test_logging_service",
        "tests.test_scheduled_task_logs",
        "tests.test_runtime_trace",
        "tests.test_privacy",
        "tests.test_privacy_sentinel",
        "tests.test_workflow_security",
        "tests.test_review_r14_guards",
        "tests.test_plugin_config_validation",
        "tests.test_plugin_actions_coverage",
        # FR-009 回归：plugin_protocol.py 的 validate_plugin_message 异常契约。
        # plugin_protocol.py 经 ImpactRule 命中 ("packaging", "security")，
        # 登记本套件即可被插件协议变更触发。
        "tests.test_plugin_protocol_contract",
        "tests.test_invariant_multi_image_whitelist",
        # 插件禁用的本机持久状态（维护者裁定 PLG-004 语义）：plugin_registry.py
        # 变更经实测命中 ("packaging", "security")，故登记 security 即可被触发。
        "tests.test_plugin_disable_persistence",
        # DPAPI 密文篡改/截断防护证据：credentials.py 命中 ("security",)。
        "tests.test_credential_dpapi_integrity",
    ),
    "packaging": (
        "tests.test_release_tooling",
        "tests.test_private_release_inputs",
        # 启动冒烟字节码条件标注（2026-10-21）：verify_release 命中 smoke/packaging，
        # 本模块守护该脚本的启动段，必须随 packaging 组选中，否则「存在但不可达」。
        "tests.test_startup_bytecode_annotation",
        # 启动测量协议组件（A/B/C 分类、缓存校验、隔离副本、5 次采样）。
        "tests.test_startup_measurement",
        # 采样驱动（scripts/run_startup_baseline.py）随 packaging 组回归。
        "tests.test_dpi_awareness",
        "tests.test_runtime_pruning",
        "tests.test_integrity",
        "tests.test_module_boundaries",
        "tests.test_startup_architecture",
        "tests.test_repository_hygiene",
        "tests.test_plugin_manifest_package",
        "tests.test_manifest_validate_p2_3",
        "tests.test_office_docs_plugin",
        "tests.test_p7a_dead_code",
        "tests.test_p7b_release_hardening",
        "tests.test_build_release_opencv_lock",
        "tests.test_verify_release_s11",
        # FR-015: 插件数据层 pyright 存量基线（棘轮）守护——静态检查工具链。
        "tests.test_pyright_plugin_baseline",
    ),
    "performance": (
        "tests.test_performance_metrics",
        "tests.test_performance_comparison",
        "tests.test_benchmark_util_percentile",
        "tests.test_benchmark_util_affinity",
        "tests.test_core_regressions",
        "tests.test_ui_performance",
    ),
    # Marker suite: plugin tests are executed via pytest (not unittest) because
    # plugin packages live outside the top-level ``tests`` namespace. The actual
    # directories are carried on the ImpactPlan, not as dotted modules here.
    "plugins": (),
}

ALL_TEST_MODULES = tuple(
    f"tests.{path.stem}"
    for path in sorted((ROOT / "tests").glob("test_*.py"))
)


# ---------------------------------------------------------------------------
# 测试收集通道分流（unittest / pytest）
# ---------------------------------------------------------------------------
# 背景（缺陷成因）：标准 unittest **只**收集 unittest.TestCase 子类，完全不收集
# 顶层 ``def test_*`` 函数；而 unittest.main() 在收集到 0 个用例时
# wasSuccessful() 仍返回 True，退出码 0。两者叠加的后果是：一个只写顶层测试
# 函数的模块，在 unittest 门禁下既不报错也不运行，输出一行 "NO TESTS RAN" 就
# 变绿。本仓库曾因此有 15 个模块、近百个测试函数长期零执行。
#
# 修法不是把整个仓库切到 pytest（143 个模块是 TestCase 写法，主运行器仍是
# unittest），而是**按模块实际写法分流**：TestCase 走 unittest，顶层函数走
# pytest，两者兼有的走两遍。分类在运行时动态完成而非硬编码清单，因此新增的
# 顶层函数模块会被自动纳入，不需要任何人记得登记。
#
# INVARIANT: 每个 tests/test_*.py 至少要被一个通道收集到。真正"两个通道都收
# 不到"的模块会被送进 unittest，并由 run_unittest_qt.py 的零用例熔断硬报错，
# 而不是静默放过。tests/test_test_collection_gap.py 门禁这一整套。

# 这些语句体在 import 时执行，其中定义的 class/def 同样绑定到模块命名空间，
# 因此必须下探。（test_json_safety_property.py 的 TestCase 就藏在
# ``if _HAS_HYPOTHESIS:`` 里，只看 tree.body 会漏判为"无用例"。）
_SCOPE_TRANSPARENT_STATEMENTS: tuple[type, ...] = tuple(
    node_type
    for node_type in (
        getattr(ast, name, None)
        for name in ("If", "Try", "TryStar", "For", "AsyncFor", "While", "With", "AsyncWith")
    )
    if node_type is not None
)


def _iter_module_scope_nodes(body):
    """产出所有在 import 时执行、且会绑定模块级名字的语句节点。

    刻意**不**下探 FunctionDef / ClassDef：函数体内的 ``def test_*`` 属于局部
    作用域，unittest 与 pytest 都收集不到，把它算作"有用例"会制造假绿。
    """
    for node in body:
        yield node
        if isinstance(node, _SCOPE_TRANSPARENT_STATEMENTS):
            for field in ("body", "orelse", "finalbody"):
                yield from _iter_module_scope_nodes(getattr(node, field, None) or [])
            for handler in getattr(node, "handlers", None) or []:
                yield from _iter_module_scope_nodes(handler.body)


def _base_name(node) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _unittest_testcase_names(nodes) -> set[str]:
    """模块级定义的 unittest.TestCase 子类名集合（含间接继承）。

    先按名字后缀识别直接继承（unittest.TestCase / SomethingTestCase），再对
    模块内自定义基类做不动点闭包，覆盖
    ``class _Base(unittest.TestCase)`` + ``class Foo(_Base)`` 这类两级写法。
    """
    classes = [node for node in nodes if isinstance(node, ast.ClassDef)]
    if not classes:
        return set()
    testcase_names: set[str] = set()
    changed = True
    while changed:
        changed = False
        for cls in classes:
            if cls.name in testcase_names:
                continue
            for base in cls.bases:
                name = _base_name(base)
                if name and (name.endswith("TestCase") or name in testcase_names):
                    testcase_names.add(cls.name)
                    changed = True
                    break
    return testcase_names


def _defines_module_level_test_function(nodes) -> bool:
    """模块级是否定义了 ``def test_*`` 函数（pytest 默认 python_functions=test*）。"""
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
        for node in nodes
    )


def _defines_pytest_test_class(nodes, testcase_names: set[str]) -> bool:
    """模块级是否定义了 pytest 风格的 ``class Test*`` 测试类。

    pytest 默认 ``python_classes = Test*``：一个**不继承 TestCase** 的
    ``class TestFoo:`` 只要有 ``test_*`` 方法就会被 pytest 收集，而标准 unittest
    对它完全视而不见。这正是本仓库那个"静默零执行"缺陷的另一种写法——只补
    顶层 ``def test_*`` 的识别会漏掉它，让同类漏测在发布门禁上继续成立。

    与 pytest 的真实收集行为对齐的两个排除条件：
    * 已是 TestCase 子类的类由 unittest 通道负责，不重复计入 pytest 通道；
    * 定义了 ``__init__`` 的类 pytest 会跳过收集（只发一条 warning）。这类模块
      因此落回 ``(False, False)``，进 unittest 通道后由零用例熔断硬报错——
      响亮失败，而不是继续假绿。
    """
    for node in nodes:
        if not isinstance(node, ast.ClassDef):
            continue
        if not node.name.startswith("Test") or node.name in testcase_names:
            continue
        methods = [
            child
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if any(method.name == "__init__" for method in methods):
            continue
        if any(method.name.startswith("test") for method in methods):
            return True
    return False


@dataclass(frozen=True)
class TestModuleChannels:
    """一个测试模块需要经由哪些运行器才能被完整执行。"""

    unittest: bool
    pytest: bool


def module_path(module: str) -> Path:
    """把 ``tests.test_foo`` 形式的点分模块名映射回文件路径。"""
    return ROOT.joinpath(*module.split(".")).with_suffix(".py")


def _resolve_test_module_path(module: str | Path) -> Path:
    """把「点分模块名」或「文件路径」统一解析成 Path。

    只按 ``isinstance(module, Path)`` 分支是不够的：调用方常常手上就是一个字符串
    形式的路径（``"tests/test_foo.py"``），走点分分支会被 split(".") 切成
    ``tests/test_foo/py`` 这种垃圾路径，最终落进 ``except OSError`` 的兜底分支，
    **静默给出错误分类**。这里显式区分两种形态。
    """
    if isinstance(module, Path):
        return module
    if module.endswith(".py") or "/" in module or "\\" in module:
        return Path(module)
    return module_path(module)


def classify_test_module(module: str | Path) -> TestModuleChannels:
    """静态判断一个测试模块该走哪些收集通道。

    解析失败或文件缺失时返回 ``unittest=True, pytest=False``——保持既有行为，
    并让零用例熔断在运行期把问题喊出来，而不是在这里悄悄吞掉。
    """
    path = _resolve_test_module_path(module)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return TestModuleChannels(unittest=True, pytest=False)
    nodes = list(_iter_module_scope_nodes(tree.body))
    testcase_names = _unittest_testcase_names(nodes)
    return TestModuleChannels(
        unittest=bool(testcase_names),
        pytest=(
            _defines_module_level_test_function(nodes)
            or _defines_pytest_test_class(nodes, testcase_names)
        ),
    )


def split_modules_by_runner(modules: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把点分模块名分流到 (unittest 通道, pytest 通道)。

    * 含 TestCase → unittest 通道。
    * 含模块级 test 函数或 pytest 风格 ``class Test*`` → pytest 通道。
    * 两者兼有（hybrid）→ 两个通道都跑。这是**安全但不理想**的兜底，见
      ``hybrid_test_modules()``；本仓库当前为 0 个，并由门禁保持为 0。
    * 两者皆无 → 仍进 unittest 通道，由零用例熔断硬报错。
    """
    unittest_modules: list[str] = []
    pytest_modules: list[str] = []
    for module in modules:
        channels = classify_test_module(module)
        if channels.pytest:
            pytest_modules.append(module)
        if channels.unittest or not channels.pytest:
            unittest_modules.append(module)
    return tuple(unittest_modules), tuple(pytest_modules)


def hybrid_test_modules() -> tuple[str, ...]:
    """同时含 TestCase 与 pytest 风格用例的模块——本仓库要求恒为空。

    为什么要禁掉这种写法，而不是"支持"它：

    * 双通道会让同一批 TestCase 方法被跑两遍（pytest 也收集 TestCase），
      带副作用的用例因此可能出现只在门禁里复现的诡异失败。
    * 更要命的是隔离语义冲突。unittest 通道刻意**逐模块起独立进程**
      （见 run_impacted_tests._run_modules_isolated 的说明：共享进程会在模块间
      泄漏环境变量、日志与翻译状态，制造"只在批量下失败"的假阳性），而 pytest
      通道是**单进程批量**。一个本来需要隔离的 TestCase 模块，只要有人给它加了
      一个顶层 ``def test_x``，就会被顺带塞进共享进程里跑第二遍。

    正确做法是让一个模块只用一种写法：把顶层函数改写成 TestCase 方法，或者把它们
    拆到独立模块。tests/test_test_collection_gap.py 会把这条约束钉成门禁。
    """
    hybrid: list[str] = []
    for module in ALL_TEST_MODULES:
        channels = classify_test_module(module)
        if channels.unittest and channels.pytest:
            hybrid.append(module)
    return tuple(hybrid)


def pytest_required_modules() -> tuple[str, ...]:
    """全仓扫描：所有必须经由 pytest 才能被执行到的测试模块。

    正式发布门禁用它补齐 ``unittest discover`` 收不到的那部分。
    """
    return split_modules_by_runner(ALL_TEST_MODULES)[1]


def discover_plugin_test_dirs():
    """Return existing plugin test directories (relative to ROOT).

    Each entry is a ``plugins/<id>/tests`` directory. These are run with pytest
    rather than unittest because plugin packages are not importable from the
    top-level ``tests`` namespace (review item #3).
    """

    dirs: list[Path] = []
    plugins_root = ROOT / "plugins"
    if plugins_root.is_dir():
        for child in sorted(plugins_root.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue
            test_dir = child / "tests"
            if test_dir.is_dir():
                dirs.append(test_dir.relative_to(ROOT))
    return dirs


def _plugin_id_from_path(path: str) -> str | None:
    parts = normalize_path(path).split("/")
    if len(parts) >= 2 and parts[0] == "plugins" and parts[1]:
        return parts[1]
    return None


# DESIGN: plugins/tools/ 不是插件，而是随仓库全部插件共用的构建工具链
# （plugins/tools/common.py 被每个 plugins/<id>/tools/build_plugin.py 直接导入）。
# 按 AGENTS.md「修改被多项功能复用的模块须运行相邻行为回归」，改动该目录必须回归
# 全部插件测试，而不是只运行 plugins/tools/tests。
SHARED_PLUGIN_TOOLCHAIN_IDS = frozenset({"tools"})

# DESIGN: 宿主侧插件契约模块——清单 schema、包校验、IPC 帧、动作契约与注册表。
# 改动其中任何一个都会直接影响每个随仓库插件自己的测试（plugins/*/tests 会经
# plugins/tools/common.py → PluginManifest.parse_json 等走同一契约）。
# tests/TEST_MATRIX.md:13 把 `fukua_rpa/plugin_*` 列为 plugins 组的常见变更路径，
# ARCHITECTURE.md #68 要求按影响选择时「必须扩大而不是缩小范围」；因此此类变更
# 展开全部插件测试目录（审查 FR-006）。
PLUGIN_CONTRACT_MODULES = (
    "fukua_rpa/plugin_manifest.py",
    "fukua_rpa/plugin_package.py",
    "fukua_rpa/plugin_protocol.py",
    "fukua_rpa/plugin_host_process.py",
    "fukua_rpa/plugin_host_client.py",
    "fukua_rpa/plugin_actions.py",
    "fukua_rpa/plugin_registry.py",
)


def collect_impacted_plugin_test_dirs(paths):
    """Map changed plugin-related paths to the plugin test dirs to run.

    Covers both ``plugins/**`` paths and the host-side plugin contract modules
    (``fukua_rpa/plugin_*.py``), which ``tests/TEST_MATRIX.md`` lists as a
    ``plugins``-group trigger path.
    """

    normalized = [normalize_path(str(path)) for path in paths]
    plugin_ids = {pid for pid in (_plugin_id_from_path(p) for p in normalized) if pid}
    contract_changed = any(path in PLUGIN_CONTRACT_MODULES for path in normalized)
    if not plugin_ids and not contract_changed:
        return []
    all_dirs = discover_plugin_test_dirs()
    if contract_changed or (plugin_ids & SHARED_PLUGIN_TOOLCHAIN_IDS):
        # 宿主插件契约或共享构建工具链变更：扩大而不是缩小范围。
        return list(all_dirs)
    return [d for d in all_dirs if d.parent.name in plugin_ids]


@dataclass(frozen=True)
class ImpactRule:
    patterns: tuple[str, ...]
    suites: tuple[str, ...]
    reason: str

    def matches(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, pattern) for pattern in self.patterns)


@dataclass(frozen=True)
class ImpactPlan:
    paths: tuple[str, ...]
    suites: tuple[str, ...]
    modules: tuple[str, ...]
    reasons: tuple[str, ...]
    full_suite: bool = False
    plugin_test_dirs: tuple[str, ...] = ()


RULES = (
    ImpactRule(
        ("AGENTS.md", "docs/代码审查基准.md", "docs/代码审查模板.md", "docs/开发备份与恢复规范.md",
         "scripts/check_review_workflow.py", ".github/workflows/**"),
        ("review",),
        "审查路由、原字节备份与交付证据校验",
    ),
    ImpactRule(
        (".github/workflows/**", ".pre-commit-config.yaml"),
        ("smoke", "packaging"),
        "源码质量门禁和 CI 配置发生变化",
    ),
    ImpactRule(
        ("docs/UI开发规范.md",),
        ("ui",),
        "UI 组件和静态门禁规范发生变化",
    ),
    ImpactRule(
        (".gitignore",),
        ("packaging",),
        "repository privacy and generated-file boundaries",
    ),
    ImpactRule(
        (
            "fukuaRPA.py",
            "fukua_rpa/dpi_awareness.py",
            "fukua_rpa/coordinates.py",
            "fukua_rpa/ui/input_tools.py",
            "fukua_rpa/ui/theme.py",
            "assets/fukuaRPA.manifest",
            "fukuaRPA_onedir.spec",
            "fukuaRPA_onefile.spec",
        ),
        ("ui", "input", "vision", "packaging"),
        "逐屏 DPI、物理坐标契约或冻结包进程清单",
    ),
    ImpactRule(
        (
            "fukua_rpa/profile_hotkeys.py",
            "fukua_rpa/ui/profile_hotkey_dialog.py",
            "fukua_rpa/ui/controllers/profile_hotkey_controller.py",
        ),
        ("ui", "config"),
        "多方案快捷启动槽位、稳定方案绑定或本机热键设置",
    ),
    ImpactRule(
        (
            "fukua_rpa/workflow_document.py",
            "fukua_rpa/workflow_metadata.py",
        ),
        ("workflow", "ui"),
        "stable workflow identity or editor-group invariants",
    ),
    ImpactRule(
        (
            "fukua_rpa/ui/task_row.py",
            "fukua_rpa/ui/task_row_controllers/template_capture_controller.py",
            "fukua_rpa/ui/controllers/workflow_editor.py",
        ),
        ("workflow", "ui", "logging", "security"),
        "step editor transactions, audit privacy, or group organization",
    ),
    ImpactRule(
        ("fukua_rpa/ui/controllers/workflow_search.py",),
        ("workflow", "ui"),
        "workflow search controller MRO and editor navigation behavior",
    ),
    ImpactRule(
        # REG-20260929-001 后续映射修正：main_window 装配主窗口运行接线
        # （错过补跑询问等），其变更须同时选中 workflow 组核心回归，
        # 避免只跑 ui 组时遗漏 workflow 套件中的主窗口行为测试。
        ("fukua_rpa/ui/main_window.py",),
        ("ui", "workflow"),
        "main window assembly: UI widgets plus engine-facing run wiring",
    ),
    ImpactRule(
        (
            "fukua_rpa/ui/**",
            "fukua_rpa/coordinates.py",
            "fukua_rpa/usage_notices.py",
            "assets/**",
            "scripts/capture_ui_review.py",
        ),
        ("ui",),
        "Qt widgets, layout, theme, and UI assets",
    ),
    ImpactRule(
        (
            # Runner 已拆为 fukua_rpa/runner/ 子包（backend/ipc/isolated/job/process/
            # protocol）；此处必须跟随实际布局，否则该子包的变更会退化为全量测试。
            "fukua_rpa/runner/**",
            "fukua_rpa/native_services.py",
            "fukua_rpa/worker.py",
            "fukua_rpa/ui/controllers/run_controller.py",
        ),
        ("runner", "workflow", "security"),
        "Runner lifecycle, IPC, cancellation, or process isolation",
    ),
    ImpactRule(
        (
            "fukua_rpa/scheduled_task_model.py",
            "fukua_rpa/scheduled_task_service.py",
            "fukua_rpa/scheduled_task_store.py",
            "fukua_rpa/scheduled_task_logs.py",
            "fukua_rpa/scheduled_run_coordinator.py",
            "fukua_rpa/missed_run_detector.py",
        ),
        ("workflow", "runner", "logging", "security"),
        "计划任务源码变更影响工作流调度、运行器及日志隐私边界",
    ),
    ImpactRule(
        (
            "fukua_rpa/engine/**",
            "fukua_rpa/run_config.py",
            "fukua_rpa/scheduler.py",
            "fukua_rpa/step_runtime.py",
            "fukua_rpa/debug_session.py",
            "fukua_rpa/expressions.py",
            "fukua_rpa/workflow_*.py",
            "fukua_rpa/bulk_task_settings.py",
            "fukua_rpa/task_model.py",
            "fukua_rpa/commands.py",
            "fukua_rpa/auxiliary_clicks.py",
            "fukua_rpa/multi_image_actions.py",
            "fukua_rpa/validation.py",
            "fukua_rpa/preview_model.py",
        ),
        ("workflow", "logging"),
        "execution scheduling, task semantics, validation, or workflow analysis",
    ),
    ImpactRule(
        (
            "fukua_rpa/engine/vision_exec.py",
            "fukua_rpa/vision/**",
            "fukua_rpa/native_services.py",
            "native_core/**",
            "fukua_rpa_core.dll",
        ),
        ("vision", "performance"),
        "image capture, matching, scene wake, scale memory, or native core",
    ),
    ImpactRule(
        (
            "fukua_rpa/engine/actions_exec.py",
            "fukua_rpa/actions/**",
            "fukua_rpa/random_distribution.py",
            "fukua_rpa/input_backend.py",
            "fukua_rpa/hook_backend.py",
            "fukua_rpa/native_services.py",
            # 录制已拆为数据模型 + UI 控制器两处；ui/** 规则只会带来 ui 套件，
            # 录制语义回归仍须由本规则显式触发 input/workflow。
            "fukua_rpa/recording_model.py",
            "fukua_rpa/ui/controllers/recording_controller.py",
            "fukua_rpa/text_input.py",
            # 单一 SendInput INPUT ABI 模块（FR-20261016-SUP-001）：其变更与
            # text_input/input_backend 同等对待，必须选中 input 组。
            "fukua_rpa/win32_input.py",
            "fukua_rpa/script_prompt.py",
        ),
        ("input", "workflow"),
        "mouse, keyboard, action execution, or recording",
    ),
    ImpactRule(
        (
            "fukua_rpa/mapping_*.py",
            "fukua_rpa/uia_*.py",
            "fukua_rpa/window_*.py",
            "fukua_rpa/win32_api.py",
            "fukua_rpa/hook_backend.py",
            "fukua_rpa/coordinates.py",
        ),
        ("mapping", "input"),
        "window binding, UI Automation, hotkeys, or Win32 integration",
    ),
    ImpactRule(
        (
            "fukua_rpa/config_*.py",
            "fukua_rpa/profile_*.py",
            "fukua_rpa/paths.py",
            "fukua_rpa/session.py",
        ),
        ("config", "security"),
        "configuration, profiles, storage paths, migration, or session recovery",
    ),
    ImpactRule(
        (
            "fukua_rpa/log*.py",
            "fukua_rpa/performance.py",
            "fukua_rpa/diagnostics.py",
            "fukua_rpa/runtime_trace.py",
        ),
        ("logging", "performance", "security"),
        "logging, diagnostics, telemetry, or bounded performance metrics",
    ),
    ImpactRule(
        (
            "fukua_rpa/credentials.py",
            "fukua_rpa/integrity.py",
            "fukua_rpa/privacy.py",
            "fukua_rpa/workflow_security.py",
        ),
        ("security",),
        "credentials or integrity boundary",
    ),
    ImpactRule(
        PLUGIN_CONTRACT_MODULES,
        ("packaging", "security"),
        "插件宿主契约变更影响打包、安全和随仓库插件测试",
    ),
    ImpactRule(
        ("plugins/**",),
        ("plugins", "packaging", "security"),
        "插件源码变更影响对应插件单元测试",
    ),
    ImpactRule(
        (
            "fukuaRPA.py",
            "fukuaRPA_*.spec",
            "fukua_rpa/constants.py",
            # 启动装配位于 fukua_rpa/ui/startup.py；仅靠 ui/** 规则不会带来
            # smoke/packaging，test_startup_architecture 会被漏掉。
            "fukua_rpa/ui/startup.py",
            "fukua_rpa/crash_guard.py",
            "scripts/build_release.py",
            "scripts/build_identity.py",
            "scripts/verify_release.py",
            "scripts/test_impact.py",
            "scripts/run_impacted_tests.py",
            "scripts/create_*.py",
            "scripts/release_*.py",
            "scripts/audit_*.py",
            "scripts/runtime_pruning.py",
            "requirements.txt",
            "requirements-build.txt",
        ),
        ("smoke", "packaging"),
        "startup identity, dependency, packaging, or release tooling",
    ),
    ImpactRule(
        (
            "fukua_rpa/ui/task_row.py",
            "fukua_rpa/ui/task_row_controllers/**",
            "fukua_rpa/ui/lazy_dialogs.py",
            "fukua_rpa/ui/controllers/workflow_group.py",
            "fukua_rpa/workflow_summary.py",
        ),
        ("smoke", "ui", "workflow"),
        "步骤行、步骤摘要与单步骤设置对话框的启动导入边界（ADR 0040）",
    ),
    ImpactRule(
        (
            "fukua_rpa/ui/controllers/workflow_review_controller.py",
            "fukua_rpa/ui/controllers/feedback_controller.py",
        ),
        ("smoke", "ui", "workflow"),
        "运行前校验层与浮层的启动导入边界（ADR 0041）",
    ),
    ImpactRule(
        ("scripts/smoke_native*.py",),
        ("vision", "packaging"),
        "native recognition or cancellation smoke gate",
    ),
    ImpactRule(
        ("scripts/benchmark_ui_interaction.py",),
        ("ui", "performance"),
        "interactive Qt move, resize, layout, or virtualization benchmark",
    ),
    ImpactRule(
        ("scripts/check_ui_conventions.py",),
        ("ui",),
        "UI 新增代码规范静态门禁",
    ),
    ImpactRule(
        ("scripts/benchmark_core.py",),
        ("workflow", "performance"),
        "workflow execution benchmark",
    ),
    ImpactRule(
        (
            "scripts/benchmark_vision.py",
            "scripts/benchmark_native_scheduler.py",
        ),
        ("vision", "performance"),
        "vision or native scheduling benchmark",
    ),
    ImpactRule(
        ("scripts/benchmark_*.py", "scripts/compare_performance.py", "scripts/soak_runtime.py"),
        ("performance",),
        "performance benchmark or soak gate",
    ),
    # FR-20260809-1306: 其余发布/冒烟/辅助脚本工具定向到 smoke+packaging，
    # 避免改动它们时回退全量测试（修复前 8 个 scripts 工具未映射）。
    ImpactRule(
        (
            "scripts/build_icon.py",
            "scripts/hashutil.py",
            "scripts/lib/*.py",
            "scripts/pyright_plugin_baseline.py",
            "scripts/run_unittest_qt.py",
            "scripts/smoke_performance_ci.py",
            "scripts/smoke_startup.py",
            "scripts/smoke_uia_control.py",
            "scripts/verify_real_machine.py",
        ),
        ("smoke", "packaging"),
        "release/smoke/auxiliary script tooling",
    ),
    # FR-20260930-017：以下源码变更此前不选中其守卫测试（独立审查实测 5 组缺口）。
    # 这些规则只做**加法**——为路径补上守卫测试所在套件，不缩小任何既有选中集合
    # （plan_impacted_tests 对全部命中规则取并集）。契约由
    # tests/test_test_impact.py::ImpactGuardCoverageTests 锁定。
    ImpactRule(
        (
            "fukua_rpa/ui/controllers/workflow_transfer_controller.py",
            "fukua_rpa/workflow_analysis.py",
        ),
        ("config",),
        "导出/导入结构校验与工作流分析的守卫测试登记在 config 套件（FR-20260930-017）",
    ),
    ImpactRule(
        (
            "fukua_rpa/scheduled_task_model.py",
            "fukua_rpa/scheduled_task_service.py",
        ),
        ("ui",),
        "计划任务模型/服务的 UI 回归与锁守卫测试登记在 ui 套件（FR-20260930-017）",
    ),
    ImpactRule(
        ("fukua_rpa/window_actions.py",),
        ("workflow",),
        "窗口动作的进程回收守卫测试登记在 workflow 套件（FR-20260930-017）",
    ),
    ImpactRule(
        (
            "fukua_rpa/plugin_actions.py",
            "fukua_rpa/plugin_protocol.py",
            "fukua_rpa/window_actions.py",
        ),
        ("runner",),
        "插件动作/协议契约测试登记在 runner 套件（FR-20260930-017）",
    ),
)


# DESIGN: 豁免存在性检查的构建产物模式。
# 这些路径被 .gitignore 排除、不受版本控制，因此不会出现在 collect_changed_paths
# 的输出里；但它们仍可经 run_impacted_tests.py --path 手动传入（
# native_sources_changed 正是为此保留的），所以规则里保留它们是有意义的，不是
# 死模式。由于干净检出上它们必然不存在，若不豁免，门禁就会取决于「开发者本机
# 是否恰好构建过」——本机绿、CI 恒红。
# 豁免不是逃生舱：tests/test_test_impact.py 断言这里的每一项都确实未被 git 跟踪，
# 因此无法用它掩盖一个真实源文件的漂移。
BUILD_ARTIFACT_PATTERNS = frozenset({"fukua_rpa_core.dll"})


def stale_rule_patterns() -> tuple[str, ...]:
    """Return RULES patterns that no longer match any repository path.

    维护上述 INVARIANT 的可执行检查：返回值非空即表示映射已相对源码布局漂移。
    去重后按 RULES 出现顺序返回，便于直接定位需要修正的模式。

    INVARIANT: 存在性判定必须与运行期匹配同源，否则门禁会漏判或误判。
    运行期 ImpactRule.matches 用 fnmatch 匹配「git 报告的变更路径」，所以这里也
    用 fnmatch 匹配「已跟踪或未被忽略的工作树文件」，而不是拿 glob 去扫文件系统。
    两者并不等价：
      * 语义不同——glob 的 ``*`` 不跨 ``/``，fnmatch 的跨；同一模式可能一个命中
        一个不命中，导致「glob 说存在、运行期其实永不命中」的死规则被放行。
      * 基准不同——文件系统含构建产物、缓存等被忽略文件，会让门禁在开发者本机
        变绿却在干净检出上失败；一般只纳入 Git 跟踪或标准未忽略文件。
    维护者明确排除公开的私有验证资料例外：正式验证会在隔离目录补齐并绑定其
    指纹，因此具体私有文件存在时仍是有效规则；缺失文件及其他忽略路径不能豁免。
    git 不可用时（例如从 tarball 解压的源码树）返回空元组：此时无从判定，不报假
    阳性；CI 与开发机都是 git 检出，门禁在真正需要它的场景里始终生效。
    """

    # 同时纳入未被 .gitignore 排除的工作树文件。开发阶段新建规则和测试通常
    # 还未暂存；若这里只看已跟踪文件，新增的有效规则会在本地门禁中被错误地
    # 判为 stale，迫使维护者为了跑测试提前改变暂存区状态。
    repository_paths = frozenset(
        normalize_path(path)
        for path in (
            _git_lines("ls-files")
            + _git_lines("ls-files", "--others", "--exclude-standard")
        )
        if path
    )
    if not repository_paths:
        return ()

    stale: list[str] = []
    seen: set[str] = set()
    for rule in RULES:
        for pattern in rule.patterns:
            if pattern in seen or pattern in BUILD_ARTIFACT_PATTERNS:
                continue
            seen.add(pattern)
            private_path = ROOT / pattern
            if (
                is_private_validation_path(pattern)
                and private_path.is_file()
                and private_path.resolve().is_relative_to(ROOT.resolve())
            ):
                continue
            if not any(
                fnmatch.fnmatch(path, pattern) for path in repository_paths
            ):
                stale.append(pattern)
    return tuple(stale)


DOC_PATTERNS = (
    "*.md",
    "docs/**",
    "LICENSE",
    ".gitignore",
    ".gitattributes",
    ".git_allowed_signers",
)


def normalize_path(path: str | Path) -> str:
    value = str(path).replace("\\", "/").strip()
    if value.startswith("./"):
        value = value[2:]
    return value


def _git_lines(*args: str) -> list[str]:
    try:
        result = subprocess.run(
            # Git defaults quote non-ASCII filenames; rules use real UTF-8 paths.
            ["git", "-c", "core.quotepath=false", *args],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,  # FR-20260812-D15-001: 防止 git 挂起无限阻塞
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # Initial commit has no parent → diff-tree fails.
        # Fall back to empty list; collect_changed_paths will use
        # ls-files --others as fallback for untracked files.
        import sys as _sys

        print(f"[test_impact] git command failed: {exc}", file=_sys.stderr)
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _impact_base_ref() -> str | None:
    """Resolve the published base of the current branch.

    Prefers the configured upstream, then common origin/main or main refs. This
    lets us capture *every* committed change on the branch (including multiple
    unpushed commits) rather than only the latest commit, so impacted-test
    selection does not silently miss changes from earlier commits.

    FR-20261006-003: a candidate that resolves to HEAD itself is **skipped**.
    On a branch that is pushed and has no unpushed commits, ``@{upstream}``
    *is* HEAD, so accepting it makes ``git diff <base> HEAD`` empty and drops
    every already-committed source file from impacted-test selection. The
    affected branch then degrades to whatever the uncommitted working tree
    happens to contain. Returning the first candidate that actually differs
    from HEAD keeps the original "capture unpushed commits" intent while
    guaranteeing a non-empty committed baseline whenever one exists.
    """
    head_lines = _git_lines("rev-parse", "HEAD")
    head = head_lines[0] if head_lines else ""
    candidates = ("@{upstream}", "origin/main", "origin/master", "main", "master")
    for ref in candidates:
        try:
            lines = _git_lines("rev-parse", "--verify", "--quiet", ref)
        except Exception:
            lines = ()
        if not lines:
            continue
        resolved = lines[0]
        if head and resolved == head:
            # 该候选等于 HEAD，提交差异恒为空；继续尝试下一个基线。
            continue
        return resolved
    return None


def collect_changed_paths() -> tuple[str, ...]:
    # Include the current commit itself. A clean feature/release branch can still
    # contain committed changes that differ from the previously packaged build.
    paths = set()
    paths.update(
        normalize_path(path)
        for path in _git_lines("diff", "--name-only", "HEAD")
        if path
    )
    paths.update(
        normalize_path(path)
        for path in _git_lines("ls-files", "--others", "--exclude-standard")
        if path
    )
    # Committed changes since the published base. Use the branch's upstream when
    # available so all unpushed commits are captured; fall back to the single
    # latest commit when no upstream is configured.
    base = _impact_base_ref()
    if base:
        paths.update(
            normalize_path(path)
            for path in _git_lines("diff", "--name-only", base, "HEAD")
            if path
        )
    else:
        paths.update(
            normalize_path(path)
            for path in _git_lines(
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "HEAD",
            )
            if path
        )
    return tuple(sorted(paths))


# Retained for use by tests/test_test_impact.py; not called by build scripts.
def native_sources_changed(paths: Iterable[str]) -> bool:
    normalized = tuple(normalize_path(path) for path in paths)
    return any(
        path == "fukua_rpa_core.dll" or path.startswith("native_core/")
        for path in normalized
    )


def _is_documentation(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in DOC_PATTERNS)


def plan_impacted_tests(paths: Iterable[str]) -> ImpactPlan:
    normalized = tuple(sorted({normalize_path(path) for path in paths if path}))
    if not normalized:
        # git unavailable or no changes detected; run full suite to be safe
        # (AGENTS.md: unknown impact must expand, not shrink, test scope)
        return ImpactPlan(
            paths=("<no-changes>",),
            suites=tuple(sorted(SUITES.keys())),
            modules=tuple(
                module
                for suite in sorted(SUITES.keys())
                for module in SUITES[suite]
            ),
            reasons=("No changed paths detected; run full suite to be safe.",),
        )

    suites: set[str] = set()
    direct_modules: set[str] = set()
    reasons: list[str] = []
    full_suite = False

    for path in normalized:
        if path.startswith("tests/test_") and path.endswith(".py"):
            direct_modules.add(path[:-3].replace("/", "."))
            reasons.append(f"{path}: changed test module")
            continue
        matched = False
        for rule in RULES:
            if not rule.matches(path):
                continue
            suites.update(rule.suites)
            reasons.append(f"{path}: {rule.reason}")
            matched = True
        if matched or _is_documentation(path):
            continue
        if path.endswith((".py", ".cpp", ".h", ".ps1", ".spec")):
            full_suite = True
            reasons.append(f"{path}: unmapped executable source; conservative full suite")
        else:
            # 未知影响：回退到全量测试（AGENTS.md "未知影响须扩大"）
            full_suite = True
            reasons.append(f"{path}: unmapped non-documentation file; conservative full suite")

    modules = set(direct_modules)
    for suite in suites:
        modules.update(SUITES[suite])
    if full_suite:
        suites.update(SUITES)
        modules.update(ALL_TEST_MODULES)

    # Plugin test directories affected by the changed paths (review item #3).
    plugin_dirs: set[str] = set()
    plugin_dirs.update(
        normalize_path(str(d)) for d in collect_impacted_plugin_test_dirs(normalized)
    )
    if full_suite:
        plugin_dirs.update(normalize_path(str(d)) for d in discover_plugin_test_dirs())
    if plugin_dirs:
        reasons.append(
            "plugins: 受影响插件测试目录 "
            + ", ".join(sorted(plugin_dirs))
        )

    return ImpactPlan(
        paths=normalized,
        suites=tuple(sorted(suites)),
        modules=tuple(sorted(modules)),
        reasons=tuple(dict.fromkeys(reasons)),
        full_suite=full_suite,
        plugin_test_dirs=tuple(sorted(plugin_dirs)),
    )
