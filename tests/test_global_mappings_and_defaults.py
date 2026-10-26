"""全局映射、默认设置模板与映射热键命名（2026-10-21 维护者裁定）。

覆盖：
1. 按键映射不随方案走——保存/加载后所有方案共用同一套映射；
2. 新建方案沿用「默认设置」模板；
3. 默认映射热键永不生成无法解析的 F 键（F13 及以上）。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QSettings

from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.config_store import (
    GLOBAL_MAPPING_KEY,
    _load_settings_record,
    _settings_commit_payload,
    load_global_mappings,
    load_profiles_state,
    persist_profiles,
    profiles_backup_payload,
)
from fukua_rpa.constants import PROFILE_COMMIT_VERSION
from fukua_rpa.ui.settings_defaults_actions import (
    build_new_profile_config,
    load_default_template,
    save_default_template,
)


def _profile(**overrides):
    config = default_profile_config()
    config.update(overrides)
    return config


class _FakeHost:
    """Minimal host exposing the two methods the template builder needs."""

    def __init__(self, settings, config):
        self.settings = settings
        self._config = config

    def get_current_ui_config(self):
        return dict(self._config)


class GlobalMappingTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.settings = QSettings(
            os.path.join(self._dir.name, "config.ini"), QSettings.IniFormat
        )
        self.addCleanup(self.settings.sync)
        self.backup = os.path.join(self._dir.name, "profiles_backup.json")

    def test_persist_mirrors_current_mappings_to_every_profile(self):
        a = _profile(key_mappings=[{"hotkey": "F1", "coord": "1,1", "enabled": False}])
        b = _profile(key_mappings=[{"hotkey": "F2", "coord": "2,2", "enabled": False}])
        persist_profiles(
            self.settings, self.backup, {"A": a, "B": b}, "A", backup_count=0
        )
        record, error = _load_settings_record(self.settings)
        self.assertEqual(error, "")
        self.assertIsNotNone(record)
        self.assertEqual(
            record.profiles["B"]["key_mappings"], record.profiles["A"]["key_mappings"]
        )
        self.assertEqual(record.profiles["A"]["key_mappings"][0]["hotkey"], "F1")
        self.assertTrue(self.settings.value(GLOBAL_MAPPING_KEY, ""))

    def test_switch_profile_keeps_mapping_set(self):
        """模拟切换：B 方案保存时带着旧映射，加载后仍等于全局映射。"""
        a = _profile(key_mappings=[{"hotkey": "F1", "coord": "1,1", "enabled": False}])
        b = _profile(key_mappings=[{"hotkey": "F9", "coord": "9,9", "enabled": False}])
        persist_profiles(
            self.settings, self.backup, {"A": a, "B": b}, "A", backup_count=0
        )
        # 再以 B 为当前方案保存（B 的内存值可能是旧的）
        record, _ = _load_settings_record(self.settings)
        profiles = record.profiles
        profiles["B"]["key_mappings"] = [
            {"hotkey": "F9", "coord": "9,9", "enabled": False}
        ]
        persist_profiles(self.settings, self.backup, profiles, "B", backup_count=0)
        record2, _ = _load_settings_record(self.settings)
        self.assertEqual(
            record2.profiles["A"]["key_mappings"], record2.profiles["B"]["key_mappings"]
        )
        self.assertEqual(record2.profiles["B"]["key_mappings"][0]["hotkey"], "F9")


    def test_load_global_mappings_matches_persisted_set(self):
        """切换方案前用它覆盖目标方案，保证界面不闪回旧映射。"""
        a = _profile(key_mappings=[{"hotkey": "F4", "coord": "4,4", "enabled": False}])
        b = _profile(key_mappings=[{"hotkey": "F5", "coord": "5,5", "enabled": False}])
        persist_profiles(
            self.settings, self.backup, {"A": a, "B": b}, "A", backup_count=0
        )
        shared = load_global_mappings(self.settings)
        self.assertEqual(shared["key_mappings"][0]["hotkey"], "F4")
        self.assertEqual(shared["key_mapping_count"], a["key_mapping_count"])

    def test_rejected_save_leaves_shared_state_untouched(self):
        """FR-20261021-003：结构校验失败的保存不得写入共享全局键。"""
        good = _profile(key_mappings=[{"hotkey": "F1", "coord": "1,1", "enabled": False}])
        persist_profiles(
            self.settings,
            self.backup,
            {"A": good, "B": dict(good)},
            "A",
            backup_count=0,
        )
        before = self.settings.value(GLOBAL_MAPPING_KEY, "")

        broken = _profile(key_mappings="not-a-list")
        with self.assertRaises(ValueError):
            persist_profiles(
                self.settings, self.backup, {"A": broken, "B": dict(good)}, "A", backup_count=0
            )

        self.assertEqual(
            self.settings.value(GLOBAL_MAPPING_KEY, ""), before, "失败保存改写了全局键"
        )
        record, error = _load_settings_record(self.settings)
        self.assertEqual(error, "")
        self.assertIsNotNone(record)
        self.assertIsInstance(record.profiles["A"]["key_mappings"], list)
        # 失败之后仍能正常保存合法内容
        persist_profiles(
            self.settings, self.backup, {"A": good, "B": dict(good)}, "A", backup_count=0
        )

    def test_invalid_global_key_is_ignored_and_repaired(self):
        """FR-20261021-003：损坏的全局键不得扩散到所有方案。"""
        good = _profile(key_mappings=[{"hotkey": "F2", "coord": "2,2", "enabled": False}])
        persist_profiles(
            self.settings,
            self.backup,
            {"A": good, "B": dict(good)},
            "A",
            backup_count=0,
        )
        self.settings.setValue(GLOBAL_MAPPING_KEY, '{"key_mappings": "not-a-list"}')

        record, error = _load_settings_record(self.settings)
        self.assertEqual(error, "")
        self.assertIsNotNone(record)
        for name in ("A", "B"):
            self.assertIsInstance(
                record.profiles[name]["key_mappings"], list, f"{name} 被无效全局键覆盖"
            )
        # 读侧自愈：用方案自带映射重新播种全局键
        self.assertNotIn("not-a-list", self.settings.value(GLOBAL_MAPPING_KEY, ""))


class DefaultTemplateTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.settings = QSettings(
            os.path.join(self._dir.name, "config.ini"), QSettings.IniFormat
        )

    def test_new_profile_inherits_saved_template(self):
        current = _profile(conf="0.55", theme="dark")
        current["tasks"] = [{"type": 1.0, "value": "1,1"}]
        save_default_template(self.settings, current)
        template = load_default_template(self.settings)
        self.assertIsNotNone(template)
        self.assertEqual(template["conf"], "0.55")
        self.assertNotIn("tasks", template)

        host = _FakeHost(self.settings, current)
        built = build_new_profile_config(host)
        self.assertEqual(built["conf"], "0.55")
        self.assertEqual(built["tasks"], [])
        # 映射沿用当前方案（全局共享），不因模板缺失而丢
        self.assertIn("key_mappings", built)

    def test_without_template_new_profile_uses_factory_defaults(self):
        host = _FakeHost(self.settings, _profile(conf="0.55"))
        built = build_new_profile_config(host)
        self.assertEqual(built["conf"], default_profile_config()["conf"])

    def test_template_uses_bounded_json_parser(self):
        """FR-20261021-005：模板必须走有界解析入口，拒绝重复键/NaN/超深结构。"""
        from fukua_rpa.ui.settings_defaults_actions import DEFAULT_TEMPLATE_KEY

        cases = {
            "重复键": '{"conf": "0.5", "conf": "0.9"}',
            "NaN": '{"conf": NaN}',
            "超深": '{"a": ' * 200 + "1" + "}" * 200,
        }
        for label, raw in cases.items():
            with self.subTest(label=label):
                self.settings.setValue(DEFAULT_TEMPLATE_KEY, raw)
                self.assertIsNone(load_default_template(self.settings), label)

    def test_valid_template_still_loads(self):
        self.settings.setValue(
            "default_profile_template_json", '{"conf": "0.66", "theme": "dark"}'
        )
        template = load_default_template(self.settings)
        self.assertIsNotNone(template)
        self.assertEqual(template["conf"], "0.66")


class UpgradeNormalizationTests(unittest.TestCase):
    """FR-20261021-002：升级期共享映射归一化不得被误判为“写入未完成”。

    构造方式：直接写「升级前」存储（各方案映射不同、无全局键、主提交与备份同代次），
    绕过 `persist_profiles` 的镜像，确保复现的是真实升级路径。
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.settings = QSettings(
            os.path.join(self._dir.name, "config.ini"), QSettings.IniFormat
        )
        self.addCleanup(self.settings.sync)
        self.backup = os.path.join(self._dir.name, "profiles_backup.json")
        self.config_path = os.path.join(self._dir.name, "config_legacy.ini")

    def _mapping(self, hotkey):
        return [{"hotkey": hotkey, "coord": "1,1", "enabled": False}]

    def _write_store(self, main_profiles, backup_profiles, generation=7,
                     current="A", backup_generation=None):
        commit = _settings_commit_payload(main_profiles, current, generation)
        self.settings.setValue(
            "profiles_commit_json", json.dumps(commit, ensure_ascii=False)
        )
        self.settings.setValue("current_profile", current)
        self.settings.sync()
        if backup_profiles is not None:
            payload = profiles_backup_payload(
                backup_profiles,
                current,
                generation if backup_generation is None else backup_generation,
            )
            Path(self.backup).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )

    def _load(self, backup_count=3):
        return load_profiles_state(
            self.settings, self.config_path, self.backup, backup_count=backup_count
        )

    def test_upgrade_with_backup_unifies_without_false_recovery(self):
        """判据 1：正常升级 + 有备份 → 映射统一、无“只完成原子备份”误报。"""
        a = _profile(key_mappings=self._mapping("F1"))
        b = _profile(key_mappings=self._mapping("F2"))
        self._write_store({"A": a, "B": b}, {"A": a, "B": b})

        result = self._load()

        self.assertEqual(result.source, "settings")
        self.assertEqual(result.recovery_message, "")
        self.assertEqual(result.profiles["A"]["key_mappings"][0]["hotkey"], "F1")
        self.assertEqual(result.profiles["B"]["key_mappings"][0]["hotkey"], "F1")

    def test_real_incomplete_write_is_still_detected(self):
        """判据 2：同代次但内容真实不同 → 仍判 pending_backup，不被统一映射掩盖。"""
        main = {
            "A": _profile(key_mappings=self._mapping("F1")),
            "B": _profile(key_mappings=self._mapping("F1")),
        }
        backup = {
            "A": _profile(key_mappings=self._mapping("F3")),
            "B": _profile(key_mappings=self._mapping("F3")),
        }
        self._write_store(main, backup)

        result = self._load()

        self.assertEqual(result.source, "pending_backup")
        self.assertIn("只完成了原子备份", result.recovery_message)
        self.assertEqual(result.profiles["A"]["key_mappings"][0]["hotkey"], "F3")

    def test_higher_generation_backup_wins(self):
        """边界：备份代次更高时仍优先采用备份。"""
        self._write_store(
            {"A": _profile(key_mappings=self._mapping("F1"))},
            {"A": _profile(key_mappings=self._mapping("F4"))},
            generation=7,
            backup_generation=8,
        )

        result = self._load()

        self.assertEqual(result.source, "pending_backup")
        self.assertEqual(result.profiles["A"]["key_mappings"][0]["hotkey"], "F4")

    def test_backup_disabled_still_unifies(self):
        """边界：backup_count=0 时不读取备份，归一化仍生效。"""
        a = _profile(key_mappings=self._mapping("F1"))
        b = _profile(key_mappings=self._mapping("F2"))
        self._write_store({"A": a, "B": b}, None)

        result = self._load(backup_count=0)

        self.assertEqual(result.source, "settings")
        self.assertEqual(result.profiles["B"]["key_mappings"][0]["hotkey"], "F1")

    def test_first_upgrade_seeds_global_key(self):
        """边界：首次升级没有全局键时，用当前方案播种。"""
        a = _profile(key_mappings=self._mapping("F1"))
        b = _profile(key_mappings=self._mapping("F2"))
        self._write_store({"A": a, "B": b}, None)

        self._load(backup_count=0)

        shared = load_global_mappings(self.settings)
        self.assertEqual(shared["key_mappings"][0]["hotkey"], "F1")

    def test_corrupt_backup_does_not_block_normal_load(self):
        """边界：备份损坏时主配置照常加载，不误报恢复。"""
        self._write_store(
            {"A": _profile(key_mappings=self._mapping("F1"))}, None
        )
        Path(self.backup).write_text("{not json", encoding="utf-8")

        result = self._load()

        self.assertEqual(result.source, "settings")
        self.assertEqual(result.recovery_message, "")
        self.assertEqual(result.profiles["A"]["key_mappings"][0]["hotkey"], "F1")

    def test_history_fallback_still_works(self):
        """边界：主配置与当前备份都不可用时，历史备份仍可恢复。"""
        history_dir = Path(self._dir.name) / "profiles_history"
        history_dir.mkdir()
        payload = profiles_backup_payload(
            {"A": _profile(key_mappings=self._mapping("F5"))}, "A", 5
        )
        (history_dir / "profiles_20261022.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        result = self._load()

        self.assertEqual(result.source, "history")
        self.assertIn("历史备份", result.recovery_message)
        self.assertEqual(result.profiles["A"]["key_mappings"][0]["hotkey"], "F5")

    def test_unsupported_version_still_blocks(self):
        """边界：更高版本写入的配置仍被拒绝并禁止保存。"""
        commit = _settings_commit_payload(
            {"A": _profile(key_mappings=self._mapping("F1"))}, "A", 7
        )
        commit["version"] = PROFILE_COMMIT_VERSION + 1
        self.settings.setValue(
            "profiles_commit_json", json.dumps(commit, ensure_ascii=False)
        )

        result = self._load()

        self.assertEqual(result.source, "unsupported")
        self.assertTrue(result.persistence_blocked)


class _HotkeyHost:
    def __init__(self, rows):
        self.key_mapping_rows = rows

class _Row:
    def __init__(self, text):
        class _Edit:
            def text(self_inner):
                return text

        self._hotkey = _Edit()

    def get(self, key):
        return self._hotkey if key == "hotkey" else None


class MappingHotkeyDefaultTests(unittest.TestCase):
    def _next(self, texts):
        from fukua_rpa.ui.controllers.mapping_mixins.row_management import (
            MappingRowMixin,
        )

        host = _HotkeyHost([_Row(t) for t in texts])
        mixin = MappingRowMixin.__new__(MappingRowMixin)
        mixin.host = host
        return mixin._next_default_mapping_hotkey()

    def test_first_free_fkey(self):
        self.assertEqual(self._next([]), "F1")
        self.assertEqual(self._next(["F1", "F2"]), "F3")

    def test_never_exceeds_f12(self):
        used = [f"F{i}" for i in range(1, 13)]
        self.assertEqual(self._next(used), "")
        # 关键回归：旧实现会返回 F13，保存后整份方案无法通过校验
        self.assertNotEqual(self._next(used), "F13")


if __name__ == "__main__":
    unittest.main()
