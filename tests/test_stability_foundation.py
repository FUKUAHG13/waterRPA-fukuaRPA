import hashlib
import json
import os
import random
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings

from fukua_rpa.commands import COMMAND_SPECS, command_code, command_name, command_names
from fukua_rpa.config_schema import (
    UnsupportedProfileVersion,
    default_profile_config,
    migrate_profile_config,
)
from fukua_rpa.config_store import (
    archive_existing_backup,
    atomic_write_json,
    list_profile_backups,
    list_profile_history,
    load_profiles_backup,
    load_profiles_state,
    persist_profiles,
    profiles_backup_payload,
    profiles_signature,
    validate_profile_config_data,
    validate_profiles_payload,
)
from fukua_rpa.constants import (
    BUILD_NAME,
    MAX_PROFILE_NAME_LENGTH,
    PROFILE_BACKUP_FORMAT,
    PROFILE_SCHEMA_VERSION,
)
from fukua_rpa.engine import RPAEngine
from fukua_rpa.engine.actions_exec import registered_action_codes
from fukua_rpa.profile_model import ProfileCollection
from fukua_rpa.run_config import EngineRunConfig, RunConfigError, RunRequest
from fukua_rpa.runtime_state import RunLifecycle, RunState
from fukua_rpa.scheduler import next_runnable_loop, non_negative_int, positive_int
from fukua_rpa.session import ApplicationSession, SingleInstanceGuard
from fukua_rpa.validation import validate_task_list
from fukua_rpa.workflow_analysis import analyze_workflow_structure, build_workflow_graph


class ConfigurationFoundationTests(unittest.TestCase):
    def test_profile_signature_remains_compatible_with_pre_commit_format(self):
        profiles = {"B": {"value": 2}, "A": {"value": 1}}
        legacy_canonical = json.dumps(
            profiles, ensure_ascii=False, sort_keys=True
        )
        expected = hashlib.sha256(
            f"A\n{legacy_canonical}".encode()
        ).hexdigest()

        _stored, signature = profiles_signature(profiles, "A")

        self.assertEqual(signature, expected)

    def test_command_registry_round_trips_every_persisted_command(self):
        self.assertEqual(len(command_names()), len(set(command_names())))
        # 29.0 = 录制输入时序（批次 2）；新指令必须同时具备注册表项与 handler，
        # 因此这里改为「连续覆盖 1..29」而不是只放到 28。
        self.assertEqual(
            [spec.code for spec in COMMAND_SPECS],
            [float(i) for i in range(1, 30)],
        )
        for spec in COMMAND_SPECS:
            self.assertEqual(command_name(spec.code), spec.name)
            self.assertEqual(command_code(spec.name), spec.code)
        self.assertIsNone(command_code("未来版本动作"))
        self.assertEqual(registered_action_codes(), {spec.code for spec in COMMAND_SPECS})

    def test_migration_preserves_unknown_fields_and_detaches_nested_values(self):
        source = {
            "tasks": [{"type": 1.0, "value": "1,2", "future_field": {"x": 1}}],
            "key_mappings": [],
            "future_profile_field": [1, 2],
            "multi_target_mode": "最佳一个",
        }
        result = migrate_profile_config(source)
        self.assertTrue(result.changed)
        self.assertEqual(result.value["_schema_version"], PROFILE_SCHEMA_VERSION)
        self.assertEqual(result.value["multi_target_mode"], "快速一个")
        self.assertEqual(result.value["future_profile_field"], [1, 2])
        result.value["tasks"][0]["future_field"]["x"] = 9
        self.assertEqual(source["tasks"][0]["future_field"]["x"], 1)

    def test_migration_removes_obsolete_low_power_ui_setting(self):
        source = default_profile_config()
        source["low_power_ui"] = True
        source["cpu_refresh_interval"] = "auto"

        result = migrate_profile_config(source)

        self.assertTrue(result.changed)
        self.assertNotIn("low_power_ui", result.value)
        self.assertEqual(result.value["cpu_refresh_interval"], 2000)

    def test_future_profile_version_is_rejected_without_downgrade(self):
        profile = default_profile_config()
        profile["_schema_version"] = PROFILE_SCHEMA_VERSION + 1
        with self.assertRaises(UnsupportedProfileVersion):
            migrate_profile_config(profile)

    def test_compatibility_floor_can_reject_an_expired_major_schema(self):
        profile = default_profile_config()
        profile["_schema_version"] = 0
        with mock.patch(
            "fukua_rpa.config_schema.MIN_SUPPORTED_PROFILE_SCHEMA_VERSION", 1
        ), self.assertRaises(UnsupportedProfileVersion):
            migrate_profile_config(profile)

    def test_signed_legacy_backup_is_verified_before_migration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = os.path.join(temp_dir, "profiles_backup.json")
            profile = default_profile_config()
            profile["_schema_version"] = 1
            profile.pop("log_mode")
            profile.pop("log_custom_categories")
            profiles = {"A": profile}
            signature = profiles_signature(profiles, "A")[1]
            atomic_write_json(
                backup_path,
                {
                    "format": PROFILE_BACKUP_FORMAT,
                    "current_profile": "A",
                    "signature": signature,
                    "profiles": profiles,
                },
            )

            loaded, current, error = load_profiles_backup(backup_path)

            self.assertEqual(error, "")
            self.assertEqual(current, "A")
            self.assertEqual(
                loaded["A"]["_schema_version"], PROFILE_SCHEMA_VERSION
            )
            self.assertEqual(loaded["A"]["log_mode"], "simple")

    def test_newer_startup_profile_enters_read_only_protection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            backup_path = os.path.join(temp_dir, "profiles_backup.json")
            settings = QSettings(config_path, QSettings.IniFormat)
            profile = default_profile_config()
            profile["_schema_version"] = PROFILE_SCHEMA_VERSION + 1
            profiles = {"Future": profile}
            signature = profiles_signature(profiles, "Future")[1]
            settings.setValue(
                "profiles_json", json.dumps(profiles, ensure_ascii=False)
            )
            settings.setValue("current_profile", "Future")
            settings.setValue("profiles_signature", signature)
            settings.sync()

            loaded = load_profiles_state(settings, config_path, backup_path)

            self.assertEqual(loaded.source, "unsupported")
            self.assertTrue(loaded.persistence_blocked)
            self.assertEqual(loaded.profiles, {})
            self.assertIn("不会被覆盖", loaded.recovery_message)

    def test_newer_atomic_backup_blocks_an_older_main_config_from_saving(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            backup_path = os.path.join(temp_dir, "profiles_backup.json")
            settings = QSettings(config_path, QSettings.IniFormat)
            current_profiles = {"Current": default_profile_config()}
            current_signature = profiles_signature(
                current_profiles, "Current"
            )[1]
            settings.setValue(
                "profiles_json",
                json.dumps(current_profiles, ensure_ascii=False),
            )
            settings.setValue("current_profile", "Current")
            settings.setValue("profiles_signature", current_signature)
            settings.sync()

            future_profile = default_profile_config()
            future_profile["_schema_version"] = PROFILE_SCHEMA_VERSION + 1
            future_profiles = {"Future": future_profile}
            atomic_write_json(
                backup_path,
                {
                    "format": PROFILE_BACKUP_FORMAT,
                    "current_profile": "Future",
                    "signature": profiles_signature(
                        future_profiles, "Future"
                    )[1],
                    "profiles": future_profiles,
                },
            )
            with open(backup_path, "rb") as handle:
                original_backup = handle.read()

            loaded = load_profiles_state(settings, config_path, backup_path)

            self.assertEqual(loaded.source, "unsupported_backup")
            self.assertTrue(loaded.persistence_blocked)
            self.assertIn("Current", loaded.profiles)
            with self.assertRaises(UnsupportedProfileVersion):
                persist_profiles(
                    settings,
                    backup_path,
                    current_profiles,
                    "Current",
                )
            with open(backup_path, "rb") as handle:
                self.assertEqual(handle.read(), original_backup)

    def test_profile_collection_keeps_order_and_unsaved_data_on_rename(self):
        model = ProfileCollection()
        model.replace({"A": default_profile_config(), "B": default_profile_config()}, "A")
        model.profiles["A"]["tasks"] = [{"type": 1.0, "value": "3,4"}]
        model.rename("A2")
        self.assertEqual(list(model.profiles), ["A2", "B"])
        self.assertEqual(model.profiles["A2"]["tasks"][0]["value"], "3,4")
        self.assertEqual(model.move_current(1), 1)
        self.assertEqual(list(model.profiles), ["B", "A2"])
        model.reorder(["A2", "B"])
        self.assertEqual(list(model.profiles), ["A2", "B"])
        with self.assertRaisesRegex(ValueError, "不一致"):
            model.reorder(["A2"])

    def test_profile_names_are_trimmed_and_bounded_before_model_mutation(self):
        model = ProfileCollection()
        model.replace({"A": default_profile_config()}, "A")
        model.create("  新方案  ")
        self.assertIn("新方案", model.profiles)
        self.assertNotIn("  新方案  ", model.profiles)

        before = model.snapshot()
        with self.assertRaisesRegex(ValueError, "最多允许"):
            model.rename("x" * (MAX_PROFILE_NAME_LENGTH + 1))
        self.assertEqual(model.snapshot(), before)

    def test_profile_store_rejects_noncanonical_whitespace_name(self):
        error = validate_profiles_payload({" 方案 ": default_profile_config()})
        self.assertIn("两端不能包含空格", error)

    def test_profile_validator_rejects_field_explosion(self):
        profile = default_profile_config()
        profile["tasks"] = [
            {"type": 1.0, "value": "1,1", **{f"extra_{index}": index for index in range(300)}}
        ]
        self.assertIn("字段过多", validate_profile_config_data(profile))

    def test_profile_validator_rejects_deep_unknown_data_without_recursion(self):
        nested = {"leaf": True}
        for _index in range(80):
            nested = {"next": nested}
        profile = default_profile_config()
        profile["unknown_future_data"] = nested

        error = validate_profile_config_data(profile)

        self.assertIsNotNone(error)
        self.assertIn("嵌套层级超过上限", error)

    def test_config_recovery_falls_back_to_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            backup_path = os.path.join(temp_dir, "profiles_backup.json")
            settings = QSettings(config_path, QSettings.IniFormat)
            profile_a = {"A": default_profile_config()}
            profile_a["A"]["tasks"] = [{"type": 1.0, "value": "10,20"}]
            persist_profiles(settings, backup_path, profile_a, "A")
            archived = archive_existing_backup(backup_path, min_interval=0)
            self.assertTrue(os.path.isfile(archived))

            settings.setValue("profiles_json", "{broken")
            settings.setValue("profiles_commit_json", "{broken")
            settings.sync()
            with open(backup_path, "w", encoding="utf-8") as handle:
                handle.write("{broken")

            loaded = load_profiles_state(settings, config_path, backup_path)
            self.assertEqual(loaded.source, "history")
            self.assertEqual(loaded.current_profile, "A")
            self.assertEqual(loaded.profiles["A"]["tasks"][0]["value"], "10,20")

    def test_newer_atomic_backup_wins_after_partial_settings_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            backup_path = os.path.join(temp_dir, "profiles_backup.json")
            settings = QSettings(config_path, QSettings.IniFormat)
            old_profiles = {"A": default_profile_config()}
            old_profiles["A"]["tasks"] = [{"type": 1.0, "value": "1,1"}]
            persist_profiles(settings, backup_path, old_profiles, "A")

            new_profiles = {"A": default_profile_config()}
            new_profiles["A"]["tasks"] = [{"type": 1.0, "value": "2,2"}]
            atomic_write_json(
                backup_path,
                profiles_backup_payload(new_profiles, "A", generation=2),
            )

            loaded = load_profiles_state(settings, config_path, backup_path)
            self.assertEqual(loaded.source, "pending_backup")
            self.assertEqual(loaded.profiles["A"]["tasks"][0]["value"], "2,2")

    def test_missing_main_record_recovers_from_atomic_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            backup_path = os.path.join(temp_dir, "profiles_backup.json")
            settings = QSettings(config_path, QSettings.IniFormat)
            profiles = {"A": default_profile_config()}
            profiles["A"]["tasks"] = [{"type": 1.0, "value": "7,8"}]
            persist_profiles(settings, backup_path, profiles, "A")
            for key in (
                "profiles_commit_json",
                "profiles_json",
                "profiles_signature",
                "profiles_generation",
            ):
                settings.remove(key)
            settings.sync()

            loaded = load_profiles_state(settings, config_path, backup_path)

            self.assertEqual(loaded.source, "backup")
            self.assertEqual(loaded.profiles["A"]["tasks"][0]["value"], "7,8")

    def test_stale_backup_does_not_replace_newer_committed_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            backup_path = os.path.join(temp_dir, "profiles_backup.json")
            settings = QSettings(config_path, QSettings.IniFormat)
            old_profiles = {"A": default_profile_config()}
            old_profiles["A"]["tasks"] = [{"type": 1.0, "value": "1,1"}]
            persist_profiles(settings, backup_path, old_profiles, "A")
            with open(backup_path, "rb") as handle:
                stale_backup = handle.read()

            new_profiles = {"A": default_profile_config()}
            new_profiles["A"]["tasks"] = [{"type": 1.0, "value": "9,9"}]
            persist_profiles(settings, backup_path, new_profiles, "A")
            with open(backup_path, "wb") as handle:
                handle.write(stale_backup)

            loaded = load_profiles_state(settings, config_path, backup_path)

            self.assertEqual(loaded.source, "settings")
            self.assertEqual(loaded.profiles["A"]["tasks"][0]["value"], "9,9")

    def test_profile_order_survives_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            backup_path = os.path.join(temp_dir, "profiles_backup.json")
            settings = QSettings(config_path, QSettings.IniFormat)
            profiles = {
                "Z-last": default_profile_config(),
                "A-first": default_profile_config(),
            }

            persist_profiles(settings, backup_path, profiles, "Z-last")
            stored = json.loads(settings.value("profiles_json"))
            loaded = load_profiles_state(settings, config_path, backup_path)

            self.assertEqual(list(stored), ["Z-last", "A-first"])
            self.assertEqual(list(loaded.profiles), ["Z-last", "A-first"])

    def test_tampered_main_generation_falls_back_to_valid_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            backup_path = os.path.join(temp_dir, "profiles_backup.json")
            settings = QSettings(config_path, QSettings.IniFormat)
            profiles = {"A": default_profile_config()}
            persist_profiles(settings, backup_path, profiles, "A")
            commit = json.loads(settings.value("profiles_commit_json"))
            commit["generation"] += 1
            settings.setValue(
                "profiles_commit_json", json.dumps(commit, ensure_ascii=False)
            )
            settings.sync()

            loaded = load_profiles_state(settings, config_path, backup_path)

            self.assertEqual(loaded.source, "backup")
            self.assertIn("提交签名不匹配", loaded.recovery_message)

    def test_signed_backup_detects_valid_json_tampering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = os.path.join(temp_dir, "profiles_backup.json")
            profiles = {"A": default_profile_config()}
            payload = profiles_backup_payload(profiles, "A")
            payload["profiles"]["A"]["conf"] = "0.1"
            atomic_write_json(backup_path, payload)
            loaded, _current, error = load_profiles_backup(backup_path)
            self.assertIsNone(loaded)
            self.assertIn("签名不匹配", error)

    def test_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            backup_path = os.path.join(temp_dir, "profiles_backup.json")
            atomic_write_json(
                backup_path,
                {
                    "format": "fukuaRPA_profiles_backup",
                    "profiles": {"A": default_profile_config()},
                    "current_profile": "A",
                },
            )
            for index in range(5):
                profile = default_profile_config()
                profile["marker"] = index
                atomic_write_json(
                    backup_path,
                    {
                        "format": "fukuaRPA_profiles_backup",
                        "profiles": {"A": profile},
                        "current_profile": "A",
                    },
                )
                archive_existing_backup(backup_path, limit=3, min_interval=0)
            self.assertLessEqual(len(list_profile_history(backup_path)), 3)

    def test_user_backup_count_includes_latest_and_can_disable_every_copy(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            backup_path = os.path.join(temp_dir, "profiles_backup.json")
            settings = QSettings(config_path, QSettings.IniFormat)
            profiles = {"A": default_profile_config()}

            for marker in range(5):
                profiles["A"]["conf"] = f"0.{marker + 1}"
                persist_profiles(
                    settings,
                    backup_path,
                    profiles,
                    "A",
                    backup_count=3,
                )
                for path in list_profile_history(backup_path):
                    os.utime(path, (1, 1))

            self.assertEqual(len(list_profile_backups(backup_path)), 3)
            self.assertEqual(len(list_profile_history(backup_path)), 2)

            persist_profiles(
                settings, backup_path, profiles, "A", backup_count=1
            )
            self.assertEqual(list_profile_backups(backup_path), [backup_path])

            persist_profiles(
                settings, backup_path, profiles, "A", backup_count=0
            )
            self.assertEqual(list_profile_backups(backup_path), [])
            loaded = load_profiles_state(
                settings,
                config_path,
                backup_path,
                backup_count=0,
            )
            self.assertEqual(loaded.source, "settings")
            self.assertEqual(loaded.profiles["A"]["conf"], "0.5")

    def test_disabled_backups_are_not_used_as_an_implicit_recovery_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = os.path.join(temp_dir, "config.ini")
            backup_path = os.path.join(temp_dir, "profiles_backup.json")
            settings = QSettings(config_path, QSettings.IniFormat)
            profiles = {"A": default_profile_config()}
            atomic_write_json(backup_path, profiles_backup_payload(profiles, "A"))

            loaded = load_profiles_state(
                settings,
                config_path,
                backup_path,
                backup_count=0,
            )

            self.assertEqual(loaded.source, "empty")
            self.assertEqual(loaded.profiles, {})


class RuntimeFoundationTests(unittest.TestCase):
    def test_only_one_thread_can_reserve_a_run(self):
        lifecycle = RunLifecycle()
        barrier = threading.Barrier(8)
        results = []
        lock = threading.Lock()

        def reserve():
            barrier.wait()
            value = lifecycle.reserve()
            with lock:
                results.append(value)

        threads = [threading.Thread(target=reserve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(lifecycle.state, RunState.PREPARING)

    def test_stale_finish_cannot_end_a_new_run(self):
        lifecycle = RunLifecycle()
        first = lifecycle.reserve()
        self.assertTrue(lifecycle.finish(first))
        second = lifecycle.reserve()
        self.assertFalse(lifecycle.finish(first))
        self.assertTrue(lifecycle.matches(second))
        lifecycle.request_stop()
        self.assertEqual(lifecycle.state, RunState.STOPPING)
        self.assertTrue(lifecycle.finish(second, "stopped"))
        self.assertEqual(lifecycle.snapshot().last_outcome, "stopped")

    def test_run_request_is_detached_from_ui_data(self):
        tasks = [{"type": 1.0, "value": "10,20", "nested": {"x": 1}}]
        config = default_profile_config()
        config["tasks"] = tasks
        request = RunRequest.create(tasks, config, "A")
        tasks[0]["value"] = "99,99"
        tasks[0]["nested"]["x"] = 2
        self.assertEqual(request.tasks[0]["value"], "10,20")
        self.assertEqual(request.tasks[0]["nested"]["x"], 1)

    def test_invalid_runtime_numbers_are_rejected(self):
        config = default_profile_config()
        config["detect_delay"] = "nan"
        with self.assertRaises(RunConfigError):
            EngineRunConfig.from_mapping(config)

    def test_runtime_waits_and_deadlines_have_finite_upper_bounds(self):
        for field in ("settle", "timeout"):
            config = default_profile_config()
            config[field] = "86401"
            with self.subTest(field=field), self.assertRaises(RunConfigError):
                EngineRunConfig.from_mapping(config)

        config = default_profile_config()
        config["loop_mode"] = "指定时间(秒)"
        config["loop_val"] = str(365 * 24 * 60 * 60 + 1)
        with self.assertRaises(RunConfigError):
            EngineRunConfig.from_mapping(config)

    def test_native_optimization_settings_validate_and_apply(self):
        config = default_profile_config()
        config.update(
            {
                "scale_memory_tier": "aggressive",
                "scale_memory_manual": "0.8, 1.0, 1.2",
                "scale_memory_custom_en": True,
                "scale_memory_preferred_limit": 5,
                "scale_memory_history_limit": 96,
            }
        )
        runtime = EngineRunConfig.from_mapping(config)
        self.assertEqual(runtime.native_parallel_mode, "auto")
        self.assertTrue(runtime.use_native_scale_hint)
        self.assertEqual(runtime.scale_memory_tier, "aggressive")
        self.assertEqual(runtime.scale_memory_manual, (0.8, 1.0, 1.2))
        self.assertTrue(runtime.scale_memory_custom_enabled)
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine()
        runtime.apply_to(engine)
        self.assertEqual(engine.native_parallel_mode, "auto")
        self.assertTrue(engine.use_native_scale_hint)
        self.assertEqual(engine.scale_memory_manual, (0.8, 1.0, 1.2))
        self.assertEqual(engine.scale_memory_preferred_limit, 5)
        self.assertEqual(engine.scale_memory_history_limit, 96)

    def test_invalid_scale_memory_settings_are_rejected(self):
        for field, value in (
            ("scale_memory_tier", "unknown"),
            ("scale_memory_manual", "not-a-number"),
            ("scale_memory_preferred_limit", 13),
            ("scale_memory_history_limit", 7),
        ):
            config = default_profile_config()
            config[field] = value
            with self.subTest(field=field), self.assertRaises(RunConfigError):
                EngineRunConfig.from_mapping(config)

        config["native_parallel_mode"] = "unbounded"
        with self.assertRaises(RunConfigError):
            EngineRunConfig.from_mapping(config)

    def test_random_bad_runtime_fields_never_escape_as_unexpected_exceptions(self):
        random.seed(481516)
        values = [None, "", "nan", "inf", -1, 0, [], {}, object(), "999999999999999999999"]
        fields = [
            "conf",
            "scale_min",
            "scale_max",
            "scale_step",
            "move_spd",
            "click_hld",
            "detect_delay",
            "playback_speed",
            "start_step",
            "loop_start_round",
            "loop_end_round",
            "log_level",
        ]
        for _case in range(500):
            config = default_profile_config()
            config[random.choice(fields)] = random.choice(values)
            try:
                EngineRunConfig.from_mapping(config, 3)
            except RunConfigError:
                pass
            except Exception as error:
                self.fail(f"unexpected {type(error).__name__}: {error}")
        config = default_profile_config()
        config["loop_mode"] = "单次"
        config["loop_start_round"] = "2"
        with self.assertRaises(RunConfigError):
            EngineRunConfig.from_mapping(config)

    def test_fuzz_from_mapping_random_seed(self):
        """Random-seed variant (set FUZZ_SEED=random to enable)."""
        if os.environ.get("FUZZ_SEED", "") != "random":
            self.skipTest("set FUZZ_SEED=random to enable")
        seed = random.randint(0, 2**32 - 1)
        random.seed(seed)
        values = [None, "", "nan", "inf", -1, 0, [], {}, object(), "999999999999999999999"]
        fields = [
            "conf",
            "scale_min",
            "scale_max",
            "scale_step",
            "move_spd",
            "click_hld",
            "detect_delay",
            "playback_speed",
            "start_step",
            "loop_start_round",
            "loop_end_round",
            "log_level",
        ]
        for _case in range(500):
            config = default_profile_config()
            config[random.choice(fields)] = random.choice(values)
            try:
                EngineRunConfig.from_mapping(config, 3)
            except RunConfigError:
                pass
            except Exception as error:
                self.fail(f"seed={seed} unexpected {type(error).__name__}: {error}")

    def test_engine_cleans_lifecycle_and_callbacks_after_exception(self):
        with mock.patch.object(RPAEngine, "set_high_priority", lambda _self: None):
            engine = RPAEngine()
        engine.log_level = -1
        engine.load_and_precompute = lambda _tasks: True
        engine.execute_task_once = mock.Mock(side_effect=RuntimeError("boom"))
        engine.run_tasks([{"type": 4.0, "value": "A"}])
        self.assertFalse(engine.is_running)
        self.assertIsNone(engine.callback_msg)
        self.assertIsNone(engine.callback_status)
        self.assertIsNone(engine.callback_click_indicator)
        self.assertEqual(engine.lifecycle.snapshot().last_outcome, "error")

    def test_application_session_detects_same_build_dead_marker_and_cleans_own_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = os.path.join(temp_dir, ".fukuaRPA_session.json")
            atomic_write_json(
                marker,
                {
                    "format": "fukuaRPA_session",
                    "version": 1,
                    "pid": 2_000_000_000,
                    "token": "old",
                    "build_name": BUILD_NAME,
                    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            session = ApplicationSession(temp_dir).start()
            self.assertTrue(session.previous_unclean)
            self.assertTrue(session.close())
            self.assertFalse(os.path.exists(marker))

    def test_application_session_ignores_dead_marker_from_another_build(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = os.path.join(temp_dir, ".fukuaRPA_session.json")
            atomic_write_json(
                marker,
                {
                    "format": "fukuaRPA_session",
                    "version": 1,
                    "pid": 2_000_000_000,
                    "token": "old",
                    "build_name": "fukuaRPA_v0.0.0-test",
                    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )

            session = ApplicationSession(temp_dir).start()

            self.assertFalse(session.previous_unclean)
            self.assertTrue(session.close())

    def test_application_session_treats_malformed_pid_as_unclean_not_fatal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = os.path.join(temp_dir, ".fukuaRPA_session.json")
            atomic_write_json(
                marker,
                {
                    "format": "fukuaRPA_session",
                    "version": 1,
                    "pid": "not-a-pid",
                    "token": "old",
                },
            )

            session = ApplicationSession(temp_dir).start()

            self.assertTrue(session.previous_unclean)
            self.assertEqual(session.previous_details["pid"], "not-a-pid")
            self.assertTrue(session.close())

    def test_application_session_treats_duplicate_marker_fields_as_unclean(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = os.path.join(temp_dir, ".fukuaRPA_session.json")
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write(
                    '{"format":"fukuaRPA_session","version":1,'
                    '"pid":123,"pid":456,"token":"old"}'
                )

            session = ApplicationSession(temp_dir).start()

            self.assertTrue(session.previous_unclean)
            self.assertIn("重复字段", session.previous_details["_marker_error"])
            self.assertTrue(session.close())

    def test_single_instance_mutex_is_scoped_to_portable_directory(self):
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = SingleInstanceGuard(first_dir)
            duplicate = SingleInstanceGuard(first_dir)
            separate = SingleInstanceGuard(second_dir)
            try:
                self.assertTrue(first.acquire())
                self.assertFalse(duplicate.acquire())
                self.assertTrue(separate.acquire())
            finally:
                duplicate.release()
                separate.release()
                first.release()


class SchedulingAndValidationTests(unittest.TestCase):
    def test_next_runnable_loop_skips_empty_ranges(self):
        tasks = [{"step_loop_start": "100", "step_loop_end": "105"}]
        next_loop = next_runnable_loop(
            tasks,
            1,
            start_step_index=0,
            loop_start_round=1,
            loop_end_round=0,
            loop_mode="无限",
            loop_value=1,
            exhausted=lambda _task, _step: False,
        )
        self.assertEqual(next_loop, 100)

    def test_scheduler_integer_parsers_never_escape_bounds(self):
        random.seed(3107)
        values = [None, "", "nan", "inf", object()]
        values.extend(random.uniform(-1e6, 1e6) for _ in range(1000))
        for value in values:
            self.assertGreaterEqual(positive_int(value), 1)
            self.assertGreaterEqual(non_negative_int(value), 0)

    def test_workflow_graph_targets_stay_inside_terminal_boundary(self):
        random.seed(731)
        for _ in range(200):
            count = random.randint(1, 40)
            tasks = []
            for _index in range(count):
                tasks.append(
                    {
                        "type": random.choice([1.0, 5.0, 15.0]),
                        "success_jump": random.randint(0, count + 20),
                        "fail_skip": random.randint(0, count + 20),
                        "until_true_jump": random.randint(0, count + 20),
                        "until_false_jump": random.randint(0, count + 20),
                    }
                )
            graph = build_workflow_graph(tasks)
            for targets in graph.values():
                self.assertTrue(all(0 <= target <= count for target in targets))

    def test_closed_self_jump_is_reported_as_no_exit(self):
        issues = analyze_workflow_structure(
            [{"type": 1.0, "success_jump": "1", "fail_jump": "1"}]
        )
        self.assertIn("no_exit_path", {issue.code for issue in issues})

    def test_variable_flow_accepts_definite_assignment(self):
        issues = analyze_workflow_structure(
            [
                {"type": 16.0, "value": "count = 1"},
                {"type": 17.0, "value": "count > 0"},
            ]
        )
        self.assertNotIn("variable_maybe_unset", {issue.code for issue in issues})

    def test_variable_flow_warns_when_branch_can_skip_assignment(self):
        issues = analyze_workflow_structure(
            [
                {"type": 17.0, "value": "loop == 1", "success_jump": "3"},
                {"type": 16.0, "value": "answer = 42"},
                {"type": 17.0, "value": "answer == 42"},
            ]
        )
        variable_issues = [
            issue for issue in issues if issue.code == "variable_maybe_unset"
        ]
        self.assertEqual(len(variable_issues), 1)
        self.assertEqual(variable_issues[0].step, 3)
        self.assertIn("answer", variable_issues[0].message)

    def test_first_self_increment_warns_about_missing_initial_value(self):
        issues = analyze_workflow_structure(
            [{"type": 16.0, "value": "count = count + 1"}]
        )
        self.assertIn("variable_maybe_unset", {issue.code for issue in issues})

    def test_conditional_breakpoint_participates_in_variable_flow_analysis(self):
        issues = analyze_workflow_structure(
            [
                {
                    "type": 4.0,
                    "value": "text",
                    "debug_breakpoint": True,
                    "debug_condition": "answer == 42",
                },
                {"type": 16.0, "value": "answer = 42"},
            ]
        )
        variable_issues = [
            issue for issue in issues if issue.code == "variable_maybe_unset"
        ]
        self.assertEqual(len(variable_issues), 1)
        self.assertEqual(variable_issues[0].step, 1)
        self.assertIn("answer", variable_issues[0].message)

    def test_pure_validator_rejects_bad_coordinate_step(self):
        config = default_profile_config()
        config["tasks"] = [
            {
                "type": 1.0,
                "value": "10,20",
                "coord_step_en": True,
                "coord_step_every": "0",
            }
        ]
        error = validate_task_list(config["tasks"], config)
        self.assertIn("步进频率", error)

    def test_pure_validator_rejects_missing_custom_screenshot_region(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = default_profile_config()
            task = {
                "type": 9.0,
                "value": temp_dir,
                "capture_region_mode": "custom",
                "capture_region": "",
            }
            error = validate_task_list([task], config)
        self.assertIn("自定义截图区域", error)

    def test_pure_validator_rejects_unknown_command(self):
        config = default_profile_config()
        config["tasks"] = [{"type": 999.0, "value": "x"}]
        self.assertIn("未注册", validate_task_list(config["tasks"], config))

    def test_pure_validator_ignores_disabled_steps_but_requires_one_enabled_step(self):
        config = default_profile_config()
        disabled_invalid = {
            "type": 999.0,
            "value": "invalid",
            "step_disabled": True,
        }
        enabled = {"type": 4.0, "value": "text"}
        self.assertIsNone(validate_task_list([disabled_invalid, enabled], config))
        self.assertIn(
            "没有已启用的步骤",
            validate_task_list([disabled_invalid], config),
        )

    def test_uia_actions_require_binding_and_read_variable_name(self):
        config = default_profile_config()
        missing = {"type": 23.0, "value": "button"}
        self.assertIn("尚未选择", validate_task_list([missing], config))

        valid = {
            "type": 25.0,
            "value": "result_text",
            "uia_binding": {"root_hwnd": 1, "target_client_x": 10},
        }
        self.assertIsNone(validate_task_list([valid], config))
        invalid_name = dict(valid, value="bad name")
        self.assertIn("变量名无效", validate_task_list([invalid_name], config))

    def test_random_malformed_tasks_return_an_error_instead_of_raising(self):
        random.seed(271828)
        values = [None, "", [], {}, object(), "nan", "-1", "999999999999999999999"]
        fields = [
            "type",
            "value",
            "success_jump",
            "fail_jump",
            "repeat_count",
            "step_loop_start",
            "step_loop_end",
            "coord_step_every",
            "coord_step_max_steps",
            "until_max_seconds",
        ]
        for _case in range(500):
            task = {"type": 1.0, "value": "10,20"}
            task[random.choice(fields)] = random.choice(values)
            result = validate_task_list([task], default_profile_config())
            self.assertTrue(result is None or isinstance(result, str))


if __name__ == "__main__":
    unittest.main()
