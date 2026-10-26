"""共享构建门禁：顶层 permissions 必须恰好等于所有动作 permissions 的并集。

背景
----
顶层 ``permissions`` 是安装确认页与插件中心展示的能力摘要。运行期并不用它做
强制拦截（消费点只有 plugin_registry 的安装日志与 plugin_center_window 的权限
摘要），因此它一旦与各动作实际申请的权限脱节，用户看到的就是失真的风险清单。
本模块既覆盖门禁函数本身的判定逻辑，也对随仓库发布的全部插件做真实回归。
"""

import importlib.util
import json
import os
import subprocess
import sys

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_ROOT = os.path.dirname(TOOLS_DIR)
COMMON_PATH = os.path.join(TOOLS_DIR, "common.py")


def _load_common():
    """按文件路径加载 common.py，避免依赖 plugins/ 的包结构（plugins/ 无 __init__.py）。"""
    spec = importlib.util.spec_from_file_location("_plugins_tools_common", COMMON_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = _load_common()


def _manifest(top, action_perms):
    """构造最小 manifest：top 为顶层权限，action_perms 为 {action_id: [权限]}。"""
    return {
        "id": "demo",
        "permissions": list(top),
        "actions": [
            {"id": aid, "permissions": list(perms)} for aid, perms in action_perms.items()
        ],
    }


# --- 判定逻辑 ---------------------------------------------------------------


def test_union_exact_match_has_no_issues():
    m = _manifest(
        ["a", "b"],
        {"act1": ["a"], "act2": ["b"]},
    )
    assert common.permissions_union_issues(m) == []
    assert common.check_permissions_union(m, verbose=False) is True


def test_union_ignores_declaration_order():
    """集合语义：顺序不同不算偏差。"""
    m = _manifest(["b", "a"], {"act1": ["a", "b"]})
    assert common.permissions_union_issues(m) == []


def test_union_ignores_duplicate_declarations_across_actions():
    """同一权限被多个动作申请，并集仍只需顶层出现一次。"""
    m = _manifest(["a"], {"act1": ["a"], "act2": ["a"], "act3": ["a"]})
    assert common.permissions_union_issues(m) == []


def test_extra_top_level_permission_is_reported():
    """顶层多列（会让用户高估插件能力）必须报出。"""
    m = _manifest(["a", "ghost"], {"act1": ["a"]})
    issues = common.permissions_union_issues(m)
    assert len(issues) == 1
    assert "顶层多余权限：ghost" in issues[0]
    assert common.check_permissions_union(m, verbose=False) is False


def test_missing_top_level_permission_is_reported_with_owner_action():
    """顶层漏列（会让用户低估插件能力）必须报出，并指明是哪个动作申请的。"""
    m = _manifest(["a"], {"act1": ["a"], "shooter": ["files.user_selected"]})
    issues = common.permissions_union_issues(m)
    assert len(issues) == 1
    assert "顶层缺失权限：files.user_selected" in issues[0]
    assert "shooter" in issues[0], "缺失项应指明申请该权限的动作，便于定位"


def test_both_directions_reported_together():
    """多列与漏列同时存在时两类问题都要报，不能只报第一类就返回。"""
    m = _manifest(["ghost"], {"act1": ["real"]})
    issues = common.permissions_union_issues(m)
    assert len(issues) == 2
    assert any("ghost" in i for i in issues)
    assert any("real" in i for i in issues)


def test_manifest_without_actions_is_skipped():
    """非动作型插件的权限语义尚未定义，门禁不越权判定。"""
    assert common.permissions_union_issues({"id": "x", "permissions": ["a"]}) == []
    assert common.permissions_union_issues({"id": "x", "permissions": ["a"], "actions": []}) == []


def test_missing_permissions_keys_are_treated_as_empty():
    """permissions 缺省或为 None 时按空集处理，不应抛异常。"""
    m = {"id": "x", "actions": [{"id": "a1"}, {"id": "a2", "permissions": None}]}
    assert common.permissions_union_issues(m) == []


# --- 磁盘入口 ---------------------------------------------------------------


def test_on_disk_missing_manifest_fails(tmp_path):
    assert common.check_permissions_union_on_disk(str(tmp_path), verbose=False) is False


def test_on_disk_invalid_json_fails(tmp_path):
    (tmp_path / "manifest.json").write_text("{not json", encoding="utf-8")
    assert common.check_permissions_union_on_disk(str(tmp_path), verbose=False) is False


def test_on_disk_reads_and_validates(tmp_path):
    good = _manifest(["a"], {"act1": ["a"]})
    (tmp_path / "manifest.json").write_text(json.dumps(good), encoding="utf-8")
    assert common.check_permissions_union_on_disk(str(tmp_path), verbose=False) is True

    bad = _manifest(["a", "ghost"], {"act1": ["a"]})
    (tmp_path / "manifest.json").write_text(json.dumps(bad), encoding="utf-8")
    assert common.check_permissions_union_on_disk(str(tmp_path), verbose=False) is False


# --- 随仓库插件的真实回归 ---------------------------------------------------


def _shipped_plugin_dirs():
    """随仓库发布的插件目录（含 manifest.json 的直接子目录）。"""
    out = []
    for name in sorted(os.listdir(PLUGINS_ROOT)):
        root = os.path.join(PLUGINS_ROOT, name)
        if os.path.isfile(os.path.join(root, "manifest.json")):
            out.append((name, root))
    return out


def test_all_shipped_plugins_satisfy_union_gate():
    """随仓库发布的每个插件都必须通过门禁。

    这是门禁的真实价值所在：新增动作却忘记同步顶层权限，会在此失败。
    """
    plugins = _shipped_plugin_dirs()
    assert plugins, f"未在 {PLUGINS_ROOT} 下发现任何带 manifest.json 的插件"
    failures = []
    for name, root in plugins:
        with open(os.path.join(root, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        issues = common.permissions_union_issues(manifest)
        if issues:
            failures.append(f"{name}: " + "; ".join(issues))
    assert not failures, "以下插件的顶层权限与动作并集不一致：\n" + "\n".join(failures)


def test_validate_cli_rejects_tampered_manifest(tmp_path):
    """端到端：--validate 遇到不合规 manifest 必须以非零退出码失败。

    直接跑各插件的 build_plugin.py 会读真实 manifest（合规），所以这里复制一份
    插件目录并注入一个多余权限，验证 CLI 层确实把门禁接上了，而不只是函数可用。
    """
    import shutil

    src = os.path.join(PLUGINS_ROOT, "browser-automation")
    if not os.path.isdir(src):
        import pytest

        pytest.skip("browser-automation 插件不存在，跳过 CLI 端到端校验")

    dst = tmp_path / "plugins" / "browser-automation"
    shutil.copytree(src, dst)
    # 共享工具需与插件保持 plugins/<id>/tools/../../tools 的相对位置
    shutil.copytree(TOOLS_DIR, tmp_path / "plugins" / "tools")

    manifest_path = dst / "manifest.json"
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    manifest["permissions"] = list(manifest["permissions"]) + ["input.keyboard"]
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    proc = subprocess.run(
        [sys.executable, os.path.join(str(dst), "tools", "build_plugin.py"), "--validate"],
        cwd=str(dst),
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0, (
        "注入多余权限后 --validate 仍然成功，门禁未接入 CLI\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    assert "input.keyboard" in proc.stdout, "失败输出应指明是哪个权限多余"
