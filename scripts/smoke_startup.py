"""Launch fukuaRPA once and validate staged-startup timing markers."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lib import run_helpers  # noqa: E402


def build_startup_smoke_environment(base_environment=None):
    """Return an isolated startup-smoke environment.

    The real application launches a detached crash watchdog which deliberately
    outlives the parent for a short clean-exit settling period.  A startup smoke
    uses a disposable runtime directory, so that expected lifetime would race
    the temporary-directory cleanup.  Crash-watchdog behavior has its own
    integration tests; this smoke measures the application startup stages.
    """

    env = dict(os.environ if base_environment is None else base_environment)
    env["FUKUA_CRASH_WATCHDOG"] = "1"
    return env


def validate_report(report, *, assert_limits=False):
    if not report.get("ok"):
        raise RuntimeError(f"Startup smoke failed: {report}")
    timing_names = (
        "python_to_shell_first_paint_ms",
        "workspace_import_ms",
        "workspace_visible_ms",
        "runtime_ready_ms",
        "runtime_backend_ms",
    )
    timings = {}
    for name in timing_names:
        try:
            value = float(report[name])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(f"Startup smoke has no valid {name}: {report}") from error
        # SAFETY: fukuaRPA.py 兜底写入 -1.0 表示冻结包 paintEvent 未触发，属合法哨兵。
        if name == "python_to_shell_first_paint_ms" and value == -1.0:
            timings[name] = value
            continue
        if value < 0.0:
            raise RuntimeError(f"Startup smoke has a negative {name}: {report}")
        timings[name] = value
    first_paint = timings["python_to_shell_first_paint_ms"]
    if first_paint != -1.0 and timings["workspace_visible_ms"] < first_paint:
        # 首帧哨兵生效时跳过依赖首帧的排序断言
        raise RuntimeError(f"Workspace appeared before the startup shell: {report}")
    if timings["runtime_ready_ms"] < timings["workspace_visible_ms"]:
        raise RuntimeError(f"Runtime initialized before the workspace handoff: {report}")
    if assert_limits:
        limits = {
            "workspace_visible_ms": 15_000.0,
            "runtime_ready_ms": 30_000.0,
        }
        if first_paint != -1.0:
            limits["python_to_shell_first_paint_ms"] = 5_000.0
        exceeded = {
            name: (timings[name], limit)
            for name, limit in limits.items()
            if timings[name] > limit
        }
        if exceeded:
            raise RuntimeError(f"Startup timing limit exceeded: {exceeded}")
    return timings


def run_startup_smoke(executable=None, *, timeout=60, assert_limits=False):
    env = build_startup_smoke_environment()
    if executable:
        command = [str(Path(executable).resolve())]
        cwd = Path(executable).resolve().parent
    else:
        env["QT_QPA_PLATFORM"] = "offscreen"
        command = [sys.executable, str(ROOT / "fukuaRPA.py")]
        cwd = ROOT
    with tempfile.TemporaryDirectory() as temp_dir:
        report_path = Path(temp_dir) / "startup-smoke.json"
        runtime_dir = Path(temp_dir) / "runtime"
        try:
            # FR-20260809-1304: 用 run_helpers.run（tree_kill=True）替代裸
            # subprocess.run——超时先 taskkill /T 清理整个进程树再 kill 直接子
            # 进程，避免 frozen exe 派生的孙进程（GUI/子执行器）在超时后残留
            # 并持有临时目录。
            run_helpers.run(
                [
                    *command,
                    "--startup-smoke-file",
                    str(report_path),
                    "--startup-runtime-dir",
                    str(runtime_dir),
                ],
                cwd=cwd,
                env=env,
                timeout=max(5, int(timeout)),
                tree_kill=True,
            )
        except subprocess.TimeoutExpired:
            raise SystemExit(f"FAIL: 启动超时（>{timeout}s）") from None
        report = json.loads(report_path.read_text(encoding="utf-8"))
    validate_report(report, assert_limits=assert_limits)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--assert-limits", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_startup_smoke(
        args.executable,
        timeout=args.timeout,
        assert_limits=args.assert_limits,
    )
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    print(serialized)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
