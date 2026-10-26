"""校验 manifest.json 与源码生成逻辑一致（manifest 单源化防漂移，对应审查项 #7）。

手工改 manifest.json 后，``python tools/build_plugin.py --check`` 会以非零退出码失败，
从而在本测试（或 CI）中暴露「manifest 与源码漂移」。
"""

import os
import subprocess
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_SCRIPT = os.path.join(PLUGIN_ROOT, "tools", "build_plugin.py")


def test_manifest_in_sync_via_cli():
    """build_plugin.py --check 与 --validate 均应成功（退出码 0）。

    --check 校验 manifest 与源码生成逻辑一致；--validate 用原项目契约严格
    校验 manifest（捕获 plugin_type / 未知字段等契约违规，对应审查项 P2-3）。
    """
    assert os.path.isfile(BUILD_SCRIPT), f"缺少构建脚本：{BUILD_SCRIPT}"
    for flag in ("--check", "--validate"):
        proc = subprocess.run(
            [sys.executable, BUILD_SCRIPT, flag],
            cwd=PLUGIN_ROOT,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (
            f"build_plugin.py {flag} 失败（退出码 {proc.returncode}）\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )


def test_manifest_in_sync_via_function():
    """直接调用 build_plugin.check_manifest_in_sync() 应返回 True（不写盘、不依赖契约）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_build_plugin_check", BUILD_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.check_manifest_in_sync(verbose=False) is True
