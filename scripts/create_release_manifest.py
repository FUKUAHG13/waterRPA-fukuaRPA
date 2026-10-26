"""Create a content-hash stable per-file manifest for an onedir release.

Metadata includes generation timestamp; per-file hashes are stable for
unchanged content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.hashutil import atomic_write_text, hash_file  # noqa: E402

REPARSE_POINT_ATTRIBUTE = 0x400


def _is_reparse_point(path: Path) -> bool:
    """FR-20260811-D15-2: 判定 reparse point（junction / symlink）。

    与 create_release_archive 侧一致：reparse 可能让发布树指向树外内容，使
    manifest 的覆盖范围失真或泄露出目录结构，必须在生成清单时拒绝。
    """
    try:
        stat_result = path.lstat()
    except OSError:
        return True  # 无法 stat 视为不可信，拒绝。
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & REPARSE_POINT_ATTRIBUTE)


def build_manifest(release_dir):
    release_dir = Path(release_dir).resolve()
    files = []
    aggregate = hashlib.sha256()
    total_bytes = 0
    # FR-20260811-D15-2: 用 os.walk(followlinks=False) 而非 rglob——rglob 会随
    # reparse point（junction/symlink）下钻，成环时挂起且遍历前无法拦截目录级
    # reparse；os.walk 不跟随符号链接目录，先逐目录检测再处理文件。
    for dirpath, dirnames, filenames in os.walk(release_dir, followlinks=False):
        # 排序 dirnames：directory_sha256 依赖遍历顺序，跨文件系统/平台必须稳定。
        dirnames.sort(key=str.casefold)
        for name in list(dirnames):
            dir_path = Path(dirpath) / name
            if _is_reparse_point(dir_path):
                raise RuntimeError(
                    f"发布树包含 reparse point 目录，拒绝生成清单：{dir_path}"
                )
        for name in sorted(filenames, key=str.casefold):
            path = Path(dirpath) / name
            if _is_reparse_point(path):
                raise RuntimeError(
                    f"发布树包含 reparse point，拒绝生成清单：{path}"
                )
            relative = path.relative_to(release_dir).as_posix()
            size = path.stat().st_size
            checksum = hash_file(path)
            total_bytes += size
            files.append({"path": relative, "size": size, "sha256": checksum})
            aggregate.update(f"{checksum}  {relative}\n".encode())
    return {
        "format": "fukuaRPA_onedir_manifest",
        "version": 1,
        "build": release_dir.name,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "file_count": len(files),
        "total_bytes": total_bytes,
        "directory_sha256": aggregate.hexdigest(),
        "files": files,
    }


def verify_manifest(release_dir, manifest_path):
    expected = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    actual = build_manifest(release_dir)
    for key in ("format", "version", "build", "file_count", "total_bytes", "directory_sha256", "files"):
        if expected.get(key) != actual.get(key):
            raise RuntimeError(f"Release manifest mismatch in {key}")
    return actual


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    release_dir = args.release_dir.resolve()
    if not release_dir.is_dir():
        raise SystemExit(f"Release directory not found: {release_dir}")
    output = args.output.resolve() if args.output else release_dir.parent / f"{release_dir.name}_manifest.json"
    if args.check:
        if not output.is_file():
            raise SystemExit(f"Manifest not found: {output}")
        manifest = verify_manifest(release_dir, output)
    else:
        manifest = build_manifest(release_dir)
        atomic_write_text(output, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: manifest[key] for key in manifest if key != "files"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
