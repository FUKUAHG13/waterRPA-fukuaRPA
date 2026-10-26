"""将公开源码身份与维护者私有验证资料分开，正式门禁仍检查两者。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
REFACTOR_BASELINE_REF = "7ee4f99451ae236db5a224b0537e597b68f8794e"
REFACTOR_BASELINE_SOURCE_PATH = "fukua_rpa/ui/main_window.py"
REFACTOR_BASELINE_PATH = "docs/validation/main_window-7ee4f994.py"
REFACTOR_BASELINE_SHA256 = "c5d0413a87492993060aec2c8b5d75f8785dfce1e029510f031e49a8d9c01641"
PRIVATE_FILES = (
    "scripts/run_startup_baseline.py",
    "tests/test_crash_guard.py",
    "tests/test_pyright_plugin_baseline.py",
)
REQUIRED_INPUTS = (
    "docs/BUILD_FROM_SOURCE.md",
    "docs/PERFORMANCE_BASELINE.json",
    "docs/UI开发规范.md",
    "docs/代码审查基准.md",
    "docs/步骤设置显隐矩阵.md",
    "docs/设计基准.md",
    REFACTOR_BASELINE_PATH,
    *PRIVATE_FILES,
)
BOUND_ROOT_ENV = "FUKUA_VALIDATION_SOURCE_ROOT"
BOUND_HASH_ENV = "FUKUA_VALIDATION_INPUTS_SHA256"
FORBIDDEN_PUBLIC_FILES = {
    ".git_allowed_signers", "config.ini", "profiles_backup.json", "credentials.dat",
    "plugins/.disabled_plugins.json", "scheduled_tasks.json", "scheduled_task_history.json",
    "scheduled_task_logs.jsonl", "portable.flag", "宣传视频文案_fukuarpa.md",
}
FORBIDDEN_PUBLIC_PREFIXES = (
    ".scratch/", ".workbuddy/", ".workbuddy-ai/", "dist/", "build/", "native_core/build/",
    "crash_reports/", "imported_assets/", "profiles_history/",
)


def is_private_validation_path(relative: str) -> bool:
    """维护者指定不公开的输入；不通过改名将内容移入公开目录。"""
    path = PurePosixPath(relative.replace("\\", "/")).as_posix().casefold()
    return path == "docs" or path.startswith("docs/") or path in PRIVATE_FILES


def assert_public_source_boundary(root: Path = ROOT) -> None:
    """验证实际Git索引，忽略规则不能使已暂存的私人资料变得可公开。"""
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True, timeout=30
    )
    forbidden = []
    for original in result.stdout.decode("utf-8").split("\0"):
        if not original:
            continue
        path = original.replace("\\", "/").casefold()
        if (
            is_private_validation_path(path)
            or path in FORBIDDEN_PUBLIC_FILES
            or path.startswith(FORBIDDEN_PUBLIC_PREFIXES)
            or Path(path).suffix in {".dll", ".exe", ".zip", ".log", ".pyc", ".pdb", ".obj"}
        ):
            forbidden.append(original)
    if forbidden:
        raise RuntimeError(f"公开候选仍跟踪了{len(forbidden)}个禁止公开的文件，拒绝正式构建")


@dataclass(frozen=True)
class ValidationInputs:
    sha256: str
    entries: tuple[tuple[str, int, str], ...]

    def receipt(self) -> dict:
        # SAFETY: 公开构建材料只保存身份和数量，不带私有内容或本机绝对路径。
        return {"format_version": 1, "sha256": self.sha256, "file_count": len(self.entries)}


def load_refactor_baseline(root: Path = ROOT) -> str:
    """读取原始固定Git文件，不要求公开快照携带私有开发提交的祖先链。"""
    root = Path(root).resolve()
    path = root / REFACTOR_BASELINE_PATH
    if not path.is_file():
        raise RuntimeError("缺少固定历史重构基线，不能跳过原方法体对照")
    if not path.resolve().is_relative_to(root):
        raise RuntimeError("历史重构基线不能指向验证目录之外")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != REFACTOR_BASELINE_SHA256:
        raise RuntimeError("历史重构基线身份不符，拒绝使用其他源码替代原始对照")
    return raw.decode("utf-8")


def capture_validation_inputs(root: Path = ROOT, *, required: bool = False) -> ValidationInputs:
    root = Path(root).resolve()
    if required:
        missing = [name for name in REQUIRED_INPUTS if not (root / name).is_file()]
        if missing:
            raise RuntimeError(
                "缺少维护者私有验证输入；不能跳过正式门禁。请在隔离验证目录配齐："
                + "、".join(missing)
            )
    paths = [root / name for name in PRIVATE_FILES if (root / name).is_file()]
    docs = root / "docs"
    if docs.is_dir():
        paths.extend(path for path in docs.rglob("*") if path.is_file())
    entries = []
    for path in sorted(set(paths), key=lambda item: item.relative_to(root).as_posix()):
        # 不允许符号链接把采集范围扩大到本机的其他文件。
        try:
            path.resolve().relative_to(root)
        except ValueError as exc:
            raise RuntimeError("私有验证输入不能指向验证目录之外") from exc
        raw = path.read_bytes()
        entries.append((path.relative_to(root).as_posix(), len(raw), hashlib.sha256(raw).hexdigest()))
    serialized = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return ValidationInputs(hashlib.sha256(serialized).hexdigest(), tuple(entries))


def bind_validation_inputs(root: Path = ROOT) -> ValidationInputs:
    """构建进程与子进程共用同一锁定身份，变化时拒绝重新绑定。"""
    root = Path(root).resolve()
    current = capture_validation_inputs(root, required=True)
    expected_root = os.environ.get(BOUND_ROOT_ENV, "")
    expected_hash = os.environ.get(BOUND_HASH_ENV, "")
    if (expected_root or expected_hash) and (
        not expected_root
        or not expected_hash
        or Path(expected_root).resolve() != root
        or current.sha256 != expected_hash
    ):
        raise RuntimeError("私有验证输入或验证源码目录发生变化，已拒绝继续正式构建")
    os.environ[BOUND_ROOT_ENV] = str(root)
    os.environ[BOUND_HASH_ENV] = current.sha256
    return current


def assert_validation_inputs_unchanged(expected: ValidationInputs, root: Path = ROOT) -> None:
    current = capture_validation_inputs(root, required=True)
    if current != expected:
        raise RuntimeError("构建期间私有验证输入发生变化，已拒绝发布")


def validation_receipt(*, formal: bool, root: Path = ROOT) -> dict:
    inputs = bind_validation_inputs(root) if formal else capture_validation_inputs(root)
    return inputs.receipt()
