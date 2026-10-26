"""Run source checks and smoke-test complete onedir/onefile builds."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

QT_UNITTEST_BOOTSTRAP = ROOT / "scripts" / "run_unittest_qt.py"

from fukua_rpa.constants import (  # noqa: E402
    APP_VERSION,
    BUILD_NAME,
    NATIVE_CORE_RELEASE_VERSION,
    ONEFILE_BUILD_NAME,
)
from scripts import startup_measurement as _sm  # noqa: E402
from scripts.build_identity import require_matching_evidence  # noqa: E402
from scripts.lib.run_helpers import run as _run_helper  # noqa: E402
from scripts.private_release_inputs import (  # noqa: E402
    assert_validation_inputs_unchanged,
    bind_validation_inputs,
)
from scripts.run_impacted_tests import run_plugin_tests  # noqa: E402
from scripts.test_impact import (  # noqa: E402
    discover_plugin_test_dirs,
    module_path,
    pytest_required_modules,
)

# 正式门禁单个子命令的超时上限。实测全量 unittest 套件（3569 项）暖机 514s
# （本轮原顺序运行退出码 0），全新克隆冷态下超过 600s（3320/3569 处被杀）——
# 600s 上限使必需门禁在正常正式构建条件下无法完成，属构建入口缺陷而非测试失败。
# 提高上限只让同一组断言有机会跑完，不放宽任何检查；真实挂起仍会被超时拦截。
# 外层整体预算见 build_release.verify_source_scope 的 timeout=1800。
SUBCOMMAND_TIMEOUT_S = 1800


def run(command, env=None):
    # 统一到 scripts/lib/run_helpers.run（§3.5(c)）；保留验证侧超时与
    # RuntimeError 语义。
    _run_helper(
        command, env=env, timeout=SUBCOMMAND_TIMEOUT_S, on_timeout=_timeout_runtime_error
    )


def _timeout_runtime_error(cmd, proc, exc):
    raise RuntimeError(
        f"Verification sub-command timed out after {SUBCOMMAND_TIMEOUT_S}s: "
        f"{subprocess.list2cmdline(cmd)}"
    ) from exc


def verify_plugin_tests(env) -> None:
    """Run every repository plugin test suite as a formal release hard gate."""

    test_dirs = tuple(discover_plugin_test_dirs())
    if not test_dirs:
        raise RuntimeError(
            "正式发布未发现任何插件测试目录；请确认 plugins/*/tests 已完整检出。"
        )
    run_plugin_tests(test_dirs, env, strict=True)


def verify_pytest_only_tests(env) -> None:
    """跑 ``unittest discover`` 收集不到的测试模块（正式发布硬门禁）。

    ``unittest discover`` 只收集 ``unittest.TestCase`` 子类。仓库里另有一批模块
    采用顶层 ``def test_*`` 写法，discover 对它们视而不见，且零收集不影响退出码
    ——历史上因此有 15 个模块、近百个测试函数在发布门禁下从未被执行过。
    这里按 scripts/test_impact.py 的分流结果补齐 pytest 通道。

    该集合为空同样视为异常：它意味着分流判定失效（而不是"恰好没有这类模块"），
    继续放行就等于把闸门关了还挂着绿灯。

    INVARIANT：必须经由本模块的 ``run()`` 执行，不要改调
    ``run_impacted_tests.run_pytest_modules``。verify_source 的每一步都走同一个
    run 入口，这既统一了超时/非零退出语义（这里要的是硬失败，不是 bool），也让
    tests/test_verify_release_s11.py 里 ``mock.patch("scripts.verify_release.run")``
    的用例保持封闭——绕过 run 会让那些单元测试真的拉起 pytest 子进程，
    把单测悄悄变成集成测试（本改动初版即踩中此坑：一次全量回归里多跑了 3 遍
    完整 pytest 通道）。
    """
    modules = pytest_required_modules()
    if not modules:
        raise RuntimeError(
            "未识别出任何需经 pytest 执行的测试模块；这通常意味着 "
            "scripts/test_impact.py 的收集分流判定已失效，而非仓库真的没有"
            "顶层 def test_* 写法的模块。请先核对 classify_test_module。"
        )
    files = [module_path(module).relative_to(ROOT).as_posix() for module in modules]
    # pytest 缺失时 ``python -m pytest`` 以非零码退出 → run() 抛错 → 门禁失败。
    # 这里刻意不提供任何"环境容忍"降级：降级就等于回到静默漏测。
    run(
        [
            sys.executable,
            "-m",
            "pytest",
            *files,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ],
        env,
    )


def verify_source():
    private_inputs = bind_validation_inputs()
    # C1-FUP-02：按**检查类型**配置子进程环境，而不是一刀切 offscreen。
    #
    # 绝大多数检查只需要无窗口的 Qt（offscreen），可以无人值守运行；
    # 但原生采集冒烟必须在**真实窗口**上核对原生 RGB 截图的尺寸与
    # 通道顺序（它对自己的探针窗口调用 `GetClientRect`）。在 offscreen 下该探针没有
    # 原生窗口句柄，检查必然失败——那是环境配错，不是产品缺陷。
    #
    # INVARIANT: 只改**环境**，不改任何检查的断言、脚本内容与退出码传播；
    # 不新增跳过或降级分支。真实桌面不可用时由冒烟自身失败使门禁非零退出
    # （阻塞，不放行）。
    env_ui = os.environ.copy()
    env_ui["QT_QPA_PLATFORM"] = "offscreen"
    # 只放开 `QT_QPA_PLATFORM` 一项；其余变量与 `env_ui` 相同。
    env_desktop = dict(env_ui)
    env_desktop.pop("QT_QPA_PLATFORM", None)
    # SAFETY: a single full-suite process must retain one QApplication for its
    # whole lifetime. Direct ``unittest discover`` can let module-local Qt
    # application objects be collected between test modules and abort Windows
    # with STATUS_STACK_BUFFER_OVERRUN when the next QWidget is constructed.
    run(
        [
            sys.executable,
            str(QT_UNITTEST_BOOTSTRAP),
            "discover",
            "-s",
            "tests",
            # Discover modules as tests.*, preserving package-relative imports.
            "-t",
            ".",
            "-v",
        ],
        env_ui,
    )
    verify_pytest_only_tests(env_ui)
    verify_plugin_tests(env_ui)
    # S11: formal release must pass the project's full ruff configuration
    # (pyproject.toml ``[tool.ruff]``), not just the minimal F/E9/E722 subset
    # used for ordinary test-package builds. Dropping ``--select`` lets ruff
    # apply every rule the project enables (E,F,W,I,UP,B,SIM,C4, ...).
    #
    # ``plugins/tools`` 是 4 个插件共享的构建逻辑，与 scripts/ 同属本仓库自有代码，
    # 因此适用同一套完整规则。它长期缺席这份清单纯属遗漏：独立审查线程发现
    # 该目录下积压了 9 项违规，其中包含一处真实的异常链丢失（B904，契约校验
    # 失败时底层诊断信息被吞）。目标清单由 test_release_lint_targets 锁定。
    run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "fukuaRPA.py",
            "fukua_rpa",
            "tests",
            "scripts",
            "plugins/tools",
        ],
        env_ui,
    )
    # 插件的 shipped 源码（plugins/*/src）只开 F 类规则，这是一条**有意的**
    # 分级策略，不是偷懒：
    # * F 类抓的是真错误——未定义名、未用导入。它们在插件里确有实例：
    #   ocr-local 的 numpy/Image 与 office-docs 的 PdfBackend 曾是 9 处 F821
    #   类型盲区，screen-recorder 的 snapshot() 还因一个多余的 ``import cv2``
    #   给截图强加了 opencv 依赖。
    # * 其余规则（UP/SIM/I 等）多为风格与 typing 现代化，插件子树尚有约 319 项
    #   存量债；与 pyright 已明确 ``exclude = [..., "plugins"]`` 的既定策略一致，
    #   这部分排入后续阶段统一清理，不在此处一次性引爆。
    # 用整个 plugins/ 作目标（而非逐个 */src）是为了让新增插件自动纳入。
    run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            "F",
            "plugins",
        ],
        env_ui,
    )
    # S11: formal release must also pass strict type checking. pyright is a
    # CI-only tool; if it is not installed the formal gate fails with a clear
    # message rather than silently skipping the check.
    import importlib.util

    if importlib.util.find_spec("pyright") is None:
        raise RuntimeError(
            "正式发布静态门禁需要 pyright（未安装）。请先 `pip install pyright` "
            "或在已配置 pyright 的 CI 环境中运行正式发布验证。"
        )
    run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--project",
            "pyrightconfig.strict.json",
        ],
        env_ui,
    )
    with tempfile.TemporaryDirectory() as performance_dir:
        performance_dir = Path(performance_dir)
        core_report = performance_dir / "core.json"
        vision_report = performance_dir / "vision.json"
        native_report = performance_dir / "native.json"
        startup_report = performance_dir / "startup.json"
        run(
            [
                sys.executable,
                "scripts/benchmark_core.py",
                "--rounds",
                "10",
                "--output",
                str(core_report),
                "--assert-limits",
            ],
            env_ui,
        )
        run(
            [
                sys.executable,
                "scripts/benchmark_vision.py",
                "--rounds",
                "10",
                "--output",
                str(vision_report),
                "--assert-limits",
            ],
            env_ui,
        )
        run(
            [
                sys.executable,
                "scripts/benchmark_native_scheduler.py",
                "--rounds",
                "10",
                "--output",
                str(native_report),
                "--assert-limits",
            ],
            env_ui,
        )
        # 2026-10-21：启动读数的字节码缓存条件此前既未控制也未记录，同一份源码
        # 冷/热相差约 340 ms，读数无法解释。此处只**记录**条件并在冷态下补一次
        # 参考读数；判定值仍是顶层 workspace_visible_ms，口径与阈值均不变。
        _record_startup_bytecode_condition()
        run(
            [
                sys.executable,
                "scripts/smoke_startup.py",
                "--assert-limits",
                "--output",
                str(startup_report),
            ],
            env_ui,
        )
        _annotate_startup_bytecode(startup_report)
        # FR-20260809-1301: UI 交互度量易受离屏/机器差异影响，正式门禁只做绝对
        # 上限断言（--assert-limits），不纳入 compare_performance 相对基线
        # （PERFORMANCE_BASELINE.json 无 ui section；此前 ui_report 生成但从不
        # 参与比较属"生成未用"残缺）。若未来需要 UI 相对回归，须先在真实构建机
        # 实测并增补 baseline 的 ui section 后再传入 --report ui=...。
        run(
            [
                sys.executable,
                "scripts/benchmark_ui_interaction.py",
                "--assert-limits",
            ],
            env_ui,
        )
        run(
            [
                sys.executable,
                "scripts/compare_performance.py",
                "--baseline",
                "docs/PERFORMANCE_BASELINE.json",
                "--report",
                f"core={core_report}",
                "--report",
                f"vision={vision_report}",
                "--report",
                f"native={native_report}",
                "--report",
                f"startup={startup_report}",
            ],
            env_ui,
        )
    run(
        [
            sys.executable,
            "scripts/soak_runtime.py",
            "--rounds",
            "20000",
            "--assert-limits",
        ],
        env_ui,
    )
    # 原生采集冒烟需要真实窗口（见函数顶部 C1-FUP-02 说明）。
    run([sys.executable, "scripts/smoke_native_capture.py"], env_desktop)
    run([sys.executable, "scripts/smoke_native_vision.py"], env_ui)
    run([sys.executable, "scripts/smoke_native_cancellation.py"], env_ui)
    run([sys.executable, "scripts/smoke_uia_control.py"], env_ui)
    required = [
        ROOT / "fukua_rpa_core.dll",
        ROOT / "assets" / "fukuaRPA.ico",
        ROOT / "assets" / "version_info.txt",
        ROOT / "assets" / "version_info_onefile.txt",
        ROOT / "fukuaRPA_onedir.spec",
        ROOT / "fukuaRPA_onefile.spec",
        ROOT / "scripts" / "build_release.py",
        ROOT / "scripts" / "audit_runtime_closure.py",
        ROOT / "scripts" / "create_build_record.py",
        ROOT / "scripts" / "create_sbom.py",
        ROOT / "scripts" / "create_release_info.py",
        ROOT / "scripts" / "create_quality_report.py",
        ROOT / "scripts" / "release_provenance.py",
        ROOT / "scripts" / "create_payload_hashes.py",
        ROOT / "scripts" / "create_release_manifest.py",
        ROOT / "scripts" / "create_release_readme.py",
        ROOT / "scripts" / "create_release_archive.py",
        ROOT / "scripts" / "create_release_checksums.py",
        ROOT / "scripts" / "benchmark_native_scheduler.py",
        ROOT / "scripts" / "benchmark_ui_interaction.py",
        ROOT / "scripts" / "benchmark_core.py",
        ROOT / "scripts" / "benchmark_vision.py",
        ROOT / "scripts" / "smoke_native_capture.py",
        ROOT / "scripts" / "smoke_native_cancellation.py",
        ROOT / "scripts" / "smoke_native_vision.py",
        ROOT / "scripts" / "smoke_uia_control.py",
        ROOT / "scripts" / "runtime_pruning.py",
        ROOT / "scripts" / "smoke_startup.py",
        ROOT / "scripts" / "compare_performance.py",
        ROOT / "docs" / "PERFORMANCE_BASELINE.json",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"Missing release files: {missing}")
    for spec_name in ("fukuaRPA_onedir.spec", "fukuaRPA_onefile.spec"):
        spec = (ROOT / spec_name).read_text(encoding="utf-8")
        if "upx=False" not in spec:
            raise RuntimeError(f"Release spec must keep UPX disabled: {spec_name}")
    assert_validation_inputs_unchanged(private_inputs)


_STARTUP_BYTECODE_STATE: dict[str, object] = {}


def _pyc_files():
    """Return bytecode cache files that affect source-mode startup timing."""
    roots = (ROOT / "fukua_rpa", ROOT / "scripts", ROOT / "plugins")
    found = []
    for root in roots:
        if root.is_dir():
            found.extend(root.rglob("*.pyc"))
    return found


def _source_py_files():
    """Source modules under the startup-timed roots."""
    roots = (ROOT / "fukua_rpa", ROOT / "scripts", ROOT / "plugins")
    found = []
    for root in roots:
        if root.is_dir():
            found.extend(root.rglob("*.py"))
    return found


def _cache_coverage():
    """Return (py_files, pyc_files) counts for the startup-timed roots."""
    roots = (ROOT / "fukua_rpa", ROOT / "scripts", ROOT / "plugins")
    py_count = 0
    for root in roots:
        if root.is_dir():
            py_count += sum(1 for _ in root.rglob("*.py"))
    return py_count, len(_pyc_files())


def _classify_cache(py_count: int, pyc_count: int) -> str:
    """Classify the bytecode state without assuming what a run produced.

    ``cold``: no cache at all. ``warm``: cache covers essentially every source
    file. Anything in between is ``partial``; when the source count is unknown
    the state is ``unknown``. 仅凭「存在任意 .pyc」就判定热态会把部分缓存误标。
    """
    if py_count <= 0:
        return "unknown"
    if pyc_count <= 0:
        return "cold"
    if pyc_count >= py_count:
        return "warm"
    return "partial"


def _record_startup_bytecode_condition():
    """Remember the cache state before the startup smoke runs."""
    py_count, pyc_count = _cache_coverage()
    condition = _classify_cache(py_count, pyc_count)
    _STARTUP_BYTECODE_STATE.update(
        {
            "py_count": py_count,
            "pyc_count": pyc_count,
            "condition": condition,
        }
    )
    print(
        f"startup bytecode condition: {condition} (pyc={pyc_count}/{py_count})",
        flush=True,
    )


def _annotate_startup_bytecode(report_path: Path):
    """Annotate the startup report and add a non-judged warm reference reading.

    The judged value stays at the top-level ``workspace_visible_ms`` so
    ``compare_performance.value_at_path`` keeps working unchanged. The warm
    re-run only writes ``workspace_visible_warm_ms`` and is marked as a
    reference value that never participates in the pass/fail decision.
    """
    state = dict(_STARTUP_BYTECODE_STATE)
    warm_visible = None
    if state.get("condition") == "cold":
        # 冷测本身会写出 .pyc（解释器正常行为），紧接着再跑一次即为热态参考值。
        # 不做 compileall、不做任何删除：删除保护拒绝时不得更换工具绕过。
        # 唯一文件名：失败时不可能读到上一次留下的旧报告。
        warm_report = report_path.with_name(
            f"startup-warm-{uuid.uuid4().hex}.json"
        )
        ran_ok = False
        try:
            _run_helper(
                [
                    sys.executable,
                    "scripts/smoke_startup.py",
                    "--output",
                    str(warm_report),
                ],
                env=dict(os.environ, QT_QPA_PLATFORM="offscreen"),
                timeout=600,
                on_timeout=_timeout_runtime_error,
            )
            ran_ok = True
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            # 参考读数失败不影响判定：打印警告，报告里只缺该字段。
            print(f"warning: warm startup reference failed: {exc}", flush=True)
        # 只有本次运行成功且文件确由本次产生时才读取（唯一文件名 + 存在性双条件）。
        if ran_ok and warm_report.is_file():
            warm_visible = json.loads(
                warm_report.read_text(encoding="utf-8")
            ).get("workspace_visible_ms")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    report["bytecode_condition"] = state.get("condition", "unknown")
    report["pyc_count"] = state.get("pyc_count", 0)
    report["py_count"] = state.get("py_count", 0)
    # A/B/C 协议（阶段 1）：只有缓存**通过五条校验**才记为 B，否则记 unknown，
    # 绝不因「有 .pyc」就宣称缓存有效。
    # 门禁是**原地**测量，拿不到被测进程的运行时模块集合（`sys.modules`），
    # 用目录扫描冒充会让路径断言失效、B 类判定变成假校验（R15 审查③）。
    # 因此这里只记录「无法校验」，不宣称缓存有效；真正的 B 类只由
    # scripts/run_startup_baseline.py 在隔离副本中给出。
    if state.get("condition") == "cold":
        validation = _sm.CacheValidation(ok=False, reasons=["no_cache"])
    else:
        validation = _sm.CacheValidation(ok=False, reasons=["runtime_modules_unavailable"])
    report["cache_validation"] = validation.as_dict()
    if state.get("condition") == "cold":
        report["measurement_class"] = _sm.classify_measurement(
            kind="source", cache_ok=False
        )
    else:
        # 无法证明缓存对应当前源码/解释器 → 只能是 unknown，不得记 B。
        report["measurement_class"] = "unknown"
    if warm_visible is not None:
        report["workspace_visible_warm_source"] = warm_report.name
    if warm_visible is not None:
        report["workspace_visible_warm_ms"] = warm_visible
        report["workspace_visible_warm_ms_note"] = "参考值，不参与判定"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def verify_packaged_smokes(executable: Path, timeout=120):
    # DESIGN: 启动冒烟以发布目录（executable.parent）为 cwd 启动冻结 exe，但经由
    # --startup-runtime-dir 把运行期 config.ini/日志重定向到临时目录，避免污染发布目录；
    # 下方 --self-test-file 子进程才真正以临时目录为 cwd。
    from scripts.smoke_startup import run_startup_smoke

    startup_report = run_startup_smoke(
        executable, timeout=timeout, assert_limits=True
    )
    print(json.dumps(startup_report, ensure_ascii=False, indent=2))
    with tempfile.TemporaryDirectory() as temp_dir:
        report_path = Path(temp_dir) / "self-test.json"
        # FR-20260809-1304: tree_kill 清理进程树，防止冻结 exe 派生的孙进程
        # 在超时后残留（裸 subprocess.run 只 kill 直接子进程）。
        _run_helper(
            [str(executable), "--self-test-file", str(report_path)],
            cwd=temp_dir,
            timeout=timeout,
            tree_kill=True,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        failures = [item for item in report["checks"] if not item["ok"]]
        if not report.get("ok") or failures:
            raise RuntimeError(f"Packaged self-test failed: {failures}")
        if report.get("build_name") != BUILD_NAME:
            raise RuntimeError(f"Packaged build identity mismatch: {report}")
        print(json.dumps(report, ensure_ascii=False, indent=2))

        uia_report_path = Path(temp_dir) / "uia-smoke.json"
        _run_helper(
            [str(executable), "--uia-smoke-file", str(uia_report_path)],
            cwd=temp_dir,
            timeout=timeout,
            tree_kill=True,
        )
        uia_report = json.loads(uia_report_path.read_text(encoding="utf-8"))
        if not uia_report.get("ok") or not uia_report.get(
            "foreground_unchanged"
        ):
            raise RuntimeError(f"Packaged UIA smoke failed: {uia_report}")
        if not uia_report.get("matched_bound_hwnd"):
            raise RuntimeError(
                f"Packaged UIA did not select the bound control: {uia_report}"
            )
        if not uia_report.get("set_value_ok") or not uia_report.get(
            "read_value_ok"
        ):
            raise RuntimeError(
                f"Packaged UIA text round trip failed: {uia_report}"
            )
        print(json.dumps(uia_report, ensure_ascii=False, indent=2))

        native_report_path = Path(temp_dir) / "native-smoke.json"
        _run_helper(
            [str(executable), "--native-smoke-file", str(native_report_path)],
            cwd=temp_dir,
            timeout=timeout,
            tree_kill=True,
        )
        native_report = json.loads(native_report_path.read_text(encoding="utf-8"))
        if not native_report.get("ok") or not native_report.get("parity", {}).get(
            "within_tolerance"
        ):
            raise RuntimeError(f"Packaged native smoke failed: {native_report}")
        print(json.dumps(native_report, ensure_ascii=False, indent=2))
        return report


def verify_dist(dist_dir: Path, *, allow_pending_quality=False):
    from scripts.runtime_pruning import find_prunable_runtime_paths

    executable = dist_dir / f"{BUILD_NAME}.exe"
    if not executable.is_file():
        raise RuntimeError(f"Packaged executable not found: {executable}")
    # FR-20260809-1303: 纵深防御——发布树不得包含运行期生成/本机状态文件
    # （config.ini、凭据、日志、崩溃报告、临时文件），防止本机数据或旧状态
    # 泄漏进发布包。常规路径由 PyInstaller --noconfirm 清空 dist 目录保证；
    # 此检查兜底"开发机曾手动运行 dist 下 exe 生成文件后重新打包"等残留场景。
    import fnmatch

    sensitive_patterns = (
        "config.ini",
        "credentials.dat",
        "*.log",
        "crash-report*.json",
        "*.tmp",
        "*.bak",
        # FR-20261003-608: 便携布局（ARCHITECTURE #47）运行后会在发布根生成以下
        # 本机状态文件；原列表漏检它们，使该兜底门禁与其声明目标不一致。
        "profiles_backup.json",
        "scheduled_tasks.json",
        "scheduled_task_history.json",
        "scheduled_task_logs.jsonl",
        ".fukuaRPA_session.json",
        "portable.flag",
        "session_state.json",
        # FR-20261005-004: 主日志是 `rpa_debug_log.txt`（paths.current_log_path
        # 返回值），轮转副本为 `<path>.N`，结构化日志为 `rpa_debug_log.jsonl`
        # 及其轮转副本。原列表只有 `rpa_debug_log*.jsonl`，漏检 .txt 主日志与
        # 两类轮转副本（实测 7/7 全部漏检），使本兜底门禁与其声明目标不一致。
        # 用一条 `rpa_debug_log*` 覆盖全族，避免逐扩展名枚举再次漂移。
        "rpa_debug_log*",
    )
    # 目录类残留（profiles_history/、imported_assets/、crash_reports/）不是文件，
    # 上面按 path.name 的文件匹配无法覆盖，需单独按路径片段判定。
    sensitive_dir_names = ("profiles_history", "imported_assets", "crash_reports")
    sensitive_hits = sorted(
        str(path.relative_to(dist_dir))
        for path in dist_dir.rglob("*")
        if (
            path.is_file()
            and any(fnmatch.fnmatch(path.name, pattern) for pattern in sensitive_patterns)
        )
        or (
            path.is_dir()
            and path.name in sensitive_dir_names
            and path != dist_dir
        )
    )
    if sensitive_hits:
        raise RuntimeError(
            f"发布树包含敏感/本机状态文件，拒绝发布：{sensitive_hits}"
        )
    unexpected_payloads = find_prunable_runtime_paths(dist_dir)
    if unexpected_payloads:
        raise RuntimeError(
            f"Pruned runtime payloads reappeared: {unexpected_payloads}"
        )
    build_info_path = dist_dir / "BUILD_INFO.json"
    if not build_info_path.is_file():
        raise RuntimeError(f"Build record not found: {build_info_path}")
    build_info = json.loads(build_info_path.read_text(encoding="utf-8"))
    if build_info.get("format") != "fukuaRPA_build_record":
        raise RuntimeError(f"Build record format mismatch: {build_info}")
    if build_info.get("application_version") != APP_VERSION:
        raise RuntimeError(f"Build record version mismatch: {build_info}")
    if build_info.get("build_name") != BUILD_NAME:
        raise RuntimeError(f"Build record identity mismatch: {build_info}")
    if int(build_info.get("native_core", {}).get("api_version", 0)) < NATIVE_CORE_RELEASE_VERSION:
        raise RuntimeError(f"Native core API is too old: {build_info}")
    native_abi = build_info.get("native_core", {}).get("abi", {})
    build_flags = native_abi.get("build_flags", {})
    if not native_abi.get("compatible") or native_abi.get("pointer_bits") != 64:
        raise RuntimeError(f"Native core ABI contract failed: {native_abi}")
    for flag in ("x64", "static_crt", "cpp17", "windows10_target", "msvc"):
        if not build_flags.get(flag):
            raise RuntimeError(f"Native core build flag is missing: {flag}")
    if not build_info.get("source_sha256"):
        raise RuntimeError("Build record has no source fingerprint")
    required_materials = [
        dist_dir / "LICENSE",
        dist_dir / "SBOM.cdx.json",
        dist_dir / "THIRD_PARTY_NOTICES.txt",
        dist_dir / "THIRD_PARTY_LICENSES.txt",
        dist_dir / "RELEASE_INFO.json",
        dist_dir / "QUALITY_REPORT.json",
        dist_dir / "PAYLOAD_HASHES.json",
        dist_dir / "README_RELEASE.txt",
        dist_dir / "RUNTIME_CLOSURE.json",
    ]
    missing_materials = [str(path) for path in required_materials if not path.is_file()]
    if missing_materials:
        raise RuntimeError(f"Release trust materials are missing: {missing_materials}")
    # FR-20260812 建议-打包-6: 移除死防御 _verify_archive_timestamps——
    # verify_dist 调用时序早于 archive 创建（恒早退），且
    # create_release_archive.py 已在生成后立即校验条目时间戳（等价断言）。
    run(
        [
            sys.executable,
            "scripts/audit_runtime_closure.py",
            str(dist_dir),
            "--output",
            str(dist_dir / "RUNTIME_CLOSURE.json"),
            "--check",
        ]
    )
    sbom = json.loads((dist_dir / "SBOM.cdx.json").read_text(encoding="utf-8"))
    component_names = {
        str(item.get("name", "")).casefold() for item in sbom.get("components", [])
    }
    for required_name in ("uiautomation", "comtypes", "pyside6", "opencv-python-headless"):
        if required_name.casefold() not in component_names:
            raise RuntimeError(f"SBOM is missing runtime dependency: {required_name}")
    release_info = json.loads(
        (dist_dir / "RELEASE_INFO.json").read_text(encoding="utf-8")
    )
    if release_info.get("build_name") != BUILD_NAME:
        raise RuntimeError(f"Release info identity mismatch: {release_info}")
    quality_report_path = dist_dir / "QUALITY_REPORT.json"
    quality_report = json.loads(quality_report_path.read_text(encoding="utf-8"))
    if quality_report.get("format") != "fukuaRPA_quality_report":
        raise RuntimeError(f"Quality report identity mismatch: {quality_report}")
    if quality_report.get("build_name") != BUILD_NAME:
        raise RuntimeError(f"Quality report build mismatch: {quality_report}")
    try:
        require_matching_evidence(build_info, quality_report, release_info)
    except ValueError as error:
        raise RuntimeError(f"Release evidence identity mismatch: {error}") from error
    quality_gates = quality_report.get("gates", {})
    if quality_gates.get("source", {}).get("status") != "passed":
        raise RuntimeError(f"Source quality gate is not passed: {quality_report}")
    package_status = quality_gates.get("frozen_package", {}).get("status")
    if package_status != "passed" and not (
        allow_pending_quality and package_status == "pending"
    ):
        raise RuntimeError(f"Frozen package quality gate is not passed: {quality_report}")
    from scripts.hashutil import hash_file

    if release_info.get("build_record", {}).get("sha256") != hash_file(
        build_info_path
    ):
        raise RuntimeError("Release info build record hash mismatch")
    if quality_report.get("build_record", {}).get("sha256") != hash_file(
        build_info_path
    ):
        raise RuntimeError("Quality report build record hash mismatch")

    if release_info.get("executable", {}).get("sha256") != hash_file(executable):
        raise RuntimeError("Release info executable hash mismatch")
    if release_info.get("sbom", {}).get("sha256") != hash_file(
        dist_dir / "SBOM.cdx.json"
    ):
        raise RuntimeError("Release info SBOM hash mismatch")
    if release_info.get("runtime_closure", {}).get("sha256") != hash_file(
        dist_dir / "RUNTIME_CLOSURE.json"
    ):
        raise RuntimeError("Release info runtime closure hash mismatch")
    if release_info.get("quality_report", {}).get("sha256") != hash_file(
        quality_report_path
    ):
        raise RuntimeError("Release info quality report hash mismatch")
    code_payloads = {
        str(item.get("role")): item
        for item in release_info.get("code_payloads", [])
        if isinstance(item, dict)
    }
    for role in ("main_executable", "native_core", "uia_bitmap_helper"):
        item = code_payloads.get(role)
        if not item:
            raise RuntimeError(f"Release info is missing code payload: {role}")
        relative = PurePosixPath(str(item.get("path", "")))
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or any(":" in part for part in relative.parts)
        ):
            raise RuntimeError(f"Release info has unsafe code payload path: {role}")
        payload_path = dist_dir.joinpath(*relative.parts).resolve()
        try:
            payload_path.relative_to(dist_dir.resolve())
        except ValueError as error:
            raise RuntimeError(
                f"Release info code payload escapes release directory: {role}"
            ) from error
        if not payload_path.is_file() or item.get("sha256") != hash_file(payload_path):
            raise RuntimeError(f"Release code payload hash mismatch: {role}")
    if code_payloads["native_core"].get("sha256") != build_info.get(
        "native_core", {}
    ).get("sha256"):
        raise RuntimeError("Packaged native core does not match build record")

    sbom_metadata = sbom.get("metadata", {})
    sbom_component = sbom_metadata.get("component", {})
    sbom_properties = {
        str(item.get("name")): str(item.get("value"))
        for item in sbom_metadata.get("properties", [])
        if isinstance(item, dict)
    }
    if sbom_component.get("version") != APP_VERSION.lstrip("v"):
        raise RuntimeError("SBOM application version mismatch")
    if sbom_properties.get("fukuaRPA.build_name") != BUILD_NAME:
        raise RuntimeError("SBOM build name mismatch")
    if sbom_properties.get("fukuaRPA.build_id") != build_info.get("build_id"):
        raise RuntimeError("SBOM build identity mismatch")
    if sbom_properties.get("fukuaRPA.source_sha256") != build_info.get(
        "source_sha256"
    ):
        raise RuntimeError("SBOM source fingerprint mismatch")
    from fukua_rpa.integrity import verify_payload

    payload_report = verify_payload(dist_dir)
    if not payload_report.get("ok"):
        raise RuntimeError(f"Packaged payload verification failed: {payload_report}")
    verify_packaged_smokes(executable)


def verify_onefile(executable: Path):
    executable = executable.resolve()
    if not executable.is_file():
        raise RuntimeError(f"Packaged onefile executable not found: {executable}")
    if executable.name.casefold() != f"{ONEFILE_BUILD_NAME}.exe".casefold():
        raise RuntimeError(f"Unexpected onefile executable name: {executable.name}")

    from scripts.audit_runtime_closure import (
        AMD64_MACHINE,
        classify_dependency,
        pe_imports,
        system_dll_names,
    )

    machine, imports = pe_imports(executable)
    if machine != AMD64_MACHINE:
        raise RuntimeError(f"Onefile bootloader is not AMD64: 0x{machine:04X}")
    package_index = {executable.name.casefold(): [executable.name]}
    system_names = system_dll_names()
    unresolved = [
        dependency
        for dependency in imports
        if classify_dependency(
            dependency,
            package_index,
            system_names,
        )
        == "unresolved"
    ]
    if unresolved:
        raise RuntimeError(
            f"Onefile bootloader has unresolved imports: {sorted(unresolved)}"
        )

    report = verify_packaged_smokes(executable, timeout=180)
    if report.get("runtime_directory_role") != "executable_directory":
        raise RuntimeError(
            "Onefile writable data path escaped the executable directory: "
            f"role={report.get('runtime_directory_role')}"
        )
    if not report.get("frozen"):
        raise RuntimeError("Onefile diagnostics did not report a frozen runtime")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dist", type=Path, help=f"Path to the built {BUILD_NAME} directory"
    )
    parser.add_argument(
        "--onefile",
        type=Path,
        help=f"Path to the built {ONEFILE_BUILD_NAME}.exe",
    )
    parser.add_argument(
        "--skip-source",
        action="store_true",
        help="Skip source quality gates when they already ran in this build job.",
    )
    parser.add_argument(
        "--allow-pending-quality",
        action="store_true",
        help="Internal two-phase build option used before frozen smokes complete.",
    )
    args = parser.parse_args()
    # DESIGN (SCR01): ``--skip-source`` 会连带跳过 verify_source() 内的 UPX 禁用
    # 检查（fukuaRPA_onedir/onefile.spec 必须含 ``upx=False``，见 :317）。这是**有意**
    # 取舍：UPX 检查属于"源码/构建配置门禁"，与源码质量门禁共享 verify_source 入口。
    # FR-20260811-D15-011（措辞更正）：普通（development）构建路径**从未**执行 UPX
    # 检查——build_release 的 verify_source_scope(formal_release=False) 仅跑
    # run_impacted_tests，且 build_onedir 以 ``--skip-source`` 调用本脚本再次跳过
    # verify_source。UPX 检查仅在正式发布流程（不传 --skip-source）执行；普通构建
    # 依赖 spec 自带 ``upx=False`` 与打包后 smoke 兜底（ARCHITECTURE #22 允许）。
    if not args.skip_source:
        verify_source()
    if args.dist:
        verify_dist(
            args.dist.resolve(),
            allow_pending_quality=args.allow_pending_quality,
        )
    if args.onefile:
        verify_onefile(args.onefile.resolve())
    print("Release verification passed.")


if __name__ == "__main__":
    main()
