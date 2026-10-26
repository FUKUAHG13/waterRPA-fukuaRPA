import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.constants import (
    FULL_PACKAGE_FORMAT,
    MAX_PACKAGE_FILES,
    MAX_TASKS_PER_PROFILE,
    PROFILE_COLLECTION_FORMAT,
    PROFILE_COLLECTION_PACKAGE_FORMAT,
    TASK_TYPE_SECRET_TEXT,
)
from fukua_rpa.log_policy import (
    LOG_MODE_CUSTOM,
    LOG_RECOGNITION,
    LOG_TIMESTAMP,
)
from fukua_rpa.profile_package import (
    MissingPackageAssetsError,
    build_profile_collection_payload,
    export_collection_package,
    export_full_package,
    import_collection_package,
    import_full_package,
    inspect_package_format,
    parse_profile_collection_payload,
    safe_extract_full_package,
)
from fukua_rpa.profile_paths import (
    MANAGED_DATA_PREFIX,
    managed_path_reference,
    resolve_profile_path,
)
from fukua_rpa.zip_member_count import CentralDirectoryUnreadable


class ProfilePackageTests(unittest.TestCase):
    def _profile_with_asset(self, root):
        image_path = os.path.join(root, "target.png")
        with open(image_path, "wb") as handle:
            handle.write(b"not-an-image-but-a-stable-package-asset")
        profile = default_profile_config()
        profile["tasks"] = [{"type": 1.0, "value": image_path}]
        return profile, image_path

    def test_version_four_package_round_trips_and_rewrites_asset_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, source_asset = self._profile_with_asset(temp_dir)
            package_path = os.path.join(temp_dir, "profile.zip")
            result = export_full_package(
                profile, "测试方案", package_path, privacy_protected=False
            )
            self.assertEqual(result.asset_count, 1)
            self.assertFalse(result.missing_images)
            with zipfile.ZipFile(package_path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["version"], 4)
                self.assertEqual(len(manifest["assets"]), 1)
                self.assertEqual(
                    manifest["profile"]["size"],
                    len(archive.read("profile.json")),
                )

            import_root = os.path.join(temp_dir, "receiver")
            imported = import_full_package(package_path, import_root)
            rewritten = imported.profile["tasks"][0]["value"]
            resolved = resolve_profile_path(rewritten, import_root)
            self.assertEqual(imported.suggested_name, "测试方案")
            self.assertTrue(rewritten.startswith(MANAGED_DATA_PREFIX))
            self.assertTrue(os.path.isfile(resolved))
            with open(source_asset, "rb") as source, open(resolved, "rb") as target:
                self.assertEqual(source.read(), target.read())

    def test_json_profile_collection_preserves_selected_order(self):
        profiles = {
            "第二个": default_profile_config(),
            "第一个": default_profile_config(),
        }
        payload = build_profile_collection_payload(profiles)
        entries = parse_profile_collection_payload(payload)
        self.assertEqual([name for name, _profile in entries], ["第二个", "第一个"])

    def test_collection_package_deduplicates_assets_and_preserves_order(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            first, source_asset = self._profile_with_asset(temp_dir)
            second = default_profile_config()
            second["tasks"] = [{"type": 2.0, "value": source_asset}]
            first["tasks"].append(
                {"type": TASK_TYPE_SECRET_TEXT, "value": "first-login"}
            )
            second["tasks"].append(
                {"type": TASK_TYPE_SECRET_TEXT, "value": "second-login"}
            )
            package_path = os.path.join(temp_dir, "profiles.zip")
            exported = export_collection_package(
                {"B": first, "A": second},
                package_path,
                privacy_protected=False,
                base_dir=temp_dir,
            )
            self.assertEqual(exported.profile_count, 2)
            self.assertEqual(exported.asset_count, 1)
            self.assertEqual(
                inspect_package_format(package_path),
                PROFILE_COLLECTION_PACKAGE_FORMAT,
            )

            receiver = os.path.join(temp_dir, "receiver")
            imported = import_collection_package(package_path, receiver)
            self.assertEqual(
                [name for name, _profile in imported.profiles], ["B", "A"]
            )
            rewritten = [
                profile["tasks"][0]["value"]
                for _name, profile in imported.profiles
            ]
            self.assertTrue(all(path.startswith(MANAGED_DATA_PREFIX) for path in rewritten))
            self.assertEqual(
                resolve_profile_path(rewritten[0], receiver),
                resolve_profile_path(rewritten[1], receiver),
            )
            self.assertEqual(
                imported.profiles[0][1]["tasks"][1]["value"], "first-login"
            )
            self.assertEqual(
                imported.profiles[1][1]["tasks"][1]["value"], "second-login"
            )

    def test_tampered_asset_is_rejected_and_staging_is_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, _source_asset = self._profile_with_asset(temp_dir)
            package_path = os.path.join(temp_dir, "profile.zip")
            export_full_package(
                profile, "A", package_path, privacy_protected=False
            )
            tampered_path = os.path.join(temp_dir, "tampered.zip")
            with zipfile.ZipFile(package_path) as source, zipfile.ZipFile(
                tampered_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as target:
                for info in source.infolist():
                    data = source.read(info.filename)
                    if info.filename.startswith("assets/"):
                        data += b"tampered"
                    target.writestr(info.filename, data)

            receiver = os.path.join(temp_dir, "receiver")
            with self.assertRaisesRegex(ValueError, "校验失败"):
                import_full_package(tampered_path, receiver)
            imported_root = os.path.join(receiver, "imported_assets")
            leftovers = os.listdir(imported_root) if os.path.isdir(imported_root) else []
            self.assertFalse(any(name.endswith(".__extracting") for name in leftovers))

    def test_future_package_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, _source_asset = self._profile_with_asset(temp_dir)
            package_path = os.path.join(temp_dir, "profile.zip")
            export_full_package(
                profile, "A", package_path, privacy_protected=False
            )
            future_path = os.path.join(temp_dir, "future.zip")
            with zipfile.ZipFile(package_path) as source, zipfile.ZipFile(
                future_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as target:
                for info in source.infolist():
                    data = source.read(info.filename)
                    if info.filename == "manifest.json":
                        manifest = json.loads(data)
                        manifest["version"] = 999
                        data = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
                    target.writestr(info.filename, data)

            with self.assertRaisesRegex(ValueError, "高于本程序支持"):
                import_full_package(
                    future_path, os.path.join(temp_dir, "receiver")
                )

    def test_undeclared_asset_and_unknown_payload_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, _source_asset = self._profile_with_asset(temp_dir)
            package_path = os.path.join(temp_dir, "profile.zip")
            export_full_package(
                profile, "A", package_path, privacy_protected=False
            )
            with zipfile.ZipFile(package_path, "a") as archive:
                archive.writestr("assets/undeclared.bin", b"extra")
            with self.assertRaisesRegex(ValueError, "未声明资产"):
                import_full_package(
                    package_path, os.path.join(temp_dir, "receiver-assets")
                )

            clean_path = os.path.join(temp_dir, "clean.zip")
            export_full_package(
                profile, "A", clean_path, privacy_protected=False
            )
            with zipfile.ZipFile(clean_path, "a") as archive:
                archive.writestr("notes.txt", "not part of the package")
            with self.assertRaisesRegex(ValueError, "额外文件"):
                import_full_package(
                    clean_path, os.path.join(temp_dir, "receiver-extra")
                )

    def test_tampered_profile_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, _source_asset = self._profile_with_asset(temp_dir)
            package_path = os.path.join(temp_dir, "profile.zip")
            export_full_package(
                profile, "A", package_path, privacy_protected=False
            )
            tampered_path = os.path.join(temp_dir, "tampered-profile.zip")
            with zipfile.ZipFile(package_path) as source, zipfile.ZipFile(
                tampered_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as target:
                for info in source.infolist():
                    data = source.read(info.filename)
                    if info.filename == "profile.json":
                        data += b" "
                    target.writestr(info.filename, data)

            with self.assertRaisesRegex(ValueError, "方案正文.*校验失败"):
                import_full_package(
                    tampered_path, os.path.join(temp_dir, "receiver")
                )

    def test_manifest_is_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "no-manifest.zip")
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr("profile.json", "{}")

            with self.assertRaisesRegex(ValueError, "缺少 manifest.json"):
                import_full_package(
                    package_path, os.path.join(temp_dir, "receiver")
                )

    def test_deep_profile_payload_is_rejected_before_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "deep-profile.zip")
            deeply_nested = "[" * 80 + "0" + "]" * 80
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(
                        {
                            "format": FULL_PACKAGE_FORMAT,
                            "version": 2,
                            "profile_name": "deep",
                        }
                    ),
                )
                archive.writestr(
                    "profile.json",
                    '{"tasks": [], "unknown": ' + deeply_nested + "}",
                )

            with self.assertRaisesRegex(ValueError, "嵌套"):
                import_full_package(
                    package_path, os.path.join(temp_dir, "receiver")
                )

    def test_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "unsafe.zip")
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr("profile.json", "{}")
                archive.writestr("../outside.txt", "bad")
            with self.assertRaisesRegex(ValueError, "不安全路径"):
                safe_extract_full_package(package_path, os.path.join(temp_dir, "extract"))
            self.assertFalse(os.path.exists(os.path.join(temp_dir, "outside.txt")))

    def test_windows_ads_and_device_names_are_rejected(self):
        unsafe_names = ["assets/image.png:stream", "assets/NUL.txt", "assets/image.png. "]
        with tempfile.TemporaryDirectory() as temp_dir:
            for index, unsafe_name in enumerate(unsafe_names):
                package_path = os.path.join(temp_dir, f"unsafe_{index}.zip")
                with zipfile.ZipFile(package_path, "w") as archive:
                    archive.writestr("profile.json", "{}")
                    archive.writestr(unsafe_name, "bad")
                with self.subTest(name=unsafe_name), self.assertRaisesRegex(
                    ValueError, "不安全路径"
                ):
                    safe_extract_full_package(
                        package_path, os.path.join(temp_dir, f"extract_{index}")
                    )

    def test_failed_export_does_not_replace_existing_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = os.path.join(temp_dir, "profile.zip")
            with open(destination, "wb") as handle:
                handle.write(b"previous")
            profile = default_profile_config()
            profile["tasks"] = [{"type": 1.0, "value": os.path.join(temp_dir, "missing.png")}]
            with self.assertRaises(MissingPackageAssetsError) as caught:
                export_full_package(
                    profile, "A", destination, privacy_protected=False
                )
            self.assertEqual(len(caught.exception.paths), 1)
            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), b"previous")
            temp_files = [name for name in os.listdir(temp_dir) if name.endswith(".tmp")]
            self.assertEqual(temp_files, [])

    def test_export_size_limit_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, _source_asset = self._profile_with_asset(temp_dir)
            destination = os.path.join(temp_dir, "profile.zip")
            with open(destination, "wb") as handle:
                handle.write(b"previous")

            with mock.patch(
                "fukua_rpa.profile_package.MAX_PACKAGE_FILE_BYTES", 8
            ), self.assertRaisesRegex(ValueError, "资产过大"):
                export_full_package(
                    profile,
                    "A",
                    destination,
                    privacy_protected=False,
                )

            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), b"previous")

    def test_export_rejects_non_finite_profile_values(self):
        # FR-009：导出侧此前允许写出 NaN 字面量，而导入侧 loads_bounded_json 会
        # 拒绝，于是生成"导出成功但本版本无法导入"的残缺包（ARCHITECTURE #48）。
        # FR-SCOPE005-003：本用例原先把 NaN 放在任务的 `value` 字段，而该字段会被
        # collect_profile_image_paths 当作图片路径收集 ⇒ 导出在**资产缺失**处
        # 先抛 MissingPackageAssetsError，它是 ValueError 的子类，因此
        # `assertRaises(ValueError)` 因错误的异常类型通过，形成假阳性（掩盖非有限值
        # 校验的真实回归）。现在用真实存在的资产满足资源前置条件，并把非有限值放到
        # 不会被当作图片路径的字段，使失败原因唯一指向非有限值校验。
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, _source_asset = self._profile_with_asset(temp_dir)
            profile["tasks"][0]["delay"] = float("nan")
            destination = os.path.join(temp_dir, "profile.zip")
            with open(destination, "wb") as handle:
                handle.write(b"previous")

            with self.assertRaises(ValueError) as caught:
                export_full_package(
                    profile, "A", destination, privacy_protected=False
                )

            self.assertNotIsInstance(caught.exception, MissingPackageAssetsError)
            self.assertIn("NaN 或无穷大", str(caught.exception))
            with open(destination, "rb") as handle:
                self.assertEqual(handle.read(), b"previous")
            self.assertEqual(
                [name for name in os.listdir(temp_dir) if name.endswith(".tmp")], []
            )

    def test_exported_package_is_importable_after_validation(self):
        # 正向对照：新增校验不得拒绝合法方案。
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, _source_asset = self._profile_with_asset(temp_dir)
            package_path = os.path.join(temp_dir, "profile.zip")
            export_full_package(profile, "A", package_path, privacy_protected=False)

            import_root = os.path.join(temp_dir, "receiver")
            os.makedirs(import_root, exist_ok=True)
            imported = import_full_package(package_path, import_root)
            self.assertEqual(len(imported.profile["tasks"]), 1)

    def test_manifest_hash_describes_the_archived_asset_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, _source_asset = self._profile_with_asset(temp_dir)
            package_path = os.path.join(temp_dir, "profile.zip")

            export_full_package(
                profile,
                "A",
                package_path,
                privacy_protected=False,
            )

            with zipfile.ZipFile(package_path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                relative, metadata = next(iter(manifest["assets"].items()))
                archived = archive.read(relative)
            import hashlib

            self.assertEqual(metadata["size"], len(archived))
            self.assertEqual(
                metadata["sha256"], hashlib.sha256(archived).hexdigest()
            )

    def test_custom_log_policy_survives_full_package_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            profile.update(
                {
                    "log_level": 2,
                    "log_mode": LOG_MODE_CUSTOM,
                    "log_custom_categories": [
                        LOG_RECOGNITION,
                        LOG_TIMESTAMP,
                    ],
                }
            )
            package_path = os.path.join(temp_dir, "custom-logging.zip")

            export_full_package(profile, "自定义日志", package_path)
            imported = import_full_package(
                package_path, os.path.join(temp_dir, "receiver")
            )

            self.assertEqual(imported.profile["log_mode"], LOG_MODE_CUSTOM)
            self.assertEqual(
                imported.profile["log_custom_categories"],
                [LOG_RECOGNITION, LOG_TIMESTAMP],
            )

    def test_extensionless_image_reference_is_rewritten(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "template_without_extension")
            with open(source, "wb") as handle:
                handle.write(b"package-asset")
            profile = default_profile_config()
            profile["tasks"] = [{"type": 1.0, "value": source}]
            package_path = os.path.join(temp_dir, "profile.zip")

            export_full_package(
                profile, "A", package_path, privacy_protected=False
            )
            imported = import_full_package(
                package_path, os.path.join(temp_dir, "receiver")
            )

            rewritten = imported.profile["tasks"][0]["value"]
            self.assertNotEqual(rewritten, source)
            self.assertTrue(
                os.path.isfile(
                    resolve_profile_path(
                        rewritten, os.path.join(temp_dir, "receiver")
                    )
                )
            )

    def test_imported_assets_follow_a_moved_portable_data_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, _source_asset = self._profile_with_asset(temp_dir)
            package_path = os.path.join(temp_dir, "profile.zip")
            export_full_package(
                profile, "A", package_path, privacy_protected=False
            )
            original_data_dir = os.path.join(temp_dir, "portable-a")
            imported = import_full_package(package_path, original_data_dir)
            reference = imported.profile["tasks"][0]["value"]
            self.assertTrue(
                os.path.isfile(
                    resolve_profile_path(reference, original_data_dir)
                )
            )

            moved_data_dir = os.path.join(temp_dir, "portable-b")
            shutil.move(original_data_dir, moved_data_dir)

            self.assertTrue(
                os.path.isfile(resolve_profile_path(reference, moved_data_dir))
            )

    def test_export_resolves_a_managed_template_reference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            template_dir = os.path.join(temp_dir, "templates")
            os.makedirs(template_dir)
            image_path = os.path.join(template_dir, "managed.png")
            with open(image_path, "wb") as handle:
                handle.write(b"managed-template")
            profile = default_profile_config()
            profile["tasks"] = [
                {
                    "type": 1.0,
                    "value": managed_path_reference(image_path, temp_dir),
                }
            ]
            package_path = os.path.join(temp_dir, "managed.zip")

            result = export_full_package(
                profile,
                "A",
                package_path,
                privacy_protected=False,
                base_dir=temp_dir,
            )

            self.assertEqual(result.asset_count, 1)

    def test_default_export_is_privacy_protected_and_contains_no_private_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "private-template.png")
            with open(image_path, "wb") as handle:
                handle.write(b"private-image-payload")
            profile = default_profile_config()
            profile["tasks"] = [
                {"type": 1.0, "value": image_path},
                {"type": 4.0, "value": "PRIVATE_SENTINEL_TEXT"},
                {"type": 12.0, "value": "PRIVATE_SENTINEL_PROMPT"},
                {"type": TASK_TYPE_SECRET_TEXT, "value": "PRIVATE_CREDENTIAL_NAME"},
            ]
            package_path = os.path.join(temp_dir, "protected.zip")

            result = export_full_package(profile, "私人方案名", package_path)

            self.assertTrue(result.privacy_protected)
            self.assertEqual(result.asset_count, 0)
            self.assertEqual(result.redacted_step_count, 4)
            with zipfile.ZipFile(package_path) as archive:
                self.assertEqual(
                    sorted(archive.namelist()), ["manifest.json", "profile.json"]
                )
                raw_payload = b"\n".join(
                    archive.read(name) for name in archive.namelist()
                )
                self.assertNotIn(b"PRIVATE_SENTINEL", raw_payload)
                self.assertNotIn(b"private-image-payload", raw_payload)
                manifest = json.loads(archive.read("manifest.json"))
                self.assertTrue(manifest["privacy_protected"])
                self.assertFalse(manifest["credentials_included"])

    def test_protected_export_drops_step_level_sound_alert_audio_path(self):
        """REVIEW-20260913-001 FR-001: 步骤级 sound_alert_audio_path 是本机绝对
        路径，脱敏分享包不得携带它（端到端：导出的 profile.json 内不得出现该
        路径或其文件名），且该步骤必须被停用。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = default_profile_config()
            profile["tasks"] = [
                {
                    "type": 14.0,
                    "value": "",
                    "step_id": "s1",
                    "sound_alert_audio_path": r"C:\Users\demo\Music\step-alert.wav",
                }
            ]
            package_path = os.path.join(temp_dir, "protected.zip")

            result = export_full_package(profile, "私人方案名", package_path)

            self.assertTrue(result.privacy_protected)
            self.assertEqual(result.redacted_step_count, 1)
            with zipfile.ZipFile(package_path) as archive:
                payload = archive.read("profile.json")
                self.assertNotIn(b"Users", payload)
                self.assertNotIn(b"step-alert.wav", payload)
                exported = json.loads(payload)
                task = exported["tasks"][0]
                self.assertEqual(task.get("sound_alert_audio_path"), "")
                self.assertTrue(task.get("step_disabled"))

    def test_unprotected_export_keeps_workflow_content_for_personal_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, _source_asset = self._profile_with_asset(temp_dir)
            profile["tasks"].append(
                {"type": 4.0, "value": "PERSONAL_MIGRATION_TEXT"}
            )
            profile["tasks"].append(
                {"type": TASK_TYPE_SECRET_TEXT, "value": "personal-login"}
            )
            package_path = os.path.join(temp_dir, "personal.zip")

            result = export_full_package(
                profile,
                "个人迁移方案",
                package_path,
                privacy_protected=False,
            )
            imported = import_full_package(
                package_path, os.path.join(temp_dir, "receiver")
            )

            self.assertFalse(result.privacy_protected)
            self.assertFalse(imported.privacy_protected)
            self.assertEqual(imported.suggested_name, "个人迁移方案")
            self.assertEqual(
                imported.profile["tasks"][1]["value"],
                "PERSONAL_MIGRATION_TEXT",
            )
            self.assertEqual(imported.profile["tasks"][2]["value"], "personal-login")


    def test_export_privacy_defaults_are_intentionally_opposite(self):
        # CFG-02: export_full_package 默认 privacy_protected=True，而
        # export_collection_package 默认 privacy_protected=False。两处调用方
        # （workflow_transfer_controller）均显式传参，因此相反默认值是「有意设计」
        # 而非缺陷；此测试锁定该设计，防止被「统一默认值」悄悄改动导致行为变化。
        import inspect

        full_default = inspect.signature(export_full_package).parameters[
            "privacy_protected"
        ].default
        coll_default = inspect.signature(export_collection_package).parameters[
            "privacy_protected"
        ].default
        self.assertEqual(full_default, True)
        self.assertEqual(coll_default, False)
        # 调用方显式传参时，默认值绝不应覆盖传入值（脚枪保护）。
        with tempfile.TemporaryDirectory() as temp_dir:
            profile, _asset = self._profile_with_asset(temp_dir)
            full = export_full_package(
                profile, "A", os.path.join(temp_dir, "full.zip"), privacy_protected=False
            )
            self.assertIs(False, full.privacy_protected)
            coll = export_collection_package(
                {"B": profile}, os.path.join(temp_dir, "coll.zip"),
                privacy_protected=True, base_dir=temp_dir,
            )
            self.assertIs(True, coll.privacy_protected)


class ImportStructuralValidationTests(unittest.TestCase):
    @staticmethod
    def _profile_with_tasks(count):
        profile = default_profile_config()
        profile["tasks"] = [
            {"type": 0.0, "value": ""} for _index in range(count)
        ]
        return profile

    def test_full_package_rejects_profile_above_task_limit_before_commit(self):
        # FR-009 起导出侧也会拒绝超限方案，因此这里改为直接构造包，继续守护导入侧
        # 的最后一道防线（手工构造或外来包仍必须被拒绝）。
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "oversize.zip")
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(
                        {
                            "format": FULL_PACKAGE_FORMAT,
                            "version": 2,
                            "profile_name": "超限方案",
                        }
                    ),
                )
                archive.writestr(
                    "profile.json",
                    json.dumps(
                        self._profile_with_tasks(MAX_TASKS_PER_PROFILE + 1)
                    ),
                )
            receiver = os.path.join(temp_dir, "receiver")

            with self.assertRaisesRegex(ValueError, "超过上限"):
                import_full_package(package_path, receiver)

            imported_root = os.path.join(receiver, "imported_assets")
            leftovers = (
                os.listdir(imported_root) if os.path.isdir(imported_root) else []
            )
            self.assertEqual(leftovers, [])

    def test_collection_rejects_any_profile_above_task_limit(self):
        # 同上：导入侧必须独立守护超限方案，不依赖导出侧的校验。
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "oversize-collection.zip")
            payload = {
                "format": PROFILE_COLLECTION_FORMAT,
                "version": 1,
                "profiles": [
                    {
                        "name": "正常方案",
                        "profile": self._profile_with_tasks(2),
                    },
                    {
                        "name": "超限方案",
                        "profile": self._profile_with_tasks(
                            MAX_TASKS_PER_PROFILE + 1
                        ),
                    },
                ],
            }
            payload_bytes = json.dumps(payload).encode("utf-8")
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps(
                        {
                            "format": PROFILE_COLLECTION_PACKAGE_FORMAT,
                            "version": 1,
                            "profile_count": 2,
                            "asset_count": 0,
                            "assets": {},
                            "profiles": {
                                "sha256": hashlib.sha256(
                                    payload_bytes
                                ).hexdigest(),
                                "size": len(payload_bytes),
                            },
                        }
                    ),
                )
                archive.writestr("profiles.json", payload_bytes)
            receiver = os.path.join(temp_dir, "receiver")

            with self.assertRaisesRegex(ValueError, "超限方案.*超过上限"):
                import_collection_package(package_path, receiver)

            imported_root = os.path.join(receiver, "imported_assets")
            leftovers = (
                os.listdir(imported_root) if os.path.isdir(imported_root) else []
            )
            self.assertEqual(leftovers, [])

    def test_package_structure_validation_remains_qt_free(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = "\n".join(
            (
                "import sys",
                "import fukua_rpa.profile_structure",
                "import fukua_rpa.profile_package",
                "assert 'PySide6' not in sys.modules",
                "from fukua_rpa.profile_structure import validate_profile_config_data as v",
                "assert v({'tasks': []}) is None",
            )
        )

        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_store_reexports_the_qt_free_validator(self):
        from fukua_rpa import config_store, profile_structure

        self.assertIs(
            config_store.validate_profile_config_data,
            profile_structure.validate_profile_config_data,
        )


class PackageMemberLimitPrecheckTests(unittest.TestCase):
    """FR-SCOPE005-002：成员数上限必须早于中央目录物化生效。

    权威缺陷（`05_权威报告.md` / `06_改进路线.md` 阶段 3）：`_safe_extract_package`
    先 `members = archive.infolist()` 全量物化，之后才检查成员数上限 ⇒ 上限不构成
    对物化本身的前置约束。而 `zipfile.ZipFile.__init__` 在打开时就已经把每个成员
    物化成 `ZipInfo`，因此"打开之后才检查"无法阻止超大目录的内存峰值。

    这些用例以「`zipfile.ZipFile` 是否被构造」为调用顺序断言：若上限生效于物化
    之前，`ZipFile` 根本不会被调用。
    """

    @staticmethod
    def _write_zip(path, entries=2, comment=b""):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {"format": FULL_PACKAGE_FORMAT, "version": 2, "profile_name": "P"}
                ),
            )
            archive.writestr("profile.json", json.dumps(default_profile_config()))
            for index in range(max(0, entries - 2)):
                archive.writestr(f"assets/a{index}.bin", b"x")
        if comment:
            with open(path, "r+b") as handle:
                handle.seek(0, os.SEEK_END)
                handle.write(comment)

    def _patch_declared_count(self, path, count):
        """把 EOCD 的「条目总数」字段改写为 *count*（其余字节保持真实）。"""
        with open(path, "rb") as handle:
            raw = bytearray(handle.read())
        index = raw.rfind(b"PK\x05\x06")
        self.assertGreaterEqual(index, 0, "测试构造的 ZIP 必须含 EOCD 记录")
        raw[index + 10 : index + 12] = int(count).to_bytes(2, "little")
        with open(path, "wb") as handle:
            handle.write(bytes(raw))

    @staticmethod
    def _write_zip64_with_declared_count(path, count):
        """构造只含 ZIP64 EOCD +定位器 + EOCD 的文件，声明 *count* 个成员。

        A2：这个合成文件**没有真实的中央目录**，记录遍历结果（0）与声明数不一致，
        因此现在会被 ``CentralDirectoryUnreadable`` 拒绝。这正是 A2 要拦的"声明
        失真"。因此"恰好等于上限不得过度拒绝"必须用**真实**归档证明，见
        ``test_zip64_at_limit_real_archive_is_not_rejected_early``。
        """
        zip64 = bytearray(56)
        zip64[0:4] = b"PK\x06\x06"
        zip64[4:12] = (44).to_bytes(8, "little")
        zip64[24:32] = int(count).to_bytes(8, "little")
        zip64[32:40] = int(count).to_bytes(8, "little")
        locator = bytearray(20)
        locator[0:4] = b"PK\x06\x07"
        locator[8:16] = (0).to_bytes(8, "little")
        eocd = bytearray(22)
        eocd[0:4] = b"PK\x05\x06"
        eocd[8:10] = (0xFFFF).to_bytes(2, "little")
        eocd[10:12] = (0xFFFF).to_bytes(2, "little")
        eocd[12:16] = (0xFFFFFFFF).to_bytes(4, "little")
        with open(path, "wb") as handle:
            handle.write(bytes(zip64) + bytes(locator) + bytes(eocd))

    # -- 判据②：超限 ZIP 在物化前被拒绝 ------------------------------------

    def test_oversized_declared_count_is_rejected_before_opening_the_zip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "oversize.zip")
            self._write_zip(package_path)
            self._patch_declared_count(package_path, MAX_PACKAGE_FILES + 1)

            with mock.patch(
                "fukua_rpa.profile_package.zipfile.ZipFile"
            ) as fake_zip:
                with self.assertRaisesRegex(
                    ValueError, f"压缩包文件数量超过上限 {MAX_PACKAGE_FILES}"
                ):
                    inspect_package_format(package_path)
                with self.assertRaisesRegex(
                    ValueError, f"压缩包文件数量超过上限 {MAX_PACKAGE_FILES}"
                ):
                    safe_extract_full_package(
                        package_path, os.path.join(temp_dir, "out")
                    )
            fake_zip.assert_not_called()

    def test_oversized_zip64_declared_count_is_rejected_before_opening_the_zip(self):
        """ZIP64 声明同样必须在物化前被拒绝（否则可绕过上限）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "oversize-zip64.zip")
            self._write_zip64_with_declared_count(
                package_path, MAX_PACKAGE_FILES * 100
            )

            with mock.patch(
                "fukua_rpa.profile_package.zipfile.ZipFile"
            ) as fake_zip, self.assertRaisesRegex(
                ValueError, f"压缩包文件数量超过上限 {MAX_PACKAGE_FILES}"
            ):
                inspect_package_format(package_path)
            fake_zip.assert_not_called()

    def test_zip64_declared_count_at_the_limit_is_not_rejected_early(self):
        """边界：恰好等于上限的 ZIP64 声明不得被提前拒绝（否则过度拒绝）。

        A2：改用**真实**归档。旧版fixture 只改EOCD 声明数、没有真实中央目录，
        声明与实扫数矛盾，按A2 属于必须拒绝的"声明失真"，无法再证明边界。
        这里用真实 ZIP64 尾部 + 真实中央目录，声明数等于实扫数。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "at-limit-zip64.zip")
            self._write_real_zip64_archive(package_path, MAX_PACKAGE_FILES)

            with mock.patch(
                "fukua_rpa.profile_package.zipfile.ZipFile"
            ) as fake_zip:
                fake_zip.side_effect = zipfile.BadZipFile("reached the real reader")
                with self.assertRaises(zipfile.BadZipFile):
                    inspect_package_format(package_path)
            fake_zip.assert_called_once()

    def test_zip64_falsified_count_is_rejected_before_opening_the_zip(self):
        """A2：ZIP64 声明数与真实中央目录不符时必须在物化前拒绝。

        这是本轮新增的守护：只补 ``size_cd`` 检查或只信声明数都拦不住它。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "falsified-zip64.zip")
            self._write_real_zip64_archive(package_path, 3, declared=2)

            with mock.patch(
                "fukua_rpa.profile_package.zipfile.ZipFile"
            ) as fake_zip:
                with self.assertRaises(CentralDirectoryUnreadable):
                    inspect_package_format(package_path)
                fake_zip.assert_not_called()

    @staticmethod
    def _write_real_zip64_archive(path, members, *, declared=None):
        """写真实 ZIP64 归档：真实中央目录 + ZIP64 EOCD + 定位器 + 哨兵 EOCD。

        *declared* 覆盖 ZIP64 EOCD 中的成员数（默认与真实值一致），用于构造
        声明失真。ZIP64 的 8字节目录大小/偏移从真实中央目录回填。
        """
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
            for index in range(members):
                archive.writestr(f"assets/a{index}.bin", b"x")
        with open(path, "rb") as handle:
            raw = bytearray(handle.read())
        eocd_at = raw.rfind(b"PK\x05\x06")
        cd_size = int.from_bytes(raw[eocd_at + 12 : eocd_at + 16], "little")
        cd_offset = int.from_bytes(raw[eocd_at + 16 : eocd_at + 20], "little")
        count = members if declared is None else declared

        # The ZIP64 EOCD record replaces the old EOCD, so it starts where the
        # EOCD used to be. The locator's absolute offset must match that, or the
        # reader seeks into the central directory and rejects the archive.
        zip64_at = eocd_at
        zip64 = bytearray(56)
        zip64[0:4] = b"PK\x06\x06"
        zip64[4:12] = (44).to_bytes(8, "little")
        zip64[24:32] = count.to_bytes(8, "little")
        zip64[32:40] = count.to_bytes(8, "little")
        zip64[40:48] = cd_size.to_bytes(8, "little")
        zip64[48:56] = cd_offset.to_bytes(8, "little")
        locator = bytearray(20)
        locator[0:4] = b"PK\x06\x07"
        locator[8:16] = zip64_at.to_bytes(8, "little")
        eocd = bytearray(raw[eocd_at:])
        eocd[8:10] = (0xFFFF).to_bytes(2, "little")
        eocd[10:12] = (0xFFFF).to_bytes(2, "little")
        eocd[12:16] = (0xFFFFFFFF).to_bytes(4, "little")
        eocd[16:20] = (0xFFFFFFFF).to_bytes(4, "little")
        with open(path, "wb") as handle:
            handle.write(bytes(raw[:eocd_at]) + bytes(zip64) + bytes(locator) + bytes(eocd))
        assert len(eocd) == 22

    # -- 边界：不得破坏正常包 ----------------------------------------------

    def test_normal_package_round_trips_through_the_precheck(self):
        """正常规模包必须继续导入成功（含带注释的 ZIP，EOCD 不在文件末尾）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "normal.zip")
            self._write_zip(package_path, comment=b"package comment")

            self.assertEqual(inspect_package_format(package_path), FULL_PACKAGE_FORMAT)
            receiver = os.path.join(temp_dir, "receiver")
            imported = import_full_package(package_path, receiver)
            self.assertIn("tasks", imported.profile)

    def test_exactly_at_the_member_limit_is_not_rejected_early(self):
        """边界：恰好等于上限的**真实**归档必须放行（不得过度拒绝）。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "at-limit.zip")
            self._write_zip(package_path, entries=MAX_PACKAGE_FILES)

            # 上限是"超过才拒绝"：恰好等于上限必须放行。
            self.assertEqual(
                inspect_package_format(package_path), FULL_PACKAGE_FORMAT
            )

    def test_falsified_declared_count_is_rejected_before_opening_the_zip(self):
        """A2：声明数被改小以夹带超大目录时，必须在物化前拒绝。

        旧实现只读EOCD 声明数，会被这份失真声明骗过。守护要求遍历真实中央目录
        并与声明数核对，两者不符即拒绝。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "falsified.zip")
            self._write_zip(package_path, entries=6000)
            self._patch_declared_count(package_path, 2)

            with mock.patch(
                "fukua_rpa.profile_package.zipfile.ZipFile"
            ) as fake_zip, self.assertRaisesRegex(
                ValueError, f"压缩包文件数量超过上限 {MAX_PACKAGE_FILES}"
            ):
                inspect_package_format(package_path)
            fake_zip.assert_not_called()

    def test_unreadable_directory_record_falls_back_to_the_post_open_check(self):
        """记录不可读时不得把包误判为合法：仍由打开后的检查兜底。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "garbage.zip")
            with open(package_path, "wb") as handle:
                handle.write(b"not a zip file at all")

            with self.assertRaises(zipfile.BadZipFile):
                inspect_package_format(package_path)

    # -- FR-20261008-201：合法归档不得被过度拒绝 ----------------------------

    def test_archive_with_a_prepended_stub_is_accepted(self):
        """FR-20261008-201：带前置数据的合法 ZIP（自解压风格）必须放行。

        EOCD 记录的 ``cd_offset`` 相对归档起点而非文件起点；``zipfile`` 用
        ``concat = eocd_location - cd_size - cd_offset`` 修正，本模块此前直接用
        ``cd_offset``，于是把这类合法归档判为 ``CentralDirectoryUnreadable``。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            original = os.path.join(temp_dir, "original.zip")
            self._write_zip(original, entries=3)
            with open(original, "rb") as handle:
                payload = handle.read()

            stubbed = os.path.join(temp_dir, "stubbed.zip")
            with open(stubbed, "wb") as handle:
                handle.write(b"MZ" + b"\x00" * 254 + payload)

            # 标准库能正常打开，本模块也必须放行（不得过度拒绝）。
            with zipfile.ZipFile(stubbed, "r") as archive:
                self.assertEqual(len(archive.infolist()), 3)
            self.assertEqual(inspect_package_format(stubbed), FULL_PACKAGE_FORMAT)

    def test_concatenated_archives_are_accepted(self):
        """FR-20261008-201：``cat a.zip b.zip`` 拼接的合法归档必须放行。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            prefix = os.path.join(temp_dir, "prefix.zip")
            with zipfile.ZipFile(prefix, "w") as archive:
                archive.writestr("unrelated.bin", b"x")

            package = os.path.join(temp_dir, "package.zip")
            self._write_zip(package, entries=3)

            concatenated = os.path.join(temp_dir, "concatenated.zip")
            with (
                open(prefix, "rb") as first,
                open(package, "rb") as second,
                open(concatenated, "wb") as out,
            ):
                out.write(first.read())
                out.write(second.read())

            self.assertEqual(
                inspect_package_format(concatenated), FULL_PACKAGE_FORMAT
            )

    def test_trailing_byte_cannot_bypass_the_member_limit_at_dispatch(self):
        """FR-20261008-202：EOCD 后有尾部字节时，上限仍须在 dispatch 阶段生效。

        此时预读返回「未校验」（无可用 EOCD 记录），设计把该情形托付给"打开后
        检查"；而 ``inspect_package_format`` 是导入流程最先打开包的地方，此前没有
        该兜底，超限包会先被完整物化。
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "oversize-trailing.zip")
            self._write_zip(package_path, entries=MAX_PACKAGE_FILES + 1)
            with open(package_path, "r+b") as handle:
                handle.seek(0, os.SEEK_END)
                handle.write(b"\x00")

            with self.assertRaisesRegex(
                ValueError, f"压缩包文件数量超过上限 {MAX_PACKAGE_FILES}"
            ):
                inspect_package_format(package_path)


if __name__ == "__main__":
    unittest.main()


class Zip64PreReadFailClosedTests(unittest.TestCase):
    """FR-20261005-501: 声明 ZIP64 但 ZIP64 记录不可读时必须失败关闭。"""

    def test_fabricated_zip64_sentinel_is_rejected_before_opening_the_zip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "fabricated.zip")
            with zipfile.ZipFile(package_path, "w") as archive:
                archive.writestr("manifest.json", "{}")
            with open(package_path, "r+b") as handle:
                raw = bytearray(handle.read())
            index = raw.rfind(b"PK\x05\x06")
            self.assertGreaterEqual(index, 0)
            # Only the 16-bit total-entries field becomes the ZIP64 sentinel; no
            # ZIP64 locator/EOCD is present, so the pre-read cannot resolve it.
            raw[index + 10 : index + 12] = (0xFFFF).to_bytes(2, "little")
            with open(package_path, "wb") as handle:
                handle.write(bytes(raw))

            with (
                mock.patch("fukua_rpa.profile_package.zipfile.ZipFile") as fake_zip,
                self.assertRaises(ValueError),
            ):
                inspect_package_format(package_path)
            fake_zip.assert_not_called()

    def test_no_eocd_still_falls_back_to_the_post_open_check(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            package_path = os.path.join(temp_dir, "garbage.zip")
            with open(package_path, "wb") as handle:
                handle.write(b"not a zip file at all")
            with self.assertRaises(zipfile.BadZipFile):
                inspect_package_format(package_path)
