"""Guarded regression tests for the three new settings/features.

Written as ``unittest.TestCase`` classes so the build gate's ``unittest`` run
actually discovers and executes them (plain ``def test_*`` functions are only
picked up by pytest and would be silently skipped by ``unittest``).

These run without a full Qt application; heavy imports are done inside the
test bodies so collection stays cheap and offscreen-only.
"""

import unittest


# --------------------------------------------------------------------------- #
# 功能1: 调整跟随（删除步骤后跳转跟随接替步骤）
# --------------------------------------------------------------------------- #
def _sample_workflow():
    # step_ids must satisfy valid_step_id (8-64 alphanumerics) or they are
    # reassigned by normalize_workflow_tasks.
    return [
        {"type": 1.0, "step_id": "stepAAAA1"},
        {
            "type": 1.0,
            "step_id": "stepBBBB2",
            "success_jump": "3",
            "success_target_id": "stepCCCC3",
        },
        {"type": 1.0, "step_id": "stepCCCC3"},
        {"type": 1.0, "step_id": "stepDDDD4"},
        {"type": 1.0, "step_id": "stepEEEE5"},
    ]


class FollowAdjustTests(unittest.TestCase):
    def test_follow_middle_delete_redirects_to_successor(self):
        from fukua_rpa.workflow_document import remove_tasks_and_follow_references

        out = remove_tasks_and_follow_references(
            _sample_workflow(), ["stepCCCC3"], follow=True
        )
        by_id = {t["step_id"]: t for t in out}
        # C deleted; successor is D (now step 3). B must follow to D.
        self.assertEqual(by_id["stepBBBB2"]["success_target_id"], "stepDDDD4")
        self.assertEqual(by_id["stepBBBB2"]["success_jump"], "3")

    def test_no_follow_middle_delete_clears_reference(self):
        from fukua_rpa.workflow_document import remove_tasks_and_follow_references

        out = remove_tasks_and_follow_references(
            _sample_workflow(), ["stepCCCC3"], follow=False
        )
        by_id = {t["step_id"]: t for t in out}
        self.assertEqual(by_id["stepBBBB2"]["success_target_id"], "")
        self.assertEqual(by_id["stepBBBB2"]["success_jump"], "0")

    def test_follow_multi_delete_redirects_to_next_survivor(self):
        from fukua_rpa.workflow_document import remove_tasks_and_follow_references

        # Delete C and D; B->C should follow to the next survivor E.
        out = remove_tasks_and_follow_references(
            _sample_workflow(), ["stepCCCC3", "stepDDDD4"], follow=True
        )
        by_id = {t["step_id"]: t for t in out}
        self.assertEqual(by_id["stepBBBB2"]["success_target_id"], "stepEEEE5")

    def test_follow_delete_last_step_with_no_successor_clears(self):
        from fukua_rpa.workflow_document import remove_tasks_and_follow_references

        # C is the last step here and B references it; deleting C leaves no successor.
        wf = [
            {"type": 1.0, "step_id": "stepAAAA1"},
            {
                "type": 1.0,
                "step_id": "stepBBBB2",
                "success_jump": "2",
                "success_target_id": "stepCCCC3",
            },
            {"type": 1.0, "step_id": "stepCCCC3"},
        ]
        out = remove_tasks_and_follow_references(wf, ["stepCCCC3"], follow=True)
        by_id = {t["step_id"]: t for t in out}
        # No successor exists, so B's reference is cleared to fall-through.
        self.assertEqual(by_id["stepBBBB2"]["success_target_id"], "")
        self.assertEqual(by_id["stepBBBB2"]["success_jump"], "0")

    def test_follow_delete_unrelated_step_keeps_references(self):
        from fukua_rpa.workflow_document import remove_tasks_and_follow_references

        out = remove_tasks_and_follow_references(
            _sample_workflow(), ["stepAAAA1"], follow=True
        )
        by_id = {t["step_id"]: t for t in out}
        self.assertEqual(by_id["stepBBBB2"]["success_target_id"], "stepCCCC3")

    def test_default_profile_enables_follow(self):
        from fukua_rpa.config_schema import default_profile_config

        self.assertTrue(default_profile_config()["step_adjust_follow"] is True)


# --------------------------------------------------------------------------- #
# 功能2: 声音提示支持本地音频文件
# --------------------------------------------------------------------------- #
class SoundAlertTests(unittest.TestCase):
    def test_sound_alert_audio_path_default_empty(self):
        from fukua_rpa.config_schema import default_profile_config

        self.assertEqual(default_profile_config()["sound_alert_audio_path"], "")

    def test_sound_alert_audio_path_round_trip(self):
        from fukua_rpa.run_config import EngineRunConfig

        cfg = EngineRunConfig.from_mapping({"sound_alert_audio_path": "C:/x/y.mp3"})
        self.assertEqual(cfg.sound_alert_audio_path, "C:/x/y.mp3")

        class FakeEngine:
            def configure_log_policy(self, *a, **k):
                pass

            def configure_input_backend(self, *a, **k):
                pass

        engine = FakeEngine()
        cfg.apply_to(engine)
        self.assertEqual(engine.sound_alert_audio_path, "C:/x/y.mp3")

    def test_play_sound_alert_falls_back_without_file(self):
        import importlib

        af = importlib.import_module("fukua_rpa.actions.flow")

        # Bind the mixin method onto a lightweight stand-in engine.
        class DummyEngine:
            sound_alert_audio_path = ""
            log_level = 0

            _play_audio_via_winmm = staticmethod(lambda path: False)

        DummyEngine._play_sound_alert = af.ActionsFlowMixin._play_sound_alert

        # Should complete without raising even with no real audio backend.
        DummyEngine._play_sound_alert(DummyEngine(), "")
        DummyEngine._play_sound_alert(DummyEngine(), "nonexistent_file.wav")


# --------------------------------------------------------------------------- #
# 功能3: 直到条件成立 —— 区域不变模式
# --------------------------------------------------------------------------- #
class UntilRegionStableTests(unittest.TestCase):
    def test_region_stable_mode_registered(self):
        from fukua_rpa.constants import UNTIL_CONDITION_MODES

        self.assertIn("区域不变", UNTIL_CONDITION_MODES)

    def test_region_stable_summary(self):
        from fukua_rpa.task_model import until_condition_summary

        data = {
            "until_cond1_en": True,
            "until_cond1_mode": "区域不变",
            "until_cond1_region": "10,20,30,40",
            "until_false_jump": "0",
            "until_true_jump": "0",
        }
        summary = until_condition_summary(data)
        self.assertIn("区域不变", summary)
        self.assertIn("10,20,30,40", summary)

    def test_region_stable_requires_region(self):
        from fukua_rpa.validation import TaskListValidator

        class Stub:
            def _add_template_budget(self, *a, **k):
                return None

        stub = Stub()
        base = {
            "type": 15.0,
            "step_id": "X",
            "until_cond1_en": True,
            "until_cond1_mode": "区域不变",
            "until_cond1_region": "",
            "until_logic": "全部满足",
            "until_false_jump": "0",
            "until_true_jump": "0",
            "until_max_checks": "0",
            "until_max_seconds": "0",
            "until_on_limit": "继续下一步",
        }
        err = TaskListValidator._validate_until_task(stub, base, 1, 5, [1.0], False)
        self.assertIsNotNone(err)
        self.assertIn("区域", err)

        with_region = dict(base, until_cond1_region="10,20,30,40")
        err2 = TaskListValidator._validate_until_task(
            stub, with_region, 1, 5, [1.0], False
        )
        self.assertTrue(err2 is None or "区域" not in err2)


if __name__ == "__main__":
    unittest.main()
