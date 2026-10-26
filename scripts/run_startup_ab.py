"""Paired A/B startup measurement for a source-level dependency change.

Reference arm = a frozen copy of the tree *before* the change; candidate arm =
the tree *after* it. Both arms are whitelist copies without user data, and the
same DLL is used for both. Cache conditions and the contamination rule are
reused from ``scripts/startup_measurement.py`` unchanged.

Usage:
    python scripts/run_startup_ab.py --reference <dir> --candidate <dir> \
        --out <dir> [--pairs 5] [--conditions a,b] [--first-open]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.startup_measurement import (  # noqa: E402
    PRIOR_REFERENCE_MS,
    is_contaminated,
    validate_cache,
)

PYTHON = sys.executable
WHITELIST_DIRS = ("fukua_rpa", "assets", "plugins")
WHITELIST_FILES = ("fukuaRPA.py", "fukua_rpa_core.dll")
METRICS = (
    "workspace_import_ms",
    "workspace_visible_ms",
    "runtime_ready_ms",
    "runtime_backend_ms",
)
FIRST_OPEN_PROBE = r"""
import json, os, time
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
started = time.perf_counter()
from fukua_rpa.ui.main_window import RPAWindow  # noqa: F401 - 与真实启动同序
import fukua_rpa.ui.task_row as task_row
row = task_row.TaskRow(lambda _row: None)
row_ready = time.perf_counter()
row.open_custom_config()
opened = time.perf_counter()
print(json.dumps({
    "row_ms": (row_ready - started) * 1000.0,
    "first_open_ms": (opened - row_ready) * 1000.0,
    "dialog": type(getattr(row, "config_dialog", None)).__name__,
}))
"""


def clean_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONPYCACHEPREFIX"):
        env.pop(key, None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


def _validate_copy_paths(sources, destinations, dll: Path) -> None:
    """任何写入前保护两臂输入、DLL和真实路径，禁止复制目录链接形成递归。"""

    sources = tuple(dict.fromkeys(Path(source).resolve() for source in sources))
    destinations = tuple(Path(destination).resolve() for destination in destinations)
    dll = dll.resolve()
    if not dll.is_file():
        raise ValueError(f"测量 DLL 不存在：{dll}")
    for source in sources:
        if not source.is_dir():
            raise ValueError(f"测量输入目录不存在：{source}")
        for destination in destinations:
            if source == destination or source in destination.parents or destination in source.parents:
                raise ValueError(f"测量输入与输出路径重叠：{source} / {destination}")
        for name in WHITELIST_DIRS:
            origin = source / name
            if origin.is_symlink() or origin.is_junction():
                raise ValueError(f"测量输入白名单不支持目录链接：{origin}")
            if not origin.is_dir():
                continue
            for parent, directories, filenames in os.walk(origin, followlinks=False):
                for child in (*directories, *filenames):
                    path = Path(parent) / child
                    if path.is_symlink() or path.is_junction():
                        raise ValueError(f"测量输入白名单不支持链接：{path}")
        entry = source / "fukuaRPA.py"
        if entry.is_symlink():
            raise ValueError(f"测量入口不支持文件链接：{entry}")
    for destination in destinations:
        if dll == destination or dll in destination.parents or destination in dll.parents:
            raise ValueError(f"测量输出与输入 DLL 路径重叠：{destination} / {dll}")


def build_copy(source: Path, destination: Path, *, precompile: bool, dll: Path) -> Path:
    """Whitelist copy into a new directory; never remove an existing object."""

    source, destination, dll = source.resolve(), destination.resolve(), dll.resolve()
    _validate_copy_paths((source,), (destination,), dll)
    # SAFETY: 已有目录可能是冻结源码或历史证据，拒绝覆盖，不能猜测它可被清理。
    destination.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in WHITELIST_DIRS:
        origin = source / name
        if origin.is_dir():
            shutil.copytree(origin, destination / name, ignore=ignore)
    for name in WHITELIST_FILES:
        origin = dll if name.endswith(".dll") else source / name
        if origin.is_file():
            shutil.copy2(origin, destination / name)
    if precompile:
        # 与既有协议一致：编译所有会参与运行的包；漏掉 plugins 会让校验把
        # 「没编译」误报成缓存缺失（独立 B 审查 P3）。
        targets = [name for name in ("fukua_rpa", "plugins") if (destination / name).is_dir()]
        if (destination / "fukuaRPA.py").is_file():
            targets.append("fukuaRPA.py")
        subprocess.run(
            [PYTHON, "-m", "compileall", "-q", *targets],
            cwd=destination,
            check=False,
            env=clean_env(),
        )
    return destination


def sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


RUNTIME_STATE = (
    "config.ini",
    "profiles_backup.json",
    "profiles_history",
    "crash_reports",
    "logs",
    "scheduled_task_logs.jsonl",
)


def reset_runtime_state(copy_root: Path) -> None:
    """Remove state written by a previous smoke run (user config is never copied)."""

    for name in RUNTIME_STATE:
        target = copy_root / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()


def run_smoke(copy_root: Path, report: Path, env: dict[str, str]) -> dict:
    reset_runtime_state(copy_root)
    if report.exists():
        report.unlink()
    subprocess.run(
        [PYTHON, "fukuaRPA.py", "--startup-smoke-file", str(report)],
        cwd=copy_root,
        env=env,
        check=False,
        timeout=300,
    )
    if not report.exists():
        raise RuntimeError("smoke report missing")
    data = json.loads(report.read_text(encoding="utf-8"))
    if data.get("ok") is not True:
        raise RuntimeError(f"smoke not ok: {data.get('error')!r}")
    return {name: float(data[name]) for name in METRICS}


def validate_b_condition(copy_root: Path, env: dict[str, str]) -> dict:
    """Validate the precompiled cache over **every** source file in the copy.

    ``import fukua_rpa`` alone only covers a handful of modules, which makes the
    five cache rules look satisfied while most of the startup closure was never
    checked (独立 B 审查 P3)。副本已用 ``compileall`` 全树预编译，因此直接校验
    副本内全部 ``.py``，比「只数文件」强。
    """

    probe = subprocess.run(
        [PYTHON, "-c", "import fukua_rpa;print(fukua_rpa.__file__)"],
        cwd=copy_root,
        capture_output=True,
        text=True,
        env=env,
    )
    runtime_file = (probe.stdout or "").strip()
    files = sorted(
        path
        for path in copy_root.rglob("*.py")
        if "__pycache__" not in path.parts
        and path.relative_to(copy_root).parts[0] in ("fukua_rpa", "plugins")
    )
    validation = validate_cache(files, runtime_file=runtime_file, copy_root=copy_root)
    return {
        "runtime_file": runtime_file,
        "checked_files": len(files),
        "validation": validation.as_dict(),
    }


def measure_first_open(copy_root: Path) -> dict:
    completed = subprocess.run(
        [PYTHON, "-c", FIRST_OPEN_PROBE],
        cwd=copy_root,
        env=clean_env(PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr[-2000:])
    return json.loads(completed.stdout.strip().splitlines()[-1])


def run_pair(run_ref, run_cand, *, order: tuple[str, str]) -> tuple[list[dict], list[str]]:
    """Run one pair in the given arm order; return samples and discard reasons."""

    samples: list[dict] = []
    reasons: list[str] = []
    for arm in order:
        try:
            values = run_ref() if arm == "ref" else run_cand()
        except Exception as error:  # noqa: BLE001 - 任何异常都算污染
            reasons.append(f"{arm}:error:{type(error).__name__}")
            break
        reason = is_contaminated(values["workspace_visible_ms"], PRIOR_REFERENCE_MS)
        if reason:
            reasons.append(f"{arm}:{reason}")
        samples.append({"arm": arm, **values})
    return samples, reasons


def summarize(samples: list[dict]) -> dict:
    out = {}
    for arm in ("ref", "cand"):
        rows = [s for s in samples if s["arm"] == arm]
        if not rows:
            continue
        out[arm] = {
            "count": len(rows),
            "medians": {
                metric: statistics.median(s[metric] for s in rows) for metric in METRICS
            },
            "ranges": {
                metric: [min(s[metric] for s in rows), max(s[metric] for s in rows)]
                for metric in METRICS
            },
        }
    if "ref" in out and "cand" in out:
        out["delta_median"] = {
            metric: out["ref"]["medians"][metric] - out["cand"]["medians"][metric]
            for metric in METRICS
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--pairs", type=int, default=5)
    parser.add_argument("--conditions", default="a,b")
    parser.add_argument("--dll", default=str(ROOT / "fukua_rpa_core.dll"))
    parser.add_argument("--first-open", action="store_true")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    dll = Path(args.dll).resolve()
    reference = Path(args.reference).resolve()
    candidate = Path(args.candidate).resolve()
    conditions = [item.strip() for item in args.conditions.split(",")]
    if any(condition not in {"a", "b"} for condition in conditions) or len(set(conditions)) != len(conditions):
        parser.error("conditions 只接受不重复的 a、b 条件")
    destinations = [out / f"{arm}-{condition.upper()}" for condition in conditions for arm in ("ref", "cand")]
    if args.first_open:
        destinations.extend(out / f"first-open-{arm}-{index + 1}" for arm in ("ref", "cand") for index in range(args.pairs))
    try:
        _validate_copy_paths((reference, candidate), (out, *destinations), dll)
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise FileExistsError(f"测量输出必须是新目录或空目录：{out}")
        if any(destination.exists() for destination in destinations):
            raise FileExistsError("测量副本目录已存在，请选择新输出目录")
    except (ValueError, FileExistsError) as error:
        parser.error(str(error))
    out.mkdir(parents=True, exist_ok=True)

    session = {
        "reference": str(reference),
        "candidate": str(candidate),
        "dll": {"path": str(dll), "sha256": sha256(dll)},
        "pairs": args.pairs,
        "conditions": conditions,
        "python": PYTHON,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    (out / "session.json").write_text(
        json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    results: dict[str, dict] = {}
    for condition in conditions:
        precompile = condition == "b"
        ref_env = clean_env(PYTHONDONTWRITEBYTECODE="1")
        cand_env = clean_env(PYTHONDONTWRITEBYTECODE="1")
        ref_copy = build_copy(
            reference, out / f"ref-{condition.upper()}", precompile=precompile, dll=dll
        )
        cand_copy = build_copy(
            candidate, out / f"cand-{condition.upper()}", precompile=precompile, dll=dll
        )
        payload: dict = {"condition": condition}
        if precompile:
            payload["cache_validation"] = {
                "ref": validate_b_condition(ref_copy, ref_env),
                "cand": validate_b_condition(cand_copy, cand_env),
            }
        else:
            payload["preconditions"] = {
                "ref_pyc": len(list(ref_copy.rglob("*.pyc"))),
                "cand_pyc": len(list(cand_copy.rglob("*.pyc"))),
                "writes_disabled": True,
            }

        counter = {"ref": 0, "cand": 0}

        def run_ref(
            copy_root=ref_copy, env=ref_env, tag=condition, counts=counter
        ) -> dict:
            counts["ref"] += 1
            return run_smoke(
                copy_root, out / f"smoke-{tag}-ref-{counts['ref']}.json", env
            )

        def run_cand(
            copy_root=cand_copy, env=cand_env, tag=condition, counts=counter
        ) -> dict:
            counts["cand"] += 1
            return run_smoke(
                copy_root, out / f"smoke-{tag}-cand-{counts['cand']}.json", env
            )

        pairs: list[dict] = []
        samples: list[dict] = []
        unstable = False
        for index in range(args.pairs):
            order = ("ref", "cand") if index % 2 == 0 else ("cand", "ref")
            attempt_samples, reasons = run_pair(run_ref, run_cand, order=order)
            if reasons:
                # 整对丢弃并只重采一次（协议：不得只挑好样本）。
                attempt_samples, reasons = run_pair(run_ref, run_cand, order=order)
                unstable = unstable or bool(reasons)
            pairs.append(
                {
                    "pair": index + 1,
                    "order": list(order),
                    "discarded": reasons,
                    "samples": attempt_samples,
                }
            )
            if not reasons:
                samples.extend(attempt_samples)
        payload["pairs"] = pairs
        payload["samples"] = samples
        payload["unstable"] = unstable
        payload["summary"] = summarize(samples)
        results[condition] = payload
        (out / f"result-{condition}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps({condition: payload["summary"]}, ensure_ascii=False, indent=2))

    if args.first_open:
        first_open = {}
        for label, source in (("ref", reference), ("cand", candidate)):
            samples = []
            for index in range(args.pairs):
                copy_root = build_copy(
                    source,
                    out / f"first-open-{label}-{index + 1}",
                    precompile=False,
                    dll=dll,
                )
                samples.append(measure_first_open(copy_root))
            first_open[label] = {
                "samples": samples,
                "median_first_open_ms": statistics.median(
                    s["first_open_ms"] for s in samples
                ),
                "median_row_ms": statistics.median(s["row_ms"] for s in samples),
                "dialogs": sorted({s["dialog"] for s in samples}),
            }
        (out / "first-open.json").write_text(
            json.dumps(first_open, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(first_open, ensure_ascii=False, indent=2))

    (out / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
