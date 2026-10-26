"""Targeted coverage tests for fukua_rpa/config_store.py uncovered branches.

Covers: validate_profiles_payload error branches, preserve_corrupt_config,
_validated_generation errors, _record_from_payload validation, legacy backup
format loading, prune_profile_backups, _copy_backup_to_history,
archive_existing_backup throttle/limit, load_latest_profile_history,
load_profiles_state recovery paths (822-877), and persist_profiles error branches.
"""

import json
import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings

from fukua_rpa.config_schema import (
    UnsupportedProfileVersion,
    default_profile_config,
)
from fukua_rpa.config_store import (
    _commit_signature,
    _copy_backup_to_history,
    _history_dir_for,
    _load_profiles_backup_record,
    _load_settings_record,
    _record_from_payload,
    _safe_mtime,
    _try_load_history,
    _unsupported_profile_message,
    _validated_generation,
    archive_existing_backup,
    atomic_write_json,
    list_profile_backups,
    list_profile_history,
    load_latest_profile_history,
    load_profiles_backup,
    load_profiles_state,
    normalize_profile_backup_count,
    persist_profiles,
    preserve_corrupt_config,
    profiles_backup_payload,
    profiles_signature,
    prune_profile_backups,
    validate_profiles_payload,
)
from fukua_rpa.constants import (
    LEGACY_PROFILE_BACKUP_FORMAT,
    MAX_PROFILE_NAME_LENGTH,
    MAX_PROFILES,
    PROFILE_BACKUP_FORMAT,
    PROFILE_BACKUP_VERSION,
    PROFILE_COMMIT_FORMAT,
    PROFILE_COMMIT_VERSION,
)
from fukua_rpa.preview_model import coordinate_preview_options
from fukua_rpa.profile_structure import validate_profile_config_data
from fukua_rpa.task_model import (
    build_coord_step_positions,
    format_region_text,
    parse_coordinate_text,
    parse_region_text,
    serialize_coordinate_sequence,
)


def _make_valid_profiles(name="A"):
    """Create a minimal valid profiles dict for testing."""
    return {name: default_profile_config()}


def _write_backup_file(backup_path, profiles, current, generation=1):
    """Write a valid signed backup file."""
    payload = profiles_backup_payload(profiles, current, generation)
    atomic_write_json(backup_path, payload)


class TestValidateProfilesPayloadErrors(unittest.TestCase):
    """Cover error branches in validate_profiles_payload (lines 106-136)."""

    def test_empty_dict_returns_error(self):
        # Line 106: not profiles
        result = validate_profiles_payload({})
        self.assertIn("为空或格式错误", result)

    def test_non_dict_returns_error(self):
        # Line 106: not isinstance(profiles, dict)
        result = validate_profiles_payload([1, 2, 3])
        self.assertIn("为空或格式错误", result)

    def test_none_returns_error(self):
        result = validate_profiles_payload(None)
        self.assertIn("为空或格式错误", result)

    def test_too_many_profiles(self):
        # Line 108: len(profiles) > MAX_PROFILES
        profiles = {f"P{i}": default_profile_config() for i in range(MAX_PROFILES + 1)}
        result = validate_profiles_payload(profiles)
        self.assertIn("超过上限", result)

    def test_empty_name_profile(self):
        # Line 112: not name.strip()
        profiles = {"": default_profile_config()}
        result = validate_profiles_payload(profiles)
        self.assertIn("空名称", result)

    def test_non_string_name(self):
        # Line 112: not isinstance(name, str)
        profiles = {123: default_profile_config()}
        result = validate_profiles_payload(profiles)
        self.assertIn("空名称或非文本名称", result)

    def test_name_with_leading_space(self):
        # Line 114: name != name.strip()
        profiles = {" A": default_profile_config()}
        result = validate_profiles_payload(profiles)
        self.assertIn("空格", result)

    def test_name_with_trailing_space(self):
        profiles = {"A ": default_profile_config()}
        result = validate_profiles_payload(profiles)
        self.assertIn("空格", result)

    def test_name_too_long(self):
        # Line 116: len(name) > MAX_PROFILE_NAME_LENGTH
        long_name = "X" * (MAX_PROFILE_NAME_LENGTH + 1)
        profiles = {long_name: default_profile_config()}
        result = validate_profiles_payload(profiles)
        self.assertIn("过长", result)

    def test_per_profile_validation_error(self):
        # Line 119: error from validate_profile_config_data
        profiles = {"A": "not_a_dict"}
        result = validate_profiles_payload(profiles)
        self.assertIsNotNone(result)

    def test_serialization_error(self):
        # Lines 130-131: TypeError during json.dumps after validation passes.
        # Mock validate_profile_config_data to pass so json.dumps is reached.
        class BadObj:
            pass

        profiles = {"A": {"tasks": [], "bad": BadObj()}}
        with mock.patch(
            "fukua_rpa.config_store.validate_profile_config_data", return_value=None
        ):
            result = validate_profiles_payload(profiles)
        self.assertIn("无法保存的数据", result)

    def test_aggregate_size_exceeded(self):
        # Line 133: aggregate_size > MAX_PROFILE_STORE_JSON_BYTES
        # Use mock to make the size check trigger without huge data
        with mock.patch(
            "fukua_rpa.config_store.MAX_PROFILE_STORE_JSON_BYTES", 10
        ):
            profiles = {"A": default_profile_config()}
            result = validate_profiles_payload(profiles)
            self.assertIn("总大小超过上限", result)

    def test_valid_profiles_returns_none(self):
        profiles = _make_valid_profiles()
        self.assertIsNone(validate_profiles_payload(profiles))


class TestPreserveCorruptConfig(unittest.TestCase):
    """Cover preserve_corrupt_config (lines 149-154)."""

    def test_nonexistent_file_returns_empty(self):
        # Line 149-150: not os.path.isfile
        result = preserve_corrupt_config("/nonexistent/path/config.ini")
        self.assertEqual(result, "")

    def test_empty_path_returns_empty(self):
        result = preserve_corrupt_config("")
        self.assertEqual(result, "")

    def test_copies_existing_file(self):
        # Lines 151-154: actual copy
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.ini")
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("{corrupt data")
            result = preserve_corrupt_config(config_path)
            self.assertTrue(result.endswith(".bak"))
            self.assertTrue(os.path.isfile(result))
            with open(result, encoding="utf-8") as f:
                self.assertEqual(f.read(), "{corrupt data")


class TestValidatedGeneration(unittest.TestCase):
    """Cover _validated_generation error branches (lines 206-209)."""

    def test_valid_generation(self):
        self.assertEqual(_validated_generation(5), 5)
        self.assertEqual(_validated_generation(0), 0)
        self.assertEqual(_validated_generation(None), 0)

    def test_invalid_type_raises(self):
        # Lines 206-207: TypeError/ValueError
        with self.assertRaises(ValueError) as ctx:
            _validated_generation("not_a_number")
        self.assertIn("代次无效", str(ctx.exception))

    def test_negative_raises(self):
        # Line 209: generation < 0
        with self.assertRaises(ValueError) as ctx:
            _validated_generation(-1)
        self.assertIn("不能为负数", str(ctx.exception))


class TestRecordFromPayload(unittest.TestCase):
    """Cover _record_from_payload validation branches (lines 236-266)."""

    def _make_valid_payload(self, profiles=None, current="A", generation=1):
        if profiles is None:
            profiles = _make_valid_profiles(current)
        _text, sig = profiles_signature(profiles, current)
        commit_sig = _commit_signature(profiles, current, generation)
        return {
            "format": PROFILE_BACKUP_FORMAT,
            "version": PROFILE_BACKUP_VERSION,
            "generation": generation,
            "current_profile": current,
            "signature": sig,
            "commit_signature": commit_sig,
            "profiles": profiles,
        }

    def test_invalid_format_raises(self):
        # Line 236: format mismatch
        with self.assertRaises(ValueError) as ctx:
            _record_from_payload(
                {"format": "wrong"},
                expected_format=PROFILE_BACKUP_FORMAT,
                maximum_version=PROFILE_BACKUP_VERSION,
                label="Test",
            )
        self.assertIn("格式无效", str(ctx.exception))

    def test_non_dict_payload_raises(self):
        with self.assertRaises(ValueError):
            _record_from_payload(
                [1, 2],
                expected_format=PROFILE_BACKUP_FORMAT,
                maximum_version=PROFILE_BACKUP_VERSION,
                label="Test",
            )

    def test_version_too_high_raises_unsupported(self):
        # Line 239: version > maximum_version
        payload = self._make_valid_payload()
        payload["version"] = PROFILE_BACKUP_VERSION + 1
        with self.assertRaises(UnsupportedProfileVersion):
            _record_from_payload(
                payload,
                expected_format=PROFILE_BACKUP_FORMAT,
                maximum_version=PROFILE_BACKUP_VERSION,
                label="Test",
            )

    def test_negative_version_raises(self):
        # Line 243: version < 0
        payload = self._make_valid_payload()
        payload["version"] = -1
        with self.assertRaises(ValueError) as ctx:
            _record_from_payload(
                payload,
                expected_format=PROFILE_BACKUP_FORMAT,
                maximum_version=PROFILE_BACKUP_VERSION,
                label="Test",
            )
        self.assertIn("版本无效", str(ctx.exception))

    def test_invalid_profiles_raises(self):
        # Line 248: validate_profiles_payload returns error
        payload = self._make_valid_payload()
        payload["profiles"] = {}
        with self.assertRaises(ValueError):
            _record_from_payload(
                payload,
                expected_format=PROFILE_BACKUP_FORMAT,
                maximum_version=PROFILE_BACKUP_VERSION,
                label="Test",
            )

    def test_signature_mismatch_raises(self):
        # Line 254: signature mismatch
        payload = self._make_valid_payload()
        payload["signature"] = "wrong_signature"
        with self.assertRaises(ValueError) as ctx:
            _record_from_payload(
                payload,
                expected_format=PROFILE_BACKUP_FORMAT,
                maximum_version=PROFILE_BACKUP_VERSION,
                label="Test",
            )
        self.assertIn("完整性签名不匹配", str(ctx.exception))

    def test_missing_commit_signature_at_max_version(self):
        # Line 258: version >= maximum_version and not expected_commit
        payload = self._make_valid_payload()
        payload["commit_signature"] = ""
        with self.assertRaises(ValueError) as ctx:
            _record_from_payload(
                payload,
                expected_format=PROFILE_BACKUP_FORMAT,
                maximum_version=PROFILE_BACKUP_VERSION,
                label="Test",
            )
        self.assertIn("缺少提交签名", str(ctx.exception))

    def test_commit_signature_mismatch_raises(self):
        # Line 262: commit signature mismatch
        payload = self._make_valid_payload()
        payload["commit_signature"] = "bad_commit_sig"
        with self.assertRaises(ValueError) as ctx:
            _record_from_payload(
                payload,
                expected_format=PROFILE_BACKUP_FORMAT,
                maximum_version=PROFILE_BACKUP_VERSION,
                label="Test",
            )
        self.assertIn("提交签名不匹配", str(ctx.exception))

    def test_current_profile_not_in_profiles_falls_back(self):
        # Line 266: current_profile not in migrated_profiles
        profiles = _make_valid_profiles("A")
        _text, sig = profiles_signature(profiles, "Missing")
        generation = 1
        commit_sig = _commit_signature(profiles, "Missing", generation)
        payload = {
            "format": PROFILE_BACKUP_FORMAT,
            "version": PROFILE_BACKUP_VERSION,
            "generation": generation,
            "current_profile": "Missing",
            "signature": sig,
            "commit_signature": commit_sig,
            "profiles": profiles,
        }
        record = _record_from_payload(
            payload,
            expected_format=PROFILE_BACKUP_FORMAT,
            maximum_version=PROFILE_BACKUP_VERSION,
            label="Test",
        )
        self.assertEqual(record.current_profile, "A")


class TestLoadProfilesBackupRecord(unittest.TestCase):
    """Cover _load_profiles_backup_record branches (lines 308-323)."""

    def test_missing_file(self):
        record, error = _load_profiles_backup_record("/nonexistent/backup.json")
        self.assertIsNone(record)
        self.assertIn("没有找到自动备份", error)

    def test_legacy_plain_profiles_format(self):
        # Lines 308-319: very early backups stored profile mapping directly
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            profiles = _make_valid_profiles("Legacy")
            atomic_write_json(backup_path, profiles)
            record, error = _load_profiles_backup_record(backup_path)
            self.assertIsNotNone(record)
            self.assertEqual(error, "")
            self.assertIn("Legacy", record.profiles)
            self.assertEqual(record.current_profile, "Legacy")
            self.assertEqual(record.generation, 0)

    def test_invalid_json_content(self):
        # Lines 322-323: OSError/TypeError/ValueError
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write("{broken json!!")
            record, error = _load_profiles_backup_record(backup_path)
            self.assertIsNone(record)
            self.assertNotEqual(error, "")

    def test_unsupported_version_in_backup(self):
        # Lines 320-321: UnsupportedProfileVersion
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            profiles = _make_valid_profiles("A")
            _text, sig = profiles_signature(profiles, "A")
            payload = {
                "format": PROFILE_BACKUP_FORMAT,
                "version": PROFILE_BACKUP_VERSION + 10,
                "generation": 1,
                "current_profile": "A",
                "signature": sig,
                "commit_signature": "x",
                "profiles": profiles,
            }
            atomic_write_json(backup_path, payload)
            record, error = _load_profiles_backup_record(backup_path)
            self.assertIsNone(record)
            self.assertIn("方案版本不受支持", error)

    def test_legacy_waterRPA_format_accepted(self):
        # Line 296-298: LEGACY_PROFILE_BACKUP_FORMAT accepted
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            profiles = _make_valid_profiles("B")
            _text, sig = profiles_signature(profiles, "B")
            generation = 1
            commit_sig = _commit_signature(profiles, "B", generation)
            payload = {
                "format": LEGACY_PROFILE_BACKUP_FORMAT,
                "version": PROFILE_BACKUP_VERSION,
                "generation": generation,
                "current_profile": "B",
                "signature": sig,
                "commit_signature": commit_sig,
                "profiles": profiles,
            }
            atomic_write_json(backup_path, payload)
            record, error = _load_profiles_backup_record(backup_path)
            self.assertIsNotNone(record)
            self.assertEqual(error, "")
            self.assertIn("B", record.profiles)

    def test_payload_not_dict_and_not_valid_profiles(self):
        # Line 308-310: payload is not a recognized format and fails validation
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            atomic_write_json(backup_path, [1, 2, 3])
            record, error = _load_profiles_backup_record(backup_path)
            self.assertIsNone(record)
            self.assertNotEqual(error, "")


class TestLoadProfilesBackup(unittest.TestCase):
    """Cover load_profiles_backup (lines 335-338)."""

    def test_success_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            profiles = _make_valid_profiles("C")
            _write_backup_file(backup_path, profiles, "C")
            result_profiles, current, error = load_profiles_backup(backup_path)
            self.assertIsNotNone(result_profiles)
            self.assertEqual(current, "C")
            self.assertEqual(error, "")

    def test_failure_path(self):
        profiles, current, error = load_profiles_backup("/nonexistent/path.json")
        self.assertIsNone(profiles)
        self.assertIsNone(current)
        self.assertNotEqual(error, "")


class TestNormalizeProfileBackupCount(unittest.TestCase):
    """Cover normalize_profile_backup_count error branch (lines 474-475)."""

    def test_invalid_value_uses_default(self):
        # Lines 474-475: TypeError/ValueError/OverflowError
        self.assertEqual(normalize_profile_backup_count("abc"), 3)
        self.assertEqual(normalize_profile_backup_count(None), 3)
        # float("inf") raises OverflowError in int(), falls back to default
        self.assertEqual(normalize_profile_backup_count(float("inf")), 3)

    def test_clamping(self):
        self.assertEqual(normalize_profile_backup_count(-5), 0)
        self.assertEqual(normalize_profile_backup_count(999), 50)
        self.assertEqual(normalize_profile_backup_count(10), 10)


class TestOverflowInputGuard(unittest.TestCase):
    """FR-20260809-0501: 异常数值字符串（"inf"/"1e309"）不得使加载/预览路径
    抛出未捕获 OverflowError（ARCHITECTURE #61，异常输入在进入界面前拒绝）。"""

    def test_key_mapping_count_inf_rejected_in_validation(self):
        # 修复前：validate_profile_config_data 抛 OverflowError 逃逸。
        result = validate_profile_config_data(
            {"key_mapping_count": "inf", "key_mappings": []}, "测试"
        )
        self.assertIsNotNone(result)  # 返回可读错误串而非抛异常
        result = validate_profile_config_data(
            {"key_mapping_count": "1e309", "key_mappings": []}, "测试"
        )
        self.assertIsNotNone(result)

    def test_parse_region_text_inf_returns_none(self):
        # 修复前：parse_region_text("0,0,inf,inf") 抛 OverflowError。
        self.assertIsNone(parse_region_text("0,0,inf,inf"))
        self.assertIsNone(parse_region_text("0,0,1e309,10"))

    def test_coordinate_preview_options_inf_clamped(self):
        # 修复前：非有限步进参数会穿透到 UI 坐标总预览并触发 OverflowError。
        options = coordinate_preview_options(
            {"coord_step_max_steps": "inf", "coord_step_en": True}
        )
        self.assertEqual(options["max_steps"], 0)
        self.assertEqual(options["reset_after"], 0)

    def test_build_coord_step_positions_inf_does_not_raise(self):
        # 修复前：int(parse_float_text("inf")) 抛 OverflowError。
        positions = build_coord_step_positions(
            0, 0, {"max_steps": "inf", "max_distance": "inf", "direction": "向下"}
        )
        self.assertIsInstance(positions, list)
        self.assertGreaterEqual(len(positions), 1)

    def test_serialize_coordinate_sequence_skips_inf_point(self):
        # 修复前：int(float("inf")) 抛 OverflowError。
        self.assertEqual(
            serialize_coordinate_sequence([(1, 2), (float("inf"), 3)]), "1,2"
        )

    def test_normal_values_unaffected(self):
        self.assertEqual(
            validate_profile_config_data(
                {"key_mapping_count": "2", "key_mappings": []}, "测试"
            ),
            None,
        )
        self.assertEqual(parse_region_text("10,20,30,40"), (10, 20, 30, 40))
        self.assertEqual(
            coordinate_preview_options({"coord_step_max_steps": "5"})["max_steps"],
            5,
        )

    def test_adjacent_parse_paths_reject_non_finite_values(self):
        """FR-20260809-0505: 相邻解析路径（format_region_text /
        parse_coordinate_text / serialize_coordinate_sequence）对异常数值
        （inf/nan/1e309）不得抛异常——修复前 int(float(inf)) 抛 OverflowError
        逃逸；现应返回规范空值/None，由上层回退默认。"""
        # 1e309 溢出 float 上限 → inf；int(float(inf)) 修复前抛 OverflowError。
        self.assertEqual(format_region_text([1e309, 0, 0, 0]), "")
        self.assertEqual(
            format_region_text([float("inf"), float("nan"), 0, 0]), ""
        )
        self.assertIsNone(parse_coordinate_text("inf,5"))
        self.assertIsNone(parse_coordinate_text("1e309,5"))
        self.assertEqual(
            serialize_coordinate_sequence(
                [(float("inf"), 2), (3, float("nan"))]
            ),
            "",
        )


class TestSafeMtime(unittest.TestCase):
    """Cover _safe_mtime error branch (lines 484-485)."""

    def test_missing_file_returns_negative(self):
        result = _safe_mtime("/nonexistent/file.json")
        self.assertEqual(result, -1.0)

    def test_existing_file_returns_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "test.json")
            with open(path, "w") as f:
                f.write("{}")
            result = _safe_mtime(path)
            self.assertGreater(result, 0)


class TestPruneProfileBackups(unittest.TestCase):
    """Cover prune_profile_backups branches (lines 542-562)."""

    def test_backup_count_zero_removes_live_backup(self):
        # Lines 542-546: backup_count == 0 removes live backup
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            profiles = _make_valid_profiles("A")
            _write_backup_file(backup_path, profiles, "A")
            self.assertTrue(os.path.isfile(backup_path))
            removed = prune_profile_backups(backup_path, 0)
            self.assertFalse(os.path.isfile(backup_path))
            self.assertEqual(len(removed), 1)

    def test_backup_count_zero_no_file(self):
        # Line 545-546: FileNotFoundError path
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "nonexistent.json")
            removed = prune_profile_backups(backup_path, 0)
            self.assertEqual(removed, [])

    def test_prunes_excess_history(self):
        # Lines 551-555: remove old history entries
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            history_dir = os.path.join(tmp, "profiles_history")
            os.makedirs(history_dir)
            # Create 5 history files
            for i in range(5):
                path = os.path.join(history_dir, f"profiles_2024010{i}_120000_abc.json")
                with open(path, "w") as f:
                    f.write("{}")
                # Stagger mtimes
                os.utime(path, (time.time() - (5 - i) * 100, time.time() - (5 - i) * 100))
            # Keep only 2 (backup_count=3 means keep_history=2)
            removed = prune_profile_backups(backup_path, 3)
            remaining = os.listdir(history_dir)
            self.assertEqual(len(remaining), 2)
            self.assertEqual(len(removed), 3)

    def test_removes_empty_history_dir(self):
        # Lines 560-562: rmdir empty history dir
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            history_dir = os.path.join(tmp, "profiles_history")
            os.makedirs(history_dir)
            # No history files, backup_count=1 means keep_history=0
            prune_profile_backups(backup_path, 1)
            self.assertFalse(os.path.isdir(history_dir))


class TestCopyBackupToHistory(unittest.TestCase):
    """Cover _copy_backup_to_history (lines 579-594)."""

    def test_successful_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            history_dir = os.path.join(tmp, "profiles_history")
            os.makedirs(history_dir)
            profiles = _make_valid_profiles("A")
            _write_backup_file(backup_path, profiles, "A")
            result = _copy_backup_to_history(backup_path, history_dir, profiles, "A")
            self.assertNotEqual(result, "")
            self.assertTrue(os.path.isfile(result))

    def test_existing_target_returns_empty(self):
        # Line 583: os.path.exists(target)
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            history_dir = os.path.join(tmp, "profiles_history")
            os.makedirs(history_dir)
            profiles = _make_valid_profiles("A")
            _write_backup_file(backup_path, profiles, "A")
            # First copy succeeds
            first = _copy_backup_to_history(backup_path, history_dir, profiles, "A")
            self.assertNotEqual(first, "")
            # Second copy with same timestamp+signature target exists
            second = _copy_backup_to_history(backup_path, history_dir, profiles, "A")
            self.assertEqual(second, "")

    def test_copy_failure_returns_empty(self):
        # Lines 588-589: OSError during copy
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "nonexistent_source.json")
            history_dir = os.path.join(tmp, "profiles_history")
            os.makedirs(history_dir)
            profiles = _make_valid_profiles("A")
            result = _copy_backup_to_history(backup_path, history_dir, profiles, "A")
            self.assertEqual(result, "")


class TestArchiveExistingBackup(unittest.TestCase):
    """Cover archive_existing_backup branches (lines 617-645)."""

    def test_limit_zero_prunes_and_returns_empty(self):
        # Lines 617-618: limit == 0
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            profiles = _make_valid_profiles("A")
            _write_backup_file(backup_path, profiles, "A")
            result = archive_existing_backup(backup_path, limit=0)
            self.assertEqual(result, "")

    def test_no_backup_file_returns_empty(self):
        # Line 620: not os.path.isfile(backup_path)
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "nonexistent.json")
            result = archive_existing_backup(backup_path)
            self.assertEqual(result, "")

    def test_invalid_backup_returns_empty(self):
        # Lines 622-623: error or not profiles
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write("{broken")
            result = archive_existing_backup(backup_path)
            self.assertEqual(result, "")

    def test_normal_archive_creates_history(self):
        # Lines 624-645: normal archive path
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            profiles = _make_valid_profiles("A")
            _write_backup_file(backup_path, profiles, "A")
            result = archive_existing_backup(backup_path, min_interval=0)
            self.assertNotEqual(result, "")
            self.assertTrue(os.path.isfile(result))
            history = list_profile_history(backup_path)
            self.assertEqual(len(history), 1)

    def test_throttle_suppresses_regular_archive(self):
        # Lines 628-637: throttle hit (min_interval not elapsed)
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            profiles = _make_valid_profiles("A")
            _write_backup_file(backup_path, profiles, "A")
            # First archive
            archive_existing_backup(backup_path, min_interval=0)
            # Second archive within min_interval - should be throttled
            result = archive_existing_backup(backup_path, min_interval=9999)
            self.assertEqual(result, "")

    def test_archive_prunes_excess_history(self):
        # Lines 641-643: prune old entries beyond limit
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            history_dir = _history_dir_for(backup_path)
            os.makedirs(history_dir, exist_ok=True)
            # Pre-create history entries
            for i in range(5):
                path = os.path.join(history_dir, f"profiles_2024010{i}_120000_sig{i}.json")
                with open(path, "w") as f:
                    f.write("{}")
                os.utime(path, (time.time() - (5 - i) * 100, time.time() - (5 - i) * 100))
            profiles = _make_valid_profiles("A")
            _write_backup_file(backup_path, profiles, "A")
            archive_existing_backup(backup_path, limit=3, min_interval=0)
            history = list_profile_history(backup_path)
            self.assertLessEqual(len(history), 3)


class TestLoadLatestProfileHistory(unittest.TestCase):
    """Cover load_latest_profile_history (lines 663-666)."""

    def test_no_history_returns_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            profiles, current, path, error = load_latest_profile_history(backup_path)
            self.assertIsNone(profiles)
            self.assertIsNone(current)
            self.assertEqual(path, "")
            self.assertIn("没有找到历史备份", error)

    def test_loads_newest_valid_history(self):
        # Lines 663-665: successful load from history
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            history_dir = _history_dir_for(backup_path)
            os.makedirs(history_dir)
            profiles = _make_valid_profiles("H")
            payload = profiles_backup_payload(profiles, "H", 1)
            history_path = os.path.join(history_dir, "profiles_20240101_120000_abc.json")
            atomic_write_json(history_path, payload)
            result_profiles, current, path, error = load_latest_profile_history(backup_path)
            self.assertIsNotNone(result_profiles)
            self.assertEqual(current, "H")
            self.assertEqual(error, "")
            self.assertIn("profiles_20240101", path)

    def test_skips_corrupt_history_entries(self):
        # Line 666: error accumulation
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            history_dir = _history_dir_for(backup_path)
            os.makedirs(history_dir)
            # Corrupt entry (newer mtime)
            corrupt_path = os.path.join(history_dir, "profiles_20240102_120000_bad.json")
            with open(corrupt_path, "w") as f:
                f.write("{broken")
            os.utime(corrupt_path, (time.time(), time.time()))
            # Valid entry (older mtime)
            profiles = _make_valid_profiles("V")
            payload = profiles_backup_payload(profiles, "V", 1)
            valid_path = os.path.join(history_dir, "profiles_20240101_120000_good.json")
            atomic_write_json(valid_path, payload)
            os.utime(valid_path, (time.time() - 100, time.time() - 100))
            result_profiles, current, path, error = load_latest_profile_history(backup_path)
            self.assertIsNotNone(result_profiles)
            self.assertEqual(current, "V")
            self.assertEqual(error, "")

    def test_all_corrupt_returns_joined_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            history_dir = _history_dir_for(backup_path)
            os.makedirs(history_dir)
            for i in range(2):
                path = os.path.join(history_dir, f"profiles_2024010{i}_120000_x.json")
                with open(path, "w") as f:
                    f.write("{broken")
            profiles, current, path, error = load_latest_profile_history(backup_path)
            self.assertIsNone(profiles)
            self.assertIn(";", error.replace("\uff1b", ";"))


class TestLoadProfilesStateRecovery(unittest.TestCase):
    """Cover load_profiles_state recovery paths (lines 759-877)."""

    def _setup(self, tmp):
        config_path = os.path.join(tmp, "config.ini")
        backup_path = os.path.join(tmp, "profiles_backup.json")
        settings = QSettings(config_path, QSettings.IniFormat)
        return config_path, backup_path, settings

    def test_backups_disabled_skips_backup(self):
        # Line 759: backups_enabled = False
        with tempfile.TemporaryDirectory() as tmp:
            config_path, backup_path, settings = self._setup(tmp)
            profiles = _make_valid_profiles("A")
            persist_profiles(settings, backup_path, profiles, "A", backup_count=3)
            # Now load with backup_count=0
            loaded = load_profiles_state(settings, config_path, backup_path, backup_count=0)
            self.assertEqual(loaded.source, "settings")

    def test_main_missing_backups_disabled_returns_empty(self):
        # Lines 814-815: main_missing and not backups_enabled
        with tempfile.TemporaryDirectory() as tmp:
            config_path, backup_path, settings = self._setup(tmp)
            # No main config, no backup, backups disabled
            loaded = load_profiles_state(settings, config_path, backup_path, backup_count=0)
            self.assertEqual(loaded.source, "empty")
            self.assertEqual(loaded.profiles, {})

    def test_main_missing_recovers_from_history(self):
        # Lines 822-833: main_missing, backup None, history available
        with tempfile.TemporaryDirectory() as tmp:
            config_path, backup_path, settings = self._setup(tmp)
            profiles = _make_valid_profiles("H")
            # Create history entry
            history_dir = _history_dir_for(backup_path)
            os.makedirs(history_dir)
            payload = profiles_backup_payload(profiles, "H", 1)
            history_path = os.path.join(history_dir, "profiles_20240101_120000_abc.json")
            atomic_write_json(history_path, payload)
            loaded = load_profiles_state(settings, config_path, backup_path)
            self.assertEqual(loaded.source, "history")
            self.assertEqual(loaded.current_profile, "H")
            self.assertIn("历史备份恢复", loaded.recovery_message)

    def test_main_missing_no_history_returns_empty(self):
        # Lines 820-821: history_profiles is None
        with tempfile.TemporaryDirectory() as tmp:
            config_path, backup_path, settings = self._setup(tmp)
            loaded = load_profiles_state(settings, config_path, backup_path)
            self.assertEqual(loaded.source, "empty")

    def test_main_corrupt_recovers_from_backup(self):
        # Lines 841-852: main corrupt, backup valid
        with tempfile.TemporaryDirectory() as tmp:
            config_path, backup_path, settings = self._setup(tmp)
            profiles = _make_valid_profiles("B")
            _write_backup_file(backup_path, profiles, "B", generation=1)
            # Corrupt main settings
            settings.setValue("profiles_json", "{broken")
            settings.setValue("profiles_commit_json", "{broken")
            settings.sync()
            # Create a config file to preserve
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("[General]\nprofiles_json={broken\n")
            loaded = load_profiles_state(settings, config_path, backup_path)
            self.assertEqual(loaded.source, "backup")
            self.assertIn("自动备份恢复", loaded.recovery_message)

    def test_main_corrupt_backup_corrupt_recovers_from_history(self):
        # Lines 854-865: main corrupt, backup corrupt, history valid
        with tempfile.TemporaryDirectory() as tmp:
            config_path, backup_path, settings = self._setup(tmp)
            profiles = _make_valid_profiles("H2")
            # Create history
            history_dir = _history_dir_for(backup_path)
            os.makedirs(history_dir)
            payload = profiles_backup_payload(profiles, "H2", 1)
            history_path = os.path.join(history_dir, "profiles_20240101_120000_xyz.json")
            atomic_write_json(history_path, payload)
            # Corrupt main and backup
            settings.setValue("profiles_json", "{broken")
            settings.setValue("profiles_commit_json", "{broken")
            settings.sync()
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write("{broken")
            loaded = load_profiles_state(settings, config_path, backup_path)
            self.assertEqual(loaded.source, "history")
            self.assertEqual(loaded.current_profile, "H2")

    def test_all_sources_fail_hits_default_branch(self):
        # Lines 867-875: all sources fail -> default branch creates empty default
        # profiles and falls back to current_hint（v1.0.124 修复了 Stage 4 的
        # current 未赋值 UnboundLocalError，此前该路径会崩溃）.
        with tempfile.TemporaryDirectory() as tmp:
            config_path, backup_path, settings = self._setup(tmp)
            # Corrupt main
            settings.setValue("profiles_json", "{broken")
            settings.setValue("profiles_commit_json", "{broken")
            settings.sync()
            # Corrupt backup
            with open(backup_path, "w", encoding="utf-8") as f:
                f.write("{broken")
            loaded = load_profiles_state(settings, config_path, backup_path)
            self.assertEqual(loaded.source, "default")
            self.assertEqual(loaded.current_profile, "默认方案")
            self.assertEqual(loaded.profiles, {})

    def test_corrupt_config_preserved_in_message(self):
        # Lines 875-876: corrupt_copy appended to message
        with tempfile.TemporaryDirectory() as tmp:
            config_path, backup_path, settings = self._setup(tmp)
            profiles = _make_valid_profiles("P")
            _write_backup_file(backup_path, profiles, "P", generation=1)
            # Corrupt main settings
            settings.setValue("profiles_json", "{broken")
            settings.setValue("profiles_commit_json", "{broken")
            settings.sync()
            # Create the config file so preserve_corrupt_config can copy it
            with open(config_path, "w", encoding="utf-8") as f:
                f.write("[General]\nprofiles_json={broken\n")
            loaded = load_profiles_state(settings, config_path, backup_path)
            self.assertIn("原配置副本已保留在", loaded.recovery_message)

    def test_unsupported_backup_with_main_missing(self):
        # Lines 799-805: main failed + unsupported backup
        with tempfile.TemporaryDirectory() as tmp:
            config_path, backup_path, settings = self._setup(tmp)
            # Write an unsupported-version backup
            profiles = _make_valid_profiles("F")
            _text, sig = profiles_signature(profiles, "F")
            payload = {
                "format": PROFILE_BACKUP_FORMAT,
                "version": PROFILE_BACKUP_VERSION + 10,
                "generation": 1,
                "current_profile": "F",
                "signature": sig,
                "commit_signature": "x",
                "profiles": profiles,
            }
            atomic_write_json(backup_path, payload)
            # No main config at all
            loaded = load_profiles_state(settings, config_path, backup_path)
            self.assertEqual(loaded.source, "unsupported")
            self.assertTrue(loaded.persistence_blocked)

    def test_same_generation_conflict_uses_backup(self):
        # Lines 777-783: same_generation_conflict
        with tempfile.TemporaryDirectory() as tmp:
            config_path, backup_path, settings = self._setup(tmp)
            profiles_a = _make_valid_profiles("A")
            persist_profiles(settings, backup_path, profiles_a, "A")
            # Write a different backup with same generation but different content
            profiles_b = {"A": default_profile_config()}
            profiles_b["A"]["tasks"] = [{"type": 1.0, "value": "99,99"}]
            # Get current generation from settings
            gen = int(settings.value("profiles_generation", 1))
            _write_backup_file(backup_path, profiles_b, "A", generation=gen)
            loaded = load_profiles_state(settings, config_path, backup_path)
            self.assertEqual(loaded.source, "pending_backup")


class TestLoadSettingsRecord(unittest.TestCase):
    """Cover _load_settings_record error branches (lines 412-448)."""

    def test_commit_json_unsupported_version_raises(self):
        # Lines 412-413: UnsupportedProfileVersion re-raised
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.ini")
            settings = QSettings(config_path, QSettings.IniFormat)
            profiles = _make_valid_profiles("A")
            _text, sig = profiles_signature(profiles, "A")
            payload = {
                "format": PROFILE_COMMIT_FORMAT,
                "version": PROFILE_COMMIT_VERSION + 10,
                "generation": 1,
                "current_profile": "A",
                "signature": sig,
                "commit_signature": "x",
                "profiles": profiles,
            }
            settings.setValue("profiles_commit_json", json.dumps(payload, ensure_ascii=False))
            settings.sync()
            with self.assertRaises(UnsupportedProfileVersion):
                _load_settings_record(settings)

    def test_commit_json_invalid_returns_error(self):
        # Lines 414-415: TypeError/ValueError
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.ini")
            settings = QSettings(config_path, QSettings.IniFormat)
            settings.setValue("profiles_commit_json", "{invalid json!!")
            settings.sync()
            record, error = _load_settings_record(settings)
            self.assertIsNone(record)
            self.assertNotEqual(error, "")

    def test_legacy_profiles_json_signature_mismatch(self):
        # Line 433: stored_signature != actual_signature
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.ini")
            settings = QSettings(config_path, QSettings.IniFormat)
            profiles = _make_valid_profiles("A")
            settings.setValue("profiles_json", json.dumps(profiles, ensure_ascii=False))
            settings.setValue("current_profile", "A")
            settings.setValue("profiles_signature", "wrong_signature")
            settings.sync()
            record, error = _load_settings_record(settings)
            self.assertIsNone(record)
            self.assertIn("签名不匹配", error)

    def test_legacy_profiles_json_current_not_in_profiles(self):
        # Lines 436-437: current_profile not in profiles
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.ini")
            settings = QSettings(config_path, QSettings.IniFormat)
            profiles = _make_valid_profiles("A")
            _text, sig = profiles_signature(profiles, "Missing")
            settings.setValue("profiles_json", json.dumps(profiles, ensure_ascii=False))
            settings.setValue("current_profile", "Missing")
            settings.setValue("profiles_signature", sig)
            settings.sync()
            record, error = _load_settings_record(settings)
            self.assertIsNotNone(record)
            self.assertEqual(record.current_profile, "A")

    def test_legacy_profiles_json_invalid_returns_error(self):
        # Lines 447-448: TypeError/ValueError in legacy path
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.ini")
            settings = QSettings(config_path, QSettings.IniFormat)
            settings.setValue("profiles_json", "{broken")
            settings.sync()
            record, error = _load_settings_record(settings)
            self.assertIsNone(record)
            self.assertNotEqual(error, "")

    def test_no_profiles_at_all(self):
        # Line 418: not settings.contains("profiles_json")
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.ini")
            settings = QSettings(config_path, QSettings.IniFormat)
            record, error = _load_settings_record(settings)
            self.assertIsNone(record)
            self.assertIn("没有找到主配置", error)


class TestPersistProfilesErrors(unittest.TestCase):
    """Cover persist_profiles error branches (lines 923-986)."""

    def _setup(self, tmp):
        config_path = os.path.join(tmp, "config.ini")
        backup_path = os.path.join(tmp, "profiles_backup.json")
        settings = QSettings(config_path, QSettings.IniFormat)
        return config_path, backup_path, settings

    def test_persistence_blocked_returns_empty(self):
        # Line 923: persistence_blocked
        with tempfile.TemporaryDirectory() as tmp:
            _config, backup_path, settings = self._setup(tmp)
            profiles = _make_valid_profiles("A")
            result = persist_profiles(
                settings, backup_path, profiles, "A",
                persistence_blocked=True,
            )
            self.assertEqual(result, "")

    def test_generation_is_max_of_main_and_backup_plus_one(self):
        # CFG-03: persist_profiles 的代次公式（config_store.py:952-955）
        #   generation = max(main_record.generation, existing_record.generation) + 1
        # 验证两条分支（备份代次更高 / 主配置代次更高）以及「单调 +1」不变量。
        with tempfile.TemporaryDirectory() as tmp:
            _config, backup_path, settings = self._setup(tmp)
            profiles = _make_valid_profiles("A")
            # 首次：main=0, existing=0 -> 1
            persist_profiles(settings, backup_path, profiles, "A")
            self.assertEqual(int(settings.value("profiles_generation")), 1)
            # 覆盖备份为更高代次（existing 占优）：max(1,5)+1 = 6
            _write_backup_file(backup_path, profiles, "A", generation=5)
            persist_profiles(settings, backup_path, profiles, "A")
            self.assertEqual(int(settings.value("profiles_generation")), 6)
            # 覆盖备份为更低代次（main 占优）：max(6,2)+1 = 7
            _write_backup_file(backup_path, profiles, "A", generation=2)
            persist_profiles(settings, backup_path, profiles, "A")
            self.assertEqual(int(settings.value("profiles_generation")), 7)

    def test_invalid_profiles_raises(self):
        # Line 927: validation error
        with tempfile.TemporaryDirectory() as tmp:
            _config, backup_path, settings = self._setup(tmp)
            with self.assertRaises(ValueError):
                persist_profiles(settings, backup_path, {}, "A")

    def test_current_profile_not_in_profiles_raises(self):
        # Line 929: current_profile not in profiles
        with tempfile.TemporaryDirectory() as tmp:
            _config, backup_path, settings = self._setup(tmp)
            profiles = _make_valid_profiles("A")
            with self.assertRaises(ValueError) as ctx:
                persist_profiles(settings, backup_path, profiles, "Missing")
            self.assertIn("当前方案不存在", str(ctx.exception))

    def test_unsupported_main_config_raises(self):
        # Lines 933-934: UnsupportedProfileVersion from _load_settings_record
        with tempfile.TemporaryDirectory() as tmp:
            _config, backup_path, settings = self._setup(tmp)
            profiles = _make_valid_profiles("A")
            payload = {
                "format": PROFILE_COMMIT_FORMAT,
                "version": PROFILE_COMMIT_VERSION + 10,
                "generation": 1,
                "current_profile": "A",
                "signature": "x",
                "commit_signature": "x",
                "profiles": profiles,
            }
            settings.setValue("profiles_commit_json", json.dumps(payload, ensure_ascii=False))
            settings.sync()
            new_profiles = _make_valid_profiles("A")
            with self.assertRaises(UnsupportedProfileVersion):
                persist_profiles(settings, backup_path, new_profiles, "A")

    def test_unsupported_backup_raises(self):
        # Line 942: unsupported backup version
        with tempfile.TemporaryDirectory() as tmp:
            _config, backup_path, settings = self._setup(tmp)
            profiles = _make_valid_profiles("A")
            # Write unsupported backup
            _text, sig = profiles_signature(profiles, "A")
            payload = {
                "format": PROFILE_BACKUP_FORMAT,
                "version": PROFILE_BACKUP_VERSION + 10,
                "generation": 1,
                "current_profile": "A",
                "signature": sig,
                "commit_signature": "x",
                "profiles": profiles,
            }
            atomic_write_json(backup_path, payload)
            new_profiles = _make_valid_profiles("A")
            with self.assertRaises(UnsupportedProfileVersion):
                persist_profiles(settings, backup_path, new_profiles, "A")

    def test_archives_when_signature_differs(self):
        # Line 950: archive_existing_backup called on signature change
        with tempfile.TemporaryDirectory() as tmp:
            _config, backup_path, settings = self._setup(tmp)
            profiles_v1 = _make_valid_profiles("A")
            persist_profiles(settings, backup_path, profiles_v1, "A")
            # Change profiles to trigger different signature
            profiles_v2 = {"A": default_profile_config()}
            profiles_v2["A"]["tasks"] = [{"type": 1.0, "value": "5,5"}]
            persist_profiles(settings, backup_path, profiles_v2, "A")
            history = list_profile_history(backup_path)
            self.assertGreaterEqual(len(history), 1)

    def test_backup_count_zero_prunes_after_commit(self):
        # Lines 985-986: backup_count == 0 prunes
        with tempfile.TemporaryDirectory() as tmp:
            _config, backup_path, settings = self._setup(tmp)
            profiles = _make_valid_profiles("A")
            # First persist with backups enabled
            persist_profiles(settings, backup_path, profiles, "A", backup_count=3)
            self.assertTrue(os.path.isfile(backup_path))
            # Now persist with backup_count=0
            persist_profiles(settings, backup_path, profiles, "A", backup_count=0)
            self.assertFalse(os.path.isfile(backup_path))

    def test_successful_persist_returns_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            _config, backup_path, settings = self._setup(tmp)
            profiles = _make_valid_profiles("A")
            sig = persist_profiles(settings, backup_path, profiles, "A")
            self.assertNotEqual(sig, "")
            self.assertEqual(len(sig), 64)  # SHA-256 hex


class TestListProfileBackups(unittest.TestCase):
    """Cover list_profile_backups and list_profile_history."""

    def test_list_includes_live_backup_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            profiles = _make_valid_profiles("A")
            _write_backup_file(backup_path, profiles, "A")
            # Create history
            archive_existing_backup(backup_path, min_interval=0)
            result = list_profile_backups(backup_path)
            self.assertGreaterEqual(len(result), 1)
            self.assertEqual(result[0], os.path.abspath(backup_path))

    def test_no_backup_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "nonexistent.json")
            result = list_profile_backups(backup_path)
            self.assertEqual(result, [])


class TestUnsupportedProfileMessage(unittest.TestCase):
    """Cover _unsupported_profile_message (line 85)."""

    def test_formats_message_with_prefix(self):
        result = _unsupported_profile_message("test error")
        self.assertTrue(result.startswith("方案版本不受支持："))
        self.assertIn("test error", result)


class TestProfilesBackupPayload(unittest.TestCase):
    """Cover profiles_backup_payload and _commit_signature."""

    def test_payload_structure(self):
        profiles = _make_valid_profiles("A")
        payload = profiles_backup_payload(profiles, "A", generation=5)
        self.assertEqual(payload["format"], PROFILE_BACKUP_FORMAT)
        self.assertEqual(payload["version"], PROFILE_BACKUP_VERSION)
        self.assertEqual(payload["generation"], 5)
        self.assertEqual(payload["current_profile"], "A")
        self.assertIn("signature", payload)
        self.assertIn("commit_signature", payload)
        self.assertIn("profiles", payload)
        self.assertIn("saved_at", payload)

    def test_invalid_generation_raises(self):
        profiles = _make_valid_profiles("A")
        with self.assertRaises(ValueError):
            profiles_backup_payload(profiles, "A", generation=-1)


class TestPruneFailureDoesNotBreakSave(unittest.TestCase):
    """FR-101-002 故障注入回归：备份清理失败（文件被占用/权限）不得让已成功的
    保存误报失败，也不得阻断返回；count=0 时残留副本不会被读取。"""

    def _setup(self, tmp):
        config_path = os.path.join(tmp, "config.ini")
        backup_path = os.path.join(tmp, "profiles_backup.json")
        settings = QSettings(config_path, QSettings.IniFormat)
        return config_path, backup_path, settings

    def test_persist_profiles_succeeds_when_prune_raises_permission_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            _config, backup_path, settings = self._setup(tmp)
            profiles = _make_valid_profiles("A")
            _write_backup_file(backup_path, profiles, "A")  # 预置残留备份
            real_remove = os.remove

            def guarded_remove(path, *args, **kwargs):
                # 仅对目标备份文件注入删除失败，其余路径照常（不影响 atomic 写）。
                if os.path.abspath(str(path)) == os.path.abspath(backup_path):
                    raise PermissionError(13, "Permission denied")
                return real_remove(path, *args, **kwargs)

            with mock.patch(
                "fukua_rpa.config_store.os.remove", side_effect=guarded_remove
            ), self.assertLogs("fukua_rpa.config_store", level="WARNING") as logs:
                signature = persist_profiles(
                    settings, backup_path, profiles, "A", backup_count=0
                )

            # 主配置已写成功，persist_profiles 不抛异常、返回签名
            self.assertIsNotNone(signature)
            self.assertEqual(int(settings.value("profiles_generation")), 1)
            # 删除失败被记录 warning 而非阻断保存
            self.assertTrue(
                any("删除自动备份失败" in message for message in logs.output)
            )
            # 残留副本仍存在，但 count=0 加载明确跳过（"自动备份已关闭"）
            self.assertTrue(os.path.isfile(backup_path))
            loaded, _current, _path, err = _try_load_history(
                os.path.dirname(backup_path), 0
            )
            self.assertIsNone(loaded)
            self.assertEqual(err, "自动备份已关闭")

    def test_prune_profile_backups_swallows_permission_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_path = os.path.join(tmp, "backup.json")
            _write_backup_file(backup_path, _make_valid_profiles("A"), "A")
            with mock.patch(
                "fukua_rpa.config_store.os.remove",
                side_effect=PermissionError(13, "denied"),
            ):
                removed = prune_profile_backups(backup_path, 0)
            self.assertEqual(removed, [])
            self.assertTrue(os.path.isfile(backup_path))


class TestCollectionNodeBoundMatchesByteCap(unittest.TestCase):
    """FR-20261011-006 / DOC-SCOPE005-001：集合级加载的节点上限必须与导出侧口径
    一致。导出侧只校验聚合字节（``MAX_PROFILE_STORE_JSON_BYTES``）与逐方案节点，
    导入侧此前按默认 2M 节点校验整份正文，于是存在“导出成功但本版本无法导入”的
    方案集合。"""

    class _FakeSettings:
        """Minimal QSettings stand-in exposing only the legacy profiles path."""

        def __init__(self, data):
            self._data = data

        def contains(self, key):
            return key in self._data

        def value(self, key, default=None):
            return self._data.get(key, default)

    def test_payload_above_default_node_cap_but_within_byte_cap_loads(self):
        from fukua_rpa.constants import MAX_PROFILE_STORE_JSON_BYTES
        from fukua_rpa.json_safety import DEFAULT_MAX_NODES, loads_bounded_json

        # 每个方案节点数低于默认上限，两份合计超过默认上限。
        filler = [0] * (DEFAULT_MAX_NODES // 2 + 1000)
        profile_a = default_profile_config()
        profile_a["tasks"] = [{"type": 1.0, "value": list(filler), "step_id": "s1"}]
        profile_b = default_profile_config()
        profile_b["tasks"] = [{"type": 1.0, "value": list(filler), "step_id": "s1"}]
        text = json.dumps({"A": profile_a, "B": profile_b}, ensure_ascii=False)
        self.assertLessEqual(len(text.encode("utf-8")), MAX_PROFILE_STORE_JSON_BYTES)

        # 旧行为：默认节点上限会拒绝这份仍在字节上限内的集合正文。
        with self.assertRaises(ValueError):
            loads_bounded_json(text, max_bytes=MAX_PROFILE_STORE_JSON_BYTES, label="t")

        # 真实加载路径必须接受它（导出成功的集合一定可被本版本导入）。
        record, error = _load_settings_record(
            self._FakeSettings({"profiles_json": text})
        )
        self.assertEqual(error, "")
        self.assertIsNotNone(record)


if __name__ == "__main__":
    unittest.main()
