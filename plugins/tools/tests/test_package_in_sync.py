"""共享构建门禁：可分发包与磁盘源码一致，且打包结果可复现。

背景
----
``.fkrplugin`` 是 README 所称的「可分发包」，是磁盘源码的一份冗余副本。历史上
4 个插件的包全部停留在 v1.0.59，而源码已推进到 v1.0.102——其间的数据安全修复
（拒绝覆盖已有文件、多显示器坐标修正）与资源泄漏修复都没有进入分发物。当时的
``--check`` 只比对 manifest 与源码，读不到包内容，因此完全没有报警。

漂移能长期存在，还有一个结构性原因：旧的 ``build_zip`` 用 ``ZipFile.write()``
写入，会把源文件 mtime 记进条目，而 ``write_manifest`` 每次都重写 manifest.json，
于是「源码一字未改，重新构建却产生新的二进制 diff」。面对这种无意义 diff，
开发者倾向于不暂存二进制包。因此本模块同时守两件事：包内容一致，以及打包确定性。
"""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_ROOT = os.path.dirname(TOOLS_DIR)
COMMON_PATH = os.path.join(TOOLS_DIR, "common.py")


def _load_common():
    """按文件路径加载 common.py，避免依赖 plugins/ 的包结构（plugins/ 无 __init__.py）。"""
    spec = importlib.util.spec_from_file_location("_plugins_tools_common_pkg", COMMON_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


common = _load_common()

_DEFAULT_SOURCES = {
    "src/demo_pkg/__init__.py": '__version__ = "1.1.0"\n',
    "src/demo_pkg/action.py": "def run():\n    return 1\n",
    "README.md": "# demo\n",
}


def _make_plugin(tmp_path, plugin_id="demo", version="1.1.0", sources=None):
    """在 tmp_path 下造一个最小插件目录，返回 (root, meta, runtime_files)。"""
    sources = dict(_DEFAULT_SOURCES if sources is None else sources)
    root = tmp_path / plugin_id
    for rel, text in sources.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    meta = {
        "format": "fukuaRPA.plugin",
        "manifest_version": 1,
        "id": plugin_id,
        "name": "演示插件",
        "version": version,
        "author": "test",
        "plugin_type": "python_action",
        "api_version": 1,
        "min_app_version": "1.0.0",
        "entrypoint": "src/demo_pkg/action.py:run",
        "permissions": [],
        "dependencies": [],
        "templates": [],
        "actions": [],
    }
    return str(root), meta, list(sources)


def _build(root, meta, runtime_files):
    """生成 manifest.json 并打包，返回包路径。"""
    manifest_path = common.write_manifest(root, meta, runtime_files)
    return common.build_zip(root, manifest_path, meta["id"])


def _sha(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# --- 确定性打包 -------------------------------------------------------------


def test_build_zip_is_byte_identical_across_rebuilds(tmp_path):
    """同一份源码重复构建必须产出完全相同的字节。

    这是防漂移门禁能长期存活的前提：若每次构建都产生无意义的二进制 diff，
    开发者会逐渐忽略它，门禁形同虚设。
    """
    root, meta, files = _make_plugin(tmp_path)
    first = _sha(_build(root, meta, files))
    second = _sha(_build(root, meta, files))
    assert first == second, "重复构建产出不同字节，打包不具确定性"


def test_build_zip_pins_entry_timestamp(tmp_path):
    """每个条目的时间戳都固定为 ZIP 最小时间，与源文件 mtime 无关。"""
    root, meta, files = _make_plugin(tmp_path)
    pkg = _build(root, meta, files)
    with zipfile.ZipFile(pkg) as zf:
        infos = zf.infolist()
    assert infos, "包内不应为空"
    for info in infos:
        assert info.date_time == (1980, 1, 1, 0, 0, 0), (
            f"{info.filename} 的时间戳未固定：{info.date_time}"
        )


def test_build_zip_pins_create_system(tmp_path):
    """固定 create_system，否则同一源码在 Windows 与 POSIX 上产出不同字节。"""
    root, meta, files = _make_plugin(tmp_path)
    with zipfile.ZipFile(_build(root, meta, files)) as zf:
        for info in zf.infolist():
            assert info.create_system == 0, (
                f"{info.filename} 的 create_system={info.create_system}，跨平台不确定"
            )


def test_build_zip_entries_are_not_taken_for_symlinks(tmp_path):
    """条目属性不得命中安装侧的符号链接判定，否则真实包会被拒绝安装。

    plugin_package._is_symlink 检查 (external_attr >> 16) & 0o170000 == 0o120000。
    手工构造 ZipInfo 时若给 external_attr 写入 Unix 权限位，必须确保不落进该值。
    """
    root, meta, files = _make_plugin(tmp_path)
    with zipfile.ZipFile(_build(root, meta, files)) as zf:
        for info in zf.infolist():
            mode = (info.external_attr >> 16) & 0o170000
            assert mode != 0o120000, f"{info.filename} 会被安装侧误判为符号链接"


def test_build_zip_members_match_manifest(tmp_path):
    """包内成员恰好是 manifest.json 加 manifest.files 所列文件。"""
    root, meta, files = _make_plugin(tmp_path)
    pkg = _build(root, meta, files)
    with zipfile.ZipFile(pkg) as zf:
        members = set(zf.namelist())
    assert members == {"manifest.json"} | set(files)


def test_build_zip_content_changes_when_source_changes(tmp_path):
    """源码真的改了就必须产生不同字节，确定性不能掩盖真实变更。"""
    root, meta, files = _make_plugin(tmp_path)
    before = _sha(_build(root, meta, files))
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as handle:
        handle.write("# demo changed\n")
    after = _sha(_build(root, meta, files))
    assert before != after


# --- 包一致性判定 -----------------------------------------------------------


def test_fresh_package_has_no_issues(tmp_path):
    root, meta, files = _make_plugin(tmp_path)
    _build(root, meta, files)
    assert common.package_sync_issues(root, meta["id"]) == []
    assert common.check_package_in_sync(root, meta["id"], verbose=False) is True


def test_stale_package_after_source_edit_is_detected(tmp_path):
    """复现历史漂移：源码与 manifest 都更新了，包却没重打。"""
    root, meta, files = _make_plugin(tmp_path)
    _build(root, meta, files)
    with open(os.path.join(root, "src", "demo_pkg", "action.py"), "w", encoding="utf-8") as handle:
        handle.write("def run():\n    return 2  # 安全修复\n")
    # 只刷新 manifest（等价于 --no-zip），不重新打包
    common.write_manifest(root, meta, files)

    issues = common.package_sync_issues(root, meta["id"])
    assert any("src/demo_pkg/action.py" in i for i in issues), issues
    assert any("manifest.json" in i for i in issues), "清单本身也已落后，应一并报出"
    assert common.check_package_in_sync(root, meta["id"], verbose=False) is False


def test_missing_package_is_detected(tmp_path):
    root, meta, files = _make_plugin(tmp_path)
    common.write_manifest(root, meta, files)
    issues = common.package_sync_issues(root, meta["id"])
    assert len(issues) == 1
    assert "缺少可分发包" in issues[0]


def test_extra_member_in_package_is_detected(tmp_path):
    """包内夹带未在 manifest.files 声明的文件必须报出（安装侧同样会拒绝）。"""
    root, meta, files = _make_plugin(tmp_path)
    pkg = _build(root, meta, files)
    with zipfile.ZipFile(pkg, "a") as zf:
        zf.writestr("stowaway.txt", "x")
    issues = common.package_sync_issues(root, meta["id"])
    assert any("包内多余文件：stowaway.txt" in i for i in issues), issues


def test_missing_member_in_package_is_detected(tmp_path):
    """manifest 声明了但包里没有的文件必须报出。"""
    root, meta, files = _make_plugin(tmp_path)
    pkg = _build(root, meta, files)
    keep = [n for n in zipfile.ZipFile(pkg).namelist() if n != "README.md"]
    payload = {n: zipfile.ZipFile(pkg).read(n) for n in keep}
    os.remove(pkg)
    with zipfile.ZipFile(pkg, "w") as zf:
        for name, data in payload.items():
            zf.writestr(name, data)
    issues = common.package_sync_issues(root, meta["id"])
    assert any("包内缺少文件：README.md" in i for i in issues), issues


def test_corrupt_package_is_reported_without_crashing(tmp_path):
    root, meta, files = _make_plugin(tmp_path)
    _build(root, meta, files)
    with open(os.path.join(root, f"{meta['id']}.fkrplugin"), "wb") as handle:
        handle.write(b"not a zip at all")
    issues = common.package_sync_issues(root, meta["id"])
    assert len(issues) == 1
    assert "无法读取可分发包" in issues[0]


def test_missing_manifest_is_reported(tmp_path):
    root, meta, files = _make_plugin(tmp_path)
    _build(root, meta, files)
    os.remove(os.path.join(root, "manifest.json"))
    issues = common.package_sync_issues(root, meta["id"])
    assert len(issues) == 1
    assert "不存在 manifest.json" in issues[0]


def test_invalid_manifest_json_is_reported(tmp_path):
    root, meta, files = _make_plugin(tmp_path)
    _build(root, meta, files)
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as handle:
        handle.write("{broken")
    issues = common.package_sync_issues(root, meta["id"])
    assert len(issues) == 1
    assert "无法读取 manifest.json" in issues[0]


# --- 版本一致性判定 ---------------------------------------------------------


def test_matching_version_has_no_issues(tmp_path):
    root, meta, files = _make_plugin(tmp_path, version="1.1.0")
    assert common.package_version_issues(root, meta, files) == []
    assert common.check_package_version(root, meta, files, verbose=False) is True


def test_version_mismatch_is_detected(tmp_path):
    """__init__.py 与 MANIFEST_META 是同一事实的两处手写副本，漏改任一处都要报。"""
    root, meta, files = _make_plugin(tmp_path, version="2.0.0")
    issues = common.package_version_issues(root, meta, files)
    assert len(issues) == 1
    assert "1.1.0" in issues[0] and "2.0.0" in issues[0]
    assert common.check_package_version(root, meta, files, verbose=False) is False


def test_missing_dunder_version_is_skipped(tmp_path):
    """未声明 __version__ 的包不做要求，门禁不对未来插件强加该约定。"""
    sources = dict(_DEFAULT_SOURCES)
    sources["src/demo_pkg/__init__.py"] = "# 无版本声明\n"
    root, meta, files = _make_plugin(tmp_path, sources=sources)
    assert common.package_version_issues(root, meta, files) == []


def test_indented_version_assignment_is_ignored(tmp_path):
    """只认模块顶层赋值，函数内的同名局部量不应被误当作包版本。"""
    sources = dict(_DEFAULT_SOURCES)
    sources["src/demo_pkg/__init__.py"] = (
        "def probe():\n    __version__ = \"0.0.1\"\n    return __version__\n"
    )
    root, meta, files = _make_plugin(tmp_path, sources=sources)
    assert common.package_version_issues(root, meta, files) == []


def test_non_init_modules_are_not_scanned(tmp_path):
    """只扫 __init__.py；其他模块里的 __version__ 不参与判定。"""
    sources = dict(_DEFAULT_SOURCES)
    sources["src/demo_pkg/__init__.py"] = "# 无版本声明\n"
    sources["src/demo_pkg/action.py"] = '__version__ = "0.0.1"\n\ndef run():\n    return 1\n'
    root, meta, files = _make_plugin(tmp_path, sources=sources)
    assert common.package_version_issues(root, meta, files) == []


# --- 随仓库插件的真实回归 ---------------------------------------------------


def _shipped_plugin_dirs():
    """随仓库发布的插件目录（含 manifest.json 的直接子目录）。"""
    out = []
    for name in sorted(os.listdir(PLUGINS_ROOT)):
        root = os.path.join(PLUGINS_ROOT, name)
        if os.path.isfile(os.path.join(root, "manifest.json")):
            out.append((name, root))
    return out


def test_all_shipped_plugin_packages_are_in_sync():
    """随仓库发布的每个插件，其 .fkrplugin 都必须与磁盘源码一致。

    这是门禁的真实价值：改了插件源码却忘记重新打包并提交，会在此失败，
    而不是等到用户装上旧代码才发现。
    """
    plugins = _shipped_plugin_dirs()
    assert plugins, f"未在 {PLUGINS_ROOT} 下发现任何带 manifest.json 的插件"
    failures = []
    for name, root in plugins:
        issues = common.package_sync_issues(root, name)
        if issues:
            failures.append(f"{name}:\n    " + "\n    ".join(issues))
    assert not failures, "以下插件的可分发包与磁盘源码不一致：\n" + "\n".join(failures)


def test_all_shipped_plugins_declare_consistent_version():
    """随仓库发布的每个插件，manifest version 与代码内 __version__ 必须一致。"""
    failures = []
    for name, root in _shipped_plugin_dirs():
        with open(os.path.join(root, "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        runtime_files = [str(f.get("path")) for f in manifest.get("files") or []]
        issues = common.package_version_issues(root, manifest, runtime_files)
        if issues:
            failures.append(f"{name}: " + "; ".join(issues))
    assert not failures, "以下插件的版本号不一致：\n" + "\n".join(failures)


def test_packaged_text_members_pin_eol():
    """每个打包成员都必须在 .gitattributes 里被显式钉死换行。

    没有显式 eol 时，`* text=auto` 会把换行交给 core.autocrlf 决定，而 Git for
    Windows 的 system 配置默认 core.autocrlf=true。后果有两层：
      1. 新克隆检出的文件是 CRLF、包内却是 LF，package_sync_issues 必然报不一致
         ——门禁在 CI 上恒红，且只在本机为绿，极难排查；
      2. manifest.json 的 files 段存的是各成员 SHA-256，换行随环境漂移会让哈希
         漂移，build_zip 承诺的「确定性打包」跨机器不再成立。
    本测试在新增一种打包成员类型（例如 .cfg/.ini）却忘记补 .gitattributes 规则
    时失败，把这条隐性约束变成显式门禁。
    """
    import pytest

    if not shutil.which("git"):
        pytest.skip("git 不可用，无法查询 .gitattributes 属性")

    unpinned = []
    for _name, root in _shipped_plugin_dirs():
        with open(os.path.join(root, "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        members = ["manifest.json"] + [
            str(f.get("path")) for f in manifest.get("files") or []
        ]
        for member in members:
            rel = os.path.relpath(
                os.path.join(root, member), os.path.dirname(PLUGINS_ROOT)
            ).replace(os.sep, "/")
            proc = subprocess.run(
                ["git", "check-attr", "eol", "--", rel],
                cwd=os.path.dirname(PLUGINS_ROOT),
                capture_output=True,
                text=True,
            )
            # 输出形如 "<path>: eol: lf"；未钉死时为 "unspecified"。
            verdict = proc.stdout.strip().rsplit(":", 1)[-1].strip()
            if verdict != "lf":
                unpinned.append(f"{rel} -> eol={verdict or '?'}")

    assert not unpinned, (
        "以下打包成员未在 .gitattributes 中钉死 eol=lf，"
        "会导致包与检出在 core.autocrlf=true 的机器上不一致：\n    "
        + "\n    ".join(unpinned)
    )


# --- CLI 端到端 -------------------------------------------------------------


def _clone_plugin(tmp_path, plugin_id="screen-recorder"):
    """把真实插件连同共享工具复制到临时目录，保持 plugins/<id>/tools/../../tools 布局。"""
    src = os.path.join(PLUGINS_ROOT, plugin_id)
    if not os.path.isdir(src):
        import pytest

        pytest.skip(f"{plugin_id} 插件不存在，跳过 CLI 端到端校验")
    dst = tmp_path / "plugins" / plugin_id
    shutil.copytree(src, dst)
    shutil.copytree(TOOLS_DIR, tmp_path / "plugins" / "tools")
    return str(dst), os.path.join(str(dst), "tools", "build_plugin.py")


def _run(script, cwd, *args):
    return subprocess.run(
        [sys.executable, script, *args], cwd=cwd, capture_output=True, text=True
    )


def test_check_cli_accepts_freshly_built_plugin(tmp_path):
    """基线：原样复制的真实插件应当通过 --check。"""
    root, script = _clone_plugin(tmp_path)
    proc = _run(script, root, "--check")
    assert proc.returncode == 0, (
        f"未改动的真实插件未通过 --check\n--- stdout ---\n{proc.stdout}\n"
        f"--- stderr ---\n{proc.stderr}"
    )


def test_check_cli_rejects_stale_package(tmp_path):
    """端到端复现历史漂移：改源码 + 刷新 manifest，但不重新打包。

    这正是 v1.0.59→v1.0.102 期间发生的事。用 --no-zip 精确制造「manifest 已同步、
    包仍落后」的状态，确保失败来自包校验而不是 manifest 校验。
    """
    root, script = _clone_plugin(tmp_path)
    target = os.path.join(root, "README.md")
    with open(target, "a", encoding="utf-8") as handle:
        handle.write("\n<!-- 模拟源码更新 -->\n")

    refreshed = _run(script, root, "--no-zip")
    assert refreshed.returncode == 0, f"刷新 manifest 失败：{refreshed.stdout}{refreshed.stderr}"

    proc = _run(script, root, "--check")
    assert proc.returncode != 0, (
        "包已落后于源码，--check 仍然成功，门禁未接入 CLI\n"
        f"--- stdout ---\n{proc.stdout}"
    )
    assert "manifest.json 与源码生成逻辑一致" in proc.stdout, (
        "本用例应只在包校验失败；manifest 校验先失败说明前提没构造对"
    )
    assert "README.md" in proc.stdout, "失败输出应指明是哪个文件落后"


def test_check_cli_rejects_version_mismatch(tmp_path):
    """端到端：只改代码内 __version__ 并完整重建，--check 必须报版本不一致。"""
    root, script = _clone_plugin(tmp_path)
    init_path = os.path.join(root, "src", "screen_recorder", "__init__.py")
    with open(init_path, encoding="utf-8") as handle:
        text = handle.read()
    with open(init_path, "w", encoding="utf-8") as handle:
        handle.write(text.replace('__version__ = "1.1.0"', '__version__ = "9.9.9"'))

    rebuilt = _run(script, root)
    assert rebuilt.returncode == 0, f"重建失败：{rebuilt.stdout}{rebuilt.stderr}"

    proc = _run(script, root, "--check")
    assert proc.returncode != 0, (
        f"__version__ 与 manifest version 不一致，--check 仍然成功\n{proc.stdout}"
    )
    assert "9.9.9" in proc.stdout, "失败输出应给出实际读到的版本号"
    assert "fkrplugin 与磁盘源码一致" in proc.stdout, (
        "本用例已完整重建，包校验应通过；否则说明失败原因不是版本校验"
    )
