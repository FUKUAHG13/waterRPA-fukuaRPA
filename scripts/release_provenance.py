"""Content-safe Git provenance used by build and release tooling."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git(*arguments: str, root: Path = ROOT, check: bool = True) -> str:
    # FR-20260812-D15-001: git 子进程必须带 timeout，防止挂起（如网络盘/被锁
    # 的 git 目录）无限阻塞构建/发布。
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    return completed.stdout.strip()


def repository_provenance(root: Path = ROOT) -> dict:
    root = Path(root).resolve()
    try:
        inside = _git("rev-parse", "--is-inside-work-tree", root=root)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {
            "available": False,
            "commit": "",
            "branch": "",
            "exact_tags": [],
            "dirty": True,
            "changed_entry_count": 0,
            "commit_signature_verified": False,
        }
    if inside.lower() != "true":
        return {
            "available": False,
            "commit": "",
            "branch": "",
            "exact_tags": [],
            "dirty": True,
            "changed_entry_count": 0,
            "commit_signature_verified": False,
        }

    status_lines = [
        line
        for line in _git(
            "status", "--porcelain=v1", "--untracked-files=all", root=root
        ).splitlines()
        if line.strip()
    ]
    tags = sorted(
        line.strip()
        for line in _git("tag", "--points-at", "HEAD", root=root).splitlines()
        if line.strip()
    )
    signature = subprocess.run(
        ["git", "verify-commit", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,  # FR-20260812-D15-001: 防止 git 挂起无限阻塞
    )
    return {
        "available": True,
        "commit": _git("rev-parse", "HEAD", root=root),
        "branch": _git(
            "rev-parse", "--abbrev-ref", "HEAD", root=root
        ),
        "commit_timestamp": _git(
            "show", "-s", "--format=%cI", "HEAD", root=root
        ),
        "exact_tags": tags,
        "dirty": bool(status_lines),
        # Do not record changed filenames; local filenames can themselves be private.
        "changed_entry_count": len(status_lines),
        "commit_signature_verified": signature.returncode == 0,
    }


def version_tag_matches(tag: str, app_version: str) -> bool:
    normalized = str(tag or "").strip().casefold()
    version = str(app_version or "").strip().casefold()
    return normalized in {
        version,
        f"fukuarpa-{version}",
        f"fukuarpa_{version}",
    }


def require_formal_release_provenance(
    app_version: str, root: Path = ROOT
) -> dict:
    provenance = repository_provenance(root)
    if not provenance.get("available"):
        raise RuntimeError("正式发布必须从 Git 仓库中的明确提交构建。")
    if provenance.get("dirty"):
        raise RuntimeError(
            "正式发布要求 Git 工作区完全干净；请先检查、提交或移走未提交修改。"
        )
    matching = [
        tag
        for tag in provenance.get("exact_tags", [])
        if version_tag_matches(tag, app_version)
    ]
    if not matching:
        raise RuntimeError(
            "正式发布提交必须带有与当前版本对应的标签，例如 "
            f"fukuaRPA-{app_version}。"
        )
    provenance["version_tags"] = matching
    return provenance
