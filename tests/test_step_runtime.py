import copy
import os
import tempfile
import unittest
from unittest import mock

from fukua_rpa.commands import (
    CONTROL_FAILURE_BRANCH,
    CONTROL_FAILURE_POLICY,
    CONTROL_REPEAT,
    SETTING_BACKGROUND_CLICK,
    SETTING_CAPTURE_REGION,
    SETTING_CLICK_MOTION,
    SETTING_CLICK_RETURN,
    SETTING_CLICK_SPATIAL_JITTER,
    SETTING_COORDINATE_PATH,
    SETTING_DELAYED_SECOND_CLICK,
    SETTING_IMAGE_CLICK_POINT,
    SETTING_IMAGE_CONFIRM_CLICK,
    SETTING_IMAGE_MASK,
    SETTING_IMAGE_POINT_LIMIT,
    SETTING_IMAGE_RECOGNITION,
    SETTING_INVERTED_MATCH,
    SETTING_MOUSE_TRAIL,
    SETTING_STEP_REGION,
    SETTING_UNTIL_CONDITIONS,
    effective_task_settings,
    step_control_capabilities,
    step_settings_capabilities,
)
from fukua_rpa.config_schema import default_profile_config
from fukua_rpa.constants import TASK_TYPE_EXPRESSION
from fukua_rpa.engine import RPAEngine
from fukua_rpa.run_config import RunRequest
from fukua_rpa.step_runtime import (
    StepAttemptState,
    StepExecutionPolicy,
    attempt_failed,
    step_attempt_label,
    step_status_display,
)
from fukua_rpa.validation import validate_task_list


class StepRuntimeTests(unittest.TestCase):
    def test_action_setting_capabilities_follow_command_and_value_kind(self):
        self.assertEqual(
            step_settings_capabilities("左键单击", "10,20"),
            frozenset(
                {
                    SETTING_COORDINATE_PATH,
                    SETTING_BACKGROUND_CLICK,
                    SETTING_DELAYED_SECOND_CLICK,
                    SETTING_CLICK_SPATIAL_JITTER,
                    SETTING_CLICK_MOTION,
                    SETTING_CLICK_RETURN,
                    SETTING_MOUSE_TRAIL,
                }
            ),
        )
        self.assertEqual(
            step_settings_capabilities("左键单击", "target.png"),
            frozenset(
                {
                    SETTING_IMAGE_RECOGNITION,
                    SETTING_IMAGE_POINT_LIMIT,
                    SETTING_IMAGE_CLICK_POINT,
                    SETTING_STEP_REGION,
                    SETTING_INVERTED_MATCH,
                    SETTING_IMAGE_CONFIRM_CLICK,
                    SETTING_IMAGE_MASK,
                    SETTING_DELAYED_SECOND_CLICK,
                    SETTING_CLICK_SPATIAL_JITTER,
                    SETTING_CLICK_MOTION,
                    SETTING_CLICK_RETURN,
                    SETTING_MOUSE_TRAIL,
                }
            ),
        )
        self.assertEqual(
            step_settings_capabilities("鼠标悬停", "target.png"),
            frozenset(
                {
                    SETTING_IMAGE_RECOGNITION,
                    SETTING_STEP_REGION,
                    SETTING_INVERTED_MATCH,
                    SETTING_IMAGE_MASK,
                    SETTING_MOUSE_TRAIL,
                }
            ),
        )
        self.assertEqual(
            step_settings_capabilities("截图保存", "D:/Screenshots"),
            frozenset({SETTING_CAPTURE_REGION}),
        )
        self.assertEqual(
            step_settings_capabilities("直到条件成立"),
            frozenset({SETTING_UNTIL_CONDITIONS}),
        )
        self.assertEqual(step_settings_capabilities("等待(秒)", "1"), frozenset())
        self.assertEqual(
            step_settings_capabilities("左键单击", ""),
            frozenset(),
        )
        self.assertEqual(
            step_settings_capabilities("左键拖拽", "10,20 -> 30,40"),
            frozenset({SETTING_CLICK_MOTION, SETTING_MOUSE_TRAIL}),
        )

    def test_drag_masks_click_only_shape_fields_but_keeps_curve_and_trail(self):
        source = {
            "type": 10.0,
            "value": "10,20 -> 30,40",
            "click_spatial_jitter": "3",
            "move_curve_strength": "0.5",
            "click_return": True,
            "mouse_trail_mode": "on",
        }

        effective = effective_task_settings(source)

        self.assertEqual(source["click_spatial_jitter"], "3")
        self.assertTrue(source["click_return"])
        self.assertEqual(effective["click_spatial_jitter"], "")
        self.assertEqual(effective["move_curve_strength"], "0.5")
        self.assertFalse(effective["click_return"])
        self.assertEqual(effective["mouse_trail_mode"], "on")

    def test_generic_controls_hide_only_semantically_inert_behavior(self):
        wait_controls = step_control_capabilities("等待(秒)", "1")
        self.assertNotIn(CONTROL_REPEAT, wait_controls)
        self.assertNotIn(CONTROL_FAILURE_POLICY, wait_controls)
        self.assertNotIn(CONTROL_FAILURE_BRANCH, wait_controls)

        click_controls = step_control_capabilities("左键单击", "10,20")
        self.assertIn(CONTROL_REPEAT, click_controls)
        self.assertIn(CONTROL_FAILURE_POLICY, click_controls)
        self.assertIn(CONTROL_FAILURE_BRANCH, click_controls)

    def test_effective_settings_mask_inactive_values_without_mutating_source(self):
        source = {
            "type": 5.0,
            "value": "0",
            "custom_en": True,
            "point_limit_en": True,
            "image_click_point_en": True,
            "step_region_en": True,
            "coord_step_en": True,
            "coord_sequence_en": True,
            "background_click_en": True,
            "repeat_mode": "无限重复",
            "fail_jump": "4",
        }

        effective = effective_task_settings(source)

        self.assertTrue(all(source[key] for key in source if key.endswith("_en")))
        self.assertFalse(effective["custom_en"])
        self.assertFalse(effective["point_limit_en"])
        self.assertFalse(effective["image_click_point_en"])
        self.assertFalse(effective["step_region_en"])
        self.assertFalse(effective["coord_step_en"])
        self.assertFalse(effective["coord_sequence_en"])
        self.assertFalse(effective["background_click_en"])
        self.assertEqual(effective["repeat_mode"], "执行一次")
        self.assertEqual(effective["fail_jump"], "0")

    def test_validation_and_run_snapshot_ignore_inactive_action_values(self):
        task = {
            "type": 5.0,
            "value": "0",
            "custom_en": True,
            "custom_conf": "invalid",
            "point_limit_en": True,
            "point_limit_count": "invalid",
            "image_click_point_en": True,
            "image_click_point_rx": "invalid",
            "step_region_en": True,
            "step_region": "invalid",
            "coord_step_en": True,
            "coord_step_every": "invalid",
            "coord_sequence_en": True,
            "coord_sequence_points": "invalid",
        }
        config = default_profile_config()

        self.assertIsNone(validate_task_list([task], config))
        request = RunRequest.create([task], config, "test")
        self.assertFalse(request.tasks[0]["custom_en"])
        self.assertFalse(request.tasks[0]["coord_step_en"])
        self.assertTrue(task["custom_en"])
        self.assertTrue(task["coord_step_en"])

        coordinate_task = dict(
            task,
            type=1.0,
            value="10,20",
            coord_sequence_en=False,
        )
        error = validate_task_list([coordinate_task], config)
        self.assertIn("步进频率", error)

    def test_background_click_requires_a_current_explicit_window_binding(self):
        config = default_profile_config()
        task = {
            "type": 1.0,
            "value": "320,240",
            "background_click_en": True,
            "background_click_binding": {},
        }
        self.assertIn("尚未选择目标窗口", validate_task_list([task], config))

        task["background_click_binding"] = {
            "root_hwnd": 700,
            "root_client_x": 120,
            "root_client_y": 80,
            "source_x": 300,
            "source_y": 220,
        }
        self.assertIn("坐标已在选择窗口后发生变化", validate_task_list([task], config))

        task["background_click_binding"].update(source_x=320, source_y=240)
        self.assertIsNone(validate_task_list([task], config))

    def test_policy_parses_string_booleans_and_repeat_target(self):
        policy = StepExecutionPolicy.from_task(
            {
                "type": 1.0,
                "value": "target.png",
                "custom_en": "true",
                "custom_conf": "0.65",
                "custom_gray": "false",
                "no_skip_wait": "false",
                "point_limit_en": "true",
                "repeat_mode": "指定次数",
                "repeat_count": "3",
                "fail_limit": "2",
            },
            default_confidence=0.8,
            default_grayscale=True,
            min_scale=0.8,
            max_scale=1.2,
            scale_step=0.05,
        )

        self.assertEqual(policy.confidence, 0.65)
        self.assertFalse(policy.use_grayscale)
        self.assertFalse(policy.no_skip_wait)
        self.assertTrue(policy.point_limit_requested)
        self.assertEqual(policy.target_successes, 3)
        self.assertEqual(policy.fail_limit, 2)

    def test_custom_point_batch_replaces_generic_repeat_for_one_visit(self):
        engine = RPAEngine(defer_backends=True)
        task = {
            "type": 1.0,
            "value": "10,20",
            "repeat_mode": "无限重复",
            "coord_sequence_en": True,
            "coord_sequence_points": "10,20; 30,40; 50,60",
            "coord_sequence_batch_mode": "count",
            "coord_sequence_batch_count": "2",
            "coord_sequence_end_action": "点完后跳过本步",
        }
        policy = StepExecutionPolicy.from_task(
            task,
            default_confidence=0.8,
            default_grayscale=True,
            min_scale=0.8,
            max_scale=1.2,
            scale_step=0.05,
        )
        context = engine._build_step_execution_context(
            task=task,
            task_count=1,
            index=0,
            loop_count=1,
            policy=policy,
        )

        self.assertEqual(context.policy.target_successes, 1)
        self.assertEqual(context.coord_sequence_config["batch_mode"], "count")
        with mock.patch.object(
            engine, "perform_mouse_click", return_value=True
        ) as click, mock.patch.object(
            engine, "schedule_click_supplements"
        ), mock.patch.object(
            engine, "wait_step_interval", return_value=True
        ) as wait:
            status = engine.mouseClick(
                1,
                "left",
                "10,20",
                1,
                step_info={"step": 1, "loop": 1, "cmd": "左键单击"},
                coord_sequence_config=context.coord_sequence_config,
            )

        self.assertEqual(status, "success")
        self.assertEqual(click.call_count, 2)
        self.assertEqual(wait.call_count, 1)
        self.assertEqual(engine.coord_sequence_states[1]["index"], 2)

    def test_custom_point_all_mode_consumes_only_remaining_points(self):
        engine = RPAEngine(defer_backends=True)
        options = engine.coord_sequence_options(
            {
                "coord_sequence_en": True,
                "coord_sequence_points": "1,1; 2,2; 3,3",
                "coord_sequence_batch_mode": "all",
                "coord_sequence_end_action": "点完后跳过本步",
            }
        )
        engine.coord_sequence_states[2] = {"index": 1}

        points, state, status = engine._coord_sequence_locations(
            {"step": 2}, options
        )

        self.assertEqual(status, "ok")
        self.assertEqual(points, [(2, 2), (3, 3)])
        engine._advance_coord_sequence(state)
        engine._advance_coord_sequence(state)
        points, _state, status = engine._coord_sequence_locations(
            {"step": 2}, options
        )
        self.assertEqual(points, [])
        self.assertEqual(status, "done")

    def test_attempt_state_snapshot_is_immutable_result(self):
        state = StepAttemptState(started_at=1.0)
        state.attempts = 2
        state.success_count = 1
        state.consecutive_failures = 3
        state.failed_branch = True
        state.made_progress = True

        result = state.as_result(stop_run=True)
        state.attempts = 9

        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.success_count, 1)
        self.assertEqual(result.consecutive_failures, 3)
        self.assertTrue(result.failed_branch)
        self.assertTrue(result.made_progress)
        self.assertTrue(result.stop_run)

    def test_attempt_labels_cover_single_fixed_and_infinite_repeat(self):
        self.assertEqual(step_attempt_label(1, 1, 0), "")
        self.assertEqual(step_attempt_label(3, 2, 1), "2/3")
        self.assertEqual(step_attempt_label(None, 4, 0), "4/∞")

    def test_status_and_failure_rules_remain_explicit(self):
        self.assertEqual(
            step_status_display(
                "not_found",
                no_skip_wait=True,
                timeout_emergency_stop=False,
            ),
            ("未找目标，继续等待", "orange"),
        )
        self.assertTrue(attempt_failed(14.0, "error"))
        self.assertTrue(attempt_failed(TASK_TYPE_EXPRESSION, "condition_false"))
        self.assertFalse(attempt_failed(4.0, "condition_false"))


class PrecomputeIsolationTests(unittest.TestCase):
    def _make_template(self, directory, name, seed):
        from PIL import Image

        path = os.path.join(directory, name)
        image = Image.new("RGB", (32, 32))
        image.putdata(
            [
                (
                    (x * 31 + y * 17 + x * y + seed) % 255,
                    (x * 11 + y * 29 + x * x + seed * 3) % 255,
                    (x * 7 + y * 43 + y * y + seed * 5) % 255,
                )
                for y in range(32)
                for x in range(32)
            ]
        )
        image.save(path)
        return path

    def test_precompute_does_not_modify_shared_tasks(self):
        engine = RPAEngine()
        engine.use_native_core = False
        with tempfile.TemporaryDirectory() as temp_dir:
            first = self._make_template(temp_dir, "first.png", 1)
            second = self._make_template(temp_dir, "second.png", 2)
            tasks = [
                {
                    "type": 1.0,
                    "value": first,
                    "template_mask_regions": [[0.2, 0.2, 0.2, 0.2]],
                },
                {
                    "type": 15.0,
                    "until_cond1_en": True,
                    "until_cond1_mode": "图片出现",
                    "until_cond1_image": first,
                },
                {
                    "type": 26.0,
                    "multi_image_actions": [
                        {"image": second, "action": "left_click"}
                    ],
                },
            ]
            before = copy.deepcopy(tasks)

            self.assertTrue(engine.load_and_precompute(tasks))

        self.assertEqual(tasks, before)
        self.assertNotIn("cache_key", tasks[0])
        self.assertNotIn("until_cond1_cache_key", tasks[1])
        self.assertNotIn("cache_key", tasks[2]["multi_image_actions"][0])

    def test_execution_policy_accepts_precompute_cache_key_override(self):
        engine = RPAEngine(defer_backends=True)
        task = {
            "type": "1.0",
            "value": "target.png",
            "cache_key": "legacy-key",
            "template_mask_regions": [[0.1, 0.1, 0.3, 0.3]],
        }
        override = engine._image_target_cache_key_override(task)
        self.assertIn("_mask=", override)

        policy = StepExecutionPolicy.from_task(
            task,
            default_confidence=0.8,
            default_grayscale=True,
            min_scale=1.0,
            max_scale=1.0,
            scale_step=0.05,
            cache_key_override=override,
        )

        self.assertEqual(policy.cache_key, override)
        self.assertEqual(task["cache_key"], "legacy-key")


if __name__ == "__main__":
    unittest.main()
