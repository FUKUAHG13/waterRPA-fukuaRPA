#!/usr/bin/env python3
"""D12-001 回归：``--check`` 在生成产物与磁盘 manifest 的 files 集合不同时，
不应以 ``KeyError`` 崩溃，而应把不一致报告为 ``ok=False`` 并以非零退出。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import unittest

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMON_PATH = os.path.join(TOOLS_DIR, "common.py")


def _load_common():
    """按文件路径加载 common.py，避免依赖 plugins/ 的包结构（plugins/ 无 __init__.py）。"""
    spec = importlib.util.spec_from_file_location("_plugins_tools_common_pkg", COMMON_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = _load_common()


_META_KEYS = {
    "format": "fukuaRPA.plugin",
    "manifest_version": 1,
    "id": "demo",
    "name": "演示",
    "version": "1.0.0",
    "author": "t",
    "plugin_type": "python_action",
    "api_version": 1,
    "min_app_version": "1.0.0",
    "permissions": [],
    "actions": [],
    "files": [],
}


class CheckKeyErrorTests(unittest.TestCase):
    def test_file_set_diff_reports_mismatch_without_keyerror(self):
        tmp = tempfile.mkdtemp()
        try:
            # 磁盘侧 manifest 故意不含 extra.py，制造 files 集合差。
            disk_manifest = dict(_META_KEYS)
            disk_manifest["files"] = [{"path": "keep.py", "sha256": "0" * 64}]
            with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump(disk_manifest, fh)
            with open(os.path.join(tmp, "keep.py"), "w", encoding="utf-8") as fh:
                fh.write("x = 1\n")

            # 让生成侧多出一个磁盘侧没有的文件 extra.py。
            orig = common.build_files_section

            def fake_build(plugin_root, runtime_files):
                return [
                    {"path": "keep.py", "sha256": "0" * 64},
                    {"path": "extra.py", "sha256": "a" * 64},
                ]

            common.build_files_section = fake_build
            try:
                # 修复前：循环内 disk_files["extra.py"] 触发 KeyError 使整个门禁崩溃。
                # 修复后：disk_files.get(rel, {}).get(...) 返回 None，报告不一致并返回 False。
                ok = common.check_manifest_in_sync(
                    tmp, dict(_META_KEYS), ["keep.py", "extra.py"], verbose=False
                )
            finally:
                common.build_files_section = orig
            self.assertFalse(ok, "files 集合差应被报告为不一致，而非崩溃")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_identical_manifests_pass(self):
        tmp = tempfile.mkdtemp()
        try:
            sources = {
                "src/demo_pkg/__init__.py": '__version__ = "1.1.0"\n',
                "src/demo_pkg/action.py": "def run():\n    return 1\n",
                "README.md": "# demo\n",
            }
            root = os.path.join(tmp, "demo")
            for rel, text in sources.items():
                p = os.path.join(root, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(text)
            # 先按相同源码写出 manifest.json，保证磁盘侧 == 生成侧。
            common.write_manifest(root, dict(_META_KEYS, version="1.1.0"), list(sources.keys()))
            ok = common.check_manifest_in_sync(
                root, dict(_META_KEYS, version="1.1.0"), list(sources.keys()), verbose=False
            )
            self.assertTrue(ok)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
