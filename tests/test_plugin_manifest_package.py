"""Unittests for the pure plugin data layer (manifest, package, protocol, registry).

Run with ``python -m unittest -v tests.test_plugin_manifest_package`` or via
``scripts/run_impacted_tests.py``. No PySide/Qt is imported.
"""

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from unittest import mock

from fukua_rpa.plugin_manifest import (
    ALL_PERMISSION_VALUES,
    PluginManifest,
    PluginManifestError,
    api_compatible,
    compare_versions,
    permission_set_valid,
)
from fukua_rpa.plugin_package import (
    PackageLimits,
    PluginPackageError,
    _check_member_name,
    install_from_file,
    load_installed_manifest,
    uninstall,
)
from fukua_rpa.plugin_protocol import (
    MESSAGE_SIZE_LIMIT,
    PluginProtocolError,
    deframe_message,
    frame_message,
    make_request,
    make_response,
    make_stop,
    parse_request,
    parse_response,
    parse_stop,
)
from fukua_rpa.plugin_registry import (
    PluginRegistry,
    PluginStatus,
)


def valid_manifest(plugin_id="com.example.report", version="1.0.0", **kw):
    base = {
        "format": "fukuaRPA.plugin",
        "manifest_version": 1,
        "id": plugin_id,
        "name": "Report Tools",
        "version": version,
        "author": "Example",
        "plugin_type": "declarative",
        "api_version": 1,
        "min_app_version": "1.0.0",
        "entrypoint": None,
        "permissions": ["files.plugin_private_data"],
        "actions": [
            {
                "id": "extract_table",
                "name": "Extract Table",
                "description": "Extracts a table",
                "permissions": [],
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            }
        ],
        "templates": [
            {
                "id": "daily_report",
                "name": "Daily Report",
                "description": "Daily report template",
                "steps": [{"type": 1}],
            }
        ],
        "files": [],
        "dependencies": [],
    }
    base.update(kw)
    return base


def file_entries(contents):
    return [
        {"path": rel, "sha256": hashlib.sha256(data).hexdigest()}
        for rel, data in contents.items()
    ]


def write_package(zip_path, manifest_dict, contents=None, *, extra_members=None,
                  symlink_member=None):
    contents = contents or {}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest_dict))
        for rel, data in contents.items():
            zf.writestr(rel, data)
        for name, data in (extra_members or {}).items():
            zf.writestr(name, data)
        if symlink_member:
            info = zipfile.ZipInfo(symlink_member)
            info.external_attr = (0o120000 << 16)  # Unix symlink bit
            zf.writestr(info, b"")


class ManifestValidationTests(unittest.TestCase):
    def test_parse_valid_minimal(self):
        m = PluginManifest.parse_json(json.dumps(valid_manifest()))
        self.assertEqual(m.id, "com.example.report")
        self.assertEqual(m.actions[0].id, "extract_table")
        self.assertEqual(m.templates[0].id, "daily_report")
        self.assertIn("files.plugin_private_data", m.permissions)

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(PluginManifestError):
            PluginManifest.parse_json(
                '{"format":"fukuaRPA.plugin","format":"x","id":"a"}'
            )

    def test_missing_required_field(self):
        m = valid_manifest()
        del m["author"]
        with self.assertRaises(PluginManifestError):
            PluginManifest.from_dict(m)

    def test_unknown_field_rejected(self):
        m = valid_manifest()
        m["surprise"] = 1
        with self.assertRaises(PluginManifestError):
            PluginManifest.from_dict(m)

    def test_bad_version_format(self):
        with self.assertRaises(PluginManifestError):
            PluginManifest.from_dict(valid_manifest(version="1.0"))

    def test_bad_permission(self):
        with self.assertRaises(PluginManifestError):
            PluginManifest.from_dict(
                valid_manifest(permissions=["screen.capture_region", "nope.net"])
            )

    def test_duplicate_action_id(self):
        m = valid_manifest()
        m["actions"] = [
            {"id": "a", "name": "A", "description": ""},
            {"id": "a", "name": "B", "description": ""},
        ]
        with self.assertRaises(PluginManifestError):
            PluginManifest.from_dict(m)

    def test_unsafe_file_path(self):
        m = valid_manifest(files=[{"path": "../escape", "sha256": "0" * 64}])
        with self.assertRaises(PluginManifestError):
            PluginManifest.from_dict(m)

    def test_permission_set_valid(self):
        self.assertTrue(permission_set_valid(["input.mouse", "input.keyboard"]))
        self.assertFalse(permission_set_valid("input.mouse"))
        self.assertFalse(permission_set_valid(["screen.capture_region", "net"]))
        self.assertEqual(len(ALL_PERMISSION_VALUES), 11)

    def test_compare_versions(self):
        self.assertEqual(compare_versions("1.0.0", "1.0.0"), 0)
        self.assertEqual(compare_versions("1.2.0", "1.10.0"), -1)
        self.assertEqual(compare_versions("2.0.0", "1.9.9"), 1)

    def test_api_compatible(self):
        m = PluginManifest.from_dict(valid_manifest())
        self.assertTrue(api_compatible(m, "1.0.0"))
        self.assertTrue(api_compatible(m, "1.5.0"))
        self.assertFalse(api_compatible(m, "0.9.0"))
        # api_version out of supported range
        m2 = PluginManifest.from_dict(valid_manifest(api_version=2))
        self.assertFalse(api_compatible(m2, "1.0.0"))


class BoundedManifestParseTests(unittest.TestCase):
    """FR-a01-002/003: 清单属不可信外部输入，必须经 json_safety 有界解析
    （大小/深度/节点数上限 + 拒 NaN/Infinity），且 actions/templates/files 数量
    与依赖 id 长度有独立上限。"""

    def test_nan_rejected(self):
        with self.assertRaises(PluginManifestError):
            PluginManifest.parse_json(
                '{"format":"fukuaRPA.plugin","id":"a","bad":NaN}'
            )

    def test_infinity_rejected(self):
        with self.assertRaises(PluginManifestError):
            PluginManifest.parse_json(
                '{"format":"fukuaRPA.plugin","id":"a","bad":Infinity}'
            )

    def test_deep_nesting_rejected(self):
        payload = '{"format":"fukuaRPA.plugin","id":"a","deep":' + (
            "[" * 5000 + "]" * 5000
        ) + "}"
        with self.assertRaises(PluginManifestError):
            PluginManifest.parse_json(payload)

    def test_too_many_actions_rejected(self):
        m = valid_manifest()
        template_action = m["actions"][0]
        m["actions"] = [
            {**template_action, "id": f"action-{i}"} for i in range(129)
        ]
        with self.assertRaises(PluginManifestError) as ctx:
            PluginManifest.parse_json(json.dumps(m))
        self.assertIn("actions 数量超过上限", str(ctx.exception))

    def test_oversized_dependency_id_rejected(self):
        m = valid_manifest()
        m["dependencies"] = ["x" * 200]
        with self.assertRaises(PluginManifestError) as ctx:
            PluginManifest.parse_json(json.dumps(m))
        self.assertIn("依赖插件 id 过长", str(ctx.exception))


class PackageSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "plugins")
        os.makedirs(self.base, exist_ok=True)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _small_limits(self):
        return PackageLimits(
            max_single_file_bytes=1_000_000,
            max_file_count=50,
            max_total_unpacked_bytes=10_000_000,
            max_compression_ratio=5.0,
            max_manifest_bytes=4096,
        )

    def test_valid_install(self):
        contents = {
            "resources/icon.png": b"PNGDATA",
            "README.md": b"readme",
        }
        m = valid_manifest(files=file_entries(contents))
        pkg = os.path.join(self.tmp, "good.fkrplugin")
        write_package(pkg, m, contents)
        result = install_from_file(pkg, self.base, limits=self._small_limits())
        self.assertEqual(result.id, "com.example.report")
        # files actually extracted and hashes match
        extracted = os.path.join(self.base, "com.example.report", "resources", "icon.png")
        self.assertTrue(os.path.isfile(extracted))
        # re-load from disk
        reloaded = load_installed_manifest("com.example.report", self.base)
        self.assertEqual(reloaded.version, "1.0.0")

    def test_file_hash_mismatch_rejected(self):
        contents = {"data.bin": b"real"}
        m = valid_manifest(files=[{"path": "data.bin", "sha256": "0" * 64}])
        pkg = os.path.join(self.tmp, "badhash.fkrplugin")
        write_package(pkg, m, contents)
        with self.assertRaises(PluginPackageError):
            install_from_file(pkg, self.base, limits=self._small_limits())

    def test_path_traversal_rejected(self):
        pkg = os.path.join(self.tmp, "traversal.fkrplugin")
        write_package(pkg, valid_manifest(), extra_members={"../../evil.txt": b"x"})
        with self.assertRaises(PluginPackageError):
            install_from_file(pkg, self.base, limits=self._small_limits())

    def test_absolute_path_rejected(self):
        pkg = os.path.join(self.tmp, "abs.fkrplugin")
        write_package(pkg, valid_manifest(), extra_members={"/abs.txt": b"x"})
        with self.assertRaises(PluginPackageError):
            install_from_file(pkg, self.base, limits=self._small_limits())

    def test_manifest_case_variant_rejected(self):
        # Windows filesystems are case-insensitive: "Manifest.json" would
        # overwrite the canonical manifest.json on extraction and bypass the
        # identity check. The package must be rejected during validation, not
        # merely at the exact-match manifest lookup.
        pkg = os.path.join(self.tmp, "case_variant.fkrplugin")
        write_package(
            pkg,
            valid_manifest(),
            extra_members={"Manifest.json": b"evil"},
        )
        with self.assertRaises(PluginPackageError):
            install_from_file(pkg, self.base, limits=self._small_limits())

    def test_drive_relative_member_rejected(self):
        """FR-20260812-D4-002: 盘符相对路径成员（"C:evil"）必须先拒绝——
        os.path.join 会丢弃临时目录前缀写出越界。"""
        pkg = os.path.join(self.tmp, "drive_rel.fkrplugin")
        write_package(pkg, valid_manifest(), extra_members={"C:evil.txt": b"x"})
        with self.assertRaises(PluginPackageError):
            install_from_file(pkg, self.base, limits=self._small_limits())

    def test_ntfs_ads_member_rejected(self):
        """FR-20260812-D4-002: 含冒号成员名（NTFS ADS）必须拒绝。"""
        pkg = os.path.join(self.tmp, "ads.fkrplugin")
        write_package(pkg, valid_manifest(), extra_members={"file.txt:stream": b"x"})
        with self.assertRaises(PluginPackageError):
            install_from_file(pkg, self.base, limits=self._small_limits())

    def test_trailing_dot_or_space_member_name_rejected(self):
        """FR-SCOPE007-003: Windows 会剥掉文件名尾随的点与空格。

        ``manifest.json.`` 与 ``manifest.json `` 在磁盘上都落成规范的
        ``manifest.json``，覆盖真清单并绕过步骤 4 的身份校验。守卫必须拒绝这类
        成员名，口径与 ``profile_package._safe_member_name`` 一致。
        """

        for name in (
            "manifest.json.",
            "manifest.json ",
            "Manifest.json.",
            "resources/icon.png ",
            "dir./file.txt",
            "dir /file.txt",
        ):
            with self.subTest(member=name), self.assertRaises(PluginPackageError):
                _check_member_name(name)

    def test_legal_member_names_still_accepted(self):
        """边界：规范化不得过宽而误拒合法成员名。"""

        for name in (
            "manifest.json",
            "resources/icon.png",
            "my file.txt",
            "dir with space/file.txt",
            "data.bin",
        ):
            with self.subTest(member=name):
                _check_member_name(name)

    def test_declared_manifest_equivalent_member_is_rejected(self):
        """ZIP 级复核：被声明的等价清单成员不得被解压（否则真清单被覆盖）。"""

        contents = {"manifest.json.": b"evil"}
        manifest = valid_manifest(files=file_entries(contents))
        pkg = os.path.join(self.tmp, "trailing_dot.fkrplugin")
        write_package(pkg, manifest, contents)
        with self.assertRaises(PluginPackageError):
            install_from_file(pkg, self.base, limits=self._small_limits())

    def test_plugin_and_profile_parsers_agree_on_member_names(self):
        """FR-SCOPE007-003: 两个 ZIP 解析器的规范化口径必须一致。

        只覆盖"尾随点/空格"这一维：这是两个解析器曾出现分歧的维度。
        """

        from fukua_rpa import profile_package

        rejected_by_both = (
            "manifest.json.",
            "manifest.json ",
            "resources/icon.png ",
            "dir./file.txt",
            "dir /file.txt",
            "data.bin.",
        )
        accepted_by_both = (
            "manifest.json",
            "resources/icon.png",
            "my file.txt",
            "dir with space/file.txt",
            "data.bin",
        )

        def _plugin_accepts(name: str) -> bool:
            try:
                _check_member_name(name)
            except PluginPackageError:
                return False
            return True

        def _profile_accepts(name: str) -> bool:
            try:
                profile_package._safe_member_name(name)
            except ValueError:
                return False
            return True

        for name in rejected_by_both:
            with self.subTest(member=name, expect="reject"):
                self.assertFalse(_plugin_accepts(name), f"插件解析器放行了 {name!r}")
                self.assertFalse(_profile_accepts(name), f"方案解析器放行了 {name!r}")
        for name in accepted_by_both:
            with self.subTest(member=name, expect="accept"):
                self.assertTrue(_plugin_accepts(name), f"插件解析器误拒了 {name!r}")
                self.assertTrue(_profile_accepts(name), f"方案解析器误拒了 {name!r}")

    def test_symlink_rejected(self):
        pkg = os.path.join(self.tmp, "sym.fkrplugin")
        write_package(pkg, valid_manifest(), symlink_member="link")
        with self.assertRaises(PluginPackageError):
            install_from_file(pkg, self.base, limits=self._small_limits())

    def test_undeclared_file_rejected(self):
        # A file that is safe by path but absent from manifest.files must be
        # rejected: it escapes the manifest's integrity scope and could ship
        # code that is never accounted for (the manifest-outside-code gap).
        contents = {"data.bin": b"x"}
        pkg = os.path.join(self.tmp, "extra.fkrplugin")
        write_package(
            pkg,
            valid_manifest(files=file_entries(contents)),
            contents,
            extra_members={"undeclared.py": b"import os"},
        )
        with self.assertRaises(PluginPackageError):
            install_from_file(pkg, self.base, limits=self._small_limits())

    def test_missing_declared_file_marks_corrupt(self):
        contents = {"data.bin": b"x"}
        limits = self._small_limits()
        pkg = os.path.join(self.tmp, "m.fkrplugin")
        write_package(pkg, valid_manifest(files=file_entries(contents)), contents)
        install_from_file(pkg, self.base, limits=limits)
        # Simulate tampering / partial install by removing a declared file.
        installed = os.path.join(self.base, "com.example.report", "data.bin")
        os.remove(installed)
        registry = PluginRegistry(self.base, "1.0.0")
        record = registry._records.get("com.example.report")
        self.assertIsNotNone(record)
        self.assertTrue(record.corrupt)
        self.assertIn("data.bin", record.corrupt_reason)

    def test_reserved_device_name_rejected(self):
        pkg = os.path.join(self.tmp, "dev.fkrplugin")
        write_package(pkg, valid_manifest(), extra_members={"CON": b"x"})
        with self.assertRaises(PluginPackageError):
            install_from_file(pkg, self.base, limits=self._small_limits())

    def test_oversize_single_file_rejected(self):
        pkg = os.path.join(self.tmp, "big.fkrplugin")
        write_package(pkg, valid_manifest(), contents={"big": b"x" * 2_000_000})
        with self.assertRaises(PluginPackageError):
            install_from_file(pkg, self.base, limits=self._small_limits())

    def test_extreme_compression_rejected(self):
        # 200k identical bytes compress to a few hundred bytes -> ratio ~1000 > 5
        pkg = os.path.join(self.tmp, "bomb.fkrplugin")
        write_package(pkg, valid_manifest(), contents={"big": b"A" * 200_000})
        with self.assertRaises(PluginPackageError):
            install_from_file(pkg, self.base, limits=self._small_limits())

    def test_upgrade_ok_downgrade_refused(self):
        contents = {"data.bin": b"x"}
        limits = self._small_limits()

        pkg_old = os.path.join(self.tmp, "v1.fkrplugin")
        write_package(pkg_old, valid_manifest(version="1.0.0", files=file_entries(contents)), contents)
        install_from_file(pkg_old, self.base, limits=limits)

        # same id, higher version -> allowed
        pkg_new = os.path.join(self.tmp, "v11.fkrplugin")
        write_package(pkg_new, valid_manifest(version="1.0.1", files=file_entries(contents)), contents)
        install_from_file(pkg_new, self.base, limits=limits)
        self.assertEqual(
            load_installed_manifest("com.example.report", self.base).version, "1.0.1"
        )

        # downgrade -> refused
        pkg_down = os.path.join(self.tmp, "v09.fkrplugin")
        write_package(pkg_down, valid_manifest(version="0.9.0", files=file_entries(contents)), contents)
        with self.assertRaises(PluginPackageError):
            install_from_file(pkg_down, self.base, limits=limits)

        # downgrade with explicit override -> allowed
        install_from_file(pkg_down, self.base, limits=limits, allow_downgrade=True)
        self.assertEqual(
            load_installed_manifest("com.example.report", self.base).version, "0.9.0"
        )

    def test_uninstall_removes_dir(self):
        contents = {"data.bin": b"x"}
        limits = self._small_limits()
        pkg = os.path.join(self.tmp, "u.fkrplugin")
        write_package(pkg, valid_manifest(files=file_entries(contents)), contents)
        install_from_file(pkg, self.base, limits=limits)
        uninstall("com.example.report", self.base)
        with self.assertRaises(PluginManifestError):
            load_installed_manifest("com.example.report", self.base)

    # -- RUN01: 设计 §5.6「运行中的插件不能被覆盖」 -------------------------

    def test_active_run_blocks_overwrite_and_keeps_old_files(self):
        limits = self._small_limits()
        old_contents = {"data.bin": b"old"}
        pkg_old = os.path.join(self.tmp, "run_v1.fkrplugin")
        write_package(
            pkg_old,
            valid_manifest(version="1.0.0", files=file_entries(old_contents)),
            old_contents,
        )
        install_from_file(pkg_old, self.base, limits=limits)

        new_contents = {"data.bin": b"new"}
        pkg_new = os.path.join(self.tmp, "run_v2.fkrplugin")
        write_package(
            pkg_new,
            valid_manifest(version="1.0.1", files=file_entries(new_contents)),
            new_contents,
        )
        with self.assertRaises(PluginPackageError) as ctx:
            install_from_file(
                pkg_new,
                self.base,
                limits=limits,
                active_run_resolver=lambda _pid: 1,
            )
        self.assertIn("正在运行", str(ctx.exception))

        # 旧安装必须原封不动：版本、文件内容、目录都不得被替换。
        self.assertEqual(
            load_installed_manifest("com.example.report", self.base).version, "1.0.0"
        )
        with open(
            os.path.join(self.base, "com.example.report", "data.bin"), "rb"
        ) as fh:
            self.assertEqual(fh.read(), b"old")
        # 失败路径不得留下临时解压目录或 .bkp 残留。
        leftovers = [
            name
            for name in os.listdir(self.base)
            if name.startswith("fkrplugin_") or name.endswith(".bkp")
        ]
        self.assertEqual(leftovers, [])

    def test_active_run_zero_allows_overwrite(self):
        limits = self._small_limits()
        contents = {"data.bin": b"x"}
        pkg_old = os.path.join(self.tmp, "z1.fkrplugin")
        write_package(
            pkg_old, valid_manifest(version="1.0.0", files=file_entries(contents)), contents
        )
        install_from_file(pkg_old, self.base, limits=limits)

        pkg_new = os.path.join(self.tmp, "z2.fkrplugin")
        write_package(
            pkg_new, valid_manifest(version="1.0.2", files=file_entries(contents)), contents
        )
        install_from_file(
            pkg_new, self.base, limits=limits, active_run_resolver=lambda _pid: 0
        )
        self.assertEqual(
            load_installed_manifest("com.example.report", self.base).version, "1.0.2"
        )

    def test_active_run_resolver_not_consulted_on_first_install(self):
        """首次安装没有可被替换的运行中文件，解析器不应被调用。"""
        limits = self._small_limits()
        contents = {"data.bin": b"x"}
        pkg = os.path.join(self.tmp, "first.fkrplugin")
        write_package(pkg, valid_manifest(files=file_entries(contents)), contents)

        calls = []

        def _resolver(plugin_id):
            calls.append(plugin_id)
            return 99

        install_from_file(pkg, self.base, limits=limits, active_run_resolver=_resolver)
        self.assertEqual(calls, [])
        self.assertEqual(
            load_installed_manifest("com.example.report", self.base).version, "1.0.0"
        )

    def test_active_run_guard_absent_when_resolver_omitted(self):
        """DESIGN: 未提供运行状态的调用方（如离线工具）保持既有行为不变。"""
        limits = self._small_limits()
        contents = {"data.bin": b"x"}
        pkg_old = os.path.join(self.tmp, "n1.fkrplugin")
        write_package(
            pkg_old, valid_manifest(version="1.0.0", files=file_entries(contents)), contents
        )
        install_from_file(pkg_old, self.base, limits=limits)
        pkg_new = os.path.join(self.tmp, "n2.fkrplugin")
        write_package(
            pkg_new, valid_manifest(version="1.0.3", files=file_entries(contents)), contents
        )
        install_from_file(pkg_new, self.base, limits=limits)
        self.assertEqual(
            load_installed_manifest("com.example.report", self.base).version, "1.0.3"
        )


class ProtocolTests(unittest.TestCase):
    def test_request_round_trip(self):
        env = make_request("r1", "s1", "com.example.report", "extract_table",
                           123456, {"q": 1})
        framed = frame_message(env)
        self.assertIsInstance(framed, bytes)
        decoded = deframe_message(framed)
        self.assertEqual(decoded["kind"], "request")
        self.assertEqual(decoded["payload"]["run_id"], "r1")
        req = parse_request(decoded)
        self.assertEqual(req.plugin_id, "com.example.report")
        self.assertEqual(req.payload["q"], 1)

    def test_response_round_trip(self):
        env = make_response("r1", "s1", True, result={"ok": 1}, error=None)
        decoded = deframe_message(frame_message(env))
        resp = parse_response(decoded)
        self.assertTrue(resp.ok)
        self.assertEqual(resp.result, {"ok": 1})

    def test_stop_round_trip(self):
        env = make_stop("r1", "s1")
        decoded = deframe_message(frame_message(env))
        stop = parse_stop(decoded)
        self.assertEqual(stop.run_id, "r1")

    def test_frame_enforces_size_limit(self):
        huge = "x" * (MESSAGE_SIZE_LIMIT + 10)
        env = make_response("r", "s", True, result=huge)
        with self.assertRaises(PluginProtocolError):
            frame_message(env)

    def test_deframe_rejects_oversize_declared_length(self):
        bad = (MESSAGE_SIZE_LIMIT + 1).to_bytes(4, "big") + b"{}"
        with self.assertRaises(PluginProtocolError):
            deframe_message(bad)

    def test_deframe_rejects_truncated(self):
        framed = frame_message(make_stop("r", "s"))
        with self.assertRaises(PluginProtocolError):
            deframe_message(framed[:-3])

    def test_framing_rejects_wrong_version(self):
        env = make_request("r", "s", "p", "a", 1)
        env["version"] = 99
        with self.assertRaises(PluginProtocolError):
            frame_message(env)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "plugins")
        os.makedirs(self.base, exist_ok=True)
        self.contents = {"data.bin": b"x"}
        self.limits = PackageLimits(
            max_single_file_bytes=1_000_000,
            max_file_count=50,
            max_total_unpacked_bytes=10_000_000,
            max_compression_ratio=5.0,
            max_manifest_bytes=4096,
        )
        pkg = os.path.join(self.tmp, "good.fkrplugin")
        write_package(
            pkg,
            valid_manifest(files=file_entries(self.contents)),
            self.contents,
        )
        install_from_file(pkg, self.base, limits=self.limits)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _registry(self, app_version="1.0.0"):
        return PluginRegistry(self.base, app_version)

    def test_scan_lists_installed(self):
        reg = self._registry()
        records = reg.list_plugins()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].plugin_id, "com.example.report")
        self.assertEqual(records[0].status, PluginStatus.ENABLED)

    def test_scan_skips_shared_toolchain_directory_without_manifest(self):
        # FR-014：源码检出时 install_base_dir 就是仓库的 plugins/，其中
        # plugins/tools（共享构建工具链）既无 manifest.json 也无 src/，不得被登记为
        # "损坏插件"并显示"插件已损坏"。
        # DEC-20260920-002：跳过只适用于"命中精确名单且缺清单"的基础设施目录；
        # 无清单的残留插件根目录改为以 CORRUPT 登记并可见，见下方独立用例。
        toolchain = os.path.join(self.base, "tools")
        os.makedirs(toolchain, exist_ok=True)
        with open(
            os.path.join(toolchain, "common.py"), "w", encoding="utf-8"
        ) as handle:
            handle.write("# shared build toolchain\n")

        reg = self._registry()
        ids = {record.plugin_id for record in reg.list_plugins()}
        self.assertNotIn("tools", ids)
        self.assertIn("com.example.report", ids)

    def test_scan_registers_infrastructure_dir_when_it_declares_a_manifest(self):
        # DEC-20260920-002①：manifest.json 是插件的明确声明，优先级高于目录名称。
        # 目录名命中 tools 但含有效清单时必须登记并校验；此处清单 id 与目录名一致
        # ⇒ 状态应为 ENABLED（而不是被名称跳过）。
        toolchain = os.path.join(self.base, "tools")
        os.makedirs(toolchain, exist_ok=True)
        with open(
            os.path.join(toolchain, "manifest.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump(valid_manifest(plugin_id="tools"), handle)

        reg = self._registry()
        records = {record.plugin_id: record for record in reg.list_plugins()}
        self.assertIn("tools", records)
        self.assertEqual(records["tools"].status, PluginStatus.ENABLED)

    def test_scan_registers_stray_plugin_root_as_corrupt(self):
        # DEC-20260920-002③：除明确忽略名单外，"既无清单又无有效 src"的插件根目录
        # 残留必须保留 CORRUPT 可见性，便于用户发现损坏或卸载残留。
        os.makedirs(os.path.join(self.base, "stray_directory"), exist_ok=True)

        reg = self._registry()
        records = {record.plugin_id: record for record in reg.list_plugins()}
        self.assertIn("stray_directory", records)
        self.assertEqual(records["stray_directory"].status, PluginStatus.CORRUPT)

    def test_scan_skip_uses_exact_name_not_substring(self):
        # DEC-20260920-002②：跳过规则只能使用基础设施目录精确名单，不得子串匹配。
        # tools_extra 无清单无 src，名称包含 tools 但不是精确命中 ⇒ 必须登记为 CORRUPT。
        os.makedirs(os.path.join(self.base, "tools_extra"), exist_ok=True)

        reg = self._registry()
        records = {record.plugin_id: record for record in reg.list_plugins()}
        self.assertIn("tools_extra", records)
        self.assertEqual(records["tools_extra"].status, PluginStatus.CORRUPT)

    def test_scan_still_flags_a_real_corrupt_plugin(self):
        # 反向：仍有 manifest.json 或 src/ 的损坏插件必须继续可见并标记为 CORRUPT。
        broken = os.path.join(self.base, "broken.plugin")
        os.makedirs(os.path.join(broken, "src"), exist_ok=True)

        reg = self._registry()
        records = {record.plugin_id: record for record in reg.list_plugins()}
        self.assertIn("broken.plugin", records)
        self.assertEqual(records["broken.plugin"].status, PluginStatus.CORRUPT)

    def test_enable_disable(self):
        reg = self._registry()
        reg.disable("com.example.report")
        self.assertEqual(reg.status("com.example.report"), PluginStatus.DISABLED)
        reg.enable("com.example.report")
        self.assertEqual(reg.status("com.example.report"), PluginStatus.ENABLED)

    def test_reference_count_resolver_and_set(self):
        reg = self._registry()
        self.assertEqual(reg.reference_count("com.example.report",
                                             resolver=lambda pid: 7), 7)
        self.assertEqual(
            reg.reference_count(
                "com.example.report", referenced_ids=["com.example.report", "x"]
            ),
            1,
        )

    def test_uninstall_blocks_when_referenced(self):
        reg = self._registry()
        ok, count = reg.uninstall(
            "com.example.report", referenced_ids=["com.example.report"]
        )
        self.assertFalse(ok)
        self.assertEqual(count, 1)
        # still installed
        self.assertTrue(os.path.isdir(os.path.join(self.base, "com.example.report")))

        ok, count = reg.uninstall("com.example.report", referenced_ids=set())
        self.assertTrue(ok)
        self.assertEqual(count, 0)
        self.assertFalse(
            os.path.isdir(os.path.join(self.base, "com.example.report"))
        )

    def test_get_action_compatible(self):
        reg = self._registry()
        action = reg.get_action("com.example.report", "extract_table")
        self.assertIsNotNone(action)
        self.assertIsNone(reg.get_action("com.example.report", "missing"))

    def test_corrupt_manifest_status(self):
        # write a plugin dir with an invalid manifest directly
        bad_dir = os.path.join(self.base, "com.example.bad")
        os.makedirs(bad_dir, exist_ok=True)
        with open(os.path.join(bad_dir, "manifest.json"), "w", encoding="utf-8") as fh:
            fh.write('{"format":"fukuaRPA.plugin"')  # truncated JSON
        reg = self._registry()
        record = next(
            r for r in reg.list_plugins() if r.plugin_id == "com.example.bad"
        )
        self.assertEqual(record.status, PluginStatus.CORRUPT)

    def test_incompatible_status(self):
        pkg = os.path.join(self.tmp, "incompat.fkrplugin")
        write_package(
            pkg,
            valid_manifest(plugin_id="com.example.new", api_version=2,
                           files=file_entries(self.contents)),
            self.contents,
        )
        install_from_file(pkg, self.base, limits=self.limits)
        reg = self._registry()
        self.assertEqual(
            reg.status("com.example.new"), PluginStatus.INCOMPATIBLE
        )

    def test_dependency_missing_status(self):
        pkg = os.path.join(self.tmp, "dep.fkrplugin")
        write_package(
            pkg,
            valid_manifest(plugin_id="com.example.dep",
                           dependencies=["com.example.missing"],
                           files=file_entries(self.contents)),
            self.contents,
        )
        install_from_file(pkg, self.base, limits=self.limits)
        reg = self._registry()
        self.assertEqual(
            reg.status("com.example.dep"), PluginStatus.DEPENDENCY_MISSING
        )

    # -- acquire_for_run / release_after_run --------------------------------

    def test_acquire_for_run_blocks_uninstall(self):
        reg = self._registry()
        reg.acquire_for_run(["com.example.report"])
        # reference_count includes the active-run ref
        self.assertEqual(reg.reference_count("com.example.report"), 1)
        # uninstall is blocked
        ok, count = reg.uninstall("com.example.report")
        self.assertFalse(ok)
        self.assertEqual(count, 1)
        self.assertTrue(os.path.isdir(os.path.join(self.base, "com.example.report")))

    def test_release_after_run_restores(self):
        reg = self._registry()
        reg.acquire_for_run(["com.example.report"])
        reg.release_after_run(["com.example.report"])
        # reference_count back to 0
        self.assertEqual(reg.reference_count("com.example.report"), 0)
        # uninstall now succeeds
        ok, count = reg.uninstall("com.example.report")
        self.assertTrue(ok)
        self.assertEqual(count, 0)
        self.assertFalse(os.path.isdir(os.path.join(self.base, "com.example.report")))

    def test_release_idempotent(self):
        reg = self._registry()
        # releasing without a prior acquire must not raise or go negative
        reg.release_after_run(["com.example.report"])
        reg.release_after_run(["com.example.report"])
        self.assertEqual(reg.reference_count("com.example.report"), 0)
        # acquire twice, release three times: count stays at 0, no error
        reg.acquire_for_run(["com.example.report"])
        reg.acquire_for_run(["com.example.report"])
        reg.release_after_run(["com.example.report"])
        reg.release_after_run(["com.example.report"])
        reg.release_after_run(["com.example.report"])
        self.assertEqual(reg.reference_count("com.example.report"), 0)

    # -- RUN01: active_run_count 与 reference_count 的口径差异 ---------------

    def test_active_run_count_excludes_profile_references(self):
        """DESIGN: 覆盖安装只看活跃运行；方案引用不阻挡升级，但仍阻挡卸载。"""
        reg = self._registry()
        resolver = {"com.example.report": 3}.get
        self.assertEqual(reg.active_run_count("com.example.report"), 0)
        self.assertEqual(
            reg.reference_count("com.example.report", resolver=lambda pid: resolver(pid) or 0),
            3,
        )
        # 卸载仍被方案引用阻挡
        ok, count = reg.uninstall(
            "com.example.report", resolver=lambda pid: resolver(pid) or 0
        )
        self.assertFalse(ok)
        self.assertEqual(count, 3)

        reg.acquire_for_run(["com.example.report"])
        self.assertEqual(reg.active_run_count("com.example.report"), 1)
        reg.release_after_run(["com.example.report"])
        self.assertEqual(reg.active_run_count("com.example.report"), 0)

    def test_registry_active_run_count_blocks_install_overwrite(self):
        """端到端：注册表活跃运行计数接入安装守卫后拒绝覆盖。"""
        reg = self._registry()
        reg.acquire_for_run(["com.example.report"])
        pkg = os.path.join(self.tmp, "upgrade.fkrplugin")
        write_package(
            pkg,
            valid_manifest(version="2.0.0", files=file_entries(self.contents)),
            self.contents,
        )
        with self.assertRaises(PluginPackageError):
            install_from_file(
                pkg,
                self.base,
                limits=self.limits,
                active_run_resolver=reg.active_run_count,
            )
        self.assertEqual(
            load_installed_manifest("com.example.report", self.base).version, "1.0.0"
        )

        reg.release_after_run(["com.example.report"])
        install_from_file(
            pkg,
            self.base,
            limits=self.limits,
            active_run_resolver=reg.active_run_count,
        )
        self.assertEqual(
            load_installed_manifest("com.example.report", self.base).version, "2.0.0"
        )


class ApiCompatibleRealVersionRegressionTests(unittest.TestCase):
    """D-01 回归：真实 APP_VERSION（带 v 前缀）应正确判断兼容性。"""

    def test_api_compatible_with_real_app_version(self):
        """用真实 APP_VERSION 调用 api_compatible 时合法插件不被判为不兼容。"""
        from fukua_rpa.constants import APP_VERSION

        manifest = PluginManifest.from_dict(valid_manifest())

        # 真实 APP_VERSION 带 "v" 前缀（如 "v1.0.50"），修复后应兼容
        self.assertTrue(api_compatible(manifest, APP_VERSION))

        # 显式带 v 的高版本
        self.assertTrue(api_compatible(manifest, "v99.0.0"))

        # 低版本不兼容（min_app_version = "1.0.0"）
        self.assertFalse(api_compatible(manifest, "v0.9.0"))

    def test_api_compatible_strips_v_prefix_for_comparison(self):
        """v 前缀归一化后版本比较逻辑正确。"""
        manifest = PluginManifest.from_dict(
            valid_manifest(min_app_version="2.0.0")
        )

        # 恰好等于 min_app_version（带 v 前缀）
        self.assertTrue(api_compatible(manifest, "v2.0.0"))
        # 高于 min_app_version
        self.assertTrue(api_compatible(manifest, "v2.1.0"))
        # 低于 min_app_version
        self.assertFalse(api_compatible(manifest, "v1.9.9"))
        # 不带前缀同样正确
        self.assertTrue(api_compatible(manifest, "2.0.0"))
        self.assertFalse(api_compatible(manifest, "1.9.9"))


class EntrypointSafetyTests(unittest.TestCase):
    """S7: the manifest entrypoint must stay inside the plugin directory."""

    def _parse(self, entrypoint):
        return PluginManifest.from_dict(valid_manifest(entrypoint=entrypoint))

    def test_bare_relative_entrypoint_accepted(self):
        m = self._parse("main.py")
        self.assertEqual(m.entrypoint, "main.py")

    def test_path_function_entrypoint_accepted(self):
        m = self._parse("src/browser_automation/action.py:run")
        self.assertEqual(m.entrypoint, "src/browser_automation/action.py:run")

    def test_parent_traversal_entrypoint_rejected(self):
        with self.assertRaises(PluginManifestError):
            self._parse("../escape.py")

    def test_nested_traversal_entrypoint_rejected(self):
        with self.assertRaises(PluginManifestError):
            self._parse("src/../../evil.py:run")

    def test_backslash_entrypoint_rejected(self):
        with self.assertRaises(PluginManifestError):
            self._parse("src\\evil.py:run")

    def test_absolute_entrypoint_rejected(self):
        with self.assertRaises(PluginManifestError):
            self._parse("C:\\windows\\system32\\evil.py")


class PluginTamperDetectionTests(unittest.TestCase):
    """S8: post-install file tampering must be detected at registry scan."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "plugins")
        os.makedirs(self.base, exist_ok=True)
        self.contents = {"data.bin": b"x"}
        self.limits = PackageLimits(
            max_single_file_bytes=1_000_000,
            max_file_count=50,
            max_total_unpacked_bytes=10_000_000,
            max_compression_ratio=5.0,
            max_manifest_bytes=4096,
        )
        pkg = os.path.join(self.tmp, "good.fkrplugin")
        write_package(
            pkg,
            valid_manifest(files=file_entries(self.contents)),
            self.contents,
        )
        install_from_file(pkg, self.base, limits=self.limits)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tampered_file_marks_corrupt(self):
        installed = os.path.join(self.base, "com.example.report", "data.bin")
        with open(installed, "wb") as fh:
            fh.write(b"tampered-content")
        reg = PluginRegistry(self.base, "1.0.0")
        record = reg._records.get("com.example.report")
        self.assertIsNotNone(record)
        self.assertTrue(record.corrupt)
        self.assertIn("哈希", record.corrupt_reason)

    def test_untampered_file_stays_enabled(self):
        reg = PluginRegistry(self.base, "1.0.0")
        record = reg._records.get("com.example.report")
        self.assertIsNotNone(record)
        self.assertFalse(record.corrupt)
        self.assertEqual(reg.status("com.example.report"), PluginStatus.ENABLED)

    def test_read_error_marks_corrupt(self):
        # S8: if a declared file cannot be read (OSError) during the hash
        # recompute, the plugin is marked corrupt rather than loaded blindly.
        with mock.patch(
            "fukua_rpa.plugin_registry.hash_file", side_effect=OSError("locked")
        ):
            reg = PluginRegistry(self.base, "1.0.0")
        record = reg._records.get("com.example.report")
        self.assertIsNotNone(record)
        self.assertTrue(record.corrupt)
        self.assertIn("清单声明文件无法读取", record.corrupt_reason)


class TestCheckMemberName(unittest.TestCase):
    """Regression coverage for ``_check_member_name`` (manifest case bypass)."""

    def test_canonical_manifest_passes(self):
        # The canonical lowercase name must always be accepted.
        _check_member_name("manifest.json")

    def test_case_variant_rejected(self):
        # Windows filesystems are case-insensitive: "Manifest.json" would
        # overwrite the canonical manifest.json on extraction and bypass the
        # identity check. It must be rejected as an equivalent-path member.
        with self.assertRaises(PluginPackageError):
            _check_member_name("Manifest.json")
        with self.assertRaises(PluginPackageError):
            _check_member_name("MANIFEST.JSON")

    def test_dot_slash_case_variant_rejected(self):
        with self.assertRaises(PluginPackageError):
            _check_member_name("./Manifest.json")

    def test_normal_equivalents_still_rejected(self):
        for name in ("../manifest.json", "/manifest.json", "manifest.json/"):
            with self.subTest(name=name), self.assertRaises(PluginPackageError):
                _check_member_name(name)


class CorruptInstalledManifestOverwriteTests(unittest.TestCase):
    """ADV-SCOPE007-001：覆盖安装「清单已损坏」的插件。

    前置验证 `FR-SCOPE007-002` 成立：磁盘 ``manifest.json`` 损坏后，覆盖安装必然
    以技术性的 ``PluginManifestError``（「插件清单 JSON 格式无效（第 1 行）」）失败，
    用户既装不上、也看不出可以先卸载。修复后覆盖安装放行，但这些用例同时锁定
    放行路径不得削弱活跃运行守卫，也不得放宽旧清单可读时的降级规则。
    """

    PLUGIN_ID = "com.example.report"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.base = os.path.join(self.tmp, "plugins")
        os.makedirs(self.base, exist_ok=True)
        self.limits = PackageLimits(max_manifest_bytes=4096)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _package(self, name, version, payload):
        contents = {"data.bin": payload}
        pkg = os.path.join(self.tmp, name)
        write_package(
            pkg,
            valid_manifest(
                plugin_id=self.PLUGIN_ID,
                version=version,
                files=file_entries(contents),
            ),
            contents,
        )
        return pkg

    def _install_then_corrupt(self, pkg):
        """装好 ``pkg`` 后把磁盘清单破坏成非法 JSON，返回清单路径。"""
        install_from_file(pkg, self.base, limits=self.limits)
        manifest_path = os.path.join(self.base, self.PLUGIN_ID, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        return manifest_path

    def _installed_version(self):
        return load_installed_manifest(self.PLUGIN_ID, self.base).version

    def _installed_payload(self):
        with open(os.path.join(self.base, self.PLUGIN_ID, "data.bin"), "rb") as handle:
            return handle.read()

    def _residue(self):
        return sorted(
            name
            for name in os.listdir(self.base)
            if name.startswith("fkrplugin_") or name.endswith(".bkp")
        )

    def test_overwrite_succeeds_when_installed_manifest_is_corrupt(self):
        old = self._package("v1.fkrplugin", "1.0.0", b"old")
        new = self._package("v11.fkrplugin", "1.1.0", b"new")
        self._install_then_corrupt(old)

        with self.assertLogs("fukua_rpa.plugin_package", level="WARNING") as captured:
            manifest = install_from_file(new, self.base, limits=self.limits)

        self.assertEqual(manifest.version, "1.1.0")
        self.assertEqual(self._installed_version(), "1.1.0")
        self.assertEqual(self._installed_payload(), b"new")
        self.assertEqual(self._residue(), [], "覆盖安装后不得残留临时或备份目录")
        # 放行不是静默：日志必须点名插件，否则排障时无从判断发生过版本不可判定的覆盖。
        self.assertTrue(
            any(self.PLUGIN_ID in line for line in captured.output),
            f"警告日志未点名插件 {self.PLUGIN_ID}：{captured.output}",
        )

    def test_active_run_guard_still_blocks_overwrite_of_a_corrupt_install(self):
        """活跃运行守卫必须仍然生效，且拒绝理由来自守卫本身。

        修复前该场景也会被拒绝，但拒绝来自「清单无法解析」，与守卫无关；只断言
        「是否抛错」会把这种遮蔽误判为守卫仍在工作。
        """
        old = self._package("v1.fkrplugin", "1.0.0", b"old")
        new = self._package("v11.fkrplugin", "1.1.0", b"new")
        self._install_then_corrupt(old)

        with self.assertRaises(PluginPackageError) as ctx:
            install_from_file(
                new, self.base, limits=self.limits, active_run_resolver=lambda _pid: 1
            )
        self.assertIn("运行", str(ctx.exception))
        self.assertEqual(self._installed_payload(), b"old", "拒绝后旧文件必须保留")
        self.assertEqual(self._residue(), [])

    def test_downgrade_over_a_corrupt_install_is_allowed(self):
        """已接受的取舍：旧清单不可读时版本关系不可判定，降级规则随之跳过。

        损坏安装本身已不可用（注册表 CORRUPT、宿主拒绝运行），重装是唯一修复路径，
        因此宁可放行也不把用户挡在死角里。此用例把该取舍钉住，避免被误当成回归。
        """
        old = self._package("v1.fkrplugin", "1.0.0", b"old")
        down = self._package("v09.fkrplugin", "0.9.0", b"down")
        self._install_then_corrupt(old)

        install_from_file(down, self.base, limits=self.limits)
        self.assertEqual(self._installed_version(), "0.9.0")

    def test_readable_install_manifest_still_refuses_downgrade(self):
        """旧清单可读时降级仍须拒绝，证明新增的 except 分支没有放宽正常路径。"""
        old = self._package("v1.fkrplugin", "1.0.0", b"old")
        down = self._package("v09.fkrplugin", "0.9.0", b"down")
        install_from_file(old, self.base, limits=self.limits)

        with self.assertRaises(PluginPackageError) as ctx:
            install_from_file(down, self.base, limits=self.limits)
        self.assertIn("禁止降级", str(ctx.exception))
        self.assertEqual(self._installed_version(), "1.0.0")


class ShippedPluginPackageInstallTests(unittest.TestCase):
    """随仓库发布的 .fkrplugin 必须能被真实安装流程接受。

    ``plugins/tools/common.py`` 的 build_zip 改为确定性打包后，条目不再由
    ``ZipFile.write()`` 生成，而是手工构造 ZipInfo（固定 date_time 与
    create_system）再 writestr。这条通路必须由真实安装流程验证：安装侧会检查
    成员名、符号链接位、大小上限、压缩比、manifest 契约、逐文件 SHA-256，以及
    拒绝清单外文件——手工构造条目时任何一处属性写错都会在这里暴露。
    """

    PLUGINS_ROOT = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins"
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self._tmp.name, "installed")
        os.makedirs(self.base, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _shipped_packages(self):
        out = []
        if not os.path.isdir(self.PLUGINS_ROOT):
            return out
        for name in sorted(os.listdir(self.PLUGINS_ROOT)):
            pkg = os.path.join(self.PLUGINS_ROOT, name, f"{name}.fkrplugin")
            if os.path.isfile(pkg):
                out.append((name, pkg))
        return out

    def test_every_shipped_package_installs(self):
        packages = self._shipped_packages()
        self.assertTrue(packages, f"未在 {self.PLUGINS_ROOT} 下发现任何可分发包")
        for name, pkg in packages:
            with self.subTest(plugin=name):
                manifest = install_from_file(pkg, self.base)
                self.assertEqual(manifest.id, name)
                # 安装后应能按契约重新读回，证明落盘内容自洽。
                reloaded = load_installed_manifest(name, self.base)
                self.assertEqual(reloaded.version, manifest.version)

    def test_shipped_packages_declare_current_version(self):
        """分发包内的版本号应为当前代次，防止旧包被重新提交回仓库。"""
        for name, pkg in self._shipped_packages():
            with self.subTest(plugin=name):
                with zipfile.ZipFile(pkg) as zf:
                    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                self.assertEqual(
                    manifest.get("version"),
                    "1.1.0",
                    f"{name} 分发包版本为 {manifest.get('version')}，与当前代次不符",
                )

    def test_shipped_packages_use_fixed_timestamps(self):
        """确定性打包的可观测特征：所有条目时间戳固定。

        若有人绕过 build_plugin.py 手工重打包，这里会失败——那样的包会带回
        「内容未变但字节变化」的问题，正是历史漂移的结构性成因。
        """
        for name, pkg in self._shipped_packages():
            with self.subTest(plugin=name), zipfile.ZipFile(pkg) as zf:
                for info in zf.infolist():
                    self.assertEqual(
                        info.date_time,
                        (1980, 1, 1, 0, 0, 0),
                        f"{name} 的 {info.filename} 时间戳未固定",
                    )


if __name__ == "__main__":
    unittest.main()


class PluginPackageMemberCountPreReadTests(unittest.TestCase):
    """FR-20261005-602: 成员数上限必须在 ZipFile 物化中央目录之前生效。"""

    def _zip_with_declared_count(self, path, declared, *, members=1):
        """写真实归档并把 EOCD 声明数改写为 *declared*。

        A2：``members`` 控制真实成员数。当它与 ``declared`` 不一致时，这份归档
        就是"声明失真"，新的有界预检必须在物化前拒绝它。
        """
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", "{}")
            for index in range(max(0, members - 1)):
                archive.writestr(f"extra/e{index}.bin", b"x")
        with open(path, "r+b") as handle:
            raw = bytearray(handle.read())
        index = raw.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(index, 0)
        raw[index + 10 : index + 12] = int(declared).to_bytes(2, "little")
        with open(path, "wb") as handle:
            handle.write(bytes(raw))

    def test_declared_count_over_limit_is_rejected_before_opening_the_zip(self):
        from fukua_rpa import plugin_package
        from fukua_rpa.plugin_package import PackageLimits, PluginPackageError

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "oversize.fkrplugin")
            self._zip_with_declared_count(path, 2001)
            with (
                mock.patch.object(plugin_package.zipfile, "ZipFile") as fake_zip,
                self.assertRaises(PluginPackageError),
            ):
                plugin_package._validate_and_extract(
                    path, os.path.join(temp_dir, "out"), PackageLimits()
                )
            fake_zip.assert_not_called()

    def test_at_limit_declared_count_is_not_rejected_early(self):
        """边界：恰好等于上限的**真实**归档不得被提前拒绝。

        A2：真实成员数与声明数一致（各 2000），因此预检可通过并到达真正的读取
        器。旧fixture 只改声明数、真实只有 1 个成员，属于声明失真，按 A2 必须拒绝，
        不能再用来证明"不过度拒绝"。
        """
        from fukua_rpa import plugin_package
        from fukua_rpa.plugin_package import PackageLimits

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "at-limit.fkrplugin")
            self._zip_with_declared_count(path, 2000, members=2000)
            with mock.patch.object(
                plugin_package.zipfile, "ZipFile"
            ) as fake_zip:
                fake_zip.side_effect = zipfile.BadZipFile("reached the real reader")
                with self.assertRaises(zipfile.BadZipFile):
                    plugin_package._validate_and_extract(
                        path, os.path.join(temp_dir, "out"), PackageLimits()
                    )
            fake_zip.assert_called_once()

    def test_falsified_declared_count_is_rejected_before_opening_the_zip(self):
        """A2：声明数被改小以夹带超大目录，必须在物化前拒绝。

        这正是旧实现（只读 EOCD 声明数）放行的形状：新预检必须靠遍历真实中央
        目录并与声明数核对来拦住它。
        """
        from fukua_rpa import plugin_package
        from fukua_rpa.plugin_package import PackageLimits, PluginPackageError

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "falsified.fkrplugin")
            self._zip_with_declared_count(path, 2, members=2500)
            with mock.patch.object(
                plugin_package.zipfile, "ZipFile"
            ) as fake_zip, self.assertRaises(PluginPackageError):
                plugin_package._validate_and_extract(
                    path, os.path.join(temp_dir, "out"), PackageLimits()
                )
            fake_zip.assert_not_called()
