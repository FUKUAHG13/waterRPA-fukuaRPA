import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.engine import RPAEngine
from fukua_rpa.workflow_security import (
    analyze_workflow_capabilities,
    disable_dangerous_workflow_actions,
    sanitize_profile_for_sharing,
)

# NOTE: 候选字段通过静态读取产品源码探测，因此测试必须从仓库根目录运行。
_REPO_ROOT_USED_BY_WHITELIST_TESTS = True


class WorkflowSecurityTests(unittest.TestCase):
    def test_privacy_sanitizer_removes_private_values_images_and_mappings(self):
        profile = default_profile_config()
        profile["tasks"] = [
            {"type": 1.0, "value": "D:/PRIVATE_IMAGE_SENTINEL.png"},
            {"type": 4.0, "value": "PRIVATE_TEXT_SENTINEL"},
            {"type": 12.0, "value": "PRIVATE_PROMPT_SENTINEL"},
            {"type": 14.0, "value": "PRIVATE_SOUND_SENTINEL"},
            {"type": 22.0, "value": "PRIVATE_CREDENTIAL_NAME"},
        ]
        profile["mapping_mode_enabled"] = True
        profile["key_mappings"] = [
            {
                "enabled": True,
                "hotkey": "Ctrl+PRIVATE_HOTKEY_SENTINEL",
                "x": 123,
                "y": 456,
                "window_binding": {"title": "PRIVATE_WINDOW_SENTINEL"},
            }
        ]

        result = sanitize_profile_for_sharing(profile)
        serialized = json.dumps(result.profile, ensure_ascii=False)

        self.assertEqual(result.redacted_steps, (1, 2, 3, 4, 5))
        self.assertEqual(result.disabled_mapping_count, 1)
        self.assertEqual(result.profile["key_mappings"], [])
        self.assertFalse(result.profile["mapping_mode_enabled"])
        self.assertNotIn("PRIVATE_", serialized)
        self.assertTrue(
            all(task["step_disabled"] for task in result.profile["tasks"])
        )

    def test_sanitizer_clears_condition_overlay_images_too(self):
        # TST04（确认缺口）：覆盖匹配小图（until_cond{index}_overlay_image）与
        # 主图同为条件引用模板图（task_images.py 共享枚举已一并纳入）。脱敏分享
        # 必须同时清除 overlay 图字段，否则分享包仍携带本机绝对路径引用。
        profile = default_profile_config()
        profile["tasks"] = [
            {
                "type": 5.0,
                "value": "1",
                "until_cond1_image": "D:/PRIVATE_MAIN_SENTINEL.png",
                "until_cond1_overlay_image": "D:/PRIVATE_OVERLAY_SENTINEL.png",
                "until_cond2_overlay_image": "D:/PRIVATE_OVERLAY2_SENTINEL.png",
            },
        ]

        result = sanitize_profile_for_sharing(profile)
        task = result.profile["tasks"][0]
        serialized = json.dumps(result.profile, ensure_ascii=False)

        self.assertEqual(task["until_cond1_image"], "")
        self.assertEqual(task["until_cond1_overlay_image"], "")
        self.assertEqual(task["until_cond2_overlay_image"], "")
        self.assertNotIn("PRIVATE_OVERLAY", serialized)
        self.assertNotIn("PRIVATE_MAIN", serialized)
        self.assertEqual(result.removed_image_count, 3)

    def test_privacy_sanitizer_fails_closed_for_unknown_fields_and_actions(self):
        profile = default_profile_config()
        profile["future_private_payload"] = {
            "value": "PRIVATE_TOP_LEVEL_SENTINEL"
        }
        profile["tasks"] = [
            {
                "type": 5.0,
                "value": "1",
                "plugin_payload": {"secret": "PRIVATE_FIELD_SENTINEL"},
            },
            {
                "type": "PRIVATE_UNKNOWN_ACTION_SENTINEL",
                "value": "PRIVATE_UNKNOWN_VALUE_SENTINEL",
                "extension": "PRIVATE_UNKNOWN_EXTENSION_SENTINEL",
            },
        ]

        result = sanitize_profile_for_sharing(profile)
        serialized = json.dumps(result.profile, ensure_ascii=False)

        self.assertNotIn("PRIVATE_", serialized)
        self.assertEqual(result.redacted_steps, (1, 2))
        self.assertTrue(result.profile["tasks"][0]["step_disabled"])
        # The string sentinel is not a valid numeric command code, so it is
        # normalized to 0.0 (S6 only preserves numeric command codes).
        self.assertEqual(result.profile["tasks"][1]["type"], 0.0)
        self.assertEqual(result.profile["tasks"][1]["value"], "")
        self.assertTrue(result.profile["tasks"][1]["step_disabled"])

    def test_global_sound_alert_audio_path_is_cleared_for_sharing(self):
        """FR-20260811-D5-002: 全局 sound_alert_audio_path 是本机绝对路径，
        分享脱敏必须清空（KNOWN_PROFILE_FIELDS 放行该字段导致分享包携带
        本机目录结构指纹；步骤级同名字段不在 TASK_DATA_DEFAULTS，已按未知
        字段剥离）。"""
        profile = default_profile_config()
        profile["sound_alert_audio_path"] = r"C:\Users\demo\Music\alert.wav"
        profile["tasks"] = [
            {"type": 14.0, "value": "声音提醒", "step_id": "s1"},
        ]
        result = sanitize_profile_for_sharing(profile)
        self.assertEqual(result.profile.get("sound_alert_audio_path"), "")
        serialized = json.dumps(result.profile, ensure_ascii=False)
        self.assertNotIn(r"C:\Users\demo", serialized)
        self.assertNotIn("alert.wav", serialized)

    def test_step_level_sound_alert_audio_path_is_cleared_for_sharing(self):
        """REVIEW-20260913-001 FR-001: 步骤级 sound_alert_audio_path 也是本机
        绝对路径，与全局同名字段（398-403 行）和图片步骤 value（456-459 行）
        同属隐私面。该字段在 _STRUCTURAL_TASK_FIELDS 内，不会被"未知字段"分支
        剥离，因此必须显式置空并停用该步骤；否则分享包会携带本机目录结构指纹，
        且接收方看到的该步骤仍处于启用状态。"""
        profile = default_profile_config()
        profile["tasks"] = [
            {
                "type": 14.0,
                "value": "",
                "step_id": "s1",
                "sound_alert_audio_path": r"C:\Users\demo\Music\step-alert.wav",
            },
        ]

        result = sanitize_profile_for_sharing(profile)

        task = result.profile["tasks"][0]
        self.assertEqual(task.get("sound_alert_audio_path"), "")
        self.assertTrue(task.get("step_disabled"))
        self.assertIn(1, result.redacted_steps)
        serialized = json.dumps(result.profile, ensure_ascii=False)
        self.assertNotIn(r"C:\Users\demo", serialized)
        self.assertNotIn("step-alert.wav", serialized)

    def test_unknown_numeric_command_preserves_original_type(self):
        # S6: a numeric command code that is simply not (yet) known must keep
        # its original type through sharing sanitization instead of being
        # flattened to 0.0, so the recipient can still see what it was. The
        # step is disabled regardless, so it never executes on import.
        profile = default_profile_config()
        profile["tasks"] = [
            {"type": 99.0, "value": "X", "step_id": "7"},
            {"type": 123.0, "value": "Y"},
        ]

        result = sanitize_profile_for_sharing(profile)

        self.assertEqual(result.redacted_steps, (1, 2))
        self.assertEqual(result.profile["tasks"][0]["type"], 99.0)
        self.assertEqual(result.profile["tasks"][1]["type"], 123.0)
        self.assertEqual(result.profile["tasks"][0]["value"], "")
        self.assertTrue(result.profile["tasks"][0]["step_disabled"])
        self.assertTrue(result.profile["tasks"][1]["step_disabled"])

    def test_known_task_fields_stay_in_sync_with_task_data_defaults(self):
        from fukua_rpa.task_model import TASK_DATA_DEFAULTS
        from fukua_rpa.workflow_security import (
            _STRUCTURAL_TASK_FIELDS,
            KNOWN_TASK_FIELDS,
        )

        # (a) Every data-default field is recognized as a known task field, so
        # the sanitizer can never mistake a legitimate field for an unknown one.
        self.assertEqual(set(TASK_DATA_DEFAULTS) - KNOWN_TASK_FIELDS, set())
        # (b) Structural fields are disjoint from the data-default fields.
        self.assertEqual(
            _STRUCTURAL_TASK_FIELDS & set(TASK_DATA_DEFAULTS), set()
        )
        # KNOWN_TASK_FIELDS is exactly their union (no stale or missing entries).
        self.assertEqual(
            KNOWN_TASK_FIELDS,
            set(TASK_DATA_DEFAULTS) | _STRUCTURAL_TASK_FIELDS,
        )

        # (c) A task carrying every default key plus the structural fields keeps
        # all of them through sharing sanitization (none stripped as unknown).
        task = copy.deepcopy(TASK_DATA_DEFAULTS)
        task.update(
            {
                "type": 5.0,
                "value": "1",
                "retry": "0",
                "window_timeout": "0",
            }
        )
        self.assertEqual(set(task) - KNOWN_TASK_FIELDS, set())
        profile = default_profile_config()
        profile["tasks"] = [task]

        result = sanitize_profile_for_sharing(profile)
        sanitized = result.profile["tasks"][0]
        self.assertEqual(set(sanitized) - KNOWN_TASK_FIELDS, set())
        # FR-007: 上面构造的入参只注入了这四个核心结构字段，因此保留性断言
        # 只针对它们。_STRUCTURAL_TASK_FIELDS 已扩展为完整白名单（含连点器等
        # 真实持久化字段），其全量保留性由 TaskFieldWhitelistSyncTests 覆盖。
        for field in ("type", "value", "retry", "window_timeout"):
            self.assertIn(field, sanitized)
        for field in TASK_DATA_DEFAULTS:
            self.assertIn(field, sanitized)

    def test_privacy_sanitizer_redacts_system_hotkey_value(self):
        profile = default_profile_config()
        profile["tasks"] = [
            {"type": 7.0, "value": "Ctrl+PRIVATE_HOTKEY_SENTINEL"}
        ]

        result = sanitize_profile_for_sharing(profile)
        serialized = json.dumps(result.profile, ensure_ascii=False)

        self.assertNotIn("PRIVATE_HOTKEY_SENTINEL", serialized)
        self.assertEqual(result.profile["tasks"][0]["value"], "")
        self.assertEqual(result.redacted_steps, (1,))
        self.assertTrue(result.profile["tasks"][0]["step_disabled"])

    def test_capability_review_counts_state_changing_actions(self):
        profile = default_profile_config()
        profile["tasks"] = [
            {"type": 1.0, "value": "10,20"},
            {"type": 4.0, "value": "text"},
            {"type": 5.0, "value": "1"},
            {"type": 18.0, "value": "program.exe"},
            {"type": 19.0, "value": "window", "step_disabled": True},
        ]
        report = analyze_workflow_capabilities(profile)

        self.assertEqual(report.step_count, 5)
        self.assertEqual(report.enabled_step_count, 4)
        self.assertEqual(report.disabled_step_count, 1)
        self.assertEqual(report.mouse_action_count, 1)
        self.assertEqual(report.keyboard_action_count, 1)
        self.assertEqual(report.launch_count, 1)
        self.assertEqual(report.dangerous_steps, (1, 2, 4))

    def test_disable_dangerous_import_keeps_wait_but_disables_input(self):
        profile = default_profile_config()
        profile["tasks"] = [
            {"type": 1.0, "value": "10,20"},
            {"type": 5.0, "value": "1"},
        ]
        protected = disable_dangerous_workflow_actions(profile)

        self.assertTrue(protected["tasks"][0]["step_disabled"])
        self.assertFalse(protected["tasks"][1].get("step_disabled", False))

    def test_plugin_action_is_counted_and_disableable_on_import(self):
        """REVIEW-20260913-001 FR-007: 插件动作（27.0）是外部代码入口，按
        ARCHITECTURE #54 属"应允许用户一键停用后再导入"的高影响能力。它此前
        既不在 DANGEROUS_ACTIONS（能力摘要不统计、一键停用不覆盖），又因在
        COMMAND_BY_CODE 内而不落入"未知动作"分支，形成导入审查盲区。"""
        from fukua_rpa.constants import TASK_TYPE_PLUGIN_ACTION

        profile = default_profile_config()
        profile["tasks"] = [
            {"type": TASK_TYPE_PLUGIN_ACTION, "value": "", "step_id": "s1"},
        ]

        report = analyze_workflow_capabilities(profile)

        self.assertIn(1, report.dangerous_steps)
        self.assertNotIn(1, report.unknown_action_steps)

        neutralized = disable_dangerous_workflow_actions(profile)
        self.assertTrue(neutralized["tasks"][0]["step_disabled"])

    def test_auto_clicker_step_is_counted_and_disableable_on_import(self):
        """FR-20261011-005: 连点器步骤（28.0）执行真实点击，按 ARCHITECTURE #54
        属"应允许用户一键停用后再导入"的高影响能力。它此前既不在
        MOUSE_ACTIONS/DANGEROUS_ACTIONS，又因在 COMMAND_BY_CODE 内而不落入
        "未知动作"分支，形成与 27.0 相同的导入审查盲区（旧版工作流可保留该
        步骤并仍执行真实点击）。"""
        from fukua_rpa.constants import TASK_TYPE_AUTO_CLICKER

        profile = default_profile_config()
        profile["tasks"] = [
            {"type": TASK_TYPE_AUTO_CLICKER, "value": "", "step_id": "s1"},
        ]

        report = analyze_workflow_capabilities(profile)

        self.assertEqual(report.mouse_action_count, 1)
        self.assertIn(1, report.dangerous_steps)
        self.assertNotIn(1, report.unknown_action_steps)

        neutralized = disable_dangerous_workflow_actions(profile)
        self.assertTrue(neutralized["tasks"][0]["step_disabled"])

    def test_recognition_test_uses_relative_click_point_without_clicking(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "target.png")
            with open(image_path, "wb") as handle:
                handle.write(b"placeholder")
            with mock.patch.object(
                RPAEngine, "set_high_priority", lambda _self: None
            ):
                engine = RPAEngine(defer_backends=True)
            engine.opencv_available = True
            task = {
                "type": 1.0,
                "value": image_path,
                "custom_en": True,
                "custom_conf": "0.75",
                "custom_scale_min": "0.8",
                "custom_scale_max": "1.2",
                "custom_scale_step": "0.1",
                "custom_gray": True,
                "image_click_point_en": True,
                "image_click_point_rx": "0.25",
                "image_click_point_ry": "0.75",
            }
            with mock.patch.object(
                engine, "load_and_precompute", return_value=True
            ), mock.patch.object(
                engine,
                "find_target_optimized",
                return_value=(500.0, 300.0, 1.2, 0.91),
            ), mock.patch.object(
                engine, "template_dimensions", return_value=(100, 40)
            ), mock.patch.object(engine, "perform_mouse_click") as click:
                result = engine.recognize_task_once(task)

            self.assertTrue(result["found"])
            self.assertAlmostEqual(result["click_x"], 470.0)
            self.assertAlmostEqual(result["click_y"], 312.0)
            self.assertAlmostEqual(result["matched_width"], 120.0)
            self.assertAlmostEqual(result["matched_height"], 48.0)
            click.assert_not_called()

    def test_recognition_test_uses_input_space_match_size(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = os.path.join(temp_dir, "target.png")
            with open(image_path, "wb") as handle:
                handle.write(b"placeholder")
            with mock.patch.object(
                RPAEngine, "set_high_priority", lambda _self: None
            ):
                engine = RPAEngine(defer_backends=True)
            task = {
                "type": 1.0,
                "value": image_path,
                "image_click_point_en": True,
                "image_click_point_rx": "1.0",
                "image_click_point_ry": "0.0",
            }
            with mock.patch.object(
                engine, "load_and_precompute", return_value=True
            ), mock.patch.object(
                engine,
                "find_target_optimized",
                return_value=(500.0, 300.0, 1.0, 0.91, 150.0, 60.0),
            ), mock.patch.object(
                engine, "template_dimensions", return_value=(100, 40)
            ):
                result = engine.recognize_task_once(task)

        self.assertEqual(result["matched_width"], 150.0)
        self.assertEqual(result["matched_height"], 60.0)
        self.assertEqual(result["click_x"], 575.0)
        self.assertEqual(result["click_y"], 270.0)

    def test_recognition_test_is_rejected_while_engine_is_running(self):
        engine = RPAEngine(defer_backends=True)
        run_id = engine.lifecycle.reserve()
        self.assertIsNotNone(run_id)
        engine.img_cache = {"runtime-sentinel": object()}
        with (
            mock.patch.object(engine, "close_screenshot_backend") as close,
            self.assertRaisesRegex(RuntimeError, "运行进行中"),
        ):
            engine.recognize_task_once({"type": 1.0, "value": "missing.png"})

        self.assertIn("runtime-sentinel", engine.img_cache)
        close.assert_not_called()
        engine.lifecycle.finish(run_id, "test-cleanup")


class TaskFieldWhitelistSyncTests(unittest.TestCase):
    """FR-007：脱敏白名单必须覆盖引擎/绑定真实写入与消费的持久化字段。

    既有测试只做**单向**同步断言（TASK_DATA_DEFAULTS ⊆ KNOWN_TASK_FIELDS），
    覆盖不到"不在 defaults 里、但确实被 task_settings_binding 写入且被
    actions/flow 消费"的字段。这类字段在脱敏分享包中被当未知字段剥离，
    并因"携带它们的步骤被整体停用"而与发送方的流程结构不一致。
    """

    def _written_by_binding(self):
        import re
        from pathlib import Path

        source = Path("fukua_rpa/ui/task_settings_binding.py").read_text(
            encoding="utf-8"
        )
        return set(re.findall(r'data\["([a-z0-9_]+)"\]', source))

    def _consumed_ac_fields(self):
        import re
        from pathlib import Path

        source = Path("fukua_rpa/actions/flow.py").read_text(encoding="utf-8")
        return set(re.findall(r'get\("(ac_[a-z0-9_]+)"', source))

    def _bulk_capability_fields(self):
        from fukua_rpa.bulk_task_settings import (
            CONTROL_FIELD_CAPABILITY,
            SETTING_FIELD_CAPABILITY,
        )

        return set(SETTING_FIELD_CAPABILITY) | set(CONTROL_FIELD_CAPABILITY)

    def test_persisted_task_fields_are_all_known(self):
        from fukua_rpa.workflow_security import KNOWN_TASK_FIELDS

        candidates = (
            self._written_by_binding()
            | self._consumed_ac_fields()
            | self._bulk_capability_fields()
        )
        self.assertTrue(candidates, "候选字段集不应为空（探测方式失效）")
        unknown = sorted(candidates - KNOWN_TASK_FIELDS)
        self.assertEqual(
            unknown,
            [],
            "以下真实持久化字段被脱敏白名单当作未知字段，"
            "会导致分享包步骤被整体停用：\n  " + "\n  ".join(unknown),
        )

    def test_sharing_package_keeps_real_clicker_fields(self):
        """端到端：携带连点器与鼠标参数的步骤在脱敏后不得被停用。

        步类型取 5.0（等待）——图片类步骤（1.0）因隐私剥离本就恒为停用，
        无法用于验证"仅因携带白名单外字段而被停用"这一缺陷。
        """

        from fukua_rpa.task_model import TASK_DATA_DEFAULTS
        from fukua_rpa.workflow_security import _STRUCTURAL_TASK_FIELDS

        profile = default_profile_config()
        # 真实步骤是"defaults + 结构字段"的完整字典，缺陷也只在完整字典上出现。
        task = copy.deepcopy(TASK_DATA_DEFAULTS)
        task.update(
            {
                "type": 5.0,
                "value": "1",
                "retry": "0",
                "window_timeout": "0",
                "click_return": True,
                "mouse_trail_mode": "inherit",
                "ac_button": "left",
                "ac_click_count": "1",
            }
        )
        # 其余结构字段用空值占位，验证整批字段都不被当未知字段剥离。
        for field in _STRUCTURAL_TASK_FIELDS:
            task.setdefault(field, "")
        task["type"] = 5.0
        task["value"] = "1"
        profile["tasks"] = [task]

        result = sanitize_profile_for_sharing(profile)
        sanitized = result.profile["tasks"][0]

        self.assertFalse(
            sanitized["step_disabled"], "白名单内字段不应导致步骤被停用"
        )
        self.assertEqual(sanitized["click_return"], True)
        self.assertEqual(sanitized["mouse_trail_mode"], "inherit")
        self.assertEqual(sanitized["ac_button"], "left")
        for field in _STRUCTURAL_TASK_FIELDS:
            self.assertIn(field, sanitized, f"{field} 被当作未知字段剥离")


if __name__ == "__main__":
    unittest.main()
