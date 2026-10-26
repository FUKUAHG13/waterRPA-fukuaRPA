"""启动测量的协议化组件（2026-10-21 协议迁移，阶段 1）。

本模块只提供**可独立测试**的能力，不改变现有门禁行为：

- ``MEASUREMENT_CLASSES`` / ``classify_measurement``：A / B / C 三类的记录口径；
- ``validate_cache``：B 类五条缓存有效性校验（cache_tag、头内嵌、魔数、覆盖率、路径断言）；
- ``build_isolated_copy``：构造可运行的隔离副本（A 排除缓存并禁写；B 预编译并校验）；
- ``collect_samples``：固定 5 次采样、取中位数、按事先写死的判据判定污染。

协议出处：``TIMELINE-20261020/10_启动测量协议标准化建议.md``。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

#: A = 源码无缓存；B = 源码预编译缓存；C = 冻结包（按打包格式再分）。
MEASUREMENT_CLASSES = ("startup_source_nocache", "startup_source_cached", "startup_frozen")

#: 固定采样与污染判据（事先写死，不因失败临时调整）。
SAMPLES_PER_CONDITION = 5
CONTAMINATION_UPPER_RATIO = 2.0
CONTAMINATION_LOWER_RATIO = 0.2
MAX_RESAMPLE_ROUNDS = 1
#: 首轮建基线时尚无历史中位数，固定用该先验值作比较基准。
PRIOR_REFERENCE_MS = 841.424


@dataclass
class CacheValidation:
    """B 类缓存校验结果；``ok`` 为 False 时不得给出 B 读数。"""

    ok: bool
    reasons: list[str] = field(default_factory=list)
    checked: int = 0
    missing: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reasons": self.reasons,
            "checked": self.checked,
            "missing": self.missing[:10],
            "stale": self.stale[:10],
        }


def classify_measurement(*, kind: str, cache_ok: bool | None = None) -> str:
    """Map a run to one of :data:`MEASUREMENT_CLASSES`.

    ``kind`` is ``"source"`` or ``"frozen"``. Source runs with a validated
    cache are B; source runs without any cache are A; source runs whose cache
    failed validation are **not** classifiable and return ``"unknown"``.
    """
    if kind == "frozen":
        return "startup_frozen"
    if kind != "source":
        return "unknown"
    if cache_ok is None:
        return "unknown"
    return "startup_source_cached" if cache_ok else "startup_source_nocache"


def _expected_cache_path(py_path: Path) -> Path:
    return Path(importlib.util.cache_from_source(str(py_path)))


def _validate_one(py_path: Path) -> tuple[str, str]:
    """Return ``(status, detail)`` for one source file's cache.

    status: ``ok`` / ``missing`` / ``stale`` / ``magic``.
    """
    cache_path = _expected_cache_path(py_path)
    if not cache_path.is_file():
        return "missing", str(cache_path)
    raw = cache_path.read_bytes()
    if len(raw) < 16:
        return "stale", str(cache_path)
    if raw[:4] != importlib.util.MAGIC_NUMBER:
        return "magic", str(cache_path)
    flags = int.from_bytes(raw[4:8], "little")
    if flags & 0b1:  # hash-based pyc：比对 source_hash
        stored = raw[8:16]
        if stored != importlib.util.source_hash(py_path.read_bytes()):
            return "stale", str(cache_path)
        return "ok", ""
    # timestamp 型：头内嵌源 mtime + size 必须与源文件精确相等
    stored_mtime = int.from_bytes(raw[8:12], "little")
    stored_size = int.from_bytes(raw[12:16], "little")
    stat = py_path.stat()
    if stored_mtime != int(stat.st_mtime) or stored_size != stat.st_size:
        return "stale", str(cache_path)
    return "ok", ""


def validate_cache(py_files: list[Path], *, runtime_file: str | None = None,
                   copy_root: Path | None = None) -> CacheValidation:
    """Validate that every runtime ``.py`` has a usable, current ``.pyc``.

    Args:
        py_files: Modules actually imported at startup (``sys.modules`` with a
            real ``.py`` ``__file__``), not a ``rglob`` of the whole tree.
        runtime_file: ``fukua_rpa.__file__`` observed in the measured process.
        copy_root: Root of the isolated copy; ``runtime_file`` must live inside.
    """
    result = CacheValidation(ok=True)
    if not py_files:
        # 空清单不能当作「全部有效」：没有可校验模块就无法证明缓存覆盖启动路径。
        result.ok = False
        result.reasons.append("no_modules")
    if copy_root is not None:
        if not runtime_file:
            result.ok = False
            result.reasons.append("runtime_path_unknown")
        else:
            try:
                Path(runtime_file).resolve().relative_to(Path(copy_root).resolve())
            except ValueError:
                result.ok = False
                result.reasons.append("runtime_outside_copy")
    for py_path in py_files:
        status, detail = _validate_one(Path(py_path))
        result.checked += 1
        if status == "ok":
            continue
        result.ok = False
        if status == "missing":
            result.missing.append(detail)
        else:
            result.stale.append(detail)
        if status not in result.reasons:
            result.reasons.append(status)
    return result


def build_isolated_copy(source: Path, destination: Path, *, precompile: bool) -> Path:
    """Copy the tree into ``destination`` without bytecode caches.

    ``precompile=False`` yields an A-class copy (no cache, writes disabled by
    the caller via ``PYTHONDONTWRITEBYTECODE``); ``precompile=True`` runs
    ``compileall`` so the copy can serve as B after :func:`validate_cache`.
    """
    source = Path(source)
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "build", "dist", ".git"),
        symlinks=True,
    )
    if precompile:
        targets = [
            name
            for name in ("fukua_rpa", "scripts", "plugins")
            if (destination / name).is_dir()
        ]
        # 入口脚本本身也参与启动导入，必须一并预编译，否则 B 类覆盖率校验会缺它。
        if (destination / "fukuaRPA.py").is_file():
            targets.append("fukuaRPA.py")
        targets = targets or ["."]
        subprocess.run(
            [sys.executable, "-m", "compileall", "-q", *targets],
            cwd=destination,
            check=False,
        )
    return destination


def clean_python_env() -> dict[str, str]:
    """Return an environment with Python path overrides removed."""
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONPYCACHEPREFIX"):
        env.pop(key, None)
    return env


def is_contaminated(value: float, reference: float = PRIOR_REFERENCE_MS) -> str:
    """Return a contamination reason, or "" when the sample is acceptable."""
    if value > reference * CONTAMINATION_UPPER_RATIO:
        return "upper_outlier"
    if value < reference * CONTAMINATION_LOWER_RATIO:
        return "lower_implausible"
    return ""


@dataclass
class SampleResult:
    """Five-sample result with every sample and any discard reason kept."""

    samples: list[float] = field(default_factory=list)
    discarded: list[dict] = field(default_factory=list)
    median: float | None = None
    unstable: bool = False
    contamination: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "samples": self.samples,
            "discarded_samples": self.discarded,
            "median": self.median,
            "unstable": self.unstable,
            "contamination": self.contamination,
        }


def collect_samples(run_once, *, reference: float = PRIOR_REFERENCE_MS,
                    count: int = SAMPLES_PER_CONDITION) -> SampleResult:
    """Run ``count`` samples, discarding a whole round at most once.

    ``run_once`` returns a float (ms) or raises; an exception marks the round
    contaminated and **never** triggers a resample of only the good samples.
    """
    result = SampleResult()
    rounds = 0
    while True:
        samples: list[float] = []
        reasons: list[str] = []
        for _ in range(count):
            try:
                value = float(run_once())
            except Exception as exc:  # noqa: BLE001 - 任何异常都算污染
                reasons.append(f"error:{type(exc).__name__}")
                break
            reason = is_contaminated(value, reference)
            if reason:
                reasons.append(reason)
            samples.append(value)
            time.sleep(0)
        if not reasons:
            result.samples = samples
            result.median = statistics.median(samples)
            return result
        result.discarded.append({"round": rounds, "samples": samples, "reasons": reasons})
        result.contamination.extend(reasons)
        rounds += 1
        if rounds > MAX_RESAMPLE_ROUNDS:
            result.unstable = True
            result.samples = samples
            result.median = statistics.median(samples) if samples else None
            return result
