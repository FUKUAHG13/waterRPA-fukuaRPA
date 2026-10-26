"""定向覆盖率测试：fukua_rpa/profile_package.py 的错误分支与边界路径。

本文件补充 test_profile_package.py 未覆盖的分支，重点覆盖：
* 方案集合 payload 的构建/解析校验分支；
* 单方案与集合方案导出的尺寸/数量/冲突/缺失分支；
* ``_write_asset_to_archive`` 的 OSError 与总量分支；
* ``_safe_extract_package`` 的目录项、重复路径、符号链接、尺寸分支；
* ``_verify_assets`` / ``_verify_profile_payload`` 的清单校验分支；
* ``import_full_package`` / ``import_collection_package`` 的版本、凭据、
  未声明/未引用资产、正文校验分支；
* ``inspect_package_format`` 与 ``discard_imported_package``。

所有测试均使用临时目录构造 ZIP/JSON 输入，offscreen 可运行。
"""

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from unittest import mock

from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.constants import (
    FULL_PACKAGE_FORMAT,
    MAX_TASKS_PER_PROFILE,
    PROFILE_COLLECTION_FORMAT,
    PROFILE_COLLECTION_PACKAGE_FORMAT,
    TASK_TYPE_SECRET_TEXT,
)
from fukua_rpa.profile_package import (
    MissingPackageAssetsError,
    _write_asset_to_archive,
    build_profile_collection_payload,
    discard_imported_package,
    export_collection_package,
    export_full_package,
    import_collection_package,
    import_full_package,
    inspect_package_format,
    parse_profile_collection_payload,
    safe_extract_full_package,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_image(root: str, name: str, payload: bytes = b"package-asset-bytes") -> str:
    path = os.path.join(root, name)
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


def _profile_with_image(image_path: str) -> dict:
    profile = default_profile_config()
    profile["tasks"] = [{"type": 1.0, "value": image_path}]
    return profile


def _build_full_zip(
    path: str,
    profile_obj: dict,
    *,
    version: int = 4,
    assets: dict | None = None,
    manifest_extra: dict | None = None,
    profile_bytes: bytes | None = None,
) -> bytes:
    """手工构造一个单方案全量包，正确计算正文与资产哈希。

    ``manifest_extra`` 用于在哈希计算完成后覆盖任意清单字段，便于注入错误。
    返回写入的 profile.json 字节，便于调用方再计算。
    """
    assets = assets or {}
    if profile_bytes is None:
        profile_bytes = json.dumps(profile_obj, ensure_ascii=False).encode("utf-8")
    asset_meta = {
        relative: {"sha256": _sha256(data), "size": len(data)}
        for relative, data in assets.items()
    }
    manifest = {
        "format": FULL_PACKAGE_FORMAT,
        "version": version,
        "profile_name": "测试方案",
        "privacy_protected": False,
        "credentials_included": False,
        "redacted_step_count": 0,
        "asset_count": len(assets),
        "missing_images": [],
        "assets": asset_meta,
        "profile": {"sha256": _sha256(profile_bytes), "size": len(profile_bytes)},
    }
    if manifest_extra:
        manifest.update(manifest_extra)
    manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("profile.json", profile_bytes)
        for relative, data in assets.items():
            archive.writestr(relative, data)
        archive.writestr("manifest.json", manifest_bytes)
    return profile_bytes


def _build_collection_zip(
    path: str,
    entries: list,
    *,
    assets: dict | None = None,
    manifest_extra: dict | None = None,
    profiles_obj: dict | None = None,
) -> bytes:
    """手工构造一个多方案集合包，正确计算正文与资产哈希。"""
    assets = assets or {}
    if profiles_obj is None:
        profiles_obj = {
            "format": PROFILE_COLLECTION_FORMAT,
            "version": 1,
            "profiles": [{"name": name, "profile": profile} for name, profile in entries],
        }
    profiles_bytes = json.dumps(profiles_obj, ensure_ascii=False).encode("utf-8")
    asset_meta = {
        relative: {"sha256": _sha256(data), "size": len(data)}
        for relative, data in assets.items()
    }
    manifest = {
        "format": PROFILE_COLLECTION_PACKAGE_FORMAT,
        "version": 1,
        "profile_count": len(entries),
        "privacy_protected": False,
        "credentials_included": False,
        "redacted_step_count": 0,
        "asset_count": len(assets),
        "assets": asset_meta,
        "profiles": {"sha256": _sha256(profiles_bytes), "size": len(profiles_bytes)},
    }
    if manifest_extra:
        manifest.update(manifest_extra)
    manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("profiles.json", profiles_bytes)
        for relative, data in assets.items():
            archive.writestr(relative, data)
        archive.writestr("manifest.json", manifest_bytes)
    return profiles_bytes


def _rewrite_zip_members(source_path: str, dest_path: str, mutator) -> None:
    """读取源 ZIP 全部成员，交由 mutator 修改 data 字典后重写为新 ZIP。

    mutator 可以向 data 添加新成员（例如未声明资产）或删除已有成员；
    输出会保留原始顺序并追加新增成员，跳过被删除的成员。
    """
    with zipfile.ZipFile(source_path) as source:
        names = source.namelist()
        data = {name: source.read(name) for name in names}
    mutator(data)
    ordered = [name for name in names if name in data]
    seen = set(ordered)
    for name in data:
        if name not in seen:
            ordered.append(name)
    with zipfile.ZipFile(dest_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ordered:
            archive.writestr(name, data[name])


def _mutate_manifest(data: dict, mutator) -> None:
    manifest = json.loads(data["manifest.json"])
    mutator(manifest, data)
    data["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")


def _sync_profile_hash(manifest: dict, data: dict) -> None:
    profile_bytes = data["profile.json"]
    manifest["profile"] = {
        "sha256": _sha256(profile_bytes),
        "size": len(profile_bytes),
    }


def _sync_profiles_hash(manifest: dict, data: dict) -> None:
    profiles_bytes = data["profiles.json"]
    manifest["profiles"] = {
        "sha256": _sha256(profiles_bytes),
        "size": len(profiles_bytes),
    }


class CollectionPayloadTests(unittest.TestCase):
    """build_profile_collection_payload / parse_profile_collection_payload 分支。"""

    def test_build_payload_rejects_empty_selection(self):
        with self.assertRaisesRegex(ValueError, "至少需要选择一个方案"):
            build_profile_collection_payload({})

    def test_build_payload_rejects_too_many_profiles(self):
        profiles = {f"方案{i}": default_profile_config() for i in range(101)}
        with mock.patch(
            "fukua_rpa.profile_package.MAX_PROFILES", 100
        ), self.assertRaisesRegex(ValueError, "数量超过上限"):
            build_profile_collection_payload(profiles)

    def test_build_payload_rejects_empty_name(self):
        with self.assertRaisesRegex(ValueError, "包含空名称"):
            build_profile_collection_payload({"   ": default_profile_config()})

    def test_build_payload_rejects_duplicate_name_case_insensitive(self):
        profiles = {
            "方案A": default_profile_config(),
            "方案a": default_profile_config(),
        }
        with self.assertRaisesRegex(ValueError, "重复名称"):
            build_profile_collection_payload(profiles)

    def test_parse_payload_returns_none_for_non_collection(self):
        self.assertIsNone(parse_profile_collection_payload([]))
        self.assertIsNone(parse_profile_collection_payload({"format": "other"}))

    def test_parse_payload_rejects_invalid_version_type(self):
        payload = {
            "format": PROFILE_COLLECTION_FORMAT,
            "version": "not-an-int",
            "profiles": [{"name": "A", "profile": {}}],
        }
        with self.assertRaisesRegex(ValueError, "版本无效"):
            parse_profile_collection_payload(payload)

    def test_parse_payload_rejects_unsupported_version(self):
        payload = {
            "format": PROFILE_COLLECTION_FORMAT,
            "version": 2,
            "profiles": [{"name": "A", "profile": {}}],
        }
        with self.assertRaisesRegex(ValueError, "不支持的方案集合版本"):
            parse_profile_collection_payload(payload)

    def test_parse_payload_rejects_missing_entries(self):
        payload = {"format": PROFILE_COLLECTION_FORMAT, "version": 1, "profiles": []}
        with self.assertRaisesRegex(ValueError, "没有可导入的方案"):
            parse_profile_collection_payload(payload)

    def test_parse_payload_rejects_too_many_entries(self):
        entries = [{"name": f"方案{i}", "profile": {}} for i in range(101)]
        payload = {"format": PROFILE_COLLECTION_FORMAT, "version": 1, "profiles": entries}
        with mock.patch(
            "fukua_rpa.profile_package.MAX_PROFILES", 100
        ), self.assertRaisesRegex(ValueError, "数量超过上限"):
            parse_profile_collection_payload(payload)

    def test_parse_payload_rejects_non_mapping_entry(self):
        payload = {
            "format": PROFILE_COLLECTION_FORMAT,
            "version": 1,
            "profiles": ["just-a-string"],
        }
        with self.assertRaisesRegex(ValueError, "第 1 项格式错误"):
            parse_profile_collection_payload(payload)

    def test_parse_payload_rejects_missing_entry_name(self):
        payload = {
            "format": PROFILE_COLLECTION_FORMAT,
            "version": 1,
            "profiles": [{"name": "", "profile": {}}],
        }
        with self.assertRaisesRegex(ValueError, "第 1 项缺少名称"):
            parse_profile_collection_payload(payload)

    def test_parse_payload_rejects_duplicate_entry_name(self):
        payload = {
            "format": PROFILE_COLLECTION_FORMAT,
            "version": 1,
            "profiles": [
                {"name": "A", "profile": {}},
                {"name": "a", "profile": {}},
            ],
        }
        with self.assertRaisesRegex(ValueError, "重复名称"):
            parse_profile_collection_payload(payload)

    def test_parse_payload_rejects_non_mapping_profile_body(self):
        payload = {
            "format": PROFILE_COLLECTION_FORMAT,
            "version": 1,
            "profiles": [{"name": "A", "profile": "not-a-mapping"}],
        }
        with self.assertRaisesRegex(ValueError, "正文格式错误"):
            parse_profile_collection_payload(payload)


class WriteAssetToArchiveTests(unittest.TestCase):
    """_write_asset_to_archive 的 OSError 与总量分支。"""

    def test_missing_source_raises_missing_assets_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "out.zip")
            missing_source = os.path.join(temp_dir, "does-not-exist.png")
            with zipfile.ZipFile(
                zip_path, "w"
            ) as archive, self.assertRaises(MissingPackageAssetsError) as caught:
                _write_asset_to_archive(
                    archive, missing_source, "assets/x.png", 0
                )
            self.assertEqual(caught.exception.paths, (missing_source,))

    def test_single_file_size_limit_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = _make_image(temp_dir, "small.png", b"some-bytes")
            zip_path = os.path.join(temp_dir, "out.zip")
            with zipfile.ZipFile(zip_path, "w") as archive, mock.patch(
                "fukua_rpa.profile_package.MAX_PACKAGE_FILE_BYTES", 4
            ), self.assertRaisesRegex(ValueError, "资产过大"):
                _write_asset_to_archive(
                    archive, source, "assets/small.png", 0
                )

    def test_total_size_limit_raises_value_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = _make_image(temp_dir, "small.png", b"some-bytes")
            zip_path = os.path.join(temp_dir, "out.zip")
            with zipfile.ZipFile(zip_path, "w") as archive, mock.patch(
                "fukua_rpa.profile_package.MAX_PACKAGE_TOTAL_BYTES", 4
            ), self.assertRaisesRegex(ValueError, "总大小超过上限"):
                _write_asset_to_archive(
                    archive, source, "assets/small.png", 0
                )


class ExportFullPackageBranchTests(unittest.TestCase):
    """export_full_package 的缺失、冲突、尺寸与同文件去重分支。"""

    def test_same_file_two_spellings_exports_once(self):
        """FR-008：同一物理文件被两种拼写引用时不得假失败。

        长期方案可能同时存在托管引用（fukua-data://...）与旧版遗留绝对路径，
        两者在收集阶段是不同字符串，但解析后指向同一文件。export_full_package
        修复前会因第二次命中 casefold 冲突而抛出"资产名称冲突"，使导出失败且
        错误信息不指向真实原因；export_collection_package 已有
        `normalized in asset_map: continue` 守卫，两入口行为不一致。
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = os.path.join(temp_dir, "data")
            os.makedirs(os.path.join(base_dir, "img"), exist_ok=True)
            image = _make_image(os.path.join(base_dir, "img"), "a.png", b"shared")
            profile = default_profile_config()
            profile["tasks"] = [
                # 托管引用：abspath 无法解析，收集阶段去重键与下面的绝对路径不同
                {"type": 1.0, "value": "fukua-data://img/a.png"},
                # 旧版遗留绝对路径：解析后指向同一个文件
                {"type": 1.0, "value": image},
            ]
            destination = os.path.join(temp_dir, "out.zip")

            export_full_package(
                profile, "A", destination, privacy_protected=False, base_dir=base_dir
            )

            with zipfile.ZipFile(destination) as archive:
                asset_members = [
                    name
                    for name in archive.namelist()
                    if name.startswith("assets/") and not name.endswith("/")
                ]
            self.assertEqual(len(asset_members), 1)

    def test_missing_image_raises_missing_assets_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            profile["tasks"] = [
                {"type": 1.0, "value": os.path.join(temp_dir, "missing.png")}
            ]
            destination = os.path.join(temp_dir, "out.zip")
            with self.assertRaises(MissingPackageAssetsError):
                export_full_package(
                    profile, "A", destination, privacy_protected=False
                )

    def test_asset_name_conflict_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = _make_image(temp_dir, "a.png", b"one")
            second = _make_image(temp_dir, "b.png", b"two")
            profile = default_profile_config()
            profile["tasks"] = [
                {"type": 1.0, "value": first},
                {"type": 1.0, "value": second},
            ]
            destination = os.path.join(temp_dir, "out.zip")
            with mock.patch(
                "fukua_rpa.profile_package.asset_export_name",
                return_value="collision.png",
            ), self.assertRaisesRegex(ValueError, "资产名称冲突"):
                export_full_package(
                    profile, "A", destination, privacy_protected=False
                )

    def test_file_count_limit_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = _make_image(temp_dir, "a.png")
            profile = _profile_with_image(image)
            destination = os.path.join(temp_dir, "out.zip")
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PACKAGE_FILES", 2
            ), self.assertRaisesRegex(ValueError, "文件数量超过上限"):
                export_full_package(
                    profile, "A", destination, privacy_protected=False
                )

    def test_profile_payload_size_limit_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            destination = os.path.join(temp_dir, "out.zip")
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PROFILE_JSON_BYTES", 8
            ), self.assertRaisesRegex(ValueError, "方案正文大小超过上限"):
                export_full_package(
                    profile, "A", destination, privacy_protected=False
                )

    def test_manifest_size_limit_raises_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            destination = os.path.join(temp_dir, "out.zip")
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PACKAGE_MANIFEST_BYTES", 8
            ), self.assertRaisesRegex(ValueError, "清单大小超过上限"):
                export_full_package(
                    profile, "A", destination, privacy_protected=False
                )
            leftovers = [n for n in os.listdir(temp_dir) if n.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_total_size_limit_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            destination = os.path.join(temp_dir, "out.zip")
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PACKAGE_TOTAL_BYTES", 8
            ), self.assertRaisesRegex(ValueError, "总大小超过上限"):
                export_full_package(
                    profile, "A", destination, privacy_protected=False
                )

    def test_privacy_protected_export_redacts_steps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            profile["tasks"] = [
                {"type": 4.0, "value": "PRIVATE_TEXT"},
                {"type": TASK_TYPE_SECRET_TEXT, "value": "credential-name"},
            ]
            destination = os.path.join(temp_dir, "protected.zip")
            result = export_full_package(profile, "私人方案", destination)
            self.assertTrue(result.privacy_protected)
            self.assertEqual(result.redacted_step_count, 2)
            with zipfile.ZipFile(destination) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["profile_name"], "脱敏方案")
            self.assertTrue(manifest["privacy_protected"])


class ExportCollectionPackageBranchTests(unittest.TestCase):
    """export_collection_package 的脱敏、缺失、冲突、尺寸分支。"""

    def test_privacy_protected_collection_export(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            profile["tasks"] = [{"type": 4.0, "value": "PRIVATE_TEXT"}]
            destination = os.path.join(temp_dir, "collection.zip")
            result = export_collection_package(
                {"A": profile}, destination, privacy_protected=True
            )
            self.assertTrue(result.privacy_protected)
            self.assertEqual(result.redacted_step_count, 1)
            with zipfile.ZipFile(destination) as archive:
                payload = json.loads(archive.read("profiles.json"))
            self.assertEqual(payload["profiles"][0]["name"], "脱敏方案 1")

    def test_missing_image_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            profile["tasks"] = [
                {"type": 1.0, "value": os.path.join(temp_dir, "missing.png")}
            ]
            destination = os.path.join(temp_dir, "collection.zip")
            with self.assertRaises(MissingPackageAssetsError):
                export_collection_package(
                    {"A": profile}, destination, privacy_protected=False
                )

    def test_asset_name_conflict_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first = _make_image(temp_dir, "a.png", b"one")
            second = _make_image(temp_dir, "b.png", b"two")
            profile_a = _profile_with_image(first)
            profile_b = _profile_with_image(second)
            destination = os.path.join(temp_dir, "collection.zip")
            with mock.patch(
                "fukua_rpa.profile_package.asset_export_name",
                return_value="collision.png",
            ), self.assertRaisesRegex(ValueError, "资产名称冲突"):
                export_collection_package(
                    {"A": profile_a, "B": profile_b},
                    destination,
                    privacy_protected=False,
                )

    def test_file_count_limit_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = _make_image(temp_dir, "a.png")
            profile = _profile_with_image(image)
            destination = os.path.join(temp_dir, "collection.zip")
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PACKAGE_FILES", 2
            ), self.assertRaisesRegex(ValueError, "文件数量超过上限"):
                export_collection_package(
                    {"A": profile}, destination, privacy_protected=False
                )

    def test_profiles_payload_size_limit_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = os.path.join(temp_dir, "collection.zip")
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PROFILE_STORE_JSON_BYTES", 8
            ), self.assertRaisesRegex(ValueError, "正文大小超过上限"):
                export_collection_package(
                    {"A": default_profile_config()},
                    destination,
                    privacy_protected=False,
                )
            leftovers = [n for n in os.listdir(temp_dir) if n.endswith(".tmp")]
            self.assertEqual(leftovers, [])

    def test_manifest_size_limit_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = os.path.join(temp_dir, "collection.zip")
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PACKAGE_MANIFEST_BYTES", 8
            ), self.assertRaisesRegex(ValueError, "清单大小超过安全上限"):
                export_collection_package(
                    {"A": default_profile_config()},
                    destination,
                    privacy_protected=False,
                )

    def test_total_size_limit_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = os.path.join(temp_dir, "collection.zip")
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PACKAGE_TOTAL_BYTES", 8
            ), self.assertRaisesRegex(ValueError, "总大小超过安全上限"):
                export_collection_package(
                    {"A": default_profile_config()},
                    destination,
                    privacy_protected=False,
                )


class SafeExtractPackageBranchTests(unittest.TestCase):
    """_safe_extract_package 的目录项、重复、符号链接、尺寸分支。"""

    def test_too_many_members_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "many.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("profile.json", "{}")
                archive.writestr("manifest.json", "{}")
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PACKAGE_FILES", 1
            ), self.assertRaisesRegex(ValueError, "文件数量超过上限"):
                safe_extract_full_package(
                    zip_path, os.path.join(temp_dir, "out")
                )

    def test_path_traversal_member_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "traversal.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("profile.json", "{}")
                archive.writestr("../outside.txt", "bad")
            with self.assertRaisesRegex(ValueError, "不安全路径"):
                safe_extract_full_package(
                    zip_path, os.path.join(temp_dir, "out")
                )
            self.assertFalse(
                os.path.exists(os.path.join(temp_dir, "outside.txt"))
            )

    def test_extra_root_file_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "extra-root.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("profile.json", "{}")
                archive.writestr("notes.txt", "not part of the package")
            with self.assertRaisesRegex(ValueError, "额外文件"):
                safe_extract_full_package(
                    zip_path, os.path.join(temp_dir, "out")
                )

    def test_declared_total_size_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "big.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("profile.json", "x" * 32)
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PACKAGE_TOTAL_BYTES", 8
            ), self.assertRaisesRegex(ValueError, "预计超过"):
                safe_extract_full_package(
                    zip_path, os.path.join(temp_dir, "out")
                )

    def test_allowed_and_disallowed_directory_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # 允许的 assets/ 目录项可以正常解压。
            good_path = os.path.join(temp_dir, "good.zip")
            with zipfile.ZipFile(good_path, "w") as archive:
                info = zipfile.ZipInfo("assets/")
                info.external_attr = (0o040755 << 16) | 0x10
                archive.writestr(info, b"")
                archive.writestr("profile.json", "{}")
            safe_extract_full_package(good_path, os.path.join(temp_dir, "good-out"))

            # 非 assets 目录项被拒绝。
            bad_path = os.path.join(temp_dir, "bad.zip")
            with zipfile.ZipFile(bad_path, "w") as archive:
                info = zipfile.ZipInfo("evil/")
                info.external_attr = (0o040755 << 16) | 0x10
                archive.writestr(info, b"")
                archive.writestr("profile.json", "{}")
            with self.assertRaisesRegex(ValueError, "不允许的目录"):
                safe_extract_full_package(
                    bad_path, os.path.join(temp_dir, "bad-out")
                )

    def test_duplicate_member_path_case_insensitive_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "dup.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("assets/a.png", b"one")
                archive.writestr("assets/A.png", b"two")
                archive.writestr("profile.json", "{}")
            with self.assertRaisesRegex(ValueError, "重复路径"):
                safe_extract_full_package(
                    zip_path, os.path.join(temp_dir, "out")
                )

    def test_oversized_single_member_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "oversize.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("profile.json", "x" * 32)
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PACKAGE_FILE_BYTES", 8
            ), self.assertRaisesRegex(ValueError, "文件过大"):
                safe_extract_full_package(
                    zip_path, os.path.join(temp_dir, "out")
                )

    def test_symlink_member_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "symlink.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                info = zipfile.ZipInfo("assets/link.png")
                info.external_attr = 0o120777 << 16
                archive.writestr(info, b"")
                archive.writestr("profile.json", "{}")
            with self.assertRaisesRegex(ValueError, "符号链接"):
                safe_extract_full_package(
                    zip_path, os.path.join(temp_dir, "out")
                )


class VerifyAssetsBranchTests(unittest.TestCase):
    """_verify_assets 经由 import_full_package 触发的清单校验分支。"""

    def _export_valid(self, temp_dir: str) -> str:
        image = _make_image(temp_dir, "target.png")
        profile = _profile_with_image(image)
        package_path = os.path.join(temp_dir, "valid.zip")
        export_full_package(profile, "A", package_path, privacy_protected=False)
        return package_path

    def _import_tampered(self, temp_dir: str, mutator, regex: str, receiver: str):
        valid = self._export_valid(temp_dir)
        tampered = os.path.join(temp_dir, f"tampered-{receiver}.zip")
        _rewrite_zip_members(
            valid,
            tampered,
            lambda data: _mutate_manifest(data, mutator),
        )
        with self.assertRaisesRegex(ValueError, regex):
            import_full_package(tampered, os.path.join(temp_dir, receiver))

    def test_assets_metadata_not_mapping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._import_tampered(
                temp_dir,
                lambda manifest, data: manifest.update({"assets": []}),
                "资产清单格式错误",
                "r1",
            )

    def test_asset_record_not_mapping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def mutator(manifest, data):
                key = next(iter(manifest["assets"]))
                manifest["assets"][key] = "bad-record"

            self._import_tampered(
                temp_dir, mutator, "资产记录格式错误", "r2"
            )

    def test_asset_path_not_under_assets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def mutator(manifest, data):
                key = next(iter(manifest["assets"]))
                record = manifest["assets"].pop(key)
                manifest["assets"]["root-file.png"] = record

            self._import_tampered(
                temp_dir, mutator, "资产路径无效", "r3"
            )

    def test_duplicate_declared_asset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def mutator(manifest, data):
                key = next(iter(manifest["assets"]))
                record = manifest["assets"][key]
                prefix = "assets/"
                basename = key[len(prefix):]
                # 保持 assets/ 前缀不变，仅改写基名大小写：原始资产先通过全部
                # 文件校验，随后大小写变体命中“按 casefold 去重”的重复检测。
                manifest["assets"][prefix + basename.upper()] = dict(record)

            self._import_tampered(
                temp_dir, mutator, "清单包含重复路径", "r4"
            )

    def test_missing_declared_asset_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def mutator(manifest, data):
                manifest["assets"]["assets/ghost.png"] = {
                    "sha256": "0" * 64,
                    "size": 0,
                }
                manifest["asset_count"] = len(manifest["assets"])

            self._import_tampered(
                temp_dir, mutator, "缺少资产", "r5"
            )

    def test_asset_size_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def mutator(manifest, data):
                key = next(iter(manifest["assets"]))
                manifest["assets"][key]["size"] = 999999

            self._import_tampered(
                temp_dir, mutator, "资产大小校验失败", "r5b"
            )

    def test_asset_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def mutator(manifest, data):
                key = next(iter(manifest["assets"]))
                manifest["assets"][key]["sha256"] = "0" * 64

            self._import_tampered(
                temp_dir, mutator, "完整性校验失败", "r6"
            )

    def test_asset_count_invalid_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._import_tampered(
                temp_dir,
                lambda manifest, data: manifest.update({"asset_count": "bad"}),
                "资产数量无效",
                "r7",
            )

    def test_asset_count_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            def mutator(manifest, data):
                manifest["asset_count"] = len(manifest["assets"]) + 5

            self._import_tampered(
                temp_dir, mutator, "资产数量与清单不一致", "r8"
            )

    def test_undeclared_extra_asset_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            valid = self._export_valid(temp_dir)
            tampered = os.path.join(temp_dir, "extra.zip")
            _rewrite_zip_members(valid, tampered, lambda data: data.update(
                {"assets/undeclared.bin": b"extra"}
            ))
            with self.assertRaisesRegex(ValueError, "未声明资产"):
                import_full_package(tampered, os.path.join(temp_dir, "r9"))


class VerifyProfilePayloadBranchTests(unittest.TestCase):
    """_verify_profile_payload 经由 import_full_package 触发的分支。"""

    def test_profile_payload_too_large(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = _make_image(temp_dir, "target.png")
            profile = _profile_with_image(image)
            package_path = os.path.join(temp_dir, "valid.zip")
            export_full_package(
                profile, "A", package_path, privacy_protected=False
            )
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PROFILE_JSON_BYTES", 8
            ), self.assertRaisesRegex(ValueError, "方案正文超过上限"):
                import_full_package(
                    package_path, os.path.join(temp_dir, "receiver")
                )

    def test_profile_metadata_not_mapping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_full_zip(
                os.path.join(temp_dir, "p.zip"),
                default_profile_config(),
                assets={},
                manifest_extra={"profile": "bad"},
            )
            with self.assertRaisesRegex(ValueError, "缺少方案正文校验信息"):
                import_full_package(
                    os.path.join(temp_dir, "p.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_profile_size_invalid_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_full_zip(
                os.path.join(temp_dir, "p.zip"),
                default_profile_config(),
                assets={},
                manifest_extra={"profile": {"size": "bad", "sha256": "0" * 64}},
            )
            with self.assertRaisesRegex(ValueError, "方案正文大小无效"):
                import_full_package(
                    os.path.join(temp_dir, "p.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_profile_size_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_full_zip(
                os.path.join(temp_dir, "p.zip"),
                default_profile_config(),
                assets={},
                manifest_extra={
                    "profile": {"size": 999999, "sha256": "0" * 64}
                },
            )
            with self.assertRaisesRegex(ValueError, "方案正文大小校验失败"):
                import_full_package(
                    os.path.join(temp_dir, "p.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_profile_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            profile_bytes = json.dumps(profile, ensure_ascii=False).encode("utf-8")
            _build_full_zip(
                os.path.join(temp_dir, "p.zip"),
                profile,
                assets={},
                profile_bytes=profile_bytes,
                manifest_extra={
                    "profile": {"size": len(profile_bytes), "sha256": "0" * 64}
                },
            )
            with self.assertRaisesRegex(ValueError, "方案正文完整性校验失败"):
                import_full_package(
                    os.path.join(temp_dir, "p.zip"),
                    os.path.join(temp_dir, "receiver"),
                )


class ImportFullPackageBranchTests(unittest.TestCase):
    """import_full_package 的目录冲突、格式、版本、凭据、资产引用分支。"""

    def test_directory_collision_appends_counter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = _make_image(temp_dir, "target.png")
            profile = _profile_with_image(image)
            package_path = os.path.join(temp_dir, "pkg.zip")
            export_full_package(
                profile, "A", package_path, privacy_protected=False
            )
            receiver = os.path.join(temp_dir, "receiver")
            import_root = os.path.join(receiver, "imported_assets")
            os.makedirs(os.path.join(import_root, "pkg"))
            imported = import_full_package(package_path, receiver)
            self.assertTrue(imported.package_dir.endswith("pkg_1"))

    def test_stale_staging_directory_is_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = _make_image(temp_dir, "target.png")
            profile = _profile_with_image(image)
            package_path = os.path.join(temp_dir, "pkg.zip")
            export_full_package(
                profile, "A", package_path, privacy_protected=False
            )
            receiver = os.path.join(temp_dir, "receiver")
            import_root = os.path.join(receiver, "imported_assets")
            stale = os.path.join(import_root, "pkg.__extracting")
            os.makedirs(stale)
            with open(os.path.join(stale, "leftover.txt"), "w") as handle:
                handle.write("stale")
            imported = import_full_package(package_path, receiver)
            self.assertTrue(os.path.isdir(imported.package_dir))
            self.assertFalse(os.path.exists(stale))

    def test_missing_profile_json_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "no-profile.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps({"format": FULL_PACKAGE_FORMAT, "version": 4}),
                )
            with self.assertRaisesRegex(ValueError, "缺少 profile.json"):
                import_full_package(zip_path, os.path.join(temp_dir, "receiver"))

    def test_missing_manifest_json_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "no-manifest.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("profile.json", "{}")
            with self.assertRaisesRegex(ValueError, "缺少 manifest.json"):
                import_full_package(zip_path, os.path.join(temp_dir, "receiver"))

    def test_unknown_format_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_full_zip(
                os.path.join(temp_dir, "p.zip"),
                default_profile_config(),
                manifest_extra={"format": "bogus_format"},
            )
            with self.assertRaisesRegex(ValueError, "无法识别的全量包格式"):
                import_full_package(
                    os.path.join(temp_dir, "p.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_invalid_version_type_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_full_zip(
                os.path.join(temp_dir, "p.zip"),
                default_profile_config(),
                manifest_extra={"version": "bad"},
            )
            with self.assertRaisesRegex(ValueError, "版本无效"):
                import_full_package(
                    os.path.join(temp_dir, "p.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_non_positive_version_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_full_zip(
                os.path.join(temp_dir, "p.zip"),
                default_profile_config(),
                manifest_extra={"version": 0},
            )
            with self.assertRaisesRegex(ValueError, "缺少有效版本"):
                import_full_package(
                    os.path.join(temp_dir, "p.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_future_version_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_full_zip(
                os.path.join(temp_dir, "p.zip"),
                default_profile_config(),
                manifest_extra={"version": 999},
            )
            with self.assertRaisesRegex(ValueError, "高于本程序支持"):
                import_full_package(
                    os.path.join(temp_dir, "p.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_structural_validation_failure_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            profile["tasks"] = [
                {"type": 0.0, "value": ""}
                for _ in range(MAX_TASKS_PER_PROFILE + 1)
            ]
            zip_path = os.path.join(temp_dir, "oversize.zip")
            _build_full_zip(zip_path, profile, assets={})
            with self.assertRaisesRegex(ValueError, "超过上限"):
                import_full_package(zip_path, os.path.join(temp_dir, "receiver"))
            imported_root = os.path.join(temp_dir, "receiver", "imported_assets")
            leftovers = (
                os.listdir(imported_root) if os.path.isdir(imported_root) else []
            )
            self.assertEqual(leftovers, [])

    def test_credentials_included_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_full_zip(
                os.path.join(temp_dir, "p.zip"),
                default_profile_config(),
                manifest_extra={"credentials_included": True},
            )
            with self.assertRaisesRegex(ValueError, "包含凭据"):
                import_full_package(
                    os.path.join(temp_dir, "p.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_legacy_version_two_keeps_external_path(self):
        # version < 3 跳过资产校验；非 assets/ 引用经 import_mapper 返回 None 保留原文。
        with tempfile.TemporaryDirectory() as temp_dir:
            external_path = os.path.join(temp_dir, "external.png")
            profile = default_profile_config()
            profile["tasks"] = [{"type": 1.0, "value": external_path}]
            zip_path = os.path.join(temp_dir, "legacy.zip")
            _build_full_zip(zip_path, profile, version=2, assets={})
            imported = import_full_package(
                zip_path, os.path.join(temp_dir, "receiver")
            )
            self.assertEqual(imported.profile["tasks"][0]["value"], external_path)

    def test_undeclared_asset_reference_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            profile["tasks"] = [{"type": 1.0, "value": "assets/foo.png"}]
            zip_path = os.path.join(temp_dir, "undeclared-ref.zip")
            _build_full_zip(zip_path, profile, version=4, assets={})
            with self.assertRaisesRegex(ValueError, "未声明的图片"):
                import_full_package(
                    zip_path, os.path.join(temp_dir, "receiver")
                )

    def test_unused_declared_asset_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = _make_image(temp_dir, "target.png")
            profile = _profile_with_image(image)
            valid = os.path.join(temp_dir, "valid.zip")
            export_full_package(profile, "A", valid, privacy_protected=False)
            # 移除正文中的图片引用，使已声明资产未被引用。
            empty_profile = default_profile_config()
            empty_profile["tasks"] = []
            new_profile_bytes = json.dumps(
                empty_profile, ensure_ascii=False
            ).encode("utf-8")

            def mutator(data):
                data["profile.json"] = new_profile_bytes
                _mutate_manifest(data, lambda manifest, d: _sync_profile_hash(manifest, d))

            tampered = os.path.join(temp_dir, "unused.zip")
            _rewrite_zip_members(valid, tampered, mutator)
            with self.assertRaisesRegex(ValueError, "未被方案引用的资产"):
                import_full_package(tampered, os.path.join(temp_dir, "receiver"))


class InspectPackageFormatTests(unittest.TestCase):
    def test_missing_manifest_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "no-manifest.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("profile.json", "{}")
            with self.assertRaisesRegex(ValueError, "只能包含一个 manifest.json"):
                inspect_package_format(zip_path)

    def test_oversized_manifest_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "big-manifest.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps({"format": FULL_PACKAGE_FORMAT, "version": 4}),
                )
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PACKAGE_MANIFEST_BYTES", 4
            ), self.assertRaisesRegex(ValueError, "清单大小超过安全上限"):
                inspect_package_format(zip_path)

    def test_non_mapping_manifest_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "list-manifest.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("manifest.json", "[]")
            with self.assertRaisesRegex(ValueError, "清单格式错误"):
                inspect_package_format(zip_path)


class ImportCollectionPackageBranchTests(unittest.TestCase):
    """import_collection_package 的格式、版本、凭据、正文、资产引用分支。"""

    def _valid_collection(self, temp_dir: str) -> str:
        image = _make_image(temp_dir, "shared.png")
        first = _profile_with_image(image)
        second = default_profile_config()
        second["tasks"] = [{"type": 2.0, "value": image}]
        package_path = os.path.join(temp_dir, "collection.zip")
        export_collection_package(
            {"B": first, "A": second},
            package_path,
            privacy_protected=False,
            base_dir=temp_dir,
        )
        return package_path

    def test_round_trip_happy_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = self._valid_collection(temp_dir)
            self.assertEqual(
                inspect_package_format(package_path),
                PROFILE_COLLECTION_PACKAGE_FORMAT,
            )
            imported = import_collection_package(
                package_path, os.path.join(temp_dir, "receiver")
            )
            self.assertEqual(
                [name for name, _ in imported.profiles], ["B", "A"]
            )
            self.assertFalse(imported.privacy_protected)

    def test_directory_collision_appends_counter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = self._valid_collection(temp_dir)
            receiver = os.path.join(temp_dir, "receiver")
            import_root = os.path.join(receiver, "imported_assets")
            os.makedirs(os.path.join(import_root, "collection"))
            imported = import_collection_package(package_path, receiver)
            self.assertTrue(imported.package_dir.endswith("collection_1"))

    def test_stale_staging_directory_is_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = self._valid_collection(temp_dir)
            receiver = os.path.join(temp_dir, "receiver")
            import_root = os.path.join(receiver, "imported_assets")
            stale = os.path.join(import_root, "collection.__extracting")
            os.makedirs(stale)
            imported = import_collection_package(package_path, receiver)
            self.assertTrue(os.path.isdir(imported.package_dir))
            self.assertFalse(os.path.exists(stale))

    def test_missing_profiles_json_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "no-profiles.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(
                        {"format": PROFILE_COLLECTION_PACKAGE_FORMAT, "version": 1}
                    ),
                )
            with self.assertRaisesRegex(ValueError, "缺少 profiles.json"):
                import_collection_package(
                    zip_path, os.path.join(temp_dir, "receiver")
                )

    def test_missing_manifest_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "no-manifest.zip")
            with zipfile.ZipFile(zip_path, "w") as archive:
                archive.writestr("profiles.json", "{}")
            with self.assertRaisesRegex(ValueError, "缺少 manifest.json"):
                import_collection_package(
                    zip_path, os.path.join(temp_dir, "receiver")
                )

    def test_unknown_format_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_collection_zip(
                os.path.join(temp_dir, "c.zip"),
                [("A", default_profile_config())],
                manifest_extra={"format": "bogus"},
            )
            with self.assertRaisesRegex(ValueError, "无法识别的方案集合包格式"):
                import_collection_package(
                    os.path.join(temp_dir, "c.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_invalid_version_type_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_collection_zip(
                os.path.join(temp_dir, "c.zip"),
                [("A", default_profile_config())],
                manifest_extra={"version": "bad"},
            )
            with self.assertRaisesRegex(ValueError, "版本无效"):
                import_collection_package(
                    os.path.join(temp_dir, "c.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_unsupported_version_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_collection_zip(
                os.path.join(temp_dir, "c.zip"),
                [("A", default_profile_config())],
                manifest_extra={"version": 2},
            )
            with self.assertRaisesRegex(ValueError, "不支持的方案集合包版本"):
                import_collection_package(
                    os.path.join(temp_dir, "c.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_credentials_included_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_collection_zip(
                os.path.join(temp_dir, "c.zip"),
                [("A", default_profile_config())],
                manifest_extra={"credentials_included": True},
            )
            with self.assertRaisesRegex(ValueError, "包含凭据"):
                import_collection_package(
                    os.path.join(temp_dir, "c.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_payload_too_large_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = self._valid_collection(temp_dir)
            with mock.patch(
                "fukua_rpa.profile_package.MAX_PROFILE_STORE_JSON_BYTES", 8
            ), self.assertRaisesRegex(ValueError, "正文大小超过安全上限"):
                import_collection_package(
                    package_path, os.path.join(temp_dir, "receiver")
                )

    def test_payload_metadata_not_mapping_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_collection_zip(
                os.path.join(temp_dir, "c.zip"),
                [("A", default_profile_config())],
                manifest_extra={"profiles": "bad"},
            )
            with self.assertRaisesRegex(ValueError, "缺少正文校验信息"):
                import_collection_package(
                    os.path.join(temp_dir, "c.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_payload_size_invalid_type_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_collection_zip(
                os.path.join(temp_dir, "c.zip"),
                [("A", default_profile_config())],
                manifest_extra={"profiles": {"size": "bad", "sha256": "0" * 64}},
            )
            with self.assertRaisesRegex(ValueError, "正文大小无效"):
                import_collection_package(
                    os.path.join(temp_dir, "c.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_payload_size_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_collection_zip(
                os.path.join(temp_dir, "c.zip"),
                [("A", default_profile_config())],
                manifest_extra={
                    "profiles": {"size": 999999, "sha256": "0" * 64}
                },
            )
            with self.assertRaisesRegex(ValueError, "正文大小校验失败"):
                import_collection_package(
                    os.path.join(temp_dir, "c.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_payload_hash_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profiles_obj = {
                "format": PROFILE_COLLECTION_FORMAT,
                "version": 1,
                "profiles": [
                    {"name": "A", "profile": default_profile_config()}
                ],
            }
            profiles_bytes = json.dumps(
                profiles_obj, ensure_ascii=False
            ).encode("utf-8")
            _build_collection_zip(
                os.path.join(temp_dir, "c.zip"),
                [("A", default_profile_config())],
                profiles_obj=profiles_obj,
                manifest_extra={
                    "profiles": {
                        "size": len(profiles_bytes),
                        "sha256": "0" * 64,
                    }
                },
            )
            with self.assertRaisesRegex(ValueError, "正文完整性校验失败"):
                import_collection_package(
                    os.path.join(temp_dir, "c.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_payload_not_collection_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profiles_obj = {"format": "bogus", "version": 1, "profiles": []}
            _build_collection_zip(
                os.path.join(temp_dir, "c.zip"),
                [("A", default_profile_config())],
                profiles_obj=profiles_obj,
            )
            with self.assertRaisesRegex(ValueError, "正文格式错误"):
                import_collection_package(
                    os.path.join(temp_dir, "c.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_profile_count_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _build_collection_zip(
                os.path.join(temp_dir, "c.zip"),
                [("A", default_profile_config())],
                manifest_extra={"profile_count": 5},
            )
            with self.assertRaisesRegex(ValueError, "数量与清单不一致"):
                import_collection_package(
                    os.path.join(temp_dir, "c.zip"),
                    os.path.join(temp_dir, "receiver"),
                )

    def test_external_path_reference_is_kept(self):
        # 非 assets/ 引用经 import_mapper 返回 None，保留原文。
        with tempfile.TemporaryDirectory() as temp_dir:
            external_path = os.path.join(temp_dir, "external.png")
            profile = default_profile_config()
            profile["tasks"] = [{"type": 1.0, "value": external_path}]
            zip_path = os.path.join(temp_dir, "c.zip")
            _build_collection_zip(zip_path, [("A", profile)], assets={})
            imported = import_collection_package(
                zip_path, os.path.join(temp_dir, "receiver")
            )
            self.assertEqual(
                imported.profiles[0][1]["tasks"][0]["value"], external_path
            )

    def test_undeclared_asset_reference_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            profile["tasks"] = [{"type": 1.0, "value": "assets/foo.png"}]
            zip_path = os.path.join(temp_dir, "c.zip")
            _build_collection_zip(zip_path, [("A", profile)], assets={})
            with self.assertRaisesRegex(ValueError, "未声明的图片"):
                import_collection_package(
                    zip_path, os.path.join(temp_dir, "receiver")
                )

    def test_unused_declared_asset_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            asset_bytes = b"unused-asset"
            profile = default_profile_config()
            profile["tasks"] = []
            zip_path = os.path.join(temp_dir, "c.zip")
            _build_collection_zip(
                zip_path,
                [("A", profile)],
                assets={"assets/unused.png": asset_bytes},
            )
            with self.assertRaisesRegex(ValueError, "未被方案引用的资产"):
                import_collection_package(
                    zip_path, os.path.join(temp_dir, "receiver")
                )

    def test_structural_validation_failure_raises(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            profile["tasks"] = [
                {"type": 0.0, "value": ""}
                for _ in range(MAX_TASKS_PER_PROFILE + 1)
            ]
            zip_path = os.path.join(temp_dir, "oversize-collection.zip")
            _build_collection_zip(zip_path, [("超限方案", profile)], assets={})
            with self.assertRaisesRegex(ValueError, "超限方案.*超过上限"):
                import_collection_package(
                    zip_path, os.path.join(temp_dir, "receiver")
                )
            imported_root = os.path.join(temp_dir, "receiver", "imported_assets")
            leftovers = (
                os.listdir(imported_root) if os.path.isdir(imported_root) else []
            )
            self.assertEqual(leftovers, [])


class DiscardImportedPackageTests(unittest.TestCase):
    def test_discard_inside_import_root_succeeds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = _make_image(temp_dir, "target.png")
            profile = _profile_with_image(image)
            package_path = os.path.join(temp_dir, "pkg.zip")
            export_full_package(
                profile, "A", package_path, privacy_protected=False
            )
            imported = import_full_package(
                package_path, os.path.join(temp_dir, "receiver")
            )
            base_dir = os.path.join(temp_dir, "receiver")
            self.assertTrue(os.path.isdir(imported.package_dir))
            self.assertTrue(
                discard_imported_package(imported.package_dir, base_dir)
            )
            self.assertFalse(os.path.exists(imported.package_dir))

    def test_discard_outside_import_root_refused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = os.path.join(temp_dir, "receiver")
            outside = os.path.join(temp_dir, "outside")
            os.makedirs(outside)
            self.assertFalse(discard_imported_package(outside, base_dir))
            self.assertTrue(os.path.isdir(outside))

    def test_discard_import_root_itself_refused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = os.path.join(temp_dir, "receiver")
            import_root = os.path.join(base_dir, "imported_assets")
            os.makedirs(import_root)
            self.assertFalse(discard_imported_package(import_root, base_dir))
            self.assertTrue(os.path.isdir(import_root))

    def test_discard_nonexistent_dir_refused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = os.path.join(temp_dir, "receiver")
            import_root = os.path.join(base_dir, "imported_assets")
            os.makedirs(import_root)
            missing = os.path.join(import_root, "ghost")
            self.assertFalse(discard_imported_package(missing, base_dir))


class FullPackageRoundTripTests(unittest.TestCase):
    """补充 happy-path 覆盖，确保导出/导入主路径在本文件内独立可测。"""

    def test_full_package_round_trip_with_asset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image = _make_image(temp_dir, "target.png")
            profile = _profile_with_image(image)
            package_path = os.path.join(temp_dir, "pkg.zip")
            result = export_full_package(
                profile, "测试方案", package_path, privacy_protected=False
            )
            self.assertEqual(result.asset_count, 1)
            self.assertFalse(result.privacy_protected)
            imported = import_full_package(
                package_path, os.path.join(temp_dir, "receiver")
            )
            self.assertEqual(imported.suggested_name, "测试方案")
            self.assertFalse(imported.privacy_protected)


if __name__ == "__main__":
    unittest.main()
