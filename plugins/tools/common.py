"""插件构建共享工具。消除 4 个 build_plugin.py 的重复逻辑。

每个插件的 tools/build_plugin.py 只需声明自身的 RUNTIME_FILES 与 MANIFEST_META，
然后调用本模块的 main()。生成逻辑（write_manifest）与校验逻辑（--check）共用
build_manifest_dict，避免两者分叉。

约定（与各插件原脚本一致）
----
* 构建脚本只读写各自插件目录，绝不修改原 fukuaRPA 项目。
* 打包内容 = manifest.json + 运行时文件（src/、README.md、requirements.txt、LICENSE）。
  tools/ 与 tests/ 属开发期产物，不进可分发包。
* 若环境能 import 到原项目的 fukua_rpa.plugin_manifest，会真实跑一遍契约校验，
  确保产出的 manifest 能被 fukuaRPA 插件系统加载；否则仅做基础校验。
* --validate 额外校验顶层 permissions 恰好等于所有动作 permissions 的并集，
  防止插件中心与安装确认页展示的权限摘要与插件实际能力脱节。该校验为纯数据
  比较，不依赖原项目，任何构建环境都会执行。
* --check 除校验 manifest 外，还校验 .fkrplugin 可分发包与磁盘源码一致、以及
  src/<pkg>/__init__.py 的 __version__ 与 manifest version 一致。三者都是
  「同一事实的多处副本」，历史上曾出现包停留在旧代码而 manifest 已更新的漂移。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import os
import re
import sys
import zipfile
from collections.abc import Callable

# 原项目契约模块的**包内**限定名。必须是包路径而非顶层模块名，
# 详见 validate_with_contract 的 INVARIANT 说明。
_CONTRACT_MODULE = "fukua_rpa.plugin_manifest"

# 优先复用主项目的规范实现；插件独立构建时 fukua_rpa 可能不在 sys.path，
# 此时回退到下面的本地 SHA-256 实现（与旧 body 一致）。
try:  # noqa: BLE001
    from fukua_rpa.io_utils import hash_file as _io_hash_file
except Exception:  # 插件构建上下文无 fukua_rpa 可用
    _io_hash_file = None  # type: ignore[assignment]

# 关键元数据字段（不含 files / actions，二者单独处理）。
_META_KEYS = [
    "format", "manifest_version", "id", "name", "version",
    "author", "plugin_type", "api_version", "min_app_version",
    "entrypoint", "permissions", "dependencies", "templates",
]


def hash_file(path: str, chunk_size: int = 1024 * 1024) -> str:
    """计算文件的 SHA-256（十六进制字符串）。

    委托到 fukua_rpa.io_utils.hash_file（若可 import）；否则回退到本地实现。
    保留 (path, chunk_size=1024*1024) 签名与 sha256_of 别名兼容。
    """
    if _io_hash_file is not None:
        return _io_hash_file(path, chunk_size=chunk_size)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


# 兼容旧命名：部分脚本/文档可能引用 sha256_of。
sha256_of = hash_file


def build_files_section(plugin_root: str, runtime_files: list[str]) -> list[dict]:
    """按 RUNTIME_FILES 顺序收集文件并计算 SHA-256，缺少任一文件即报错。"""
    files = []
    for rel in runtime_files:
        abspath = os.path.join(plugin_root, rel)
        if not os.path.isfile(abspath):
            raise SystemExit(f"缺少运行时文件：{rel}")
        files.append({"path": rel, "sha256": hash_file(abspath)})
    return files


def build_manifest_dict(manifest_meta: dict, plugin_root: str, runtime_files: list[str]) -> dict:
    """在内存中生成完整的 manifest 内容（含 files 段 SHA-256）。

    主流程（write_manifest）与 --check 共用本函数，避免生成逻辑与校验逻辑分叉。
    """
    manifest = dict(manifest_meta)
    manifest["files"] = build_files_section(plugin_root, runtime_files)
    return manifest


def write_manifest(plugin_root: str, manifest_meta: dict, runtime_files: list[str]) -> str:
    """生成 manifest.json 并写入插件根目录，返回其绝对路径。

    INVARIANT: 必须以 LF 换行写出。.gitattributes 将 *.json 声明为
    ``text eol=lf``，而 .fkrplugin 是二进制、入库出库都不做换行转换。若这里用
    平台默认换行（Windows 文本模式会把 \\n 变成 \\r\\n），打包进 .fkrplugin 的
    manifest.json 就是 CRLF，检出到磁盘的却是 LF——package_sync_issues 逐字节
    比对二者必然不等，一致性门禁会在任何干净检出上恒红（本机因为磁盘残留着
    构建时写下的 CRLF 反而是绿的，问题只在 CI 暴露）。newline="\\n" 关闭文本
    模式的换行翻译，使「写出的字节 == 入库的字节 == 检出的字节」。
    """
    manifest = build_manifest_dict(manifest_meta, plugin_root, runtime_files)
    dest = os.path.join(plugin_root, "manifest.json")
    with open(dest, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    return dest


# DESIGN: 可复现打包所用的固定时间戳。1980-01-01 00:00:00 是 ZIP 格式能表示的
# 最小时间，被 wheel、reproducible-builds 等广泛用作「无意义时间」的约定值。
_ZIP_FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)


def build_zip(plugin_root: str, manifest_path: str, plugin_id: str) -> str:
    """把 manifest.json 与其 files 段所列文件打包成 <plugin_id>.fkrplugin。

    INVARIANT: 打包必须是确定性的——包内容相同时产出的字节必须完全相同。
    zipfile.ZipFile.write() 会把源文件的 mtime 写进每个条目，而 write_manifest
    每次都重写 manifest.json，导致「源码一字未改，重新构建却产生新的二进制
    diff」。历史上 4 个插件的 .fkrplugin 长期停留在 v1.0.59，很可能正是因为
    开发者面对这种无意义 diff 时选择不暂存二进制包。固定 date_time 与
    create_system 后，只有内容真正变化才会产生 diff，--check 门禁才可持续。

    SAFETY: 安装侧不依赖这些字段——plugin_package._extract_member 手动写文件而
    不恢复 mtime，_validate_and_extract 只校验成员名、符号链接位、file_size 与
    compress_size。external_attr 保持默认 0，其高 16 位不会命中
    _is_symlink 的 0o120000 判定。
    """
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    members = ["manifest.json"] + [f["path"] for f in manifest["files"]]
    dest = os.path.join(plugin_root, f"{plugin_id}.fkrplugin")
    # 清掉旧包，避免残留
    if os.path.exists(dest):
        os.remove(dest)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in members:
            info = zipfile.ZipInfo(filename=rel, date_time=_ZIP_FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            # 固定 create_system，否则同一份源码在 Windows(0) 与 POSIX(3) 上
            # 会产出不同字节，确定性只在单一平台成立。
            info.create_system = 0
            with open(os.path.join(plugin_root, rel), "rb") as handle:
                zf.writestr(info, handle.read())
    return dest


def validate_with_contract(plugin_root: str) -> bool:
    """尝试用原项目契约严格校验 manifest.json。成功返回 True，不可用则 False。

    INVARIANT：真实仓库布局（<codex>/fukua_rpa 是一个包）下**必须**真的跑严格校验，
    不允许降级为 warn。历史缺陷 P2-3 的第二次复发就发生在这里：本函数原先把
    ``<codex>/fukua_rpa`` 塞进 sys.path 后做顶层 ``import plugin_manifest``，而
    plugin_manifest 自引入 ``from .constants import PLUGIN_TYPES`` 起，顶层导入
    必然抛 ``ImportError: attempted relative import with no known parent package``；
    下面的 except 又把它降级成 warn + return False——严格校验因此变成永不执行的
    死代码，4 个插件包全部是在跳过契约校验的情况下产出的。

    正确做法是把 ``<codex>``（而非 ``<codex>/fukua_rpa``）放进 sys.path，按**包**
    导入 ``fukua_rpa.plugin_manifest``，相对导入才有 parent package 可解析。
    该依赖链是纯标准库的：fukua_rpa/__init__.py 只 import .constants，
    constants.py 无任何 import，所以插件构建环境不会因此被拖入 Qt 等重依赖。

    INVARIANT：不要为顶层 ``import plugin_manifest`` 保留任何 fallback 分支。
    那条路径在当前依赖结构下**永远**失败，保留它等于保留一个静默放行的逃生舱。
    tests/test_manifest_validate_p2_3.py::test_real_repo_layout_never_downgrades
    会锁死这一点。
    """
    # plugin_root = <codex>/plugins/<id>；codex 在其上两级。
    codex_root = os.path.dirname(os.path.dirname(plugin_root))
    codex_fukua = os.path.join(codex_root, "fukua_rpa")
    if not os.path.isdir(codex_fukua):
        print(f"[warn] 未找到原项目目录（{codex_fukua}），跳过严格契约校验")
        return False
    # 只在缺失时插入，并在导入后还原，避免把构建期临时路径永久留在 sys.path 上。
    inserted = codex_root not in sys.path
    if inserted:
        sys.path.insert(0, codex_root)
    try:
        plugin_manifest = importlib.import_module(_CONTRACT_MODULE)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 无法 import 原项目契约（{exc}），跳过严格校验")
        return False
    finally:
        if inserted:
            # 被导入模块可能自行改动过 sys.path，移不掉就算了，不值得中断构建。
            with contextlib.suppress(ValueError):
                sys.path.remove(codex_root)
    manifest_path = os.path.join(plugin_root, "manifest.json")
    try:
        with open(manifest_path, encoding="utf-8") as handle:
            plugin_manifest.PluginManifest.parse_json(handle.read())
        print("[ok] 原项目契约校验通过：manifest.json 可被 fukuaRPA 插件系统加载")
        return True
    except Exception as exc:  # noqa: BLE001
        # ``from exc`` 不能省：契约校验失败时真正有诊断价值的是**底层**异常
        # （哪个字段不合法、JSON 哪一行崩的），SystemExit 的 message 只是它的
        # 一句摘要。丢掉异常链后构建日志里就只剩这句摘要，排查全靠猜。
        raise SystemExit(f"[fail] 原项目契约校验未通过：{exc}") from exc


def permissions_union_issues(manifest: dict) -> list[str]:
    """返回顶层 permissions 与动作并集之间的偏差描述（无偏差返回空列表）。

    DESIGN：顶层 permissions 是安装确认与插件中心向用户展示的「这个插件会做什么」
    摘要。运行期并不用它做强制拦截（消费点只有 plugin_registry 的安装日志与
    plugin_center_window 的权限摘要），所以它一旦与各动作实际申请的权限脱节，
    用户看到的就是失真的风险清单：多列会制造不必要的警觉，少列会掩盖真实能力。
    两个方向都算缺陷，因此这里要求集合**相等**而不是单向包含。

    无 actions 的清单直接跳过：非动作型插件的权限语义尚未定义，此处不越权判定。
    """
    actions = manifest.get("actions") or []
    if not actions:
        return []
    top = set(manifest.get("permissions") or [])
    union: set[str] = set()
    owners: dict[str, list[str]] = {}
    for action in actions:
        for perm in action.get("permissions") or []:
            union.add(perm)
            owners.setdefault(perm, []).append(str(action.get("id", "?")))
    issues: list[str] = []
    for perm in sorted(top - union):
        issues.append(f"顶层多余权限：{perm}（没有任何动作申请它，会让用户高估插件能力）")
    for perm in sorted(union - top):
        issues.append(
            f"顶层缺失权限：{perm}（动作 {', '.join(owners[perm])} 已申请，"
            f"但顶层摘要未列出，会让用户低估插件能力）"
        )
    return issues


def check_permissions_union(manifest: dict, verbose: bool = True) -> bool:
    """校验顶层 permissions 恰好等于所有 action.permissions 的并集。"""
    issues = permissions_union_issues(manifest)
    if not issues:
        return True
    if verbose:
        print("[fail] 顶层 permissions 与动作权限并集不一致：")
        for issue in issues:
            print(f"  - {issue}")
        actions = manifest.get("actions") or []
        union = sorted({p for a in actions for p in (a.get("permissions") or [])})
        print(f"  应为：{union}")
        print(f"  实为：{sorted(manifest.get('permissions') or [])}")
    return False


def check_permissions_union_on_disk(plugin_root: str, verbose: bool = True) -> bool:
    """读取磁盘 manifest.json 后做权限并集校验。文件缺失或非法 JSON 视为失败。"""
    disk_path = os.path.join(plugin_root, "manifest.json")
    if not os.path.isfile(disk_path):
        if verbose:
            print(f"[fail] 磁盘上不存在 manifest.json（{disk_path}）")
        return False
    try:
        with open(disk_path, encoding="utf-8") as handle:
            disk = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        if verbose:
            print(f"[fail] 无法读取 manifest.json：{exc}")
        return False
    return check_permissions_union(disk, verbose=verbose)


def package_sync_issues(plugin_root: str, plugin_id: str) -> list[str]:
    """返回 <plugin_id>.fkrplugin 与磁盘 manifest/源码之间的偏差描述。

    DESIGN：.fkrplugin 是 README 所称的「可分发包」，任何人取它直接安装。它是
    磁盘源码的一份冗余副本，一旦落后就会静默分发旧代码。历史上 4 个插件的包
    全部停留在 v1.0.59，而源码已推进到 v1.0.102，其间的数据安全修复（拒绝覆盖
    已有文件、多显示器坐标修正）和资源泄漏修复都没有进入分发物；`--check` 当时
    只比对 manifest 与源码，读不到包内容，因此完全没有报警。

    校验三件事：包内成员集合恰好等于 manifest.files 加 manifest.json 本身、
    每个成员的内容 SHA-256 与磁盘同名文件一致、包可正常打开。
    """
    pkg_path = os.path.join(plugin_root, f"{plugin_id}.fkrplugin")
    if not os.path.isfile(pkg_path):
        return [f"缺少可分发包 {plugin_id}.fkrplugin（请运行 build_plugin.py 生成）"]

    disk_manifest_path = os.path.join(plugin_root, "manifest.json")
    if not os.path.isfile(disk_manifest_path):
        return [f"磁盘上不存在 manifest.json（{disk_manifest_path}）"]
    try:
        with open(disk_manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"无法读取 manifest.json：{exc}"]

    expected = ["manifest.json"] + [
        str(entry.get("path")) for entry in (manifest.get("files") or [])
    ]
    issues: list[str] = []
    try:
        with zipfile.ZipFile(pkg_path) as zf:
            actual = zf.namelist()
            for extra in sorted(set(actual) - set(expected)):
                issues.append(f"包内多余文件：{extra}（不在 manifest.files 中）")
            for rel in expected:
                if rel not in actual:
                    issues.append(f"包内缺少文件：{rel}")
                    continue
                disk_path = os.path.join(plugin_root, rel.replace("/", os.sep))
                if not os.path.isfile(disk_path):
                    issues.append(f"磁盘缺少文件：{rel}（包内存在）")
                    continue
                pkg_sha = hashlib.sha256(zf.read(rel)).hexdigest()
                disk_sha = hash_file(disk_path)
                if pkg_sha != disk_sha:
                    issues.append(
                        f"包内文件与磁盘源码不一致：{rel}"
                        f"（包内 {pkg_sha[:12]}… / 磁盘 {disk_sha[:12]}…）"
                    )
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"无法读取可分发包 {plugin_id}.fkrplugin：{exc}"]
    return issues


def check_package_in_sync(plugin_root: str, plugin_id: str, verbose: bool = True) -> bool:
    """校验可分发包与磁盘源码一致。一致返回 True。"""
    issues = package_sync_issues(plugin_root, plugin_id)
    if not issues:
        return True
    if verbose:
        print(f"[fail] {plugin_id}.fkrplugin 与磁盘源码不一致：")
        for issue in issues:
            print(f"  - {issue}")
        print("  修复：运行 python tools/build_plugin.py 重新打包，并把包一并提交")
    return False


# 只匹配模块顶层（行首无缩进）的 __version__ 赋值，避免命中函数内的同名局部量。
_VERSION_RE = re.compile(r"^__version__\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)


def package_version_issues(
    plugin_root: str, manifest_meta: dict, runtime_files: list[str]
) -> list[str]:
    """返回 src/<pkg>/__init__.py 的 __version__ 与 manifest version 的偏差。

    DESIGN：版本号同样是「同一事实的两处副本」，且两处都靠手写维护。提升插件
    版本时漏改任一处，插件中心显示的版本就会与代码实际代次不符。没有声明
    __version__ 的包直接跳过，不对未来插件强加该约定。
    """
    declared = manifest_meta.get("version")
    issues: list[str] = []
    for rel in runtime_files:
        if not rel.endswith("__init__.py"):
            continue
        path = os.path.join(plugin_root, rel.replace("/", os.sep))
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            issues.append(f"无法读取 {rel}：{exc}")
            continue
        match = _VERSION_RE.search(text)
        if match is None:
            continue
        if match.group(1) != declared:
            issues.append(
                f"{rel} 的 __version__={match.group(1)!r} "
                f"与 manifest version={declared!r} 不一致"
            )
    return issues


def check_package_version(
    plugin_root: str, manifest_meta: dict, runtime_files: list[str], verbose: bool = True
) -> bool:
    """校验代码内 __version__ 与 manifest version 一致。一致返回 True。"""
    issues = package_version_issues(plugin_root, manifest_meta, runtime_files)
    if not issues:
        return True
    if verbose:
        print("[fail] 代码内 __version__ 与 manifest version 不一致：")
        for issue in issues:
            print(f"  - {issue}")
        print("  修复：提升版本时须同时改 build_plugin.py 的 MANIFEST_META 与 __init__.py")
    return False


def _print_diff(a: object, b: object, prefix: str) -> None:
    """递归打印两个值的差异（a=生成值，b=磁盘值）。"""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                print(f"{prefix}.{key} = 缺失(生成) ; 磁盘={b[key]!r}")
            elif key not in b:
                print(f"{prefix}.{key} = {a[key]!r} ; 缺失(磁盘)")
            else:
                _print_diff(a[key], b[key], f"{prefix}.{key}")
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            print(f"{prefix} = 长度不一致 生成={len(a)} 磁盘={len(b)}")
        for i in range(min(len(a), len(b))):
            _print_diff(a[i], b[i], f"{prefix}[{i}]")
    else:
        if a != b:
            print(f"{prefix} = 生成:{a!r} 磁盘:{b!r}")


def check_manifest_in_sync(
    plugin_root: str,
    manifest_meta: dict,
    runtime_files: list[str],
    verbose: bool = True,
    extra_check: Callable[[], bool] | None = None,
) -> bool:
    """在内存中重新生成 manifest，与磁盘 manifest.json 做结构化比较。

    比较范围：files 列表里每个 path 的 sha256、actions（按 id 对齐）、以及
    permissions / id / name / version 等关键元数据字段。一致返回 True，否则
    打印差异并返回 False（供 --check 以非零退出码失败）。

    extra_check：可选的插件专属校验（如 ocr-local 校验 action ids 与代码一致），
    返回 False 会使整体校验失败。

    本函数不写盘、不依赖原项目契约，可在任意环境运行。
    """
    fresh = build_manifest_dict(manifest_meta, plugin_root, runtime_files)
    disk_path = os.path.join(plugin_root, "manifest.json")
    if not os.path.isfile(disk_path):
        if verbose:
            print(f"[fail] 磁盘上不存在 manifest.json（{disk_path}）")
        return False
    with open(disk_path, encoding="utf-8") as handle:
        disk = json.load(handle)

    ok = True

    # 顶层字段集合一致（防止任一侧多/缺字段）
    fresh_keys = set(fresh)
    disk_keys = set(disk)
    if fresh_keys != disk_keys:
        ok = False
        if verbose:
            print(f"[fail] manifest 顶层字段集合不一致：磁盘={sorted(disk_keys)} 生成={sorted(fresh_keys)}")
            missing = fresh_keys - disk_keys
            extra = disk_keys - fresh_keys
            if missing:
                print(f"  磁盘缺少：{sorted(missing)}")
            if extra:
                print(f"  磁盘多余：{sorted(extra)}")

    # 关键元数据字段逐项一致（不含 files / actions，单独处理）
    for key in _META_KEYS:
        if fresh.get(key) != disk.get(key):
            ok = False
            if verbose:
                print(f"[fail] 元数据字段不一致：{key}")
                print(f"  生成: {fresh.get(key)!r}")
                print(f"  磁盘: {disk.get(key)!r}")

    # actions：按 id 对齐逐字段比较
    fresh_actions = {a["id"]: a for a in fresh.get("actions", [])}
    disk_actions = {a["id"]: a for a in disk.get("actions", [])}
    if set(fresh_actions) != set(disk_actions):
        ok = False
        if verbose:
            print(f"[fail] actions id 集合不一致：磁盘={sorted(disk_actions)} 生成={sorted(fresh_actions)}")
    else:
        for aid in fresh_actions:
            if fresh_actions[aid] != disk_actions[aid]:
                ok = False
                if verbose:
                    print(f"[fail] action 不一致：{aid}")
                    _print_diff(fresh_actions[aid], disk_actions[aid], prefix=f"  {aid}")

    # files：按 path 对齐比较 sha256（顺序也保持一致）
    fresh_files = {f["path"]: f for f in fresh.get("files", [])}
    disk_files = {f["path"]: f for f in disk.get("files", [])}
    if list(fresh_files) != list(disk_files):
        ok = False
        if verbose:
            print(f"[fail] files 列表顺序/集合不一致：磁盘={list(disk_files)} 生成={list(fresh_files)}")
    for rel in fresh_files:
        f_sha = fresh_files[rel].get("sha256")
        # FR-20260813-D12-001: disk_files 可能缺少 rel（生成产物未落盘或反之），
        # 用 .get 兜底，避免集合差触发 KeyError 使整个 --check 门禁崩溃。
        d_sha = disk_files.get(rel, {}).get("sha256")
        if f_sha != d_sha:
            ok = False
            if verbose:
                print(f"[fail] 文件 SHA-256 不一致：{rel}")
                print(f"  生成: {f_sha}")
                print(f"  磁盘: {d_sha}")

    # 插件专属校验（如 ocr-local 的 action ids 与代码一致性）
    if extra_check is not None and not extra_check():
        ok = False

    return ok


def main(
    plugin_root: str,
    manifest_meta: dict,
    runtime_files: list[str],
    description: str,
    extra_check: Callable[[], bool] | None = None,
) -> None:
    """插件构建 CLI 入口：生成 manifest.json（含逐文件 SHA-256）并打包成 .fkrplugin。

    plugin_root：插件根目录（含 manifest.json 与 src/）。
    manifest_meta：插件特有的清单元数据（id、name、version、actions 等）。
    runtime_files：进包且写入 manifest.files 的运行时文件（相对插件根目录）。
    description：argparse 描述（如「构建本地 OCR 插件」）。
    extra_check：可选的插件专属 --check 校验。
    """
    plugin_id = manifest_meta["id"]
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--no-zip", action="store_true", help="只生成 manifest.json，不打包")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="用原项目契约严格校验 manifest.json，并校验顶层 permissions 等于动作权限并集",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "校验构建产物无漂移：manifest.json 与源码生成逻辑一致、"
            ".fkrplugin 与磁盘源码一致、代码内 __version__ 与 manifest version 一致"
        ),
    )
    args = parser.parse_args()

    if args.check:
        # 三项校验都不写盘、不依赖原项目，任何环境都能执行。逐项报告而不是
        # 短路，一次运行就能看到全部漂移点，避免修一个跑一次。
        if not check_manifest_in_sync(plugin_root, manifest_meta, runtime_files,
                                      extra_check=extra_check):
            raise SystemExit("[fail] manifest.json 与源码生成逻辑不一致，请重新运行 build_plugin.py 生成")
        print("[ok] manifest.json 与源码生成逻辑一致（无漂移）")

        version_ok = check_package_version(plugin_root, manifest_meta, runtime_files)
        if version_ok:
            print("[ok] 代码内 __version__ 与 manifest version 一致")
        package_ok = check_package_in_sync(plugin_root, plugin_id)
        if package_ok:
            print(f"[ok] {plugin_id}.fkrplugin 与磁盘源码一致（无漂移）")
        if not (version_ok and package_ok):
            raise SystemExit("[fail] 构建产物存在漂移，请重新运行 build_plugin.py")
        return

    if args.validate:
        # 顺序有意为之：并集校验不依赖原项目，任何环境都能执行，先跑它可以在
        # 缺少 fukua_rpa 的独立插件构建环境里仍然守住权限摘要不失真。
        if not check_permissions_union_on_disk(plugin_root):
            raise SystemExit(
                "[fail] 顶层 permissions 必须等于所有动作 permissions 的并集，"
                "请修正 build_plugin.py 的 MANIFEST_META 后重新生成 manifest.json"
            )
        print("[ok] 顶层 permissions 与动作权限并集一致")
        validate_with_contract(plugin_root)
        return

    manifest_path = write_manifest(plugin_root, manifest_meta, runtime_files)
    print(f"[ok] 生成 {os.path.relpath(manifest_path, plugin_root)}")

    validate_with_contract(plugin_root)

    if not args.no_zip:
        zip_path = build_zip(plugin_root, manifest_path, plugin_id)
        size = os.path.getsize(zip_path)
        print(f"[ok] 打包 {os.path.relpath(zip_path, plugin_root)} ({size} 字节)")
