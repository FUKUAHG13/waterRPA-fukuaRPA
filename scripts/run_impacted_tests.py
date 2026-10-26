"""Run conservative source checks selected from changed repository paths."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Bootstrap wrapper that guarantees a process-lifetime QApplication before any
# UI test module is imported, so batch runs never hit the native
# "QWidget without QApplication" abort (see scripts/run_unittest_qt.py).
QT_UNITTEST_BOOTSTRAP = ROOT / "scripts" / "run_unittest_qt.py"

from scripts.lib.run_helpers import run as _run_helper  # noqa: E402
from scripts.test_impact import (  # noqa: E402
    collect_changed_paths,
    discover_plugin_test_dirs,
    module_path,
    plan_impacted_tests,
    split_modules_by_runner,
)


class _ModuleTimeout(Exception):
    """单模块超时，不中止整批。"""


def run(command, env):
    # 统一到 scripts/lib/run_helpers.run（§3.5(c)）；保留测试侧 1200s 超时。
    # 超时抛 _ModuleTimeout 而非 SystemExit，使 _run_modules_isolated 能捕获
    # 单模块超时并继续执行后续模块（S10）。
    _run_helper(command, env=env, timeout=1200, on_timeout=_timeout_raise)


def _timeout_raise(cmd, proc, exc):
    proc.kill()
    raise _ModuleTimeout(
        f"Command timed out after 1200s: {subprocess.list2cmdline(cmd)}"
    )


def _run_modules_isolated(modules, env, *, exit_on_failure=True):
    """Run each impacted unittest module in its own isolated subprocess.

    Previously all impacted modules were passed to a single ``unittest`` process
    (one shared QApplication bootstrap). That shared process leaked global
    state — environment variables, subprocess exit codes, logging/translation
    state — between modules, producing batch-only "false positive" failures in
    modules like ``test_repository_hygiene`` and ``test_startup_architecture``
    that pass cleanly when run alone.

    Running each module in its own process mirrors the standard ``unittest``
    default (one process per module) and fully isolates those side effects.
    All modules run before the gate fails, so the full failure list is visible.

    ``exit_on_failure=False`` 让调用方自行聚合失败：unittest 与 pytest 两个通道
    都要跑完再一次性判定，否则 unittest 侧先 sys.exit 会把 pytest 通道整段吞掉，
    使用者只能看到半张失败清单。
    """
    failed = []
    for module in modules:
        try:
            run([sys.executable, str(QT_UNITTEST_BOOTSTRAP), "-v", module], env=env)
        except subprocess.CalledProcessError:
            failed.append(module)
        except _ModuleTimeout as exc:
            print(f"  [TIMEOUT] {module}: {exc}")
            failed.append(module)
    if failed and exit_on_failure:
        sys.exit(1)
    return failed


def run_pytest_modules(modules, env) -> bool:
    """用 pytest 执行 unittest 收集不到的测试模块；成功返回 True。

    标准 unittest 只收集 ``unittest.TestCase`` 子类，完全看不见顶层
    ``def test_*`` 函数；而它在零收集时退出码仍为 0。两者叠加使这类模块长期
    「静默零执行」。这里补上 pytest 通道，把它们真正跑起来。
    分流规则见 scripts/test_impact.py::split_modules_by_runner。

    单进程批量执行（而非像 unittest 侧那样逐模块隔离）：pytest 自身会汇总并
    打印全部失败，一次进程即可拿到完整清单；若将来出现跨模块状态串扰，再退化
    为逐模块隔离。
    """
    files = [module_path(module).relative_to(ROOT).as_posix() for module in modules]
    if not files:
        return True
    # flush=True：父进程 stdout 被重定向到文件时是块缓冲的，而子进程直接写同一个
    # fd。不 flush 会让这行横幅在日志里飘到几百行之后，看上去像是 pytest 在别的
    # 模块中间跑了一遍——排查时极易误判。
    print(f"> pytest {' '.join(files)}", flush=True)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *files,
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
    ]
    try:
        subprocess.run(cmd, cwd=ROOT, env=env, check=True, timeout=1200)
    except subprocess.CalledProcessError:
        print("[test_impact] pytest 通道失败（顶层 def test_* 测试模块）")
        return False
    except subprocess.TimeoutExpired:
        print("[test_impact] pytest 通道超时（1200s）")
        return False
    except FileNotFoundError:
        # 与插件测试不同，这里不能降级跳过：跳过就等于回到「静默零执行」。
        print("[test_impact] 未找到 pytest，无法执行顶层函数测试模块")
        return False
    return True


def run_plugin_tests(dirs, env, strict=False):
    """Run plugin unit tests via pytest for each affected plugin test dir.

    Plugin tests live outside the unittest namespace, so they run with pytest.
    A plugin-test failure is *non-fatal* by default (reported as a warning) so a
    missing optional dependency in a given environment never breaks the build;
    pass ``strict=True`` to make them a hard gate for local development
    (review item #3).
    """

    if not dirs:
        return
    for d in dirs:
        dstr = str(d)
        print(f"> pytest {dstr}", flush=True)
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            dstr,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "--import-mode=importlib",
        ]
        try:
            subprocess.run(cmd, cwd=ROOT, env=env, check=True, timeout=600)
        except subprocess.CalledProcessError as exc:
            if strict:
                raise SystemExit(f"插件测试失败：{dstr} ({exc})") from exc
            print(
                f"[test_impact] 插件测试未通过（非致命，视为环境/可选依赖问题）：{dstr}"
            )
        except subprocess.TimeoutExpired as exc:
            # FR-20260809-1308: 插件测试挂起超时（600s）必须被捕获，与失败
            # 同语义处理——修复前 TimeoutExpired 未捕获，直接穿透使构建中断
            # 且无明确错误信息。
            if strict:
                raise SystemExit(f"插件测试超时：{dstr}（>{exc.timeout}s）") from exc
            print(f"[test_impact] 插件测试超时（非致命）：{dstr}")
        except FileNotFoundError as exc:
            # FR-010: ARCHITECTURE #68 要求严格模式"失败**或缺失**均阻断"。
            # 修复前此分支在 strict 下仍 print 后 return，插件测试可静默零执行，
            # 与上方 CalledProcessError / TimeoutExpired 的 strict 处理不一致；
            # 三条路径现统一为 strict 即阻断。非 strict 维持"视为环境/可选依赖
            # 问题"的宽容语义（本地跑测试缺 pytest 不应中断）。
            if strict:
                raise SystemExit(
                    f"未找到 pytest，无法执行插件测试：{dstr}"
                ) from exc
            print("[test_impact] 未找到 pytest，跳过插件测试")
            return


def run_short_benchmarks(suites, env):
    selected = set(suites)
    if "performance" in selected and not selected.intersection(
        {"workflow", "vision", "ui"}
    ):
        selected.update({"workflow", "vision", "ui"})
    if "workflow" in selected:
        run(
            [
                sys.executable,
                "scripts/benchmark_core.py",
                "--rounds",
                "3",
                "--assert-limits",
            ],
            env,
        )
    if "vision" in selected:
        run(
            [
                sys.executable,
                "scripts/benchmark_vision.py",
                "--rounds",
                "3",
                "--assert-limits",
            ],
            env,
        )
        run(
            [
                sys.executable,
                "scripts/benchmark_native_scheduler.py",
                "--rounds",
                "3",
                "--assert-limits",
            ],
            env,
        )
    if "ui" in selected:
        run(
            [
                sys.executable,
                "scripts/benchmark_ui_interaction.py",
                "--iterations",
                "80",
                "--tasks",
                "150",
                "--assert-limits",
            ],
            env,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="Changed repository path; repeat to override automatic Git detection.",
    )
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--tests-only", action="store_true")
    parser.add_argument(
        "--plugins-strict",
        action="store_true",
        help="Make plugin-test failures fatal (default: non-fatal, environment-tolerant)",
    )
    args = parser.parse_args()

    paths = tuple(args.path) if args.path else collect_changed_paths()
    plan = plan_impacted_tests(paths)
    payload = {
        "paths": list(plan.paths),
        "suites": list(plan.suites),
        "modules": list(plan.modules),
        "full_suite": plan.full_suite,
        "reasons": list(plan.reasons),
    }
    if args.json or args.list_only:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        scope = "full unit suite" if plan.full_suite else ", ".join(plan.suites) or "direct tests"
        print(f"Impacted test scope: {scope}")
        for reason in plan.reasons:
            print(f"  - {reason}")
    # FR-20260812 建议-体验-5: --json 隐含 --list-only——JSON 输出用于机器
    # 消费（CI 上报/工具解析），只打印计划不应顺带执行测试，避免副作用。
    if args.list_only or args.json:
        return

    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    if plan.modules:
        # 按模块实际写法分流：TestCase → unittest（逐模块隔离进程）；
        # 顶层 def test_* → pytest。两个通道都跑完再一次性判定，
        # 避免只看到半张失败清单。
        unittest_modules, pytest_modules = split_modules_by_runner(plan.modules)
        failed = _run_modules_isolated(unittest_modules, env, exit_on_failure=False)
        pytest_ok = run_pytest_modules(pytest_modules, env)
        if failed or not pytest_ok:
            if failed:
                print("以下 unittest 模块失败：", flush=True)
                for module in failed:
                    print(f"  - {module}")
            sys.exit(1)

    # Plugin unit tests (pytest, outside the unittest namespace). Run impacted
    # dirs when selected by the impact plan; on a full-suite run, run them all.
    plugin_dirs = list(plan.plugin_test_dirs)
    if plan.full_suite:
        plugin_dirs = [str(d) for d in discover_plugin_test_dirs()]
    if plugin_dirs:
        run_plugin_tests(plugin_dirs, env, strict=args.plugins_strict)

    if not args.tests_only:
        run([sys.executable, "-m", "compileall", "-q", "fukua_rpa", "tests", "scripts"], env)
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
                "--select",
                "F,E9,E722",
            ],
            env,
        )
        run_short_benchmarks(plan.suites, env)


if __name__ == "__main__":
    main()
