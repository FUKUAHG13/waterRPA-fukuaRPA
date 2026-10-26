"""Regression tests for bug P2-3: the plugin manifest contract must be *actually*
validated (the build script now runs ``--validate`` and imports ``plugin_manifest``),
instead of the old behaviour where only ``--check`` ran and the contract parse was
never exercised — producing a silent green.

These tests are self-contained and mock only the isolated/offline conditions. They
mirror what ``plugins/*/tests/test_manifest_in_sync.py`` now exercises via
``build_plugin.py --validate`` (see ``plugins/tools/common.py::validate_with_contract``).

Coverage:
1. The ``--validate`` path truly imports/uses ``plugin_manifest.PluginManifest.parse_json``
   (not just ``--check``).  -> proves the contract is parsed.
2. When the contract *is* available, a malformed manifest makes ``--validate`` HARD FAIL
   (SystemExit), so it can never silently pass.
3. In a simulated isolated environment (no fukua_rpa dir) the contract parse downgrades to
   a WARNING and returns cleanly (no crash) — and the test asserts the warning text, so it
   cannot silently pass either.
4. In a simulated offline environment (fukua_rpa present but ``plugin_manifest`` unimportable)
   it likewise downgrades to a WARNING.
5. CLI integration: ``build_plugin.py --validate`` for a real plugin runs the contract path.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load the shared build helper without polluting the package namespace.
_COMMON_PATH = os.path.join(REPO_ROOT, "plugins", "tools", "common.py")
_spec = importlib.util.spec_from_file_location("_p2_3_common", _COMMON_PATH)
common = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(common)

REAL_PLUGIN_MANIFEST = os.path.join(REPO_ROOT, "fukua_rpa", "plugin_manifest.py")
BROWSER_PLUGIN_ROOT = os.path.join(REPO_ROOT, "plugins", "browser-automation")


def _valid_manifest(plugin_id="browser-automation"):
    return {
        "format": "fukuaRPA.plugin",
        "manifest_version": 1,
        "id": plugin_id,
        "name": "browser automation",
        "version": "1.0.0",
        "author": "x",
        "plugin_type": "python_action",
        "api_version": 1,
        "min_app_version": "1.0.0",
        "entrypoint": "src/browser_automation/action.py:run",
        "permissions": ["process.launch_confirmed"],
        "actions": [
            {
                "id": "a",
                "name": "A",
                "description": "d",
                "permissions": [],
                "input_schema": {},
                "output_schema": {},
            }
        ],
        "templates": [],
        "files": [],
        "dependencies": [],
    }


def _make_plugin_root(tmp_path, manifest_dict, *, with_real_contract=False):
    """Build ``<tmp>/<codex>/plugins/<id>/manifest.json`` plus a ``fukua_rpa`` dir.

    If ``with_real_contract`` is True, copy the real ``plugin_manifest.py`` into the
    fukua_rpa dir so an in-process ``import plugin_manifest`` resolves to the real logic.
    Returns the plugin root path.
    """
    codex = tmp_path / "codex"
    plugin_root = codex / "plugins" / manifest_dict["id"]
    plugin_root.mkdir(parents=True)
    fukua = codex / "fukua_rpa"
    fukua.mkdir()
    if with_real_contract:
        shutil.copy(REAL_PLUGIN_MANIFEST, fukua / "plugin_manifest.py")
    with open(plugin_root / "manifest.json", "w", encoding="utf-8") as fh:
        import json

        json.dump(manifest_dict, fh)
    return str(plugin_root)


#: 契约模块的**包内**限定名。必须与 ``common._CONTRACT_MODULE`` 保持一致。
#: 历史上这里写的是顶层名 ``plugin_manifest``，而 ``plugin_manifest.py`` 里有
#: ``from .constants import ...``，顶层导入必然抛「attempted relative import」
#: 并被降级成 warn —— 契约校验因此长期空转。见 common.validate_with_contract。
CONTRACT_MODULE = "fukua_rpa.plugin_manifest"


def test_contract_module_name_matches_implementation():
    """脚手架用的模块名必须与实现一致，否则下面的注入/隔离全是自欺欺人。"""
    assert common._CONTRACT_MODULE == CONTRACT_MODULE


@pytest.fixture
def clean_plugin_manifest_module():
    """Reset/restore every module key the contract import could touch.

    既清顶层 ``plugin_manifest``（旧机制残留），也清包内
    ``fukua_rpa.plugin_manifest``（现行机制），避免测试间互相污染。
    """
    keys = ("plugin_manifest", CONTRACT_MODULE)
    saved = {key: sys.modules.pop(key, None) for key in keys}
    try:
        yield
    finally:
        for key, module in saved.items():
            sys.modules.pop(key, None)
            if module is not None:
                sys.modules[key] = module


def test_validate_actually_uses_plugin_manifest(clean_plugin_manifest_module, tmp_path, capsys):
    """The ``--validate`` path must import ``plugin_manifest`` and call parse_json."""
    plugin_root = _make_plugin_root(tmp_path, _valid_manifest(), with_real_contract=False)

    # Inject a spy standing in for ``plugin_manifest`` so we can prove the
    # validate path reaches the contract parser (the exact thing P2-3 missed).
    calls = []
    fake = types.ModuleType(CONTRACT_MODULE)

    # 必须在注入 fake **之前**解析出真实模块并闭包持有：注入之后
    # import_module(CONTRACT_MODULE) 拿到的就是 fake 自己，会无限递归。
    real_contract = importlib.import_module(CONTRACT_MODULE)

    def spy_parse_json(text):
        # exercise the real parser to keep the assertion meaningful
        result = real_contract.PluginManifest.parse_json(text)
        calls.append(text)
        return result

    fake.PluginManifest = type("PM", (), {"parse_json": staticmethod(spy_parse_json)})
    # 注入到**包内**限定名下：这正是 validate_with_contract 实际 import 的键。
    sys.modules[CONTRACT_MODULE] = fake

    ok = common.validate_with_contract(plugin_root)

    assert ok is True, "validate_with_contract should succeed on a valid manifest"
    assert len(calls) == 1, "validate_with_contract must actually call PluginManifest.parse_json"
    out = capsys.readouterr().out
    assert "契约校验通过" in out, "expected the contract-parse success message"


def test_validate_malformed_manifest_hard_fails(clean_plugin_manifest_module, tmp_path):
    """When the contract is available, a malformed manifest must HARD FAIL (not silent green)."""
    malformed = _valid_manifest()
    malformed["surprise"] = 1  # unknown field -> PluginManifestError
    plugin_root = _make_plugin_root(tmp_path, malformed, with_real_contract=True)

    # No fake injected: the real plugin_manifest (copied into the temp fukua_rpa)
    # is used, so a contract violation must raise SystemExit.
    with pytest.raises(SystemExit):
        common.validate_with_contract(plugin_root)


def test_isolated_env_downgrades_to_warning_without_crash(
    clean_plugin_manifest_module, tmp_path, capsys
):
    """Simulated isolated env (no fukua_rpa dir): downgrade to WARNING, no crash, not silent."""
    # Build a plugin root whose grandparent has NO fukua_rpa directory.
    codex = tmp_path / "codex"
    plugin_root = codex / "plugins" / "browser-automation"
    plugin_root.mkdir(parents=True)
    with open(plugin_root / "manifest.json", "w", encoding="utf-8") as fh:
        import json

        json.dump(_valid_manifest(), fh)

    # Must NOT raise; must return False (warn downgrade) and emit a warning.
    result = common.validate_with_contract(str(plugin_root))

    assert result is False, "isolated env should downgrade (return False), not hard-fail"
    out = capsys.readouterr().out
    assert "[warn]" in out, "isolated env must surface a WARNING instead of silently passing"
    assert "未找到原项目目录" in out, "expected the 'fukua_rpa not found' warning"


def test_offline_env_import_unavailable_downgrades_to_warning(
    clean_plugin_manifest_module, tmp_path, capsys
):
    """Simulated offline env (fukua_rpa present but plugin_manifest unimportable): warn downgrade."""
    plugin_root = _make_plugin_root(tmp_path, _valid_manifest(), with_real_contract=False)
    # tmp 侧的 fukua_rpa 目录存在，但既没有 plugin_manifest.py 也没有 __init__.py。

    # 要真正模拟「离线」，必须同时切断三条供给：
    #   1) sys.path 上任何能提供 fukua_rpa **包**的目录（真实仓库根就是其中之一）；
    #   2) sys.path 上任何能提供顶层 plugin_manifest.py 的目录（旧机制残留）；
    #   3) sys.modules 里已缓存的 fukua_rpa 包 —— 否则 import_module 直接命中缓存。
    # 只做第 2 条是不够的：那样真实仓库的包导入照样成功，本测试会变成假绿。
    saved_path = list(sys.path)
    saved_pkg = sys.modules.pop("fukua_rpa", None)
    sys.path = [
        p
        for p in sys.path
        if not os.path.isdir(os.path.join(p or ".", "fukua_rpa"))
        and not os.path.isfile(os.path.join(p or ".", "plugin_manifest.py"))
    ]
    try:
        result = common.validate_with_contract(plugin_root)
    finally:
        sys.path = saved_path
        # 失败的包导入会在 sys.modules 里留下指向 tmp 的 namespace 包，必须清掉，
        # 否则后续测试会拿到一个空壳 fukua_rpa。
        sys.modules.pop("fukua_rpa", None)
        if saved_pkg is not None:
            sys.modules["fukua_rpa"] = saved_pkg

    assert result is False, "offline env should downgrade (return False), not hard-fail"
    out = capsys.readouterr().out
    assert "[warn]" in out, "offline env must surface a WARNING instead of silently passing"
    assert "无法 import 原项目契约" in out, "expected the 'cannot import contract' warning"


def test_cli_validate_uses_contract():
    """Integration: the real ``build_plugin.py --validate`` exercises the contract path."""
    build_script = os.path.join(BROWSER_PLUGIN_ROOT, "tools", "build_plugin.py")
    if not os.path.isfile(build_script):
        pytest.skip("browser-automation build script not present")

    proc = subprocess.run(
        [sys.executable, build_script, "--validate"],
        cwd=BROWSER_PLUGIN_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"--validate 失败（退出码 {proc.returncode}）\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    # Proves the contract was actually parsed (P2-3 regression guard).
    assert "契约校验通过" in proc.stdout


def test_real_repo_layout_never_downgrades(capsys):
    """真实仓库布局下，严格契约校验必须真的跑起来，绝不允许降级为 warn。

    这是 P2-3 二次复发的直接闸门。上面几条测试都在 tmp 目录里搭布局，一旦
    实现里的 import 机制与真实仓库布局脱节（历史上就是：把 <codex>/fukua_rpa
    塞进 sys.path 后做顶层 import plugin_manifest，撞上相对导入必然失败），
    它们仍可能靠 mock 变绿，而线上每一次插件构建都在静默跳过校验。

    本测试不 mock 任何东西：直接拿仓库里真实的插件根跑一遍，要求
    返回 True 且 stdout 里不出现任何 [warn]。
    """
    if not os.path.isfile(os.path.join(BROWSER_PLUGIN_ROOT, "manifest.json")):
        pytest.skip("browser-automation plugin not present")

    result = common.validate_with_contract(BROWSER_PLUGIN_ROOT)
    out = capsys.readouterr().out

    assert result is True, (
        "真实仓库布局下 validate_with_contract 必须返回 True（严格校验已执行），"
        f"实际返回 {result!r}；输出：\n{out}"
    )
    assert "[warn]" not in out, (
        f"真实仓库布局下不应出现任何降级告警，实际输出：\n{out}"
    )
    assert "契约校验通过" in out


def test_all_shipped_plugins_pass_the_real_contract():
    """仓库内每个插件的 manifest 都必须通过真实契约校验。

    严格校验空转期间，4 个插件包全部是在「未经契约校验」的状态下产出的。
    闸门重新打开后，这里把「全部插件都合规」这一事实钉死，避免只有
    browser-automation 被覆盖、其余三个继续裸奔。
    """
    plugins_dir = os.path.join(REPO_ROOT, "plugins")
    roots = sorted(
        os.path.join(plugins_dir, name)
        for name in os.listdir(plugins_dir)
        if os.path.isfile(os.path.join(plugins_dir, name, "manifest.json"))
    )
    assert roots, "未发现任何带 manifest.json 的插件目录，测试本身失去意义"

    for root in roots:
        # 契约不通过时 validate_with_contract 直接 SystemExit，天然是硬失败。
        assert common.validate_with_contract(root) is True, (
            f"{os.path.basename(root)} 的严格契约校验被降级跳过"
        )
