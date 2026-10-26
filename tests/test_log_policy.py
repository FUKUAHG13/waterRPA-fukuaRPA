import unittest

from fukua_rpa.config_schema import default_profile_config, migrate_profile_config
from fukua_rpa.constants import PROFILE_SCHEMA_VERSION
from fukua_rpa.engine import RPAEngine
from fukua_rpa.log_policy import (
    COMPLETE_LOG_CATEGORIES,
    DEFAULT_CUSTOM_LOG_CATEGORIES,
    DETAILED_LOG_CATEGORIES,
    LOG_ACTION,
    LOG_CRITICAL,
    LOG_MODE_COMPLETE,
    LOG_MODE_CUSTOM,
    LOG_MODE_DETAILED,
    LOG_MODE_SIMPLE,
    LOG_PARAMETERS,
    LOG_RECOGNITION,
    LOG_STEP,
    LOG_TIMESTAMP,
    LOG_TIMING,
    SIMPLE_LOG_CATEGORIES,
    LogPolicy,
    normalize_log_categories,
    normalize_log_mode,
)
from fukua_rpa.logging_service import GLOBAL_CONFIG, flush_logs
from fukua_rpa.run_config import EngineRunConfig


class LogPolicyTests(unittest.TestCase):
    def setUp(self):
        self.previous_config = dict(GLOBAL_CONFIG)
        GLOBAL_CONFIG.update({"log_to_file": False, "log_to_ui": True})

    def tearDown(self):
        flush_logs()
        GLOBAL_CONFIG.update(self.previous_config)

    def test_presets_keep_stable_category_sets(self):
        self.assertEqual(
            LogPolicy.create(LOG_MODE_SIMPLE).enabled_categories,
            SIMPLE_LOG_CATEGORIES,
        )
        self.assertEqual(
            LogPolicy.create(LOG_MODE_DETAILED).enabled_categories,
            DETAILED_LOG_CATEGORIES,
        )
        self.assertEqual(
            LogPolicy.create(LOG_MODE_COMPLETE).enabled_categories,
            COMPLETE_LOG_CATEGORIES,
        )
        self.assertFalse(LogPolicy.create(LOG_MODE_SIMPLE).timestamp_enabled)
        self.assertTrue(LogPolicy.create(LOG_MODE_DETAILED).timestamp_enabled)

    def test_custom_policy_filters_categories_but_never_critical_events(self):
        policy = LogPolicy.create(LOG_MODE_CUSTOM, [LOG_RECOGNITION])
        self.assertTrue(policy.allows(LOG_RECOGNITION))
        self.assertFalse(policy.allows(LOG_ACTION))
        self.assertFalse(policy.timestamp_enabled)
        self.assertTrue(policy.allows(LOG_CRITICAL))
        self.assertTrue(policy.allows(LOG_ACTION, message="[错误] test"))

    def test_category_normalization_is_ordered_and_bounded(self):
        self.assertEqual(
            normalize_log_categories(
                [LOG_TIMESTAMP, "unknown", LOG_RECOGNITION, LOG_TIMESTAMP]
            ),
            (LOG_RECOGNITION, LOG_TIMESTAMP),
        )
        self.assertEqual(normalize_log_mode("自定义"), LOG_MODE_CUSTOM)

    def test_schema_one_profile_migrates_from_legacy_level(self):
        profile = default_profile_config()
        profile["_schema_version"] = 1
        profile.pop("log_mode")
        profile.pop("log_custom_categories")
        profile["log_level"] = 1

        migrated = migrate_profile_config(profile)

        self.assertTrue(migrated.changed)
        self.assertEqual(migrated.value["_schema_version"], PROFILE_SCHEMA_VERSION)
        self.assertEqual(migrated.value["log_mode"], LOG_MODE_DETAILED)
        self.assertEqual(
            tuple(migrated.value["log_custom_categories"]),
            DEFAULT_CUSTOM_LOG_CATEGORIES,
        )

    def test_runtime_config_applies_custom_policy_to_engine(self):
        config = default_profile_config()
        config.update(
            {
                "log_level": 2,
                "log_mode": LOG_MODE_CUSTOM,
                "log_custom_categories": [LOG_RECOGNITION],
            }
        )
        runtime = EngineRunConfig.from_mapping(config)
        engine = RPAEngine(defer_backends=True)

        runtime.apply_to(engine)

        self.assertEqual(engine.log_mode, LOG_MODE_CUSTOM)
        self.assertEqual(engine.log_level, 2)
        self.assertEqual(
            engine.current_log_policy().enabled_categories,
            (LOG_RECOGNITION,),
        )

    def test_engine_filters_custom_categories_before_queueing(self):
        engine = RPAEngine(defer_backends=True)
        engine.configure_log_policy(LOG_MODE_CUSTOM, [LOG_RECOGNITION])
        messages = []
        engine.callback_msg = messages.append

        self.assertFalse(engine.log("action", LOG_ACTION))
        self.assertTrue(engine.log("recognition", LOG_RECOGNITION))
        self.assertTrue(
            engine.log("critical", LOG_CRITICAL, critical=True)
        )
        self.assertTrue(flush_logs())

        self.assertEqual(messages, ["recognition", "critical"])

    def run_custom_engine(self, categories, *, execute_result="success"):
        engine = RPAEngine(defer_backends=True)
        engine.configure_log_policy(LOG_MODE_CUSTOM, categories)
        engine.detect_delay = 0
        engine.settlement_wait = 0
        engine.load_and_precompute = lambda _tasks: True
        if isinstance(execute_result, Exception):
            def execute(*_args, **_kwargs):
                raise execute_result
            engine.execute_task_once = execute
        else:
            engine.execute_task_once = lambda *_args, **_kwargs: execute_result
        messages = []
        engine.run_tasks(
            [{"type": 4.0, "value": "secret-value"}],
            callback_msg=messages.append,
        )
        self.assertTrue(flush_logs())
        return "\n".join(messages)

    def test_custom_step_only_run_excludes_other_sections(self):
        joined = self.run_custom_engine([LOG_STEP])
        self.assertIn("循环 #1 步 1", joined)
        self.assertNotIn("完全/运行参数", joined)
        self.assertNotIn("性能摘要", joined)
        self.assertNotIn("结束", joined)

    def test_custom_parameters_emit_snapshots_without_timing(self):
        joined = self.run_custom_engine([LOG_PARAMETERS])
        self.assertIn("完全/运行参数", joined)
        self.assertIn("完全/步骤参数", joined)
        self.assertNotIn("完全/步骤耗时", joined)
        self.assertNotIn("完全/运行性能报告", joined)
        self.assertNotIn("secret-value", joined)

    def test_custom_timing_emits_phase_data_without_parameters(self):
        joined = self.run_custom_engine([LOG_TIMING])
        self.assertIn("完全/步骤耗时", joined)
        self.assertIn("性能摘要", joined)
        self.assertIn("完全/运行性能报告", joined)
        self.assertNotIn("完全/运行参数", joined)

    def test_critical_runtime_failure_survives_empty_custom_selection(self):
        joined = self.run_custom_engine([], execute_result=RuntimeError("boom"))
        self.assertIn("引擎异常: RuntimeError", joined)
        self.assertNotIn("boom", joined)
        self.assertNotIn("完全/运行参数", joined)

    def test_critical_runtime_failure_details_require_privacy_opt_out(self):
        GLOBAL_CONFIG["privacy_protected"] = False
        joined = self.run_custom_engine([], execute_result=RuntimeError("boom"))
        self.assertIn("引擎异常: RuntimeError: 'boom'", joined)


if __name__ == "__main__":
    unittest.main()
